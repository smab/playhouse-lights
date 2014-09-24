# Playhouse: Making buildings into interactive displays using remotely controllable lights.
# Copyright (C) 2014  John Eriksson, Arvid Fahlström Myrman, Jonas Höglund,
#                     Hannes Leskelä, Christian Lidström, Mattias Palo,
#                     Markus Videll, Tomas Wickman, Emil Öhman.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Resetutil is a simple utility for pairing Philips hue bulbs to bridges.

It is a command-line utility and can be run by executing ``python3 resetutil.py``.

The program will first attempt to list all available bridges in the network.
It will then list all the options and allow you to pick a bridge or write a IP address
if the bridge you want to connect to is not in the list.

Once a bridge has been selected, the program will enter a loop where you can reset as many
lamps as you want. To reset a lamp, plug it in within 30 centimeters from the bridge, make
sure that no other nearby lamps are plugged in, and type "reset".
When you are done with all lamps, type "done".
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

class _StopError(Exception):
    pass

def ask_for_y(s):
    while True:
        prompt = input(s)
        if prompt[0] == "y":
            break
        if prompt[0] == "n":
            raise _StopError


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
                bridge_map[b.serial_number] = b
                print(b.serial_number)
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

@tornado.gen.coroutine
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


@tornado.gen.coroutine
def reset_lamp(bridge):
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

            name = yield create_user(bridge)

            light_num = yield reset_lamp(bridge)

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
    except _StopError:
        pass
    except Exception:
        traceback.print_exc()
    finally:
        if len(usernames) > 0:
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
        else:
            print("No bridges were reset.")

if __name__ == '__main__':
    loop = tornado.ioloop.IOLoop.instance()
    loop.run_sync(do_stuff)
