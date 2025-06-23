from rest_framework.response import Response
from collections.abc import Mapping

def api_response(
    *,
    message="Success",
    status_code=200,
    success=True,
    data=None,
    errors=None,
    code=None,
    meta=None,
    pagination=None
):
    """
    A standard API response format for all views.

    Parameters:
        - message: str Message string
        - status_code: int HTTP status code
        - success: bool Response success flag
        - data: dict/list Response payload
        - errors: dict/list Error details (optional)
        - code: str/int Optional app-level error code
        - meta: dict Any extra metadata
        - pagination: dict Pagination info (if any)

    Returns:
        DRF Response with structured data
    """

    response = {
        "success": success,
        "message": message,
        "statusCode": status_code,
        "data": data or {},  # Prevent null issues
    }

    if errors:
        response["errors"] = errors

    if code:
        response["code"] = code

    if isinstance(meta, Mapping):
        response["meta"] = meta

    if isinstance(pagination, Mapping):
        response["pagination"] = pagination

    return Response(response, status=status_code)
