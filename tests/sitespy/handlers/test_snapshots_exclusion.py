"""Example/edge tests for default snapshot views excluding Out_Of_Hours.

Covers:
- latest with only Out_Of_Hours records → 404 (Req 11.3)
- weather populated vs null in list/latest entries (Req 11.4 / 11.5)
- presigned URL expiry of 300 seconds (Req 11.6)
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

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
from sitespy.data import _dynamodb_client  # noqa: E402
from sitespy.handlers.snapshots import handler_latest, handler_list  # noqa: E402
from sitespy.storage import _s3_client  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TENANT_ID = "acme_corp"
_SITE_ID = "site_001"
_CAMERA_ID = "cam_01"
_TS_IN = "2026-01-01T09:00:00Z"
_TS_OOH = "2026-01-01T22:00:00Z"
_FROM = "2025-01-01T00:00:00Z"
_TO = "2030-01-01T00:00:00Z"

_WEATHER_MAP = {
    "M": {
        "condition": {"S": "Rain"},
        "description": {"S": "light rain"},
        "temp_c": {"N": "14.2"},
        "feels_like_c": {"N": "13.0"},
        "humidity_pct": {"N": "80"},
        "wind_speed_ms": {"N": "5.1"},
        "wind_deg": {"N": "220"},
        "visibility_m": {"N": "10000"},
        "cloud_pct": {"N": "75"},
    }
}


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
    ddb.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": f"TENANT#{_TENANT_ID}"},
            "SK": {"S": f"SITE#{_SITE_ID}"},
            "site_name": {"S": "Acme Tower"},
            "timezone": {"S": "Europe/London"},
        },
    )


def _put_in_hours(weather: dict | None = None) -> None:
    data.put_img_record(
        tenant_id=_TENANT_ID,
        site_id=_SITE_ID,
        camera_id=_CAMERA_ID,
        snapshot_ts=_TS_IN,
        s3_key=f"{_TENANT_ID}/{_SITE_ID}/{_CAMERA_ID}/{_TS_IN}.jpg",
        sha256_hex="0" * 64,
        size_bytes=1024,
        weather=weather,
        retention_class="In_Hours",
    )


def _put_out_of_hours() -> None:
    data.put_out_of_hours_img_record(
        tenant_id=_TENANT_ID,
        site_id=_SITE_ID,
        camera_id=_CAMERA_ID,
        snapshot_ts=_TS_OOH,
        s3_key=f"security/{_TENANT_ID}/{_SITE_ID}/{_CAMERA_ID}/{_TS_OOH}.jpg",
        sha256_hex="0" * 64,
        size_bytes=1024,
        ttl=9999999999,
    )


def _make_event(camera_id: str | None = _CAMERA_ID, with_range: bool = True) -> dict[str, Any]:
    query_params: dict[str, str] = {"site_id": _SITE_ID}
    if camera_id is not None:
        query_params["camera_id"] = camera_id
    if with_range:
        query_params["from"] = _FROM
        query_params["to"] = _TO
    return {
        "httpMethod": "GET",
        "path": "/v1/snapshots",
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


# ---------------------------------------------------------------------------
# Req 11.3 — latest with only Out_Of_Hours records → 404
# ---------------------------------------------------------------------------


def test_latest_with_only_out_of_hours_returns_404():
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)
        _put_out_of_hours()  # no In_Hours record at all

        result = handler_latest(_make_event(with_range=False), MagicMock())

    assert result["statusCode"] == 404
    body = json.loads(result["body"])
    assert body["error"] == "NOT_FOUND"


def test_list_with_only_out_of_hours_returns_empty():
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)
        _put_out_of_hours()

        result = handler_list(_make_event(), MagicMock())

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["images"] == []


# ---------------------------------------------------------------------------
# Req 11.4 / 11.5 — weather populated vs null
# ---------------------------------------------------------------------------


def test_latest_includes_weather_when_present():
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)
        _put_in_hours(weather=_WEATHER_MAP)

        result = handler_latest(_make_event(), MagicMock())

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["weather"] is not None
    assert body["weather"]["condition"] == "Rain"
    assert body["weather"]["temp_c"] == 14.2


def test_latest_weather_absent_when_record_has_none():
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)
        _put_in_hours(weather=None)

        result = handler_latest(_make_event(), MagicMock())

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    # Single-camera latest omits weather entirely when the record has none.
    assert "weather" not in body


def test_list_weather_populated_and_null_variants():
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)
        # Two In_Hours records: one with weather, one without.
        _put_in_hours(weather=_WEATHER_MAP)
        data.put_img_record(
            tenant_id=_TENANT_ID,
            site_id=_SITE_ID,
            camera_id=_CAMERA_ID,
            snapshot_ts="2026-01-01T10:00:00Z",
            s3_key=f"{_TENANT_ID}/{_SITE_ID}/{_CAMERA_ID}/2026-01-01T10:00:00Z.jpg",
            sha256_hex="0" * 64,
            size_bytes=1024,
            weather=None,
            retention_class="In_Hours",
        )

        result = handler_list(_make_event(), MagicMock())

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    by_ts = {img["timestamp"]: img for img in body["images"]}
    # Record with weather has a populated weather field.
    assert by_ts[_TS_IN]["weather"]["condition"] == "Rain"
    # Record without weather omits the weather field (list builder only adds it
    # when present on the record).
    assert "weather" not in by_ts["2026-01-01T10:00:00Z"]


# ---------------------------------------------------------------------------
# Req 11.6 — presigned URL expiry of 300 seconds
# ---------------------------------------------------------------------------


def test_latest_presign_expiry_is_300_seconds():
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)
        _put_in_hours()

        result = handler_latest(_make_event(), MagicMock())

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["expires_in"] == 300
    _assert_url_expiry(body["presigned_url"], 300)


def test_list_presign_expiry_is_300_seconds():
    _reset_caches()
    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)
        _put_in_hours()

        result = handler_list(_make_event(), MagicMock())

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert len(body["images"]) == 1
    entry = body["images"][0]
    assert entry["expires_in"] == 300
    _assert_url_expiry(entry["presigned_url"], 300)


def _assert_url_expiry(url: str, expected: int) -> None:
    """Assert the presigned URL carries the expected X-Amz-Expires value."""
    qs = parse_qs(urlparse(url).query)
    assert qs.get("X-Amz-Expires") == [str(expected)]
