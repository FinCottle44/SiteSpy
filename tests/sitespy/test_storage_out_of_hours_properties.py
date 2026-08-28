r"""Property tests for out-of-hours storage keys.

Feature: working-hours-retention
Property 12: Out-of-hours prefix is distinct

Validates: Requirements 5.4, 5.5

For any tenant/site/camera/timestamp, the out-of-hours S3 key begins with
``security/`` and its first path segment shares no common segment with the
in-hours long-term key, the ``live/`` key, or the ``timelapse/`` key.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from sitespy.storage import (
    build_live_snapshot_key,
    build_out_of_hours_key,
    build_snapshot_key,
    build_timelapse_key,
)

# Reserved literal prefixes used by the fixed key namespaces. Identifiers are
# constrained to avoid these so the property exercises the structural prefix
# design rather than a pathological tenant/site/camera name collision.
_RESERVED = frozenset({"security", "preserved", "live", "timelapse"})

_ID_STRATEGY = st.from_regex(r"^[a-z0-9_]{1,64}$", fullmatch=True).filter(
    lambda s: s not in _RESERVED
)
_TS_STRATEGY = st.from_regex(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", fullmatch=True)
_JOB_STRATEGY = st.from_regex(r"^[a-z0-9-]{1,64}$", fullmatch=True)


# Feature: working-hours-retention, Property 12: Out-of-hours prefix is distinct
@given(
    tenant_id=_ID_STRATEGY,
    site_id=_ID_STRATEGY,
    camera_id=_ID_STRATEGY,
    snapshot_ts=_TS_STRATEGY,
    job_id=_JOB_STRATEGY,
)
@settings(max_examples=100)
def test_out_of_hours_prefix_is_distinct(
    tenant_id: str,
    site_id: str,
    camera_id: str,
    snapshot_ts: str,
    job_id: str,
) -> None:
    """P12: the out-of-hours key begins with ``security/`` and its first path
    segment differs from the first segment of the in-hours, ``live/`` and
    ``timelapse/`` keys."""
    ooh_key = build_out_of_hours_key(tenant_id, site_id, camera_id, snapshot_ts)
    in_hours_key = build_snapshot_key(tenant_id, site_id, camera_id, snapshot_ts)
    live_key = build_live_snapshot_key(tenant_id, site_id, camera_id, snapshot_ts)
    timelapse_key = build_timelapse_key(tenant_id, site_id, camera_id, job_id)

    # Begins with the distinct security/ prefix (Req 5.5).
    assert ooh_key.startswith("security/")

    ooh_first = ooh_key.split("/", 1)[0]
    in_hours_first = in_hours_key.split("/", 1)[0]
    live_first = live_key.split("/", 1)[0]
    timelapse_first = timelapse_key.split("/", 1)[0]

    assert ooh_first == "security"
    # Shares no first path segment with any other snapshot namespace (Req 5.4, 5.5).
    assert ooh_first != in_hours_first
    assert ooh_first != live_first
    assert ooh_first != timelapse_first
