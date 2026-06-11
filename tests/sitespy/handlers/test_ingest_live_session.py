"""Unit tests for live session check and response shapes in ingest handler.

Tests cover:
- Active session + timelapse saved → 201 with live_captured: true
- Active session + cadence suppressed timelapse → 200 skipped with live_captured: true
- No active session + timelapse saved → 201 with live_captured: false
- No active session + cadence suppressed → 200 skipped with live_captured: false
- Live S3 write failure → 500

Validates: Requirements 5.1, 5.3, 5.4, 5.5, 5.9, 5.10, 5.11
"""

from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

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

from sitespy.handlers.ingest import _handle, handler  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TENANT_ID = "acme_corp"
_SITE_ID = "site_001"
_CAMERA_ID = "cam_01"
_TOKEN = "tk_" + "a" * 40
_JPEG_BODY = b"\xff\xd8\xff\xe0" + b"\x00" * 100

_CAMERA_ITEM = {
    "PK": {"S": f"TENANT#{_TENANT_ID}"},
    "SK": {"S": f"SITE#{_SITE_ID}#CAM#{_CAMERA_ID}"},
    "GSI1PK": {"S": f"TOKEN#{_TOKEN}"},
    "GSI1SK": {"S": "CAMERA"},
    "camera_name": {"S": "North elevation"},
    "ingest_token": {"S": _TOKEN},
}


def _active_session_record() -> dict:
    """Return a SESSION# record with expires_at 5 minutes in the future."""
    expires_at = (datetime.now(tz=UTC) + timedelta(minutes=5)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return {
        "PK": {"S": f"TENANT#{_TENANT_ID}"},
        "SK": {"S": f"SESSION#{_SITE_ID}#{_CAMERA_ID}"},
        "session_id": {"S": "test-session-id"},
        "expires_at": {"S": expires_at},
        "ttl": {"N": "9999999999"},
        "created_by": {"S": "user-sub-123"},
        "created_at": {"S": "2025-06-15T14:00:00Z"},
    }


def _recent_img_record() -> dict:
    """Return an IMG# record with ingested_at 2 minutes ago (within cadence)."""
    ingested_at = (datetime.now(tz=UTC) - timedelta(minutes=2)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return {
        "PK": {"S": f"TENANT#{_TENANT_ID}"},
        "SK": {"S": f"IMG#{_SITE_ID}#{_CAMERA_ID}#{ingested_at}"},
        "ingested_at": {"S": ingested_at},
        "s3_key": {"S": f"{_TENANT_ID}/{_SITE_ID}/{_CAMERA_ID}/old.jpg"},
    }


def _old_img_record() -> dict:
    """Return an IMG# record with ingested_at 20 minutes ago (cadence passed)."""
    ingested_at = (datetime.now(tz=UTC) - timedelta(minutes=20)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return {
        "PK": {"S": f"TENANT#{_TENANT_ID}"},
        "SK": {"S": f"IMG#{_SITE_ID}#{_CAMERA_ID}#{ingested_at}"},
        "ingested_at": {"S": ingested_at},
        "s3_key": {"S": f"{_TENANT_ID}/{_SITE_ID}/{_CAMERA_ID}/old.jpg"},
    }


def _make_event() -> dict:
    """Build a token-based ingest event with a valid JPEG body."""
    return {
        "httpMethod": "POST",
        "path": f"/v1/ingest/{_TOKEN}",
        "pathParameters": {"token": _TOKEN},
        "headers": {"Content-Type": "image/jpeg"},
        "queryStringParameters": None,
        "body": base64.b64encode(_JPEG_BODY).decode(),
        "isBase64Encoded": True,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestActiveSessionTimelapsSaved:
    """Active session + timelapse saved → 201 with live_captured: true.

    Validates: Requirements 5.1, 5.4, 5.10
    """

    def test_returns_201_with_live_captured_true(self):
        """When session is active and cadence allows timelapse, response is 201
        with live_captured=true indicating both writes occurred."""
        event = _make_event()
        with (
            patch("sitespy.data.get_camera_by_token", return_value=_CAMERA_ITEM),
            patch("sitespy.data.get_site_ingest_hours", return_value=None),
            patch("sitespy.data.get_latest_img_record", return_value=_old_img_record()),
            patch("sitespy.data.get_live_session", return_value=_active_session_record()),
            patch("sitespy.data.get_retention_years", return_value=5),
            patch("sitespy.storage.put_snapshot"),
            patch("sitespy.data.put_img_record"),
            patch("sitespy.storage.put_live_snapshot") as mock_put_live,
            patch("sitespy.data.put_live_img_record") as mock_put_live_record,
        ):
            result = _handle(event, "test-correlation-id")

        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        assert body["live_captured"] is True
        assert "key" in body
        assert "timestamp" in body
        assert "sha256" in body
        assert "size_bytes" in body
        assert body["camera_id"] == _CAMERA_ID
        mock_put_live.assert_called_once()
        mock_put_live_record.assert_called_once()


class TestActiveSessionCadenceSuppressed:
    """Active session + cadence suppressed timelapse → 200 skipped with live_captured: true.

    Validates: Requirements 5.5, 5.11
    """

    def test_returns_200_skipped_with_live_captured_true(self):
        """When session is active but cadence suppresses timelapse, response is 200
        with status=skipped, reason=cadence_filter, live_captured=true."""
        event = _make_event()
        with (
            patch("sitespy.data.get_camera_by_token", return_value=_CAMERA_ITEM),
            patch("sitespy.data.get_site_ingest_hours", return_value=None),
            patch("sitespy.data.get_latest_img_record", return_value=_recent_img_record()),
            patch("sitespy.data.get_live_session", return_value=_active_session_record()),
            patch("sitespy.storage.put_live_snapshot") as mock_put_live,
            patch("sitespy.data.put_live_img_record") as mock_put_live_record,
        ):
            result = _handle(event, "test-correlation-id")

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "skipped"
        assert body["reason"] == "cadence_filter"
        assert body["live_captured"] is True
        assert body["camera_id"] == _CAMERA_ID
        mock_put_live.assert_called_once()
        mock_put_live_record.assert_called_once()


class TestNoSessionTimelapsSaved:
    """No active session + timelapse saved → 201 with live_captured: false.

    Validates: Requirements 5.1, 5.3, 5.10
    """

    def test_returns_201_with_live_captured_false(self):
        """When no session exists and cadence allows timelapse, response is 201
        with live_captured=false."""
        event = _make_event()
        with (
            patch("sitespy.data.get_camera_by_token", return_value=_CAMERA_ITEM),
            patch("sitespy.data.get_site_ingest_hours", return_value=None),
            patch("sitespy.data.get_latest_img_record", return_value=_old_img_record()),
            patch("sitespy.data.get_live_session", return_value=None),
            patch("sitespy.data.get_retention_years", return_value=5),
            patch("sitespy.storage.put_snapshot"),
            patch("sitespy.data.put_img_record"),
            patch("sitespy.storage.put_live_snapshot") as mock_put_live,
            patch("sitespy.data.put_live_img_record") as mock_put_live_record,
        ):
            result = _handle(event, "test-correlation-id")

        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        assert body["live_captured"] is False
        assert "key" in body
        assert "timestamp" in body
        assert "sha256" in body
        assert "size_bytes" in body
        assert body["camera_id"] == _CAMERA_ID
        mock_put_live.assert_not_called()
        mock_put_live_record.assert_not_called()


class TestNoSessionCadenceSuppressed:
    """No active session + cadence suppressed → 200 skipped with live_captured: false.

    Validates: Requirements 5.3, 5.9
    """

    def test_returns_200_skipped_with_live_captured_false(self):
        """When no session exists and cadence suppresses timelapse, response is 200
        with status=skipped, reason=cadence_filter, live_captured=false."""
        event = _make_event()
        with (
            patch("sitespy.data.get_camera_by_token", return_value=_CAMERA_ITEM),
            patch("sitespy.data.get_site_ingest_hours", return_value=None),
            patch("sitespy.data.get_latest_img_record", return_value=_recent_img_record()),
            patch("sitespy.data.get_live_session", return_value=None),
        ):
            result = _handle(event, "test-correlation-id")

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "skipped"
        assert body["reason"] == "cadence_filter"
        assert body["live_captured"] is False
        assert body["camera_id"] == _CAMERA_ID


class TestLiveS3WriteFailure:
    """Live S3 write failure → 500.

    Validates: Requirement 5.9
    """

    def test_s3_put_live_snapshot_failure_returns_500(self):
        """When storage.put_live_snapshot raises an exception, the handler
        returns HTTP 500 with an internal error."""
        event = _make_event()
        with (
            patch("sitespy.data.get_camera_by_token", return_value=_CAMERA_ITEM),
            patch("sitespy.data.get_site_ingest_hours", return_value=None),
            patch("sitespy.data.get_latest_img_record", return_value=_recent_img_record()),
            patch("sitespy.data.get_live_session", return_value=_active_session_record()),
            patch(
                "sitespy.storage.put_live_snapshot",
                side_effect=Exception("S3 write failed"),
            ),
        ):
            result = handler(event, MagicMock())

        assert result["statusCode"] == 500
        body = json.loads(result["body"])
        assert body["error"] == "INTERNAL_ERROR"
