"""Snapshots DELETE handler for SiteSpy — DELETE /v1/snapshots.

Deletes one or more snapshot captures (DynamoDB IMG# record + S3 object).
Accessible to super admins and tenant admins only.

Body accepts a list of snapshot keys to delete in batch.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit

from sitespy import data, storage
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
_ROUTE = "DELETE /v1/snapshots"

_GROUP_SUPER_ADMINS = "SuperAdmins"
_GROUP_TENANT_ADMINS = "TenantAdmins"

_MAX_DELETE_BATCH = 25


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for DELETE /v1/snapshots."""
    start_ms = time.monotonic() * 1000
    correlation_id = _resolve_correlation_id(event)

    try:
        result = _handle(event, correlation_id)
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_metric(name="DeleteSnapshotsSuccess", unit=MetricUnit.Count, value=1)
        logger.info(
            "delete_snapshots_success",
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
        metrics.add_metric(name="DeleteSnapshotsFailure", unit=MetricUnit.Count, value=1)

        logger.warning(
            "delete_snapshots_failure",
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
        metrics.add_metric(name="DeleteSnapshotsFailure", unit=MetricUnit.Count, value=1)

        logger.exception(
            "delete_snapshots_unhandled_error",
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
    """Core logic for DELETE /v1/snapshots — super admin or tenant admin only."""
    # --- Extract JWT claims and resolve role ---
    claims = _extract_claims(event)
    role, caller_tenant_id, _site_access = _resolve_caller(claims)

    # --- Enforce admin roles only ---
    if role not in ("super_admin", "tenant_admin"):
        raise Forbidden()

    # --- Resolve tenant_id ---
    query_params = event.get("queryStringParameters") or {}

    if role == "super_admin":
        tenant_id = (query_params.get("tenant_id") or "").strip()
        if not tenant_id:
            raise BadRequest("Super admins must supply tenant_id as a query parameter.")
    else:
        if not caller_tenant_id:
            raise Forbidden()
        tenant_id = caller_tenant_id

    # --- Sandbox visibility guard ---
    sandbox_visibility_guard(tenant_id, role)

    # --- Parse JSON body ---
    body = _parse_body(event)

    # --- Validate required fields ---
    site_id = body.get("site_id")
    camera_id = body.get("camera_id")
    timestamps = body.get("timestamps")

    if not site_id or not isinstance(site_id, str) or not site_id.strip():
        raise BadRequest("Missing required field: site_id.")

    if not camera_id or not isinstance(camera_id, str) or not camera_id.strip():
        raise BadRequest("Missing required field: camera_id.")

    if not timestamps or not isinstance(timestamps, list):
        raise BadRequest("Missing required field: timestamps (must be a non-empty array).")

    if len(timestamps) > _MAX_DELETE_BATCH:
        raise BadRequest(f"Cannot delete more than {_MAX_DELETE_BATCH} snapshots per request.")

    # Validate each timestamp is a string
    for ts in timestamps:
        if not isinstance(ts, str) or not ts.strip():
            raise BadRequest("Each item in timestamps must be a non-empty string.")

    # --- Verify site exists and caller has access ---
    site_item = _fetch_site_or_404(tenant_id, site_id)

    # Tenant admin: verify site belongs to their tenant
    if role == "tenant_admin" and caller_tenant_id != tenant_id:
        raise Forbidden()

    # --- Delete each snapshot ---
    deleted: list[str] = []
    not_found: list[str] = []

    for ts in timestamps:
        ts = ts.strip()
        # Build the IMG# SK to check existence
        img_sk = data.build_img_sk(site_id, camera_id, ts)
        pk = data.build_tenant_pk(tenant_id)

        try:
            img_item = data.get_img_record_by_key(tenant_id, img_sk)
        except Exception as exc:
            logger.exception("dynamodb_get_img_record_failed", extra={"sk": img_sk})
            raise InternalError() from exc

        if img_item is None:
            not_found.append(ts)
            continue

        # Get the S3 key from the record
        s3_key = img_item.get("s3_key", {}).get("S", "")

        # Delete from S3
        if s3_key:
            try:
                storage.delete_snapshot(s3_key)
            except Exception as exc:
                logger.exception("s3_delete_snapshot_failed", extra={"s3_key": s3_key})
                raise InternalError() from exc

        # Delete from DynamoDB
        try:
            data.delete_img_record(tenant_id, img_sk)
        except Exception as exc:
            logger.exception("dynamodb_delete_img_record_failed", extra={"sk": img_sk})
            raise InternalError() from exc

        deleted.append(ts)

    response_body: dict[str, Any] = {
        "deleted": deleted,
        "deleted_count": len(deleted),
    }
    if not_found:
        response_body["not_found"] = not_found

    return json_response(200, response_body, correlation_id)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _extract_claims(event: dict[str, Any]) -> dict[str, Any]:
    """Extract Cognito JWT claims from the API Gateway authorizer context."""
    request_context = event.get("requestContext") or {}
    authorizer = request_context.get("authorizer") or {}
    return authorizer.get("claims") or {}


def _resolve_caller(
    claims: dict[str, Any],
) -> tuple[str, str | None, list[str]]:
    """Resolve role, tenant_id, and site_access from JWT claims."""
    raw_groups = claims.get("cognito:groups") or ""
    if isinstance(raw_groups, list):
        groups: list[str] = raw_groups
    else:
        groups = [g.strip() for g in str(raw_groups).split(",") if g.strip()]
        if len(groups) == 1 and " " in groups[0]:
            groups = [g.strip() for g in groups[0].split() if g.strip()]

    if _GROUP_SUPER_ADMINS in groups:
        role = "super_admin"
    elif _GROUP_TENANT_ADMINS in groups:
        role = "tenant_admin"
    else:
        role = "user"

    tenant_id: str | None = claims.get("custom:tenant_id") or None
    raw_site_access = claims.get("custom:site_access") or ""
    site_access = [s.strip() for s in str(raw_site_access).split(",") if s.strip()]

    return role, tenant_id, site_access


# ---------------------------------------------------------------------------
# DynamoDB helpers
# ---------------------------------------------------------------------------


def _fetch_site_or_404(tenant_id: str, site_id: str) -> Any:
    """Fetch the site item or raise NotFound."""
    try:
        site_item = data.get_site(tenant_id, site_id)
    except Exception as exc:
        logger.exception("dynamodb_get_site_failed")
        raise InternalError() from exc

    if site_item is None:
        raise NotFound("Site not found.")

    return site_item


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
