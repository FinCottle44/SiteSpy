"""Unit tests for GET /v1/snapshots/latest handler.

Tests cover:
- Single camera: correct response shape including age_seconds
- All cameras: list with camera_name
- Camera with no snapshots: null/empty entry in all-cameras response
- Single camera with no snapshot: 404
- Missing site_id: 400
- Access denied: 403
- Super admin requires tenant_id query param: 400
- Tenant admin no tenant_id in token: 403
- User site not in site_access: 403
- Site not found: 404
- Correlation ID echoed in response header
- compute_age_seconds: basic correctness
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
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

from sitespy.handlers.snapshots import compute_age_seconds, handler_latest  # noqa: E402

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

_SITE_ITEM = {
    "PK": {"S": "TENANT#acme_corp"},
    "SK": {"S": "SITE#site_001"},
    "site_name": {"S": "Acme Tower — Phase 2"},
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

_TIMESTAMP = "2025-06-15T14:00:00Z"
_S3_KEY = "acme_corp/site_001/cam_01/2025/06/15/2025-06-15T14:00:00Z.jpg"
_PRESIGNED_URL = "https://s3.amazonaws.com/test-bucket/signed-url"

_IMG_RECORD = {
    "PK": {"S": "TENANT#acme_corp"},
    "SK": {"S": "IMG#site_001#cam_01#2025-06-15T14:00:00Z"},
    "s3_key": {"S": _S3_KEY},
    "ingested_at": {"S": _TIMESTAMP},
    "sha256": {"S": "abc123"},
    "size_bytes": {"N": "204800"},
    "content_type": {"S": "image/jpeg"},
}


# ---------------------------------------------------------------------------
# Event builder helpers
# ---------------------------------------------------------------------------


def _make_event(
    site_id: str | None = "site_001",
    camera_id: str | None = None,
    groups: str = "TenantAdmins",
    tenant_id: str = "acme_corp",
    site_access: str = "",
    extra_query_params: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a minimal API Gateway proxy event for the snapshots/latest handler."""
    query_params: dict[str, str] = {}
    if site_id is not None:
        query_params["site_id"] = site_id
    if camera_id is not None:
        query_params["camera_id"] = camera_id
    if extra_query_params:
        query_params.update(extra_query_params)

    return {
        "httpMethod": "GET",
        "path": "/v1/snapshots/latest",
        "pathParameters": None,
        "queryStringParameters": query_params,
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
# Tests: single camera mode
# ---------------------------------------------------------------------------


class TestSnapshotsLatestSingleCamera:
    def test_returns_200_with_correct_shape(self):
        event = _make_event(camera_id="cam_01")
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch("sitespy.data.get_latest_img_record", return_value=_IMG_RECORD),
            patch("sitespy.storage.generate_presigned_url", return_value=_PRESIGNED_URL),
        ):
            result = handler_latest(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["camera_id"] == "cam_01"
        assert body["timestamp"] == _TIMESTAMP
        assert body["key"] == _S3_KEY
        assert body["presigned_url"] == _PRESIGNED_URL
        assert body["expires_in"] == 300
        assert isinstance(body["age_seconds"], int)
        assert body["age_seconds"] >= 0

    def test_age_seconds_is_non_negative(self):
        event = _make_event(camera_id="cam_01")
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch("sitespy.data.get_latest_img_record", return_value=_IMG_RECORD),
            patch("sitespy.storage.generate_presigned_url", return_value=_PRESIGNED_URL),
        ):
            result = handler_latest(event, MagicMock())

        body = json.loads(result["body"])
        assert body["age_seconds"] >= 0

    def test_age_seconds_reflects_timestamp_age(self):
        """age_seconds should be close to the actual elapsed time."""
        # Use a timestamp 1 hour ago
        one_hour_ago = datetime.now(tz=UTC) - timedelta(hours=1)
        ts_str = one_hour_ago.strftime("%Y-%m-%dT%H:%M:%SZ")
        img_record = {**_IMG_RECORD, "ingested_at": {"S": ts_str}}

        event = _make_event(camera_id="cam_01")
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch("sitespy.data.get_latest_img_record", return_value=img_record),
            patch("sitespy.storage.generate_presigned_url", return_value=_PRESIGNED_URL),
        ):
            result = handler_latest(event, MagicMock())

        body = json.loads(result["body"])
        # Should be approximately 3600 seconds (±10s tolerance for test execution)
        assert 3590 <= body["age_seconds"] <= 3610

    def test_no_snapshot_returns_404(self):
        event = _make_event(camera_id="cam_01")
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch("sitespy.data.get_latest_img_record", return_value=None),
        ):
            result = handler_latest(event, MagicMock())

        assert result["statusCode"] == 404
        body = json.loads(result["body"])
        assert body["error"] == "NOT_FOUND"

    def test_correlation_id_echoed(self):
        event = _make_event(camera_id="cam_01")
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch("sitespy.data.get_latest_img_record", return_value=_IMG_RECORD),
            patch("sitespy.storage.generate_presigned_url", return_value=_PRESIGNED_URL),
        ):
            result = handler_latest(event, MagicMock())

        assert result["headers"]["X-Correlation-Id"] == "test-corr-id"


# ---------------------------------------------------------------------------
# Tests: all cameras mode
# ---------------------------------------------------------------------------


class TestSnapshotsLatestAllCameras:
    def test_returns_cameras_list(self):
        event = _make_event()  # no camera_id
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch("sitespy.data.get_cameras_for_site", return_value=_CAMERA_ITEMS),
            patch("sitespy.data.get_latest_img_record", return_value=_IMG_RECORD),
            patch("sitespy.storage.generate_presigned_url", return_value=_PRESIGNED_URL),
        ):
            result = handler_latest(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert "cameras" in body
        assert len(body["cameras"]) == 2

    def test_each_entry_has_camera_name(self):
        event = _make_event()
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch("sitespy.data.get_cameras_for_site", return_value=_CAMERA_ITEMS),
            patch("sitespy.data.get_latest_img_record", return_value=_IMG_RECORD),
            patch("sitespy.storage.generate_presigned_url", return_value=_PRESIGNED_URL),
        ):
            result = handler_latest(event, MagicMock())

        body = json.loads(result["body"])
        names = {c["camera_name"] for c in body["cameras"]}
        assert "North elevation" in names
        assert "Crane cab" in names

    def test_each_entry_has_required_fields(self):
        event = _make_event()
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch("sitespy.data.get_cameras_for_site", return_value=_CAMERA_ITEMS),
            patch("sitespy.data.get_latest_img_record", return_value=_IMG_RECORD),
            patch("sitespy.storage.generate_presigned_url", return_value=_PRESIGNED_URL),
        ):
            result = handler_latest(event, MagicMock())

        body = json.loads(result["body"])
        for cam in body["cameras"]:
            assert "camera_id" in cam
            assert "camera_name" in cam
            assert "timestamp" in cam
            assert "presigned_url" in cam
            assert "expires_in" in cam
            assert "age_seconds" in cam

    def test_camera_with_no_snapshot_returns_null_fields(self):
        """A camera that has never received a snapshot should appear with null values."""
        event = _make_event()
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch("sitespy.data.get_cameras_for_site", return_value=_CAMERA_ITEMS),
            patch("sitespy.data.get_latest_img_record", return_value=None),
            patch("sitespy.storage.generate_presigned_url", return_value=_PRESIGNED_URL),
        ):
            result = handler_latest(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        for cam in body["cameras"]:
            assert cam["timestamp"] is None
            assert cam["presigned_url"] is None
            assert cam["age_seconds"] is None

    def test_empty_camera_list_returns_empty_cameras(self):
        event = _make_event()
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch("sitespy.data.get_cameras_for_site", return_value=[]),
        ):
            result = handler_latest(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["cameras"] == []


# ---------------------------------------------------------------------------
# Tests: validation errors
# ---------------------------------------------------------------------------


class TestSnapshotsLatestValidation:
    def test_missing_site_id_returns_400(self):
        event = _make_event(site_id=None)
        result = handler_latest(event, MagicMock())
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"

    def test_empty_site_id_returns_400(self):
        event = _make_event(site_id="")
        result = handler_latest(event, MagicMock())
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"

    def test_site_not_found_returns_404(self):
        event = _make_event()
        with patch("sitespy.data.get_site", return_value=None):
            result = handler_latest(event, MagicMock())
        assert result["statusCode"] == 404
        body = json.loads(result["body"])
        assert body["error"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Tests: access control
# ---------------------------------------------------------------------------


class TestSnapshotsLatestAccessControl:
    def test_user_with_site_access_allowed(self):
        event = _make_event(
            groups="",
            tenant_id="acme_corp",
            site_access="site_001,site_002",
        )
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch("sitespy.data.get_cameras_for_site", return_value=[]),
        ):
            result = handler_latest(event, MagicMock())
        assert result["statusCode"] == 200

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
            result = handler_latest(event, MagicMock())
        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "ACCESS_DENIED"

    def test_tenant_admin_no_tenant_id_in_token_returns_403(self):
        event = _make_event(groups="TenantAdmins", tenant_id="")
        result = handler_latest(event, MagicMock())
        assert result["statusCode"] == 403

    def test_super_admin_missing_tenant_id_param_returns_400(self):
        event = _make_event(
            groups="SuperAdmins",
            tenant_id="",
        )
        # No tenant_id in query params
        result = handler_latest(event, MagicMock())
        assert result["statusCode"] == 400

    def test_super_admin_with_tenant_id_param_allowed(self):
        event = _make_event(
            groups="SuperAdmins",
            tenant_id="",
            extra_query_params={"tenant_id": "acme_corp"},
        )
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch("sitespy.data.get_cameras_for_site", return_value=[]),
        ):
            result = handler_latest(event, MagicMock())
        assert result["statusCode"] == 200

    def test_tenant_admin_wrong_tenant_site_not_found(self):
        """Tenant admin from other_corp gets 404 — site doesn't exist under their tenant."""
        event = _make_event(groups="TenantAdmins", tenant_id="other_corp")
        with patch("sitespy.data.get_site", return_value=None):
            result = handler_latest(event, MagicMock())
        assert result["statusCode"] == 404


# ---------------------------------------------------------------------------
# Tests: compute_age_seconds pure function
# ---------------------------------------------------------------------------


class TestComputeAgeSeconds:
    def test_past_timestamp_returns_positive(self):
        one_hour_ago = datetime.now(tz=UTC) - timedelta(hours=1)
        ts = one_hour_ago.strftime("%Y-%m-%dT%H:%M:%SZ")
        age = compute_age_seconds(ts)
        assert 3590 <= age <= 3610

    def test_very_old_timestamp_returns_large_value(self):
        old_ts = "2020-01-01T00:00:00Z"
        age = compute_age_seconds(old_ts)
        assert age > 0

    def test_future_timestamp_returns_zero(self):
        """A future timestamp should not produce a negative age."""
        future = datetime.now(tz=UTC) + timedelta(hours=1)
        ts = future.strftime("%Y-%m-%dT%H:%M:%SZ")
        age = compute_age_seconds(ts)
        assert age == 0

    def test_recent_timestamp_returns_small_value(self):
        just_now = datetime.now(tz=UTC) - timedelta(seconds=5)
        ts = just_now.strftime("%Y-%m-%dT%H:%M:%SZ")
        age = compute_age_seconds(ts)
        assert 0 <= age <= 15  # generous tolerance for test execution time
