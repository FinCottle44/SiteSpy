"""Unit tests for GET /v1/sites/{site_id} handler.

Tests cover:
- Happy path: site + cameras returned for tenant admin
- Happy path: super admin with tenant_id query param
- Happy path: user with site in site_access
- 400 when site_id is missing
- 400 when super admin omits tenant_id query param
- 403 when tenant admin accesses wrong tenant's site
- 403 when user's site_id not in site_access
- 404 when site does not exist
- Correct camera list parsing (SK prefix stripping)
- Empty camera list
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Environment setup (must happen before importing handler)
# ---------------------------------------------------------------------------

os.environ.setdefault("SNAPSHOTS_BUCKET", "test-snapshots-bucket")
os.environ.setdefault("DATA_TABLE", "test-data-table")
os.environ.setdefault("AWS_REGION", "eu-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-west-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("LOG_LEVEL", "INFO")

from sitespy.handlers.sites import _handle, handler  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SITE_ITEM = {
    "PK": {"S": "TENANT#acme_corp"},
    "SK": {"S": "SITE#site_001"},
    "site_name": {"S": "Acme Tower — Phase 2"},
    "latitude": {"N": "51.5074"},
    "longitude": {"N": "-0.1278"},
    "timezone": {"S": "Europe/London"},
}

_CAMERA_ITEMS = [
    {
        "PK": {"S": "TENANT#acme_corp"},
        "SK": {"S": "SITE#site_001#CAM#cam_01"},
        "camera_name": {"S": "North elevation"},
        "camera_model": {"S": "Axis P1455-LE"},
    },
    {
        "PK": {"S": "TENANT#acme_corp"},
        "SK": {"S": "SITE#site_001#CAM#cam_02"},
        "camera_name": {"S": "Crane cab"},
        "camera_model": {"S": "Axis P1455-LE"},
    },
]


def _make_event(
    site_id: str = "site_001",
    groups: str = "TenantAdmins",
    tenant_id: str = "acme_corp",
    site_access: str = "",
    query_params: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a minimal API Gateway proxy event for the sites handler."""
    return {
        "httpMethod": "GET",
        "path": f"/v1/sites/{site_id}",
        "pathParameters": {"site_id": site_id},
        "queryStringParameters": query_params or {},
        "headers": {"X-Correlation-Id": "test-corr-id"},
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:groups": groups,
                    "custom:tenant_id": tenant_id,
                    "custom:site_access": site_access,
                }
            }
        },
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSitesHandlerHappyPath:
    def test_tenant_admin_gets_site_and_cameras(self):
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch("sitespy.data.get_cameras_for_site", return_value=_CAMERA_ITEMS),
        ):
            result = handler(_make_event(), MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["site_id"] == "site_001"
        assert body["site_name"] == "Acme Tower — Phase 2"
        assert body["tenant_id"] == "acme_corp"
        assert body["latitude"] == 51.5074
        assert body["longitude"] == -0.1278
        assert body["timezone"] == "Europe/London"
        assert len(body["cameras"]) == 2

    def test_camera_ids_parsed_from_sk(self):
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch("sitespy.data.get_cameras_for_site", return_value=_CAMERA_ITEMS),
        ):
            result = handler(_make_event(), MagicMock())

        body = json.loads(result["body"])
        camera_ids = {c["camera_id"] for c in body["cameras"]}
        assert camera_ids == {"cam_01", "cam_02"}

    def test_camera_attributes_present(self):
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch("sitespy.data.get_cameras_for_site", return_value=_CAMERA_ITEMS),
        ):
            result = handler(_make_event(), MagicMock())

        body = json.loads(result["body"])
        cam = next(c for c in body["cameras"] if c["camera_id"] == "cam_01")
        assert cam["camera_name"] == "North elevation"
        assert cam["camera_model"] == "Axis P1455-LE"

    def test_empty_camera_list(self):
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch("sitespy.data.get_cameras_for_site", return_value=[]),
        ):
            result = handler(_make_event(), MagicMock())

        body = json.loads(result["body"])
        assert body["cameras"] == []

    def test_user_with_site_access(self):
        event = _make_event(
            groups="",
            tenant_id="acme_corp",
            site_access="site_001,site_002",
        )
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch("sitespy.data.get_cameras_for_site", return_value=[]),
        ):
            result = handler(event, MagicMock())

        assert result["statusCode"] == 200

    def test_super_admin_with_tenant_id_query_param(self):
        event = _make_event(
            groups="SuperAdmins",
            tenant_id="",
            query_params={"tenant_id": "acme_corp"},
        )
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch("sitespy.data.get_cameras_for_site", return_value=_CAMERA_ITEMS),
        ):
            result = handler(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["tenant_id"] == "acme_corp"

    def test_correlation_id_echoed_in_header(self):
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch("sitespy.data.get_cameras_for_site", return_value=[]),
        ):
            event = _make_event()
            result = handler(event, MagicMock())

        # The handler generates a correlation ID from the header or a fresh UUID
        assert "X-Correlation-Id" in result["headers"]
        assert result["headers"]["X-Correlation-Id"] == "test-corr-id"


class TestSitesHandlerErrors:
    def test_missing_site_id_returns_400(self):
        event = _make_event(site_id="")
        event["pathParameters"] = {}
        result = handler(event, MagicMock())
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"

    def test_site_not_found_returns_404(self):
        with patch("sitespy.data.get_site", return_value=None):
            result = handler(_make_event(), MagicMock())

        assert result["statusCode"] == 404
        body = json.loads(result["body"])
        assert body["error"] == "NOT_FOUND"

    def test_tenant_admin_site_not_in_their_tenant_returns_404(self):
        """Tenant admin from 'other_corp' gets 404 — site doesn't exist under their tenant."""
        event = _make_event(groups="TenantAdmins", tenant_id="other_corp")
        # The site doesn't exist under other_corp's namespace
        with patch("sitespy.data.get_site", return_value=None):
            result = handler(event, MagicMock())

        assert result["statusCode"] == 404

    def test_user_wrong_tenant_returns_404(self):
        """User from 'other_corp' gets 404 — site doesn't exist under their tenant."""
        event = _make_event(
            groups="",
            tenant_id="other_corp",
            site_access="site_001",
        )
        with patch("sitespy.data.get_site", return_value=None):
            result = handler(event, MagicMock())

        assert result["statusCode"] == 404

    def test_user_site_not_in_access_list_returns_403(self):
        event = _make_event(
            groups="",
            tenant_id="acme_corp",
            site_access="site_002,site_003",  # site_001 not included
        )
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch("sitespy.data.get_cameras_for_site", return_value=[]),
        ):
            result = handler(event, MagicMock())

        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "ACCESS_DENIED"

    def test_super_admin_missing_tenant_id_param_returns_400(self):
        event = _make_event(
            groups="SuperAdmins",
            tenant_id="",
            query_params={},  # no tenant_id
        )
        result = handler(event, MagicMock())
        assert result["statusCode"] == 400

    def test_tenant_admin_no_tenant_id_in_token_returns_403(self):
        """Tenant admin with empty custom:tenant_id claim is denied."""
        event = _make_event(groups="TenantAdmins", tenant_id="")
        result = handler(event, MagicMock())
        assert result["statusCode"] == 403
