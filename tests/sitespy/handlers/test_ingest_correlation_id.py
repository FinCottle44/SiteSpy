"""Correlation ID reuse and generation tests.

Requirements validated: 10.1
"""

from __future__ import annotations

import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from sitespy.handlers.ingest import resolve_correlation_id

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_VALID_CORR_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _make_event(correlation_id=None):
    headers = {}
    if correlation_id is not None:
        headers["X-Correlation-Id"] = correlation_id
    return {"headers": headers}


# ---------------------------------------------------------------------------
# Valid header → reused verbatim
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "corr_id",
    [
        "abc123",
        "A-B_C",
        "a" * 128,
        "1",
        "test-correlation-id-12345",
    ],
)
def test_valid_correlation_id_reused(corr_id):
    """Valid X-Correlation-Id is returned verbatim."""
    event = _make_event(corr_id)
    result = resolve_correlation_id(event)
    assert result == corr_id


# ---------------------------------------------------------------------------
# Invalid / missing header → fresh UUID v4
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "corr_id",
    [
        None,  # missing header
        "",  # empty string
        "a" * 129,  # too long (>128)
        "has space",  # invalid character
        "has!bang",  # invalid character
        "has@at",  # invalid character
    ],
)
def test_invalid_correlation_id_generates_uuid(corr_id):
    """Invalid or missing X-Correlation-Id generates a fresh UUID v4."""
    event = _make_event(corr_id)
    result = resolve_correlation_id(event)
    assert _UUID_RE.match(result), f"Expected UUID v4, got: {result}"


# ---------------------------------------------------------------------------
# Hypothesis: any valid string is reused; any invalid generates UUID
# ---------------------------------------------------------------------------


@given(st.from_regex(r"^[A-Za-z0-9_-]{1,128}$", fullmatch=True))
@settings(max_examples=50)
def test_hypothesis_valid_correlation_id_reused(corr_id):
    """@given: any string matching the valid regex is reused verbatim."""
    event = _make_event(corr_id)
    result = resolve_correlation_id(event)
    assert result == corr_id
