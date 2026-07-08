"""Property-based tests for the timelapse job listing handler.

Handler under test: sitespy.handlers.timelapse_list.handler (GET /v1/timelapse-jobs)

Feature: timelapse-job-listing

Properties covered in this file:
  1  Filtering returns exactly the matching jobs
  2  Results are ordered newest-first with a stable tie-break
  3  A page is bounded by the effective limit
  4  Cursor paging partitions a stable dataset without overlap or gap
  5  No cross-tenant leakage
  6  A user is confined to its site access
  7  Response shape follows job status
  9  Invalid input is rejected with a 400 ApiError before any query
 10  Callers with no resolvable tenant are forbidden

Validates: Requirements 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 2.2, 2.3, 2.4, 2.5, 2.6,
2.7, 2.8, 2.9, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7,
4.8, 5.1, 5.2, 6.3, 6.5, 6.6, 8.1, 8.2, 8.4, 8.5
"""

from __future__ import annotations

import base64
import json
import os
from contextlib import contextmanager
from typing import Any, Iterator
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
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402
from moto import mock_aws  # noqa: E402

from sitespy import data, storage  # noqa: E402
from sitespy.config import get_settings  # noqa: E402
from sitespy.data import _dynamodb_client  # noqa: E402
from sitespy.handlers.timelapse_list import handler  # noqa: E402
from sitespy.timelapse import (  # noqa: E402
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_PROCESSING,
    STATUS_QUEUED,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TABLE_NAME = "test-data-table"
_REGION = "eu-west-2"
_EXPECTED_TTL = 3600

_SUPPRESS = [HealthCheck.function_scoped_fixture]

_ALL_STATUSES = (STATUS_QUEUED, STATUS_PROCESSING, STATUS_COMPLETE, STATUS_FAILED)

# Small, fixed pools so filters frequently match seeded data (and sometimes
# miss, exercising the empty-result path). None contain an underscore, so a
# generated tenant id can never collide with the sandbox tenant.
_SITE_POOL = ["siteA", "siteB", "siteC"]
_CAMERA_POOL = ["cam1", "cam2"]

# A small timestamp pool deliberately forces created_at collisions so the
# job_id tie-break is exercised.
_TS_POOL = [
    "2025-01-01T00:00:00Z",
    "2025-06-15T14:00:00Z",
    "2025-06-15T14:00:00Z",  # duplicated value to encourage collisions
    "2025-06-16T09:30:00Z",
    "2025-09-01T12:00:00Z",
    "2025-12-31T23:59:59Z",
]

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Non-empty ASCII alphanumeric identifiers (no commas / underscores / spaces),
# safe for DynamoDB keys and comma-delimited site_access claims.
_IDENTIFIERS = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), max_codepoint=0x7F),
    min_size=1,
    max_size=24,
)

_SITE = st.sampled_from(_SITE_POOL)
_CAMERA = st.sampled_from(_CAMERA_POOL)
_STATUS = st.sampled_from(_ALL_STATUSES)
_CREATED_AT = st.sampled_from(_TS_POOL)
_REQUESTED_BY = st.one_of(st.none(), _IDENTIFIERS)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _env_and_cache() -> Iterator[None]:
    """Ensure env vars are set and cached clients/settings are reset per test."""
    os.environ.setdefault("DATA_TABLE", _TABLE_NAME)
    os.environ.setdefault("AWS_REGION", _REGION)
    _dynamodb_client.cache_clear()
    storage._s3_client.cache_clear()
    get_settings.cache_clear()
    yield
    _dynamodb_client.cache_clear()
    storage._s3_client.cache_clear()
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Table / seeding helpers
# ---------------------------------------------------------------------------


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


def _seed_job(job: dict[str, Any]) -> None:
    """Seed a single JOB# record in the requested lifecycle state.

    Every job is created via ``put_timelapse_job`` (status ``queued``) then
    transitioned via ``update_timelapse_job_status`` when a non-queued status
    is requested — mirroring the real worker lifecycle. ``complete`` jobs get
    ``set_completed_at=True`` so ``completed_at`` is stamped, and carry an
    ``artifact_key``.
    """
    data.put_timelapse_job(
        tenant_id=job["tenant_id"],
        job_id=job["job_id"],
        site_id=job["site_id"],
        camera_id=job["camera_id"],
        start_ts="2025-06-15T14:00:00Z",
        end_ts="2025-06-15T15:00:00Z",
        length_seconds=60,
        fps=24,
        status=STATUS_QUEUED,
        created_at=job["created_at"],
        ttl=2_000_000_000,
        requested_by=job.get("requested_by"),
    )
    status = job["status"]
    if status == STATUS_QUEUED:
        return
    data.update_timelapse_job_status(
        tenant_id=job["tenant_id"],
        job_id=job["job_id"],
        status=status,
        artifact_key=job.get("artifact_key") if status == STATUS_COMPLETE else None,
        failure_reason="render failed" if status == STATUS_FAILED else None,
        set_completed_at=(status == STATUS_COMPLETE),
    )


def _seed_jobs(jobs: list[dict[str, Any]]) -> None:
    for job in jobs:
        _seed_job(job)


@contextmanager
def _stub_storage(existing_keys: set[str] | None = None) -> Iterator[None]:
    """Stub the S3-touching download helpers.

    ``timelapse_artifact_exists`` returns True only for keys in
    ``existing_keys`` (empty set by default → every artifact is "missing"), and
    ``generate_presigned_url`` returns a fixed URL. This keeps the tests off S3
    entirely and makes artifact existence deterministic per job.
    """
    existing = existing_keys or set()
    with (
        patch("sitespy.storage.timelapse_artifact_exists", side_effect=lambda k: k in existing),
        patch("sitespy.storage.generate_presigned_url", return_value="https://example.com/download"),
    ):
        yield


def _make_event(
    *,
    groups: str,
    tenant_id_claim: str | None = None,
    site_access: str | None = None,
    query_params: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build an API Gateway proxy event for GET /v1/timelapse-jobs."""
    claims: dict[str, Any] = {"cognito:groups": groups}
    if tenant_id_claim is not None:
        claims["custom:tenant_id"] = tenant_id_claim
    if site_access is not None:
        claims["custom:site_access"] = site_access

    return {
        "httpMethod": "GET",
        "path": "/v1/timelapse-jobs",
        "pathParameters": None,
        "queryStringParameters": query_params or None,
        "headers": {"X-Correlation-Id": "test-corr-id"},
        "requestContext": {"authorizer": {"claims": claims}},
    }


# ---------------------------------------------------------------------------
# Dataset strategies
# ---------------------------------------------------------------------------


@st.composite
def _single_tenant_jobs(
    draw: st.DrawFn,
    tenant_id: str = "tenantA",
    *,
    min_size: int = 0,
    max_size: int = 12,
) -> list[dict[str, Any]]:
    """Draw a dataset of jobs for a single tenant with unique job_ids."""
    n = draw(st.integers(min_value=min_size, max_value=max_size))
    job_ids = draw(st.lists(_IDENTIFIERS, min_size=n, max_size=n, unique=True))
    jobs: list[dict[str, Any]] = []
    for jid in job_ids:
        jobs.append(
            {
                "tenant_id": tenant_id,
                "job_id": jid,
                "site_id": draw(_SITE),
                "camera_id": draw(_CAMERA),
                "status": draw(_STATUS),
                "created_at": draw(_CREATED_AT),
                "requested_by": draw(_REQUESTED_BY),
            }
        )
    return jobs


# ===========================================================================
# Property 1: Filtering returns exactly the matching jobs
# Validates: Requirements 2.2, 2.3, 2.4, 2.5, 2.9, 8.1
# ===========================================================================


@st.composite
def _filter_subset(draw: st.DrawFn) -> dict[str, str]:
    """Draw an optional subset of site_id / camera_id / status filters.

    A ``camera_id`` is only ever drawn together with a ``site_id`` (the handler
    rejects camera-without-site as a 400, which is out of scope here). Filter
    values are drawn from the same pools the dataset uses so matches are common,
    with an occasional out-of-pool value producing an empty result.
    """
    filters: dict[str, str] = {}
    site_choice = draw(st.sampled_from([None, "siteA", "siteB", "siteC", "siteZZZ"]))
    if site_choice is not None:
        filters["site_id"] = site_choice
        if draw(st.booleans()):
            filters["camera_id"] = draw(st.sampled_from(["cam1", "cam2", "camZZZ"]))
    status_choice = draw(st.sampled_from([None, *_ALL_STATUSES]))
    if status_choice is not None:
        filters["status"] = status_choice
    return filters


@given(jobs=_single_tenant_jobs(), filters=_filter_subset())
@settings(max_examples=200, deadline=None, suppress_health_check=_SUPPRESS)
def test_filtering_returns_exactly_the_matching_jobs(
    jobs: list[dict[str, Any]], filters: dict[str, str]
) -> None:
    """Every result matches all supplied filters, and no in-scope match is lost.

    For any dataset and any subset of the site_id / camera_id / status filters,
    the returned jobs are exactly the dataset jobs satisfying every supplied
    filter (exact, case-sensitive AND). When nothing matches the response is a
    200 empty page with next_cursor null.

    Feature: timelapse-job-listing, Property 1: Filtering returns exactly the
    matching jobs

    **Validates: Requirements 2.2, 2.3, 2.4, 2.5, 2.9, 8.1**
    """
    tenant_id = "tenantA"

    def _matches(job: dict[str, Any]) -> bool:
        return all(job[name] == value for name, value in filters.items())

    expected_ids = {job["job_id"] for job in jobs if _matches(job)}

    query_params = dict(filters)
    # A large limit ensures a single page returns every match (dataset <= 12).
    query_params["limit"] = "100"

    with mock_aws():
        _dynamodb_client.cache_clear()
        storage._s3_client.cache_clear()
        _create_table(boto3.client("dynamodb", region_name=_REGION))
        _seed_jobs(jobs)

        event = _make_event(
            groups="TenantAdmins", tenant_id_claim=tenant_id, query_params=query_params
        )
        with _stub_storage():
            result = handler(event, MagicMock())

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    returned = body["jobs"]

    # Every returned job satisfies every supplied filter (exact, case-sensitive).
    for entry in returned:
        for name, value in filters.items():
            assert entry[name] == value

    # Exactness: returned set equals the expected in-scope matching set.
    returned_ids = {entry["job_id"] for entry in returned}
    assert returned_ids == expected_ids

    # Empty-match ⇒ 200 empty page with next_cursor null (Req 8.1).
    if not expected_ids:
        assert returned == []
        assert body["next_cursor"] is None


# ===========================================================================
# Property 2: Results are ordered newest-first with a stable tie-break
# Validates: Requirements 1.6, 2.9
# ===========================================================================


@given(jobs=_single_tenant_jobs(min_size=2, max_size=12))
@settings(max_examples=200, deadline=None, suppress_health_check=_SUPPRESS)
def test_results_ordered_newest_first_with_stable_tie_break(
    jobs: list[dict[str, Any]],
) -> None:
    """Returned entries are ordered by (created_at desc, job_id desc).

    Datasets draw created_at from a small pool that forces collisions, so the
    job_id descending tie-break is exercised alongside the primary created_at
    descending order.

    Feature: timelapse-job-listing, Property 2: Results are ordered newest-first
    with a stable tie-break

    **Validates: Requirements 1.6, 2.9**
    """
    tenant_id = "tenantA"

    with mock_aws():
        _dynamodb_client.cache_clear()
        storage._s3_client.cache_clear()
        _create_table(boto3.client("dynamodb", region_name=_REGION))
        _seed_jobs(jobs)

        event = _make_event(
            groups="TenantAdmins",
            tenant_id_claim=tenant_id,
            query_params={"limit": "100"},
        )
        with _stub_storage():
            result = handler(event, MagicMock())

    assert result["statusCode"] == 200
    returned = json.loads(result["body"])["jobs"]

    keys = [(entry["created_at"], entry["job_id"]) for entry in returned]
    # Each adjacent pair must be in non-increasing (created_at, job_id) order.
    for earlier, later in zip(keys, keys[1:]):
        assert earlier >= later
    assert keys == sorted(keys, reverse=True)


# ===========================================================================
# Property 3: A page is bounded by the effective limit
# Validates: Requirements 1.2, 1.3, 3.1, 3.2
# ===========================================================================

_LIMIT_PARAM = st.one_of(
    st.none(),
    st.just(""),
    st.just("   "),
    st.integers(min_value=1, max_value=100).map(str),
)


@given(jobs=_single_tenant_jobs(min_size=0, max_size=12), limit=_LIMIT_PARAM)
@settings(max_examples=200, deadline=None, suppress_health_check=_SUPPRESS)
def test_page_is_bounded_by_effective_limit(
    jobs: list[dict[str, Any]], limit: str | None
) -> None:
    """The number of returned jobs never exceeds the effective limit.

    The effective limit is the supplied value when it parses to an integer in
    [1, 100], otherwise the default of 20 when limit is omitted / empty /
    whitespace.

    Feature: timelapse-job-listing, Property 3: A page is bounded by the
    effective limit

    **Validates: Requirements 1.2, 1.3, 3.1, 3.2**
    """
    tenant_id = "tenantA"

    if limit is None or limit.strip() == "":
        effective_limit = 20
    else:
        effective_limit = int(limit.strip())

    query_params: dict[str, str] = {}
    if limit is not None:
        query_params["limit"] = limit

    with mock_aws():
        _dynamodb_client.cache_clear()
        storage._s3_client.cache_clear()
        _create_table(boto3.client("dynamodb", region_name=_REGION))
        _seed_jobs(jobs)

        event = _make_event(
            groups="TenantAdmins",
            tenant_id_claim=tenant_id,
            query_params=query_params or None,
        )
        with _stub_storage():
            result = handler(event, MagicMock())

    assert result["statusCode"] == 200
    returned = json.loads(result["body"])["jobs"]
    assert len(returned) <= effective_limit


# ===========================================================================
# Property 4: Cursor paging partitions a stable dataset without overlap or gap
# Validates: Requirements 1.3, 3.4, 3.5, 3.6
# ===========================================================================


@st.composite
def _paged_dataset(draw: st.DrawFn) -> dict[str, Any]:
    """Draw a stable dataset strictly larger than one page but at most two.

    The dataset size is ``limit + extra`` with ``2 <= limit <= 6`` and
    ``1 <= extra <= limit - 1``, guaranteeing page one fills to ``limit`` (so a
    non-null next_cursor) and page two returns the remaining ``extra`` jobs and
    exhausts the partition (so a null next_cursor).
    """
    limit = draw(st.integers(min_value=2, max_value=6))
    extra = draw(st.integers(min_value=1, max_value=limit - 1))
    count = limit + extra
    job_ids = draw(st.lists(_IDENTIFIERS, min_size=count, max_size=count, unique=True))
    jobs = [
        {
            "tenant_id": "tenantA",
            "job_id": jid,
            "site_id": draw(_SITE),
            "camera_id": draw(_CAMERA),
            "status": draw(_STATUS),
            "created_at": draw(_CREATED_AT),
            "requested_by": draw(_REQUESTED_BY),
        }
        for jid in job_ids
    ]
    return {"limit": limit, "jobs": jobs}


@given(dataset=_paged_dataset())
@settings(max_examples=200, deadline=None, suppress_health_check=_SUPPRESS)
def test_cursor_paging_partitions_without_overlap_or_gap(
    dataset: dict[str, Any],
) -> None:
    """Two consecutive pages are disjoint and together cover the dataset.

    Page one is fetched with the page size; its next_cursor is fed into a
    second request. The two pages share no job (no overlap) and their union is
    exactly the full dataset (no gap). next_cursor is a base64-decodable token
    while more jobs remain and null once the dataset is exhausted, and each page
    is internally ordered by (created_at desc, job_id desc).

    Feature: timelapse-job-listing, Property 4: Cursor paging partitions a
    stable dataset without overlap or gap

    **Validates: Requirements 1.3, 3.4, 3.5, 3.6**
    """
    tenant_id = "tenantA"
    limit = dataset["limit"]
    jobs = dataset["jobs"]
    all_ids = {job["job_id"] for job in jobs}

    with mock_aws():
        _dynamodb_client.cache_clear()
        storage._s3_client.cache_clear()
        _create_table(boto3.client("dynamodb", region_name=_REGION))
        _seed_jobs(jobs)

        with _stub_storage():
            event1 = _make_event(
                groups="TenantAdmins",
                tenant_id_claim=tenant_id,
                query_params={"limit": str(limit)},
            )
            result1 = handler(event1, MagicMock())
            assert result1["statusCode"] == 200
            body1 = json.loads(result1["body"])
            page1 = body1["jobs"]
            cursor = body1["next_cursor"]

            # More jobs remain, so next_cursor must be a base64-decodable token.
            assert cursor is not None
            base64.b64decode(cursor, validate=True)  # raises if not valid base64
            assert len(page1) <= limit

            event2 = _make_event(
                groups="TenantAdmins",
                tenant_id_claim=tenant_id,
                query_params={"limit": str(limit), "cursor": cursor},
            )
            result2 = handler(event2, MagicMock())
            assert result2["statusCode"] == 200
            body2 = json.loads(result2["body"])
            page2 = body2["jobs"]

    ids1 = [entry["job_id"] for entry in page1]
    ids2 = [entry["job_id"] for entry in page2]

    # Each page is bounded by the limit.
    assert len(ids1) <= limit
    assert len(ids2) <= limit

    # No overlap between the two pages.
    assert set(ids1).isdisjoint(set(ids2))

    # No gap: the disjoint union of the two pages is exactly the full dataset.
    assert set(ids1) | set(ids2) == all_ids

    # The dataset is exhausted after page two → next_cursor is null.
    assert body2["next_cursor"] is None

    # Each page is internally ordered newest-first with the job_id tie-break.
    for page in (page1, page2):
        keys = [(e["created_at"], e["job_id"]) for e in page]
        assert keys == sorted(keys, reverse=True)


# ===========================================================================
# Property 5: No cross-tenant leakage
# Validates: Requirements 1.1, 4.1, 4.3, 4.5
# ===========================================================================


@st.composite
def _multi_tenant_jobs(draw: st.DrawFn) -> list[dict[str, Any]]:
    """Draw a dataset spanning two tenants with globally unique job_ids."""
    n = draw(st.integers(min_value=1, max_value=14))
    job_ids = draw(st.lists(_IDENTIFIERS, min_size=n, max_size=n, unique=True))
    jobs: list[dict[str, Any]] = []
    for jid in job_ids:
        jobs.append(
            {
                "tenant_id": draw(st.sampled_from(["tenantA", "tenantB"])),
                "job_id": jid,
                "site_id": draw(_SITE),
                "camera_id": draw(_CAMERA),
                "status": draw(_STATUS),
                "created_at": draw(_CREATED_AT),
                "requested_by": draw(_REQUESTED_BY),
            }
        )
    return jobs


@given(
    jobs=_multi_tenant_jobs(),
    resolved_tenant=st.sampled_from(["tenantA", "tenantB"]),
    role=st.sampled_from(["super_admin", "tenant_admin", "user"]),
)
@settings(max_examples=200, deadline=None, suppress_health_check=_SUPPRESS)
def test_no_cross_tenant_leakage(
    jobs: list[dict[str, Any]], resolved_tenant: str, role: str
) -> None:
    """Every returned job belongs to the resolved tenant, whatever the role.

    For a super_admin the tenant resolves from the trimmed tenant_id query
    parameter; for a tenant_admin / user it resolves from the caller's own
    tenant claim. No job of any other tenant ever appears.

    Feature: timelapse-job-listing, Property 5: No cross-tenant leakage

    **Validates: Requirements 1.1, 4.1, 4.3, 4.5**
    """
    id_to_tenant = {job["job_id"]: job["tenant_id"] for job in jobs}

    if role == "super_admin":
        groups = "SuperAdmins"
        tenant_id_claim = None
        query_params: dict[str, str] = {"tenant_id": resolved_tenant, "limit": "100"}
        site_access = None
    elif role == "tenant_admin":
        groups = "TenantAdmins"
        tenant_id_claim = resolved_tenant
        query_params = {"limit": "100"}
        site_access = None
    else:  # user — grant access to every site so results are not empty
        groups = ""
        tenant_id_claim = resolved_tenant
        query_params = {"limit": "100"}
        site_access = ",".join(_SITE_POOL)

    with mock_aws():
        _dynamodb_client.cache_clear()
        storage._s3_client.cache_clear()
        _create_table(boto3.client("dynamodb", region_name=_REGION))
        _seed_jobs(jobs)

        event = _make_event(
            groups=groups,
            tenant_id_claim=tenant_id_claim,
            site_access=site_access,
            query_params=query_params,
        )
        with _stub_storage():
            result = handler(event, MagicMock())

    assert result["statusCode"] == 200
    returned = json.loads(result["body"])["jobs"]

    # Every returned job is a member of the resolved tenant's partition.
    for entry in returned:
        assert id_to_tenant[entry["job_id"]] == resolved_tenant


# ===========================================================================
# Property 6: A user is confined to its site access
# Validates: Requirements 4.4, 4.6, 4.7
# ===========================================================================


@st.composite
def _user_scope_scenario(draw: st.DrawFn) -> dict[str, Any]:
    """Draw a user + site-access scenario across the three confinement cases."""
    kind = draw(st.sampled_from(["normal", "site_out_of_access", "empty_access"]))
    jobs = draw(_single_tenant_jobs(min_size=0, max_size=12))

    if kind == "empty_access":
        return {"kind": kind, "jobs": jobs, "site_access": [], "site_filter": None}

    # A non-empty, proper subset of the site pool.
    access = draw(
        st.lists(st.sampled_from(_SITE_POOL), min_size=1, max_size=2, unique=True)
    )
    if kind == "normal":
        return {"kind": kind, "jobs": jobs, "site_access": access, "site_filter": None}

    # site_out_of_access: supply a site_id that is provably not in the access set.
    out_of_access = [s for s in _SITE_POOL if s not in access]
    site_filter = draw(st.sampled_from(out_of_access + ["siteZZZ"]))
    return {"kind": kind, "jobs": jobs, "site_access": access, "site_filter": site_filter}


@given(scenario=_user_scope_scenario())
@settings(max_examples=200, deadline=None, suppress_health_check=_SUPPRESS)
def test_user_is_confined_to_its_site_access(scenario: dict[str, Any]) -> None:
    """A user only ever sees jobs whose site is in its site access.

    - normal: results are a subset of the caller's Site_Access.
    - a supplied site_id outside Site_Access ⇒ 200 empty page, next_cursor null.
    - empty Site_Access ⇒ 200 empty page, next_cursor null.

    Feature: timelapse-job-listing, Property 6: A user is confined to its site
    access

    **Validates: Requirements 4.4, 4.6, 4.7**
    """
    tenant_id = "tenantA"
    access = scenario["site_access"]
    query_params: dict[str, str] = {"limit": "100"}
    if scenario["site_filter"] is not None:
        query_params["site_id"] = scenario["site_filter"]

    with mock_aws():
        _dynamodb_client.cache_clear()
        storage._s3_client.cache_clear()
        _create_table(boto3.client("dynamodb", region_name=_REGION))
        _seed_jobs(scenario["jobs"])

        event = _make_event(
            groups="",
            tenant_id_claim=tenant_id,
            site_access=",".join(access),
            query_params=query_params,
        )
        with _stub_storage():
            result = handler(event, MagicMock())

    assert result["statusCode"] == 200
    body = json.loads(result["body"])
    returned = body["jobs"]

    if scenario["kind"] in ("site_out_of_access", "empty_access"):
        assert returned == []
        assert body["next_cursor"] is None
    else:
        access_set = set(access)
        for entry in returned:
            assert entry["site_id"] in access_set


# ===========================================================================
# Property 7: Response shape follows job status
# Validates: Requirements 1.4, 1.5, 5.1, 5.2, 6.3, 6.5, 6.6
# ===========================================================================

_REQUIRED_FIELDS = (
    "job_id",
    "site_id",
    "camera_id",
    "start",
    "end",
    "length_seconds",
    "status",
    "created_at",
    "completed_at",
    "requested_by",
)


@st.composite
def _status_shape_jobs(draw: st.DrawFn) -> list[dict[str, Any]]:
    """Draw jobs of all statuses; complete jobs carry an artifact-exists flag."""
    n = draw(st.integers(min_value=1, max_value=12))
    job_ids = draw(st.lists(_IDENTIFIERS, min_size=n, max_size=n, unique=True))
    jobs: list[dict[str, Any]] = []
    for jid in job_ids:
        status = draw(_STATUS)
        job: dict[str, Any] = {
            "tenant_id": "tenantA",
            "job_id": jid,
            "site_id": draw(_SITE),
            "camera_id": draw(_CAMERA),
            "status": status,
            "created_at": draw(_CREATED_AT),
            "requested_by": draw(_REQUESTED_BY),
        }
        if status == STATUS_COMPLETE:
            job["artifact_key"] = f"art-{jid}"
            job["artifact_exists"] = draw(st.booleans())
        jobs.append(job)
    return jobs


@given(jobs=_status_shape_jobs())
@settings(max_examples=200, deadline=None, suppress_health_check=_SUPPRESS)
def test_response_shape_follows_job_status(jobs: list[dict[str, Any]]) -> None:
    """Each entry's field presence and null rules follow the job's status.

    - The base fields are always present.
    - completed_at is null unless status == complete (and non-null when complete).
    - requested_by is null when no value was captured.
    - download_url + expires_in (positive int 3600) appear only for a complete
      job whose Artifact exists; absent for every other status; a complete job
      whose Artifact is missing carries artifact_available: false and no link.

    Feature: timelapse-job-listing, Property 7: Response shape follows job status

    **Validates: Requirements 1.4, 1.5, 5.1, 5.2, 6.3, 6.5, 6.6**
    """
    tenant_id = "tenantA"
    by_id = {job["job_id"]: job for job in jobs}
    existing_keys = {
        job["artifact_key"]
        for job in jobs
        if job["status"] == STATUS_COMPLETE and job.get("artifact_exists")
    }

    with mock_aws():
        _dynamodb_client.cache_clear()
        storage._s3_client.cache_clear()
        _create_table(boto3.client("dynamodb", region_name=_REGION))
        _seed_jobs(jobs)

        event = _make_event(
            groups="TenantAdmins",
            tenant_id_claim=tenant_id,
            query_params={"limit": "100"},
        )
        with _stub_storage(existing_keys):
            result = handler(event, MagicMock())

    assert result["statusCode"] == 200
    returned = json.loads(result["body"])["jobs"]
    assert len(returned) == len(jobs)

    for entry in returned:
        source = by_id[entry["job_id"]]
        status = source["status"]

        # Base fields are always present.
        for field in _REQUIRED_FIELDS:
            assert field in entry
        assert entry["status"] == status

        # requested_by is null when no value was captured, else the stored value.
        assert entry["requested_by"] == source.get("requested_by")

        # completed_at is null unless the job is complete.
        if status == STATUS_COMPLETE:
            assert isinstance(entry["completed_at"], str) and entry["completed_at"]
        else:
            assert entry["completed_at"] is None

        # download_url / expires_in appear only for complete-with-artifact.
        if status == STATUS_COMPLETE and source.get("artifact_exists"):
            assert entry["download_url"] == "https://example.com/download"
            assert entry["expires_in"] == _EXPECTED_TTL
            assert isinstance(entry["expires_in"], int) and entry["expires_in"] > 0
            assert "artifact_available" not in entry
        elif status == STATUS_COMPLETE:
            # Complete but artifact missing → availability indicator, no link.
            assert entry.get("artifact_available") is False
            assert "download_url" not in entry
            assert "expires_in" not in entry
        else:
            assert "download_url" not in entry
            assert "expires_in" not in entry


# ===========================================================================
# Property 9: Invalid input is rejected with a 400 ApiError before any query
# Validates: Requirements 2.6, 2.7, 2.8, 3.3, 3.7, 4.2, 8.4, 8.5
# ===========================================================================

_BLANK_VALUES = st.sampled_from(["", "   ", "\t"])
_BAD_LIMITS = st.sampled_from(["0", "-1", "101", "150", "1000", "abc", "1.5", "10x"])
# Empty (invalid), not-base64, and valid-base64-that-is-not-a-dict cursors.
_BAD_CURSORS = st.sampled_from(["", "!!!not-base64$$", "%%%%", "W10=", "NQ==", "InN0ciI="])
_BAD_STATUSES = st.sampled_from(["Complete", "COMPLETE", "done", "queued ", "running", "xyz"])


@st.composite
def _invalid_request(draw: st.DrawFn) -> dict[str, Any]:
    """Draw an invalid request scenario (query params + caller claims)."""
    kind = draw(
        st.sampled_from(
            [
                "blank_site",
                "blank_camera",
                "blank_status",
                "camera_without_site",
                "bad_status",
                "bad_limit",
                "bad_cursor",
                "super_admin_no_tenant",
            ]
        )
    )

    # Default: a tenant_admin with a resolvable tenant so validation is what fails.
    groups = "TenantAdmins"
    tenant_id_claim: str | None = "tenantA"
    query_params: dict[str, str] = {}

    if kind == "blank_site":
        query_params["site_id"] = draw(_BLANK_VALUES)
    elif kind == "blank_camera":
        # camera_id is validated for blankness before the camera-needs-site rule.
        query_params["camera_id"] = draw(_BLANK_VALUES)
        query_params["site_id"] = "siteA"
    elif kind == "blank_status":
        query_params["status"] = draw(_BLANK_VALUES)
    elif kind == "camera_without_site":
        query_params["camera_id"] = draw(st.sampled_from(_CAMERA_POOL))
    elif kind == "bad_status":
        query_params["status"] = draw(_BAD_STATUSES)
    elif kind == "bad_limit":
        query_params["limit"] = draw(_BAD_LIMITS)
    elif kind == "bad_cursor":
        query_params["cursor"] = draw(_BAD_CURSORS)
    else:  # super_admin_no_tenant
        groups = "SuperAdmins"
        tenant_id_claim = None
        missing = draw(st.sampled_from([None, "", "   "]))
        if missing is not None:
            query_params["tenant_id"] = missing

    return {
        "kind": kind,
        "groups": groups,
        "tenant_id_claim": tenant_id_claim,
        "query_params": query_params,
    }


@given(scenario=_invalid_request())
@settings(max_examples=200, deadline=None, suppress_health_check=_SUPPRESS)
def test_invalid_input_rejected_with_400_before_any_query(
    scenario: dict[str, Any],
) -> None:
    """Every invalid request yields a 400 ApiError envelope and returns no jobs.

    Covers blank site_id / camera_id / status, camera-without-site, an invalid
    status, a limit outside [1, 100] / non-integer, an empty / non-base64 /
    non-dict cursor, and (for super_admin) an absent or blank tenant_id.

    Feature: timelapse-job-listing, Property 9: Invalid input is rejected with a
    400 ApiError before any query

    **Validates: Requirements 2.6, 2.7, 2.8, 3.3, 3.7, 4.2, 8.4, 8.5**
    """
    # Seed a valid dataset so we confirm no jobs are returned despite data
    # existing — validation runs before any query.
    jobs = [
        {
            "tenant_id": "tenantA",
            "job_id": f"seed{i}",
            "site_id": "siteA",
            "camera_id": "cam1",
            "status": STATUS_QUEUED,
            "created_at": "2025-06-15T14:00:00Z",
            "requested_by": None,
        }
        for i in range(3)
    ]

    with mock_aws():
        _dynamodb_client.cache_clear()
        storage._s3_client.cache_clear()
        _create_table(boto3.client("dynamodb", region_name=_REGION))
        _seed_jobs(jobs)

        event = _make_event(
            groups=scenario["groups"],
            tenant_id_claim=scenario["tenant_id_claim"],
            query_params=scenario["query_params"] or None,
        )
        with _stub_storage():
            result = handler(event, MagicMock())

    assert result["statusCode"] == 400
    body = json.loads(result["body"])
    # Canonical ApiError envelope, no jobs data.
    assert body["error"] == "BAD_REQUEST"
    assert "message" in body
    assert "jobs" not in body


# ===========================================================================
# Property 10: Callers with no resolvable tenant are forbidden
# Validates: Requirements 4.8, 8.2
# ===========================================================================


@given(
    role_groups=st.sampled_from(["", "TenantAdmins"]),
    tenant_claim=st.sampled_from([None, ""]),
    query_params=st.sampled_from(
        [None, {"limit": "50"}, {"site_id": "siteA"}]
    ),
)
@settings(max_examples=200, deadline=None, suppress_health_check=_SUPPRESS)
def test_callers_with_no_resolvable_tenant_are_forbidden(
    role_groups: str, tenant_claim: str | None, query_params: dict[str, str] | None
) -> None:
    """A tenant_admin / user with no resolvable tenant gets 403 and no jobs.

    The custom:tenant_id claim is absent or blank, so the caller's tenant cannot
    be resolved and the endpoint returns a 403 ApiError envelope with no jobs
    data. Only claims that resolve to a blank tenant reach this path (a blank
    claim resolves to None via the handler's ``or None``).

    Feature: timelapse-job-listing, Property 10: Callers with no resolvable
    tenant are forbidden

    **Validates: Requirements 4.8, 8.2**
    """
    with mock_aws():
        _dynamodb_client.cache_clear()
        storage._s3_client.cache_clear()
        _create_table(boto3.client("dynamodb", region_name=_REGION))
        _seed_jobs(
            [
                {
                    "tenant_id": "tenantA",
                    "job_id": "seed1",
                    "site_id": "siteA",
                    "camera_id": "cam1",
                    "status": STATUS_QUEUED,
                    "created_at": "2025-06-15T14:00:00Z",
                    "requested_by": None,
                }
            ]
        )

        # A whitespace-only claim: the handler resolves it via `claims.get(...)
        # or None`; a non-empty whitespace string is truthy, so include only
        # values that actually resolve to no tenant (None or empty string).
        event = _make_event(
            groups=role_groups,
            tenant_id_claim=tenant_claim,
            site_access="siteA",
            query_params=query_params,
        )
        with _stub_storage():
            result = handler(event, MagicMock())

    assert result["statusCode"] == 403
    body = json.loads(result["body"])
    assert body["error"] == "ACCESS_DENIED"
    assert "message" in body
    assert "jobs" not in body
