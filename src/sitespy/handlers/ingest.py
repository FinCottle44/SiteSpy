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
from datetime import UTC, datetime
from typing import Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit

from sitespy import data, storage
from sitespy.errors import ApiError, BadRequest, InternalError, Unauthorized
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
_TOKEN_RE = re.compile(r"^tk_[A-Za-z0-9_-]{20,80}$")
_MAX_BODY_BYTES = 10 * 1024 * 1024  # 10 MiB
_JPEG_MAGIC = b"\xff\xd8\xff"
_ROUTE = "POST /v1/ingest/{token}"


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

    # --- Hash, key, S3 write, DynamoDB write ---
    snapshot_ts = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    sha256_hex = hashlib.sha256(body).hexdigest()
    key = storage.build_snapshot_key(tenant_id, site_id, camera_id, snapshot_ts)
    retention_years = data.get_retention_years(tenant_id)

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
        )
    except Exception as exc:
        logger.exception("dynamodb_put_img_record_failed")
        raise InternalError("An internal error occurred.") from exc

    return json_response(
        201,
        {
            "key": key,
            "timestamp": snapshot_ts,
            "camera_id": camera_id,
            "sha256": sha256_hex,
            "size_bytes": len(body),
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
