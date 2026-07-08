"""Property-based tests for the timelapse submit handler.

Handler under test: sitespy.handlers.timelapse_post (POST /v1/timelapse-jobs).

Feature: timelapse-generation
Property 5: Submission validation
Property 6: Submission authorization
Property 7: No-footage submissions are rejected

Validates: Requirements 1.6, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 7.1
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import boto3
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from moto import mock_aws

from sitespy.config import get_settings
from sitespy.data import _dynamodb_client
from sitespy.handlers import timelapse_post

_TABLE_NAME = "test-data-table"
_REGION = "eu-west-2"
_TENANT = "acme"

# Suppress the function-scoped-fixture health check: the autouse fixture below
# only manages env vars / client caches and is intentionally not re-run per
# Hypothesis example. Each example creates its own moto backend inside the test.
_SUPPRESS = [HealthCheck.function_scoped_fixture]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env_and_cache():
    """Set env vars and clear cached boto3 clients / settings around each test."""
    os.environ.setdefault("DATA_TABLE", _TABLE_NAME)
    os.environ.setdefault("AWS_REGION", _REGION)
    os.environ.setdefault("SNAPSHOTS_BUCKET", "test-bucket")
    os.environ["JOB_QUEUE_URL"] = "https://sqs.eu-west-2.amazonaws.com/123456789012/test-queue"
    _dynamodb_client.cache_clear()
    get_settings.cache_clear()
    yield
    _dynamodb_client.cache_clear()
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Table / seed helpers
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


def _job_count(client) -> int:
    """Return the number of JOB# records currently in the table."""
    response = client.scan(
        TableName=_TABLE_NAME,
        FilterExpression="begins_with(SK, :prefix)",
        ExpressionAttributeValues={":prefix": {"S": "JOB#"}},
    )
    return response.get("Count", 0)


# ---------------------------------------------------------------------------
# Event builder
# ---------------------------------------------------------------------------


def _make_event(
    *,
    body: Any,
    groups: str = "SuperAdmins",
    tenant_id_query: str | None = _TENANT,
    tenant_id_claim: str | None = _TENANT,
    site_access: str | None = "",
    correlation_id: str = "test-corr-id",
) -> dict[str, Any]:
    """Build an API Gateway proxy event for POST /v1/timelapse-jobs."""
    query_params: dict[str, str] | None = None
    if tenant_id_query is not None:
        query_params = {"tenant_id": tenant_id_query}

    raw_body = json.dumps(body) if isinstance(body, (dict, list)) else body

    claims: dict[str, Any] = {"cognito:groups": groups}
    if tenant_id_claim is not None:
        claims["custom:tenant_id"] = tenant_id_claim
    if site_access is not None:
        claims["custom:site_access"] = site_access

    return {
        "httpMethod": "POST",
        "path": "/v1/timelapse-jobs",
        "queryStringParameters": query_params,
        "pathParameters": None,
        "headers": {"X-Correlation-Id": correlation_id},
        "body": raw_body,
        "requestContext": {"authorizer": {"claims": claims}},
    }


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_VALID_BODY = {
    "site_id": "site_1",
    "camera_id": "cam_1",
    "start": "2025-01-01T00:00:00Z",
    "end": "2025-06-01T00:00:00Z",
}


@st.composite
def _malformed_body(draw: st.DrawFn) -> dict[str, Any]:
    """Draw a request body guaranteed to fail submission validation.

    Covers every validation branch: missing/empty required fields, invalid
    ISO 8601 start/end, end <= start, and out-of-range length_seconds/fps.
    """
    body: dict[str, Any] = dict(_VALID_BODY)
    kind = draw(
        st.sampled_from(
            [
                "missing_required",
                "empty_required",
                "bad_start",
                "bad_end",
                "end_before_start",
                "end_equal_start",
                "bad_length",
                "bad_fps",
            ]
        )
    )

    if kind == "missing_required":
        field = draw(st.sampled_from(["site_id", "camera_id", "start", "end"]))
        del body[field]
    elif kind == "empty_required":
        field = draw(st.sampled_from(["site_id", "camera_id", "start", "end"]))
        body[field] = draw(st.sampled_from(["", "   ", "\t"]))
    elif kind == "bad_start":
        body["start"] = draw(
            st.sampled_from(
                ["not-a-date", "2025-13-01", "2025/01/01", "2025-02-30", "garbage", "2025-01-01T99:00:00Z"]
            )
        )
    elif kind == "bad_end":
        body["end"] = draw(
            st.sampled_from(
                ["not-a-date", "2025-13-45", "abc", "2025-01-01T25:61:00Z", "13/13/2013"]
            )
        )
    elif kind == "end_before_start":
        body["start"] = "2025-06-01T00:00:00Z"
        body["end"] = "2025-01-01T00:00:00Z"
    elif kind == "end_equal_start":
        body["start"] = "2025-03-01T00:00:00Z"
        body["end"] = "2025-03-01T00:00:00Z"
    elif kind == "bad_length":
        body["length_seconds"] = draw(
            st.sampled_from([0, -1, -100, 121, 500, 10_000, "60", 1.5, True])
        )
    elif kind == "bad_fps":
        body["fps"] = draw(st.sampled_from([0, -1, 31, 100, "24", 2.5, True]))

    return body


@st.composite
def _unauthorized_scenario(draw: st.DrawFn) -> dict[str, Any]:
    """Draw an unauthorized caller scenario expected to yield 403 or 404."""
    kind = draw(
        st.sampled_from(["user_no_access", "missing_site", "no_tenant_claim"])
    )
    site_id = draw(
        st.text(
            alphabet=st.characters(whitelist_categories=("L", "N"), max_codepoint=0x7F),
            min_size=1,
            max_size=20,
        )
    )

    if kind == "user_no_access":
        # Regular user whose site_access does not include the target site.
        # The site exists (so the 404 existence check passes) but access is denied.
        return {
            "seed_site": True,
            "site_id": site_id,
            "groups": "",
            "tenant_id_claim": _TENANT,
            "tenant_id_query": None,
            "site_access": "other_site_a,other_site_b",
        }
    if kind == "missing_site":
        # Tenant admin referencing a site that does not exist for their tenant.
        return {
            "seed_site": False,
            "site_id": site_id,
            "groups": "TenantAdmins",
            "tenant_id_claim": _TENANT,
            "tenant_id_query": None,
            "site_access": "",
        }
    # no_tenant_claim: non-super caller without a resolvable tenant.
    return {
        "seed_site": False,
        "site_id": site_id,
        "groups": draw(st.sampled_from(["", "TenantAdmins"])),
        "tenant_id_claim": None,
        "tenant_id_query": None,
        "site_access": "",
    }


# ---------------------------------------------------------------------------
# Property 5: Submission validation
# Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 7.1
# ---------------------------------------------------------------------------


@given(body=_malformed_body())
@settings(max_examples=200, suppress_health_check=_SUPPRESS)
def test_submission_validation_rejects_malformed_bodies(body: dict[str, Any]) -> None:
    """Malformed submissions return 400 and create no job.

    A body with a missing/empty required field, an invalid ISO 8601
    start/end, end <= start, or an out-of-range length_seconds/fps is
    rejected with 400 before any job record is written or message enqueued.

    Feature: timelapse-generation, Property 5: Submission validation

    **Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 7.1**
    """
    with mock_aws():
        _dynamodb_client.cache_clear()
        client = boto3.client("dynamodb", region_name=_REGION)
        _create_table(client)

        mock_sqs = MagicMock()
        with patch.object(timelapse_post, "_sqs_client", return_value=mock_sqs):
            event = _make_event(body=body, groups="SuperAdmins", tenant_id_query=_TENANT)
            result = timelapse_post.handler(event, MagicMock())

        assert result["statusCode"] == 400
        assert json.loads(result["body"])["error"] == "BAD_REQUEST"
        # No job created and no render message enqueued.
        mock_sqs.send_message.assert_not_called()
        assert _job_count(client) == 0


# ---------------------------------------------------------------------------
# Property 6: Submission authorization
# Validates: Requirements 1.6, 2.6, 2.7
# ---------------------------------------------------------------------------


@given(scenario=_unauthorized_scenario())
@settings(max_examples=200, suppress_health_check=_SUPPRESS)
def test_submission_authorization_rejects_unauthorized_callers(
    scenario: dict[str, Any],
) -> None:
    """Unauthorized callers get 403 (or 404 for a missing site) and no job.

    Regardless of an otherwise-valid body, a caller who is not authorized for
    the referenced site is rejected without creating a Timelapse_Job or
    enqueuing a message.

    Feature: timelapse-generation, Property 6: Submission authorization

    **Validates: Requirements 1.6, 2.6, 2.7**
    """
    body = dict(_VALID_BODY)
    body["site_id"] = scenario["site_id"]

    with mock_aws():
        _dynamodb_client.cache_clear()
        client = boto3.client("dynamodb", region_name=_REGION)
        _create_table(client)
        if scenario["seed_site"]:
            _seed_site(client, _TENANT, scenario["site_id"])

        mock_sqs = MagicMock()
        with patch.object(timelapse_post, "_sqs_client", return_value=mock_sqs):
            event = _make_event(
                body=body,
                groups=scenario["groups"],
                tenant_id_query=scenario["tenant_id_query"],
                tenant_id_claim=scenario["tenant_id_claim"],
                site_access=scenario["site_access"],
            )
            result = timelapse_post.handler(event, MagicMock())

        assert result["statusCode"] in (403, 404)
        error_key = json.loads(result["body"])["error"]
        assert error_key in ("ACCESS_DENIED", "NOT_FOUND")
        # No job created and no render message enqueued.
        mock_sqs.send_message.assert_not_called()
        assert _job_count(client) == 0


# ---------------------------------------------------------------------------
# Property 7: No-footage submissions are rejected
# Validates: Requirements 2.8
# ---------------------------------------------------------------------------


@given(
    site_id=st.text(
        alphabet=st.characters(whitelist_categories=("L", "N"), max_codepoint=0x7F),
        min_size=1,
        max_size=20,
    ),
    camera_id=st.text(
        alphabet=st.characters(whitelist_categories=("L", "N"), max_codepoint=0x7F),
        min_size=1,
        max_size=20,
    ),
)
@settings(max_examples=200, suppress_health_check=_SUPPRESS)
def test_no_footage_submissions_are_rejected(site_id: str, camera_id: str) -> None:
    """A range with no snapshots returns 404 and writes nothing.

    An authorized submission whose [start, end] range contains no IMG#
    records is rejected with 404, and neither a JOB# record nor a render
    message is created.

    Feature: timelapse-generation, Property 7: No-footage submissions are rejected

    **Validates: Requirements 2.8**
    """
    body = dict(_VALID_BODY)
    body["site_id"] = site_id
    body["camera_id"] = camera_id

    with mock_aws():
        _dynamodb_client.cache_clear()
        client = boto3.client("dynamodb", region_name=_REGION)
        _create_table(client)
        # Site exists and caller is authorized, but no footage is seeded.
        _seed_site(client, _TENANT, site_id)

        mock_sqs = MagicMock()
        with patch.object(timelapse_post, "_sqs_client", return_value=mock_sqs):
            event = _make_event(body=body, groups="SuperAdmins", tenant_id_query=_TENANT)
            result = timelapse_post.handler(event, MagicMock())

        assert result["statusCode"] == 404
        assert json.loads(result["body"])["error"] == "NOT_FOUND"
        # No job created and no render message enqueued.
        mock_sqs.send_message.assert_not_called()
        assert _job_count(client) == 0


# ---------------------------------------------------------------------------
# Feature: timelapse-job-listing
#
# Property 11: Requested_By capture follows sub -> email -> null
# Property 13: Job TTL equals created_at plus the Retention_Period
# ---------------------------------------------------------------------------


def _seed_img(
    client,
    tenant_id: str,
    site_id: str,
    camera_id: str,
    timestamp: str,
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


# ---------------------------------------------------------------------------
# Property 11: Requested_By capture follows sub -> email -> null
# Validates: Requirements 6.1, 6.7
# ---------------------------------------------------------------------------


@given(
    sub=st.one_of(
        st.none(),
        st.text(
            alphabet=st.characters(whitelist_categories=("L", "N"), max_codepoint=0x7F),
            min_size=0,
            max_size=40,
        ),
    ),
    email=st.one_of(
        st.none(),
        st.text(
            alphabet=st.characters(whitelist_categories=("L", "N"), max_codepoint=0x7F),
            min_size=0,
            max_size=40,
        ),
    ),
)
@settings(max_examples=200, suppress_health_check=_SUPPRESS)
def test_requested_by_capture_follows_sub_email_null(
    sub: str | None, email: str | None
) -> None:
    """Stored requested_by is sub, else email, else null; submission always succeeds.

    For any combination of present/absent (and possibly empty) ``sub`` and
    ``email`` claims, an otherwise-valid submission succeeds (202) and the
    JOB# record stores ``requested_by`` equal to ``sub or email or None``.
    A value of None omits the attribute (read back as null).

    Feature: timelapse-job-listing, Property 11: Requested_By capture follows sub -> email -> null

    **Validates: Requirements 6.1, 6.7**
    """
    # Replicate the handler's precedence: (sub or email or None), which treats
    # empty strings as falsy and therefore falls through.
    expected = (sub or email) or None

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
            event = _make_event(
                body=dict(_VALID_BODY), groups="SuperAdmins", tenant_id_query=_TENANT
            )
            claims = event["requestContext"]["authorizer"]["claims"]
            if sub is not None:
                claims["sub"] = sub
            if email is not None:
                claims["email"] = email

            result = timelapse_post.handler(event, MagicMock())

        # Submission always succeeds regardless of which claims are present.
        assert result["statusCode"] == 202

        job = _get_job(client)
        assert job is not None
        if expected is None:
            assert "requested_by" not in job
        else:
            assert job["requested_by"]["S"] == expected


# ---------------------------------------------------------------------------
# Property 13: Job TTL equals created_at plus the Retention_Period
# Validates: Requirements 7.1, 7.5
# ---------------------------------------------------------------------------


class _FrozenDateTime:
    """A datetime stand-in that freezes ``now`` while delegating parsing."""

    _fixed: datetime

    @classmethod
    def now(cls, tz: Any = None) -> datetime:
        return cls._fixed

    strptime = staticmethod(datetime.strptime)
    fromisoformat = staticmethod(datetime.fromisoformat)


@given(
    created=st.datetimes(
        min_value=datetime(2001, 1, 1),
        max_value=datetime(2099, 12, 31),
        timezones=st.just(UTC),
    )
)
@settings(max_examples=200, suppress_health_check=_SUPPRESS)
def test_job_ttl_equals_created_at_plus_retention_period(created: datetime) -> None:
    """The JOB# ttl equals created_at epoch plus the configured Retention_Period.

    For any submission instant, the stored ttl equals the epoch seconds of
    ``created_at`` plus retention_days * 86400 (2,592,000s for the 30-day
    default), so the record expires at or before its Artifact.

    Feature: timelapse-job-listing, Property 13: Job TTL equals created_at plus the Retention_Period

    **Validates: Requirements 7.1, 7.5**
    """
    settings_ = get_settings()
    retention_seconds = settings_.job_ttl_days * 86400
    # The 30-day default resolves to 2,592,000 seconds.
    assert settings_.retention_days == 30
    assert retention_seconds == 2_592_000

    frozen = type("_Frozen", (_FrozenDateTime,), {"_fixed": created})
    expected_created_at = created.strftime("%Y-%m-%dT%H:%M:%SZ")
    expected_ttl = int(created.timestamp()) + retention_seconds

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
        with (
            patch.object(timelapse_post, "_sqs_client", return_value=mock_sqs),
            patch.object(timelapse_post, "datetime", frozen),
        ):
            event = _make_event(
                body=dict(_VALID_BODY), groups="SuperAdmins", tenant_id_query=_TENANT
            )
            result = timelapse_post.handler(event, MagicMock())

        assert result["statusCode"] == 202

        job = _get_job(client)
        assert job is not None
        assert job["created_at"]["S"] == expected_created_at
        assert int(job["ttl"]["N"]) == expected_ttl
