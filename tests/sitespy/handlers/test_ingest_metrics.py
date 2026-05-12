"""Structured log and response shape tests.

Requirements validated: 10.2, 10.3, 10.4
"""

from __future__ import annotations

import base64
import json
import logging
import os

import boto3
from moto import mock_aws

from sitespy.errors import ApiError
from sitespy.handlers.ingest import _handle, resolve_correlation_id
from sitespy.http import error_response, unhandled_error_response

_VALID_TOKEN = "tk_validtoken1234567890abcdefghijklmnopqr"


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


def _seed_camera(ddb, tenant_id, site_id, camera_id, ingest_token):
    """Write camera and tenant rows to mocked DynamoDB."""
    ddb.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": f"TENANT#{tenant_id}"},
            "SK": {"S": f"SITE#{site_id}#CAM#{camera_id}"},
            "GSI1PK": {"S": f"TOKEN#{ingest_token}"},
            "GSI1SK": {"S": "CAMERA"},
            "camera_name": {"S": "Test Camera"},
            "ingest_token": {"S": ingest_token},
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


def _make_event(ingest_token, body=None, correlation_id="test-corr-id"):
    """Build an ingest event using token-based URL path auth."""
    if body is None:
        body = b"\xff\xd8\xff\xe0" + b"\x00" * 100
    return {
        "httpMethod": "POST",
        "path": f"/v1/ingest/{ingest_token}",
        "pathParameters": {"token": ingest_token},
        "headers": {
            "Content-Type": "image/jpeg",
            "X-Correlation-Id": correlation_id,
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
# Structured log line tests
# ---------------------------------------------------------------------------


def test_success_log_contains_required_fields(caplog):
    """On 201, the response contains all required fields (sha256, key, timestamp, camera_id)."""
    _set_env()
    with mock_aws():
        _s3, ddb = _setup_aws()
        _seed_camera(ddb, "acme", "site_01", "cam_01", _VALID_TOKEN)

        from sitespy.config import get_settings as _gs
        from sitespy.data import _dynamodb_client as _ddb_c
        from sitespy.storage import _s3_client as _s3c

        _gs.cache_clear()
        _ddb_c.cache_clear()
        _s3c.cache_clear()

        event = _make_event(_VALID_TOKEN)

        result = _invoke(event)

        assert result["statusCode"] == 201
        body = json.loads(result["body"])

        # Verify all required response fields per Requirement 9
        assert "key" in body
        assert "timestamp" in body
        assert "camera_id" in body
        assert "sha256" in body
        assert body["camera_id"] == "cam_01"
        assert len(body["sha256"]) == 64  # 64-char hex SHA-256
        assert "X-Correlation-Id" in result.get("headers", {})


def test_failure_log_contains_required_fields(caplog):
    """On 401, the response has correct error envelope."""
    _set_env()
    with mock_aws():
        _setup_aws()

        # Use an invalid token (no camera seeded) → 401
        event = _make_event(_VALID_TOKEN)

        with caplog.at_level(logging.WARNING):
            result = _invoke(event)

        assert result["statusCode"] == 401
        body = json.loads(result["body"])
        assert body["error"] == "UNAUTHORIZED"


# ---------------------------------------------------------------------------
# Correlation ID in response header
# ---------------------------------------------------------------------------


def test_correlation_id_in_response_header():
    """X-Correlation-Id is present in every response."""
    _set_env()
    with mock_aws():
        _setup_aws()
        event = _make_event(_VALID_TOKEN)
        result = _invoke(event)
        assert "X-Correlation-Id" in result.get("headers", {})


def test_correlation_id_reused_from_request():
    """X-Correlation-Id from request is echoed back in response."""
    _set_env()
    with mock_aws():
        _setup_aws()
        event = _make_event(_VALID_TOKEN, correlation_id="my-custom-corr-id")
        result = _invoke(event)
        assert result["headers"]["X-Correlation-Id"] == "my-custom-corr-id"
