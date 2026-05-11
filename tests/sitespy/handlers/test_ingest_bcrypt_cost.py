"""P8 Bcrypt Cost Confinement property test.

Property 8: Bcrypt Cost Confinement
Validates: Requirement 2.9

No authentication success is ever produced against a hash with cost < 12.
Parameterised over costs [4, 8, 10, 11, 12, 13]: assert 201 iff cost >= 12.
"""

from __future__ import annotations

import base64
import json
import os

import bcrypt
import boto3
import pytest
from moto import mock_aws

from sitespy.errors import ApiError
from sitespy.handlers.ingest import _handle, resolve_correlation_id
from sitespy.http import error_response, unhandled_error_response


def _invoke(event):
    """Call _handle and convert ApiError to a response dict."""
    corr_id = resolve_correlation_id(event)
    try:
        return _handle(event, corr_id)
    except ApiError as exc:
        return error_response(exc, corr_id)
    except Exception:
        return unhandled_error_response(corr_id)


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


def _seed_camera_with_cost(ddb, cost, username, password):
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=cost))
    ddb.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": "TENANT#acme"},
            "SK": {"S": "SITE#site_01#CAM#cam_01"},
            "ingest_username": {"S": username},
            "ingest_password_hash": {"S": hashed.decode()},
        },
    )
    ddb.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": "TENANT#acme"},
            "SK": {"S": "TENANT#acme"},
            "retention_years": {"N": "5"},
        },
    )


def _make_event(username, password):
    body = b"\xff\xd8\xff\xe0" + b"\x00" * 100
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {
        "httpMethod": "POST",
        "path": "/v1/ingest",
        "headers": {
            "Authorization": f"Basic {credentials}",
            "X-Tenant-ID": "acme",
            "X-Site-ID": "site_01",
            "Content-Type": "image/jpeg",
        },
        "queryStringParameters": {"cameraID": "cam_01"},
        "body": base64.b64encode(body).decode(),
        "isBase64Encoded": True,
    }


@pytest.mark.parametrize(
    "cost,expected_status",
    [
        (4, 401),
        (8, 401),
        (10, 401),
        (11, 401),
        (12, 201),
        (13, 201),
    ],
)
def test_p8_bcrypt_cost_confinement(cost, expected_status):
    """P8: 201 iff cost >= 12; 401 for cost < 12."""
    _set_env()
    with mock_aws():
        _s3, ddb = _setup_aws()

        username = "sitespy_cam_costtest"
        password = "costpassword123"

        _seed_camera_with_cost(ddb, cost, username, password)

        event = _make_event(username, password)

        result = _invoke(event)

        assert result["statusCode"] == expected_status, (
            f"cost={cost}: expected {expected_status}, got {result['statusCode']}"
        )

        if expected_status == 401:
            body = json.loads(result["body"])
            assert body["error"] == "UNAUTHORIZED"
        elif expected_status == 201:
            body = json.loads(result["body"])
            assert "sha256" in body
            assert "key" in body
