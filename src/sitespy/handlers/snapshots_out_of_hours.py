"""Out-of-hours snapshot handlers for SiteSpy.

Three endpoints back the review / promote / download flow for Out_Of_Hours
snapshots — snapshots captured outside a site's configured working hours that
carry a fixed 7-day (604800 s) TTL unless preserved:

- ``GET  /v1/snapshots/out-of-hours``          — review (list by camera + date range)
- ``POST /v1/snapshots/out-of-hours/promote``  — promote (preserve past the 7-day expiry)
- ``GET  /v1/snapshots/out-of-hours/download`` — download (900 s presigned URL)

All three reuse the established Powertools + correlation-id + ``ApiError``
conventions and the shared ``data`` / ``storage`` / ``sandbox`` modules, and
mirror the auth / role / site-access model of ``snapshots.py``.

Requirements validated: 8.1–8.7, 9.1–9.8, 10.1–10.4
"""

from __future__ import annotations

import base64
import json
import re
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
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
# Strict UTC ISO-8601 with a literal Z suffix and second-level precision.
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

_ROUTE_LIST = "GET /v1/snapshots/out-of-hours"
_ROUTE_PROMOTE = "POST /v1/snapshots/out-of-hours/promote"
_ROUTE_DOWNLOAD = "GET /v1/snapshots/out-of-hours/download"

_REVIEW_PRESIGNED_TTL = 300  # 5 minutes (Req 8.3)
_DOWNLOAD_PRESIGNED_TTL = 900  # 15 minutes (Req 10.1)

_LIST_DEFAULT_LIMIT = 50
_LIST_MAX_LIMIT = 200
_LIST_DEFAULT_DAYS_BACK = 30

_RETENTION_CLASS_OUT_OF_HOURS = "Out_Of_Hours"

# Cognito group names
_GROUP_SUPER_ADMINS = "SuperAdmins"
_GROUP_TENANT_ADMINS = "TenantAdmins"


# ===========================================================================
# GET /v1/snapshots/out-of-hours — review (Req 8)
# ===========================================================================


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler_list(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for GET /v1/snapshots/out-of-hours."""
    start_ms = time.monotonic() * 1000
    correlation_id = _resolve_correlation_id(event)
    site_id = "unknown"
    tenant_id = "unknown"

    try:
        result = _handle_list(event, correlation_id)
        latency_ms = int(time.monotonic() * 1000 - start_ms)
        query_params = event.get("queryStringParameters") or {}
        site_id = query_params.get("site_id", "unknown")

        metrics.add_dimension(name="site_id", value=site_id)
        metrics.add_metric(name="ListOutOfHoursSuccess", unit=MetricUnit.Count, value=1)
        logger.info(
            "list_out_of_hours_success",
            extra={
                "correlation_id": correlation_id,
                "site_id": site_id,
                "route": _ROUTE_LIST,
                "status_code": result["statusCode"],
                "latency_ms": latency_ms,
            },
        )
        return result

    except ApiError as exc:
        latency_ms = int(time.monotonic() * 1000 - start_ms)
        metrics.add_dimension(name="site_id", value=site_id)
        metrics.add_dimension(name="tenant_id", value=tenant_id)
        metrics.add_dimension(name="status_code", value=str(exc.status_code))
        metrics.add_metric(name="ListOutOfHoursFailure", unit=MetricUnit.Count, value=1)
        logger.warning(
            "list_out_of_hours_failure",
            extra={
                "correlation_id": correlation_id,
                "site_id": site_id,
                "tenant_id": tenant_id,
                "route": _ROUTE_LIST,
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
        metrics.add_metric(name="ListOutOfHoursFailure", unit=MetricUnit.Count, value=1)
        logger.exception(
            "list_out_of_hours_unhandled_error",
            extra={
                "correlation_id": correlation_id,
                "site_id": site_id,
                "tenant_id": tenant_id,
                "route": _ROUTE_LIST,
                "status_code": 500,
                "latency_ms": latency_ms,
                "error": "INTERNAL_ERROR",
                "failure_reason": "unhandled_exception",
            },
        )
        return unhandled_error_response(correlation_id)


def _handle_list(event: dict[str, Any], correlation_id: str) -> dict[str, Any]:
    """Core logic for GET /v1/snapshots/out-of-hours — raises ApiError on failure."""
    query_params = event.get("queryStringParameters") or {}

    # --- Validate required parameters (Req 8.6) ---
    site_id = (query_params.get("site_id") or "").strip()
    if not site_id:
        raise BadRequest("Missing required query parameter: site_id.")

    camera_id = (query_params.get("camera_id") or "").strip()
    if not camera_id:
        raise BadRequest("Missing required query parameter: camera_id.")

    # --- Parse and validate limit (Req 8.4, 8.6) ---
    raw_limit = query_params.get("limit")
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except (ValueError, TypeError):
            raise BadRequest("limit must be an integer.") from None
        if limit < 1 or limit > _LIST_MAX_LIMIT:
            raise BadRequest(f"limit must be between 1 and {_LIST_MAX_LIMIT}.")
    else:
        limit = _LIST_DEFAULT_LIMIT

    # --- Parse from/to with a 30-day default range (Req 8.2, 8.6) ---
    now_utc = datetime.now(tz=UTC)
    raw_from = (query_params.get("from") or "").strip() or None
    raw_to = (query_params.get("to") or "").strip() or None

    if raw_from is None:
        from_ts = _format_ts(now_utc - timedelta(days=_LIST_DEFAULT_DAYS_BACK))
    else:
        from_ts = _validate_ooh_timestamp(raw_from, "from")

    if raw_to is None:
        to_ts = _format_ts(now_utc)
    else:
        to_ts = _validate_ooh_timestamp(raw_to, "to")

    # --- Decode opaque cursor (Req 8.4, 8.6) ---
    exclusive_start_key: dict | None = None
    raw_cursor = (query_params.get("cursor") or "").strip() or None
    if raw_cursor:
        try:
            decoded = base64.b64decode(raw_cursor.encode()).decode()
            exclusive_start_key = json.loads(decoded)
        except Exception:
            raise BadRequest("Invalid cursor value.") from None
        if not isinstance(exclusive_start_key, dict):
            raise BadRequest("Invalid cursor value.")

    # --- Auth (Req 8.7) ---
    tenant_id = _authorize(event, query_params, site_id)

    # --- Query DynamoDB (inclusive range, newest-first) ---
    try:
        items, last_evaluated_key = data.list_out_of_hours_img_records(
            tenant_id=tenant_id,
            site_id=site_id,
            camera_id=camera_id,
            from_ts=from_ts,
            to_ts=to_ts,
            limit=limit,
            exclusive_start_key=exclusive_start_key,
        )
    except Exception as exc:
        logger.exception("dynamodb_list_out_of_hours_failed")
        raise InternalError() from exc

    # --- Build snapshot list with 300 s presigned URLs (Req 8.3) ---
    snapshots: list[dict[str, Any]] = []
    for item in items:
        s3_key = item.get("s3_key", {}).get("S", "")
        timestamp = item.get("ingested_at", {}).get("S", "")
        promoted = bool(item.get("promoted", {}).get("BOOL", False))
        try:
            presigned_url = storage.generate_presigned_url(
                s3_key, expires_in=_REVIEW_PRESIGNED_TTL
            )
        except Exception as exc:
            logger.exception("s3_presign_failed", extra={"s3_key": s3_key})
            raise InternalError() from exc

        snapshots.append(
            {
                "snapshot_id": timestamp,
                "timestamp": timestamp,
                "camera_id": camera_id,
                "key": s3_key,
                "presigned_url": presigned_url,
                "expires_in": _REVIEW_PRESIGNED_TTL,
                "promoted": promoted,
            }
        )

    next_cursor: str | None = None
    if last_evaluated_key is not None:
        next_cursor = base64.b64encode(json.dumps(last_evaluated_key).encode()).decode()

    body = {
        "snapshots": snapshots,
        "next_cursor": next_cursor,
    }
    return json_response(200, body, correlation_id)


# ===========================================================================
# POST /v1/snapshots/out-of-hours/promote — promote (Req 9)
# ===========================================================================


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler_promote(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for POST /v1/snapshots/out-of-hours/promote."""
    start_ms = time.monotonic() * 1000
    correlation_id = _resolve_correlation_id(event)
    site_id = "unknown"
    tenant_id = "unknown"

    try:
        result = _handle_promote(event, correlation_id)
        latency_ms = int(time.monotonic() * 1000 - start_ms)
        metrics.add_metric(name="PromoteOutOfHoursSuccess", unit=MetricUnit.Count, value=1)
        logger.info(
            "promote_out_of_hours_success",
            extra={
                "correlation_id": correlation_id,
                "route": _ROUTE_PROMOTE,
                "status_code": result["statusCode"],
                "latency_ms": latency_ms,
            },
        )
        return result

    except ApiError as exc:
        latency_ms = int(time.monotonic() * 1000 - start_ms)
        metrics.add_dimension(name="status_code", value=str(exc.status_code))
        metrics.add_metric(name="PromoteOutOfHoursFailure", unit=MetricUnit.Count, value=1)
        logger.warning(
            "promote_out_of_hours_failure",
            extra={
                "correlation_id": correlation_id,
                "route": _ROUTE_PROMOTE,
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
        metrics.add_metric(name="PromoteOutOfHoursFailure", unit=MetricUnit.Count, value=1)
        logger.exception(
            "promote_out_of_hours_unhandled_error",
            extra={
                "correlation_id": correlation_id,
                "route": _ROUTE_PROMOTE,
                "status_code": 500,
                "latency_ms": latency_ms,
                "error": "INTERNAL_ERROR",
                "failure_reason": "unhandled_exception",
            },
        )
        return unhandled_error_response(correlation_id)


def _handle_promote(event: dict[str, Any], correlation_id: str) -> dict[str, Any]:
    """Core logic for POST /v1/snapshots/out-of-hours/promote — raises ApiError on failure."""
    body = _parse_body(event)

    # --- Validate required parameters (Req 9.6) ---
    site_id = _require_str(body.get("site_id"), "site_id")
    camera_id = _require_str(body.get("camera_id"), "camera_id")
    snapshot_id = _require_str(body.get("snapshot_id"), "snapshot_id")

    # --- Auth (Req 9.8) ---
    tenant_id = _authorize(event, body, site_id)

    # --- Load the out-of-hours record (Req 9.4) ---
    try:
        record = data.get_out_of_hours_img_record(
            tenant_id, site_id, camera_id, snapshot_id
        )
    except Exception as exc:
        logger.exception("dynamodb_get_out_of_hours_failed")
        raise InternalError() from exc

    if record is None:
        raise NotFound()

    retention_class = record.get("retention_class", {}).get("S", "")
    if retention_class != _RETENTION_CLASS_OUT_OF_HOURS:
        raise NotFound()

    promoted = bool(record.get("promoted", {}).get("BOOL", False))

    # --- Idempotent no-op for an already-promoted snapshot (Req 9.5) ---
    if promoted:
        return json_response(
            200,
            {
                "snapshot_id": snapshot_id,
                "site_id": site_id,
                "camera_id": camera_id,
                "promoted": True,
            },
            correlation_id,
        )

    # --- Expired-and-never-promoted → treat as not found (Req 9.4) ---
    if _is_expired(record):
        raise NotFound()

    source_key = record.get("s3_key", {}).get("S", "")
    if not source_key:
        logger.error("out_of_hours_record_missing_s3_key")
        raise InternalError()

    dest_key = storage.build_preserved_key(tenant_id, site_id, camera_id, snapshot_id)
    ooh_sk = data.build_out_of_hours_img_sk(site_id, camera_id, snapshot_id)
    promoted_at = _format_ts(datetime.now(tz=UTC))

    # --- Step 1: copy security/ → preserved/ (original left intact) ---
    try:
        storage.copy_object(source_key, dest_key)
    except Exception as exc:
        # Copy failed: record + ttl untouched, original still under its expiry.
        logger.exception("out_of_hours_promote_copy_failed", extra={"source_key": source_key})
        raise InternalError() from exc

    # --- Step 2: commit the DynamoDB update (SET s3_key/promoted/promoted_at REMOVE ttl) ---
    try:
        data.promote_out_of_hours_record(tenant_id, ooh_sk, dest_key, promoted_at)
    except Exception as exc:
        # Commit failed: roll back the copied preserved/ object so the original
        # object + ttl remain and the snapshot keeps its original expiry (Req 9.7).
        logger.exception("out_of_hours_promote_commit_failed", extra={"ooh_sk": ooh_sk})
        try:
            storage.delete_object(dest_key)
        except Exception:
            logger.exception(
                "out_of_hours_promote_rollback_failed", extra={"dest_key": dest_key}
            )
        raise InternalError() from exc

    # --- Step 3: delete the original security/ object (best-effort cleanup) ---
    # The record already points at preserved/; a failure here is harmless because
    # the stray original self-expires within the 7-day out-of-hours window.
    try:
        storage.delete_object(source_key)
    except Exception:
        logger.warning(
            "out_of_hours_promote_source_delete_failed",
            extra={"source_key": source_key},
        )

    return json_response(
        200,
        {
            "snapshot_id": snapshot_id,
            "site_id": site_id,
            "camera_id": camera_id,
            "promoted": True,
            "key": dest_key,
        },
        correlation_id,
    )


# ===========================================================================
# GET /v1/snapshots/out-of-hours/download — download (Req 10)
# ===========================================================================


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler_download(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for GET /v1/snapshots/out-of-hours/download."""
    start_ms = time.monotonic() * 1000
    correlation_id = _resolve_correlation_id(event)
    site_id = "unknown"
    tenant_id = "unknown"

    try:
        result = _handle_download(event, correlation_id)
        latency_ms = int(time.monotonic() * 1000 - start_ms)
        query_params = event.get("queryStringParameters") or {}
        site_id = query_params.get("site_id", "unknown")

        metrics.add_dimension(name="site_id", value=site_id)
        metrics.add_metric(name="DownloadOutOfHoursSuccess", unit=MetricUnit.Count, value=1)
        logger.info(
            "download_out_of_hours_success",
            extra={
                "correlation_id": correlation_id,
                "site_id": site_id,
                "route": _ROUTE_DOWNLOAD,
                "status_code": result["statusCode"],
                "latency_ms": latency_ms,
            },
        )
        return result

    except ApiError as exc:
        latency_ms = int(time.monotonic() * 1000 - start_ms)
        metrics.add_dimension(name="site_id", value=site_id)
        metrics.add_dimension(name="tenant_id", value=tenant_id)
        metrics.add_dimension(name="status_code", value=str(exc.status_code))
        metrics.add_metric(name="DownloadOutOfHoursFailure", unit=MetricUnit.Count, value=1)
        logger.warning(
            "download_out_of_hours_failure",
            extra={
                "correlation_id": correlation_id,
                "site_id": site_id,
                "tenant_id": tenant_id,
                "route": _ROUTE_DOWNLOAD,
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
        metrics.add_metric(name="DownloadOutOfHoursFailure", unit=MetricUnit.Count, value=1)
        logger.exception(
            "download_out_of_hours_unhandled_error",
            extra={
                "correlation_id": correlation_id,
                "site_id": site_id,
                "tenant_id": tenant_id,
                "route": _ROUTE_DOWNLOAD,
                "status_code": 500,
                "latency_ms": latency_ms,
                "error": "INTERNAL_ERROR",
                "failure_reason": "unhandled_exception",
            },
        )
        return unhandled_error_response(correlation_id)


def _handle_download(event: dict[str, Any], correlation_id: str) -> dict[str, Any]:
    """Core logic for GET /v1/snapshots/out-of-hours/download — raises ApiError on failure."""
    query_params = event.get("queryStringParameters") or {}

    # --- Validate required parameters (Req 10.3) ---
    site_id = _require_str(query_params.get("site_id"), "site_id")
    camera_id = _require_str(query_params.get("camera_id"), "camera_id")
    snapshot_id = _require_str(query_params.get("snapshot_id"), "snapshot_id")

    # --- Auth (Req 10.4) ---
    tenant_id = _authorize(event, query_params, site_id)

    # --- Load the out-of-hours record (Req 10.2) ---
    try:
        record = data.get_out_of_hours_img_record(
            tenant_id, site_id, camera_id, snapshot_id
        )
    except Exception as exc:
        logger.exception("dynamodb_get_out_of_hours_failed")
        raise InternalError() from exc

    if record is None:
        raise NotFound()

    retention_class = record.get("retention_class", {}).get("S", "")
    if retention_class != _RETENTION_CLASS_OUT_OF_HOURS:
        raise NotFound()

    promoted = bool(record.get("promoted", {}).get("BOOL", False))

    # --- Expired-and-never-promoted → 404 (Req 10.2) ---
    if not promoted and _is_expired(record):
        raise NotFound()

    s3_key = record.get("s3_key", {}).get("S", "")
    if not s3_key:
        logger.error("out_of_hours_record_missing_s3_key")
        raise InternalError()

    # --- Confirm the object is present so we never mint a broken link (Req 10.2) ---
    try:
        exists = storage.object_exists(s3_key)
    except Exception as exc:
        logger.exception("s3_object_exists_failed", extra={"s3_key": s3_key})
        raise InternalError() from exc

    if not exists:
        raise NotFound()

    # --- Mint a 900 s presigned URL (Req 10.1) ---
    try:
        presigned_url = storage.generate_presigned_url(
            s3_key, expires_in=_DOWNLOAD_PRESIGNED_TTL
        )
    except Exception as exc:
        logger.exception("s3_presign_failed", extra={"s3_key": s3_key})
        raise InternalError() from exc

    body = {
        "snapshot_id": snapshot_id,
        "camera_id": camera_id,
        "timestamp": record.get("ingested_at", {}).get("S", ""),
        "key": s3_key,
        "presigned_url": presigned_url,
        "expires_in": _DOWNLOAD_PRESIGNED_TTL,
        "promoted": promoted,
    }
    return json_response(200, body, correlation_id)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _authorize(
    event: dict[str, Any],
    params: Mapping[str, Any],
    site_id: str,
) -> str:
    """Resolve the caller, apply the sandbox guard, verify the site, and check access.

    Mirrors the auth/role/site-access model of ``snapshots.py``. Returns the
    resolved tenant_id. ``params`` supplies ``tenant_id`` for super admins
    (query params for GET, request body for POST).
    """
    claims = _extract_claims(event)
    role, caller_tenant_id, site_access = _resolve_caller(claims)

    if role == "super_admin":
        tenant_id_param = str(params.get("tenant_id") or "").strip()
        if not tenant_id_param:
            raise BadRequest("Super admins must supply tenant_id.")
        tenant_id = tenant_id_param
    else:
        if not caller_tenant_id:
            raise Forbidden()
        tenant_id = caller_tenant_id

    sandbox_visibility_guard(tenant_id, role)

    _fetch_site_or_404(tenant_id, site_id)
    _check_access(role, caller_tenant_id, tenant_id, site_id, site_access)

    return tenant_id


def _is_expired(record: Mapping[str, Any]) -> bool:
    """Return True if the record carries a ``ttl`` that has already elapsed.

    A promoted record has no ``ttl`` and is never considered expired. Records
    whose ``ttl`` is at or before the current epoch are logically expired even
    if the DynamoDB TimeToLive sweep has not yet removed them (the sweep is
    eventual, up to ~48h), so the handlers treat them as gone (Req 9.4, 10.2).
    """
    ttl_attr = record.get("ttl")
    if ttl_attr is None:
        return False
    try:
        ttl = int(ttl_attr.get("N", "0"))
    except (ValueError, TypeError, AttributeError):
        return False
    return ttl <= int(time.time())


def _validate_ooh_timestamp(raw: str, field: str) -> str:
    """Validate a from/to parameter as a strict YYYY-MM-DDTHH:MM:SSZ UTC datetime.

    Raises BadRequest when the value does not match the required format or is
    not a real calendar datetime (Req 8.6).
    """
    if not _TS_RE.match(raw):
        raise BadRequest(
            f"{field} must be a UTC datetime in YYYY-MM-DDTHH:MM:SSZ format."
        )
    try:
        datetime.strptime(raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        raise BadRequest(
            f"{field} must be a valid UTC datetime in YYYY-MM-DDTHH:MM:SSZ format."
        ) from None
    return raw


def _format_ts(dt: datetime) -> str:
    """Format a datetime as a YYYY-MM-DDTHH:MM:SSZ UTC string."""
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _require_str(value: Any, field: str) -> str:
    """Return a non-empty stripped string or raise BadRequest identifying the field."""
    if not isinstance(value, str):
        raise BadRequest(f"Missing required parameter: {field}.")
    stripped = value.strip()
    if not stripped:
        raise BadRequest(f"Missing required parameter: {field}.")
    return stripped


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
# Auth helpers (mirror snapshots.py)
# ---------------------------------------------------------------------------


def _extract_claims(event: dict[str, Any]) -> dict[str, Any]:
    """Extract Cognito JWT claims from the API Gateway authorizer context."""
    request_context = event.get("requestContext") or {}
    authorizer = request_context.get("authorizer") or {}
    claims: dict[str, Any] = authorizer.get("claims") or {}
    return claims


def _resolve_caller(claims: dict[str, Any]) -> tuple[str, str | None, list[str]]:
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


def _check_access(
    role: str,
    caller_tenant_id: str | None,
    site_tenant_id: str,
    site_id: str,
    site_access: list[str],
) -> None:
    """Raise Forbidden if the caller is not allowed to access this site."""
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


def _fetch_site_or_404(tenant_id: str, site_id: str) -> Mapping[str, Any]:
    """Fetch the site item or raise NotFound."""
    try:
        site_item = data.get_site(tenant_id, site_id)
    except Exception as exc:
        logger.exception("dynamodb_get_site_failed")
        raise InternalError() from exc

    if site_item is None:
        raise NotFound()

    return site_item


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
