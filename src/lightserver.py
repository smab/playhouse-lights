
import datetime
import functools
import inspect
import json
import logging
import os
import time
import traceback

import tornado.escape
import tornado.gen
import tornado.httpserver
import tornado.ioloop
import tornado.web

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


class BaseHandler(tornado.web.RequestHandler):
    def get_current_user(self):
        return self.get_secure_cookie("user")

def authenticated(func):
    @functools.wraps(func)
    def new_func(self, *args, **kwargs):
        if CONFIG['require_password'] and not self.current_user:
            return errorcodes.E_NOT_LOGGED_IN
        else:
            #return (yield from func(self, *args, **kwargs))
            return func(self, *args, **kwargs)
    return new_func

def return_as_json(func):
    @functools.wraps(func)
    def new_func(self, *args, **kwargs):
        self.set_header("Content-Type", "application/json")
        data = func(self, *args, **kwargs)
        if inspect.isgenerator(data):
            data = yield from data

        logging.debug("Sent response %s", data)
        self.write(tornado.escape.json_encode(data))
    return new_func

def parse_json(jformat):
    def decorator(func):
        def is_valid(data, schema):
            logging.debug("Testing %s vs %s", repr(data), schema)
            if type(schema) is dict:
                # handle optional keys (?-prefixed)
                all_keys = set(x[1:] if x[0] == '?' else x for x in schema)
                required_keys = set(x for x in schema if x[0] != '?')
                schema = {k[1:] if k[0] == '?' else k: v for k, v in schema.items()}
            # don't even ask
            valid_format = (type(schema) is list and len(schema) == 1 and type(data) is list and
                                all(is_valid(d, schema[0]) for d in data)) or \
                           (type(schema) is list and len(schema) > 1 and type(data) is list and
                                len(data) == len(schema) and
                                all(is_valid(a, b) for a, b in zip(data, schema))) or \
                           (type(schema) is dict and type(data) is dict and
                                data.keys() <= all_keys and data.keys() >= required_keys and
                                all(is_valid(data[k], schema[k]) for k in data)) or \
                           (type(schema) is tuple and any(is_valid(data, a) for a in schema)) or \
                           (type(schema) is type and type(data) is schema)
            if valid_format:
                logging.debug("%s vs %s was valid", repr(data), schema)
            else:
                logging.debug("%s vs %s was invalid", repr(data), schema)
            return valid_format


        @functools.wraps(func)
        def new_func(self, *args, **kwargs):
            try:
                data = tornado.escape.json_decode(self.request.body)
                logging.debug("Got request %s", data)

                if is_valid(data, jformat):
                    #return (yield from func(self, data, *args, **kwargs))
                    return func(self, data, *args, **kwargs)
                else:
                    logging.debug("Request was invalid")
                    return errorcodes.E_INVALID_FORMAT

            except UnicodeDecodeError:
                return errorcodes.E_NOT_UNICODE
            except ValueError:
                return errorcodes.E_INVALID_JSON
        return new_func
    return decorator


class LightsHandler(BaseHandler):
    @tornado.gen.coroutine
    @return_as_json
    @authenticated
    @parse_json([{"x": int, "y": int, "?delay": float, "change": dict}])
    def post(self, data):
        def handle_exceptions(exceptions):
            for (x, y), e in exceptions.items():
                if type(e) is playhouse.NoBridgeAtCoordinateException:
                    logging.warning("No bridge added for (%s,%s)", x, y)
                    logging.debug("", exc_info=(type(e), e, e.__traceback__))
                elif type(e) is playhouse.OutsideGridException:
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

        return {"state": "success"}


class LightsAllHandler(BaseHandler):
    @tornado.gen.coroutine
    @return_as_json
    @authenticated
    @parse_json(dict)
    def post(self, data):
        yield GRID.set_all(**data)
        yield GRID.commit()
        return {"state": "success"}

class BridgesHandler(BaseHandler):
    @tornado.gen.coroutine
    @return_as_json
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
        return res

class BridgesAddHandler(BaseHandler):
    @tornado.gen.coroutine
    @return_as_json
    @authenticated
    @parse_json({"ip": str, "?username": (str, type(None))})
    def post(self, data):
        try:
            username = data.get("username", None)
            bridge = yield GRID.add_bridge(data['ip'], username)
        except playhouse.BridgeAlreadyAddedException:
            return errorcodes.E_BRIDGE_ALREADY_ADDED
        except:
            return errorcodes.E_BRIDGE_NOT_FOUND.format(ip=data['ip'])
        return {
            "state": "success",
            "bridges": {
                bridge.serial_number: {
                    "ip": bridge.ipaddress,
                    "username": bridge.username,
                    "valid_username": bridge.logged_in,
                    "lights": len((yield bridge.get_lights())) if bridge.logged_in else -1
                }
            }
        }


class BridgesMacHandler(BaseHandler):
    @tornado.gen.coroutine
    @return_as_json
    @authenticated
    @parse_json({"username": (str, type(None))})
    def post(self, data, mac):
        if mac not in GRID.bridges:
            return errorcodes.E_NO_SUCH_MAC.format(mac=mac)
        yield GRID.bridges[mac].set_username(data['username'])
        return {"state": "success", "username": data['username'],
                "valid_username": GRID.bridges[mac].logged_in}

    @tornado.gen.coroutine
    @return_as_json
    @authenticated
    def delete(self, mac):
        if mac not in GRID.bridges:
            return errorcodes.E_NO_SUCH_MAC.format(mac=mac)

        del GRID.bridges[mac]
        return {"state": "success"}


class BridgeLightsHandler(BaseHandler):
    @tornado.gen.coroutine
    @return_as_json
    @authenticated
    @parse_json([{"light": int, "change": dict}])
    def post(self, data, mac):
        if mac not in GRID.bridges:
            return errorcodes.E_NO_SUCH_MAC.format(mac=mac)

        for light in data:
            yield GRID.bridges[mac].set_state(light['light'], **light['change'])

        return {'state': 'success'}


class BridgeLightsAllHandler(BaseHandler):
    @tornado.gen.coroutine
    @return_as_json
    @authenticated
    @parse_json(dict)
    def post(self, data, mac):
        if mac not in GRID.bridges:
            return errorcodes.E_NO_SUCH_MAC.format(mac=mac)

        yield GRID.bridges[mac].set_group(0, **data)

        return {'state': 'success'}


class BridgeLampSearchHandler(BaseHandler):
    @tornado.gen.coroutine
    @return_as_json
    @authenticated
    def post(self, mac):
        if mac not in GRID.bridges:
            return errorcodes.E_NO_SUCH_MAC.format(mac=mac)
        yield GRID.bridges[mac].search_lights()
        return {"state": "success"}


class BridgeAddUserHandler(BaseHandler):
    @tornado.gen.coroutine
    @return_as_json
    @authenticated
    @parse_json({"?username": str})
    def post(self, data, mac):
        if mac not in GRID.bridges:
            return errorcodes.E_NO_SUCH_MAC.format(mac=mac)
        username = data.get("username", None)

        try:
            newname = yield GRID.bridges[mac].create_user("playhouse user", username)
            return {"state": "success", "username": newname}
        except playhouse.NoLinkButtonPressedException:
            return errorcodes.E_NO_LINKBUTTON
        except Exception:
            logging.debug("", exc_info=True)
            return errorcodes.E_INVALID_NAME


class BridgesSearchHandler(BaseHandler):
    new_bridges = []
    last_search = -1
    is_running = False

    @tornado.gen.coroutine
    @return_as_json
    @authenticated
    @parse_json({"auto_add": bool})
    def post(self, data):
        if BridgesSearchHandler.is_running:
            return errorcodes.E_CURRENTLY_SEARCHING

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

                BridgesSearchHandler.is_running = False
                logging.info("Bridge discovery finished")
            except Exception:
                traceback.print_exc()

        logging.info("Doing bridge discovery")
        BridgesSearchHandler.is_running = True
        tornado.ioloop.IOLoop.current().add_future(playhouse.discover(),
                                                   functools.partial(get_result))

        return {"state": "success"}



    @tornado.gen.coroutine
    @return_as_json
    @authenticated
    def get(self):
        if BridgesSearchHandler.is_running:
            return errorcodes.E_CURRENTLY_SEARCHING

        return {
            "state": "success",
            "finished": BridgesSearchHandler.last_search,
            "bridges": {
                bridge.serial_number: bridge.ipaddress
                for bridge in BridgesSearchHandler.new_bridges
            }
        }


class GridHandler(BaseHandler):
    @tornado.gen.coroutine
    @return_as_json
    @authenticated
    @parse_json([[({"mac": str, "lamp": int}, type(None))]])
    def post(self, data):
        g = [[(lamp['mac'], lamp['lamp']) if lamp is not None else None
              for lamp in row]
             for row in data]
        GRID.set_grid(g)
        logging.debug("Grid is set to %s", g)
        return {"state": "success"}

    @tornado.gen.coroutine
    @return_as_json
    @authenticated
    def get(self):
        data = [[{"mac": col[0], "lamp": col[1]} if col is not None else None
                 for col in row]
                for row in GRID.grid]
        return {"state": "success", "grid": data, "width": GRID.width, "height": GRID.height}

class BridgesSaveHandler(BaseHandler):
    @tornado.gen.coroutine
    @return_as_json
    @authenticated
    def post(self):
        try:
            with open(BRIDGE_CONFIG_FILE, 'r') as f:
                conf = tornado.escape.json_decode(f.read())
        except (FileNotFoundError, ValueError):
            logging.warning("%s not found or contained invalid JSON, creating new file",
                            BRIDGE_CONFIG_FILE)
            conf = {}

        conf['ips'] = [bridge.ipaddress for bridge in GRID.bridges.values()]
        conf['usernames'] = {bridge.serial_number: bridge.username
                             for bridge in GRID.bridges.values()}

        with open(BRIDGE_CONFIG_FILE, 'w') as f:
            f.write(tornado.escape.json_encode(conf))

        return {"state": "success"}

class GridSaveHandler(BaseHandler):
    @tornado.gen.coroutine
    @return_as_json
    @authenticated
    def post(self):
        try:
            with open(BRIDGE_CONFIG_FILE, 'r') as f:
                conf = tornado.escape.json_decode(f.read())
        except (FileNotFoundError, ValueError):
            logging.warning("%s not found or contained invalid JSON, creating new file",
                            BRIDGE_CONFIG_FILE)
            conf = {}

        conf['grid'] = GRID.grid

        with open(BRIDGE_CONFIG_FILE, 'w') as f:
            conf['grid'] = GRID.grid
            f.write(tornado.escape.json_encode(conf))

        return {"state": "success"}

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
    @tornado.gen.coroutine
    @return_as_json
    @parse_json({"password": str, "username": str})
    def post(self, data):
        if CONFIG['require_password']:
            if data['password'] == CONFIG['password']:
                self.set_secure_cookie('user', data['username'])
                return {"state": "success"}
            else:
                return errorcodes.E_INVALID_PASSWORD
        else:
            return errorcodes.E_AUTH_NOT_ENABLED


class StatusHandler(BaseHandler):
    def get(self):
        pass


def init_logging():
    format_string = "%(created)d:%(levelname)s:%(module)s:%(funcName)s:%(lineno)d > %(message)s"
    formatter = logging.Formatter(format_string)

    logging.basicConfig(filename="lightserver-all.log",
                        level=logging.DEBUG,
                        format=format_string)

    file_handler = logging.FileHandler(filename="lightserver.log")
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    stderr_handler = logging.StreamHandler()
    stderr_handler.setLevel(logging.INFO)
    stderr_handler.setFormatter(formatter)

    logging.getLogger().addHandler(file_handler)
    logging.getLogger().addHandler(stderr_handler)


@tornado.gen.coroutine
def init_lightgrid():
    logging.info("Initializing the LightGrid")

    logging.info("Reading bridge setup file (%s)", BRIDGE_CONFIG_FILE)
    bridge_config = {"grid": [], "usernames": {}, "ips": []}
    try:
        with open(BRIDGE_CONFIG_FILE, 'r') as file:
            bridge_config.update(tornado.escape.json_decode(file.read()))
            logging.debug("Configuration was %s", bridge_config)

            bridge_config["grid"] = [[tuple(x) for x in row] for row in bridge_config["grid"]]
            logging.debug("Constructed grid %s", bridge_config["grid"])
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
        logging.info("Couldn't find a bridge at %s", ip)
        logging.debug("", exc_info=(type(e), e, e.__traceback__))

    logging.info("Finished adding bridges")

def init_http():
    logging.info("Creating Application object")

    # NOTE: every new instance will have a unique cookie secret,
    # meaning that cookies created by other instances will be incompatible
    # with this one
    application = tornado.web.Application([
        (r'/lights', LightsHandler),
        (r'/lights/all', LightsAllHandler),
        (r'/bridges', BridgesHandler),
        (r'/bridges/add', BridgesAddHandler),
        (r'/bridges/([0-9a-f]{12})', BridgesMacHandler),
        (r'/bridges/([0-9a-f]{12})/lampsearch', BridgeLampSearchHandler),
        (r'/bridges/([0-9a-f]{12})/adduser', BridgeAddUserHandler),
        (r'/bridges/([0-9a-f]{12})/lights', BridgeLightsHandler),
        (r'/bridges/([0-9a-f]{12})/lights/all', BridgeLightsAllHandler),
        (r'/bridges/search', BridgesSearchHandler),
        (r'/grid', GridHandler),
        (r'/bridges/save', BridgesSaveHandler),
        (r'/grid/save', GridSaveHandler),
        (r'/debug', DebugHandler),
        (r'/authenticate', AuthenticateHandler),
        (r'/status', StatusHandler),
    ], cookie_secret=os.urandom(256))


    logging.info("Reading configuration file (%s)", CONFIG_FILE)

    try:
        CONFIG.update(json.load(open(CONFIG_FILE)))
    except FileNotFoundError:
        logging.warning("%s not found, using default configuration values", CONFIG_FILE)

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
    init_logging()

    init_lightgrid() # will run when IO loop has started

    init_http()

    logging.info("Server now listening at port %s", CONFIG['port'])
    tornado.ioloop.IOLoop.instance().start()
