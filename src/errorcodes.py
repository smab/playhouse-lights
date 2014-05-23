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

"""
Error messages are accessed as errorcodes.E_ERROR_TYPE, e.g. errorcodes.E_INVALID_JSON.
The result is an ErrorCodeDict of the format:
  {"status": "error", "errorcode": "ERROR_TYPE", "errormessage": "human-readable error message"}
"""


E_INTERNAL_ERROR = "an internal error occured; please see the light server logs"
E_NOT_UNICODE = "couldn't decode as UTF-8"
E_INVALID_JSON = "invalid JSON"
E_INVALID_FORMAT = "the JSON was in an unexpected format"
E_BRIDGE_NOT_FOUND = "couldn't find a Hue bridge at given address"
E_BRIDGE_ALREADY_ADDED = "bridge has already been added to the server"
E_NO_SUCH_MAC = "the server does not know of a bridge with the given MAC address"
E_CURRENTLY_SEARCHING = "currently searching for bridges"
E_NOT_IMPLEMENTED = "feature not implemented yet"
E_NO_LINKBUTTON = "link button not pressed"
E_INVALID_USERNAME = "could not send request to bridge with the MAC address '{mac}' " \
    "using the username {username}"
E_NOT_LOGGED_IN = "user has not yet authenticated using /authenticate, " \
    "or 'user' cookie was malformed"
E_INVALID_PASSWORD = "the supplied password was invalid"
E_AUTH_NOT_ENABLED = "authentication is not enabled for this server instance"
E_INVALID_NAME = "user name is too short or otherwise invalid"
E_BULB_NOT_RESET = "failed to reset a bulb"


class ErrorCodeDict(dict):
    """A dictionary for storing error messages.

    Expects the existence of an 'errormessage' key in the dictionary.
    """

    def format(self, **kwargs):
        """Replace placeholders in the error message with the given strings."""
        return ErrorCodeDict(self, errormessage=self['errormessage'].format(**kwargs))

    def merge(self, **kwargs):
        """Add new key/value pairs to the dictionary."""
        return ErrorCodeDict(self, **kwargs)

for ec in list(globals()):
    if ec.startswith("E_"):
        globals()[ec] = ErrorCodeDict({"state": "error",
                                       "errorcode": ec[2:],
                                       "errormessage": globals()[ec]})


class LightserverException(Exception):
    error = None

class RequestInvalidUnicodeException(LightserverException):
    error = E_NOT_UNICODE

class RequestInvalidJSONException(LightserverException):
    error = E_INVALID_JSON

class RequestInvalidFormatException(LightserverException):
    error = E_INVALID_FORMAT

class NotLoggedInException(LightserverException):
    error = E_NOT_LOGGED_IN

class InvalidUserNameException(LightserverException):
    error = E_INVALID_NAME

class CurrentlySearchingException(LightserverException):
    error = E_CURRENTLY_SEARCHING

class NoSuchMacException(LightserverException):
    error = E_NO_SUCH_MAC

class InvalidPasswordException(LightserverException):
    error = E_INVALID_PASSWORD

class AuthNotEnabledException(LightserverException):
    error = E_AUTH_NOT_ENABLED
