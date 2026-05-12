"""P6 Error Envelope Closure property test.

Property 6: Error Envelope Closure
Validates: Requirements 2.10, 3.1, 8.1, 8.2, 8.3

For every non-2xx response:
- status ∈ {400, 401, 500}
- body parses as JSON
- error ∈ {"BAD_REQUEST", "UNAUTHORIZED", "INTERNAL_ERROR"}
- X-Correlation-Id header present
- every 401 response body is byte-identical
"""

from __future__ import annotations

import base64
import json
import os
from unittest.mock import patch

import boto3
from botocore.exceptions import ClientError
from moto import mock_aws

from sitespy.errors import ApiError
from sitespy.handlers.ingest import _handle, resolve_correlation_id
from sitespy.http import error_response, unhandled_error_response

_VALID_ERROR_KEYS = {"BAD_REQUEST", "UNAUTHORIZED", "INTERNAL_ERROR"}
_VALID_STATUS_CODES = {400, 401, 500}


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
    """Seed a camera with token-based auth."""
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


def _make_event(
    token="tk_validtoken1234567890abcdefghijklmnopqr",
    body=None,
    correlation_id="test-corr-id",
    omit_token=False,
    invalid_token=False,
):
    """Build an ingest event using token-based URL path auth."""
    if body is None:
        body = b"\xff\xd8\xff\xe0" + b"\x00" * 100

    path_token = "" if omit_token else ("bad!" if invalid_token else token)

    return {
        "httpMethod": "POST",
        "path": f"/v1/ingest/{path_token}",
        "pathParameters": {"token": path_token} if not omit_token else {},
        "headers": {
            "Content-Type": "image/jpeg",
            "X-Correlation-Id": correlation_id,
        },
        "queryStringParameters": None,
        "body": base64.b64encode(body).decode(),
        "isBase64Encoded": True,
    }


def _invoke(event):
    """Call _handle and convert ApiError to a response dict (mirrors the outer handler)."""
    corr_id = resolve_correlation_id(event)
    try:
        return _handle(event, corr_id)
    except ApiError as exc:
        return error_response(exc, corr_id)
    except Exception:
        return unhandled_error_response(corr_id)


def _assert_error_envelope(result, expected_status=None):
    """Assert the canonical error envelope invariants."""
    status = result["statusCode"]
    assert status in _VALID_STATUS_CODES, f"Unexpected status {status}"
    if expected_status is not None:
        assert status == expected_status

    body = json.loads(result["body"])
    assert "error" in body
    assert body["error"] in _VALID_ERROR_KEYS
    assert "message" in body

    headers = result.get("headers", {})
    assert "X-Correlation-Id" in headers

    return body


# ---------------------------------------------------------------------------
# 400 cases
# ---------------------------------------------------------------------------


def test_p6_empty_body():
    _set_env()
    with mock_aws():
        _s3, ddb = _setup_aws()
        token = "tk_validtoken1234567890abcdefghijklmnopqr"
        _seed_camera(ddb, "acme", "site_01", "cam_01", token)
        event = _make_event(token=token)
        event["body"] = ""
        event["isBase64Encoded"] = False
        result = _invoke(event)
    _assert_error_envelope(result, 400)


def test_p6_bad_magic_bytes():
    _set_env()
    with mock_aws():
        _s3, ddb = _setup_aws()
        token = "tk_validtoken1234567890abcdefghijklmnopqr"
        _seed_camera(ddb, "acme", "site_01", "cam_01", token)
        bad_body = b"\x00\x01\x02\x03" + b"\x00" * 100
        result = _invoke(_make_event(token=token, body=bad_body))
    _assert_error_envelope(result, 400)


def test_p6_body_too_large():
    _set_env()
    with mock_aws():
        _s3, ddb = _setup_aws()
        token = "tk_validtoken1234567890abcdefghijklmnopqr"
        _seed_camera(ddb, "acme", "site_01", "cam_01", token)
        big_body = b"\xff\xd8\xff\xe0" + b"\x00" * (10 * 1024 * 1024 + 1)
        result = _invoke(_make_event(token=token, body=big_body))
    _assert_error_envelope(result, 400)


# ---------------------------------------------------------------------------
# 401 cases
# ---------------------------------------------------------------------------


def test_p6_missing_token():
    _set_env()
    with mock_aws():
        _setup_aws()
        event = _make_event(omit_token=True)
        result = _invoke(event)
    _assert_error_envelope(result, 401)


def test_p6_invalid_token_format():
    _set_env()
    with mock_aws():
        _setup_aws()
        event = _make_event(invalid_token=True)
        result = _invoke(event)
    _assert_error_envelope(result, 401)


def test_p6_no_camera_for_token():
    _set_env()
    with mock_aws():
        _setup_aws()
        # No camera seeded → token lookup returns None → 401
        result = _invoke(_make_event())
    _assert_error_envelope(result, 401)


# ---------------------------------------------------------------------------
# 500 cases
# ---------------------------------------------------------------------------


def test_p6_s3_failure():
    _set_env()
    with mock_aws():
        _s3, ddb = _setup_aws()
        token = "tk_validtoken1234567890abcdefghijklmnopqr"
        _seed_camera(ddb, "acme", "site_01", "cam_01", token)

        import sitespy.storage as storage_module

        def fail_put(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "S3 down"}},
                "PutObject",
            )

        with patch.object(storage_module, "put_snapshot", side_effect=fail_put):
            result = _invoke(_make_event(token=token))
    _assert_error_envelope(result, 500)


# ---------------------------------------------------------------------------
# Uniform 401 surface (Requirement 2.10)
# ---------------------------------------------------------------------------


def test_p6_all_401_bodies_are_identical():
    """Every 401 cause returns a byte-identical response body."""
    _set_env()
    with mock_aws():
        _setup_aws()

        # Collect 401 bodies from different causes
        bodies = []

        # Missing/empty token
        r = _invoke(_make_event(omit_token=True))
        assert r["statusCode"] == 401
        bodies.append(r["body"])

        # Invalid token format
        r = _invoke(_make_event(invalid_token=True))
        assert r["statusCode"] == 401
        bodies.append(r["body"])

        # Valid format but no camera exists for this token
        r = _invoke(_make_event())
        assert r["statusCode"] == 401
        bodies.append(r["body"])

        # All bodies must be identical
        assert len(set(bodies)) == 1, f"401 bodies differ: {bodies}"

        # No 404 ever emitted
        assert all(json.loads(b)["error"] != "NOT_FOUND" for b in bodies)
