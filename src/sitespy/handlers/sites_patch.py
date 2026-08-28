"""Sites PATCH handler for SiteSpy — PATCH /v1/sites/{site_id}.

Updates site configuration. Supports:
- working_hours: days-of-week + HH:MM window classifying snapshot retention
- latitude: site latitude (-90 to 90)
- longitude: site longitude (-180 to 180)
- timezone: IANA timezone identifier (e.g. 'Europe/London')

The legacy `ingest_hours` field is rejected with HTTP 400 (Req 1.4); any
attempt to configure an out-of-hours TTL is rejected with HTTP 400 (Req 7.6).

Accessible to super admins and tenant admins only.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit

from sitespy import data, retention
from sitespy.errors import ApiError, BadRequest, Forbidden, InternalError, NotFound
from sitespy.http import error_response, json_response, unhandled_error_response
from sitespy.sandbox import sandbox_visibility_guard
from sitespy.validation import validate_latitude, validate_longitude, validate_timezone

# ---------------------------------------------------------------------------
# Powertools setup
# ---------------------------------------------------------------------------

logger = Logger(service="sitespy")
metrics = Metrics(namespace="SiteSpy", service="sitespy")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_ROUTE = "PATCH /v1/sites/{site_id}"

_GROUP_SUPER_ADMINS = "SuperAdmins"
_GROUP_TENANT_ADMINS = "TenantAdmins"

# HH:MM format validation
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

# Valid working-hours day-of-week members (exact lowercase, Monday-based).
_VALID_DAYS = retention.DAYS

# Out-of-hours TTL is a fixed v1 default and is explicitly not configurable
# (Req 7.6). Any attempt to set one of these fields is rejected with 400.
_FORBIDDEN_TTL_FIELDS = frozenset(
    {
        "out_of_hours_ttl",
        "out_of_hours_ttl_seconds",
        "ooh_ttl",
        "ooh_ttl_seconds",
        "ttl",
        "ttl_seconds",
        "retention_ttl",
    }
)


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for PATCH /v1/sites/{site_id}."""
    start_ms = time.monotonic() * 1000
    correlation_id = _resolve_correlation_id(event)

    try:
        result = _handle(event, correlation_id)
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_metric(name="PatchSiteSuccess", unit=MetricUnit.Count, value=1)
        logger.info(
            "patch_site_success",
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
        metrics.add_metric(name="PatchSiteFailure", unit=MetricUnit.Count, value=1)

        logger.warning(
            "patch_site_failure",
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
        metrics.add_metric(name="PatchSiteFailure", unit=MetricUnit.Count, value=1)

        logger.exception(
            "patch_site_unhandled_error",
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
    """Core logic for PATCH /v1/sites/{site_id} — super admin or tenant admin only."""
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

    # --- Extract site_id from path ---
    path_params = event.get("pathParameters") or {}
    site_id = (path_params.get("site_id") or "").strip()

    if not site_id:
        raise BadRequest("Missing required path parameter: site_id.")

    # --- Verify site exists ---
    site_item = _fetch_site_or_404(tenant_id, site_id)

    # Tenant admin: verify site belongs to their tenant
    if role == "tenant_admin" and caller_tenant_id != tenant_id:
        raise Forbidden()

    # --- Parse JSON body ---
    body = _parse_body(event)

    if not body:
        raise BadRequest("Request body must contain at least one field to update.")

    updates: dict[str, Any] = {}

    # --- Reject the legacy ingest_hours field (Req 1.4) ---
    if "ingest_hours" in body:
        raise BadRequest(
            "The 'ingest_hours' field is no longer supported; use 'working_hours' instead."
        )

    # --- Reject any attempt to configure the out-of-hours TTL (Req 7.6) ---
    for field in body:
        if field in _FORBIDDEN_TTL_FIELDS:
            raise BadRequest(
                "The out-of-hours retention TTL is fixed and is not configurable."
            )

    # --- Process working_hours (Req 1.1, 1.2, 3.1–3.7) ---
    if "working_hours" in body:
        updates["working_hours"] = _validate_working_hours(body["working_hours"])

    # --- Process latitude ---
    if "latitude" in body:
        lat = body["latitude"]
        if lat is None:
            raise BadRequest("latitude cannot be null.")
        if not isinstance(lat, (int, float)) or isinstance(lat, bool):
            raise BadRequest("latitude must be a number.")
        if not validate_latitude(float(lat)):
            raise BadRequest("latitude must be between -90 and 90.")
        updates["latitude"] = float(lat)

    # --- Process longitude ---
    if "longitude" in body:
        lon = body["longitude"]
        if lon is None:
            raise BadRequest("longitude cannot be null.")
        if not isinstance(lon, (int, float)) or isinstance(lon, bool):
            raise BadRequest("longitude must be a number.")
        if not validate_longitude(float(lon)):
            raise BadRequest("longitude must be between -180 and 180.")
        updates["longitude"] = float(lon)

    # --- Process timezone ---
    if "timezone" in body:
        tz = body["timezone"]
        if tz is None:
            raise BadRequest("timezone cannot be null.")
        if not isinstance(tz, str) or not tz.strip():
            raise BadRequest("timezone must be a non-empty string.")
        if not validate_timezone(tz.strip()):
            raise BadRequest("timezone must be a valid IANA timezone (e.g. 'Europe/London').")
        updates["timezone"] = tz.strip()

    if not updates:
        raise BadRequest("Request body must contain at least one supported field to update.")

    # --- Write update to DynamoDB ---
    try:
        data.update_site(
            tenant_id=tenant_id,
            site_id=site_id,
            updates=updates,
        )
    except Exception as exc:
        logger.exception("dynamodb_update_site_failed")
        raise InternalError() from exc

    # --- Build response ---
    response_body: dict[str, Any] = {
        "site_id": site_id,
        "tenant_id": tenant_id,
    }

    for key in ("working_hours", "latitude", "longitude", "timezone"):
        if key in updates:
            response_body[key] = updates[key]

    return json_response(200, response_body, correlation_id)


# ---------------------------------------------------------------------------
# working_hours validation
# ---------------------------------------------------------------------------


def _validate_working_hours(working_hours: Any) -> dict[str, Any] | None:
    """Validate a working_hours payload, returning the value to persist.

    Returns ``None`` when the caller passes ``working_hours: null`` (signals a
    REMOVE of the attribute — Req 1.2). Otherwise returns a dict
    ``{"days": [...], "start": "HH:MM", "end": "HH:MM"}`` ready to persist,
    with ``days`` defaulted to all seven days when omitted (Req 3.7).

    Raises BadRequest on any validation failure so the stored record is left
    unchanged (Req 3.3, 3.4, 3.6).
    """
    # working_hours: null removes the attribute (Req 1.2).
    if working_hours is None:
        return None

    if not isinstance(working_hours, dict):
        raise BadRequest(
            "working_hours must be an object with 'start' and 'end' fields, "
            "or null to clear."
        )

    start = working_hours.get("start")
    end = working_hours.get("end")

    # start/end are required (Req 3.1, 3.3).
    if start is None or end is None:
        raise BadRequest(
            "working_hours must contain both 'start' and 'end' fields (HH:MM format)."
        )

    # start/end must be valid HH:MM in 00:00–23:59 (Req 3.2, 3.4).
    if not isinstance(start, str) or not _TIME_RE.match(start):
        raise BadRequest(
            "working_hours.start must be in HH:MM format (00:00–23:59)."
        )

    if not isinstance(end, str) or not _TIME_RE.match(end):
        raise BadRequest(
            "working_hours.end must be in HH:MM format (00:00–23:59)."
        )

    days = _validate_days(working_hours.get("days"))

    return {"days": days, "start": start, "end": end}


def _validate_days(days: Any) -> list[str]:
    """Validate the optional working_hours.days list (Req 3.5, 3.6, 3.7).

    Returns the resolved list of days: the provided list when valid, or all
    seven days when ``days`` is omitted. Raises BadRequest when the provided
    list is empty, exceeds 7 entries, contains a duplicate, an unrecognized
    value, or an entry whose case does not exactly match a lowercase member.
    """
    # Omitted days defaults to all seven days (Req 3.7).
    if days is None:
        return list(_VALID_DAYS)

    if not isinstance(days, list):
        raise BadRequest(
            "working_hours.days must be a list of 1–7 entries drawn from "
            "{mon, tue, wed, thu, fri, sat, sun}."
        )

    # 1–7 entries (Req 3.5, 3.6).
    if len(days) < 1 or len(days) > 7:
        raise BadRequest(
            "working_hours.days must contain between 1 and 7 entries."
        )

    seen: set[str] = set()
    for day in days:
        # Exact-lowercase membership; reject non-strings and wrong case (Req 3.6).
        if not isinstance(day, str) or day not in _VALID_DAYS:
            raise BadRequest(
                "working_hours.days entries must be exact-lowercase members of "
                "{mon, tue, wed, thu, fri, sat, sun}."
            )
        if day in seen:
            raise BadRequest("working_hours.days must not contain duplicate entries.")
        seen.add(day)

    return list(days)


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
