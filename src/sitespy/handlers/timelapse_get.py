"""Timelapse status/retrieve handler for SiteSpy — GET /v1/timelapse-jobs/{job_id}.

Returns the current status of a Timelapse_Job. When the job is ``complete`` the
response carries a freshly-minted presigned download URL for the rendered MP4
Artifact (never a stored URL, so expiry is never a stored-state problem). When
the job is ``failed`` the response carries the failure reason. For ``queued`` and
``processing`` the response carries only the status.

Authorization mirrors the snapshots endpoints: the caller must be authorized for
the job's tenant/site. On an authorization failure the handler raises NotFound
(404) rather than Forbidden so that the existence of another tenant's job is not
leaked.

Requirements validated: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit

from sitespy import data
from sitespy.errors import ApiError, BadRequest, Forbidden, InternalError, NotFound
from sitespy.http import error_response, json_response, unhandled_error_response
from sitespy.sandbox import sandbox_visibility_guard
from sitespy.timelapse import (
    STATUS_COMPLETE,
    STATUS_FAILED,
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
_ROUTE = "GET /v1/timelapse-jobs/{job_id}"

# Cognito group names
_GROUP_SUPER_ADMINS = "SuperAdmins"
_GROUP_TENANT_ADMINS = "TenantAdmins"


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for GET /v1/timelapse-jobs/{job_id}."""
    start_ms = time.monotonic() * 1000
    correlation_id = _resolve_correlation_id(event)

    job_id = "unknown"
    tenant_id = "unknown"

    try:
        result = _handle(event, correlation_id)
        status_code = result["statusCode"]
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        path_params = event.get("pathParameters") or {}
        job_id = path_params.get("job_id", "unknown")

        metrics.add_dimension(name="job_id", value=job_id)
        metrics.add_metric(name="GetTimelapseJobSuccess", unit=MetricUnit.Count, value=1)

        logger.info(
            "get_timelapse_job_success",
            extra={
                "correlation_id": correlation_id,
                "job_id": job_id,
                "route": _ROUTE,
                "status_code": status_code,
                "latency_ms": latency_ms,
            },
        )
        return result

    except ApiError as exc:
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_dimension(name="job_id", value=job_id)
        metrics.add_dimension(name="tenant_id", value=tenant_id)
        metrics.add_dimension(name="status_code", value=str(exc.status_code))
        metrics.add_metric(name="GetTimelapseJobFailure", unit=MetricUnit.Count, value=1)

        logger.warning(
            "get_timelapse_job_failure",
            extra={
                "correlation_id": correlation_id,
                "job_id": job_id,
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

        metrics.add_dimension(name="job_id", value=job_id)
        metrics.add_dimension(name="tenant_id", value=tenant_id)
        metrics.add_dimension(name="status_code", value="500")
        metrics.add_metric(name="GetTimelapseJobFailure", unit=MetricUnit.Count, value=1)

        logger.exception(
            "get_timelapse_job_unhandled_error",
            extra={
                "correlation_id": correlation_id,
                "job_id": job_id,
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
    """Core logic for GET /v1/timelapse-jobs/{job_id} — raises ApiError on failure."""
    query_params = event.get("queryStringParameters") or {}
    path_params = event.get("pathParameters") or {}

    # --- Extract job_id from path ---
    job_id = (path_params.get("job_id") or "").strip()
    if not job_id:
        raise BadRequest("Missing required path parameter: job_id.")

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

    # --- Sandbox visibility guard ---
    sandbox_visibility_guard(tenant_id, role)

    # --- Fetch the job record ---
    try:
        job = data.get_timelapse_job(tenant_id, job_id)
    except Exception as exc:
        logger.exception("dynamodb_get_timelapse_job_failed")
        raise InternalError() from exc

    if job is None:
        raise NotFound()

    # --- Authorise the caller against the job's stored site ---
    # On failure raise NotFound (not Forbidden) so we do not leak the job's
    # existence to a caller who cannot access it.
    site_id = job.get("site_id", {}).get("S", "")
    try:
        _check_access(role, caller_tenant_id, tenant_id, site_id, site_access)
    except Forbidden:
        raise NotFound() from None

    # --- Build the response by status ---
    status = job.get("status", {}).get("S", "")
    body = _build_status_body(status, job)
    return json_response(200, body, correlation_id)


# ---------------------------------------------------------------------------
# Response builder
# ---------------------------------------------------------------------------


def _build_status_body(status: str, job: dict[str, Any]) -> dict[str, Any]:
    """Build the status response body for a job based on its lifecycle status.

    Every response exposes ``requested_by`` (null when no value was captured)
    and ``completed_at`` (null unless the job is ``complete``), mirroring the
    list endpoint (Requirement 6.4). The ``complete`` branch delegates the
    download-related fields to the shared ``build_download_fields`` helper so
    that this endpoint behaves identically to the list endpoint — checking
    Artifact existence before presigning and never emitting a broken link
    (Requirement 5.4).

    - queued / processing -> {"status", "requested_by", "completed_at"}
    - complete            -> base + shared download fields ({"download_url",
      "expires_in"} when the Artifact exists, else {"artifact_available": False})
    - failed              -> base + {"reason"}
    """
    requested_by = job.get("requested_by", {}).get("S") or None
    completed_at = (
        job.get("completed_at", {}).get("S") or None if status == STATUS_COMPLETE else None
    )

    body: dict[str, Any] = {
        "status": status,
        "requested_by": requested_by,
        "completed_at": completed_at,
    }

    if status == STATUS_COMPLETE:
        artifact_key = job.get("artifact_key", {}).get("S", "")
        body.update(build_download_fields(status, artifact_key))
    elif status == STATUS_FAILED:
        body["reason"] = job.get("failure_reason", {}).get("S", "")

    return body


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
# Correlation ID helper
# ---------------------------------------------------------------------------


def _resolve_correlation_id(event: dict[str, Any]) -> str:
    """Return the X-Correlation-Id header if valid, else a fresh UUID v4."""
    headers = event.get("headers") or {}
    value = headers.get("X-Correlation-Id") or headers.get("x-correlation-id") or ""
    if _CORRELATION_ID_RE.match(value):
        return value
    return str(uuid.uuid4())
