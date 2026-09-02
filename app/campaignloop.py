"""The campaign sender: one loop per campaign that is on, and no queue row anywhere.

Why this exists instead of the queue. `app/writers.py` used to send a campaign as a chain of `app.job` rows:
twenty people, then a row for the next twenty, keyed on how many contacts had been written. That chain is
where every one of the operator's reports came from. A key held by a row that had already closed swallowed the
next start, so "Start sending" answered "already queued" and nothing went out. An hourly cap nobody chose
stopped a 340-person list at twenty and parked the campaign as `paused`. Re-arming the wait from inside the
run collided with the run's own key and left a campaign armed with no timer at all. Each fix was correct and
each one added a state the operator had to understand.

The rule they asked for is one sentence: **it sends until I stop it.** That is a loop, not a queue — read the
list, send to the next person after the campaign's own delay, look again, stop when the row says something
other than "on". A page of twenty is still what one read fetches, because that is the size Telegram's importer
answers with, and the per-person rules that were never about the queue are all still in force: the contact row
is written before the send, one person is never messaged twice, `⏸` is re-read between every two people, and a
flood wait from Telegram is slept out rather than shaved.

One service per campaign is the assumption this makes, and it is the deployment's own: one Render web service
running one loop.

**A wait is obeyed here, wherever it comes from.** A pass that met Telegram's limit before it sent anything
raises `RetryLater`; a pass that sent two hundred messages and *then* met it returns its counts with a
`retry_after` on them. Both are the same instruction — sleep this number, keep the campaign on, ask again —
and the second one exists because the alternative was the pass carrying on through the rest of the list at one
refusal every three seconds, which is how a campaign of 1 775 people ended a night with 200 messages sent and
everybody else marked `failed`.

**Why the service starts this and not the queue worker.** It rode the worker for exactly one round, and the
result was a campaign screen that needed a paragraph about `app.job` to explain why nothing was being sent: the
operator had to start the queue before their DMs would move, and `▶️ Run the queue` sat on a screen that is
supposed to have two controls. So `app/main.py` wakes this loop when the service comes up and stops it on
shutdown, and the one service-wide switch it obeys is the pause flag in `app.setting` — because a paused service
must not DM strangers while the operator is trying to make it do nothing. Everything else about sending is the
campaign's own row: ✅ on, ⏸ off.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .sender import RetryLater

log = logging.getLogger("auto_manager.campaignloop")

__all__ = ["CAMPAIGN_ON_SQL", "CampaignLoop"]

#: Which campaigns want to be sending. `ready` is "started and not finished", `running` is "in the middle of
#: the list"; `paused`, `aborted`, `completed` and `draft` are all off, in the sense that no message goes out
#: while they say what they say. The operator's switch is `app/controlbot.py`'s start and stop taps, which are
#: the only things that move a row into or out of these two states.
CAMPAIGN_ON_SQL = (
    "select id, name, status from app.join_campaign"
    " where status in ('ready', 'running') order by id"
)


@dataclass
class CampaignLoop:
    """Sends every campaign that is on, one message at a time, until each one is off or empty.

    `writers` is the same `app/writers.py` build the job handlers use, so a pass through this loop and a pass
    through a leftover queue row run the identical code. `snapshot()` is what `app/controlbot.py` prints on
    the campaign screens: the loop remembers what it last did so the operator is not told a future tense the
    database never promised, and nothing in here is written to the database, so a restart loses the memory and
    keeps the sending.
    """

    db: Any
    writers: Any
    settings: Any
    #: Between two pages of the same campaign. Small, because the pacing the operator cares about happens
    #: person to person inside a pass; this is only the breath between reads.
    page_seconds: float = 1.0
    #: After a pass that sent, planned or failed nothing. Without it an unreadable queue read would be
    #: re-read a thousand times an hour for no reason, which is a way to lose a free month of hours.
    idle_seconds: float = 30.0
    #: After a pass that raised. Shorter than `idle_seconds` on purpose: a fault in the code is worth trying
    #: again sooner than a channel with nobody waiting.
    error_seconds: float = 15.0
    poll_seconds: float = 5.0

    _task: asyncio.Task | None = field(default=None, repr=False)
    _stop: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    #: One entry per campaign this loop has touched since it started, and the last thing it did. Read by
    #: `⏱`-adjacent screens in `app/controlbot.py`, never written to the database.
    rounds: int = 0
    passes: int = 0
    errors: int = 0
    detail: dict[int, dict[str, Any]] = field(default_factory=dict, repr=False)

    # ------------------------------------------------------------ lifecycle
    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Wake the sender. Idempotent, because the service calls it on boot and a restart calls it again.

        Shadow mode is the one thing that keeps it asleep: a loop in a read-only service would read a
        stranger queue forever and tell nobody anything, so the plan tap runs one pass instead, on purpose.
        """
        if self.running:
            return
        if not getattr(self.settings, "outbound_enabled", False):
            # A dry run in a loop is a service that reads a stranger queue forever and tells nobody
            # anything. Shadow mode plans on request — the plan tap runs one pass and reports — and that is
            # all it should ever do here.
            log.info("campaign loop not started: this service is in shadow mode, so it plans but never sends")
            self.detail[-1] = {"shadow": True}
            return
        self._stop.clear()
        self._task = asyncio.create_task(self.run(), name="auto-manager-campaign-loop")

    async def stop(self, drain_seconds: float | None = None) -> None:
        """Ask the loop to finish the person it is on, then stop waiting for it."""
        self._stop.set()
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        grace = drain_seconds or getattr(self.settings, "graceful_shutdown_seconds", 10.0)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=grace)
        if not task.done():
            # A send that has left the account has either been recorded or not, and both of those answers
            # are already in `app.join_campaign_contact`. Cancelling here cannot un-send or double-send.
            log.warning("campaign loop did not drain in %ss; cancelling", grace)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._round()
            except Exception as exc:  # noqa: BLE001 - a loop that dies silently is the silence we are fixing
                self.errors += 1
                log.warning("campaign loop round failed: %s", exc)
                await self._pauseable(self.error_seconds)

    # ------------------------------------------------------------ one round
    async def _round(self) -> None:
        self.rounds += 1
        state = await self._service_state()
        if state != "on":
            # The service-wide pause is the operator's emergency stop, and "everything stops" has to include
            # the DMs. Nothing is skipped or lost by waiting: the campaigns keep their status, and the next
            # round after a `/resume` reads the same list again. An unreadable flag waits too, and says so —
            # "the service is paused" would be a story about a fact this loop could not read.
            if state == "paused":
                self.detail[-2] = {"paused": True, "at": time.time()}
            await self._pauseable(self.poll_seconds)
            return
        rows = list(await self.db.fetch(CAMPAIGN_ON_SQL) or [])
        if not rows:
            await self._pauseable(self.poll_seconds)
            return
        for row in rows:
            if self._stop.is_set():
                return
            campaign_id = int(row["id"])
            await self._pass(campaign_id, str(row.get("name") or ""))

    async def _service_state(self) -> str:
        """`"on"`, `"paused"`, or `"unreadable"` — read fresh every round, and never assumed.

        A database that cannot answer is not a licence to send: the flag exists for the moments when the
        operator wants nothing to leave the account, so the safe answer to a failed read is "wait". It is kept
        apart from a real pause because the two need different sentences on the operator's screen — one is
        "you stopped me", the other is "I cannot see". A loop that reported the first while it meant the second
        is how a campaign looks fine while nothing goes out, which is the report this whole file exists to
        make impossible.
        """
        try:
            return "paused" if await self.db.is_paused() else "on"
        except Exception as exc:  # noqa: BLE001 - a wait, not a crash: the next round asks again
            self.detail[-3] = {"unreadable": f"{type(exc).__name__}: {exc}"[:140], "at": time.time()}
            self.detail.pop(-2, None)  # a stale "paused" would explain something this loop did not read
            return "unreadable"

    async def _pass(self, campaign_id: int, name: str) -> None:
        started = time.monotonic()
        try:
            result = await self.writers.campaign_pass(campaign_id)
        except RetryLater as exc:
            # Telegram's own number, obeyed, and the campaign stays on. This is the one wait this loop does
            # deliberately: `app/sender.py` refused to shave it when the job form of a pass was the only way
            # to run, and a loop that sleeps it in place is the same courtesy without a queue row involved.
            self._note(campaign_id, name, flood=exc.retry_after)
            log.warning("campaign %s: flood wait honoured, next page in %ss", campaign_id, exc.retry_after)
            await self._pauseable(max(1.0, float(exc.retry_after)))
            return
        except Exception as exc:  # noqa: BLE001 - one campaign's fault must not stop the others' sending
            self.errors += 1
            self._note(campaign_id, name, error=str(exc)[:200])
            log.warning("campaign %s pass failed: %s", campaign_id, exc)
            await self._pauseable(self.error_seconds)
            return

        self.passes += 1
        sent = int(result.get("sent") or 0)
        planned = int(result.get("planned") or 0)
        failed = int(result.get("failed") or 0)
        owed = int(result.get("owed") or 0)
        more = bool(result.get("more"))
        # A pass that ended because Telegram said stop says so with a number, not with an exception: it has
        # already handed its unsent people back to the list, and the only thing left to do with the number is
        # sleep it. This is the same courtesy as the `RetryLater` branch above, for the case where the pass
        # got far enough to have counts worth showing — a flood after two hundred messages is a report, and it
        # used to be nothing at all, because the pass simply carried on burning the list.
        wait = float(result.get("retry_after") or 0.0)
        self._note(
            campaign_id,
            name,
            sent=sent,
            failed=failed,
            planned=planned,
            owed=owed,
            flood=int(wait) or None,
            waiting=int(result.get("waiting_after") or 0),
            gap=float(result.get("gap_seconds") or 0.0),
            stopped=bool(result.get("stopped")),
            finished=not more,
            # The pass's own sentence for "there was nothing for me to do", carried to the operator's screen
            # verbatim. It matters more than it looks: "I tapped Start and no DM went out" is usually this
            # branch — the channel's list is empty, or everybody on it already has a row — and a screen that
            # answers "on" to that is the silence with a smile on it.
            skipped=str(result["skipped"]) if result.get("skipped") else None,
            seconds=round(time.monotonic() - started, 1),
        )
        if wait > 0:
            # Obeyed where it lands, exactly like `RetryLater`. Nothing is skipped: the people this pass did
            # not reach are `skipped` rows again, which is this campaign's word for "owed a message", so the
            # next pass after this sleep reads them like anybody else.
            log.warning("campaign %s: waiting %ss before the next page", campaign_id, int(wait))
            await self._pauseable(max(1.0, wait))
            return
        if more and owed:
            # People were handed back as still owed. Sleeping the idle gap here is how a start tap after a
            # rate-limit recovery sat for half a minute between every empty-looking page and looked like
            # "the bot is not sending DMs".
            await self._pauseable(self.page_seconds)
            return
        if sent + planned + failed == 0:
            # This covers a `skipped` answer too — a campaign that stopped between this round's read of the
            # list and the pass itself, or a row that has gone. Waiting is the right answer either way: the
            # next round re-reads the list, and a loop that spun on a row it cannot act on would be reading a
            # stranger queue as fast as the pooler will answer, for nothing.
            await self._pauseable(self.idle_seconds)
            return
        if more:
            await self._pauseable(self.page_seconds)

    # ------------------------------------------------------------ helpers
    def _note(self, campaign_id: int, name: str, **fields: Any) -> None:
        seen = self.detail.get(campaign_id) or {}
        seen.update({"name": name, "at": time.time(), **fields})
        self.detail[campaign_id] = seen

    async def _pauseable(self, seconds: float) -> None:
        """Sleep, but wake the moment `stop()` is called.

        A loop that slept an uninterruptible 30 seconds would make a shutdown wait for it for no reason, and
        a `⏸` tapped while the loop is between pages is answered by the campaign row rather than by this
        sleep — so an early wake costs nothing and a late one is a stuck service.
        """
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    def snapshot(self) -> dict[str, Any]:
        """What the operator's screen is allowed to say about sending.

        `on` is the list of campaigns the loop found on at its last look, per campaign the last thing that
        happened, and `shadow` when this service does not send at all. Everything here is a memory of the
        last round: the database is what decides whether a campaign is on, and a screen that preferred this
        dictionary would be reading a summary of a decision it could have asked about.
        """
        return {
            "running": self.running,
            "paused": bool(self.detail.get(-2, {}).get("paused")),
            "unreadable": str(self.detail.get(-3, {}).get("unreadable") or ""),
            "shadow": bool(self.detail.get(-1, {}).get("shadow")),
            "rounds": self.rounds,
            "passes": self.passes,
            "errors": self.errors,
            "on": sorted(key for key in self.detail if key > 0),
            "campaigns": {str(key): dict(value) for key, value in self.detail.items() if key > 0},
        }
