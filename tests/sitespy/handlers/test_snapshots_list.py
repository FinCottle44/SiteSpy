"""Unit tests for GET /v1/snapshots handler (paginated list).

Tests cover:
- Happy path: returns images list with correct shape
- Pagination: next_cursor present when more pages exist
- Pagination: next_cursor null when no more pages
- Missing site_id: 400
- Missing camera_id: 400
- Invalid limit (> 200): 400
- Access denied: 403
- Cursor decoding: valid cursor passed as ExclusiveStartKey
- Default date range applied when from/to omitted
- total_available count
"""

from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime, timedelta
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

from sitespy.handlers.snapshots import _normalize_timestamp, handler_list  # noqa: E402

# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

_SITE_ITEM = {
    "PK": {"S": "TENANT#acme_corp"},
    "SK": {"S": "SITE#site_001"},
    "site_name": {"S": "Acme Tower — Phase 2"},
    "timezone": {"S": "Europe/London"},
}

_PRESIGNED_URL = "https://s3.amazonaws.com/test-bucket/signed-url"

_TIMESTAMP_1 = "2025-06-15T14:00:00Z"
_TIMESTAMP_2 = "2025-06-15T13:00:00Z"
_S3_KEY_1 = "acme_corp/site_001/cam_01/2025/06/15/2025-06-15T14:00:00Z.jpg"
_S3_KEY_2 = "acme_corp/site_001/cam_01/2025/06/15/2025-06-15T13:00:00Z.jpg"


def _make_img_record(timestamp: str, s3_key: str) -> dict[str, Any]:
    return {
        "PK": {"S": "TENANT#acme_corp"},
        "SK": {"S": f"IMG#site_001#cam_01#{timestamp}"},
        "s3_key": {"S": s3_key},
        "ingested_at": {"S": timestamp},
        "sha256": {"S": "abc123"},
        "size_bytes": {"N": "204800"},
        "content_type": {"S": "image/jpeg"},
    }


_IMG_RECORDS = [
    _make_img_record(_TIMESTAMP_1, _S3_KEY_1),
    _make_img_record(_TIMESTAMP_2, _S3_KEY_2),
]

_LAST_EVALUATED_KEY = {
    "PK": {"S": "TENANT#acme_corp"},
    "SK": {"S": "IMG#site_001#cam_01#2025-06-15T13:00:00Z"},
}


# ---------------------------------------------------------------------------
# Event builder helpers
# ---------------------------------------------------------------------------


def _make_event(
    site_id: str | None = "site_001",
    camera_id: str | None = "cam_01",
    groups: str = "TenantAdmins",
    tenant_id: str = "acme_corp",
    site_access: str = "",
    from_ts: str | None = None,
    to_ts: str | None = None,
    limit: str | None = None,
    cursor: str | None = None,
    extra_query_params: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build a minimal API Gateway proxy event for the GET /v1/snapshots handler."""
    query_params: dict[str, str] = {}
    if site_id is not None:
        query_params["site_id"] = site_id
    if camera_id is not None:
        query_params["camera_id"] = camera_id
    if from_ts is not None:
        query_params["from"] = from_ts
    if to_ts is not None:
        query_params["to"] = to_ts
    if limit is not None:
        query_params["limit"] = limit
    if cursor is not None:
        query_params["cursor"] = cursor
    if extra_query_params:
        query_params.update(extra_query_params)

    return {
        "httpMethod": "GET",
        "path": "/v1/snapshots",
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


def _encode_cursor(key: dict) -> str:
    return base64.b64encode(json.dumps(key).encode()).decode()


# ---------------------------------------------------------------------------
# Tests: happy path
# ---------------------------------------------------------------------------


class TestSnapshotsListHappyPath:
    def test_returns_200_with_correct_shape(self):
        event = _make_event()
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch(
                "sitespy.data.list_img_records",
                return_value=(_IMG_RECORDS, None),
            ),
            patch("sitespy.storage.generate_presigned_url", return_value=_PRESIGNED_URL),
        ):
            result = handler_list(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert "images" in body
        assert "next_cursor" in body
        assert "total_available" in body

    def test_images_have_correct_fields(self):
        event = _make_event()
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch(
                "sitespy.data.list_img_records",
                return_value=(_IMG_RECORDS, None),
            ),
            patch("sitespy.storage.generate_presigned_url", return_value=_PRESIGNED_URL),
        ):
            result = handler_list(event, MagicMock())

        body = json.loads(result["body"])
        assert len(body["images"]) == 2
        for img in body["images"]:
            assert "timestamp" in img
            assert "camera_id" in img
            assert "key" in img
            assert "presigned_url" in img
            assert "expires_in" in img
            assert img["camera_id"] == "cam_01"
            assert img["expires_in"] == 300
            assert img["presigned_url"] == _PRESIGNED_URL

    def test_images_contain_correct_timestamps(self):
        event = _make_event()
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch(
                "sitespy.data.list_img_records",
                return_value=(_IMG_RECORDS, None),
            ),
            patch("sitespy.storage.generate_presigned_url", return_value=_PRESIGNED_URL),
        ):
            result = handler_list(event, MagicMock())

        body = json.loads(result["body"])
        timestamps = [img["timestamp"] for img in body["images"]]
        assert _TIMESTAMP_1 in timestamps
        assert _TIMESTAMP_2 in timestamps

    def test_correlation_id_echoed(self):
        event = _make_event()
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch(
                "sitespy.data.list_img_records",
                return_value=(_IMG_RECORDS, None),
            ),
            patch("sitespy.storage.generate_presigned_url", return_value=_PRESIGNED_URL),
        ):
            result = handler_list(event, MagicMock())

        assert result["headers"]["X-Correlation-Id"] == "test-corr-id"

    def test_empty_result_returns_empty_images(self):
        event = _make_event()
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch("sitespy.data.list_img_records", return_value=([], None)),
        ):
            result = handler_list(event, MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["images"] == []
        assert body["next_cursor"] is None
        assert body["total_available"] == 0


# ---------------------------------------------------------------------------
# Tests: pagination
# ---------------------------------------------------------------------------


class TestSnapshotsListPagination:
    def test_next_cursor_present_when_more_pages_exist(self):
        event = _make_event()
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch(
                "sitespy.data.list_img_records",
                return_value=(_IMG_RECORDS, _LAST_EVALUATED_KEY),
            ),
            patch("sitespy.storage.generate_presigned_url", return_value=_PRESIGNED_URL),
        ):
            result = handler_list(event, MagicMock())

        body = json.loads(result["body"])
        assert body["next_cursor"] is not None
        assert isinstance(body["next_cursor"], str)
        # Verify it's valid base64 JSON
        decoded = json.loads(base64.b64decode(body["next_cursor"].encode()).decode())
        assert decoded == _LAST_EVALUATED_KEY

    def test_next_cursor_null_when_no_more_pages(self):
        event = _make_event()
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch(
                "sitespy.data.list_img_records",
                return_value=(_IMG_RECORDS, None),
            ),
            patch("sitespy.storage.generate_presigned_url", return_value=_PRESIGNED_URL),
        ):
            result = handler_list(event, MagicMock())

        body = json.loads(result["body"])
        assert body["next_cursor"] is None

    def test_cursor_decoded_and_passed_as_exclusive_start_key(self):
        cursor = _encode_cursor(_LAST_EVALUATED_KEY)
        event = _make_event(cursor=cursor)

        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch(
                "sitespy.data.list_img_records",
                return_value=([], None),
            ) as mock_list,
            patch("sitespy.storage.generate_presigned_url", return_value=_PRESIGNED_URL),
        ):
            result = handler_list(event, MagicMock())

        assert result["statusCode"] == 200
        # Verify exclusive_start_key was passed correctly
        call_kwargs = mock_list.call_args
        assert call_kwargs.kwargs["exclusive_start_key"] == _LAST_EVALUATED_KEY

    def test_total_available_includes_next_page_indicator(self):
        """total_available = len(items) + 1 when next_cursor is present."""
        event = _make_event()
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch(
                "sitespy.data.list_img_records",
                return_value=(_IMG_RECORDS, _LAST_EVALUATED_KEY),
            ),
            patch("sitespy.storage.generate_presigned_url", return_value=_PRESIGNED_URL),
        ):
            result = handler_list(event, MagicMock())

        body = json.loads(result["body"])
        # 2 items + 1 because there's a next page
        assert body["total_available"] == len(_IMG_RECORDS) + 1

    def test_total_available_equals_item_count_when_no_next_page(self):
        event = _make_event()
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch(
                "sitespy.data.list_img_records",
                return_value=(_IMG_RECORDS, None),
            ),
            patch("sitespy.storage.generate_presigned_url", return_value=_PRESIGNED_URL),
        ):
            result = handler_list(event, MagicMock())

        body = json.loads(result["body"])
        assert body["total_available"] == len(_IMG_RECORDS)


# ---------------------------------------------------------------------------
# Tests: validation errors
# ---------------------------------------------------------------------------


class TestSnapshotsListValidation:
    def test_missing_site_id_returns_400(self):
        event = _make_event(site_id=None)
        result = handler_list(event, MagicMock())
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"
        assert "site_id" in body["message"]

    def test_empty_site_id_returns_400(self):
        event = _make_event(site_id="")
        result = handler_list(event, MagicMock())
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"

    def test_missing_camera_id_returns_400(self):
        event = _make_event(camera_id=None)
        result = handler_list(event, MagicMock())
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"
        assert "camera_id" in body["message"]

    def test_empty_camera_id_returns_400(self):
        event = _make_event(camera_id="")
        result = handler_list(event, MagicMock())
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"

    def test_limit_above_200_returns_400(self):
        event = _make_event(limit="201")
        result = handler_list(event, MagicMock())
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"

    def test_limit_zero_returns_400(self):
        event = _make_event(limit="0")
        result = handler_list(event, MagicMock())
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"

    def test_limit_non_integer_returns_400(self):
        event = _make_event(limit="abc")
        result = handler_list(event, MagicMock())
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"

    def test_limit_200_is_valid(self):
        event = _make_event(limit="200")
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch("sitespy.data.list_img_records", return_value=([], None)),
        ):
            result = handler_list(event, MagicMock())
        assert result["statusCode"] == 200

    def test_invalid_cursor_returns_400(self):
        event = _make_event(cursor="not-valid-base64!!!")
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
        ):
            result = handler_list(event, MagicMock())
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"

    def test_site_not_found_returns_404(self):
        event = _make_event()
        with patch("sitespy.data.get_site", return_value=None):
            result = handler_list(event, MagicMock())
        assert result["statusCode"] == 404
        body = json.loads(result["body"])
        assert body["error"] == "NOT_FOUND"


# ---------------------------------------------------------------------------
# Tests: access control
# ---------------------------------------------------------------------------


class TestSnapshotsListAccessControl:
    def test_tenant_admin_allowed(self):
        event = _make_event(groups="TenantAdmins", tenant_id="acme_corp")
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch("sitespy.data.list_img_records", return_value=([], None)),
        ):
            result = handler_list(event, MagicMock())
        assert result["statusCode"] == 200

    def test_user_with_site_access_allowed(self):
        event = _make_event(
            groups="",
            tenant_id="acme_corp",
            site_access="site_001,site_002",
        )
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch("sitespy.data.list_img_records", return_value=([], None)),
        ):
            result = handler_list(event, MagicMock())
        assert result["statusCode"] == 200

    def test_user_without_site_access_returns_403(self):
        event = _make_event(
            groups="",
            tenant_id="acme_corp",
            site_access="site_002,site_003",  # site_001 not included
        )
        with patch("sitespy.data.get_site", return_value=_SITE_ITEM):
            result = handler_list(event, MagicMock())
        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "ACCESS_DENIED"

    def test_tenant_admin_no_tenant_id_in_token_returns_403(self):
        event = _make_event(groups="TenantAdmins", tenant_id="")
        result = handler_list(event, MagicMock())
        assert result["statusCode"] == 403

    def test_super_admin_missing_tenant_id_param_returns_400(self):
        event = _make_event(groups="SuperAdmins", tenant_id="")
        result = handler_list(event, MagicMock())
        assert result["statusCode"] == 400

    def test_super_admin_with_tenant_id_param_allowed(self):
        event = _make_event(
            groups="SuperAdmins",
            tenant_id="",
            extra_query_params={"tenant_id": "acme_corp"},
        )
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch("sitespy.data.list_img_records", return_value=([], None)),
        ):
            result = handler_list(event, MagicMock())
        assert result["statusCode"] == 200


# ---------------------------------------------------------------------------
# Tests: default date range
# ---------------------------------------------------------------------------


class TestSnapshotsListDefaultDateRange:
    def test_default_from_is_30_days_ago(self):
        """When from is omitted, list_img_records is called with ~30 days ago."""
        event = _make_event()  # no from/to

        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch(
                "sitespy.data.list_img_records",
                return_value=([], None),
            ) as mock_list,
        ):
            result = handler_list(event, MagicMock())

        assert result["statusCode"] == 200
        call_kwargs = mock_list.call_args.kwargs
        from_ts = call_kwargs["from_ts"]

        # Parse the from_ts and verify it's approximately 30 days ago
        from_dt = datetime.fromisoformat(from_ts.replace("Z", "+00:00"))
        expected = datetime.now(tz=UTC) - timedelta(days=30)
        diff = abs((from_dt - expected).total_seconds())
        assert diff < 60, f"from_ts {from_ts!r} is not ~30 days ago (diff={diff}s)"

    def test_default_to_is_now(self):
        """When to is omitted, list_img_records is called with ~now."""
        event = _make_event()  # no from/to

        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch(
                "sitespy.data.list_img_records",
                return_value=([], None),
            ) as mock_list,
        ):
            result = handler_list(event, MagicMock())

        assert result["statusCode"] == 200
        call_kwargs = mock_list.call_args.kwargs
        to_ts = call_kwargs["to_ts"]

        to_dt = datetime.fromisoformat(to_ts.replace("Z", "+00:00"))
        now = datetime.now(tz=UTC)
        diff = abs((to_dt - now).total_seconds())
        assert diff < 60, f"to_ts {to_ts!r} is not ~now (diff={diff}s)"

    def test_date_only_from_normalized_to_start_of_day(self):
        event = _make_event(from_ts="2025-06-15")
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch(
                "sitespy.data.list_img_records",
                return_value=([], None),
            ) as mock_list,
        ):
            result = handler_list(event, MagicMock())

        assert result["statusCode"] == 200
        call_kwargs = mock_list.call_args.kwargs
        assert call_kwargs["from_ts"] == "2025-06-15T00:00:00Z"

    def test_date_only_to_normalized_to_end_of_day(self):
        event = _make_event(to_ts="2025-06-15")
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch(
                "sitespy.data.list_img_records",
                return_value=([], None),
            ) as mock_list,
        ):
            result = handler_list(event, MagicMock())

        assert result["statusCode"] == 200
        call_kwargs = mock_list.call_args.kwargs
        assert call_kwargs["to_ts"] == "2025-06-15T23:59:59Z"

    def test_full_datetime_from_passed_through(self):
        event = _make_event(from_ts="2025-06-15T08:30:00Z")
        with (
            patch("sitespy.data.get_site", return_value=_SITE_ITEM),
            patch(
                "sitespy.data.list_img_records",
                return_value=([], None),
            ) as mock_list,
        ):
            result = handler_list(event, MagicMock())

        assert result["statusCode"] == 200
        call_kwargs = mock_list.call_args.kwargs
        assert call_kwargs["from_ts"] == "2025-06-15T08:30:00Z"


# ---------------------------------------------------------------------------
# Tests: _normalize_timestamp pure function
# ---------------------------------------------------------------------------


class TestNormalizeTimestamp:
    def test_none_uses_default(self):
        default = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)
        result = _normalize_timestamp(None, default, is_start=True)
        assert result == "2025-06-01T12:00:00Z"

    def test_date_only_start_of_day(self):
        default = datetime(2025, 1, 1, tzinfo=UTC)
        result = _normalize_timestamp("2025-06-15", default, is_start=True)
        assert result == "2025-06-15T00:00:00Z"

    def test_date_only_end_of_day(self):
        default = datetime(2025, 1, 1, tzinfo=UTC)
        result = _normalize_timestamp("2025-06-15", default, is_start=False)
        assert result == "2025-06-15T23:59:59Z"

    def test_full_datetime_with_z_suffix(self):
        default = datetime(2025, 1, 1, tzinfo=UTC)
        result = _normalize_timestamp("2025-06-15T14:30:00Z", default, is_start=True)
        assert result == "2025-06-15T14:30:00Z"

    def test_full_datetime_with_offset(self):
        default = datetime(2025, 1, 1, tzinfo=UTC)
        result = _normalize_timestamp("2025-06-15T15:30:00+01:00", default, is_start=True)
        assert result == "2025-06-15T14:30:00Z"

    def test_invalid_datetime_raises_bad_request(self):
        from sitespy.errors import BadRequest

        default = datetime(2025, 1, 1, tzinfo=UTC)
        with pytest.raises(BadRequest):
            _normalize_timestamp("not-a-date", default, is_start=True)
