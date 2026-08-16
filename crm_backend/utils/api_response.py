"""Shared Django Ninja response envelope.

Every endpoint returns the same {success, message, statusCode, data, errors}
shape as a (status_code, body) tuple, which is what Ninja's ``response={...}``
status-code dispatch expects.
"""


def api_response(message: str, status_code: int = 200, success: bool = True, data=None, errors=None):
    body = {'success': success, 'message': message, 'statusCode': status_code, 'data': data or {}}
    if errors:
        body['errors'] = errors
    return status_code, body
