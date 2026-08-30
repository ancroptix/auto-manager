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
from pathlib import Path

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


# --------------------------------------------------------------------------- the join-request campaign
class CampaignDb:
    """The statements one campaign run makes, in order, with nothing invented between them.

    This fake answers like a database only where the handler asks; every other query returns nothing,
    because a silent `[]` is the answer that makes a wrong SQL string fail in the test instead of in a
    DM thread. The point of recording `enqueue` calls is the hand-off: a campaign that stops early has to
    ask for its next run, and a run that asks twice would message the same stranger twice.
    """

    def __init__(self, *, waiting: int, rate: int = 100, recorded: int = 0, contacts: int = 0) -> None:
        self.waiting = waiting
        self.rate = rate
        # `contacts` is how far into the queue this campaign already is. It drives both halves of the story:
        # the rows the read is told to skip, and the count the continuation key is named after, so a test
        # cannot make one agree with the other by accident.
        self.contacts = contacts
        self.recorded = recorded if recorded else contacts
        self.sql: list[tuple[str, tuple]] = []
        self.queued: list[tuple[str, str]] = []

    async def fetchrow(self, statement: str, *args):
        self.sql.append((statement, args))
        if "from app.join_campaign c" in statement:
            return {
                "id": 7,
                "status": "ready",
                "name": "default",
                "message_template": "{name}, aapka request dekh liya jaa raha hai",
                "rate_per_hour": self.rate,
                "destination_id": 21,
                "telegram_channel_id": -1002575861262,
                "title": "Dekin no mogura Anime in Hindi",
            }
        return None

    async def fetch(self, statement: str, *args):
        self.sql.append((statement, args))
        if "join_campaign_contact" in statement and "count(*)" not in statement:
            return [{"telegram_user_id": 900 + n} for n in range(self.contacts)]
        return []

    async def fetchval(self, statement: str, *args):
        self.sql.append((statement, args))
        if "count(*) from app.join_campaign_contact" in statement and "sent_at" not in statement:
            # The table's own count, so the continuation key is derived from what was recorded rather than
            # from a number the test happens to agree with.
            return self.recorded + sum(1 for sql, _ in self.sql if "insert into app.join_campaign_contact" in sql)
        return 0

    async def execute(self, statement: str, *args) -> int:
        self.sql.append((statement, args))
        return 1

    async def enqueue(self, kind: str, dedup_key: str, **kwargs):
        self.queued.append((kind, dedup_key))
        return {"id": 99}


class CampaignSender:
    """The two calls the campaign makes on the session: who is waiting, and one text to one person."""

    def __init__(self, *, action: str = "sent", total: int | None = None) -> None:
        self.action = action
        self.sent: list[tuple[str, str]] = []
        self._total = total

    async def pending_requests(self, peer, *, limit: int = 100, skip=(), **kwargs):
        # `skip` is honoured the way the real read honours it: the known ids are walked past rather than
        # handed back, so a test of a long queue cannot pass by pretending the run starts at page one
        # every time. `total` is the queue's size, and it is deliberately larger than what a page holds.
        known = {int(one) for one in skip}
        rows = [
            {"user_id": 900 + n, "about": "", "date": "", "approved_by": None}
            for n in range(self._waiting)
            if 900 + n not in known
        ][:limit]
        return (
            sender.Result(ok=True, action="read_requests", detail=f"{len(rows)} pending", total=self._total),
            rows,
        )

    def set_waiting(self, count: int) -> None:
        self._waiting = count

    async def send_text(self, peer, text: str, **kwargs):
        self.sent.append((str(peer), text))
        return sender.Result(ok=True, action=self.action, detail="sent")


def _campaign_writers(
    db, *, outbound: bool, action: str = "sent", total: int | None = None
) -> Writers:
    from types import SimpleNamespace

    writers = Writers(db=db, settings=SimpleNamespace(outbound_enabled=outbound))
    # `total` is the channel's own count of waiting people, which is deliberately settable apart from the
    # rows a page returned: a queue of 250 with a page of 3 is the situation that used to be misread.
    total = db.waiting if total is None else total
    transport = CampaignSender(action=action, total=total)
    transport.set_waiting(db.waiting)
    writers._writer = lambda peers, **kwargs: transport  # type: ignore[assignment]
    writers.transport = transport  # type: ignore[attr-defined]
    return writers


@pytest.mark.asyncio
async def test_a_campaign_writes_to_one_person_every_three_seconds(monkeypatch) -> None:
    """The operator's own pacing — "har 3 second me ek user ko message" — as an assertion on the sleeps.

    Spacing the sends is the *slower* of the two choices the queue allows, and the loop has to stay that
    way however the ceiling is configured: a campaign that could write 1 200 messages an hour would
    otherwise fire them back to back at people who only asked to join a channel.
    """
    from app import writers as writers_module

    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(writers_module.asyncio, "sleep", fake_sleep)
    db = CampaignDb(waiting=5, rate=100)
    handlers = _campaign_writers(db, outbound=True)

    result = await handlers.join_request_campaign({"campaign_id": 7}, None)

    assert result["sent"] == 5 and result["failed"] == 0
    assert slept == [writers_module.JOIN_SEND_GAP_SECONDS] * 4, "a gap between sends, none before the first"
    assert db.queued == [], "everyone is done, so there is no next run to ask for"
    closing = [args for sql, args in db.sql if "app.join_campaign set status" in sql]
    assert closing and closing[-1][1] == "completed" and closing[-1][2] is True, closing


@pytest.mark.asyncio
async def test_a_queue_bigger_than_the_pages_is_not_reported_as_finished(monkeypatch) -> None:
    """Nothing may be called "nobody is waiting" because this run's read came back with no new rows.

    The campaign had written to two people; the channel says 250 are waiting; the read this run could make
    returned an empty page (the offset ran out, or the queue is deeper than the pages a 120-second lease
    allows). Marking the campaign `completed` there is the loudest kind of wrong, because the operator read
    that number as "everyone has been told" — the exact shape of the bug that emptied a 20-person queue into
    a `0 pending` sentence. So the run hands itself to the next one and says how many it could not reach.
    """
    from app import keys

    db = CampaignDb(waiting=0, rate=1000, contacts=2)
    handlers = _campaign_writers(db, outbound=True, total=250)

    result = await handlers.join_request_campaign({"campaign_id": 7}, None)

    assert result["waiting"] == 248, result
    assert result["sent"] == 0 and result["resumed"] is True, result
    assert not [
        sql for sql, _ in db.sql if "'completed'" in sql or "status = 'completed'" in sql
    ], "the campaign was closed while people were still waiting"
    assert db.queued == [("join_request_campaign", keys.campaign_run_key(7, 2))], db.queued
    assert not [
        sql for sql, _ in db.sql if "insert into app.join_campaign_contact" in sql
    ], "an unreachable tail is not an excuse to write to anyone twice"


async def test_a_run_stops_at_the_lease_and_hands_the_rest_to_the_next_one(monkeypatch) -> None:
    """Twenty people per run, then the next run under a key nobody else can duplicate.

    `app.enqueue_job` gives a claimed job 120 seconds and `release_expired_locks` re-queues a stale one:
    a handler that slept its way through 300 contacts would be handed to a second worker while the first was
    still dialling, and two passes over the same strangers is the one thing a DM campaign must not do. The
    continuation key counts the contacts already recorded, so the two paths that could resume this
    campaign — the runner and the boot-time sweep in `app/handlers.py` — land on the same row.
    """
    from app import keys
    from app import writers as writers_module

    slept: list[float] = []
    monkeypatch.setattr(writers_module.asyncio, "sleep", lambda delay: slept.append(delay) or _noop())

    async def _noop():
        return None

    db = CampaignDb(waiting=writers_module.JOIN_MAX_PER_RUN + 7, rate=1000)
    handlers = _campaign_writers(db, outbound=True)

    result = await handlers.join_request_campaign({"campaign_id": 7}, None)

    assert result["sent"] == writers_module.JOIN_MAX_PER_RUN, "the batch is capped, not the list"
    assert result["waiting_after"] == 7 and result["continued"] is True
    assert len(slept) == writers_module.JOIN_MAX_PER_RUN - 1
    assert db.queued == [
        ("join_request_campaign", keys.campaign_run_key(7, db.recorded + writers_module.JOIN_MAX_PER_RUN))
    ]
    assert not any("'completed'" in sql for sql, _ in db.sql), "the campaign is still running"


@pytest.mark.asyncio
async def test_a_shadow_run_does_not_pace_itself(monkeypatch) -> None:
    """Nothing goes on the wire in shadow mode, so sleeping between plans would only make a dry run slow.

    The count is still spent the way a live run spends it — the ceiling has to be honest about what this
    plan would cost — but the gap exists to protect real messages from looking like a flood.
    """
    from app import writers as writers_module

    slept: list[float] = []
    monkeypatch.setattr(writers_module.asyncio, "sleep", lambda delay: slept.append(delay) or _noop())

    async def _noop():
        return None

    db = CampaignDb(waiting=4, rate=100)
    handlers = _campaign_writers(db, outbound=False, action="planned")

    result = await handlers.join_request_campaign({"campaign_id": 7}, None)

    assert slept == []
    assert result["sent"] == 4, "planned sends are counted, which is what keeps the ceiling honest"


def test_the_resume_key_counts_contacts_instead_of_trying_again() -> None:
    """Two callers, one key, no duplicate run — and a key that moves only when the campaign moved."""
    from app import keys

    assert keys.campaign_run_key(7, 0) == "campaign:7:run0"
    assert keys.campaign_run_key(7, 12) == "campaign:7:run12"
    assert keys.campaign_run_key(7, -3) == keys.campaign_run_key(7, 0), "no negative counter in a key"
    assert keys.campaign_run_key(7, 12) != keys.campaign_key(21, "default"), "a run is not a start"


def test_the_boot_sweep_resumes_a_campaign_that_was_left_running() -> None:
    """Render free tier sleeps in fifteen minutes; a half-sent campaign has to be picked up, not forgotten.

    Asserted on the statement and the key rather than on a mock of the loop, because the bug this guards is
    the resume asking for a job under a key that the still-queued run already holds: that dedupes to nothing,
    and the operator sees a campaign sitting at `running` with nobody assigned to it.
    """
    from app import handlers as handlers_module

    source = Path(handlers_module.__file__).read_text(encoding="utf-8")
    assert "campaign_run_key" in source, "the resume has to use the shared key, not its own spelling"
    assert "status in ('ready', 'running')" in source
