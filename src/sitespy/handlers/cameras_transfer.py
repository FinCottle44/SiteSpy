"""Cameras Transfer handler for SiteSpy — POST /v1/cameras/transfer.

Atomically moves a camera from a source tenant/site to a target tenant/site,
preserving the ingest token. Super admin only.

Requirements validated: 5.1–5.8, 6.1–6.9
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
from sitespy.errors import (
    ApiError,
    BadRequest,
    Conflict,
    Forbidden,
    InternalError,
    NotFound,
)
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
_ROUTE = "POST /v1/cameras/transfer"

_GROUP_SUPER_ADMINS = "SuperAdmins"
_GROUP_TENANT_ADMINS = "TenantAdmins"

_REQUIRED_FIELDS = (
    "source_tenant_id",
    "source_site_id",
    "camera_id",
    "target_tenant_id",
    "target_site_id",
)


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for POST /v1/cameras/transfer."""
    start_ms = time.monotonic() * 1000
    correlation_id = _resolve_correlation_id(event)

    try:
        result = _handle(event, correlation_id)
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_metric(name="TransferCameraSuccess", unit=MetricUnit.Count, value=1)
        logger.info(
            "transfer_camera_success",
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
        metrics.add_metric(name="TransferCameraFailure", unit=MetricUnit.Count, value=1)

        logger.warning(
            "transfer_camera_failure",
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
        metrics.add_metric(name="TransferCameraFailure", unit=MetricUnit.Count, value=1)

        logger.exception(
            "transfer_camera_unhandled_error",
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
    """Core transfer logic:

    1. Extract claims, enforce super_admin role
    2. Parse body, validate required fields
    3. Fetch source camera (404 if missing)
    4. Validate target tenant exists (404 if missing)
    5. Validate target site exists and belongs to target tenant (404 if missing)
    6. Check no conflict at target (409 if exists)
    7. Call data.transfer_camera() — atomic transact_write_items
    8. Return 200 with new location
    """
    # --- Extract JWT claims and resolve role ---
    claims = _extract_claims(event)
    role = _resolve_role(claims)

    # --- Enforce super_admin only ---
    if role != "super_admin":
        raise Forbidden("You do not have access to this resource.")

    # --- Parse JSON body ---
    body = _parse_body(event)

    # --- Validate required fields ---
    source_tenant_id = _validate_required_field(body, "source_tenant_id")
    source_site_id = _validate_required_field(body, "source_site_id")
    camera_id = _validate_required_field(body, "camera_id")
    target_tenant_id = _validate_required_field(body, "target_tenant_id")
    target_site_id = _validate_required_field(body, "target_site_id")

    # --- Fetch source camera ---
    try:
        source_camera = data.get_camera(source_tenant_id, source_site_id, camera_id)
    except Exception as exc:
        logger.exception("dynamodb_get_source_camera_failed")
        raise InternalError() from exc

    if source_camera is None:
        raise NotFound("Source camera not found.")

    # --- Validate target tenant exists ---
    try:
        target_tenant = data.get_tenant(target_tenant_id)
    except Exception as exc:
        logger.exception("dynamodb_get_target_tenant_failed")
        raise InternalError() from exc

    if target_tenant is None:
        raise NotFound("Target tenant not found.")

    # --- Validate target site exists and belongs to target tenant ---
    try:
        target_site = data.get_site(target_tenant_id, target_site_id)
    except Exception as exc:
        logger.exception("dynamodb_get_target_site_failed")
        raise InternalError() from exc

    if target_site is None:
        raise NotFound("Target site not found.")

    # --- Check for conflict at target ---
    try:
        existing_camera = data.get_camera(target_tenant_id, target_site_id, camera_id)
    except Exception as exc:
        logger.exception("dynamodb_get_target_camera_failed")
        raise InternalError() from exc

    if existing_camera is not None:
        raise Conflict("A camera with this camera_id already exists at the target site.")

    # --- Extract camera attributes from source record ---
    camera_name = source_camera.get("camera_name", {}).get("S", "")
    camera_model_attr = source_camera.get("camera_model")
    camera_model = camera_model_attr.get("S") if camera_model_attr else None
    ingest_token = source_camera.get("ingest_token", {}).get("S", "")
    created_at = source_camera.get("created_at", {}).get("S", "")

    # --- Perform atomic transfer ---
    try:
        data.transfer_camera(
            source_tenant_id=source_tenant_id,
            source_site_id=source_site_id,
            target_tenant_id=target_tenant_id,
            target_site_id=target_site_id,
            camera_id=camera_id,
            camera_name=camera_name,
            camera_model=camera_model,
            ingest_token=ingest_token,
            created_at=created_at,
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code == "TransactionCanceledException":
            cancellation_reasons = exc.response.get("CancellationReasons", [])
            logger.warning(
                "transfer_camera_transaction_cancelled",
                extra={"cancellation_reasons": cancellation_reasons},
            )
            # Inspect cancellation reasons:
            # Item 0 = Put (target) — ConditionalCheckFailed means conflict
            # Item 1 = Delete (source) — ConditionalCheckFailed means source gone
            if len(cancellation_reasons) >= 2:
                put_reason = cancellation_reasons[0].get("Code", "")
                delete_reason = cancellation_reasons[1].get("Code", "")

                if put_reason == "ConditionalCheckFailed":
                    raise Conflict(
                        "A camera with this camera_id already exists at the target site."
                    ) from exc
                if delete_reason == "ConditionalCheckFailed":
                    raise NotFound("Source camera not found.") from exc

            # Unexpected failure
            raise InternalError() from exc

        logger.exception("dynamodb_transfer_camera_failed")
        raise InternalError() from exc

    # --- Return 200 with new location ---
    response_body: dict[str, Any] = {
        "tenant_id": target_tenant_id,
        "site_id": target_site_id,
        "camera_id": camera_id,
    }

    return json_response(200, response_body, correlation_id)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_required_field(body: dict[str, Any], field_name: str) -> str:
    """Validate a required non-empty string field from the request body.

    Raises BadRequest with the field name if the field is missing or empty.
    """
    value = body.get(field_name)
    if value is None or not isinstance(value, str) or not value.strip():
        raise BadRequest(f"Missing required field: {field_name}.")
    return value.strip()


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
