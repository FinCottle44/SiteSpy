"""Unit tests for POST /v1/tenants handler.

Tests cover:
- Happy-path creation → 201
- Duplicate tenant_id → 409
- Missing required fields → 400
- Invalid tenant_id format → 400
- stale_threshold_hours default (24) and out-of-range → 400
- Non-super_admin caller → 403

Requirements validated: 1.1–1.11
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from moto import mock_aws

from sitespy.data import _dynamodb_client
from sitespy.handlers.tenants_post import handler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    body: dict[str, Any] | str | None = None,
    groups: str = "SuperAdmins",
    correlation_id: str = "test-corr-id",
) -> dict[str, Any]:
    """Build a minimal API Gateway proxy event for POST /v1/tenants."""
    headers: dict[str, str] = {}
    if correlation_id:
        headers["X-Correlation-Id"] = correlation_id

    event: dict[str, Any] = {
        "httpMethod": "POST",
        "path": "/v1/tenants",
        "headers": headers,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:groups": groups,
                    "custom:tenant_id": "caller_tenant",
                }
            }
        },
    }

    if body is None:
        event["body"] = None
    elif isinstance(body, str):
        event["body"] = body
    else:
        event["body"] = json.dumps(body)

    return event


def _valid_body(**overrides: Any) -> dict[str, Any]:
    """Return a valid tenant creation request body with optional overrides."""
    base = {
        "tenant_id": "acme_corp",
        "tenant_name": "Acme Construction Ltd",
        "primary_contact_email": "ops@acme.example.com",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Fixture: moto DynamoDB with table + cache clear
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_dynamodb_cache():
    """Clear the _dynamodb_client lru_cache before and after each test."""
    _dynamodb_client.cache_clear()
    yield
    _dynamodb_client.cache_clear()


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


class TestTenantsPostHappyPath:
    @mock_aws
    def test_creates_tenant_returns_201(self, moto_dynamodb):
        """Valid request creates tenant and returns 201 with all fields."""
        body = _valid_body()
        event = _make_event(body=body)

        result = handler(event, MagicMock())

        assert result["statusCode"] == 201
        response_body = json.loads(result["body"])
        assert response_body["tenant_id"] == "acme_corp"
        assert response_body["tenant_name"] == "Acme Construction Ltd"
        assert response_body["primary_contact_email"] == "ops@acme.example.com"
        assert response_body["stale_threshold_hours"] == 24

    @mock_aws
    def test_default_stale_threshold_hours_is_24(self, moto_dynamodb):
        """When stale_threshold_hours is omitted, defaults to 24."""
        body = _valid_body()
        assert "stale_threshold_hours" not in body
        event = _make_event(body=body)

        result = handler(event, MagicMock())

        assert result["statusCode"] == 201
        response_body = json.loads(result["body"])
        assert response_body["stale_threshold_hours"] == 24

    @mock_aws
    def test_custom_stale_threshold_hours(self, moto_dynamodb):
        """When stale_threshold_hours is provided within range, it is used."""
        body = _valid_body(stale_threshold_hours=48)
        event = _make_event(body=body)

        result = handler(event, MagicMock())

        assert result["statusCode"] == 201
        response_body = json.loads(result["body"])
        assert response_body["stale_threshold_hours"] == 48

    @mock_aws
    def test_correlation_id_echoed(self, moto_dynamodb):
        """The X-Correlation-Id header is echoed in the response."""
        event = _make_event(body=_valid_body(), correlation_id="my-corr-123")

        result = handler(event, MagicMock())

        assert result["headers"]["X-Correlation-Id"] == "my-corr-123"

    @mock_aws
    def test_cors_headers_present(self, moto_dynamodb):
        """CORS headers are included in the response."""
        event = _make_event(body=_valid_body())

        result = handler(event, MagicMock())

        assert result["headers"]["Access-Control-Allow-Origin"] == "*"
        assert "Content-Type" in result["headers"]["Access-Control-Allow-Headers"]
        assert "POST" in result["headers"]["Access-Control-Allow-Methods"]


# ---------------------------------------------------------------------------
# Conflict (duplicate) tests
# ---------------------------------------------------------------------------


class TestTenantsPostConflict:
    @mock_aws
    def test_duplicate_tenant_id_returns_409(self, moto_dynamodb):
        """Creating a tenant with an existing tenant_id returns 409."""
        body = _valid_body()
        event = _make_event(body=body)

        # First creation succeeds
        result1 = handler(event, MagicMock())
        assert result1["statusCode"] == 201

        # Second creation with same tenant_id returns 409
        result2 = handler(event, MagicMock())
        assert result2["statusCode"] == 409
        response_body = json.loads(result2["body"])
        assert response_body["error"] == "CONFLICT"


# ---------------------------------------------------------------------------
# Missing required fields tests
# ---------------------------------------------------------------------------


class TestTenantsPostMissingFields:
    @mock_aws
    def test_missing_tenant_id_returns_400(self, moto_dynamodb):
        """Missing tenant_id field returns 400."""
        body = _valid_body()
        del body["tenant_id"]
        event = _make_event(body=body)

        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        response_body = json.loads(result["body"])
        assert response_body["error"] == "BAD_REQUEST"
        assert "tenant_id" in response_body["message"]

    @mock_aws
    def test_missing_tenant_name_returns_400(self, moto_dynamodb):
        """Missing tenant_name field returns 400."""
        body = _valid_body()
        del body["tenant_name"]
        event = _make_event(body=body)

        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        response_body = json.loads(result["body"])
        assert response_body["error"] == "BAD_REQUEST"
        assert "tenant_name" in response_body["message"]

    @mock_aws
    def test_missing_primary_contact_email_returns_400(self, moto_dynamodb):
        """Missing primary_contact_email field returns 400."""
        body = _valid_body()
        del body["primary_contact_email"]
        event = _make_event(body=body)

        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        response_body = json.loads(result["body"])
        assert response_body["error"] == "BAD_REQUEST"
        assert "primary_contact_email" in response_body["message"]

    @mock_aws
    def test_missing_body_returns_400(self, moto_dynamodb):
        """Missing request body returns 400."""
        event = _make_event(body=None)

        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        response_body = json.loads(result["body"])
        assert response_body["error"] == "BAD_REQUEST"

    @mock_aws
    def test_invalid_json_body_returns_400(self, moto_dynamodb):
        """Non-JSON request body returns 400."""
        event = _make_event(body="not valid json {{{")

        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        response_body = json.loads(result["body"])
        assert response_body["error"] == "BAD_REQUEST"


# ---------------------------------------------------------------------------
# Invalid tenant_id format tests
# ---------------------------------------------------------------------------


class TestTenantsPostInvalidTenantId:
    @mock_aws
    def test_tenant_id_too_short_returns_400(self, moto_dynamodb):
        """tenant_id shorter than 3 chars returns 400."""
        body = _valid_body(tenant_id="ab")
        event = _make_event(body=body)

        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        response_body = json.loads(result["body"])
        assert response_body["error"] == "BAD_REQUEST"
        assert "tenant_id" in response_body["message"]

    @mock_aws
    def test_tenant_id_too_long_returns_400(self, moto_dynamodb):
        """tenant_id longer than 32 chars returns 400."""
        body = _valid_body(tenant_id="a" * 33)
        event = _make_event(body=body)

        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        response_body = json.loads(result["body"])
        assert response_body["error"] == "BAD_REQUEST"
        assert "tenant_id" in response_body["message"]

    @mock_aws
    def test_tenant_id_with_uppercase_returns_400(self, moto_dynamodb):
        """tenant_id with uppercase letters returns 400."""
        body = _valid_body(tenant_id="AcmeCorp")
        event = _make_event(body=body)

        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        response_body = json.loads(result["body"])
        assert response_body["error"] == "BAD_REQUEST"

    @mock_aws
    def test_tenant_id_with_special_chars_returns_400(self, moto_dynamodb):
        """tenant_id with special characters (not a-z0-9_) returns 400."""
        body = _valid_body(tenant_id="acme-corp!")
        event = _make_event(body=body)

        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        response_body = json.loads(result["body"])
        assert response_body["error"] == "BAD_REQUEST"


# ---------------------------------------------------------------------------
# stale_threshold_hours validation tests
# ---------------------------------------------------------------------------


class TestTenantsPostStaleThreshold:
    @mock_aws
    def test_stale_threshold_below_1_returns_400(self, moto_dynamodb):
        """stale_threshold_hours < 1 returns 400."""
        body = _valid_body(stale_threshold_hours=0)
        event = _make_event(body=body)

        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        response_body = json.loads(result["body"])
        assert response_body["error"] == "BAD_REQUEST"
        assert "stale_threshold_hours" in response_body["message"]

    @mock_aws
    def test_stale_threshold_above_720_returns_400(self, moto_dynamodb):
        """stale_threshold_hours > 720 returns 400."""
        body = _valid_body(stale_threshold_hours=721)
        event = _make_event(body=body)

        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        response_body = json.loads(result["body"])
        assert response_body["error"] == "BAD_REQUEST"
        assert "stale_threshold_hours" in response_body["message"]

    @mock_aws
    def test_stale_threshold_not_integer_returns_400(self, moto_dynamodb):
        """stale_threshold_hours as a float returns 400."""
        body = _valid_body(stale_threshold_hours=12.5)
        event = _make_event(body=body)

        result = handler(event, MagicMock())

        assert result["statusCode"] == 400
        response_body = json.loads(result["body"])
        assert response_body["error"] == "BAD_REQUEST"
        assert "stale_threshold_hours" in response_body["message"]

    @mock_aws
    def test_stale_threshold_boundary_1_accepted(self, moto_dynamodb):
        """stale_threshold_hours = 1 (lower boundary) is accepted."""
        body = _valid_body(stale_threshold_hours=1)
        event = _make_event(body=body)

        result = handler(event, MagicMock())

        assert result["statusCode"] == 201
        response_body = json.loads(result["body"])
        assert response_body["stale_threshold_hours"] == 1

    @mock_aws
    def test_stale_threshold_boundary_720_accepted(self, moto_dynamodb):
        """stale_threshold_hours = 720 (upper boundary) is accepted."""
        body = _valid_body(stale_threshold_hours=720)
        event = _make_event(body=body)

        result = handler(event, MagicMock())

        assert result["statusCode"] == 201
        response_body = json.loads(result["body"])
        assert response_body["stale_threshold_hours"] == 720


# ---------------------------------------------------------------------------
# Authorization tests
# ---------------------------------------------------------------------------


class TestTenantsPostAuthorization:
    @mock_aws
    def test_tenant_admin_returns_403(self, moto_dynamodb):
        """Tenant admin caller returns 403 ACCESS_DENIED."""
        event = _make_event(body=_valid_body(), groups="TenantAdmins")

        result = handler(event, MagicMock())

        assert result["statusCode"] == 403
        response_body = json.loads(result["body"])
        assert response_body["error"] == "ACCESS_DENIED"

    @mock_aws
    def test_regular_user_returns_403(self, moto_dynamodb):
        """Regular user (no admin group) returns 403 ACCESS_DENIED."""
        event = _make_event(body=_valid_body(), groups="")

        result = handler(event, MagicMock())

        assert result["statusCode"] == 403
        response_body = json.loads(result["body"])
        assert response_body["error"] == "ACCESS_DENIED"

    @mock_aws
    def test_no_claims_returns_403(self, moto_dynamodb):
        """Event with no authorizer claims returns 403."""
        event = _make_event(body=_valid_body())
        event["requestContext"] = {}

        result = handler(event, MagicMock())

        assert result["statusCode"] == 403
        response_body = json.loads(result["body"])
        assert response_body["error"] == "ACCESS_DENIED"
