"""Tenants Logo handler for SiteSpy — PUT/GET /v1/tenants/{tenant_id}/logo.

PUT: Uploads or replaces a tenant's company logo. Super admin only.
GET: Returns a presigned URL to display the logo. Any authenticated user.

The client sends the logo image as a binary body with Content-Type header
set to image/jpeg or image/png. The handler stores it in S3 and records
the logo_url on the tenant DynamoDB record.
"""

from __future__ import annotations

import base64
import re
import time
import uuid
from typing import Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit

from sitespy import data
from sitespy.config import get_settings
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
_ROUTE_PUT = "PUT /v1/tenants/{tenant_id}/logo"
_ROUTE_GET = "GET /v1/tenants/{tenant_id}/logo"

_GROUP_SUPER_ADMINS = "SuperAdmins"

_ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png"}
_MAX_LOGO_BYTES = 2 * 1024 * 1024  # 2 MB


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for PUT /v1/tenants/{tenant_id}/logo."""
    start_ms = time.monotonic() * 1000
    correlation_id = _resolve_correlation_id(event)

    try:
        result = _handle(event, correlation_id)
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_metric(name="PutTenantLogoSuccess", unit=MetricUnit.Count, value=1)
        logger.info(
            "put_tenant_logo_success",
            extra={
                "correlation_id": correlation_id,
                "route": _ROUTE_PUT,
                "status_code": result["statusCode"],
                "latency_ms": latency_ms,
            },
        )
        return result

    except ApiError as exc:
        latency_ms = int(time.monotonic() * 1000 - start_ms)
        metrics.add_metric(name="PutTenantLogoFailure", unit=MetricUnit.Count, value=1)
        logger.warning(
            "put_tenant_logo_failure",
            extra={
                "correlation_id": correlation_id,
                "route": _ROUTE_PUT,
                "status_code": exc.status_code,
                "latency_ms": latency_ms,
                "error": exc.error_key,
            },
        )
        return error_response(exc, correlation_id)

    except Exception:
        latency_ms = int(time.monotonic() * 1000 - start_ms)
        metrics.add_metric(name="PutTenantLogoFailure", unit=MetricUnit.Count, value=1)
        logger.exception(
            "put_tenant_logo_unhandled_error",
            extra={
                "correlation_id": correlation_id,
                "route": _ROUTE_PUT,
                "status_code": 500,
                "latency_ms": latency_ms,
            },
        )
        return unhandled_error_response(correlation_id)


# ---------------------------------------------------------------------------
# Internal handler
# ---------------------------------------------------------------------------


def _handle(event: dict[str, Any], correlation_id: str) -> dict[str, Any]:
    """Core logic for PUT /v1/tenants/{tenant_id}/logo — super admin only."""
    # --- Extract JWT claims and resolve caller role ---
    claims = _extract_claims(event)
    role = _resolve_role(claims)

    # --- Enforce super admin only ---
    if role != "super_admin":
        raise Forbidden()

    # --- Extract tenant_id from path ---
    path_params = event.get("pathParameters") or {}
    tenant_id = (path_params.get("tenant_id") or "").strip()

    if not tenant_id:
        raise BadRequest("Missing required path parameter: tenant_id.")

    # --- Verify tenant exists ---
    tenant_item = data.get_tenant(tenant_id)
    if tenant_item is None:
        raise NotFound("Tenant not found.")

    # --- Validate Content-Type ---
    headers = event.get("headers") or {}
    content_type = (
        headers.get("Content-Type")
        or headers.get("content-type")
        or ""
    ).strip().lower()

    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise BadRequest(
            f"Unsupported Content-Type: must be one of {', '.join(sorted(_ALLOWED_CONTENT_TYPES))}."
        )

    # --- Decode body ---
    raw_body = event.get("body")
    if not raw_body:
        raise BadRequest("Request body is required (binary image data).")

    is_base64 = event.get("isBase64Encoded", False)
    if is_base64:
        try:
            body_bytes = base64.b64decode(raw_body)
        except Exception:
            raise BadRequest("Failed to decode base64 body.")
    else:
        body_bytes = raw_body.encode("latin-1") if isinstance(raw_body, str) else raw_body

    # --- Validate file size ---
    if len(body_bytes) > _MAX_LOGO_BYTES:
        raise BadRequest(
            f"Logo file too large: maximum size is {_MAX_LOGO_BYTES // (1024 * 1024)} MB."
        )

    if len(body_bytes) == 0:
        raise BadRequest("Logo file is empty.")

    # --- Determine file extension ---
    extension = "jpg" if content_type == "image/jpeg" else "png"

    # --- Upload to S3 ---
    s3_key = f"logos/{tenant_id}/logo.{extension}"

    try:
        _put_logo(s3_key, body_bytes, content_type)
    except Exception:
        logger.exception("s3_put_logo_failed", extra={"tenant_id": tenant_id})
        raise InternalError()

    # --- Update tenant record with logo_url ---
    logo_url = f"logos/{tenant_id}/logo.{extension}"

    try:
        data.update_tenant_logo(tenant_id, logo_url)
    except Exception:
        logger.exception("dynamodb_update_tenant_logo_failed", extra={"tenant_id": tenant_id})
        raise InternalError()

    return json_response(
        200,
        {
            "tenant_id": tenant_id,
            "logo_key": logo_url,
            "content_type": content_type,
            "size_bytes": len(body_bytes),
        },
        correlation_id,
    )


# ---------------------------------------------------------------------------
# S3 helper
# ---------------------------------------------------------------------------


def _put_logo(key: str, body: bytes, content_type: str) -> None:
    """Upload a logo image to S3."""
    import boto3
    import botocore.config

    from sitespy.config import get_settings

    config = botocore.config.Config(retries={"mode": "standard", "max_attempts": 3})
    client = boto3.client("s3", region_name=get_settings().aws_region, config=config)

    client.put_object(
        Bucket=get_settings().snapshots_bucket,
        Key=key,
        Body=body,
        ContentType=content_type,
    )


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


# ---------------------------------------------------------------------------
# GET handler — returns presigned URL for the logo
# ---------------------------------------------------------------------------

_LOGO_URL_EXPIRY_SECONDS = 3600  # 1 hour


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler_get(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for GET /v1/tenants/{tenant_id}/logo."""
    start_ms = time.monotonic() * 1000
    correlation_id = _resolve_correlation_id(event)

    try:
        result = _handle_get(event, correlation_id)
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_metric(name="GetTenantLogoSuccess", unit=MetricUnit.Count, value=1)
        logger.info(
            "get_tenant_logo_success",
            extra={
                "correlation_id": correlation_id,
                "route": _ROUTE_GET,
                "status_code": result["statusCode"],
                "latency_ms": latency_ms,
            },
        )
        return result

    except ApiError as exc:
        latency_ms = int(time.monotonic() * 1000 - start_ms)
        metrics.add_metric(name="GetTenantLogoFailure", unit=MetricUnit.Count, value=1)
        logger.warning(
            "get_tenant_logo_failure",
            extra={
                "correlation_id": correlation_id,
                "route": _ROUTE_GET,
                "status_code": exc.status_code,
                "latency_ms": latency_ms,
                "error": exc.error_key,
            },
        )
        return error_response(exc, correlation_id)

    except Exception:
        latency_ms = int(time.monotonic() * 1000 - start_ms)
        metrics.add_metric(name="GetTenantLogoFailure", unit=MetricUnit.Count, value=1)
        logger.exception(
            "get_tenant_logo_unhandled_error",
            extra={
                "correlation_id": correlation_id,
                "route": _ROUTE_GET,
                "status_code": 500,
                "latency_ms": latency_ms,
            },
        )
        return unhandled_error_response(correlation_id)


def _handle_get(event: dict[str, Any], correlation_id: str) -> dict[str, Any]:
    """Core logic for GET /v1/tenants/{tenant_id}/logo — any authenticated user."""
    # --- Extract tenant_id from path ---
    path_params = event.get("pathParameters") or {}
    tenant_id = (path_params.get("tenant_id") or "").strip()

    if not tenant_id:
        raise BadRequest("Missing required path parameter: tenant_id.")

    # --- Fetch tenant and check for logo ---
    tenant_item = data.get_tenant(tenant_id)
    if tenant_item is None:
        raise NotFound("Tenant not found.")

    logo_url_attr = tenant_item.get("logo_url")
    if logo_url_attr is None:
        raise NotFound("No logo has been uploaded for this tenant.")

    logo_key = logo_url_attr.get("S", "") if isinstance(logo_url_attr, dict) else str(logo_url_attr)
    if not logo_key:
        raise NotFound("No logo has been uploaded for this tenant.")

    # --- Generate presigned URL ---
    try:
        presigned_url = _generate_logo_presigned_url(logo_key)
    except Exception:
        logger.exception("s3_presign_logo_failed", extra={"tenant_id": tenant_id})
        raise InternalError()

    return json_response(
        200,
        {
            "tenant_id": tenant_id,
            "presigned_url": presigned_url,
            "expires_in": _LOGO_URL_EXPIRY_SECONDS,
        },
        correlation_id,
    )


def _generate_logo_presigned_url(key: str) -> str:
    """Generate a presigned S3 GET URL for the logo."""
    import boto3
    import botocore.config

    config = botocore.config.Config(retries={"mode": "standard", "max_attempts": 3})
    client = boto3.client("s3", region_name=get_settings().aws_region, config=config)

    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": get_settings().snapshots_bucket, "Key": key},
        ExpiresIn=_LOGO_URL_EXPIRY_SECONDS,
    )
