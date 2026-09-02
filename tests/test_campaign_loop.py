"""The campaign sender: one loop, a page at a time, and no `app.job` row anywhere.

The queue this replaced produced four separate reports from the same operator — it stopped at twenty, "Start
sending" answered *already queued* and sent nothing, a run that closed its own row left the campaign unstartable,
and the screen said the campaign was on while no process disagreed with that only by silence. Every one of those
was a property of a batch being a job row: a dedup key, an attempt counter, a wake-up time. So the promises
tested here are the few that remain, and they are all about a loop that has nothing to get wrong:

* it keeps going while the campaign's row says it is on, and stops the moment it does not;
* a page that is not the end of the list is followed by the next page without a human in between;
* a pass that got nothing does not turn into a spin;
* a flood wait from Telegram is slept where it is standing, and is not shaved;
* a fault is counted, shown, and does not end the sending;
* a service in shadow mode does not run it at all.

`app/writers.py` is not under test here — its own per-person rules (the contact row before the send, `⏸` re-read
between two people, one message per person per campaign) are pinned in `tests/test_writers.py`. What is pinned
here is that those passes get *asked for*, again and again, by something that cannot lose the list.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.campaignloop import CAMPAIGN_ON_SQL, CampaignLoop
from app.sender import RetryLater


class LoopDb:
    """The one read the loop makes on its own: which campaigns are on. Plus the log of everything it asked.

    `execute` and `fetchrow` raise, because this loop's whole design is that it writes nothing: the sending
    and the closing of a campaign happen in `app/writers.py`, one person at a time, and a loop that started
    keeping its own state is where a "the screen and the database disagree" report begins.
    """

    def __init__(self, on: list[dict], *, paused: bool = False) -> None:
        self.on = list(on)
        self.sql: list[tuple[str, tuple]] = []
        self.reads = 0
        # The service-wide pause flag, which the loop obeys before it reads anything. It is on this fake
        # because "a paused service must not DM strangers" is the one thing the loop is allowed to check by
        # itself, and a fake that could not say "paused" would let that rule go untested.
        self.paused = paused

    async def is_paused(self) -> bool:
        return self.paused

    async def fetch(self, sql: str, *args):
        self.sql.append((sql, args))
        if sql == CAMPAIGN_ON_SQL:
            self.reads += 1
            return [dict(row) for row in self.on]
        raise AssertionError(f"the sender has no reason to read this: {sql[:80]}")

    async def fetchrow(self, sql: str, *args):
        raise AssertionError(f"the sender reads one query, in one place: {sql[:80]}")

    async def execute(self, sql: str, *args):
        raise AssertionError(f"the sender writes nothing itself: {sql[:80]}")


class LoopWriters:
    """`campaign_pass` as the sender sees it: scripted answers, and the ids it was asked about.

    The answers are exactly the dictionaries `app/writers.py` returns, because the contract between the two is
    those keys (`sent`, `failed`, `waiting_after`, `more`, `gap_seconds`, and the occasional `stopped` or
    `skipped`) — nothing else crosses the boundary, and a loop that needed more would be a loop with state the
    campaign row does not have.
    """

    def __init__(self, *answers: dict | BaseException) -> None:
        self.answers = list(answers)
        self.asked: list[int] = []

    async def campaign_pass(self, campaign_id, *, ctx=None) -> dict:
        self.asked.append(int(campaign_id))
        if not self.answers:
            return {"sent": 0, "failed": 0, "waiting_after": 0, "more": False}
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


def live_loop(db: LoopDb, writers: LoopWriters, **kw) -> CampaignLoop:
    settings = SimpleNamespace(outbound_enabled=True, graceful_shutdown_seconds=1)
    return CampaignLoop(db=db, writers=writers, settings=settings, **kw)


def watch_pauses(loop: CampaignLoop) -> list[float]:
    """Record the durations the loop asks to wait, without the suite waiting through them.

    `_pauseable` is this loop's only sleep, so the numbers handed to it *are* the contract: `page_seconds`
    between two pages, `idle_seconds` when a pass moved nothing, `error_seconds` after a fault, and Telegram's
    own number on a flood wait. A test that slept them for real would take a minute to prove three sentences.
    """
    pauses: list[float] = []

    async def _pause(seconds: float) -> None:
        pauses.append(seconds)
        await asyncio.sleep(0)

    loop._pauseable = _pause  # type: ignore[method-assign]
    return pauses


@pytest.mark.asyncio
async def test_a_page_that_is_not_the_end_is_followed_by_the_next_one() -> None:
    """Twenty is a page, not a limit — and the next page is asked for, not waited for.

    This is the sentence the operator's screen promises ("it will not stop until you stop it") and the one the
    old run broke by handing the list to a queue row that then had to be claimed. Here the only thing between
    two pages is a breath.
    """
    db = LoopDb([{"id": 7, "name": "default", "status": "ready"}])
    writers = LoopWriters(
        {"sent": 20, "failed": 0, "waiting_after": 7, "more": True, "gap_seconds": 3.0},
        {"sent": 7, "failed": 0, "waiting_after": 0, "more": False, "gap_seconds": 3.0},
    )
    loop = live_loop(db, writers, page_seconds=0.5)
    pauses = watch_pauses(loop)

    await loop._round()
    await loop._round()

    assert writers.asked == [7, 7], "the campaign was asked for a second page, not told to wait"
    assert pauses == [0.5], pauses
    assert loop.passes == 2
    seen = loop.snapshot()["campaigns"]["7"]
    assert seen["sent"] == 7 and seen["waiting"] == 0 and seen["finished"] is True, seen
    assert not [sql for sql, _ in db.sql if "app.job" in sql], "no queue row is written by any of this"


@pytest.mark.asyncio
async def test_a_pause_needs_no_signal_of_its_own() -> None:
    """`⏸ Stop after this one` writes the campaign's row, and that is the whole stop mechanism.

    No event to fire at the loop, no queue row to cancel, no timer to un-arm. The loop asks the database again
    before the next round, which is also why a stop cannot be swallowed the way a start used to be: the answer
    is read, not remembered.
    """
    db = LoopDb([{"id": 7, "name": "default", "status": "running"}])
    writers = LoopWriters({"sent": 4, "failed": 0, "waiting_after": 4, "more": True, "stopped": True})
    loop = live_loop(db, writers, page_seconds=0.25, poll_seconds=5.0)
    pauses: list[float] = []

    async def _pause_after_the_tap(seconds: float) -> None:
        pauses.append(seconds)
        db.on = []  # the row says `paused` now, which is all the loop has to be told

    loop._pauseable = _pause_after_the_tap  # type: ignore[method-assign]

    await loop._round()
    await loop._round()

    assert writers.asked == [7], "a paused campaign is not asked for another page"
    assert pauses == [0.25, 5.0], "the page breath, then the ordinary wait for the next look at the list"
    assert loop.snapshot()["campaigns"]["7"]["stopped"] is True


@pytest.mark.asyncio
async def test_an_empty_page_waits_a_moment_instead_of_reading_forever() -> None:
    """A queue deeper than the page that came back empty is a wait, not a `completed`, and not a spin.

    `app/writers.py` keeps the count honest in that case (248 waiting, `more` true, nobody written to twice)
    and this file keeps the *loop* honest: it comes back, but on a schedule rather than as fast as it can read
    a stranger queue.
    """
    db = LoopDb([{"id": 7, "name": "default", "status": "ready"}])
    writers = LoopWriters({"sent": 0, "failed": 0, "waiting_after": 248, "more": True, "gap_seconds": 3.0})
    loop = live_loop(db, writers, page_seconds=0.5, idle_seconds=30.0)
    pauses = watch_pauses(loop)

    await loop._pass(7, "default")

    assert pauses == [30.0], "an unreadable page is not a reason to hammer the importer"
    assert loop.snapshot()["campaigns"]["7"]["waiting"] == 248, "and the number still reaches the screen"
    assert not [sql for sql, _ in db.sql if "app.job" in sql]


@pytest.mark.asyncio
async def test_a_flood_wait_is_slept_where_it_lands() -> None:
    """Telegram's number, obeyed by the process that was asked, with no queue row involved.

    The old shape re-queued the job with a wake-up time — which is how a campaign ended up parked invisibly.
    Sleeping here is the same courtesy to Telegram's limit and it cannot be lost, because there is nothing to
    claim and nothing to expire.
    """
    db = LoopDb([{"id": 7, "name": "default", "status": "ready"}])
    writers = LoopWriters(RetryLater("FLOOD_WAIT_90", retry_after=90))
    loop = live_loop(db, writers, page_seconds=0.5)
    pauses = watch_pauses(loop)

    await loop._round()

    assert pauses == [90.0], pauses
    assert loop.errors == 0, "a wait is not a fault"
    assert loop.snapshot()["campaigns"]["7"]["flood"] == 90, "and the screen says the campaign is waiting"


@pytest.mark.asyncio
async def test_a_fault_is_counted_shown_and_survived() -> None:
    """One campaign's crash must not end the sending, and must not be silent either.

    The loop catches it because a loop that dies quietly is the exact silence this redesign exists to remove —
    and it shows the fault on the campaign's own screen for the same reason: "it is on, nothing is happening,
    and the log is on another machine" was the report that started all of this.
    """
    db = LoopDb([{"id": 7, "name": "default", "status": "ready"}])
    writers = LoopWriters(RuntimeError("the session went away"), {"sent": 3, "waiting_after": 1, "more": True})
    loop = live_loop(db, writers, error_seconds=15.0, page_seconds=0.5)
    pauses = watch_pauses(loop)

    await loop._round()
    await loop._round()

    assert loop.errors == 1
    assert pauses == [15.0, 0.5], pauses
    assert writers.asked == [7, 7], "the campaign is tried again, on the same terms as everybody else"
    assert "session went away" in loop.snapshot()["campaigns"]["7"]["error"]


@pytest.mark.asyncio
async def test_a_campaign_that_says_not_now_is_left_alone() -> None:
    """The `skipped` answer is a sentence, not a fault.

    A campaign can move between the round's read and the pass itself — the operator tapped `⏸` in that gap,
    or an older build's row closed itself. `app/writers.py` answers "this is not sending" rather than raising,
    because a raise would have made a campaign the operator had just switched off look broken in the log.
    """
    db = LoopDb([{"id": 7, "name": "default", "status": "ready"}])
    writers = LoopWriters({"campaign_id": 7, "skipped": "the campaign is paused, which is not sending"})
    loop = live_loop(db, writers, idle_seconds=30.0)
    pauses = watch_pauses(loop)

    await loop._pass(7, "default")

    assert pauses == [30.0], "a campaign that said not now is waited out, not read again at once"
    assert loop.errors == 0, "and it is not a fault, because the operator is the one who said not now"
    seen = loop.snapshot()["campaigns"]["7"]
    assert seen["sent"] == 0 and "error" not in seen, seen
    assert seen["skipped"] == "the campaign is paused, which is not sending", seen

    from app.controlbot import ControlBot  # noqa: PLC0415  (the screen's half of the same sentence)

    bot = ControlBot.__new__(ControlBot)
    bot.sender_state = lambda: {"absent": False, "running": True, "campaigns": {"7": seen}}  # type: ignore[attr-defined]
    assert "the campaign is paused, which is not sending" in bot._campaign_sending_line(7), (
        "the reason the pass gave is the reason the operator reads"
    )


@pytest.mark.asyncio
async def test_shadow_mode_refuses_to_run_a_sender() -> None:
    """A dry run is a decision to plan, not a process that reads a stranger queue forever.

    In shadow mode the plan tap runs one pass and reports it, which is the whole point of the mode. A loop
    doing that on a timer would read the same pending list all day, write nothing anybody could see, and — if
    it ever forgot which mode it was in — send. So it is not started at all, and the reason is put where the
    operator's screen can quote it.
    """
    db = LoopDb([{"id": 7, "name": "default", "status": "ready"}])
    writers = LoopWriters({"sent": 20, "waiting_after": 0, "more": False})
    settings = SimpleNamespace(outbound_enabled=False, graceful_shutdown_seconds=1)
    loop = CampaignLoop(db=db, writers=writers, settings=settings)

    loop.start()

    assert not loop.running, "shadow mode does not run a sender"
    assert db.reads == 0 and writers.asked == []

    # And the sentence the operator gets on the campaign's screen is built from that refusal, not from a
    # hope: the control bot's own line is called here so the wording and the behaviour cannot drift apart.
    from app.controlbot import ControlBot  # noqa: PLC0415  (one helper, for one assertion)

    bot = ControlBot.__new__(ControlBot)
    bot.sender_state = lambda: {"absent": False, **loop.snapshot()}  # type: ignore[attr-defined]
    line = bot._campaign_sending_line(7)
    assert "shadow mode" in line and "plans each message and blocks" in line, line


@pytest.mark.asyncio
async def test_a_service_it_cannot_read_is_reported_rather_than_promised() -> None:
    """No sender that cannot see the service's own state gets to say "awake" on the operator's screen.

    The pause flag is the loop's first read of every round, and a database that is down answers with an
    exception. Waiting is right; reassuring is not. So the failure is remembered, it rides out in
    `snapshot()`, and the campaign screen says what is unknown instead of printing a promise.
    """

    class Unreachable:
        async def is_paused(self) -> bool:
            raise ConnectionError("pooler refused")

    loop = live_loop(Unreachable(), LoopWriters(), poll_seconds=0.01)  # type: ignore[arg-type]
    pauses = watch_pauses(loop)

    await loop._round()

    assert pauses == [0.01], "the round waited instead of sending"
    snapshot = loop.snapshot()
    assert snapshot["unreadable"].startswith("ConnectionError"), snapshot
    assert snapshot["on"] == [], "and it claimed no campaign at all"

    from app.controlbot import ControlBot  # noqa: PLC0415  (the screen's half)

    bot = ControlBot.__new__(ControlBot)
    bot.sender_state = lambda: {"absent": False, **snapshot}  # type: ignore[attr-defined]
    line = bot._campaign_sending_line(7)
    assert "cannot read this service" in line, line
    assert "sending: awake" not in line, "the reassurance is the bug this test exists to stop"


@pytest.mark.asyncio
async def test_a_round_that_cannot_read_the_list_is_retried_not_fatal() -> None:
    """The list read is the loop's one dependency, and an outage there must not end sending.

    Two halves, both of them needed: `_round` lets a failed read out (a swallowed database error is how a
    service looks healthy while doing nothing), and `run` is what catches it, counts it, waits a moment and
    tries again. The operator's campaign is then expected to carry on by itself after a Render spin or a dropped
    pooler connection — which is what the loop is: the row still says on, so the next round asks again, and
    nobody had to tap anything.
    """

    class Unreadable:
        def __init__(self) -> None:
            self.calls = 0

        async def is_paused(self) -> bool:
            return False

        async def fetch(self, sql: str, *args):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("the pooler said no")
            return [{"id": 7, "name": "default", "status": "ready"}]

    class UnreadableOnce(Unreadable):
        pass

    writers = LoopWriters({"sent": 5, "failed": 0, "waiting_after": 0, "more": False})
    loop = live_loop(Unreadable(), writers, error_seconds=0.01, page_seconds=0.01, idle_seconds=0.01)  # type: ignore[arg-type]

    with pytest.raises(ConnectionError):
        await loop._round()

    # ...and the same outage in a running loop is what has to be survivable, so the second loop gets its own
    # first read to fail on.
    loop = live_loop(UnreadableOnce(), writers, error_seconds=0.01, page_seconds=0.01, idle_seconds=0.01)  # type: ignore[arg-type]
    task = asyncio.create_task(loop.run())
    for _ in range(200):
        await asyncio.sleep(0.005)
        if loop.passes:
            break
    await loop.stop(1)
    await task

    assert loop.errors >= 1, "the fault was counted, not swallowed"
    assert loop.passes >= 1 and writers.asked[0] == 7, "and the campaign was picked up without a hand in it"


@pytest.mark.asyncio
async def test_a_pass_that_stopped_on_a_rate_limit_is_waited_out_and_asked_again() -> None:
    """The other half of "a flood is slept where it lands", for the pass that got somewhere first.

    `RetryLater` is what a pass raises when it has nothing to report. A pass that sent two hundred messages
    and *then* met Telegram's limit has plenty to report, so it returns instead — its counts, the people it
    handed back, and the seconds it was asked to wait. The loop's job is to treat that number exactly like
    the exception's: sleep it, then come back for the rest of the list. Before this, the number did not
    exist, the pass carried on through fifteen hundred more strangers, and every one of them was written off.
    """
    db = LoopDb([{"id": 7, "name": "default", "status": "running"}])
    writers = LoopWriters(
        {
            "sent": 200,
            "failed": 0,
            "owed": 1,
            "waiting_after": 1575,
            "more": True,
            "retry_after": 3600,
            "why": "Telegram asked this account to wait 3600s",
        }
    )
    loop = live_loop(db, writers, page_seconds=0.5)
    pauses = watch_pauses(loop)

    await loop._round()

    assert pauses == [3600.0], pauses
    assert loop.errors == 0, "a wait is not a fault"
    seen = loop.snapshot()["campaigns"]["7"]
    assert seen["sent"] == 200 and seen["flood"] == 3600 and seen["owed"] == 1, seen
    # And the campaign is still on, so the next round after the sleep asks for the rest.
    assert writers.asked == [7], writers.asked
