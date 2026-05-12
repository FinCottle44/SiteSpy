"""Unit tests for data.py key builders and DynamoDB operations.

Requirements validated: 1.1, 1.2, 2.3, 2.6, 2.8, 6.1
"""

import pytest

from sitespy.data import (
    build_camera_sk,
    build_img_sk,
    build_tenant_pk,
    build_tenant_sk,
)


def test_build_tenant_pk():
    assert build_tenant_pk("acme_corp") == "TENANT#acme_corp"


def test_build_tenant_sk():
    assert build_tenant_sk("acme_corp") == "TENANT#acme_corp"


def test_build_camera_sk():
    assert build_camera_sk("site_001", "cam_01") == "SITE#site_001#CAM#cam_01"


def test_build_img_sk():
    assert (
        build_img_sk("site_001", "cam_01", "2025-06-15T14:00:00Z")
        == "IMG#site_001#cam_01#2025-06-15T14:00:00Z"
    )


def test_build_tenant_pk_single_char():
    assert build_tenant_pk("a") == "TENANT#a"


def test_build_camera_sk_underscores():
    assert build_camera_sk("site_a_b", "cam_x_y") == "SITE#site_a_b#CAM#cam_x_y"


def test_build_img_sk_midnight():
    assert build_img_sk("s", "c", "2025-01-01T00:00:00Z") == "IMG#s#c#2025-01-01T00:00:00Z"


# ===========================================================================
# DynamoDB operation tests
# Requirements validated: 2.3, 5.6, 6.1, 6.2, 7.2
# ===========================================================================
import os

import boto3
from moto import mock_aws

from sitespy.data import (
    _dynamodb_client,
    get_camera,
    get_retention_years,
    get_users_for_tenant,
    put_img_record,
    put_user,
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
def reset_dynamodb_cache():
    """Clear the _dynamodb_client lru_cache before each test."""
    _dynamodb_client.cache_clear()
    yield
    _dynamodb_client.cache_clear()


# ---------------------------------------------------------------------------
# get_camera tests
# ---------------------------------------------------------------------------


@mock_aws
def test_get_camera_item_present():
    """get_camera returns the item when it exists."""
    os.environ.setdefault("DATA_TABLE", "test-data-table")
    os.environ.setdefault("AWS_REGION", "eu-west-2")

    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    client.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": "TENANT#acme"},
            "SK": {"S": "SITE#site_01#CAM#cam_01"},
            "ingest_username": {"S": "sitespy_cam_abc"},
            "ingest_password_hash": {"S": "$2b$12$fakehash"},
        },
    )

    item = get_camera("acme", "site_01", "cam_01")
    assert item is not None
    assert item["PK"]["S"] == "TENANT#acme"
    assert item["SK"]["S"] == "SITE#site_01#CAM#cam_01"
    assert item["ingest_username"]["S"] == "sitespy_cam_abc"


@mock_aws
def test_get_camera_item_absent():
    """get_camera returns None when the item does not exist."""
    os.environ.setdefault("DATA_TABLE", "test-data-table")
    os.environ.setdefault("AWS_REGION", "eu-west-2")

    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    item = get_camera("nonexistent", "site_01", "cam_01")
    assert item is None


# ---------------------------------------------------------------------------
# get_retention_years tests
# ---------------------------------------------------------------------------


@mock_aws
def test_get_retention_years_attribute_present():
    """get_retention_years returns the stored value when present."""
    os.environ.setdefault("DATA_TABLE", "test-data-table")
    os.environ.setdefault("AWS_REGION", "eu-west-2")

    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    client.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": "TENANT#acme"},
            "SK": {"S": "TENANT#acme"},
            "retention_years": {"N": "7"},
        },
    )

    result = get_retention_years("acme")
    assert result == 7


@mock_aws
def test_get_retention_years_attribute_absent_defaults_to_5():
    """get_retention_years returns 5 when the attribute is absent."""
    os.environ.setdefault("DATA_TABLE", "test-data-table")
    os.environ.setdefault("AWS_REGION", "eu-west-2")

    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    # Tenant item exists but has no retention_years attribute
    client.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": "TENANT#acme"},
            "SK": {"S": "TENANT#acme"},
        },
    )

    result = get_retention_years("acme")
    assert result == 5


@mock_aws
def test_get_retention_years_item_absent_defaults_to_5():
    """get_retention_years returns 5 when the tenant item does not exist."""
    os.environ.setdefault("DATA_TABLE", "test-data-table")
    os.environ.setdefault("AWS_REGION", "eu-west-2")

    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    result = get_retention_years("nonexistent_tenant")
    assert result == 5


# ---------------------------------------------------------------------------
# put_img_record tests
# ---------------------------------------------------------------------------


@mock_aws
def test_put_img_record_and_read_back():
    """put_img_record writes the item; reading it back returns all attributes."""
    os.environ.setdefault("DATA_TABLE", "test-data-table")
    os.environ.setdefault("AWS_REGION", "eu-west-2")

    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    put_img_record(
        tenant_id="acme",
        site_id="site_01",
        camera_id="cam_01",
        snapshot_ts="2025-06-15T14:00:00Z",
        s3_key="acme/site_01/cam_01/2025/06/15/2025-06-15T14:00:00Z.jpg",
        sha256_hex="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        size_bytes=1024,
    )

    response = client.get_item(
        TableName="test-data-table",
        Key={
            "PK": {"S": "TENANT#acme"},
            "SK": {"S": "IMG#site_01#cam_01#2025-06-15T14:00:00Z"},
        },
    )
    item = response.get("Item")
    assert item is not None
    assert item["s3_key"]["S"] == "acme/site_01/cam_01/2025/06/15/2025-06-15T14:00:00Z.jpg"
    assert item["sha256"]["S"] == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert item["size_bytes"]["N"] == "1024"
    assert item["ingested_at"]["S"] == "2025-06-15T14:00:00Z"
    assert item["content_type"]["S"] == "image/jpeg"


# ---------------------------------------------------------------------------
# put_user tests
# Requirements validated: 1.1, 1.2
# ---------------------------------------------------------------------------


@mock_aws
def test_put_user_writes_correct_item_structure():
    """put_user writes a User_Record with correct PK, SK, and all attributes."""
    os.environ.setdefault("DATA_TABLE", "test-data-table")
    os.environ.setdefault("AWS_REGION", "eu-west-2")

    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    put_user(
        tenant_id="acme_corp",
        sub="a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        email="jane.doe@acme.example.com",
        full_name="Jane Doe",
        role="user",
        site_access=["site_001", "site_002"],
    )

    response = client.get_item(
        TableName="test-data-table",
        Key={
            "PK": {"S": "TENANT#acme_corp"},
            "SK": {"S": "USER#a1b2c3d4-e5f6-7890-abcd-ef1234567890"},
        },
    )
    item = response.get("Item")
    assert item is not None
    assert item["PK"]["S"] == "TENANT#acme_corp"
    assert item["SK"]["S"] == "USER#a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    assert item["sub"]["S"] == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    assert item["email"]["S"] == "jane.doe@acme.example.com"
    assert item["full_name"]["S"] == "Jane Doe"
    assert item["tenant_id"]["S"] == "acme_corp"
    assert item["role"]["S"] == "user"
    assert item["site_access"]["L"] == [{"S": "site_001"}, {"S": "site_002"}]


# ---------------------------------------------------------------------------
# get_users_for_tenant tests
# Requirements validated: 2.6, 2.8
# ---------------------------------------------------------------------------


@mock_aws
def test_get_users_for_tenant_returns_all_user_items():
    """get_users_for_tenant returns all User_Records for a tenant."""
    os.environ.setdefault("DATA_TABLE", "test-data-table")
    os.environ.setdefault("AWS_REGION", "eu-west-2")

    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    # Insert two user records for the same tenant
    client.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": "TENANT#acme_corp"},
            "SK": {"S": "USER#sub-001"},
            "sub": {"S": "sub-001"},
            "email": {"S": "alice@acme.example.com"},
            "full_name": {"S": "Alice Smith"},
            "tenant_id": {"S": "acme_corp"},
            "role": {"S": "user"},
            "site_access": {"L": [{"S": "site_001"}]},
        },
    )
    client.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": "TENANT#acme_corp"},
            "SK": {"S": "USER#sub-002"},
            "sub": {"S": "sub-002"},
            "email": {"S": "bob@acme.example.com"},
            "full_name": {"S": "Bob Jones"},
            "tenant_id": {"S": "acme_corp"},
            "role": {"S": "tenant_admin"},
            "site_access": {"L": []},
        },
    )

    # Insert a non-user item for the same tenant (should not be returned)
    client.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": "TENANT#acme_corp"},
            "SK": {"S": "SITE#site_001"},
            "site_name": {"S": "Main Site"},
        },
    )

    items = get_users_for_tenant("acme_corp")
    assert len(items) == 2

    subs = {item["sub"]["S"] for item in items}
    assert subs == {"sub-001", "sub-002"}


@mock_aws
def test_get_users_for_tenant_returns_empty_list_when_no_users():
    """get_users_for_tenant returns an empty list when no user records exist."""
    os.environ.setdefault("DATA_TABLE", "test-data-table")
    os.environ.setdefault("AWS_REGION", "eu-west-2")

    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    # Insert a non-user item for the tenant
    client.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": "TENANT#empty_tenant"},
            "SK": {"S": "SITE#site_001"},
            "site_name": {"S": "Some Site"},
        },
    )

    items = get_users_for_tenant("empty_tenant")
    assert items == []
