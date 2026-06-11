"""Cadence check unit tests for the ingest handler.

Tests the 15-minute minimum gap enforcement between saved timelapse
snapshots. The cadence check queries DynamoDB for the latest IMG# record
and decides whether to write a new timelapse snapshot.

Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.7
"""

from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from sitespy.errors import ApiError
from sitespy.handlers.ingest import _handle, resolve_correlation_id
from sitespy.http import error_response, unhandled_error_response

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_TOKEN = "tk_validtoken1234567890abcdefghijklmnopqr"
_TENANT_ID = "acme"
_SITE_ID = "site_01"
_CAMERA_ID = "cam_01"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_env():
    os.environ["SNAPSHOTS_BUCKET"] = "test-snapshots-bucket"
    os.environ["DATA_TABLE"] = "test-data-table"
    os.environ["AWS_REGION"] = "eu-west-2"
    os.environ["AWS_DEFAULT_REGION"] = "eu-west-2"
    os.environ["AWS_ACCESS_KEY_ID"] = "testing"
    os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
    os.environ["AWS_SECURITY_TOKEN"] = "testing"
    os.environ["AWS_SESSION_TOKEN"] = "testing"
    os.environ["ENVIRONMENT"] = "test"
    os.environ["LOG_LEVEL"] = "INFO"
    from sitespy.config import get_settings
    from sitespy.data import _dynamodb_client
    from sitespy.storage import _s3_client

    get_settings.cache_clear()
    _dynamodb_client.cache_clear()
    _s3_client.cache_clear()


def _setup_aws():
    s3 = boto3.client("s3", region_name="eu-west-2")
    s3.create_bucket(
        Bucket="test-snapshots-bucket",
        CreateBucketConfiguration={"LocationConstraint": "eu-west-2"},
    )
    s3.put_bucket_versioning(
        Bucket="test-snapshots-bucket",
        VersioningConfiguration={"Status": "Enabled"},
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


def _seed_camera(ddb, tenant_id=_TENANT_ID, site_id=_SITE_ID, camera_id=_CAMERA_ID):
    """Seed a camera with token-based GSI1 index and tenant record."""
    ddb.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": f"TENANT#{tenant_id}"},
            "SK": {"S": f"SITE#{site_id}#CAM#{camera_id}"},
            "GSI1PK": {"S": f"TOKEN#{_VALID_TOKEN}"},
            "GSI1SK": {"S": "CAMERA"},
            "camera_name": {"S": "Test Camera"},
            "ingest_token": {"S": _VALID_TOKEN},
        },
    )
    ddb.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": f"TENANT#{tenant_id}"},
            "SK": {"S": f"TENANT#{tenant_id}"},
            "retention_years": {"N": "5"},
        },
    )


def _seed_img_record(ddb, ingested_at: str, tenant_id=_TENANT_ID, site_id=_SITE_ID, camera_id=_CAMERA_ID):
    """Seed an IMG# record with the given ingested_at timestamp."""
    sk = f"IMG#{site_id}#{camera_id}#{ingested_at}"
    ddb.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": f"TENANT#{tenant_id}"},
            "SK": {"S": sk},
            "s3_key": {"S": f"{tenant_id}/{site_id}/{camera_id}/2025/06/10/{ingested_at}.jpg"},
            "sha256": {"S": "abc123"},
            "size_bytes": {"N": "1024"},
            "ingested_at": {"S": ingested_at},
            "content_type": {"S": "image/jpeg"},
        },
    )


def _make_event(body=None):
    """Build a valid ingest event."""
    if body is None:
        body = b"\xff\xd8\xff\xe0" + b"\x00" * 100
    return {
        "httpMethod": "POST",
        "path": f"/v1/ingest/{_VALID_TOKEN}",
        "pathParameters": {"token": _VALID_TOKEN},
        "headers": {
            "Content-Type": "image/jpeg",
        },
        "queryStringParameters": None,
        "body": base64.b64encode(body).decode(),
        "isBase64Encoded": True,
    }


def _invoke(event):
    corr_id = resolve_correlation_id(event)
    try:
        return _handle(event, corr_id)
    except ApiError as exc:
        return error_response(exc, corr_id)
    except Exception:
        return unhandled_error_response(corr_id)


# ---------------------------------------------------------------------------
# Tests: Cadence check logic
# ---------------------------------------------------------------------------


@patch("sitespy.handlers.ingest.data.get_live_session", return_value=None)
def test_first_push_no_img_record_saves_timelapse(mock_live_session):
    """First push (no IMG# record) → save_timelapse = True → 201.

    Validates: Requirement 1.4
    When no prior IMG# record exists for the camera, the handler should
    save the snapshot unconditionally and return 201.
    """
    _set_env()
    with mock_aws():
        _s3, ddb = _setup_aws()
        _seed_camera(ddb)

        # Clear caches inside the mock context
        from sitespy.config import get_settings
        from sitespy.data import _dynamodb_client
        from sitespy.storage import _s3_client

        get_settings.cache_clear()
        _dynamodb_client.cache_clear()
        _s3_client.cache_clear()

        result = _invoke(_make_event())

    assert result["statusCode"] == 201
    body = json.loads(result["body"])
    assert "key" in body
    assert "sha256" in body


@patch("sitespy.handlers.ingest.data.get_live_session", return_value=None)
def test_last_record_less_than_15_min_ago_skips_timelapse(mock_live_session):
    """Last IMG# record < 15 min ago → save_timelapse = False → 200 skipped.

    Validates: Requirement 1.2
    When the last saved snapshot was less than 15 minutes ago, the handler
    should skip saving and return 200 with cadence_filter reason.
    """
    _set_env()
    with mock_aws():
        _s3, ddb = _setup_aws()
        _seed_camera(ddb)

        # Clear caches inside the mock context
        from sitespy.config import get_settings
        from sitespy.data import _dynamodb_client
        from sitespy.storage import _s3_client

        get_settings.cache_clear()
        _dynamodb_client.cache_clear()
        _s3_client.cache_clear()

        # Seed an IMG# record from 5 minutes ago
        five_min_ago = (datetime.now(tz=UTC) - timedelta(minutes=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        _seed_img_record(ddb, five_min_ago)

        result = _invoke(_make_event())

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["status"] == "skipped"
    assert body["reason"] == "cadence_filter"
    assert body["camera_id"] == _CAMERA_ID
    assert body["live_captured"] is False


@patch("sitespy.handlers.ingest.data.get_live_session", return_value=None)
def test_last_record_15_min_or_more_ago_saves_timelapse(mock_live_session):
    """Last IMG# record ≥ 15 min ago → save_timelapse = True → 201.

    Validates: Requirement 1.3
    When the last saved snapshot was 15 or more minutes ago, the handler
    should save a new snapshot and return 201.
    """
    _set_env()
    with mock_aws():
        _s3, ddb = _setup_aws()
        _seed_camera(ddb)

        # Clear caches inside the mock context
        from sitespy.config import get_settings
        from sitespy.data import _dynamodb_client
        from sitespy.storage import _s3_client

        get_settings.cache_clear()
        _dynamodb_client.cache_clear()
        _s3_client.cache_clear()

        # Seed an IMG# record from 20 minutes ago
        twenty_min_ago = (datetime.now(tz=UTC) - timedelta(minutes=20)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        _seed_img_record(ddb, twenty_min_ago)

        result = _invoke(_make_event())

    assert result["statusCode"] == 201
    body = json.loads(result["body"])
    assert "key" in body
    assert "sha256" in body
    assert body["live_captured"] is False


@patch("sitespy.handlers.ingest.data.get_live_session", return_value=None)
@patch("sitespy.handlers.ingest.data.get_latest_img_record", side_effect=Exception("DynamoDB timeout"))
def test_dynamo_error_fails_open_saves_timelapse(mock_get_latest, mock_live_session):
    """DynamoDB error during cadence check → fail open (save_timelapse = True) → 201.

    Validates: Requirement 1.7
    When the DynamoDB query for the latest IMG# record fails, the handler
    should log the error and proceed as if no prior record exists (fail open),
    saving the snapshot and returning 201.
    """
    _set_env()
    with mock_aws():
        _s3, ddb = _setup_aws()
        _seed_camera(ddb)

        # Clear caches inside the mock context
        from sitespy.config import get_settings
        from sitespy.data import _dynamodb_client
        from sitespy.storage import _s3_client

        get_settings.cache_clear()
        _dynamodb_client.cache_clear()
        _s3_client.cache_clear()

        result = _invoke(_make_event())

    assert result["statusCode"] == 201
    body = json.loads(result["body"])
    assert "key" in body
    assert "sha256" in body
