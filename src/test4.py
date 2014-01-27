
import random
import sys
import time

import playhouse

bridges = playhouse.discover()

bridge = playhouse.Bridge("192.168.0.102")

usernames = {
    "001788182e78": "25116fda765dc973fae9b4611ec2fb3"
}

print(bridge.logged_in)
bridge.set_username(usernames[bridge.serial_number])
print(bridge.logged_in)

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
