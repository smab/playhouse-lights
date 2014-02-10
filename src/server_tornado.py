
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


class LightsHandler(tornado.web.RequestHandler):
    @return_json
    @json_parser
    def post(self, data):
        print("Request was", data)
        for light in data:
            grid.set_state(light['x'], light['y'], **light['change'])
        grid.commit()
        return {"state": "success"}

class LightsAllHandler(tornado.web.RequestHandler):
    @return_json
    @json_parser
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


application = tornado.web.Application([
    (r'/lights', LightsHandler),
    (r'/lights/all', LightsAllHandler),
    (r'/bridges', BridgesHandler),
    (r'/bridges/add', BridgesAddHandler),
    (r'/bridges/([0-9a-f]{12})', BridgesMacHandler),
    (r'/bridges/search', BridgesSearchHandler),
    (r'/bridges/search/result', BridgesSearchResultHandler)
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