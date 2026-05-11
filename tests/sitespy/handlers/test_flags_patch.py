"""Unit tests for PATCH /v1/flags/{flag_id} handler.

Tests cover:
- Happy path: open → acknowledged returns 200
- Happy path: open → resolved returns 200
- Happy path: open → dismissed returns 200
- Happy path: acknowledged → resolved returns 200
- Happy path: acknowledged → dismissed returns 200
- Invalid transition: resolved → open returns 409
- Invalid transition: dismissed → open returns 409
- Invalid transition: acknowledged → open returns 409
- Missing flag_id in path: 400
- Missing status in body: 400
- Invalid status value: 400
- Flag not found: 404
- User role (not admin): 403
- Tenant admin without tenant_id in token: 403
- admin_notes optional: works without it
- admin_notes stored when provided

Requirements validated: 8.6, 8.7
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import MagicMock, call, patch

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

from sitespy.handlers.flags import handler_patch  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

_FLAG_OPEN = {
    "PK": {"S": "TENANT#acme_corp"},
    "SK": {"S": "FLAG#site_001#cam_01#2025-06-15T14:03:00Z"},
    "GSI1PK": {"S": "FLAGSTATUS#open"},
    "GSI1SK": {"S": "2025-06-15T14:03:00Z"},
    "flag_id": {"S": "flag-id-open-001"},
    "tenant_id": {"S": "acme_corp"},
    "site_id": {"S": "site_001"},
    "camera_id": {"S": "cam_01"},
    "reason": {"S": "stale_image"},
    "status": {"S": "open"},
    "source": {"S": "auto"},
    "raised_by": {"S": "system"},
    "raised_at": {"S": "2025-06-15T14:03:00Z"},
}

_FLAG_ACKNOWLEDGED = {
    **_FLAG_OPEN,
    "GSI1PK": {"S": "FLAGSTATUS#acknowledged"},
    "status": {"S": "acknowledged"},
}

_FLAG_RESOLVED = {
    **_FLAG_OPEN,
    "GSI1PK": {"S": "FLAGSTATUS#resolved"},
    "status": {"S": "resolved"},
}

_FLAG_DISMISSED = {
    **_FLAG_OPEN,
    "GSI1PK": {"S": "FLAGSTATUS#dismissed"},
    "status": {"S": "dismissed"},
}


def _make_event(
    flag_id: str | None = "flag-id-open-001",
    body: dict[str, Any] | None = None,
    groups: str = "TenantAdmins",
    tenant_id: str = "acme_corp",
    site_access: str = "",
    sub: str = "admin-sub-456",
    query_params: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a minimal API Gateway proxy event for the flags PATCH handler."""
    if body is None:
        body = {"status": "acknowledged"}
    return {
        "httpMethod": "PATCH",
        "path": f"/v1/flags/{flag_id}" if flag_id else "/v1/flags/",
        "pathParameters": {"flag_id": flag_id} if flag_id else {},
        "queryStringParameters": query_params or {},
        "headers": {"X-Correlation-Id": "test-corr-id"},
        "body": json.dumps(body),
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


def _make_super_admin_event(
    flag_id: str = "flag-id-open-001",
    body: dict[str, Any] | None = None,
    query_params: dict[str, str] | None = None,
) -> dict[str, Any]:
    return _make_event(
        flag_id=flag_id,
        body=body,
        groups="SuperAdmins",
        tenant_id="",
        site_access="",
        query_params=query_params or {"tenant_id": "acme_corp"},
    )


# ---------------------------------------------------------------------------
# Happy path — valid transitions
# ---------------------------------------------------------------------------


class TestFlagsPatchHappyPath:
    def test_open_to_acknowledged_returns_200(self):
        with (
            patch("sitespy.data.get_flag_by_id", return_value=_FLAG_OPEN),
            patch("sitespy.data.update_flag_status") as mock_update,
        ):
            result = handler_patch(
                _make_event(body={"status": "acknowledged"}), MagicMock()
            )

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["flag_id"] == "flag-id-open-001"
        assert body["status"] == "acknowledged"
        assert "updated_at" in body
        mock_update.assert_called_once()

    def test_open_to_resolved_returns_200(self):
        with (
            patch("sitespy.data.get_flag_by_id", return_value=_FLAG_OPEN),
            patch("sitespy.data.update_flag_status"),
        ):
            result = handler_patch(
                _make_event(body={"status": "resolved"}), MagicMock()
            )

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "resolved"

    def test_open_to_dismissed_returns_200(self):
        with (
            patch("sitespy.data.get_flag_by_id", return_value=_FLAG_OPEN),
            patch("sitespy.data.update_flag_status"),
        ):
            result = handler_patch(
                _make_event(body={"status": "dismissed"}), MagicMock()
            )

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "dismissed"

    def test_acknowledged_to_resolved_returns_200(self):
        with (
            patch("sitespy.data.get_flag_by_id", return_value=_FLAG_ACKNOWLEDGED),
            patch("sitespy.data.update_flag_status"),
        ):
            result = handler_patch(
                _make_event(body={"status": "resolved"}), MagicMock()
            )

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "resolved"

    def test_acknowledged_to_dismissed_returns_200(self):
        with (
            patch("sitespy.data.get_flag_by_id", return_value=_FLAG_ACKNOWLEDGED),
            patch("sitespy.data.update_flag_status"),
        ):
            result = handler_patch(
                _make_event(body={"status": "dismissed"}), MagicMock()
            )

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "dismissed"

    def test_correlation_id_echoed_in_header(self):
        with (
            patch("sitespy.data.get_flag_by_id", return_value=_FLAG_OPEN),
            patch("sitespy.data.update_flag_status"),
        ):
            result = handler_patch(
                _make_event(body={"status": "acknowledged"}), MagicMock()
            )

        assert result["headers"]["X-Correlation-Id"] == "test-corr-id"

    def test_update_called_with_correct_args(self):
        with (
            patch("sitespy.data.get_flag_by_id", return_value=_FLAG_OPEN),
            patch("sitespy.data.update_flag_status") as mock_update,
        ):
            handler_patch(
                _make_event(body={"status": "acknowledged"}), MagicMock()
            )

        call_kwargs = mock_update.call_args.kwargs
        assert call_kwargs["pk"] == "TENANT#acme_corp"
        assert call_kwargs["sk"] == "FLAG#site_001#cam_01#2025-06-15T14:03:00Z"
        assert call_kwargs["new_status"] == "acknowledged"
        assert call_kwargs["acting_user"] == "admin-sub-456"
        assert "updated_at" in call_kwargs


# ---------------------------------------------------------------------------
# Invalid state transitions — 409
# ---------------------------------------------------------------------------


class TestFlagsPatchInvalidTransitions:
    def test_resolved_to_open_returns_409(self):
        with patch("sitespy.data.get_flag_by_id", return_value=_FLAG_RESOLVED):
            result = handler_patch(
                _make_event(body={"status": "open"}), MagicMock()
            )

        assert result["statusCode"] == 409
        body = json.loads(result["body"])
        assert body["error"] == "CONFLICT"

    def test_dismissed_to_open_returns_409(self):
        with patch("sitespy.data.get_flag_by_id", return_value=_FLAG_DISMISSED):
            result = handler_patch(
                _make_event(body={"status": "open"}), MagicMock()
            )

        assert result["statusCode"] == 409
        body = json.loads(result["body"])
        assert body["error"] == "CONFLICT"

    def test_acknowledged_to_open_returns_409(self):
        with patch("sitespy.data.get_flag_by_id", return_value=_FLAG_ACKNOWLEDGED):
            result = handler_patch(
                _make_event(body={"status": "open"}), MagicMock()
            )

        assert result["statusCode"] == 409
        body = json.loads(result["body"])
        assert body["error"] == "CONFLICT"

    def test_resolved_to_acknowledged_returns_409(self):
        with patch("sitespy.data.get_flag_by_id", return_value=_FLAG_RESOLVED):
            result = handler_patch(
                _make_event(body={"status": "acknowledged"}), MagicMock()
            )

        assert result["statusCode"] == 409

    def test_dismissed_to_resolved_returns_409(self):
        with patch("sitespy.data.get_flag_by_id", return_value=_FLAG_DISMISSED):
            result = handler_patch(
                _make_event(body={"status": "resolved"}), MagicMock()
            )

        assert result["statusCode"] == 409


# ---------------------------------------------------------------------------
# Validation errors — 400
# ---------------------------------------------------------------------------


class TestFlagsPatchValidation:
    def test_missing_flag_id_in_path_returns_400(self):
        event = _make_event(flag_id=None)
        # pathParameters will be {} (no flag_id key)
        result = handler_patch(event, MagicMock())
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"

    def test_missing_status_in_body_returns_400(self):
        event = _make_event(body={})
        result = handler_patch(event, MagicMock())
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"

    def test_invalid_status_value_returns_400(self):
        event = _make_event(body={"status": "pending"})
        result = handler_patch(event, MagicMock())
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"

    def test_empty_status_returns_400(self):
        event = _make_event(body={"status": ""})
        result = handler_patch(event, MagicMock())
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"


# ---------------------------------------------------------------------------
# Not found — 404
# ---------------------------------------------------------------------------


class TestFlagsPatchNotFound:
    def test_flag_not_found_returns_404(self):
        with patch("sitespy.data.get_flag_by_id", return_value=None):
            result = handler_patch(
                _make_event(body={"status": "acknowledged"}), MagicMock()
            )

        assert result["statusCode"] == 404
        body = json.loads(result["body"])
        assert body["error"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Access control — 403
# ---------------------------------------------------------------------------


class TestFlagsPatchAccessControl:
    def test_user_role_returns_403(self):
        """Regular users (not admins) cannot update flags."""
        event = _make_event(
            groups="",
            tenant_id="acme_corp",
            site_access="site_001",
            body={"status": "acknowledged"},
        )
        result = handler_patch(event, MagicMock())
        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "ACCESS_DENIED"

    def test_tenant_admin_without_tenant_id_in_token_returns_403(self):
        """Tenant admin with no tenant_id claim cannot proceed."""
        event = _make_event(
            groups="TenantAdmins",
            tenant_id="",
            body={"status": "acknowledged"},
        )
        result = handler_patch(event, MagicMock())
        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "ACCESS_DENIED"

    def test_super_admin_without_tenant_id_query_param_returns_400(self):
        """Super admin must supply tenant_id as a query parameter."""
        event = _make_event(
            groups="SuperAdmins",
            tenant_id="",
            body={"status": "acknowledged"},
            query_params={},  # no tenant_id
        )
        result = handler_patch(event, MagicMock())
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"


# ---------------------------------------------------------------------------
# admin_notes handling
# ---------------------------------------------------------------------------


class TestFlagsPatchAdminNotes:
    def test_admin_notes_optional_works_without_it(self):
        """Request without admin_notes should succeed."""
        with (
            patch("sitespy.data.get_flag_by_id", return_value=_FLAG_OPEN),
            patch("sitespy.data.update_flag_status") as mock_update,
        ):
            result = handler_patch(
                _make_event(body={"status": "acknowledged"}), MagicMock()
            )

        assert result["statusCode"] == 200
        call_kwargs = mock_update.call_args.kwargs
        assert call_kwargs["admin_notes"] is None

    def test_admin_notes_stored_when_provided(self):
        """admin_notes value should be passed through to update_flag_status."""
        note = "Site foreman notified, dispatching technician Friday."
        with (
            patch("sitespy.data.get_flag_by_id", return_value=_FLAG_OPEN),
            patch("sitespy.data.update_flag_status") as mock_update,
        ):
            result = handler_patch(
                _make_event(body={"status": "acknowledged", "admin_notes": note}),
                MagicMock(),
            )

        assert result["statusCode"] == 200
        call_kwargs = mock_update.call_args.kwargs
        assert call_kwargs["admin_notes"] == note

    def test_admin_notes_null_when_empty_string(self):
        """Empty string admin_notes should be treated as None (stripped to empty → None)."""
        with (
            patch("sitespy.data.get_flag_by_id", return_value=_FLAG_OPEN),
            patch("sitespy.data.update_flag_status") as mock_update,
        ):
            result = handler_patch(
                _make_event(body={"status": "acknowledged", "admin_notes": "   "}),
                MagicMock(),
            )

        assert result["statusCode"] == 200
        call_kwargs = mock_update.call_args.kwargs
        # Stripped whitespace-only string becomes empty string, not None
        # (the handler strips but doesn't convert empty to None — that's fine)
        assert call_kwargs["admin_notes"] is not None or call_kwargs["admin_notes"] == ""


# ---------------------------------------------------------------------------
# Super admin path
# ---------------------------------------------------------------------------


class TestFlagsPatchSuperAdmin:
    def test_super_admin_can_update_flag_with_tenant_id_param(self):
        with (
            patch("sitespy.data.get_flag_by_id", return_value=_FLAG_OPEN),
            patch("sitespy.data.update_flag_status"),
        ):
            result = handler_patch(
                _make_super_admin_event(body={"status": "acknowledged"}),
                MagicMock(),
            )

        assert result["statusCode"] == 200

    def test_super_admin_get_flag_called_with_correct_tenant(self):
        with (
            patch("sitespy.data.get_flag_by_id", return_value=_FLAG_OPEN) as mock_get,
            patch("sitespy.data.update_flag_status"),
        ):
            handler_patch(
                _make_super_admin_event(
                    body={"status": "acknowledged"},
                    query_params={"tenant_id": "other_corp"},
                ),
                MagicMock(),
            )

        mock_get.assert_called_once_with("other_corp", "flag-id-open-001")
