import copy
import http.client
import io
import json
import socket
import urllib.parse
import urllib.request
import collections
from xml.etree import ElementTree

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
        def default():
            return copy.copy(defaults)
        self.buffer = collections.defaultdict(default)
        
        for mac, ip in ip_addresses.items():
            self.bridges[mac] = http.client.HTTPConnection(ip)
        self.grid = copy.deepcopy(grid)
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
                self._send_request(bridge, "PUT", "/lights/{}/state".format(n), v) 
        self.buffer.clear()
                
    def _send_request(self, bridge, method, url, body=None):
        if body is not None:
            body = json.dumps(body)
        bridge.request(method, "/api/{}{}".format(self.username, url), body)
        # We should consider having asynchronous request to minimize delay
        return json.loads(bridge.getresponse().read().decode('utf-8'))
        
                        
                   

        
     