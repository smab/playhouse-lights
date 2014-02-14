
import http.client
import json

client = http.client.HTTPConnection("localhost", 4711)
json = '[[{"mac": "001788182e78", "lamp": 3}, {"mac": "001788182e78", "lamp": 1}, {"mac": "001788182e78", "lamp": 2}], [{"mac": "00178811f9c2", "lamp": 1}, {"mac": "00178811f9c2", "lamp": 3}, {"mac": "00178811f9c2", "lamp": 2}], [{"mac": "001788182c73", "lamp": 1}, {"mac": "001788182c73", "lamp": 2}, {"mac": "001788182c73","lamp": 3}]]'
client.request("POST", "/grid",json)

print(client.getresponse().read())


