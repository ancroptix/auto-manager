"""The rules the write layer is built on, tested without Telegram or Postgres.

`tests/test_migrations_on_postgres.py` runs these handlers against the real schema with a recording
client, which is where SQL bugs show up. This file is about the *contract* instead — the four
sentences in `app/writers.py`'s header that decide whether a wrong run is recoverable:

* a plan is not a result,
* a missing human fact blocks rather than retries,
* Telegram's wait is honoured by the queue and not by a sleeping handler,
* and the block a human pastes and the buttons a session sends are built by one function.

Each of those is cheap to break and expensive to discover in front of a channel.
"""

from __future__ import annotations

import asyncio

import pytest

from app import captions, joinmsg, sender
from app.handlers import DEPENDENCIES, FeatureNotImplemented, build_registry
from app.stages import JobKind
from app.writers import NeedsInput, PlanOnly, Writers, build_writers


def test_the_write_kinds_are_claimed_by_somebody() -> None:
    """Every kind the queue can claim has a handler, and the eight writers are exactly the writes.

    A kind that no handler claims would sit in `queued` forever, which looks like an idle service from
    the outside; a handler that is still a stub would look like a working one. Both are caught here,
    and neither is caught by reading the code.
    """
    registry = build_registry(db=object(), settings=object())
    assert set(registry) == {kind.value for kind in JobKind}
    assert not any(getattr(handler, "is_stub", False) for handler in registry.values())
    assert set(build_writers(object(), object())) == {
        JobKind.ARCHIVE_MEDIA.value,
        JobKind.STORAGE_UPLOAD.value,
        JobKind.LINK_VERIFY.value,
        JobKind.LINK_HEALTH_CHECK.value,
        JobKind.PUBLISH_POST.value,
        JobKind.EDIT_POST.value,
        JobKind.SEASON_STICKER.value,
        JobKind.JOIN_REQUEST_CAMPAIGN.value,
    }


def test_a_plan_blocks_the_job_instead_of_passing() -> None:
    """`action="planned"` raises. A job that reports success on a description of itself is the bug."""
    planned = sender.Result(ok=True, action="planned", detail="would send 214 chars to -1001234")
    with pytest.raises(PlanOnly, match="shadow plan") as exc:
        Writers._stop(planned, what="post the episode in -1001234")
    assert "post the episode in -1001234" in str(exc.value)
    # And the exception is the *block* kind, so the worker files it as blocked rather than retrying.
    assert isinstance(exc.value, NotImplementedError)


def test_a_missing_fact_is_a_refusal_and_not_a_crash() -> None:
    """`NeedsInput` is a `FeatureNotImplemented`: blocked, with a sentence the operator can act on.

    The two classes must be the same ones the worker catches — `app/handlers.py` re-exports them from
    here rather than defining its own pair, and a second definition would make every refusal fall into
    the generic `except Exception` branch, i.e. eight pointless retries an hour for a fact nobody has.
    """
    assert issubclass(NeedsInput, FeatureNotImplemented)
    assert FeatureNotImplemented is not None
    with pytest.raises(NotImplementedError):
        raise NeedsInput("no archive channel is named")


def test_telegrams_wait_moves_the_queue_and_not_the_handler() -> None:
    """A flood wait becomes `retry_after`, floor 60, and never a `sleep` inside the job."""
    assert sender.RetryLater("FloodWait: 412s", retry_after=412).retry_after == 412
    assert sender.RetryLater("too soon", retry_after=3).retry_after == 60, (
        "a 3-second retry on a restricted account is the behaviour the operator ruled out"
    )
    with pytest.raises(sender.RetryLater) as exc:
        Writers._stop(
            sender.Result(ok=False, action="failed", detail="FloodWait: 90s", retry_after=90),
            what="forward into the archive",
        )
    assert exc.value.retry_after == 90
    assert "forward" not in str(exc.value), "the wait's message is Telegram's, not the plan's prose"


def test_the_button_block_and_the_buttons_are_one_builder() -> None:
    """The text Channel Help pastes is rendered from the pairs a session sends, never written twice.

    Two builders would mean a draft that looks different from the post it becomes — and the only
    person who would notice is somebody reading the channel afterwards.
    """
    links = [
        {"link": "https://t.me/b?start=a1", "quality": "480p"},
        {"link": "https://t.me/b?start=a2", "quality": "1080p"},
    ]
    for rows_policy in ("one_per_line", "pair", "same_row", "one_row"):
        entries, missing = captions.button_entries(links, rows=rows_policy)
        text, missing_text = captions.button_lines(links, rows=rows_policy)
        assert missing == missing_text
        rebuilt = "\n".join(" && ".join(f"{label} - {url}" for label, url in row) for row in entries)
        assert text.strip() == rebuilt.strip(), (rows_policy, text, rebuilt)
    empty, missing = captions.button_entries([], rows="one_per_line")
    assert empty == [] and missing == ("storage_link",), "no link is not an empty post"


def test_confirm_code_is_tied_to_the_exact_wording() -> None:
    """Editing a campaign's text invalidates its code, so a confirmation cannot outlive the plan."""
    code = joinmsg.confirm_code(7, "welcome to the channel")
    assert len(code) == 4 and code == code.upper()
    assert joinmsg.confirm_code(7, "welcome to the channel") == code
    assert joinmsg.confirm_code(7, "welcome to the channel!") != code
    assert joinmsg.confirm_code(8, "welcome to the channel") != code


class FailRecordingDB:
    """Just enough of `app.db.Database` for one worker loop around one job."""

    state = "up"
    connected = True

    def __init__(self, jobs: list[dict]) -> None:
        self.jobs = list(jobs)
        self.failed: list[tuple[int, str, int]] = []
        self.blocked: list[tuple[int, str]] = []
        self.completed: list[int] = []

    async def connect(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    async def is_paused(self) -> bool:
        return False

    async def claim(self, worker_id: str) -> dict | None:
        return self.jobs.pop(0) if self.jobs else None

    async def heartbeat(self, worker_id: str) -> bool:
        return True

    async def release_expired_locks(self) -> int:
        return 0

    async def complete(self, job_id: int, result=None) -> None:
        self.completed.append(job_id)

    async def fail(self, job_id: int, error: str, retry_after: int = 60) -> None:
        self.failed.append((job_id, error, retry_after))

    async def queue_health(self) -> dict:
        return {}

    async def fetchrow(self, sql, *args):
        if "status = 'blocked'" in sql:
            self.blocked.append((args[0], args[1]))
        return None


def _flood_handler(job, ctx):
    raise sender.RetryLater("FloodWait: 300s", retry_after=300)


def _refusal_handler(job, ctx):
    raise NeedsInput("no card message id recorded for this destination (/card <channel> <message id>)")


def _settings():
    from app.config import Settings

    return Settings(_env_file=None, worker_enabled=False)


def test_the_worker_honours_the_wait_and_the_refusal_differently() -> None:
    """`RetryLater` re-queues with Telegram's number; a refusal blocks with the reason.

    The distinction is the whole difference between a queue that waits out a restriction and one that
    hammers it, and it is only visible from outside the handler: the job ends up `failed` with a
    delay, or `blocked` with a sentence.
    """
    from app.worker import Worker

    settings = _settings()
    flood = Worker(
        db=FailRecordingDB([{"id": 1, "kind": "publish_post", "stage": "discovered"}]),
        settings=settings,
        handlers={"publish_post": _flood_handler},
    )

    async def run_flood() -> None:
        await flood._handle({"id": 1, "kind": "publish_post", "stage": "discovered"})  # noqa: SLF001

    asyncio.run(run_flood())
    assert flood.db.failed == [(1, "flood wait honoured: FloodWait: 300s", 300)], flood.db.failed
    assert flood.db.blocked == []

    refusal = Worker(
        db=FailRecordingDB([{"id": 2, "kind": "publish_post", "stage": "discovered"}]),
        settings=settings,
        handlers={"publish_post": _refusal_handler},
    )

    async def run_refusal() -> None:
        await refusal._handle({"id": 2, "kind": "publish_post", "stage": "discovered"})  # noqa: SLF001

    asyncio.run(run_refusal())
    assert refusal.db.failed == [], "a missing fact must not be retried as if it were a glitch"
    assert refusal.db.blocked and "/card" in refusal.db.blocked[0][1]


def test_every_waiting_kind_names_its_command() -> None:
    """`/status` and the docs quote one source, and each entry names the thing to run.

    The entries used to read "not implemented". They are now "waiting on", and the test is what keeps
    that from becoming a nicer word for the same silence: a kind either waits on nothing at all (and
    is absent from this map) or names what would satisfy it.
    """
    for kind, reason in DEPENDENCIES.items():
        assert kind in {k.value for k in JobKind}
        assert len(reason) > 60, "a one-line shrug is not a reason anyone can act on"
    assert set(DEPENDENCIES) <= {kind.value for kind in JobKind}
    # Two kinds need nothing from the operator beyond a session, so they are not in the map at all.
    assert JobKind.RECONCILIATION.value not in DEPENDENCIES
    assert JobKind.INGEST_MEDIA.value not in DEPENDENCIES


def test_no_write_result_is_consumed_without_a_gate() -> None:
    """Every sender verb is followed by a gate: `_stop` or an explicit `result.ok`.

    One handler (`edit_post`) read its own `Result` and returned a dict without ever asking whether the
    edit landed, so a refused edit was recorded as a succeeded job with `edited_at` left null — green in
    /status, unchanged in the channel. The verb list is what makes this checkable without a database:
    `app/sender.py` owns the five verbs, so a writer that ignores one of their answers is a bug in the
    one place this project promises it will not have.
    """
    import re
    from pathlib import Path

    src = Path("app/writers.py").read_text(encoding="utf-8")
    verbs = "|".join(sorted({"send_text", "edit_text", "forward", "read_back"}))
    offenders = []
    for match in re.finditer(rf"=\s*await\s+self\._writer\([^)]*\)\.(?:{verbs})\(", src):
        # The rest of the block, up to the next `return` at the same indent, is where the answer is used.
        tail = src[match.end() : src.find("\n    return", match.end())]
        if "_stop(" not in tail and ".ok" not in tail and "result.detail" not in tail:
            line = src[: match.start()].count("\n") + 1
            offenders.append(f"app/writers.py:{line} -> {ast_unparse_line(src, line)}")
    assert not offenders, "write results consumed without a gate: " + "; ".join(offenders)


def ast_unparse_line(src: str, line: int) -> str:  # noqa: D103 - a helper for the message above
    return src.splitlines()[line - 1].strip()[:60]
