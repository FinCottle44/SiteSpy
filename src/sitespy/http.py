"""HTTP response helpers for SiteSpy Lambda handlers.

All responses produced by the ingest pipeline (and future handlers) should go
through these helpers so that the canonical envelope, headers, and correlation
ID are applied consistently.

Requirements validated: 8.1, 8.3
"""

import json

from sitespy.errors import ApiError, InternalError

_CONTENT_TYPE = "application/json"

# CORS headers included in all Lambda responses.
# API Gateway Cors config handles OPTIONS preflight; these headers are needed
# on actual responses for the browser to accept them.
_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization,X-Correlation-Id",
    "Access-Control-Allow-Methods": "GET,POST,PATCH,OPTIONS",
}


def json_response(status: int, body: dict[str, object], correlation_id: str) -> dict[str, object]:
    """Build an API Gateway proxy response dict.

    Args:
        status:         HTTP status code.
        body:           Response payload; serialised to a JSON string.
        correlation_id: Value to echo back in the ``X-Correlation-Id`` header.

    Returns:
        A dict compatible with the API Gateway Lambda proxy integration format.
    """
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": _CONTENT_TYPE,
            "X-Correlation-Id": correlation_id,
            **_CORS_HEADERS,
        },
        "body": json.dumps(body),
    }


def error_response(exc: ApiError, correlation_id: str) -> dict[str, object]:
    """Build a canonical error envelope response from an ``ApiError``.

    The response body shape is ``{"error": <error_key>, "message": <message>}``.

    Args:
        exc:            The ``ApiError`` (or subclass) to serialise.
        correlation_id: Value to echo back in the ``X-Correlation-Id`` header.

    Returns:
        A dict compatible with the API Gateway Lambda proxy integration format.
    """
    return json_response(
        exc.status_code,
        {"error": exc.error_key, "message": exc.message},
        correlation_id,
    )


def unhandled_error_response(correlation_id: str) -> dict[str, object]:
    """Build a 500 response for any unhandled exception.

    Wraps a fresh ``InternalError`` so the wire body is indistinguishable from
    an explicit ``InternalError`` raise — no exception detail leaks to the
    caller.

    Args:
        correlation_id: Value to echo back in the ``X-Correlation-Id`` header.

    Returns:
        A dict compatible with the API Gateway Lambda proxy integration format.
    """
    return error_response(InternalError(), correlation_id)
