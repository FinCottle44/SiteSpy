"""Camera ingest token generation for SiteSpy.

Provides cryptographically secure opaque token generation for camera
devices that authenticate by including the token in the ingest URL path.
"""

from __future__ import annotations

import secrets
import string

_ALPHABET = string.ascii_letters + string.digits + "_-"

_TOKEN_PREFIX = "tk_"
_TOKEN_RANDOM_LENGTH = 40


def generate_ingest_token() -> str:
    """Generate a camera ingest token.

    Returns:
        A string of the form ``tk_<40 random chars>`` where the random
        portion uses mixed-case alphanumeric characters plus ``_`` and ``-``.
        Total length: 43 characters (matches the ingest handler's
        ``^tk_[A-Za-z0-9_-]{20,80}$`` validation regex).
    """
    suffix = "".join(secrets.choice(_ALPHABET) for _ in range(_TOKEN_RANDOM_LENGTH))
    return f"{_TOKEN_PREFIX}{suffix}"
