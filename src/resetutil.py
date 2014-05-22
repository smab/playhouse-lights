
"""
Resetutil is a simple utility for pairing Philips hue bulbs to bridges. It is a command-line utility. It is run simply by executing "python resetutil.py"

The program will first attempt to list all available bridges in the network. It will then list all the options and allow you to pick a bridge or write a IP address if the bridge you want to connect to is not in the list.

Once a bridge has been selected, the program will enter a loop where you can reset as many lamps as you want. To reset a lamp, plug it in within 30 centimeters from the bridge, make sure that no other nearby lamps are plugged in and type "reset". When you are done with all lamps, type "done".

"""

import datetime
import json
import os
import sys
import socket
import traceback

import tornado.concurrent
import tornado.gen
import tornado.ioloop
import tornado.stack_context

import playhouse

def ask_for_y(s):
    while True:
        prompt = input(s)
        if prompt[0] == "y":
            break
        if prompt[0] == "n":
            loop.stop()
            sys.exit()


@tornado.gen.coroutine
def enter_manual_ip():
    while True:
        try:
            ip = input("Enter manual IP address:")
            bridge = yield playhouse.Bridge(ip)
            print("Using bridge with MAC adress", bridge.serial_number)
            break
        except playhouse.NoBridgeFoundException:
            print("No bridge found at given adress")
    return bridge

@tornado.gen.coroutine
def pick_bridge():

    while True:
        print("Beginning search for bridges")
        bridges = yield playhouse.discover()
        bridge_map = dict()
        if len(bridges) != 0:
            print("Found bridges:")
            for b in bridges:
                bridge_map[bridges.mac] = bridges
                print(bridges.mac)
            print("Enter the bridge MAC to pick a bridge")
        else:
            print("No bridges found")
        print("Enter 'manual' to enter a manual IP address")
        print("Enter 'search' to search for bridges again")
        while True:
            i = input(":")
            if i == "search":
                break
            elif i=="manual":
                return (yield enter_manual_ip())
            else:
                bridge = bridge_map.get(i)
                if bridge is None:
                    print("No such bridge exists")
                else:
                    return bridge


def create_user(bridge):
    while True:
        try:
            input("Press bridge link button, so that a new user can be created, press enter afterwards:")
            name = yield bridge.create_user("resetutil")
            return name
        except playhouse.NoLinkButtonPressedException:
            print("Failed to create user")
            pass

def enter_num(s):
    light_num = None
    while light_num is None:
        try:
            light_num = int(input(s))
            if light_num <= 0:
                raise ValueError
        except ValueError:
            print("Invalid number, must be a positive integer")
    return light_num

    
def reset_lamp(): 
    resets = 0
    while True:
        print("Plug in a lamp, and enter 'reset' to reset a lamp")
        print("Enter 'done' when all lamps have been reset")
        i = input(":")
        if i == "reset":       
            try:
                yield bridge.reset_nearby_bulb()
                print("Lamp was successfully reset")
                resets += 1
            except playhouse.BulbNotResetException:
                print("Failed to reset lamp")
        elif i == "done":
            break
    return resets
    

@tornado.gen.coroutine
def do_stuff():
    usernames = {}
    try:
        while True:
            print("Make sure that the bridge is factory-reset")

            prompt = ask_for_y("Is it (y/n)?")

            bridge = yield pick_bridge()

            name = create_user()
            
            light_num = yield reset_lamp()
            
            if light_num != 0:
                print("All bulbs reset")
                input("Plug in all {} reset lamps and press enter:".format(light_num))
                yield bridge.search_lights()
                while True:
                    yield tornado.gen.Task(loop.add_timeout, datetime.timedelta(seconds=10))
                    res = yield bridge.get_new_lights()
                    if res['lastscan'] != 'active':
                        del res['lastscan']
                        print("Found", len(res), "lights")
                        break


            usernames[bridge.serial_number] = name

            while True:
                prompt = input("Repeat with another bridge(y/n)?")
                if prompt == 'y':
                    break
                elif prompt == 'n':
                    return

    except Exception:
        traceback.print_exc()
    finally:
        print("Write down these MAC addresses and usernames for later use in the web-based "
            "configuration system, or copy the newly created bridge_setup.json to "
            "the lamp server's root directory.")
        print("The usernames are vital in order for the lamp server "
            "to be able to communicate with the bridges.")

        print("{:<16}{}".format("MAC", "username"))
        print("{:<16}{}".format("---", "--------"))
        for mac, username in usernames.items():
            print("{:<16}{}".format(mac, username))
        with open("bridge_setup.json", 'w') as f:
            json.dump({"usernames": usernames}, f)

        loop.stop()

if __name__ == '__main__':
    loop = tornado.ioloop.IOLoop.instance()
    loop.add_callback(do_stuff)
    loop.start()
