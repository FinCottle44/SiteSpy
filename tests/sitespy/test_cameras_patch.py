"""Unit tests for PATCH /v1/sites/{site_id}/cameras/{camera_id} handler.

Tests cover:
- Happy-path rename (camera_name) → 200
- Updating camera_model, and clearing it with null
- Updating both fields at once
- Empty / invalid body → 400
- Non-existent camera → 404
- Tenant admin accessing other tenant's camera → 403
- Super admin without tenant_id → 400
- Regular user → 403
- Sandbox tenant access for non-super_admin → 403
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from sitespy.data import _dynamodb_client, get_camera
from sitespy.handlers.cameras_patch import handler


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
    body: dict[str, Any] | None = None,
    correlation_id: str = "test-corr-id",
) -> dict[str, Any]:
    """Build a minimal API Gateway proxy event for PATCH .../cameras/{camera_id}."""
    query_params: dict[str, str] | None = None
    if tenant_id_query is not None:
        query_params = {"tenant_id": tenant_id_query}

    path_params: dict[str, str] = {}
    if site_id_path is not None:
        path_params["site_id"] = site_id_path
    if camera_id_path is not None:
        path_params["camera_id"] = camera_id_path

    return {
        "httpMethod": "PATCH",
        "path": f"/v1/sites/{site_id_path}/cameras/{camera_id_path}",
        "queryStringParameters": query_params,
        "pathParameters": path_params if path_params else None,
        "headers": {"X-Correlation-Id": correlation_id},
        "body": json.dumps(body) if body is not None else None,
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
    client.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": f"TENANT#{tenant_id}"},
            "SK": {"S": f"TENANT#{tenant_id}"},
            "tenant_name": {"S": "Acme Corp"},
            "stale_threshold_hours": {"N": "24"},
        },
    )


def _seed_site(client, tenant_id: str = "acme_corp", site_id: str = "site_001") -> None:
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
    ingest_token: str = "tk_token1234567890abcdefghijklmnopqrstuv",
) -> None:
    item: dict[str, Any] = {
        "PK": {"S": f"TENANT#{tenant_id}"},
        "SK": {"S": f"SITE#{site_id}#CAM#{camera_id}"},
        "GSI1PK": {"S": f"TOKEN#{ingest_token}"},
        "GSI1SK": {"S": "CAMERA"},
        "camera_name": {"S": camera_name},
        "ingest_token": {"S": ingest_token},
    }
    if camera_model is not None:
        item["camera_model"] = {"S": camera_model}
    client.put_item(TableName="test-data-table", Item=item)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_caches():
    _dynamodb_client.cache_clear()
    yield
    _dynamodb_client.cache_clear()


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


class TestCamerasPatchHappyPath:
    @mock_aws
    def test_tenant_admin_renames_camera_returns_200(self):
        """Tenant admin can rename a camera in their tenant → 200."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)
        _seed_camera(client)

        event = _make_event(
            groups="TenantAdmins",
            tenant_id_claim="acme_corp",
            body={"camera_name": "South gate"},
        )
        result = handler(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["camera_id"] == "cam_01"
        assert body["camera_name"] == "South gate"

        # Verify the rename persisted
        camera = get_camera("acme_corp", "site_001", "cam_01")
        assert camera["camera_name"]["S"] == "South gate"
        # Model and token untouched
        assert camera["camera_model"]["S"] == "Axis P1455-LE"
        assert camera["ingest_token"]["S"].startswith("tk_")

    @mock_aws
    def test_super_admin_renames_camera_returns_200(self):
        """Super admin can rename a camera with tenant_id query param → 200."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)
        _seed_camera(client)

        event = _make_event(
            groups="SuperAdmins",
            tenant_id_query="acme_corp",
            body={"camera_name": "Renamed by super admin"},
        )
        result = handler(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["camera_name"] == "Renamed by super admin"

    @mock_aws
    def test_update_camera_model(self):
        """Updating camera_model persists the new value."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)
        _seed_camera(client)

        event = _make_event(body={"camera_model": "Axis Q6135-LE"})
        result = handler(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["camera_model"] == "Axis Q6135-LE"
        camera = get_camera("acme_corp", "site_001", "cam_01")
        assert camera["camera_model"]["S"] == "Axis Q6135-LE"

    @mock_aws
    def test_clear_camera_model_with_null(self):
        """Setting camera_model to null removes the attribute."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)
        _seed_camera(client)

        event = _make_event(body={"camera_model": None})
        result = handler(event, MagicMock())

        assert result["statusCode"] == 200
        camera = get_camera("acme_corp", "site_001", "cam_01")
        assert "camera_model" not in camera

    @mock_aws
    def test_update_both_fields(self):
        """Updating both camera_name and camera_model at once."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)
        _seed_camera(client)

        event = _make_event(
            body={"camera_name": "West tower", "camera_model": "Axis P3265"}
        )
        result = handler(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["camera_name"] == "West tower"
        assert body["camera_model"] == "Axis P3265"

    @mock_aws
    def test_camera_name_is_trimmed(self):
        """Leading/trailing whitespace is stripped from camera_name."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)
        _seed_camera(client)

        event = _make_event(body={"camera_name": "  Padded name  "})
        result = handler(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["camera_name"] == "Padded name"


# ---------------------------------------------------------------------------
# Validation tests
# ---------------------------------------------------------------------------


class TestCamerasPatchValidation:
    @mock_aws
    def test_empty_body_returns_400(self):
        """Empty body (no updatable fields) returns 400."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)
        _seed_camera(client)

        event = _make_event(body={})
        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"

    @mock_aws
    def test_empty_camera_name_returns_400(self):
        """Empty-string camera_name returns 400."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)
        _seed_camera(client)

        event = _make_event(body={"camera_name": "   "})
        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"

    @mock_aws
    def test_empty_camera_model_string_returns_400(self):
        """Empty-string camera_model (not null) returns 400."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)
        _seed_camera(client)

        event = _make_event(body={"camera_model": ""})
        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"

    @mock_aws
    def test_non_string_camera_name_returns_400(self):
        """Non-string camera_name returns 400."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)
        _seed_camera(client)

        event = _make_event(body={"camera_name": 12345})
        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"


# ---------------------------------------------------------------------------
# Not-found tests
# ---------------------------------------------------------------------------


class TestCamerasPatchNotFound:
    @mock_aws
    def test_non_existent_camera_returns_404(self):
        """Camera that doesn't exist returns 404."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)
        # No camera seeded

        event = _make_event(
            camera_id_path="ghost_cam", body={"camera_name": "X"}
        )
        result = handler(event, MagicMock())

        assert result["statusCode"] == 404
        body = json.loads(result["body"])
        assert body["error"] == "NOT_FOUND"

    @mock_aws
    def test_non_existent_site_for_tenant_admin_returns_403(self):
        """Tenant admin with non-existent site returns 403."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        # No site seeded

        event = _make_event(
            site_id_path="ghost_site", body={"camera_name": "X"}
        )
        result = handler(event, MagicMock())

        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "ACCESS_DENIED"


# ---------------------------------------------------------------------------
# Authorization tests
# ---------------------------------------------------------------------------


class TestCamerasPatchAuthorization:
    @mock_aws
    def test_regular_user_returns_403(self):
        """Regular user caller returns 403."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)
        _seed_camera(client)

        event = _make_event(groups="", body={"camera_name": "X"})
        result = handler(event, MagicMock())

        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "ACCESS_DENIED"

    @mock_aws
    def test_super_admin_without_tenant_id_returns_400(self):
        """Super admin without tenant_id query param returns 400."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)
        _seed_camera(client)

        event = _make_event(
            groups="SuperAdmins", tenant_id_query=None, body={"camera_name": "X"}
        )
        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"
        assert "tenant_id" in body["message"].lower()

    @mock_aws
    def test_tenant_admin_cross_tenant_returns_403(self):
        """Tenant admin cannot rename a camera in another tenant."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client, tenant_id="other_corp")
        _seed_site(client, tenant_id="other_corp", site_id="site_001")
        _seed_camera(client, tenant_id="other_corp", site_id="site_001", camera_id="cam_01")

        event = _make_event(
            groups="TenantAdmins",
            tenant_id_claim="acme_corp",
            body={"camera_name": "Hijack attempt"},
        )
        result = handler(event, MagicMock())

        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "ACCESS_DENIED"

    @mock_aws
    def test_non_super_admin_sandbox_access_returns_403(self):
        """Tenant admin cannot rename a camera in the sandbox tenant."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client, tenant_id="sandbox_construction")
        _seed_site(client, tenant_id="sandbox_construction", site_id="site_001")
        _seed_camera(client, tenant_id="sandbox_construction", site_id="site_001", camera_id="cam_01")

        event = _make_event(
            groups="TenantAdmins",
            tenant_id_claim="sandbox_construction",
            body={"camera_name": "X"},
        )
        result = handler(event, MagicMock())

        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "ACCESS_DENIED"
