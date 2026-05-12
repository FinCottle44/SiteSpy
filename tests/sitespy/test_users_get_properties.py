"""Property-based tests for the users_get handler.

Feature: list-users-endpoint
Property 1: Role enforcement rejects unauthorized callers
Property 2: Tenant ID resolution by role
Property 5: Query-to-response mapping preserves all fields

**Validates: Requirements 2.2, 2.3, 2.4, 2.5, 2.6, 2.7**
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import MagicMock, patch

import boto3
import pytest
from hypothesis import given, settings, HealthCheck
from hypothesis import strategies as st
from moto import mock_aws

from sitespy.data import _dynamodb_client, put_user
from sitespy.handlers.users_get import handler

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


def _seed_user(client, tenant_id: str, sub: str) -> None:
    """Insert a user record for the given tenant."""
    client.put_item(
        TableName=_TABLE_NAME,
        Item={
            "PK": {"S": f"TENANT#{tenant_id}"},
            "SK": {"S": f"USER#{sub}"},
            "sub": {"S": sub},
            "email": {"S": f"{sub}@example.com"},
            "full_name": {"S": "Test User"},
            "tenant_id": {"S": tenant_id},
            "role": {"S": "user"},
            "site_access": {"L": []},
        },
    )


def _build_event(
    *,
    groups: str | list[str],
    tenant_id_claim: str = "",
    query_params: dict[str, str] | None = None,
    correlation_id: str = "test-corr-id",
) -> dict[str, Any]:
    """Build an API Gateway proxy event for GET /v1/users."""
    claims: dict[str, Any] = {"cognito:groups": groups}
    if tenant_id_claim:
        claims["custom:tenant_id"] = tenant_id_claim

    return {
        "httpMethod": "GET",
        "path": "/v1/users",
        "headers": {"X-Correlation-Id": correlation_id},
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

# Valid tenant IDs matching ^[a-z0-9_]{3,32}$
_VALID_TENANT_IDS = st.from_regex(r"[a-z0-9_]{3,32}", fullmatch=True)

# Generate group strings that do NOT contain SuperAdmins or TenantAdmins.
# This covers: empty string, "user", random group names, lists without admin groups.
_NON_ADMIN_GROUP_NAMES = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "N"),
        whitelist_characters="_-",
    ),
    min_size=1,
    max_size=30,
).filter(lambda s: s not in ("SuperAdmins", "TenantAdmins"))

# Strategy for cognito:groups claim values that represent unauthorized callers.
# Can be: empty string, a single non-admin group, or a list of non-admin groups.
_UNAUTHORIZED_GROUPS = st.one_of(
    # Empty string (no groups)
    st.just(""),
    # Single non-admin group name
    _NON_ADMIN_GROUP_NAMES,
    # Comma-separated list of non-admin groups
    st.lists(_NON_ADMIN_GROUP_NAMES, min_size=1, max_size=3, unique=True).map(
        lambda groups: ",".join(groups)
    ),
    # List type (as API Gateway sometimes passes lists)
    st.lists(_NON_ADMIN_GROUP_NAMES, min_size=0, max_size=3, unique=True),
)


# ---------------------------------------------------------------------------
# Property 1: Role enforcement rejects unauthorized callers
# Validates: Requirements 2.2
#
# For any GET /v1/users request where the caller's JWT cognito:groups does
# not contain SuperAdmins or TenantAdmins, the handler SHALL return a 403
# response with error key ACCESS_DENIED and SHALL NOT perform any DynamoDB
# query.
# ---------------------------------------------------------------------------


@given(
    groups=_UNAUTHORIZED_GROUPS,
    tenant_id=_VALID_TENANT_IDS,
)
@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_role_enforcement_rejects_unauthorized_callers(
    groups: str | list[str],
    tenant_id: str,
) -> None:
    """Property 1: Role enforcement rejects unauthorized callers.

    For any GET /v1/users request where the caller's JWT cognito:groups
    does not contain SuperAdmins or TenantAdmins, the handler SHALL return
    a 403 response with error key ACCESS_DENIED and SHALL NOT perform any
    DynamoDB query.

    Feature: list-users-endpoint, Property 1: Role enforcement rejects unauthorized callers

    **Validates: Requirements 2.2**
    """
    event = _build_event(
        groups=groups,
        tenant_id_claim=tenant_id,
        query_params={"tenant_id": tenant_id},
    )

    # Patch data.get_users_for_tenant to verify it is NOT called
    with patch("sitespy.handlers.users_get.data.get_users_for_tenant") as mock_query:
        result = handler(event, MagicMock())

    # Assert 403 with ACCESS_DENIED
    assert result["statusCode"] == 403
    response_body = json.loads(result["body"])
    assert response_body["error"] == "ACCESS_DENIED"

    # Assert DynamoDB query was NOT performed
    mock_query.assert_not_called()


# ---------------------------------------------------------------------------
# Property 2: Tenant ID resolution by role
# Validates: Requirements 2.3, 2.4, 2.5
#
# For any authorized request:
# - If the caller is a tenant_admin, the tenant_id used for the DynamoDB
#   query SHALL equal the caller's JWT custom:tenant_id claim.
# - If the caller is a super_admin, the tenant_id SHALL equal the tenant_id
#   query parameter.
# - For any super_admin request where the tenant_id query parameter is absent
#   or empty, the handler SHALL return a 400 response with error key BAD_REQUEST.
# ---------------------------------------------------------------------------


@given(tenant_id=_VALID_TENANT_IDS)
@settings(max_examples=100, deadline=None)
def test_tenant_admin_resolves_tenant_id_from_jwt_claim(tenant_id: str) -> None:
    """Property 2: Tenant ID resolution — tenant_admin uses JWT claim.

    For any tenant_admin caller, the tenant_id used for the DynamoDB query
    SHALL equal the caller's JWT custom:tenant_id claim.

    Feature: list-users-endpoint, Property 2: Tenant ID resolution by role

    **Validates: Requirements 2.3**
    """
    with mock_aws():
        _dynamodb_client.cache_clear()
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        # Seed a user record so we can verify the correct tenant was queried
        test_sub = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        _seed_user(client, tenant_id, test_sub)

        # Build event: tenant_admin with custom:tenant_id in JWT
        event = _build_event(
            groups="TenantAdmins",
            tenant_id_claim=tenant_id,
        )

        result = handler(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])

        # The response should contain users from the tenant matching the JWT claim
        assert "users" in body
        # Verify the user we seeded for this tenant_id is returned
        assert len(body["users"]) == 1
        assert body["users"][0]["tenant_id"] == tenant_id
        assert body["users"][0]["sub"] == test_sub


@given(tenant_id=_VALID_TENANT_IDS)
@settings(max_examples=100, deadline=None)
def test_super_admin_resolves_tenant_id_from_query_param(tenant_id: str) -> None:
    """Property 2: Tenant ID resolution — super_admin uses query parameter.

    For any super_admin caller, the tenant_id used for the DynamoDB query
    SHALL equal the tenant_id query parameter.

    Feature: list-users-endpoint, Property 2: Tenant ID resolution by role

    **Validates: Requirements 2.4**
    """
    with mock_aws():
        _dynamodb_client.cache_clear()
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        # Seed a user record so we can verify the correct tenant was queried
        test_sub = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        _seed_user(client, tenant_id, test_sub)

        # Build event: super_admin with tenant_id as query parameter
        event = _build_event(
            groups="SuperAdmins",
            query_params={"tenant_id": tenant_id},
        )

        result = handler(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])

        # The response should contain users from the tenant matching the query param
        assert "users" in body
        assert len(body["users"]) == 1
        assert body["users"][0]["tenant_id"] == tenant_id
        assert body["users"][0]["sub"] == test_sub


@given(tenant_id_claim=_VALID_TENANT_IDS)
@settings(max_examples=100, deadline=None)
def test_super_admin_without_tenant_id_query_param_returns_400(
    tenant_id_claim: str,
) -> None:
    """Property 2: Tenant ID resolution — super_admin missing tenant_id returns 400.

    For any super_admin request where the tenant_id query parameter is absent
    or empty, the handler SHALL return a 400 response with error key BAD_REQUEST.

    Feature: list-users-endpoint, Property 2: Tenant ID resolution by role

    **Validates: Requirements 2.5**
    """
    with mock_aws():
        _dynamodb_client.cache_clear()
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        # Build event: super_admin WITHOUT tenant_id query parameter
        event = _build_event(
            groups="SuperAdmins",
            tenant_id_claim=tenant_id_claim,
            query_params=None,  # No query params at all
        )

        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"


# Strategy for empty/whitespace-only strings that should be treated as absent
_EMPTY_OR_WHITESPACE = st.sampled_from(["", " ", "  ", "\t", "\n", "   "])


@given(empty_value=_EMPTY_OR_WHITESPACE)
@settings(max_examples=100, deadline=None)
def test_super_admin_with_empty_tenant_id_query_param_returns_400(
    empty_value: str,
) -> None:
    """Property 2: Tenant ID resolution — super_admin with empty tenant_id returns 400.

    For any super_admin request where the tenant_id query parameter is empty
    or whitespace-only, the handler SHALL return a 400 response with error
    key BAD_REQUEST.

    Feature: list-users-endpoint, Property 2: Tenant ID resolution by role

    **Validates: Requirements 2.5**
    """
    with mock_aws():
        _dynamodb_client.cache_clear()
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        # Build event: super_admin with empty/whitespace tenant_id query param
        event = _build_event(
            groups="SuperAdmins",
            query_params={"tenant_id": empty_value},
        )

        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"


# ---------------------------------------------------------------------------
# Strategies for Property 5
# ---------------------------------------------------------------------------

# Valid Cognito sub (UUID-like strings)
_VALID_SUBS = st.from_regex(
    r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}",
    fullmatch=True,
)

# Valid email addresses
_VALID_EMAILS = st.from_regex(r"[a-z]{1,10}@[a-z]{1,10}\.[a-z]{2,4}", fullmatch=True)

# Valid full names (non-empty printable strings)
_VALID_FULL_NAMES = st.text(
    alphabet=st.characters(
        whitelist_categories=("L", "Zs"),
        whitelist_characters="-'.",
    ),
    min_size=1,
    max_size=50,
).filter(lambda s: s.strip())

# Valid roles
_VALID_ROLES = st.sampled_from(["user", "tenant_admin", "super_admin"])

# Valid site IDs for site_access lists
_VALID_SITE_IDS = st.from_regex(r"[a-z0-9_]{1,20}", fullmatch=True)

# site_access lists (0 to 5 items)
_VALID_SITE_ACCESS = st.lists(_VALID_SITE_IDS, min_size=0, max_size=5, unique=True)

# All fields that must be present in each user response object
_REQUIRED_USER_FIELDS = {"sub", "email", "full_name", "tenant_id", "role", "site_access"}

# A single user record strategy
_USER_RECORD = st.fixed_dictionaries({
    "sub": _VALID_SUBS,
    "email": _VALID_EMAILS,
    "full_name": _VALID_FULL_NAMES,
    "role": _VALID_ROLES,
    "site_access": _VALID_SITE_ACCESS,
})

# Lists of user records (1 to 5 users per test, unique subs)
_USER_LISTS = st.lists(
    _USER_RECORD,
    min_size=1,
    max_size=5,
    unique_by=lambda u: u["sub"],
)


# ---------------------------------------------------------------------------
# Property 5: Query-to-response mapping preserves all fields
# Validates: Requirements 2.6, 2.7
#
# For any set of User_Record items stored in DynamoDB for a tenant,
# calling GET /v1/users returns a response where every user object
# contains all expected fields (sub, email, full_name, tenant_id, role,
# site_access) and the values match what was stored.
# ---------------------------------------------------------------------------


@given(
    tenant_id=_VALID_TENANT_IDS,
    users=_USER_LISTS,
)
@settings(max_examples=100, deadline=None)
def test_query_to_response_mapping_preserves_all_fields(
    tenant_id: str,
    users: list[dict[str, Any]],
) -> None:
    """Property 5: Query-to-response mapping preserves all fields.

    For any set of User_Record items returned by the DynamoDB query for a
    tenant, the GET /v1/users response SHALL contain a users array with
    exactly one element per DynamoDB item, and each element SHALL include
    sub, email, full_name, tenant_id, role, and site_access with values
    matching the stored item.

    Feature: list-users-endpoint, Property 5: Query-to-response mapping preserves all fields

    **Validates: Requirements 2.6, 2.7**
    """
    with mock_aws():
        _dynamodb_client.cache_clear()

        # Set up DynamoDB table
        ddb_client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(ddb_client)

        # Seed user records directly into DynamoDB using put_user
        for user in users:
            put_user(
                tenant_id=tenant_id,
                sub=user["sub"],
                email=user["email"],
                full_name=user["full_name"],
                role=user["role"],
                site_access=user["site_access"],
            )

        # Call GET /v1/users as super_admin with tenant_id query param
        event = _build_event(
            groups="SuperAdmins",
            query_params={"tenant_id": tenant_id},
        )
        result = handler(event, MagicMock())

        # Should return 200
        assert result["statusCode"] == 200

        body = json.loads(result["body"])
        assert "users" in body

        response_users = body["users"]

        # Response should contain exactly one element per stored user
        assert len(response_users) == len(users)

        # Build a lookup by sub for easy comparison
        response_by_sub = {u["sub"]: u for u in response_users}

        for stored_user in users:
            sub = stored_user["sub"]
            assert sub in response_by_sub, (
                f"User with sub={sub} not found in response"
            )

            response_user = response_by_sub[sub]

            # Every response object must contain all required fields
            assert set(response_user.keys()) == _REQUIRED_USER_FIELDS, (
                f"Response user missing fields. Got: {set(response_user.keys())}"
            )

            # Values must match what was stored
            assert response_user["sub"] == stored_user["sub"]
            assert response_user["email"] == stored_user["email"]
            assert response_user["full_name"] == stored_user["full_name"]
            assert response_user["tenant_id"] == tenant_id
            assert response_user["role"] == stored_user["role"]
            assert response_user["site_access"] == stored_user["site_access"]
