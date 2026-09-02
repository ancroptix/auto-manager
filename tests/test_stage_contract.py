"""The stage ladder must be identical in Python and in SQL.

If these drift, a job checkpoint fails at exactly the wrong time — during a
restart, mid-upload — and the queue wedges. Parsed from the migration text
rather than hardcoded so the SQL file stays the single source of truth.
"""

from __future__ import annotations

import re

import pytest

from app.stages import LADDER, JobKind, JobStage, JobStatus, is_valid_transition, next_stage, rank


def _enum_values(migrations: dict[str, str], type_name: str) -> list[str]:
    text = migrations["0001_init.sql"]
    match = re.search(rf"'{type_name}:([^']+)'", text)
    assert match, f"{type_name} enum is missing from 0001_init.sql"
    return [v.strip() for v in match.group(1).split(",") if v.strip()]


def test_ladder_matches_sql_enum(migrations: dict[str, str]) -> None:
    assert [s.value for s in LADDER] == _enum_values(migrations, "job_stage")


def test_job_kinds_match_sql_enum(migrations: dict[str, str]) -> None:
    assert [k.value for k in JobKind] == _enum_values(migrations, "job_kind")


def test_job_statuses_match_sql_enum(migrations: dict[str, str]) -> None:
    assert [s.value for s in JobStatus] == _enum_values(migrations, "job_status")


def test_python_stage_rank_matches_sql_function(migrations: dict[str, str]) -> None:
    """app.stage_rank() numbers stages 1..7; Python ranks are 0-based."""
    body = re.search(
        r"create or replace function app\.stage_rank.*?\$\$\s*(.*?)\s*;\s*\$\$;",
        migrations["0002_functions.sql"],
        re.S,
    )
    assert body, "app.stage_rank() not found in 0002_functions.sql"
    pairs = re.findall(r"when '([a-z_]+)'\s+then (\d+)", body.group(1))
    sql_ranks = {name: int(num) for name, num in pairs}
    assert sql_ranks == {s.value: rank(s) + 1 for s in LADDER}, "stage numbering diverged"


def test_every_stage_is_reachable_in_order() -> None:
    stage = JobStage.DISCOVERED
    visited = [stage]
    while (nxt := next_stage(stage)) is not None:
        assert is_valid_transition(stage, nxt)
        visited.append(nxt)
        stage = nxt
    assert visited == list(LADDER)
    assert next_stage(JobStage.COMPLETED) is None


@pytest.mark.parametrize(
    "current,target,legal",
    [
        (JobStage.DISCOVERED, JobStage.THUMBNAIL_CHECKED, True),
        (JobStage.THUMBNAIL_CHECKED, JobStage.THUMBNAIL_CHECKED, True),  # retry replay
        (JobStage.DISCOVERED, JobStage.ARCHIVED, False),  # skipping a checkpoint
        (JobStage.ARCHIVED, JobStage.DISCOVERED, False),  # rewind -> duplicate upload
        (JobStage.LINK_RECEIVED, JobStage.DESTINATION_POSTED, True),
    ],
)
def test_transition_rules(current: JobStage, target: JobStage, legal: bool) -> None:
    assert is_valid_transition(current, target) is legal


def test_invalid_stage_name_is_not_transitionable() -> None:
    assert is_valid_transition(JobStage.DISCOVERED, "not_a_stage") is False  # type: ignore[arg-type]
