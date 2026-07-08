"""Example-based unit tests for the refactored timelapse status/retrieve handler.

Handler under test: sitespy.handlers.timelapse_get (GET /v1/timelapse-jobs/{job_id})

These example-based tests cover the behaviors introduced when
``_build_status_body`` was refactored to delegate the ``complete`` branch to the
shared ``sitespy.timelapse_download.build_download_fields`` helper:

- Complete-with-artifact returns a freshly presigned ``download_url`` and
  ``expires_in`` (via the shared helper).
- Complete-with-missing-artifact returns ``artifact_available: false`` and omits
  both ``download_url`` and ``expires_in`` (never a broken link).
- The response exposes ``requested_by`` and ``completed_at`` (null when absent).

Feature: timelapse-job-listing
Validates: Requirements 5.3, 5.4, 6.4
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Environment setup (must happen before importing the handler / config)
# ---------------------------------------------------------------------------

os.environ.setdefault("SNAPSHOTS_BUCKET", "test-snapshots-bucket")
os.environ.setdefault("DATA_TABLE", "test-data-table")
os.environ.setdefault("AWS_REGION", "eu-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-west-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("LOG_LEVEL", "INFO")

import boto3  # noqa: E402
import pytest  # noqa: E402
from moto import mock_aws  # noqa: E402

from sitespy import data, storage  # noqa: E402
from sitespy.config import get_settings  # noqa: E402
from sitespy.data import _dynamodb_client  # noqa: E402
from sitespy.handlers.timelapse_get import handler  # noqa: E402
from sitespy.timelapse import (  # noqa: E402
    STATUS_COMPLETE,
    STATUS_PROCESSING,
    STATUS_QUEUED,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TABLE_NAME = "test-data-table"
_BUCKET_NAME = "test-snapshots-bucket"
_REGION = "eu-west-2"

_TENANT_ID = "tenantA"
_JOB_ID = "job123"
_SITE_ID = "siteA"
_CAMERA_ID = "cam1"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env_and_cache():
    """Ensure env vars are set and cached clients are reset around each test.

    Env vars are hard-assigned (not ``setdefault``) so a prior test module that
    left a different ``SNAPSHOTS_BUCKET`` in the environment cannot leak into
    these tests, and ``get_settings`` is cache-cleared so the settings snapshot
    is rebuilt from this module's env values regardless of test ordering.
    """
    os.environ["DATA_TABLE"] = _TABLE_NAME
    os.environ["SNAPSHOTS_BUCKET"] = _BUCKET_NAME
    os.environ["AWS_REGION"] = _REGION
    _dynamodb_client.cache_clear()
    storage._s3_client.cache_clear()
    get_settings.cache_clear()
    yield
    _dynamodb_client.cache_clear()
    storage._s3_client.cache_clear()
    get_settings.cache_clear()


def _create_table(client) -> None:
    """Create the test DynamoDB table matching the project single-table schema."""
    client.create_table(
        TableName=_TABLE_NAME,
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


def _create_bucket(s3_client) -> None:
    """Create the snapshots bucket so presigned-URL generation has a target."""
    s3_client.create_bucket(
        Bucket=_BUCKET_NAME,
        CreateBucketConfiguration={"LocationConstraint": _REGION},
    )


def _make_event(
    job_id: str,
    *,
    groups: str,
    tenant_id: str,
    site_access: str = "",
) -> dict[str, Any]:
    """Build a minimal API Gateway proxy event for the timelapse_get handler."""
    return {
        "httpMethod": "GET",
        "path": f"/v1/timelapse-jobs/{job_id}",
        "pathParameters": {"job_id": job_id},
        "queryStringParameters": None,
        "headers": {"X-Correlation-Id": "test-corr-id"},
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:groups": groups,
                    "custom:tenant_id": tenant_id,
                    "custom:site_access": site_access,
                }
            }
        },
    }


def _seed_queued_job(*, requested_by: str | None = None) -> None:
    """Seed a queued JOB# record (optionally with requested_by)."""
    data.put_timelapse_job(
        tenant_id=_TENANT_ID,
        job_id=_JOB_ID,
        site_id=_SITE_ID,
        camera_id=_CAMERA_ID,
        start_ts="2025-06-15T14:00:00Z",
        end_ts="2025-06-15T15:00:00Z",
        length_seconds=60,
        fps=24,
        status=STATUS_QUEUED,
        created_at="2025-06-15T13:00:00Z",
        ttl=2_000_000_000,
        requested_by=requested_by,
    )


def _mark_complete(artifact_key: str) -> None:
    """Transition the seeded job to complete, stamping completed_at once."""
    data.update_timelapse_job_status(
        tenant_id=_TENANT_ID,
        job_id=_JOB_ID,
        status=STATUS_COMPLETE,
        artifact_key=artifact_key,
        set_completed_at=True,
    )


# ---------------------------------------------------------------------------
# Complete-with-artifact -> download_url + expires_in via the shared helper
# Validates: Requirements 5.4
# ---------------------------------------------------------------------------


def test_complete_with_artifact_returns_download_url_and_expires_in() -> None:
    """A complete job whose Artifact exists gets a presigned URL + expires_in."""
    artifact_key = storage.build_timelapse_key(
        _TENANT_ID, _SITE_ID, _CAMERA_ID, _JOB_ID
    )

    with mock_aws():
        _dynamodb_client.cache_clear()
        storage._s3_client.cache_clear()
        _create_table(boto3.client("dynamodb", region_name=_REGION))
        s3 = boto3.client("s3", region_name=_REGION)
        _create_bucket(s3)

        _seed_queued_job()
        _mark_complete(artifact_key)
        # The Artifact actually exists in S3.
        s3.put_object(Bucket=_BUCKET_NAME, Key=artifact_key, Body=b"fake-mp4")

        event = _make_event(_JOB_ID, groups="TenantAdmins", tenant_id=_TENANT_ID)
        result = handler(event, MagicMock())

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["status"] == STATUS_COMPLETE
    assert isinstance(body["download_url"], str) and body["download_url"]
    assert body["expires_in"] == get_settings().artifact_presign_ttl
    assert body["expires_in"] > 0
    assert "artifact_available" not in body


# ---------------------------------------------------------------------------
# Complete-with-missing-artifact -> artifact_available: false, no URL
# Validates: Requirements 5.3, 5.4
# ---------------------------------------------------------------------------


def test_complete_with_missing_artifact_returns_availability_indicator() -> None:
    """A complete job whose Artifact is gone yields an availability indicator."""
    artifact_key = storage.build_timelapse_key(
        _TENANT_ID, _SITE_ID, _CAMERA_ID, _JOB_ID
    )

    with mock_aws():
        _dynamodb_client.cache_clear()
        storage._s3_client.cache_clear()
        _create_table(boto3.client("dynamodb", region_name=_REGION))
        _create_bucket(boto3.client("s3", region_name=_REGION))

        _seed_queued_job()
        _mark_complete(artifact_key)
        # NOTE: the Artifact is deliberately NOT uploaded to S3.

        event = _make_event(_JOB_ID, groups="TenantAdmins", tenant_id=_TENANT_ID)
        result = handler(event, MagicMock())

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["status"] == STATUS_COMPLETE
    assert body["artifact_available"] is False
    assert "download_url" not in body
    assert "expires_in" not in body


# ---------------------------------------------------------------------------
# Response exposes requested_by and completed_at
# Validates: Requirements 6.4
# ---------------------------------------------------------------------------


def test_response_exposes_requested_by_and_completed_at_when_present() -> None:
    """A complete job exposes the captured requested_by and stamped completed_at."""
    artifact_key = storage.build_timelapse_key(
        _TENANT_ID, _SITE_ID, _CAMERA_ID, _JOB_ID
    )

    with mock_aws():
        _dynamodb_client.cache_clear()
        storage._s3_client.cache_clear()
        _create_table(boto3.client("dynamodb", region_name=_REGION))
        s3 = boto3.client("s3", region_name=_REGION)
        _create_bucket(s3)

        _seed_queued_job(requested_by="user-sub-123")
        _mark_complete(artifact_key)
        s3.put_object(Bucket=_BUCKET_NAME, Key=artifact_key, Body=b"fake-mp4")

        event = _make_event(_JOB_ID, groups="TenantAdmins", tenant_id=_TENANT_ID)
        result = handler(event, MagicMock())

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["requested_by"] == "user-sub-123"
    # completed_at is stamped by the worker (set_completed_at) and exposed as an
    # ISO 8601 UTC timestamp for a complete job.
    assert isinstance(body["completed_at"], str)
    assert body["completed_at"].endswith("Z")


def test_requested_by_and_completed_at_null_when_absent() -> None:
    """A non-complete job with no requested_by exposes both fields as null."""
    with mock_aws():
        _dynamodb_client.cache_clear()
        storage._s3_client.cache_clear()
        _create_table(boto3.client("dynamodb", region_name=_REGION))
        _create_bucket(boto3.client("s3", region_name=_REGION))

        # Queued job, no requested_by captured, then moved to processing.
        _seed_queued_job(requested_by=None)
        data.update_timelapse_job_status(
            tenant_id=_TENANT_ID,
            job_id=_JOB_ID,
            status=STATUS_PROCESSING,
        )

        event = _make_event(_JOB_ID, groups="TenantAdmins", tenant_id=_TENANT_ID)
        result = handler(event, MagicMock())

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["status"] == STATUS_PROCESSING
    # requested_by null when no value captured; completed_at null unless complete.
    assert body["requested_by"] is None
    assert body["completed_at"] is None
