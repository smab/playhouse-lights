import os
import sys
import playhouse
import socket

import tornado.concurrent
import tornado.gen
import tornado.ioloop
import tornado.stack_context

@tornado.gen.coroutine
def do_stuff():
    try:
        print("Make sure that the bridge is factory-reset")
        while True:
            prompt = input("Is it (y/n)?")
            if prompt[0] == "y":
                break
            if prompt[0] == "n":
                loop.stop()
                sys.exit()
        print("Beginning search for bridges")
        bridges = yield playhouse.discover()
      
        bridge = None
        if len(bridges) != 0:
            bridge_map = dict()
            print("Found bridges:")
            for b in bridges:
                bridge_map[bridges.mac] = bridges
                print(bridges.mac)   
            while True:
                mac = input("Enter the bridge MAC, or nothing to pick a manual IP address")
                if mac == "":
                    break
                bridge = bridge_map.get(mac)
                if bridge is None:
                    print("Not a correct MAC address")
                else:
                    break
        else:
            print("No bridges found")
        if bridges is None:
            ip = input("Enter manual IP address:")
            bridge = yield Bridge(ip)
    
        while True:
            try:
                input("Press bridge link button, so that a new user can be created, press enter afterwards:")          
                name = bridge.create_user("resetutil")
                break
            except playhouse.NoLinkButtonPressedException:
                print("Failed to create user")
                pass
        light_num = None
        while light_num is None:
            try:
                light_num = int(input("Enter the number of lights to add to bridge:"))
                if light_num <= 0:
                    raise ValueError
            except ValueError:
                print("Invalid number, must be a positive integer")
        print("Plug in each lamp one by one")
        while True:
            while True:
                prompt = input("Do you want to reset a new lamp (y/n)?")
                if prompt[0] == "y":
                    break
                if prompt[0] == "n":
                    loop.stop()
                    sys.exit()
            input("Plug in lamp, then press enter")
            s = socket.socket(socket.AF_INET)
            s.connect((bridge.ipaddress, 30000))
            f = s.makefile('r')
            s.send(b'[Link,Touchlink]')
            ack = f.readline()
            res = f.readline()
            
      
         
         
         
            
            
    except ex:
        print(ex)
    finally:
        loop.stop()

loop = tornado.ioloop.IOLoop.instance()
loop.add_callback(do_stuff)
loop.start()