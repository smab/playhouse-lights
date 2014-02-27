
import collections
import copy
import http.client
import io
import json
import socket
import traceback
import urllib.parse
import urllib.request
from xml.etree import ElementTree

import tornado.httpclient
import tornado.curl_httpclient
import tornado.escape


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
    def __this__(self, mac):
        self.mac = mac



class Bridge:
    
    def __init__(self, ip, username=None, defaults={"transitiontime": 0}, timeout=2):
        self.defaults = defaults
        self.username = username
        self.ipaddress = ip
        #self.bridge = http.client.HTTPConnection(ip, timeout=2)
        self.bridge_async = tornado.curl_httpclient.CurlAsyncHTTPClient()
        self.bridge_sync = tornado.httpclient.HTTPClient()
        self.timeout = timeout
        
        try:
            if self.send_request("GET", "/config")['name'] != "Philips hue":
                raise Exception()
            
            res = self.http_request("GET", "/description.xml")
            if res.code != 200:
                raise Exception()
            
            et, ns = parse_description(res.buffer)
            desc = et.find('./default:device/default:modelDescription', namespaces=ns)
            if desc.text != "Philips hue Personal Wireless Lighting":
                raise Exception()
            
            self.serial_number = et.find('./default:device/default:serialNumber', namespaces=ns).text
        except:
            raise Exception("{}: not a Philips Hue bridge".format(ip))
        
        self.update_info()
    
    
    def set_defaults(self, defaults):
        self.defaults = defaults
    
    def http_request(self, method, url, body=None, async=False):
        if async:
            def fetch_result(response):
                # TODO: the fuck do we do with the responses here?
                #print(response)
                pass
            self.bridge_async.fetch("http://{}{}".format(self.ipaddress, url), fetch_result,
                                    method=method, body=body, request_timeout=self.timeout)
        else:
            return self.bridge_sync.fetch("http://{}{}".format(self.ipaddress, url),
                                          method=method, body=body, request_timeout=self.timeout)
        
    
    def send_raw(self, method, url, body=None, async=False):
        if body is not None:
            body = json.dumps(body)
        
        res = self.http_request(method, url, body, async)
        if res is None:
            return
        res = tornado.escape.json_decode(res.body)
        
        #res = json.loads(self.bridge.getresponse().read().decode('utf-8'))
        
        exceptions = {
            101: NoLinkButtonPressedException
        }
        if type(res) is list:
            for item in res:
                if "error" in item:
                    raise exceptions.get(item["error"]["type"], HueAPIException)(item)
        
        return res
    
    def send_request(self, method, url, body=None, async=False):
        user = self.username
        if user is None:
            user = "none"
        
        return self.send_raw(method, "/api/{}{}".format(user, url), body, async)
    
    def _set_state(self, url, args):
        defs = self.defaults.copy()
        defs.update(args)
        
        if 'rgb' in defs:
            defs['xy'] = rgb2xy(*defs['rgb'])
            del defs['rgb']
        
        return self.send_request("PUT", url, defs, async=True)
    
    def set_state(self, i, **args):
        return self._set_state('/lights/{}/state'.format(i), args)
    
    def set_group(self, i, **args):
        return self._set_state('/groups/{}/action'.format(i), args)
    
    def get_lights(self):
        return self.send_request("GET", "/lights")
    
    def get_new_lights(self):
        return self.send_request("GET", "/lights/new")
    
    def search_lights(self):
        return self.send_request("POST", "/lights")
    
    def get_bridge_info(self):
        return self.send_request("GET", "/")
    
    def set_username(self, username):
        self.username = username
        self.update_info()
    
    def create_user(self, devicetype, username=None):
        body = {'devicetype': devicetype}
        if username is not None:
            body['username'] = username
        res = self.send_raw("POST", "/api", body)[0]
        self.username = res['success']['username']
        self.update_info()
        return self.username
    
    def update_info(self):
        info = self.send_request("GET", "/config")
        
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
                    
    
    def add_bridge(self, ip_address, username=None):
        bridge = Bridge(ip_address, username, self.defaults)
        if bridge.serial_number in self.bridges:
            raise BridgeAlreadyAddedException()
        
        if username is None and bridge.serial_number in self.usernames:
            bridge.set_username(self.usernames[bridge.serial_number])
        self.bridges[bridge.serial_number] = bridge
        return bridge
    
    def set_grid(self, grid):
        for row in grid:
            for (mac, lamp) in row:
                pass
        #        if mac not in self.bridges:
        #            raise UnknownBridgeException
        self.grid = grid
        self.height = len(self.grid)
        self.width = max(len(x) for x in self.grid) if self.height > 0 else 0
    
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
            self.commit()
    
    def set_all(self, **args):
        for bridge in self.bridges.values():
            try:
                bridge.set_group(0, **args)
            except HueAPIException:
                # TODO: do something with the exception
                pass
    
    def commit(self):
        """Commit saved state changes to the lamps"""
        for k, v in self.buffer.items():
            if len(v) != 0:
                mac, n = k
                bridge = self.bridges[mac]
                try:
                    bridge.set_state(n, **v)
                except HueAPIException:
                    # TODO: do something with the exception
                    pass
        self.buffer.clear()
                


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
    
    for i in range(attempts):
        sock.sendto(message, ("239.255.255.250", 1900))
        while True:
            try:
                raw, (address, port) = sock.recvfrom(1024)
                response = io.BytesIO(raw)
                response.makefile = lambda *args, **kwargs: response
                header = http.client.HTTPResponse(response)
                header.begin()
                print(header.status)
                if header.status == 200 and header.getheader('location') is not None:
                    locations.add(address)
            except socket.timeout:
                break
    
    bridges = []
    for loc in locations:
        try:
            bridges.append(Bridge(loc))
        except:
            traceback.print_exc()
            pass # invalid bridge
        """et, ns = parse_description(urllib.request.urlopen(loc))
        if et.find("./default:device/default:modelDescription", namespaces=ns).text == 'Philips hue Personal Wireless Lighting':
            mac = et.find("./default:device/default:serialNumber", namespaces=ns).text
            url = urllib.parse.urlparse(et.find("./default:URLBase", namespaces=ns).text)
            bridges[mac] = url.netloc"""
    
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
        
     