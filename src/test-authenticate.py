# NOTE: this is just an ugly proof of concept.
# try using e.g. tornado's built-in http client for better cookie support

import http.client
import http.cookies

conn = http.client.HTTPConnection("localhost", 4711)
print("Attempting to get grid before authenticating")
conn.request("GET", "/grid")
res = conn.getresponse()
print("Response was", res.read())
print("Got cookies:", res.headers.get_all("Set-Cookie"))

print("Authenticating...")
conn.request("POST", "/authenticate", body='{"username":"username", "password":"password"}')
res = conn.getresponse()
print("Response was", res.read())
print("Got cookies:", res.headers.get_all("Set-Cookie"))

bc = http.cookies.BaseCookie()
for cookie in res.headers.get_all("Set-Cookie"):
  print("Added cookie", cookie)
  bc.load(cookie)

print("BaseCookie now contains", repr(bc))

print("Attempting to get grid after authenticating")
conn.request("GET", "/grid", headers={"Cookie": bc['user'].output(attrs=[], header='')})
res = conn.getresponse()
print("Response was", res.read())
