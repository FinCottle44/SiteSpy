"""Unit tests for data.py write operations (admin management).

Requirements validated: 1.1, 1.9, 2.12, 2.13, 3.11, 3.12, 3.14
"""

import os

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from sitespy.data import (
    _dynamodb_client,
    delete_camera,
    get_tenant,
    put_camera,
    put_site,
    put_tenant,
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
# get_tenant tests
# ---------------------------------------------------------------------------


@mock_aws
def test_get_tenant_returns_item_when_exists():
    """get_tenant returns the tenant item when it exists."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    client.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": "TENANT#acme"},
            "SK": {"S": "TENANT#acme"},
            "tenant_name": {"S": "Acme Corp"},
            "primary_contact_email": {"S": "ops@acme.com"},
            "stale_threshold_hours": {"N": "24"},
        },
    )

    item = get_tenant("acme")
    assert item is not None
    assert item["PK"]["S"] == "TENANT#acme"
    assert item["tenant_name"]["S"] == "Acme Corp"


@mock_aws
def test_get_tenant_returns_none_when_absent():
    """get_tenant returns None when the tenant does not exist."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    item = get_tenant("nonexistent")
    assert item is None


# ---------------------------------------------------------------------------
# put_tenant tests
# ---------------------------------------------------------------------------


@mock_aws
def test_put_tenant_creates_record():
    """put_tenant writes a tenant record and returns the plain dict."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    result = put_tenant(
        tenant_id="acme",
        tenant_name="Acme Corp",
        primary_contact_email="ops@acme.com",
        stale_threshold_hours=24,
    )

    assert result["tenant_id"] == "acme"
    assert result["tenant_name"] == "Acme Corp"
    assert result["primary_contact_email"] == "ops@acme.com"
    assert result["stale_threshold_hours"] == 24

    # Verify in DynamoDB
    response = client.get_item(
        TableName="test-data-table",
        Key={"PK": {"S": "TENANT#acme"}, "SK": {"S": "TENANT#acme"}},
    )
    item = response["Item"]
    assert item["tenant_name"]["S"] == "Acme Corp"
    assert item["stale_threshold_hours"]["N"] == "24"
    assert "created_at" in item


@mock_aws
def test_put_tenant_duplicate_raises_conditional_check():
    """put_tenant raises ConditionalCheckFailedException on duplicate."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    put_tenant("acme", "Acme Corp", "ops@acme.com", 24)

    with pytest.raises(ClientError) as exc_info:
        put_tenant("acme", "Acme Corp 2", "other@acme.com", 48)

    assert exc_info.value.response["Error"]["Code"] == "ConditionalCheckFailedException"


# ---------------------------------------------------------------------------
# put_site tests
# ---------------------------------------------------------------------------


@mock_aws
def test_put_site_creates_record():
    """put_site writes a site record and returns the plain dict."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    result = put_site(
        tenant_id="acme",
        site_id="site_001",
        site_name="Acme Tower",
        latitude=51.5074,
        longitude=-0.1278,
        timezone_str="Europe/London",
    )

    assert result["site_id"] == "site_001"
    assert result["site_name"] == "Acme Tower"
    assert result["tenant_id"] == "acme"
    assert result["latitude"] == 51.5074
    assert result["longitude"] == -0.1278
    assert result["timezone"] == "Europe/London"

    # Verify in DynamoDB
    response = client.get_item(
        TableName="test-data-table",
        Key={"PK": {"S": "TENANT#acme"}, "SK": {"S": "SITE#site_001"}},
    )
    item = response["Item"]
    assert item["site_name"]["S"] == "Acme Tower"
    assert item["latitude"]["N"] == "51.5074"
    assert item["timezone"]["S"] == "Europe/London"
    assert "created_at" in item


@mock_aws
def test_put_site_duplicate_raises_conditional_check():
    """put_site raises ConditionalCheckFailedException on duplicate."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    put_site("acme", "site_001", "Acme Tower", 51.5074, -0.1278, "Europe/London")

    with pytest.raises(ClientError) as exc_info:
        put_site("acme", "site_001", "Different Name", 52.0, -1.0, "US/Eastern")

    assert exc_info.value.response["Error"]["Code"] == "ConditionalCheckFailedException"


# ---------------------------------------------------------------------------
# put_camera tests
# ---------------------------------------------------------------------------


@mock_aws
def test_put_camera_creates_record_with_model():
    """put_camera writes a camera record with camera_model."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    result = put_camera(
        tenant_id="acme",
        site_id="site_001",
        camera_id="cam_01",
        camera_name="North elevation",
        camera_model="Axis P1455-LE",
        ingest_token="tok_abc123",
    )

    assert result["camera_id"] == "cam_01"
    assert result["camera_name"] == "North elevation"
    assert result["camera_model"] == "Axis P1455-LE"

    # Verify in DynamoDB
    response = client.get_item(
        TableName="test-data-table",
        Key={"PK": {"S": "TENANT#acme"}, "SK": {"S": "SITE#site_001#CAM#cam_01"}},
    )
    item = response["Item"]
    assert item["camera_name"]["S"] == "North elevation"
    assert item["camera_model"]["S"] == "Axis P1455-LE"
    assert item["ingest_token"]["S"] == "tok_abc123"
    assert item["GSI1PK"]["S"] == "TOKEN#tok_abc123"
    assert item["GSI1SK"]["S"] == "CAMERA"
    assert "created_at" in item


@mock_aws
def test_put_camera_creates_record_without_model():
    """put_camera writes a camera record without camera_model."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    result = put_camera(
        tenant_id="acme",
        site_id="site_001",
        camera_id="cam_01",
        camera_name="North elevation",
        camera_model=None,
        ingest_token="tok_xyz789",
    )

    assert result["camera_id"] == "cam_01"
    assert result["camera_name"] == "North elevation"
    assert "camera_model" not in result

    # Verify in DynamoDB — no camera_model attribute
    response = client.get_item(
        TableName="test-data-table",
        Key={"PK": {"S": "TENANT#acme"}, "SK": {"S": "SITE#site_001#CAM#cam_01"}},
    )
    item = response["Item"]
    assert "camera_model" not in item
    assert item["ingest_token"]["S"] == "tok_xyz789"
    assert item["GSI1PK"]["S"] == "TOKEN#tok_xyz789"


@mock_aws
def test_put_camera_duplicate_raises_conditional_check():
    """put_camera raises ConditionalCheckFailedException on duplicate."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    put_camera("acme", "site_001", "cam_01", "North elevation", "Axis P1455-LE", "tok_first")

    with pytest.raises(ClientError) as exc_info:
        put_camera("acme", "site_001", "cam_01", "Different Name", None, "tok_second")

    assert exc_info.value.response["Error"]["Code"] == "ConditionalCheckFailedException"


# ---------------------------------------------------------------------------
# delete_camera tests
# ---------------------------------------------------------------------------


@mock_aws
def test_delete_camera_removes_record():
    """delete_camera removes the camera record from DynamoDB."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    # First create a camera
    put_camera("acme", "site_001", "cam_01", "North elevation", "Axis P1455-LE", "tok_delete_me")

    # Verify it exists
    response = client.get_item(
        TableName="test-data-table",
        Key={"PK": {"S": "TENANT#acme"}, "SK": {"S": "SITE#site_001#CAM#cam_01"}},
    )
    assert "Item" in response

    # Delete it
    delete_camera("acme", "site_001", "cam_01")

    # Verify it's gone
    response = client.get_item(
        TableName="test-data-table",
        Key={"PK": {"S": "TENANT#acme"}, "SK": {"S": "SITE#site_001#CAM#cam_01"}},
    )
    assert "Item" not in response


@mock_aws
def test_delete_camera_nonexistent_does_not_raise():
    """delete_camera does not raise when the item doesn't exist (idempotent)."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    # Should not raise
    delete_camera("acme", "site_001", "cam_nonexistent")
