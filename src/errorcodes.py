
"""
Error messages are accessed as errorcodes.ERROR_TYPE, e.g. errorcodes.INVALID_JSON.
The result is an ErrorCodeDict of the format:
  {"status": "error", "errorcode": "ERROR_TYPE", "errormessage": "human-readable error message"}
"""

_error_codes = {
    "NOT_UNICODE": "couldn't decode as UTF-8",
    "INVALID_JSON": "invalid JSON",
    "INVALID_FORMAT": "the JSON was in an unexpected format",
    "BRIDGE_NOT_FOUND": "couldn't find a Hue bridge at given address '{ip}'",
    "BRIDGE_ALREADY_ADDED": "bridge has already been added to the server",
    "NO_SUCH_MAC": "the server does not know of a bridge with the MAC address '{mac}'",
    "CURRENTLY_SEARCHING": "currently searching for bridges",
    "NOT_IMPLEMENTED": "feature not implemented yet",
    "NO_LINKBUTTON": "link button not pressed",
    "NOT_LOGGED_IN": "user has not yet authenticated using /authenticate, or 'user' cookie was malformed",
    "INVALID_PASSWORD": "the supplied password was invalid",
    "AUTH_NOT_ENABLED": "authentication is not enabled for this server instance",
    "INVALID_NAME": "user name is too short"
}

class ErrorCodeDict(dict):
    
    def format(self, **kwargs):
        return ErrorCodeDict(self, errormessage=self['errormessage'].format(**kwargs))
    
    def merge(self, **kwargs):
        return ErrorCodeDict(self, **kwargs)


for ec, em in _error_codes.items():
    globals()[ec] = ErrorCodeDict({"state": "error", "errorcode": ec, "errormessage": em})
