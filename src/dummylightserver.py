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

#if __name__ == "__main__":
#    import logging.config
#    logging.config.fileConfig('logging.conf')

import datetime
import functools
import inspect
import json
import logging
import os
import time
import traceback
import sys
import copy
from tkinter import *
import colorsys

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

                if isinstance(e, playhouse.OutsideGridException):
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
            
class GridHandler(BaseHandler):
    
    @error_handler
    @authenticated
    def get(self):
        """Get the current grid used for ``(x, y)`` coordinate -> light mapping.

        :request-format:

        **Example response**: See the example request of :http:post:`/grid`.

        **Successful response format**: See the request format of :http:post:`/grid`.
        """
        
        data = [[{"mac": "0fb2a8549ec2", "lamp": GRID.width*row+col}
                 for col in range(GRID.width)]
                for row in range(GRID.height)]
        self.write({"state": "success", "grid": data,
                         "width": GRID.width, "height": GRID.height})



class StatusHandler(BaseHandler):
    def get(self):
        """Always responds with 200 OK. Can be used to tell whether the server is up.

        :request-format:
        """
        pass




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
    (r'/debug', DebugHandler),
    (r'/authenticate', AuthenticateHandler),
    (r'/grid', GridHandler),
    (r'/status', StatusHandler),
], cookie_secret=os.urandom(256))

class LampWindow:
    def __init__(self, width, height):
        self.width = width
        self.height = height
        self.tk = Tk()
        self.padding = 2
        self.offset = 2
        self.diameter = 50
        canvas_width = (self.diameter+self.padding)*self.width+self.offset
        canvas_height = (self.diameter+self.padding)*self.height+self.offset
        self.canvas = Canvas(self.tk, 
                   width=canvas_width,
                   height=canvas_height)
        self.canvas.pack()
        
    def repaint(self, data):
        for width in range(self.width):
            for height in range(self.height):
                lamp_data = data[height][width]
                
                if lamp_data["on"] is False:
                    fill = "#000000"
                else:
                    h = lamp_data["hue"]
                    s = lamp_data["sat"]
                    v = lamp_data["bri"]
                    
                    def to_hex(n):
                        s = hex(n)[2:]
                        if len(s) == 1:
                            s = "0"+s
                        return s
               
                    r, g, b = (to_hex(int(x*255)) for x in self.hsv_to_rgb(h,s,v))
                    fill="#" + r + g + b
                x0 = self.offset+width*(self.diameter+self.padding)
                y0 = self.offset+height*(self.diameter+self.padding)
                x1 = x0+self.diameter
                y1 = y0+self.diameter
                self.canvas.create_oval(x0, y0, x1, y1, fill=fill)
        self.tk.update()
        
    def hsv_to_rgb(self, h, s, v):
        # Hue is something between 0 and 65535
        # Saturation is something between 0 and 255
        # Value is something between 0 and 255

        return colorsys.hsv_to_rgb(h/65536, s/255, 0.5+(0.5*v/255))


if __name__ == "__main__":
    w = int(sys.argv[1])
    h = int(sys.argv[2])
    GRID = playhouse.DummyLightGrid(w, h, buffered=True)
    loop = tornado.ioloop.IOLoop.current()
    window = LampWindow(w, h)
    def update():
        #print("Repaint")
        window.repaint(GRID._lamp_data)
    io_loop = tornado.ioloop.PeriodicCallback(update,50, loop)
    
    init_http()
    io_loop.start()
    logging.info("Server now listening at port %s", CONFIG['port'])
    loop.start()
    