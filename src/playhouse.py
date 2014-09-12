# Playhouse: Making buildings into interactive displays using remotely controllable lights.
# Copyright (C) 2014  John Eriksson, Arvid Fahlström Myrman, Jonas Höglund,
#                     Hannes Leskelä, Christian Lidström, Mattias Palo,
#                     Markus Videll, Tomas Wickman, Emil Öhman.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""A `Tornado <http://www.tornadoweb.org/>`_-based Python library for communicating
with Philips Hue bridges.

This library exposes the Philips Hue API, normally a JSON API, as an object oriented Python API.
These methods require that the `IOLoop <tornado.ioloop.IOLoop>` returned by `tornado.ioloop.IOLoop.current`
is running in order to function.
"""
import copy
import colorsys
import collections
import datetime
import errno
import itertools
import json
import logging
import re
import socket
from xml.etree import ElementTree

import tornado.concurrent
import tornado.escape
import tornado.gen
import tornado.httpclient
import tornado.iostream

try:
    import tornado.curl_httpclient
    tornado.httpclient.AsyncHTTPClient.configure(tornado.curl_httpclient.CurlAsyncHTTPClient)
except ImportError:
    logging.warning("Couldn't import CurlAsyncHTTPClient, reverting to slow default implementation")


class TaskTimedOutException(Exception):
    pass

class NoBridgeFoundException(Exception):
    pass

class BridgeAlreadyAddedException(Exception):
    pass

class OutsideGridException(Exception):
    pass

class NoBridgeAtCoordinateException(Exception):
    pass

class BulbNotResetException(Exception):
    pass

class HueAPIException(Exception):
    def __init__(self, error, bridge):
        super().__init__("{}: {}".format(error["error"]["address"], error["error"]["description"]))
        self.address = error["error"]["address"]
        self.description = error["error"]["description"]
        self.type = error["error"]["type"]
        self.bridge = bridge

class UnauthorizedUserException(HueAPIException):
    pass
class BodyContainsInvalidJSONException(HueAPIException):
    pass
class ResourceNotAvailableException(HueAPIException):
    pass
class MethodNotAvailableException(HueAPIException):
    pass
class MissingParameterException(HueAPIException):
    pass
class ParameterNotAvailableException(HueAPIException):
    pass
class InvalidValueException(HueAPIException):
    pass
class ReadOnlyParameterException(HueAPIException):
    pass
class NoLinkButtonPressedException(HueAPIException):
    pass
class DeviceIsOffException(HueAPIException):
    pass
class CouldNotCreateGroupException(HueAPIException):
    pass
class CouldNotAddToGroupException(HueAPIException):
    pass
class InternalErrorException(HueAPIException):
    pass

HUE_ERRORS = {
    1: UnauthorizedUserException,
    2: BodyContainsInvalidJSONException,
    3: ResourceNotAvailableException,
    4: MethodNotAvailableException,
    5: MissingParameterException,
    6: ParameterNotAvailableException,
    7: InvalidValueException,
    8: ReadOnlyParameterException,
    101: NoLinkButtonPressedException,
    201: DeviceIsOffException,
    301: CouldNotCreateGroupException,
    302: CouldNotAddToGroupException,
    901: InternalErrorException
}

class UnknownBridgeException(Exception):
    def __init__(self, mac):
        super().__init__()
        self.mac = mac



class ExceptionCatcher(tornado.gen.YieldPoint):
    """Runs multiple asynchronous operations in parallel until each operation
    has either completed or raised an exception.

    Takes a dictionary of ``Tasks`` or other ``YieldPoints`` and returns a
    (results, exceptions) tuple, where results and exceptions are dictionaries
    with the same keys as the given dictionary, and the operation result or
    the exception object that the operation raised respectively as values.
    """
    def __init__(self, children):
        self.children = children
        for k, v in self.children.items():
            if isinstance(v, tornado.concurrent.Future):
                self.children[k] = tornado.gen.YieldFuture(v)
        assert all(isinstance(v, tornado.gen.YieldPoint) for v in self.children.values())
        self.unfinished_children = set(self.children)
        self.exceptions = {}

    def start(self, runner):
        for k, v in self.children.items():
            try:
                v.start(runner)
            except Exception as e:
                self.exceptions[k] = e

    def is_ready(self):
        finished = list(itertools.takewhile(
            lambda k: self.children[k].is_ready(), self.unfinished_children))
        self.unfinished_children.difference_update(finished)
        return not self.unfinished_children

    def get_result(self):
        results = {}
        for k, v in self.children.items():
            try:
                results[k] = v.get_result()
            except Exception as e: # pylint: disable=broad-except
                self.exceptions[k] = e
        return results, self.exceptions


class TimeoutTask(tornado.gen.YieldPoint):
    def __init__(self, func, *args, timeout=2, **kwargs):
        assert "callback" not in kwargs
        self.args = args
        self.kwargs = kwargs
        self.timeout = timeout
        self.func = func
        self.has_timed_out = False
        self.timeout_handler = None
        self.ioloop = tornado.ioloop.IOLoop.current()

    def start(self, runner):
        self.runner = runner
        self.key = object()
        runner.register_callback(self.key)
        self.kwargs["callback"] = runner.result_callback(self.key)
        self.timeout_handler = self.ioloop.add_timeout(datetime.timedelta(seconds=self.timeout),
                                                       self.timed_out)
        self.func(*self.args, **self.kwargs)

    def timed_out(self):
        self.timeout_handler = None
        self.has_timed_out = True
        self.kwargs["callback"]()

    def is_ready(self):
        ir = self.runner.is_ready(self.key)
        if ir and not self.has_timed_out:
            self.ioloop.remove_timeout(self.timeout_handler)
            self.timeout_handler = None
        return self.runner.is_ready(self.key)

    def get_result(self):
        res = self.runner.pop_result(self.key)
        if not self.has_timed_out:
            return res
        else:
            raise TaskTimedOutException


class Bridge:

    # pylint: disable=too-many-instance-attributes
    """Instances of the Bridge handle a connection to a specific Hue bridge.

    Hue bridges communicate through a RESTful JSON API through TCP port 80.
    This class wraps this RESTful interface and provides a Python API for
    convenient Hue bridge operation. The functions for setting the current
    state of a particular lamp use the Hue light state names, see
    http://developers.meethue.com/1_lightsapi.html#16_set_light_state for reference.
    """
    ignoredkeys = {"transitiontime", "alert", "effect", "colormode", "reachable"}

    @tornado.gen.coroutine
    def __new__(cls, ipaddress, username=None, defaults=None, timeout=2):
        """Create a new Bridge object. Example usage::

            @tornado.gen.coroutine
            def turn_off_lights():
                future = Bridge("192.168.0.105", username="mysecretusername",
                                defaults={"transitiontime": 10})
                bridge = yield future
                yield bridge.set_group(0, on=False})

            ioloop.run_sync(turn_off_lights)

        :param str ipaddress: The IP address for this bridge
        :param str username: The username for this bridge as described in the Hue documentation.
                             Required for most commands.
        :param dict defaults: A dictionary containing default state changes to be sent with any
                              state-changing commands.
        :param int timeout: The time in seconds to wait for any requests to the Hue bridge
                            to complete.
        :return: A `tornado.concurrent.Future` that resolves to a bridge object when completed.
        :raises: :exc:`NoBridgeFoundException` if no bridge was found at the given IP address.
        """
        self = super(Bridge, cls).__new__(cls)

        self.defaults = defaults if defaults is not None else {"transitiontime": 0}
        self.username = username
        self.ipaddress = ipaddress
        self.client = tornado.httpclient.AsyncHTTPClient()
        self.timeout = timeout
        self.light_data = collections.defaultdict(dict)
        self.groups = collections.defaultdict(list)

        self.name = None
        self.mac = None
        self.gateway = None
        self.netmask = None

        self.logged_in = False

        try:
            assert (yield self.send_request("GET", "/config",
                                            force_send=True))['name'] == "Philips hue"

            # assume Philips Hue bridge from here on

            res = yield self.http_request("GET", "/description.xml")

            et, ns = parse_description(res.buffer)
            self.serial_number = et.find('./default:device/default:serialNumber',
                                           namespaces=ns).text

        except (ValueError, UnicodeDecodeError, AssertionError, tornado.httpclient.HTTPError):
            raise NoBridgeFoundException("{}: not a Philips Hue bridge".format(ipaddress))

        yield self.update_info()

        return self

    @tornado.gen.coroutine
    def http_request(self, method, url, body=None, timeout=None):
        """Send an HTTP request to the bridge.

        :param str method: HTTP request method (POST/GET/PUT/DELETE)
        :param str url: The URL to send this request to.
        :param str body: HTTP POST request body.
        :param int timeout: The time to wait for this request to complete. Defaults to
                            the timeout supplied to the `Bridge` constructor.
        :return: A `tornado.concurrent.Future` that resolves to the response from the bridge
                 when complete.
        :rtype: `tornado.httpclient.HTTPResponse` if the HTTP request failed.
        :raises: `tornado.httpclient.HTTPError` if the HTTP request failed.
        """
        logging.debug("Sending request %s %s (data: %s) to %s",
                      method, url, body, self.ipaddress)
        if timeout is None:
            timeout = self.timeout
        response = yield self.client.fetch("http://{}{}".format(self.ipaddress, url),
                                           method=method, body=body, request_timeout=timeout)
        return response

    @tornado.gen.coroutine
    def send_raw(self, method, url, body=None, timeout=None):
        """Send an HTTP request to the bridge, automatically parsing the returned JSON.

        :param str method: HTTP request method (POST/GET/PUT/DELETE).
        :param str url: The URL to send this request to.
        :param dict body: HTTP POST request body as a Python dictionary. The dictionary
                          will be converted to a JSON string.
        :param int timeout: The time to wait for this request to complete. Defaults to
                            the timeout supplied to the `Bridge` constructor.
        :return: A `tornado.concurrent.Future` that resolves to the response from the bridge,
                 converted from JSON to a Python dictionary, when complete.
        :rtype: `dict`
        :raises: `tornado.httpclient.HTTPError` if the HTTP request failed.

                 `HueAPIException` if the Hue API returned an error.
        """
        if body is not None:
            body = json.dumps(body)
        elif method in ("POST", "PUT"): # the curl http client doesn't accept body=None for POST/PUT
            body = ''

        logging.debug("Sending %s %s request to %s: %s", method, url, self.ipaddress, body)
        res = yield self.http_request(method, url, body, timeout)
        if res is None:
            return
        res = tornado.escape.json_decode(res.body)
        logging.debug("Got %s %s response from %s: %s", method, url, self.ipaddress, res)

        if type(res) is list:
            for item in res:
                if "error" in item:
                    raise HUE_ERRORS.get(item["error"]["type"], HueAPIException)(item, self)

        return res

    def send_request(self, method, url, body=None, timeout=None, force_send=False):
        """Send an HTTP request to the bridge using this `Bridge` instance's `username`.

        :param str method: HTTP request method (POST/GET/PUT/DELETE).
        :param str url: The URL to send this request to. ``/api/`` followed by the username
                        is automatically prepended to the URL.
        :param dict body: HTTP POST request body as a Python dictionary. The dictionary
                          will be converted to a JSON string.
        :param int timeout: The time to wait for this request to complete. Defaults to
                            the timeout supplied to the `Bridge` constructor.
        :param bool force_send: By default no request will be attempted and an
                                `UnauthorizedUserException` will be raised if no username is set;
                                setting this parameter to `True` will force a request using
                                a dummy username (``none``).
        :return: A `tornado.concurrent.Future` that resolves to the response from the bridge,
                 converted from JSON to a Python dictionary, when complete.
        :rtype: `dict`
        :raises: `tornado.httpclient.HTTPError` if the HTTP request failed.

                 `HueAPIException` if the Hue API returned an error.

        """
        username = self.username
        if username is None and not force_send:
            raise UnauthorizedUserException({"error": {"type": 1, "address": url,
                                                       "description": "unauthorized user"}}, self)
        elif username is None:
            username = "none" # dummy username guaranteed to be invalid (too short)

        return self.send_raw(method, "/api/{}{}".format(username, url), body, timeout)

    def _set_state(self, url, args):
        return self.send_request("PUT", url, body=args)

    def _state_preprocess(self, args):
        defs = self.defaults.copy()
        defs.update(args)

        if 'rgb' in defs:
            hue, sat, lum = rgb_to_hsl(*defs['rgb'])
            defs['hue'] = int(hue * 65536 / 360)
            defs['sat'] = sat
            del defs['rgb']

        return defs

    def set_state(self, i, **args):
        """Set state of a particular lamp.

        :param int i: ID number for the light whose state to change.
        :param args: Hue state changes. A full list of allowed state change can be found at
                     http://developers.meethue.com/1_lightsapi.html#16_set_light_state.
        :return: A `tornado.concurrent.Future` that resolves to the response from the bridge,
                 converted from JSON to a Python dictionary, when complete.
        :rtype: `dict`
        :raises: `tornado.httpclient.HTTPError` if the HTTP request failed.

                 `HueAPIException` if the Hue API returned an error.
        """
        args = self._state_preprocess(args)

        # Remove unnecessary commands
        state = self.light_data[i]
        final_send = dict()
        for k, v in args.items():
            if k in self.ignoredkeys or k not in state or state[k] != v:
                final_send[k] = v
                if k not in self.ignoredkeys:
                    state[k] = v
            elif k in state and state[k] == v:
                pass # Do not include this redundant command
        #print("Started with:" + str(args))
        #print("Reduced to:" + str(final_send))

        return self._set_state('/lights/{}/state'.format(i), final_send)

    def set_group(self, i, **args):
        """Set the state of a given lamp group.

        :param int i: ID for the group whose state to change.
        :param args: Hue state changes. A full list of allowed state changes can be found at
                     http://developers.meethue.com/1_lightsapi.html#16_set_light_state.
        :return: A `tornado.concurrent.Future` that resolves to the response from the bridge,
                 converted from JSON to a Python dictionary, when complete.
        :rtype: `dict`
        :raises: `tornado.httpclient.HTTPError` if the HTTP request failed.

                 `HueAPIException` if the Hue API returned an error.
        """

        args = self._state_preprocess(args)
        keys = self.light_data.keys() if i == 0 else self.groups[i]
        for k, v in args.items():
            if k in self.ignoredkeys:
                continue
            else:
                for lamp in keys:
                    self.light_data[lamp][k] = v

        return self._set_state('/groups/{}/action'.format(i), args)

    @tornado.gen.coroutine
    def create_group(self, lights, name=None):
        """Create a new group for this bridge.

        :param list lights: a list of lamp IDs (`int`).
        :param str name: Name for this group, optional argument.
        :return: A `tornado.concurrent.Future` that resolves to the response from the bridge,
                 converted from JSON to a Python dictionary, when complete.
        :rtype: `dict`
        :raises: `tornado.httpclient.HTTPError` if the HTTP request failed.

                 `HueAPIException` if the Hue API returned an error.
        """
        body = {'lights':[str(x) for x in lights]}
        if name is not None:
            body['name'] = name
        res = yield self.send_request("POST", "/groups", body)
        match = re.match(r"/groups/(\d+)", res[0]["success"]["id"])
        group = int(match.group(1))
        self.groups[group] = lights.copy()
        return res

    @tornado.gen.coroutine
    def delete_group(self, i):
        """Delete a new group from this bridge.

        :param int i: ID number for the group to be removed.
        :return: A `tornado.concurrent.Future` that resolves to the response from the bridge,
                 converted from JSON to a Python dictionary, when complete.
        :rtype: `dict`
        :raises: `tornado.httpclient.HTTPError` if the HTTP request failed.

                 `HueAPIException` if the Hue API returned an error.
        """
        res = (yield self.send_request("DELETE", "/groups/{}".format(i)))[0]
        del self.groups[i]
        return res

    def get_lights(self):
        """Fetch a list of all lights known to the bridge.

        :return: A `tornado.concurrent.Future` that resolves to the response from the bridge,
                 converted from JSON to a Python dictionary, when complete.
        :rtype: `dict`
        :raises: `tornado.httpclient.HTTPError` if the HTTP request failed.
                 `HueAPIException` if the Hue API returned an error.
        """
        return self.send_request("GET", "/lights")

    def search_lights(self):
        """Start a new light search.

        :return: A `tornado.concurrent.Future` that resolves to the response from the bridge,
                 converted from JSON to a Python dictionary, when complete.
        :rtype: `dict`
        :raises: `tornado.httpclient.HTTPError` if the HTTP request failed.

                 `HueAPIException` if the Hue API returned an error.
        """
        return self.send_request("POST", "/lights")

    def get_new_lights(self):
        """Get a list of all lights found after running `search_lights`.

        :return: A `tornado.concurrent.Future` that resolves to the response from the bridge,
                 converted from JSON to a Python dictionary, when complete.
        :rtype: `dict`
        :raises: `tornado.httpclient.HTTPError` if the HTTP request failed.
                 `HueAPIException` if the Hue API returned an error.
        """
        return self.send_request("GET", "/lights/new")

    def get_bridge_info(self):
        """ TODO TODO TODO

        :return: A `tornado.concurrent.Future` that resolves to the response from the bridge,
                 converted from JSON to a Python dictionary, when complete.
        :rtype: `dict`
        :raises: `tornado.httpclient.HTTPError` if the HTTP request failed.

                 `HueAPIException` if the Hue API returned an error.
        """
        return self.send_request("GET", "/")

    @tornado.gen.coroutine
    def create_user(self, devicetype, username=None):
        """Create a new user for this bridge.

        A valid user name is required to execute most commands. In order to create a new user,
        the link button on the Hue bridge must be pressed before this command is executed.

        `update_info` will be called automatically after setting the username.

        :param str devicetype: The 'type' of user. Should be related to the application
                               for which this user is created.
        :param str username: The new user name. If not supplied a random user name will
                             be generated by the bridge.
        :return: A `tornado.concurrent.Future` that resolves to the new username when complete.
        :rtype: `str`
        :raises: `tornado.httpclient.HTTPError` if the HTTP request failed.

                 `NoLinkButtonPressedException` if the link button was not pressed.

                 `HueAPIException` if the Hue API returned an error.
        """
        body = {'devicetype': devicetype}
        if username is not None:
            body['username'] = username
        res = (yield self.send_raw("POST", "/api", body))[0]
        yield self.set_username(res['success']['username'])
        return self.username

    def set_username(self, username):
        """Set the user name for this bridge.

        A valid user name is required to execute most commands.

        `update_info` will be automatically after setting the username.

        :param str username: The new user name.
        :return: A `tornado.concurrent.Future` that completes when `update_info` has finished
                 updating the metadata.
        :raises: `tornado.httpclient.HTTPError` if the HTTP request failed.

                 `HueAPIException` if the Hue API returned an error.
        """
        self.username = username
        return self.update_info()

    def set_defaults(self, defaults):
        """Set the default state changes to be included with any calls to state-changing operations.

        These changes are included whenever the state is set, unless overridden
        by the provided state change argument.

        :param dict defaults: The new default state changes.
        """
        self.defaults = defaults

    @tornado.gen.coroutine
    def update_info(self):
        """Update the Hue bridge metadata.

        If the current `username` is valid, `logged_in` will be set to `True` and
        `gateway`, `netmask`, `name` and `mac` will be updated; otherwise, `logged_in`
        will be set to `False` and the remaining attributes to `None`.

        This method is called automatically by `create_user` and `set_username`.

        :return: A `tornado.concurrent.Future` that completes when the update attempt completes.
        :raises: `tornado.httpclient.HTTPError` if the HTTP request failed.

                 `HueAPIException` if the Hue API returned an error.
        """
        try:
            data = yield self.send_request("GET", "/")
            info = data["config"]

            self.logged_in = True
            self.gateway = info['gateway']
            self.netmask = info['netmask']
            self.name = info['name']
            self.mac = info['mac']

            self.light_data.clear()
            self.groups.clear()
            for lamp_num, lamp in data["lights"].items():
                state = lamp["state"]
                for k, v in state.items():
                    if k in self.ignoredkeys:
                        continue

                    self.light_data[int(lamp_num)][k] = v
            for group_num, group in data["groups"].items():
                lights = group["lights"]
                self.groups[int(group_num)] = [int(x) for x in lights]
        except UnauthorizedUserException:
            if self.username is not None:
                logging.warning("Couldn't send request to %s using username %s",
                                self.serial_number, self.username)
            self.logged_in = False
            self.gateway = None
            self.netmask = None
            self.name = None
            self.mac = None

    @tornado.gen.coroutine
    def reset_nearby_bulb(self):
        """Attempts to reset a Hue light bulb in proximity to the bridge.

        For best effect, place the bulb in close proximity to the bridge, and make
        sure that there are no other turned on Hue lights nearby.

        :return: A `tornado.concurrent.Future` that completes when a bulb has been successfully
                 reset, or when the reset attempt has failed.
        :raises: `BulbNotResetException` if no bulb was reset.
        """
        sock = socket.socket()
        stream = tornado.iostream.IOStream(sock)
        try:
            logging.debug("Connecting to %s at port 30000", self.ipaddress)
            yield TimeoutTask(stream.connect, (self.ipaddress, 30000))
            logging.debug("Sending [Link,Touchlink]")
            yield TimeoutTask(stream.write, b'[Link,Touchlink]')

            echo = yield TimeoutTask(stream.read_until, b'\n')
            logging.debug("Got %s as an echo response", echo)

            if echo != b'[Link,Touchlink]\n':
                logging.debug("Echo response was not [Link,Touchlink]: '%s'", echo.strip())
                raise BulbNotResetException

            response = (yield TimeoutTask(stream.read_until, b'\n', timeout=10)).decode()
            logging.debug("Got reset response %s", response)

            if response == '[Link,Touchlink,failed]\n':
                logging.debug("Bridge failed to reset a bulb")
                raise BulbNotResetException

            match = re.match(r"\[Link,Touchlink,success,NwkAddr=([^,]+),pan=([^]]+)\]\n",
                             response)
            if match:
                logging.debug("Reset bulb has NwkAddr %s and pan %s", *match.groups())
                return match.groups()
            else:
                logging.debug("Unexpected reset response %s", response)
                raise BulbNotResetException
        except TaskTimedOutException:
            logging.debug("Reset attempt timed out")
            raise BulbNotResetException
        finally:
            logging.debug("Closing stream used for bulb reset")
            stream.close()


class LightGrid:
    """Keeps track of several bridges, abstracting access to individual lights."""
    def __init__(self, usernames=None, grid=None, buffered=False, defaults=None,
                 assert_reachable=True):
        """Initializes the `LightGrid`.

        :param dict usernames: Dictionary of MAC address -> username pairs. When a bridge is
                               added without specifying a username, and the bridge's MAC address
                               is present in the ``usernames`` dictionary, the username of the
                               bridge will automatically be set to the corresponding value
                               in the dictionary.
        :param list grid: A list of lists of ``(mac_address, light_id)`` tuples, specifying
                          a light belonging to a bridge with the given mac address.
                          Maps ``(x, y)`` coordinates to the light specified at ``grid[y][x]``.
        :param bool buffered: If `True`, calls to `set_state` will not prompt an immediate
                              request to the bridge in question; to send the buffered state
                              changes, call `commit`.
        :param dict defaults: Additional instructions to include in each state change
                              request to a bridge.
        :param bool assert_reachable: If `True`, the grid will occasionally check that all
                                      bridges are reachable; any unreachable bridge will be removed.
                                      Setting this parameter to `True` is equivalent to manually
                                      calling the `assert_reachable` method.
        """
        self.defaults = defaults if defaults is not None else {}
        self.bridges = {}
        self.usernames = usernames if usernames is not None else {}
        self.buffered = buffered
        self._buffer = collections.defaultdict(dict)

        self.grid = []
        self.height = 0
        self.width = 0

        self.set_grid(grid if grid is not None else [])

        self.running = True

        if assert_reachable:
            self.assert_reachable()

    @tornado.gen.coroutine
    def add_bridge(self, ip_address_or_bridge, username=None):
        """Add a new bridge to this light grid.

        :param ip_address_or_bridge: Can be either a `Bridge` object, or an IP address to a bridge,
                                     in which case a new `Bridge` object will be created from
                                     the IP address.
        :param str username: User name for this bridge. User names are required
                             to perform most bridge commands. Ignored if ``ip_address_or_bridge``
                             is a `Bridge` instance.
        :return: A `tornado.concurrent.Future` that resolves to the `Bridge` instance added to
                 the `LightGrid` when complete.
        :rtype: `Bridge`
        :raises: `NoBridgeFoundException` if no bridge was found at the given IP address.

                 `BridgeAlreadyAddedException` if the `Bridge` is already present
                 in the `LightGrid`.
        """
        if isinstance(ip_address_or_bridge, Bridge):
            bridge = ip_address_or_bridge
        else:
            bridge = yield Bridge(ip_address_or_bridge, username, self.defaults)

        if self.has_bridge(bridge):
            raise BridgeAlreadyAddedException()

        if bridge.username is None and bridge.serial_number in self.usernames:
            yield bridge.set_username(self.usernames[bridge.serial_number])
        self.bridges[bridge.serial_number] = bridge
        return bridge

    def has_bridge(self, mac_or_bridge):
        """Check whether this `LightGrid` has a bridge with the given MAC address
        stored in its configuration.

        If a `Bridge` instance is given, compares `Bridge.serial_number` of
        the provided instance to the known bridges.

        :param mac_or_bridge: Either a MAC address or a `Bridge` object.
        :returns: `True` if a `Bridge` instance with the given MAC address is present
                  in the `LightGrid`; `False` otherwise.
        """
        if isinstance(mac_or_bridge, Bridge):
            mac = mac_or_bridge.serial_number
        else:
            mac = mac_or_bridge

        return mac in self.bridges

    def set_usernames(self, usernames):
        """Sets the username map for this light grid.

        :param dict usernames: Dictionary of MAC address -> username pairs. See the ``usernames``
                               parameter of `__init__`.
        """
        self.usernames = usernames

    def set_grid(self, grid):
        """Set the grid that maps coordinates to ``(mac_address, light_id)`` pairs.

        :param list grid: A list of lists of ``(mac_address, light_id)`` tuples. See the ``grid``
                          parameter of `__init__`.
        """
        self.grid = grid
        self.height = len(self.grid)
        self.width = max(len(x) for x in self.grid) if self.height > 0 else 0

    @tornado.gen.coroutine
    def set_state(self, x, y, **args):
        # pylint: disable=invalid-name
        """Set the state for the light at the given coordinate.

        If this grid is buffered, the state will not be sent to the lamp immediately; call
        `commit` to send the buffered state changes. See :exc:`HueAPIException`.

        :param int x: X coordinate.
        :param int y: Y coordinate.
        :param args: State argument, see the Philips Hue documentation.
        :return: A `tornado.concurrent.Future` that completes when `set_state` has finished.
        :raises: `tornado.httpclient.HTTPError` if the grid is unbuffered and
                 the HTTP request failed.

                 `HueAPIException` if the grid is unbuffered and the Hue API returned an error.
        """

        self._buffer[(x, y)].update(args)

        if not self.buffered:
            exceptions = yield self.commit()
            if len(exceptions) > 0:
                # pass on first (and only, since this grid isn't buffered) exception
                raise next(iter(exceptions.values()))

    @tornado.gen.coroutine
    def set_all(self, **args):
        """Set the state of every light known to every bridge added to this `LightGrid`.

        :param args: State argument, see the Philips Hue documentation.
        """
        _, exc = yield ExceptionCatcher({bridge.serial_number: bridge.set_group(0, **args)
                                           for bridge in self.bridges.values()})
        return exc


    @tornado.gen.coroutine
    def commit(self):
        """Commit buffered state changes to the lamps.

        This method is automatically called whenever `set_state` is called if the ``buffered``
        parameter of `__init__` was set to `False`.

        :return: A `tornado.concurrent.Future` that resolves to a dictionary consisting of
                 ``(x, y)`` coordinate -> exception object key/value pairs, where a given
                 exception object is associated with the operation of changing the state
                 of the light at the corresponding coordinate.
        :rtype: `dict`
        :raises: `tornado.httpclient.HTTPError` if the HTTP request failed.
                 `HueAPIException` if the Hue API returned an error.
        """

        futures = {}
        exceptions = {}
        for (x, y), changes in self._buffer.items():
            try:
                if x >= self.width or y >= self.height or self.grid[y][x] is None:
                    raise OutsideGridException

                mac, light = self.grid[y][x]
                if mac not in self.bridges:
                    raise NoBridgeAtCoordinateException

                bridge = self.bridges[mac]
                futures[(x, y)] = bridge.set_state(light, **changes)
            except (OutsideGridException, NoBridgeAtCoordinateException) as e:
                exceptions[(x, y)] = e

        self._buffer.clear()

        res, exc = yield ExceptionCatcher(futures)
        exceptions.update(exc)
        logging.debug("Got results %s", res)
        logging.debug("Got exceptions %s", exceptions)
        return exceptions

    @tornado.gen.coroutine
    def assert_reachable(self):
        """Coroutine that runs indefinitely, periodically ensuring that all bridges are reachable.

        If a bridge cannot be reached three times in a row, it is removed from the `LightGrid`.
        Upon removing a bridge, `discover` will be run once as a last-ditch effort to find
        the lost bridge.

        This method is automatically called if the ``assert_reachable`` parameter of `__init__`
        was set to `True`.
        """
        strikes = collections.defaultdict(int)

        while self.running:
            try:
                yield tornado.gen.Task(tornado.ioloop.IOLoop.current().add_timeout,
                                       datetime.timedelta(seconds=20))

                macs_to_remove = set()
                for mac, bridge in self.bridges.items():
                    logging.debug("Pinging bridge %s at %s",
                                  mac, bridge.ipaddress)
                    try:
                        res = yield bridge.send_request("GET", "/config",
                                                        timeout=5, force_send=True)
                        if res['name'] != 'Philips hue':
                            raise ValueError
                        strikes[bridge.ipaddress] = 0
                    except (ValueError, TypeError, KeyError,
                            UnicodeError, tornado.httpclient.HTTPError):
                        strikes[bridge.ipaddress] += 1
                        logging.warning("Couldn't reach bridge at %s; strikes: %s/3",
                                        bridge.ipaddress, strikes[bridge.ipaddress])

                        if strikes[bridge.ipaddress] >= 3:
                            macs_to_remove.add(mac)
                            del strikes[bridge.ipaddress]

                for mac in macs_to_remove:
                    logging.error("Removing bridge %s at %s", mac, bridge.ipaddress)
                    del self.bridges[mac]

                if len(macs_to_remove) > 0:
                    logging.info("Attempting to find lost bridges")
                    new_bridges = yield discover()
                    logging.info("Found bridges: %s", {b.serial_number: b.ipaddress
                                                       for b in new_bridges})

                    for bridge in new_bridges:
                        if bridge.serial_number in macs_to_remove:
                            logging.info("Re-adding %s at %s",
                                         bridge.serial_number, bridge.ipaddress)
                            self.add_bridge(bridge)

            except Exception:
                logging.exception("Encountered exception while pinging bridges")
                
default_lamp = {
    "hue":0, "sat":0, "bri":255, "on":True
}            

class DummyLightGrid:
    """Keeps track of several bridges, abstracting access to individual lights."""
    def __init__(self, width, height, buffered=False, defaults=None):
        """Initializes the `LightGrid`.

        :param dict usernames: Dictionary of MAC address -> username pairs. When a bridge is
                               added without specifying a username, and the bridge's MAC address
                               is present in the ``usernames`` dictionary, the username of the
                               bridge will automatically be set to the corresponding value
                               in the dictionary.
        :param list grid: A list of lists of ``(mac_address, light_id)`` tuples, specifying
                          a light belonging to a bridge with the given mac address.
                          Maps ``(x, y)`` coordinates to the light specified at ``grid[y][x]``.
        :param bool buffered: If `True`, calls to `set_state` will not prompt an immediate
                              request to the bridge in question; to send the buffered state
                              changes, call `commit`.
        :param dict defaults: Additional instructions to include in each state change
                              request to a bridge.
        :param bool assert_reachable: If `True`, the grid will occasionally check that all
                                      bridges are reachable; any unreachable bridge will be removed.
                                      Setting this parameter to `True` is equivalent to manually
                                      calling the `assert_reachable` method.
        """
        self.defaults = defaults if defaults is not None else {}
        
        self.buffered = buffered
        self._buffer = collections.defaultdict(dict)

        self._lamp_data = [[copy.copy(default_lamp) for w in range(width)] for h in range(height)]
        self.height = height
        self.width = width

        self.running = True

    def _state_preprocess(self, args):
        defs = self.defaults.copy()
        defs.update(args)

        if 'rgb' in defs:
            hue, sat, val = colorsys.rgb_to_hsv(*[x/255 for x in defs['rgb']])
            defs['hue'] = int(hue*65536)
            defs['sat'] = int(sat*255)
            defs['bri'] = int(val*255)
            del defs['rgb']

        return defs


    @tornado.gen.coroutine
    def set_state(self, x, y, **args):
        # pylint: disable=invalid-name
        """Set the state for the light at the given coordinate.

        If this grid is buffered, the state will not be sent to the lamp immediately; call
        `commit` to send the buffered state changes. See :exc:`HueAPIException`.

        :param int x: X coordinate.
        :param int y: Y coordinate.
        :param args: State argument, see the Philips Hue documentation.
        :return: A `tornado.concurrent.Future` that completes when `set_state` has finished.
        :raises: `tornado.httpclient.HTTPError` if the grid is unbuffered and
                 the HTTP request failed.

                 `HueAPIException` if the grid is unbuffered and the Hue API returned an error.
        """
        args = self._state_preprocess(args)
        self._buffer[(x, y)].update(args)

        if not self.buffered:
            exceptions = yield self.commit()
            if len(exceptions) > 0:
                # pass on first (and only, since this grid isn't buffered) exception
                raise next(iter(exceptions.values()))

    @tornado.gen.coroutine
    def set_all(self, **args):
        args = self._state_preprocess(args)
    
        for x in range(self.width):
            for y in range(self.height):
                self._buffer[(x,y)].update(args)
                
                
        return self.commit()


    @tornado.gen.coroutine
    def commit(self):
        """Commit buffered state changes to the lamps.

        This method is automatically called whenever `set_state` is called if the ``buffered``
        parameter of `__init__` was set to `False`.

        :return: A `tornado.concurrent.Future` that resolves to a dictionary consisting of
                 ``(x, y)`` coordinate -> exception object key/value pairs, where a given
                 exception object is associated with the operation of changing the state
                 of the light at the corresponding coordinate.
        :rtype: `dict`
        :raises: `tornado.httpclient.HTTPError` if the HTTP request failed.
                 `HueAPIException` if the Hue API returned an error.
        """

        futures = {}
        exceptions = {}
        for (x, y), changes in self._buffer.items():
            try:
                if x >= self.width or y >= self.height:
                    raise OutsideGridException

                self._lamp_data[y][x].update(changes)
                
            except OutsideGridException as e:
                exceptions[(x, y)] = e

        self._buffer.clear()

        return exceptions





class AsyncSocket(socket.socket):
    def __init__(self, *args):
        super().__init__(*args)
        self.setblocking(False)

        self._responses = []
        self._io_loop = tornado.ioloop.IOLoop.current()
        self._timeout = 0
        self._future = None
        self._timeout_handle = None

    def wait_for_responses(self, timeout=2):
        self._responses = []
        self._timeout = timeout
        self._future = tornado.concurrent.Future()

        self._io_loop.add_handler(self.fileno(), self._fetch_response, self._io_loop.READ)
        self._set_timeout()

        return self._future

    def _set_timeout(self):
        self._timeout_handle = self._io_loop.add_timeout(
            datetime.timedelta(seconds=self._timeout), self._on_timeout)

    def _fetch_response(self, _fd, _events):
        while True:
            try:
                self._responses.append(self.recvfrom(1024))
                self._io_loop.remove_timeout(self._timeout_handle)
                self._set_timeout()
            except socket.error as e:
                if e.args[0] not in (errno.EWOULDBLOCK, errno.EAGAIN):
                    raise
                return

    def _on_timeout(self):
        self._io_loop.remove_handler(self.fileno())
        self._future.set_result(self._responses)


@tornado.gen.coroutine
def discover(attempts=2, timeout=2):
    """Search for bridges on the local network.

    Uses UPnP discovery to query the network for bridges. Additionally a request is sent to
    `Philips' NUPnP API <http://www.meethue.com/api/nupnp>`_, which lists all bridges with the
    same external IP address as the client issuing the request.

    :param int attempts: Number of times to run the UPnP discovery.
    :param int timeout: Time in seconds to wait for a new response during a UPnP discovery
                        attempt before giving up.
    :return: A `tornado.concurrent.Future` that resolves to a list of `Bridge` instances
             when complete.
    :rtype: `list`
    """
    s = AsyncSocket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)

    message = b'M-SEARCH * HTTP/1.1\r\n'\
              b'HOST: 239.255.255.250:1900\r\n'\
              b'MAN: "ssdp:discover"\r\n'\
              b'ST: my:test\r\n'\
              b'MX: 3\r\n\r\n'

    locations = set()
    try:
        logging.info("Fetching bridges from meethue.com")
        nupnp_response = yield tornado.httpclient.AsyncHTTPClient().fetch(
            "http://www.meethue.com/api/nupnp", request_timeout=4)
        nupnp_bridges = tornado.escape.json_decode(nupnp_response.body)
        logging.debug("Response from meethue.com was %s", nupnp_bridges)
        locations.update(bridge['internalipaddress'] for bridge in nupnp_bridges)
    except tornado.httpclient.HTTPError:
        logging.info("Couldn't connect to meethue.com")

    for _ in range(attempts):
        logging.debug("Broadcasting %s", message)
        for _ in range(2):
            s.sendto(message, ("239.255.255.250", 1900))

        responses = yield s.wait_for_responses(timeout=timeout)
        for raw, (address, port) in responses:
            logging.debug("%s:%s says: %s", address, port, raw)

            # NOTE: we'll skip doing any checks in particular here,
            # and instead just rely on Bridge.__new__ to
            # correctly identify whether the address belongs to
            # a Hue bridge or not

            #response = io.BytesIO(raw)

            #logging.debug("Got response: %s", response)

            #response.makefile = lambda *args, **kwargs: response
            #header = http.client.HTTPResponse(response)
            #header.begin()

            #logging.debug("Response status was %s and location header was %s",
                            #header.status, header.getheader('location'))
            #if header.status == 200 and header.getheader('location') is not None:
                #logging.debug("Adding %s to list", address)
                #locations.add(address)

            locations.add(address)

    logging.debug("List of addresses is %s", locations)
    bridges = []
    for loc in locations:
        try:
            logging.debug("Attempting to find a bridge at %s", loc)
            bridges.append((yield Bridge(loc)))
            logging.debug("Bridge found")
        except NoBridgeFoundException:
            logging.debug("Bridge not found", exc_info=True)

    return bridges

def parse_description(document):
    root = None
    namespaces = {}
    for event, elem in ElementTree.iterparse(document, ("start", "start-ns")):
        if event == "start-ns":
            namespaces[elem[0] if elem[0] != '' else 'default'] = elem[1]
        elif event == "start":
            if root is None:
                root = elem

    return ElementTree.ElementTree(root), namespaces

# shamelessly stolen from the web server for backwards compatibility reasons
def rgb_to_hsl(r, g, b):
    # via http://en.wikipedia.org/wiki/HSL_and_HSV
    M, m = max(r,g,b), min(r,g,b)
    c = M - m

    if c == 0:
        hue = 0
    elif M == r: # ↓ may be <0, so use + and % to make sure that it is in [0,360]
        hue = ((g - b)/c * 360 + 360*6) % (360 * 6)
    elif M == g:
        hue = (b - r)/c * 360 + (360 * 2)
    elif M == b:
        hue = (r - g)/c * 360 + (360 * 4)

    hue /= 6
    lum = M/2 + m/2
    divisor = 2 * (lum if lum < 128 else 256 - lum)
    if divisor == 0:
        return 0, 0, 0
    sat = c / divisor * 255

    return int(hue), int(sat), int(lum)
