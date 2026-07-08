"""Example-based unit tests for the timelapse list handler error paths.

Handler under test: sitespy.handlers.timelapse_list (GET /v1/timelapse-jobs)

These example-based tests cover the 500 error paths of the list endpoint:

- A DynamoDB failure while paging the partition surfaces as a 500 ``ApiError``
  envelope with no ``jobs`` body (Requirement 8.3).
- A non-404 S3/DynamoDB error raised during the Artifact existence check for a
  ``complete`` job surfaces as a 500 ``ApiError`` envelope with no ``jobs`` body
  (Requirement 8.3).

Both assert the canonical ``ApiError`` envelope (``{"error", "message"}``) and
that no ``jobs`` key is present in the 500 body.

Feature: timelapse-job-listing
Validates: Requirements 2.1, 8.3
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import MagicMock, patch

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
from botocore.exceptions import ClientError  # noqa: E402
from moto import mock_aws  # noqa: E402

from sitespy import data, storage  # noqa: E402
from sitespy.config import get_settings  # noqa: E402
from sitespy.data import _dynamodb_client  # noqa: E402
from sitespy.handlers.timelapse_list import handler  # noqa: E402
from sitespy.timelapse import STATUS_COMPLETE, STATUS_QUEUED  # noqa: E402

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
    """Hard-assign env vars and reset cached clients around each test."""
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
    *,
    groups: str,
    tenant_id: str,
    site_access: str = "",
) -> dict[str, Any]:
    """Build a minimal API Gateway proxy event for the timelapse_list handler."""
    return {
        "httpMethod": "GET",
        "path": "/v1/timelapse-jobs",
        "pathParameters": None,
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


def _seed_complete_job(artifact_key: str) -> None:
    """Seed a JOB# record and transition it to complete (stamping completed_at)."""
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
    )
    data.update_timelapse_job_status(
        tenant_id=_TENANT_ID,
        job_id=_JOB_ID,
        status=STATUS_COMPLETE,
        artifact_key=artifact_key,
        set_completed_at=True,
    )


def _assert_internal_error_envelope(result: dict[str, Any]) -> None:
    """Assert a 500 ApiError envelope with no jobs data."""
    assert result["statusCode"] == 500
    body = json.loads(result["body"])
    # Canonical ApiError envelope: {"error", "message"}.
    assert set(body.keys()) == {"error", "message"}
    assert body["error"] == "INTERNAL_ERROR"
    assert isinstance(body["message"], str) and body["message"]
    # No partial jobs data is ever returned on a 500.
    assert "jobs" not in body


# ---------------------------------------------------------------------------
# DynamoDB failure in the list path -> 500 ApiError, no jobs
# Validates: Requirements 2.1, 8.3
# ---------------------------------------------------------------------------


def test_dynamodb_failure_in_list_path_returns_500_without_jobs() -> None:
    """A DynamoDB failure while paging surfaces as a 500 ApiError envelope."""
    boom = ClientError(
        {"Error": {"Code": "InternalServerError", "Message": "boom"}},
        "Query",
    )

    with patch.object(data, "list_timelapse_jobs", side_effect=boom):
        event = _make_event(groups="TenantAdmins", tenant_id=_TENANT_ID)
        result = handler(event, MagicMock())

    _assert_internal_error_envelope(result)


# ---------------------------------------------------------------------------
# Non-404 error during the artifact existence check -> 500 ApiError, no jobs
# Validates: Requirements 8.3
# ---------------------------------------------------------------------------


def test_existence_check_non_404_error_surfaces_as_500_without_jobs() -> None:
    """A non-404 S3 error during the existence check surfaces as a 500."""
    artifact_key = storage.build_timelapse_key(
        _TENANT_ID, _SITE_ID, _CAMERA_ID, _JOB_ID
    )
    # A non-404 S3 error (e.g. AccessDenied) must NOT be swallowed as "missing";
    # it propagates and the handler surfaces a 500.
    access_denied = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "denied"}},
        "HeadObject",
    )

    with mock_aws():
        _dynamodb_client.cache_clear()
        storage._s3_client.cache_clear()
        _create_table(boto3.client("dynamodb", region_name=_REGION))
        _create_bucket(boto3.client("s3", region_name=_REGION))

        _seed_complete_job(artifact_key)

        event = _make_event(groups="TenantAdmins", tenant_id=_TENANT_ID)
        with patch.object(
            storage, "timelapse_artifact_exists", side_effect=access_denied
        ):
            result = handler(event, MagicMock())

    _assert_internal_error_envelope(result)
