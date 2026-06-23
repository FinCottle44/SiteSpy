"""Sites POST handler for SiteSpy — POST /v1/sites.

Creates a new site under an existing tenant. Super admin only.

Requirements validated: 2.1–2.14
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit

from sitespy import data
from sitespy.errors import ApiError, BadRequest, Conflict, Forbidden, InternalError, NotFound
from sitespy.http import error_response, json_response, unhandled_error_response
from sitespy.sandbox import sandbox_visibility_guard
from sitespy.validation import (
    validate_latitude,
    validate_longitude,
    validate_site_id,
    validate_timezone,
)

# ---------------------------------------------------------------------------
# Powertools setup
# ---------------------------------------------------------------------------

logger = Logger(service="sitespy")
metrics = Metrics(namespace="SiteSpy", service="sitespy")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_ROUTE = "POST /v1/sites"

_GROUP_SUPER_ADMINS = "SuperAdmins"
_GROUP_TENANT_ADMINS = "TenantAdmins"

_DEFAULT_TIMEZONE = "Europe/London"
_MAX_SITE_NAME_LENGTH = 128


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for POST /v1/sites."""
    start_ms = time.monotonic() * 1000
    correlation_id = _resolve_correlation_id(event)

    try:
        result = _handle(event, correlation_id)
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_metric(name="PostSiteSuccess", unit=MetricUnit.Count, value=1)
        logger.info(
            "post_site_success",
            extra={
                "correlation_id": correlation_id,
                "route": _ROUTE,
                "status_code": result["statusCode"],
                "latency_ms": latency_ms,
            },
        )
        return result

    except ApiError as exc:
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_dimension(name="status_code", value=str(exc.status_code))
        metrics.add_metric(name="PostSiteFailure", unit=MetricUnit.Count, value=1)

        logger.warning(
            "post_site_failure",
            extra={
                "correlation_id": correlation_id,
                "route": _ROUTE,
                "status_code": exc.status_code,
                "latency_ms": latency_ms,
                "error": exc.error_key,
                "failure_reason": type(exc).__name__.lower(),
            },
        )
        return error_response(exc, correlation_id)

    except Exception:
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_dimension(name="status_code", value="500")
        metrics.add_metric(name="PostSiteFailure", unit=MetricUnit.Count, value=1)

        logger.exception(
            "post_site_unhandled_error",
            extra={
                "correlation_id": correlation_id,
                "route": _ROUTE,
                "status_code": 500,
                "latency_ms": latency_ms,
                "error": "INTERNAL_ERROR",
                "failure_reason": "unhandled_exception",
            },
        )
        return unhandled_error_response(correlation_id)


# ---------------------------------------------------------------------------
# Internal handler
# ---------------------------------------------------------------------------


def _handle(event: dict[str, Any], correlation_id: str) -> dict[str, Any]:
    """Core logic for POST /v1/sites — raises ApiError on any user-visible failure."""
    # --- Extract JWT claims and resolve role ---
    claims = _extract_claims(event)
    role = _resolve_role(claims)

    # --- Enforce super_admin only ---
    if role != "super_admin":
        raise Forbidden()

    # --- Extract and validate tenant_id query parameter ---
    query_params = event.get("queryStringParameters") or {}
    tenant_id = (query_params.get("tenant_id") or "").strip()

    if not tenant_id:
        raise BadRequest("Missing required query parameter: tenant_id.")

    # --- Sandbox visibility guard ---
    sandbox_visibility_guard(tenant_id, role)

    # --- Verify tenant exists ---
    try:
        tenant_item = data.get_tenant(tenant_id)
    except Exception as exc:
        logger.exception("dynamodb_get_tenant_failed")
        raise InternalError() from exc

    if tenant_item is None:
        raise NotFound("Tenant not found.")

    # --- Parse JSON body ---
    body = _parse_body(event)

    # --- Validate required fields ---
    site_id = body.get("site_id")
    site_name = body.get("site_name")
    latitude = body.get("latitude")
    longitude = body.get("longitude")
    timezone_value = body.get("timezone")

    if site_id is None or not isinstance(site_id, str) or not site_id.strip():
        raise BadRequest("Missing required field: site_id.")

    if not validate_site_id(site_id):
        raise BadRequest(
            "Invalid site_id. Must match pattern: ^[a-z0-9_]{1,64}$."
        )

    if site_name is None or not isinstance(site_name, str) or not site_name.strip():
        raise BadRequest("Missing required field: site_name.")

    if len(site_name) > _MAX_SITE_NAME_LENGTH:
        raise BadRequest(
            f"site_name must be at most {_MAX_SITE_NAME_LENGTH} characters."
        )

    if latitude is None:
        raise BadRequest("Missing required field: latitude.")

    if not isinstance(latitude, (int, float)) or isinstance(latitude, bool):
        raise BadRequest("latitude must be a number.")

    if not validate_latitude(float(latitude)):
        raise BadRequest("latitude must be between -90 and 90.")

    if longitude is None:
        raise BadRequest("Missing required field: longitude.")

    if not isinstance(longitude, (int, float)) or isinstance(longitude, bool):
        raise BadRequest("longitude must be a number.")

    if not validate_longitude(float(longitude)):
        raise BadRequest("longitude must be between -180 and 180.")

    # --- Handle timezone (default or validate) ---
    if timezone_value is None:
        timezone_value = _DEFAULT_TIMEZONE
    else:
        if not isinstance(timezone_value, str) or not timezone_value.strip():
            raise BadRequest("timezone must be a non-empty string.")
        if not validate_timezone(timezone_value):
            raise BadRequest(
                "Invalid timezone. Must be a valid IANA timezone identifier."
            )

    # --- Write site record to DynamoDB ---
    try:
        site_record = data.put_site(
            tenant_id=tenant_id,
            site_id=site_id,
            site_name=site_name,
            latitude=float(latitude),
            longitude=float(longitude),
            timezone_str=timezone_value,
        )
    except Exception as exc:
        if _is_conditional_check_failed(exc):
            raise Conflict(
                "A site with this site_id already exists for this tenant."
            ) from exc
        logger.exception("dynamodb_put_site_failed")
        raise InternalError() from exc

    # --- Return 201 with site record ---
    return json_response(201, site_record, correlation_id)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _extract_claims(event: dict[str, Any]) -> dict[str, Any]:
    """Extract Cognito JWT claims from the API Gateway authorizer context."""
    request_context = event.get("requestContext") or {}
    authorizer = request_context.get("authorizer") or {}
    return authorizer.get("claims") or {}


def _resolve_role(claims: dict[str, Any]) -> str:
    """Resolve role from JWT claims."""
    raw_groups = claims.get("cognito:groups") or ""
    if isinstance(raw_groups, list):
        groups: list[str] = raw_groups
    else:
        groups = [g.strip() for g in str(raw_groups).split(",") if g.strip()]
        if len(groups) == 1 and " " in groups[0]:
            groups = [g.strip() for g in groups[0].split() if g.strip()]

    if _GROUP_SUPER_ADMINS in groups:
        return "super_admin"
    elif _GROUP_TENANT_ADMINS in groups:
        return "tenant_admin"
    return "user"


# ---------------------------------------------------------------------------
# Body parsing
# ---------------------------------------------------------------------------


def _parse_body(event: dict[str, Any]) -> dict[str, Any]:
    """Parse the JSON request body, raising BadRequest on failure."""
    raw_body = event.get("body")
    if raw_body is None:
        raise BadRequest("Request body is required.")

    if isinstance(raw_body, dict):
        return raw_body

    try:
        parsed = json.loads(raw_body)
    except (json.JSONDecodeError, TypeError, ValueError):
        raise BadRequest("Request body must be valid JSON.") from None

    if not isinstance(parsed, dict):
        raise BadRequest("Request body must be a JSON object.")

    return parsed


# ---------------------------------------------------------------------------
# DynamoDB error helpers
# ---------------------------------------------------------------------------


def _is_conditional_check_failed(exc: Exception) -> bool:
    """Check if an exception is a DynamoDB ConditionalCheckFailedException."""
    from botocore.exceptions import ClientError

    if isinstance(exc, ClientError):
        return exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException"
    return False


# ---------------------------------------------------------------------------
# Correlation ID helper
# ---------------------------------------------------------------------------


def _resolve_correlation_id(event: dict[str, Any]) -> str:
    """Return the X-Correlation-Id header if valid, else a fresh UUID v4."""
    headers = event.get("headers") or {}
    value = headers.get("X-Correlation-Id") or headers.get("x-correlation-id") or ""
    if _CORRELATION_ID_RE.match(value):
        return value
    return str(uuid.uuid4())
