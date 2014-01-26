
import random
import sys
import time

import playhouse

bridge = playhouse.Bridge(next(iter(playhouse.discover().values())), "newdeveloper")
print("Current lights:", bridge.get_lights())
print(bridge.search_lights())

new_lights = bridge.get_new_lights()

while new_lights['lastscan'] == "active":
    print("Scanning...")
    time.sleep(5)
    new_lights = bridge.get_new_lights()

print("Finished!")
print("Result:", new_lights)
print("Current lights:", bridge.get_lights())

bridge.set_group(0, bri=0, sat=255, hue=0)

while True:
    time.sleep(1)
    
    for i in range(6):
        if i % 2 == 0:
            bridge.set_group(0, sat=255)
        else:
            bridge.set_group(0, sat=0)
        time.sleep(0.5)
    
    bridge.set_group(0, hue=25500)
    time.sleep(1)
    
    for i in range(3):
        prev_i = 3
        for i in range(1, 3+1):
            bridge.set_state(prev_i, sat=0)
            bridge.set_state(i, sat=255)
            prev_i = i
            time.sleep(0.5)
    
    bridge.set_group(0, sat=0, hue=0)
