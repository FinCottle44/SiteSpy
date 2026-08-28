"""Pure retention classifier for SiteSpy working-hours retention.

This module is intentionally free of I/O so the classification logic can be
unit- and property-tested in isolation. It resolves a site's working-hours
configuration (with backward-compatible reading of the legacy ``ingest_hours``
attribute) and classifies a capture timestamp as ``In_Hours`` or
``Out_Of_Hours`` in the site's timezone, with day-of-week and overnight-window
support.

Requirements validated: 2.1, 2.2, 2.3, 2.5, 3.7, 3.8, 3.9, 3.10, 5.1, 5.2, 5.3
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Monday-based weekday order; index 0..6 aligns with datetime.weekday().
DAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Capture timestamps are UTC ISO-8601 with a literal Z suffix, second precision.
_CAPTURE_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

# HH:MM in the inclusive range 00:00–23:59.
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkingHours:
    """Resolved working-hours configuration.

    Attributes:
        days:  Non-empty subset of ``DAYS`` the window applies to.
        start: Window start as ``"HH:MM"``.
        end:   Window end as ``"HH:MM"``.
    """

    days: frozenset[str]
    start: str
    end: str


class RetentionClass(str, Enum):
    """Retention class assigned to a saved snapshot."""

    IN_HOURS = "In_Hours"
    OUT_OF_HOURS = "Out_Of_Hours"


# ---------------------------------------------------------------------------
# DynamoDB attribute extraction helpers
# ---------------------------------------------------------------------------
#
# Site records read via data.get_site are raw DynamoDB-typed items, e.g.
#   working_hours -> {"M": {"days": {"L": [{"S": "mon"}, ...]},
#                           "start": {"S": "07:00"}, "end": {"S": "18:00"}}}
#   ingest_hours  -> {"M": {"start": {"S": "07:00"}, "end": {"S": "18:00"}}}
# The helpers below also accept already-unwrapped plain values so the module is
# convenient to exercise directly.


def _extract_map(attr: Any) -> Mapping[str, Any] | None:
    """Return the inner map of a DynamoDB ``M`` attribute (or a plain dict)."""
    if attr is None:
        return None
    if isinstance(attr, Mapping):
        inner = attr.get("M")
        if isinstance(inner, Mapping):
            return inner
        return attr
    return None


def _extract_str(field: Any) -> str | None:
    """Return the string value of a DynamoDB ``S`` attribute (or a plain str)."""
    if isinstance(field, str):
        return field
    if isinstance(field, Mapping):
        value = field.get("S")
        if isinstance(value, str):
            return value
    return None


def _extract_days(field: Any) -> list[str] | None:
    """Return the list of day strings from a DynamoDB ``L`` attribute or list."""
    if field is None:
        return None
    raw: Any = None
    if isinstance(field, Mapping) and "L" in field:
        raw = field["L"]
    elif isinstance(field, (list, tuple)):
        raw = field
    if raw is None:
        return None
    days: list[str] = []
    for entry in raw:
        value = _extract_str(entry)
        if value is not None:
            days.append(value)
    return days


def _parse_hhmm(value: str | None) -> int | None:
    """Parse ``"HH:MM"`` into minutes since midnight, or ``None`` if malformed."""
    if value is None:
        return None
    match = _TIME_RE.match(value)
    if match is None:
        return None
    hours, minutes = int(match.group(1)), int(match.group(2))
    return hours * 60 + minutes


def _resolve_zone(tz_name: str | None) -> ZoneInfo | None:
    """Return a ZoneInfo for a valid IANA name, else ``None``."""
    if not tz_name:
        return None
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Working-hours resolution (Requirement 2)
# ---------------------------------------------------------------------------


def resolve_working_hours(site_item: Mapping[str, Any]) -> WorkingHours | None:
    """Resolve working hours from a site record, backward-compatibly.

    Resolution order (Requirement 2):

    - ``working_hours`` present  -> parse and return it, ignoring any legacy
      ``ingest_hours`` attribute on the same record (Req 2.1). A ``days`` field
      that is absent or empty defaults to all seven days (Req 3.7).
    - else a valid legacy ``ingest_hours`` (``start`` and ``end`` both present,
      each a valid ``HH:MM``, and ``start != end``) -> derive
      ``WorkingHours(days=all seven, start, end)`` without mutating the stored
      record (Req 2.2).
    - else ``None`` -> the caller treats this as "always In_Hours"
      (Req 2.3, 2.5).

    Args:
        site_item: Raw DynamoDB-typed site record (or an already-unwrapped dict).

    Returns:
        The resolved ``WorkingHours``, or ``None`` when no usable configuration
        exists.
    """
    working_hours = _extract_map(site_item.get("working_hours"))
    if working_hours is not None:
        start = _extract_str(working_hours.get("start"))
        end = _extract_str(working_hours.get("end"))
        if start is not None and end is not None:
            day_list = _extract_days(working_hours.get("days"))
            if day_list:
                days = frozenset(day_list)
            else:
                # Omitted/empty days defaults to all seven (Req 3.7).
                days = frozenset(DAYS)
            return WorkingHours(days=days, start=start, end=end)
        # working_hours present but unusable: still ignore legacy (Req 2.1).
        return None

    legacy = _extract_map(site_item.get("ingest_hours"))
    if legacy is not None:
        start = _extract_str(legacy.get("start"))
        end = _extract_str(legacy.get("end"))
        if (
            start is not None
            and end is not None
            and _TIME_RE.match(start) is not None
            and _TIME_RE.match(end) is not None
            and start != end
        ):
            return WorkingHours(days=frozenset(DAYS), start=start, end=end)

    return None


# ---------------------------------------------------------------------------
# Classification (Requirements 3, 5)
# ---------------------------------------------------------------------------


def _in_window(
    day_name: str,
    weekday_index: int,
    minute_of_day: int,
    start: int | None,
    end: int | None,
    days: frozenset[str],
) -> bool:
    """Return True if a site-local (weekday, minute) falls inside the window.

    - Normal window (``start < end``): In_Hours iff ``day in days`` and
      ``start <= t < end`` (Req 5.1).
    - Overnight window (``start > end``): the window that begins on day ``D``
      spans ``[start on D, end on D+1)``; In_Hours iff
      ``(t >= start and day in days)`` or ``(t < end and prev(day) in days)``
      (Req 3.9).
    - Degenerate window (``start == end`` or unparseable): the in-hours interval
      is empty -> always Out_Of_Hours.
    """
    if start is None or end is None or start == end:
        return False
    if start < end:
        return day_name in days and start <= minute_of_day < end
    # Overnight window spanning midnight.
    prev_day = DAYS[(weekday_index - 1) % 7]
    return (minute_of_day >= start and day_name in days) or (
        minute_of_day < end and prev_day in days
    )


def classify(
    working_hours: WorkingHours | None,
    capture_ts: str,
    site_timezone: str | None,
) -> tuple[RetentionClass, str | None]:
    """Classify a capture as In_Hours or Out_Of_Hours.

    Args:
        working_hours: Resolved configuration, or ``None`` when unconfigured.
        capture_ts:    Capture instant as UTC ``"YYYY-MM-DDTHH:MM:SSZ"``.
        site_timezone: IANA timezone identifier for the site, or ``None``.

    Returns:
        A ``(RetentionClass, error_reason)`` tuple:

        - ``working_hours is None`` -> ``(IN_HOURS, None)`` (Req 2.3, 2.5).
        - missing/unrecognised timezone -> ``(OUT_OF_HOURS, "invalid_timezone")``
          (Req 3.10).
        - capture inside the window (site-local) -> ``(IN_HOURS, None)`` (Req 5.1).
        - otherwise -> ``(OUT_OF_HOURS, None)`` (Req 5.2).

    The function never mutates any stored state and always returns exactly one
    retention class (Req 5.3).
    """
    if working_hours is None:
        return (RetentionClass.IN_HOURS, None)

    zone = _resolve_zone(site_timezone)
    if zone is None:
        return (RetentionClass.OUT_OF_HOURS, "invalid_timezone")

    # Convert the UTC capture instant to site-local weekday and minute-of-day.
    utc_dt = datetime.strptime(capture_ts, _CAPTURE_TS_FMT).replace(
        tzinfo=timezone.utc
    )
    local_dt = utc_dt.astimezone(zone)
    weekday_index = local_dt.weekday()  # 0=Mon .. 6=Sun
    day_name = DAYS[weekday_index]
    minute_of_day = local_dt.hour * 60 + local_dt.minute

    start = _parse_hhmm(working_hours.start)
    end = _parse_hhmm(working_hours.end)

    if _in_window(day_name, weekday_index, minute_of_day, start, end, working_hours.days):
        return (RetentionClass.IN_HOURS, None)
    return (RetentionClass.OUT_OF_HOURS, None)
