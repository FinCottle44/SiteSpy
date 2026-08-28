"""Property tests for the out-of-hours snapshot handlers.

Covers the review / promote / download flow in
``sitespy.handlers.snapshots_out_of_hours`` against the correctness properties
from the working-hours-retention design:

- Property 15: Review returns the correct filtered, ordered set (Req 8.1)
- Property 16: Review entry shape (Req 8.3)
- Property 17: Promotion result (Req 9.1, 9.2, 9.3)
- Property 18: Promotion idempotence (Req 9.5)
- Property 19: Promotion failure leaves original expiry intact (Req 9.7)
- Property 20: Download URL for promoted snapshots (Req 10.1)

All AWS interactions use moto; the DynamoDB table and S3 bucket mirror the
project schema (PK/SK + GSI1), and records are seeded through the shared
``data`` / ``storage`` helpers exactly as production does.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

import boto3
from hypothesis import assume, given, settings
from hypothesis import strategies as st
from moto import mock_aws

# ---------------------------------------------------------------------------
# Environment setup (must happen before importing handler modules)
# ---------------------------------------------------------------------------

os.environ.setdefault("SNAPSHOTS_BUCKET", "test-snapshots-bucket")
os.environ.setdefault("DATA_TABLE", "test-data-table")
os.environ.setdefault("AWS_REGION", "eu-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-west-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("LOG_LEVEL", "INFO")

from sitespy import data, storage  # noqa: E402
from sitespy.config import get_settings  # noqa: E402
from sitespy.data import _dynamodb_client  # noqa: E402
from sitespy.handlers.snapshots_out_of_hours import (  # noqa: E402
    handler_download,
    handler_list,
    handler_promote,
)
from sitespy.storage import _s3_client  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TENANT_ID = "acme_corp"
_SITE_ID = "site_001"
_CAMERA_ID = "cam_01"
_BASE = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
_TS_RE = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$"
_NO_EXPIRE_TTL = 9999999999  # far-future ttl so seeded records are not "expired"
_TTL_SECONDS = 604800  # Out_Of_Hours 7-day TTL


# ---------------------------------------------------------------------------
# AWS setup / seeding helpers
# ---------------------------------------------------------------------------


def _setup_aws() -> tuple[Any, Any]:
    """Create the moto S3 bucket and DynamoDB table matching the project schema."""
    s3 = boto3.client("s3", region_name="eu-west-2")
    s3.create_bucket(
        Bucket="test-snapshots-bucket",
        CreateBucketConfiguration={"LocationConstraint": "eu-west-2"},
    )

    ddb = boto3.client("dynamodb", region_name="eu-west-2")
    ddb.create_table(
        TableName="test-data-table",
        BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "GSI1",
                "KeySchema": [
                    {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
    )
    return s3, ddb


def _seed_site(ddb) -> None:
    """Write the site record so the handler's auth get_site lookup succeeds."""
    ddb.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": f"TENANT#{_TENANT_ID}"},
            "SK": {"S": f"SITE#{_SITE_ID}"},
            "site_name": {"S": "Acme Tower"},
            "timezone": {"S": "Europe/London"},
        },
    )


def _fmt(offset_seconds: int) -> str:
    """Format a base-relative offset as a YYYY-MM-DDTHH:MM:SSZ timestamp."""
    return (_BASE + timedelta(seconds=offset_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _seed_ooh(ts: str, *, ttl: int = _NO_EXPIRE_TTL) -> str:
    """Seed an Out_Of_Hours record (+ its S3 object) and return the s3_key."""
    s3_key = storage.build_out_of_hours_key(_TENANT_ID, _SITE_ID, _CAMERA_ID, ts)
    data.put_out_of_hours_img_record(
        tenant_id=_TENANT_ID,
        site_id=_SITE_ID,
        camera_id=_CAMERA_ID,
        snapshot_ts=ts,
        s3_key=s3_key,
        sha256_hex="0" * 64,
        size_bytes=1024,
        ttl=ttl,
    )
    storage.put_out_of_hours_snapshot(
        key=s3_key,
        body=b"\xff\xd8\xff\xe0jpeg-bytes",
        sha256_hex="0" * 64,
        snapshot_ts=ts,
        tenant_id=_TENANT_ID,
    )
    return s3_key


def _seed_in_hours(ts: str) -> None:
    """Seed an In_Hours (IMG#) record to prove review excludes it."""
    data.put_img_record(
        tenant_id=_TENANT_ID,
        site_id=_SITE_ID,
        camera_id=_CAMERA_ID,
        snapshot_ts=ts,
        s3_key=f"{_TENANT_ID}/{_SITE_ID}/{_CAMERA_ID}/{ts}.jpg",
        sha256_hex="0" * 64,
        size_bytes=1024,
        retention_class="In_Hours",
    )


def _list_event(from_ts: str | None = None, to_ts: str | None = None) -> dict[str, Any]:
    query_params: dict[str, str] = {"site_id": _SITE_ID, "camera_id": _CAMERA_ID}
    if from_ts is not None:
        query_params["from"] = from_ts
    if to_ts is not None:
        query_params["to"] = to_ts
    return _wrap_event(query_params, method="GET")


def _download_event(snapshot_id: str) -> dict[str, Any]:
    query_params = {
        "site_id": _SITE_ID,
        "camera_id": _CAMERA_ID,
        "snapshot_id": snapshot_id,
    }
    return _wrap_event(query_params, method="GET")


def _promote_event(snapshot_id: str) -> dict[str, Any]:
    body = {
        "site_id": _SITE_ID,
        "camera_id": _CAMERA_ID,
        "snapshot_id": snapshot_id,
    }
    event = _wrap_event(None, method="POST")
    event["body"] = json.dumps(body)
    return event


def _wrap_event(query_params: dict[str, str] | None, *, method: str) -> dict[str, Any]:
    return {
        "httpMethod": method,
        "path": "/v1/snapshots/out-of-hours",
        "pathParameters": None,
        "queryStringParameters": query_params,
        "headers": {"X-Correlation-Id": "test-corr-id"},
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:groups": "TenantAdmins",
                    "custom:tenant_id": _TENANT_ID,
                    "custom:site_access": "",
                }
            }
        },
    }


def _reset_caches() -> None:
    get_settings.cache_clear()
    _s3_client.cache_clear()
    _dynamodb_client.cache_clear()


def _get_record(ts: str):
    return data.get_out_of_hours_img_record(_TENANT_ID, _SITE_ID, _CAMERA_ID, ts)


def _assert_url_expiry(url: str, expected: int) -> None:
    qs = parse_qs(urlparse(url).query)
    assert qs.get("X-Amz-Expires") == [str(expected)]


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Unique second-offsets within a 60-day span so timestamps are distinct and
# fewer than the default page size of 50.
_OFFSETS_ST = st.lists(
    st.integers(min_value=0, max_value=60 * 24 * 3600),
    min_size=1,
    max_size=12,
    unique=True,
)
_RANGE_BOUND_ST = st.integers(min_value=-1000, max_value=60 * 24 * 3600 + 1000)


# ===========================================================================
# Property 15: Review returns the correct filtered, ordered set
# Feature: working-hours-retention, Property 15: Review returns the correct filtered, ordered set
# Validates: Requirements 8.1
# ===========================================================================


@given(offsets=_OFFSETS_ST, bound_a=_RANGE_BOUND_ST, bound_b=_RANGE_BOUND_ST)
@settings(max_examples=100, deadline=None)
def test_property_15_review_filtered_ordered_set(offsets, bound_a, bound_b):
    """Review returns exactly the OOH records with from<=ts<=to, newest-first."""
    from_off, to_off = sorted((bound_a, bound_b))
    from_ts = _fmt(from_off) if from_off >= 0 else "2025-01-01T00:00:00Z"
    to_ts = _fmt(to_off)

    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)

        ooh_ts = [_fmt(o) for o in offsets]
        for ts in ooh_ts:
            _seed_ooh(ts)

        # Seed In_Hours (IMG#) records at the same timestamps; review must never
        # surface them — they live under a distinct SK prefix.
        for ts in ooh_ts[:2]:
            _seed_in_hours(ts)

        expected = sorted(
            (ts for ts in ooh_ts if from_ts <= ts <= to_ts),
            reverse=True,
        )

        result = handler_list(_list_event(from_ts, to_ts), MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        returned = [snap["timestamp"] for snap in body["snapshots"]]

        # Exact filtered set, in newest-first order.
        assert returned == expected
        # Every returned entry is an Out_Of_Hours record (security/ key prefix).
        for snap in body["snapshots"]:
            assert snap["key"].startswith("security/")


# ===========================================================================
# Property 16: Review entry shape
# Feature: working-hours-retention, Property 16: Review entry shape
# Validates: Requirements 8.3
# ===========================================================================


@given(offsets=_OFFSETS_ST)
@settings(max_examples=100, deadline=None)
def test_property_16_review_entry_shape(offsets):
    """Each review entry has ts, key, a 300 s presign, and promotion state."""
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)

        ooh_ts = [_fmt(o) for o in offsets]
        for ts in ooh_ts:
            _seed_ooh(ts)

        # A window that comfortably brackets every seeded timestamp.
        result = handler_list(
            _list_event("2025-01-01T00:00:00Z", "2030-01-01T00:00:00Z"), MagicMock()
        )

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["snapshots"]) == len(ooh_ts)

        import re

        for snap in body["snapshots"]:
            # Timestamp in strict YYYY-MM-DDTHH:MM:SSZ UTC format.
            assert re.match(_TS_RE, snap["timestamp"])
            # Storage key present.
            assert snap["key"]
            # 300 s presigned URL.
            assert snap["expires_in"] == 300
            _assert_url_expiry(snap["presigned_url"], 300)
            # Promotion state present as a boolean.
            assert "promoted" in snap
            assert isinstance(snap["promoted"], bool)


# ===========================================================================
# Property 17: Promotion result
# Feature: working-hours-retention, Property 17: Promotion result
# Validates: Requirements 9.1, 9.2, 9.3
# ===========================================================================


@given(offset=st.integers(min_value=0, max_value=60 * 24 * 3600))
@settings(max_examples=100, deadline=None)
def test_property_17_promotion_result(offset):
    """After a successful promotion: promoted=true, no ttl, object under preserved/."""
    ts = _fmt(offset)

    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)
        # A near-future ttl proves the record was subject to expiry pre-promotion.
        _seed_ooh(ts, ttl=int(datetime.now(tz=UTC).timestamp()) + _TTL_SECONDS)

        result = handler_promote(_promote_event(ts), MagicMock())

        assert result["statusCode"] == 200
        rbody = json.loads(result["body"])
        assert rbody["promoted"] is True

        record = _get_record(ts)
        assert record is not None
        # promoted = true
        assert record.get("promoted", {}).get("BOOL") is True
        # ttl removed → no longer auto-expired, survives past 604800 s
        assert "ttl" not in record
        # object relocated under preserved/ (not security/)
        preserved_key = record.get("s3_key", {}).get("S", "")
        assert preserved_key.startswith("preserved/")
        assert not preserved_key.startswith("security/")
        # object physically resides at the preserved key
        assert storage.object_exists(preserved_key) is True


# ===========================================================================
# Property 18: Promotion idempotence
# Feature: working-hours-retention, Property 18: Promotion idempotence
# Validates: Requirements 9.5
# ===========================================================================


@given(offset=st.integers(min_value=0, max_value=60 * 24 * 3600))
@settings(max_examples=100, deadline=None)
def test_property_18_promotion_idempotence(offset):
    """Promoting twice yields the same final state as promoting once."""
    ts = _fmt(offset)

    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)
        _seed_ooh(ts, ttl=int(datetime.now(tz=UTC).timestamp()) + _TTL_SECONDS)

        first = handler_promote(_promote_event(ts), MagicMock())
        assert first["statusCode"] == 200
        record_after_first = _get_record(ts)
        assert record_after_first is not None
        key_after_first = record_after_first.get("s3_key", {}).get("S", "")

        second = handler_promote(_promote_event(ts), MagicMock())
        assert second["statusCode"] == 200
        sbody = json.loads(second["body"])
        assert sbody["promoted"] is True

        record_after_second = _get_record(ts)
        assert record_after_second is not None
        # Promotion state unchanged.
        assert record_after_second.get("promoted", {}).get("BOOL") is True
        assert "ttl" not in record_after_second
        # Storage location unchanged by the second call.
        assert record_after_second.get("s3_key", {}).get("S", "") == key_after_first
        assert key_after_first.startswith("preserved/")


# ===========================================================================
# Property 19: Promotion failure leaves original expiry intact
# Feature: working-hours-retention, Property 19: Promotion failure leaves original expiry intact
# Validates: Requirements 9.7
# ===========================================================================


@given(
    offset=st.integers(min_value=0, max_value=60 * 24 * 3600),
    fail_on_copy=st.booleans(),
)
@settings(max_examples=100, deadline=None)
def test_property_19_promotion_failure_leaves_original_intact(offset, fail_on_copy):
    """A copy or commit failure → error; ttl intact and object still under security/."""
    ts = _fmt(offset)
    original_ttl = int(datetime.now(tz=UTC).timestamp()) + _TTL_SECONDS

    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)
        source_key = _seed_ooh(ts, ttl=original_ttl)

        boom = RuntimeError("simulated S3/DynamoDB failure")
        if fail_on_copy:
            # Copy fails before any mutation.
            patch_target = patch("sitespy.storage.copy_object", side_effect=boom)
        else:
            # Copy succeeds, commit fails → handler rolls the copy back.
            patch_target = patch(
                "sitespy.data.promote_out_of_hours_record", side_effect=boom
            )

        with patch_target:
            result = handler_promote(_promote_event(ts), MagicMock())

        # Error response, promotion did not complete (Req 9.7).
        assert result["statusCode"] == 500

        record = _get_record(ts)
        assert record is not None
        # ttl intact — snapshot still subject to its original expiry.
        assert "ttl" in record
        assert int(record["ttl"]["N"]) == original_ttl
        # Not marked promoted.
        assert record.get("promoted", {}).get("BOOL") is False
        # s3_key still under security/ (unchanged).
        assert record.get("s3_key", {}).get("S", "") == source_key
        assert source_key.startswith("security/")
        # Original object still present under security/.
        assert storage.object_exists(source_key) is True


# ===========================================================================
# Property 20: Download URL for promoted snapshots
# Feature: working-hours-retention, Property 20: Download URL for promoted snapshots
# Validates: Requirements 10.1
# ===========================================================================


@given(offset=st.integers(min_value=0, max_value=60 * 24 * 3600))
@settings(max_examples=100, deadline=None)
def test_property_20_download_url_expiry_is_900(offset):
    """A promoted Out_Of_Hours snapshot yields a presign that expires in exactly 900 s."""
    ts = _fmt(offset)

    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)
        # Seed an OOH record then promote it so it is preserved + downloadable.
        _seed_ooh(ts, ttl=int(datetime.now(tz=UTC).timestamp()) + _TTL_SECONDS)
        promote_result = handler_promote(_promote_event(ts), MagicMock())
        assert promote_result["statusCode"] == 200

        result = handler_download(_download_event(ts), MagicMock())

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["expires_in"] == 900
        _assert_url_expiry(body["presigned_url"], 900)
