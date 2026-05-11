"""Canonical API error hierarchy for SiteSpy.

All non-2xx responses raised by the ingest pipeline (and future handlers)
should use one of the concrete subclasses below so that ``http.error_response``
can serialise them into the canonical envelope.
"""


class ApiError(Exception):
    """Base class for all user-visible API errors.

    Attributes:
        status_code: The HTTP status code to return.
        error_key:   The stable machine-readable error identifier.
        message:     The human-readable message included in the response body.
    """

    status_code: int
    error_key: str
    message: str

    def __init__(
        self,
        message: str | None = None,
        *,
        status_code: int | None = None,
        error_key: str | None = None,
    ) -> None:
        # Allow subclasses to define class-level defaults; callers may override.
        if status_code is not None:
            self.status_code = status_code
        if error_key is not None:
            self.error_key = error_key
        if message is not None:
            self.message = message
        super().__init__(self.message)


class BadRequest(ApiError):
    """HTTP 400 — the request was malformed or failed validation."""

    status_code = 400
    error_key = "BAD_REQUEST"
    message = "Bad request."


class Unauthorized(ApiError):
    """HTTP 401 — authentication failed or credentials were not provided."""

    status_code = 401
    error_key = "UNAUTHORIZED"
    message = "Authentication failed."


class Forbidden(ApiError):
    """HTTP 403 — the caller does not have access to the requested resource."""

    status_code = 403
    error_key = "ACCESS_DENIED"
    message = "You do not have access to this site."


class NotFound(ApiError):
    """HTTP 404 — the requested resource does not exist."""

    status_code = 404
    error_key = "NOT_FOUND"
    message = "The requested resource was not found."


class Conflict(ApiError):
    """HTTP 409 — the requested state transition is not allowed."""

    status_code = 409
    error_key = "CONFLICT"
    message = "The requested state transition is not allowed."


class InternalError(ApiError):
    """HTTP 500 — an unexpected server-side error occurred."""

    status_code = 500
    error_key = "INTERNAL_ERROR"
    message = "An internal error occurred."
