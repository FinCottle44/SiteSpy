"""P7 Secret Redaction property test.

Property 7: Secret Redaction
Validates: Requirement 10.5

No log record contains the raw ingest token value. The token is the
authentication secret in the token-based auth model.
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

_VALID_TOKEN = "tk_supersecrettoken1234567890abcdefghijk"


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


def _assert_no_secrets_in_logs(caplog_records, secrets):
    """Assert none of the secret strings appear in any application log record.

    Filters out botocore/boto3/moto infrastructure logs — those are not
    application logs and may legitimately contain request payloads.
    """
    app_records = [
        r for r in caplog_records if not r.name.startswith(("botocore", "boto3", "moto", "urllib3"))
    ]
    all_log_text = " ".join(
        str(r.getMessage()) + str(getattr(r, "extra", "")) + str(r.__dict__) for r in app_records
    )
    for secret in secrets:
        if secret:
            assert secret not in all_log_text, (
                f"Secret '{secret[:8]}...' found in application log output"
            )


def test_p7_redaction_happy_path(caplog):
    """P7: no token in logs on successful ingest."""
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

        with caplog.at_level(logging.DEBUG):
            result = _invoke(event)

        assert result["statusCode"] == 201

        secrets = [_VALID_TOKEN]
        _assert_no_secrets_in_logs(caplog.records, secrets)


def test_p7_redaction_auth_failure(caplog):
    """P7: no token in logs on auth failure (unknown token)."""
    _set_env()
    unknown_token = "tk_unknowntoken1234567890abcdefghijklmno"
    with mock_aws():
        _setup_aws()

        event = _make_event(unknown_token)

        with caplog.at_level(logging.DEBUG):
            result = _invoke(event)

        assert result["statusCode"] == 401

        secrets = [unknown_token]
        _assert_no_secrets_in_logs(caplog.records, secrets)


def test_p7_redaction_missing_token(caplog):
    """P7: no crash when token is empty/missing."""
    _set_env()
    with mock_aws():
        _setup_aws()

        event = _make_event("")

        with caplog.at_level(logging.DEBUG):
            result = _invoke(event)

        assert result["statusCode"] == 401
        body = json.loads(result["body"])
        assert body["error"] == "UNAUTHORIZED"
