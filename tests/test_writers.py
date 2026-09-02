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
import re
from types import SimpleNamespace  # the campaign fake hands out input entities, like the real read does

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

    def __init__(
        self,
        *,
        waiting: int,
        rate: int = 100,
        delay: float | None = None,
        recorded: int = 0,
        contacts: int = 0,
        released: int = 0,
        unsent: int = 0,
    ) -> None:
        self.waiting = waiting
        self.rate = rate
        # The campaign's own spacing, as the row holds it. `None` is the "the row says nothing usable" state,
        # which is a real one: the column's own check only refuses a negative, so zero and text both arrive.
        self.delay = delay
        # `contacts` is how far into the queue this campaign already is. It drives both halves of the story:
        # the rows the read is told to skip, and the count the continuation key is named after, so a test
        # cannot make one agree with the other by accident.
        self.contacts = contacts
        # `released` is how many of those contact rows an operator has freed with `/joinreq free` (status
        # `skipped`); `unsent` is how many are still `queued` with no send, which is the number the run has to
        # report without being able to tell why. Both are separate from `contacts` because the handler is
        # allowed to filter on one and count on the other, and a fake that conflated them would hide it.
        self.released = released
        self.unsent = unsent
        # The campaign's own status, and the operator's ⏸ modelled as what it really is: a write to that row
        # from another connection, while this run is working. `status_flip` is the value the tap writes and
        # `flip_after` how many people the run has to record before the tap lands (`-1`: as soon as the run
        # has read its campaign row and looked away). Reads and writes here both see `status`, so the fake
        # can tell a run that re-reads between sends from one that remembers the plan, and a guarded write
        # from an unguarded one — which is the whole difference between "my stop worked" and twenty DMs.
        self.status = "ready"
        self.status_flip: str | None = None
        self.flip_after = 0
        self.sends = 0
        self.status_reads = 0
        self.applied: list[str] = []
        # The hour the campaign has spent, kept only so a test can prove the run never asks for it. A
        # `fetchval` the fake answers with `0` would hide a leftover read; a knob nobody can set would hide
        # one that was deleted from the code but not from the schema's memory.
        self.sent_this_hour: int | None = None
        self.next_slot_in: int | None = None
        self.job_writes: list[tuple] = []
        # What a run that is waiting said about itself, on the job's own timeline. `next_attempt_at` is a
        # column no screen reads directly, so the sentence is the only thing an operator can check.
        self.events: list[tuple] = []
        self.recorded = recorded if recorded else contacts
        self.sql: list[tuple[str, tuple]] = []
        self.queued: list[tuple[str, str]] = []

    async def fetchrow(self, statement: str, *args):
        self.sql.append((statement, args))
        if "update app.job set" in statement:
            # The queue row's own state: when a run was queued, and how far in the future it was pushed.
            self.job_writes.append((statement, args))
            return {"id": 7}
        if "insert into app.job_event" in statement:
            self.events.append(args)
            return None
        if "from app.job where dedup_key" in statement:
            return None
        if "from app.join_campaign c" in statement:
            row = {
                "id": 7,
                "status": self.status,
                "name": "default",
                "message_template": "{name}, aapka request dekh liya jaa raha hai",
                "rate_per_hour": self.rate,
                "per_message_delay_seconds": self.delay,
                "destination_id": 21,
                "telegram_channel_id": -1002575861262,
                "title": "Dekin no mogura Anime in Hindi",
            }
            if self.status_flip and self.flip_after < 0:
                # Read as it stood, and the tap lands right after the run looked: the status the run planned
                # against is then not the status it writes against, which is the race itself.
                self.status, self.status_flip = self.status_flip, None
            return row
        return None

    async def fetch(self, statement: str, *args):
        self.sql.append((statement, args))
        if "join_campaign_contact" in statement and "count(*)" not in statement:
            ids = list(range(self.contacts))
            if "status <> 'skipped'" in statement:
                # Only the query that says it filters gets the filtered answer: drop the predicate from the
                # SQL and this fake hands back the released rows again, which is the bug being guarded.
                ids = ids[self.released :]
            return [{"telegram_user_id": 900 + n} for n in ids]
        return []

    async def fetchval(self, statement: str, *args):
        self.sql.append((statement, args))
        if "extract(epoch" in statement or "status = 'sent' and sent_at > now()" in statement:
            # An hour-window read of any shape: the campaign loop is not allowed to ask this any more, so
            # the fake answers nothing and the test that trips over it is the one that caught the regression.
            raise AssertionError(f"a campaign run read an hour window it no longer has: {statement[:80]}")
        if "select status from app.join_campaign" in statement:
            self.status_reads += 1
            return self.status
        if "sent_at is null" in statement:
            return self.unsent
        if "count(*) from app.join_campaign_contact" in statement and "sent_at" not in statement:
            # The table's own count, so the continuation key is derived from what was recorded rather than
            # from a number the test happens to agree with.
            return self.recorded + sum(1 for sql, _ in self.sql if "insert into app.join_campaign_contact" in sql)
        return 0

    async def execute(self, statement: str, *args) -> int:
        self.sql.append((statement, args))
        if "insert into app.join_campaign_contact" in statement:
            self.sends += 1
            if self.status_flip and self.sends > self.flip_after >= 0:
                self.status, self.status_flip = self.status_flip, None
            return 1
        if "update app.join_campaign set" in statement and "status" in statement:
            # The guard clauses in those statements are obeyed the way Postgres obeys them: no `and status
            # in (...)`, or a row whose status is not in the list, and the write simply matches nothing.
            guard = re.search(r"and status in \(([^)]*)\)", statement)
            allowed = re.findall(r"'([a-z_]+)'", guard.group(1)) if guard else None
            if allowed is not None and self.status not in allowed:
                return 0
            taken = re.search(r"set status = '([a-z_]+)'", statement)
            self.status = taken.group(1) if taken else str(args[1])
            self.applied.append(self.status)
            return 1
        return 1

    async def enqueue(self, kind: str, dedup_key: str, **kwargs):
        self.queued.append((kind, dedup_key))
        return {"id": 99}


class CampaignSender:
    """The two calls the campaign makes on the session: who is waiting, and one text to one person."""

    def __init__(
        self, *, action: str = "sent", total: int | None = None, results: list | None = None
    ) -> None:
        self.action = action
        self.sent: list[tuple[str, str]] = []
        self.peers: list[object] = []
        self._total = total
        self._hash = 911
        # What Telegram answers, send by send. A campaign that only ever meets `sent` is a campaign nobody
        # has watched meet a rate limit at two hundred people, which is the run this list exists to model:
        # the entries are used in order and the last one repeats for as long as the pass keeps asking.
        self._results = list(results or [])

    def set_hash(self, value: int | None) -> None:
        """Whether the read hands out a usable address for the people it returns."""
        self._hash = value

    async def pending_requests(self, peer, *, limit: int = 100, skip=(), **kwargs):
        # `skip` is honoured the way the real read honours it: the known ids are walked past rather than
        # handed back, so a test of a long queue cannot pass by pretending the run starts at page one
        # every time. `total` is the queue's size, and it is deliberately larger than what a page holds.
        known = {int(one) for one in skip}
        rows = [
            {
                "user_id": 900 + n,
                "about": "",
                "date": "",
                "approved_by": None,
                **({"input_user": SimpleNamespace(user_id=900 + n, access_hash=self._hash)} if self._hash else {}),
            }
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
        # `peers` keeps the object as it arrived: which spelling a campaign addresses a stranger by is a
        # fact the string form hides, and it is the fact that decides whether the DM can be sent at all.
        self.peers.append(peer)
        self.sent.append((str(peer), text))
        if self._results:
            return self._results[0] if len(self._results) == 1 else self._results.pop(0)
        return sender.Result(ok=True, action=self.action, detail="sent")


def _campaign_writers(
    db, *, outbound: bool, action: str = "sent", total: int | None = None, results: list | None = None
) -> Writers:
    from types import SimpleNamespace

    writers = Writers(db=db, settings=SimpleNamespace(outbound_enabled=outbound))
    # `total` is the channel's own count of waiting people, which is deliberately settable apart from the
    # rows a page returned: a queue of 250 with a page of 3 is the situation that used to be misread.
    total = db.waiting if total is None else total
    transport = CampaignSender(action=action, total=total, results=results)
    transport.set_waiting(db.waiting)
    writers._writer = lambda peers, **kwargs: transport  # type: ignore[assignment]
    writers.transport = transport  # type: ignore[attr-defined]
    return writers


@pytest.mark.asyncio
async def test_a_queue_row_left_by_an_older_build_still_does_a_real_pass() -> None:
    """The job form of a campaign is a wrapper, and the wrapper is what their live database calls.

    `app.job` may still hold a campaign row from before sending became a loop. Deleting the handler would
    have answered it with `no handler registered for job kind 'join_request_campaign'`, which the worker turns
    into `blocked` — a campaign that looks broken to the person who did nothing wrong. So the kind stays routed,
    it runs the same pass the loop runs, and the row closes itself on the result like any other job. A payload
    with no campaign in it is a ValueError rather than a silent zero, because a run that "sent nobody" would be
    read as an empty queue.
    """
    db = CampaignDb(waiting=2, rate=1000)
    handlers = _campaign_writers(db, outbound=True)

    from_job = await handlers.join_request_campaign(
        {"id": 41, "stage": "sending", "campaign_id": 7}, None
    )
    from_payload = await handlers.join_request_campaign(
        {"id": 42, "stage": "sending", "payload": {"campaign_id": 7}}, None
    )

    assert from_job["sent"] == 2, from_job
    # The second row arrived after the list had run out, and it was answered rather than raised: the campaign
    # says `completed` now, which is a fact about the row, not a fault in the queue.
    assert from_payload["campaign_id"] == 7 and "skipped" in from_payload, from_payload
    assert "not sending" in from_payload["skipped"], from_payload
    assert db.queued == [], "and a pass queues nothing, in either form"

    empty = _campaign_writers(CampaignDb(waiting=0, rate=1000), outbound=True)
    with pytest.raises(ValueError, match="campaign_id"):
        await empty.join_request_campaign({"id": 43, "payload": {}}, None)


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
async def test_a_queue_bigger_than_the_pages_is_not_reported_as_finished(monkeypatch) -> None:
    """Nothing may be called "nobody is waiting" because one pass's read came back with no new rows.

    The campaign had written to two people; the channel says 250 are waiting; the read this pass could make
    returned an empty page — the offset ran out, or the queue is deeper than one page. Calling the campaign
    `completed` there is the loudest kind of wrong, because the operator would read that as "everyone has been
    told", which is the same shape as the bug that emptied a 20-person queue into a `0 pending` sentence. So
    the pass says there is more, and `app/campaignloop.py` comes straight back for it.
    """

    db = CampaignDb(waiting=0, rate=1000, contacts=2)
    handlers = _campaign_writers(db, outbound=True, total=250)

    result = await handlers.join_request_campaign({"campaign_id": 7}, None)

    assert result["waiting"] == 248, result
    assert result["sent"] == 0 and result["more"] is True, result
    assert not [
        sql for sql, _ in db.sql if "'completed'" in sql or "status = 'completed'" in sql
    ], "the campaign was closed while people were still waiting"
    assert not [
        sql for sql, _ in db.sql if "insert into app.join_campaign_contact" in sql
    ], "an unreachable tail is not an excuse to write to anyone twice"


class QueueDb:
    """One `app.job` row, one unique key, and the two statements a start is made of.

    The shape that matters is the one that made a campaign unstartable in production: `app.enqueue_job`
    collides on `dedup_key text not null unique`, so the closed row a finished run leaves behind swallows
    every later insert of the same key. A fake that always let the insert succeed could not tell "queued"
    from "silently ignored", and that is the whole difference the operator experienced.
    """

    def __init__(self, *, holder: dict | None = None, revive_matches: bool = True) -> None:
        self.holder = holder
        self.revive_matches = revive_matches
        self.queued: list[tuple[str, str]] = []
        self.revived: list[tuple] = []
        self.reads: list[tuple] = []
        self.sql: list[tuple[str, tuple]] = []

    async def enqueue(self, kind: str, dedup_key: str, **kwargs):
        self.queued.append((kind, dedup_key))
        if self.holder is not None:
            return None
        return {"id": 900 + len(self.queued)}

    async def fetchrow(self, statement: str, *args):
        if "from app.job where dedup_key" in statement:
            self.reads.append(args)
            return dict(self.holder) if self.holder else None
        if "update app.job set status = 'queued'" in statement:
            self.sql.append((statement, args))
            if self.holder and self.revive_matches:
                self.revived.append(args)
                return {"id": int(self.holder.get("id") or 7), "status": "queued"}
            return None
        raise AssertionError(f"the queue helper sent SQL this fake does not model: {statement[:60]}")


def _queued(status: str, *, attempts: int = 1, ceiling: int = 8, why: str = "") -> QueueDb:
    return QueueDb(holder={"id": 77, "status": status, "last_error": why, "attempts": attempts,
                           "max_attempts": ceiling})


@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_a_released_contact_is_owed_a_message_again(monkeypatch) -> None:
    """`skipped` is the one contact status that means "still waiting", and it is what `/joinreq free` writes.

    A row written by a run that never sent is the difference between a person being told twice and never
    being told at all. The release is the operator's decision — but once they have made it, the campaign must
    treat that person as new, or the tap does nothing and the number on the screen never moves.
    """
    db = CampaignDb(waiting=2, rate=1000, contacts=2, released=2)
    handlers = _campaign_writers(db, outbound=True)

    result = await handlers.join_request_campaign({"campaign_id": 7}, None)

    assert result["sent"] == 2, result
    assert [peer.user_id for peer in handlers.transport.peers] == [900, 901], handlers.transport.peers
    # The pass does not report how many rows it left alone: `app/controlbot.py` counts them from the campaign
    # row, and a second copy of that figure inside a pass result is a second truth about the same table.


class ReleaseOnlyDb:
    """A database that answers the release the way asyncpg does, and refuses to be asked another way.

    `Database.execute` returns the statement's **status tag** — `"UPDATE 2"`, never a count — and counting it
    is what made an operator's ✅ tap raise `ValueError: invalid literal for int() with base 10: 'UPDATE 0'`.
    Every fake in this suite used to hand back a tidy integer from `execute`, so the tests passed while the
    deployment could not start a campaign; this one is deliberately unpleasant instead.
    """

    def __init__(self, rows: int) -> None:
        self.rows = rows
        self.fetched: list[str] = []

    async def fetch(self, sql: str, *args):
        self.fetched.append(sql)
        return [{"telegram_user_id": 900 + one} for one in range(self.rows)]

    async def execute(self, sql: str, *args):
        raise AssertionError(
            "a release is counted from the rows it returned: `execute` answers with a status tag"
        )

    async def fetchval(self, sql: str, *args):
        return None


@pytest.mark.asyncio
async def test_a_write_that_wants_rows_back_cannot_be_asked_of_execute(make_settings) -> None:
    """The guard that stops the next `int(await db.execute(…))`, and it needs no connection to do it.

    The bug this stops was real: a campaign start counted the rows it released with `execute`, asyncpg
    answered with the status tag `"UPDATE 0"`, `int()` of that raised, and the operator's ✅ tap ended in
    `this command could not finish`. No fake in this suite could have seen it, because a fake returns `1`.
    So the check is not on the answer, it is on the question — `returning` in an `execute` is refused.
    """
    from app.db import Database

    db = Database(make_settings())

    with pytest.raises(ValueError, match="`fetch`"):
        await db.execute(
            "update app.join_campaign_contact set status = 'skipped' where id = $1 returning id", 1
        )


@pytest.mark.asyncio
async def test_a_release_counts_the_rows_and_never_the_status_tag() -> None:
    """The one number the start tap prints has to come from the rows that changed.

    Two people freed is two rows back from `update … returning`, and that is the whole contract: no parsing
    of a driver's status string, no `int()` of anything, and no count that can disagree with the write.
    """
    from app.writers import campaign_release_unsent

    db = ReleaseOnlyDb(2)

    assert await campaign_release_unsent(db, 7) == 2
    assert len(db.fetched) == 1 and "returning telegram_user_id" in db.fetched[0], db.fetched

    empty = ReleaseOnlyDb(0)
    assert await campaign_release_unsent(empty, 7) == 0, "nobody to release is a zero, not an error"


@pytest.mark.asyncio
async def test_the_pass_leaves_alone_the_rows_nobody_released(monkeypatch) -> None:
    """A row that exists and was never sent is not a licence to message that person again.

    "nobody is left" and "nobody is left *that I have not already written about*" are different facts, and the
    second is the one this campaign has been judged by twice: the operator saw a count that never moved and a
    reply that said nobody was waiting. The pass may not resolve that on its own — a row written and not sent
    is exactly the state that guards against a second DM to a stranger — so it closes the campaign with the
    sentence the operator can act on, and the release is a human tap: `✅ Start` (which does it as part of
    starting) or `/joinreq free` (which does it without).
    """
    db = CampaignDb(waiting=2, rate=1000, contacts=2, unsent=2)
    handlers = _campaign_writers(db, outbound=True)

    result = await handlers.join_request_campaign({"campaign_id": 7}, None)

    assert result.get("skipped") == "nobody is waiting on this channel", result
    assert handlers.transport.sent == [], "a row nobody released is not a licence to message that person"
    assert not result.get("more"), "a pass that found nobody to send to does not ask to be called again"


async def test_a_campaign_addresses_people_by_the_handle_telegram_gave(monkeypatch) -> None:
    """The DM goes to the `InputUser` the queue read carried, not to the bare id the contact row stores.

    `get_entity(900)` answers for a person the account has met and fails for one it has not, and a join
    request is by definition somebody the account has only seen in a queue. Sending by id worked in every
    test because every test's fake accepts anything, so this asserts the object the transport was called
    with — the only place the difference between the two spellings can be seen.
    """
    db = CampaignDb(waiting=2, rate=1000)
    handlers = _campaign_writers(db, outbound=True)
    handlers.transport.set_hash(911)

    result = await handlers.join_request_campaign({"campaign_id": 7}, None)

    assert result["sent"] == 2, result
    assert [getattr(peer, "access_hash", None) for peer in handlers.transport.peers] == [911, 911], (
        handlers.transport.peers
    )
    assert [peer.user_id for peer in handlers.transport.peers] == [900, 901], handlers.transport.peers
    assert db.queued == [], "a finished list queues nothing — there is no run to hand anything to"


async def test_a_campaign_without_a_hash_still_uses_the_id_it_has(monkeypatch) -> None:
    """No hash is not a crash: the id is tried, and a refusal comes back as a failed contact, not a lost row.

    The contact is recorded *before* the send, so a person the account cannot address is left as `failed`
    with the reason, rather than being retried into a second message if the session later learns them.
    """
    db = CampaignDb(waiting=1, rate=1000)
    handlers = _campaign_writers(db, outbound=True)
    handlers.transport.set_hash(None)

    result = await handlers.join_request_campaign({"campaign_id": 7}, None)

    assert result["sent"] == 1, result
    assert handlers.transport.peers == [900], handlers.transport.peers


async def test_a_pause_tap_stops_the_batch_after_the_message_in_flight(monkeypatch) -> None:
    """**⏸ Stop after this one means the next message is not sent.** The loop re-reads the campaign between
    every person, so a tap that arrives mid-batch lands on the batch. It used to land nowhere: the run
    planned twenty, sent twenty, and only then would anything have noticed the pause — which is exactly the
    "I clicked pause and it sent DMs to 20 people" the operator reported.

    What the pass gives back is a stopped answer rather than a finished one: the messages already sent stay
    sent (nothing un-sends), the campaign keeps the status the operator wrote, and the pass does not claim
    there is more to do under that status — `app/campaignloop.py` re-reads the row before every person, so a
    paused campaign simply stops being looked at.
    """
    from app import writers as writers_module

    slept: list[float] = []
    monkeypatch.setattr(writers_module.asyncio, "sleep", lambda delay: slept.append(delay) or _noop())

    async def _noop():
        return None

    db = CampaignDb(waiting=8, rate=1000)
    db.status_flip = "paused"
    db.flip_after = 3
    handlers = _campaign_writers(db, outbound=True)

    result = await handlers.join_request_campaign({"campaign_id": 7}, None)

    assert result["sent"] == 4, result
    assert result["more"] is False and "stopped" in result, result
    assert result["waiting_after"] == 4, result
    assert slept == [writers_module.JOIN_SEND_GAP_SECONDS] * 3, slept
    assert len(handlers.transport.sent) == 4, handlers.transport.sent
    assert not [
        sql for sql, _ in db.sql if "update app.join_campaign set status" in sql
    ], "a finishing pass overwrote the pause the operator just tapped"
    assert db.queued == [], "a paused campaign writes no queue row of any kind"


async def test_no_hour_window_is_read_at_all_anymore_only_the_tap_stops_a_run() -> None:
    """The operator's rule: start it, and it sends until I stop it. So the hour is out of the loop.

    `rate_per_hour` was a number nobody chose — the default of 20 — and it stopped a 340-person list after
    twenty messages, then parked the campaign invisibly. That whole shape is gone: nothing here reads an hour
    window, nothing writes `paused`, nothing re-queues a row to wait for a minute that nobody asked for. The
    only stops left are the ones with a hand in them: an empty list, `⏸ Stop after this one`, and the flood
    wait Telegram itself sends (which `app/sender.py` honours, elsewhere).
    """
    db = CampaignDb(waiting=5, rate=2, delay=1)
    db.sent_this_hour = 2
    handlers = _campaign_writers(db, outbound=True)

    result = await handlers.join_request_campaign({"campaign_id": 7, "id": 41, "stage": "discovered"}, None)

    assert result["sent"] == 5, result
    assert "ceiling" not in result and "rescheduled" not in result, result
    assert not [sql for sql, _ in db.sql if "sent_at > now() - interval" in sql], (
        "an hour window is still being read, which is what stopped the list at twenty"
    )
    assert not [sql for sql, _ in db.sql if "status = 'paused'" in sql], db.sql
    assert db.job_writes == [], "sending touches no queue row at all"
    # The list is empty, so there is nothing more to read: `more` is False and the campaign closes itself.
    assert result["more"] is False and result["waiting_after"] == 0, result
    assert db.queued == [], db.queued
    assert not [sql for sql, _ in db.sql if "app.job_event" in sql], db.sql


async def test_a_pass_works_the_whole_list_it_can_read(monkeypatch) -> None:
    """All of it, not twenty of it — and a list deeper than the read is said, not finished.

    The operator's words were "it should not pick 20 people one by one, it should list all the requested users
    at once and keep the record of who has been sent". So the pass reads as deep as the importer answers
    (`JOIN_READ_PAGES` pages of a hundred per link) and sends to everyone it reached, one message per person,
    spaced by the campaign's own delay. What it could not reach is reported as `waiting_after` with `more`
    true, which the sender comes back for; what it could not reach is never written as "nobody is left".
    """
    from app import writers as writers_module

    slept: list[float] = []
    monkeypatch.setattr(writers_module.asyncio, "sleep", lambda delay: slept.append(delay) or _noop())

    async def _noop():
        return None

    # A small ceiling rather than the production 2 000, so the fake does not build a two-thousand-person list
    # to prove the same sentence. The constant is read when the pass runs, which is what makes this legal.
    monkeypatch.setattr(writers_module, "JOIN_LIST_CEILING", 25)
    db = CampaignDb(waiting=25, rate=1000)
    handlers = _campaign_writers(db, outbound=True, total=32)

    result = await handlers.join_request_campaign({"campaign_id": 7}, None)

    assert result["sent"] == 25, "the whole list is the batch"
    assert result["waiting_after"] == 7 and result["more"] is True, result
    assert len(slept) == 24, "one gap per pair of people, none before the first"
    assert db.queued == [], "no queue row is written, so no queue row can be swallowed"
    assert not any("'completed'" in sql for sql, _ in db.sql), "seven people short is not a finished campaign"


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_a_shadow_run_plans_without_recording_anybody(monkeypatch) -> None:
    """Nothing goes on the wire in shadow mode, so sleeping between plans would only make a dry run slow.

    The part that matters more than the pacing: a dry run writes **no contact rows**. Those rows are what
    promise "this person has been told, do not write again", and a campaign dry-run once in shadow mode used
    to leave twenty strangers recorded as contacted while their inboxes stayed empty — then every later live
    run skipped exactly those people and the operator watched a number that never moved. A plan is a plan:
    counted in the reply, absent from the table, and the campaign goes back to `ready` for a real tap instead
    of queueing itself around and around.
    """
    from app import writers as writers_module

    slept: list[float] = []
    monkeypatch.setattr(writers_module.asyncio, "sleep", lambda delay: slept.append(delay) or _noop())

    async def _noop():
        return None

    db = CampaignDb(waiting=4, rate=100)
    handlers = _campaign_writers(db, outbound=False, action="planned")  # the shadow knob, not a fake

    result = await handlers.join_request_campaign({"campaign_id": 7}, None)

    assert slept == []
    assert result["planned"] == 4 and result["sent"] == 0, result
    assert result["shadow"] == "nothing was sent, so nobody is recorded as contacted", result
    assert not [sql for sql, _ in db.sql if "insert into app.join_campaign_contact" in sql], (
        "a dry run wrote contacts it never messaged"
    )
    assert not [sql for sql, _ in db.sql if "'completed'" in sql], "a plan completed a campaign it only read"
    assert db.queued == [], "a shadow run must not hand itself to the next run and re-read forever"


# --------------------------------------------------------- the rate limit that ate fifteen hundred people
from app import writers as writers_module  # noqa: E402  (the constants below are the subject of these tests)


async def _no_sleep(delay: float) -> None:
    """The campaign's own pacing, skipped: three seconds times fifty people is not a unit test."""


def _contact_writes(db: "CampaignDb") -> list[tuple[str, tuple]]:
    """Every write this run made against a contact row, in order."""
    return [
        (sql, args) for sql, args in db.sql if "update app.join_campaign_contact set" in sql
    ]


@pytest.mark.asyncio
async def test_a_rate_limit_stops_the_pass_instead_of_burning_the_rest_of_the_list(monkeypatch) -> None:
    """The report this fix came from: 1 775 people waiting, about 200 DMs, and then nothing, ever.

    Telegram's answer after a couple of hundred DMs to strangers is `PeerFloodError`, which `app/sender.py`
    maps to a blocked result carrying an hour. The send loop used to read that the only way it read anything —
    "it does not say `sent`" — mark *that person* `failed`, sleep the campaign's three seconds and try the
    next stranger, who was refused for the same reason, and the next. An hour later every remaining person
    carried a `failed` row, `failed` rows are rows this campaign already has, and no later pass would ever
    read them again. The campaign then closed itself `completed`.

    So a wait is now a fact about the account: the person in front of the loop keeps their place, the pass
    stops, and it says how long for.
    """
    flood = sender.Result(
        ok=False,
        action="blocked",
        detail="Telegram says this account is rate-limited for now",
        retry_after=3600,
    )
    ok = sender.Result(ok=True, action="sent", detail="sent")
    db = CampaignDb(waiting=40, rate=1000)
    handlers = _campaign_writers(db, outbound=True, results=[ok, ok, flood])
    monkeypatch.setattr("app.writers.asyncio.sleep", _no_sleep)

    result = await handlers.campaign_pass(7)

    assert result["sent"] == 2, result
    # Three people were written to at most: two sent, one handed back. The other thirty-seven were never
    # touched, which is the difference between this and the run that wrote to all forty.
    assert len(handlers.transport.sent) == 3, handlers.transport.sent
    assert result["retry_after"] == 3600, result
    assert result["more"] is True and result["waiting_after"] == 38, result
    assert result["failed"] == 0 and result["owed"] == 1, result
    # Nobody was marked `failed` for somebody else's rate limit, and the person the flood landed on is
    # `skipped`, which is this table's word for "owed a message".
    writes = _contact_writes(db)
    assert not [sql for sql, _ in writes if "'failed'" in sql], writes
    assert [sql for sql, _ in writes if "status = 'skipped'" in sql], writes
    # And the campaign is still on: `completed` here is the sentence that made 1 575 people unreachable.
    assert db.status == "running", db.applied


@pytest.mark.asyncio
async def test_a_person_the_wire_failed_is_owed_a_message_rather_than_written_off(monkeypatch) -> None:
    """A timeout is not a fact about the stranger, so it may not be recorded as one.

    `failed` is permanent in this table — a `failed` row is in the `already` set every later pass reads — and
    spending it on a dropped connection is how a list shrinks without anybody being told anything. The row
    goes back to `skipped` with the reason on it, and the next pass tries again.
    """
    hiccup = sender.Result(ok=False, action="failed", detail="ConnectionResetError: connection lost")
    ok = sender.Result(ok=True, action="sent", detail="sent")
    db = CampaignDb(waiting=3, rate=1000)
    handlers = _campaign_writers(db, outbound=True, results=[hiccup, ok, ok])
    monkeypatch.setattr("app.writers.asyncio.sleep", _no_sleep)

    result = await handlers.campaign_pass(7)

    assert result["sent"] == 2 and result["owed"] == 1 and result["failed"] == 0, result
    # One person is still owed, so the campaign is not finished and the loop is told to come back.
    assert result["more"] is True and result["waiting_after"] == 1, result
    assert db.status == "running", db.applied
    releases = [sql for sql, _ in _contact_writes(db) if "status = 'skipped'" in sql]
    assert len(releases) == 1, _contact_writes(db)


@pytest.mark.asyncio
async def test_a_privacy_refusal_is_recorded_once_and_never_tried_again(monkeypatch) -> None:
    """The one refusal that *is* about the person keeps its old promise, in the word the schema has for it."""
    refused = sender.Result(
        ok=False, action="blocked", detail="this user's privacy does not allow a DM"
    )
    ok = sender.Result(ok=True, action="sent", detail="sent")
    db = CampaignDb(waiting=2, rate=1000)
    handlers = _campaign_writers(db, outbound=True, results=[refused, ok])
    monkeypatch.setattr("app.writers.asyncio.sleep", _no_sleep)

    result = await handlers.campaign_pass(7)

    assert result["sent"] == 1 and result["failed"] == 1 and result["owed"] == 0, result
    assert result["more"] is False, "nobody is owed, so the list is done"
    assert [sql for sql, _ in _contact_writes(db) if "status = 'restricted'" in sql], _contact_writes(db)


@pytest.mark.asyncio
async def test_five_failures_in_a_row_are_the_account_and_the_pass_says_so(monkeypatch) -> None:
    """A streak is the guard for the flood nobody mapped: stop, name a wait, keep everybody's place."""
    hiccup = sender.Result(ok=False, action="failed", detail="OSError: broken pipe")
    db = CampaignDb(waiting=50, rate=1000)
    handlers = _campaign_writers(db, outbound=True, results=[hiccup])
    monkeypatch.setattr("app.writers.asyncio.sleep", _no_sleep)

    result = await handlers.campaign_pass(7)

    assert len(handlers.transport.sent) == writers_module.JOIN_FAILURE_STREAK, handlers.transport.sent
    assert result["retry_after"] == writers_module.JOIN_STREAK_WAIT_SECONDS, result
    assert result["owed"] == writers_module.JOIN_FAILURE_STREAK, result
    assert result["waiting_after"] == 50, "everybody is still owed a message"
    assert result["more"] is True and db.status == "running", (result, db.applied)


@pytest.mark.asyncio
async def test_the_release_frees_the_people_a_rate_limit_wrote_off(monkeypatch) -> None:
    """`✅ Start` has to be able to undo the damage the old loop did, or 1 575 people stay unreachable.

    The rows are `failed` with no `sent_at` and one attempt on them. Nobody refused those people — the
    account was rate-limited — so the operator's own start tap owes them a message again. A privacy refusal
    is left alone by the same statement, because retrying it is a second attempt at the same impossible thing.
    """
    from app.writers import JOIN_MAX_ATTEMPTS, campaign_release_unsent

    db = ReleaseOnlyDb(1575)

    assert await campaign_release_unsent(db, 7) == 1575
    sql = db.fetched[0]
    assert "status = 'queued'" in sql and "status = 'failed'" in sql, sql
    assert f"attempts < {JOIN_MAX_ATTEMPTS}" in sql, sql
    assert "not ilike '%privacy%'" in sql, sql
    assert "sent_at is null" in sql, sql


def test_the_read_budget_grows_with_the_people_already_written_to() -> None:
    """A page budget spent walking past known ids is a queue that becomes unreachable as it nears the end.

    1 775 waiting and 1 700 already messaged is seventeen pages of nothing before the first new person, and a
    fixed twenty-page read would find them only by luck. The budget is what is known, plus the same twenty.
    """
    from app.writers import JOIN_READ_PAGES, JOIN_READ_PAGES_MAX, _read_pages

    assert _read_pages(0) == JOIN_READ_PAGES + 1
    assert _read_pages(1700) == JOIN_READ_PAGES + 18
    assert _read_pages(10_000_000) == JOIN_READ_PAGES_MAX, "and it is still bounded"
