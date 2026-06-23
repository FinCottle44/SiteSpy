"""Unit tests for data.py sandbox provisioning and camera transfer operations.

Requirements validated (camera-sandbox): 1.2, 1.3, 1.4, 5.3, 5.4, 5.5, 5.6, 7.1
"""

import os

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from sitespy.data import (
    _dynamodb_client,
    ensure_sandbox_default_site,
    ensure_sandbox_tenant_record,
    get_camera,
    get_camera_by_token,
    get_site,
    get_tenant,
    put_camera,
    transfer_camera,
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
# ensure_sandbox_tenant_record tests
# ---------------------------------------------------------------------------


@mock_aws
def test_ensure_sandbox_tenant_record_creates_when_not_exist():
    """ensure_sandbox_tenant_record creates the tenant record and returns True."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    result = ensure_sandbox_tenant_record()
    assert result is True

    # Verify record in DynamoDB
    item = get_tenant("sandbox_construction")
    assert item is not None
    assert item["PK"]["S"] == "TENANT#sandbox_construction"
    assert item["SK"]["S"] == "TENANT#sandbox_construction"
    assert item["tenant_name"]["S"] == "Sandbox Construction"
    assert item["stale_threshold_hours"]["N"] == "24"
    assert "created_at" in item


@mock_aws
def test_ensure_sandbox_tenant_record_returns_false_when_exists():
    """ensure_sandbox_tenant_record returns False if the record already exists."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    # Create first time
    assert ensure_sandbox_tenant_record() is True
    # Second call should return False (already exists)
    assert ensure_sandbox_tenant_record() is False


@mock_aws
def test_ensure_sandbox_tenant_record_does_not_overwrite():
    """Calling ensure_sandbox_tenant_record when record exists leaves it unchanged."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    ensure_sandbox_tenant_record()
    first = get_tenant("sandbox_construction")

    ensure_sandbox_tenant_record()
    second = get_tenant("sandbox_construction")

    assert first["created_at"] == second["created_at"]


# ---------------------------------------------------------------------------
# ensure_sandbox_default_site tests
# ---------------------------------------------------------------------------


@mock_aws
def test_ensure_sandbox_default_site_creates_when_not_exist():
    """ensure_sandbox_default_site creates the site record and returns True."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    result = ensure_sandbox_default_site()
    assert result is True

    # Verify record in DynamoDB
    item = get_site("sandbox_construction", "default_sandbox_site")
    assert item is not None
    assert item["PK"]["S"] == "TENANT#sandbox_construction"
    assert item["SK"]["S"] == "SITE#default_sandbox_site"
    assert item["site_name"]["S"] == "Default Sandbox Site"
    assert item["latitude"]["N"] == str(-33.8688)
    assert item["longitude"]["N"] == str(151.2093)
    assert item["timezone"]["S"] == "Australia/Sydney"
    assert "created_at" in item


@mock_aws
def test_ensure_sandbox_default_site_returns_false_when_exists():
    """ensure_sandbox_default_site returns False if the record already exists."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    assert ensure_sandbox_default_site() is True
    assert ensure_sandbox_default_site() is False


@mock_aws
def test_ensure_sandbox_default_site_does_not_overwrite():
    """Calling ensure_sandbox_default_site when record exists leaves it unchanged."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    ensure_sandbox_default_site()
    first = get_site("sandbox_construction", "default_sandbox_site")

    ensure_sandbox_default_site()
    second = get_site("sandbox_construction", "default_sandbox_site")

    assert first["created_at"] == second["created_at"]


# ---------------------------------------------------------------------------
# transfer_camera tests
# ---------------------------------------------------------------------------


@mock_aws
def test_transfer_camera_moves_record_atomically():
    """transfer_camera creates target record and deletes source in one transaction."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    # Create source camera
    put_camera("sandbox_construction", "default_sandbox_site", "cam_01", "North", "Axis P1455", "tok_abc")

    # Transfer
    transfer_camera(
        source_tenant_id="sandbox_construction",
        source_site_id="default_sandbox_site",
        target_tenant_id="acme",
        target_site_id="site_001",
        camera_id="cam_01",
        camera_name="North",
        camera_model="Axis P1455",
        ingest_token="tok_abc",
        created_at="2024-01-15T10:00:00Z",
    )

    # Source should be gone
    assert get_camera("sandbox_construction", "default_sandbox_site", "cam_01") is None

    # Target should exist with correct attributes
    target = get_camera("acme", "site_001", "cam_01")
    assert target is not None
    assert target["camera_name"]["S"] == "North"
    assert target["camera_model"]["S"] == "Axis P1455"
    assert target["ingest_token"]["S"] == "tok_abc"
    assert target["GSI1PK"]["S"] == "TOKEN#tok_abc"
    assert target["GSI1SK"]["S"] == "CAMERA"
    assert target["created_at"]["S"] == "2024-01-15T10:00:00Z"
    assert "transferred_at" in target


@mock_aws
def test_transfer_camera_without_model():
    """transfer_camera works correctly when camera_model is None."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    put_camera("sandbox_construction", "default_sandbox_site", "cam_02", "South", None, "tok_xyz")

    transfer_camera(
        source_tenant_id="sandbox_construction",
        source_site_id="default_sandbox_site",
        target_tenant_id="acme",
        target_site_id="site_001",
        camera_id="cam_02",
        camera_name="South",
        camera_model=None,
        ingest_token="tok_xyz",
        created_at="2024-01-15T11:00:00Z",
    )

    target = get_camera("acme", "site_001", "cam_02")
    assert target is not None
    assert "camera_model" not in target
    assert target["camera_name"]["S"] == "South"


@mock_aws
def test_transfer_camera_preserves_token_in_gsi1():
    """After transfer, the GSI1 token lookup resolves to the target record."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    put_camera("sandbox_construction", "default_sandbox_site", "cam_03", "East", None, "tok_gsi")

    transfer_camera(
        source_tenant_id="sandbox_construction",
        source_site_id="default_sandbox_site",
        target_tenant_id="customer",
        target_site_id="site_a",
        camera_id="cam_03",
        camera_name="East",
        camera_model=None,
        ingest_token="tok_gsi",
        created_at="2024-02-01T08:00:00Z",
    )

    # GSI1 lookup should find the target record
    result = get_camera_by_token("tok_gsi")
    assert result is not None
    assert result["PK"]["S"] == "TENANT#customer"
    assert "SITE#site_a#CAM#cam_03" in result["SK"]["S"]


@mock_aws
def test_transfer_camera_fails_if_target_exists():
    """transfer_camera raises ClientError if camera already exists at target."""
    client = boto3.client("dynamodb", region_name="eu-west-2")
    _create_table(client)

    # Create source
    put_camera("sandbox_construction", "default_sandbox_site", "cam_04", "West", None, "tok_src")
    # Create conflicting target
    put_camera("acme", "site_001", "cam_04", "Existing", None, "tok_existing")

    with pytest.raises(ClientError) as exc_info:
        transfer_camera(
            source_tenant_id="sandbox_construction",
            source_site_id="default_sandbox_site",
            target_tenant_id="acme",
            target_site_id="site_001",
            camera_id="cam_04",
            camera_name="West",
            camera_model=None,
            ingest_token="tok_src",
            created_at="2024-03-01T09:00:00Z",
        )

    assert "TransactionCanceledException" in exc_info.value.response["Error"]["Code"]

    # Source should still exist (transaction rolled back)
    assert get_camera("sandbox_construction", "default_sandbox_site", "cam_04") is not None
