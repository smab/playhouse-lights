
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


class NoBridgeFoundException(Exception):
    pass

class BridgeAlreadyAddedException(Exception):
    pass

class OutsideGridException(Exception):
    pass

class NoBridgeAtCoordinateException(Exception):
    pass

class HueAPIException(Exception):
    def __init__(self, error):
        super().__init__("{}: {}".format(error["error"]["address"], error["error"]["description"]))
        self.address = error["error"]["address"]
        self.description = error["error"]["description"]
        self.type = error["error"]["type"]

class UnauthorizedUserException(HueAPIException):
    pass

class NoLinkButtonPressedException(HueAPIException):
    pass

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

    def start(self, runner):
        for v in self.children.values():
            v.start(runner)

    def is_ready(self):
        finished = list(itertools.takewhile(
            lambda k: self.children[k].is_ready(), self.unfinished_children))
        self.unfinished_children.difference_update(finished)
        return not self.unfinished_children

    def get_result(self):
        exceptions = {}
        results = {}
        for k, v in self.children.items():
            try:
                results[k] = v.get_result()
            except Exception as e: # pylint: disable=broad-except
                exceptions[k] = e
        return results, exceptions


class LineReader:

    def __init__(self):
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM, 0)
        self.stream = tornado.iostream.IOStream(self.socket)
        self.stream.set_close_callback(self._closed)

        self._future = None
        self._is_connected = False
        self._timeout_handler = None
        self._ioloop = tornado.ioloop.IOLoop.current()
        self._error = None

    def _set_future(self):
        if self._future is not None:
            raise Exception("LineReader already running")
        self._future = tornado.concurrent.Future()
        return self._future

    def _unset_future(self, result=None):
        self._future.set_result(result)
        self._future = None

    def _set_timeout(self, timeout):
        self._timeout_handler = self._ioloop.add_timeout(
            datetime.timedelta(seconds=timeout),
            self._timeout)

    def _unset_timeout(self):
        self._ioloop.remove_timeout(self._timeout_handler)

    def _timeout(self):
        self._error = socket.timeout()
        self.stream.close()

    def close(self):
        future = self._set_future()
        self.stream.close()
        return future

    def _closed(self):
        if self._future is not None and (self._error or not self._is_connected):
            self._future.set_exception(self._error or self.stream.error)
            self._future = None
        elif self._future is not None:
            self._unset_future()

    def connect(self, address, timeout=5):
        future = self._set_future()
        self.stream.connect(address, callback=self._connected)
        self._set_timeout(timeout)
        return future

    def _connected(self):
        self._unset_timeout()
        self._is_connected = True
        self._unset_future()

    def write(self, data):
        self.stream.write(data, callback=self._wrote)
        return self._set_future()

    def _wrote(self):
        self._unset_future()

    def read_until(self, delimiter=b'\n', timeout=5):
        future = self._set_future()
        self.stream.read_until(delimiter, callback=self._read)
        self._set_timeout(timeout)
        return future

    def _read(self, data):
        self._unset_timeout()
        self._unset_future(data)




class Bridge:

    # pylint: disable=too-many-instance-attributes
    """ 
    Instances of the Bridge handle a connection to a specific Hue bridge.
    
    Hue bridges communicate through a RESTful JSON API through TCP port 80. 
    This class wraps this RESTful interface and provides a Python API for
    convenient Hue bridge operation. The function for setting the current
    state of a particular lamp uses the Hue light state names, check
    http://developers.meethue.com/1_lightsapi.html#16_set_light_state for reference.
    """
    ignoredkeys = {"transitiontime", "alert", "effect", "colormode", "reachable"}

    @tornado.gen.coroutine
    def __new__(cls, ipaddress, username=None, defaults=None, timeout=2):
        """
        Create a new Bridge object.
        
        Args:
            ipaddress - The IP address for this bridge
            username - Username as described in Hue documentation. Required for almost all commands.
            defaults - A dictionary of "default" state changes. These changes are included whenever the state is set, unless overridden by the provided state change argument.
            timeout - Time until a HTTP request times out, in seconds.
        """
        #bridge = cls(ipaddress, username, defaults, timeout)
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
            if (yield self.send_request("GET", "/config",
                                        force_send=True))['name'] != "Philips hue":
                raise Exception()

            res = yield self.http_request("GET", "/description.xml")
            if res.code != 200:
                raise Exception()

            et, ns = parse_description(res.buffer)
            desc = et.find('./default:device/default:modelDescription', namespaces=ns)
            if desc.text != "Philips hue Personal Wireless Lighting":
                raise Exception()

            self.serial_number = et.find('./default:device/default:serialNumber',
                                           namespaces=ns).text
        except Exception:
            raise NoBridgeFoundException("{}: not a Philips Hue bridge".format(ipaddress))

        yield self.update_info()

        return self

    def __init__(self):
        pass

    def set_defaults(self, defaults):
        """
        Set the current "default" state changes. These changes are included whenever the state is set, unless overridden by the provided state change argument.
        
        Args:
            defaults: The new default state chances, as a dictionary.
        
        """
        self.defaults = defaults

    @tornado.gen.coroutine
    def http_request(self, method, url, body=None):
        """Send a HTTP request to the bridge.
        
        Args:
            method - HTTP request method (POST/GET/PUT/DELETE)
            url - The URL to send this request to.
            body - HTTP POST request body, as a string.
        """
        logging.debug("Sending request %s %s (data: %s) to %s",
                      method, url, body, self.ipaddress)
        response = yield self.client.fetch("http://{}{}".format(self.ipaddress, url),
                                           method=method, body=body, request_timeout=self.timeout)
        logging.debug("Response from bridge: %s", response)
        return response

    @tornado.gen.coroutine
    def send_raw(self, method, url, body=None):
        """Send a HTTP request to the bridge.
        
        Args:
            method - HTTP request method (POST/GET/PUT/DELETE)
            url - The URL to send this request to.
            body - HTTP POST request body, as a Python dictionary. This object will be converted to JSON.
        """
        if body is not None:
            body = json.dumps(body)

        res = yield self.http_request(method, url, body)
        if res is None:
            return

        res = tornado.escape.json_decode(res.body)

        exceptions = {
            1: UnauthorizedUserException,
            101: NoLinkButtonPressedException
        }
        if type(res) is list:
            for item in res:
                if "error" in item:
                    raise exceptions.get(item["error"]["type"], HueAPIException)(item)

        return res

    @tornado.gen.coroutine
    def send_request(self, method, url, body=None, force_send=False):
        """Send a HTTP request to the bridge. 
        
        Args:
            method - HTTP request method (POST/GET/PUT/DELETE)
            url - The URL to send this request to. Unlike the other HTTP
            request methods, this argument should only include the part of the URL which is after the username.
            body - HTTP POST request body, as a Python dictionary. This object will be converted to JSON.
        """
        username = self.username
        if username is None and not force_send:
            raise UnauthorizedUserException({"error": {"type": 1, "address": url,
                                                       "description": "unauthorized user"}})
        elif username is None:
            username = "none" # dummy username guaranteed to be invalid (too short)

        return (yield self.send_raw(method, "/api/{}{}".format(username, url), body))

    @tornado.gen.coroutine
    def _set_state(self, url, args):
        return (yield self.send_request("PUT", url, body=args))

    def _state_preprocess(self, args):
        defs = self.defaults.copy()
        defs.update(args)

        if 'rgb' in defs:
            defs['xy'] = rgb2xy(*defs['rgb'])
            del defs['rgb']

        return defs

    @tornado.gen.coroutine
    def set_state(self, i, **args):
        """
        Set state of a particular lamp.
        
        Args:
            i: ID number for light
            args: Hue state changes. A full list of allowed state change can be found in http://developers.meethue.com/1_lightsapi.html#16_set_light_state.
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

        return (yield self._set_state('/lights/{}/state'.format(i), final_send))

    @tornado.gen.coroutine
    def set_group(self, i, **args):
        """
        Set state of a particular lamp group.
        
        Args:
            i: ID number for group
            args: Hue state changes. A full list of allowed state change can be found in http://developers.meethue.com/1_lightsapi.html#16_set_light_state.
        """
    
        args = self._state_preprocess(args)
        keys = self.light_data.keys() if i == 0 else self.groups[i]
        for k, v in args.items():
            if k in self.ignoredkeys:
                continue
            else:
                for lamp in keys:
                    self.light_data[lamp][k] = v

        return (yield self._set_state('/groups/{}/action'.format(i), args))

    @tornado.gen.coroutine
    def get_lights(self):
        return (yield self.send_request("GET", "/lights"))

    @tornado.gen.coroutine
    def get_new_lights(self):
        return (yield self.send_request("GET", "/lights/new"))

    @tornado.gen.coroutine
    def search_lights(self):
        """Start a new light search"""
        return (yield self.send_request("POST", "/lights"))

    @tornado.gen.coroutine
    def reset_nearby_bulb(self):
        lr = LineReader()
        yield lr.connect((self.ipaddress, 30000))
        yield lr.write(b'[Link,Touchlink]')
        yield lr.read_until() # echo
        response = (yield lr.read_until(timeout=10)).decode()
        yield lr.close()
        return re.match(r"\[Link,Touchlink,success,NwkAddr=([^,]+),pan=([^]]+)\]\n",
                        response).groups()

    @tornado.gen.coroutine
    def get_bridge_info(self):
        return (yield self.send_request("GET", "/"))

    @tornado.gen.coroutine
    def set_username(self, username):
        """
        Set the user name for this bridge. A valid user name is required to execute most commands.
        
        Args:
            username - The new user name
        """
        self.username = username
        yield self.update_info()

    @tornado.gen.coroutine
    def create_user(self, devicetype, username=None):
        """
        Create a new user for this bridge. A valid user name is required to execute most commands.
        In order to create a new user, the link button on the Hue bridge must pressed before this
        command is executed.
        
        Arguments:
            devicetype - The 'type' of user. Should be related to the application for which this user is created.
            username - The new user name. Optional argument, a random user name will be generated by the bridge
        if a user name is not provided.
        """
        body = {'devicetype': devicetype}
        if username is not None:
            body['username'] = username
        res = (yield self.send_raw("POST", "/api", body))[0]
        yield self.set_username(res['success']['username'])
        return self.username

    @tornado.gen.coroutine
    def create_group(self, lights, name=None):
        """Create a new group for this bridge.
        
        Args:
            lights: a list of lamp IDs (integers).
            name: Name for this group, optional argument.
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
        
        Args:
            i: ID number for the group to be removed.
        """
        res = (yield self.send_request("DELETE", "/groups/{}".format(i)))[0]
        del self.groups[i]
        return res

    @tornado.gen.coroutine
    def update_info(self):
        """Update the Hue bridge metadata."""
        #print("Update " + str(self.username))
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
            #print(self.light_data)
            #print(self.groups)
        except UnauthorizedUserException:
            if self.username is not None:
                logging.warning("Couldn't send request to %s using username %s",
                                self.serial_number, self.username)
            self.logged_in = False
            self.gateway = None
            self.netmask = None
            self.name = None
            self.mac = None


class LightGrid:
    def __init__(self, usernames=None, grid=None, buffered=False, defaults=None):
        """Create a new light grid-

        Args:
            username: Map of serial number -> username pairs
            grid: A list of lists of (mac address, light) tuples, specifying
                a light belonging to a bridge with the given mac address.
                Maps (x,y) coordinates to the light specified at grid[y][x].
            buffered: If True, calls to set_state will not prompt an immediate
                request to the bridge in question; to send the buffered state
                changes, run commit.
            defaults: Additional instructions to include in each state change
                request to a bridge.
        """
        self.defaults = defaults if defaults is not None else {}
        self.bridges = {}
        self.usernames = usernames if usernames is not None else {}
        self.buffered = buffered
        self._buffer = collections.defaultdict(dict)

        self.grid = []
        self.height = 0
        self.width = 0

        #for ip in ip_addresses:
        #    bridge = Bridge(ip, defaults=defaults)
        #    self.bridges[bridge.serial_number] = bridge
        #    if bridge.serial_number in usernames:
        #        bridge.set_username(usernames[bridge.serial_number])
        self.set_grid(grid if grid is not None else [])

    @tornado.gen.coroutine
    def add_bridge(self, ip_address_or_bridge, username=None):
        """Add a new bridge to this light grid
        
        Args:
            ip_address_or_bridge: Can be either a Bridge object, or an IP address to the bridge, in which case a new Bridge object will be created.
            username: User name for this bridge. User names are required to perform most bridge commands.
        """
        # can take an already instantiated bridge instance
        if type(ip_address_or_bridge) is Bridge:
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
        """
        Check if this light grid has a particular bridge stored in its configuration.
        
        Args:
            mac_or_bridge: Either a MAC addressed for the requested bridge, or a Bridge object, in which case this method will search for a bridge with the same MAC address as the provided Bridge object.
        """
        if type(mac_or_bridge) is Bridge:
            mac = mac_or_bridge.serial_number
        else:
            mac = mac_or_bridge

        return mac in self.bridges

    def set_usernames(self, usernames):
        """
        Sets the user name map for this light grid.
        
        Args:
            username: Map of serial number -> username pairs
        """
        self.usernames = usernames

    def set_grid(self, grid):
        #for row in grid:
        #    for (mac, lamp) in row:
        #        pass
        #        if mac not in self.bridges:
        #            raise UnknownBridgeException
        self.grid = grid
        self.height = len(self.grid)
        self.width = max(len(x) for x in self.grid) if self.height > 0 else 0

    @tornado.gen.coroutine
    def set_state(self, x, y, **args):
        # pylint: disable=invalid-name
        """Set the state for a specific lamp.

        If this grid is buffered, the state will not be sent to the lamp immediately; run
        commit to send the buffered state changes.

        Args:
            x: X coordinate
            y: Y coordinate
            args: State argument, see Philips Hue documentation
        """

        self._buffer[(x, y)].update(args)

        if not self.buffered:
            exceptions = yield self.commit()
            if len(exceptions) > 0:
                # pass on first (and only, since this grid isn't buffered) exception
                raise next(iter(exceptions.values()))

    @tornado.gen.coroutine
    def set_all(self, **args):
        _, exc = yield ExceptionCatcher({bridge.serial_number: bridge.set_group(0, **args)
                                           for bridge in self.bridges.values()})
        return exc


    @tornado.gen.coroutine
    def commit(self):
        """Commit saved state changes to the lamps"""

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


class AsyncSocket(socket.socket):
    def __init__(self, *args):
        super().__init__(*args)
        self.setblocking(False)

        self._responses = []
        self._io_loop = tornado.ioloop.IOLoop.current()
        self._timeout = 0
        self._callback = None
        self._timeout_handle = None

    @tornado.gen.coroutine
    def wait_for_responses(self, timeout=2):
        self._responses = []
        self._timeout = timeout
        self._callback = yield tornado.gen.Callback(self.fileno())

        self._io_loop.add_handler(self.fileno(), self._fetch_response, self._io_loop.READ)
        self._set_timeout()

        yield tornado.gen.Wait(self.fileno())

        return self._responses

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
        self._callback(self._responses)


@tornado.gen.coroutine
def discover(attempts=2, timeout=2):
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
            "http://www.methue.com/api/nupnp", request_timeout=4)
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

def rgb2xy(red, green, blue):
    """Converts an RGB colour to XY colour format."""

    # Apply gamma
    if red > 0.04045:
        red = ((red + 0.055) / (1.0 + 0.055))**2.4
    else:
        red = red / 12.92

    if green > 0.04045:
        green = ((green + 0.055) / (1.0 + 0.055))**2.4
    else:
        green = green / 12.92

    if blue > 0.04045:
        blue = ((blue + 0.055) / (1.0 + 0.055))**2.4
    else:
        blue = blue / 12.92

    # pylint: disable=invalid-name
    # Convert to XYZ
    X = red * 0.649926 + green * 0.103455 + blue * 0.197109
    Y = red * 0.234327 + green * 0.743075 + blue * 0.022598
    Z = red * 0.000000 + green * 0.053077 + blue * 1.035763

    # Calculate xy values
    x = X / (X + Y + Z)
    y = Y / (X + Y + Z)

    return (x, y)

