"""Shared timelapse logic for SiteSpy — pure functions and lifecycle constants.

This module holds the AWS-independent building blocks used by both the API
handlers and the render Worker: job lifecycle statuses, default output
parameters, Frame_Budget computation, evenly-spaced Frame_Selection, and
canonical key construction. Keeping this logic pure makes it unit- and
property-testable without any AWS dependencies.

Requirements validated: 3.2, 3.3, 3.4, 3.5, 7.2
"""

from __future__ import annotations

from typing import TypeVar

# ---------------------------------------------------------------------------
# Job lifecycle statuses
# ---------------------------------------------------------------------------

STATUS_QUEUED = "queued"
STATUS_PROCESSING = "processing"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"

# ---------------------------------------------------------------------------
# Default output parameters
# ---------------------------------------------------------------------------

DEFAULT_LENGTH_SECONDS = 60
DEFAULT_FPS = 24

T = TypeVar("T")


def compute_frame_budget(length_seconds: int, fps: int) -> int:
    """Return the Frame_Budget: the maximum number of source frames to use.

    The Frame_Budget is simply ``length_seconds * fps`` — the number of frames
    required to fill a clip of the requested duration at the requested frame
    rate. It bounds the Worker's work regardless of how many snapshots exist in
    the requested range.

    Args:
        length_seconds: Requested output video duration in seconds.
        fps:            Frames per second of the output video.

    Returns:
        The frame budget as an integer (``length_seconds * fps``).
    """
    return length_seconds * fps


def select_frames(items: list[T], frame_budget: int) -> list[T]:
    """Select an evenly-spaced subset of ``items`` down to at most ``frame_budget``.

    Preconditions:
        - ``items`` is in chronological (ascending) order.
        - ``frame_budget >= 1``.

    Behavior:
        - If ``len(items) <= frame_budget``: return all items unchanged.
        - Otherwise: pick ``frame_budget`` evenly-spaced items across the full
          range, always including the first and last item so the clip spans the
          entire requested range. Selection is computed by interpolating
          ``frame_budget`` indices across ``[0, len(items) - 1]``.
        - The special case ``frame_budget == 1`` (which cannot include both the
          first and last item) returns just the first item.

    The result preserves chronological order and contains no more than
    ``frame_budget`` items (and no more than ``len(items)``).

    Args:
        items:        Chronologically-ordered source items (e.g. IMG_Records).
        frame_budget: Maximum number of items to select (``>= 1``).

    Returns:
        A new list containing the selected subset in chronological order.

    Raises:
        ValueError: If ``frame_budget < 1``.
    """
    if frame_budget < 1:
        raise ValueError("frame_budget must be >= 1")

    n = len(items)
    if n <= frame_budget:
        return list(items)

    if frame_budget == 1:
        return [items[0]]

    # Interpolate `frame_budget` indices evenly across [0, n - 1] inclusive.
    # Since n > frame_budget, step > 1, so rounded indices are strictly
    # increasing (first -> 0, last -> n - 1), giving an evenly-spaced subset
    # that always includes the first and last item.
    step = (n - 1) / (frame_budget - 1)
    indices = sorted({round(i * step) for i in range(frame_budget)})
    return [items[i] for i in indices]


def build_job_sk(job_id: str) -> str:
    """Build the DynamoDB sort key for a Timelapse_Job record.

    Args:
        job_id: Timelapse job identifier (uuid4).

    Returns:
        The sort key string ``JOB#<job_id>``.
    """
    return f"JOB#{job_id}"


def build_artifact_key(
    tenant_id: str,
    site_id: str,
    camera_id: str,
    job_id: str,
) -> str:
    """Build the canonical S3 key for a rendered timelapse Artifact.

    Key format: ``timelapse/<tenant_id>/<site_id>/<camera_id>/<job_id>.mp4``

    Kept in sync with ``sitespy.storage.build_timelapse_key``.

    Args:
        tenant_id: Tenant identifier.
        site_id:   Site identifier.
        camera_id: Camera identifier.
        job_id:    Timelapse job identifier (uuid4).

    Returns:
        The canonical S3 object key string for the timelapse Artifact.
    """
    return f"timelapse/{tenant_id}/{site_id}/{camera_id}/{job_id}.mp4"
