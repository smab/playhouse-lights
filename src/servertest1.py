
import http.client
import json

client = http.client.HTTPConnection("localhost", 4711)
client.request("POST", "/lights/all", '{"hue": 0, "bri": 0, "sat": 255}')

print(client.getresponse().read())
