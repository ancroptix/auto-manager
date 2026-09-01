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

One service per campaign is the assumption this makes, and it is the deployment's own: one Render web service,
`WORKER_ENABLED` as its switch, and this loop started and stopped with the worker inside it.
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
        rows = list(await self.db.fetch(CAMPAIGN_ON_SQL) or [])
        if not rows:
            await self._pauseable(self.poll_seconds)
            return
        for row in rows:
            if self._stop.is_set():
                return
            campaign_id = int(row["id"])
            await self._pass(campaign_id, str(row.get("name") or ""))

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
        more = bool(result.get("more"))
        self._note(
            campaign_id,
            name,
            sent=sent,
            failed=failed,
            planned=planned,
            waiting=int(result.get("waiting_after") or 0),
            gap=float(result.get("gap_seconds") or 0.0),
            stopped=bool(result.get("stopped")),
            finished=not more,
            seconds=round(time.monotonic() - started, 1),
        )
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
            "shadow": bool(self.detail.get(-1, {}).get("shadow")),
            "rounds": self.rounds,
            "passes": self.passes,
            "errors": self.errors,
            "on": sorted(key for key in self.detail if key > 0),
            "campaigns": {str(key): dict(value) for key, value in self.detail.items() if key > 0},
        }
