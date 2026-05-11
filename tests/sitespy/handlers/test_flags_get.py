"""Unit tests for GET /v1/flags handler.

Tests cover:
- Happy path: returns flags list with correct shape
- latest_snapshot included for each flag
- latest_snapshot is null when no snapshot exists
- Default status filter (open + acknowledged)
- Custom status filter
- Tenant admin sees only own tenant flags
- User sees only own site flags
- Super admin sees all flags
- Super admin with tenant_id filter
- Pagination: next_cursor present/absent
- Cursor decoded correctly
- Missing/invalid limit: 400
- Access control: 403 for user without site access

Requirements validated: 8.1, 8.2, 8.3, 8.4, 8.5, 8.8, 8.9
"""

from __future__ import annotations

import base64
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

from sitespy.handlers.flags import handler_get  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

_OPEN_FLAG = {
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

_ACKNOWLEDGED_FLAG = {
    **_OPEN_FLAG,
    "flag_id": {"S": "flag-id-ack-001"},
    "GSI1PK": {"S": "FLAGSTATUS#acknowledged"},
    "status": {"S": "acknowledged"},
    "raised_at": {"S": "2025-06-14T10:00:00Z"},
    "GSI1SK": {"S": "2025-06-14T10:00:00Z"},
    "SK": {"S": "FLAG#site_001#cam_01#2025-06-14T10:00:00Z"},
}

_IMG_RECORD = {
    "PK": {"S": "TENANT#acme_corp"},
    "SK": {"S": "IMG#site_001#cam_01#2025-06-12T09:00:00Z"},
    "s3_key": {"S": "acme_corp/site_001/cam_01/2025/06/12/2025-06-12T09:00:00Z.jpg"},
    "ingested_at": {"S": "2025-06-12T09:00:00Z"},
    "sha256": {"S": "abc123"},
    "size_bytes": {"N": "1024"},
    "content_type": {"S": "image/jpeg"},
}

_PRESIGNED_URL = "https://s3.amazonaws.com/test-bucket/presigned-url"


def _make_event(
    query_params: dict[str, str] | None = None,
    groups: str = "TenantAdmins",
    tenant_id: str = "acme_corp",
    site_access: str = "",
    sub: str = "user-sub-123",
) -> dict[str, Any]:
    """Build a minimal API Gateway proxy event for the flags GET handler."""
    return {
        "httpMethod": "GET",
        "path": "/v1/flags",
        "pathParameters": None,
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


def _make_super_admin_event(
    query_params: dict[str, str] | None = None,
) -> dict[str, Any]:
    return _make_event(
        query_params=query_params,
        groups="SuperAdmins",
        tenant_id="",
        site_access="",
    )


# ---------------------------------------------------------------------------
# Happy path tests
# ---------------------------------------------------------------------------


class TestFlagsGetHappyPath:
    def test_returns_200_with_flags_list(self):
        with (
            patch("sitespy.data.list_flags", return_value=([_OPEN_FLAG], None)),
            patch("sitespy.data.get_latest_img_record", return_value=_IMG_RECORD),
            patch(
                "sitespy.handlers.flags.generate_presigned_url",
                return_value=_PRESIGNED_URL,
            ),
        ):
            result = handler_get(_make_event(), MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert "flags" in body
        assert len(body["flags"]) == 1

    def test_flag_has_correct_shape(self):
        with (
            patch("sitespy.data.list_flags", return_value=([_OPEN_FLAG], None)),
            patch("sitespy.data.get_latest_img_record", return_value=_IMG_RECORD),
            patch(
                "sitespy.handlers.flags.generate_presigned_url",
                return_value=_PRESIGNED_URL,
            ),
        ):
            result = handler_get(_make_event(), MagicMock())

        body = json.loads(result["body"])
        flag = body["flags"][0]
        assert flag["flag_id"] == "flag-id-open-001"
        assert flag["tenant_id"] == "acme_corp"
        assert flag["site_id"] == "site_001"
        assert flag["camera_id"] == "cam_01"
        assert flag["reason"] == "stale_image"
        assert flag["status"] == "open"
        assert flag["source"] == "auto"
        assert flag["raised_by"] == "system"
        assert flag["raised_at"] == "2025-06-15T14:03:00Z"

    def test_correlation_id_echoed_in_header(self):
        with (
            patch("sitespy.data.list_flags", return_value=([], None)),
            patch("sitespy.data.get_latest_img_record", return_value=None),
        ):
            result = handler_get(_make_event(), MagicMock())

        assert result["headers"]["X-Correlation-Id"] == "test-corr-id"

    def test_empty_flags_list_returns_200(self):
        with (
            patch("sitespy.data.list_flags", return_value=([], None)),
        ):
            result = handler_get(_make_event(), MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["flags"] == []
        assert body["next_cursor"] is None
        assert body["total_available"] == 0


# ---------------------------------------------------------------------------
# latest_snapshot tests
# ---------------------------------------------------------------------------


class TestFlagsGetLatestSnapshot:
    def test_latest_snapshot_included_when_img_record_exists(self):
        with (
            patch("sitespy.data.list_flags", return_value=([_OPEN_FLAG], None)),
            patch("sitespy.data.get_latest_img_record", return_value=_IMG_RECORD),
            patch(
                "sitespy.handlers.flags.generate_presigned_url",
                return_value=_PRESIGNED_URL,
            ),
        ):
            result = handler_get(_make_event(), MagicMock())

        body = json.loads(result["body"])
        snapshot = body["flags"][0]["latest_snapshot"]
        assert snapshot is not None
        assert snapshot["timestamp"] == "2025-06-12T09:00:00Z"
        assert snapshot["presigned_url"] == _PRESIGNED_URL
        assert snapshot["expires_in"] == 300

    def test_latest_snapshot_is_null_when_no_img_record(self):
        with (
            patch("sitespy.data.list_flags", return_value=([_OPEN_FLAG], None)),
            patch("sitespy.data.get_latest_img_record", return_value=None),
        ):
            result = handler_get(_make_event(), MagicMock())

        body = json.loads(result["body"])
        assert body["flags"][0]["latest_snapshot"] is None

    def test_latest_snapshot_is_null_when_img_fetch_raises(self):
        with (
            patch("sitespy.data.list_flags", return_value=([_OPEN_FLAG], None)),
            patch(
                "sitespy.data.get_latest_img_record",
                side_effect=Exception("DynamoDB error"),
            ),
        ):
            result = handler_get(_make_event(), MagicMock())

        # Should still return 200 — snapshot failure is non-fatal
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["flags"][0]["latest_snapshot"] is None

    def test_note_is_null_when_absent_from_item(self):
        """Flag items without a 'note' attribute should have note=null in response."""
        flag_no_note = {k: v for k, v in _OPEN_FLAG.items() if k != "note"}
        with (
            patch("sitespy.data.list_flags", return_value=([flag_no_note], None)),
            patch("sitespy.data.get_latest_img_record", return_value=None),
        ):
            result = handler_get(_make_event(), MagicMock())

        body = json.loads(result["body"])
        assert body["flags"][0]["note"] is None


# ---------------------------------------------------------------------------
# Status filter tests
# ---------------------------------------------------------------------------


class TestFlagsGetStatusFilter:
    def test_default_status_filter_queries_open_and_acknowledged(self):
        with (
            patch("sitespy.data.list_flags", return_value=([], None)) as mock_list,
            patch("sitespy.data.get_latest_img_record", return_value=None),
        ):
            handler_get(_make_event(), MagicMock())

        call_kwargs = mock_list.call_args.kwargs
        assert set(call_kwargs["status_list"]) == {"open", "acknowledged"}

    def test_custom_status_filter_single_status(self):
        with (
            patch("sitespy.data.list_flags", return_value=([], None)) as mock_list,
        ):
            handler_get(
                _make_event(query_params={"status": "resolved"}), MagicMock()
            )

        call_kwargs = mock_list.call_args.kwargs
        assert call_kwargs["status_list"] == ["resolved"]

    def test_custom_status_filter_multiple_statuses(self):
        with (
            patch("sitespy.data.list_flags", return_value=([], None)) as mock_list,
        ):
            handler_get(
                _make_event(query_params={"status": "open,resolved"}), MagicMock()
            )

        call_kwargs = mock_list.call_args.kwargs
        assert set(call_kwargs["status_list"]) == {"open", "resolved"}

    def test_invalid_status_returns_400(self):
        result = handler_get(
            _make_event(query_params={"status": "invalid_status"}), MagicMock()
        )
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"


# ---------------------------------------------------------------------------
# Role-based scoping tests
# ---------------------------------------------------------------------------


class TestFlagsGetRoleScoping:
    def test_tenant_admin_scoped_to_own_tenant(self):
        with (
            patch("sitespy.data.list_flags", return_value=([], None)) as mock_list,
        ):
            handler_get(
                _make_event(groups="TenantAdmins", tenant_id="acme_corp"),
                MagicMock(),
            )

        call_kwargs = mock_list.call_args.kwargs
        assert call_kwargs["tenant_id"] == "acme_corp"

    def test_tenant_admin_cannot_override_tenant_id(self):
        """Tenant admin's tenant_id param is ignored — always uses their own tenant."""
        with (
            patch("sitespy.data.list_flags", return_value=([], None)) as mock_list,
        ):
            handler_get(
                _make_event(
                    groups="TenantAdmins",
                    tenant_id="acme_corp",
                    query_params={"tenant_id": "other_corp"},
                ),
                MagicMock(),
            )

        call_kwargs = mock_list.call_args.kwargs
        assert call_kwargs["tenant_id"] == "acme_corp"

    def test_user_scoped_to_own_tenant(self):
        with (
            patch("sitespy.data.list_flags", return_value=([], None)) as mock_list,
        ):
            handler_get(
                _make_event(
                    groups="",
                    tenant_id="acme_corp",
                    site_access="site_001,site_002",
                ),
                MagicMock(),
            )

        call_kwargs = mock_list.call_args.kwargs
        assert call_kwargs["tenant_id"] == "acme_corp"

    def test_user_sees_only_own_site_flags(self):
        """User with site_001 access should not see flags for site_002."""
        flag_site2 = {
            **_OPEN_FLAG,
            "site_id": {"S": "site_002"},
            "flag_id": {"S": "flag-site2"},
        }
        with (
            patch(
                "sitespy.data.list_flags",
                return_value=([_OPEN_FLAG, flag_site2], None),
            ),
            patch("sitespy.data.get_latest_img_record", return_value=None),
        ):
            result = handler_get(
                _make_event(
                    groups="",
                    tenant_id="acme_corp",
                    site_access="site_001",
                ),
                MagicMock(),
            )

        body = json.loads(result["body"])
        assert len(body["flags"]) == 1
        assert body["flags"][0]["site_id"] == "site_001"

    def test_user_with_no_site_access_sees_no_flags(self):
        with (
            patch(
                "sitespy.data.list_flags",
                return_value=([_OPEN_FLAG], None),
            ),
            patch("sitespy.data.get_latest_img_record", return_value=None),
        ):
            result = handler_get(
                _make_event(
                    groups="",
                    tenant_id="acme_corp",
                    site_access="",
                ),
                MagicMock(),
            )

        body = json.loads(result["body"])
        assert body["flags"] == []

    def test_super_admin_sees_all_flags_no_tenant_filter(self):
        with (
            patch("sitespy.data.list_flags", return_value=([], None)) as mock_list,
        ):
            handler_get(_make_super_admin_event(), MagicMock())

        call_kwargs = mock_list.call_args.kwargs
        assert call_kwargs["tenant_id"] is None

    def test_super_admin_with_tenant_id_filter(self):
        with (
            patch("sitespy.data.list_flags", return_value=([], None)) as mock_list,
        ):
            handler_get(
                _make_super_admin_event(query_params={"tenant_id": "acme_corp"}),
                MagicMock(),
            )

        call_kwargs = mock_list.call_args.kwargs
        assert call_kwargs["tenant_id"] == "acme_corp"

    def test_super_admin_returns_flags_from_multiple_tenants(self):
        flag_tenant2 = {
            **_OPEN_FLAG,
            "tenant_id": {"S": "other_corp"},
            "flag_id": {"S": "flag-other-corp"},
        }
        with (
            patch(
                "sitespy.data.list_flags",
                return_value=([_OPEN_FLAG, flag_tenant2], None),
            ),
            patch("sitespy.data.get_latest_img_record", return_value=None),
        ):
            result = handler_get(_make_super_admin_event(), MagicMock())

        body = json.loads(result["body"])
        assert len(body["flags"]) == 2


# ---------------------------------------------------------------------------
# Access control tests
# ---------------------------------------------------------------------------


class TestFlagsGetAccessControl:
    def test_tenant_admin_without_tenant_id_returns_403(self):
        result = handler_get(
            _make_event(groups="TenantAdmins", tenant_id=""),
            MagicMock(),
        )
        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "ACCESS_DENIED"

    def test_user_without_tenant_id_returns_403(self):
        result = handler_get(
            _make_event(groups="", tenant_id=""),
            MagicMock(),
        )
        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert body["error"] == "ACCESS_DENIED"


# ---------------------------------------------------------------------------
# Pagination tests
# ---------------------------------------------------------------------------


class TestFlagsGetPagination:
    def test_next_cursor_absent_when_no_more_pages(self):
        with (
            patch("sitespy.data.list_flags", return_value=([_OPEN_FLAG], None)),
            patch("sitespy.data.get_latest_img_record", return_value=None),
        ):
            result = handler_get(_make_event(), MagicMock())

        body = json.loads(result["body"])
        assert body["next_cursor"] is None

    def test_next_cursor_present_when_more_pages(self):
        last_key = {
            "PK": {"S": "TENANT#acme_corp"},
            "SK": {"S": "FLAG#site_001#cam_01#2025-06-15T14:03:00Z"},
            "GSI1PK": {"S": "FLAGSTATUS#open"},
            "GSI1SK": {"S": "2025-06-15T14:03:00Z"},
        }
        with (
            patch("sitespy.data.list_flags", return_value=([_OPEN_FLAG], last_key)),
            patch("sitespy.data.get_latest_img_record", return_value=None),
        ):
            result = handler_get(_make_event(), MagicMock())

        body = json.loads(result["body"])
        assert body["next_cursor"] is not None
        # Verify it's valid base64-encoded JSON
        decoded = json.loads(base64.b64decode(body["next_cursor"]))
        assert "PK" in decoded

    def test_cursor_passed_to_list_flags(self):
        last_key = {
            "PK": {"S": "TENANT#acme_corp"},
            "SK": {"S": "FLAG#site_001#cam_01#2025-06-15T14:03:00Z"},
            "GSI1PK": {"S": "FLAGSTATUS#open"},
            "GSI1SK": {"S": "2025-06-15T14:03:00Z"},
        }
        cursor = base64.b64encode(json.dumps(last_key).encode()).decode()

        with (
            patch("sitespy.data.list_flags", return_value=([], None)) as mock_list,
        ):
            handler_get(
                _make_event(query_params={"cursor": cursor}), MagicMock()
            )

        call_kwargs = mock_list.call_args.kwargs
        assert call_kwargs["exclusive_start_key"] == last_key

    def test_invalid_cursor_returns_400(self):
        result = handler_get(
            _make_event(query_params={"cursor": "not-valid-base64!!!"}),
            MagicMock(),
        )
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"

    def test_total_available_equals_flag_count_when_no_next_page(self):
        with (
            patch("sitespy.data.list_flags", return_value=([_OPEN_FLAG], None)),
            patch("sitespy.data.get_latest_img_record", return_value=None),
        ):
            result = handler_get(_make_event(), MagicMock())

        body = json.loads(result["body"])
        assert body["total_available"] == 1

    def test_total_available_is_count_plus_one_when_next_page(self):
        last_key = {
            "PK": {"S": "TENANT#acme_corp"},
            "SK": {"S": "FLAG#site_001#cam_01#2025-06-15T14:03:00Z"},
            "GSI1PK": {"S": "FLAGSTATUS#open"},
            "GSI1SK": {"S": "2025-06-15T14:03:00Z"},
        }
        with (
            patch("sitespy.data.list_flags", return_value=([_OPEN_FLAG], last_key)),
            patch("sitespy.data.get_latest_img_record", return_value=None),
        ):
            result = handler_get(_make_event(), MagicMock())

        body = json.loads(result["body"])
        assert body["total_available"] == 2  # 1 flag + 1 for next page


# ---------------------------------------------------------------------------
# Limit parameter tests
# ---------------------------------------------------------------------------


class TestFlagsGetLimit:
    def test_default_limit_is_50(self):
        with (
            patch("sitespy.data.list_flags", return_value=([], None)) as mock_list,
        ):
            handler_get(_make_event(), MagicMock())

        call_kwargs = mock_list.call_args.kwargs
        assert call_kwargs["limit"] == 50

    def test_custom_limit_passed_through(self):
        with (
            patch("sitespy.data.list_flags", return_value=([], None)) as mock_list,
        ):
            handler_get(
                _make_event(query_params={"limit": "10"}), MagicMock()
            )

        call_kwargs = mock_list.call_args.kwargs
        assert call_kwargs["limit"] == 10

    def test_limit_above_max_returns_400(self):
        result = handler_get(
            _make_event(query_params={"limit": "201"}), MagicMock()
        )
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"

    def test_limit_zero_returns_400(self):
        result = handler_get(
            _make_event(query_params={"limit": "0"}), MagicMock()
        )
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"

    def test_non_integer_limit_returns_400(self):
        result = handler_get(
            _make_event(query_params={"limit": "abc"}), MagicMock()
        )
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "BAD_REQUEST"

    def test_max_limit_200_is_valid(self):
        with (
            patch("sitespy.data.list_flags", return_value=([], None)) as mock_list,
        ):
            result = handler_get(
                _make_event(query_params={"limit": "200"}), MagicMock()
            )

        assert result["statusCode"] == 200
        call_kwargs = mock_list.call_args.kwargs
        assert call_kwargs["limit"] == 200


# ---------------------------------------------------------------------------
# Optional filter parameter tests
# ---------------------------------------------------------------------------


class TestFlagsGetFilters:
    def test_site_id_filter_passed_to_list_flags(self):
        with (
            patch("sitespy.data.list_flags", return_value=([], None)) as mock_list,
        ):
            handler_get(
                _make_event(query_params={"site_id": "site_001"}), MagicMock()
            )

        call_kwargs = mock_list.call_args.kwargs
        assert call_kwargs["site_id"] == "site_001"

    def test_camera_id_filter_passed_to_list_flags(self):
        with (
            patch("sitespy.data.list_flags", return_value=([], None)) as mock_list,
        ):
            handler_get(
                _make_event(query_params={"camera_id": "cam_01"}), MagicMock()
            )

        call_kwargs = mock_list.call_args.kwargs
        assert call_kwargs["camera_id"] == "cam_01"

    def test_no_filters_passes_none_to_list_flags(self):
        with (
            patch("sitespy.data.list_flags", return_value=([], None)) as mock_list,
        ):
            handler_get(_make_event(), MagicMock())

        call_kwargs = mock_list.call_args.kwargs
        assert call_kwargs["site_id"] is None
        assert call_kwargs["camera_id"] is None
