"""Cameras rotate-token handler for SiteSpy — POST /v1/sites/{site_id}/cameras/{camera_id}/rotate-credentials.

Rotates camera ingest token and returns the new token.
Tenant admin or super admin.

Requirements validated: 6.1–6.10
"""

from __future__ import annotations

import os
import re
import time
import uuid
from typing import Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit

from sitespy import credentials, data
from sitespy.errors import ApiError, BadRequest, Forbidden, InternalError, NotFound
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
_ROUTE = "POST /v1/sites/{site_id}/cameras/{camera_id}/rotate-credentials"

_GROUP_SUPER_ADMINS = "SuperAdmins"
_GROUP_TENANT_ADMINS = "TenantAdmins"


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for POST /v1/sites/{site_id}/cameras/{camera_id}/rotate-credentials."""
    start_ms = time.monotonic() * 1000
    correlation_id = _resolve_correlation_id(event)

    try:
        result = _handle(event, correlation_id)
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_metric(name="RotateCredentialsSuccess", unit=MetricUnit.Count, value=1)
        logger.info(
            "rotate_credentials_success",
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
        metrics.add_metric(name="RotateCredentialsFailure", unit=MetricUnit.Count, value=1)

        logger.warning(
            "rotate_credentials_failure",
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
        metrics.add_metric(name="RotateCredentialsFailure", unit=MetricUnit.Count, value=1)

        logger.exception(
            "rotate_credentials_unhandled_error",
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
    """Core logic for POST /v1/sites/{site_id}/cameras/{camera_id}/rotate-credentials."""
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

    # --- Extract site_id and camera_id from path parameters ---
    path_params = event.get("pathParameters") or {}
    site_id = (path_params.get("site_id") or "").strip()
    camera_id = (path_params.get("camera_id") or "").strip()

    if not site_id:
        raise BadRequest("Missing required path parameter: site_id.")

    if not camera_id:
        raise BadRequest("Missing required path parameter: camera_id.")

    # --- Tenant admin: verify site belongs to own tenant ---
    if role == "tenant_admin":
        try:
            site_item = data.get_site(tenant_id, site_id)
        except Exception as exc:
            logger.exception("dynamodb_get_site_failed")
            raise InternalError() from exc

        if site_item is None:
            raise Forbidden("Site does not belong to your tenant.")

    # --- Verify camera exists ---
    try:
        camera_item = data.get_camera(tenant_id, site_id, camera_id)
    except Exception as exc:
        logger.exception("dynamodb_get_camera_failed")
        raise InternalError() from exc

    if camera_item is None:
        raise NotFound("Camera not found.")

    # --- Generate new ingest token ---
    new_token = credentials.generate_ingest_token()

    # --- Update token in DynamoDB (overwrites GSI1 index entry) ---
    try:
        data.update_camera_token(
            tenant_id=tenant_id,
            site_id=site_id,
            camera_id=camera_id,
            new_token=new_token,
        )
    except Exception as exc:
        logger.exception(
            "dynamodb_update_camera_token_failed",
            extra={
                "tenant_id": tenant_id,
                "site_id": site_id,
                "camera_id": camera_id,
            },
        )
        raise InternalError("Failed to rotate camera token.") from exc

    # --- Build ingest URL ---
    ingest_base_url = os.environ.get("INGEST_BASE_URL", "")
    ingest_url = f"{ingest_base_url}/v1/ingest/{new_token}"

    # --- Return 200 with new token ---
    response_body: dict[str, Any] = {
        "camera_id": camera_id,
        "ingest_url": ingest_url,
        "ingest_token": new_token,
    }

    return json_response(200, response_body, correlation_id)


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
