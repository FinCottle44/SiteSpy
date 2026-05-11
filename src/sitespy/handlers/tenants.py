"""Tenants handler for SiteSpy — GET /v1/tenants.

Returns a list of all tenants. Super admin only.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit

from sitespy import data
from sitespy.errors import ApiError, Forbidden, InternalError
from sitespy.http import error_response, json_response, unhandled_error_response

# ---------------------------------------------------------------------------
# Powertools setup
# ---------------------------------------------------------------------------

logger = Logger(service="sitespy")
metrics = Metrics(namespace="SiteSpy", service="sitespy")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_ROUTE = "GET /v1/tenants"

_GROUP_SUPER_ADMINS = "SuperAdmins"
_GROUP_TENANT_ADMINS = "TenantAdmins"


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for GET /v1/tenants."""
    start_ms = time.monotonic() * 1000
    correlation_id = _resolve_correlation_id(event)

    try:
        result = _handle(event, correlation_id)
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_metric(name="GetTenantsSuccess", unit=MetricUnit.Count, value=1)
        logger.info(
            "get_tenants_success",
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
        metrics.add_metric(name="GetTenantsFailure", unit=MetricUnit.Count, value=1)
        logger.warning(
            "get_tenants_failure",
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
        metrics.add_metric(name="GetTenantsFailure", unit=MetricUnit.Count, value=1)
        logger.exception(
            "get_tenants_unhandled_error",
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
    """Core logic for GET /v1/tenants — super admin only."""
    # --- Extract JWT claims and resolve caller ---
    claims = _extract_claims(event)
    role = _resolve_role(claims)

    # --- Enforce super admin only ---
    if role != "super_admin":
        raise Forbidden()

    # --- Query DynamoDB ---
    try:
        tenant_items = data.list_tenants()
    except Exception as exc:
        logger.exception("dynamodb_list_tenants_failed")
        raise InternalError() from exc

    # --- Build response ---
    tenants = []
    for item in tenant_items:
        pk = item.get("PK", {}).get("S", "")
        tenant_id = pk.removeprefix("TENANT#")
        tenants.append({
            "tenant_id": tenant_id,
            "tenant_name": item.get("tenant_name", {}).get("S", ""),
            "retention_years": int(item.get("retention_years", {}).get("N", "5")),
        })

    return json_response(200, {"tenants": tenants}, correlation_id)


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
# Correlation ID helper
# ---------------------------------------------------------------------------


def _resolve_correlation_id(event: dict[str, Any]) -> str:
    """Return the X-Correlation-Id header if valid, else a fresh UUID v4."""
    headers = event.get("headers") or {}
    value = headers.get("X-Correlation-Id") or headers.get("x-correlation-id") or ""
    if _CORRELATION_ID_RE.match(value):
        return value
    return str(uuid.uuid4())
