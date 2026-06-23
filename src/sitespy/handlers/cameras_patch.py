"""Cameras PATCH handler for SiteSpy — PATCH /v1/sites/{site_id}/cameras/{camera_id}.

Updates mutable camera metadata (camera_name, camera_model). The camera_id and
ingest token are immutable. Tenant admin or super admin.
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
_ROUTE = "PATCH /v1/sites/{site_id}/cameras/{camera_id}"

_GROUP_SUPER_ADMINS = "SuperAdmins"
_GROUP_TENANT_ADMINS = "TenantAdmins"

_CAMERA_NAME_MAX_LEN = 120
_CAMERA_MODEL_MAX_LEN = 120


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for PATCH /v1/sites/{site_id}/cameras/{camera_id}."""
    start_ms = time.monotonic() * 1000
    correlation_id = _resolve_correlation_id(event)

    try:
        result = _handle(event, correlation_id)
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_metric(name="PatchCameraSuccess", unit=MetricUnit.Count, value=1)
        logger.info(
            "patch_camera_success",
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
        metrics.add_metric(name="PatchCameraFailure", unit=MetricUnit.Count, value=1)

        logger.warning(
            "patch_camera_failure",
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
        metrics.add_metric(name="PatchCameraFailure", unit=MetricUnit.Count, value=1)

        logger.exception(
            "patch_camera_unhandled_error",
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
    """Core logic for PATCH /v1/sites/{site_id}/cameras/{camera_id}.

    Tenant admin (own tenant) or super admin only.
    """
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

    # --- Parse and validate body ---
    body = _parse_body(event)
    updates = _build_updates(body)

    # --- Apply update ---
    try:
        data.update_camera(
            tenant_id=tenant_id,
            site_id=site_id,
            camera_id=camera_id,
            updates=updates,
        )
    except Exception as exc:
        logger.exception(
            "dynamodb_update_camera_failed",
            extra={
                "tenant_id": tenant_id,
                "site_id": site_id,
                "camera_id": camera_id,
            },
        )
        raise InternalError("Failed to update camera.") from exc

    # --- Build response (merge updates over existing values) ---
    existing_name = camera_item.get("camera_name", {}).get("S", "")
    existing_model_attr = camera_item.get("camera_model")
    existing_model = existing_model_attr.get("S") if existing_model_attr else None

    response_body: dict[str, Any] = {
        "camera_id": camera_id,
        "site_id": site_id,
        "tenant_id": tenant_id,
        "camera_name": updates.get("camera_name", existing_name),
    }

    if "camera_model" in updates:
        response_body["camera_model"] = updates["camera_model"]
    elif existing_model is not None:
        response_body["camera_model"] = existing_model

    return json_response(200, response_body, correlation_id)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _build_updates(body: dict[str, Any]) -> dict[str, Any]:
    """Validate the body and build the updates map.

    At least one of camera_name / camera_model must be present.
    - camera_name: required non-empty string when present.
    - camera_model: non-empty string, or null to clear it.
    """
    updates: dict[str, Any] = {}

    if "camera_name" in body:
        name = body["camera_name"]
        if not isinstance(name, str) or not name.strip():
            raise BadRequest("camera_name must be a non-empty string.")
        if len(name.strip()) > _CAMERA_NAME_MAX_LEN:
            raise BadRequest(
                f"camera_name must be at most {_CAMERA_NAME_MAX_LEN} characters."
            )
        updates["camera_name"] = name.strip()

    if "camera_model" in body:
        model = body["camera_model"]
        if model is None:
            updates["camera_model"] = None  # clear the attribute
        elif isinstance(model, str) and model.strip():
            if len(model.strip()) > _CAMERA_MODEL_MAX_LEN:
                raise BadRequest(
                    f"camera_model must be at most {_CAMERA_MODEL_MAX_LEN} characters."
                )
            updates["camera_model"] = model.strip()
        else:
            raise BadRequest("camera_model must be a non-empty string or null.")

    if not updates:
        raise BadRequest(
            "Request body must contain at least one of: camera_name, camera_model."
        )

    return updates


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
# Correlation ID helper
# ---------------------------------------------------------------------------


def _resolve_correlation_id(event: dict[str, Any]) -> str:
    """Return the X-Correlation-Id header if valid, else a fresh UUID v4."""
    headers = event.get("headers") or {}
    value = headers.get("X-Correlation-Id") or headers.get("x-correlation-id") or ""
    if _CORRELATION_ID_RE.match(value):
        return value
    return str(uuid.uuid4())
