"""P3 Idempotency property test.

Property 3: Idempotency (f ∘ f ≡ f)
Validates: Requirements 7.1, 7.2, 7.3

Running _handle twice with identical bytes and frozen clock leaves exactly
one current S3 version and exactly one IMG# item.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import UTC
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
_BODY_ST = st.binary(min_size=4, max_size=512).map(lambda b: b"\xff\xd8\xff\xe0" + b)

_FROZEN_TS = "2025-06-15T14:00:00Z"


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


def _make_event(tenant_id, site_id, camera_id, body, username, password):
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


@given(
    tenant_id=_ID_ST,
    site_id=_ID_ST,
    camera_id=_ID_ST,
    body=_BODY_ST,
)
@settings(max_examples=10, deadline=None)
def test_p3_idempotency_same_body_twice(tenant_id, site_id, camera_id, body):
    """P3: two identical ingests produce exactly one S3 version and one IMG# item."""
    _set_env()

    with mock_aws():
        s3, ddb = _setup_aws()
        username = "sitespy_cam_idem"
        password = "idempassword"
        _seed_camera(ddb, tenant_id, site_id, camera_id, username, password)

        # Clear caches inside the mock context to ensure fresh clients
        from sitespy.config import get_settings as _gs
        from sitespy.data import _dynamodb_client as _ddb_client
        from sitespy.storage import _s3_client as _s3c

        _gs.cache_clear()
        _ddb_client.cache_clear()
        _s3c.cache_clear()

        event = _make_event(tenant_id, site_id, camera_id, body, username, password)

        # Freeze the timestamp so both calls produce the same canonical key
        with patch("sitespy.handlers.ingest.datetime") as mock_dt:
            from datetime import datetime

            class FakeDatetime:
                @staticmethod
                def now(tz=None):
                    return datetime(2025, 6, 15, 14, 0, 0, tzinfo=UTC)

            mock_dt.now = FakeDatetime.now

            # First ingest
            r1 = _handle(event, "corr-1")
            assert r1["statusCode"] == 201

            # Second ingest — same event, same frozen timestamp
            r2 = _handle(event, "corr-2")
            assert r2["statusCode"] == 201

        resp1 = json.loads(r1["body"])
        resp2 = json.loads(r2["body"])
        s3_key = resp1["key"]
        assert resp1["key"] == resp2["key"]

        # Exactly one current S3 version
        versions = s3.list_object_versions(Bucket="test-snapshots-bucket", Prefix=s3_key)
        current_versions = [v for v in versions.get("Versions", []) if v["IsLatest"]]
        assert len(current_versions) == 1

        # Exactly one IMG# item
        snapshot_ts = resp1["timestamp"]
        img_sk = f"IMG#{site_id}#{camera_id}#{snapshot_ts}"
        scan = ddb.query(
            TableName="test-data-table",
            KeyConditionExpression="PK = :pk AND SK = :sk",
            ExpressionAttributeValues={
                ":pk": {"S": f"TENANT#{tenant_id}"},
                ":sk": {"S": img_sk},
            },
        )
        assert scan["Count"] == 1
