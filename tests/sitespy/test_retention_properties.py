"""Property-based tests for the sitespy.retention classifier.

Feature: working-hours-retention
Property 1: Classification totality
Property 2: Classification membership correctness (day-of-week + overnight)
Property 3: Missing/invalid timezone classifies as Out_Of_Hours
Property 4: Absent-configuration defaults to In_Hours
Property 5: Legacy migration equivalence (read-only)
Property 7: Omitted days defaults to all seven days

Validates: Requirements 2.1, 2.2, 2.3, 2.5, 3.7, 3.8, 3.9, 3.10, 5.1, 5.2, 5.3
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from hypothesis import given, settings
from hypothesis import strategies as st

from sitespy.retention import (
    DAYS,
    RetentionClass,
    WorkingHours,
    classify,
    resolve_working_hours,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_CAPTURE_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

# Valid IANA timezones, deliberately including DST-observing zones (northern
# and southern hemisphere) and fixed-offset / half-hour / 45-minute zones so
# DST boundaries and unusual offsets are exercised.
_VALID_TZS = [
    "UTC",
    "America/New_York",     # DST, northern hemisphere
    "America/Los_Angeles",  # DST
    "Europe/London",        # DST, crosses at 01:00 UTC
    "Europe/Berlin",        # DST
    "Australia/Sydney",     # DST, southern hemisphere (opposite phase)
    "Pacific/Auckland",     # DST, southern hemisphere
    "Asia/Kolkata",         # +05:30, no DST
    "Asia/Kathmandu",       # +05:45, no DST
    "Pacific/Chatham",      # +12:45 / +13:45 DST
    "America/Sao_Paulo",    # southern hemisphere
]

_valid_tz = st.sampled_from(_VALID_TZS)


def _is_invalid_tz(name: str | None) -> bool:
    """True when ``name`` is not a loadable IANA timezone identifier."""
    if not name:
        return True
    try:
        ZoneInfo(name)
    except Exception:  # noqa: BLE001 - any failure means "not a valid zone"
        return True
    return False


# Missing or unrecognised timezone identifiers (Req 3.10).
_invalid_tz = st.one_of(
    st.none(),
    st.just(""),
    st.sampled_from(
        ["Not/AZone", "Mars/Phobos", "Europe/Atlantis", "12345", "foo/bar", "GMT+25"]
    ),
    st.text(min_size=1, max_size=20).filter(_is_invalid_tz),
)

# Any timezone value at all: valid, missing, or unrecognised.
_any_tz = st.one_of(_valid_tz, _invalid_tz)


# Capture timestamps as UTC "YYYY-MM-DDTHH:MM:SSZ". A wide multi-year range so
# DST transition dates in both hemispheres are covered.
_capture_dt = st.datetimes(
    min_value=datetime(2020, 1, 1, 0, 0, 0),
    max_value=datetime(2030, 12, 31, 23, 59, 59),
)
_capture_ts = _capture_dt.map(lambda dt: dt.strftime(_CAPTURE_TS_FMT))


def _minute_to_hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


_hhmm = st.integers(min_value=0, max_value=1439).map(_minute_to_hhmm)

_days_subset = st.lists(
    st.sampled_from(DAYS), min_size=1, max_size=7, unique=True
).map(frozenset)


@st.composite
def _working_hours(draw: st.DrawFn) -> WorkingHours:
    """Arbitrary WorkingHours; naturally produces normal, overnight, and
    degenerate (start == end) windows across examples."""
    days = draw(_days_subset)
    start = draw(_hhmm)
    end = draw(_hhmm)
    return WorkingHours(days=days, start=start, end=end)


@st.composite
def _overnight_working_hours(draw: st.DrawFn) -> WorkingHours:
    """WorkingHours whose window strictly spans midnight (start > end)."""
    days = draw(_days_subset)
    start_min = draw(st.integers(min_value=1, max_value=1439))
    end_min = draw(st.integers(min_value=0, max_value=start_min - 1))
    return WorkingHours(
        days=days, start=_minute_to_hhmm(start_min), end=_minute_to_hhmm(end_min)
    )


# ---------------------------------------------------------------------------
# Oracle: independent reimplementation of the membership rule (Req 3.8/3.9/5.1/5.2)
# ---------------------------------------------------------------------------


def _expected_in_hours(wh: WorkingHours, capture_ts: str, tz_name: str) -> bool:
    """Independent oracle for the day-of-week + overnight membership rule."""
    utc_dt = datetime.strptime(capture_ts, _CAPTURE_TS_FMT).replace(tzinfo=timezone.utc)
    local_dt = utc_dt.astimezone(ZoneInfo(tz_name))
    weekday_index = local_dt.weekday()
    day_name = DAYS[weekday_index]
    t = local_dt.hour * 60 + local_dt.minute

    s = int(wh.start[:2]) * 60 + int(wh.start[3:])
    e = int(wh.end[:2]) * 60 + int(wh.end[3:])

    if s == e:
        return False
    if s < e:
        return day_name in wh.days and s <= t < e
    prev_day = DAYS[(weekday_index - 1) % 7]
    return (t >= s and day_name in wh.days) or (t < e and prev_day in wh.days)


# ---------------------------------------------------------------------------
# Property 1: Classification totality
# Validates: Requirements 5.3
# ---------------------------------------------------------------------------


# Feature: working-hours-retention, Property 1: Classification totality
@given(
    wh=st.one_of(st.none(), _working_hours()),
    capture_ts=_capture_ts,
    tz=_any_tz,
)
@settings(max_examples=200)
def test_classification_is_total(
    wh: WorkingHours | None, capture_ts: str, tz: str | None
) -> None:
    """Property 1: classify returns exactly one retention class, never unclassified.

    **Validates: Requirements 5.3**
    """
    result, _reason = classify(wh, capture_ts, tz)
    assert result in (RetentionClass.IN_HOURS, RetentionClass.OUT_OF_HOURS)


# ---------------------------------------------------------------------------
# Property 2: Classification membership correctness (day-of-week + overnight)
# Validates: Requirements 3.8, 3.9, 5.1, 5.2
# ---------------------------------------------------------------------------


# Feature: working-hours-retention, Property 2: Classification membership correctness (day-of-week + overnight)  # noqa: E501
@given(wh=_working_hours(), capture_ts=_capture_ts, tz=_valid_tz)
@settings(max_examples=200)
def test_membership_correctness(wh: WorkingHours, capture_ts: str, tz: str) -> None:
    """Property 2: In_Hours iff the site-local (weekday, minute) satisfies the
    normal / overnight membership rule; Out_Of_Hours otherwise.

    **Validates: Requirements 3.8, 3.9, 5.1, 5.2**
    """
    result, reason = classify(wh, capture_ts, tz)
    expected = (
        RetentionClass.IN_HOURS
        if _expected_in_hours(wh, capture_ts, tz)
        else RetentionClass.OUT_OF_HOURS
    )
    assert result is expected
    assert reason is None


# Feature: working-hours-retention, Property 2: Classification membership correctness (day-of-week + overnight)  # noqa: E501
@given(wh=_overnight_working_hours(), capture_ts=_capture_ts, tz=_valid_tz)
@settings(max_examples=200)
def test_membership_correctness_overnight(
    wh: WorkingHours, capture_ts: str, tz: str
) -> None:
    """Property 2 (overnight focus): windows that span midnight are anchored to
    the day the window begins; captures on both sides of the boundary classify
    per the overnight rule.

    **Validates: Requirements 3.9, 5.1, 5.2**
    """
    result, _reason = classify(wh, capture_ts, tz)
    expected = (
        RetentionClass.IN_HOURS
        if _expected_in_hours(wh, capture_ts, tz)
        else RetentionClass.OUT_OF_HOURS
    )
    assert result is expected


# ---------------------------------------------------------------------------
# Property 3: Missing/invalid timezone classifies as Out_Of_Hours
# Validates: Requirements 3.10
# ---------------------------------------------------------------------------


# Feature: working-hours-retention, Property 3: Missing/invalid timezone classifies as Out_Of_Hours
@given(wh=_working_hours(), capture_ts=_capture_ts, tz=_invalid_tz)
@settings(max_examples=200)
def test_invalid_timezone_is_out_of_hours(
    wh: WorkingHours, capture_ts: str, tz: str | None
) -> None:
    """Property 3: a missing/unrecognised timezone yields Out_Of_Hours with an
    error reason and mutates no state.

    **Validates: Requirements 3.10**
    """
    before = copy.deepcopy(wh)
    result, reason = classify(wh, capture_ts, tz)
    assert result is RetentionClass.OUT_OF_HOURS
    assert reason == "invalid_timezone"
    # No mutation of the supplied configuration.
    assert wh == before


# ---------------------------------------------------------------------------
# Property 4: Absent-configuration defaults to In_Hours
# Validates: Requirements 2.3, 2.5
# ---------------------------------------------------------------------------

# Site records with no working_hours and no *usable* legacy ingest_hours.
_unusable_legacy = st.one_of(
    st.just({}),                                              # neither attribute (Req 2.5)
    st.builds(lambda t: {"ingest_hours": {"start": t}}, _hhmm),   # end missing (Req 2.3)
    st.builds(lambda t: {"ingest_hours": {"end": t}}, _hhmm),     # start missing (Req 2.3)
    st.builds(                                                # start == end (Req 2.3)
        lambda t: {"ingest_hours": {"start": t, "end": t}}, _hhmm
    ),
    st.just({"ingest_hours": {}}),                            # empty legacy map (Req 2.3)
)


# Feature: working-hours-retention, Property 4: Absent-configuration defaults to In_Hours
@given(site_item=_unusable_legacy, capture_ts=_capture_ts, tz=_any_tz)
@settings(max_examples=200)
def test_absent_configuration_defaults_in_hours(
    site_item: dict, capture_ts: str, tz: str | None
) -> None:
    """Property 4: no working_hours and no usable legacy hours -> always In_Hours.

    **Validates: Requirements 2.3, 2.5**
    """
    resolved = resolve_working_hours(site_item)
    assert resolved is None
    result, reason = classify(resolved, capture_ts, tz)
    assert result is RetentionClass.IN_HOURS
    assert reason is None


# ---------------------------------------------------------------------------
# Property 5: Legacy migration equivalence (read-only)
# Validates: Requirements 2.1, 2.2
# ---------------------------------------------------------------------------


@st.composite
def _distinct_start_end(draw: st.DrawFn) -> tuple[str, str]:
    start = draw(_hhmm)
    end = draw(_hhmm.filter(lambda v: v != start))
    return start, end


# Feature: working-hours-retention, Property 5: Legacy migration equivalence (read-only)
@given(start_end=_distinct_start_end(), capture_ts=_capture_ts, tz=_valid_tz)
@settings(max_examples=200)
def test_legacy_migration_equivalence(
    start_end: tuple[str, str], capture_ts: str, tz: str
) -> None:
    """Property 5: a valid legacy ingest_hours resolves to an all-7-days
    WorkingHours without mutating the record, and classifies identically to an
    explicit all-7-days working_hours config.

    **Validates: Requirements 2.1, 2.2**
    """
    start, end = start_end
    legacy_item = {"ingest_hours": {"start": start, "end": end}}
    before = copy.deepcopy(legacy_item)

    resolved = resolve_working_hours(legacy_item)

    # Read-only derivation to all seven days (Req 2.2).
    assert resolved == WorkingHours(days=frozenset(DAYS), start=start, end=end)
    # The stored record is not mutated by resolution (Req 2.2).
    assert legacy_item == before

    # Equivalent to an explicit all-7-days working_hours config (Req 2.1/2.2).
    explicit_item = {
        "working_hours": {"days": list(DAYS), "start": start, "end": end}
    }
    explicit_resolved = resolve_working_hours(explicit_item)
    assert explicit_resolved == resolved

    assert classify(resolved, capture_ts, tz) == classify(
        explicit_resolved, capture_ts, tz
    )


# Feature: working-hours-retention, Property 5: Legacy migration equivalence (read-only)
@given(start_end=_distinct_start_end())
@settings(max_examples=100)
def test_working_hours_overrides_legacy(start_end: tuple[str, str]) -> None:
    """Property 5 (Req 2.1): when working_hours is present the legacy
    ingest_hours attribute is ignored."""
    start, end = start_end
    item = {
        "working_hours": {"days": ["mon"], "start": start, "end": end},
        "ingest_hours": {"start": "00:00", "end": "23:59"},
    }
    resolved = resolve_working_hours(item)
    assert resolved == WorkingHours(days=frozenset({"mon"}), start=start, end=end)


# ---------------------------------------------------------------------------
# Property 7: Omitted days defaults to all seven days
# Validates: Requirements 3.7
# ---------------------------------------------------------------------------


# Feature: working-hours-retention, Property 7: Omitted days defaults to all seven days
@given(start=_hhmm, end=_hhmm)
@settings(max_examples=100)
def test_omitted_days_defaults_to_all_seven(start: str, end: str) -> None:
    """Property 7: an accepted working_hours object that omits days resolves to
    all seven days {mon..sun}.

    **Validates: Requirements 3.7**
    """
    item = {"working_hours": {"start": start, "end": end}}
    resolved = resolve_working_hours(item)
    assert resolved is not None
    assert resolved.days == frozenset(DAYS)
