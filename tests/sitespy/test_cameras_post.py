"""Unit tests for POST /v1/sites/{site_id}/cameras handler.

Tests cover:
- Happy-path creation → 201 with ingest token
- Duplicate camera_id → 409
- Non-existent site → 404
- Credentials format in response (token-based)
- Non-super_admin caller → 403
- Validation errors → 400

Requirements validated: 3.1–3.16
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
from sitespy.handlers.cameras_post import handler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    *,
    body: dict[str, Any] | str | None = None,
    groups: str = "SuperAdmins",
    tenant_id_query: str | None = "acme_corp",
    site_id_path: str | None = "site_001",
    correlation_id: str = "test-corr-id",
) -> dict[str, Any]:
    """Build a minimal API Gateway proxy event for POST /v1/sites/{site_id}/cameras."""
    query_params: dict[str, str] | None = None
    if tenant_id_query is not None:
        query_params = {"tenant_id": tenant_id_query}

    path_params: dict[str, str] | None = None
    if site_id_path is not None:
        path_params = {"site_id": site_id_path}

    if body is None:
        body = {
            "camera_id": "cam_01",
            "camera_name": "North elevation",
            "camera_model": "Axis P1455-LE",
        }

    raw_body = json.dumps(body) if isinstance(body, dict) else body

    return {
        "httpMethod": "POST",
        "path": f"/v1/sites/{site_id_path or 'site_001'}/cameras",
        "queryStringParameters": query_params,
        "pathParameters": path_params,
        "headers": {"X-Correlation-Id": correlation_id},
        "body": raw_body,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:groups": groups,
                    "custom:tenant_id": "acme_corp",
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


class TestCamerasPostHappyPath:
    @mock_aws
    def test_creates_camera_returns_201(self):
        """Valid request creates camera and returns 201 with ingest token."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)

        event = _make_event()
        result = handler(event, MagicMock())

        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        assert body["camera_id"] == "cam_01"
        assert "ingest_token" in body
        assert "ingest_url" in body

    @mock_aws
    def test_ingest_url_contains_token(self):
        """The ingest_url includes the ingest token."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)

        event = _make_event()
        result = handler(event, MagicMock())

        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        token = body["ingest_token"]
        assert token in body["ingest_url"]
        assert body["ingest_url"].startswith("https://api.example.com/v1/ingest/")

    @mock_aws
    def test_ingest_token_format(self):
        """The ingest_token starts with tk_ prefix and has expected length."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)

        event = _make_event()
        result = handler(event, MagicMock())

        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        token = body["ingest_token"]
        assert token.startswith("tk_")
        assert len(token) == 43  # tk_ + 40 random chars

    @mock_aws
    def test_optional_camera_model_accepted(self):
        """camera_model is optional and accepted when provided."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)

        event = _make_event(body={
            "camera_id": "cam_02",
            "camera_name": "South elevation",
        })
        result = handler(event, MagicMock())

        assert result["statusCode"] == 201

    @mock_aws
    def test_correlation_id_echoed(self):
        """The X-Correlation-Id header is echoed in the response."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)

        event = _make_event(correlation_id="my-corr-123")
        result = handler(event, MagicMock())

        assert result["headers"]["X-Correlation-Id"] == "my-corr-123"

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
        assert "POST" in result["headers"]["Access-Control-Allow-Methods"]


# ---------------------------------------------------------------------------
# Conflict (duplicate) tests
# ---------------------------------------------------------------------------


class TestCamerasPostConflict:
    @mock_aws
    def test_duplicate_camera_id_returns_409(self):
        """Creating a camera with an existing camera_id on the same site returns 409."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)

        event = _make_event()

        # First creation succeeds
        result1 = handler(event, MagicMock())
        assert result1["statusCode"] == 201

        # Clear caches so second call gets fresh clients within moto
        _dynamodb_client.cache_clear()

        # Second creation with same camera_id returns 409
        result2 = handler(event, MagicMock())
        assert result2["statusCode"] == 409
        body = json.loads(result2["body"])
        assert body["error"] == "CONFLICT"


# ---------------------------------------------------------------------------
# Non-existent site tests
# ---------------------------------------------------------------------------


class TestCamerasPostNotFound:
    @mock_aws
    def test_non_existent_site_returns_404(self):
        """Site that doesn't exist returns 404."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        # Do NOT seed site — it should not exist

        event = _make_event(site_id_path="nonexistent_site")
        result = handler(event, MagicMock())

        assert result["statusCode"] == 404
        body = json.loads(result["body"])
        assert body["error"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Authorization tests
# ---------------------------------------------------------------------------


class TestCamerasPostAuthorization:
    @mock_aws
    def test_tenant_admin_returns_403(self):
        """Tenant admin caller returns 403 ACCESS_DENIED."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)

        event = _make_event(groups="TenantAdmins")
        result = handler(event, MagicMock())

        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "ACCESS_DENIED"

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


# ---------------------------------------------------------------------------
# Validation error tests
# ---------------------------------------------------------------------------


class TestCamerasPostValidation:
    @mock_aws
    def test_missing_tenant_id_query_param_returns_400(self):
        """Missing tenant_id query parameter returns 400."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        event = _make_event(tenant_id_query=None)
        event["queryStringParameters"] = None
        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"
        assert "tenant_id" in body["message"].lower()

    @mock_aws
    def test_missing_camera_id_returns_400(self):
        """Missing camera_id in body returns 400."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)

        event = _make_event(body={
            "camera_name": "North elevation",
        })
        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"
        assert "camera_id" in body["message"].lower()

    @mock_aws
    def test_missing_camera_name_returns_400(self):
        """Missing camera_name in body returns 400."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)

        event = _make_event(body={
            "camera_id": "cam_01",
        })
        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"
        assert "camera_name" in body["message"].lower()

    @mock_aws
    def test_invalid_camera_id_format_returns_400(self):
        """camera_id with invalid characters returns 400."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)

        event = _make_event(body={
            "camera_id": "INVALID-CAM!",
            "camera_name": "North elevation",
        })
        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"
        assert "camera_id" in body["message"].lower()

    @mock_aws
    def test_invalid_json_body_returns_400(self):
        """Non-JSON body returns 400."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)

        event = _make_event(body="not valid json {{{")
        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"

    @mock_aws
    def test_missing_body_returns_400(self):
        """Missing request body returns 400."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)
        _seed_site(client)

        event = _make_event()
        event["body"] = None
        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"
