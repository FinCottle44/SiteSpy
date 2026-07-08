"""Shared download-field handling for SiteSpy timelapse endpoints.

A small AWS-touching helper shared by ``timelapse_list.py`` and
``timelapse_get.py``. It builds the download-related fragment of a job's
response entry, checking Artifact existence in S3 *before* presigning so a
``complete`` job whose Artifact has expired never yields a broken link.

This helper is deliberately kept out of the pure ``sitespy.timelapse`` module
(which stays AWS-independent) because it touches S3 via ``sitespy.storage``.

Requirements validated: 5.1, 5.2, 5.3
"""

from __future__ import annotations

from typing import Any

from sitespy import storage
from sitespy.config import get_settings
from sitespy.timelapse import STATUS_COMPLETE


def build_download_fields(status: str, artifact_key: str) -> dict[str, Any]:
    """Return the download-related response fragment for a job entry.

    Behavior:
        - ``status != complete``                -> ``{}``  (Req 5.2)
        - ``complete`` and Artifact exists      -> ``{"download_url": <presigned>,
          "expires_in": <ttl>}``  (Req 5.1)
        - ``complete`` and Artifact missing     -> ``{"artifact_available": False}``
          (Req 5.3)

    Existence is checked with ``storage.timelapse_artifact_exists`` (HeadObject)
    before presigning, so a ``complete`` job whose Artifact has expired never
    yields a broken link. The presign TTL is ``settings.artifact_presign_ttl``
    (3600 seconds).

    Args:
        status:       The Timelapse_Job Lifecycle_Status.
        artifact_key: The canonical S3 object key of the Artifact.

    Returns:
        A dict fragment to merge into the job's response entry.
    """
    if status != STATUS_COMPLETE:
        return {}

    if not storage.timelapse_artifact_exists(artifact_key):
        return {"artifact_available": False}

    expires_in = get_settings().artifact_presign_ttl
    download_url = storage.generate_presigned_url(artifact_key, expires_in=expires_in)
    return {"download_url": download_url, "expires_in": expires_in}
