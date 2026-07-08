"""Property-based tests for the timelapse status/retrieve handler.

Handler under test: sitespy.handlers.timelapse_get (GET /v1/timelapse-jobs/{job_id})

Feature: timelapse-generation
Property 9: Status response shape by state
Property 10: Retrieval authorization

Validates: Requirements 5.2, 5.3, 5.4, 5.5, 5.6
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
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from moto import mock_aws  # noqa: E402

from sitespy import data, storage  # noqa: E402
from sitespy.config import get_settings  # noqa: E402
from sitespy.data import _dynamodb_client  # noqa: E402
from sitespy.handlers.timelapse_get import handler  # noqa: E402
from sitespy.timelapse import (  # noqa: E402
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_PROCESSING,
    STATUS_QUEUED,
)

# ---------------------------------------------------------------------------
# Constants / strategies
# ---------------------------------------------------------------------------

_TABLE_NAME = "test-data-table"
_BUCKET_NAME = "test-snapshots-bucket"
_REGION = "eu-west-2"

# Non-empty ASCII alphanumeric identifiers (no commas / underscores), safe for
# DynamoDB keys, comma-delimited site_access, and never equal to the sandbox
# tenant id ("sandbox_construction" contains an underscore).
_IDENTIFIERS = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), max_codepoint=0x7F),
    min_size=1,
    max_size=30,
)

_ISO_TIMESTAMPS = st.just("2025-06-15T14:00:00Z")
_OUTPUT_PARAM = st.integers(min_value=1, max_value=300)
_NON_EMPTY_TEXT = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), max_codepoint=0x7F),
    min_size=1,
    max_size=40,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env_and_cache():
    """Ensure env vars are set and cached clients are reset around each test."""
    os.environ.setdefault("DATA_TABLE", _TABLE_NAME)
    os.environ.setdefault("SNAPSHOTS_BUCKET", _BUCKET_NAME)
    os.environ.setdefault("AWS_REGION", _REGION)
    _dynamodb_client.cache_clear()
    storage._s3_client.cache_clear()
    yield
    _dynamodb_client.cache_clear()
    storage._s3_client.cache_clear()


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
    query_tenant_id: str | None = None,
) -> dict[str, Any]:
    """Build a minimal API Gateway proxy event for the timelapse_get handler."""
    query_params: dict[str, str] = {}
    if query_tenant_id is not None:
        query_params["tenant_id"] = query_tenant_id

    return {
        "httpMethod": "GET",
        "path": f"/v1/timelapse-jobs/{job_id}",
        "pathParameters": {"job_id": job_id},
        "queryStringParameters": query_params or None,
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


def _seed_job(
    *,
    tenant_id: str,
    job_id: str,
    site_id: str,
    camera_id: str,
    status: str,
    artifact_key: str | None = None,
    failure_reason: str | None = None,
) -> None:
    """Seed a JOB# record in the requested lifecycle state.

    Jobs start as ``queued`` via put_timelapse_job; processing/complete/failed
    states (and their artifact_key/failure_reason) are applied via
    update_timelapse_job_status, mirroring the real worker lifecycle.
    """
    data.put_timelapse_job(
        tenant_id=tenant_id,
        job_id=job_id,
        site_id=site_id,
        camera_id=camera_id,
        start_ts="2025-06-15T14:00:00Z",
        end_ts="2025-06-15T15:00:00Z",
        length_seconds=60,
        fps=24,
        status=STATUS_QUEUED,
        created_at="2025-06-15T13:00:00Z",
        ttl=2_000_000_000,
    )
    if status == STATUS_QUEUED:
        return
    data.update_timelapse_job_status(
        tenant_id=tenant_id,
        job_id=job_id,
        status=status,
        artifact_key=artifact_key,
        failure_reason=failure_reason,
    )


# ---------------------------------------------------------------------------
# Property 9: Status response shape by state
# Validates: Requirements 5.2, 5.3, 5.4
# ---------------------------------------------------------------------------


@given(
    tenant_id=_IDENTIFIERS,
    job_id=_IDENTIFIERS,
    site_id=_IDENTIFIERS,
    camera_id=_IDENTIFIERS,
    status=st.sampled_from(
        [STATUS_QUEUED, STATUS_PROCESSING, STATUS_COMPLETE, STATUS_FAILED]
    ),
    artifact_key=_NON_EMPTY_TEXT,
    failure_reason=_NON_EMPTY_TEXT,
)
@settings(max_examples=200, deadline=None)
def test_status_response_shape_by_state(
    tenant_id: str,
    job_id: str,
    site_id: str,
    camera_id: str,
    status: str,
    artifact_key: str,
    failure_reason: str,
) -> None:
    """The retrieve response carries exactly the fields appropriate to the state.

    - queued / processing -> status only, no download_url / expires_in
    - complete            -> presigned download_url + expires_in
    - failed              -> failure reason

    Feature: timelapse-generation, Property 9: Status response shape by state

    **Validates: Requirements 5.2, 5.3, 5.4**
    """
    with mock_aws():
        _dynamodb_client.cache_clear()
        storage._s3_client.cache_clear()
        _create_table(boto3.client("dynamodb", region_name=_REGION))
        _create_bucket(boto3.client("s3", region_name=_REGION))

        _seed_job(
            tenant_id=tenant_id,
            job_id=job_id,
            site_id=site_id,
            camera_id=camera_id,
            status=status,
            artifact_key=artifact_key if status == STATUS_COMPLETE else None,
            failure_reason=failure_reason if status == STATUS_FAILED else None,
        )

        # For a complete job the Artifact must actually exist in S3 so the
        # shared download helper (which checks existence before presigning)
        # emits a download_url rather than an availability indicator.
        if status == STATUS_COMPLETE:
            boto3.client("s3", region_name=_REGION).put_object(
                Bucket=_BUCKET_NAME, Key=artifact_key, Body=b"fake-mp4"
            )

        # A tenant_admin whose tenant matches the job's tenant is authorized
        # regardless of site_access, isolating this property to response shape.
        event = _make_event(job_id, groups="TenantAdmins", tenant_id=tenant_id)
        result = handler(event, MagicMock())

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    assert body["status"] == status

    if status in (STATUS_QUEUED, STATUS_PROCESSING):
        assert "download_url" not in body
        assert "expires_in" not in body
        assert "reason" not in body
    elif status == STATUS_COMPLETE:
        assert isinstance(body["download_url"], str) and body["download_url"]
        assert body["expires_in"] == get_settings().artifact_presign_ttl
        assert "reason" not in body
    else:  # STATUS_FAILED
        assert body["reason"] == failure_reason
        assert "download_url" not in body
        assert "expires_in" not in body


# ---------------------------------------------------------------------------
# Property 10: Retrieval authorization
# Validates: Requirements 5.5, 5.6
# ---------------------------------------------------------------------------


@given(
    tenant_id=_IDENTIFIERS,
    job_id=_IDENTIFIERS,
    site_id=_IDENTIFIERS,
    camera_id=_IDENTIFIERS,
    artifact_key=_NON_EMPTY_TEXT,
    other_sites=st.lists(_IDENTIFIERS, min_size=0, max_size=5),
)
@settings(max_examples=200, deadline=None)
def test_retrieval_authorization_returns_404_without_leaking(
    tenant_id: str,
    job_id: str,
    site_id: str,
    camera_id: str,
    artifact_key: str,
    other_sites: list[str],
) -> None:
    """An unauthorized caller gets 404 and no status/URL is leaked.

    The job is seeded as ``complete`` (with an artifact_key) so that any leak
    would be visible as a status or download_url in the body. The caller is a
    ``user`` in the same tenant but WITHOUT the job's site in their site_access,
    so authorization fails and the handler returns NotFound (404) rather than
    Forbidden — never revealing the job's existence.

    Feature: timelapse-generation, Property 10: Retrieval authorization

    **Validates: Requirements 5.5, 5.6**
    """
    # site_access that provably excludes the job's site.
    site_access_list = [s for s in other_sites if s != site_id]
    site_access = ",".join(site_access_list)

    with mock_aws():
        _dynamodb_client.cache_clear()
        storage._s3_client.cache_clear()
        _create_table(boto3.client("dynamodb", region_name=_REGION))
        _create_bucket(boto3.client("s3", region_name=_REGION))

        _seed_job(
            tenant_id=tenant_id,
            job_id=job_id,
            site_id=site_id,
            camera_id=camera_id,
            status=STATUS_COMPLETE,
            artifact_key=artifact_key,
        )

        event = _make_event(
            job_id,
            groups="",  # regular user
            tenant_id=tenant_id,
            site_access=site_access,
        )
        result = handler(event, MagicMock())

    assert result["statusCode"] == 404
    body = json.loads(result["body"])
    assert body["error"] == "NOT_FOUND"

    # Nothing about the job's state or artifact is leaked in the envelope.
    assert "status" not in body
    assert "download_url" not in body
    assert "expires_in" not in body
    assert "reason" not in body
