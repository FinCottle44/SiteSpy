"""P4 Authentication Binding — Structural Invariant property test.

Property 4: Authentication Binding
Validates: Requirements 2.3, 3.2, 3.3

For any ingest request, the single DynamoDB GetItem during authentication
uses exactly Key = {"PK": "TENANT#"+t, "SK": "SITE#"+s+"#CAM#"+c}.
"""

from __future__ import annotations

import base64
import os
from unittest.mock import patch

import bcrypt
import boto3
from hypothesis import given, settings
from hypothesis import strategies as st
from moto import mock_aws

from sitespy.data import _dynamodb_client
from sitespy.handlers.ingest import _handle
from sitespy.storage import _s3_client

_ID_ST = st.from_regex(r"^[a-z0-9_]{1,64}$", fullmatch=True)


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

    get_settings.cache_clear()
    _s3_client.cache_clear()
    _dynamodb_client.cache_clear()


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


def _seed_camera(ddb, tenant_id, site_id, camera_id, username, password):
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12))
    ddb.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": f"TENANT#{tenant_id}"},
            "SK": {"S": f"SITE#{site_id}#CAM#{camera_id}"},
            "ingest_username": {"S": username},
            "ingest_password_hash": {"S": hashed.decode()},
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


def _make_event(tenant_id, site_id, camera_id, username, password, body=None):
    if body is None:
        body = b"\xff\xd8\xff\xe0" + b"\x00" * 100
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {
        "httpMethod": "POST",
        "path": "/v1/ingest",
        "headers": {
            "Authorization": f"Basic {credentials}",
            "X-Tenant-ID": tenant_id,
            "X-Site-ID": site_id,
            "Content-Type": "image/jpeg",
        },
        "queryStringParameters": {"cameraID": camera_id},
        "body": base64.b64encode(body).decode(),
        "isBase64Encoded": True,
    }


@given(
    tenant_id=_ID_ST,
    site_id=_ID_ST,
    camera_id=_ID_ST,
)
@settings(max_examples=20, deadline=None)
def test_p4_auth_binding_get_item_key(tenant_id, site_id, camera_id):
    """P4: the single GetItem during auth uses exactly the request's (t, s, c) triple."""
    _set_env()

    with mock_aws():
        _s3, ddb = _setup_aws()
        username = "sitespy_cam_bind"
        password = "bindpassword"
        _seed_camera(ddb, tenant_id, site_id, camera_id, username, password)

        event = _make_event(tenant_id, site_id, camera_id, username, password)

        # Spy on the DynamoDB client's get_item calls
        get_item_calls = []
        original_get_camera = None

        import sitespy.data as data_module

        original_get_camera = data_module.get_camera

        def spy_get_camera(t, s, c):
            get_item_calls.append((t, s, c))
            return original_get_camera(t, s, c)

        import contextlib

        with (
            patch.object(data_module, "get_camera", side_effect=spy_get_camera),
            contextlib.suppress(Exception),
        ):
            _handle(event, "corr-p4")

        # Exactly one get_camera call
        assert len(get_item_calls) == 1
        called_t, called_s, called_c = get_item_calls[0]
        assert called_t == tenant_id
        assert called_s == site_id
        assert called_c == camera_id
