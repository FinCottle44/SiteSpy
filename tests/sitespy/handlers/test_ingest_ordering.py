"""P5 Ordering — No Orphan IMG Records property test.

Property 5: Ordering — No Orphan IMG Records
Validates: Requirements 5.7, 6.3, 6.4

S3 write always precedes the IMG# DynamoDB write.
On S3 failure: IMG# write is skipped.
On IMG# failure after S3 success: S3 object remains, 500 returned.
"""

from __future__ import annotations

import base64
import json
import os
from unittest.mock import patch

import bcrypt
import boto3
from botocore.exceptions import ClientError
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


def _make_event(tenant_id, site_id, camera_id, username, password):
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


def test_p5_img_write_fails_after_s3_success():
    """P5: IMG# write failure after S3 success → 500, S3 object present, no IMG# item."""
    _set_env()
    with mock_aws():
        s3, ddb = _setup_aws()

        tenant_id, site_id, camera_id = "acme", "site_01", "cam_01"
        username, password = "sitespy_cam_ord", "ordpassword"
        _seed_camera(ddb, tenant_id, site_id, camera_id, username, password)

        event = _make_event(tenant_id, site_id, camera_id, username, password)

        import sitespy.data as data_module

        def fail_put_img(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "Injected failure"}},
                "PutItem",
            )

        with patch.object(data_module, "put_img_record", side_effect=fail_put_img):
            result = _invoke(event)

        assert result["statusCode"] == 500
        resp = json.loads(result["body"])
        assert resp["error"] == "INTERNAL_ERROR"

        # S3 object IS present (S3 write succeeded before the IMG# failure)
        # We can't know the exact timestamp, so list objects
        objs = s3.list_objects_v2(
            Bucket="test-snapshots-bucket", Prefix=f"{tenant_id}/{site_id}/{camera_id}/"
        )
        assert objs.get("KeyCount", 0) >= 1

        # No IMG# items in DynamoDB
        scan = ddb.scan(
            TableName="test-data-table",
            FilterExpression="begins_with(SK, :prefix)",
            ExpressionAttributeValues={":prefix": {"S": "IMG#"}},
        )
        assert scan["Count"] == 0


def test_p5_s3_write_fails_no_img_record():
    """P5: S3 write failure → 500, zero put_item calls for IMG#."""
    _set_env()
    with mock_aws():
        _s3, ddb = _setup_aws()

        tenant_id, site_id, camera_id = "acme", "site_01", "cam_01"
        username, password = "sitespy_cam_ord2", "ordpassword2"
        _seed_camera(ddb, tenant_id, site_id, camera_id, username, password)

        event = _make_event(tenant_id, site_id, camera_id, username, password)

        import sitespy.data as data_module
        import sitespy.storage as storage_module

        put_img_calls = []
        original_put_img = data_module.put_img_record

        def track_put_img(*args, **kwargs):
            put_img_calls.append((args, kwargs))
            return original_put_img(*args, **kwargs)

        def fail_put_snapshot(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "S3 failure"}},
                "PutObject",
            )

        with (
            patch.object(storage_module, "put_snapshot", side_effect=fail_put_snapshot),
            patch.object(data_module, "put_img_record", side_effect=track_put_img),
        ):
            result = _invoke(event)

        assert result["statusCode"] == 500
        # put_img_record was never called
        assert len(put_img_calls) == 0
