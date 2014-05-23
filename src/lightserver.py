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

"""This is the light server component of the Playhouse project.

The light server handles the current light grid configuration (with Hue bridges and lights)
and the communication between the application (typically the Playhouse web server)
and the separate Hue bridges. It exposes an HTTP API to enable remote control of the
lights over the network; see :ref:`api`.

.. highlight:: json

Running the server
------------------

Dependencies
^^^^^^^^^^^^

* Python 3.3+
* Tornado 3.2+
* PycURL 7.19.3+
* libcurl 7.21.1+
* jsonschema 2.3.0+

Setup
^^^^^

The Hue bridges themselves have no HTTPS support, and the protection against unauthorized commands
is very weak. Therefore, it is strongly recommended to set up the Hue bridges in an
internal network configured so that they cannot communicate with the outside world directly.
The Hue bridges communicate through port 80 and 30000, so this can be done by configuring
a firewall to restrict access through those ports.
The light server must then be run within this internal network.

The light server supports HTTPS for communication between itself and the application,
and can therefore safely communicate with the outside world if HTTPS is configured and enabled.
Additionally, authentication can be enabled so that a valid password will be required
in order to send requests to the light server.

The standard port for the light server is 4711.

The light server setup is a few simple steps:

1. Download all files from the playhouse-lights repository to the computer where
   the light server will run. This can be done by running
   ``git clone https://github.com/smab/playhouse-lights.git``.
2. If you do not already have a config file, copy the file ``config.json.default``
   and name it ``config.json``
3. Change the configuration file as necessary; see :ref:`config`.
4. Run the server by executing ``python3 src/lightserver.py``
   from the folder where you just created the configuration file.

.. _config:

Configuration file
^^^^^^^^^^^^^^^^^^

The configuration file uses JSON syntax.
The configuration itself is a JSON object with various properties, as listed here:

============================  ====================  ===========
Name                          Allowed values        Meaning
============================  ====================  ===========
port                          Integer, 0-65535      Server port number (default: 4711).
password                      Text string           Server password.
require_password              Boolean               If true, the server will require a password;
                                                    see :ref:`authentication`.
validate_state_changes        Boolean               If true, the server will validate values and
                                                    parameters when changing the state of lights.
                                                    Setting this value to false can be useful
                                                    in cases where the internal validation schema
                                                    is outdated, though it may lead to unexpected
                                                    results.
ssl                           Boolean               If true, the server will communicate with
                                                    the application using HTTPS.
certfile                      String, path to file  If SSL is enabled, this file will
                                                    be used as the SSL certificate.
keyfile                       String, path to file  If SSL is enabled, this file will
                                                    be used as the SSL private key.
============================  ====================  ===========

.. _api:

The API
-------

This is a REST-inspired API that simplifies communication with Philips Hue bridges.

The API abstracts access to individual bridges by placing each light associated with a bridge
into a virtual grid, after which lights can be addressed by specifying the ``(x, y)`` coordinate
of the light in the grid, without having to take into account which bridge the light belongs to.

A number of API methods expect the request body to contain well-formed JSON conforming to
a certain format which differs between methods. This format is specified using JSON Schema; see
`the official website <http://json-schema.org/>`_ for more information and
`Understanding JSON Schema <http://spacetelescope.github.io/understanding-json-schema/>`_ for
a friendlier introduction.

Responses
^^^^^^^^^

All responses from the API will be a well-formed JSON object containing at the very least
a ``state`` property with either the string ``success`` or ``error``. Unless otherwise specified,
any successful responses will contain only ``{"state": "success"}``.

Failed requests
^^^^^^^^^^^^^^^

If a request fails, the response will contain an object with the poperty ``state`` set to the
string ``error``. Additionally the property ``errorcode`` will contain a short string identifying
the error type, and the property ``errormessage`` a human-readable error message.

.. _authentication:

Authentication
^^^^^^^^^^^^^^

TODO

API methods
^^^^^^^^^^^

.. autorest:: lightserver:application

"""

if __name__ == "__main__":
    import logging.config
    logging.config.fileConfig('logging.conf')

import datetime
import functools
import inspect
import json
import logging
import os
import time
import traceback

import tornado.concurrent
import tornado.escape
import tornado.gen
import tornado.httpserver
import tornado.ioloop
import tornado.web

import jsonschema

import errorcodes
import playhouse

# disabling too-many-public methods globally in the module
# because of Tornado's RequestHandler
# disabling arguments-differ as this is a consequence of
# the use of the parse_json decorator
# pylint: disable=too-many-public-methods,arguments-differ

CONFIG_FILE = "config.json"
BRIDGE_CONFIG_FILE = "bridge_setup.json"

CONFIG = {
    "port": 4711,
    "require_password": False,
    "password": None,
    "validate_state_changes": True,
    "ssl": False
}

GRID = playhouse.LightGrid(buffered=True)


def save_grid_changes():
    try:
        with open(BRIDGE_CONFIG_FILE, 'r') as f:
            conf = tornado.escape.json_decode(f.read())
    except (FileNotFoundError, ValueError):
        logging.warning("%s not found or contained invalid JSON, creating new file",
                        BRIDGE_CONFIG_FILE)
        conf = {"usernames": {}, "ips": []}

    conf['grid'] = GRID.grid
    conf['ips'] = list(set(conf['ips']) | set(bridge.ipaddress for bridge in GRID.bridges.values()))
    conf['usernames'].update({ # 'update' in order to keep old usernames
        mac: bridge.username for mac, bridge in GRID.bridges.items() if bridge.logged_in
    })

    # NOTE: this is the only place where the grid's usernames dict is updated
    GRID.set_usernames(conf['usernames'])

    json_data = tornado.escape.json_encode(conf)
    with open(BRIDGE_CONFIG_FILE, 'w') as f:
        f.write(json_data)
        logging.debug("Wrote %s to %s", conf, BRIDGE_CONFIG_FILE)


class BaseHandler(tornado.web.RequestHandler):
    def get_current_user(self):
        return self.get_secure_cookie("user")

    def read_json(self, schema=None, validator=None):
        try:
            logging.debug("Request is %s", self.request.body)
            data = tornado.escape.json_decode(self.request.body)
            logging.debug("Parsed JSON %s", data)

            if schema is not None and validator is None:
                jsonschema.validate(data, schema)
            elif validator is not None:
                validator.validate(data)
            return data
        except UnicodeDecodeError:
            raise errorcodes.RequestInvalidUnicodeException
        except ValueError:
            logging.debug("Unable to parse JSON")
            raise errorcodes.RequestInvalidJSONException
        except jsonschema.ValidationError:
            logging.debug("JSON was in an invalid format")
            raise


def authenticated(func):
    @functools.wraps(func)
    def wrapper(self, *args, **kwargs):
        if CONFIG['require_password'] and not self.current_user:
            raise errorcodes.NotLoggedInException
        else:
            return func(self, *args, **kwargs)
    return wrapper

def error_handler(func):
    @tornado.gen.coroutine
    @functools.wraps(func)
    def new_func(self, *args, **kwargs):
        try:
            result = func(self, *args, **kwargs)
            if isinstance(result, tornado.concurrent.Future):
                yield result
        except errorcodes.LightserverException as e:
            self.write(e.error)
        except jsonschema.ValidationError as e:
            self.write(errorcodes.E_INVALID_FORMAT.merge(format_error=e.message))
        except playhouse.UnauthorizedUserException as e:
            self.write(errorcodes.E_INVALID_USERNAME.format(
                mac=e.bridge.serial_number, username=e.bridge.username).merge(
                    mac=e.bridge.serial_number, username=e.bridge.username))
        except playhouse.BridgeAlreadyAddedException:
            self.write(errorcodes.E_BRIDGE_ALREADY_ADDED)
        except playhouse.NoBridgeFoundException:
            self.write(errorcodes.E_BRIDGE_NOT_FOUND)
        except playhouse.NoLinkButtonPressedException:
            self.write(errorcodes.E_NO_LINKBUTTON)
        except playhouse.BulbNotResetException:
            self.write(errorcodes.E_BULB_NOT_RESET)
        except Exception as e: # should not happen
            self.write(errorcodes.E_INTERNAL_ERROR)
            logging.exception("Received an unexpected exception!")
    return new_func

def read_json(schema=None):
    def decorator(func):
        if schema is not None:
            func._json_schema = schema # used by documentation

        jsonschema.Draft4Validator.check_schema(schema)
        validator = jsonschema.Draft4Validator(schema)

        @functools.wraps(func)
        def wrapper(self, *args, **kwargs):
            return func(self, self.read_json(validator=validator), *args, **kwargs)
        return wrapper
    return decorator

_CHANGE_SPECIFICATION = {
    "type": "object",
    "properties": {
        "on": { "type": "boolean" },
        "bri": {
            "type": "integer",
            "minimum": 0,
            "maximum": 255
        },
        "hue": {
            "type": "integer",
            "minimum": 0,
            "maximum": 65535
        },
        "sat": {
            "type": "integer",
            "minimum": 0,
            "maximum": 255
        },
        "xy": {
            "type": "array",
            "items": {
                "type": "number",
                "minimum": 0,
                "maximum": 1
            },
            "maxItems": 2,
            "minItems": 2
        },
        "ct": {
            "type": "integer",
            "minimum": 153,
            "maximum": 500
        },
        "rgb": {
            "type": "array",
            "items": {
                "type": "number",
                "minimum": 0,
                "maximum": 255
            },
            "maxItems": 3,
            "minItems": 3
        },
        "alert": {
            "enum": ["none", "select", "lselect"]
        },
        "effect": {
            "enum": ["none", "colorloop"]
        },
        "transitiontime": {
            "type": "integer",
            "minimum": 0
        }
    }
}

class LightsHandler(BaseHandler):
    @error_handler
    @tornado.gen.coroutine
    @authenticated
    @read_json({
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "x": { "type": "integer" },
                "y": { "type": "integer" },
                "delay": { "type": "number" },
                "change": _CHANGE_SPECIFICATION
            },
            "required": ["x", "y", "change"]
        }
    })
    def post(self, data):
        """Change the state of the lights at the given coordinates.

        **Example request**::

            [
                {
                    "x": 0,
                    "y": 2,
                    "change": {
                        "hue": 0,
                        "sat": 255,
                        "bri": 100
                    }
                },
                {
                    "x": 1,
                    "y": 1,
                    "delay": 0.8,
                    "change": {
                        "xy": [0.2, 0.4],
                        "transitiontime": 10
                    }
                }
            ]

        :request-format:
        """
        def handle_exceptions(exceptions):
            # TODO: partial error reporting?
            for (x, y), e in exceptions.items():
                if isinstance(e, playhouse.NoBridgeAtCoordinateException):
                    logging.warning("No bridge added for (%s,%s)", x, y)
                    logging.debug("", exc_info=(type(e), e, e.__traceback__))
                elif isinstance(e, playhouse.OutsideGridException):
                    logging.warning("(%s,%s) is outside grid bounds", x, y)
                    logging.debug("", exc_info=(type(e), e, e.__traceback__))
                else:
                    raise e

        @tornado.gen.coroutine
        def set_state(light, do_commit=False):
            GRID.set_state(light['x'], light['y'], **light['change'])
            if do_commit:
                handle_exceptions((yield GRID.commit()))

        for light in data:
            if "delay" in light:
                tornado.ioloop.IOLoop.instance().add_timeout(
                    datetime.timedelta(seconds=light['delay']),
                    functools.partial(set_state, light, do_commit=True))
            else:
                set_state(light)

        handle_exceptions((yield GRID.commit()))

        self.write({"state": "success"})


class LightsAllHandler(BaseHandler):
    @error_handler
    @tornado.gen.coroutine
    @authenticated
    @read_json(_CHANGE_SPECIFICATION)
    def post(self, data):
        """Set all lights known by all bridges added to the grid to the same state.

        **Example request**::

            {"on": false}

        :request-format:
        """
        yield GRID.set_all(**data)
        yield GRID.commit()
        self.write({"state": "success"})

class BridgesHandler(BaseHandler):
    @error_handler
    @tornado.gen.coroutine
    @authenticated
    def get(self):
        """Retrieve a list of all bridges added to the grid.

        :request-format:

        **Example response**::

            {
                "state": "success",
                "bridges": {
                    "cd3facebd076": {
                        "ip": "192.168.0.101",
                        "username": null,
                        "valid_username": false,
                        "lights": -1
                    },
                    "f827aef865ca": {
                        "ip": "192.168.0.104",
                        "username": "my-username",
                        "valid_username": true,
                        "lights": 3
                    }
                }
            }

        **Successful response format**::

            {
                "type": "object",
                "properties": {
                    "state": {
                        "enum": [
                            "success"
                        ]
                    },
                    "bridges": {
                        "type": "object",
                        "description": "Map of MAC address -> bridge information key/value pairs.",
                        "patternProperties": {
                            "[0-9a-f]{12}": {
                                "type": "object",
                                "properties": {
                                    "ip": {
                                        "type": "string",
                                        "description": "The IP address of the bridge."
                                    },
                                    "username": {
                                        "type": "string",
                                        "description": "The username used with the bridge."
                                    },
                                    "valid_username": {
                                        "type": "boolean",
                                        "description": "Whether the username is valid """ \
                                            """for use with the bridge."
                                    },
                                    "lights": {
                                        "type": "integer",
                                        "description": "Number of lights belonging """ \
                                            """to the bridge. -1 if valid_username is false."
                                    }
                                }
                            }
                        }
                    }
                }
            }
        """
        lights = yield {mac: bridge.get_lights()
                        for mac, bridge in GRID.bridges.items()
                        if bridge.logged_in}
        res = {
            "state": "success",
            "bridges": {
                mac: {
                    "ip": bridge.ipaddress,
                    "username": bridge.username,
                    "valid_username": bridge.logged_in,
                    "lights": len(lights[mac]) if bridge.logged_in else -1
                }
                for mac, bridge in GRID.bridges.items()
            }
        }
        self.write(res)

class BridgesAddHandler(BaseHandler):
    @error_handler
    @tornado.gen.coroutine
    @authenticated
    @read_json({
        "type": "object",
        "properties": {
            "ip": { "type": "string" },
            "username": {
                "anyOf": [
                    { "type": "string" },
                    { "type": "null" },
                ]
            }
        },
        "required": ["ip"]
    })
    def post(self, data):
        """Add a new bridge with the given IP address and username.

        A username is not required and may be either given as ``null`` or not supplied at all,
        though without one most actions involving the bridge will fail.

        **Example request**::

            {
                "ip": "192.168.0.104",
                "username": "my-username"
            }

        :request-format:

        **Example response**::

            {
                "state": "success",
                "bridges": {
                    "f827aef865ca": {
                        "ip": "192.168.0.104",
                        "username": "my-username",
                        "valid_username": true,
                        "lights": 3
                    }
                }
            }

        **Successful response format**::

            {
                "type": "object",
                "properties": {
                    "state": {
                        "enum": [
                            "success"
                        ]
                    },
                    "bridges": {
                        "type": "object",
                        "description": "Map of MAC address -> bridge information key/value pairs.",
                        "maxProperties": 1,
                        "patternProperties": {
                            "[0-9a-f]{12}": {
                                "type": "object",
                                "properties": {
                                    "ip": {
                                        "type": "string",
                                        "description": "The IP address of the bridge."
                                    },
                                    "username": {
                                        "type": "string",
                                        "description": "The username used with the bridge."
                                    },
                                    "valid_username": {
                                        "type": "boolean",
                                        "description": "Whether the username is valid """ \
                                            """for use with the bridge."
                                    },
                                    "lights": {
                                        "type": "integer",
                                        "description": "Number of lights belonging """ \
                                            """to the bridge. -1 if valid_username is false."
                                    }
                                }
                            }
                        }
                    }
                }
            }
        """
        username = data.get("username", None)
        bridge = yield GRID.add_bridge(data['ip'], username)
        save_grid_changes()

        self.write({
            "state": "success",
            "bridges": {
                bridge.serial_number: {
                    "ip": bridge.ipaddress,
                    "username": bridge.username,
                    "valid_username": bridge.logged_in,
                    "lights": len((yield bridge.get_lights())) if bridge.logged_in else -1
                }
            }
        })

def check_mac_exists(func):
    @functools.wraps(func)
    def wrapper(self, mac, *args, **kwargs):
        if mac not in GRID.bridges:
            raise errorcodes.NoSuchMacException
        return func(self, mac, *args, **kwargs)
    return wrapper

class BridgesMacHandler(BaseHandler):
    @error_handler
    @tornado.gen.coroutine
    @authenticated
    @check_mac_exists
    @read_json({
        "type": "object",
        "properties": {
            "username": {
                "anyOf": [
                    { "type": "string" },
                    { "type": "null" }
                ]
            }
        },
        "required": ["username"]
    })
    def post(self, data, mac):
        """Set the username to use for this bridge.

        :param mac: The MAC address of the bridge whose username to change.

        **Example request**::

            {"username": "my-username"}

        :request-format:

        **Example response**::

            {"username": "myusername", "valid_username": true}

        **Successful response format**::

            {
                "type": "object",
                "properties": {
                    "state": {
                        "enum": [
                            "success"
                        ]
                    },
                    "username": {
                        "anyOf": [
                            {
                                "type": "string"
                            },
                            {
                                "type": "null",
                            }
                        ],
                        "description": "The new username."
                    },
                    "valid_username": {
                        "type": "boolean",
                        "description": "Whether the username is valid for use with the bridge."
                    }
                }
            }
        """
        yield GRID.bridges[mac].set_username(data['username'])
        save_grid_changes()
        self.write({"state": "success", "username": data['username'],
                         "valid_username": GRID.bridges[mac].logged_in})

    @error_handler
    @authenticated
    @check_mac_exists
    def delete(self, mac):
        """Remove this bridge from the grid.

        :param mac: The MAC address of the bridge to remove.

        :request-format:
        """
        del GRID.bridges[mac]
        save_grid_changes()
        self.write({"state": "success"})


class BridgeLightsHandler(BaseHandler):
    @error_handler
    @tornado.gen.coroutine
    @authenticated
    @check_mac_exists
    @read_json({
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "light": { "type": "integer" },
                "change": _CHANGE_SPECIFICATION
            },
            "required": ["light", "change"]
        }
    })
    def post(self, data, mac):
        """Change the state of the given lights known by this bridge.

        :param mac: The MAC address of the bridge that the lights whose state to change belong to.

        **Example request**::

            [
                {
                    "light": 1,
                    "change": {
                        "hue": 0,
                        "sat": 255
                    }
                },
                {
                    "light": 2,
                    "change": {
                        "hue": 21845,
                        "sat": 255
                    }
                },
                {
                    "light": 3,
                    "change": {
                        "hue": 43690,
                        "sat": 255
                    }
                },
            ]

        :request-format:
        """
        # TODO: partial error reporting?
        _, errors = yield playhouse.ExceptionCatcher({
            light['light']: GRID.bridges[mac].set_state(light['light'], **light['change'])
            for light in data
        })
        self.write({'state': 'success'})


class BridgeLightsAllHandler(BaseHandler):
    @error_handler
    @tornado.gen.coroutine
    @authenticated
    @check_mac_exists
    @read_json(_CHANGE_SPECIFICATION)
    def post(self, data, mac):
        """Change the state of all lights known by this bridge.

        :param mac: The MAC address of the bridge that the lights whose state to change belong to.

        **Example request**::

            {"alert": "select"}

        :request-format:
        """
        yield GRID.bridges[mac].set_group(0, **data)

        self.write({'state': 'success'})


class BridgeLampSearchHandler(BaseHandler):
    @error_handler
    @tornado.gen.coroutine
    @authenticated
    @check_mac_exists
    def post(self, mac):
        """Search for nearby lights for one minute and add them to this bridge.

        :param mac: The MAC address of the bridge that is to commence the search.

        :request-format:
        """
        yield GRID.bridges[mac].search_lights()
        self.write({"state": "success"})

class BridgeResetBulbHandler(BaseHandler):
    @error_handler
    @tornado.gen.coroutine
    @authenticated
    @check_mac_exists
    def post(self, mac):
        """Reset a light nearby this bridge.

        This will undo the pairing of the light with the bridge it was previously paired with.
        The light will become paired with this bridge, however it will not be added automatically
        to this bridge; :http:post:`/bridges/(?P<mac>[0-9a-f]{12})/lampsearch` must
        be issued separately.

        :param mac: The bridge to use to perform the reset.

        :request-format:
        """
        nwkaddr, pan = yield GRID.bridges[mac].reset_nearby_bulb()
        self.write({"state": "success", "nwkaddr": nwkaddr, "pan": pan})

class BridgeAddUserHandler(BaseHandler):
    @error_handler
    @tornado.gen.coroutine
    @authenticated
    @check_mac_exists
    @read_json({
        "type": "object",
        "properties": {
            "username": { "type": "string" }
        }
    })
    def post(self, data, mac):
        """Create a new username for use with this bridge.

        If a username is not supplied, a new one will be generated by the bridge. The link button
        on the bridge must be pressed prior to issuing a request.

        :param mac: The MAC address of the bridge for which to create the new username.

        **Example request**::

            {}

        :request-format:

        **Example response**::

            {
                "state": "success",
                "username": "f321c5d40b3a79eed6adc08eb3997a5e",
                "valid_username": true
            }

        **Successful response format**::

            {
                "type": "object",
                "properties": {
                    "state": {
                        "enum": [
                            "success"
                        ]
                    },
                    "username": {
                        "type": "string",
                        "description": "The new username."
                    },
                    "valid_username": {
                        "type": "boolean",
                        "description": "Whether the username is valid for use with the bridge """ \
                            """(always true)."
                    }
                }
            }
        """
        username = data.get("username", None)

        bridge = GRID.bridges[mac]
        try:
            newname = yield bridge.create_user("playhouse user", username)
        except playhouse.InvalidValueException:
            raise errorcodes.InvalidUserNameException
        save_grid_changes()
        self.write({"state": "success", "username": newname,
                            "valid_username": bridge.logged_in})


class BridgesSearchHandler(BaseHandler):
    new_bridges = []
    last_search = -1
    is_running = False

    @error_handler
    @authenticated
    @read_json({
        "type": "object",
        "properties": {
            "auto_add": {
                "type": "boolean",
                "description": "If true, new bridges are automatically added to the grid."
            }
        },
        "required": ["auto_add"]
    })
    def post(self, data):
        """Search for bridges on the local network.

        Returns immediately and runs the search asynchronously. To see when the search has
        finished and retrieve the new bridges, issue a request to
        :http:get:`/bridges/search` periodically.

        **Example request**::

            {"auto_add": true}

        :request-format:
        """
        if BridgesSearchHandler.is_running:
            raise errorcodes.CurrentlySearchingException

        @tornado.gen.coroutine
        def get_result(future):
            try: # add_future seems to discard the returned future along with its exception
                BridgesSearchHandler.new_bridges = future.result()
                logging.info("Bridge discovery found bridges at %s",
                            [b.ipaddress for b in BridgesSearchHandler.new_bridges])
                BridgesSearchHandler.last_search = int(time.time())

                if data['auto_add']:
                    logging.info("Auto-adding bridges")
                    for b in BridgesSearchHandler.new_bridges:
                        try:
                            yield GRID.add_bridge(b)
                            logging.info("Added %s at %s", b.serial_number, b.ipaddress)
                        except playhouse.BridgeAlreadyAddedException:
                            logging.info("%s at %s already added", b.serial_number, b.ipaddress)
                    logging.info("Finished auto-adding bridges")
                    save_grid_changes()

                BridgesSearchHandler.is_running = False
                logging.info("Bridge discovery finished")
            except Exception:
                traceback.print_exc()

        logging.info("Doing bridge discovery")
        BridgesSearchHandler.is_running = True
        tornado.ioloop.IOLoop.current().add_future(playhouse.discover(),
                                                   functools.partial(get_result))

        self.write({"state": "success"})

    @error_handler
    @authenticated
    def get(self):
        """Retrieve bridges found after running :http:post:`/bridges/search`.

        :request-format:

        **Example response**::

            {
                "state": "success",
                "finished": 1400776113,
                "bridges": {
                    "0fb2a8549ec2": "192.168.0.105",
                    "92d0d3cdbc7f": "192.168.0.109",
                    "e2fa9cc7df08": "192.168.0.103"
                }
            }

        **Successful response format**::

            {
                "type": "object",
                "properties": {
                    "state": {
                        "enum": [
                            "success"
                        ]
                    },
                    "finished": {
                        "type": "integer",
                        "description": "UNIX timestamp indicating when a search last finished. """ \
                            """-1 if no search has been issued yet."
                    },
                    "bridges": {
                        "patternProperties": {
                            "[0-9a-f]{12}": {
                                "type": "string",
                                "description": "The IP address of the bridge."
                            }
                        }
                    }
                }
            }
        """
        if BridgesSearchHandler.is_running:
            raise errorcodes.CurrentlySearchingException
        else:
            self.write({
                "state": "success",
                "finished": BridgesSearchHandler.last_search,
                "bridges": {
                    bridge.serial_number: bridge.ipaddress
                    for bridge in BridgesSearchHandler.new_bridges
                }
            })


class GridHandler(BaseHandler):
    @error_handler
    @authenticated
    @read_json({
        "type": "array",
        "items": {
            "type": "array",
            "items": {
                "anyOf": [
                    {
                        "type": "object",
                        "properties": {
                            "mac": { "type": "string" },
                            "lamp": { "type": "integer" }
                        },
                        "required": ["mac", "lamp"]
                    },
                    { "type": "null" }
                ]
            }
        }
    })
    def post(self, data):
        """Set the grid used for ``(x, y)`` coordinate -> light mapping.

        An ``(x, y)`` coordinate will be mapped to the light specified at ``grid[y][x]``.
        ``null`` values are allowed in the grid.

        **Example request**::

            [
                [
                    {"mac": "0fb2a8549ec2", "lamp": 1},
                    {"mac": "0fb2a8549ec2", "lamp": 3},
                    {"mac": "0fb2a8549ec2", "lamp": 2}
                ],
                [
                    null,
                    null,
                    null
                ],
                [
                    {"mac": "e2fa9cc7df08", "lamp": 1},
                    {"mac": "e2fa9cc7df08", "lamp": 2},
                    {"mac": "e2fa9cc7df08", "lamp": 3},
                ]
            ]

        :request-format:
        """
        g = [[(lamp['mac'], lamp['lamp']) if lamp is not None else None
              for lamp in row]
             for row in data]
        GRID.set_grid(g)

        save_grid_changes()

        logging.debug("Grid is set to %s", g)
        self.write({"state": "success"})

    @error_handler
    @authenticated
    def get(self):
        """Get the current grid used for ``(x, y)`` coordinate -> light mapping.

        :request-format:

        **Example response**: See the example request of :http:post:`/grid`.

        **Successful response format**: See the request format of :http:post:`/grid`.
        """
        data = [[{"mac": col[0], "lamp": col[1]} if col is not None else None
                 for col in row]
                for row in GRID.grid]
        self.write({"state": "success", "grid": data,
                         "width": GRID.width, "height": GRID.height})

class DebugHandler(BaseHandler):
    def get(self):
        website = """
<!DOCTYPE html>
<html>
<head><title>Debug</title></head>
<script>
function send_get(){
    var req = new XMLHttpRequest();
    url = document.getElementById('url').value;
    req.open("GET",url,false);
    req.send(null);
    response = req.responseText;
    document.getElementById('response').value = response;
}

function send_post(){
    var req = new XMLHttpRequest();
    url = document.getElementById('url').value;
    request = document.getElementById('request').value;
    req.open("POST",url,false);
    req.setRequestHeader("Content-type", "application/json");
    req.setRequestHeader("Content-length", request.length);
    req.setRequestHeader("Connection", "close");
    req.send(request);
    response = req.responseText;
    document.getElementById('response').value = response;
}
</script>
<body>

<h2>Request</h2>
<button type="button" onclick="send_get()">GET</button>
<button type="button" onclick="send_post()">POST</button><br />
<input type="text" name="url" id="url"><br />
<textarea rows="10" cols="50" id="request"></textarea>
<h2>Response</h2>
<textarea readonly="readonly" rows="10" cols="50" id="response"></textarea>

</body>
</html>



</html>


        """
        self.write(website)


class AuthenticateHandler(BaseHandler):
    @error_handler
    @read_json({
        "type": "object",
        "properties": {
            "password": { "type": "string" },
            "username": { "type": "string" }
        },
        "required": ["password", "username"]
    })
    def post(self):
        """Authenticate against the server. See :ref:`authentication`.

        If the password was valid, responds with a ``user`` cookie in the ``Set-Cookie``
        HTTP header, to be used with other requests.
        The username is currently not taken into account and may be set to any value.

        **Example request**::

            {
                "password": "mysecretpassword",
                "username": "myusername"
            }

        :request-format:

        """
        if CONFIG['require_password']:
            if data['password'] == CONFIG['password']:
                self.set_secure_cookie('user', data['username'])
                self.write({"state": "success"})
            else:
                raise errorcodes.InvalidPasswordException
        else:
            raise errorcodes.AuthNotEnabledException


class StatusHandler(BaseHandler):
    def get(self):
        """Always responds with 200 OK. Can be used to tell whether the server is up.

        :request-format:
        """
        pass


@tornado.gen.coroutine
def init_lightgrid():
    logging.info("Initializing the LightGrid")

    logging.info("Reading bridge setup file (%s)", BRIDGE_CONFIG_FILE)
    bridge_config = {"grid": [], "usernames": {}, "ips": []}
    try:
        with open(BRIDGE_CONFIG_FILE, 'r') as f:
            bridge_config.update(tornado.escape.json_decode(f.read()))
            logging.debug("Configuration was %s", bridge_config)
    except (FileNotFoundError, ValueError):
        logging.warning("%s not found or contained invalid JSON, using empty grid",
                        BRIDGE_CONFIG_FILE)

    GRID.set_usernames(bridge_config["usernames"])
    GRID.set_grid(bridge_config["grid"])

    logging.info("Adding preconfigured bridges")


    res, exc = yield playhouse.ExceptionCatcher({ip: GRID.add_bridge(ip)
                                                 for ip in bridge_config['ips']})
    for ip, bridge in res.items():
        logging.info("Added bridge %s at %s", bridge.serial_number, bridge.ipaddress)
    for ip, e in exc.items():
        logging.warning("Couldn't find a bridge at %s", ip)
        logging.debug("", exc_info=(type(e), e, e.__traceback__))

    logging.info("Finished adding bridges")

def init_http():
    logging.info("Reading configuration file (%s)", CONFIG_FILE)

    try:
        with open(CONFIG_FILE) as f:
            CONFIG.update(json.load(f))
    except (FileNotFoundError, ValueError):
        logging.warning("%s not found or contained invalid JSON, " \
                        "using default configuration values: %s", CONFIG_FILE, CONFIG)

    if not CONFIG['validate_state_changes']:
        _CHANGE_SPECIFICATION.clear()
        _CHANGE_SPECIFICATION['type'] = 'object'

    if CONFIG['require_password']:
        logging.info("This instance will require authentication")
    else:
        logging.warning("This instance will NOT require authentication")

    if CONFIG['ssl']:
        logging.info("Setting up HTTPS server")
        http_server = tornado.httpserver.HTTPServer(application, ssl_options={
            "certfile": CONFIG['certfile'],
            "keyfile": CONFIG['keyfile']
        })
    else:
        logging.info("Setting up HTTP server")
        http_server = tornado.httpserver.HTTPServer(application)

    http_server.listen(CONFIG['port'])

# NOTE: every new instance will have a unique cookie secret,
# meaning that cookies created by other instances will be incompatible
# with this one
# NOTE: make sure to call save_grid_changes from any method that somehow
# modifies the LightGrid (adds/removes bridges, changes username, changed the grid, etc)
application = tornado.web.Application([
    (r'/lights', LightsHandler),
    (r'/lights/all', LightsAllHandler),
    (r'/bridges', BridgesHandler),
    (r'/bridges/add', BridgesAddHandler), # POST save_grid_changes
    (r'/bridges/search', BridgesSearchHandler), # POST save_grid_changes
    (r'/bridges/(?P<mac>[0-9a-f]{12})', BridgesMacHandler), # POST/DELETE save_grid_changes
    (r'/bridges/(?P<mac>[0-9a-f]{12})/adduser', BridgeAddUserHandler), # POST save_grid_changes
    (r'/bridges/(?P<mac>[0-9a-f]{12})/lights', BridgeLightsHandler),
    (r'/bridges/(?P<mac>[0-9a-f]{12})/lights/all', BridgeLightsAllHandler),
    (r'/bridges/(?P<mac>[0-9a-f]{12})/lampsearch', BridgeLampSearchHandler),
    (r'/bridges/(?P<mac>[0-9a-f]{12})/resetbulb', BridgeResetBulbHandler),
    (r'/grid', GridHandler), # POST save_grid_changes
    (r'/debug', DebugHandler),
    (r'/authenticate', AuthenticateHandler),
    (r'/status', StatusHandler),
], cookie_secret=os.urandom(256))

if __name__ == "__main__":
    loop = tornado.ioloop.IOLoop.current()
    loop.run_sync(init_lightgrid)

    init_http()

    logging.info("Server now listening at port %s", CONFIG['port'])
    loop.start()
