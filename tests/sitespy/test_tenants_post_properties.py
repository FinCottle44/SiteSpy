"""Property-based tests for the tenants_post handler.

Feature: admin-management-endpoints
Property 1: Role enforcement rejects unauthorized callers
Property 3: Missing required fields produce 400
Property 5: Valid creation round-trip preserves all input fields
Property 8: Invalid JSON body produces 400

**Validates: Requirements 1.2, 1.3, 1.11, 7.7**
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import boto3
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from moto import mock_aws

from sitespy.data import _dynamodb_client
from sitespy.handlers.tenants_post import handler

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TABLE_NAME = "test-data-table"


def _create_table(client):
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
    body: str | None = None,
    groups: str | None = None,
    tenant_id_claim: str | None = None,
    correlation_id: str | None = None,
) -> dict:
    """Build an API Gateway proxy event for POST /v1/tenants."""
    claims: dict = {}
    if groups is not None:
        claims["cognito:groups"] = groups
    if tenant_id_claim is not None:
        claims["custom:tenant_id"] = tenant_id_claim

    headers: dict = {}
    if correlation_id is not None:
        headers["X-Correlation-Id"] = correlation_id

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


def _is_not_valid_json(s: str) -> bool:
    """Return True if s is not valid JSON."""
    try:
        json.loads(s)
        return False
    except (json.JSONDecodeError, ValueError):
        return True


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

# Roles that are NOT super_admin — the handler requires super_admin
_NON_SUPER_ADMIN_GROUPS = st.one_of(
    st.just("TenantAdmins"),
    st.just("Users"),
    st.just(""),
    st.just("SomeOtherGroup"),
    st.text(
        alphabet=st.characters(blacklist_categories=("Cs",)),
        min_size=1,
        max_size=30,
    ).filter(lambda s: s != "SuperAdmins"),
)

# Required fields for tenant creation
_REQUIRED_FIELDS = ["tenant_id", "tenant_name", "primary_contact_email"]

# Strategy for subsets of required fields that are missing at least one
_MISSING_FIELD_SUBSETS = st.lists(
    st.sampled_from(_REQUIRED_FIELDS),
    min_size=1,
    max_size=3,
    unique=True,
)

# Valid tenant creation inputs
_VALID_TENANT_IDS = st.from_regex(r"[a-z0-9_]{3,32}", fullmatch=True)
_VALID_TENANT_NAMES = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    min_size=1,
    max_size=128,
).filter(lambda s: s.strip())
_VALID_EMAILS = st.from_regex(r"[a-z]{1,20}@[a-z]{1,20}\.[a-z]{2,5}", fullmatch=True)
_VALID_STALE_THRESHOLDS = st.integers(min_value=1, max_value=720)

# Invalid JSON bodies — strings that are not valid JSON
_INVALID_JSON_BODIES = st.one_of(
    st.just("{invalid json"),
    st.just("not json at all"),
    st.just("<xml>data</xml>"),
    st.just("{\"key\": }"),
    st.just("[unclosed"),
    st.text(
        alphabet=st.characters(blacklist_categories=("Cs",)),
        min_size=1,
        max_size=50,
    ).filter(_is_not_valid_json),
)


# ---------------------------------------------------------------------------
# Property 1: Role enforcement rejects unauthorized callers
# Validates: Requirements 1.2
# ---------------------------------------------------------------------------


@given(groups=_NON_SUPER_ADMIN_GROUPS)
@settings(max_examples=100)
def test_role_enforcement_rejects_unauthorized_callers(groups: str) -> None:
    """Property 1: Role enforcement rejects unauthorized callers.

    For any JWT claims where the caller's resolved role is not super_admin,
    the handler SHALL return a 403 response with error key ACCESS_DENIED
    and SHALL NOT perform any write operation.

    Feature: admin-management-endpoints, Property 1: Role enforcement rejects unauthorized callers

    **Validates: Requirements 1.2**
    """
    with mock_aws():
        _dynamodb_client.cache_clear()
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        body = json.dumps({
            "tenant_id": "test_tenant",
            "tenant_name": "Test Tenant",
            "primary_contact_email": "test@example.com",
        })

        event = _build_event(body=body, groups=groups)
        result = handler(event, MagicMock())

        assert result["statusCode"] == 403
        response_body = json.loads(result["body"])
        assert response_body["error"] == "ACCESS_DENIED"

        # Verify no write occurred
        response = client.get_item(
            TableName=_TABLE_NAME,
            Key={"PK": {"S": "TENANT#test_tenant"}, "SK": {"S": "TENANT#test_tenant"}},
        )
        assert "Item" not in response


# ---------------------------------------------------------------------------
# Property 3: Missing required fields produce 400
# Validates: Requirements 1.3
# ---------------------------------------------------------------------------


@given(missing_fields=_MISSING_FIELD_SUBSETS)
@settings(max_examples=100)
def test_missing_required_fields_produce_400(missing_fields: list[str]) -> None:
    """Property 3: Missing required fields produce 400.

    For any request body that is missing at least one required field,
    the handler SHALL return a 400 response with error key BAD_REQUEST.

    Feature: admin-management-endpoints, Property 3: Missing required fields produce 400

    **Validates: Requirements 1.3**
    """
    with mock_aws():
        _dynamodb_client.cache_clear()
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        # Build a complete body then remove the missing fields
        full_body: dict = {
            "tenant_id": "valid_tenant",
            "tenant_name": "Valid Tenant Name",
            "primary_contact_email": "valid@example.com",
        }
        for field in missing_fields:
            full_body.pop(field, None)

        event = _build_event(body=json.dumps(full_body), groups="SuperAdmins")
        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        response_body = json.loads(result["body"])
        assert response_body["error"] == "BAD_REQUEST"


# ---------------------------------------------------------------------------
# Property 5: Valid creation round-trip preserves all input fields
# Validates: Requirements 1.11
# ---------------------------------------------------------------------------


@given(
    tenant_id=_VALID_TENANT_IDS,
    tenant_name=_VALID_TENANT_NAMES,
    email=_VALID_EMAILS,
    stale_threshold=_VALID_STALE_THRESHOLDS,
)
@settings(max_examples=100)
def test_valid_creation_round_trip_preserves_input_fields(
    tenant_id: str,
    tenant_name: str,
    email: str,
    stale_threshold: int,
) -> None:
    """Property 5: Valid creation round-trip preserves all input fields.

    For any valid tenant creation request, the 201 response body SHALL
    contain all input fields with values matching the submitted request.

    Feature: admin-management-endpoints, Property 5: Valid creation round-trip preserves all input fields

    **Validates: Requirements 1.11**
    """
    with mock_aws():
        _dynamodb_client.cache_clear()
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        body = json.dumps({
            "tenant_id": tenant_id,
            "tenant_name": tenant_name,
            "primary_contact_email": email,
            "stale_threshold_hours": stale_threshold,
        })

        event = _build_event(body=body, groups="SuperAdmins")
        result = handler(event, MagicMock())

        assert result["statusCode"] == 201
        response_body = json.loads(result["body"])

        assert response_body["tenant_id"] == tenant_id
        assert response_body["tenant_name"] == tenant_name
        assert response_body["primary_contact_email"] == email
        assert response_body["stale_threshold_hours"] == stale_threshold


# ---------------------------------------------------------------------------
# Property 8: Invalid JSON body produces 400
# Validates: Requirements 7.7
# ---------------------------------------------------------------------------


@given(invalid_body=_INVALID_JSON_BODIES)
@settings(max_examples=100)
def test_invalid_json_body_produces_400(invalid_body: str) -> None:
    """Property 8: Invalid JSON body produces 400.

    For any request body that is not valid JSON, the handler SHALL return
    a 400 response with error key BAD_REQUEST.

    Feature: admin-management-endpoints, Property 8: Invalid JSON body produces 400

    **Validates: Requirements 7.7**
    """
    # No DynamoDB needed — the handler rejects before any DB call
    event = _build_event(body=invalid_body, groups="SuperAdmins")
    result = handler(event, MagicMock())

    assert result["statusCode"] == 400
    response_body = json.loads(result["body"])
    assert response_body["error"] == "BAD_REQUEST"
