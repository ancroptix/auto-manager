"""The job stage ladder.

These strings are a contract shared with ``supabase/migrations/0001_init.sql``
(the ``app.job_stage`` enum) and ``app.stage_is_valid_transition`` in
0002_functions.sql. ``tests/test_stage_contract.py`` fails if either side drifts,
because a drift here means a job could checkpoint to a stage the database
rejects mid-restart — the exact moment the design cannot afford to fail.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

__all__ = ["JobStage", "JobStatus", "JobKind", "LADDER", "rank", "is_valid_transition", "next_stage", "TERMINAL_STATUSES"]


class JobStage(str, Enum):
    """Checkpoint ladder; each completed stage is persisted before the next starts."""

    DISCOVERED = "discovered"
    THUMBNAIL_CHECKED = "thumbnail_checked"
    ARCHIVED = "archived"
    SENT_TO_STORAGE_BOT = "sent_to_storage_bot"
    LINK_RECEIVED = "link_received"
    DESTINATION_POSTED = "destination_posted"
    COMPLETED = "completed"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class JobKind(str, Enum):
    INGEST_MEDIA = "ingest_media"
    THUMBNAIL_SCREEN = "thumbnail_screen"
    ARCHIVE_MEDIA = "archive_media"
    STORAGE_UPLOAD = "storage_upload"
    LINK_VERIFY = "link_verify"
    PUBLISH_POST = "publish_post"
    EDIT_POST = "edit_post"
    SEASON_STICKER = "season_sticker"
    JOIN_REQUEST_CAMPAIGN = "join_request_campaign"
    RECONCILIATION = "reconciliation"
    LINK_HEALTH_CHECK = "link_health_check"


#: Ordered ladder. Index == stage_rank() in SQL (0-based here, 1-based there).
LADDER: tuple[JobStage, ...] = (
    JobStage.DISCOVERED,
    JobStage.THUMBNAIL_CHECKED,
    JobStage.ARCHIVED,
    JobStage.SENT_TO_STORAGE_BOT,
    JobStage.LINK_RECEIVED,
    JobStage.DESTINATION_POSTED,
    JobStage.COMPLETED,
)

TERMINAL_STATUSES: frozenset[JobStatus] = frozenset(
    {JobStatus.SUCCEEDED, JobStatus.CANCELLED}
)


def rank(stage: JobStage | str) -> int:
    value = JobStage(stage)
    return LADDER.index(value)


def is_valid_transition(current: JobStage | str, target: JobStage | str) -> bool:
    """Forward by at most one step, or stay put so a retry can replay a stage.

    Mirrors ``app.stage_is_valid_transition``. Rewinding is refused: a job that
    "goes back" to re-archive would upload a duplicate file to storage.
    """
    try:
        delta = rank(target) - rank(current)
    except ValueError:
        return False
    return delta in (0, 1)


def next_stage(stage: JobStage | str) -> JobStage | None:
    index = rank(stage) + 1
    return LADDER[index] if index < len(LADDER) else None


def stage_labels() -> list[str]:
    return [s.value for s in LADDER]


def as_json(stage: JobStage | str) -> dict[str, Any]:
    value = JobStage(stage)
    return {
        "stage": value.value,
        "rank": rank(value),
        "of": len(LADDER),
        "is_last": next_stage(value) is None,
    }
