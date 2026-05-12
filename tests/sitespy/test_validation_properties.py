"""Property-based tests for the sitespy.validation module.

Feature: admin-management-endpoints
Property 2: ID format validation rejects non-conforming identifiers
Property 9: Latitude/longitude range validation
Property 10: stale_threshold_hours range validation

Validates: Requirements 1.4, 1.8, 2.6, 2.8, 3.6
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from sitespy.validation import (
    CAMERA_ID_RE,
    SITE_ID_RE,
    TENANT_ID_RE,
    validate_camera_id,
    validate_latitude,
    validate_longitude,
    validate_site_id,
    validate_stale_threshold_hours,
    validate_tenant_id,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Characters that are NOT in the valid ID alphabet [a-z0-9_]
_INVALID_ID_CHARS = (
    st.characters(
        blacklist_categories=("Cs",),  # exclude surrogates
        blacklist_characters="abcdefghijklmnopqrstuvwxyz0123456789_",
    )
)

# Strings that violate tenant_id pattern: either wrong length or wrong chars
_invalid_tenant_id_too_short = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789_", min_size=0, max_size=2
)
_invalid_tenant_id_too_long = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789_", min_size=33, max_size=64
)
_invalid_tenant_id_bad_chars = st.text(min_size=3, max_size=32).filter(
    lambda s: not TENANT_ID_RE.match(s)
)

_invalid_tenant_ids = st.one_of(
    _invalid_tenant_id_too_short,
    _invalid_tenant_id_too_long,
    _invalid_tenant_id_bad_chars,
)

# Strings that violate site_id / camera_id pattern: empty or too long or wrong chars
_invalid_site_id_empty = st.just("")
_invalid_site_id_too_long = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789_", min_size=65, max_size=100
)
_invalid_site_id_bad_chars = st.text(min_size=1, max_size=64).filter(
    lambda s: not SITE_ID_RE.match(s)
)

_invalid_site_ids = st.one_of(
    _invalid_site_id_empty,
    _invalid_site_id_too_long,
    _invalid_site_id_bad_chars,
)

_invalid_camera_ids = st.one_of(
    _invalid_site_id_empty,
    _invalid_site_id_too_long,
    st.text(min_size=1, max_size=64).filter(lambda s: not CAMERA_ID_RE.match(s)),
)

# Floats outside valid latitude range [-90, 90]
_invalid_latitudes = st.one_of(
    st.floats(min_value=90.0001, max_value=1e10, allow_nan=False, allow_infinity=False),
    st.floats(min_value=-1e10, max_value=-90.0001, allow_nan=False, allow_infinity=False),
)

# Floats outside valid longitude range [-180, 180]
_invalid_longitudes = st.one_of(
    st.floats(min_value=180.0001, max_value=1e10, allow_nan=False, allow_infinity=False),
    st.floats(min_value=-1e10, max_value=-180.0001, allow_nan=False, allow_infinity=False),
)

# Valid latitudes and longitudes (for positive testing)
_valid_latitudes = st.floats(min_value=-90, max_value=90, allow_nan=False, allow_infinity=False)
_valid_longitudes = st.floats(
    min_value=-180, max_value=180, allow_nan=False, allow_infinity=False
)

# stale_threshold_hours: values outside [1, 720]
_invalid_stale_threshold_below = st.integers(min_value=-1000, max_value=0)
_invalid_stale_threshold_above = st.integers(min_value=721, max_value=10000)
_invalid_stale_threshold_not_int = st.one_of(
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(min_size=1, max_size=10),
    st.booleans(),
    st.none(),
)

_invalid_stale_thresholds = st.one_of(
    _invalid_stale_threshold_below,
    _invalid_stale_threshold_above,
    _invalid_stale_threshold_not_int,
)

_valid_stale_thresholds = st.integers(min_value=1, max_value=720)


# ---------------------------------------------------------------------------
# Property 2: ID format validation rejects non-conforming identifiers
# Validates: Requirements 1.4, 2.6, 3.6
# ---------------------------------------------------------------------------


@given(value=_invalid_tenant_ids)
@settings(max_examples=200)
def test_tenant_id_rejects_non_conforming(value: str) -> None:
    """Property 2: validate_tenant_id rejects strings not matching ^[a-z0-9_]{3,32}$.

    **Validates: Requirements 1.4**
    """
    assert validate_tenant_id(value) is False


@given(value=_invalid_site_ids)
@settings(max_examples=200)
def test_site_id_rejects_non_conforming(value: str) -> None:
    """Property 2: validate_site_id rejects strings not matching ^[a-z0-9_]{1,64}$.

    **Validates: Requirements 2.6**
    """
    assert validate_site_id(value) is False


@given(value=_invalid_camera_ids)
@settings(max_examples=200)
def test_camera_id_rejects_non_conforming(value: str) -> None:
    """Property 2: validate_camera_id rejects strings not matching ^[a-z0-9_]{1,64}$.

    **Validates: Requirements 3.6**
    """
    assert validate_camera_id(value) is False


# ---------------------------------------------------------------------------
# Property 9: Latitude/longitude range validation
# Validates: Requirements 2.8
# ---------------------------------------------------------------------------


@given(value=_invalid_latitudes)
@settings(max_examples=200)
def test_latitude_rejects_out_of_range(value: float) -> None:
    """Property 9: validate_latitude rejects values outside [-90, 90].

    **Validates: Requirements 2.8**
    """
    assert validate_latitude(value) is False


@given(value=_invalid_longitudes)
@settings(max_examples=200)
def test_longitude_rejects_out_of_range(value: float) -> None:
    """Property 9: validate_longitude rejects values outside [-180, 180].

    **Validates: Requirements 2.8**
    """
    assert validate_longitude(value) is False


@given(value=_valid_latitudes)
@settings(max_examples=200)
def test_latitude_accepts_valid_range(value: float) -> None:
    """Property 9: validate_latitude accepts values within [-90, 90].

    **Validates: Requirements 2.8**
    """
    assert validate_latitude(value) is True


@given(value=_valid_longitudes)
@settings(max_examples=200)
def test_longitude_accepts_valid_range(value: float) -> None:
    """Property 9: validate_longitude accepts values within [-180, 180].

    **Validates: Requirements 2.8**
    """
    assert validate_longitude(value) is True


# ---------------------------------------------------------------------------
# Property 10: stale_threshold_hours range validation
# Validates: Requirements 1.8
# ---------------------------------------------------------------------------


@given(value=_invalid_stale_thresholds)
@settings(max_examples=200)
def test_stale_threshold_hours_rejects_invalid(value: object) -> None:
    """Property 10: validate_stale_threshold_hours rejects values outside [1, 720] or non-integers.

    **Validates: Requirements 1.8**
    """
    assert validate_stale_threshold_hours(value) is False


@given(value=_valid_stale_thresholds)
@settings(max_examples=200)
def test_stale_threshold_hours_accepts_valid(value: int) -> None:
    """Property 10: validate_stale_threshold_hours accepts integers in [1, 720].

    **Validates: Requirements 1.8**
    """
    assert validate_stale_threshold_hours(value) is True
