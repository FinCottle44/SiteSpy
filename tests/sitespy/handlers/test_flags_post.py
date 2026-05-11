"""Unit tests for POST /v1/flags handler.

Tests cover:
- Happy path: new flag created, returns 201
- Duplicate: existing open flag, returns 200 with duplicate=true
- Duplicate: existing acknowledged flag, returns 200 with duplicate=true
- Missing site_id: 400
- Missing camera_id: 400
- Missing reason: 400
- Invalid reason: 400
- reason=other without note: 400
- reason=other with note: 201
- note too long (>1000 chars): 400
- Access denied: 403
- Site not found (camera not found): 404

Requirements validated: 7.5, 7.6
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

from sitespy.handlers.flags import handler_post  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CAMERA_ITEM = {
    "PK": {"S": "TENANT#acme_corp"},
    "SK": {"S": "SITE#site_001#CAM#cam_01"},
    "camera_name": {"S": "North elevation"},
    "camera_model": {"S": "Axis P1455-LE"},
}

_EXISTING_OPEN_FLAG = {
    "PK": {"S": "TENANT#acme_corp"},
    "SK": {"S": "FLAG#site_001#cam_01#2025-06-15T14:03:00Z"},
    "GSI1PK": {"S": "FLAGSTATUS#open"},
    "GSI1SK": {"S": "2025-06-15T14:03:00Z"},
    "flag_id": {"S": "existing-flag-id-open"},
    "tenant_id": {"S": "acme_corp"},
    "site_id": {"S": "site_001"},
    "camera_id": {"S": "cam_01"},
    "reason": {"S": "physical_damage"},
    "status": {"S": "open"},
    "source": {"S": "user"},
    "raised_by": {"S": "user-sub-123"},
    "raised_at": {"S": "2025-06-15T14:03:00Z"},
}

_EXISTING_ACKNOWLEDGED_FLAG = {
    **_EXISTING_OPEN_FLAG,
    "flag_id": {"S": "existing-flag-id-ack"},
    "GSI1PK": {"S": "FLAGSTATUS#acknowledged"},
    "status": {"S": "acknowledged"},
}


def _make_event(
    body: dict[str, Any] | None = None,
    groups: str = "TenantAdmins",
    tenant_id: str = "acme_corp",
    site_access: str = "",
    sub: str = "user-sub-123",
    query_params: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a minimal API Gateway proxy event for the flags POST handler."""
    if body is None:
        body = {
            "site_id": "site_001",
            "camera_id": "cam_01",
            "reason": "physical_damage",
            "note": "Mount has drooped ~30°",
        }
    return {
        "httpMethod": "POST",
        "path": "/v1/flags",
        "pathParameters": None,
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


# ---------------------------------------------------------------------------
# Happy path tests
# ---------------------------------------------------------------------------


class TestFlagsPostHappyPath:
    def test_new_flag_returns_201(self):
        with (
            patch("sitespy.data.get_camera", return_value=_CAMERA_ITEM),
            patch("sitespy.data.get_open_flag", return_value=None),
            patch("sitespy.data.put_flag") as mock_put,
        ):
            result = handler_post(_make_event(), MagicMock())

        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        assert "flag_id" in body
        assert body["status"] == "open"
        assert "raised_at" in body
        assert "duplicate" not in body
        mock_put.assert_called_once()

    def test_new_flag_put_called_with_correct_args(self):
        with (
            patch("sitespy.data.get_camera", return_value=_CAMERA_ITEM),
            patch("sitespy.data.get_open_flag", return_value=None),
            patch("sitespy.data.put_flag") as mock_put,
        ):
            handler_post(_make_event(), MagicMock())

        call_kwargs = mock_put.call_args.kwargs
        assert call_kwargs["tenant_id"] == "acme_corp"
        assert call_kwargs["site_id"] == "site_001"
        assert call_kwargs["camera_id"] == "cam_01"
        assert call_kwargs["reason"] == "physical_damage"
        assert call_kwargs["note"] == "Mount has drooped ~30°"
        assert call_kwargs["raised_by"] == "user-sub-123"

    def test_reason_other_with_note_returns_201(self):
        event = _make_event(
            body={
                "site_id": "site_001",
                "camera_id": "cam_01",
                "reason": "other",
                "note": "Something unusual is happening",
            }
        )
        with (
            patch("sitespy.data.get_camera", return_value=_CAMERA_ITEM),
            patch("sitespy.data.get_open_flag", return_value=None),
            patch("sitespy.data.put_flag"),
        ):
            result = handler_post(event, MagicMock())

        assert result["statusCode"] == 201

    def test_correlation_id_echoed_in_header(self):
        with (
            patch("sitespy.data.get_camera", return_value=_CAMERA_ITEM),
            patch("sitespy.data.get_open_flag", return_value=None),
            patch("sitespy.data.put_flag"),
        ):
            result = handler_post(_make_event(), MagicMock())

        assert result["headers"]["X-Correlation-Id"] == "test-corr-id"

    def test_user_with_site_access_returns_201(self):
        event = _make_event(
            groups="",
            tenant_id="acme_corp",
            site_access="site_001,site_002",
        )
        with (
            patch("sitespy.data.get_camera", return_value=_CAMERA_ITEM),
            patch("sitespy.data.get_open_flag", return_value=None),
            patch("sitespy.data.put_flag"),
        ):
            result = handler_post(event, MagicMock())

        assert result["statusCode"] == 201


# ---------------------------------------------------------------------------
# Duplicate suppression tests
# ---------------------------------------------------------------------------


class TestFlagsPostDuplicateSuppression:
    def test_existing_open_flag_returns_200_with_duplicate_true(self):
        with (
            patch("sitespy.data.get_camera", return_value=_CAMERA_ITEM),
            patch("sitespy.data.get_open_flag", return_value=_EXISTING_OPEN_FLAG),
            patch("sitespy.data.put_flag") as mock_put,
        ):
            result = handler_post(_make_event(), MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["flag_id"] == "existing-flag-id-open"
        assert body["status"] == "open"
        assert body["raised_at"] == "2025-06-15T14:03:00Z"
        assert body["duplicate"] is True
        mock_put.assert_not_called()

    def test_existing_acknowledged_flag_returns_200_with_duplicate_true(self):
        with (
            patch("sitespy.data.get_camera", return_value=_CAMERA_ITEM),
            patch("sitespy.data.get_open_flag", return_value=_EXISTING_ACKNOWLEDGED_FLAG),
            patch("sitespy.data.put_flag") as mock_put,
        ):
            result = handler_post(_make_event(), MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["flag_id"] == "existing-flag-id-ack"
        assert body["status"] == "acknowledged"
        assert body["duplicate"] is True
        mock_put.assert_not_called()


# ---------------------------------------------------------------------------
# Validation error tests
# ---------------------------------------------------------------------------


class TestFlagsPostValidation:
    def test_missing_site_id_returns_400(self):
        event = _make_event(
            body={"camera_id": "cam_01", "reason": "physical_damage"}
        )
        result = handler_post(event, MagicMock())
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"

    def test_missing_camera_id_returns_400(self):
        event = _make_event(
            body={"site_id": "site_001", "reason": "physical_damage"}
        )
        result = handler_post(event, MagicMock())
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"

    def test_missing_reason_returns_400(self):
        event = _make_event(
            body={"site_id": "site_001", "camera_id": "cam_01"}
        )
        result = handler_post(event, MagicMock())
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"

    def test_invalid_reason_returns_400(self):
        event = _make_event(
            body={"site_id": "site_001", "camera_id": "cam_01", "reason": "broken_lens"}
        )
        result = handler_post(event, MagicMock())
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"

    def test_reason_other_without_note_returns_400(self):
        event = _make_event(
            body={"site_id": "site_001", "camera_id": "cam_01", "reason": "other"}
        )
        result = handler_post(event, MagicMock())
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"

    def test_note_too_long_returns_400(self):
        event = _make_event(
            body={
                "site_id": "site_001",
                "camera_id": "cam_01",
                "reason": "physical_damage",
                "note": "x" * 1001,
            }
        )
        result = handler_post(event, MagicMock())
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"

    def test_note_exactly_1000_chars_is_valid(self):
        event = _make_event(
            body={
                "site_id": "site_001",
                "camera_id": "cam_01",
                "reason": "physical_damage",
                "note": "x" * 1000,
            }
        )
        with (
            patch("sitespy.data.get_camera", return_value=_CAMERA_ITEM),
            patch("sitespy.data.get_open_flag", return_value=None),
            patch("sitespy.data.put_flag"),
        ):
            result = handler_post(event, MagicMock())

        assert result["statusCode"] == 201


# ---------------------------------------------------------------------------
# Access control tests
# ---------------------------------------------------------------------------


class TestFlagsPostAccessControl:
    def test_user_without_site_access_returns_403(self):
        event = _make_event(
            groups="",
            tenant_id="acme_corp",
            site_access="site_002,site_003",  # site_001 not included
        )
        with patch("sitespy.data.get_camera", return_value=_CAMERA_ITEM):
            result = handler_post(event, MagicMock())

        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "ACCESS_DENIED"

    def test_user_wrong_tenant_camera_not_found_returns_404(self):
        """User from other_corp with site_001 in access list gets 404 — camera doesn't exist under other_corp."""
        event = _make_event(
            groups="",
            tenant_id="other_corp",
            site_access="site_001",
        )
        with patch("sitespy.data.get_camera", return_value=None):
            result = handler_post(event, MagicMock())

        assert result["statusCode"] == 404

    def test_tenant_admin_no_tenant_id_in_token_returns_403(self):
        event = _make_event(groups="TenantAdmins", tenant_id="")
        result = handler_post(event, MagicMock())
        assert result["statusCode"] == 403


# ---------------------------------------------------------------------------
# Not found tests
# ---------------------------------------------------------------------------


class TestFlagsPostNotFound:
    def test_camera_not_found_returns_404(self):
        with patch("sitespy.data.get_camera", return_value=None):
            result = handler_post(_make_event(), MagicMock())

        assert result["statusCode"] == 404
        body = json.loads(result["body"])
        assert body["error"] == "NOT_FOUND"
