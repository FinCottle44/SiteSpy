"""Cameras POST handler for SiteSpy — POST /v1/sites/{site_id}/cameras.

Creates a new camera on a site and returns the ingest token. Super admin only.

Requirements validated: 3.1–3.16
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from botocore.exceptions import ClientError

from sitespy import credentials, data
from sitespy.errors import ApiError, BadRequest, Conflict, Forbidden, InternalError, NotFound
from sitespy.http import error_response, json_response, unhandled_error_response
from sitespy.validation import validate_camera_id

# ---------------------------------------------------------------------------
# Powertools setup
# ---------------------------------------------------------------------------

logger = Logger(service="sitespy")
metrics = Metrics(namespace="SiteSpy", service="sitespy")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_ROUTE = "POST /v1/sites/{site_id}/cameras"

_GROUP_SUPER_ADMINS = "SuperAdmins"
_GROUP_TENANT_ADMINS = "TenantAdmins"

_MAX_CAMERA_NAME_LENGTH = 128
_MAX_CAMERA_MODEL_LENGTH = 128


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for POST /v1/sites/{site_id}/cameras."""
    start_ms = time.monotonic() * 1000
    correlation_id = _resolve_correlation_id(event)

    try:
        result = _handle(event, correlation_id)
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_metric(name="PostCameraSuccess", unit=MetricUnit.Count, value=1)
        logger.info(
            "post_camera_success",
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
        metrics.add_metric(name="PostCameraFailure", unit=MetricUnit.Count, value=1)

        logger.warning(
            "post_camera_failure",
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
        metrics.add_metric(name="PostCameraFailure", unit=MetricUnit.Count, value=1)

        logger.exception(
            "post_camera_unhandled_error",
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
    """Core logic for POST /v1/sites/{site_id}/cameras — super admin only."""
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

    # --- Parse JSON body ---
    body = _parse_body(event)

    # --- Validate required fields ---
    camera_id = body.get("camera_id")
    camera_name = body.get("camera_name")
    camera_model = body.get("camera_model")

    if camera_id is None or not isinstance(camera_id, str) or not camera_id.strip():
        raise BadRequest("Missing required field: camera_id.")

    if not validate_camera_id(camera_id):
        raise BadRequest(
            "Invalid camera_id. Must match pattern: ^[a-z0-9_]{1,64}$."
        )

    if camera_name is None or not isinstance(camera_name, str) or not camera_name.strip():
        raise BadRequest("Missing required field: camera_name.")

    if len(camera_name) > _MAX_CAMERA_NAME_LENGTH:
        raise BadRequest(
            f"camera_name must be at most {_MAX_CAMERA_NAME_LENGTH} characters."
        )

    # --- Validate optional camera_model ---
    if camera_model is not None:
        if not isinstance(camera_model, str):
            raise BadRequest("camera_model must be a string.")
        if len(camera_model) > _MAX_CAMERA_MODEL_LENGTH:
            raise BadRequest(
                f"camera_model must be at most {_MAX_CAMERA_MODEL_LENGTH} characters."
            )

    # --- Generate ingest token ---
    ingest_token = credentials.generate_ingest_token()

    # --- Write camera record to DynamoDB (includes GSI1 token index) ---
    try:
        data.put_camera(
            tenant_id=tenant_id,
            site_id=site_id,
            camera_id=camera_id,
            camera_name=camera_name,
            camera_model=camera_model,
            ingest_token=ingest_token,
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise Conflict(
                "A camera with this camera_id already exists on this site."
            ) from exc
        logger.exception("dynamodb_put_camera_failed")
        raise InternalError() from exc

    # --- Build ingest URL ---
    ingest_base_url = os.environ.get("INGEST_BASE_URL", "")
    ingest_url = f"{ingest_base_url}/v1/ingest/{ingest_token}"

    # --- Return 201 with camera record and ingest details ---
    response_body: dict[str, Any] = {
        "camera_id": camera_id,
        "ingest_url": ingest_url,
        "ingest_token": ingest_token,
    }

    return json_response(201, response_body, correlation_id)


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
