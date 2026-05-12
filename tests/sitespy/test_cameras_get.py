"""Unit tests for GET /v1/sites/{site_id}/cameras handler.

Tests cover:
- Happy-path listing → 200 with cameras array
- Empty site → 200 with empty array
- Non-existent site → 404
- Super admin without tenant_id → 400
- No credential fields in response
- Tenant admin resolves tenant_id from JWT claims
- Non-authorized caller → 403

Requirements validated: 5.1–5.10
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from sitespy.data import _dynamodb_client
from sitespy.handlers.cameras_get import handler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    *,
    groups: str = "TenantAdmins",
    tenant_id_claim: str = "acme_corp",
    tenant_id_query: str | None = None,
    site_id_path: str = "site_001",
    correlation_id: str = "test-corr-id",
) -> dict[str, Any]:
    """Build a minimal API Gateway proxy event for GET /v1/sites/{site_id}/cameras."""
    query_params: dict[str, str] | None = None
    if tenant_id_query is not None:
        query_params = {"tenant_id": tenant_id_query}

    path_params: dict[str, str] | None = None
    if site_id_path is not None:
        path_params = {"site_id": site_id_path}

    return {
        "httpMethod": "GET",
        "path": f"/v1/sites/{site_id_path}/cameras",
        "queryStringParameters": query_params,
        "pathParameters": path_params,
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
    camera_name: str = "North elevation",
    camera_model: str | None = "Axis P1455-LE",
) -> None:
    """Insert a camera record directly into DynamoDB."""
    item: dict[str, Any] = {
        "PK": {"S": f"TENANT#{tenant_id}"},
        "SK": {"S": f"SITE#{site_id}#CAM#{camera_id}"},
        "camera_name": {"S": camera_name},
    }
    if camera_model is not None:
        item["camera_model"] = {"S": camera_model}
    client.put_item(TableName="test-data-table", Item=item)


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


class TestCamerasGetHappyPath:
    @mock_aws
    def test_tenant_admin_lists_cameras_returns_200(self):
        """Tenant admin can list cameras for a site → 200 with cameras array."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)
        _seed_camera(client, camera_id="cam_01", camera_name="North elevation", camera_model="Axis P1455-LE")
        _seed_camera(client, camera_id="cam_02", camera_name="South elevation", camera_model="Hikvision DS-2CD2T47")

        event = _make_event(groups="TenantAdmins", tenant_id_claim="acme_corp")
        result = handler(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert "cameras" in body
        assert len(body["cameras"]) == 2

        camera_ids = {cam["camera_id"] for cam in body["cameras"]}
        assert camera_ids == {"cam_01", "cam_02"}

    @mock_aws
    def test_super_admin_lists_cameras_returns_200(self):
        """Super admin can list cameras with tenant_id query param → 200."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)
        _seed_camera(client, camera_id="cam_01", camera_name="North elevation")

        event = _make_event(groups="SuperAdmins", tenant_id_query="acme_corp")
        result = handler(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["cameras"]) == 1
        assert body["cameras"][0]["camera_id"] == "cam_01"
        assert body["cameras"][0]["camera_name"] == "North elevation"

    @mock_aws
    def test_camera_fields_in_response(self):
        """Response includes camera_id, camera_name, and camera_model."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)
        _seed_camera(client, camera_id="cam_01", camera_name="North elevation", camera_model="Axis P1455-LE")

        event = _make_event(groups="TenantAdmins", tenant_id_claim="acme_corp")
        result = handler(event, MagicMock())

        body = json.loads(result["body"])
        cam = body["cameras"][0]
        assert cam["camera_id"] == "cam_01"
        assert cam["camera_name"] == "North elevation"
        assert cam["camera_model"] == "Axis P1455-LE"

    @mock_aws
    def test_correlation_id_echoed(self):
        """The X-Correlation-Id header is echoed in the response."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)

        event = _make_event(correlation_id="my-corr-456")
        result = handler(event, MagicMock())

        assert result["headers"]["X-Correlation-Id"] == "my-corr-456"

    @mock_aws
    def test_cors_headers_present(self):
        """CORS headers are included in the response."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)

        event = _make_event()
        result = handler(event, MagicMock())

        assert result["headers"]["Access-Control-Allow-Origin"] == "*"
        assert "Content-Type" in result["headers"]["Access-Control-Allow-Headers"]
        assert "GET" in result["headers"]["Access-Control-Allow-Methods"]


# ---------------------------------------------------------------------------
# Empty site tests
# ---------------------------------------------------------------------------


class TestCamerasGetEmptySite:
    @mock_aws
    def test_empty_site_returns_200_with_empty_array(self):
        """Site with no cameras returns 200 with empty cameras array."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)
        # No cameras seeded

        event = _make_event(groups="TenantAdmins", tenant_id_claim="acme_corp")
        result = handler(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["cameras"] == []


# ---------------------------------------------------------------------------
# Non-existent site tests
# ---------------------------------------------------------------------------


class TestCamerasGetNotFound:
    @mock_aws
    def test_non_existent_site_returns_404(self):
        """Site that doesn't exist returns 404."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        # Do NOT seed site — it should not exist

        event = _make_event(groups="TenantAdmins", tenant_id_claim="acme_corp", site_id_path="nonexistent_site")
        result = handler(event, MagicMock())

        assert result["statusCode"] == 404
        body = json.loads(result["body"])
        assert body["error"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Super admin without tenant_id tests
# ---------------------------------------------------------------------------


class TestCamerasGetSuperAdminMissingTenantId:
    @mock_aws
    def test_super_admin_without_tenant_id_returns_400(self):
        """Super admin without tenant_id query parameter returns 400."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)

        event = _make_event(groups="SuperAdmins", tenant_id_query=None)
        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"
        assert "tenant_id" in body["message"].lower()


# ---------------------------------------------------------------------------
# No credential fields in response tests
# ---------------------------------------------------------------------------


class TestCamerasGetNoCredentials:
    @mock_aws
    def test_no_credential_fields_in_response(self):
        """Camera listing response does not contain any credential fields."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)

        # Seed a camera with credential-like fields in DynamoDB (simulating stored data)
        client.put_item(
            TableName="test-data-table",
            Item={
                "PK": {"S": "TENANT#acme_corp"},
                "SK": {"S": "SITE#site_001#CAM#cam_01"},
                "camera_name": {"S": "North elevation"},
                "camera_model": {"S": "Axis P1455-LE"},
                "ingest_username": {"S": "sitespy_cam_abc12345678901234"},
                "ingest_password_hash": {"S": "$2b$12$somehash"},
            },
        )

        event = _make_event(groups="TenantAdmins", tenant_id_claim="acme_corp")
        result = handler(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        response_str = json.dumps(body)

        # Ensure no credential-related fields appear anywhere in the response
        assert "username" not in response_str
        assert "password" not in response_str
        assert "ingest_credentials" not in response_str
        assert "ingest_username" not in response_str
        assert "ingest_password_hash" not in response_str

    @mock_aws
    def test_only_expected_fields_in_camera_objects(self):
        """Each camera object only contains camera_id, camera_name, camera_model."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)
        _seed_camera(client, camera_id="cam_01", camera_name="North elevation", camera_model="Axis P1455-LE")

        event = _make_event(groups="TenantAdmins", tenant_id_claim="acme_corp")
        result = handler(event, MagicMock())

        body = json.loads(result["body"])
        cam = body["cameras"][0]
        allowed_keys = {"camera_id", "camera_name", "camera_model"}
        assert set(cam.keys()) <= allowed_keys


# ---------------------------------------------------------------------------
# Authorization tests
# ---------------------------------------------------------------------------


class TestCamerasGetAuthorization:
    @mock_aws
    def test_regular_user_returns_403(self):
        """Regular user caller returns 403 ACCESS_DENIED."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)

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

        event = _make_event()
        event["requestContext"] = {}
        result = handler(event, MagicMock())

        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "ACCESS_DENIED"
