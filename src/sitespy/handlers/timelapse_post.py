"""Timelapse submit handler for SiteSpy — POST /v1/timelapse-jobs.

Validates a timelapse render request, verifies the caller is authorized for the
referenced site, confirms footage exists in the requested range, persists a
``queued`` Timelapse_Job record, and enqueues a message on the Job_Queue for the
render Worker. Returns 202 with the ``job_id`` the client uses to poll status.

Requirements validated: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 2.1, 2.2, 2.3, 2.4, 2.5,
                        2.6, 2.7, 2.8, 7.1
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

import boto3
import botocore.config
from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit

from sitespy import data
from sitespy.config import get_settings
from sitespy.errors import ApiError, BadRequest, Forbidden, InternalError, NotFound
from sitespy.http import error_response, json_response, unhandled_error_response
from sitespy.sandbox import sandbox_visibility_guard
from sitespy.timelapse import (
    DEFAULT_FPS,
    DEFAULT_LENGTH_SECONDS,
    STATUS_QUEUED,
)

# ---------------------------------------------------------------------------
# Powertools setup
# ---------------------------------------------------------------------------

logger = Logger(service="sitespy")
metrics = Metrics(namespace="SiteSpy", service="sitespy")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_ROUTE = "POST /v1/timelapse-jobs"

_BOTO_CONFIG = botocore.config.Config(retries={"mode": "standard", "max_attempts": 3})

# Cognito group names
_GROUP_SUPER_ADMINS = "SuperAdmins"
_GROUP_TENANT_ADMINS = "TenantAdmins"


# ---------------------------------------------------------------------------
# SQS client
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _sqs_client() -> Any:
    return boto3.client(
        "sqs",
        region_name=get_settings().aws_region,
        config=_BOTO_CONFIG,
    )


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for POST /v1/timelapse-jobs."""
    start_ms = time.monotonic() * 1000
    correlation_id = _resolve_correlation_id(event)

    try:
        result = _handle(event, correlation_id)
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_metric(name="PostTimelapseJobSuccess", unit=MetricUnit.Count, value=1)
        logger.info(
            "post_timelapse_job_success",
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
        metrics.add_metric(name="PostTimelapseJobFailure", unit=MetricUnit.Count, value=1)

        logger.warning(
            "post_timelapse_job_failure",
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
        metrics.add_metric(name="PostTimelapseJobFailure", unit=MetricUnit.Count, value=1)

        logger.exception(
            "post_timelapse_job_unhandled_error",
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
    """Core logic for POST /v1/timelapse-jobs — raises ApiError on failure."""
    settings = get_settings()

    # --- Extract JWT claims and resolve caller ---
    claims = _extract_claims(event)
    role, caller_tenant_id, site_access = _resolve_caller(claims)

    # --- Parse JSON body ---
    body = _parse_body(event)

    # --- Validate required string fields ---
    site_id = _require_non_empty_str(body, "site_id")
    camera_id = _require_non_empty_str(body, "camera_id")
    raw_start = _require_non_empty_str(body, "start")
    raw_end = _require_non_empty_str(body, "end")

    # --- Normalize and validate start/end timestamps ---
    start_ts = _normalize_timestamp(raw_start, is_start=True)
    end_ts = _normalize_timestamp(raw_end, is_start=False)

    if end_ts <= start_ts:
        raise BadRequest("end must be strictly after start.")

    # --- Validate optional length_seconds / fps ---
    length_seconds = _parse_bounded_int(
        body.get("length_seconds"),
        field="length_seconds",
        default=DEFAULT_LENGTH_SECONDS,
        maximum=settings.max_length_seconds,
    )
    fps = _parse_bounded_int(
        body.get("fps"),
        field="fps",
        default=DEFAULT_FPS,
        maximum=settings.max_fps,
    )

    # --- Resolve tenant_id ---
    query_params = event.get("queryStringParameters") or {}
    if role == "super_admin":
        tenant_id_param = (query_params.get("tenant_id") or "").strip()
        if not tenant_id_param:
            raise BadRequest("Super admins must supply tenant_id as a query parameter.")
        tenant_id = tenant_id_param
    else:
        if not caller_tenant_id:
            raise Forbidden()
        tenant_id = caller_tenant_id

    # --- Sandbox visibility guard ---
    sandbox_visibility_guard(tenant_id, role)

    # --- Verify site exists ---
    try:
        site_item = data.get_site(tenant_id, site_id)
    except Exception as exc:
        logger.exception("dynamodb_get_site_failed")
        raise InternalError() from exc

    if site_item is None:
        raise NotFound("Site not found.")

    # --- Authorise the caller ---
    _check_access(role, caller_tenant_id, tenant_id, site_id, site_access)

    # --- Verify footage exists in the requested range ---
    try:
        img_items, _ = data.list_img_records(
            tenant_id=tenant_id,
            site_id=site_id,
            camera_id=camera_id,
            from_ts=start_ts,
            to_ts=end_ts,
            limit=1,
        )
    except Exception as exc:
        logger.exception("dynamodb_list_img_records_failed")
        raise InternalError() from exc

    if not img_items:
        raise NotFound("No footage available to render in the requested range.")

    # --- Create the job record and enqueue the render message ---
    job_id = str(uuid.uuid4())
    now = datetime.now(tz=UTC)
    created_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    ttl = int(now.timestamp()) + settings.job_ttl_days * 86400

    # Capture the caller identity for accountability (Requirements 6.1, 6.7).
    # Prefer the JWT ``sub`` claim, fall back to ``email``, else store null.
    # A caller lacking both claims still submits successfully.
    requested_by = claims.get("sub") or claims.get("email") or None

    try:
        data.put_timelapse_job(
            tenant_id=tenant_id,
            job_id=job_id,
            site_id=site_id,
            camera_id=camera_id,
            start_ts=start_ts,
            end_ts=end_ts,
            length_seconds=length_seconds,
            fps=fps,
            status=STATUS_QUEUED,
            created_at=created_at,
            ttl=ttl,
            requested_by=requested_by,
        )
    except Exception as exc:
        logger.exception("dynamodb_put_timelapse_job_failed")
        raise InternalError() from exc

    payload = {
        "tenant_id": tenant_id,
        "site_id": site_id,
        "camera_id": camera_id,
        "job_id": job_id,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "length_seconds": length_seconds,
        "fps": fps,
    }

    try:
        _sqs_client().send_message(
            QueueUrl=settings.job_queue_url,
            MessageBody=json.dumps(payload),
        )
    except Exception as exc:
        logger.exception("sqs_send_message_failed")
        raise InternalError() from exc

    return json_response(202, {"job_id": job_id, "status": STATUS_QUEUED}, correlation_id)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _require_non_empty_str(body: dict[str, Any], field: str) -> str:
    """Return the stripped string value of a required field or raise BadRequest."""
    value = body.get(field)
    if value is None or not isinstance(value, str) or not value.strip():
        raise BadRequest(f"Missing required field: {field}.")
    return value.strip()


def _parse_bounded_int(
    raw: Any,
    *,
    field: str,
    default: int,
    maximum: int,
) -> int:
    """Parse an optional integer field, defaulting and range-checking it.

    - If ``raw`` is None, return ``default``.
    - Otherwise, ``raw`` must be an integer within ``[1, maximum]``.
    - Booleans are rejected (they are ints in Python but not valid here).
    """
    if raw is None:
        return default

    if isinstance(raw, bool) or not isinstance(raw, int):
        raise BadRequest(f"{field} must be an integer between 1 and {maximum}.")

    if raw < 1 or raw > maximum:
        raise BadRequest(f"{field} must be between 1 and {maximum}.")

    return raw


def _normalize_timestamp(raw: str, *, is_start: bool) -> str:
    """Normalize a raw ISO 8601 date or datetime to a full UTC timestamp string.

    - Date-only (YYYY-MM-DD): expand to start-of-day (T00:00:00Z) for
      ``is_start=True``, or end-of-day (T23:59:59Z) for ``is_start=False``.
    - Full datetime: parse and normalize to a ``...Z`` UTC string.

    Returns a string like "2025-06-15T14:00:00Z". Raises BadRequest on an
    invalid format.
    """
    # Date-only: YYYY-MM-DD
    if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
        try:
            datetime.strptime(raw, "%Y-%m-%d")
        except ValueError:
            raise BadRequest(
                f"Invalid date format: {raw!r}. Use ISO8601 (e.g. 2025-06-15 or 2025-06-15T14:00:00Z)."
            ) from None
        return f"{raw}T00:00:00Z" if is_start else f"{raw}T23:59:59Z"

    # Full datetime — parse and normalize
    try:
        normalized = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        raise BadRequest(
            f"Invalid datetime format: {raw!r}. Use ISO8601 (e.g. 2025-06-15 or 2025-06-15T14:00:00Z)."
        ) from None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Auth helpers (mirrors snapshots.py)
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
