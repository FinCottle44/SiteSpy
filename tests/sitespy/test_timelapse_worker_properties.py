"""Property-based tests for completed_at idempotence in the timelapse worker path.

Data layer under test: sitespy.data.update_timelapse_job_status (called by
sitespy.handlers.timelapse_worker when marking a job complete with
``set_completed_at=True``).

Feature: timelapse-job-listing
Property 12: Completed_At is stamped once and never overwritten

Validates: Requirements 6.2
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any

import boto3
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from moto import mock_aws

from sitespy import data, timelapse
from sitespy.config import get_settings
from sitespy.data import _dynamodb_client

_TABLE_NAME = "test-data-table"
_REGION = "eu-west-2"
_TENANT = "acme"
_JOB_ID = "job-1234"

# ISO 8601 UTC instant with a trailing "Z", e.g. 2025-06-08T09:17:22Z.
_ISO_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# Suppress the function-scoped-fixture health check: the autouse fixture only
# manages env vars / client caches and is intentionally not re-run per example.
_SUPPRESS = [HealthCheck.function_scoped_fixture]

_ALL_STATUSES = (
    timelapse.STATUS_QUEUED,
    timelapse.STATUS_PROCESSING,
    timelapse.STATUS_COMPLETE,
    timelapse.STATUS_FAILED,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env_and_cache():
    """Set env vars and clear cached boto3 clients / settings around each test."""
    os.environ.setdefault("DATA_TABLE", _TABLE_NAME)
    os.environ.setdefault("AWS_REGION", _REGION)
    os.environ.setdefault("SNAPSHOTS_BUCKET", "test-bucket")
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


def _seed_job(*, status: str = timelapse.STATUS_PROCESSING) -> None:
    """Seed a JOB# record (with no completed_at yet) via the data layer."""
    data.put_timelapse_job(
        tenant_id=_TENANT,
        job_id=_JOB_ID,
        site_id="site_1",
        camera_id="cam_1",
        start_ts="2025-01-01T00:00:00Z",
        end_ts="2025-06-01T00:00:00Z",
        length_seconds=60,
        fps=24,
        status=status,
        created_at="2025-06-08T09:15:00Z",
        ttl=9_999_999_999,
    )


def _read_completed_at(client) -> str | None:
    """Return the stored completed_at value of the seeded job, or None."""
    response = client.get_item(
        TableName=_TABLE_NAME,
        Key={
            "PK": {"S": f"TENANT#{_TENANT}"},
            "SK": {"S": f"JOB#{_JOB_ID}"},
        },
    )
    item = response.get("Item")
    if item is None:
        return None
    field = item.get("completed_at")
    return field["S"] if field else None


# ---------------------------------------------------------------------------
# Strategy: a sequence of subsequent status updates applied after completion.
# Each update carries a status and whether it re-requests the completed_at
# stamp (a later transition may again pass set_completed_at=True).
# ---------------------------------------------------------------------------


@st.composite
def _subsequent_updates(draw: st.DrawFn) -> list[dict[str, Any]]:
    """Draw 1..8 follow-up status updates applied after the first completion."""
    count = draw(st.integers(min_value=1, max_value=8))
    updates: list[dict[str, Any]] = []
    for _ in range(count):
        status = draw(st.sampled_from(_ALL_STATUSES))
        update: dict[str, Any] = {
            "status": status,
            "set_completed_at": draw(st.booleans()),
        }
        # Occasionally attach an artifact_key / failure_reason to exercise the
        # other SET branches alongside the completed_at branch.
        if draw(st.booleans()):
            update["artifact_key"] = draw(
                st.text(
                    alphabet=st.characters(
                        whitelist_categories=("L", "N"), max_codepoint=0x7F
                    ),
                    min_size=1,
                    max_size=20,
                ).map(lambda s: f"timelapse/{s}.mp4")
            )
        if draw(st.booleans()):
            update["failure_reason"] = draw(
                st.text(
                    alphabet=st.characters(
                        whitelist_categories=("L", "N"), max_codepoint=0x7F
                    ),
                    min_size=1,
                    max_size=20,
                )
            )
        updates.append(update)
    return updates


# ---------------------------------------------------------------------------
# Property 12: Completed_At is stamped once and never overwritten
# Validates: Requirements 6.2
# ---------------------------------------------------------------------------


@given(updates=_subsequent_updates())
@settings(max_examples=200, suppress_health_check=_SUPPRESS)
def test_completed_at_stamped_once_and_never_overwritten(
    updates: list[dict[str, Any]],
) -> None:
    """The first completion stamps completed_at; later updates never change it.

    A freshly seeded job (no completed_at) is marked complete via
    ``update_timelapse_job_status(..., set_completed_at=True)``. After that
    first stamp, ``completed_at`` is a valid ISO 8601 UTC timestamp. Any
    subsequent status update — including ones that again pass
    ``set_completed_at=True`` — leaves the stored ``completed_at`` unchanged,
    because the data layer uses ``if_not_exists(completed_at, :now)``.

    Feature: timelapse-job-listing, Property 12: Completed_At is stamped once
    and never overwritten

    **Validates: Requirements 6.2**
    """
    with mock_aws():
        _dynamodb_client.cache_clear()
        client = boto3.client("dynamodb", region_name=_REGION)
        _create_table(client)
        _seed_job()

        # No completed_at before the first completion.
        assert _read_completed_at(client) is None

        # First transition to complete stamps completed_at.
        data.update_timelapse_job_status(
            _TENANT,
            _JOB_ID,
            timelapse.STATUS_COMPLETE,
            artifact_key="timelapse/first.mp4",
            set_completed_at=True,
        )

        first_stamp = _read_completed_at(client)
        assert first_stamp is not None
        # It is a valid ISO 8601 UTC timestamp (...Z form) and parses cleanly.
        assert _ISO_UTC_RE.match(first_stamp), first_stamp
        datetime.strptime(first_stamp, "%Y-%m-%dT%H:%M:%SZ")

        # Any number of subsequent updates leave completed_at unchanged.
        for update in updates:
            data.update_timelapse_job_status(
                _TENANT,
                _JOB_ID,
                update["status"],
                artifact_key=update.get("artifact_key"),
                failure_reason=update.get("failure_reason"),
                set_completed_at=update["set_completed_at"],
            )
            assert _read_completed_at(client) == first_stamp
