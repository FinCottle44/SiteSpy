"""Property-based tests for the shared timelapse download-field helper.

Helper under test: sitespy.timelapse_download.build_download_fields

Feature: timelapse-job-listing
Property 8: Missing artifact yields an availability indicator, never a broken link

Validates: Requirements 5.3, 5.4
"""

from __future__ import annotations

import os
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Environment setup (must happen before importing the helper / config)
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

from sitespy import timelapse_download  # noqa: E402
from sitespy.config import get_settings  # noqa: E402
from sitespy.timelapse import (  # noqa: E402
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_PROCESSING,
    STATUS_QUEUED,
)

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_EXPECTED_TTL = 3600

# All four lifecycle statuses; only ``complete`` is download-eligible.
_STATUSES = st.sampled_from(
    [STATUS_QUEUED, STATUS_PROCESSING, STATUS_COMPLETE, STATUS_FAILED]
)

# Non-empty artifact keys (S3 object keys) safe for presigning.
_ARTIFACT_KEYS = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N"), max_codepoint=0x7F),
    min_size=1,
    max_size=60,
).map(lambda s: f"timelapse/{s}.mp4")


# ---------------------------------------------------------------------------
# Property 8: Missing artifact yields an availability indicator,
#             never a broken link
# Validates: Requirements 5.3, 5.4
# ---------------------------------------------------------------------------


@given(status=_STATUSES, exists=st.booleans(), artifact_key=_ARTIFACT_KEYS)
@settings(max_examples=200, deadline=None)
def test_missing_artifact_yields_availability_indicator_never_broken_link(
    status: str, exists: bool, artifact_key: str
) -> None:
    """The shared helper never emits a broken link for a missing artifact.

    For any ``(status, exists)`` pair the fragment returned by
    ``build_download_fields`` is:

    - ``{}``                                      for any non-``complete`` status
    - ``{download_url, expires_in=3600}``          for ``complete`` + artifact exists
    - ``{artifact_available: False}``              for ``complete`` + artifact missing

    In every case a ``download_url`` is present *only* when the artifact
    actually exists, so a broken link is never emitted.

    Feature: timelapse-job-listing, Property 8: Missing artifact yields an
    availability indicator, never a broken link

    **Validates: Requirements 5.3, 5.4**
    """
    presigned = f"https://example.com/{artifact_key}?signature=abc"

    with patch.object(
        timelapse_download.storage,
        "timelapse_artifact_exists",
        return_value=exists,
    ) as mock_exists, patch.object(
        timelapse_download.storage,
        "generate_presigned_url",
        return_value=presigned,
    ) as mock_presign:
        fragment = timelapse_download.build_download_fields(status, artifact_key)

    if status != STATUS_COMPLETE:
        # Non-complete statuses never touch S3 and yield an empty fragment.
        assert fragment == {}
        mock_exists.assert_not_called()
        mock_presign.assert_not_called()
    elif exists:
        # Complete + artifact present -> fresh presigned URL + 3600s expiry.
        assert fragment == {
            "download_url": presigned,
            "expires_in": _EXPECTED_TTL,
        }
        # expires_in is a positive integer of exactly 3600 seconds.
        assert isinstance(fragment["expires_in"], int)
        assert fragment["expires_in"] == get_settings().artifact_presign_ttl
        assert fragment["expires_in"] > 0
    else:
        # Complete + artifact missing -> availability indicator, no link.
        assert fragment == {"artifact_available": False}
        # A broken link is never emitted.
        assert "download_url" not in fragment
        assert "expires_in" not in fragment
        mock_presign.assert_not_called()

    # In no case is a download_url emitted when the artifact does not exist.
    if not (status == STATUS_COMPLETE and exists):
        assert "download_url" not in fragment
