"""Example/edge tests for the out-of-hours snapshot handlers.

Exercises the review / promote / download handlers in
``sitespy.handlers.snapshots_out_of_hours`` against moto-backed S3 + DynamoDB,
mirroring the seeding and JWT-authorizer conventions used by the other
snapshot handler tests.

Covers:

Review (GET /v1/snapshots/out-of-hours):
- default 30-day range              (Req 8.2)
- empty list                        (Req 8.5)
- pagination bounds (limit 1-200)   (Req 8.4)
- pagination cursor round-trip      (Req 8.4)
- invalid params                    (Req 8.6)
- access denied 403                 (Req 8.7)

Promote (POST /v1/snapshots/out-of-hours/promote):
- 404: missing / expired / not Out_Of_Hours   (Req 9.4)
- 400: missing parameter                       (Req 9.6)
- 403: access denied                           (Req 9.8)

Download (GET /v1/snapshots/out-of-hours/download):
- 404: missing / expired-and-never-promoted    (Req 10.2)
- 400: missing parameter                        (Req 10.3)
- 403: access denied                            (Req 10.4)
"""

from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import boto3
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

from sitespy import data  # noqa: E402
from sitespy.config import get_settings  # noqa: E402
from sitespy.data import _dynamodb_client, build_tenant_pk  # noqa: E402
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
_BUCKET = "test-snapshots-bucket"
_TABLE = "test-data-table"

_FAR_FUTURE_TTL = 9999999999
_PAST_TTL = 100  # 1970 → always elapsed


def _format_ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# AWS setup / seeding helpers
# ---------------------------------------------------------------------------


def _setup_aws() -> tuple[Any, Any]:
    """Create the moto S3 bucket and DynamoDB table matching the project schema."""
    s3 = boto3.client("s3", region_name="eu-west-2")
    s3.create_bucket(
        Bucket=_BUCKET,
        CreateBucketConfiguration={"LocationConstraint": "eu-west-2"},
    )

    ddb = boto3.client("dynamodb", region_name="eu-west-2")
    ddb.create_table(
        TableName=_TABLE,
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
    ddb.put_item(
        TableName=_TABLE,
        Item={
            "PK": {"S": f"TENANT#{_TENANT_ID}"},
            "SK": {"S": f"SITE#{_SITE_ID}"},
            "site_name": {"S": "Acme Tower"},
            "timezone": {"S": "Europe/London"},
        },
    )


def _seed_ooh(
    snapshot_ts: str,
    *,
    ttl: int = _FAR_FUTURE_TTL,
    s3_key: str | None = None,
) -> str:
    """Seed an Out_Of_Hours record; returns the snapshot_ts (== snapshot_id)."""
    key = s3_key or f"security/{_TENANT_ID}/{_SITE_ID}/{_CAMERA_ID}/{snapshot_ts}.jpg"
    data.put_out_of_hours_img_record(
        tenant_id=_TENANT_ID,
        site_id=_SITE_ID,
        camera_id=_CAMERA_ID,
        snapshot_ts=snapshot_ts,
        s3_key=key,
        sha256_hex="0" * 64,
        size_bytes=1024,
        ttl=ttl,
    )
    return snapshot_ts


def _seed_ooh_with_class(snapshot_ts: str, retention_class: str) -> str:
    """Seed a record under the OOH_IMG# SK but with an arbitrary retention_class."""
    _dynamodb_client().put_item(
        TableName=_TABLE,
        Item={
            "PK": {"S": build_tenant_pk(_TENANT_ID)},
            "SK": {"S": data.build_out_of_hours_img_sk(_SITE_ID, _CAMERA_ID, snapshot_ts)},
            "s3_key": {
                "S": f"security/{_TENANT_ID}/{_SITE_ID}/{_CAMERA_ID}/{snapshot_ts}.jpg"
            },
            "ingested_at": {"S": snapshot_ts},
            "retention_class": {"S": retention_class},
            "ttl": {"N": str(_FAR_FUTURE_TTL)},
            "promoted": {"BOOL": False},
        },
    )
    return snapshot_ts


def _reset_caches() -> None:
    get_settings.cache_clear()
    _s3_client.cache_clear()
    _dynamodb_client.cache_clear()


# ---------------------------------------------------------------------------
# Event builders
# ---------------------------------------------------------------------------


def _claims(
    groups: str = "TenantAdmins",
    tenant_id: str = _TENANT_ID,
    site_access: str = "",
) -> dict[str, Any]:
    return {
        "cognito:groups": groups,
        "custom:tenant_id": tenant_id,
        "custom:site_access": site_access,
    }


def _review_event(
    *,
    site_id: str | None = _SITE_ID,
    camera_id: str | None = _CAMERA_ID,
    from_ts: str | None = None,
    to_ts: str | None = None,
    limit: str | None = None,
    cursor: str | None = None,
    claims: dict[str, Any] | None = None,
) -> dict[str, Any]:
    query: dict[str, str] = {}
    if site_id is not None:
        query["site_id"] = site_id
    if camera_id is not None:
        query["camera_id"] = camera_id
    if from_ts is not None:
        query["from"] = from_ts
    if to_ts is not None:
        query["to"] = to_ts
    if limit is not None:
        query["limit"] = limit
    if cursor is not None:
        query["cursor"] = cursor
    return {
        "httpMethod": "GET",
        "path": "/v1/snapshots/out-of-hours",
        "queryStringParameters": query,
        "headers": {"X-Correlation-Id": "test-corr-id"},
        "requestContext": {"authorizer": {"claims": claims or _claims()}},
    }


def _promote_event(
    *,
    site_id: str | None = _SITE_ID,
    camera_id: str | None = _CAMERA_ID,
    snapshot_id: str | None = None,
    claims: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, str] = {}
    if site_id is not None:
        body["site_id"] = site_id
    if camera_id is not None:
        body["camera_id"] = camera_id
    if snapshot_id is not None:
        body["snapshot_id"] = snapshot_id
    return {
        "httpMethod": "POST",
        "path": "/v1/snapshots/out-of-hours/promote",
        "body": json.dumps(body),
        "headers": {"X-Correlation-Id": "test-corr-id"},
        "requestContext": {"authorizer": {"claims": claims or _claims()}},
    }


def _download_event(
    *,
    site_id: str | None = _SITE_ID,
    camera_id: str | None = _CAMERA_ID,
    snapshot_id: str | None = None,
    claims: dict[str, Any] | None = None,
) -> dict[str, Any]:
    query: dict[str, str] = {}
    if site_id is not None:
        query["site_id"] = site_id
    if camera_id is not None:
        query["camera_id"] = camera_id
    if snapshot_id is not None:
        query["snapshot_id"] = snapshot_id
    return {
        "httpMethod": "GET",
        "path": "/v1/snapshots/out-of-hours/download",
        "queryStringParameters": query,
        "headers": {"X-Correlation-Id": "test-corr-id"},
        "requestContext": {"authorizer": {"claims": claims or _claims()}},
    }


# A "user" who belongs to the right tenant but has no access to _SITE_ID.
_NO_ACCESS_CLAIMS = _claims(groups="", tenant_id=_TENANT_ID, site_access="site_999")


# ===========================================================================
# Review — GET /v1/snapshots/out-of-hours
# ===========================================================================


def test_review_default_range_covers_last_30_days():
    """Req 8.2 — omitting from/to applies a default range of the last 30 days."""
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)
        now = datetime.now(tz=UTC)
        recent = _seed_ooh(_format_ts(now - timedelta(days=5)))
        old = _seed_ooh(_format_ts(now - timedelta(days=40)))

        result = handler_list(_review_event(), MagicMock())

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    returned = {s["snapshot_id"] for s in body["snapshots"]}
    assert recent in returned  # within the default 30-day window
    assert old not in returned  # older than 30 days → excluded


def test_review_empty_list_returns_200():
    """Req 8.5 — no matching records yields HTTP 200 with an empty list."""
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)

        result = handler_list(_review_event(), MagicMock())

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["snapshots"] == []
    assert body["next_cursor"] is None


def test_review_limit_one_returns_single_item_with_cursor():
    """Req 8.4 — a page size of 1 returns one item and a continuation cursor."""
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)
        now = datetime.now(tz=UTC)
        _seed_ooh(_format_ts(now - timedelta(days=1)))
        _seed_ooh(_format_ts(now - timedelta(days=2)))
        _seed_ooh(_format_ts(now - timedelta(days=3)))

        result = handler_list(_review_event(limit="1"), MagicMock())

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert len(body["snapshots"]) == 1
    assert body["next_cursor"] is not None


def test_review_cursor_round_trip_paginates():
    """Req 8.4 — following next_cursor returns the subsequent page."""
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)
        now = datetime.now(tz=UTC)
        _seed_ooh(_format_ts(now - timedelta(days=1)))
        _seed_ooh(_format_ts(now - timedelta(days=2)))

        first = handler_list(_review_event(limit="1"), MagicMock())
        first_body = json.loads(first["body"])
        cursor = first_body["next_cursor"]

        second = handler_list(_review_event(limit="1", cursor=cursor), MagicMock())
        second_body = json.loads(second["body"])

    assert second["statusCode"] == 200
    assert len(second_body["snapshots"]) == 1
    first_id = first_body["snapshots"][0]["snapshot_id"]
    second_id = second_body["snapshots"][0]["snapshot_id"]
    assert first_id != second_id  # cursor advanced to a new record


def test_review_limit_200_boundary_is_valid():
    """Req 8.4 — the maximum page size of 200 is accepted."""
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)

        result = handler_list(_review_event(limit="200"), MagicMock())

    assert result["statusCode"] == 200


def test_review_missing_site_id_returns_400():
    """Req 8.6 — a missing site_id is rejected with 400."""
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)

        result = handler_list(_review_event(site_id=None), MagicMock())

    assert result["statusCode"] == 400
    assert json.loads(result["body"])["error"] == "BAD_REQUEST"


def test_review_missing_camera_id_returns_400():
    """Req 8.6 — a missing camera_id is rejected with 400."""
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)

        result = handler_list(_review_event(camera_id=None), MagicMock())

    assert result["statusCode"] == 400
    assert json.loads(result["body"])["error"] == "BAD_REQUEST"


def test_review_bad_from_returns_400():
    """Req 8.6 — a malformed from datetime is rejected with 400."""
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)

        result = handler_list(_review_event(from_ts="2025-13-40"), MagicMock())

    assert result["statusCode"] == 400
    assert json.loads(result["body"])["error"] == "BAD_REQUEST"


def test_review_bad_to_returns_400():
    """Req 8.6 — a malformed to datetime is rejected with 400."""
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)

        result = handler_list(_review_event(to_ts="not-a-datetime"), MagicMock())

    assert result["statusCode"] == 400
    assert json.loads(result["body"])["error"] == "BAD_REQUEST"


def test_review_limit_zero_returns_400():
    """Req 8.6 — a page size below 1 is rejected with 400."""
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)

        result = handler_list(_review_event(limit="0"), MagicMock())

    assert result["statusCode"] == 400
    assert json.loads(result["body"])["error"] == "BAD_REQUEST"


def test_review_limit_over_max_returns_400():
    """Req 8.6 — a page size above 200 is rejected with 400."""
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)

        result = handler_list(_review_event(limit="201"), MagicMock())

    assert result["statusCode"] == 400
    assert json.loads(result["body"])["error"] == "BAD_REQUEST"


def test_review_non_integer_limit_returns_400():
    """Req 8.6 — a non-integer page size is rejected with 400."""
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)

        result = handler_list(_review_event(limit="abc"), MagicMock())

    assert result["statusCode"] == 400
    assert json.loads(result["body"])["error"] == "BAD_REQUEST"


def test_review_malformed_cursor_returns_400():
    """Req 8.6 — a malformed pagination cursor is rejected with 400."""
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)

        result = handler_list(_review_event(cursor="!!!not-base64!!!"), MagicMock())

    assert result["statusCode"] == 400
    assert json.loads(result["body"])["error"] == "BAD_REQUEST"


def test_review_non_dict_cursor_returns_400():
    """Req 8.6 — a well-formed but non-object cursor is rejected with 400."""
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)

        bad_cursor = base64.b64encode(json.dumps(["not", "a", "dict"]).encode()).decode()
        result = handler_list(_review_event(cursor=bad_cursor), MagicMock())

    assert result["statusCode"] == 400
    assert json.loads(result["body"])["error"] == "BAD_REQUEST"


def test_review_access_denied_returns_403():
    """Req 8.7 — a caller lacking site access is rejected with 403."""
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)

        result = handler_list(
            _review_event(claims=_NO_ACCESS_CLAIMS), MagicMock()
        )

    assert result["statusCode"] == 403
    assert json.loads(result["body"])["error"] == "ACCESS_DENIED"


# ===========================================================================
# Promote — POST /v1/snapshots/out-of-hours/promote
# ===========================================================================


def test_promote_missing_snapshot_returns_404():
    """Req 9.4 — promoting a non-existent snapshot returns 404."""
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)

        result = handler_promote(
            _promote_event(snapshot_id="2026-01-01T22:00:00Z"), MagicMock()
        )

    assert result["statusCode"] == 404
    assert json.loads(result["body"])["error"] == "NOT_FOUND"


def test_promote_expired_snapshot_returns_404():
    """Req 9.4 — promoting an expired, never-promoted snapshot returns 404."""
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)
        snapshot_id = _seed_ooh("2026-01-01T22:00:00Z", ttl=_PAST_TTL)

        result = handler_promote(
            _promote_event(snapshot_id=snapshot_id), MagicMock()
        )

    assert result["statusCode"] == 404
    assert json.loads(result["body"])["error"] == "NOT_FOUND"


def test_promote_non_out_of_hours_returns_404():
    """Req 9.4 — a record not classified Out_Of_Hours returns 404."""
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)
        snapshot_id = _seed_ooh_with_class("2026-01-01T22:00:00Z", "In_Hours")

        result = handler_promote(
            _promote_event(snapshot_id=snapshot_id), MagicMock()
        )

    assert result["statusCode"] == 404
    assert json.loads(result["body"])["error"] == "NOT_FOUND"


def test_promote_missing_snapshot_id_returns_400():
    """Req 9.6 — a missing snapshot_id parameter is rejected with 400."""
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)

        result = handler_promote(_promote_event(snapshot_id=None), MagicMock())

    assert result["statusCode"] == 400
    body = json.loads(result["body"])
    assert body["error"] == "BAD_REQUEST"
    assert "snapshot_id" in body["message"]


def test_promote_missing_site_id_returns_400():
    """Req 9.6 — a missing site_id parameter is rejected with 400."""
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)

        result = handler_promote(
            _promote_event(site_id=None, snapshot_id="2026-01-01T22:00:00Z"),
            MagicMock(),
        )

    assert result["statusCode"] == 400
    body = json.loads(result["body"])
    assert body["error"] == "BAD_REQUEST"
    assert "site_id" in body["message"]


def test_promote_missing_camera_id_returns_400():
    """Req 9.6 — a missing camera_id parameter is rejected with 400."""
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)

        result = handler_promote(
            _promote_event(camera_id=None, snapshot_id="2026-01-01T22:00:00Z"),
            MagicMock(),
        )

    assert result["statusCode"] == 400
    body = json.loads(result["body"])
    assert body["error"] == "BAD_REQUEST"
    assert "camera_id" in body["message"]


def test_promote_access_denied_returns_403():
    """Req 9.8 — a caller lacking site access is rejected with 403."""
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)
        snapshot_id = _seed_ooh("2026-01-01T22:00:00Z")

        result = handler_promote(
            _promote_event(snapshot_id=snapshot_id, claims=_NO_ACCESS_CLAIMS),
            MagicMock(),
        )

    assert result["statusCode"] == 403
    assert json.loads(result["body"])["error"] == "ACCESS_DENIED"


# ===========================================================================
# Download — GET /v1/snapshots/out-of-hours/download
# ===========================================================================


def test_download_missing_snapshot_returns_404():
    """Req 10.2 — downloading a non-existent snapshot returns 404."""
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)

        result = handler_download(
            _download_event(snapshot_id="2026-01-01T22:00:00Z"), MagicMock()
        )

    assert result["statusCode"] == 404
    assert json.loads(result["body"])["error"] == "NOT_FOUND"


def test_download_expired_never_promoted_returns_404():
    """Req 10.2 — an expired, never-promoted snapshot returns 404."""
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)
        snapshot_id = _seed_ooh("2026-01-01T22:00:00Z", ttl=_PAST_TTL)

        result = handler_download(
            _download_event(snapshot_id=snapshot_id), MagicMock()
        )

    assert result["statusCode"] == 404
    assert json.loads(result["body"])["error"] == "NOT_FOUND"


def test_download_missing_snapshot_id_returns_400():
    """Req 10.3 — a missing snapshot_id parameter is rejected with 400."""
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)

        result = handler_download(_download_event(snapshot_id=None), MagicMock())

    assert result["statusCode"] == 400
    body = json.loads(result["body"])
    assert body["error"] == "BAD_REQUEST"
    assert "snapshot_id" in body["message"]


def test_download_missing_site_id_returns_400():
    """Req 10.3 — a missing site_id parameter is rejected with 400."""
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)

        result = handler_download(
            _download_event(site_id=None, snapshot_id="2026-01-01T22:00:00Z"),
            MagicMock(),
        )

    assert result["statusCode"] == 400
    body = json.loads(result["body"])
    assert body["error"] == "BAD_REQUEST"
    assert "site_id" in body["message"]


def test_download_missing_camera_id_returns_400():
    """Req 10.3 — a missing camera_id parameter is rejected with 400."""
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)

        result = handler_download(
            _download_event(camera_id=None, snapshot_id="2026-01-01T22:00:00Z"),
            MagicMock(),
        )

    assert result["statusCode"] == 400
    body = json.loads(result["body"])
    assert body["error"] == "BAD_REQUEST"
    assert "camera_id" in body["message"]


def test_download_access_denied_returns_403():
    """Req 10.4 — a caller lacking site access is rejected with 403."""
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)
        snapshot_id = _seed_ooh("2026-01-01T22:00:00Z")

        result = handler_download(
            _download_event(snapshot_id=snapshot_id, claims=_NO_ACCESS_CLAIMS),
            MagicMock(),
        )

    assert result["statusCode"] == 403
    assert json.loads(result["body"])["error"] == "ACCESS_DENIED"
