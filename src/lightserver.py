
import datetime
import functools
import inspect
import json
import logging
import os
import ssl
import threading
import time
import traceback

import tornado.escape
import tornado.gen
import tornado.httpserver
import tornado.ioloop
import tornado.web

import errorcodes
import playhouse


CONFIG = "config.json"
BRIDGE_CONFIG = "bridge_setup.json"

config = {
    "port": 4711,
    "require_password": False,
    "password": None,
    "ssl": False
}

class AuthenticationHandler(tornado.web.RequestHandler):
    def get_current_user(self):
        return self.get_secure_cookie("user")

def authenticated(func):
    @functools.wraps(func)
    def new_func(self, *args, **kwargs):
        if config['require_password'] and not self.current_user:
            return errorcodes.NOT_LOGGED_IN
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
        def is_valid(data, jf):
            logging.debug("Testing %s vs %s", repr(data), jf)
            if type(jf) is dict:
                # handle optional keys (?-prefixed)
                all_keys = set(x[1:] if x[0] == '?' else x for x in jf)
                required_keys = set(x for x in jf if x[0] != '?')
                jf = {k[1:] if k[0] == '?' else k: v for k, v in jf.items()}
            # don't even ask
            return (type(jf) is list and type(data) is list and all(is_valid(d, jf[0]) for d in data)) or \
                   (type(jf) is tuple and type(data) is list and len(data) == len(jf) and all(is_valid(a, b) for a, b in zip(data, jf))) or \
                   (type(jf) is dict and type(data) is dict and data.keys() <= all_keys and data.keys() >= required_keys and all(is_valid(data[k], jf[k]) for k in data)) or \
                   (type(jf) is set and type(data) in jf) or \
                   (type(jf) is type and type(data) is jf)
        
        
        @functools.wraps(func)
        def new_func(self, *args, **kwargs):
            try:
                data = tornado.escape.json_decode(self.request.body)
                logging.debug("Got request %s", data)
                
                if (is_valid(data, jformat)):
                    #return (yield from func(self, data, *args, **kwargs))
                    return func(self, data, *args, **kwargs)
                else:
                    logging.debug("Request was invalid")
                    return errorcodes.INVALID_FORMAT
                
            except UnicodeDecodeError:
                return errorcodes.NOT_UNICODE
            except ValueError:
                return errorcodes.INVALID_JSON
        return new_func
    return decorator


class LightsHandler(AuthenticationHandler):
    @tornado.gen.coroutine
    @return_as_json
    @authenticated
    @parse_json([{"x": int, "y": int, "?delay": float, "change": dict}])
    def post(self, data):
        def set_state(light, do_commit=False):
            try:
                grid.set_state(light['x'], light['y'], **light['change'])
            except playhouse.NoBridgeAtCoordinateException:
                logging.warning("No bridge added for (%s,%s)", light['x'], light['y'])
                logging.debug("", exc_info=True)
            except playhouse.OutsideGridException:
                logging.warning("(%s,%s) is outside grid bounds", light['x'], light['y'])
                logging.debug("", exc_info=True)
            
            if do_commit:
                grid.commit()
        
        for light in data:
            if "delay" in light:
                tornado.ioloop.IOLoop.instance().add_timeout(
                    datetime.timedelta(seconds=light['delay']),
                    functools.partial(set_state, light, do_commit=True))
            else:
                set_state(light)
        
        yield grid.commit()
        return {"state": "success"}


class LightsAllHandler(AuthenticationHandler):
    @tornado.gen.coroutine
    @return_as_json
    @authenticated
    @parse_json(dict)
    def post(self, data):
        yield grid.set_all(**data)
        yield grid.commit()
        return {"state": "success"}

class BridgesHandler(AuthenticationHandler):
    @tornado.gen.coroutine
    @return_as_json
    @authenticated
    def get(self):
        res = {
            "bridges": {
                mac: {
                    "ip": bridge.ipaddress,
                    "username": bridge.username,
                    "valid_username": bridge.logged_in,
                    "lights": len(bridge.get_lights()) if bridge.logged_in else -1
                }
                for mac, bridge in grid.bridges.items()
            },
            "state": "success"
        }
        return res

class BridgesAddHandler(AuthenticationHandler):
    @tornado.gen.coroutine
    @return_as_json
    @authenticated
    @parse_json({"ip": str, "?username": {str, type(None)}})
    def post(self, data):
        try:
            username = data.get("username", None)
            bridge = grid.add_bridge(data['ip'], username)
        except playhouse.BridgeAlreadyAddedException:
            return errorcodes.BRIDGE_ALREADY_ADDED
        except:
            return errorcodes.BRIDGE_NOT_FOUND.format(ip=data['ip'])
        return {"state": "success",
                "bridges": {
                    bridge.serial_number: {
                        "ip": bridge.ipaddress,
                        "username": bridge.username,
                        "valid_username": bridge.logged_in,
                        "lights": len(bridge.get_lights()) if bridge.logged_in else -1
                    }}}

class BridgesMacHandler(AuthenticationHandler):
    @tornado.gen.coroutine
    @return_as_json
    @authenticated
    @parse_json({"username": {str, type(None)}})
    def post(self, data, mac):
        if mac not in grid.bridges:
            return errorcodes.NO_SUCH_MAC.format(mac=mac)
        grid.bridges[mac].set_username(data['username'])
        return {"state": "success", "username": data['username'], "valid_username": grid.bridges[mac].logged_in}

    @tornado.gen.coroutine
    @return_as_json
    @authenticated
    def delete(self, mac):
        if mac not in grid.bridges:
            return errorcodes.NO_SUCH_MAC.format(mac=mac)
        
        del grid.bridges[mac]
        return {"state": "success"}


class BridgeLightsHandler(AuthenticationHandler):
    @tornado.gen.coroutine
    @return_as_json
    @authenticated
    @parse_json([{"light": int, "change": dict}])
    def post(self, data, mac):
        if mac not in grid.bridges:
            return errorcodes.NO_SUCH_MAC.format(mac=mac)
        
        for light in data:
            grid.bridges[mac].set_state(light['light'], **light['change'])
        
        return {'state': 'success'}


class BridgeLightsAllHandler(AuthenticationHandler):
    @tornado.gen.coroutine
    @return_as_json
    @authenticated
    @parse_json(dict)
    def post(self, data, mac):
        if mac not in grid.bridges:
            return errorcodes.NO_SUCH_MAC.format(mac=mac)
        
        grid.bridges[mac].set_group(0, **data)
        
        return {'state': 'success'}


class BridgeLampSearchHandler(AuthenticationHandler):
    @tornado.gen.coroutine
    @return_as_json
    @authenticated
    def post(self, mac):        
        if mac not in grid.bridges:
            return errorcodes.NO_SUCH_MAC.format(mac=mac)
        grid.bridges[mac].search_lights()
        return {"state": "success"}


class BridgeAddUserHandler(AuthenticationHandler):
    @tornado.gen.coroutine
    @return_as_json
    @authenticated
    @parse_json({"?username": str})
    def post(self, data, mac):
        if mac not in grid.bridges:
            return errorcodes.NO_SUCH_MAC.format(mac=mac)
        username = data.get("username", None)
        
        try:
            newname = grid.bridges[mac].create_user("playhouse user", username)
            return {"state": "success", "username": newname}
        except playhouse.NoLinkButtonPressedException:
            return errorcodes.NO_LINKBUTTON
        except Exception:
            logging.debug("", exc_info=True)
            return errorcodes.INVALID_NAME


event = threading.Event()
# later changes to the bridges if auto_add is True will be reflected
# in new_bridges
new_bridges = []
last_search = -1

class BridgesSearchHandler(AuthenticationHandler):
    @tornado.gen.coroutine
    @return_as_json
    @authenticated
    @parse_json({"auto_add":bool})
    def post(self, data):
        if event.is_set():
            return errorcodes.CURRENTLY_SEARCHING
        
        def myfunc():
            global new_bridges, last_search
            nonlocal data
            event.set()
            
            logging.info("Running bridge discovery")
            new_bridges = playhouse.discover()
            logging.debug("Bridges found: %s", new_bridges)
            
            last_search = int(time.time())
            if data['auto_add']:
                logging.info("Auto-adding bridges")
                for b in new_bridges:
                    try:
                        grid.add_bridge(b)
                        logging.info("Added %s", b.serial_number)
                    except playhouse.BridgeAlreadyAddedException:
                        logging.info("%s already added", b.serial_number)
            logging.info("Bridge discovery finished")
            event.clear()
        thread = threading.Thread()
        thread.run = myfunc
        thread.start()
        
        return {"state": "success"}
    
    @tornado.gen.coroutine
    @return_as_json
    @authenticated
    def get(self):
        if event.is_set():
            return errorcodes.CURRENTLY_SEARCHING
        
        return {
            "state": "success",
            "finished": last_search,
            "bridges": {
                b.serial_number: {
                    "ip": b.ipaddress,
                    "username": b.username,
                    "valid_username": b.logged_in,
                    "lights": len(b.get_lights()) if b.logged_in else -1
                }
                for b in new_bridges
            }
        }


class GridHandler(AuthenticationHandler):
    @tornado.gen.coroutine
    @return_as_json
    @authenticated
    @parse_json([[{"mac": str, "lamp": int}]])
    def post(self, data):
        g = [[(lamp['mac'], lamp['lamp']) for lamp in row] for row in data]
        grid.set_grid(g)
        logging.debug("Grid is set to %s", g)
        return {"state": "success"}
    
    @tornado.gen.coroutine
    @return_as_json
    @authenticated
    def get(self):
        data = [[{"mac": mac, "lamp": lamp} for mac, lamp in row] for row in grid.grid]
        return {"state":"success", "grid":data, "width":grid.width, "height":grid.height}

class BridgesSaveHandler(AuthenticationHandler):
    @tornado.gen.coroutine
    @return_as_json
    @authenticated
    def post(self):
        try:
            with open(BRIDGE_CONFIG, 'r') as f:
                conf = tornado.escape.json_decode(f.read())
        except (FileNotFoundError, ValueError):
            logging.warning("%s not found or contained invalid JSON, creating new file", BRIDGE_CONFIG)
            conf = {}
        
        conf['ips'] = [bridge.ipaddress for bridge in grid.bridges.values()]
        conf['usernames'] = {bridge.serial_number: bridge.username for bridge in grid.bridges.values()}
        
        with open(BRIDGE_CONFIG, 'w') as f:
            f.write(tornado.escape.json_encode(conf))
        
        return {"state": "success"}

class GridSaveHandler(AuthenticationHandler):
    @tornado.gen.coroutine
    @return_as_json
    @authenticated
    def post(self):
        try:
            with open(BRIDGE_CONFIG, 'r') as f:
                conf = tornado.escape.json_decode(f.read())
        except (FileNotFoundError, ValueError):
            logging.warning("%s not found or contained invalid JSON, creating new file", BRIDGE_CONFIG)
            conf = {}
        
        conf['grid'] = grid.grid
        
        with open(BRIDGE_CONFIG, 'w') as f:
            conf['grid'] = grid.grid
            f.write(tornado.escape.json_encode(conf))
        
        return {"state": "success"}
        
class DebugHandler(tornado.web.RequestHandler):
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


class AuthenticateHandler(tornado.web.RequestHandler):
    @tornado.gen.coroutine
    @return_as_json
    @parse_json({"password": str, "username": str})
    def post(self, data):
        if config['require_password']:
            if data['password'] == config['password']:
                self.set_secure_cookie('user', data['username'])
                return {"state": "success"}
            else:
                return errorcodes.INVALID_PASSWORD
        else:
            return errorcodes.AUTH_NOT_ENABLED


class StatusHandler(tornado.web.RequestHandler):
    def get(self):
        pass


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


@tornado.gen.coroutine
def init_lightgrid(grid):
    logging.info("Initializing the LightGrid")
    logging.info("Reading bridge setup file (%s)", BRIDGE_CONFIG)
    bridge_config = {"grid": [], "usernames": {}, "ips": []}
    try:
        with open(BRIDGE_CONFIG, 'r') as file:
            bridge_config.update(tornado.escape.json_decode(file.read()))
            logging.debug("Configuration was %s", bridge_config)
            
            bridge_config["grid"] = [[tuple(x) for x in row] for row in bridge_config["grid"]]
            logging.debug("Constructed grid %s", bridge_config["grid"])
    except (FileNotFoundError, ValueError):
        logging.warning("%s not found or contained invalid JSON, using empty grid", BRIDGE_CONFIG)
    
    grid.set_usernames(bridge_config["usernames"])
    grid.set_grid(bridge_config["grid"])
    
    logging.info("Adding preconfigured bridges")
    for ip in bridge_config["ips"]:
        try:
            bridge = yield grid.add_bridge(ip)
            logging.info("Added bridge %s at %s", bridge.serial_number, bridge.ipaddress)
        except:
            logging.warning("Couldn't find a bridge at %s", ip)
            logging.debug("", exc_info=True)
    logging.info("Finished adding bridges")


if __name__ == "__main__":
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
    
    logging.info("Initializing light server")
    
    logging.info("Creating empty LightGrid")
    grid = playhouse.LightGrid(buffered=True)
    tornado.ioloop.IOLoop.instance().add_callback(init_lightgrid, grid)
    
    logging.info("Reading configuration file (%s)", CONFIG)
    
    try:
        config.update(json.load(open(CONFIG)))
    except FileNotFoundError:
        logging.warning("%s not found, using default configuration values", CONFIG)
    
    if config['require_password']:
        logging.info("This instance will require authentication")
    else:
        logging.warning("This instance will NOT require authentication")
    
    if config['ssl']:
        logging.info("Setting up HTTPS server")
        http_server = tornado.httpserver.HTTPServer(application, ssl_options={
            "certfile": config['certfile'],
            "keyfile": config['keyfile']
        })
    else:
        logging.info("Setting up HTTP server")
        http_server = tornado.httpserver.HTTPServer(application)
    
    http_server.listen(config['port'])
    
    logging.info("Server now listening at port %s", config['port'])
    tornado.ioloop.IOLoop.instance().start()
