"""Example-based unit tests for the timelapse render Worker.

Covers the four Worker behaviours called out in the design's unit-test matrix,
exercising ``_process_record`` directly with a stubbed SQS record:

    1. A stride subset is selected and ffmpeg is invoked with the requested fps.   (3.3, 4.2)
    2. An unreadable frame is skipped and the render still proceeds.               (4.5)
    3. On success the job is marked complete with an artifact_key and the MP4      (4.3, 4.4)
       is uploaded.
    4. An empty range marks the job failed with reason "no_frames" and does not    (4.6)
       re-raise (SQS deletes the message).

The data/storage layers and the ffmpeg subprocess are mocked; the pure
``sitespy.timelapse`` functions and the real /tmp scratch file IO are exercised.

Validates: Requirements 3.3, 4.2, 4.3, 4.4, 4.5, 4.6
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

from sitespy.handlers import timelapse_worker
from sitespy.timelapse import STATUS_COMPLETE, STATUS_FAILED, STATUS_PROCESSING

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeRecord:
    """Minimal stand-in for a Powertools SQSRecord — only ``.body`` is used."""

    body: str


def _payload(*, length_seconds: int = 2, fps: int = 3, **overrides) -> str:
    """Build a JSON job payload string for a Worker record."""
    payload = {
        "tenant_id": "acme",
        "site_id": "site_01",
        "camera_id": "cam_01",
        "job_id": "job_abc",
        "start_ts": "2024-01-01T00:00:00",
        "end_ts": "2024-01-02T00:00:00",
        "length_seconds": length_seconds,
        "fps": fps,
    }
    payload.update(overrides)
    return json.dumps(payload)


def _img_items(n: int) -> list[dict]:
    """Return ``n`` chronological IMG_Record-shaped items with distinct s3_keys."""
    return [{"s3_key": {"S": f"live/acme/site_01/cam_01/frame_{i:04d}.jpg"}} for i in range(n)]


def _write_output_mp4(cmd, *args, **kwargs):
    """subprocess.run side effect: emulate ffmpeg by writing the output file."""
    with open(timelapse_worker._OUTPUT_PATH, "wb") as fh:
        fh.write(b"\x00\x00\x00\x18ftypmp42fake-mp4-bytes")
    return subprocess.CompletedProcess(cmd, 0)


@pytest.fixture()
def worker_mocks():
    """Patch the Worker's data/storage/ffmpeg/settings dependencies.

    Yields a namespace of the mocks so each test can configure return values
    and assert on calls. ``subprocess.run`` writes a fake MP4 by default so the
    happy path completes; the real /tmp scratch IO is left untouched.
    """
    fake_settings = MagicMock()
    fake_settings.ffmpeg_path = "/opt/bin/ffmpeg"

    with (
        patch.object(timelapse_worker, "data") as mock_data,
        patch.object(timelapse_worker, "storage") as mock_storage,
        patch.object(timelapse_worker.subprocess, "run", side_effect=_write_output_mp4) as mock_run,
        patch.object(timelapse_worker, "get_settings", return_value=fake_settings),
    ):
        mock_storage.download_snapshot.return_value = b"\xff\xd8\xff\xe0jpeg-bytes"
        mock_storage.build_timelapse_key.return_value = (
            "timelapse/acme/site_01/cam_01/job_abc.mp4"
        )

        @dataclass
        class _Mocks:
            data: MagicMock
            storage: MagicMock
            run: MagicMock

        yield _Mocks(data=mock_data, storage=mock_storage, run=mock_run)

    # Clean up scratch artifacts created during the test.
    timelapse_worker._reset_scratch(create=False)


# ---------------------------------------------------------------------------
# 1. Stride subset selected + ffmpeg invoked with the requested fps
# ---------------------------------------------------------------------------


def test_stride_subset_selected_and_ffmpeg_uses_requested_fps(worker_mocks):
    """More frames than the budget → a stride subset is rendered at the requested fps.

    length_seconds=2, fps=3 → Frame_Budget = 6. With 20 available frames the
    Worker must down-select to at most 6 and invoke ffmpeg with ``-framerate 3``.

    Validates: Requirements 3.3, 4.2
    """
    worker_mocks.data.list_all_img_records_in_range.return_value = _img_items(20)

    timelapse_worker._process_record(_FakeRecord(body=_payload(length_seconds=2, fps=3)))

    # ffmpeg was invoked exactly once with the requested framerate.
    worker_mocks.run.assert_called_once()
    cmd = worker_mocks.run.call_args.args[0]
    assert "-framerate" in cmd
    assert cmd[cmd.index("-framerate") + 1] == "3"

    # A stride subset (not all 20) was downloaded — bounded by the budget of 6.
    assert worker_mocks.storage.download_snapshot.call_count <= 6
    assert worker_mocks.storage.download_snapshot.call_count < 20


# ---------------------------------------------------------------------------
# 2. Unreadable frame skipped and render still proceeds
# ---------------------------------------------------------------------------


def test_unreadable_frame_is_skipped_and_render_proceeds(worker_mocks):
    """One failing download is skipped; the render continues with the rest.

    Validates: Requirements 4.5
    """
    worker_mocks.data.list_all_img_records_in_range.return_value = _img_items(5)

    calls: list[str] = []

    def _download(key: str) -> bytes:
        calls.append(key)
        # Fail exactly one specific frame; all others succeed.
        if key.endswith("frame_0002.jpg"):
            raise RuntimeError("object not found")
        return b"\xff\xd8\xff\xe0jpeg-bytes"

    worker_mocks.storage.download_snapshot.side_effect = _download

    timelapse_worker._process_record(_FakeRecord(body=_payload(length_seconds=5, fps=1)))

    # All five were attempted, one failed, and the render still ran + uploaded.
    assert len(calls) == 5
    worker_mocks.run.assert_called_once()
    worker_mocks.storage.put_timelapse_artifact.assert_called_once()

    # Completed with the remaining frames (not marked failed).
    statuses = [c.args[2] for c in worker_mocks.data.update_timelapse_job_status.call_args_list]
    assert STATUS_COMPLETE in statuses
    assert STATUS_FAILED not in statuses


# ---------------------------------------------------------------------------
# 3. Complete sets artifact_key and uploads the MP4
# ---------------------------------------------------------------------------


def test_complete_uploads_mp4_and_sets_artifact_key(worker_mocks):
    """On success the MP4 is uploaded and the job is marked complete with its key.

    Validates: Requirements 4.3, 4.4
    """
    artifact_key = "timelapse/acme/site_01/cam_01/job_abc.mp4"
    worker_mocks.storage.build_timelapse_key.return_value = artifact_key
    worker_mocks.data.list_all_img_records_in_range.return_value = _img_items(3)

    timelapse_worker._process_record(_FakeRecord(body=_payload(length_seconds=3, fps=1)))

    # The Artifact was uploaded under the built key with the rendered bytes.
    worker_mocks.storage.put_timelapse_artifact.assert_called_once()
    put_args = worker_mocks.storage.put_timelapse_artifact.call_args.args
    assert put_args[0] == artifact_key
    assert isinstance(put_args[1], bytes) and len(put_args[1]) > 0

    # First status update is "processing", and the terminal update is
    # "complete" carrying the artifact_key.
    calls = worker_mocks.data.update_timelapse_job_status.call_args_list
    assert calls[0].args[2] == STATUS_PROCESSING
    complete_call = calls[-1]
    assert complete_call.args[2] == STATUS_COMPLETE
    assert complete_call.kwargs["artifact_key"] == artifact_key


# ---------------------------------------------------------------------------
# 4. No frames → failed with reason "no_frames", no re-raise
# ---------------------------------------------------------------------------


def test_no_frames_marks_failed_and_does_not_raise(worker_mocks):
    """Empty range → job marked failed with reason "no_frames"; returns normally.

    Validates: Requirements 4.6
    """
    worker_mocks.data.list_all_img_records_in_range.return_value = []

    # Must not raise (permanent failure — SQS should delete the message).
    timelapse_worker._process_record(_FakeRecord(body=_payload()))

    # Marked processing then failed with the documented reason.
    calls = worker_mocks.data.update_timelapse_job_status.call_args_list
    assert calls[0].args[2] == STATUS_PROCESSING
    failed_call = calls[-1]
    assert failed_call.args[2] == STATUS_FAILED
    assert failed_call.kwargs["failure_reason"] == "no_frames"

    # No rendering or upload happened.
    worker_mocks.run.assert_not_called()
    worker_mocks.storage.put_timelapse_artifact.assert_not_called()
