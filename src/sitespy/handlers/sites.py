"""Sites handler for SiteSpy — GET /v1/sites/{site_id} and GET /v1/sites.

GET /v1/sites/{site_id}: Returns site metadata and the list of cameras.
GET /v1/sites: Returns all sites accessible to the caller.

Access is validated against the caller's JWT claims (Cognito authorizer).

Requirements validated: 3.1
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Mapping
from typing import Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit

from sitespy import data
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
_ROUTE = "GET /v1/sites/{site_id}"

# Cognito group names
_GROUP_SUPER_ADMINS = "SuperAdmins"
_GROUP_TENANT_ADMINS = "TenantAdmins"


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for GET /v1/sites/{site_id}."""
    start_ms = time.monotonic() * 1000
    correlation_id = _resolve_correlation_id(event)

    site_id = "unknown"
    tenant_id = "unknown"

    try:
        result = _handle(event, correlation_id)
        status_code = result["statusCode"]
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        path_params = event.get("pathParameters") or {}
        site_id = path_params.get("site_id", "unknown")

        metrics.add_dimension(name="site_id", value=site_id)
        metrics.add_metric(name="GetSiteSuccess", unit=MetricUnit.Count, value=1)

        logger.info(
            "get_site_success",
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
        metrics.add_metric(name="GetSiteFailure", unit=MetricUnit.Count, value=1)

        logger.warning(
            "get_site_failure",
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
        metrics.add_metric(name="GetSiteFailure", unit=MetricUnit.Count, value=1)

        logger.exception(
            "get_site_unhandled_error",
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


def _handle(event: dict[str, Any], correlation_id: str) -> dict[str, Any]:
    """Straight-line sites logic — raises ApiError on any user-visible failure."""
    # --- Extract and validate site_id from path ---
    path_params = event.get("pathParameters") or {}
    site_id = (path_params.get("site_id") or "").strip()

    if not site_id:
        raise BadRequest("Missing required path parameter: site_id.")

    # --- Extract JWT claims from API Gateway authorizer context ---
    claims = _extract_claims(event)
    role, caller_tenant_id, site_access = _resolve_caller(claims)

    # --- Fetch site record from DynamoDB ---
    # For super admins we don't know the tenant_id upfront; we need to find
    # the site. Super admins can access any tenant, so we use the site's own
    # tenant_id once we've fetched it.
    #
    # For tenant admins and users, we know the tenant_id from the token.
    # We use it directly to build the DynamoDB key.

    if role == "super_admin":
        # Super admin: prefer tenant_id from query param, fall back to token claim.
        query_params = event.get("queryStringParameters") or {}
        tenant_id_param = (query_params.get("tenant_id") or "").strip()

        if not tenant_id_param:
            # Fall back to the token's custom:tenant_id if present
            if caller_tenant_id:
                tenant_id_param = caller_tenant_id
            else:
                raise BadRequest(
                    "Super admins must supply tenant_id as a query parameter."
                )

        site_item = _fetch_site_or_404(tenant_id_param, site_id)
        site_tenant_id = tenant_id_param

    else:
        # Tenant admin or user: tenant_id comes from the token.
        if not caller_tenant_id:
            raise Forbidden()

        site_item = _fetch_site_or_404(caller_tenant_id, site_id)
        site_tenant_id = caller_tenant_id

    # --- Authorise the caller ---
    _check_access(role, caller_tenant_id, site_tenant_id, site_id, site_access)

    # --- Fetch cameras ---
    try:
        camera_items = data.get_cameras_for_site(site_tenant_id, site_id)
    except Exception as exc:
        logger.exception("dynamodb_get_cameras_failed")
        raise InternalError() from exc

    # --- Build response ---
    cameras = [_marshal_camera(item, site_id) for item in camera_items]

    body: dict[str, Any] = {
        "site_id": site_id,
        "site_name": site_item.get("site_name", {}).get("S", ""),
        "tenant_id": site_tenant_id,
        "latitude": _parse_float(site_item.get("latitude")),
        "longitude": _parse_float(site_item.get("longitude")),
        "timezone": site_item.get("timezone", {}).get("S", "Europe/London"),
        "cameras": cameras,
    }

    # Include ingest_hours if configured
    ingest_hours_attr = site_item.get("ingest_hours")
    if ingest_hours_attr is not None:
        m = ingest_hours_attr.get("M")
        if m:
            start = m.get("start", {}).get("S")
            end = m.get("end", {}).get("S")
            if start and end:
                body["ingest_hours"] = {"start": start, "end": end}
            else:
                body["ingest_hours"] = None
        else:
            body["ingest_hours"] = None
    else:
        body["ingest_hours"] = None

    return json_response(200, body, correlation_id)


# ---------------------------------------------------------------------------
# Auth helpers
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
    # cognito:groups may be a space-separated string or a list depending on
    # how API Gateway serialises the claim.
    raw_groups = claims.get("cognito:groups") or ""
    if isinstance(raw_groups, list):
        groups: list[str] = raw_groups
    else:
        groups = [g.strip() for g in str(raw_groups).split(",") if g.strip()]
        # Also handle space-separated (Cognito sometimes uses spaces)
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
    - super_admin: always allowed (already resolved tenant_id via query param)
    - tenant_admin: site's tenant_id must match token's tenant_id
    - user: site's tenant_id must match AND site_id must be in site_access
    """
    if role == "super_admin":
        return  # Super admins are allowed on any site

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
# DynamoDB helpers
# ---------------------------------------------------------------------------


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


def _marshal_camera(item: Mapping[str, Any], site_id: str) -> dict[str, Any]:
    """Convert a DynamoDB camera item to the API response shape."""
    sk: str = item.get("SK", {}).get("S", "")
    # SK format: SITE#<site_id>#CAM#<camera_id>
    camera_id = sk.removeprefix(f"SITE#{site_id}#CAM#")

    return {
        "camera_id": camera_id,
        "camera_name": item.get("camera_name", {}).get("S", ""),
        "camera_model": item.get("camera_model", {}).get("S", ""),
    }


def _parse_float(attr: Any) -> float | None:
    """Parse a DynamoDB Number attribute to a Python float."""
    if attr is None:
        return None
    n_val = attr.get("N")
    if n_val is None:
        return None
    try:
        return float(n_val)
    except (TypeError, ValueError):
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


# ---------------------------------------------------------------------------
# Lambda handler — GET /v1/sites (list all sites)
# ---------------------------------------------------------------------------

_ROUTE_LIST = "GET /v1/sites"


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler_list(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for GET /v1/sites."""
    start_ms = time.monotonic() * 1000
    correlation_id = _resolve_correlation_id(event)

    try:
        result = _handle_list(event, correlation_id)
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_metric(name="ListSitesSuccess", unit=MetricUnit.Count, value=1)
        logger.info(
            "list_sites_success",
            extra={
                "correlation_id": correlation_id,
                "route": _ROUTE_LIST,
                "status_code": result["statusCode"],
                "latency_ms": latency_ms,
            },
        )
        return result

    except ApiError as exc:
        latency_ms = int(time.monotonic() * 1000 - start_ms)
        metrics.add_metric(name="ListSitesFailure", unit=MetricUnit.Count, value=1)
        logger.warning(
            "list_sites_failure",
            extra={
                "correlation_id": correlation_id,
                "route": _ROUTE_LIST,
                "status_code": exc.status_code,
                "latency_ms": latency_ms,
                "error": exc.error_key,
            },
        )
        return error_response(exc, correlation_id)

    except Exception:
        latency_ms = int(time.monotonic() * 1000 - start_ms)
        metrics.add_metric(name="ListSitesFailure", unit=MetricUnit.Count, value=1)
        logger.exception(
            "list_sites_unhandled_error",
            extra={
                "correlation_id": correlation_id,
                "route": _ROUTE_LIST,
                "status_code": 500,
                "latency_ms": latency_ms,
            },
        )
        return unhandled_error_response(correlation_id)


# ---------------------------------------------------------------------------
# Internal handler — GET /v1/sites
# ---------------------------------------------------------------------------


def _handle_list(event: dict[str, Any], correlation_id: str) -> dict[str, Any]:
    """Core logic for GET /v1/sites — returns all sites accessible to the caller."""
    claims = _extract_claims(event)
    role, caller_tenant_id, site_access = _resolve_caller(claims)

    # --- Resolve tenant_id ---
    if role == "super_admin":
        query_params = event.get("queryStringParameters") or {}
        tenant_id_param = (query_params.get("tenant_id") or "").strip()
        if not tenant_id_param:
            if caller_tenant_id:
                tenant_id_param = caller_tenant_id
            else:
                raise BadRequest(
                    "Super admins must supply tenant_id as a query parameter."
                )
        tenant_id = tenant_id_param
    else:
        if not caller_tenant_id:
            raise Forbidden()
        tenant_id = caller_tenant_id

    # --- Fetch all sites for the tenant ---
    try:
        site_items = data.list_sites_for_tenant(tenant_id)
    except Exception as exc:
        logger.exception("dynamodb_list_sites_failed")
        raise InternalError() from exc

    # --- Filter by site_access for regular users ---
    sites = []
    for item in site_items:
        sk = item.get("SK", {}).get("S", "")
        site_id = sk.removeprefix("SITE#")

        # Regular users can only see sites in their site_access list
        if role == "user" and site_id not in site_access:
            continue

        site_entry: dict[str, Any] = {
            "site_id": site_id,
            "site_name": item.get("site_name", {}).get("S", ""),
            "tenant_id": tenant_id,
            "latitude": _parse_float(item.get("latitude")),
            "longitude": _parse_float(item.get("longitude")),
            "timezone": item.get("timezone", {}).get("S", "Europe/London"),
        }

        # Include ingest_hours if configured
        ingest_hours_attr = item.get("ingest_hours")
        if ingest_hours_attr is not None:
            m = ingest_hours_attr.get("M")
            if m:
                start = m.get("start", {}).get("S")
                end = m.get("end", {}).get("S")
                if start and end:
                    site_entry["ingest_hours"] = {"start": start, "end": end}
                else:
                    site_entry["ingest_hours"] = None
            else:
                site_entry["ingest_hours"] = None
        else:
            site_entry["ingest_hours"] = None

        sites.append(site_entry)

    return json_response(200, {"sites": sites}, correlation_id)
