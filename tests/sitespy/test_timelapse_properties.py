"""Property-based tests for the sitespy.timelapse pure functions.

Feature: timelapse-generation
Property 1: Frame budget computation
Property 2: Frame selection is bounded
Property 3: Frame selection preserves order and span
Property 4: Frame selection uses all items when under budget
Property 8: Job persistence round-trip

Validates: Requirements 1.4, 3.2, 3.3, 3.4, 3.5, 5.1, 7.2
"""

from __future__ import annotations

import os
from datetime import datetime

import boto3
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from moto import mock_aws

from sitespy.data import _dynamodb_client, get_timelapse_job, put_timelapse_job
from sitespy.timelapse import (
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_PROCESSING,
    STATUS_QUEUED,
    compute_frame_budget,
    select_frames,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Valid output parameters. Ranges are kept generous (beyond the configured
# MAX_* caps) so the pure arithmetic is exercised across a wide input space
# without producing budgets so large they slow generation.
_LENGTH_SECONDS = st.integers(min_value=1, max_value=600)
_FPS = st.integers(min_value=1, max_value=120)

# Arbitrary lists of items (opaque payloads) for the bounded-selection property.
_ARBITRARY_ITEMS = st.lists(st.integers(), min_size=0, max_size=200)

# A frame budget of at least 1 (the documented precondition of select_frames).
_BUDGET = st.integers(min_value=1, max_value=300)


@st.composite
def _chronological_items(draw: st.DrawFn, min_size: int = 1) -> list[int]:
    """Draw a non-empty, strictly-increasing (chronological) list of ints.

    Using strictly-increasing values lets us assert order/span unambiguously:
    the original order is fully recoverable from the values themselves.
    """
    values = draw(
        st.lists(
            st.integers(min_value=-1_000_000, max_value=1_000_000),
            min_size=min_size,
            max_size=200,
            unique=True,
        )
    )
    return sorted(values)


# ---------------------------------------------------------------------------
# Property 1: Frame budget computation
# Validates: Requirements 3.2, 7.2
# ---------------------------------------------------------------------------


@given(length_seconds=_LENGTH_SECONDS, fps=_FPS)
@settings(max_examples=200)
def test_frame_budget_is_product_of_length_and_fps(length_seconds: int, fps: int) -> None:
    """compute_frame_budget returns exactly length_seconds * fps.

    Feature: timelapse-generation, Property 1: Frame budget computation

    **Validates: Requirements 3.2, 7.2**
    """
    assert compute_frame_budget(length_seconds, fps) == length_seconds * fps


# ---------------------------------------------------------------------------
# Property 2: Frame selection is bounded
# Validates: Requirements 3.3, 3.4, 7.2
# ---------------------------------------------------------------------------


@given(items=_ARBITRARY_ITEMS, frame_budget=_BUDGET)
@settings(max_examples=200)
def test_frame_selection_is_bounded(items: list[int], frame_budget: int) -> None:
    """select_frames returns at most min(frame_budget, len(items)) items.

    Feature: timelapse-generation, Property 2: Frame selection is bounded

    **Validates: Requirements 3.3, 3.4, 7.2**
    """
    result = select_frames(items, frame_budget)
    assert len(result) <= min(frame_budget, len(items))


# ---------------------------------------------------------------------------
# Property 3: Frame selection preserves order and span
# Validates: Requirements 3.3, 3.5
# ---------------------------------------------------------------------------


@given(items=_chronological_items(min_size=1), frame_budget=st.integers(min_value=2, max_value=300))
@settings(max_examples=200)
def test_frame_selection_preserves_order_and_span(
    items: list[int], frame_budget: int
) -> None:
    """Result is chronologically ordered and keeps the first and last items.

    Constrained to frame_budget >= 2: with a budget of 1 and more than one
    item, select_frames returns only the first item and therefore cannot keep
    both the first and last elements.

    Feature: timelapse-generation, Property 3: Frame selection preserves order and span

    **Validates: Requirements 3.3, 3.5**
    """
    result = select_frames(items, frame_budget)

    # Chronologically ordered (items are strictly increasing, so a correctly
    # ordered subset is itself strictly increasing / equal to its own sort).
    assert result == sorted(result)

    # First and last elements of the input are preserved.
    assert result[0] == items[0]
    assert result[-1] == items[-1]


# ---------------------------------------------------------------------------
# Property 4: Frame selection uses all items when under budget
# Validates: Requirements 3.4
# ---------------------------------------------------------------------------


@given(data=st.data())
@settings(max_examples=200)
def test_frame_selection_under_budget_passthrough(data: st.DataObject) -> None:
    """Lists no longer than the budget are returned unchanged.

    Feature: timelapse-generation, Property 4: Frame selection uses all items when under budget

    **Validates: Requirements 3.4**
    """
    items = data.draw(st.lists(st.integers(), min_size=0, max_size=200))
    # Choose a budget >= len(items) so the passthrough branch is exercised.
    frame_budget = data.draw(st.integers(min_value=max(1, len(items)), max_value=len(items) + 300))

    result = select_frames(items, frame_budget)
    assert result == list(items)


# ---------------------------------------------------------------------------
# Property 8: Job persistence round-trip
# Validates: Requirements 1.4, 5.1
# ---------------------------------------------------------------------------

_TABLE_NAME = "test-data-table"

# Identifiers (tenant/site/camera/job) — non-empty ASCII alphanumerics so they
# are valid DynamoDB key/attribute values.
_IDENTIFIERS = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), max_codepoint=0x7F),
    min_size=1,
    max_size=40,
)

# ISO 8601 timestamps for the requested range.
_ISO_TIMESTAMPS = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2100, 1, 1),
).map(lambda d: d.isoformat())

_OUTPUT_PARAM = st.integers(min_value=1, max_value=10_000)
_STATUS = st.sampled_from(
    [STATUS_QUEUED, STATUS_PROCESSING, STATUS_COMPLETE, STATUS_FAILED]
)
_TTL = st.integers(min_value=0, max_value=2_000_000_000)


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


@pytest.fixture(autouse=True)
def _env_and_cache():
    """Set env vars and clear the _dynamodb_client lru_cache."""
    os.environ.setdefault("DATA_TABLE", _TABLE_NAME)
    os.environ.setdefault("AWS_REGION", "eu-west-2")
    os.environ.setdefault("SNAPSHOTS_BUCKET", "test-bucket")
    _dynamodb_client.cache_clear()
    yield
    _dynamodb_client.cache_clear()


@given(
    tenant_id=_IDENTIFIERS,
    job_id=_IDENTIFIERS,
    site_id=_IDENTIFIERS,
    camera_id=_IDENTIFIERS,
    start_ts=_ISO_TIMESTAMPS,
    end_ts=_ISO_TIMESTAMPS,
    length_seconds=_OUTPUT_PARAM,
    fps=_OUTPUT_PARAM,
    status=_STATUS,
    created_at=_ISO_TIMESTAMPS,
    ttl=_TTL,
)
@settings(max_examples=200)
def test_job_persistence_round_trip(
    tenant_id: str,
    job_id: str,
    site_id: str,
    camera_id: str,
    start_ts: str,
    end_ts: str,
    length_seconds: int,
    fps: int,
    status: str,
    created_at: str,
    ttl: int,
) -> None:
    """put_timelapse_job then get_timelapse_job returns matching fields.

    For any valid job parameters, writing a JOB# record and reading it back
    yields identical site_id, camera_id, start_ts, end_ts, length_seconds,
    fps, and status.

    Feature: timelapse-generation, Property 8: Job persistence round-trip

    **Validates: Requirements 1.4, 5.1**
    """
    with mock_aws():
        _dynamodb_client.cache_clear()
        client = boto3.client("dynamodb", region_name="eu-west-2")
        _create_table(client)

        put_timelapse_job(
            tenant_id=tenant_id,
            job_id=job_id,
            site_id=site_id,
            camera_id=camera_id,
            start_ts=start_ts,
            end_ts=end_ts,
            length_seconds=length_seconds,
            fps=fps,
            status=status,
            created_at=created_at,
            ttl=ttl,
        )

        item = get_timelapse_job(tenant_id, job_id)

        assert item is not None
        assert item["site_id"]["S"] == site_id
        assert item["camera_id"]["S"] == camera_id
        assert item["start_ts"]["S"] == start_ts
        assert item["end_ts"]["S"] == end_ts
        assert int(item["length_seconds"]["N"]) == length_seconds
        assert int(item["fps"]["N"]) == fps
        assert item["status"]["S"] == status
