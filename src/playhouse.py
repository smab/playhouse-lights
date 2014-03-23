
import collections
import copy
import http.client
import io
import json
import logging
import socket
import traceback
import urllib.parse
import urllib.request
from xml.etree import ElementTree

import tornado.escape
import tornado.gen
import tornado.httpclient
try:
    import tornado.curl_httpclient
    tornado.httpclient.AsyncHTTPClient.configure(tornado.curl_httpclient.CurlAsyncHTTPClient)
except ImportError:
    pass # use slower default implementation


class BridgeAlreadyAddedException(Exception):
    pass

class OutsideGridException(Exception):
    pass

class NoBridgeAtCoordinateException(Exception):
    pass

class HueAPIException(Exception):
    def __init__(self, error):
        super().__init__("{}: {}".format(error["error"]["address"], error["error"]["description"]))
        self.address = error["error"]["address"]
        self.description = error["error"]["description"]
        self.type = error["error"]["type"]

class NoLinkButtonPressedException(HueAPIException):
    pass

class UnknownBridgeException(Exception):
    def __init__(self, mac):
        self.mac = mac



class Bridge:
    
    @classmethod
    @tornado.gen.coroutine
    def create_bridge(cls, ip, username=None, defaults={"transitiontime": 0}, timeout=2):
        bridge = cls()
        bridge.defaults = defaults
        bridge.username = username
        bridge.ipaddress = ip
        bridge.client = tornado.httpclient.AsyncHTTPClient()
        bridge.timeout = timeout
        
        try:
            if (yield bridge.send_request("GET", "/config"))['name'] != "Philips hue":
                raise Exception()
            
            res = yield bridge.http_request("GET", "/description.xml")
            if res.code != 200:
                raise Exception()
            
            et, ns = parse_description(res.buffer)
            desc = et.find('./default:device/default:modelDescription', namespaces=ns)
            if desc.text != "Philips hue Personal Wireless Lighting":
                raise Exception()
            
            bridge.serial_number = et.find('./default:device/default:serialNumber', namespaces=ns).text
        except:
            raise Exception("{}: not a Philips Hue bridge".format(ip))
        
        yield bridge.update_info()
        
        return bridge
    
    def set_defaults(self, defaults):
        self.defaults = defaults
    
    @tornado.gen.coroutine
    def http_request(self, method, url, body=None):
        logging.debug("Sending request %s %s (data: %s) to %s",
                      method, url, body, self.ipaddress)
        response = yield self.client.fetch("http://{}{}".format(self.ipaddress, url),
                                           method=method, body=body, request_timeout=self.timeout)
        logging.debug("Response from bridge: %s", response)
        return response
    
    @tornado.gen.coroutine
    def send_raw(self, method, url, body=None):
        if body is not None:
            body = json.dumps(body)
        
        res = yield self.http_request(method, url, body)
        if res is None:
            return
        
        res = tornado.escape.json_decode(res.body)
        
        exceptions = {
            101: NoLinkButtonPressedException
        }
        if type(res) is list:
            for item in res:
                if "error" in item:
                    raise exceptions.get(item["error"]["type"], HueAPIException)(item)
        
        return res
    
    @tornado.gen.coroutine
    def send_request(self, method, url, body=None):
        user = self.username
        if user is None:
            user = "none"
        
        return (yield self.send_raw(method, "/api/{}{}".format(user, url), body))
    
    @tornado.gen.coroutine
    def _set_state(self, url, args):
        defs = self.defaults.copy()
        defs.update(args)
        
        if 'rgb' in defs:
            defs['xy'] = rgb2xy(*defs['rgb'])
            del defs['rgb']
        
        return (yield self.send_request("PUT", url, body=defs))
    
    @tornado.gen.coroutine
    def set_state(self, i, **args):
        return (yield self._set_state('/lights/{}/state'.format(i), args))
    
    @tornado.gen.coroutine
    def set_group(self, i, **args):
        return (yield self._set_state('/groups/{}/action'.format(i), args))
    
    @tornado.gen.coroutine
    def get_lights(self):
        return (yield self.send_request("GET", "/lights"))
    
    @tornado.gen.coroutine
    def get_new_lights(self):
        return (yield self.send_request("GET", "/lights/new"))
    
    @tornado.gen.coroutine
    def search_lights(self):
        return (yield self.send_request("POST", "/lights"))
    
    @tornado.gen.coroutine
    def get_bridge_info(self):
        return (yield self.send_request("GET", "/"))
    
    @tornado.gen.coroutine
    def set_username(self, username):
        self.username = username
        yield self.update_info()
    
    @tornado.gen.coroutine
    def create_user(self, devicetype, username=None):
        body = {'devicetype': devicetype}
        if username is not None:
            body['username'] = username
        res = (yield self.send_raw("POST", "/api", body))[0]
        yield self.set_username(res['success']['username'])
        return self.username
    
    @tornado.gen.coroutine
    def update_info(self):
        info = yield self.send_request("GET", "/config")
        
        if self.username is None or 'mac' not in info:
            self.logged_in = False
        else:
            self.logged_in = True
        
        self.gateway = info.get('gateway', None)
        self.netmask = info.get('netmask', None)
        self.name = info.get('name', None)
        self.mac = info.get('mac', None)


class LightGrid:
    def __init__(self, usernames={}, grid=[], buffered=False, defaults = {}):
        """Create a new light grid-
        
        username - Map of serial number -> username pairs
        grid - A list of list of tuples (mac address, light). Maps grid pixels to specific lamps belonging to specific bridges. The top list contains pixel rows from the highest to the lowest. Each pixel row is a list containg the tuples from the left-most pixel in the row to the right-most
        ip-addresses - Maps Hue bridge id's to IP addresses
        """
        self.defaults = defaults
        self.bridges = {}
        self.usernames = usernames
        self.buffered = buffered        
        self.buffer = collections.defaultdict(dict)
        
        """for ip in ip_addresses:
            bridge = Bridge(ip, defaults=defaults)
            self.bridges[bridge.serial_number] = bridge
            if bridge.serial_number in usernames:
                bridge.set_username(usernames[bridge.serial_number])"""
        self.set_grid(grid)
        
        #self.state = {}
        #self._synchronize_state()
        
#    def _synchronize_state(self):
#        for mac, bridge in self.bridges.items():
#            data = self._send_request(bridge, "GET", "/")
#            for k, v in data["lights"].items():
#                self.state[(mac, int(k))] = v["state"]
                    
    
    @tornado.gen.coroutine
    def add_bridge(self, ip_address_or_bridge, username=None):
        # can take an already instantiated bridge instance
        if type(ip_address_or_bridge) is Bridge:
            bridge = ip_address_or_bridge
        else:
            bridge = yield Bridge.create_bridge(ip_address_or_bridge, username, self.defaults)
        
        if bridge.serial_number in self.bridges:
            raise BridgeAlreadyAddedException()
        
        if bridge.username is None and bridge.serial_number in self.usernames:
            bridge.set_username(self.usernames[bridge.serial_number])
        self.bridges[bridge.serial_number] = bridge
        return bridge
    
    def set_usernames(self, usernames):
        self.usernames = usernames
    
    def set_grid(self, grid):
        #for row in grid:
        #    for (mac, lamp) in row:
        #        pass
        #        if mac not in self.bridges:
        #            raise UnknownBridgeException
        self.grid = grid
        self.height = len(self.grid)
        self.width = max(len(x) for x in self.grid) if self.height > 0 else 0
    
    @tornado.gen.coroutine
    def set_state(self, x, y, **args):
        """Set the state for a specific lamp. If this grid is buffered, the state will not be sent to the lamp directly.
        
        x -- X coordinate
        y -- Y coordinate
        args -- State argument, see Philips Hue documentation
        
        """
        if x >= self.width or y >= self.height:
            raise OutsideGridException
        
        mac = self.grid[y][x][0]
        if mac not in self.bridges:
            raise NoBridgeAtCoordinateException
        
        row = self.grid[y]
        cell = row[x]
        self.buffer[cell].update(args)
#        for k, v in args.items():
#                if self.state[cell].get(k) == v:
#                    del self.buffer[cell][k]
        if not self.buffered:
            yield self.commit()
    
    @tornado.gen.coroutine
    def set_all(self, **args):
        keys = []
        for bridge in self.bridges.values():
            bridge.set_group(0, callback=(yield tornado.gen.Callback(bridge)), **args)
            keys.append(bridge)
        # TODO: get exceptions
        results = yield tornado.gen.WaitAll(keys)
    
    @tornado.gen.coroutine
    def commit(self):
        """Commit saved state changes to the lamps"""
        keys = []
        
        for k, v in self.buffer.items():
            if len(v) != 0:
                mac, n = k
                bridge = self.bridges[mac]
                bridge.set_state(n, callback=(yield tornado.gen.Callback((bridge, n))), **v)
                keys.append((bridge, n))
        
        self.buffer.clear()
        # TODO: get exceptions
        results = yield tornado.gen.WaitAll(keys)
        logging.debug("Waited for %s", results)
        


def discover(attempts=2, timeout=2):
    socket.setdefaulttimeout(timeout)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    
    message = b'M-SEARCH * HTTP/1.1\r\n'\
              b'HOST: 239.255.255.250:1900\r\n'\
              b'MAN: "ssdp:discover"\r\n'\
              b'ST: my:test\r\n'\
              b'MX: 3\r\n\r\n'
    
    locations = set()
    
    logging.info("Broadcasting %s times", attempts)
    for i in range(attempts):
        logging.info("Broadcast #%s", i + 1)
        sock.sendto(message, ("239.255.255.250", 1900))
        while True:
            try:
                logging.debug("Waiting for response")
                
                raw, (address, port) = sock.recvfrom(1024)
                response = io.BytesIO(raw)
                
                logging.debug("Got response: %s", response)
                
                response.makefile = lambda *args, **kwargs: response
                header = http.client.HTTPResponse(response)
                header.begin()
                
                logging.debug("Response status was %s and location header was %s",
                              header.status, header.getheader('location'))
                if header.status == 200 and header.getheader('location') is not None:
                    logging.debug("Adding %s to list", address)
                    locations.add(address)
            except socket.timeout:
                logging.info("No response from broadcast #%s in %s second(s)", i + 1, timeout)
                break
    
    logging.debug("List of addresses is %s", locations)
    bridges = []
    for loc in locations:
        try:
            logging.debug("Attempting to find a bridge at %s", loc)
            bridges.append(Bridge.create_bridge(loc))
            logging.debug("Bridge found")
        except:
            logging.debug("Bridge not found", exc_info=True)
            pass
    
    return bridges

def parse_description(f):
    root = None
    namespaces = {}
    for event, elem in ElementTree.iterparse(f, ("start", "start-ns")):
        if event == "start-ns":
            namespaces[elem[0] if elem[0] != '' else 'default'] = elem[1]
        elif event == "start":
            if root is None:
                root = elem
    
    return ElementTree.ElementTree(root), namespaces

def rgb2xy(red, green, blue):

    # Apply gamma
    if red > 0.04045: red = ((red + 0.055) / (1.0 + 0.055))**2.4
    else: red = red / 12.92

    if green > 0.04045: green = ((green + 0.055) / (1.0 + 0.055))**2.4
    else: green = green / 12.92

    if blue > 0.04045: blue = ((blue + 0.055) / (1.0 + 0.055))**2.4
    else: blue = blue / 12.92

    # Convert to XYZ
    X = red * 0.649926 + green * 0.103455 + blue * 0.197109;
    Y = red * 0.234327 + green * 0.743075 + blue * 0.022598;
    Z = red * 0.000000 + green * 0.053077 + blue * 1.035763;

    # Calculate xy values
    x = X / (X + Y + Z);
    y = Y / (X + Y + Z);

    return (x, y)
        
     