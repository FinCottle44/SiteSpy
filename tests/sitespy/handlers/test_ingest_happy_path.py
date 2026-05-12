"""P1 Integrity — Transitive Hash Equality property test.

Property 1: Integrity — Transitive Hash Equality
Validates: Requirements 4.1, 4.2, 4.3, 5.3, 5.4, 5.5, 5.6, 6.1, 6.2,
           9.1, 9.2, 9.3, 9.4, 9.5

For any successful ingest of a valid JPEG body B:
    sha256_hex(B)
      == response_body.sha256
      == IMG_Record.sha256
      == s3_object.metadata["sha256"]
      == sha256_hex(s3_get_object(Canonical_Key).body)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os

import boto3
from hypothesis import given, settings
from hypothesis import strategies as st
from moto import mock_aws

from sitespy.data import _dynamodb_client
from sitespy.handlers.ingest import _handle
from sitespy.storage import _s3_client

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_ID_ST = st.from_regex(r"^[a-z0-9_]{1,64}$", fullmatch=True)
_TOKEN_SUFFIX_ST = st.from_regex(r"^[A-Za-z0-9_-]{40}$", fullmatch=True)
# JPEG bodies: 4 B to 1 KiB, magic-byte prefixed (kept small for speed)
_BODY_ST = st.integers(min_value=4, max_value=1024).map(
    lambda n: b"\xff\xd8\xff\xe0" + b"\x00" * (n - 4)
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _setup_aws():
    """Create the moto S3 bucket and DynamoDB table."""
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


def _seed_camera(ddb, tenant_id, site_id, camera_id, ingest_token, retention_years=5):
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
            "retention_years": {"N": str(retention_years)},
        },
    )


def _make_event(ingest_token, body):
    """Build an ingest event using token-based URL path auth."""
    return {
        "httpMethod": "POST",
        "path": f"/v1/ingest/{ingest_token}",
        "pathParameters": {"token": ingest_token},
        "headers": {
            "Content-Type": "image/jpeg",
        },
        "queryStringParameters": None,
        "body": base64.b64encode(body).decode(),
        "isBase64Encoded": True,
    }


# ---------------------------------------------------------------------------
# P1 property test
# ---------------------------------------------------------------------------


@given(
    tenant_id=_ID_ST,
    site_id=_ID_ST,
    camera_id=_ID_ST,
    token_suffix=_TOKEN_SUFFIX_ST,
    body=_BODY_ST,
)
@settings(max_examples=20, deadline=None)
def test_p1_integrity_transitive_hash_equality(tenant_id, site_id, camera_id, token_suffix, body):
    """P1: four-way SHA-256 equality across response / IMG# / S3 metadata / S3 body."""
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

    ingest_token = f"tk_{token_suffix}"

    with mock_aws():
        s3, ddb = _setup_aws()
        _seed_camera(ddb, tenant_id, site_id, camera_id, ingest_token, retention_years=7)

        # Clear caches inside the mock context to ensure fresh clients
        from sitespy.config import get_settings as _gs
        from sitespy.data import _dynamodb_client as _ddb_client
        from sitespy.storage import _s3_client as _s3c

        _gs.cache_clear()
        _ddb_client.cache_clear()
        _s3c.cache_clear()

        event = _make_event(ingest_token, body)
        result = _handle(event, "test-correlation-id")

        assert result["statusCode"] == 201
        resp_body = json.loads(result["body"])

        # Expected SHA-256
        expected_sha256 = hashlib.sha256(body).hexdigest()

        # 1. Response body sha256
        assert resp_body["sha256"] == expected_sha256

        # 2. IMG# record sha256
        s3_key = resp_body["key"]
        snapshot_ts = resp_body["timestamp"]
        img_sk = f"IMG#{site_id}#{camera_id}#{snapshot_ts}"
        img_item = ddb.get_item(
            TableName="test-data-table",
            Key={
                "PK": {"S": f"TENANT#{tenant_id}"},
                "SK": {"S": img_sk},
            },
        ).get("Item")
        assert img_item is not None
        assert img_item["sha256"]["S"] == expected_sha256

        # 3. S3 metadata sha256
        head = s3.head_object(Bucket="test-snapshots-bucket", Key=s3_key)
        assert head["Metadata"]["sha256"] == expected_sha256

        # 4. S3 body sha256
        s3_obj = s3.get_object(Bucket="test-snapshots-bucket", Key=s3_key)
        s3_body = s3_obj["Body"].read()
        assert hashlib.sha256(s3_body).hexdigest() == expected_sha256

        # Additional assertions per spec
        assert img_item["size_bytes"]["N"] == str(len(body))
        assert img_item["s3_key"]["S"] == s3_key
        assert img_item["ingested_at"]["S"] == snapshot_ts
        assert img_item["content_type"]["S"] == "image/jpeg"

        # S3 tags
        tags_resp = s3.get_object_tagging(Bucket="test-snapshots-bucket", Key=s3_key)
        tags = {t["Key"]: t["Value"] for t in tags_resp["TagSet"]}
        assert tags["tenant_id"] == tenant_id
        assert tags["retention_years"] == "7"

        # Response shape
        assert resp_body["key"] == s3_key
        assert resp_body["timestamp"] == snapshot_ts
        assert resp_body["camera_id"] == camera_id
