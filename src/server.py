
import json

import tornado.ioloop
import tornado.web

import playhouse

def return_json(func):
    def new_func(self, *args, **kwargs):
        self.set_header("Content-Type", "application/json")
        data = func(self, *args, **kwargs)
        self.write(json.dumps(data))
    return new_func

def json_parser(func):
    def new_post(self, *args, **kwargs):
        try:
            rawjson = self.request.body.decode('utf-8')
            data = json.loads(rawjson)
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
            #lightgrid.set_state(data['x'], data['y'], **data['change'])
            pass
        return {"state": "success"}

class LightsAllHandler(tornado.web.RequestHandler):
    @return_json
    @json_parser
    def post(self, data):
        #lightgrid.set_all(data)
        return {"state": "success"}

application = tornado.web.Application([
    (r'/lights', LightsHandler),
    (r'/lights/all', LightsAllHandler),
])

if __name__ == "__main__":
    application.listen(8081)
    tornado.ioloop.IOLoop.instance().start()
