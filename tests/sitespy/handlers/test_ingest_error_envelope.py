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

import bcrypt
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


def _seed_camera(ddb, tenant_id, site_id, camera_id, username, password, cost=12):
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=cost))
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


def _make_event(
    tenant_id="acme",
    site_id="site_01",
    camera_id="cam_01",
    username="sitespy_cam_test",
    password="testpass",
    body=None,
    content_type="image/jpeg",
    correlation_id="test-corr-id",
    omit_tenant=False,
    omit_site=False,
    omit_camera=False,
    omit_auth=False,
):
    if body is None:
        body = b"\xff\xd8\xff\xe0" + b"\x00" * 100
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
    headers = {
        "Content-Type": content_type,
        "X-Correlation-Id": correlation_id,
    }
    if not omit_auth:
        headers["Authorization"] = f"Basic {credentials}"
    if not omit_tenant:
        headers["X-Tenant-ID"] = tenant_id
    if not omit_site:
        headers["X-Site-ID"] = site_id

    qs = {}
    if not omit_camera:
        qs["cameraID"] = camera_id

    return {
        "httpMethod": "POST",
        "path": "/v1/ingest",
        "headers": headers,
        "queryStringParameters": qs,
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


def test_p6_missing_tenant_id():
    _set_env()
    with mock_aws():
        _setup_aws()
        result = _invoke(_make_event(omit_tenant=True))
    _assert_error_envelope(result, 400)


def test_p6_missing_site_id():
    _set_env()
    with mock_aws():
        _setup_aws()
        result = _invoke(_make_event(omit_site=True))
    _assert_error_envelope(result, 400)


def test_p6_missing_camera_id():
    _set_env()
    with mock_aws():
        _setup_aws()
        result = _invoke(_make_event(omit_camera=True))
    _assert_error_envelope(result, 400)


def test_p6_invalid_tenant_id_regex():
    _set_env()
    with mock_aws():
        _setup_aws()
        result = _invoke(_make_event(tenant_id="INVALID-UPPER"))
    _assert_error_envelope(result, 400)


def test_p6_empty_body():
    _set_env()
    with mock_aws():
        _setup_aws()
        event = _make_event()
        event["body"] = ""
        event["isBase64Encoded"] = False
        result = _invoke(event)
    _assert_error_envelope(result, 400)


def test_p6_bad_magic_bytes():
    _set_env()
    with mock_aws():
        _setup_aws()
        bad_body = b"\x00\x01\x02\x03" + b"\x00" * 100
        result = _invoke(_make_event(body=bad_body))
    _assert_error_envelope(result, 400)


def test_p6_body_too_large():
    _set_env()
    with mock_aws():
        _setup_aws()
        big_body = b"\xff\xd8\xff\xe0" + b"\x00" * (10 * 1024 * 1024 + 1)
        result = _invoke(_make_event(body=big_body))
    _assert_error_envelope(result, 400)


def test_p6_wrong_content_type():
    _set_env()
    with mock_aws():
        _setup_aws()
        result = _invoke(_make_event(content_type="application/octet-stream"))
    _assert_error_envelope(result, 400)


# ---------------------------------------------------------------------------
# 401 cases
# ---------------------------------------------------------------------------


def test_p6_missing_auth_header():
    _set_env()
    with mock_aws():
        _setup_aws()
        result = _invoke(_make_event(omit_auth=True))
    _assert_error_envelope(result, 401)


def test_p6_no_camera_item():
    _set_env()
    with mock_aws():
        _setup_aws()
        # No camera row seeded → 401
        result = _invoke(_make_event())
    _assert_error_envelope(result, 401)


def test_p6_wrong_password():
    _set_env()
    with mock_aws():
        _s3, ddb = _setup_aws()
        _seed_camera(ddb, "acme", "site_01", "cam_01", "sitespy_cam_test", "correctpass")
        result = _invoke(_make_event(password="wrongpass"))
    _assert_error_envelope(result, 401)


def test_p6_hash_cost_too_low():
    _set_env()
    with mock_aws():
        _s3, ddb = _setup_aws()
        # Manually craft a cost-10 hash string to trigger the cost guard
        real_hash = bcrypt.hashpw(b"testpass", bcrypt.gensalt(rounds=4)).decode()
        # Replace cost digits with "10" to simulate a cost-10 hash
        cost10_hash = real_hash[:4] + "10" + real_hash[6:]
        ddb.put_item(
            TableName="test-data-table",
            Item={
                "PK": {"S": "TENANT#acme"},
                "SK": {"S": "SITE#site_01#CAM#cam_01"},
                "ingest_username": {"S": "sitespy_cam_test"},
                "ingest_password_hash": {"S": cost10_hash},
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
        result = _invoke(_make_event())
    _assert_error_envelope(result, 401)


# ---------------------------------------------------------------------------
# 500 cases
# ---------------------------------------------------------------------------


def test_p6_s3_failure():
    _set_env()
    with mock_aws():
        _s3, ddb = _setup_aws()
        _seed_camera(ddb, "acme", "site_01", "cam_01", "sitespy_cam_test", "testpass")

        import sitespy.storage as storage_module

        def fail_put(*args, **kwargs):
            raise ClientError(
                {"Error": {"Code": "InternalServerError", "Message": "S3 down"}},
                "PutObject",
            )

        with patch.object(storage_module, "put_snapshot", side_effect=fail_put):
            result = _invoke(_make_event())
    _assert_error_envelope(result, 500)


# ---------------------------------------------------------------------------
# Uniform 401 surface (Requirement 2.10)
# ---------------------------------------------------------------------------


def test_p6_all_401_bodies_are_identical():
    """Every 401 cause returns a byte-identical response body."""
    _set_env()
    with mock_aws():
        _s3, ddb = _setup_aws()
        _seed_camera(ddb, "acme", "site_01", "cam_01", "sitespy_cam_test", "correctpass")

        # Collect 401 bodies from different causes
        bodies = []

        # Missing auth header
        r = _invoke(_make_event(omit_auth=True))
        assert r["statusCode"] == 401
        bodies.append(r["body"])

        # No camera item (different tenant)
        r = _invoke(_make_event(tenant_id="other_tenant"))
        assert r["statusCode"] == 401
        bodies.append(r["body"])

        # Wrong password
        r = _invoke(_make_event(password="wrongpass"))
        assert r["statusCode"] == 401
        bodies.append(r["body"])

        # All bodies must be identical
        assert len(set(bodies)) == 1, f"401 bodies differ: {bodies}"

        # No 404 ever emitted
        assert all(json.loads(b)["error"] != "NOT_FOUND" for b in bodies)
