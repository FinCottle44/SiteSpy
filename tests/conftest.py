"""Shared pytest fixtures and helpers for the SiteSpy test suite.

Covers:
- AWS environment setup (session-scoped, sets env vars before get_settings runs)
- Moto-backed S3 and DynamoDB fixtures
- DynamoDB row factories for camera and tenant items
- JPEG body helper and API Gateway event builder
"""

from __future__ import annotations

import base64
import os
from typing import Any

import bcrypt
import boto3
import pytest
from moto import mock_aws

# ---------------------------------------------------------------------------
# Session-scoped: environment variables
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def aws_env():
    """Set AWS / app environment variables before any code imports get_settings.

    Session-scoped so the env is stable for the entire test run.  The
    get_settings lru_cache is cleared after the env vars are applied so that
    any cached call from a previous import is invalidated.
    """
    env_vars = {
        "SNAPSHOTS_BUCKET": "test-snapshots-bucket",
        "DATA_TABLE": "test-data-table",
        "AWS_REGION": "eu-west-2",
        "AWS_DEFAULT_REGION": "eu-west-2",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "ENVIRONMENT": "test",
        "LOG_LEVEL": "INFO",
        "POWERTOOLS_SERVICE_NAME": "sitespy",
        "POWERTOOLS_METRICS_NAMESPACE": "SiteSpy",
    }

    original = {k: os.environ.get(k) for k in env_vars}
    os.environ.update(env_vars)

    # Clear the get_settings cache so it picks up the new env vars.
    try:
        from sitespy.config import get_settings

        get_settings.cache_clear()
    except (ImportError, AttributeError):
        pass

    yield

    # Restore original values (or remove if they weren't set before).
    for k, v in original.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# ---------------------------------------------------------------------------
# Function-scoped: moto S3
# ---------------------------------------------------------------------------


@pytest.fixture()
def moto_s3(aws_env):
    """Moto-backed S3 client with the test bucket pre-created (versioning on).

    Depends on ``aws_env`` to ensure credentials and region are set before
    boto3 is called.
    """
    with mock_aws():
        client = boto3.client("s3", region_name="eu-west-2")
        client.create_bucket(
            Bucket="test-snapshots-bucket",
            CreateBucketConfiguration={"LocationConstraint": "eu-west-2"},
        )
        client.put_bucket_versioning(
            Bucket="test-snapshots-bucket",
            VersioningConfiguration={"Status": "Enabled"},
        )
        yield client


# ---------------------------------------------------------------------------
# Function-scoped: moto DynamoDB
# ---------------------------------------------------------------------------


@pytest.fixture()
def moto_dynamodb(aws_env):
    """Moto-backed DynamoDB client with the test table pre-created.

    Table schema mirrors ``template.yaml``:
    - PK (S) / SK (S) primary key
    - GSI1 with GSI1PK (S) / GSI1SK (S), ProjectionType ALL
    - BillingMode: PAY_PER_REQUEST

    Depends on ``aws_env`` to ensure credentials and region are set before
    boto3 is called.
    """
    with mock_aws():
        client = boto3.client("dynamodb", region_name="eu-west-2")
        client.create_table(
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
        yield client


# ---------------------------------------------------------------------------
# Function-scoped: camera row factory
# ---------------------------------------------------------------------------


@pytest.fixture()
def camera_row_factory(moto_dynamodb):
    """Return a callable that writes a camera item to DynamoDB.

    Usage::

        username, password = camera_row_factory(
            "acme", "site_01", "cam_01",
            username="sitespy_cam_abc123",
            password="s3cr3t",
        )

    Parameters
    ----------
    tenant_id:        Tenant identifier.
    site_id:          Site identifier.
    camera_id:        Camera identifier.
    username:         Plaintext ingest username to store.
    password:         Plaintext ingest password to hash and store.
    cost:             bcrypt work factor (default 12).
    include_hash:     Whether to write ``ingest_password_hash`` (default True).
    include_username: Whether to write ``ingest_username`` (default True).

    Returns
    -------
    ``(username, password)`` — the plaintext credentials that were stored.
    """

    def _factory(
        tenant_id: str,
        site_id: str,
        camera_id: str,
        *,
        username: str,
        password: str,
        cost: int = 12,
        include_hash: bool = True,
        include_username: bool = True,
    ) -> tuple[str, str]:
        item: dict[str, Any] = {
            "PK": {"S": f"TENANT#{tenant_id}"},
            "SK": {"S": f"SITE#{site_id}#CAM#{camera_id}"},
        }

        if include_username:
            item["ingest_username"] = {"S": username}

        if include_hash:
            hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=cost))
            item["ingest_password_hash"] = {"S": hashed.decode()}

        moto_dynamodb.put_item(TableName="test-data-table", Item=item)
        return (username, password)

    return _factory


# ---------------------------------------------------------------------------
# Function-scoped: tenant row factory
# ---------------------------------------------------------------------------


@pytest.fixture()
def tenant_row_factory(moto_dynamodb):
    """Return a callable that writes a tenant item to DynamoDB.

    Usage::

        tenant_row_factory("acme", retention_years=7)

    Parameters
    ----------
    tenant_id:       Tenant identifier.
    retention_years: Optional retention value; omitted from the item when None.
    """

    def _factory(tenant_id: str, *, retention_years: int | None = None) -> None:
        item: dict[str, Any] = {
            "PK": {"S": f"TENANT#{tenant_id}"},
            "SK": {"S": f"TENANT#{tenant_id}"},
        }

        if retention_years is not None:
            item["retention_years"] = {"N": str(retention_years)}

        moto_dynamodb.put_item(TableName="test-data-table", Item=item)

    return _factory


# ---------------------------------------------------------------------------
# Plain helpers (not fixtures)
# ---------------------------------------------------------------------------


def jpeg_body(size: int = 1024) -> bytes:
    """Return a minimal synthetic JPEG body of the requested size.

    The first four bytes are the JPEG SOI + APP0 magic (``FF D8 FF E0``);
    the remainder is zero-padded.  This satisfies the magic-byte check in the
    ingest handler without requiring a real image file.

    Args:
        size: Total byte length of the returned body (default 1024).

    Returns:
        Bytes starting with ``\\xff\\xd8\\xff\\xe0`` followed by zero padding.
    """
    return b"\xff\xd8\xff\xe0" + b"\x00" * (size - 4)


def ingest_event(
    tenant_id: str,
    site_id: str,
    camera_id: str,
    *,
    body: bytes,
    username: str,
    password: str,
    correlation_id: str | None = None,
    content_type: str = "image/jpeg",
) -> dict:
    """Build an API Gateway REST proxy event dict for the ingest endpoint.

    Args:
        tenant_id:      Value for the ``X-Tenant-ID`` header.
        site_id:        Value for the ``X-Site-ID`` header.
        camera_id:      Value for the ``cameraID`` query string parameter.
        body:           Raw request body bytes; will be base64-encoded.
        username:       HTTP Basic Auth username.
        password:       HTTP Basic Auth password.
        correlation_id: Optional value for the ``X-Correlation-Id`` header.
                        Omitted from the headers dict when ``None``.
        content_type:   ``Content-Type`` header value (default ``image/jpeg``).

    Returns:
        A dict matching the API Gateway REST proxy event format, with
        ``isBase64Encoded`` set to ``True``.
    """
    credentials = base64.b64encode(f"{username}:{password}".encode()).decode()

    headers: dict[str, str] = {
        "Authorization": f"Basic {credentials}",
        "X-Tenant-ID": tenant_id,
        "X-Site-ID": site_id,
        "Content-Type": content_type,
    }

    if correlation_id is not None:
        headers["X-Correlation-Id"] = correlation_id

    return {
        "httpMethod": "POST",
        "path": "/v1/ingest",
        "headers": headers,
        "multiValueHeaders": {k: [v] for k, v in headers.items()},
        "queryStringParameters": {"cameraID": camera_id},
        "multiValueQueryStringParameters": {"cameraID": [camera_id]},
        "pathParameters": None,
        "stageVariables": None,
        "requestContext": {
            "resourcePath": "/v1/ingest",
            "httpMethod": "POST",
            "stage": "prod",
        },
        "body": base64.b64encode(body).decode(),
        "isBase64Encoded": True,
    }
