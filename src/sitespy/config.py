from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    snapshots_bucket: str
    data_table: str
    aws_region: str
    environment: str
    log_level: str
    job_queue_url: str
    max_length_seconds: int
    max_fps: int
    retention_days: int
    job_ttl_days: int
    ffmpeg_path: str
    artifact_presign_ttl: int


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # Single source of truth for record/artifact retention (default 30 days).
    # The submit TTL derives from this value, so the JOB# record TTL and the
    # S3 timelapse/ lifecycle rule always resolve to the same duration.
    retention_days = int(os.environ.get("RETENTION_DAYS", "30"))
    return Settings(
        snapshots_bucket=os.environ["SNAPSHOTS_BUCKET"],
        data_table=os.environ["DATA_TABLE"],
        aws_region=os.environ.get("AWS_REGION", "eu-west-2"),
        environment=os.environ.get("ENVIRONMENT", "dev"),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        job_queue_url=os.environ.get("JOB_QUEUE_URL", ""),
        max_length_seconds=int(os.environ.get("MAX_LENGTH_SECONDS", "120")),
        max_fps=int(os.environ.get("MAX_FPS", "30")),
        retention_days=retention_days,
        job_ttl_days=retention_days,
        ffmpeg_path=os.environ.get("FFMPEG_PATH", "/opt/bin/ffmpeg"),
        artifact_presign_ttl=int(os.environ.get("ARTIFACT_PRESIGN_TTL", "3600")),
    )
