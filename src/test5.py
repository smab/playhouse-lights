import random
import sys
import time


import playhouse

usernames = {
    "001788182e78": "25116fda765dc973fae9b4611ec2fb3",
    "00178811f9c2": "newdeveloper",
    "001788182c73": "3PeT4zaFlqOtf2Pr"
}

grid = [
    [("00178811f9c2", 9), ("00178811f9c2", 4), ("00178811f9c2", 7)],
    [("00178811f9c2", 5), ("00178811f9c2", 6), ("00178811f9c2", 8)],
    [("00178811f9c2", 2), ("00178811f9c2", 3), ("00178811f9c2", 1)]
]

ips = {"130.237.228.161:80"}#, "130.237.228.58:80", "130.237.228.213:80"}

lg = playhouse.LightGrid(usernames, grid, ips, buffered=False)

lg.set_state(0,0,effect="colorloop")
lg.set_state(0,1,effect="colorloop")
lg.set_state(0,2,effect="colorloop")
lg.set_state(1,0,effect="colorloop")
lg.set_state(1,1,effect="colorloop")
lg.set_state(1,2,effect="colorloop")
lg.set_state(2,0,effect="colorloop")
lg.set_state(2,1,effect="colorloop")
lg.set_state(2,2,effect="colorloop")