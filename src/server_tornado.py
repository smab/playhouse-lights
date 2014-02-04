import tornado.ioloop
import tornado.web

import playhouse

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