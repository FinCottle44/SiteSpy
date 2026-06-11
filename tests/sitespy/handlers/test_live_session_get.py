"""Unit tests for GET /v1/sites/{site_id}/cameras/{camera_id}/live-session handler.

Tests cover:
- Active session with live image → 200 with presigned_url
- Active session, no live image yet → 200 latest_image: null
- No session record → 200 {"status": "none"}
- Expired session record → 200 {"status": "none"}
- DynamoDB error on session lookup → 500
- DynamoDB error on live img query → 200 with session fields, no latest_image

Requirements validated: 3.1, 3.2, 3.3, 3.4, 3.7, 3.8
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

from sitespy.handlers.live_session import handler_get  # noqa: E402

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

_NOW = datetime.now(timezone.utc)
_EXPIRES_FUTURE = (_NOW + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
_EXPIRES_PAST = (_NOW - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
_SESSION_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_PRESIGNED_URL = "https://s3.amazonaws.com/test-bucket/signed-url"
_CAPTURED_AT = (_NOW - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
_S3_KEY = "live/acme_corp/site_001/cam_01/2025-06-15T14:04:00Z.jpg"

_ACTIVE_SESSION_RECORD = {
    "PK": {"S": "TENANT#acme_corp"},
    "SK": {"S": "SESSION#site_001#cam_01"},
    "session_id": {"S": _SESSION_ID},
    "expires_at": {"S": _EXPIRES_FUTURE},
    "ttl": {"N": "9999999999"},
    "created_by": {"S": "user-sub-123"},
    "created_at": {"S": "2025-06-15T14:00:00Z"},
}

_EXPIRED_SESSION_RECORD = {
    "PK": {"S": "TENANT#acme_corp"},
    "SK": {"S": "SESSION#site_001#cam_01"},
    "session_id": {"S": _SESSION_ID},
    "expires_at": {"S": _EXPIRES_PAST},
    "ttl": {"N": "9999999999"},
    "created_by": {"S": "user-sub-123"},
    "created_at": {"S": "2025-06-15T14:00:00Z"},
}

_LIVE_IMG_RECORD = {
    "PK": {"S": "TENANT#acme_corp"},
    "SK": {"S": "LIVE_IMG#site_001#cam_01#2025-06-15T14:04:00Z"},
    "s3_key": {"S": _S3_KEY},
    "captured_at": {"S": _CAPTURED_AT},
    "sha256": {"S": "abc123def456"},
    "size_bytes": {"N": "102400"},
    "ttl": {"N": "9999999999"},
}


# ---------------------------------------------------------------------------
# Event builder helper
# ---------------------------------------------------------------------------


def _make_event(
    site_id: str = "site_001",
    camera_id: str = "cam_01",
    groups: str = "TenantAdmins",
    tenant_id: str = "acme_corp",
    site_access: str = "",
) -> dict[str, Any]:
    """Build a minimal API Gateway proxy event for GET live-session."""
    return {
        "httpMethod": "GET",
        "path": f"/v1/sites/{site_id}/cameras/{camera_id}/live-session",
        "pathParameters": {"site_id": site_id, "camera_id": camera_id},
        "queryStringParameters": {},
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


class TestGetLiveSessionActiveWithImage:
    """Test: active session with live image → 200 with presigned_url."""

    def test_returns_200_with_presigned_url(self):
        event = _make_event()
        with (
            patch("sitespy.handlers.live_session.data.get_live_session", return_value=_ACTIVE_SESSION_RECORD),
            patch("sitespy.handlers.live_session.data.get_latest_live_img_record", return_value=_LIVE_IMG_RECORD),
            patch("sitespy.handlers.live_session.storage.generate_presigned_url", return_value=_PRESIGNED_URL),
        ):
            result = handler_get(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "active"
        assert body["session_id"] == _SESSION_ID
        assert body["expires_at"] == _EXPIRES_FUTURE
        assert body["latest_image"]["presigned_url"] == _PRESIGNED_URL
        assert body["latest_image"]["captured_at"] == _CAPTURED_AT
        assert body["latest_image"]["expires_in"] == 300

    def test_generate_presigned_url_called_with_correct_args(self):
        event = _make_event()
        mock_presign = MagicMock(return_value=_PRESIGNED_URL)
        with (
            patch("sitespy.handlers.live_session.data.get_live_session", return_value=_ACTIVE_SESSION_RECORD),
            patch("sitespy.handlers.live_session.data.get_latest_live_img_record", return_value=_LIVE_IMG_RECORD),
            patch("sitespy.handlers.live_session.storage.generate_presigned_url", mock_presign),
        ):
            handler_get(event, MagicMock())

        mock_presign.assert_called_once_with(_S3_KEY, expires_in=300)


class TestGetLiveSessionActiveNoImage:
    """Test: active session, no live image yet → 200 latest_image: null."""

    def test_returns_200_with_null_latest_image(self):
        event = _make_event()
        with (
            patch("sitespy.handlers.live_session.data.get_live_session", return_value=_ACTIVE_SESSION_RECORD),
            patch("sitespy.handlers.live_session.data.get_latest_live_img_record", return_value=None),
        ):
            result = handler_get(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "active"
        assert body["session_id"] == _SESSION_ID
        assert body["expires_at"] == _EXPIRES_FUTURE
        assert body["latest_image"] is None


class TestGetLiveSessionNoRecord:
    """Test: no session record → 200 {"status": "none"}."""

    def test_returns_status_none_when_no_session(self):
        event = _make_event()
        with patch("sitespy.handlers.live_session.data.get_live_session", return_value=None):
            result = handler_get(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body == {"status": "none"}


class TestGetLiveSessionExpired:
    """Test: expired session record → 200 {"status": "none"}."""

    def test_returns_status_none_when_session_expired(self):
        event = _make_event()
        with patch("sitespy.handlers.live_session.data.get_live_session", return_value=_EXPIRED_SESSION_RECORD):
            result = handler_get(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body == {"status": "none"}


class TestGetLiveSessionDynamoErrorOnSessionLookup:
    """Test: DynamoDB error on session lookup → 500."""

    def test_returns_500_on_dynamo_session_error(self):
        event = _make_event()
        with patch(
            "sitespy.handlers.live_session.data.get_live_session",
            side_effect=Exception("DynamoDB connection error"),
        ):
            result = handler_get(event, MagicMock())

        assert result["statusCode"] == 500
        body = json.loads(result["body"])
        assert body["error"] == "INTERNAL_ERROR"


class TestGetLiveSessionDynamoErrorOnLiveImgQuery:
    """Test: DynamoDB error on live img query → 200 with session fields, no latest_image."""

    def test_returns_200_with_session_fields_no_latest_image(self):
        event = _make_event()
        with (
            patch("sitespy.handlers.live_session.data.get_live_session", return_value=_ACTIVE_SESSION_RECORD),
            patch(
                "sitespy.handlers.live_session.data.get_latest_live_img_record",
                side_effect=Exception("DynamoDB query failed"),
            ),
        ):
            result = handler_get(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "active"
        assert body["session_id"] == _SESSION_ID
        assert body["expires_at"] == _EXPIRES_FUTURE
        assert "latest_image" not in body
