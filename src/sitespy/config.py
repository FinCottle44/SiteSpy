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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        snapshots_bucket=os.environ["SNAPSHOTS_BUCKET"],
        data_table=os.environ["DATA_TABLE"],
        aws_region=os.environ.get("AWS_REGION", "eu-west-2"),
        environment=os.environ.get("ENVIRONMENT", "dev"),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
    )
