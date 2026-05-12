"""Property-based tests for ingest token generation.

Feature: admin-management-endpoints, Property 4: Ingest token format invariant

**Validates: Requirements 3.10, 6.6**
"""

from __future__ import annotations

import string

from hypothesis import given, settings
from hypothesis import strategies as st

from sitespy.credentials import generate_ingest_token

_TOKEN_ALPHABET = set(string.ascii_letters + string.digits + "_-")
_TOKEN_PREFIX = "tk_"


@given(st.integers(min_value=0, max_value=99))
@settings(max_examples=100)
def test_token_has_correct_prefix(_iteration: int) -> None:
    """Ingest token format invariant — prefix.

    For any invocation of the token generation function, the returned
    token SHALL start with 'tk_'.
    """
    token = generate_ingest_token()
    assert token.startswith(_TOKEN_PREFIX), (
        f"Token '{token}' does not start with '{_TOKEN_PREFIX}'"
    )


@given(st.integers(min_value=0, max_value=99))
@settings(max_examples=100)
def test_token_is_exactly_43_chars(_iteration: int) -> None:
    """Ingest token format invariant — length.

    For any invocation of the token generation function, the returned
    token SHALL be exactly 43 characters (3 prefix + 40 random).
    """
    token = generate_ingest_token()
    assert len(token) == 43, f"Token length {len(token)} != 43"


@given(st.integers(min_value=0, max_value=99))
@settings(max_examples=100)
def test_token_suffix_uses_valid_alphabet(_iteration: int) -> None:
    """Ingest token format invariant — character set.

    For any invocation of the token generation function, the random suffix
    SHALL contain only alphanumeric characters plus '_' and '-'.
    This matches the ingest handler's validation regex: ^tk_[A-Za-z0-9_-]{20,80}$
    """
    token = generate_ingest_token()
    suffix = token[len(_TOKEN_PREFIX):]
    assert len(suffix) == 40, f"Token suffix length {len(suffix)} != 40"
    assert all(c in _TOKEN_ALPHABET for c in suffix), (
        f"Token suffix '{suffix}' contains invalid characters"
    )
