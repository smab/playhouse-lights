
import random
import time
import playhouse


grid = [
    [("001788fffe11f9c2",1),("001788fffe11f9c2",2),("001788fffe11f9c2",3)]
]

ip_addresses = {"001788fffe11f9c2":"192.168.1.24"}

grid = playhouse.LightGrid("newdeveloper", grid, ip_addresses, buffered=True, defaults={"transitiontime":0}) 
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



