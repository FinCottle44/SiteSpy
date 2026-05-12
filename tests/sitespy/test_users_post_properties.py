"""Property-based tests for the users_post handler.

Feature: admin-management-endpoints
Property 6: Tenant admin cross-tenant isolation
Property 13: site_access serialization round-trip

Feature: list-users-endpoint
Property 3: User record write completeness
Property 4: site_access storage invariant

**Validates: Requirements 4.4, 4.14, 1.1, 1.2, 1.3, 1.4**
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import MagicMock

import boto3
import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st
from moto import mock_aws

from sitespy.data import _dynamodb_client, put_user
from sitespy.handlers.users_post import _cognito_client, handler

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TABLE_NAME = "test-data-table"


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


def _create_user_pool() -> str:
    """Create a Cognito user pool and return its ID."""
    cognito = boto3.client("cognito-idp", region_name="eu-west-2")
    response = cognito.create_user_pool(
        PoolName="TestPool",
        Schema=[
            {"Name": "email", "AttributeDataType": "String", "Mutable": True},
            {"Name": "custom:tenant_id", "AttributeDataType": "String", "Mutable": True},
            {"Name": "custom:site_access", "AttributeDataType": "String", "Mutable": True},
        ],
    )
    pool_id = response["UserPool"]["Id"]

    # Create required groups
    cognito.create_group(GroupName="TenantAdmins", UserPoolId=pool_id)
    cognito.create_group(GroupName="SuperAdmins", UserPoolId=pool_id)

    return pool_id


def _seed_tenant(client, tenant_id: str) -> None:
    """Insert a tenant record."""
    client.put_item(
        TableName=_TABLE_NAME,
        Item={
            "PK": {"S": f"TENANT#{tenant_id}"},
            "SK": {"S": f"TENANT#{tenant_id}"},
            "tenant_name": {"S": "Test Tenant"},
            "primary_contact_email": {"S": "ops@test.com"},
            "stale_threshold_hours": {"N": "24"},
        },
    )


def _seed_site(client, tenant_id: str, site_id: str) -> None:
    """Insert a site record for the given tenant."""
    client.put_item(
        TableName=_TABLE_NAME,
        Item={
            "PK": {"S": f"TENANT#{tenant_id}"},
            "SK": {"S": f"SITE#{site_id}"},
            "site_name": {"S": f"Site {site_id}"},
            "latitude": {"N": "51.5074"},
            "longitude": {"N": "-0.1278"},
            "timezone": {"S": "Europe/London"},
        },
    )


def _build_event(
    *,
    body: dict[str, Any] | str,
    groups: str = "TenantAdmins",
    tenant_id_claim: str = "tenant_a",
    correlation_id: str = "test-corr-id",
) -> dict[str, Any]:
    """Build an API Gateway proxy event for POST /v1/users."""
    claims: dict[str, Any] = {
        "cognito:groups": groups,
        "custom:tenant_id": tenant_id_claim,
    }

    raw_body = json.dumps(body) if isinstance(body, dict) else body

    return {
        "httpMethod": "POST",
        "path": "/v1/users",
        "headers": {"X-Correlation-Id": correlation_id},
        "queryStringParameters": None,
        "pathParameters": None,
        "requestContext": {
            "resourcePath": "/v1/users",
            "httpMethod": "POST",
            "stage": "prod",
            "authorizer": {"claims": claims},
        },
        "body": raw_body,
        "isBase64Encoded": False,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env_and_cache():
    """Set env vars and clear caches."""
    os.environ.setdefault("DATA_TABLE", _TABLE_NAME)
    os.environ.setdefault("AWS_REGION", "eu-west-2")
    os.environ.setdefault("SNAPSHOTS_BUCKET", "test-bucket")
    os.environ["COGNITO_USER_POOL_ID"] = "eu-west-2_TestPool"
    _dynamodb_client.cache_clear()
    _cognito_client.cache_clear()
    yield
    _dynamodb_client.cache_clear()
    _cognito_client.cache_clear()


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Valid tenant IDs matching ^[a-z0-9_]{3,32}$
_VALID_TENANT_IDS = st.from_regex(r"[a-z0-9_]{3,32}", fullmatch=True)

# Strategy for generating two different tenant IDs (caller vs target)
_CROSS_TENANT_PAIRS = st.tuples(
    _VALID_TENANT_IDS,
    _VALID_TENANT_IDS,
).filter(lambda pair: pair[0] != pair[1])

# Valid site IDs matching ^[a-z0-9_]{1,64}$
_VALID_SITE_IDS = st.from_regex(r"[a-z0-9_]{1,64}", fullmatch=True)

# Non-empty lists of valid site IDs (1 to 5 items for reasonable test speed)
_SITE_ACCESS_LISTS = st.lists(
    _VALID_SITE_IDS,
    min_size=1,
    max_size=5,
    unique=True,
)


# ---------------------------------------------------------------------------
# Property 6: Tenant admin cross-tenant isolation
# Validates: Requirements 4.4
#
# For any tenant admin caller, attempting to create a user with a different
# tenant_id than their own JWT custom:tenant_id should return 403.
# ---------------------------------------------------------------------------


@given(tenant_pair=_CROSS_TENANT_PAIRS)
@settings(max_examples=100, deadline=None)
def test_tenant_admin_cross_tenant_isolation(tenant_pair: tuple[str, str]) -> None:
    """Property 6: Tenant admin cross-tenant isolation.

    For any tenant admin caller with custom:tenant_id = X, attempting to
    create a user with a different tenant_id Y (where X != Y) should
    return 403 with error key ACCESS_DENIED.

    Feature: admin-management-endpoints, Property 6: Tenant admin cross-tenant isolation

    **Validates: Requirements 4.4**
    """
    caller_tenant_id, target_tenant_id = tenant_pair

    with mock_aws():
        _dynamodb_client.cache_clear()
        _cognito_client.cache_clear()

        # Set up DynamoDB — the handler rejects before reaching Cognito
        # so we only need the DDB table (no Cognito pool needed).
        ddb_client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(ddb_client)

        # Build request: tenant admin trying to create user in different tenant
        body = {
            "email": "user@example.com",
            "full_name": "Test User",
            "role": "user",
            "tenant_id": target_tenant_id,
            "site_access": ["site_001"],
        }

        event = _build_event(
            body=body,
            groups="TenantAdmins",
            tenant_id_claim=caller_tenant_id,
        )

        result = handler(event, MagicMock())

        assert result["statusCode"] == 403
        response_body = json.loads(result["body"])
        assert response_body["error"] == "ACCESS_DENIED"


# ---------------------------------------------------------------------------
# Property 13: site_access serialization round-trip
# Validates: Requirements 4.14
#
# For any valid list of site_ids, the site_access field in the 201 response
# should match the input list exactly (round-trip preservation).
# ---------------------------------------------------------------------------


@given(site_ids=_SITE_ACCESS_LISTS)
@settings(max_examples=100, deadline=None)
def test_site_access_serialization_round_trip(site_ids: list[str]) -> None:
    """Property 13: site_access serialization round-trip.

    For any valid list of site_ids, the site_access field in the 201 response
    should match the input list exactly (round-trip preservation).

    Feature: admin-management-endpoints, Property 13: site_access serialization round-trip

    **Validates: Requirements 4.14**
    """
    with mock_aws():
        _dynamodb_client.cache_clear()
        _cognito_client.cache_clear()

        # Set up DynamoDB
        ddb_client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(ddb_client)

        tenant_id = "test_tenant"
        _seed_tenant(ddb_client, tenant_id)

        # Seed all sites referenced in site_access
        for sid in site_ids:
            _seed_site(ddb_client, tenant_id, sid)

        # Create Cognito user pool
        pool_id = _create_user_pool()
        os.environ["COGNITO_USER_POOL_ID"] = pool_id

        # Build request: super admin creating a user with role=user
        body = {
            "email": "testuser@example.com",
            "full_name": "Test User",
            "role": "user",
            "tenant_id": tenant_id,
            "site_access": site_ids,
        }

        event = _build_event(
            body=body,
            groups="SuperAdmins",
            tenant_id_claim=tenant_id,
        )

        result = handler(event, MagicMock())

        assert result["statusCode"] == 201
        response_body = json.loads(result["body"])

        # The site_access in the response should match the input exactly
        assert response_body["site_access"] == site_ids


# ---------------------------------------------------------------------------
# Strategies for Property 3
# ---------------------------------------------------------------------------

# Valid tenant IDs matching ^[a-z0-9_]{3,32}$
_PROP3_TENANT_IDS = st.from_regex(r"[a-z0-9_]{3,32}", fullmatch=True)

# Valid Cognito sub (UUID-like strings)
_PROP3_SUBS = st.from_regex(
    r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}",
    fullmatch=True,
)

# Valid email addresses
_PROP3_EMAILS = st.from_regex(r"[a-z]{1,20}@[a-z]{1,20}\.[a-z]{2,5}", fullmatch=True)

# Valid full names (non-empty printable strings)
_PROP3_FULL_NAMES = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "Zs"),
        whitelist_characters="-'.",
    ),
    min_size=1,
    max_size=100,
).filter(lambda s: s.strip())

# Valid roles
_PROP3_ROLES = st.sampled_from(["user", "tenant_admin", "super_admin"])

# Valid site IDs for site_access lists
_PROP3_SITE_IDS = st.from_regex(r"[a-z0-9_]{1,64}", fullmatch=True)

# site_access lists (0 to 5 items for reasonable test speed)
_PROP3_SITE_ACCESS = st.lists(_PROP3_SITE_IDS, min_size=0, max_size=5, unique=True)


# ---------------------------------------------------------------------------
# Property 3: User record write completeness
# Validates: Requirements 1.1, 1.2
#
# For any valid combination of user attributes (tenant_id, sub, email,
# full_name, role, site_access), calling put_user() writes a complete
# User_Record to DynamoDB with PK=TENANT#<tenant_id>, SK=USER#<sub>,
# and all six attributes present and correctly structured.
# ---------------------------------------------------------------------------


@given(
    tenant_id=_PROP3_TENANT_IDS,
    sub=_PROP3_SUBS,
    email=_PROP3_EMAILS,
    full_name=_PROP3_FULL_NAMES,
    role=_PROP3_ROLES,
    site_access=_PROP3_SITE_ACCESS,
)
@settings(max_examples=100, deadline=None)
def test_user_record_write_completeness(
    tenant_id: str,
    sub: str,
    email: str,
    full_name: str,
    role: str,
    site_access: list[str],
) -> None:
    """Property 3: User record write completeness.

    For any valid combination of user attributes, calling put_user() writes
    a complete User_Record to DynamoDB with PK=TENANT#<tenant_id>,
    SK=USER#<sub>, and all six attributes (sub, email, full_name, tenant_id,
    role, site_access) present with values matching the input.

    Feature: list-users-endpoint, Property 3: User record write completeness

    **Validates: Requirements 1.1, 1.2**
    """
    with mock_aws():
        _dynamodb_client.cache_clear()

        # Set up DynamoDB table
        ddb_client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(ddb_client)

        # Call put_user with the generated inputs
        put_user(
            tenant_id=tenant_id,
            sub=sub,
            email=email,
            full_name=full_name,
            role=role,
            site_access=site_access,
        )

        # Read the item back from DynamoDB
        response = ddb_client.get_item(
            TableName=_TABLE_NAME,
            Key={
                "PK": {"S": f"TENANT#{tenant_id}"},
                "SK": {"S": f"USER#{sub}"},
            },
        )

        assert "Item" in response, "User_Record was not written to DynamoDB"
        item = response["Item"]

        # Verify PK and SK structure
        assert item["PK"]["S"] == f"TENANT#{tenant_id}"
        assert item["SK"]["S"] == f"USER#{sub}"

        # Verify all six attributes are present and correct
        assert item["sub"]["S"] == sub
        assert item["email"]["S"] == email
        assert item["full_name"]["S"] == full_name
        assert item["tenant_id"]["S"] == tenant_id
        assert item["role"]["S"] == role

        # Verify site_access is stored as a DynamoDB List of Strings
        assert "site_access" in item
        assert "L" in item["site_access"]
        stored_site_access = [entry["S"] for entry in item["site_access"]["L"]]
        assert stored_site_access == site_access



# ---------------------------------------------------------------------------
# Property 4: site_access storage invariant
# Validates: Requirements 1.3, 1.4
#
# For any User_Record written to DynamoDB:
# - If role=user, site_access is stored as the list of site IDs from the request
# - If role=tenant_admin or super_admin, site_access is stored as an empty list
#   regardless of what was provided in the input
# ---------------------------------------------------------------------------

# Strategy for roles that should have empty site_access
_ADMIN_ROLES = st.sampled_from(["tenant_admin", "super_admin"])

# Strategy for arbitrary site_access lists (including non-empty ones for admin roles)
_ARBITRARY_SITE_ACCESS = st.lists(
    _VALID_SITE_IDS,
    min_size=0,
    max_size=5,
    unique=True,
)


@given(
    role=_ADMIN_ROLES,
    site_access_input=_ARBITRARY_SITE_ACCESS,
)
@settings(max_examples=100, deadline=None)
def test_site_access_storage_invariant_admin_roles(
    role: str,
    site_access_input: list[str],
) -> None:
    """Property 4: site_access storage invariant (admin roles).

    For any user with role=tenant_admin or super_admin, the site_access
    stored in DynamoDB SHALL be an empty list, regardless of what site_access
    was provided in the creation request.

    Feature: list-users-endpoint, Property 4: site_access storage invariant

    **Validates: Requirements 1.3, 1.4**
    """
    with mock_aws():
        _dynamodb_client.cache_clear()
        _cognito_client.cache_clear()

        # Set up DynamoDB
        ddb_client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(ddb_client)

        tenant_id = "test_tenant"
        _seed_tenant(ddb_client, tenant_id)

        # Create Cognito user pool
        pool_id = _create_user_pool()
        os.environ["COGNITO_USER_POOL_ID"] = pool_id

        # Build request: super admin creating a user with an admin role
        # Note: site_access in the body is irrelevant for admin roles,
        # but we pass it to verify it gets ignored
        body: dict[str, Any] = {
            "email": "admin@example.com",
            "full_name": "Admin User",
            "role": role,
            "tenant_id": tenant_id,
        }
        # Include site_access in body if non-empty (handler ignores it for admin roles)
        if site_access_input:
            body["site_access"] = site_access_input

        event = _build_event(
            body=body,
            groups="SuperAdmins",
            tenant_id_claim=tenant_id,
        )

        result = handler(event, MagicMock())

        assert result["statusCode"] == 201
        response_body = json.loads(result["body"])
        user_sub = response_body["sub"]

        # Verify what was actually stored in DynamoDB
        stored_item = ddb_client.get_item(
            TableName=_TABLE_NAME,
            Key={
                "PK": {"S": f"TENANT#{tenant_id}"},
                "SK": {"S": f"USER#{user_sub}"},
            },
        ).get("Item")

        assert stored_item is not None
        # For admin roles, site_access MUST be an empty list in DynamoDB
        assert stored_item["site_access"] == {"L": []}


@given(site_ids=_SITE_ACCESS_LISTS)
@settings(max_examples=100, deadline=None)
def test_site_access_storage_invariant_user_role(site_ids: list[str]) -> None:
    """Property 4: site_access storage invariant (user role).

    For any user with role=user, the site_access stored in DynamoDB SHALL
    be the exact list of site IDs from the creation request.

    Feature: list-users-endpoint, Property 4: site_access storage invariant

    **Validates: Requirements 1.3, 1.4**
    """
    with mock_aws():
        _dynamodb_client.cache_clear()
        _cognito_client.cache_clear()

        # Set up DynamoDB
        ddb_client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(ddb_client)

        tenant_id = "test_tenant"
        _seed_tenant(ddb_client, tenant_id)

        # Seed all sites referenced in site_access
        for sid in site_ids:
            _seed_site(ddb_client, tenant_id, sid)

        # Create Cognito user pool
        pool_id = _create_user_pool()
        os.environ["COGNITO_USER_POOL_ID"] = pool_id

        # Build request: super admin creating a user with role=user
        body = {
            "email": "user@example.com",
            "full_name": "Test User",
            "role": "user",
            "tenant_id": tenant_id,
            "site_access": site_ids,
        }

        event = _build_event(
            body=body,
            groups="SuperAdmins",
            tenant_id_claim=tenant_id,
        )

        result = handler(event, MagicMock())

        assert result["statusCode"] == 201
        response_body = json.loads(result["body"])
        user_sub = response_body["sub"]

        # Verify what was actually stored in DynamoDB
        stored_item = ddb_client.get_item(
            TableName=_TABLE_NAME,
            Key={
                "PK": {"S": f"TENANT#{tenant_id}"},
                "SK": {"S": f"USER#{user_sub}"},
            },
        ).get("Item")

        assert stored_item is not None
        # For role=user, site_access MUST match the input list exactly
        expected_site_access = {"L": [{"S": sid} for sid in site_ids]}
        assert stored_item["site_access"] == expected_site_access
