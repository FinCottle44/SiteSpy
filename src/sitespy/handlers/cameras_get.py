"""Cameras GET handler for SiteSpy — GET /v1/sites/{site_id}/cameras.

Lists all cameras registered on a site. Tenant admin or super admin.

Requirements validated: 5.1–5.10
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit

from sitespy import data
from sitespy.errors import ApiError, BadRequest, Forbidden, InternalError, NotFound
from sitespy.http import error_response, json_response, unhandled_error_response
from sitespy.sandbox import sandbox_visibility_guard

# ---------------------------------------------------------------------------
# Powertools setup
# ---------------------------------------------------------------------------

logger = Logger(service="sitespy")
metrics = Metrics(namespace="SiteSpy", service="sitespy")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_ROUTE = "GET /v1/sites/{site_id}/cameras"

_GROUP_SUPER_ADMINS = "SuperAdmins"
_GROUP_TENANT_ADMINS = "TenantAdmins"


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for GET /v1/sites/{site_id}/cameras."""
    start_ms = time.monotonic() * 1000
    correlation_id = _resolve_correlation_id(event)

    try:
        result = _handle(event, correlation_id)
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_metric(name="GetCamerasSuccess", unit=MetricUnit.Count, value=1)
        logger.info(
            "get_cameras_success",
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
        metrics.add_metric(name="GetCamerasFailure", unit=MetricUnit.Count, value=1)

        logger.warning(
            "get_cameras_failure",
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
        metrics.add_metric(name="GetCamerasFailure", unit=MetricUnit.Count, value=1)

        logger.exception(
            "get_cameras_unhandled_error",
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
    """Core logic for GET /v1/sites/{site_id}/cameras — tenant_admin or super_admin."""
    # --- Extract JWT claims and resolve role ---
    claims = _extract_claims(event)
    role = _resolve_role(claims)

    # --- Enforce tenant_admin or super_admin ---
    if role not in ("tenant_admin", "super_admin"):
        raise Forbidden()

    # --- Resolve tenant_id based on role ---
    if role == "tenant_admin":
        tenant_id = (claims.get("custom:tenant_id") or "").strip()
        if not tenant_id:
            raise Forbidden("Unable to resolve tenant from JWT claims.")
    else:
        # Super admin: require tenant_id as query parameter
        query_params = event.get("queryStringParameters") or {}
        tenant_id = (query_params.get("tenant_id") or "").strip()
        if not tenant_id:
            raise BadRequest("Missing required query parameter: tenant_id.")

    # --- Sandbox visibility guard ---
    sandbox_visibility_guard(tenant_id, role)

    # --- Extract site_id from path parameters ---
    path_params = event.get("pathParameters") or {}
    site_id = (path_params.get("site_id") or "").strip()

    if not site_id:
        raise BadRequest("Missing required path parameter: site_id.")

    # --- Verify site exists ---
    try:
        site_item = data.get_site(tenant_id, site_id)
    except Exception as exc:
        logger.exception("dynamodb_get_site_failed")
        raise InternalError() from exc

    if site_item is None:
        raise NotFound("Site not found.")

    # --- Query cameras for the site ---
    try:
        camera_items = data.get_cameras_for_site(tenant_id, site_id)
    except Exception as exc:
        logger.exception("dynamodb_get_cameras_failed")
        raise InternalError() from exc

    # --- Build response — no credentials ---
    cameras = []
    for item in camera_items:
        camera_record: dict[str, Any] = {}

        # Extract camera_id from the SK: SITE#<site_id>#CAM#<camera_id>
        sk_value = item.get("SK", {}).get("S", "")
        cam_prefix = f"SITE#{site_id}#CAM#"
        if sk_value.startswith(cam_prefix):
            camera_record["camera_id"] = sk_value[len(cam_prefix):]
        else:
            camera_record["camera_id"] = sk_value

        # Extract camera_name
        if "camera_name" in item:
            camera_record["camera_name"] = item["camera_name"].get("S", "")
        else:
            camera_record["camera_name"] = ""

        # Extract camera_model
        if "camera_model" in item:
            camera_record["camera_model"] = item["camera_model"].get("S", "")
        else:
            camera_record["camera_model"] = None

        cameras.append(camera_record)

    return json_response(200, {"cameras": cameras}, correlation_id)


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
