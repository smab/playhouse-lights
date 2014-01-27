
import random
import time
import playhouse


grid = [
    [("001788182e78",1),("001788182e78",2),("001788182e78",3)]
]

ip_addresses = {"192.168.0.102"}
usernames = {"001788182e78": "25116fda765dc973fae9b4611ec2fb3"}

grid = playhouse.LightGrid(usernames, grid, ip_addresses, buffered=True, defaults={"transitiontime":0}) 
colors = [0,10000, 45000]
i = 0
grid.set_state(0,0, on=True, hue=colors[(i) % 3], sat=255, bri=255)
grid.set_state(1,0, on=True, hue=colors[(i+1) % 3], sat=255, bri=255)
grid.set_state(2,0, on=True, hue=colors[(i+2) % 3], sat=255, bri=255)
while True:
    grid.set_state(0,0, hue=colors[(i) % 3])
    grid.set_state(1,0, hue=colors[(i+1) % 3])
    grid.set_state(2,0, hue=colors[(i+2) % 3])
    grid.commit()
    i = (i+1) % 3
    time.sleep(1)



