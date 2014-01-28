
import socket

ip = "130.237.228.161" # mac 00178811f9c2, newdeveloper

s = socket.socket(socket.AF_INET)
s.connect((ip, 30000))
f = s.makefile('r')
s.send(b'[Link,Touchlink]')

ack = f.readline()
print(ack)
res = f.readline()
print(res)
