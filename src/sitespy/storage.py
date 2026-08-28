"""S3 storage helpers for SiteSpy — canonical key construction and snapshot writes.

Requirements validated: 5.2, 5.3, 5.4, 5.5, 5.6, 5.8, 6.1
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import boto3
import botocore.config
from botocore.exceptions import ClientError

from sitespy.config import get_settings

_BOTO_CONFIG = botocore.config.Config(retries={"mode": "standard", "max_attempts": 3})


@lru_cache(maxsize=1)
def _s3_client() -> Any:
    return boto3.client(
        "s3",
        region_name=get_settings().aws_region,
        config=_BOTO_CONFIG,
    )


def build_snapshot_key(
    tenant_id: str,
    site_id: str,
    camera_id: str,
    snapshot_ts: str,
) -> str:
    """Build the canonical S3 key for a snapshot.

    Key format: <tenant_id>/<site_id>/<camera_id>/<YYYY>/<MM>/<DD>/<snapshot_ts>.jpg

    Date components are parsed from snapshot_ts itself (format YYYY-MM-DDTHH:mm:ssZ)
    so there is a single source of truth for the date segments.

    Args:
        tenant_id:   Tenant identifier.
        site_id:     Site identifier.
        camera_id:   Camera identifier.
        snapshot_ts: UTC timestamp in YYYY-MM-DDTHH:mm:ssZ format.

    Returns:
        The canonical S3 object key string.
    """
    # snapshot_ts format: 2025-06-15T14:00:00Z
    # Extract date components from the timestamp string directly.
    date_part = snapshot_ts[:10]  # "2025-06-15"
    yyyy, mm, dd = date_part.split("-")
    return f"{tenant_id}/{site_id}/{camera_id}/{yyyy}/{mm}/{dd}/{snapshot_ts}.jpg"


def parse_snapshot_key(key: str) -> tuple[str, str, str, str]:
    """Parse a canonical S3 key back into its component parts.

    Inverse of build_snapshot_key. Key format:
    <tenant_id>/<site_id>/<camera_id>/<YYYY>/<MM>/<DD>/<snapshot_ts>.jpg

    Args:
        key: A canonical S3 object key produced by build_snapshot_key.

    Returns:
        A tuple (tenant_id, site_id, camera_id, snapshot_ts).
    """
    parts = key.split("/")
    # parts: [tenant_id, site_id, camera_id, YYYY, MM, DD, snapshot_ts.jpg]
    tenant_id = parts[0]
    site_id = parts[1]
    camera_id = parts[2]
    # parts[3], parts[4], parts[5] are YYYY, MM, DD — embedded in snapshot_ts
    snapshot_ts = parts[6].removesuffix(".jpg")
    return (tenant_id, site_id, camera_id, snapshot_ts)


def generate_presigned_url(key: str, expires_in: int = 300) -> str:
    """Generate a pre-signed S3 GET URL for a snapshot.

    Args:
        key:        Canonical S3 object key.
        expires_in: TTL in seconds (default 300 = 5 minutes).

    Returns:
        A pre-signed HTTPS URL string.
    """
    return _s3_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": get_settings().snapshots_bucket, "Key": key},
        ExpiresIn=expires_in,
    )


def delete_snapshot(key: str) -> None:
    """Delete a snapshot object from S3.

    Args:
        key: Canonical S3 object key to delete.
    """
    _s3_client().delete_object(
        Bucket=get_settings().snapshots_bucket,
        Key=key,
    )


def put_snapshot(
    key: str,
    body: bytes,
    sha256_hex: str,
    snapshot_ts: str,
    tenant_id: str,
    retention_years: int,
) -> None:
    """Write a JPEG snapshot to S3 with integrity metadata and retention tags.

    Args:
        key:             Canonical S3 object key.
        body:            Raw JPEG bytes.
        sha256_hex:      Lowercase hex SHA-256 digest of body.
        snapshot_ts:     UTC timestamp (YYYY-MM-DDTHH:mm:ssZ).
        tenant_id:       Tenant identifier (used in the retention tag).
        retention_years: Retention period in years (used in the retention tag).
    """
    _s3_client().put_object(
        Bucket=get_settings().snapshots_bucket,
        Key=key,
        Body=body,
        ContentType="image/jpeg",
        Metadata={"sha256": sha256_hex, "ingested-at": snapshot_ts},
        Tagging=f"tenant_id={tenant_id}&retention_years={retention_years}",
    )


def build_live_snapshot_key(
    tenant_id: str,
    site_id: str,
    camera_id: str,
    snapshot_ts: str,
) -> str:
    """Build the canonical S3 key for a live-view snapshot.

    Key format: live/<tenant_id>/<site_id>/<camera_id>/<snapshot_ts>.jpg

    Unlike timelapse keys there are no date-component sub-directories; objects are
    short-lived and cleaned up by the S3 Lifecycle rule on the ``live/`` prefix.

    Args:
        tenant_id:   Tenant identifier.
        site_id:     Site identifier.
        camera_id:   Camera identifier.
        snapshot_ts: UTC timestamp in YYYY-MM-DDTHH:mm:ssZ format.

    Returns:
        The canonical S3 object key string for the live snapshot.
    """
    return f"live/{tenant_id}/{site_id}/{camera_id}/{snapshot_ts}.jpg"


def put_live_snapshot(
    key: str,
    body: bytes,
    sha256_hex: str,
    snapshot_ts: str,
    tenant_id: str,
) -> None:
    """Write a live-view JPEG snapshot to S3 with integrity metadata.

    No retention tag is applied — cleanup is handled exclusively by the S3
    Lifecycle rule that expires objects under the ``live/`` prefix after 1 day.

    Args:
        key:         Canonical S3 object key (from build_live_snapshot_key).
        body:        Raw JPEG bytes.
        sha256_hex:  Lowercase hex SHA-256 digest of body.
        snapshot_ts: UTC timestamp (YYYY-MM-DDTHH:mm:ssZ).
        tenant_id:   Tenant identifier (stored in object metadata for traceability).
    """
    _s3_client().put_object(
        Bucket=get_settings().snapshots_bucket,
        Key=key,
        Body=body,
        ContentType="image/jpeg",
        Metadata={
            "sha256": sha256_hex,
            "captured-at": snapshot_ts,
            "tenant-id": tenant_id,
        },
    )


def build_timelapse_key(
    tenant_id: str,
    site_id: str,
    camera_id: str,
    job_id: str,
) -> str:
    """Build the canonical S3 key for a rendered timelapse Artifact.

    Key format: timelapse/<tenant_id>/<site_id>/<camera_id>/<job_id>.mp4

    The format is kept in sync with ``sitespy.timelapse.build_artifact_key``.
    It is duplicated here rather than imported so this module stays free of a
    dependency on the timelapse module.

    Args:
        tenant_id: Tenant identifier.
        site_id:   Site identifier.
        camera_id: Camera identifier.
        job_id:    Timelapse job identifier (uuid4).

    Returns:
        The canonical S3 object key string for the timelapse Artifact.
    """
    return f"timelapse/{tenant_id}/{site_id}/{camera_id}/{job_id}.mp4"


def download_snapshot(key: str) -> bytes:
    """Download a snapshot object from S3 and return its raw bytes.

    Used by the timelapse Worker to fetch selected source frames.

    Args:
        key: Canonical S3 object key of the snapshot.

    Returns:
        The raw object bytes.
    """
    response = _s3_client().get_object(
        Bucket=get_settings().snapshots_bucket,
        Key=key,
    )
    return response["Body"].read()


def timelapse_artifact_exists(key: str) -> bool:
    """Return True if the timelapse Artifact object exists in S3, else False.

    Issues a ``HeadObject`` against the snapshots bucket. A response indicating
    the object is absent — ``404`` / ``NoSuchKey`` / a 403 raised when the object
    does not exist (HeadObject on a missing key without ``s3:ListBucket``) — is
    treated as "does not exist" and returns ``False``. Any other error propagates
    so the calling handler can surface a 500 (Requirement 8.3).

    Used by ``timelapse_download.build_download_fields`` to confirm an Artifact is
    present before minting a presigned URL, so an expired Artifact never yields a
    broken link (Requirements 5.1, 5.3).

    Args:
        key: Canonical S3 object key of the Artifact (from build_timelapse_key).

    Returns:
        True if the object exists, False if it is absent.
    """
    try:
        _s3_client().head_object(
            Bucket=get_settings().snapshots_bucket,
            Key=key,
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in ("404", "NoSuchKey", "NotFound", "403", "Forbidden"):
            return False
        raise
    return True


def put_timelapse_artifact(key: str, body: bytes) -> None:
    """Write a rendered MP4 timelapse Artifact to S3.

    Args:
        key:  Canonical S3 object key (from build_timelapse_key).
        body: Raw MP4 bytes.
    """
    _s3_client().put_object(
        Bucket=get_settings().snapshots_bucket,
        Key=key,
        Body=body,
        ContentType="video/mp4",
    )


def build_out_of_hours_key(
    tenant_id: str,
    site_id: str,
    camera_id: str,
    snapshot_ts: str,
) -> str:
    """Build the canonical S3 key for an out-of-hours snapshot.

    Key format: security/<tenant_id>/<site_id>/<camera_id>/<YYYY>/<MM>/<DD>/<snapshot_ts>.jpg

    Out-of-hours snapshots live under a distinct ``security/`` prefix that shares
    no common path segment with the in-hours long-term key, the ``live/`` key, or
    the ``timelapse/`` key (Requirement 5.5). The ``<YYYY>/<MM>/<DD>`` date
    segments are retained (unlike ``live/``) so review can be served efficiently
    and keys remain human-diagnosable. Date components are parsed from
    snapshot_ts itself (format YYYY-MM-DDTHH:mm:ssZ) so there is a single source
    of truth for the date segments.

    Args:
        tenant_id:   Tenant identifier.
        site_id:     Site identifier.
        camera_id:   Camera identifier.
        snapshot_ts: UTC timestamp in YYYY-MM-DDTHH:mm:ssZ format.

    Returns:
        The canonical S3 object key string for the out-of-hours snapshot.
    """
    date_part = snapshot_ts[:10]  # "2025-06-15"
    yyyy, mm, dd = date_part.split("-")
    return f"security/{tenant_id}/{site_id}/{camera_id}/{yyyy}/{mm}/{dd}/{snapshot_ts}.jpg"


def build_preserved_key(
    tenant_id: str,
    site_id: str,
    camera_id: str,
    snapshot_ts: str,
) -> str:
    """Build the canonical S3 key for a promoted (preserved) out-of-hours snapshot.

    Key format: preserved/<tenant_id>/<site_id>/<camera_id>/<YYYY>/<MM>/<DD>/<snapshot_ts>.jpg

    The ``preserved/`` prefix is not covered by the ``security/`` 7-day lifecycle
    expiry rule, so a promoted object survives past the out-of-hours TTL and
    remains downloadable (Requirements 9.3, 10.1). Date components are parsed from
    snapshot_ts itself (format YYYY-MM-DDTHH:mm:ssZ) so there is a single source
    of truth for the date segments.

    Args:
        tenant_id:   Tenant identifier.
        site_id:     Site identifier.
        camera_id:   Camera identifier.
        snapshot_ts: UTC timestamp in YYYY-MM-DDTHH:mm:ssZ format.

    Returns:
        The canonical S3 object key string for the preserved snapshot.
    """
    date_part = snapshot_ts[:10]  # "2025-06-15"
    yyyy, mm, dd = date_part.split("-")
    return f"preserved/{tenant_id}/{site_id}/{camera_id}/{yyyy}/{mm}/{dd}/{snapshot_ts}.jpg"


def put_out_of_hours_snapshot(
    key: str,
    body: bytes,
    sha256_hex: str,
    snapshot_ts: str,
    tenant_id: str,
) -> None:
    """Write an out-of-hours JPEG snapshot to S3 with integrity metadata.

    No retention tag is applied — cleanup is handled exclusively by the S3
    Lifecycle rule that expires objects under the ``security/`` prefix after
    7 days, mirroring the ``live/`` snapshot pattern (Requirements 5.5, 7.4).

    Args:
        key:         Canonical S3 object key (from build_out_of_hours_key).
        body:        Raw JPEG bytes.
        sha256_hex:  Lowercase hex SHA-256 digest of body.
        snapshot_ts: UTC timestamp (YYYY-MM-DDTHH:mm:ssZ).
        tenant_id:   Tenant identifier (stored in object metadata for traceability).
    """
    _s3_client().put_object(
        Bucket=get_settings().snapshots_bucket,
        Key=key,
        Body=body,
        ContentType="image/jpeg",
        Metadata={
            "sha256": sha256_hex,
            "captured-at": snapshot_ts,
            "tenant-id": tenant_id,
        },
    )


def promote_object(source_key: str, dest_key: str) -> None:
    """Relocate an S3 object from ``source_key`` to ``dest_key``.

    Copies the source object to the destination then deletes the source. The
    copy is naturally idempotent for the same destination, so this is safe to
    re-run when recovering from a partially-completed promotion (Requirement 9.3).

    Args:
        source_key: Canonical S3 object key to relocate from (e.g. security/...).
        dest_key:   Canonical S3 object key to relocate to (e.g. preserved/...).
    """
    bucket = get_settings().snapshots_bucket
    _s3_client().copy_object(
        Bucket=bucket,
        Key=dest_key,
        CopySource={"Bucket": bucket, "Key": source_key},
    )
    _s3_client().delete_object(
        Bucket=bucket,
        Key=source_key,
    )


def copy_object(source_key: str, dest_key: str) -> None:
    """Copy an S3 object from ``source_key`` to ``dest_key`` without deleting the source.

    This is the first step of the promotion sequence (copy → commit → delete):
    the source object is intentionally left in place so that, if the subsequent
    DynamoDB commit fails, the copied destination can be rolled back and the
    original object remains under its original expiry (Requirement 9.7). The copy
    is naturally idempotent for the same destination, so it is safe to re-run.

    Args:
        source_key: Canonical S3 object key to copy from (e.g. security/...).
        dest_key:   Canonical S3 object key to copy to (e.g. preserved/...).
    """
    bucket = get_settings().snapshots_bucket
    _s3_client().copy_object(
        Bucket=bucket,
        Key=dest_key,
        CopySource={"Bucket": bucket, "Key": source_key},
    )


def delete_object(key: str) -> None:
    """Delete a single object from the snapshots bucket.

    Used by the promotion flow both to roll back a copied ``preserved/`` object
    when the DynamoDB commit fails (Requirement 9.7) and to remove the original
    ``security/`` object after a successful commit (best-effort cleanup).

    Args:
        key: Canonical S3 object key to delete.
    """
    _s3_client().delete_object(
        Bucket=get_settings().snapshots_bucket,
        Key=key,
    )


def object_exists(key: str) -> bool:
    """Return True if the object exists in the snapshots bucket, else False.

    Issues a ``HeadObject`` against the snapshots bucket. A response indicating
    the object is absent — ``404`` / ``NoSuchKey`` / a 403 raised when the object
    does not exist (HeadObject on a missing key without ``s3:ListBucket``) — is
    treated as "does not exist" and returns ``False``. Any other error propagates
    so the calling handler can surface a 500. Mirrors ``timelapse_artifact_exists``.

    Used by the out-of-hours download handler to confirm an object is present
    before minting a presigned URL, so an expired object never yields a broken
    link (Requirement 10.1, 10.2).

    Args:
        key: Canonical S3 object key.

    Returns:
        True if the object exists, False if it is absent.
    """
    try:
        _s3_client().head_object(
            Bucket=get_settings().snapshots_bucket,
            Key=key,
        )
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in ("404", "NoSuchKey", "NotFound", "403", "Forbidden"):
            return False
        raise
    return True
