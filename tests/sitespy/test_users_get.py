"""Unit tests for GET /v1/users handler.

Tests cover:
- Tenant admin lists users for own tenant (200 with users array)
- Super admin lists users for specified tenant (200 with users array)
- Empty tenant returns 200 with empty users array
- Super admin without tenant_id query param returns 400
- Unauthorized role (plain user) returns 403
- DynamoDB query failure returns 500
- Correlation ID echoed in response header
- CORS headers present on all responses

Requirements validated: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 3.1, 3.2, 3.3, 3.7
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from sitespy.data import _dynamodb_client
from sitespy.handlers.users_get import handler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    *,
    groups: str = "TenantAdmins",
    tenant_id_claim: str = "acme_corp",
    tenant_id_query: str | None = None,
    correlation_id: str = "test-corr-id",
) -> dict[str, Any]:
    """Build a minimal API Gateway proxy event for GET /v1/users."""
    query_params: dict[str, str] | None = None
    if tenant_id_query is not None:
        query_params = {"tenant_id": tenant_id_query}

    return {
        "httpMethod": "GET",
        "path": "/v1/users",
        "queryStringParameters": query_params,
        "headers": {"X-Correlation-Id": correlation_id},
        "body": None,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:groups": groups,
                    "custom:tenant_id": tenant_id_claim,
                }
            }
        },
    }


def _create_table(client) -> None:
    """Helper to create the test DynamoDB table."""
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


def _seed_user(
    client,
    tenant_id: str = "acme_corp",
    sub: str = "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    email: str = "jane.doe@acme.example.com",
    full_name: str = "Jane Doe",
    role: str = "user",
    site_access: list[str] | None = None,
) -> None:
    """Insert a User_Record directly into DynamoDB."""
    if site_access is None:
        site_access = ["site_001"]

    client.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": f"TENANT#{tenant_id}"},
            "SK": {"S": f"USER#{sub}"},
            "sub": {"S": sub},
            "email": {"S": email},
            "full_name": {"S": full_name},
            "tenant_id": {"S": tenant_id},
            "role": {"S": role},
            "site_access": {"L": [{"S": sid} for sid in site_access]},
        },
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_caches():
    """Clear cached boto3 clients before and after each test."""
    _dynamodb_client.cache_clear()
    yield
    _dynamodb_client.cache_clear()


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


class TestUsersGetHappyPath:
    @mock_aws
    def test_tenant_admin_lists_users_returns_200(self):
        """Tenant admin can list users for own tenant → 200 with users array.

        Requirements: 2.1, 2.3, 2.6, 2.7
        """
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_user(
            client,
            sub="user-sub-001",
            email="jane@acme.com",
            full_name="Jane Doe",
            role="user",
            site_access=["site_001", "site_002"],
        )
        _seed_user(
            client,
            sub="user-sub-002",
            email="admin@acme.com",
            full_name="Admin User",
            role="tenant_admin",
            site_access=[],
        )

        event = _make_event(groups="TenantAdmins", tenant_id_claim="acme_corp")
        result = handler(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert "users" in body
        assert len(body["users"]) == 2

        subs = {u["sub"] for u in body["users"]}
        assert subs == {"user-sub-001", "user-sub-002"}

    @mock_aws
    def test_super_admin_lists_users_returns_200(self):
        """Super admin can list users with tenant_id query param → 200.

        Requirements: 2.1, 2.4, 2.6, 2.7
        """
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_user(
            client,
            sub="user-sub-001",
            email="jane@acme.com",
            full_name="Jane Doe",
            role="user",
            site_access=["site_001"],
        )

        event = _make_event(groups="SuperAdmins", tenant_id_query="acme_corp")
        result = handler(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["users"]) == 1
        assert body["users"][0]["sub"] == "user-sub-001"
        assert body["users"][0]["email"] == "jane@acme.com"
        assert body["users"][0]["full_name"] == "Jane Doe"
        assert body["users"][0]["tenant_id"] == "acme_corp"
        assert body["users"][0]["role"] == "user"
        assert body["users"][0]["site_access"] == ["site_001"]

    @mock_aws
    def test_response_includes_all_user_fields(self):
        """Each user object includes sub, email, full_name, tenant_id, role, site_access.

        Requirements: 2.7
        """
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_user(
            client,
            sub="user-sub-001",
            email="jane@acme.com",
            full_name="Jane Doe",
            role="user",
            site_access=["site_001", "site_002"],
        )

        event = _make_event(groups="TenantAdmins", tenant_id_claim="acme_corp")
        result = handler(event, MagicMock())

        body = json.loads(result["body"])
        user = body["users"][0]
        expected_keys = {"sub", "email", "full_name", "tenant_id", "role", "site_access"}
        assert set(user.keys()) == expected_keys


# ---------------------------------------------------------------------------
# Empty tenant tests
# ---------------------------------------------------------------------------


class TestUsersGetEmptyTenant:
    @mock_aws
    def test_empty_tenant_returns_200_with_empty_array(self):
        """Tenant with no user records returns 200 with empty users array.

        Requirements: 2.8
        """
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        # No users seeded

        event = _make_event(groups="TenantAdmins", tenant_id_claim="acme_corp")
        result = handler(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["users"] == []


# ---------------------------------------------------------------------------
# Super admin without tenant_id tests
# ---------------------------------------------------------------------------


class TestUsersGetSuperAdminMissingTenantId:
    @mock_aws
    def test_super_admin_without_tenant_id_returns_400(self):
        """Super admin without tenant_id query parameter returns 400.

        Requirements: 2.5
        """
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        event = _make_event(groups="SuperAdmins", tenant_id_query=None)
        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"
        assert "tenant_id" in body["message"].lower()


# ---------------------------------------------------------------------------
# Authorization tests
# ---------------------------------------------------------------------------


class TestUsersGetAuthorization:
    @mock_aws
    def test_regular_user_returns_403(self):
        """Regular user (no admin group) returns 403 ACCESS_DENIED.

        Requirements: 2.2
        """
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        event = _make_event(groups="")
        result = handler(event, MagicMock())

        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "ACCESS_DENIED"

    @mock_aws
    def test_no_claims_returns_403(self):
        """Event with no authorizer claims returns 403.

        Requirements: 2.2
        """
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        event = _make_event()
        event["requestContext"] = {}
        result = handler(event, MagicMock())

        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "ACCESS_DENIED"


# ---------------------------------------------------------------------------
# DynamoDB query failure tests
# ---------------------------------------------------------------------------


class TestUsersGetDynamoDBFailure:
    @mock_aws
    def test_dynamodb_query_failure_returns_500(self):
        """DynamoDB query failure returns 500 INTERNAL_ERROR.

        Requirements: 2.6 (error path)
        """
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        event = _make_event(groups="TenantAdmins", tenant_id_claim="acme_corp")

        with patch(
            "sitespy.handlers.users_get.data.get_users_for_tenant",
            side_effect=Exception("DynamoDB query failed"),
        ):
            result = handler(event, MagicMock())

        assert result["statusCode"] == 500
        body = json.loads(result["body"])
        assert body["error"] == "INTERNAL_ERROR"


# ---------------------------------------------------------------------------
# Correlation ID tests
# ---------------------------------------------------------------------------


class TestUsersGetCorrelationId:
    @mock_aws
    def test_correlation_id_echoed_in_response(self):
        """The X-Correlation-Id header is echoed in the response.

        Requirements: 3.1, 3.3
        """
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        event = _make_event(correlation_id="my-corr-456")
        result = handler(event, MagicMock())

        assert result["headers"]["X-Correlation-Id"] == "my-corr-456"

    @mock_aws
    def test_missing_correlation_id_generates_uuid(self):
        """When X-Correlation-Id is absent, a UUID v4 is generated.

        Requirements: 3.2
        """
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        event = _make_event(groups="TenantAdmins", tenant_id_claim="acme_corp")
        event["headers"] = {}
        result = handler(event, MagicMock())

        corr_id = result["headers"]["X-Correlation-Id"]
        # UUID v4 format: 8-4-4-4-12 hex chars
        assert len(corr_id) == 36
        assert corr_id.count("-") == 4


# ---------------------------------------------------------------------------
# CORS headers tests
# ---------------------------------------------------------------------------


class TestUsersGetCorsHeaders:
    @mock_aws
    def test_cors_headers_on_success_response(self):
        """CORS headers are included on successful 200 responses.

        Requirements: 3.7
        """
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        event = _make_event(groups="TenantAdmins", tenant_id_claim="acme_corp")
        result = handler(event, MagicMock())

        assert result["headers"]["Access-Control-Allow-Origin"] == "*"
        assert "Content-Type" in result["headers"]["Access-Control-Allow-Headers"]
        assert "Authorization" in result["headers"]["Access-Control-Allow-Headers"]
        assert "X-Correlation-Id" in result["headers"]["Access-Control-Allow-Headers"]
        assert "GET" in result["headers"]["Access-Control-Allow-Methods"]
        assert "POST" in result["headers"]["Access-Control-Allow-Methods"]
        assert "PATCH" in result["headers"]["Access-Control-Allow-Methods"]
        assert "OPTIONS" in result["headers"]["Access-Control-Allow-Methods"]

    @mock_aws
    def test_cors_headers_on_error_response(self):
        """CORS headers are included on error responses (403).

        Requirements: 3.7
        """
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        event = _make_event(groups="")
        result = handler(event, MagicMock())

        assert result["statusCode"] == 403
        assert result["headers"]["Access-Control-Allow-Origin"] == "*"
        assert "Content-Type" in result["headers"]["Access-Control-Allow-Headers"]
        assert "GET" in result["headers"]["Access-Control-Allow-Methods"]

    @mock_aws
    def test_cors_headers_on_500_response(self):
        """CORS headers are included on 500 error responses.

        Requirements: 3.7
        """
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        event = _make_event(groups="TenantAdmins", tenant_id_claim="acme_corp")

        with patch(
            "sitespy.handlers.users_get.data.get_users_for_tenant",
            side_effect=Exception("DynamoDB query failed"),
        ):
            result = handler(event, MagicMock())

        assert result["statusCode"] == 500
        assert result["headers"]["Access-Control-Allow-Origin"] == "*"
        assert "Content-Type" in result["headers"]["Access-Control-Allow-Headers"]
        assert "GET" in result["headers"]["Access-Control-Allow-Methods"]
