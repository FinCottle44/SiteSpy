"""Token and body validation partition tests.

Requirements validated: 1.2, 1.3, 1.4, 1.5, 1.6, 1.7

The ingest handler uses token-based auth via URL path (/v1/ingest/{token}).
Token format: ^tk_[A-Za-z0-9_-]{20,80}$
"""

from __future__ import annotations

import base64
import os

import boto3
import pytest
from moto import mock_aws

from sitespy.errors import ApiError
from sitespy.handlers.ingest import _handle, resolve_correlation_id
from sitespy.http import error_response, unhandled_error_response

_MAX_BODY = 10 * 1024 * 1024  # 10 MiB

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
    """Seed a camera with token-based GSI1 index."""
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


def _invoke(event):
    corr_id = resolve_correlation_id(event)
    try:
        return _handle(event, corr_id)
    except ApiError as exc:
        return error_response(exc, corr_id)
    except Exception:
        return unhandled_error_response(corr_id)


def _make_valid_event(token=_VALID_TOKEN, body=None):
    """Build a valid ingest event with token-based auth."""
    if body is None:
        body = b"\xff\xd8\xff\xe0" + b"\x00" * 100
    return {
        "httpMethod": "POST",
        "path": f"/v1/ingest/{token}",
        "pathParameters": {"token": token},
        "headers": {
            "Content-Type": "image/jpeg",
        },
        "queryStringParameters": None,
        "body": base64.b64encode(body).decode(),
        "isBase64Encoded": True,
    }


# ---------------------------------------------------------------------------
# Token validation → 401
# ---------------------------------------------------------------------------


def test_missing_token():
    """Missing token in path → 401."""
    _set_env()
    event = _make_valid_event(token="")
    result = _invoke(event)
    assert result["statusCode"] == 401


def test_invalid_token_format_no_prefix():
    """Token without tk_ prefix → 401."""
    _set_env()
    event = _make_valid_event(token="noprefixtoken1234567890abcdefgh")
    result = _invoke(event)
    assert result["statusCode"] == 401


def test_invalid_token_format_too_short():
    """Token with tk_ prefix but too short suffix → 401."""
    _set_env()
    event = _make_valid_event(token="tk_short")
    result = _invoke(event)
    assert result["statusCode"] == 401


def test_invalid_token_format_special_chars():
    """Token with invalid characters → 401."""
    _set_env()
    event = _make_valid_event(token="tk_invalid!@#$%^&*()token12345")
    result = _invoke(event)
    assert result["statusCode"] == 401


def test_valid_token_no_camera_found():
    """Valid token format but no camera in DB → 401."""
    _set_env()
    with mock_aws():
        _setup_aws()
        result = _invoke(_make_valid_event())
    assert result["statusCode"] == 401


# ---------------------------------------------------------------------------
# Body validation → 400 (requires valid auth first)
# ---------------------------------------------------------------------------


def test_empty_body():
    _set_env()
    with mock_aws():
        _s3, ddb = _setup_aws()
        _seed_camera(ddb, "acme", "site_01", "cam_01", _VALID_TOKEN)
        event = _make_valid_event()
        event["body"] = ""
        event["isBase64Encoded"] = False
        result = _invoke(event)
    assert result["statusCode"] == 400


def test_bad_magic_bytes():
    _set_env()
    with mock_aws():
        _s3, ddb = _setup_aws()
        _seed_camera(ddb, "acme", "site_01", "cam_01", _VALID_TOKEN)
        bad_body = b"\x00\x01\x02\x03" + b"\x00" * 100
        result = _invoke(_make_valid_event(body=bad_body))
    assert result["statusCode"] == 400


def test_body_at_max_minus_1_is_accepted():
    """Body at 10 MiB - 1 should pass body size validation."""
    _set_env()
    with mock_aws():
        _s3, ddb = _setup_aws()
        _seed_camera(ddb, "acme", "site_01", "cam_01", _VALID_TOKEN)
        body = b"\xff\xd8\xff\xe0" + b"\x00" * (_MAX_BODY - 1 - 4)
        result = _invoke(_make_valid_event(body=body))
    # Should succeed (201) since auth and body are valid
    assert result["statusCode"] == 201


def test_body_at_max_is_accepted():
    """Body at exactly 10 MiB should pass body size validation."""
    _set_env()
    with mock_aws():
        _s3, ddb = _setup_aws()
        _seed_camera(ddb, "acme", "site_01", "cam_01", _VALID_TOKEN)
        body = b"\xff\xd8\xff\xe0" + b"\x00" * (_MAX_BODY - 4)
        result = _invoke(_make_valid_event(body=body))
    assert result["statusCode"] == 201


def test_body_over_max_is_rejected():
    """Body at 10 MiB + 1 must return 400."""
    _set_env()
    with mock_aws():
        _s3, ddb = _setup_aws()
        _seed_camera(ddb, "acme", "site_01", "cam_01", _VALID_TOKEN)
        body = b"\xff\xd8\xff\xe0" + b"\x00" * (_MAX_BODY + 1 - 4)
        result = _invoke(_make_valid_event(body=body))
    assert result["statusCode"] == 400
