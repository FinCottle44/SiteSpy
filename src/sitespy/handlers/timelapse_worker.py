"""Timelapse render Worker for SiteSpy — SQS-triggered MP4 generation.

Consumes messages from the Job_Queue, one per requested render, and processes
each record independently via the Powertools ``BatchProcessor`` so that a single
failing record does not fail the entire batch (partial batch responses feed the
SQS redrive policy → DLQ).

Per record the Worker:
    1. Parses the job payload.
    2. Marks the Timelapse_Job ``processing``.
    3. Enumerates IMG_Records in ``[start, end]`` chronologically.
    4. If there are no frames, marks the job ``failed`` with reason ``no_frames``
       and returns normally (permanent failure — no retry).
    5. Computes the Frame_Budget and selects an evenly-spaced subset.
    6. Downloads the selected frames concurrently to ``/tmp/frames`` as
       ``frame_%06d.jpg`` (individually-failed objects are skipped).
    7. Encodes the frames into an H.264 MP4 with ffmpeg.
    8. Uploads the Artifact and marks the job ``complete`` with its S3 key.

Transient/infra failures (S3, ffmpeg non-zero exit, DynamoDB) are re-raised so
the SQS redrive policy retries and eventually routes the message to the DLQ.

Requirements validated: 3.1, 3.2, 3.3, 3.4, 3.5, 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 7.2, 7.3
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.batch import BatchProcessor, EventType, process_partial_response
from aws_lambda_powertools.utilities.data_classes.sqs_event import SQSRecord

from sitespy import data, storage, timelapse
from sitespy.config import get_settings

# ---------------------------------------------------------------------------
# Powertools setup
# ---------------------------------------------------------------------------

logger = Logger(service="sitespy")
metrics = Metrics(namespace="SiteSpy", service="sitespy")

processor = BatchProcessor(event_type=EventType.SQS)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_FRAMES_DIR = "/tmp/frames"  # noqa: S108 — Lambda ephemeral scratch space
_OUTPUT_PATH = "/tmp/out.mp4"  # noqa: S108 — Lambda ephemeral scratch space
_FRAME_NAME_TEMPLATE = "frame_%06d.jpg"
_DOWNLOAD_MAX_WORKERS = 16
_FAILURE_NO_FRAMES = "no_frames"


# ---------------------------------------------------------------------------
# Lambda handler — SQS event source
# ---------------------------------------------------------------------------


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entry point for the timelapse render Worker (SQS-triggered).

    Delegates to the Powertools ``BatchProcessor`` so each record is processed
    independently; failed records are reported back to SQS as partial batch
    item failures for redrive/DLQ handling.
    """
    return process_partial_response(
        event=event,
        record_handler=_process_record,
        processor=processor,
        context=context,
    )


# ---------------------------------------------------------------------------
# Per-record processing
# ---------------------------------------------------------------------------


def _process_record(record: SQSRecord) -> None:
    """Render a single timelapse job described by one SQS record.

    Raises on transient/infra failures so the record is retried (and eventually
    dead-lettered). Returns normally on permanent failures (e.g. no frames),
    after marking the job ``failed``, so the message is deleted.
    """
    payload = json.loads(record.body)

    tenant_id: str = payload["tenant_id"]
    site_id: str = payload["site_id"]
    camera_id: str = payload["camera_id"]
    job_id: str = payload["job_id"]
    start_ts: str = payload["start_ts"]
    end_ts: str = payload["end_ts"]
    length_seconds: int = int(payload["length_seconds"])
    fps: int = int(payload["fps"])

    logger.append_keys(job_id=job_id, tenant_id=tenant_id, site_id=site_id, camera_id=camera_id)
    logger.info("timelapse_worker_start")

    # --- Mark processing ---
    data.update_timelapse_job_status(tenant_id, job_id, timelapse.STATUS_PROCESSING)

    # --- Enumerate candidate frames (chronological) ---
    items = data.list_all_img_records_in_range(tenant_id, site_id, camera_id, start_ts, end_ts)

    if not items:
        # Permanent failure — no footage to render. Do NOT re-raise so SQS
        # deletes the message rather than retrying pointlessly.
        logger.warning("timelapse_worker_no_frames")
        data.update_timelapse_job_status(
            tenant_id,
            job_id,
            timelapse.STATUS_FAILED,
            failure_reason=_FAILURE_NO_FRAMES,
        )
        metrics.add_metric(name="TimelapseRenderFailure", unit=MetricUnit.Count, value=1)
        return

    # --- Select an evenly-spaced subset bounded by the Frame_Budget ---
    frame_budget = timelapse.compute_frame_budget(length_seconds, fps)
    selected = timelapse.select_frames(items, frame_budget)
    logger.info(
        "timelapse_worker_frames_selected",
        extra={"available": len(items), "budget": frame_budget, "selected": len(selected)},
    )

    # --- Prepare a fresh scratch directory for this job ---
    _reset_scratch()

    try:
        # --- Download selected frames concurrently, skipping failures ---
        frame_count = _download_frames(selected)
        if frame_count == 0:
            # Every selected object failed to download — treat as transient and
            # re-raise so SQS retries / dead-letters the message.
            raise RuntimeError("no frames could be downloaded for job")

        logger.info("timelapse_worker_frames_downloaded", extra={"frames": frame_count})

        # --- Encode with ffmpeg (raises on non-zero exit → retry) ---
        _run_ffmpeg(fps)

        # --- Upload the Artifact ---
        artifact_key = storage.build_timelapse_key(tenant_id, site_id, camera_id, job_id)
        with open(_OUTPUT_PATH, "rb") as fh:
            mp4_bytes = fh.read()
        storage.put_timelapse_artifact(artifact_key, mp4_bytes)

        # --- Mark complete ---
        data.update_timelapse_job_status(
            tenant_id,
            job_id,
            timelapse.STATUS_COMPLETE,
            artifact_key=artifact_key,
            set_completed_at=True,
        )
        metrics.add_metric(name="TimelapseRenderSuccess", unit=MetricUnit.Count, value=1)
        logger.info("timelapse_worker_complete", extra={"artifact_key": artifact_key})
    finally:
        _reset_scratch(create=False)


# ---------------------------------------------------------------------------
# Frame download
# ---------------------------------------------------------------------------


def _download_frames(selected: list[Any]) -> int:
    """Download the selected frames concurrently into the scratch directory.

    Frames are fetched with a thread pool and staged under their original
    chronological position, then renamed to a contiguous ``frame_%06d.jpg``
    sequence (ffmpeg's image demuxer requires gapless numbering). Individually
    failed objects are skipped (Requirement 4.5).

    Returns the number of frames successfully written.
    """
    keys = [item["s3_key"]["S"] for item in selected]

    successful_positions: list[int] = []
    with ThreadPoolExecutor(max_workers=_DOWNLOAD_MAX_WORKERS) as executor:
        future_to_pos = {
            executor.submit(_download_one, pos, key): pos for pos, key in enumerate(keys)
        }
        for future in as_completed(future_to_pos):
            pos = future.result()
            if pos is not None:
                successful_positions.append(pos)

    # Rename staged frames to a contiguous, chronologically-ordered sequence.
    frame_count = 0
    for seq, pos in enumerate(sorted(successful_positions)):
        staged = os.path.join(_FRAMES_DIR, f"staged_{pos:06d}.jpg")
        final = os.path.join(_FRAMES_DIR, _FRAME_NAME_TEMPLATE % seq)
        os.rename(staged, final)
        frame_count += 1

    return frame_count


def _download_one(pos: int, key: str) -> int | None:
    """Download a single snapshot to the scratch dir; return its position or None.

    A failure to retrieve one object is logged and swallowed (the frame is
    skipped) so the render can proceed as long as at least one frame remains.
    """
    try:
        body = storage.download_snapshot(key)
    except Exception:
        logger.warning("timelapse_worker_frame_download_failed", extra={"s3_key": key})
        return None

    staged = os.path.join(_FRAMES_DIR, f"staged_{pos:06d}.jpg")
    with open(staged, "wb") as fh:
        fh.write(body)
    return pos


# ---------------------------------------------------------------------------
# ffmpeg invocation
# ---------------------------------------------------------------------------


def _run_ffmpeg(fps: int) -> None:
    """Encode the staged frames into an H.264 MP4 using ffmpeg.

    Uses ``check=True`` so a non-zero exit raises ``CalledProcessError`` and the
    record is retried by SQS.
    """
    ffmpeg_path = get_settings().ffmpeg_path
    cmd = [
        ffmpeg_path,
        "-y",
        "-framerate",
        str(fps),
        "-i",
        os.path.join(_FRAMES_DIR, _FRAME_NAME_TEMPLATE),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        _OUTPUT_PATH,
    ]
    result = subprocess.run(cmd, capture_output=True, check=True)  # noqa: S603
    logger.debug("ffmpeg_completed", extra={"returncode": result.returncode})


# ---------------------------------------------------------------------------
# Scratch directory management
# ---------------------------------------------------------------------------


def _reset_scratch(*, create: bool = True) -> None:
    """Remove any previous scratch artifacts and (optionally) recreate the dir.

    Ensures ``/tmp/frames`` starts empty for each job (Lambda execution
    environments are reused across invocations) and cleans up afterwards.
    """
    shutil.rmtree(_FRAMES_DIR, ignore_errors=True)
    if os.path.exists(_OUTPUT_PATH):
        try:
            os.remove(_OUTPUT_PATH)
        except OSError:
            pass
    if create:
        os.makedirs(_FRAMES_DIR, exist_ok=True)
