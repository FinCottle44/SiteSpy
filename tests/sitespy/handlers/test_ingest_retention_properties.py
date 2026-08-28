"""Property tests for the reworked ingest handler (working-hours retention).

These Hypothesis property tests validate the 24/7 ingestion + retention-class
behaviour of the reworked ``POST /v1/ingest/{token}`` handler:

- Feature: working-hours-retention, Property 8: 24/7 persistence — hours never
  suppress a save.
- Feature: working-hours-retention, Property 9: Cadence enforced across
  retention classes.
- Feature: working-hours-retention, Property 10: Integrity digest and size.
- Feature: working-hours-retention, Property 11: Retention class recorded
  matches assigned class.
- Feature: working-hours-retention, Property 13: TTL correctness by class.
- Feature: working-hours-retention, Property 14: DynamoDB TTL and S3 expiry
  agree within 300 seconds.
- Feature: working-hours-retention, Property 22: Capture timestamp format.

Validates: Requirements 4.1, 4.2, 4.3, 4.4, 5.6, 6.5, 7.1, 7.3, 7.5, 11.7
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import boto3
from hypothesis import given, settings
from hypothesis import strategies as st
from moto import mock_aws

from sitespy.data import _dynamodb_client
from sitespy.handlers.ingest import _handle
from sitespy.storage import _s3_client

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_OUT_OF_HOURS_TTL_SECONDS = 604800
_CAPTURE_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_ALL_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_ID_ST = st.from_regex(r"^[a-z0-9_]{1,32}$", fullmatch=True)
_TOKEN_SUFFIX_ST = st.from_regex(r"^[A-Za-z0-9_-]{40}$", fullmatch=True)
# JPEG bodies: 4 B to 1 KiB, magic-byte prefixed (kept small for speed).
_BODY_ST = st.integers(min_value=4, max_value=1024).map(
    lambda n: b"\xff\xd8\xff\xe0" + b"\x00" * (n - 4)
)
_CLASS_ST = st.sampled_from(["In_Hours", "Out_Of_Hours"])


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _set_env() -> None:
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


def _clear_caches() -> None:
    from sitespy.config import get_settings

    get_settings.cache_clear()
    _dynamodb_client.cache_clear()
    _s3_client.cache_clear()


def _setup_aws():
    """Create the moto S3 bucket and DynamoDB table."""
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


def _seed_camera(ddb, tenant_id, site_id, camera_id, ingest_token, retention_years=5):
    """Write camera (GSI1 token index) and tenant rows to mocked DynamoDB."""
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
            "retention_years": {"N": str(retention_years)},
        },
    )


def _seed_site(ddb, tenant_id, site_id, retention_class):
    """Seed a site record that deterministically forces the retention class.

    - ``In_Hours``: a site record with a valid timezone and NO ``working_hours``
      attribute → ``resolve_working_hours`` returns None → classifier returns
      In_Hours regardless of wall-clock time.
    - ``Out_Of_Hours``: a site record with a valid timezone and a degenerate
      ``working_hours`` window (``start == end``) → the in-hours interval is
      empty → classifier returns Out_Of_Hours regardless of wall-clock time.
    """
    item = {
        "PK": {"S": f"TENANT#{tenant_id}"},
        "SK": {"S": f"SITE#{site_id}"},
        "timezone": {"S": "UTC"},
    }
    if retention_class == "Out_Of_Hours":
        item["working_hours"] = {
            "M": {
                "days": {"L": [{"S": d} for d in _ALL_DAYS]},
                "start": {"S": "00:00"},
                "end": {"S": "00:00"},
            }
        }
    ddb.put_item(TableName="test-data-table", Item=item)


def _seed_prior_record(ddb, tenant_id, site_id, camera_id, ingested_at, klass):
    """Seed a prior saved snapshot record of the given retention class."""
    if klass == "In_Hours":
        sk = f"IMG#{site_id}#{camera_id}#{ingested_at}"
        s3_key = f"{tenant_id}/{site_id}/{camera_id}/2025/06/10/{ingested_at}.jpg"
        rc = "In_Hours"
    else:
        sk = f"OOH_IMG#{site_id}#{camera_id}#{ingested_at}"
        s3_key = f"security/{tenant_id}/{site_id}/{camera_id}/2025/06/10/{ingested_at}.jpg"
        rc = "Out_Of_Hours"
    ddb.put_item(
        TableName="test-data-table",
        Item={
            "PK": {"S": f"TENANT#{tenant_id}"},
            "SK": {"S": sk},
            "s3_key": {"S": s3_key},
            "sha256": {"S": "seed"},
            "size_bytes": {"N": "1024"},
            "ingested_at": {"S": ingested_at},
            "content_type": {"S": "image/jpeg"},
            "retention_class": {"S": rc},
        },
    )


def _make_event(ingest_token, body):
    """Build an ingest event using token-based URL path auth (base64 body)."""
    return {
        "httpMethod": "POST",
        "path": f"/v1/ingest/{ingest_token}",
        "pathParameters": {"token": ingest_token},
        "headers": {"Content-Type": "image/jpeg"},
        "queryStringParameters": None,
        "body": base64.b64encode(body).decode(),
        "isBase64Encoded": True,
    }


def _get_record(ddb, tenant_id, sk):
    return ddb.get_item(
        TableName="test-data-table",
        Key={"PK": {"S": f"TENANT#{tenant_id}"}, "SK": {"S": sk}},
    ).get("Item")


def _count_snapshot_records(ddb, tenant_id, site_id, camera_id):
    """Count all IMG# + OOH_IMG# records for a camera."""
    total = 0
    for prefix in (f"IMG#{site_id}#{camera_id}#", f"OOH_IMG#{site_id}#{camera_id}#"):
        resp = ddb.query(
            TableName="test-data-table",
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
            ExpressionAttributeValues={
                ":pk": {"S": f"TENANT#{tenant_id}"},
                ":sk": {"S": prefix},
            },
        )
        total += len(resp.get("Items", []))
    return total


def _record_for_response(ddb, tenant_id, site_id, camera_id, snapshot_ts, retention_class):
    """Fetch the stored snapshot record matching the response class."""
    if retention_class == "In_Hours":
        sk = f"IMG#{site_id}#{camera_id}#{snapshot_ts}"
    else:
        sk = f"OOH_IMG#{site_id}#{camera_id}#{snapshot_ts}"
    return _get_record(ddb, tenant_id, sk)


# ---------------------------------------------------------------------------
# Property 8: 24/7 persistence — hours never suppress a save
# ---------------------------------------------------------------------------


@given(
    tenant_id=_ID_ST,
    site_id=_ID_ST,
    camera_id=_ID_ST,
    token_suffix=_TOKEN_SUFFIX_ST,
    body=_BODY_ST,
    retention_class=_CLASS_ST,
)
@settings(max_examples=100, deadline=None)
@patch("sitespy.handlers.ingest.data.get_live_session", return_value=None)
def test_p8_hours_never_suppress_a_save(
    _mock_live, tenant_id, site_id, camera_id, token_suffix, body, retention_class
):
    """Property 8: a valid, non-cadence-suppressed snapshot is always persisted
    regardless of whether it is inside or outside working hours.

    Feature: working-hours-retention, Property 8: 24/7 persistence — hours never
    suppress a save.
    Validates: Requirements 4.1
    """
    _set_env()
    _clear_caches()
    ingest_token = f"tk_{token_suffix}"

    with mock_aws():
        _s3, ddb = _setup_aws()
        _clear_caches()
        _seed_camera(ddb, tenant_id, site_id, camera_id, ingest_token)
        _seed_site(ddb, tenant_id, site_id, retention_class)

        result = _handle(_make_event(ingest_token, body), "corr-p8")

        assert result["statusCode"] == 201
        resp = json.loads(result["body"])
        assert resp["retention_class"] == retention_class
        record = _record_for_response(
            ddb, tenant_id, site_id, camera_id, resp["timestamp"], retention_class
        )
        assert record is not None


# ---------------------------------------------------------------------------
# Property 9: Cadence enforced across retention classes
# ---------------------------------------------------------------------------


@given(
    tenant_id=_ID_ST,
    site_id=_ID_ST,
    camera_id=_ID_ST,
    token_suffix=_TOKEN_SUFFIX_ST,
    body=_BODY_ST,
    prior_class=_CLASS_ST,
    new_class=_CLASS_ST,
    gap_seconds=st.integers(min_value=0, max_value=899),
)
@settings(max_examples=100, deadline=None)
@patch("sitespy.handlers.ingest.data.get_live_session", return_value=None)
def test_p9_cadence_enforced_across_retention_classes(
    _mock_live,
    tenant_id,
    site_id,
    camera_id,
    token_suffix,
    body,
    prior_class,
    new_class,
    gap_seconds,
):
    """Property 9: a prior saved snapshot of either class plus a new snapshot
    arriving <900 s later with no live session is suppressed and writes no new
    record.

    Feature: working-hours-retention, Property 9: Cadence enforced across
    retention classes.
    Validates: Requirements 4.2, 4.3
    """
    _set_env()
    _clear_caches()
    ingest_token = f"tk_{token_suffix}"

    with mock_aws():
        _s3, ddb = _setup_aws()
        _clear_caches()
        _seed_camera(ddb, tenant_id, site_id, camera_id, ingest_token)
        _seed_site(ddb, tenant_id, site_id, new_class)

        prior_ts = (
            datetime.now(tz=UTC) - timedelta(seconds=gap_seconds)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        _seed_prior_record(ddb, tenant_id, site_id, camera_id, prior_ts, prior_class)

        before = _count_snapshot_records(ddb, tenant_id, site_id, camera_id)
        result = _handle(_make_event(ingest_token, body), "corr-p9")

        assert result["statusCode"] == 200
        resp = json.loads(result["body"])
        assert resp["status"] == "skipped"
        assert resp["reason"] == "cadence_filter"

        after = _count_snapshot_records(ddb, tenant_id, site_id, camera_id)
        assert after == before  # no new snapshot record written


# ---------------------------------------------------------------------------
# Property 10: Integrity digest and size
# ---------------------------------------------------------------------------


@given(
    tenant_id=_ID_ST,
    site_id=_ID_ST,
    camera_id=_ID_ST,
    token_suffix=_TOKEN_SUFFIX_ST,
    body=_BODY_ST,
    retention_class=_CLASS_ST,
)
@settings(max_examples=100, deadline=None)
@patch("sitespy.handlers.ingest.data.get_live_session", return_value=None)
def test_p10_integrity_digest_and_size(
    _mock_live, tenant_id, site_id, camera_id, token_suffix, body, retention_class
):
    """Property 10: the stored record's sha256 equals SHA-256(body) and
    size_bytes equals len(body).

    Feature: working-hours-retention, Property 10: Integrity digest and size.
    Validates: Requirements 4.4
    """
    _set_env()
    _clear_caches()
    ingest_token = f"tk_{token_suffix}"

    with mock_aws():
        _s3, ddb = _setup_aws()
        _clear_caches()
        _seed_camera(ddb, tenant_id, site_id, camera_id, ingest_token)
        _seed_site(ddb, tenant_id, site_id, retention_class)

        result = _handle(_make_event(ingest_token, body), "corr-p10")
        assert result["statusCode"] == 201
        resp = json.loads(result["body"])

        expected_sha = hashlib.sha256(body).hexdigest()
        assert resp["sha256"] == expected_sha
        assert resp["size_bytes"] == len(body)

        record = _record_for_response(
            ddb, tenant_id, site_id, camera_id, resp["timestamp"], retention_class
        )
        assert record is not None
        assert record["sha256"]["S"] == expected_sha
        assert record["size_bytes"]["N"] == str(len(body))


# ---------------------------------------------------------------------------
# Property 11: Retention class recorded matches assigned class
# ---------------------------------------------------------------------------


@given(
    tenant_id=_ID_ST,
    site_id=_ID_ST,
    camera_id=_ID_ST,
    token_suffix=_TOKEN_SUFFIX_ST,
    body=_BODY_ST,
    retention_class=_CLASS_ST,
)
@settings(max_examples=100, deadline=None)
@patch("sitespy.handlers.ingest.data.get_live_session", return_value=None)
def test_p11_retention_class_recorded_matches_assigned(
    _mock_live, tenant_id, site_id, camera_id, token_suffix, body, retention_class
):
    """Property 11: In_Hours records live under IMG# and Out_Of_Hours under
    OOH_IMG#, and the retention_class attribute equals the assigned class.

    Feature: working-hours-retention, Property 11: Retention class recorded
    matches assigned class.
    Validates: Requirements 5.6
    """
    _set_env()
    _clear_caches()
    ingest_token = f"tk_{token_suffix}"

    with mock_aws():
        _s3, ddb = _setup_aws()
        _clear_caches()
        _seed_camera(ddb, tenant_id, site_id, camera_id, ingest_token)
        _seed_site(ddb, tenant_id, site_id, retention_class)

        result = _handle(_make_event(ingest_token, body), "corr-p11")
        assert result["statusCode"] == 201
        resp = json.loads(result["body"])
        assert resp["retention_class"] == retention_class
        ts = resp["timestamp"]

        img_sk = f"IMG#{site_id}#{camera_id}#{ts}"
        ooh_sk = f"OOH_IMG#{site_id}#{camera_id}#{ts}"
        img_record = _get_record(ddb, tenant_id, img_sk)
        ooh_record = _get_record(ddb, tenant_id, ooh_sk)

        if retention_class == "In_Hours":
            assert img_record is not None
            assert ooh_record is None
            assert img_record["retention_class"]["S"] == "In_Hours"
        else:
            assert ooh_record is not None
            assert img_record is None
            assert ooh_record["retention_class"]["S"] == "Out_Of_Hours"


# ---------------------------------------------------------------------------
# Property 13: TTL correctness by class
# ---------------------------------------------------------------------------


@given(
    tenant_id=_ID_ST,
    site_id=_ID_ST,
    camera_id=_ID_ST,
    token_suffix=_TOKEN_SUFFIX_ST,
    body=_BODY_ST,
    retention_class=_CLASS_ST,
    retention_years=st.integers(min_value=1, max_value=50),
)
@settings(max_examples=100, deadline=None)
@patch("sitespy.handlers.ingest.data.get_live_session", return_value=None)
def test_p13_ttl_correctness_by_class(
    _mock_live,
    tenant_id,
    site_id,
    camera_id,
    token_suffix,
    body,
    retention_class,
    retention_years,
):
    """Property 13: In_Hours records have no ttl; Out_Of_Hours records have
    ttl == int(epoch(capture_ts)) + 604800, independent of config.

    Feature: working-hours-retention, Property 13: TTL correctness by class.
    Validates: Requirements 6.5, 7.1, 7.3
    """
    _set_env()
    _clear_caches()
    ingest_token = f"tk_{token_suffix}"

    with mock_aws():
        _s3, ddb = _setup_aws()
        _clear_caches()
        # retention_years varied to prove the OOH TTL is config-independent.
        _seed_camera(
            ddb, tenant_id, site_id, camera_id, ingest_token, retention_years
        )
        _seed_site(ddb, tenant_id, site_id, retention_class)

        result = _handle(_make_event(ingest_token, body), "corr-p13")
        assert result["statusCode"] == 201
        resp = json.loads(result["body"])
        ts = resp["timestamp"]
        record = _record_for_response(
            ddb, tenant_id, site_id, camera_id, ts, retention_class
        )
        assert record is not None

        if retention_class == "In_Hours":
            assert "ttl" not in record
        else:
            capture_epoch = int(
                datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
                .replace(tzinfo=UTC)
                .timestamp()
            )
            assert int(record["ttl"]["N"]) == capture_epoch + _OUT_OF_HOURS_TTL_SECONDS


# ---------------------------------------------------------------------------
# Property 14: DynamoDB TTL and S3 expiry agree within 300 seconds
# ---------------------------------------------------------------------------


@given(
    tenant_id=_ID_ST,
    site_id=_ID_ST,
    camera_id=_ID_ST,
    token_suffix=_TOKEN_SUFFIX_ST,
    body=_BODY_ST,
)
@settings(max_examples=100, deadline=None)
@patch("sitespy.handlers.ingest.data.get_live_session", return_value=None)
def test_p14_ttl_and_s3_expiry_agree_within_300s(
    _mock_live, tenant_id, site_id, camera_id, token_suffix, body
):
    """Property 14: an Out_Of_Hours S3 object is written in the same invocation
    that computed the capture timestamp, so its creation time is within 300 s of
    the capture instant; therefore the DynamoDB TTL expiry and the S3 lifecycle
    expiry differ by at most 300 s.

    Feature: working-hours-retention, Property 14: DynamoDB TTL and S3 expiry
    agree within 300 seconds.
    Validates: Requirements 7.5
    """
    _set_env()
    _clear_caches()
    ingest_token = f"tk_{token_suffix}"

    with mock_aws():
        s3, ddb = _setup_aws()
        _clear_caches()
        _seed_camera(ddb, tenant_id, site_id, camera_id, ingest_token)
        _seed_site(ddb, tenant_id, site_id, "Out_Of_Hours")

        result = _handle(_make_event(ingest_token, body), "corr-p14")
        assert result["statusCode"] == 201
        resp = json.loads(result["body"])
        assert resp["retention_class"] == "Out_Of_Hours"
        ts = resp["timestamp"]
        s3_key = resp["key"]

        capture_epoch = int(
            datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=UTC)
            .timestamp()
        )

        head = s3.head_object(Bucket="test-snapshots-bucket", Key=s3_key)
        object_creation_epoch = int(head["LastModified"].timestamp())

        # Object creation is within 300 s of the capture instant.
        assert abs(object_creation_epoch - capture_epoch) <= 300

        # DynamoDB TTL expiry vs S3 lifecycle expiry (both + 604800) agree.
        record = _record_for_response(
            ddb, tenant_id, site_id, camera_id, ts, "Out_Of_Hours"
        )
        ddb_expiry = int(record["ttl"]["N"])
        s3_expiry = object_creation_epoch + _OUT_OF_HOURS_TTL_SECONDS
        assert abs(ddb_expiry - s3_expiry) <= 300


# ---------------------------------------------------------------------------
# Property 22: Capture timestamp format
# ---------------------------------------------------------------------------


@given(
    tenant_id=_ID_ST,
    site_id=_ID_ST,
    camera_id=_ID_ST,
    token_suffix=_TOKEN_SUFFIX_ST,
    body=_BODY_ST,
    retention_class=_CLASS_ST,
)
@settings(max_examples=100, deadline=None)
@patch("sitespy.handlers.ingest.data.get_live_session", return_value=None)
def test_p22_capture_timestamp_format(
    _mock_live, tenant_id, site_id, camera_id, token_suffix, body, retention_class
):
    """Property 22: the stored capture timestamp matches
    ``^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}Z$``.

    Feature: working-hours-retention, Property 22: Capture timestamp format.
    Validates: Requirements 11.7
    """
    _set_env()
    _clear_caches()
    ingest_token = f"tk_{token_suffix}"

    with mock_aws():
        _s3, ddb = _setup_aws()
        _clear_caches()
        _seed_camera(ddb, tenant_id, site_id, camera_id, ingest_token)
        _seed_site(ddb, tenant_id, site_id, retention_class)

        result = _handle(_make_event(ingest_token, body), "corr-p22")
        assert result["statusCode"] == 201
        resp = json.loads(result["body"])
        ts = resp["timestamp"]

        assert _CAPTURE_TS_RE.match(ts) is not None

        record = _record_for_response(
            ddb, tenant_id, site_id, camera_id, ts, retention_class
        )
        assert record is not None
        assert _CAPTURE_TS_RE.match(record["ingested_at"]["S"]) is not None
