"""Unit tests for LIVE_IMG# data functions in data.py.

Requirements validated: 3.1, 5.6, 6.4
"""

from __future__ import annotations

import os

import boto3
import pytest
from moto import mock_aws

from sitespy.data import (
    _dynamodb_client,
    build_live_img_sk,
    get_latest_live_img_record,
    put_live_img_record,
)


def _create_table(client):
    """Helper to create the test DynamoDB table."""
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
# build_live_img_sk tests
# ---------------------------------------------------------------------------


def test_build_live_img_sk_output_format():
    """build_live_img_sk returns LIVE_IMG#<site_id>#<camera_id>#<snapshot_ts>."""
    result = build_live_img_sk("site_01", "cam_01", "2025-06-15T14:00:00Z")
    assert result == "LIVE_IMG#site_01#cam_01#2025-06-15T14:00:00Z"


def test_build_live_img_sk_preserves_special_characters():
    """build_live_img_sk correctly concatenates IDs with hashes and timestamps."""
    result = build_live_img_sk("site_a_b", "cam_x_y", "2025-01-01T00:00:00Z")
    assert result == "LIVE_IMG#site_a_b#cam_x_y#2025-01-01T00:00:00Z"


# ---------------------------------------------------------------------------
# get_latest_live_img_record tests
# ---------------------------------------------------------------------------


@mock_aws
def test_get_latest_live_img_record_returns_none_when_no_records():
    """get_latest_live_img_record returns None when no LIVE_IMG# records exist."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    result = get_latest_live_img_record("acme", "site_01", "cam_01")
    assert result is None


@mock_aws
def test_get_latest_live_img_record_returns_most_recent():
    """get_latest_live_img_record queries with ScanIndexForward=False, Limit=1
    and returns the most recent record (lexicographically last SK)."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    # Insert two LIVE_IMG# records with different timestamps
    client.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": "TENANT#acme"},
            "SK": {"S": "LIVE_IMG#site_01#cam_01#2025-06-15T14:00:00Z"},
            "s3_key": {"S": "live/acme/site_01/cam_01/2025-06-15T14:00:00Z.jpg"},
            "sha256": {"S": "abc123"},
            "size_bytes": {"N": "1024"},
            "captured_at": {"S": "2025-06-15T14:00:00Z"},
            "ttl": {"N": "1750003600"},
        },
    )
    client.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": "TENANT#acme"},
            "SK": {"S": "LIVE_IMG#site_01#cam_01#2025-06-15T14:01:00Z"},
            "s3_key": {"S": "live/acme/site_01/cam_01/2025-06-15T14:01:00Z.jpg"},
            "sha256": {"S": "def456"},
            "size_bytes": {"N": "2048"},
            "captured_at": {"S": "2025-06-15T14:01:00Z"},
            "ttl": {"N": "1750003660"},
        },
    )

    result = get_latest_live_img_record("acme", "site_01", "cam_01")
    assert result is not None
    # Should return the later timestamp (descending sort)
    assert result["SK"]["S"] == "LIVE_IMG#site_01#cam_01#2025-06-15T14:01:00Z"
    assert result["captured_at"]["S"] == "2025-06-15T14:01:00Z"


@mock_aws
def test_get_latest_live_img_record_does_not_return_other_cameras():
    """get_latest_live_img_record only returns records for the specified camera."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    # Insert a record for cam_02 (different camera)
    client.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": "TENANT#acme"},
            "SK": {"S": "LIVE_IMG#site_01#cam_02#2025-06-15T14:05:00Z"},
            "s3_key": {"S": "live/acme/site_01/cam_02/2025-06-15T14:05:00Z.jpg"},
            "sha256": {"S": "other"},
            "size_bytes": {"N": "512"},
            "captured_at": {"S": "2025-06-15T14:05:00Z"},
            "ttl": {"N": "1750003900"},
        },
    )

    # Query for cam_01 should return None
    result = get_latest_live_img_record("acme", "site_01", "cam_01")
    assert result is None


# ---------------------------------------------------------------------------
# put_live_img_record tests
# ---------------------------------------------------------------------------


@mock_aws
def test_put_live_img_record_writes_correct_ttl():
    """put_live_img_record writes the TTL value as provided (captured_at + 3600)."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    # captured_at = 2025-06-15T14:00:00Z → epoch = 1750000800
    # Expected ttl = 1750000800 + 3600 = 1750004400
    captured_at_epoch = 1750000800
    expected_ttl = captured_at_epoch + 3600

    put_live_img_record(
        tenant_id="acme",
        site_id="site_01",
        camera_id="cam_01",
        snapshot_ts="2025-06-15T14:00:00Z",
        s3_key="live/acme/site_01/cam_01/2025-06-15T14:00:00Z.jpg",
        sha256_hex="e3b0c44298fc1c149afbf4c8996fb924",
        size_bytes=2048,
        ttl=expected_ttl,
    )

    # Read back directly from DynamoDB to verify ttl
    response = client.get_item(
        TableName="test-data-table",
        Key={
            "PK": {"S": "TENANT#acme"},
            "SK": {"S": "LIVE_IMG#site_01#cam_01#2025-06-15T14:00:00Z"},
        },
    )
    item = response["Item"]
    assert item["ttl"]["N"] == str(expected_ttl)
    assert item["captured_at"]["S"] == "2025-06-15T14:00:00Z"
    assert item["s3_key"]["S"] == "live/acme/site_01/cam_01/2025-06-15T14:00:00Z.jpg"
    assert item["sha256"]["S"] == "e3b0c44298fc1c149afbf4c8996fb924"
    assert item["size_bytes"]["N"] == "2048"


@mock_aws
def test_put_live_img_record_writes_all_attributes():
    """put_live_img_record writes the complete item with all expected attributes."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    put_live_img_record(
        tenant_id="tenant_x",
        site_id="site_a",
        camera_id="cam_b",
        snapshot_ts="2025-01-01T12:30:00Z",
        s3_key="live/tenant_x/site_a/cam_b/2025-01-01T12:30:00Z.jpg",
        sha256_hex="abcdef1234567890",
        size_bytes=4096,
        ttl=1735736600,
    )

    response = client.get_item(
        TableName="test-data-table",
        Key={
            "PK": {"S": "TENANT#tenant_x"},
            "SK": {"S": "LIVE_IMG#site_a#cam_b#2025-01-01T12:30:00Z"},
        },
    )
    item = response["Item"]
    assert item["PK"]["S"] == "TENANT#tenant_x"
    assert item["SK"]["S"] == "LIVE_IMG#site_a#cam_b#2025-01-01T12:30:00Z"
    assert item["s3_key"]["S"] == "live/tenant_x/site_a/cam_b/2025-01-01T12:30:00Z.jpg"
    assert item["sha256"]["S"] == "abcdef1234567890"
    assert item["size_bytes"]["N"] == "4096"
    assert item["captured_at"]["S"] == "2025-01-01T12:30:00Z"
    assert item["ttl"]["N"] == "1735736600"
