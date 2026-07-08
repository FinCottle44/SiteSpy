"""Timelapse job listing handler for SiteSpy — GET /v1/timelapse-jobs.

Returns a paginated, filterable list of Timelapse_Jobs for the caller's
resolved tenant so the frontend can render an "all renders" view (tenant-wide)
and a "renders for this camera" view (site + camera scoped). Results can be
narrowed by ``site_id`` / ``camera_id`` / ``status``, are returned newest-first
(``created_at`` descending, ``job_id`` descending tie-break), and page through
an opaque base64 cursor.

The handler queries the DynamoDB base table by partition key
(``PK = TENANT#<tenant_id>`` with ``begins_with(SK, "JOB#")`` — no GSI), applies
``site_id`` / ``camera_id`` / ``status`` as post-query AND-filters, applies the
``user`` site-access scope, sorts and slices in the handler, and mints download
fields for ``complete`` jobs via the shared ``build_download_fields`` helper.

Mirrors the handler shape of ``timelapse_get.py`` / ``snapshots.py``: Powertools
``Logger`` / ``Metrics``, correlation-id resolution, a thin ``handler`` wrapper
around ``_handle``, and the same auth helpers (``_extract_claims`` /
``_resolve_caller`` / ``_check_access``) plus ``sandbox_visibility_guard``.

Requirements validated: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 2.1, 2.2, 2.3, 2.4, 2.5,
2.6, 2.7, 2.8, 2.9, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 4.1, 4.2, 4.3, 4.4, 4.5,
4.6, 4.7, 4.8, 5.1, 5.2, 5.3, 6.3, 6.5, 6.6, 8.1, 8.2, 8.4, 8.5
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
import uuid
from typing import Any, Mapping

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit

from sitespy import data, storage
from sitespy.errors import ApiError, BadRequest, Forbidden, InternalError
from sitespy.http import error_response, json_response, unhandled_error_response
from sitespy.sandbox import sandbox_visibility_guard
from sitespy.timelapse import (
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_PROCESSING,
    STATUS_QUEUED,
)
from sitespy.timelapse_download import build_download_fields

# ---------------------------------------------------------------------------
# Powertools setup
# ---------------------------------------------------------------------------

logger = Logger(service="sitespy")
metrics = Metrics(namespace="SiteSpy", service="sitespy")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_ROUTE = "GET /v1/timelapse-jobs"

_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100
_VALID_STATUSES = (STATUS_QUEUED, STATUS_PROCESSING, STATUS_COMPLETE, STATUS_FAILED)

_TENANT_PK_PREFIX = "TENANT#"

# Cognito group names
_GROUP_SUPER_ADMINS = "SuperAdmins"
_GROUP_TENANT_ADMINS = "TenantAdmins"


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for GET /v1/timelapse-jobs."""
    start_ms = time.monotonic() * 1000
    correlation_id = _resolve_correlation_id(event)

    tenant_id = "unknown"

    try:
        result = _handle(event, correlation_id)
        status_code = result["statusCode"]
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_metric(name="ListTimelapseJobsSuccess", unit=MetricUnit.Count, value=1)

        logger.info(
            "list_timelapse_jobs_success",
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

        metrics.add_dimension(name="tenant_id", value=tenant_id)
        metrics.add_dimension(name="status_code", value=str(exc.status_code))
        metrics.add_metric(name="ListTimelapseJobsFailure", unit=MetricUnit.Count, value=1)

        logger.warning(
            "list_timelapse_jobs_failure",
            extra={
                "correlation_id": correlation_id,
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

        metrics.add_dimension(name="tenant_id", value=tenant_id)
        metrics.add_dimension(name="status_code", value="500")
        metrics.add_metric(name="ListTimelapseJobsFailure", unit=MetricUnit.Count, value=1)

        logger.exception(
            "list_timelapse_jobs_unhandled_error",
            extra={
                "correlation_id": correlation_id,
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


def _handle(event: dict[str, Any], correlation_id: str) -> dict[str, Any]:
    """Core logic for GET /v1/timelapse-jobs — raises ApiError on failure."""
    query_params = event.get("queryStringParameters") or {}

    # --- 1. Parse + validate query params BEFORE any query (Req 8.4) ---
    filters = _parse_filters(query_params)
    limit = _parse_limit(query_params)
    start_key = _parse_cursor(query_params)

    # --- 2. Extract claims and resolve caller ---
    claims = _extract_claims(event)
    role, caller_tenant_id, site_access = _resolve_caller(claims)

    # --- 3. Resolve tenant ---
    if role == "super_admin":
        tenant_id_param = (query_params.get("tenant_id") or "").strip()
        if not tenant_id_param:
            raise BadRequest("Super admins must supply tenant_id as a query parameter.")
        tenant_id = tenant_id_param
    else:
        if not caller_tenant_id:
            raise Forbidden()
        tenant_id = caller_tenant_id

    # --- 4. Sandbox visibility guard ---
    sandbox_visibility_guard(tenant_id, role)

    # --- 5. User site-access scoping (Req 4.4, 4.6, 4.7) ---
    site_access_scope: set[str] | None = None
    if role == "user":
        # A user with no authorized sites can never see any job.
        if not site_access:
            return json_response(200, {"jobs": [], "next_cursor": None}, correlation_id)
        # A supplied site_id outside the caller's access yields an empty page.
        supplied_site_id = filters.get("site_id")
        if supplied_site_id is not None and supplied_site_id not in site_access:
            return json_response(200, {"jobs": [], "next_cursor": None}, correlation_id)
        site_access_scope = set(site_access)

    # --- 6. Page + filter + sort ---
    page_jobs, next_key = _collect_page(
        tenant_id, filters, site_access_scope, limit, start_key
    )

    # --- 7. Build each entry (+ shared download fields for complete jobs) ---
    jobs = [_build_job_entry(job) for job in page_jobs]

    # --- 8. Encode next_cursor ---
    next_cursor = _encode_cursor(next_key)

    # --- 9. Return 200 ---
    return json_response(200, {"jobs": jobs, "next_cursor": next_cursor}, correlation_id)


# ---------------------------------------------------------------------------
# Query-parameter parsing / validation
# ---------------------------------------------------------------------------


def _parse_filters(query_params: Mapping[str, Any]) -> dict[str, str]:
    """Validate and collect the supplied site_id / camera_id / status filters.

    Raises BadRequest when a supplied value is empty/whitespace, when ``status``
    is not a valid Lifecycle_Status, or when ``camera_id`` is supplied without
    ``site_id``. Returns a dict of the supplied (non-blank) filter values.
    """
    filters: dict[str, str] = {}

    for name in ("site_id", "camera_id", "status"):
        raw = query_params.get(name)
        if raw is None:
            continue
        if raw.strip() == "":
            raise BadRequest(f"Query parameter '{name}' must not be empty or blank.")
        filters[name] = raw

    if "status" in filters and filters["status"] not in _VALID_STATUSES:
        allowed = ", ".join(_VALID_STATUSES)
        raise BadRequest(f"Query parameter 'status' must be one of: {allowed}.")

    if "camera_id" in filters and "site_id" not in filters:
        raise BadRequest("Query parameter 'camera_id' requires 'site_id'.")

    return filters


def _parse_limit(query_params: Mapping[str, Any]) -> int:
    """Resolve the effective page size.

    Defaults to 20 when omitted / empty / whitespace. Otherwise must parse to an
    integer in [1, 100] inclusive, else BadRequest.
    """
    raw = query_params.get("limit")
    if raw is None or raw.strip() == "":
        return _DEFAULT_LIMIT

    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        raise BadRequest(
            f"Query parameter 'limit' must be an integer between 1 and {_MAX_LIMIT} inclusive."
        ) from None

    if value < 1 or value > _MAX_LIMIT:
        raise BadRequest(
            f"Query parameter 'limit' must be an integer between 1 and {_MAX_LIMIT} inclusive."
        )
    return value


def _parse_cursor(query_params: Mapping[str, Any]) -> dict | None:
    """Decode an opaque base64 cursor into an ExclusiveStartKey dict.

    Returns None when no cursor is supplied. Raises BadRequest when the cursor
    is empty, is not valid base64, or does not decode into a dict (Req 3.7).
    """
    raw = query_params.get("cursor")
    if raw is None:
        return None
    if raw.strip() == "":
        raise BadRequest("Query parameter 'cursor' is invalid.")

    try:
        decoded = base64.b64decode(raw, validate=True)
        obj = json.loads(decoded)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        raise BadRequest("Query parameter 'cursor' is invalid.") from None

    if not isinstance(obj, dict):
        raise BadRequest("Query parameter 'cursor' is invalid.")
    return obj


def _encode_cursor(key: dict | None) -> str | None:
    """Encode a DynamoDB LastEvaluatedKey as an opaque base64 token, or null."""
    if not key:
        return None
    return base64.b64encode(json.dumps(key).encode("utf-8")).decode("ascii")


# ---------------------------------------------------------------------------
# Paging / filtering / sorting
# ---------------------------------------------------------------------------


def _collect_page(
    tenant_id: str,
    filters: Mapping[str, str],
    site_access_scope: set[str] | None,
    limit: int,
    start_key: dict | None,
) -> tuple[list[Mapping[str, Any]], dict | None]:
    """Page ``data.list_timelapse_jobs`` until ``limit`` post-filtered jobs are
    collected or the partition is exhausted.

    Applies the supplied ``site_id`` / ``camera_id`` / ``status`` AND-filters
    (exact, case-sensitive) and, for a ``user`` caller, the ``site_access``
    scope. Sorts the accumulated matches by ``created_at`` descending then
    ``job_id`` descending, slices to ``limit``, and returns the DynamoDB
    ``LastEvaluatedKey`` to resume from (or None when the partition is
    exhausted).

    A DynamoDB failure surfaces as InternalError (500).
    """
    collected: list[Mapping[str, Any]] = []
    resume_key = start_key
    last_key: dict | None = None

    while True:
        try:
            items, last_key = data.list_timelapse_jobs(tenant_id, resume_key, limit)
        except Exception as exc:
            logger.exception("dynamodb_list_timelapse_jobs_failed")
            raise InternalError() from exc

        for item in items:
            if _matches(item, filters, site_access_scope):
                collected.append(item)

        resume_key = last_key
        if len(collected) >= limit or not last_key:
            break

    collected.sort(
        key=lambda job: (_s(job, "created_at"), _s(job, "job_id")),
        reverse=True,
    )
    page = collected[:limit]
    next_key = last_key if last_key else None
    return page, next_key


def _matches(
    job: Mapping[str, Any],
    filters: Mapping[str, str],
    site_access_scope: set[str] | None,
) -> bool:
    """Return True when a job satisfies every supplied filter and the caller's
    site-access scope (exact, case-sensitive AND combination)."""
    for name, expected in filters.items():
        if _s(job, name) != expected:
            return False
    if site_access_scope is not None and _s(job, "site_id") not in site_access_scope:
        return False
    return True


# ---------------------------------------------------------------------------
# Response builder
# ---------------------------------------------------------------------------


def _build_job_entry(job: Mapping[str, Any]) -> dict[str, Any]:
    """Unmarshal a JOB# item into the response entry.

    Maps the stored ``start_ts`` / ``end_ts`` attributes to the ``start`` /
    ``end`` response fields. ``completed_at`` is null unless the job is
    ``complete``; ``requested_by`` is null when no value was captured. For
    ``complete`` jobs the shared download fields are merged in.
    """
    status = _s(job, "status")

    entry: dict[str, Any] = {
        "job_id": _s(job, "job_id"),
        "site_id": _s(job, "site_id"),
        "camera_id": _s(job, "camera_id"),
        "start": _s(job, "start_ts"),
        "end": _s(job, "end_ts"),
        "length_seconds": _n(job, "length_seconds"),
        "status": status,
        "created_at": _s(job, "created_at"),
        "completed_at": (_s(job, "completed_at") or None) if status == STATUS_COMPLETE else None,
        "requested_by": _s(job, "requested_by") or None,
    }

    if status == STATUS_COMPLETE:
        artifact_key = _resolve_artifact_key(job)
        entry.update(build_download_fields(status, artifact_key))

    return entry


def _resolve_artifact_key(job: Mapping[str, Any]) -> str:
    """Return the job's stored artifact_key, or derive it when absent."""
    stored = _s(job, "artifact_key")
    if stored:
        return stored

    pk = _s(job, "PK")
    tenant_id = pk[len(_TENANT_PK_PREFIX):] if pk.startswith(_TENANT_PK_PREFIX) else ""
    return storage.build_timelapse_key(
        tenant_id,
        _s(job, "site_id"),
        _s(job, "camera_id"),
        _s(job, "job_id"),
    )


def _s(job: Mapping[str, Any], name: str) -> str:
    """Read a DynamoDB string attribute, defaulting to an empty string."""
    return job.get(name, {}).get("S", "") or ""


def _n(job: Mapping[str, Any], name: str) -> int:
    """Read a DynamoDB numeric attribute as an int, defaulting to 0."""
    raw = job.get(name, {}).get("N")
    if raw is None:
        return 0
    return int(raw)


# ---------------------------------------------------------------------------
# Auth helpers (mirrors snapshots.py / timelapse_get.py)
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
# Correlation ID helper
# ---------------------------------------------------------------------------


def _resolve_correlation_id(event: dict[str, Any]) -> str:
    """Return the X-Correlation-Id header if valid, else a fresh UUID v4."""
    headers = event.get("headers") or {}
    value = headers.get("X-Correlation-Id") or headers.get("x-correlation-id") or ""
    if _CORRELATION_ID_RE.match(value):
        return value
    return str(uuid.uuid4())
