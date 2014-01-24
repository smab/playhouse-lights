
import random
import time

import playhouse

morse_map = {
    "A":".-",
    "B":"-...",
    "C":"-.-.",
    "D":"-..",
    "E":".",
    "F":"..-.",
    "G":"--.",
    "H":"....",
    "I":"..",
    "J":".---",
    "K":"-.-",
    "L":".-..",
    "M":"--",
    "N":"-.",
    "O":"---",
    "P":".--.",
    "Q":"--.-",
    "R":".-.",
    "S":"...",
    "T":"-",
    "U":"..-",
    "V":"...-",
    "W":".--",
    "X":"-..-",
    "Y":"-.--",
    "Z":"--..",
    
}

def convert_morse(s):
    result = ""
    needspause = False
    for c in s:
        if c == ' ':
            result += "  " # two spaces
            needsspace = False
        else:
            if needspause:
                result += " "
            result += morse_map[c]
            needspause = True
    return result

   
    
def play_morse(grid, s, unit=0.3):
    i = 0
    needspause = False
    while i < len(s):
        c = s[i]
        if c == '.':
            if needspause:
                time.sleep(unit)   
            grid.set_state(0,0, bri=255)
            time.sleep(unit)
            grid.set_state(0,0, bri=0)
            needspause=True
        elif c == '-':
            if needspause:
                time.sleep(unit)   
            grid.set_state(0,0, bri=255)
            time.sleep(3*unit)
            grid.set_state(0,0, bri=0)
            needspause=True
        elif c == ' ':
            spacecount=0
            while s[i] == ' ' and i<len(s):
                spacecount+=1
                i+=1
            if spacecount == 1:
                time.sleep(3*unit)    
            else:
                time.sleep(7*unit)                    
            needspause=False
            i-=1
        i+=1

        
grid = [
    [("001788fffe11f9c2",1),("001788fffe11f9c2",2),("001788fffe11f9c2",3)]
]

ip_addresses = {"001788fffe11f9c2":"192.168.1.24"}

grid = playhouse.LightGrid("newdeveloper", grid, ip_addresses, defaults={"transitiontime":0})         
        
grid.set_state(0,0,on=True, sat=0)
grid.set_state(1,0,on=False)
grid.set_state(2,0,on=False)
morse = convert_morse("SMAB")
print(morse)
play_morse(grid,morse)
grid.set_state(0,0,on=False, sat=0)



# 0 = red
# 5000 = orange
# 10000 = yellow
# 15000 = yellow
# 20000 = green ?
# 50000 = magenta
