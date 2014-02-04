import tornado.escape
import tornado.ioloop
import tornado.web

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
            return {"state": "error", "errorcode": 1, "errormessage": "couldn't decode as UTF-8"}
        except ValueError:
            return {"state": "error", "errorcode": 1, "errormessage": "invalid JSON"}
    return new_post


class LightsHandler(tornado.web.RequestHandler):
    @return_json
    @json_parser
    def post(self, data):
        print("Request was", data)
        for light in data:
            grid.set_state(light['x'], light['y'], **light['change'])
            pass
        return {"state": "success"}

class LightsAllHandler(tornado.web.RequestHandler):
    @return_json
    @json_parser
    def post(self, data):
        grid.set_all(**data)
        return {"state": "success"}

class MainHandler(tornado.web.RequestHandler):
    def post(self):
        self.get()

    def get(self):
        global grid

        self.set_header("Content-Type", "text/plain")

        json = tornado.escape.json_decode(self.get_argument("json"))
        print("Received: %s" % json)

        if "action" in json:
            action = json["action"]

            if action == "set_state":
                self.write("set_state\n")
                self.write("x: %d\n" % json["x"])
                self.write("y: %d\n" % json["y"])
                self.write("body: %s\n" % json["body"])
                
                grid.set_state(json["x"], json["y"], **json["body"])
            elif action == "commit":
                self.write("commit\n")

                grid.commit()
            else:
                self.set_status(501)
                self.write("Error: Unknown action (%s)\n" % action)
        else:
            self.set_status(400)
            self.write("Error: No action specified!\n")


application = tornado.web.Application([
    (r"/display", MainHandler),
    (r'/lights', LightsHandler),
    (r'/lights/all', LightsAllHandler),
])


def init_lightgrid():
    with open('config.json', 'r') as file:
        config = tornado.escape.json_decode(file.read())
        config["grid"] = [ [ (x[0], x[1]) for x in row ] for row in config["grid"] ]
        print(config)

    return playhouse.LightGrid(config["usernames"], config["grid"], config["ips"], buffered=False) 



if __name__ == "__main__":
    grid = init_lightgrid()

    application.listen(4711)
    tornado.ioloop.IOLoop.instance().start()