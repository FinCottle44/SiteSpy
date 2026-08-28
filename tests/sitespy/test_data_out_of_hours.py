"""Unit tests for out-of-hours (OOH_IMG#) and working-hours data additions.

Covers the data-layer additions for the working-hours-retention feature:

- OOH_IMG# records are excluded from the existing IMG# list/latest queries
  (Req 11.1/11.2).
- ``list_out_of_hours_img_records`` returns only OOH_IMG# items within the
  inclusive range, newest capture time first (Req 8.1).
- ``promote_out_of_hours_record`` sets ``promoted``/``promoted_at``/``s3_key``
  and removes ``ttl`` (Req 9.1/9.2).
- ``get_latest_any_img_record`` returns the newer of the two retention classes
  (Req 4.2).
- ``update_site`` removes ``ingest_hours`` when writing ``working_hours``
  (Req 2.4); ``working_hours=None`` removes the attribute.
- ``resolve_working_hours_attr`` resolves working hours for GET responses,
  including legacy derivation.

Requirements validated: 2.4, 4.2, 8.1, 9.1, 9.2, 11.1, 11.2
"""

from __future__ import annotations

import os

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from sitespy.data import (
    _dynamodb_client,
    build_out_of_hours_img_sk,
    get_latest_any_img_record,
    get_latest_img_record,
    get_out_of_hours_img_record,
    list_img_records,
    list_out_of_hours_img_records,
    promote_out_of_hours_record,
    put_img_record,
    put_out_of_hours_img_record,
    resolve_working_hours_attr,
    update_site,
)


def _create_table(client):
    """Helper to create the test DynamoDB table (PK/SK + GSI1)."""
    client.create_table(
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


@pytest.fixture(autouse=True)
def _env_and_cache():
    """Set env vars and clear the _dynamodb_client lru_cache."""
    os.environ.setdefault("DATA_TABLE", "test-data-table")
    os.environ.setdefault("AWS_REGION", "eu-west-2")
    os.environ.setdefault("SNAPSHOTS_BUCKET", "test-bucket")
    _dynamodb_client.cache_clear()
    yield
    _dynamodb_client.cache_clear()


# ---------------------------------------------------------------------------
# build_out_of_hours_img_sk
# ---------------------------------------------------------------------------


def test_build_out_of_hours_img_sk_output_format():
    """build_out_of_hours_img_sk returns OOH_IMG#<site>#<cam>#<ts>."""
    result = build_out_of_hours_img_sk("site_01", "cam_01", "2025-06-15T22:00:00Z")
    assert result == "OOH_IMG#site_01#cam_01#2025-06-15T22:00:00Z"


# ---------------------------------------------------------------------------
# OOH_IMG# excluded from existing IMG# list/latest queries (Req 11.1/11.2)
# ---------------------------------------------------------------------------


@mock_aws
def test_get_latest_img_record_excludes_ooh_records():
    """get_latest_img_record ignores OOH_IMG# records even when newer."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    # An In_Hours record.
    put_img_record(
        tenant_id="acme",
        site_id="site_01",
        camera_id="cam_01",
        snapshot_ts="2025-06-15T09:00:00Z",
        s3_key="acme/site_01/cam_01/2025/06/15/2025-06-15T09:00:00Z.jpg",
        sha256_hex="abc",
        size_bytes=100,
    )
    # A NEWER Out_Of_Hours record — must not be selected.
    put_out_of_hours_img_record(
        tenant_id="acme",
        site_id="site_01",
        camera_id="cam_01",
        snapshot_ts="2025-06-15T23:00:00Z",
        s3_key="security/acme/site_01/cam_01/2025/06/15/2025-06-15T23:00:00Z.jpg",
        sha256_hex="def",
        size_bytes=200,
        ttl=1750000000,
    )

    latest = get_latest_img_record("acme", "site_01", "cam_01")
    assert latest is not None
    assert latest["SK"]["S"] == "IMG#site_01#cam_01#2025-06-15T09:00:00Z"


@mock_aws
def test_get_latest_img_record_returns_none_when_only_ooh_records():
    """get_latest_img_record returns None when only OOH_IMG# records exist."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    put_out_of_hours_img_record(
        tenant_id="acme",
        site_id="site_01",
        camera_id="cam_01",
        snapshot_ts="2025-06-15T23:00:00Z",
        s3_key="security/acme/site_01/cam_01/2025/06/15/2025-06-15T23:00:00Z.jpg",
        sha256_hex="def",
        size_bytes=200,
        ttl=1750000000,
    )

    assert get_latest_img_record("acme", "site_01", "cam_01") is None


@mock_aws
def test_list_img_records_excludes_ooh_records():
    """list_img_records returns only IMG# records, not OOH_IMG# records."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    put_img_record(
        tenant_id="acme",
        site_id="site_01",
        camera_id="cam_01",
        snapshot_ts="2025-06-15T09:00:00Z",
        s3_key="acme/site_01/cam_01/2025/06/15/2025-06-15T09:00:00Z.jpg",
        sha256_hex="abc",
        size_bytes=100,
    )
    put_out_of_hours_img_record(
        tenant_id="acme",
        site_id="site_01",
        camera_id="cam_01",
        snapshot_ts="2025-06-15T23:00:00Z",
        s3_key="security/acme/site_01/cam_01/2025/06/15/2025-06-15T23:00:00Z.jpg",
        sha256_hex="def",
        size_bytes=200,
        ttl=1750000000,
    )

    items, _ = list_img_records(
        "acme",
        "site_01",
        "cam_01",
        from_ts="2025-06-15T00:00:00Z",
        to_ts="2025-06-15T23:59:59Z",
    )

    assert len(items) == 1
    assert items[0]["SK"]["S"] == "IMG#site_01#cam_01#2025-06-15T09:00:00Z"


@mock_aws
def test_list_img_records_order_respects_ascending_flag():
    """ascending=True returns oldest-first; default (False) returns newest-first."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    for ts in (
        "2025-06-15T09:00:00Z",
        "2025-06-15T12:00:00Z",
        "2025-06-15T17:00:00Z",
    ):
        put_img_record(
            tenant_id="acme",
            site_id="site_01",
            camera_id="cam_01",
            snapshot_ts=ts,
            s3_key=f"acme/site_01/cam_01/{ts}.jpg",
            sha256_hex="abc",
            size_bytes=100,
        )

    from_ts, to_ts = "2025-06-15T00:00:00Z", "2025-06-15T23:59:59Z"

    desc_items, _ = list_img_records(
        "acme", "site_01", "cam_01", from_ts=from_ts, to_ts=to_ts
    )
    asc_items, _ = list_img_records(
        "acme", "site_01", "cam_01", from_ts=from_ts, to_ts=to_ts, ascending=True
    )

    desc_ts = [i["ingested_at"]["S"] for i in desc_items]
    asc_ts = [i["ingested_at"]["S"] for i in asc_items]

    # Default is newest-first; ascending reverses to oldest-first.
    assert desc_ts == ["2025-06-15T17:00:00Z", "2025-06-15T12:00:00Z", "2025-06-15T09:00:00Z"]
    assert asc_ts == ["2025-06-15T09:00:00Z", "2025-06-15T12:00:00Z", "2025-06-15T17:00:00Z"]
    # With limit=1 the two directions return opposite ends of the range.
    first_asc, _ = list_img_records(
        "acme", "site_01", "cam_01", from_ts=from_ts, to_ts=to_ts, limit=1, ascending=True
    )
    first_desc, _ = list_img_records(
        "acme", "site_01", "cam_01", from_ts=from_ts, to_ts=to_ts, limit=1, ascending=False
    )
    assert first_asc[0]["ingested_at"]["S"] == "2025-06-15T09:00:00Z"
    assert first_desc[0]["ingested_at"]["S"] == "2025-06-15T17:00:00Z"


# ---------------------------------------------------------------------------
# list_out_of_hours_img_records — only OOH records, inclusive range, newest first
# ---------------------------------------------------------------------------


@mock_aws
def test_list_out_of_hours_img_records_returns_only_ooh_newest_first():
    """list_out_of_hours_img_records returns only OOH_IMG# items in range,
    ordered newest capture time first."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    # In_Hours record — must be excluded from OOH review.
    put_img_record(
        tenant_id="acme",
        site_id="site_01",
        camera_id="cam_01",
        snapshot_ts="2025-06-15T12:00:00Z",
        s3_key="acme/site_01/cam_01/2025/06/15/2025-06-15T12:00:00Z.jpg",
        sha256_hex="in",
        size_bytes=1,
    )

    for ts in (
        "2025-06-15T22:00:00Z",
        "2025-06-15T23:00:00Z",
        "2025-06-16T00:00:00Z",
    ):
        put_out_of_hours_img_record(
            tenant_id="acme",
            site_id="site_01",
            camera_id="cam_01",
            snapshot_ts=ts,
            s3_key=f"security/acme/site_01/cam_01/{ts}.jpg",
            sha256_hex="x",
            size_bytes=1,
            ttl=1750000000,
        )

    items, last_key = list_out_of_hours_img_records(
        "acme",
        "site_01",
        "cam_01",
        from_ts="2025-06-15T00:00:00Z",
        to_ts="2025-06-16T23:59:59Z",
    )

    # Only OOH records, all with the OOH_IMG# prefix.
    assert all(item["SK"]["S"].startswith("OOH_IMG#") for item in items)
    # Newest capture time first.
    assert [item["ingested_at"]["S"] for item in items] == [
        "2025-06-16T00:00:00Z",
        "2025-06-15T23:00:00Z",
        "2025-06-15T22:00:00Z",
    ]
    assert last_key is None


@mock_aws
def test_list_out_of_hours_img_records_range_is_inclusive():
    """Both range bounds are inclusive; out-of-range records are excluded."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    for ts in (
        "2025-06-14T23:59:59Z",  # before range
        "2025-06-15T00:00:00Z",  # lower bound (inclusive)
        "2025-06-15T23:00:00Z",  # within
        "2025-06-16T00:00:00Z",  # upper bound (inclusive)
        "2025-06-16T00:00:01Z",  # after range
    ):
        put_out_of_hours_img_record(
            tenant_id="acme",
            site_id="site_01",
            camera_id="cam_01",
            snapshot_ts=ts,
            s3_key=f"security/acme/site_01/cam_01/{ts}.jpg",
            sha256_hex="x",
            size_bytes=1,
            ttl=1750000000,
        )

    items, _ = list_out_of_hours_img_records(
        "acme",
        "site_01",
        "cam_01",
        from_ts="2025-06-15T00:00:00Z",
        to_ts="2025-06-16T00:00:00Z",
    )

    returned = {item["ingested_at"]["S"] for item in items}
    assert returned == {
        "2025-06-15T00:00:00Z",
        "2025-06-15T23:00:00Z",
        "2025-06-16T00:00:00Z",
    }


@mock_aws
def test_list_out_of_hours_img_records_excludes_other_cameras():
    """list_out_of_hours_img_records only returns records for the given camera."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    put_out_of_hours_img_record(
        tenant_id="acme",
        site_id="site_01",
        camera_id="cam_02",
        snapshot_ts="2025-06-15T23:00:00Z",
        s3_key="security/acme/site_01/cam_02/2025-06-15T23:00:00Z.jpg",
        sha256_hex="x",
        size_bytes=1,
        ttl=1750000000,
    )

    items, _ = list_out_of_hours_img_records(
        "acme",
        "site_01",
        "cam_01",
        from_ts="2025-06-15T00:00:00Z",
        to_ts="2025-06-16T00:00:00Z",
    )
    assert items == []


# ---------------------------------------------------------------------------
# promote_out_of_hours_record — sets promoted/promoted_at/s3_key, removes ttl
# ---------------------------------------------------------------------------


@mock_aws
def test_promote_out_of_hours_record_sets_fields_and_removes_ttl():
    """promote_out_of_hours_record marks promoted, updates s3_key, and removes ttl."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    put_out_of_hours_img_record(
        tenant_id="acme",
        site_id="site_01",
        camera_id="cam_01",
        snapshot_ts="2025-06-15T23:00:00Z",
        s3_key="security/acme/site_01/cam_01/2025/06/15/2025-06-15T23:00:00Z.jpg",
        sha256_hex="def",
        size_bytes=200,
        ttl=1750000000,
    )

    ooh_sk = build_out_of_hours_img_sk("site_01", "cam_01", "2025-06-15T23:00:00Z")
    new_key = "preserved/acme/site_01/cam_01/2025/06/15/2025-06-15T23:00:00Z.jpg"

    promote_out_of_hours_record(
        tenant_id="acme",
        ooh_sk=ooh_sk,
        new_s3_key=new_key,
        promoted_at="2025-06-16T08:00:00Z",
    )

    record = get_out_of_hours_img_record("acme", "site_01", "cam_01", "2025-06-15T23:00:00Z")
    assert record is not None
    assert record["promoted"]["BOOL"] is True
    assert record["promoted_at"]["S"] == "2025-06-16T08:00:00Z"
    assert record["s3_key"]["S"] == new_key
    assert "ttl" not in record


@mock_aws
def test_promote_out_of_hours_record_missing_item_raises_conditional_check():
    """promote_out_of_hours_record raises when the target record does not exist."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    ooh_sk = build_out_of_hours_img_sk("site_01", "cam_01", "2025-06-15T23:00:00Z")

    with pytest.raises(ClientError) as exc_info:
        promote_out_of_hours_record(
            tenant_id="acme",
            ooh_sk=ooh_sk,
            new_s3_key="preserved/whatever.jpg",
            promoted_at="2025-06-16T08:00:00Z",
        )

    assert exc_info.value.response["Error"]["Code"] == "ConditionalCheckFailedException"


# ---------------------------------------------------------------------------
# get_latest_any_img_record — newer of the two classes (Req 4.2)
# ---------------------------------------------------------------------------


@mock_aws
def test_get_latest_any_img_record_returns_none_when_empty():
    """get_latest_any_img_record returns None when no records exist."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    assert get_latest_any_img_record("acme", "site_01", "cam_01") is None


@mock_aws
def test_get_latest_any_img_record_returns_newer_ooh():
    """When the OOH record is newer, get_latest_any_img_record returns it."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    put_img_record(
        tenant_id="acme",
        site_id="site_01",
        camera_id="cam_01",
        snapshot_ts="2025-06-15T09:00:00Z",
        s3_key="acme/site_01/cam_01/2025/06/15/2025-06-15T09:00:00Z.jpg",
        sha256_hex="abc",
        size_bytes=100,
    )
    put_out_of_hours_img_record(
        tenant_id="acme",
        site_id="site_01",
        camera_id="cam_01",
        snapshot_ts="2025-06-15T23:00:00Z",
        s3_key="security/acme/site_01/cam_01/2025-06-15T23:00:00Z.jpg",
        sha256_hex="def",
        size_bytes=200,
        ttl=1750000000,
    )

    latest = get_latest_any_img_record("acme", "site_01", "cam_01")
    assert latest is not None
    assert latest["ingested_at"]["S"] == "2025-06-15T23:00:00Z"
    assert latest["SK"]["S"].startswith("OOH_IMG#")


@mock_aws
def test_get_latest_any_img_record_returns_newer_in_hours():
    """When the In_Hours record is newer, get_latest_any_img_record returns it."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    put_out_of_hours_img_record(
        tenant_id="acme",
        site_id="site_01",
        camera_id="cam_01",
        snapshot_ts="2025-06-15T05:00:00Z",
        s3_key="security/acme/site_01/cam_01/2025-06-15T05:00:00Z.jpg",
        sha256_hex="def",
        size_bytes=200,
        ttl=1750000000,
    )
    put_img_record(
        tenant_id="acme",
        site_id="site_01",
        camera_id="cam_01",
        snapshot_ts="2025-06-15T09:00:00Z",
        s3_key="acme/site_01/cam_01/2025/06/15/2025-06-15T09:00:00Z.jpg",
        sha256_hex="abc",
        size_bytes=100,
    )

    latest = get_latest_any_img_record("acme", "site_01", "cam_01")
    assert latest is not None
    assert latest["ingested_at"]["S"] == "2025-06-15T09:00:00Z"
    assert latest["SK"]["S"].startswith("IMG#")


@mock_aws
def test_get_latest_any_img_record_single_class_only():
    """get_latest_any_img_record works when only one class of record exists."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    put_out_of_hours_img_record(
        tenant_id="acme",
        site_id="site_01",
        camera_id="cam_01",
        snapshot_ts="2025-06-15T23:00:00Z",
        s3_key="security/acme/site_01/cam_01/2025-06-15T23:00:00Z.jpg",
        sha256_hex="def",
        size_bytes=200,
        ttl=1750000000,
    )

    latest = get_latest_any_img_record("acme", "site_01", "cam_01")
    assert latest is not None
    assert latest["SK"]["S"].startswith("OOH_IMG#")


# ---------------------------------------------------------------------------
# update_site — working_hours SET removes legacy ingest_hours (Req 2.4)
# ---------------------------------------------------------------------------


def _put_site_with_ingest_hours(client, start="07:00", end="18:00"):
    client.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": "TENANT#acme"},
            "SK": {"S": "SITE#site_01"},
            "site_name": {"S": "Acme Tower"},
            "timezone": {"S": "Europe/London"},
            "ingest_hours": {
                "M": {"start": {"S": start}, "end": {"S": end}},
            },
        },
    )


@mock_aws
def test_update_site_working_hours_removes_legacy_ingest_hours():
    """Writing working_hours removes any legacy ingest_hours in the same write."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)
    _put_site_with_ingest_hours(client)

    update_site(
        tenant_id="acme",
        site_id="site_01",
        updates={
            "working_hours": {
                "days": ["mon", "tue", "wed", "thu", "fri"],
                "start": "09:00",
                "end": "17:00",
            }
        },
    )

    response = client.get_item(
        TableName="test-data-table",
        Key={"PK": {"S": "TENANT#acme"}, "SK": {"S": "SITE#site_01"}},
    )
    item = response["Item"]
    assert "ingest_hours" not in item
    wh = item["working_hours"]["M"]
    assert wh["start"]["S"] == "09:00"
    assert wh["end"]["S"] == "17:00"
    assert [d["S"] for d in wh["days"]["L"]] == ["mon", "tue", "wed", "thu", "fri"]


@mock_aws
def test_update_site_working_hours_null_removes_attribute():
    """working_hours=None removes the working_hours attribute (Req 1.2)."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)
    client.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": "TENANT#acme"},
            "SK": {"S": "SITE#site_01"},
            "site_name": {"S": "Acme Tower"},
            "working_hours": {
                "M": {
                    "start": {"S": "09:00"},
                    "end": {"S": "17:00"},
                    "days": {"L": [{"S": "mon"}]},
                }
            },
        },
    )

    update_site(
        tenant_id="acme",
        site_id="site_01",
        updates={"working_hours": None},
    )

    response = client.get_item(
        TableName="test-data-table",
        Key={"PK": {"S": "TENANT#acme"}, "SK": {"S": "SITE#site_01"}},
    )
    item = response["Item"]
    assert "working_hours" not in item


@mock_aws
def test_update_site_working_hours_without_days_omits_days_attr():
    """working_hours without days is written without a days attribute."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)
    _put_site_with_ingest_hours(client)

    update_site(
        tenant_id="acme",
        site_id="site_01",
        updates={"working_hours": {"start": "09:00", "end": "17:00"}},
    )

    response = client.get_item(
        TableName="test-data-table",
        Key={"PK": {"S": "TENANT#acme"}, "SK": {"S": "SITE#site_01"}},
    )
    wh = response["Item"]["working_hours"]["M"]
    assert "days" not in wh
    assert wh["start"]["S"] == "09:00"
    assert wh["end"]["S"] == "17:00"


# ---------------------------------------------------------------------------
# resolve_working_hours_attr — GET response resolution incl. legacy derivation
# ---------------------------------------------------------------------------


def test_resolve_working_hours_attr_from_working_hours():
    """resolve_working_hours_attr returns the explicit working_hours config."""
    site_item = {
        "working_hours": {
            "M": {
                "start": {"S": "09:00"},
                "end": {"S": "17:00"},
                "days": {"L": [{"S": "mon"}, {"S": "wed"}, {"S": "fri"}]},
            }
        }
    }
    result = resolve_working_hours_attr(site_item)
    assert result == {
        "days": ["mon", "wed", "fri"],
        "start": "09:00",
        "end": "17:00",
    }


def test_resolve_working_hours_attr_legacy_derivation_all_seven_days():
    """A valid legacy ingest_hours derives to all seven days (Req 2.2)."""
    site_item = {
        "ingest_hours": {
            "M": {"start": {"S": "07:00"}, "end": {"S": "18:00"}},
        }
    }
    result = resolve_working_hours_attr(site_item)
    assert result == {
        "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        "start": "07:00",
        "end": "18:00",
    }


def test_resolve_working_hours_attr_none_when_absent():
    """resolve_working_hours_attr returns None when no usable config exists."""
    assert resolve_working_hours_attr({}) is None


def test_resolve_working_hours_attr_none_for_degenerate_legacy():
    """Legacy ingest_hours with start == end is not usable (None)."""
    site_item = {
        "ingest_hours": {
            "M": {"start": {"S": "09:00"}, "end": {"S": "09:00"}},
        }
    }
    assert resolve_working_hours_attr(site_item) is None
