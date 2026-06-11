"""Unit tests for POST /v1/sites/{site_id}/cameras/{camera_id}/live-session handler.

Tests cover:
- User with valid site access → 201
- User with missing site access → 403
- Super_Admin without tenant_id param → 400
- Camera not found → 404
- Session already active → 409
- ConditionalCheckFailedException on put_live_session → 409
- DynamoDB error on existence check → 500

Requirements validated: 2.1, 2.6, 2.7, 2.8, 2.9, 2.10, 2.12
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
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

from sitespy.handlers.live_session import handler_post  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CAMERA_ITEM = {
    "PK": {"S": "TENANT#acme_corp"},
    "SK": {"S": "SITE#site_001#CAM#cam_01"},
    "camera_name": {"S": "North elevation"},
}


def _make_event(
    site_id: str = "site_001",
    camera_id: str = "cam_01",
    groups: str = "",
    tenant_id: str = "acme_corp",
    site_access: str = "site_001",
    sub: str = "user-abc-123",
    query_params: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a minimal API Gateway proxy event for the live session POST handler."""
    return {
        "httpMethod": "POST",
        "path": f"/v1/sites/{site_id}/cameras/{camera_id}/live-session",
        "pathParameters": {"site_id": site_id, "camera_id": camera_id},
        "queryStringParameters": query_params or {},
        "headers": {"X-Correlation-Id": "test-corr-id"},
        "body": None,
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:groups": groups,
                    "custom:tenant_id": tenant_id,
                    "custom:site_access": site_access,
                    "sub": sub,
                }
            }
        },
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHandlerPostHappyPath:
    """Test: User with valid site access → 201."""

    def test_user_with_valid_site_access_returns_201(self):
        event = _make_event(
            groups="",
            tenant_id="acme_corp",
            site_access="site_001,site_002",
        )
        with (
            patch("sitespy.handlers.live_session.data.get_camera", return_value=_CAMERA_ITEM),
            patch("sitespy.handlers.live_session.data.get_live_session", return_value=None),
            patch("sitespy.handlers.live_session.data.put_live_session") as mock_put,
        ):
            result = handler_post(event, MagicMock())

        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        assert "session_id" in body
        assert "expires_at" in body
        assert body["camera_id"] == "cam_01"
        mock_put.assert_called_once()

    def test_tenant_admin_can_create_session(self):
        event = _make_event(
            groups="TenantAdmins",
            tenant_id="acme_corp",
            site_access="",
        )
        with (
            patch("sitespy.handlers.live_session.data.get_camera", return_value=_CAMERA_ITEM),
            patch("sitespy.handlers.live_session.data.get_live_session", return_value=None),
            patch("sitespy.handlers.live_session.data.put_live_session"),
        ):
            result = handler_post(event, MagicMock())

        assert result["statusCode"] == 201

    def test_super_admin_with_tenant_id_param_returns_201(self):
        event = _make_event(
            groups="SuperAdmins",
            tenant_id="",
            site_access="",
            query_params={"tenant_id": "acme_corp"},
        )
        with (
            patch("sitespy.handlers.live_session.data.get_camera", return_value=_CAMERA_ITEM),
            patch("sitespy.handlers.live_session.data.get_live_session", return_value=None),
            patch("sitespy.handlers.live_session.data.put_live_session"),
        ):
            result = handler_post(event, MagicMock())

        assert result["statusCode"] == 201

    def test_response_includes_correlation_id_header(self):
        event = _make_event()
        with (
            patch("sitespy.handlers.live_session.data.get_camera", return_value=_CAMERA_ITEM),
            patch("sitespy.handlers.live_session.data.get_live_session", return_value=None),
            patch("sitespy.handlers.live_session.data.put_live_session"),
        ):
            result = handler_post(event, MagicMock())

        assert result["headers"]["X-Correlation-Id"] == "test-corr-id"


class TestHandlerPostAccessDenied:
    """Test: User with missing site access → 403."""

    def test_user_without_site_access_returns_403(self):
        event = _make_event(
            groups="",
            tenant_id="acme_corp",
            site_access="site_002,site_003",  # site_001 NOT included
        )
        result = handler_post(event, MagicMock())

        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "ACCESS_DENIED"

    def test_user_with_empty_site_access_returns_403(self):
        event = _make_event(
            groups="",
            tenant_id="acme_corp",
            site_access="",
        )
        result = handler_post(event, MagicMock())

        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "ACCESS_DENIED"


class TestHandlerPostSuperAdminMissingTenantId:
    """Test: Super_Admin without tenant_id param → 400."""

    def test_super_admin_missing_tenant_id_returns_400(self):
        event = _make_event(
            groups="SuperAdmins",
            tenant_id="",
            site_access="",
            query_params={},
        )
        result = handler_post(event, MagicMock())

        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"

    def test_super_admin_empty_tenant_id_param_returns_400(self):
        event = _make_event(
            groups="SuperAdmins",
            tenant_id="",
            site_access="",
            query_params={"tenant_id": "  "},
        )
        result = handler_post(event, MagicMock())

        assert result["statusCode"] == 400


class TestHandlerPostCameraNotFound:
    """Test: camera not found → 404."""

    def test_camera_not_found_returns_404(self):
        event = _make_event()
        with patch("sitespy.handlers.live_session.data.get_camera", return_value=None):
            result = handler_post(event, MagicMock())

        assert result["statusCode"] == 404
        body = json.loads(result["body"])
        assert body["error"] == "NOT_FOUND"


class TestHandlerPostSessionAlreadyActive:
    """Test: session already active → 409."""

    def test_active_session_returns_409(self):
        future_time = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        existing_session = {
            "PK": {"S": "TENANT#acme_corp"},
            "SK": {"S": "SESSION#site_001#cam_01"},
            "session_id": {"S": "existing-session-id"},
            "expires_at": {"S": future_time},
        }
        event = _make_event()
        with (
            patch("sitespy.handlers.live_session.data.get_camera", return_value=_CAMERA_ITEM),
            patch(
                "sitespy.handlers.live_session.data.get_live_session",
                return_value=existing_session,
            ),
        ):
            result = handler_post(event, MagicMock())

        assert result["statusCode"] == 409
        body = json.loads(result["body"])
        assert body["error"] == "SESSION_ALREADY_ACTIVE"


class TestHandlerPostConditionalCheckFailed:
    """Test: ConditionalCheckFailedException on put_live_session → 409."""

    def test_conditional_check_failed_returns_409(self):
        # Simulate a race condition: get_live_session returns None (no session),
        # but put_live_session raises ConditionalCheckFailedException
        error_response = {
            "Error": {"Code": "ConditionalCheckFailedException", "Message": "Condition not met"}
        }
        exc = Exception("ConditionalCheckFailedException")
        exc.response = error_response

        event = _make_event()
        with (
            patch("sitespy.handlers.live_session.data.get_camera", return_value=_CAMERA_ITEM),
            patch("sitespy.handlers.live_session.data.get_live_session", return_value=None),
            patch("sitespy.handlers.live_session.data.put_live_session", side_effect=exc),
        ):
            result = handler_post(event, MagicMock())

        assert result["statusCode"] == 409
        body = json.loads(result["body"])
        assert body["error"] == "SESSION_ALREADY_ACTIVE"


class TestHandlerPostDynamoDBError:
    """Test: DynamoDB error on existence check → 500."""

    def test_dynamo_error_on_get_live_session_returns_500(self):
        event = _make_event()
        with (
            patch("sitespy.handlers.live_session.data.get_camera", return_value=_CAMERA_ITEM),
            patch(
                "sitespy.handlers.live_session.data.get_live_session",
                side_effect=Exception("DynamoDB unavailable"),
            ),
        ):
            result = handler_post(event, MagicMock())

        assert result["statusCode"] == 500
        body = json.loads(result["body"])
        assert body["error"] == "INTERNAL_ERROR"

    def test_dynamo_error_on_put_live_session_returns_500(self):
        # Non-ConditionalCheckFailedException error on put
        exc = Exception("DynamoDB write failed")
        # No .response attribute, so it's treated as a generic error

        event = _make_event()
        with (
            patch("sitespy.handlers.live_session.data.get_camera", return_value=_CAMERA_ITEM),
            patch("sitespy.handlers.live_session.data.get_live_session", return_value=None),
            patch("sitespy.handlers.live_session.data.put_live_session", side_effect=exc),
        ):
            result = handler_post(event, MagicMock())

        assert result["statusCode"] == 500
        body = json.loads(result["body"])
        assert body["error"] == "INTERNAL_ERROR"
