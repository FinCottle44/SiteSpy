"""Unit tests for POST /v1/sites/{site_id}/cameras/{camera_id}/rotate-credentials handler.

Tests cover:
- Happy-path rotation → 200 with new ingest token
- Non-existent camera → 404
- Tenant admin accessing other tenant's camera → 403
- Super admin without tenant_id → 400
- Regular user → 403
- Token format validation

Requirements validated: 6.1–6.10
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
from sitespy.handlers.cameras_rotate import handler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    *,
    groups: str = "TenantAdmins",
    tenant_id_claim: str = "acme_corp",
    tenant_id_query: str | None = None,
    site_id_path: str = "site_001",
    camera_id_path: str = "cam_01",
    correlation_id: str = "test-corr-id",
) -> dict[str, Any]:
    """Build a minimal API Gateway proxy event for POST .../rotate-credentials."""
    query_params: dict[str, str] | None = None
    if tenant_id_query is not None:
        query_params = {"tenant_id": tenant_id_query}

    path_params: dict[str, str] = {}
    if site_id_path is not None:
        path_params["site_id"] = site_id_path
    if camera_id_path is not None:
        path_params["camera_id"] = camera_id_path

    return {
        "httpMethod": "POST",
        "path": f"/v1/sites/{site_id_path}/cameras/{camera_id_path}/rotate-credentials",
        "queryStringParameters": query_params,
        "pathParameters": path_params if path_params else None,
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
    """Insert a site record so the handler can verify site existence."""
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


def _seed_camera(
    client,
    tenant_id: str = "acme_corp",
    site_id: str = "site_001",
    camera_id: str = "cam_01",
    ingest_token: str = "tk_oldtoken1234567890abcdefghijklmnopqrst",
) -> None:
    """Insert a camera record directly into DynamoDB."""
    client.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": f"TENANT#{tenant_id}"},
            "SK": {"S": f"SITE#{site_id}#CAM#{camera_id}"},
            "GSI1PK": {"S": f"TOKEN#{ingest_token}"},
            "GSI1SK": {"S": "CAMERA"},
            "camera_name": {"S": "North elevation"},
            "camera_model": {"S": "Axis P1455-LE"},
            "ingest_token": {"S": ingest_token},
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


@pytest.fixture(autouse=True)
def _set_ingest_base_url():
    """Set the INGEST_BASE_URL env var for tests."""
    os.environ["INGEST_BASE_URL"] = "https://api.example.com"
    yield
    os.environ.pop("INGEST_BASE_URL", None)


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


class TestCamerasRotateHappyPath:
    @mock_aws
    def test_tenant_admin_rotates_token_returns_200(self):
        """Tenant admin can rotate token for a camera in their tenant → 200."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)
        _seed_camera(client)

        event = _make_event(groups="TenantAdmins", tenant_id_claim="acme_corp")
        result = handler(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["camera_id"] == "cam_01"
        assert "ingest_token" in body
        assert "ingest_url" in body

    @mock_aws
    def test_super_admin_rotates_token_returns_200(self):
        """Super admin can rotate token with tenant_id query param → 200."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)
        _seed_camera(client)

        event = _make_event(groups="SuperAdmins", tenant_id_query="acme_corp")
        result = handler(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["camera_id"] == "cam_01"
        assert "ingest_token" in body

    @mock_aws
    def test_new_token_differs_from_old(self):
        """The new ingest token is different from the original."""
        old_token = "tk_oldtoken1234567890abcdefghijklmnopqrst"
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)
        _seed_camera(client, ingest_token=old_token)

        event = _make_event(groups="TenantAdmins", tenant_id_claim="acme_corp")
        result = handler(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["ingest_token"] != old_token

    @mock_aws
    def test_ingest_url_contains_new_token(self):
        """The ingest_url includes the new ingest token."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)
        _seed_camera(client)

        event = _make_event(groups="TenantAdmins", tenant_id_claim="acme_corp")
        result = handler(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["ingest_token"] in body["ingest_url"]
        assert body["ingest_url"].startswith("https://api.example.com/v1/ingest/")

    @mock_aws
    def test_token_format(self):
        """The new token starts with tk_ prefix and has expected length."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)
        _seed_camera(client)

        event = _make_event(groups="TenantAdmins", tenant_id_claim="acme_corp")
        result = handler(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        token = body["ingest_token"]
        assert token.startswith("tk_")
        assert len(token) == 43  # tk_ + 40 random chars

    @mock_aws
    def test_correlation_id_echoed(self):
        """The X-Correlation-Id header is echoed in the response."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)
        _seed_camera(client)

        event = _make_event(correlation_id="my-corr-789")
        result = handler(event, MagicMock())

        assert result["headers"]["X-Correlation-Id"] == "my-corr-789"

    @mock_aws
    def test_cors_headers_present(self):
        """CORS headers are included in the response."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)
        _seed_camera(client)

        event = _make_event(groups="TenantAdmins", tenant_id_claim="acme_corp")
        result = handler(event, MagicMock())

        assert result["headers"]["Access-Control-Allow-Origin"] == "*"
        assert "Content-Type" in result["headers"]["Access-Control-Allow-Headers"]
        assert "POST" in result["headers"]["Access-Control-Allow-Methods"]


# ---------------------------------------------------------------------------
# Non-existent camera tests
# ---------------------------------------------------------------------------


class TestCamerasRotateNotFound:
    @mock_aws
    def test_non_existent_camera_returns_404(self):
        """Camera that doesn't exist returns 404."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)
        # Do NOT seed camera — it should not exist

        event = _make_event(
            groups="TenantAdmins",
            tenant_id_claim="acme_corp",
            camera_id_path="nonexistent_cam",
        )
        result = handler(event, MagicMock())

        assert result["statusCode"] == 404
        body = json.loads(result["body"])
        assert body["error"] == "NOT_FOUND"

    @mock_aws
    def test_non_existent_site_for_tenant_admin_returns_403(self):
        """Tenant admin with non-existent site returns 403 (site doesn't belong to tenant)."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        # Do NOT seed site — it should not exist

        event = _make_event(
            groups="TenantAdmins",
            tenant_id_claim="acme_corp",
            site_id_path="nonexistent_site",
        )
        result = handler(event, MagicMock())

        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "ACCESS_DENIED"


# ---------------------------------------------------------------------------
# Tenant admin cross-tenant isolation tests
# ---------------------------------------------------------------------------


class TestCamerasRotateCrossTenant:
    @mock_aws
    def test_tenant_admin_accessing_other_tenants_camera_returns_403(self):
        """Tenant admin cannot rotate token for a camera in another tenant."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        # Seed data for "other_corp" tenant
        _seed_tenant(client, tenant_id="other_corp")
        _seed_site(client, tenant_id="other_corp", site_id="site_001")
        _seed_camera(client, tenant_id="other_corp", site_id="site_001", camera_id="cam_01")

        # Caller is tenant admin for "acme_corp" but site_001 belongs to "other_corp"
        event = _make_event(
            groups="TenantAdmins",
            tenant_id_claim="acme_corp",
            site_id_path="site_001",
            camera_id_path="cam_01",
        )
        result = handler(event, MagicMock())

        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "ACCESS_DENIED"


# ---------------------------------------------------------------------------
# Authorization tests
# ---------------------------------------------------------------------------


class TestCamerasRotateAuthorization:
    @mock_aws
    def test_regular_user_returns_403(self):
        """Regular user caller returns 403 ACCESS_DENIED."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)
        _seed_camera(client)

        event = _make_event(groups="")
        result = handler(event, MagicMock())

        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "ACCESS_DENIED"

    @mock_aws
    def test_no_claims_returns_403(self):
        """Event with no authorizer claims returns 403."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)
        _seed_camera(client)

        event = _make_event()
        event["requestContext"] = {}
        result = handler(event, MagicMock())

        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "ACCESS_DENIED"

    @mock_aws
    def test_super_admin_without_tenant_id_returns_400(self):
        """Super admin without tenant_id query parameter returns 400."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)
        _seed_camera(client)

        event = _make_event(groups="SuperAdmins", tenant_id_query=None)
        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"
        assert "tenant_id" in body["message"].lower()
