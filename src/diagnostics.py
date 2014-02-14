

import http.client
import json
import time

def send_raw(conn, method, url, body=None):
    if body is not None:
        body = json.dumps(body)
    
    conn.request(method, url, body)
    return json.loads(conn.getresponse().read().decode('utf-8'))

client = http.client.HTTPConnection("localhost", 4711)

grid = send_raw(client, "GET", "/grid")

height = len(grid)
width = max([len(x) for x in grid])
print(width, height)

changes = []

for x in range(width): 
    for y in range(height):
        changes.append({"x":x,"y":y,"change":{"hue":0,"sat":0, "bri":0}})
  
send_raw(client, "POST", "/lights", changes)
changes.clear()
time.sleep(1)

delay = 0.5
while True:
    for x in range(width + 1): 
        changes = []
        for y in range(height):
            if x != width:
                changes.append({"x":x,"y":y,"change":{"sat":255, "transitiontime":0}})
            if x != 0:
                changes.append({"x":x-1,"y":y,"change":{"sat":0, "transitiontime":0}})
        send_raw(client, "POST", "/lights", changes)
        time.sleep(delay)
        
        
    for y in range(height + 1): 
        changes = []
        for x in range(height):
            if y != width:
                changes.append({"x":x,"y":y,"change":{"sat":255, "transitiontime":0}})
            if y != 0:
                changes.append({"x":x,"y":y-1,"change":{"sat":0, "transitiontime":0}})
        send_raw(client, "POST", "/lights", changes)
        time.sleep(delay)
      