"""Property-based tests for common handler behaviour of GET /v1/users.

Feature: list-users-endpoint
Property 6: Correlation ID round-trip

**Validates: Requirements 3.1, 3.2, 3.3**
"""

from __future__ import annotations

import json
import os
import re
from typing import Any
from unittest.mock import MagicMock, patch

import boto3
import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st
from moto import mock_aws

from sitespy.data import _dynamodb_client
from sitespy.handlers.users_get import handler

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TABLE_NAME = "test-data-table"
_CORRELATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_UUID_V4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


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


def _build_event(
    *,
    groups: str | list[str] = "TenantAdmins",
    tenant_id_claim: str = "test_tenant",
    query_params: dict[str, str] | None = None,
    correlation_id: str | None = "test-corr-id",
) -> dict[str, Any]:
    """Build an API Gateway proxy event for GET /v1/users."""
    claims: dict[str, Any] = {"cognito:groups": groups}
    if tenant_id_claim:
        claims["custom:tenant_id"] = tenant_id_claim

    headers: dict[str, str] = {}
    if correlation_id is not None:
        headers["X-Correlation-Id"] = correlation_id

    return {
        "httpMethod": "GET",
        "path": "/v1/users",
        "headers": headers,
        "queryStringParameters": query_params,
        "pathParameters": None,
        "requestContext": {
            "resourcePath": "/v1/users",
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


def _is_not_valid_correlation_id(s: str) -> bool:
    """Return True if the string does NOT match the valid correlation ID pattern."""
    return not _CORRELATION_ID_RE.match(s)


_INVALID_CORRELATION_IDS = st.one_of(
    # Empty string
    st.just(""),
    # Too long (>128 chars)
    st.from_regex(r"[A-Za-z0-9_\-]{129,200}", fullmatch=True),
    # Contains invalid characters (spaces, special chars)
    st.text(
        alphabet=st.characters(
            whitelist_categories=("P", "Z", "S"),
        ),
        min_size=1,
        max_size=50,
    ).filter(_is_not_valid_correlation_id),
)

# Handler scenarios covering success and error paths for GET /v1/users.
# This ensures the correlation ID property holds regardless of response status.
_HANDLER_SCENARIOS = st.sampled_from([
    "success_200",
    "error_403",
    "error_400",
])


# ---------------------------------------------------------------------------
# Scenario dispatcher
# ---------------------------------------------------------------------------


def _invoke_scenario(
    client, scenario: str, *, correlation_id: str | None
) -> dict[str, Any]:
    """Invoke a GET /v1/users scenario and return the response dict.

    Covers success and error paths to verify the correlation ID property
    holds universally across all response types.
    """
    if scenario == "success_200":
        # Valid tenant_admin request — 200
        event = _build_event(
            groups="TenantAdmins",
            tenant_id_claim="test_tenant",
            correlation_id=correlation_id,
        )
        return handler(event, MagicMock())

    elif scenario == "error_403":
        # Unauthorized caller (plain user) — 403
        event = _build_event(
            groups="Users",
            tenant_id_claim="test_tenant",
            correlation_id=correlation_id,
        )
        return handler(event, MagicMock())

    elif scenario == "error_400":
        # Super admin without tenant_id query param — 400
        event = _build_event(
            groups="SuperAdmins",
            tenant_id_claim="",
            query_params=None,
            correlation_id=correlation_id,
        )
        return handler(event, MagicMock())

    else:
        raise ValueError(f"Unknown scenario: {scenario}")


# ---------------------------------------------------------------------------
# Property 6: Correlation ID round-trip
# Validates: Requirements 3.1, 3.2, 3.3
#
# For any request where the X-Correlation-Id header matches
# ^[A-Za-z0-9_-]{1,128}$, the response SHALL echo that exact value in the
# X-Correlation-Id response header.
#
# For any request where the header is absent or does not match the pattern,
# the response SHALL contain a valid UUID v4 in the X-Correlation-Id
# response header.
# ---------------------------------------------------------------------------


@given(correlation_id=_VALID_CORRELATION_IDS, scenario=_HANDLER_SCENARIOS)
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_valid_correlation_id_echoed_in_response(
    correlation_id: str, scenario: str
) -> None:
    """Property 6: Correlation ID round-trip (valid header echoed).

    For any valid X-Correlation-Id header (matching ^[A-Za-z0-9_-]{1,128}$),
    the GET /v1/users response SHALL echo the same value in the
    X-Correlation-Id response header.

    Feature: list-users-endpoint, Property 6: Correlation ID round-trip

    **Validates: Requirements 3.1, 3.3**
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
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_absent_correlation_id_generates_uuid_v4(scenario: str) -> None:
    """Property 6: Correlation ID round-trip (absent header generates UUID v4).

    For any GET /v1/users request where the X-Correlation-Id header is absent,
    the response SHALL contain a valid UUID v4 in the X-Correlation-Id
    response header.

    Feature: list-users-endpoint, Property 6: Correlation ID round-trip

    **Validates: Requirements 3.2, 3.3**
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
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_invalid_correlation_id_generates_uuid_v4(
    invalid_id: str, scenario: str
) -> None:
    """Property 6: Correlation ID round-trip (invalid header generates UUID v4).

    For any GET /v1/users request where the X-Correlation-Id header does not
    match the valid pattern ^[A-Za-z0-9_-]{1,128}$, the response SHALL
    contain a valid UUID v4 in the X-Correlation-Id response header.

    Feature: list-users-endpoint, Property 6: Correlation ID round-trip

    **Validates: Requirements 3.2, 3.3**
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
