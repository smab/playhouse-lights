
import threading
import traceback

import tornado.escape
import tornado.gen
import tornado.ioloop
import tornado.web

import errorcodes
import playhouse


def return_json(func):
    def new_func(self, *args, **kwargs):
        self.set_header("Content-Type", "application/json")
        data = func(self, *args, **kwargs)
        self.write(tornado.escape.json_encode(data))
    return new_func

def json_parser(func):
    def new_post(self, *args, **kwargs):
        try:
            data = tornado.escape.json_decode(self.request.body)
            return func(self, data, *args, **kwargs)
        except UnicodeDecodeError:
            return errorcodes.NOT_UNICODE
        except ValueError:
            return errorcodes.INVALID_JSON
    return new_post

def json_validator(jformat):
    def decorator(func):
        def is_valid(data, jf):
            print("Testing", data, "vs", jf)
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
        
        def new_func(self, data, *args, **kwargs):
            if is_valid(data, jformat):
                return func(self, data, *args, **kwargs)
            else:
                return errorcodes.INVALID_FORMAT
        
        return new_func
    
    return decorator


class LightsHandler(tornado.web.RequestHandler):
    @return_json
    @json_parser
    @json_validator([{"x": int, "y": int, "change": dict}])
    def post(self, data):
        print("Request was", data)
        for light in data:
            grid.set_state(light['x'], light['y'], **light['change'])
        grid.commit()
        return {"state": "success"}

class LightsAllHandler(tornado.web.RequestHandler):
    @return_json
    @json_parser
    @json_validator(dict)
    def post(self, data):
        grid.set_all(**data)
        grid.commit()
        return {"state": "success"}

class BridgesHandler(tornado.web.RequestHandler):
    @return_json
    def get(self):
        res = {"state": "success"}
        for mac, bridge in grid.bridges.items():
            res[mac] = {
                "ip": bridge.ipaddress,
                "username": bridge.username,
                "valid_username": bridge.logged_in,
                "lights": len(bridge.get_lights()) if bridge.logged_in else -1
            }
        return res

class BridgesAddHandler(tornado.web.RequestHandler):
    @return_json
    @json_parser
    @json_validator({"ip": str, "?username": {str, type(None)}})
    def post(self, data):
        try:
            username = data.get("username", None)
            bridge = grid.add_bridge(data['ip'], username)
        except playhouse.BridgeAlreadyAddedException:
            return errorcodes.BRIDGE_ALREADY_ADDED
        except:
            return errorcodes.BRIDGE_NOT_FOUND.format(ip=data['ip'])
        return {"state": "success", "mac": bridge.serial_number, "valid_username": bridge.logged_in}

class BridgesMacHandler(tornado.web.RequestHandler):
    @return_json
    @json_parser
    @json_validator({"username": {str, type(None)}})
    def post(self, data, mac):
        if mac not in grid.bridges:
            return errorcodes.NO_SUCH_MAC.format(mac=mac)
        grid.bridges[mac].set_username(data['username'])
        return {"state": "success", "username": data['username'], "valid_username": grid.bridges[mac].logged_in}

    @return_json
    def delete(self, mac):
        if mac not in grid.bridges:
            return errorcodes.NO_SUCH_MAC.format(mac=mac)
        
        del grid.bridges[mac]
        return {"state": "success"}
        



event = threading.Event()
new_bridges = []

class BridgesSearchHandler(tornado.web.RequestHandler):
    @return_json
    def post(self):
        if event.is_set():
            return errorcodes.CURRENTLY_SEARCHING
        
        def myfunc():
            global new_bridges
            event.set()
            print("running")
            new_bridges = playhouse.discover()
            print("finished")
            event.clear()
        thread = threading.Thread()
        thread.run = myfunc
        thread.start()
        
        return {"state": "success"}

class BridgesSearchResultHandler(tornado.web.RequestHandler):
    @return_json
    def get(self):
        if event.is_set():
            return errorcodes.CURRENTLY_SEARCHING
        
        return {"state": "success", "bridges": {b.serial_number: b.ipaddress for b in new_bridges}}
        
        
class BridgeLampSearchHandler(tornado.web.RequestHandler):
    @return_json
    @json_parser
    def post(self, data, mac):        
        if mac not in grid.bridges:
            return errorcodes.NO_SUCH_MAC.format(mac=mac)
        grid.bridges[mac].search_lights()
        return {"state": "success"}
        
class GridHandler(tornado.web.RequestHandler):
    @return_json
    @json_parser
    def post(self, data):
        try:
            g = []
            for d_row in data:
                row = []
                for d_lamp in d_row:
                    lamp = (d_lamp["mac"],d_lamp["lamp"])
                    row.append(lamp)
                g.append(row)              
            grid.set_grid(g)
            return {"state": "success"}
        except UnicodeDecodeError:
            return errorcodes.NOT_UNICODE
        #except playhouse.UnknownBridgeException as e:
        #    return errorcodes.NO_SUCH_MAC.format(mac=e.mac)
        except ValueError:
            return errorcodes.INVALID_JSON
            
    @return_json    
    def get(self):
        data = []
        for row in grid.grid:
            row_data = []
            for (mac, lamp) in row:
                row_data.append({"mac":mac,"lamp":lamp})
            data.append(row_data)
        return {"state":"success", "grid":data, "width":grid.width, "height":grid.height}

class BridgesSaveHandler(tornado.web.RequestHandler):
    @return_json
    def post(self):
        # TODO
        return errorcodes.NOT_IMPLEMENTED

class GridSaveHandler(tornado.web.RequestHandler):
    @return_json
    def post(self):
        # TODO
        return errorcodes.NOT_IMPLEMENTED

application = tornado.web.Application([
    (r'/lights', LightsHandler),
    (r'/lights/all', LightsAllHandler),
    (r'/bridges', BridgesHandler),
    (r'/bridges/add', BridgesAddHandler),
    (r'/bridges/([0-9a-f]{12})', BridgesMacHandler),
    (r'/bridges/([0-9a-f]{12})/lampsearch', BridgeLampSearchHandler),
    (r'/bridges/search', BridgesSearchHandler),
    (r'/bridges/search/result', BridgesSearchResultHandler),
    (r'/grid', GridHandler),
    (r'/bridges/save', BridgesSaveHandler),
    (r'/grid/save', GridSaveHandler),
])


def init_lightgrid():
    with open('config.json', 'r') as file:
        config = tornado.escape.json_decode(file.read())
        config["grid"] = [ [ (x[0], x[1]) for x in row ] for row in config["grid"] ]
        print(config)

    grid = playhouse.LightGrid(config["usernames"], config["grid"], buffered=True)
    for ip in config["ips"]:
        try:
            grid.add_bridge(ip)
        except:
            traceback.print_exc()
            print("Couldn't add ip", ip)
    return grid



if __name__ == "__main__":
    grid = init_lightgrid()

    application.listen(4711)
    tornado.ioloop.IOLoop.instance().start()