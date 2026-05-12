"""Tenants POST handler for SiteSpy — POST /v1/tenants.

Creates a new tenant. Super admin only.

Requirements validated: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 1.10, 1.11
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from botocore.exceptions import ClientError

from sitespy import data
from sitespy.errors import ApiError, BadRequest, Conflict, Forbidden, InternalError
from sitespy.http import error_response, json_response, unhandled_error_response
from sitespy.validation import validate_email, validate_tenant_id

# ---------------------------------------------------------------------------
# Powertools setup
# ---------------------------------------------------------------------------

logger = Logger(service="sitespy")
metrics = Metrics(namespace="SiteSpy", service="sitespy")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_ROUTE = "POST /v1/tenants"

_GROUP_SUPER_ADMINS = "SuperAdmins"
_GROUP_TENANT_ADMINS = "TenantAdmins"

_DEFAULT_STALE_THRESHOLD_HOURS = 24
_MAX_TENANT_NAME_LENGTH = 128


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for POST /v1/tenants."""
    start_ms = time.monotonic() * 1000
    correlation_id = _resolve_correlation_id(event)

    try:
        result = _handle(event, correlation_id)
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_metric(name="PostTenantsSuccess", unit=MetricUnit.Count, value=1)
        logger.info(
            "post_tenants_success",
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
        metrics.add_metric(name="PostTenantsFailure", unit=MetricUnit.Count, value=1)
        logger.warning(
            "post_tenants_failure",
            extra={
                "correlation_id": correlation_id,
                "route": _ROUTE,
                "status_code": exc.status_code,
                "latency_ms": latency_ms,
                "error": exc.error_key,
            },
        )
        return error_response(exc, correlation_id)

    except Exception:
        latency_ms = int(time.monotonic() * 1000 - start_ms)
        metrics.add_metric(name="PostTenantsFailure", unit=MetricUnit.Count, value=1)
        logger.exception(
            "post_tenants_unhandled_error",
            extra={
                "correlation_id": correlation_id,
                "route": _ROUTE,
                "status_code": 500,
                "latency_ms": latency_ms,
            },
        )
        return unhandled_error_response(correlation_id)


# ---------------------------------------------------------------------------
# Internal handler
# ---------------------------------------------------------------------------


def _handle(event: dict[str, Any], correlation_id: str) -> dict[str, Any]:
    """Core logic for POST /v1/tenants — super admin only."""
    # --- Extract JWT claims and resolve caller role ---
    claims = _extract_claims(event)
    role = _resolve_role(claims)

    # --- Enforce super admin only ---
    if role != "super_admin":
        raise Forbidden()

    # --- Parse JSON body ---
    body = _parse_body(event)

    # --- Validate required fields ---
    tenant_id = body.get("tenant_id")
    tenant_name = body.get("tenant_name")
    primary_contact_email = body.get("primary_contact_email")

    if tenant_id is None:
        raise BadRequest("Missing required field: tenant_id.")
    if tenant_name is None:
        raise BadRequest("Missing required field: tenant_name.")
    if primary_contact_email is None:
        raise BadRequest("Missing required field: primary_contact_email.")

    # --- Validate field formats ---
    if not isinstance(tenant_id, str) or not validate_tenant_id(tenant_id):
        raise BadRequest(
            "Invalid tenant_id: must be 3-32 lowercase alphanumeric characters or underscores."
        )

    if not isinstance(tenant_name, str) or not tenant_name.strip():
        raise BadRequest("Invalid tenant_name: must be a non-empty string.")
    if len(tenant_name) > _MAX_TENANT_NAME_LENGTH:
        raise BadRequest(
            f"Invalid tenant_name: must be at most {_MAX_TENANT_NAME_LENGTH} characters."
        )

    if not isinstance(primary_contact_email, str) or not validate_email(primary_contact_email):
        raise BadRequest(
            "Invalid primary_contact_email: must be a valid email address (max 254 characters)."
        )

    # --- Handle stale_threshold_hours (optional, default 24) ---
    stale_threshold_hours = body.get("stale_threshold_hours", _DEFAULT_STALE_THRESHOLD_HOURS)

    if not isinstance(stale_threshold_hours, int) or isinstance(stale_threshold_hours, bool):
        raise BadRequest(
            "Invalid stale_threshold_hours: must be an integer between 1 and 720."
        )
    if stale_threshold_hours < 1 or stale_threshold_hours > 720:
        raise BadRequest(
            "Invalid stale_threshold_hours: must be an integer between 1 and 720."
        )

    # --- Write tenant to DynamoDB ---
    try:
        tenant_record = data.put_tenant(
            tenant_id=tenant_id,
            tenant_name=tenant_name,
            primary_contact_email=primary_contact_email,
            stale_threshold_hours=stale_threshold_hours,
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise Conflict("A tenant with this tenant_id already exists.")
        logger.exception("dynamodb_put_tenant_failed")
        raise InternalError() from exc

    # --- Return 201 with tenant record ---
    return json_response(201, tenant_record, correlation_id)


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
# Body parsing helper
# ---------------------------------------------------------------------------


def _parse_body(event: dict[str, Any]) -> dict[str, Any]:
    """Parse the JSON request body from the event.

    Raises BadRequest if the body is missing or not valid JSON.
    """
    raw_body = event.get("body")
    if raw_body is None:
        raise BadRequest("Request body is required.")
    try:
        parsed = json.loads(raw_body)
    except (json.JSONDecodeError, TypeError):
        raise BadRequest("Request body must be valid JSON.")
    if not isinstance(parsed, dict):
        raise BadRequest("Request body must be a JSON object.")
    return parsed


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
