
import http.client
import json

client = http.client.HTTPConnection("localhost", 8081)
client.request("POST", "/lights", '[{"x": 1, "y": 1, "change": {"hue": 0}}]')

print(client.getresponse().read())
