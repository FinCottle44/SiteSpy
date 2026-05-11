"""Flags handler for SiteSpy — POST /v1/flags and GET /v1/flags.

POST /v1/flags: Raises a new flag on a specific camera.  Performs duplicate
suppression: if an open or acknowledged flag already exists for the same
camera + reason, the existing flag is returned with 200 instead of creating
a duplicate.

GET /v1/flags: Lists flags scoped by the caller's role, with optional
filtering by status, tenant_id, site_id, camera_id.  Includes latest_snapshot
for each flag's camera.

Requirements validated: 7.5, 7.6, 8.1, 8.2, 8.3, 8.4, 8.5, 8.8, 8.9
"""

from __future__ import annotations

import base64
import json
import re
import time
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit

from sitespy import data
from sitespy.errors import ApiError, BadRequest, Conflict, Forbidden, InternalError, NotFound
from sitespy.http import error_response, json_response, unhandled_error_response
from sitespy.storage import build_snapshot_key, generate_presigned_url

# ---------------------------------------------------------------------------
# Powertools setup
# ---------------------------------------------------------------------------

logger = Logger(service="sitespy")
metrics = Metrics(namespace="SiteSpy", service="sitespy")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_ROUTE = "POST /v1/flags"

_VALID_REASONS = frozenset(
    {"stale_image", "physical_damage", "obstruction", "image_quality", "other"}
)
_NOTE_MAX_LEN = 1000

# Cognito group names
_GROUP_SUPER_ADMINS = "SuperAdmins"
_GROUP_TENANT_ADMINS = "TenantAdmins"

# GET /v1/flags constants
_ROUTE_GET = "GET /v1/flags"
_DEFAULT_STATUSES = ["open", "acknowledged"]
_VALID_STATUSES = frozenset({"open", "acknowledged", "resolved", "dismissed"})
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200
_PRESIGNED_URL_TTL = 300


# ---------------------------------------------------------------------------
# Lambda handler — GET /v1/flags
# ---------------------------------------------------------------------------


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler_get(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for GET /v1/flags."""
    start_ms = time.monotonic() * 1000
    correlation_id = _resolve_correlation_id(event)

    try:
        result = _handle_get(event, correlation_id)
        status_code = result["statusCode"]
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_metric(name="GetFlagsSuccess", unit=MetricUnit.Count, value=1)

        logger.info(
            "get_flags_success",
            extra={
                "correlation_id": correlation_id,
                "route": _ROUTE_GET,
                "status_code": status_code,
                "latency_ms": latency_ms,
            },
        )
        return result

    except ApiError as exc:
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_metric(name="GetFlagsFailure", unit=MetricUnit.Count, value=1)

        logger.warning(
            "get_flags_failure",
            extra={
                "correlation_id": correlation_id,
                "route": _ROUTE_GET,
                "status_code": exc.status_code,
                "latency_ms": latency_ms,
                "error": exc.error_key,
                "failure_reason": type(exc).__name__.lower(),
            },
        )
        return error_response(exc, correlation_id)

    except Exception:
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_metric(name="GetFlagsFailure", unit=MetricUnit.Count, value=1)

        logger.exception(
            "get_flags_unhandled_error",
            extra={
                "correlation_id": correlation_id,
                "route": _ROUTE_GET,
                "status_code": 500,
                "latency_ms": latency_ms,
                "error": "INTERNAL_ERROR",
                "failure_reason": "unhandled_exception",
            },
        )
        return unhandled_error_response(correlation_id)


# ---------------------------------------------------------------------------
# Internal handler — GET /v1/flags
# ---------------------------------------------------------------------------


def _handle_get(event: dict[str, Any], correlation_id: str) -> dict[str, Any]:
    """Straight-line flags GET logic — raises ApiError on any user-visible failure."""
    query_params = event.get("queryStringParameters") or {}

    # --- Parse limit ---
    limit_raw = query_params.get("limit", "")
    if limit_raw:
        try:
            limit = int(limit_raw)
        except ValueError:
            raise BadRequest("Invalid 'limit' parameter: must be an integer.")
        if limit < 1 or limit > _MAX_LIMIT:
            raise BadRequest(
                f"Invalid 'limit' parameter: must be between 1 and {_MAX_LIMIT}."
            )
    else:
        limit = _DEFAULT_LIMIT

    # --- Parse status filter ---
    status_raw = query_params.get("status", "")
    if status_raw:
        status_list = [s.strip() for s in status_raw.split(",") if s.strip()]
        for s in status_list:
            if s not in _VALID_STATUSES:
                raise BadRequest(
                    f"Invalid status '{s}'. Must be one of: {', '.join(sorted(_VALID_STATUSES))}."
                )
    else:
        status_list = list(_DEFAULT_STATUSES)

    # --- Parse optional filters ---
    site_id_filter = (query_params.get("site_id") or "").strip() or None
    camera_id_filter = (query_params.get("camera_id") or "").strip() or None
    tenant_id_param = (query_params.get("tenant_id") or "").strip() or None

    # --- Extract JWT claims and resolve caller ---
    claims = _extract_claims(event)
    role, caller_tenant_id, site_access = _resolve_caller(claims)

    # --- Determine effective tenant_id filter based on role ---
    if role == "super_admin":
        # Super admin can optionally filter by tenant_id
        effective_tenant_id = tenant_id_param  # may be None (all tenants)
    elif role == "tenant_admin":
        # Tenant admin sees only their own tenant; ignore tenant_id param
        if not caller_tenant_id:
            raise Forbidden()
        effective_tenant_id = caller_tenant_id
    else:
        # Regular user: must have a tenant_id, sees only their own tenant
        if not caller_tenant_id:
            raise Forbidden()
        effective_tenant_id = caller_tenant_id

    # --- Parse cursor ---
    cursor_raw = (query_params.get("cursor") or "").strip()
    exclusive_start_key: dict | None = None
    if cursor_raw:
        try:
            decoded = base64.b64decode(cursor_raw.encode()).decode()
            exclusive_start_key = json.loads(decoded)
        except Exception:
            raise BadRequest("Invalid 'cursor' parameter.")

    # --- Query DynamoDB ---
    try:
        items, last_key = data.list_flags(
            status_list=status_list,
            tenant_id=effective_tenant_id,
            site_id=site_id_filter,
            camera_id=camera_id_filter,
            limit=limit,
            exclusive_start_key=exclusive_start_key,
        )
    except Exception as exc:
        logger.exception("dynamodb_list_flags_failed")
        raise InternalError() from exc

    # --- For user role: filter to only sites in site_access ---
    if role == "user":
        items = [
            item for item in items
            if item.get("site_id", {}).get("S", "") in site_access
        ]

    # --- Build next_cursor ---
    next_cursor: str | None = None
    if last_key and last_key != {"_overflow": True}:
        next_cursor = base64.b64encode(json.dumps(last_key).encode()).decode()
    elif last_key == {"_overflow": True}:
        # We had more items than limit — encode a synthetic cursor
        # Use the last item's keys as the continuation point
        if items:
            last_item = items[-1]
            synthetic_key = {
                "PK": last_item.get("PK", {}).get("S", ""),
                "SK": last_item.get("SK", {}).get("S", ""),
                "GSI1PK": last_item.get("GSI1PK", {}).get("S", ""),
                "GSI1SK": last_item.get("GSI1SK", {}).get("S", ""),
            }
            next_cursor = base64.b64encode(json.dumps(synthetic_key).encode()).decode()

    # --- Build response flags with latest_snapshot ---
    flags_out: list[dict[str, Any]] = []
    for item in items:
        flag_tenant_id = item.get("tenant_id", {}).get("S", "")
        flag_site_id = item.get("site_id", {}).get("S", "")
        flag_camera_id = item.get("camera_id", {}).get("S", "")

        # Fetch latest snapshot for this camera
        latest_snapshot: dict[str, Any] | None = None
        try:
            img_record = data.get_latest_img_record(
                flag_tenant_id, flag_site_id, flag_camera_id
            )
            if img_record is not None:
                s3_key = img_record.get("s3_key", {}).get("S", "")
                snapshot_ts = img_record.get("ingested_at", {}).get("S", "")
                presigned_url = generate_presigned_url(s3_key, expires_in=_PRESIGNED_URL_TTL)
                latest_snapshot = {
                    "timestamp": snapshot_ts,
                    "presigned_url": presigned_url,
                    "expires_in": _PRESIGNED_URL_TTL,
                }
        except Exception:
            logger.warning(
                "latest_snapshot_fetch_failed",
                extra={
                    "tenant_id": flag_tenant_id,
                    "site_id": flag_site_id,
                    "camera_id": flag_camera_id,
                },
            )
            latest_snapshot = None

        flag_out: dict[str, Any] = {
            "flag_id": item.get("flag_id", {}).get("S", ""),
            "tenant_id": flag_tenant_id,
            "site_id": flag_site_id,
            "camera_id": flag_camera_id,
            "reason": item.get("reason", {}).get("S", ""),
            "note": item.get("note", {}).get("S") if "note" in item else None,
            "status": item.get("status", {}).get("S", ""),
            "source": item.get("source", {}).get("S", ""),
            "raised_by": item.get("raised_by", {}).get("S", ""),
            "raised_at": item.get("raised_at", {}).get("S", ""),
            "latest_snapshot": latest_snapshot,
        }
        flags_out.append(flag_out)

    # --- total_available: best-effort ---
    total_available = len(flags_out) + (1 if next_cursor else 0)

    return json_response(
        200,
        {
            "flags": flags_out,
            "next_cursor": next_cursor,
            "total_available": total_available,
        },
        correlation_id,
    )


# ---------------------------------------------------------------------------
# Lambda handler — POST /v1/flags
# ---------------------------------------------------------------------------


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler_post(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for POST /v1/flags."""
    start_ms = time.monotonic() * 1000
    correlation_id = _resolve_correlation_id(event)

    site_id = "unknown"
    tenant_id = "unknown"

    try:
        result = _handle_post(event, correlation_id)
        status_code = result["statusCode"]
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_metric(name="PostFlagSuccess", unit=MetricUnit.Count, value=1)

        logger.info(
            "post_flag_success",
            extra={
                "correlation_id": correlation_id,
                "route": _ROUTE,
                "status_code": status_code,
                "latency_ms": latency_ms,
            },
        )
        return result

    except ApiError as exc:
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_dimension(name="site_id", value=site_id)
        metrics.add_dimension(name="tenant_id", value=tenant_id)
        metrics.add_dimension(name="status_code", value=str(exc.status_code))
        metrics.add_metric(name="PostFlagFailure", unit=MetricUnit.Count, value=1)

        logger.warning(
            "post_flag_failure",
            extra={
                "correlation_id": correlation_id,
                "site_id": site_id,
                "tenant_id": tenant_id,
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

        metrics.add_dimension(name="site_id", value=site_id)
        metrics.add_dimension(name="tenant_id", value=tenant_id)
        metrics.add_dimension(name="status_code", value="500")
        metrics.add_metric(name="PostFlagFailure", unit=MetricUnit.Count, value=1)

        logger.exception(
            "post_flag_unhandled_error",
            extra={
                "correlation_id": correlation_id,
                "site_id": site_id,
                "tenant_id": tenant_id,
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


def _handle_post(event: dict[str, Any], correlation_id: str) -> dict[str, Any]:
    """Straight-line flags POST logic — raises ApiError on any user-visible failure."""
    # --- Parse and validate request body ---
    body = _parse_body(event)

    site_id = (body.get("site_id") or "").strip()
    camera_id = (body.get("camera_id") or "").strip()
    reason = (body.get("reason") or "").strip()
    note_raw = body.get("note")
    note: str | None = note_raw.strip() if isinstance(note_raw, str) else None

    if not site_id:
        raise BadRequest("Missing required field: site_id.")
    if not camera_id:
        raise BadRequest("Missing required field: camera_id.")
    if not reason:
        raise BadRequest("Missing required field: reason.")
    if reason not in _VALID_REASONS:
        raise BadRequest(
            f"Invalid reason '{reason}'. Must be one of: {', '.join(sorted(_VALID_REASONS))}."
        )
    if reason == "other" and not note:
        raise BadRequest("Field 'note' is required when reason is 'other'.")
    if note and len(note) > _NOTE_MAX_LEN:
        raise BadRequest(f"Field 'note' must not exceed {_NOTE_MAX_LEN} characters.")

    # --- Extract JWT claims ---
    claims = _extract_claims(event)
    role, caller_tenant_id, site_access = _resolve_caller(claims)
    raised_by: str = claims.get("sub") or "unknown"

    # --- Resolve tenant_id ---
    if role == "super_admin":
        query_params = event.get("queryStringParameters") or {}
        tenant_id_param = (query_params.get("tenant_id") or "").strip()
        if not tenant_id_param:
            raise BadRequest("Super admins must supply tenant_id as a query parameter.")
        tenant_id = tenant_id_param
    else:
        if not caller_tenant_id:
            raise Forbidden()
        tenant_id = caller_tenant_id

    # --- Authorise the caller ---
    _check_access(role, caller_tenant_id, tenant_id, site_id, site_access)

    # --- Verify camera exists ---
    try:
        camera_item = data.get_camera(tenant_id, site_id, camera_id)
    except Exception as exc:
        logger.exception("dynamodb_get_camera_failed")
        raise InternalError() from exc

    if camera_item is None:
        raise NotFound()

    # --- Duplicate suppression ---
    try:
        existing = data.get_open_flag(tenant_id, site_id, camera_id, reason)
    except Exception as exc:
        logger.exception("dynamodb_get_open_flag_failed")
        raise InternalError() from exc

    if existing is not None:
        return json_response(
            200,
            {
                "flag_id": existing["flag_id"]["S"],
                "status": existing["status"]["S"],
                "raised_at": existing["raised_at"]["S"],
                "duplicate": True,
            },
            correlation_id,
        )

    # --- Create new flag ---
    flag_id = str(uuid.uuid4())
    raised_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        data.put_flag(
            tenant_id=tenant_id,
            site_id=site_id,
            camera_id=camera_id,
            reason=reason,
            note=note if note else None,
            raised_by=raised_by,
            flag_id=flag_id,
            raised_at=raised_at,
        )
    except Exception as exc:
        logger.exception("dynamodb_put_flag_failed")
        raise InternalError() from exc

    return json_response(
        201,
        {
            "flag_id": flag_id,
            "status": "open",
            "raised_at": raised_at,
        },
        correlation_id,
    )


# ---------------------------------------------------------------------------
# Constants — PATCH /v1/flags/{flag_id}
# ---------------------------------------------------------------------------

_ROUTE_PATCH = "PATCH /v1/flags/{flag_id}"
_ADMIN_NOTES_MAX_LEN = 2000

# Valid state transitions: {current_status: set of allowed next statuses}
_VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "open": frozenset({"acknowledged", "resolved", "dismissed"}),
    "acknowledged": frozenset({"resolved", "dismissed"}),
}


# ---------------------------------------------------------------------------
# Lambda handler — PATCH /v1/flags/{flag_id}
# ---------------------------------------------------------------------------


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler_patch(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for PATCH /v1/flags/{flag_id}."""
    start_ms = time.monotonic() * 1000
    correlation_id = _resolve_correlation_id(event)

    try:
        result = _handle_patch(event, correlation_id)
        status_code = result["statusCode"]
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_metric(name="PatchFlagSuccess", unit=MetricUnit.Count, value=1)

        logger.info(
            "patch_flag_success",
            extra={
                "correlation_id": correlation_id,
                "route": _ROUTE_PATCH,
                "status_code": status_code,
                "latency_ms": latency_ms,
            },
        )
        return result

    except ApiError as exc:
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_metric(name="PatchFlagFailure", unit=MetricUnit.Count, value=1)

        logger.warning(
            "patch_flag_failure",
            extra={
                "correlation_id": correlation_id,
                "route": _ROUTE_PATCH,
                "status_code": exc.status_code,
                "latency_ms": latency_ms,
                "error": exc.error_key,
                "failure_reason": type(exc).__name__.lower(),
            },
        )
        return error_response(exc, correlation_id)

    except Exception:
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_metric(name="PatchFlagFailure", unit=MetricUnit.Count, value=1)

        logger.exception(
            "patch_flag_unhandled_error",
            extra={
                "correlation_id": correlation_id,
                "route": _ROUTE_PATCH,
                "status_code": 500,
                "latency_ms": latency_ms,
                "error": "INTERNAL_ERROR",
                "failure_reason": "unhandled_exception",
            },
        )
        return unhandled_error_response(correlation_id)


# ---------------------------------------------------------------------------
# Internal handler — PATCH /v1/flags/{flag_id}
# ---------------------------------------------------------------------------


def _handle_patch(event: dict[str, Any], correlation_id: str) -> dict[str, Any]:
    """Straight-line flags PATCH logic — raises ApiError on any user-visible failure."""
    # --- Extract path parameter ---
    path_params = event.get("pathParameters") or {}
    flag_id = (path_params.get("flag_id") or "").strip()
    if not flag_id:
        raise BadRequest("Missing required path parameter: flag_id.")

    # --- Parse and validate request body ---
    body = _parse_body(event)

    new_status = (body.get("status") or "").strip()
    if not new_status:
        raise BadRequest("Missing required field: status.")
    if new_status not in _VALID_STATUSES:
        raise BadRequest(
            f"Invalid status '{new_status}'. Must be one of: {', '.join(sorted(_VALID_STATUSES))}."
        )

    admin_notes_raw = body.get("admin_notes")
    admin_notes: str | None = (
        admin_notes_raw.strip() if isinstance(admin_notes_raw, str) else None
    )
    if admin_notes is not None and len(admin_notes) > _ADMIN_NOTES_MAX_LEN:
        raise BadRequest(
            f"Field 'admin_notes' must not exceed {_ADMIN_NOTES_MAX_LEN} characters."
        )

    # --- Extract JWT claims and resolve caller ---
    claims = _extract_claims(event)
    role, caller_tenant_id, _site_access = _resolve_caller(claims)
    acting_user: str = claims.get("sub") or "unknown"

    # --- Enforce min role: tenant admin ---
    if role == "user":
        raise Forbidden()

    # --- Resolve tenant_id ---
    if role == "super_admin":
        query_params = event.get("queryStringParameters") or {}
        tenant_id_param = (query_params.get("tenant_id") or "").strip()
        if not tenant_id_param:
            raise BadRequest("Super admins must supply tenant_id as a query parameter.")
        tenant_id = tenant_id_param
    else:
        # tenant_admin
        if not caller_tenant_id:
            raise Forbidden()
        tenant_id = caller_tenant_id

    # --- Fetch the flag ---
    try:
        flag_item = data.get_flag_by_id(tenant_id, flag_id)
    except Exception as exc:
        logger.exception("dynamodb_get_flag_by_id_failed")
        raise InternalError() from exc

    if flag_item is None:
        raise NotFound()

    # --- Validate state transition ---
    current_status = flag_item.get("status", {}).get("S", "")
    allowed_next = _VALID_TRANSITIONS.get(current_status, frozenset())
    if new_status not in allowed_next:
        raise Conflict()

    # --- Perform the update ---
    pk = flag_item["PK"]["S"]
    sk = flag_item["SK"]["S"]
    updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        data.update_flag_status(
            pk=pk,
            sk=sk,
            new_status=new_status,
            admin_notes=admin_notes,
            acting_user=acting_user,
            updated_at=updated_at,
        )
    except Exception as exc:
        logger.exception("dynamodb_update_flag_status_failed")
        raise InternalError() from exc

    return json_response(
        200,
        {
            "flag_id": flag_id,
            "status": new_status,
            "updated_at": updated_at,
        },
        correlation_id,
    )


# ---------------------------------------------------------------------------
# Auth helpers (mirrors sites.py)
# ---------------------------------------------------------------------------


def _extract_claims(event: dict[str, Any]) -> dict[str, Any]:
    """Extract Cognito JWT claims from the API Gateway authorizer context."""
    request_context = event.get("requestContext") or {}
    authorizer = request_context.get("authorizer") or {}
    claims: dict[str, Any] = authorizer.get("claims") or {}
    return claims


def _resolve_caller(
    claims: dict[str, Any],
) -> tuple[str, str | None, list[str]]:
    """Resolve role, tenant_id, and site_access from JWT claims.

    Returns:
        (role, tenant_id, site_access)
        role: 'super_admin' | 'tenant_admin' | 'user'
        tenant_id: str or None (super admins have no tenant)
        site_access: list of site IDs (empty for admins)
    """
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


def _check_access(
    role: str,
    caller_tenant_id: str | None,
    site_tenant_id: str,
    site_id: str,
    site_access: list[str],
) -> None:
    """Raise Forbidden if the caller is not allowed to access this site.

    Rules (from multi-tenant-auth.md §4):
    - super_admin: always allowed
    - tenant_admin: site's tenant_id must match token's tenant_id
    - user: site's tenant_id must match AND site_id must be in site_access
    """
    if role == "super_admin":
        return

    if role == "tenant_admin":
        if caller_tenant_id != site_tenant_id:
            raise Forbidden()
        return

    # role == "user"
    if caller_tenant_id != site_tenant_id:
        raise Forbidden()
    if site_id not in site_access:
        raise Forbidden()


# ---------------------------------------------------------------------------
# Request parsing helpers
# ---------------------------------------------------------------------------


def _parse_body(event: dict[str, Any]) -> dict[str, Any]:
    """Parse the JSON request body, returning an empty dict on failure."""
    raw = event.get("body") or ""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        return {}
    except (json.JSONDecodeError, ValueError):
        return {}


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
