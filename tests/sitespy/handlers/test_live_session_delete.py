"""Unit tests for DELETE /v1/sites/{site_id}/cameras/{camera_id}/live-session handler.

Tests cover:
- Active session → 200 {"status": "deleted"}
- No active session (None returned) → 404
- Expired session → 404
- DynamoDB delete error → 500

Requirements validated: 4.1, 4.2, 4.3
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


from sitespy.handlers.live_session import handler_delete


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_delete_event(
    site_id: str = "site_001",
    camera_id: str = "cam_01",
    groups: str = "TenantAdmins",
    tenant_id: str = "acme_corp",
    site_access: str = "",
    query_params: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a minimal API Gateway proxy event for the DELETE live-session handler."""
    return {
        "httpMethod": "DELETE",
        "path": f"/v1/sites/{site_id}/cameras/{camera_id}/live-session",
        "pathParameters": {"site_id": site_id, "camera_id": camera_id},
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


def _active_session_record() -> dict[str, Any]:
    """Return a session record with expires_at 5 minutes in the future."""
    future = datetime.now(timezone.utc) + timedelta(minutes=5)
    return {
        "PK": {"S": "TENANT#acme_corp"},
        "SK": {"S": "SESSION#site_001#cam_01"},
        "session_id": {"S": "sess-1234"},
        "expires_at": {"S": future.strftime("%Y-%m-%dT%H:%M:%SZ")},
        "ttl": {"N": str(int(future.timestamp()) + 3600)},
        "created_by": {"S": "user-sub-123"},
        "created_at": {"S": "2025-06-10T14:00:00Z"},
    }


def _expired_session_record() -> dict[str, Any]:
    """Return a session record with expires_at 5 minutes in the past."""
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    return {
        "PK": {"S": "TENANT#acme_corp"},
        "SK": {"S": "SESSION#site_001#cam_01"},
        "session_id": {"S": "sess-expired"},
        "expires_at": {"S": past.strftime("%Y-%m-%dT%H:%M:%SZ")},
        "ttl": {"N": str(int(past.timestamp()) + 3600)},
        "created_by": {"S": "user-sub-123"},
        "created_at": {"S": "2025-06-10T13:50:00Z"},
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDeleteActiveSession:
    """Requirement 4.1: Active session → 200 {"status": "deleted"}."""

    def test_active_session_returns_200_deleted(self):
        """DELETE with an active session deletes the record and returns 200."""
        with (
            patch(
                "sitespy.handlers.live_session.data.get_live_session",
                return_value=_active_session_record(),
            ),
            patch(
                "sitespy.handlers.live_session.data.delete_live_session",
                return_value=None,
            ) as mock_delete,
        ):
            result = handler_delete(_make_delete_event(), MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "deleted"
        mock_delete.assert_called_once_with("acme_corp", "site_001", "cam_01")

    def test_correlation_id_echoed(self):
        """Response includes the correlation ID from the request."""
        with (
            patch(
                "sitespy.handlers.live_session.data.get_live_session",
                return_value=_active_session_record(),
            ),
            patch(
                "sitespy.handlers.live_session.data.delete_live_session",
                return_value=None,
            ),
        ):
            result = handler_delete(_make_delete_event(), MagicMock())

        assert result["headers"]["X-Correlation-Id"] == "test-corr-id"


class TestDeleteNoActiveSession:
    """Requirement 4.3: No active session → 404."""

    def test_no_session_record_returns_404(self):
        """DELETE when no session record exists returns 404."""
        with patch(
            "sitespy.handlers.live_session.data.get_live_session",
            return_value=None,
        ):
            result = handler_delete(_make_delete_event(), MagicMock())

        assert result["statusCode"] == 404
        body = json.loads(result["body"])
        assert body["error"] == "NOT_FOUND"

    def test_expired_session_returns_404(self):
        """DELETE when session has expired returns 404."""
        with patch(
            "sitespy.handlers.live_session.data.get_live_session",
            return_value=_expired_session_record(),
        ):
            result = handler_delete(_make_delete_event(), MagicMock())

        assert result["statusCode"] == 404
        body = json.loads(result["body"])
        assert body["error"] == "NOT_FOUND"


class TestDeleteDynamoDBError:
    """Requirement 4.2: DynamoDB delete error → 500."""

    def test_delete_dynamo_error_returns_500(self):
        """DELETE when DynamoDB delete_live_session raises returns 500."""
        with (
            patch(
                "sitespy.handlers.live_session.data.get_live_session",
                return_value=_active_session_record(),
            ),
            patch(
                "sitespy.handlers.live_session.data.delete_live_session",
                side_effect=Exception("DynamoDB connection timeout"),
            ),
        ):
            result = handler_delete(_make_delete_event(), MagicMock())

        assert result["statusCode"] == 500
        body = json.loads(result["body"])
        assert body["error"] == "INTERNAL_ERROR"

    def test_get_session_dynamo_error_returns_500(self):
        """DELETE when DynamoDB get_live_session raises returns 500."""
        with patch(
            "sitespy.handlers.live_session.data.get_live_session",
            side_effect=Exception("DynamoDB service unavailable"),
        ):
            result = handler_delete(_make_delete_event(), MagicMock())

        assert result["statusCode"] == 500
        body = json.loads(result["body"])
        assert body["error"] == "INTERNAL_ERROR"
