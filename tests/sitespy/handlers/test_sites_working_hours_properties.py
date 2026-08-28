"""Property-based tests for the Sites API working_hours `days` validation.

Feature: working-hours-retention
Property 6: days validation acceptance

The Sites_Api accepts a `days` list iff it has between 1 and 7 entries, contains
no duplicates, and every entry is an exact-lowercase member of
{mon,tue,wed,thu,fri,sat,sun}; otherwise it rejects the request with HTTP 400
and persists no change.

Validates: Requirements 3.5, 3.6
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Environment setup (must happen before importing handler)
# ---------------------------------------------------------------------------

os.environ.setdefault("SNAPSHOTS_BUCKET", "test-snapshots-bucket")
os.environ.setdefault("DATA_TABLE", "test-data-table")
os.environ.setdefault("AWS_REGION", "eu-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-west-2")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("LOG_LEVEL", "INFO")

from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from sitespy.handlers.sites_patch import handler  # noqa: E402
from sitespy.retention import DAYS  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

_SITE_ITEM = {
    "PK": {"S": "TENANT#acme_corp"},
    "SK": {"S": "SITE#site_001"},
    "site_name": {"S": "Acme Tower"},
    "timezone": {"S": "Europe/London"},
}


def _make_event(body: dict[str, Any]) -> dict[str, Any]:
    """Build a minimal PATCH /v1/sites/{site_id} event for a tenant admin."""
    return {
        "httpMethod": "PATCH",
        "path": "/v1/sites/site_001",
        "pathParameters": {"site_id": "site_001"},
        "queryStringParameters": {},
        "headers": {"X-Correlation-Id": "test-corr-id"},
        "body": json.dumps(body),
        "requestContext": {
            "authorizer": {
                "claims": {
                    "cognito:groups": "TenantAdmins",
                    "custom:tenant_id": "acme_corp",
                    "custom:site_access": "",
                }
            }
        },
    }


def _is_valid_days(days: list[Any]) -> bool:
    """Independent oracle for the acceptance predicate (Req 3.5, 3.6)."""
    if not (1 <= len(days) <= 7):
        return False
    if len(set(days)) != len(days):
        return False
    return all(isinstance(d, str) and d in DAYS for d in days)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# A pool of candidate day entries: exact-lowercase members, wrong-case variants,
# and unrecognised values — so empty/oversized/duplicate/unknown/wrong-case
# lists are all exercised.
_valid_day = st.sampled_from(DAYS)
_wrong_case_day = st.sampled_from(
    [d.upper() for d in DAYS] + [d.capitalize() for d in DAYS]
)
_unknown_day = st.sampled_from(["monday", "funday", "xyz", "", "mo", "sunn"])
_any_day_entry = st.one_of(_valid_day, _wrong_case_day, _unknown_day)

# Lists from length 0 (empty → invalid) to 9 (oversized → invalid), covering
# duplicates and mixed-validity entries in between.
_days_lists = st.lists(_any_day_entry, min_size=0, max_size=9)


# ---------------------------------------------------------------------------
# Property 6: days validation acceptance
# ---------------------------------------------------------------------------


# Feature: working-hours-retention, Property 6: days validation acceptance
@settings(max_examples=200)
@given(days=_days_lists)
def test_days_validation_acceptance(days: list[str]) -> None:
    """Accept iff 1–7 exact-lowercase, no-duplicate members; else 400, no change.

    Validates: Requirements 3.5, 3.6
    """
    body = {"working_hours": {"start": "07:00", "end": "18:00", "days": days}}
    event = _make_event(body)

    update_mock = MagicMock()
    with (
        patch("sitespy.data.get_site", return_value=_SITE_ITEM),
        patch("sitespy.data.update_site", update_mock),
    ):
        result = handler(event, MagicMock())

    expected_accepted = _is_valid_days(days)

    if expected_accepted:
        assert result["statusCode"] == 200, (days, result["body"])
        # The accepted value must have been persisted.
        assert update_mock.called
    else:
        assert result["statusCode"] == 400, (days, result["body"])
        parsed = json.loads(result["body"])
        assert parsed["error"] == "BAD_REQUEST"
        # A rejected request must persist no change (Req 3.6).
        assert not update_mock.called
