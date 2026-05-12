"""Field-level validators for SiteSpy admin management endpoints.

Provides reusable validation functions for tenant IDs, site IDs, camera IDs,
email addresses, geographic coordinates, and IANA timezone identifiers.

Requirements validated: 1.4, 1.5, 1.6, 2.6, 2.7, 2.8, 2.11, 3.6, 3.7, 3.8, 4.7, 4.8, 4.9
"""

from __future__ import annotations

import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ---------------------------------------------------------------------------
# Regex constants
# ---------------------------------------------------------------------------

TENANT_ID_RE = re.compile(r"^[a-z0-9_]{3,32}$")
SITE_ID_RE = re.compile(r"^[a-z0-9_]{1,64}$")
CAMERA_ID_RE = re.compile(r"^[a-z0-9_]{1,64}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def validate_tenant_id(value: str) -> bool:
    """Validate that a tenant_id matches the required pattern.

    Must be 3–32 characters consisting of lowercase letters, digits, and
    underscores.
    """
    return bool(TENANT_ID_RE.match(value))


def validate_site_id(value: str) -> bool:
    """Validate that a site_id matches the required pattern.

    Must be 1–64 characters consisting of lowercase letters, digits, and
    underscores.
    """
    return bool(SITE_ID_RE.match(value))


def validate_camera_id(value: str) -> bool:
    """Validate that a camera_id matches the required pattern.

    Must be 1–64 characters consisting of lowercase letters, digits, and
    underscores.
    """
    return bool(CAMERA_ID_RE.match(value))


def validate_email(value: str) -> bool:
    """Validate that an email address has a basic valid format.

    Checks for the pattern: non-whitespace@non-whitespace.non-whitespace
    and that the total length does not exceed 254 characters.
    """
    if len(value) > 254:
        return False
    return bool(EMAIL_RE.match(value))


def validate_latitude(value: float) -> bool:
    """Validate that a latitude is within the valid range [-90, 90]."""
    return -90 <= value <= 90


def validate_longitude(value: float) -> bool:
    """Validate that a longitude is within the valid range [-180, 180]."""
    return -180 <= value <= 180


def validate_stale_threshold_hours(value: object) -> bool:
    """Validate that stale_threshold_hours is an integer in [1, 720].

    Rejects non-integer types (including floats) and values outside the
    valid range.
    """
    if not isinstance(value, int) or isinstance(value, bool):
        return False
    return 1 <= value <= 720


def validate_timezone(value: str) -> bool:
    """Validate that a timezone string is a valid IANA timezone identifier.

    Uses zoneinfo.ZoneInfo to verify the timezone exists.
    """
    try:
        ZoneInfo(value)
        return True
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return False
