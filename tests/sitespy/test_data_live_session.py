"""Unit tests for SESSION# data functions (live-view-session feature).

Requirements validated: 2.1, 2.6, 2.13, 4.1
"""

import os

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from sitespy.data import (
    _dynamodb_client,
    build_session_sk,
    build_tenant_pk,
    delete_live_session,
    get_live_session,
    put_live_session,
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
# build_session_sk tests
# Validates: Requirement 2.1, 2.13
# ---------------------------------------------------------------------------


def test_build_session_sk_output_format():
    """build_session_sk returns SESSION#<site_id>#<camera_id>."""
    result = build_session_sk("site_01", "cam_01")
    assert result == "SESSION#site_01#cam_01"


def test_build_session_sk_with_underscores():
    """build_session_sk preserves underscores and special chars in ids."""
    result = build_session_sk("site_a_b", "cam_x_y")
    assert result == "SESSION#site_a_b#cam_x_y"


# ---------------------------------------------------------------------------
# get_live_session tests
# Validates: Requirement 2.6, 4.1
# ---------------------------------------------------------------------------


@mock_aws
def test_get_live_session_returns_none_when_absent():
    """get_live_session returns None when no SESSION# record exists."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    result = get_live_session("acme", "site_01", "cam_01")
    assert result is None


@mock_aws
def test_get_live_session_returns_item_when_present():
    """get_live_session returns the item when a SESSION# record exists."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    # Seed the SESSION# record directly
    client.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": "TENANT#acme"},
            "SK": {"S": "SESSION#site_01#cam_01"},
            "session_id": {"S": "550e8400-e29b-41d4-a716-446655440000"},
            "expires_at": {"S": "2025-06-15T14:10:00Z"},
            "ttl": {"N": "1750006200"},
            "created_by": {"S": "user-sub-123"},
            "created_at": {"S": "2025-06-15T14:00:00Z"},
        },
    )

    item = get_live_session("acme", "site_01", "cam_01")
    assert item is not None
    assert item["PK"]["S"] == "TENANT#acme"
    assert item["SK"]["S"] == "SESSION#site_01#cam_01"
    assert item["session_id"]["S"] == "550e8400-e29b-41d4-a716-446655440000"
    assert item["expires_at"]["S"] == "2025-06-15T14:10:00Z"
    assert item["ttl"]["N"] == "1750006200"
    assert item["created_by"]["S"] == "user-sub-123"
    assert item["created_at"]["S"] == "2025-06-15T14:00:00Z"


# ---------------------------------------------------------------------------
# put_live_session tests
# Validates: Requirement 2.1, 2.13
# ---------------------------------------------------------------------------


@mock_aws
def test_put_live_session_writes_correct_item():
    """put_live_session writes a SESSION# record with all expected attributes."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    put_live_session(
        tenant_id="acme",
        site_id="site_01",
        camera_id="cam_01",
        session_id="550e8400-e29b-41d4-a716-446655440000",
        expires_at="2025-06-15T14:10:00Z",
        ttl=1750006200,
        created_by="user-sub-123",
        created_at="2025-06-15T14:00:00Z",
        now_ts="2025-06-15T14:00:00Z",
    )

    # Read back from DynamoDB directly
    response = client.get_item(
        TableName="test-data-table",
        Key={
            "PK": {"S": "TENANT#acme"},
            "SK": {"S": "SESSION#site_01#cam_01"},
        },
    )
    item = response["Item"]
    assert item["PK"]["S"] == "TENANT#acme"
    assert item["SK"]["S"] == "SESSION#site_01#cam_01"
    assert item["session_id"]["S"] == "550e8400-e29b-41d4-a716-446655440000"
    assert item["expires_at"]["S"] == "2025-06-15T14:10:00Z"
    assert item["ttl"]["N"] == "1750006200"
    assert item["created_by"]["S"] == "user-sub-123"
    assert item["created_at"]["S"] == "2025-06-15T14:00:00Z"


@mock_aws
def test_put_live_session_raises_conditional_check_on_duplicate():
    """put_live_session raises ConditionalCheckFailedException on duplicate."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    # First write succeeds
    put_live_session(
        tenant_id="acme",
        site_id="site_01",
        camera_id="cam_01",
        session_id="first-session-id",
        expires_at="2025-06-15T14:10:00Z",
        ttl=1750006200,
        created_by="user-sub-001",
        created_at="2025-06-15T14:00:00Z",
        now_ts="2025-06-15T14:00:00Z",
    )

    # Second write for the same camera should fail — the first session is still
    # active (now is before its expires_at of 14:10:00Z).
    with pytest.raises(ClientError) as exc_info:
        put_live_session(
            tenant_id="acme",
            site_id="site_01",
            camera_id="cam_01",
            session_id="second-session-id",
            expires_at="2025-06-15T14:20:00Z",
            ttl=1750006800,
            created_by="user-sub-002",
            created_at="2025-06-15T14:10:00Z",
            now_ts="2025-06-15T14:05:00Z",
        )

    assert exc_info.value.response["Error"]["Code"] == "ConditionalCheckFailedException"


@mock_aws
def test_put_live_session_overwrites_expired_session():
    """put_live_session overwrites a stale, expired SESSION# record.

    DynamoDB TTL deletion is lazy, so an expired session can physically remain
    in the table. A new POST must be able to replace it rather than being
    blocked with a false 'session already active' conflict.
    """
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    # First session — already expired relative to the second write's "now".
    put_live_session(
        tenant_id="acme",
        site_id="site_01",
        camera_id="cam_01",
        session_id="expired-session-id",
        expires_at="2025-06-15T14:10:00Z",
        ttl=1750006200,
        created_by="user-sub-001",
        created_at="2025-06-15T14:00:00Z",
        now_ts="2025-06-15T14:00:00Z",
    )

    # Second write — now is past the first session's expiry, so it should succeed.
    put_live_session(
        tenant_id="acme",
        site_id="site_01",
        camera_id="cam_01",
        session_id="fresh-session-id",
        expires_at="2025-06-15T14:30:00Z",
        ttl=1750007400,
        created_by="user-sub-002",
        created_at="2025-06-15T14:20:00Z",
        now_ts="2025-06-15T14:20:00Z",
    )

    response = client.get_item(
        TableName="test-data-table",
        Key={
            "PK": {"S": "TENANT#acme"},
            "SK": {"S": "SESSION#site_01#cam_01"},
        },
    )
    item = response["Item"]
    assert item["session_id"]["S"] == "fresh-session-id"
    assert item["expires_at"]["S"] == "2025-06-15T14:30:00Z"


# ---------------------------------------------------------------------------
# delete_live_session tests
# Validates: Requirement 4.1
# ---------------------------------------------------------------------------


@mock_aws
def test_delete_live_session_removes_record():
    """delete_live_session removes the SESSION# record from DynamoDB."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    # Seed a SESSION# record
    client.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": "TENANT#acme"},
            "SK": {"S": "SESSION#site_01#cam_01"},
            "session_id": {"S": "to-delete-session"},
            "expires_at": {"S": "2025-06-15T14:10:00Z"},
            "ttl": {"N": "1750006200"},
            "created_by": {"S": "user-sub-123"},
            "created_at": {"S": "2025-06-15T14:00:00Z"},
        },
    )

    # Delete
    delete_live_session("acme", "site_01", "cam_01")

    # Verify the item is gone
    response = client.get_item(
        TableName="test-data-table",
        Key={
            "PK": {"S": "TENANT#acme"},
            "SK": {"S": "SESSION#site_01#cam_01"},
        },
    )
    assert "Item" not in response


@mock_aws
def test_delete_live_session_calls_with_correct_pk_sk():
    """delete_live_session uses the correct PK=TENANT#<id> and SK=SESSION#<site>#<cam>."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    # Seed two SESSION# records for different cameras
    client.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": "TENANT#acme"},
            "SK": {"S": "SESSION#site_01#cam_01"},
            "session_id": {"S": "session-a"},
            "expires_at": {"S": "2025-06-15T14:10:00Z"},
            "ttl": {"N": "1750006200"},
            "created_by": {"S": "user-sub-001"},
            "created_at": {"S": "2025-06-15T14:00:00Z"},
        },
    )
    client.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": "TENANT#acme"},
            "SK": {"S": "SESSION#site_01#cam_02"},
            "session_id": {"S": "session-b"},
            "expires_at": {"S": "2025-06-15T14:10:00Z"},
            "ttl": {"N": "1750006200"},
            "created_by": {"S": "user-sub-002"},
            "created_at": {"S": "2025-06-15T14:00:00Z"},
        },
    )

    # Delete only cam_01's session
    delete_live_session("acme", "site_01", "cam_01")

    # cam_01's session is gone
    response = client.get_item(
        TableName="test-data-table",
        Key={
            "PK": {"S": "TENANT#acme"},
            "SK": {"S": "SESSION#site_01#cam_01"},
        },
    )
    assert "Item" not in response

    # cam_02's session remains intact
    response = client.get_item(
        TableName="test-data-table",
        Key={
            "PK": {"S": "TENANT#acme"},
            "SK": {"S": "SESSION#site_01#cam_02"},
        },
    )
    assert "Item" in response


@mock_aws
def test_delete_live_session_nonexistent_does_not_raise():
    """delete_live_session does not raise when the item doesn't exist (idempotent)."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    # Should not raise
    delete_live_session("acme", "site_01", "cam_nonexistent")
