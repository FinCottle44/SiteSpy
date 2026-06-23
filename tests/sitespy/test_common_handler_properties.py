"""Property-based tests for common handler behaviour.

Feature: admin-management-endpoints
Property 7: Correlation ID round-trip
Property 12: CORS headers present on all responses

**Validates: Requirements 7.1, 7.2, 7.3, 7.9**
"""

from __future__ import annotations

import json
import os
import re
from unittest.mock import MagicMock, patch

import boto3
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from moto import mock_aws

from sitespy.data import _dynamodb_client
from sitespy.handlers.cameras_get import handler as cameras_get_handler
from sitespy.handlers.sites_post import handler as sites_post_handler
from sitespy.handlers.tenants_post import handler as tenants_post_handler

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TABLE_NAME = "test-data-table"
_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_UUID_V4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Required CORS headers per Requirement 7.9
_REQUIRED_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization,X-Correlation-Id",
    "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _create_table(client) -> None:
    """Create the test DynamoDB table matching the project schema."""
    client.create_table(
        TableName=_TABLE_NAME,
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "GSI1",
                "KeySchema": [
                    {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )


def _seed_tenant(client, tenant_id: str) -> None:
    """Insert a tenant record so site creation can verify tenant exists."""
    client.put_item(
        TableName=_TABLE_NAME,
        Item={
            "PK": {"S": f"TENANT#{tenant_id}"},
            "SK": {"S": f"TENANT#{tenant_id}"},
            "tenant_name": {"S": "Test Tenant"},
            "primary_contact_email": {"S": "test@example.com"},
            "stale_threshold_hours": {"N": "24"},
        },
    )


def _seed_site(client, tenant_id: str, site_id: str) -> None:
    """Insert a site record so cameras_get can verify site exists."""
    client.put_item(
        TableName=_TABLE_NAME,
        Item={
            "PK": {"S": f"TENANT#{tenant_id}"},
            "SK": {"S": f"SITE#{site_id}"},
            "site_name": {"S": "Test Site"},
            "latitude": {"N": "51.5074"},
            "longitude": {"N": "-0.1278"},
            "timezone": {"S": "Europe/London"},
        },
    )


def _build_tenants_post_event(
    *,
    correlation_id: str | None = None,
    groups: str = "SuperAdmins",
    body: str | None = None,
) -> dict:
    """Build an API Gateway proxy event for POST /v1/tenants."""
    headers: dict = {}
    if correlation_id is not None:
        headers["X-Correlation-Id"] = correlation_id

    claims: dict = {"cognito:groups": groups}

    if body is None:
        body = json.dumps({
            "tenant_id": "test_tenant",
            "tenant_name": "Test Tenant",
            "primary_contact_email": "test@example.com",
        })

    return {
        "httpMethod": "POST",
        "path": "/v1/tenants",
        "headers": headers,
        "queryStringParameters": None,
        "pathParameters": None,
        "requestContext": {
            "resourcePath": "/v1/tenants",
            "httpMethod": "POST",
            "stage": "prod",
            "authorizer": {"claims": claims},
        },
        "body": body,
        "isBase64Encoded": False,
    }


def _build_sites_post_event(
    *,
    correlation_id: str | None = None,
    groups: str = "SuperAdmins",
    tenant_id: str = "test_tenant",
    body: str | None = None,
) -> dict:
    """Build an API Gateway proxy event for POST /v1/sites."""
    headers: dict = {}
    if correlation_id is not None:
        headers["X-Correlation-Id"] = correlation_id

    claims: dict = {"cognito:groups": groups}

    if body is None:
        body = json.dumps({
            "site_id": "test_site",
            "site_name": "Test Site",
            "latitude": 51.5074,
            "longitude": -0.1278,
        })

    return {
        "httpMethod": "POST",
        "path": "/v1/sites",
        "headers": headers,
        "queryStringParameters": {"tenant_id": tenant_id},
        "pathParameters": None,
        "requestContext": {
            "resourcePath": "/v1/sites",
            "httpMethod": "POST",
            "stage": "prod",
            "authorizer": {"claims": claims},
        },
        "body": body,
        "isBase64Encoded": False,
    }


def _build_cameras_get_event(
    *,
    correlation_id: str | None = None,
    groups: str = "SuperAdmins",
    tenant_id: str = "test_tenant",
    site_id: str = "test_site",
) -> dict:
    """Build an API Gateway proxy event for GET /v1/sites/{site_id}/cameras."""
    headers: dict = {}
    if correlation_id is not None:
        headers["X-Correlation-Id"] = correlation_id

    claims: dict = {"cognito:groups": groups}

    return {
        "httpMethod": "GET",
        "path": f"/v1/sites/{site_id}/cameras",
        "headers": headers,
        "queryStringParameters": {"tenant_id": tenant_id},
        "pathParameters": {"site_id": site_id},
        "requestContext": {
            "resourcePath": "/v1/sites/{site_id}/cameras",
            "httpMethod": "GET",
            "stage": "prod",
            "authorizer": {"claims": claims},
        },
        "body": None,
        "isBase64Encoded": False,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env_and_cache():
    """Set env vars and clear the _dynamodb_client lru_cache."""
    os.environ.setdefault("DATA_TABLE", _TABLE_NAME)
    os.environ.setdefault("AWS_REGION", "eu-west-2")
    os.environ.setdefault("SNAPSHOTS_BUCKET", "test-bucket")
    _dynamodb_client.cache_clear()
    yield
    _dynamodb_client.cache_clear()


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Valid correlation IDs matching ^[A-Za-z0-9_-]{1,128}$
_VALID_CORRELATION_IDS = st.from_regex(r"[A-Za-z0-9_\-]{1,128}", fullmatch=True)

# Invalid correlation IDs — strings that do NOT match the valid pattern
_INVALID_CORRELATION_IDS = st.one_of(
    # Empty string
    st.just(""),
    # Too long (>128 chars)
    st.from_regex(r"[A-Za-z0-9_\-]{129,200}", fullmatch=True),
    # Contains invalid characters (spaces, special chars)
    st.text(
        alphabet=st.characters(
            whitelist_categories=("L", "N", "P", "Z"),
            blacklist_characters=tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"),
        ),
        min_size=1,
        max_size=50,
    ).filter(lambda s: not _CORRELATION_ID_RE.match(s)),
    # Contains spaces
    st.text(min_size=2, max_size=50).filter(
        lambda s: " " in s and not _CORRELATION_ID_RE.match(s)
    ),
)

# Handler scenarios that produce various response codes (success and error)
# Each scenario is (handler_fn, event_builder_fn, description)
_HANDLER_SCENARIOS = st.sampled_from([
    "tenants_post_success",
    "tenants_post_error_403",
    "tenants_post_error_400",
    "sites_post_error_403",
    "sites_post_error_400",
    "cameras_get_success",
    "cameras_get_error_403",
    "cameras_get_error_400",
])


# ---------------------------------------------------------------------------
# Property 7: Correlation ID round-trip
# Validates: Requirements 7.1, 7.2, 7.3
# ---------------------------------------------------------------------------


@given(correlation_id=_VALID_CORRELATION_IDS, scenario=_HANDLER_SCENARIOS)
@settings(max_examples=100)
def test_valid_correlation_id_echoed_in_response(
    correlation_id: str, scenario: str
) -> None:
    """Property 7: Correlation ID round-trip (valid header echoed).

    For any valid X-Correlation-Id header (matching ^[A-Za-z0-9_-]{1,128}$),
    the response SHALL echo the same value in the X-Correlation-Id response
    header.

    Feature: admin-management-endpoints, Property 7: Correlation ID round-trip

    **Validates: Requirements 7.1, 7.3**
    """
    with mock_aws():
        _dynamodb_client.cache_clear()
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        result = _invoke_scenario(client, scenario, correlation_id=correlation_id)

        # The response must echo the exact correlation ID
        response_headers = result.get("headers", {})
        assert response_headers.get("X-Correlation-Id") == correlation_id, (
            f"Expected correlation ID '{correlation_id}' in response, "
            f"got '{response_headers.get('X-Correlation-Id')}'"
        )


@given(scenario=_HANDLER_SCENARIOS)
@settings(max_examples=100)
def test_absent_correlation_id_generates_uuid_v4(scenario: str) -> None:
    """Property 7: Correlation ID round-trip (absent header generates UUID v4).

    For any request where the X-Correlation-Id header is absent, the response
    SHALL contain a valid UUID v4 in the X-Correlation-Id response header.

    Feature: admin-management-endpoints, Property 7: Correlation ID round-trip

    **Validates: Requirements 7.2, 7.3**
    """
    with mock_aws():
        _dynamodb_client.cache_clear()
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        result = _invoke_scenario(client, scenario, correlation_id=None)

        response_headers = result.get("headers", {})
        response_corr_id = response_headers.get("X-Correlation-Id", "")
        assert _UUID_V4_RE.match(response_corr_id), (
            f"Expected a valid UUID v4 when no correlation ID provided, "
            f"got '{response_corr_id}'"
        )


@given(invalid_id=_INVALID_CORRELATION_IDS, scenario=_HANDLER_SCENARIOS)
@settings(max_examples=100, deadline=None)
def test_invalid_correlation_id_generates_uuid_v4(
    invalid_id: str, scenario: str
) -> None:
    """Property 7: Correlation ID round-trip (invalid header generates UUID v4).

    For any request where the X-Correlation-Id header does not match the valid
    pattern, the response SHALL contain a valid UUID v4 in the X-Correlation-Id
    response header.

    Feature: admin-management-endpoints, Property 7: Correlation ID round-trip

    **Validates: Requirements 7.2, 7.3**
    """
    with mock_aws():
        _dynamodb_client.cache_clear()
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        result = _invoke_scenario(client, scenario, correlation_id=invalid_id)

        response_headers = result.get("headers", {})
        response_corr_id = response_headers.get("X-Correlation-Id", "")
        assert _UUID_V4_RE.match(response_corr_id), (
            f"Expected a valid UUID v4 for invalid correlation ID '{invalid_id}', "
            f"got '{response_corr_id}'"
        )


# ---------------------------------------------------------------------------
# Property 12: CORS headers present on all responses
# Validates: Requirements 7.9
# ---------------------------------------------------------------------------


@given(scenario=_HANDLER_SCENARIOS, correlation_id=st.one_of(st.none(), _VALID_CORRELATION_IDS))
@settings(max_examples=100)
def test_cors_headers_present_on_all_responses(
    scenario: str, correlation_id: str | None
) -> None:
    """Property 12: CORS headers present on all responses.

    For any response from any handler (success or error), the response SHALL
    include Access-Control-Allow-Origin: *, Access-Control-Allow-Headers
    containing Content-Type, and Access-Control-Allow-Methods.

    Feature: admin-management-endpoints, Property 12: CORS headers present on all responses

    **Validates: Requirements 7.9**
    """
    with mock_aws():
        _dynamodb_client.cache_clear()
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        result = _invoke_scenario(client, scenario, correlation_id=correlation_id)

        response_headers = result.get("headers", {})

        # Check Access-Control-Allow-Origin
        assert response_headers.get("Access-Control-Allow-Origin") == "*", (
            f"Missing or incorrect Access-Control-Allow-Origin header. "
            f"Got: {response_headers.get('Access-Control-Allow-Origin')}"
        )

        # Check Access-Control-Allow-Headers contains Content-Type
        allow_headers = response_headers.get("Access-Control-Allow-Headers", "")
        assert "Content-Type" in allow_headers, (
            f"Access-Control-Allow-Headers must contain 'Content-Type'. "
            f"Got: {allow_headers}"
        )

        # Check Access-Control-Allow-Methods is present and non-empty
        allow_methods = response_headers.get("Access-Control-Allow-Methods", "")
        assert allow_methods, (
            "Access-Control-Allow-Methods header must be present and non-empty."
        )


# ---------------------------------------------------------------------------
# Scenario dispatcher
# ---------------------------------------------------------------------------


def _invoke_scenario(
    client, scenario: str, *, correlation_id: str | None
) -> dict:
    """Invoke a handler scenario and return the response dict.

    Covers multiple handlers and both success and error paths to verify
    the properties hold universally.
    """
    if scenario == "tenants_post_success":
        # Valid tenant creation — 201
        event = _build_tenants_post_event(
            correlation_id=correlation_id,
            groups="SuperAdmins",
        )
        return tenants_post_handler(event, MagicMock())

    elif scenario == "tenants_post_error_403":
        # Non-super_admin caller — 403
        event = _build_tenants_post_event(
            correlation_id=correlation_id,
            groups="TenantAdmins",
        )
        return tenants_post_handler(event, MagicMock())

    elif scenario == "tenants_post_error_400":
        # Invalid JSON body — 400
        event = _build_tenants_post_event(
            correlation_id=correlation_id,
            groups="SuperAdmins",
            body="{not valid json",
        )
        return tenants_post_handler(event, MagicMock())

    elif scenario == "sites_post_error_403":
        # Non-super_admin caller — 403
        event = _build_sites_post_event(
            correlation_id=correlation_id,
            groups="TenantAdmins",
        )
        return sites_post_handler(event, MagicMock())

    elif scenario == "sites_post_error_400":
        # Missing tenant_id query param — 400
        event = _build_sites_post_event(
            correlation_id=correlation_id,
            groups="SuperAdmins",
            tenant_id="",
        )
        # Override query params to be empty
        event["queryStringParameters"] = {}
        return sites_post_handler(event, MagicMock())

    elif scenario == "cameras_get_success":
        # Valid cameras listing — 200
        _seed_tenant(client, "test_tenant")
        _seed_site(client, "test_tenant", "test_site")
        event = _build_cameras_get_event(
            correlation_id=correlation_id,
            groups="SuperAdmins",
        )
        return cameras_get_handler(event, MagicMock())

    elif scenario == "cameras_get_error_403":
        # Non-admin caller — 403
        event = _build_cameras_get_event(
            correlation_id=correlation_id,
            groups="Users",
        )
        return cameras_get_handler(event, MagicMock())

    elif scenario == "cameras_get_error_400":
        # Super admin without tenant_id — 400
        event = _build_cameras_get_event(
            correlation_id=correlation_id,
            groups="SuperAdmins",
        )
        event["queryStringParameters"] = {}
        return cameras_get_handler(event, MagicMock())

    else:
        raise ValueError(f"Unknown scenario: {scenario}")
