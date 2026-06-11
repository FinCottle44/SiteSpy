"""Snapshots handler for SiteSpy — GET /v1/snapshots/latest and GET /v1/snapshots.

GET /v1/snapshots/latest returns the most recent snapshot for a single camera
(when ``camera_id`` is supplied) or for every camera in a site (when
``camera_id`` is omitted).

GET /v1/snapshots returns a paginated list of snapshots for a camera within a
date range.

Requirements validated: 3.1, 3.4, 3.5, 4.4, 4.5, 4.7, 6.1, 6.2, 6.3, 6.4
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

# ---------------------------------------------------------------------------
# Powertools setup
# ---------------------------------------------------------------------------

logger = Logger(service="sitespy")
metrics = Metrics(namespace="SiteSpy", service="sitespy")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_ROUTE = "GET /v1/snapshots/latest"
_ROUTE_LIST = "GET /v1/snapshots"
_PRESIGNED_TTL = 300  # 5 minutes

_LIST_DEFAULT_LIMIT = 50
_LIST_MAX_LIMIT = 200
_LIST_DEFAULT_DAYS_BACK = 30

# Cognito group names
_GROUP_SUPER_ADMINS = "SuperAdmins"
_GROUP_TENANT_ADMINS = "TenantAdmins"


# ---------------------------------------------------------------------------
# Lambda handler — GET /v1/snapshots (paginated list)
# ---------------------------------------------------------------------------


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler_list(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for GET /v1/snapshots."""
    start_ms = time.monotonic() * 1000
    correlation_id = _resolve_correlation_id(event)

    site_id = "unknown"
    tenant_id = "unknown"

    try:
        result = _handle_list(event, correlation_id)
        status_code = result["statusCode"]
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        query_params = event.get("queryStringParameters") or {}
        site_id = query_params.get("site_id", "unknown")

        metrics.add_dimension(name="site_id", value=site_id)
        metrics.add_metric(name="GetSnapshotsListSuccess", unit=MetricUnit.Count, value=1)

        logger.info(
            "get_snapshots_list_success",
            extra={
                "correlation_id": correlation_id,
                "site_id": site_id,
                "route": _ROUTE_LIST,
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
        metrics.add_metric(name="GetSnapshotsListFailure", unit=MetricUnit.Count, value=1)

        logger.warning(
            "get_snapshots_list_failure",
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
        metrics.add_metric(name="GetSnapshotsListFailure", unit=MetricUnit.Count, value=1)

        logger.exception(
            "get_snapshots_list_unhandled_error",
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


# ---------------------------------------------------------------------------
# Internal handler — GET /v1/snapshots
# ---------------------------------------------------------------------------


def _handle_list(event: dict[str, Any], correlation_id: str) -> dict[str, Any]:
    """Core logic for GET /v1/snapshots — raises ApiError on failure."""
    query_params = event.get("queryStringParameters") or {}

    # --- Validate required parameters ---
    site_id = (query_params.get("site_id") or "").strip()
    if not site_id:
        raise BadRequest("Missing required query parameter: site_id.")

    camera_id = (query_params.get("camera_id") or "").strip()
    if not camera_id:
        raise BadRequest("Missing required query parameter: camera_id.")

    # --- Parse and validate limit ---
    raw_limit = query_params.get("limit")
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except (ValueError, TypeError):
            raise BadRequest("limit must be an integer.")
        if limit < 1 or limit > _LIST_MAX_LIMIT:
            raise BadRequest(f"limit must be between 1 and {_LIST_MAX_LIMIT}.")
    else:
        limit = _LIST_DEFAULT_LIMIT

    # --- Parse from/to with defaults and normalization ---
    now_utc = datetime.now(tz=UTC)
    default_from = now_utc - timedelta(days=_LIST_DEFAULT_DAYS_BACK)

    raw_from = (query_params.get("from") or "").strip() or None
    raw_to = (query_params.get("to") or "").strip() or None

    from_ts = _normalize_timestamp(raw_from, default_from, is_start=True)
    to_ts = _normalize_timestamp(raw_to, now_utc, is_start=False)

    # --- Decode cursor ---
    exclusive_start_key: dict | None = None
    raw_cursor = (query_params.get("cursor") or "").strip() or None
    if raw_cursor:
        try:
            decoded = base64.b64decode(raw_cursor.encode()).decode()
            exclusive_start_key = json.loads(decoded)
        except Exception:
            raise BadRequest("Invalid cursor value.")

    # --- Extract JWT claims and resolve caller ---
    claims = _extract_claims(event)
    role, caller_tenant_id, site_access = _resolve_caller(claims)

    # --- Resolve tenant_id ---
    if role == "super_admin":
        tenant_id_param = (query_params.get("tenant_id") or "").strip()
        if not tenant_id_param:
            raise BadRequest("Super admins must supply tenant_id as a query parameter.")
        tenant_id = tenant_id_param
    else:
        if not caller_tenant_id:
            raise Forbidden()
        tenant_id = caller_tenant_id

    # --- Verify site exists ---
    _fetch_site_or_404(tenant_id, site_id)

    # --- Authorise the caller ---
    _check_access(role, caller_tenant_id, tenant_id, site_id, site_access)

    # --- Query DynamoDB ---
    try:
        items, last_evaluated_key = data.list_img_records(
            tenant_id=tenant_id,
            site_id=site_id,
            camera_id=camera_id,
            from_ts=from_ts,
            to_ts=to_ts,
            limit=limit,
            exclusive_start_key=exclusive_start_key,
        )
    except Exception as exc:
        logger.exception("dynamodb_list_img_records_failed")
        raise InternalError() from exc

    # --- Build image list with pre-signed URLs ---
    images = []
    for item in items:
        s3_key: str = item.get("s3_key", {}).get("S", "")
        timestamp: str = item.get("ingested_at", {}).get("S", "")
        try:
            presigned_url = storage.generate_presigned_url(s3_key, expires_in=_PRESIGNED_TTL)
        except Exception as exc:
            logger.exception("s3_presign_failed", extra={"s3_key": s3_key})
            raise InternalError() from exc

        image_entry: dict[str, Any] = {
            "timestamp": timestamp,
            "camera_id": camera_id,
            "key": s3_key,
            "presigned_url": presigned_url,
            "expires_in": _PRESIGNED_TTL,
        }

        # Include weather if present on the record
        weather_attr = item.get("weather")
        if weather_attr is not None:
            image_entry["weather"] = _marshal_weather(weather_attr)

        images.append(image_entry)

    # --- Build next_cursor ---
    next_cursor: str | None = None
    if last_evaluated_key is not None:
        encoded = base64.b64encode(json.dumps(last_evaluated_key).encode()).decode()
        next_cursor = encoded

    # --- total_available: best-effort count ---
    # MVP: return count of items on this page + 1 if there's a next page.
    total_available = len(images) + (1 if next_cursor is not None else 0)

    body = {
        "images": images,
        "next_cursor": next_cursor,
        "total_available": total_available,
    }
    return json_response(200, body, correlation_id)


# ---------------------------------------------------------------------------
# Timestamp normalization helpers
# ---------------------------------------------------------------------------


def _normalize_timestamp(
    raw: str | None,
    default: datetime,
    *,
    is_start: bool,
) -> str:
    """Normalize a raw from/to query parameter to a full ISO8601 UTC timestamp string.

    - If raw is None, use the default datetime.
    - If raw is a date only (YYYY-MM-DD), expand to start-of-day (T00:00:00Z) for
      is_start=True, or end-of-day (T23:59:59Z) for is_start=False.
    - If raw is already a full datetime, strip timezone info and normalize to Z suffix.

    Returns a string like "2025-06-15T14:00:00Z".
    """
    if raw is None:
        return default.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Date-only: YYYY-MM-DD
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        if is_start:
            return f"{raw}T00:00:00Z"
        else:
            return f"{raw}T23:59:59Z"

    # Full datetime — parse and normalize
    try:
        # Handle both "Z" suffix and "+00:00" offset
        normalized = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise BadRequest(f"Invalid datetime format: {raw!r}. Use ISO8601 (e.g. 2025-06-15 or 2025-06-15T14:00:00Z).")


# ---------------------------------------------------------------------------
# Lambda handler — GET /v1/snapshots/latest
# ---------------------------------------------------------------------------


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler_latest(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for GET /v1/snapshots/latest."""
    start_ms = time.monotonic() * 1000
    correlation_id = _resolve_correlation_id(event)

    site_id = "unknown"
    tenant_id = "unknown"

    try:
        result = _handle_latest(event, correlation_id)
        status_code = result["statusCode"]
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        query_params = event.get("queryStringParameters") or {}
        site_id = query_params.get("site_id", "unknown")

        metrics.add_dimension(name="site_id", value=site_id)
        metrics.add_metric(name="GetSnapshotsLatestSuccess", unit=MetricUnit.Count, value=1)

        logger.info(
            "get_snapshots_latest_success",
            extra={
                "correlation_id": correlation_id,
                "site_id": site_id,
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
        metrics.add_metric(name="GetSnapshotsLatestFailure", unit=MetricUnit.Count, value=1)

        logger.warning(
            "get_snapshots_latest_failure",
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
        metrics.add_metric(name="GetSnapshotsLatestFailure", unit=MetricUnit.Count, value=1)

        logger.exception(
            "get_snapshots_latest_unhandled_error",
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


def _handle_latest(event: dict[str, Any], correlation_id: str) -> dict[str, Any]:
    """Core logic for GET /v1/snapshots/latest — raises ApiError on failure."""
    query_params = event.get("queryStringParameters") or {}

    # --- Validate required site_id ---
    site_id = (query_params.get("site_id") or "").strip()
    if not site_id:
        raise BadRequest("Missing required query parameter: site_id.")

    camera_id = (query_params.get("camera_id") or "").strip() or None

    # --- Extract JWT claims and resolve caller ---
    claims = _extract_claims(event)
    role, caller_tenant_id, site_access = _resolve_caller(claims)

    # --- Resolve tenant_id ---
    if role == "super_admin":
        tenant_id_param = (query_params.get("tenant_id") or "").strip()
        if not tenant_id_param:
            raise BadRequest("Super admins must supply tenant_id as a query parameter.")
        tenant_id = tenant_id_param
    else:
        if not caller_tenant_id:
            raise Forbidden()
        tenant_id = caller_tenant_id

    # --- Verify site exists ---
    site_item = _fetch_site_or_404(tenant_id, site_id)

    # --- Authorise the caller ---
    _check_access(role, caller_tenant_id, tenant_id, site_id, site_access)

    # --- Single camera mode ---
    if camera_id is not None:
        body = _build_single_camera_response(tenant_id, site_id, camera_id)
        return json_response(200, body, correlation_id)

    # --- All cameras mode ---
    try:
        camera_items = data.get_cameras_for_site(tenant_id, site_id)
    except Exception as exc:
        logger.exception("dynamodb_get_cameras_failed")
        raise InternalError() from exc

    cameras = []
    for cam_item in camera_items:
        sk: str = cam_item.get("SK", {}).get("S", "")
        cam_id = sk.removeprefix(f"SITE#{site_id}#CAM#")
        cam_name: str = cam_item.get("camera_name", {}).get("S", "")
        entry = _build_camera_entry(tenant_id, site_id, cam_id, cam_name)
        cameras.append(entry)

    return json_response(200, {"cameras": cameras}, correlation_id)


# ---------------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------------


def _build_single_camera_response(
    tenant_id: str,
    site_id: str,
    camera_id: str,
) -> dict[str, Any]:
    """Build the single-camera response body, or raise NotFound if no snapshot."""
    try:
        img_record = data.get_latest_img_record(tenant_id, site_id, camera_id)
    except Exception as exc:
        logger.exception("dynamodb_get_latest_img_failed")
        raise InternalError() from exc

    if img_record is None:
        raise NotFound()

    s3_key: str = img_record.get("s3_key", {}).get("S", "")
    timestamp: str = img_record.get("ingested_at", {}).get("S", "")

    try:
        presigned_url = storage.generate_presigned_url(s3_key, expires_in=_PRESIGNED_TTL)
    except Exception as exc:
        logger.exception("s3_presign_failed")
        raise InternalError() from exc

    result: dict[str, Any] = {
        "camera_id": camera_id,
        "timestamp": timestamp,
        "key": s3_key,
        "presigned_url": presigned_url,
        "expires_in": _PRESIGNED_TTL,
        "age_seconds": compute_age_seconds(timestamp),
    }

    weather_attr = img_record.get("weather")
    if weather_attr is not None:
        result["weather"] = _marshal_weather(weather_attr)

    return result


def _build_camera_entry(
    tenant_id: str,
    site_id: str,
    camera_id: str,
    camera_name: str,
) -> dict[str, Any]:
    """Build one entry in the all-cameras response list.

    Returns a dict with null presigned_url / timestamp when no snapshot exists.
    """
    try:
        img_record = data.get_latest_img_record(tenant_id, site_id, camera_id)
    except Exception as exc:
        logger.exception("dynamodb_get_latest_img_failed", extra={"camera_id": camera_id})
        raise InternalError() from exc

    if img_record is None:
        return {
            "camera_id": camera_id,
            "camera_name": camera_name,
            "timestamp": None,
            "presigned_url": None,
            "expires_in": _PRESIGNED_TTL,
            "age_seconds": None,
            "weather": None,
        }

    s3_key: str = img_record.get("s3_key", {}).get("S", "")
    timestamp: str = img_record.get("ingested_at", {}).get("S", "")

    try:
        presigned_url = storage.generate_presigned_url(s3_key, expires_in=_PRESIGNED_TTL)
    except Exception as exc:
        logger.exception("s3_presign_failed", extra={"camera_id": camera_id})
        raise InternalError() from exc

    result: dict[str, Any] = {
        "camera_id": camera_id,
        "camera_name": camera_name,
        "timestamp": timestamp,
        "presigned_url": presigned_url,
        "expires_in": _PRESIGNED_TTL,
        "age_seconds": compute_age_seconds(timestamp),
    }

    weather_attr = img_record.get("weather")
    if weather_attr is not None:
        result["weather"] = _marshal_weather(weather_attr)
    else:
        result["weather"] = None

    return result


# ---------------------------------------------------------------------------
# Age computation
# ---------------------------------------------------------------------------


def compute_age_seconds(timestamp_str: str) -> int:
    """Compute the age in whole seconds between a UTC timestamp and now.

    Args:
        timestamp_str: UTC timestamp in YYYY-MM-DDTHH:mm:ssZ format.

    Returns:
        Non-negative integer seconds elapsed since the timestamp.
    """
    ts = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
    delta = datetime.now(tz=UTC) - ts
    return max(0, int(delta.total_seconds()))


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
# Weather marshalling
# ---------------------------------------------------------------------------


def _marshal_weather(weather_attr: dict[str, Any]) -> dict[str, Any] | None:
    """Convert a DynamoDB weather map attribute to a plain JSON-friendly dict.

    The weather attribute in DynamoDB is stored as:
      {"M": {"condition": {"S": "Rain"}, "temp_c": {"N": "14.2"}, ...}}

    Returns a flat dict like:
      {"condition": "Rain", "temp_c": 14.2, ...}

    Returns None if the attribute is malformed.
    """
    m = weather_attr.get("M")
    if not m:
        return None

    try:
        return {
            "condition": m.get("condition", {}).get("S", "Unknown"),
            "description": m.get("description", {}).get("S", ""),
            "temp_c": float(m.get("temp_c", {}).get("N", "0")),
            "feels_like_c": float(m.get("feels_like_c", {}).get("N", "0")),
            "humidity_pct": int(m.get("humidity_pct", {}).get("N", "0")),
            "wind_speed_ms": float(m.get("wind_speed_ms", {}).get("N", "0")),
            "wind_deg": int(m.get("wind_deg", {}).get("N", "0")),
            "visibility_m": int(m.get("visibility_m", {}).get("N", "0")),
            "cloud_pct": int(m.get("cloud_pct", {}).get("N", "0")),
        }
    except (ValueError, TypeError, AttributeError):
        return None


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
