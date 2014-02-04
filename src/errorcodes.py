
"""
Error codes are accessed as errorcodes.ERROR_TYPE, e.g. errorcodes.INVALID_JSON.
A human-readable string explaining the error can be accessed by adding _S to the
error type, e.g. errorcodes.INVALID_JSON_S
"""

_error_codes = {
    "NOT_UNICODE": "couldn't decode as UTF-8",
    "INVALID_JSON": "invalid JSON",
    "NOT_IMPLEMENTED": "feature not implemented yet"
}

for ec, em in _error_codes.items():
    globals()[ec] = ec
    globals()[ec + "_S"] = em

def message(ec):
    return _error_codes[ec]
