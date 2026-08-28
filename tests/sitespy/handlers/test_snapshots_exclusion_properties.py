"""Property test for default snapshot views excluding Out_Of_Hours records.

# Feature: working-hours-retention, Property 21: Default views exclude Out_Of_Hours

Property 21: Default views exclude Out_Of_Hours
Validates: Requirements 11.1, 11.2

For any camera whose stored records mix In_Hours (IMG#) and Out_Of_Hours
(OOH_IMG#) snapshots:
  - GET /v1/snapshots (list) returns only In_Hours records, and
  - GET /v1/snapshots/latest returns the most recent In_Hours snapshot (by
    capture time),
never selecting an Out_Of_Hours record.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

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
_BASE = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
# A [from, to] window that comfortably brackets every generated timestamp.
_FROM = "2025-01-01T00:00:00Z"
_TO = "2030-01-01T00:00:00Z"


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
    """Write the site record so the handler's get_site lookup succeeds."""
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


def _make_event(camera_id: str | None = _CAMERA_ID) -> dict[str, Any]:
    """Build an API Gateway proxy event for a tenant admin caller."""
    query_params: dict[str, str] = {"site_id": _SITE_ID}
    if camera_id is not None:
        query_params["camera_id"] = camera_id
    # Wide range for the list handler so every seeded record is in scope.
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


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Each record: (offset_seconds, is_in_hours). Offsets are unique so timestamps
# are distinct (records address by IMG#/OOH_IMG# SK on the timestamp).
_RECORDS_ST = st.lists(
    st.tuples(
        st.integers(min_value=0, max_value=10_000_000),
        st.booleans(),
    ),
    min_size=1,
    max_size=10,
    unique_by=lambda t: t[0],
)


# ---------------------------------------------------------------------------
# Property 21
# ---------------------------------------------------------------------------


@given(records=_RECORDS_ST)
@settings(max_examples=100, deadline=None)
def test_property_21_default_views_exclude_out_of_hours(records):
    """Property 21: list returns only In_Hours; latest is the newest In_Hours."""
    # At least one In_Hours record so the latest endpoint has something to return.
    assume(any(is_in for _, is_in in records))

    get_settings.cache_clear()
    _s3_client.cache_clear()
    _dynamodb_client.cache_clear()

    with mock_aws():
        _, ddb = _setup_aws()
        _seed_site(ddb)

        in_hours_ts: set[str] = set()
        out_of_hours_ts: set[str] = set()

        for offset, is_in_hours in records:
            ts = _fmt(offset)
            if is_in_hours:
                in_hours_ts.add(ts)
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
            else:
                out_of_hours_ts.add(ts)
                data.put_out_of_hours_img_record(
                    tenant_id=_TENANT_ID,
                    site_id=_SITE_ID,
                    camera_id=_CAMERA_ID,
                    snapshot_ts=ts,
                    s3_key=(
                        f"security/{_TENANT_ID}/{_SITE_ID}/{_CAMERA_ID}/{ts}.jpg"
                    ),
                    sha256_hex="0" * 64,
                    size_bytes=1024,
                    ttl=9999999999,
                )

        # --- list: only In_Hours records ---
        list_result = handler_list(_make_event(), MagicMock())
        assert list_result["statusCode"] == 200
        body = json.loads(list_result["body"])
        returned_ts = {img["timestamp"] for img in body["images"]}
        assert returned_ts == in_hours_ts
        assert returned_ts.isdisjoint(out_of_hours_ts)

        # --- latest: the most recent In_Hours snapshot, never Out_Of_Hours ---
        latest_result = handler_latest(_make_event(), MagicMock())
        assert latest_result["statusCode"] == 200
        latest_body = json.loads(latest_result["body"])
        expected_latest = max(in_hours_ts)
        assert latest_body["timestamp"] == expected_latest
        assert latest_body["timestamp"] not in out_of_hours_ts
