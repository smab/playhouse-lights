
 playhouse-lights
==================

This is the lamp server component of the Playhouse project.  For API documentation, see [the repository wiki][1].

The lamp server handles the current lamp grid configuration (with Hue bridges and lights) and the communication between the application (typically the Playhouse web server) and the separate Hue bridges.

The Hue bridges themselves have no HTTPS support, and the protection against unauthorized commands is very weak. Therefore, it is strongly recommended to set up the Hue bridges in a internal network configured so that they can't communicate with the outside world directly. The Hue bridges communicate through port 80 and 30000, so this can be done by configuring a firewall to restrict access through those ports. The lamp server must then be run within this internal network.

The lamp server supports HTTPS for communication between itself and the application, and can therefore safely communicate with the outside world if HTTPS is configured and enabled. If HTTPS is not enabled, then anyone can send instructions to the lamp server, so this is not recommended. The standard port for the lamp server is 4711.

[1]: https://github.com/smab/playhouse-lights/wiki/API


Requirements:
------------------------

* Python 3.3+
* Tornado 3.2+
* PycURL 7.19.3+
* libcurl 7.21.1+
* jsonschema 2.3.0+

Setup:
------------------------


