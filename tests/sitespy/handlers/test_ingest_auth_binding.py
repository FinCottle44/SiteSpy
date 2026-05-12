"""P4 Authentication Binding — Structural Invariant property test.

Property 4: Authentication Binding
Validates: Requirements 2.3, 3.2, 3.3

For any ingest request, the token-based authentication resolves the camera
via GSI1 lookup using exactly the token from the URL path. The resolved
camera's tenant_id, site_id, and camera_id are used for all subsequent
operations.
"""

from __future__ import annotations

import base64
import os
from unittest.mock import patch

import boto3
from hypothesis import given, settings
from hypothesis import strategies as st
from moto import mock_aws

from sitespy.data import _dynamodb_client
from sitespy.handlers.ingest import _handle
from sitespy.storage import _s3_client

_ID_ST = st.from_regex(r"^[a-z0-9_]{1,64}$", fullmatch=True)
_TOKEN_SUFFIX_ST = st.from_regex(r"^[A-Za-z0-9_-]{40}$", fullmatch=True)


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


def _seed_camera(ddb, tenant_id, site_id, camera_id, ingest_token):
    """Seed a camera record with token-based GSI1 index."""
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


def _make_event(ingest_token, body=None):
    """Build an ingest event using token-based URL path auth."""
    if body is None:
        body = b"\xff\xd8\xff\xe0" + b"\x00" * 100
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


@given(
    tenant_id=_ID_ST,
    site_id=_ID_ST,
    camera_id=_ID_ST,
    token_suffix=_TOKEN_SUFFIX_ST,
)
@settings(max_examples=20, deadline=None)
def test_p4_auth_binding_get_camera_by_token(tenant_id, site_id, camera_id, token_suffix):
    """P4: token-based auth resolves the camera via get_camera_by_token with the request token."""
    _set_env()

    ingest_token = f"tk_{token_suffix}"

    with mock_aws():
        _s3, ddb = _setup_aws()
        _seed_camera(ddb, tenant_id, site_id, camera_id, ingest_token)

        event = _make_event(ingest_token)

        # Spy on get_camera_by_token calls
        lookup_calls = []

        import sitespy.data as data_module

        original_get_camera_by_token = data_module.get_camera_by_token

        def spy_get_camera_by_token(token):
            lookup_calls.append(token)
            return original_get_camera_by_token(token)

        import contextlib

        with (
            patch.object(data_module, "get_camera_by_token", side_effect=spy_get_camera_by_token),
            contextlib.suppress(Exception),
        ):
            _handle(event, "corr-p4")

        # Exactly one get_camera_by_token call with the request's token
        assert len(lookup_calls) == 1
        assert lookup_calls[0] == ingest_token
