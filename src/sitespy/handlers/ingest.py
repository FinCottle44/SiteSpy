"""Ingest handler for SiteSpy — POST /v1/ingest/{token}.

Receives JPEG snapshots from Axis cameras authenticated by a per-camera
opaque token in the URL path. Writes to S3 with integrity metadata and
records an IMG# item in DynamoDB.
"""

from __future__ import annotations

import base64
import hashlib
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit

from sitespy import data, storage
from sitespy.errors import ApiError, BadRequest, InternalError, Unauthorized
from sitespy.http import error_response, json_response, unhandled_error_response
from sitespy.weather import fetch_current_weather, weather_to_dynamo_map

# ---------------------------------------------------------------------------
# Powertools setup
# ---------------------------------------------------------------------------

logger = Logger(service="sitespy")
metrics = Metrics(namespace="SiteSpy", service="sitespy")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_TOKEN_RE = re.compile(r"^tk_[A-Za-z0-9_-]{20,80}$")
_MAX_BODY_BYTES = 10 * 1024 * 1024  # 10 MiB
_JPEG_MAGIC = b"\xff\xd8\xff"
_ROUTE = "POST /v1/ingest/{token}"
_CADENCE_MINUTES = 15


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for POST /v1/ingest/{token}."""
    start_ms = time.monotonic() * 1000

    tenant_id = "unknown"
    site_id = "unknown"
    camera_id = "unknown"

    correlation_id = resolve_correlation_id(event)

    try:
        result = _handle(event, correlation_id)
        status_code = result["statusCode"]
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        import json as _json

        body_dict = _json.loads(result.get("body", "{}"))
        sha256 = body_dict.get("sha256")
        size_bytes = body_dict.get("size_bytes")
        camera_id = body_dict.get("camera_id", "unknown")

        s3_key = body_dict.get("key", "")
        key_parts = s3_key.split("/")
        if len(key_parts) >= 3:
            tenant_id = key_parts[0]
            site_id = key_parts[1]

        metrics.add_dimension(name="tenant_id", value=tenant_id)
        metrics.add_dimension(name="site_id", value=site_id)
        metrics.add_dimension(name="camera_id", value=camera_id)
        metrics.add_metric(name="IngestSuccess", unit=MetricUnit.Count, value=1)

        logger.info(
            "ingest_success",
            extra={
                "correlation_id": correlation_id,
                "tenant_id": tenant_id,
                "site_id": site_id,
                "camera_id": camera_id,
                "route": _ROUTE,
                "status_code": status_code,
                "latency_ms": latency_ms,
                "sha256": sha256,
                "size_bytes": size_bytes,
            },
        )
        return result

    except ApiError as exc:
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_dimension(name="tenant_id", value=tenant_id)
        metrics.add_dimension(name="site_id", value=site_id)
        metrics.add_dimension(name="camera_id", value=camera_id)
        metrics.add_dimension(name="status_code", value=str(exc.status_code))
        metrics.add_metric(name="IngestFailure", unit=MetricUnit.Count, value=1)

        logger.warning(
            "ingest_failure",
            extra={
                "correlation_id": correlation_id,
                "tenant_id": tenant_id,
                "site_id": site_id,
                "camera_id": camera_id,
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

        metrics.add_dimension(name="tenant_id", value=tenant_id)
        metrics.add_dimension(name="site_id", value=site_id)
        metrics.add_dimension(name="camera_id", value=camera_id)
        metrics.add_dimension(name="status_code", value="500")
        metrics.add_metric(name="IngestFailure", unit=MetricUnit.Count, value=1)

        logger.exception(
            "ingest_unhandled_error",
            extra={
                "correlation_id": correlation_id,
                "tenant_id": tenant_id,
                "site_id": site_id,
                "camera_id": camera_id,
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
    """Straight-line ingest logic — raises ApiError on any user-visible failure."""
    # --- Extract and validate token from URL path ---
    path_params = event.get("pathParameters") or {}
    token = path_params.get("token") or ""

    if not token or not _TOKEN_RE.match(token):
        raise Unauthorized("Authentication failed.")

    # --- Look up camera by token via GSI1 ---
    camera_item = data.get_camera_by_token(token)
    if camera_item is None:
        raise Unauthorized("Authentication failed.")

    # Extract tenant/site/camera from the matched camera row
    tenant_id, site_id, camera_id = data.parse_camera_item_ids(camera_item)

    # --- Validate body ---
    body = _decode_body(event)

    if not body:
        raise BadRequest("Request body must not be empty.")

    if len(body) > _MAX_BODY_BYTES:
        raise BadRequest(f"Request body exceeds maximum size of {_MAX_BODY_BYTES} bytes.")

    if not body.startswith(_JPEG_MAGIC):
        raise BadRequest("Request body must be a valid JPEG (magic bytes FF D8 FF).")

    # --- Check ingest hours ---
    # If the site has ingest_hours configured, check whether the current time
    # (in the site's timezone) falls within the allowed window. If not, accept
    # the request (200) but skip saving the image.
    if _is_outside_ingest_hours(tenant_id, site_id):
        logger.info(
            "ingest_skipped_outside_hours",
            extra={
                "tenant_id": tenant_id,
                "site_id": site_id,
                "camera_id": camera_id,
                "correlation_id": correlation_id,
            },
        )
        return json_response(
            200,
            {
                "status": "skipped",
                "reason": "outside_ingest_hours",
                "camera_id": camera_id,
            },
            correlation_id,
        )

    # --- Cadence check ---
    # Enforce a minimum 15-minute gap between saved timelapse snapshots.
    # Fail open: if DynamoDB read errors, treat as no prior record.
    save_timelapse = True
    try:
        latest_img = data.get_latest_img_record(tenant_id, site_id, camera_id)
        if latest_img is not None:
            ingested_at_str = latest_img.get("ingested_at", {}).get("S", "")
            if ingested_at_str:
                ingested_at_dt = datetime.fromisoformat(
                    ingested_at_str.replace("Z", "+00:00")
                )
                now_utc = datetime.now(tz=UTC)
                if (now_utc - ingested_at_dt) < timedelta(minutes=_CADENCE_MINUTES):
                    save_timelapse = False
    except Exception:
        logger.exception(
            "cadence_check_dynamo_error",
            extra={
                "tenant_id": tenant_id,
                "site_id": site_id,
                "camera_id": camera_id,
                "correlation_id": correlation_id,
            },
        )
        save_timelapse = True

    # --- Live session check ---
    # Check if an active live session exists for this camera.
    # Fail open (save_live = False) on DynamoDB error per requirement 5.8.
    save_live = False
    try:
        session_record = data.get_live_session(tenant_id, site_id, camera_id)
        if session_record is not None:
            expires_at_str = session_record.get("expires_at", {}).get("S", "")
            if expires_at_str:
                expires_at_dt = datetime.fromisoformat(
                    expires_at_str.replace("Z", "+00:00")
                )
                now_utc = datetime.now(tz=UTC)
                if expires_at_dt > now_utc:
                    save_live = True
    except Exception:
        logger.exception(
            "live_session_check_dynamo_error",
            extra={
                "tenant_id": tenant_id,
                "site_id": site_id,
                "camera_id": camera_id,
                "correlation_id": correlation_id,
            },
        )
        save_live = False

    # --- If cadence suppresses timelapse and no live session, return early ---
    if not save_timelapse and not save_live:
        logger.info(
            "ingest_skipped_cadence_filter",
            extra={
                "tenant_id": tenant_id,
                "site_id": site_id,
                "camera_id": camera_id,
                "correlation_id": correlation_id,
            },
        )
        return json_response(
            200,
            {
                "status": "skipped",
                "reason": "cadence_filter",
                "camera_id": camera_id,
                "live_captured": False,
            },
            correlation_id,
        )

    # --- Hash and timestamp ---
    snapshot_ts = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    sha256_hex = hashlib.sha256(body).hexdigest()

    # --- Timelapse write (existing code path) ---
    key = ""
    if save_timelapse:
        key = storage.build_snapshot_key(tenant_id, site_id, camera_id, snapshot_ts)
        retention_years = data.get_retention_years(tenant_id)

        # --- Fetch weather for timelapse snapshots only (fail open) ---
        weather_map = None
        try:
            site_item = data.get_site(tenant_id, site_id)
            if site_item:
                lat_val = site_item.get("latitude", {}).get("N")
                lon_val = site_item.get("longitude", {}).get("N")
                if lat_val and lon_val:
                    weather = fetch_current_weather(float(lat_val), float(lon_val))
                    if weather:
                        weather_map = weather_to_dynamo_map(weather)
        except Exception:
            logger.warning(
                "weather_fetch_failed",
                extra={
                    "tenant_id": tenant_id,
                    "site_id": site_id,
                    "camera_id": camera_id,
                    "correlation_id": correlation_id,
                },
            )

        try:
            storage.put_snapshot(key, body, sha256_hex, snapshot_ts, tenant_id, retention_years)
        except Exception as exc:
            logger.exception("s3_put_snapshot_failed")
            raise InternalError("An internal error occurred.") from exc

        try:
            data.put_img_record(
                tenant_id=tenant_id,
                site_id=site_id,
                camera_id=camera_id,
                snapshot_ts=snapshot_ts,
                s3_key=key,
                sha256_hex=sha256_hex,
                size_bytes=len(body),
                weather=weather_map,
            )
        except Exception as exc:
            logger.exception("dynamodb_put_img_record_failed")
            raise InternalError("An internal error occurred.") from exc

    # --- Live write ---
    live_captured = False
    if save_live:
        live_key = storage.build_live_snapshot_key(tenant_id, site_id, camera_id, snapshot_ts)
        live_ttl = int(
            datetime.fromisoformat(snapshot_ts.replace("Z", "+00:00")).timestamp()
        ) + 3600

        try:
            storage.put_live_snapshot(live_key, body, sha256_hex, snapshot_ts, tenant_id)
        except Exception as exc:
            logger.exception("s3_put_live_snapshot_failed")
            raise InternalError("An internal error occurred.") from exc

        try:
            data.put_live_img_record(
                tenant_id=tenant_id,
                site_id=site_id,
                camera_id=camera_id,
                snapshot_ts=snapshot_ts,
                s3_key=live_key,
                sha256_hex=sha256_hex,
                size_bytes=len(body),
                ttl=live_ttl,
            )
        except Exception as exc:
            logger.exception("dynamodb_put_live_img_record_failed")
            raise InternalError("An internal error occurred.") from exc

        live_captured = True

    # --- Build response ---
    if save_timelapse:
        return json_response(
            201,
            {
                "key": key,
                "timestamp": snapshot_ts,
                "camera_id": camera_id,
                "sha256": sha256_hex,
                "size_bytes": len(body),
                "live_captured": live_captured,
            },
            correlation_id,
        )
    else:
        # Only live write occurred — cadence suppressed timelapse
        return json_response(
            200,
            {
                "status": "skipped",
                "reason": "cadence_filter",
                "camera_id": camera_id,
                "live_captured": live_captured,
            },
            correlation_id,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def resolve_correlation_id(event: dict[str, Any]) -> str:
    """Return the X-Correlation-Id header if valid, else a fresh UUID v4."""
    headers = event.get("headers") or {}
    value = headers.get("X-Correlation-Id") or headers.get("x-correlation-id") or ""
    if _CORRELATION_ID_RE.match(value):
        return value
    return str(uuid.uuid4())


def _is_outside_ingest_hours(tenant_id: str, site_id: str) -> bool:
    """Check if the current time is outside the site's configured ingest hours.

    Returns False (allow ingest) if:
    - No ingest_hours are configured on the site
    - The site doesn't exist (shouldn't happen at this point)
    - The current time in the site's timezone is within the configured window

    Returns True (skip ingest) if the current time is outside the window.

    Supports overnight windows (e.g. start=22:00, end=06:00).
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ingest_hours = data.get_site_ingest_hours(tenant_id, site_id)
    except Exception:
        logger.exception("failed_to_fetch_ingest_hours")
        # On failure, allow ingest (fail open)
        return False

    if ingest_hours is None:
        return False

    start_str = ingest_hours.get("start", "")
    end_str = ingest_hours.get("end", "")

    if not start_str or not end_str:
        return False

    # Get the site's timezone
    try:
        site_item = data.get_site(tenant_id, site_id)
    except Exception:
        logger.exception("failed_to_fetch_site_for_timezone")
        return False

    if site_item is None:
        return False

    tz_str = site_item.get("timezone", {}).get("S", "UTC")
    try:
        site_tz = ZoneInfo(tz_str)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        site_tz = ZoneInfo("UTC")

    # Get current time in site's timezone
    now_local = datetime.now(tz=UTC).astimezone(site_tz)
    current_minutes = now_local.hour * 60 + now_local.minute

    # Parse start/end into minutes since midnight
    start_parts = start_str.split(":")
    end_parts = end_str.split(":")
    start_minutes = int(start_parts[0]) * 60 + int(start_parts[1])
    end_minutes = int(end_parts[0]) * 60 + int(end_parts[1])

    # Check if current time is within the window
    if start_minutes < end_minutes:
        # Normal window (e.g. 07:00 to 18:00)
        is_within = start_minutes <= current_minutes < end_minutes
    else:
        # Overnight window (e.g. 22:00 to 06:00)
        is_within = current_minutes >= start_minutes or current_minutes < end_minutes

    return not is_within


def _decode_body(event: dict[str, Any]) -> bytes:
    """Decode the request body from the API Gateway event."""
    raw = event.get("body") or ""
    if not raw:
        return b""
    if event.get("isBase64Encoded"):
        return base64.b64decode(str(raw))
    if isinstance(raw, str):
        return raw.encode("utf-8")
    if isinstance(raw, bytes):
        return raw
    return str(raw).encode("utf-8")
