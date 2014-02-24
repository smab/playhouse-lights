
import http.client
import json

client = http.client.HTTPConnection("localhost", 4711)
json = '{}'
client.request("POST", "/bridges/001788182c73/lampsearch",json)

print(client.getresponse().read())


