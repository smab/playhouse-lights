import copy
import http.client
import io
import json
import socket
import urllib.parse
import urllib.request
import collections
from xml.etree import ElementTree

class Bridge:
    
    def __init__(self, ip, username, defaults={"transitiontime": 0}):
        self.defaults = defaults
        self.username = username
        self.bridge = http.client.HTTPConnection(ip)
        self.get_info()
        
        
    def set_defaults(self, defaults):
        self.defaults = defaults
    
    def send_request(self, method, url, body=None):
        if body is not None:
            body = json.dumps(body)
        
        self.bridge.request(method, "/api/{}{}".format(self.username, url), body)
        return json.loads(self.bridge.getresponse().read().decode('utf-8'))
    
    def _set_state(self, url, args):
        defs = self.defaults.copy()
        defs.update(args)
        
        if 'rgb' in defs:
            defs['xy'] = rgb2xy(*defs['rgb'])
            del defs['rgb']
        
        return self.send_request("PUT", url, defs)
    
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
    
    def get_info(self):
        info = self.send_request("GET", "/config")
        self.ipaddress = info['ipaddress']
        self.gateway = info['gateway']
        self.netmask = info['netmask']
        self.name = info['name']
        self.mac = info['mac']


class LightGrid:
    def __init__(self, username, grid, ip_addresses, buffered=False, defaults = {}):
        """Create a new light grid-
        
        username - Username to the Hue bridges
        grid - A list of list of tuples (mac address, light). Maps grid pixels to specific lamps belonging to specific bridges. The top list contains pixel rows from the highest to the lowest. Each pixel row is a list containg the tuples from the left-most pixel in the row to the right-most
        ip-addresses - Maps Hue bridge id's to IP addresses
        """
        self.defaults = defaults
        self.bridges = {}
        self.username = username
        self.buffered = buffered        
        self.buffer = collections.defaultdict(dict)
        
        for mac, ip in ip_addresses.items():
            self.bridges[mac] = Bridge(ip, username, defaults)
        self.grid = grid
        self.height = len(self.grid)
        self.width = max([len(x) for x in self.grid])
        
        #self.state = {}
        #self._synchronize_state()
        
#    def _synchronize_state(self):
#        for mac, bridge in self.bridges.items():
#            data = self._send_request(bridge, "GET", "/")
#            for k, v in data["lights"].items():
#                self.state[(mac, int(k))] = v["state"]
                    
    def set_state(self, x, y, **args):
        """Set the state for a specific lamp. If this grid is buffered, the state will not be sent to the lamp directly.
        
        x -- X coordinate
        y -- Y coordinate
        args -- State argument, see Philips Hue documentation
        
        """
        if x >= self.width or y >= self.height:
            raise Exception
        row = self.grid[y]
        cell = row[x]
        self.buffer[cell].update(args)
#        for k, v in args.items():
#                if self.state[cell].get(k) == v:
#                    del self.buffer[cell][k]
        if not self.buffered:
            self.commit()
            
    def commit(self):
        """Commit saved state changes to the lamps"""
        for k, v in self.buffer.items():
            if len(v) != 0:
                mac, n = k
                bridge = self.bridges[mac]
                bridge.set_state(n, **v) 
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
                
                response = io.BytesIO(sock.recv(1024))
                response.makefile = lambda *args, **kwargs: response
                header = http.client.HTTPResponse(response)
                header.begin()
                print(header.status)
                if header.status == 200:
                    locations.add(header.getheader('location'))
            except socket.timeout:
                break
    
    bridges = {}
    for loc in locations:
        root = ElementTree.parse(urllib.request.urlopen(loc)).getroot()
        NS = "{urn:schemas-upnp-org:device-1-0}"
        if root.find("./{ns}device/{ns}modelName".format(ns=NS)).text == 'Philips hue bridge 2012':
            mac = root.find("./{ns}device/{ns}serialNumber".format(ns=NS)).text
            url = urllib.parse.urlparse(root.find("./{ns}URLBase".format(ns=NS)).text)
            bridges[mac] = url.netloc
    
    return bridges

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
        
     