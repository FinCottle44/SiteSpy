"""Unit tests for POST /v1/sites handler.

Tests cover:
- Happy-path creation → 201
- Missing tenant_id query param → 400
- Non-existent tenant → 404
- Duplicate site_id → 409
- Invalid lat/lon → 400
- Invalid timezone → 400
- Default timezone applied (Europe/London)
- Non-super_admin caller → 403

Requirements validated: 2.1–2.14
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

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_caches():
    """Clear the _dynamodb_client lru_cache before and after each test."""
    _dynamodb_client.cache_clear()
    yield
    _dynamodb_client.cache_clear()


def _create_table(client):
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
    """Insert a tenant record so the handler can verify tenant existence."""
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


def _make_event(
    *,
    body: dict[str, Any] | str | None = None,
    groups: str = "SuperAdmins",
    tenant_id_query: str | None = "acme_corp",
    correlation_id: str = "test-corr-id",
) -> dict[str, Any]:
    """Build a minimal API Gateway proxy event for POST /v1/sites."""
    query_params: dict[str, str] | None = None
    if tenant_id_query is not None:
        query_params = {"tenant_id": tenant_id_query}

    if body is None:
        body = {
            "site_id": "site_001",
            "site_name": "Acme Tower — Phase 2",
            "latitude": 51.5074,
            "longitude": -0.1278,
        }

    raw_body = json.dumps(body) if isinstance(body, dict) else body

    return {
        "httpMethod": "POST",
        "path": "/v1/sites",
        "queryStringParameters": query_params,
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


# ---------------------------------------------------------------------------
# Import handler (after env is set via conftest)
# ---------------------------------------------------------------------------

from sitespy.handlers.sites_post import handler  # noqa: E402


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


class TestSitesPostHappyPath:
    @mock_aws
    def test_creates_site_returns_201(self):
        """Valid request creates a site and returns 201 with all fields."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)

        event = _make_event()
        result = handler(event, MagicMock())

        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        assert body["site_id"] == "site_001"
        assert body["site_name"] == "Acme Tower — Phase 2"
        assert body["tenant_id"] == "acme_corp"
        assert body["latitude"] == 51.5074
        assert body["longitude"] == -0.1278
        assert body["timezone"] == "Europe/London"

    @mock_aws
    def test_default_timezone_applied(self):
        """When timezone is omitted, defaults to Europe/London."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)

        event = _make_event(body={
            "site_id": "site_002",
            "site_name": "Test Site",
            "latitude": 40.0,
            "longitude": -3.0,
        })
        result = handler(event, MagicMock())

        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        assert body["timezone"] == "Europe/London"

    @mock_aws
    def test_custom_timezone_accepted(self):
        """When a valid timezone is provided, it is used."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)

        event = _make_event(body={
            "site_id": "site_003",
            "site_name": "NYC Site",
            "latitude": 40.7128,
            "longitude": -74.006,
            "timezone": "America/New_York",
        })
        result = handler(event, MagicMock())

        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        assert body["timezone"] == "America/New_York"

    @mock_aws
    def test_correlation_id_echoed(self):
        """The X-Correlation-Id header is echoed in the response."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)

        event = _make_event(correlation_id="my-custom-corr-id")
        result = handler(event, MagicMock())

        assert result["headers"]["X-Correlation-Id"] == "my-custom-corr-id"

    @mock_aws
    def test_cors_headers_present(self):
        """CORS headers are included in the response."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)

        event = _make_event()
        result = handler(event, MagicMock())

        assert result["headers"]["Access-Control-Allow-Origin"] == "*"
        assert "Content-Type" in result["headers"]["Access-Control-Allow-Headers"]
        assert "POST" in result["headers"]["Access-Control-Allow-Methods"]


# ---------------------------------------------------------------------------
# Error tests
# ---------------------------------------------------------------------------


class TestSitesPostErrors:
    @mock_aws
    def test_missing_tenant_id_query_param_returns_400(self):
        """Missing tenant_id query parameter returns 400."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        event = _make_event(tenant_id_query=None)
        # Remove queryStringParameters entirely
        event["queryStringParameters"] = None
        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"
        assert "tenant_id" in body["message"].lower()

    @mock_aws
    def test_empty_tenant_id_query_param_returns_400(self):
        """Empty tenant_id query parameter returns 400."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        event = _make_event(tenant_id_query="")
        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"

    @mock_aws
    def test_non_existent_tenant_returns_404(self):
        """Tenant that doesn't exist in DynamoDB returns 404."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        # Do NOT seed tenant — it should not exist

        event = _make_event(tenant_id_query="nonexistent_tenant")
        result = handler(event, MagicMock())

        assert result["statusCode"] == 404
        body = json.loads(result["body"])
        assert body["error"] == "NOT_FOUND"

    @mock_aws
    def test_duplicate_site_id_returns_409(self):
        """Creating a site with an existing site_id returns 409."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)

        # Create the site first
        event = _make_event()
        result = handler(event, MagicMock())
        assert result["statusCode"] == 201

        # Clear cache so the second call gets a fresh DynamoDB client within moto
        _dynamodb_client.cache_clear()

        # Try to create the same site again
        result = handler(event, MagicMock())
        assert result["statusCode"] == 409
        body = json.loads(result["body"])
        assert body["error"] == "CONFLICT"

    @mock_aws
    def test_invalid_latitude_too_high_returns_400(self):
        """Latitude > 90 returns 400."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)

        event = _make_event(body={
            "site_id": "site_001",
            "site_name": "Test Site",
            "latitude": 91.0,
            "longitude": -0.1278,
        })
        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"
        assert "latitude" in body["message"].lower()

    @mock_aws
    def test_invalid_latitude_too_low_returns_400(self):
        """Latitude < -90 returns 400."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)

        event = _make_event(body={
            "site_id": "site_001",
            "site_name": "Test Site",
            "latitude": -91.0,
            "longitude": -0.1278,
        })
        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"

    @mock_aws
    def test_invalid_longitude_too_high_returns_400(self):
        """Longitude > 180 returns 400."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)

        event = _make_event(body={
            "site_id": "site_001",
            "site_name": "Test Site",
            "latitude": 51.0,
            "longitude": 181.0,
        })
        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"
        assert "longitude" in body["message"].lower()

    @mock_aws
    def test_invalid_longitude_too_low_returns_400(self):
        """Longitude < -180 returns 400."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)

        event = _make_event(body={
            "site_id": "site_001",
            "site_name": "Test Site",
            "latitude": 51.0,
            "longitude": -181.0,
        })
        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"

    @mock_aws
    def test_invalid_timezone_returns_400(self):
        """Invalid IANA timezone returns 400."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)

        event = _make_event(body={
            "site_id": "site_001",
            "site_name": "Test Site",
            "latitude": 51.0,
            "longitude": -0.1278,
            "timezone": "Not/A/Real/Timezone",
        })
        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"
        assert "timezone" in body["message"].lower()

    @mock_aws
    def test_non_super_admin_returns_403(self):
        """Tenant admin caller returns 403."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)

        event = _make_event(groups="TenantAdmins")
        result = handler(event, MagicMock())

        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "ACCESS_DENIED"

    @mock_aws
    def test_regular_user_returns_403(self):
        """Regular user caller returns 403."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)

        event = _make_event(groups="")
        result = handler(event, MagicMock())

        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "ACCESS_DENIED"

    @mock_aws
    def test_missing_site_id_returns_400(self):
        """Missing site_id in body returns 400."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)

        event = _make_event(body={
            "site_name": "Test Site",
            "latitude": 51.0,
            "longitude": -0.1278,
        })
        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"
        assert "site_id" in body["message"].lower()

    @mock_aws
    def test_invalid_site_id_format_returns_400(self):
        """site_id with invalid characters returns 400."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)

        event = _make_event(body={
            "site_id": "INVALID-SITE!",
            "site_name": "Test Site",
            "latitude": 51.0,
            "longitude": -0.1278,
        })
        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"
        assert "site_id" in body["message"].lower()

    @mock_aws
    def test_missing_site_name_returns_400(self):
        """Missing site_name in body returns 400."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)

        event = _make_event(body={
            "site_id": "site_001",
            "latitude": 51.0,
            "longitude": -0.1278,
        })
        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"
        assert "site_name" in body["message"].lower()

    @mock_aws
    def test_missing_latitude_returns_400(self):
        """Missing latitude in body returns 400."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)

        event = _make_event(body={
            "site_id": "site_001",
            "site_name": "Test Site",
            "longitude": -0.1278,
        })
        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"
        assert "latitude" in body["message"].lower()

    @mock_aws
    def test_missing_longitude_returns_400(self):
        """Missing longitude in body returns 400."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)

        event = _make_event(body={
            "site_id": "site_001",
            "site_name": "Test Site",
            "latitude": 51.0,
        })
        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"
        assert "longitude" in body["message"].lower()

    @mock_aws
    def test_invalid_json_body_returns_400(self):
        """Non-JSON body returns 400."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)

        event = _make_event(body="not valid json {{{")
        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"

    @mock_aws
    def test_boundary_latitude_90_accepted(self):
        """Latitude exactly 90 is valid."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)

        event = _make_event(body={
            "site_id": "site_north_pole",
            "site_name": "North Pole Site",
            "latitude": 90.0,
            "longitude": 0.0,
        })
        result = handler(event, MagicMock())

        assert result["statusCode"] == 201

    @mock_aws
    def test_boundary_latitude_neg90_accepted(self):
        """Latitude exactly -90 is valid."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)

        event = _make_event(body={
            "site_id": "site_south_pole",
            "site_name": "South Pole Site",
            "latitude": -90.0,
            "longitude": 0.0,
        })
        result = handler(event, MagicMock())

        assert result["statusCode"] == 201

    @mock_aws
    def test_boundary_longitude_180_accepted(self):
        """Longitude exactly 180 is valid."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)

        event = _make_event(body={
            "site_id": "site_dateline",
            "site_name": "Dateline Site",
            "latitude": 0.0,
            "longitude": 180.0,
        })
        result = handler(event, MagicMock())

        assert result["statusCode"] == 201

    @mock_aws
    def test_boundary_longitude_neg180_accepted(self):
        """Longitude exactly -180 is valid."""
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)
        _seed_tenant(client)

        event = _make_event(body={
            "site_id": "site_dateline_neg",
            "site_name": "Dateline Neg Site",
            "latitude": 0.0,
            "longitude": -180.0,
        })
        result = handler(event, MagicMock())

        assert result["statusCode"] == 201
