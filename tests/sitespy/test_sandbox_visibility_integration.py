"""Unit tests for sandbox visibility integration in existing handlers.

Validates that the sandbox_visibility_guard is correctly integrated into
tenant-scoped handlers (cameras_get, sites), ensuring:
- Non-super_admin callers get 403 when accessing sandbox tenant resources
- Super_admin can still access sandbox tenant resources normally

Requirements validated: 2.2, 2.3
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from sitespy.data import _dynamodb_client
from sitespy.handlers.cameras_get import handler as cameras_get_handler
from sitespy.handlers.sites import handler as sites_handler
from sitespy.handlers.sites import handler_list as sites_list_handler
from sitespy.sandbox import SANDBOX_TENANT_ID


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SANDBOX_SITE_ITEM = {
    "PK": {"S": f"TENANT#{SANDBOX_TENANT_ID}"},
    "SK": {"S": "SITE#default_sandbox_site"},
    "site_name": {"S": "Default Sandbox Site"},
    "latitude": {"N": "-33.8688"},
    "longitude": {"N": "151.2093"},
    "timezone": {"S": "Australia/Sydney"},
}

_SANDBOX_CAMERA_ITEMS = [
    {
        "PK": {"S": f"TENANT#{SANDBOX_TENANT_ID}"},
        "SK": {"S": "SITE#default_sandbox_site#CAM#cam_test_01"},
        "camera_name": {"S": "Test Camera"},
        "camera_model": {"S": "Axis P1455-LE"},
    },
]


def _make_cameras_get_event(
    *,
    groups: str = "TenantAdmins",
    tenant_id_claim: str = SANDBOX_TENANT_ID,
    tenant_id_query: str | None = None,
    site_id_path: str = "default_sandbox_site",
) -> dict[str, Any]:
    """Build an API Gateway proxy event for GET /v1/sites/{site_id}/cameras."""
    query_params: dict[str, str] | None = None
    if tenant_id_query is not None:
        query_params = {"tenant_id": tenant_id_query}

    return {
        "httpMethod": "GET",
        "path": f"/v1/sites/{site_id_path}/cameras",
        "queryStringParameters": query_params,
        "pathParameters": {"site_id": site_id_path},
        "headers": {"X-Correlation-Id": "test-sandbox-vis"},
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


def _make_sites_get_event(
    *,
    groups: str = "TenantAdmins",
    tenant_id_claim: str = SANDBOX_TENANT_ID,
    tenant_id_query: str | None = None,
    site_id_path: str = "default_sandbox_site",
    site_access: str = "",
) -> dict[str, Any]:
    """Build an API Gateway proxy event for GET /v1/sites/{site_id}."""
    query_params: dict[str, str] | None = None
    if tenant_id_query is not None:
        query_params = {"tenant_id": tenant_id_query}

    return {
        "httpMethod": "GET",
        "path": f"/v1/sites/{site_id_path}",
        "pathParameters": {"site_id": site_id_path},
        "queryStringParameters": query_params or {},
        "headers": {"X-Correlation-Id": "test-sandbox-vis"},
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:groups": groups,
                    "custom:tenant_id": tenant_id_claim,
                    "custom:site_access": site_access,
                }
            }
        },
    }


def _make_sites_list_event(
    *,
    groups: str = "TenantAdmins",
    tenant_id_claim: str = SANDBOX_TENANT_ID,
    tenant_id_query: str | None = None,
) -> dict[str, Any]:
    """Build an API Gateway proxy event for GET /v1/sites."""
    query_params: dict[str, str] | None = None
    if tenant_id_query is not None:
        query_params = {"tenant_id": tenant_id_query}

    return {
        "httpMethod": "GET",
        "path": "/v1/sites",
        "pathParameters": None,
        "queryStringParameters": query_params or {},
        "headers": {"X-Correlation-Id": "test-sandbox-vis"},
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:groups": groups,
                    "custom:tenant_id": tenant_id_claim,
                }
            }
        },
    }


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
# cameras_get: Non-super_admin gets 403 for sandbox tenant
# ---------------------------------------------------------------------------


class TestCamerasGetSandboxVisibility:
    """Validates Requirement 2.2, 2.3: non-super_admin cannot access sandbox resources."""

    def test_tenant_admin_gets_403_for_sandbox_cameras(self):
        """Tenant admin accessing sandbox tenant cameras gets 403."""
        event = _make_cameras_get_event(
            groups="TenantAdmins",
            tenant_id_claim=SANDBOX_TENANT_ID,
        )
        result = cameras_get_handler(event, MagicMock())

        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "ACCESS_DENIED"

    def test_user_role_gets_403_for_sandbox_cameras(self):
        """User role accessing sandbox tenant cameras gets 403.

        Note: users without TenantAdmins or SuperAdmins group will get 403
        from role check, which is fine — the point is they cannot access
        sandbox resources.
        """
        event = _make_cameras_get_event(
            groups="",
            tenant_id_claim=SANDBOX_TENANT_ID,
        )
        result = cameras_get_handler(event, MagicMock())

        assert result["statusCode"] == 403

    def test_super_admin_can_access_sandbox_cameras(self, moto_dynamodb):
        """Super admin can still access sandbox tenant cameras normally."""
        # Seed sandbox tenant and site data
        moto_dynamodb.put_item(
            TableName="test-data-table",
            Item={
                "PK": {"S": f"TENANT#{SANDBOX_TENANT_ID}"},
                "SK": {"S": f"TENANT#{SANDBOX_TENANT_ID}"},
                "tenant_name": {"S": "Sandbox Construction"},
                "stale_threshold_hours": {"N": "24"},
            },
        )
        moto_dynamodb.put_item(
            TableName="test-data-table",
            Item=_SANDBOX_SITE_ITEM,
        )
        moto_dynamodb.put_item(
            TableName="test-data-table",
            Item=_SANDBOX_CAMERA_ITEMS[0],
        )

        event = _make_cameras_get_event(
            groups="SuperAdmins",
            tenant_id_query=SANDBOX_TENANT_ID,
        )
        result = cameras_get_handler(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["cameras"]) == 1
        assert body["cameras"][0]["camera_id"] == "cam_test_01"


# ---------------------------------------------------------------------------
# sites (GET /v1/sites/{site_id}): Non-super_admin gets 403 for sandbox tenant
# ---------------------------------------------------------------------------


class TestSitesGetSandboxVisibility:
    """Validates Requirement 2.2, 2.3: non-super_admin cannot access sandbox site details."""

    def test_tenant_admin_gets_403_for_sandbox_site(self):
        """Tenant admin accessing sandbox tenant site gets 403."""
        event = _make_sites_get_event(
            groups="TenantAdmins",
            tenant_id_claim=SANDBOX_TENANT_ID,
        )
        result = sites_handler(event, MagicMock())

        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "ACCESS_DENIED"

    def test_user_with_site_access_gets_403_for_sandbox_site(self):
        """User with site_access for a sandbox site still gets 403."""
        event = _make_sites_get_event(
            groups="",
            tenant_id_claim=SANDBOX_TENANT_ID,
            site_access="default_sandbox_site",
        )
        result = sites_handler(event, MagicMock())

        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "ACCESS_DENIED"

    def test_super_admin_can_access_sandbox_site(self):
        """Super admin can access sandbox site details normally."""
        event = _make_sites_get_event(
            groups="SuperAdmins",
            tenant_id_claim="",
            tenant_id_query=SANDBOX_TENANT_ID,
        )
        with (
            patch("sitespy.data.get_site", return_value=_SANDBOX_SITE_ITEM),
            patch("sitespy.data.get_cameras_for_site", return_value=_SANDBOX_CAMERA_ITEMS),
        ):
            result = sites_handler(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["site_id"] == "default_sandbox_site"
        assert body["tenant_id"] == SANDBOX_TENANT_ID


# ---------------------------------------------------------------------------
# sites list (GET /v1/sites): Non-super_admin gets 403 for sandbox tenant
# ---------------------------------------------------------------------------


class TestSitesListSandboxVisibility:
    """Validates Requirement 2.2, 2.3: non-super_admin cannot list sandbox sites."""

    def test_tenant_admin_gets_403_for_sandbox_sites_list(self):
        """Tenant admin trying to list sites for sandbox tenant gets 403."""
        event = _make_sites_list_event(
            groups="TenantAdmins",
            tenant_id_claim=SANDBOX_TENANT_ID,
        )
        result = sites_list_handler(event, MagicMock())

        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "ACCESS_DENIED"

    def test_super_admin_can_list_sandbox_sites(self):
        """Super admin can list sandbox sites normally."""
        event = _make_sites_list_event(
            groups="SuperAdmins",
            tenant_id_claim="",
            tenant_id_query=SANDBOX_TENANT_ID,
        )
        with patch("sitespy.data.list_sites_for_tenant", return_value=[_SANDBOX_SITE_ITEM]):
            result = sites_list_handler(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["sites"]) == 1
        assert body["sites"][0]["site_id"] == "default_sandbox_site"
