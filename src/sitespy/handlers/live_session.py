"""Live session handler for SiteSpy — POST/GET/DELETE /v1/sites/{site_id}/cameras/{camera_id}/live-session.

POST   creates a new 10-minute live view session for a camera.
GET    polls the session status and returns the latest live image.
DELETE ends a session early.

Requirements validated: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6
"""

from __future__ import annotations

import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit

from sitespy import data, storage
from sitespy.errors import ApiError, BadRequest, Conflict, Forbidden, InternalError, NotFound
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

# Cognito group names
_GROUP_SUPER_ADMINS = "SuperAdmins"
_GROUP_TENANT_ADMINS = "TenantAdmins"


# ---------------------------------------------------------------------------
# Unified Lambda handler — dispatches by HTTP method (used when a single SAM
# function resource serves POST, GET, DELETE via separate API events).
# ---------------------------------------------------------------------------


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Dispatch entry point for LiveSessionFunction — routes by httpMethod."""
    method = (event.get("httpMethod") or "").upper()
    if method == "GET":
        return handler_get(event, context)
    if method == "DELETE":
        return handler_delete(event, context)
    # Default to POST (covers POST and any unrecognised method)
    return handler_post(event, context)


# ---------------------------------------------------------------------------
# Lambda handler — POST /v1/sites/{site_id}/cameras/{camera_id}/live-session
# ---------------------------------------------------------------------------


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler_post(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for POST live-session."""
    start_ms = time.monotonic() * 1000
    correlation_id = _resolve_correlation_id(event)

    try:
        result = _handle_post(event, correlation_id)
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_metric(name="LiveSessionPostSuccess", unit=MetricUnit.Count, value=1)
        logger.info(
            "live_session_post_success",
            extra={
                "correlation_id": correlation_id,
                "status_code": result["statusCode"],
                "latency_ms": latency_ms,
            },
        )
        return result

    except ApiError as exc:
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_dimension(name="status_code", value=str(exc.status_code))
        metrics.add_metric(name="LiveSessionPostFailure", unit=MetricUnit.Count, value=1)

        logger.warning(
            "live_session_post_failure",
            extra={
                "correlation_id": correlation_id,
                "status_code": exc.status_code,
                "latency_ms": latency_ms,
                "error": exc.error_key,
            },
        )
        return error_response(exc, correlation_id)

    except Exception:
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_dimension(name="status_code", value="500")
        metrics.add_metric(name="LiveSessionPostFailure", unit=MetricUnit.Count, value=1)

        logger.exception(
            "live_session_post_unhandled_error",
            extra={
                "correlation_id": correlation_id,
                "status_code": 500,
                "latency_ms": latency_ms,
            },
        )
        return unhandled_error_response(correlation_id)


# ---------------------------------------------------------------------------
# Lambda handler — GET /v1/sites/{site_id}/cameras/{camera_id}/live-session
# ---------------------------------------------------------------------------


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler_get(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for GET live-session."""
    start_ms = time.monotonic() * 1000
    correlation_id = _resolve_correlation_id(event)

    try:
        result = _handle_get(event, correlation_id)
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_metric(name="LiveSessionGetSuccess", unit=MetricUnit.Count, value=1)
        logger.info(
            "live_session_get_success",
            extra={
                "correlation_id": correlation_id,
                "status_code": result["statusCode"],
                "latency_ms": latency_ms,
            },
        )
        return result

    except ApiError as exc:
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_dimension(name="status_code", value=str(exc.status_code))
        metrics.add_metric(name="LiveSessionGetFailure", unit=MetricUnit.Count, value=1)

        logger.warning(
            "live_session_get_failure",
            extra={
                "correlation_id": correlation_id,
                "status_code": exc.status_code,
                "latency_ms": latency_ms,
                "error": exc.error_key,
            },
        )
        return error_response(exc, correlation_id)

    except Exception:
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_dimension(name="status_code", value="500")
        metrics.add_metric(name="LiveSessionGetFailure", unit=MetricUnit.Count, value=1)

        logger.exception(
            "live_session_get_unhandled_error",
            extra={
                "correlation_id": correlation_id,
                "status_code": 500,
                "latency_ms": latency_ms,
            },
        )
        return unhandled_error_response(correlation_id)


# ---------------------------------------------------------------------------
# Lambda handler — DELETE /v1/sites/{site_id}/cameras/{camera_id}/live-session
# ---------------------------------------------------------------------------


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler_delete(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for DELETE live-session."""
    start_ms = time.monotonic() * 1000
    correlation_id = _resolve_correlation_id(event)

    try:
        result = _handle_delete(event, correlation_id)
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_metric(name="LiveSessionDeleteSuccess", unit=MetricUnit.Count, value=1)
        logger.info(
            "live_session_delete_success",
            extra={
                "correlation_id": correlation_id,
                "status_code": result["statusCode"],
                "latency_ms": latency_ms,
            },
        )
        return result

    except ApiError as exc:
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_dimension(name="status_code", value=str(exc.status_code))
        metrics.add_metric(name="LiveSessionDeleteFailure", unit=MetricUnit.Count, value=1)

        logger.warning(
            "live_session_delete_failure",
            extra={
                "correlation_id": correlation_id,
                "status_code": exc.status_code,
                "latency_ms": latency_ms,
                "error": exc.error_key,
            },
        )
        return error_response(exc, correlation_id)

    except Exception:
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_dimension(name="status_code", value="500")
        metrics.add_metric(name="LiveSessionDeleteFailure", unit=MetricUnit.Count, value=1)

        logger.exception(
            "live_session_delete_unhandled_error",
            extra={
                "correlation_id": correlation_id,
                "status_code": 500,
                "latency_ms": latency_ms,
            },
        )
        return unhandled_error_response(correlation_id)


# ---------------------------------------------------------------------------
# Internal handlers (placeholder — expanded in tasks 6.2–6.4)
# ---------------------------------------------------------------------------


def _handle_post(event: dict[str, Any], correlation_id: str) -> dict[str, Any]:
    """Core logic for POST live-session — raises ApiError on failure."""
    query_params = event.get("queryStringParameters") or {}
    path_params = event.get("pathParameters") or {}

    site_id = (path_params.get("site_id") or "").strip()
    if not site_id:
        raise BadRequest("Missing required path parameter: site_id.")

    camera_id = (path_params.get("camera_id") or "").strip()
    if not camera_id:
        raise BadRequest("Missing required path parameter: camera_id.")

    # --- Extract JWT claims and resolve caller ---
    claims = _extract_claims(event)
    role, caller_tenant_id, site_access = _resolve_caller(claims)

    # --- Resolve tenant_id ---
    tenant_id = _resolve_tenant_id(role, caller_tenant_id, query_params)

    # --- Authorise the caller ---
    _check_access(role, caller_tenant_id, tenant_id, site_id, site_access)

    # --- Verify camera exists ---
    camera_item = data.get_camera(tenant_id, site_id, camera_id)
    if camera_item is None:
        raise NotFound(f"Camera '{camera_id}' not found.")

    # --- Check for existing active session ---
    try:
        existing_session = data.get_live_session(tenant_id, site_id, camera_id)
    except Exception:
        logger.exception("dynamo_get_live_session_error")
        raise InternalError()

    if existing_session is not None:
        existing_expires_at = existing_session.get("expires_at", {}).get("S", "")
        if existing_expires_at:
            try:
                expires_dt = datetime.fromisoformat(existing_expires_at.replace("Z", "+00:00"))
                if expires_dt > datetime.now(timezone.utc):
                    raise Conflict(
                        "A live session is already active for this camera.",
                        error_key="SESSION_ALREADY_ACTIVE",
                    )
            except Conflict:
                raise
            except Exception:
                # If we can't parse expires_at, treat session as expired
                pass

    # --- Compute session parameters ---
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=10)
    ttl = int(expires_at.timestamp()) + 3600
    session_id = str(uuid.uuid4())
    created_by = claims.get("sub", "unknown")

    # --- Write session record ---
    try:
        data.put_live_session(
            tenant_id=tenant_id,
            site_id=site_id,
            camera_id=camera_id,
            session_id=session_id,
            expires_at=expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            ttl=ttl,
            created_by=created_by,
            created_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    except Exception as exc:
        # ConditionalCheckFailedException means a race condition — another
        # request created the session between our check and our write.
        if hasattr(exc, "response") and exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise Conflict(
                "A live session is already active for this camera.",
                error_key="SESSION_ALREADY_ACTIVE",
            )
        logger.exception("dynamo_put_live_session_error")
        raise InternalError()

    # --- Return success ---
    return json_response(
        201,
        {
            "session_id": session_id,
            "expires_at": expires_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "camera_id": camera_id,
        },
        correlation_id,
    )


def _handle_get(event: dict[str, Any], correlation_id: str) -> dict[str, Any]:
    """Core logic for GET live-session — raises ApiError on failure."""
    query_params = event.get("queryStringParameters") or {}
    path_params = event.get("pathParameters") or {}

    site_id = (path_params.get("site_id") or "").strip()
    if not site_id:
        raise BadRequest("Missing required path parameter: site_id.")

    camera_id = (path_params.get("camera_id") or "").strip()
    if not camera_id:
        raise BadRequest("Missing required path parameter: camera_id.")

    # --- Extract JWT claims and resolve caller ---
    claims = _extract_claims(event)
    role, caller_tenant_id, site_access = _resolve_caller(claims)

    # --- Resolve tenant_id ---
    tenant_id = _resolve_tenant_id(role, caller_tenant_id, query_params)

    # --- Authorise the caller ---
    _check_access(role, caller_tenant_id, tenant_id, site_id, site_access)

    # --- Fetch the SESSION# record ---
    try:
        session_record = data.get_live_session(tenant_id, site_id, camera_id)
    except Exception:
        logger.exception(
            "get_live_session_dynamo_error",
            extra={"tenant_id": tenant_id, "site_id": site_id, "camera_id": camera_id},
        )
        raise InternalError()

    # If no session record or session has expired, return status: "none"
    if session_record is None:
        return json_response(200, {"status": "none"}, correlation_id)

    expires_at_str = session_record.get("expires_at", {}).get("S", "")
    now = datetime.now(timezone.utc)

    try:
        expires_at_dt = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        # Malformed expires_at — treat as expired
        return json_response(200, {"status": "none"}, correlation_id)

    if expires_at_dt <= now:
        return json_response(200, {"status": "none"}, correlation_id)

    # --- Session is active — fetch the latest LIVE_IMG# record ---
    session_id = session_record.get("session_id", {}).get("S", "")

    try:
        live_img_record = data.get_latest_live_img_record(tenant_id, site_id, camera_id)
    except Exception:
        logger.exception(
            "get_latest_live_img_dynamo_error",
            extra={"tenant_id": tenant_id, "site_id": site_id, "camera_id": camera_id},
        )
        # DynamoDB error on live img query — return session fields, omit latest_image
        return json_response(
            200,
            {
                "status": "active",
                "session_id": session_id,
                "expires_at": expires_at_str,
            },
            correlation_id,
        )

    # No live image records yet
    if live_img_record is None:
        return json_response(
            200,
            {
                "status": "active",
                "session_id": session_id,
                "expires_at": expires_at_str,
                "latest_image": None,
            },
            correlation_id,
        )

    # --- Generate presigned URL for the live S3 object ---
    s3_key = live_img_record.get("s3_key", {}).get("S", "")
    captured_at = live_img_record.get("captured_at", {}).get("S", "")
    presigned_url = storage.generate_presigned_url(s3_key, expires_in=300)

    return json_response(
        200,
        {
            "status": "active",
            "session_id": session_id,
            "expires_at": expires_at_str,
            "latest_image": {
                "presigned_url": presigned_url,
                "captured_at": captured_at,
                "expires_in": 300,
            },
        },
        correlation_id,
    )


def _handle_delete(event: dict[str, Any], correlation_id: str) -> dict[str, Any]:
    """Core logic for DELETE live-session — raises ApiError on failure."""
    query_params = event.get("queryStringParameters") or {}
    path_params = event.get("pathParameters") or {}

    site_id = (path_params.get("site_id") or "").strip()
    if not site_id:
        raise BadRequest("Missing required path parameter: site_id.")

    camera_id = (path_params.get("camera_id") or "").strip()
    if not camera_id:
        raise BadRequest("Missing required path parameter: camera_id.")

    # --- Extract JWT claims and resolve caller ---
    claims = _extract_claims(event)
    role, caller_tenant_id, site_access = _resolve_caller(claims)

    # --- Resolve tenant_id ---
    tenant_id = _resolve_tenant_id(role, caller_tenant_id, query_params)

    # --- Authorise the caller ---
    _check_access(role, caller_tenant_id, tenant_id, site_id, site_access)

    # --- Fetch the session record ---
    try:
        session_record = data.get_live_session(tenant_id, site_id, camera_id)
    except Exception as exc:
        logger.exception("dynamodb_get_live_session_failed")
        raise InternalError() from exc

    # --- Check session exists and is still active ---
    if session_record is None:
        raise NotFound()

    expires_at_str = session_record.get("expires_at", {}).get("S", "")
    if not expires_at_str:
        raise NotFound()

    expires_at_dt = datetime.fromisoformat(expires_at_str.replace("Z", "+00:00"))
    now_utc = datetime.now(timezone.utc)
    if expires_at_dt <= now_utc:
        raise NotFound()

    # --- Delete the session record ---
    try:
        data.delete_live_session(tenant_id, site_id, camera_id)
    except Exception as exc:
        logger.exception("dynamodb_delete_live_session_failed")
        raise InternalError() from exc

    return json_response(200, {"status": "deleted"}, correlation_id)


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


def _resolve_tenant_id(
    role: str,
    caller_tenant_id: str | None,
    query_params: dict[str, Any],
) -> str:
    """Resolve the effective tenant_id for the request.

    - super_admin: requires ?tenant_id= query parameter → 400 if absent
    - tenant_admin / user: uses the caller's own tenant_id → 403 if missing
    """
    if role == "super_admin":
        tenant_id_param = (query_params.get("tenant_id") or "").strip()
        if not tenant_id_param:
            raise BadRequest("Super admins must supply tenant_id as a query parameter.")
        return tenant_id_param

    if not caller_tenant_id:
        raise Forbidden()
    return caller_tenant_id


def _check_access(
    role: str,
    caller_tenant_id: str | None,
    site_tenant_id: str,
    site_id: str,
    site_access: list[str],
) -> None:
    """Raise Forbidden if the caller is not allowed to access this site.

    Rules:
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
