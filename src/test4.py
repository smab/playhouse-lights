
import random
import sys
import time

import playhouse

#bridges = playhouse.discover()

usernames = {
    "001788182e78": "25116fda765dc973fae9b4611ec2fb3",
    "00178811f9c2": "newdeveloper",
    "001788182c73": "3PeT4zaFlqOtf2Pr"
}

grid = [
    [("001788182c73", 1), ("001788182c73", 2), ("001788182c73", 3)],
    [("001788182e78", 3), ("001788182e78", 2), ("001788182e78", 1)],
    [("00178811f9c2", 2), ("00178811f9c2", 3), ("00178811f9c2", 1)]
]

#lg = playhouse.LightGrid(usernames, grid, {"130.237.228.161:80", "130.237.228.58:80", "130.237.228.213:80"}, buffered=False, defaults={"transitiontime": 0})

bridge = playhouse.Bridge("130.237.228.161")
bridge.set_username(usernames[bridge.serial_number])

print(bridge.get_bridge_info())

hue = 0
while True:
    hue = 45000 - hue
    bridge.set_state(1, hue=hue)
    bridge.get_bridge_info()
    time.sleep(0.2)

