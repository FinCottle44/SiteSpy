"""Example-based unit tests for the timelapse submit handler.

Handler under test: sitespy.handlers.timelapse_post (POST /v1/timelapse-jobs).

Covers the two example-based behaviours called out in the design's unit-test
matrix for the timelapse-job-listing feature:

    1. Submit with neither ``sub`` nor ``email`` claim succeeds (202) and stores
       ``requested_by`` as null (the attribute is omitted, read back as null).   (6.7)
    2. The ``created_at`` used as the JOB# TTL anchor precedes the later artifact
       put time — the JOB# record is created at submit time, before the artifact
       exists, and the TTL anchor is ``created_at``.                             (7.5)

DynamoDB and S3 are backed by moto ``mock_aws``; SQS is mocked as in the
property tests.

Validates: Requirements 6.7, 7.5
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

from sitespy import storage
from sitespy.config import get_settings
from sitespy.data import _dynamodb_client
from sitespy.handlers import timelapse_post

_TABLE_NAME = "test-data-table"
_BUCKET = "test-bucket"
_REGION = "eu-west-2"
_TENANT = "acme"

_VALID_BODY = {
    "site_id": "site_1",
    "camera_id": "cam_1",
    "start": "2025-01-01T00:00:00Z",
    "end": "2025-06-01T00:00:00Z",
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env_and_cache():
    """Set env vars and clear cached boto3 clients / settings around each test."""
    os.environ["DATA_TABLE"] = _TABLE_NAME
    os.environ["AWS_REGION"] = _REGION
    os.environ["SNAPSHOTS_BUCKET"] = _BUCKET
    os.environ["JOB_QUEUE_URL"] = (
        "https://sqs.eu-west-2.amazonaws.com/123456789012/test-queue"
    )
    _dynamodb_client.cache_clear()
    storage._s3_client.cache_clear()
    get_settings.cache_clear()
    yield
    _dynamodb_client.cache_clear()
    storage._s3_client.cache_clear()
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Table / bucket / seed helpers
# ---------------------------------------------------------------------------


def _create_table(client) -> None:
    """Create the single-table schema used across the project."""
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


def _seed_site(client, tenant_id: str, site_id: str) -> None:
    """Insert a site record so the handler's existence check passes."""
    client.put_item(
        TableName=_TABLE_NAME,
        Item={
            "PK": {"S": f"TENANT#{tenant_id}"},
            "SK": {"S": f"SITE#{site_id}"},
            "site_name": {"S": "Test Site"},
            "timezone": {"S": "Europe/London"},
        },
    )


def _seed_img(
    client, tenant_id: str, site_id: str, camera_id: str, timestamp: str
) -> None:
    """Seed a single IMG# record so the footage-existence check passes."""
    client.put_item(
        TableName=_TABLE_NAME,
        Item={
            "PK": {"S": f"TENANT#{tenant_id}"},
            "SK": {"S": f"IMG#{site_id}#{camera_id}#{timestamp}"},
            "s3_key": {"S": f"{tenant_id}/{site_id}/{camera_id}/{timestamp}.jpg"},
            "ingested_at": {"S": timestamp},
        },
    )


def _get_job(client) -> dict[str, Any] | None:
    """Return the single JOB# item in the table, or None when absent."""
    response = client.scan(
        TableName=_TABLE_NAME,
        FilterExpression="begins_with(SK, :prefix)",
        ExpressionAttributeValues={":prefix": {"S": "JOB#"}},
    )
    items = response.get("Items", [])
    return items[0] if items else None


def _make_event(*, claims_extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build an API Gateway proxy event for POST /v1/timelapse-jobs.

    The caller is a super admin scoped to ``_TENANT`` via the ``tenant_id``
    query parameter. Additional JWT claims (e.g. ``sub`` / ``email``) can be
    merged via ``claims_extra``.
    """
    claims: dict[str, Any] = {
        "cognito:groups": "SuperAdmins",
        "custom:tenant_id": _TENANT,
        "custom:site_access": "",
    }
    if claims_extra:
        claims.update(claims_extra)

    return {
        "httpMethod": "POST",
        "path": "/v1/timelapse-jobs",
        "queryStringParameters": {"tenant_id": _TENANT},
        "pathParameters": None,
        "headers": {"X-Correlation-Id": "test-corr-id"},
        "body": json.dumps(dict(_VALID_BODY)),
        "requestContext": {"authorizer": {"claims": claims}},
    }


class _FrozenDateTime:
    """A datetime stand-in that freezes ``now`` while delegating parsing."""

    _fixed: datetime

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        return cls._fixed

    strptime = staticmethod(datetime.strptime)
    fromisoformat = staticmethod(datetime.fromisoformat)


# ---------------------------------------------------------------------------
# 1. Submit with neither sub nor email → 202 and requested_by null
# Validates: Requirements 6.7
# ---------------------------------------------------------------------------


def test_submit_without_sub_or_email_succeeds_and_stores_requested_by_null() -> None:
    """A caller lacking both ``sub`` and ``email`` claims still submits (202).

    The JOB# record omits the ``requested_by`` attribute entirely (read back as
    null), and the submission is not rejected for the absence of an identity.

    Validates: Requirements 6.7
    """
    with mock_aws():
        _dynamodb_client.cache_clear()
        client = boto3.client("dynamodb", region_name=_REGION)
        _create_table(client)
        _seed_site(client, _TENANT, _VALID_BODY["site_id"])
        _seed_img(
            client,
            _TENANT,
            _VALID_BODY["site_id"],
            _VALID_BODY["camera_id"],
            "2025-03-01T00:00:00Z",
        )

        mock_sqs = MagicMock()
        with patch.object(timelapse_post, "_sqs_client", return_value=mock_sqs):
            # Event carries no ``sub`` and no ``email`` claim.
            event = _make_event()
            result = timelapse_post.handler(event, MagicMock())

        # Submission succeeds despite the missing identity claims.
        assert result["statusCode"] == 202
        body = json.loads(result["body"])
        assert body["status"] == "queued"
        assert "job_id" in body

        # The render message was enqueued and the JOB# record was written.
        mock_sqs.send_message.assert_called_once()

        job = _get_job(client)
        assert job is not None
        # requested_by is omitted (read back as null) — never stored empty.
        assert "requested_by" not in job


# ---------------------------------------------------------------------------
# 2. created_at (JOB# TTL anchor) precedes the later artifact put time
# Validates: Requirements 7.5
# ---------------------------------------------------------------------------


def test_created_at_ttl_anchor_precedes_later_artifact_put_time() -> None:
    """The JOB# TTL is anchored to ``created_at``, which precedes the artifact.

    The JOB# record is created at submit time — before any artifact exists.
    This test freezes the submit instant well in the past, submits the job, and
    asserts:

    - the stored ``ttl`` equals ``created_at`` epoch + the Retention_Period,
      proving the TTL anchor is ``created_at`` (not some later time), and
    - the ``created_at`` instant precedes the artifact's later put time (the
      artifact is only written afterwards, by the Worker), so the JOB# record
      expires at or before its Artifact.

    Validates: Requirements 7.5
    """
    # Freeze the submit instant well before "now" so the artifact, written
    # afterwards, always has a strictly later creation time.
    submit_instant = datetime(2020, 1, 1, 0, 0, 0, tzinfo=UTC)
    frozen = type("_Frozen", (_FrozenDateTime,), {"_fixed": submit_instant})

    with mock_aws():
        _dynamodb_client.cache_clear()
        storage._s3_client.cache_clear()
        client = boto3.client("dynamodb", region_name=_REGION)
        _create_table(client)
        _seed_site(client, _TENANT, _VALID_BODY["site_id"])
        _seed_img(
            client,
            _TENANT,
            _VALID_BODY["site_id"],
            _VALID_BODY["camera_id"],
            "2025-03-01T00:00:00Z",
        )

        s3 = boto3.client("s3", region_name=_REGION)
        s3.create_bucket(
            Bucket=_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": _REGION},
        )

        mock_sqs = MagicMock()
        with (
            patch.object(timelapse_post, "_sqs_client", return_value=mock_sqs),
            patch.object(timelapse_post, "datetime", frozen),
        ):
            event = _make_event()
            result = timelapse_post.handler(event, MagicMock())

        assert result["statusCode"] == 202
        job_id = json.loads(result["body"])["job_id"]

        # --- The JOB# record is created at submit time, anchored on created_at ---
        job = _get_job(client)
        assert job is not None
        created_at = job["created_at"]["S"]
        ttl = int(job["ttl"]["N"])

        retention_seconds = get_settings().job_ttl_days * 86400
        created_epoch = int(submit_instant.timestamp())
        assert created_at == submit_instant.strftime("%Y-%m-%dT%H:%M:%SZ")
        # The TTL anchor is created_at (created_at epoch + Retention_Period).
        assert ttl == created_epoch + retention_seconds

        # --- The artifact is only written later (simulating the Worker) ---
        artifact_key = storage.build_timelapse_key(
            _TENANT,
            _VALID_BODY["site_id"],
            _VALID_BODY["camera_id"],
            job_id,
        )
        storage.put_timelapse_artifact(artifact_key, b"\x00\x00\x00\x18ftypmp42")

        head = s3.head_object(Bucket=_BUCKET, Key=artifact_key)
        artifact_put_time = head["LastModified"]

        # The TTL anchor (created_at) precedes the artifact's creation time, so
        # the JOB# record expires at or before its Artifact.
        created_at_dt = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )
        assert created_at_dt < artifact_put_time
