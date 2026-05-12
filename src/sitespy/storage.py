"""S3 storage helpers for SiteSpy — canonical key construction and snapshot writes.

Requirements validated: 5.2, 5.3, 5.4, 5.5, 5.6, 5.8
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import boto3
import botocore.config

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
