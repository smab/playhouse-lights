
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

    def read_json(self, schema=None):
        try:
            logging.debug("Request is %s", self.request.body)
            data = tornado.escape.json_decode(self.request.body)
            logging.debug("Parsed JSON %s", data)

            if schema is not None:
                validator = jsonschema.Draft4Validator(
                    schema, types={"nullablestring": (str, type(None)),
                                   "nullableobject": (dict, type(None))})
                validator.validate(data)
            return data
        except UnicodeDecodeError:
            raise errorcodes.RequestInvalidUnicodeException
        except ValueError:
            logging.debug("Unable to parse JSON")
            raise errorcodes.RequestInvalidJSONException
        except jsonschema.ValidationError:
            logging.debug("JSON was in an invalid format")
            raise errorcodes.RequestInvalidFormatException

    def write_json(self, data):
        self.set_header("Content-Type", "application/json")
        logging.debug("Sent response %s", data)
        self.write(tornado.escape.json_encode(data))


def authenticated(func):
    @functools.wraps(func)
    def new_func(self, *args, **kwargs):
        if CONFIG['require_password'] and not self.current_user:
            raise errorcodes.NotLoggedInException
        else:
            return func(self, *args, **kwargs)
    return new_func

def error_handler(func):
    @tornado.gen.coroutine
    @functools.wraps(func)
    def new_func(self, *args, **kwargs):
        try:
            result = func(self, *args, **kwargs)
            if isinstance(result, tornado.concurrent.Future):
                yield result
        except errorcodes.LightserverException as e:
            self.write_json(e.error)
        except playhouse.UnauthorizedUserException as e:
            self.write_json(errorcodes.E_INVALID_USERNAME.format(
                mac=e.bridge.serial_number, username=e.bridge.username).merge(
                    mac=e.bridge.serial_number, username=e.bridge.username))
        except playhouse.BridgeAlreadyAddedException:
            self.write_json(errorcodes.E_BRIDGE_ALREADY_ADDED)
        except playhouse.NoBridgeFoundException:
            self.write_json(errorcodes.E_BRIDGE_NOT_FOUND)
        except playhouse.NoLinkButtonPressedException:
            self.write_json(errorcodes.E_NO_LINKBUTTON)
        except playhouse.BulbNotResetException:
            self.write_json(errorcodes.E_BULB_NOT_RESET)
        except Exception as e: # should not happen
            self.write_json(errorcodes.E_INTERNAL_ERROR)
            logging.exception("Received an unexpected exception!")
    return new_func

class LightsHandler(BaseHandler):
    @error_handler
    @tornado.gen.coroutine
    @authenticated
    def post(self):
        data = self.read_json({
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "x": { "type": "integer" },
                    "y": { "type": "integer" },
                    "delay": { "type": "number" },
                    "change": { "type": "object" }
                },
                "required": ["x", "y", "change"]
            }
        })

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

        self.write_json({"state": "success"})


class LightsAllHandler(BaseHandler):
    @error_handler
    @tornado.gen.coroutine
    @authenticated
    def post(self):
        data = self.read_json({"type": "object"})
        yield GRID.set_all(**data)
        yield GRID.commit()
        self.write_json({"state": "success"})

class BridgesHandler(BaseHandler):
    @error_handler
    @tornado.gen.coroutine
    @authenticated
    def get(self):
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
        self.write_json(res)

class BridgesAddHandler(BaseHandler):
    @error_handler
    @tornado.gen.coroutine
    @authenticated
    def post(self):
        data = self.read_json({
            "type": "object",
            "properties": {
                "ip": { "type": "string" },
                "username": { "type": "nullablestring" }
            },
            "required": ["ip"]
        })

        username = data.get("username", None)
        bridge = yield GRID.add_bridge(data['ip'], username)
        save_grid_changes()

        self.write_json({
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
    def new_func_coroutine(self, mac, *args, **kwargs):
        if mac not in GRID.bridges:
            raise errorcodes.NoSuchMacException

        yield from func(self, mac, *args, **kwargs)

    @functools.wraps(func)
    def new_func(self, mac, *args, **kwargs):
        if mac not in GRID.bridges:
            raise errorcodes.NoSuchMacException

        func(self, mac, *args, **kwargs)

    return new_func if not inspect.isgeneratorfunction(func) else new_func_coroutine

class BridgesMacHandler(BaseHandler):
    @error_handler
    @tornado.gen.coroutine
    @authenticated
    @check_mac_exists
    def post(self, mac):
        data = self.read_json({
            "type": "object",
            "properties": {
                "username": { "type": "nullablestring" }
            },
            "required": ["username"]
        })

        yield GRID.bridges[mac].set_username(data['username'])
        save_grid_changes()
        self.write_json({"state": "success", "username": data['username'],
                         "valid_username": GRID.bridges[mac].logged_in})

    @error_handler
    @authenticated
    @check_mac_exists
    def delete(self, mac):
        del GRID.bridges[mac]
        save_grid_changes()
        self.write_json({"state": "success"})


class BridgeLightsHandler(BaseHandler):
    @error_handler
    @tornado.gen.coroutine
    @authenticated
    @check_mac_exists
    def post(self, mac):
        data = self.read_json({
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "light": { "type": "integer" },
                    "change": { "type": "object" }
                },
                "required": ["light", "change"]
            }
        })

        # TODO: partial error reporting?
        _, errors = yield playhouse.ExceptionCatcher({
            light['light']: GRID.bridges[mac].set_state(light['light'], **light['change'])
            for light in data
        })
        self.write_json({'state': 'success'})


class BridgeLightsAllHandler(BaseHandler):
    @error_handler
    @tornado.gen.coroutine
    @authenticated
    @check_mac_exists
    def post(self, mac):
        data = self.read_json({"type": "object"})
        yield GRID.bridges[mac].set_group(0, **data)

        self.write_json({'state': 'success'})


class BridgeLampSearchHandler(BaseHandler):
    @error_handler
    @tornado.gen.coroutine
    @authenticated
    @check_mac_exists
    def post(self, mac):
        yield GRID.bridges[mac].search_lights()
        self.write_json({"state": "success"})

class BridgeResetBulbHandler(BaseHandler):
    @error_handler
    @tornado.gen.coroutine
    @authenticated
    @check_mac_exists
    def post(self, mac):
        nwkaddr, pan = yield GRID.bridges[mac].reset_nearby_bulb()
        self.write_json({"state": "success", "nwkaddr": nwkaddr, "pan": pan})

class BridgeAddUserHandler(BaseHandler):
    @error_handler
    @tornado.gen.coroutine
    @authenticated
    @check_mac_exists
    def post(self, mac):
        data = self.read_json({
            "type": "object",
            "properties": {
                "username": { "type": "string" }
            }
        })
        username = data.get("username", None)

        bridge = GRID.bridges[mac]
        try:
            newname = yield bridge.create_user("playhouse user", username)
        except playhouse.InvalidValueException:
            raise errorcodes.InvalidUserNameException
        save_grid_changes()
        self.write_json({"state": "success", "username": newname,
                            "valid_username": bridge.logged_in})


class BridgesSearchHandler(BaseHandler):
    new_bridges = []
    last_search = -1
    is_running = False

    @error_handler
    @authenticated
    def post(self):
        data = self.read_json({
            "type": "object",
            "properties": {
                "auto_add": { "type": "boolean" }
            },
            "required": ["auto_add"]
        })
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

        self.write_json({"state": "success"})


    @error_handler
    @authenticated
    def get(self):
        if BridgesSearchHandler.is_running:
            raise errorcodes.CurrentlySearchingException
        else:
            self.write_json({
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
    def post(self):
        data = self.read_json({
            "type": "array",
            "items": {
                "type": "array",
                "items": {
                    "type": "nullableobject",
                    "properties": {
                        "mac": { "type": "string" },
                        "lamp": { "type": "integer" }
                    },
                    "required": ["mac", "lamp"]
                }
            }
        })

        g = [[(lamp['mac'], lamp['lamp']) if lamp is not None else None
              for lamp in row]
             for row in data]
        GRID.set_grid(g)

        save_grid_changes()

        logging.debug("Grid is set to %s", g)
        self.write_json({"state": "success"})

    @error_handler
    @authenticated
    def get(self):
        data = [[{"mac": col[0], "lamp": col[1]} if col is not None else None
                 for col in row]
                for row in GRID.grid]
        self.write_json({"state": "success", "grid": data,
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
    def post(self):
        data = self.read_json({
            "type": "object",
            "properties": {
                "password": { "type": "string" },
                "username": { "type": "string" }
            },
            "required": ["password", "username"]
        })

        if CONFIG['require_password']:
            if data['password'] == CONFIG['password']:
                self.set_secure_cookie('user', data['username'])
                self.write_json({"state": "success"})
            else:
                raise errorcodes.InvalidPasswordException
        else:
            raise errorcodes.AuthNotEnabledException


class StatusHandler(BaseHandler):
    def get(self):
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
    logging.info("Creating Application object")

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
        (r'/bridges/([0-9a-f]{12})', BridgesMacHandler), # POST/DELETE save_grid_changes
        (r'/bridges/([0-9a-f]{12})/lampsearch', BridgeLampSearchHandler),
        (r'/bridges/([0-9a-f]{12})/adduser', BridgeAddUserHandler), # POST save_grid_changes
        (r'/bridges/([0-9a-f]{12})/lights', BridgeLightsHandler),
        (r'/bridges/([0-9a-f]{12})/lights/all', BridgeLightsAllHandler),
        (r'/bridges/([0-9a-f]{12})/resetbulb', BridgeResetBulbHandler),
        (r'/bridges/search', BridgesSearchHandler), # POST save_grid_changes
        (r'/grid', GridHandler), # POST save_grid_changes
        (r'/debug', DebugHandler),
        (r'/authenticate', AuthenticateHandler),
        (r'/status', StatusHandler),
    ], cookie_secret=os.urandom(256))


    logging.info("Reading configuration file (%s)", CONFIG_FILE)

    try:
        with open(CONFIG_FILE) as f:
            CONFIG.update(json.load(f))
    except (FileNotFoundError, ValueError):
        logging.warning("%s not found or contained invalid JSON, " \
                        "using default configuration values: %s", CONFIG_FILE, CONFIG)

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

if __name__ == "__main__":
    loop = tornado.ioloop.IOLoop.current()
    loop.run_sync(init_lightgrid)

    init_http()

    logging.info("Server now listening at port %s", CONFIG['port'])
    loop.start()
