"""Users POST handler for SiteSpy — POST /v1/users.

Creates a new user in Cognito. Tenant admin or super admin.

Requirements validated: 4.1–4.17
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from functools import lru_cache
from typing import Any

import boto3
import botocore.config
from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from botocore.exceptions import ClientError

from sitespy import data
from sitespy.errors import ApiError, BadRequest, Conflict, Forbidden, InternalError
from sitespy.http import error_response, json_response, unhandled_error_response
from sitespy.validation import validate_email, validate_site_id

# ---------------------------------------------------------------------------
# Powertools setup
# ---------------------------------------------------------------------------

logger = Logger(service="sitespy")
metrics = Metrics(namespace="SiteSpy", service="sitespy")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_ROUTE = "POST /v1/users"

_GROUP_SUPER_ADMINS = "SuperAdmins"
_GROUP_TENANT_ADMINS = "TenantAdmins"

_VALID_ROLES = {"user", "tenant_admin", "super_admin"}
_MAX_FULL_NAME_LENGTH = 128

_BOTO_CONFIG = botocore.config.Config(retries={"mode": "standard", "max_attempts": 3})


# ---------------------------------------------------------------------------
# Cognito client
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _cognito_client() -> Any:
    return boto3.client(
        "cognito-idp",
        region_name=os.environ.get("AWS_REGION", "eu-west-2"),
        config=_BOTO_CONFIG,
    )


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda entry point for POST /v1/users."""
    start_ms = time.monotonic() * 1000
    correlation_id = _resolve_correlation_id(event)

    try:
        result = _handle(event, correlation_id)
        latency_ms = int(time.monotonic() * 1000 - start_ms)

        metrics.add_metric(name="PostUserSuccess", unit=MetricUnit.Count, value=1)
        logger.info(
            "post_user_success",
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
        metrics.add_metric(name="PostUserFailure", unit=MetricUnit.Count, value=1)

        logger.warning(
            "post_user_failure",
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
        metrics.add_metric(name="PostUserFailure", unit=MetricUnit.Count, value=1)

        logger.exception(
            "post_user_unhandled_error",
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
    """Core logic for POST /v1/users — tenant_admin or super_admin."""
    # --- Extract JWT claims and resolve caller role ---
    claims = _extract_claims(event)
    role = _resolve_role(claims)

    # --- Enforce tenant_admin or super_admin ---
    if role not in ("tenant_admin", "super_admin"):
        raise Forbidden()

    # --- Parse JSON body ---
    body = _parse_body(event)

    # --- Validate required fields ---
    email = body.get("email")
    full_name = body.get("full_name")
    target_role = body.get("role")
    site_access = body.get("site_access")
    body_tenant_id = body.get("tenant_id")

    if email is None:
        raise BadRequest("Missing required field: email.")
    if full_name is None:
        raise BadRequest("Missing required field: full_name.")
    if target_role is None:
        raise BadRequest("Missing required field: role.")

    # --- Validate email ---
    if not isinstance(email, str) or not validate_email(email):
        raise BadRequest(
            "Invalid email: must be a valid email address (max 254 characters)."
        )

    # --- Validate full_name ---
    if not isinstance(full_name, str) or not full_name.strip():
        raise BadRequest("Invalid full_name: must be a non-empty string.")
    if len(full_name) > _MAX_FULL_NAME_LENGTH:
        raise BadRequest(
            f"Invalid full_name: must be at most {_MAX_FULL_NAME_LENGTH} characters."
        )

    # --- Validate role ---
    if not isinstance(target_role, str) or target_role not in _VALID_ROLES:
        raise BadRequest(
            "Invalid role: must be one of 'user', 'tenant_admin', or 'super_admin'."
        )

    # --- Determine target tenant_id ---
    caller_tenant_id = claims.get("custom:tenant_id") or ""

    if role == "tenant_admin":
        # Tenant admin: scope to own tenant_id from JWT
        target_tenant_id = caller_tenant_id

        # Reject cross-tenant creation
        if body_tenant_id and body_tenant_id != caller_tenant_id:
            raise Forbidden("Tenant admins cannot create users in other tenants.")

        # Reject super_admin creation by tenant admin
        if target_role == "super_admin":
            raise Forbidden("Tenant admins cannot create super_admin users.")
    else:
        # Super admin: use tenant_id from request body
        if not body_tenant_id:
            raise BadRequest("Missing required field: tenant_id.")
        if not isinstance(body_tenant_id, str) or not body_tenant_id.strip():
            raise BadRequest("Invalid tenant_id: must be a non-empty string.")
        target_tenant_id = body_tenant_id

    # --- Validate site_access for role=user ---
    if target_role == "user":
        if site_access is None or not isinstance(site_access, list) or len(site_access) == 0:
            raise BadRequest(
                "site_access is required and must be a non-empty list when role is 'user'."
            )

        # Validate each site_id exists for the tenant
        for sid in site_access:
            if not isinstance(sid, str) or not validate_site_id(sid):
                raise BadRequest(
                    f"Invalid site_id in site_access: '{sid}'. "
                    "Must match pattern: ^[a-z0-9_]{1,64}$."
                )
            try:
                site_item = data.get_site(target_tenant_id, sid)
            except Exception as exc:
                logger.exception("dynamodb_get_site_failed", extra={"site_id": sid})
                raise InternalError() from exc

            if site_item is None:
                raise BadRequest(
                    f"Site '{sid}' does not exist for tenant '{target_tenant_id}'."
                )
    else:
        # For non-user roles, site_access is not used
        site_access = []

    # --- Call Cognito AdminCreateUser ---
    user_pool_id = os.environ.get("COGNITO_USER_POOL_ID", "")
    if not user_pool_id:
        logger.error("COGNITO_USER_POOL_ID environment variable not set")
        raise InternalError("Cognito user pool not configured.")

    # Build user attributes
    user_attributes = [
        {"Name": "email", "Value": email},
        {"Name": "email_verified", "Value": "true"},
        {"Name": "custom:tenant_id", "Value": target_tenant_id},
        {"Name": "name", "Value": full_name},
    ]

    # Set site_access as comma-separated list for role=user
    site_access_str = ",".join(site_access) if site_access else ""
    if target_role == "user" and site_access_str:
        user_attributes.append(
            {"Name": "custom:site_access", "Value": site_access_str}
        )

    try:
        cognito_response = _cognito_client().admin_create_user(
            UserPoolId=user_pool_id,
            Username=email,
            UserAttributes=user_attributes,
            DesiredDeliveryMediums=["EMAIL"],
        )
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        if error_code == "UsernameExistsException":
            raise Conflict("A user with this email already exists.") from exc
        if error_code == "InvalidParameterException":
            raise BadRequest(
                f"Invalid parameter: {exc.response['Error'].get('Message', '')}"
            ) from exc
        logger.exception("cognito_admin_create_user_failed")
        raise InternalError() from exc

    # Extract the sub from the Cognito response
    user_record = cognito_response.get("User", {})
    user_sub = ""
    for attr in user_record.get("Attributes", []):
        if attr["Name"] == "sub":
            user_sub = attr["Value"]
            break

    # --- If role is tenant_admin, add to TenantAdmins group ---
    if target_role == "tenant_admin":
        try:
            _cognito_client().admin_add_user_to_group(
                UserPoolId=user_pool_id,
                Username=email,
                GroupName=_GROUP_TENANT_ADMINS,
            )
        except ClientError as exc:
            logger.exception(
                "cognito_admin_add_user_to_group_failed",
                extra={"email": email, "group": _GROUP_TENANT_ADMINS},
            )
            raise InternalError() from exc

    # --- If role is super_admin, add to SuperAdmins group ---
    if target_role == "super_admin":
        try:
            _cognito_client().admin_add_user_to_group(
                UserPoolId=user_pool_id,
                Username=email,
                GroupName=_GROUP_SUPER_ADMINS,
            )
        except ClientError as exc:
            logger.exception(
                "cognito_admin_add_user_to_group_failed",
                extra={"email": email, "group": _GROUP_SUPER_ADMINS},
            )
            raise InternalError() from exc

    # --- Write User_Record to DynamoDB ---
    try:
        data.put_user(
            tenant_id=target_tenant_id,
            sub=user_sub,
            email=email,
            full_name=full_name,
            role=target_role,
            site_access=site_access if target_role == "user" else [],
        )
    except Exception as exc:
        logger.exception(
            "dynamodb_put_user_failed",
            extra={"sub": user_sub, "tenant_id": target_tenant_id},
        )
        raise InternalError() from exc

    # --- Return 201 with user record ---
    response_body: dict[str, Any] = {
        "sub": user_sub,
        "email": email,
        "full_name": full_name,
        "tenant_id": target_tenant_id,
        "role": target_role,
        "site_access": site_access if site_access else [],
    }

    return json_response(201, response_body, correlation_id)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _extract_claims(event: dict[str, Any]) -> dict[str, Any]:
    """Extract Cognito JWT claims from the API Gateway authorizer context."""
    request_context = event.get("requestContext") or {}
    authorizer = request_context.get("authorizer") or {}
    return authorizer.get("claims") or {}


def _resolve_role(claims: dict[str, Any]) -> str:
    """Resolve role from JWT claims."""
    raw_groups = claims.get("cognito:groups") or ""
    if isinstance(raw_groups, list):
        groups: list[str] = raw_groups
    else:
        groups = [g.strip() for g in str(raw_groups).split(",") if g.strip()]
        if len(groups) == 1 and " " in groups[0]:
            groups = [g.strip() for g in groups[0].split() if g.strip()]

    if _GROUP_SUPER_ADMINS in groups:
        return "super_admin"
    elif _GROUP_TENANT_ADMINS in groups:
        return "tenant_admin"
    return "user"


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
