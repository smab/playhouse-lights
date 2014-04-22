import datetime
import os
import sys
import socket
import traceback

import tornado.concurrent
import tornado.gen
import tornado.ioloop
import tornado.stack_context

import playhouse

@tornado.gen.coroutine
def do_stuff():
    usernames = {}
    try:
        while True:
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
            if bridge is None:
                while True:
                    try:
                        ip = input("Enter manual IP address:")
                        bridge = yield playhouse.Bridge(ip)
                        print("Using bridge with MAC adress", bridge.serial_number)
                        break
                    except playhouse.NoBridgeFoundException:
                        print("No bridge found at given adress")

            while True:
                try:
                    input("Press bridge link button, so that a new user can be created, press enter afterwards:")
                    name = yield bridge.create_user("resetutil")
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
            for i in range(1, light_num + 1):
                print("Preparing to reset lamp {}; make sure that only " \
                    "one light is plugged in and that the bridge is close to the light.".format(i))
                while True:
                    try:
                        input("Press enter to perform a reset attempt:")
                        yield bridge.reset_nearby_bulb()
                        break
                    except playhouse.BulbNotResetException:
                        print("Failed to reset a bulb, trying again...")
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
        print(usernames)
        loop.stop()

loop = tornado.ioloop.IOLoop.instance()
loop.add_callback(do_stuff)
loop.start()
