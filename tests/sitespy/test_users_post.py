"""Unit tests for POST /v1/users handler.

Tests cover:
- Happy-path user creation → 201
- Tenant admin cannot create super_admin → 403
- Tenant admin cannot create cross-tenant user → 403
- Duplicate email → 409
- Missing site_access for role=user → 400
- Invalid site_id in site_access → 400
- DynamoDB User_Record write on successful creation
- DynamoDB write failure after Cognito success → 500
- site_access stored as list for role=user
- site_access stored as empty list for tenant_admin/super_admin

Requirements validated: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 4.1–4.17
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from sitespy.data import _dynamodb_client
from sitespy.handlers.users_post import _cognito_client, handler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    *,
    body: dict[str, Any] | str | None = None,
    groups: str = "SuperAdmins",
    caller_tenant_id: str = "acme_corp",
    correlation_id: str = "test-corr-id",
) -> dict[str, Any]:
    """Build a minimal API Gateway proxy event for POST /v1/users."""
    headers: dict[str, str] = {}
    if correlation_id:
        headers["X-Correlation-Id"] = correlation_id

    raw_body: str | None
    if body is None:
        raw_body = None
    elif isinstance(body, str):
        raw_body = body
    else:
        raw_body = json.dumps(body)

    return {
        "httpMethod": "POST",
        "path": "/v1/users",
        "headers": headers,
        "body": raw_body,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:groups": groups,
                    "custom:tenant_id": caller_tenant_id,
                }
            }
        },
    }


def _valid_user_body(**overrides: Any) -> dict[str, Any]:
    """Return a valid user creation request body with optional overrides."""
    base: dict[str, Any] = {
        "email": "jane.doe@acme.example.com",
        "full_name": "Jane Doe",
        "tenant_id": "acme_corp",
        "role": "user",
        "site_access": ["site_001"],
    }
    base.update(overrides)
    return base


def _create_table(client) -> None:
    """Create the test DynamoDB table."""
    client.create_table(
        TableName="test-data-table",
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


def _seed_tenant(client, tenant_id: str = "acme_corp") -> None:
    """Insert a tenant record."""
    client.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": f"TENANT#{tenant_id}"},
            "SK": {"S": f"TENANT#{tenant_id}"},
            "tenant_name": {"S": "Acme Corp"},
            "primary_contact_email": {"S": "ops@acme.com"},
            "stale_threshold_hours": {"N": "24"},
        },
    )


def _seed_site(client, tenant_id: str = "acme_corp", site_id: str = "site_001") -> None:
    """Insert a site record so site_access validation passes."""
    client.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": f"TENANT#{tenant_id}"},
            "SK": {"S": f"SITE#{site_id}"},
            "site_name": {"S": "Test Site"},
            "latitude": {"N": "51.5074"},
            "longitude": {"N": "-0.1278"},
            "timezone": {"S": "Europe/London"},
        },
    )


def _create_user_pool() -> str:
    """Create a Cognito user pool in moto and return its ID."""
    cognito = boto3.client("cognito-idp", region_name="eu-west-2")
    response = cognito.create_user_pool(
        PoolName="test-pool",
        Schema=[
            {"Name": "email", "AttributeDataType": "String", "Mutable": True},
            {"Name": "custom:tenant_id", "AttributeDataType": "String", "Mutable": True},
            {"Name": "custom:site_access", "AttributeDataType": "String", "Mutable": True},
        ],
    )
    pool_id = response["UserPool"]["Id"]

    # Create the TenantAdmins and SuperAdmins groups
    cognito.create_group(GroupName="TenantAdmins", UserPoolId=pool_id)
    cognito.create_group(GroupName="SuperAdmins", UserPoolId=pool_id)

    return pool_id


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_caches():
    """Clear cached boto3 clients before and after each test."""
    _dynamodb_client.cache_clear()
    _cognito_client.cache_clear()
    yield
    _dynamodb_client.cache_clear()
    _cognito_client.cache_clear()


@pytest.fixture(autouse=True)
def _set_cognito_env():
    """Set COGNITO_USER_POOL_ID env var — overwritten per test with real pool ID."""
    os.environ.setdefault("COGNITO_USER_POOL_ID", "placeholder")
    yield
    os.environ.pop("COGNITO_USER_POOL_ID", None)


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


class TestUsersPostHappyPath:
    @mock_aws
    def test_creates_user_returns_201(self):
        """Valid request creates user and returns 201 with all fields."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)

        pool_id = _create_user_pool()
        os.environ["COGNITO_USER_POOL_ID"] = pool_id

        body = _valid_user_body()
        event = _make_event(body=body)

        result = handler(event, MagicMock())

        assert result["statusCode"] == 201
        response_body = json.loads(result["body"])
        assert response_body["email"] == "jane.doe@acme.example.com"
        assert response_body["full_name"] == "Jane Doe"
        assert response_body["tenant_id"] == "acme_corp"
        assert response_body["role"] == "user"
        assert response_body["site_access"] == ["site_001"]
        assert "sub" in response_body

    @mock_aws
    def test_tenant_admin_creates_user_in_own_tenant(self):
        """Tenant admin can create a user in their own tenant."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)

        pool_id = _create_user_pool()
        os.environ["COGNITO_USER_POOL_ID"] = pool_id

        body = _valid_user_body(tenant_id="acme_corp")
        event = _make_event(body=body, groups="TenantAdmins", caller_tenant_id="acme_corp")

        result = handler(event, MagicMock())

        assert result["statusCode"] == 201
        response_body = json.loads(result["body"])
        assert response_body["tenant_id"] == "acme_corp"

    @mock_aws
    def test_tenant_admin_creates_tenant_admin_user(self):
        """Tenant admin can create a tenant_admin user (no site_access needed)."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)

        pool_id = _create_user_pool()
        os.environ["COGNITO_USER_POOL_ID"] = pool_id

        body = _valid_user_body(role="tenant_admin")
        del body["site_access"]
        event = _make_event(body=body, groups="TenantAdmins", caller_tenant_id="acme_corp")

        result = handler(event, MagicMock())

        assert result["statusCode"] == 201
        response_body = json.loads(result["body"])
        assert response_body["role"] == "tenant_admin"

    @mock_aws
    def test_super_admin_creates_user_in_any_tenant(self):
        """Super admin can create a user in any tenant."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client, "other_tenant")
        _seed_site(client, "other_tenant", "site_001")

        pool_id = _create_user_pool()
        os.environ["COGNITO_USER_POOL_ID"] = pool_id

        body = _valid_user_body(tenant_id="other_tenant")
        event = _make_event(body=body, groups="SuperAdmins", caller_tenant_id="acme_corp")

        result = handler(event, MagicMock())

        assert result["statusCode"] == 201
        response_body = json.loads(result["body"])
        assert response_body["tenant_id"] == "other_tenant"

    @mock_aws
    def test_correlation_id_echoed(self):
        """The X-Correlation-Id header is echoed in the response."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)

        pool_id = _create_user_pool()
        os.environ["COGNITO_USER_POOL_ID"] = pool_id

        body = _valid_user_body()
        event = _make_event(body=body, correlation_id="my-corr-123")

        result = handler(event, MagicMock())

        assert result["headers"]["X-Correlation-Id"] == "my-corr-123"

    @mock_aws
    def test_cors_headers_present(self):
        """CORS headers are included in the response."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)

        pool_id = _create_user_pool()
        os.environ["COGNITO_USER_POOL_ID"] = pool_id

        body = _valid_user_body()
        event = _make_event(body=body)

        result = handler(event, MagicMock())

        assert result["headers"]["Access-Control-Allow-Origin"] == "*"
        assert "Content-Type" in result["headers"]["Access-Control-Allow-Headers"]
        assert "POST" in result["headers"]["Access-Control-Allow-Methods"]


# ---------------------------------------------------------------------------
# Tenant admin cannot create super_admin tests
# ---------------------------------------------------------------------------


class TestUsersPostTenantAdminCannotCreateSuperAdmin:
    @mock_aws
    def test_tenant_admin_creating_super_admin_returns_403(self):
        """Tenant admin attempting to create a super_admin user returns 403."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)

        pool_id = _create_user_pool()
        os.environ["COGNITO_USER_POOL_ID"] = pool_id

        body = _valid_user_body(role="super_admin")
        event = _make_event(body=body, groups="TenantAdmins", caller_tenant_id="acme_corp")

        result = handler(event, MagicMock())

        assert result["statusCode"] == 403
        response_body = json.loads(result["body"])
        assert response_body["error"] == "ACCESS_DENIED"


# ---------------------------------------------------------------------------
# Tenant admin cannot create cross-tenant user tests
# ---------------------------------------------------------------------------


class TestUsersPostCrossTenantIsolation:
    @mock_aws
    def test_tenant_admin_cross_tenant_returns_403(self):
        """Tenant admin attempting to create user in another tenant returns 403."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client, "other_tenant")
        _seed_site(client, "other_tenant", "site_001")

        pool_id = _create_user_pool()
        os.environ["COGNITO_USER_POOL_ID"] = pool_id

        body = _valid_user_body(tenant_id="other_tenant")
        event = _make_event(body=body, groups="TenantAdmins", caller_tenant_id="acme_corp")

        result = handler(event, MagicMock())

        assert result["statusCode"] == 403
        response_body = json.loads(result["body"])
        assert response_body["error"] == "ACCESS_DENIED"


# ---------------------------------------------------------------------------
# Duplicate email tests
# ---------------------------------------------------------------------------


class TestUsersPostDuplicateEmail:
    @mock_aws
    def test_duplicate_email_returns_409(self):
        """Creating a user with an existing email returns 409."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)

        pool_id = _create_user_pool()
        os.environ["COGNITO_USER_POOL_ID"] = pool_id

        body = _valid_user_body()
        event = _make_event(body=body)

        # First creation succeeds
        result1 = handler(event, MagicMock())
        assert result1["statusCode"] == 201

        # Clear caches so second call gets fresh clients within moto
        _dynamodb_client.cache_clear()
        _cognito_client.cache_clear()

        # Second creation with same email returns 409
        result2 = handler(event, MagicMock())
        assert result2["statusCode"] == 409
        response_body = json.loads(result2["body"])
        assert response_body["error"] == "CONFLICT"


# ---------------------------------------------------------------------------
# Missing site_access for role=user tests
# ---------------------------------------------------------------------------


class TestUsersPostMissingSiteAccess:
    @mock_aws
    def test_missing_site_access_for_user_role_returns_400(self):
        """When role is 'user' and site_access is missing, returns 400."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)

        pool_id = _create_user_pool()
        os.environ["COGNITO_USER_POOL_ID"] = pool_id

        body = _valid_user_body()
        del body["site_access"]
        event = _make_event(body=body)

        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        response_body = json.loads(result["body"])
        assert response_body["error"] == "BAD_REQUEST"
        assert "site_access" in response_body["message"]

    @mock_aws
    def test_empty_site_access_for_user_role_returns_400(self):
        """When role is 'user' and site_access is an empty list, returns 400."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)

        pool_id = _create_user_pool()
        os.environ["COGNITO_USER_POOL_ID"] = pool_id

        body = _valid_user_body(site_access=[])
        event = _make_event(body=body)

        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        response_body = json.loads(result["body"])
        assert response_body["error"] == "BAD_REQUEST"
        assert "site_access" in response_body["message"]


# ---------------------------------------------------------------------------
# Invalid site_id in site_access tests
# ---------------------------------------------------------------------------


class TestUsersPostInvalidSiteAccess:
    @mock_aws
    def test_nonexistent_site_in_site_access_returns_400(self):
        """site_access containing a site_id that doesn't exist returns 400."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        # Do NOT seed site — it should not exist

        pool_id = _create_user_pool()
        os.environ["COGNITO_USER_POOL_ID"] = pool_id

        body = _valid_user_body(site_access=["nonexistent_site"])
        event = _make_event(body=body)

        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        response_body = json.loads(result["body"])
        assert response_body["error"] == "BAD_REQUEST"
        assert "nonexistent_site" in response_body["message"]

    @mock_aws
    def test_invalid_format_site_id_in_site_access_returns_400(self):
        """site_access containing a site_id with invalid format returns 400."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)

        pool_id = _create_user_pool()
        os.environ["COGNITO_USER_POOL_ID"] = pool_id

        body = _valid_user_body(site_access=["INVALID-SITE!"])
        event = _make_event(body=body)

        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        response_body = json.loads(result["body"])
        assert response_body["error"] == "BAD_REQUEST"


# ---------------------------------------------------------------------------
# Authorization tests
# ---------------------------------------------------------------------------


class TestUsersPostAuthorization:
    @mock_aws
    def test_regular_user_returns_403(self):
        """Regular user (no admin group) returns 403 ACCESS_DENIED."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        event = _make_event(body=_valid_user_body(), groups="")

        result = handler(event, MagicMock())

        assert result["statusCode"] == 403
        response_body = json.loads(result["body"])
        assert response_body["error"] == "ACCESS_DENIED"

    @mock_aws
    def test_no_claims_returns_403(self):
        """Event with no authorizer claims returns 403."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        event = _make_event(body=_valid_user_body())
        event["requestContext"] = {}

        result = handler(event, MagicMock())

        assert result["statusCode"] == 403
        response_body = json.loads(result["body"])
        assert response_body["error"] == "ACCESS_DENIED"


# ---------------------------------------------------------------------------
# DynamoDB User_Record write tests
# Requirements validated: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6
# ---------------------------------------------------------------------------


class TestUsersPostDynamoDBWrite:
    """Tests for the DynamoDB User_Record write on successful user creation."""

    @mock_aws
    def test_successful_creation_writes_user_record_to_dynamodb(self):
        """After successful Cognito creation, a User_Record is written to DynamoDB.

        Requirements: 1.1, 1.2, 1.5
        """
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)

        pool_id = _create_user_pool()
        os.environ["COGNITO_USER_POOL_ID"] = pool_id

        body = _valid_user_body()
        event = _make_event(body=body)

        result = handler(event, MagicMock())

        assert result["statusCode"] == 201
        response_body = json.loads(result["body"])
        user_sub = response_body["sub"]

        # Verify the User_Record was written to DynamoDB
        _dynamodb_client.cache_clear()
        ddb = boto3.client("dynamodb", region_name="eu-west-2")
        item_response = ddb.get_item(
            TableName="test-data-table",
            Key={
                "PK": {"S": "TENANT#acme_corp"},
                "SK": {"S": f"USER#{user_sub}"},
            },
        )
        item = item_response.get("Item")
        assert item is not None
        assert item["PK"]["S"] == "TENANT#acme_corp"
        assert item["SK"]["S"] == f"USER#{user_sub}"
        assert item["sub"]["S"] == user_sub
        assert item["email"]["S"] == "jane.doe@acme.example.com"
        assert item["full_name"]["S"] == "Jane Doe"
        assert item["tenant_id"]["S"] == "acme_corp"
        assert item["role"]["S"] == "user"
        assert item["site_access"]["L"] == [{"S": "site_001"}]

    @mock_aws
    def test_dynamodb_write_failure_after_cognito_success_returns_500(self):
        """If DynamoDB write fails after Cognito user creation, returns 500.

        Requirements: 1.6
        """
        from unittest.mock import patch

        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)

        pool_id = _create_user_pool()
        os.environ["COGNITO_USER_POOL_ID"] = pool_id

        body = _valid_user_body()
        event = _make_event(body=body)

        # Patch data.put_user to raise an exception (simulating DynamoDB failure)
        with patch("sitespy.handlers.users_post.data.put_user", side_effect=Exception("DynamoDB write failed")):
            result = handler(event, MagicMock())

        assert result["statusCode"] == 500
        response_body = json.loads(result["body"])
        assert response_body["error"] == "INTERNAL_ERROR"

    @mock_aws
    def test_site_access_stored_as_list_for_role_user(self):
        """When role is 'user', site_access is stored as a list of site IDs.

        Requirements: 1.3
        """
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client, "acme_corp", "site_001")
        _seed_site(client, "acme_corp", "site_002")

        pool_id = _create_user_pool()
        os.environ["COGNITO_USER_POOL_ID"] = pool_id

        body = _valid_user_body(site_access=["site_001", "site_002"])
        event = _make_event(body=body)

        result = handler(event, MagicMock())

        assert result["statusCode"] == 201
        response_body = json.loads(result["body"])
        user_sub = response_body["sub"]

        # Verify site_access is stored as a list in DynamoDB
        _dynamodb_client.cache_clear()
        ddb = boto3.client("dynamodb", region_name="eu-west-2")
        item_response = ddb.get_item(
            TableName="test-data-table",
            Key={
                "PK": {"S": "TENANT#acme_corp"},
                "SK": {"S": f"USER#{user_sub}"},
            },
        )
        item = item_response.get("Item")
        assert item is not None
        assert item["site_access"]["L"] == [{"S": "site_001"}, {"S": "site_002"}]

    @mock_aws
    def test_site_access_stored_as_empty_list_for_tenant_admin(self):
        """When role is 'tenant_admin', site_access is stored as an empty list.

        Requirements: 1.4
        """
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)

        pool_id = _create_user_pool()
        os.environ["COGNITO_USER_POOL_ID"] = pool_id

        body = _valid_user_body(role="tenant_admin")
        del body["site_access"]
        event = _make_event(body=body, groups="TenantAdmins", caller_tenant_id="acme_corp")

        result = handler(event, MagicMock())

        assert result["statusCode"] == 201
        response_body = json.loads(result["body"])
        user_sub = response_body["sub"]

        # Verify site_access is stored as an empty list in DynamoDB
        _dynamodb_client.cache_clear()
        ddb = boto3.client("dynamodb", region_name="eu-west-2")
        item_response = ddb.get_item(
            TableName="test-data-table",
            Key={
                "PK": {"S": "TENANT#acme_corp"},
                "SK": {"S": f"USER#{user_sub}"},
            },
        )
        item = item_response.get("Item")
        assert item is not None
        assert item["site_access"]["L"] == []

    @mock_aws
    def test_site_access_stored_as_empty_list_for_super_admin(self):
        """When role is 'super_admin', site_access is stored as an empty list.

        Requirements: 1.4
        """
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)

        pool_id = _create_user_pool()
        os.environ["COGNITO_USER_POOL_ID"] = pool_id

        body = _valid_user_body(role="super_admin", tenant_id="acme_corp")
        del body["site_access"]
        event = _make_event(body=body, groups="SuperAdmins", caller_tenant_id="acme_corp")

        result = handler(event, MagicMock())

        assert result["statusCode"] == 201
        response_body = json.loads(result["body"])
        user_sub = response_body["sub"]

        # Verify site_access is stored as an empty list in DynamoDB
        _dynamodb_client.cache_clear()
        ddb = boto3.client("dynamodb", region_name="eu-west-2")
        item_response = ddb.get_item(
            TableName="test-data-table",
            Key={
                "PK": {"S": "TENANT#acme_corp"},
                "SK": {"S": f"USER#{user_sub}"},
            },
        )
        item = item_response.get("Item")
        assert item is not None
        assert item["site_access"]["L"] == []
