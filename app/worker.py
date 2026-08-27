"""The queue loop.

Everything here is written for one assumption: the process can be killed at any
moment (Render free tier restarts and spins down). So the loop

* claims at most one job per iteration, under a DB lease,
* lets handlers checkpoint stages *inside* the job (that is what makes resume
  possible), and never mutates job state in memory,
* refuses to claim anything while the operator has paused the service,
* drains gracefully on shutdown: finish the current job's stage, stop claiming.

UptimeRobot keeps the instance awake; this loop keeps the work correct when it
fails to.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .db import Database, DatabaseUnavailable
from .handlers import FeatureNotImplemented, Context, Handler
from .stages import JobKind

log = logging.getLogger("auto_manager.worker")

__all__ = ["Worker"]

#: Default widening pauses after a loop error. Overridable via
#: WORKER_ERROR_BACKOFF so a deployment can be gentler or more eager, and so the
#: behaviour is testable without waiting a minute.
_DEFAULT_BACKOFF: tuple[float, ...] = (2.0, 5.0, 10.0, 30.0, 60.0)


@dataclass
class Worker:
    db: Database
    settings: Settings
    handlers: dict[str, Handler] = field(default_factory=dict)
    telegram: Any = None
    worker_id: str = field(default_factory=lambda: f"worker-{int(time.time())}")

    _task: asyncio.Task | None = field(default=None, repr=False)
    _stop: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _idle_rounds: int = 0
    _processed: int = 0
    _errors: int = 0
    _last_job_at: float | None = None
    _started_at: float = field(default_factory=time.monotonic)
    _boot_reconciled: bool = False

    def __post_init__(self) -> None:
        if not self.handlers:
            from .handlers import build_registry

            self.handlers = build_registry()

    # ------------------------------------------------------------ public API
    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self.run(), name="auto-manager-worker")

    async def stop(self, drain_seconds: float | None = None) -> None:
        grace = drain_seconds or self.settings.graceful_shutdown_seconds
        self._stop.set()
        task = self._task
        self._task = None
        if task and not task.done():
            # The loop checks _stop between claims, so a running job finishes its
            # current stage; we only wait for that much.
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(task, timeout=grace)
            if not task.done():
                log.warning("worker did not drain in %ss; cancelling", grace)
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    @property
    def alive(self) -> bool:
        return self._task is not None and not self._task.done()

    def snapshot(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "alive": self.alive,
            "processed": self._processed,
            "errors": self._errors,
            "idle_rounds": self._idle_rounds,
            "uptime_seconds": round(time.monotonic() - self._started_at, 1),
            "last_job_at": (
                round(time.time() - self._last_job_at, 1) if self._last_job_at else None
            ),
        }

    # ------------------------------------------------------------ the loop
    async def run(self) -> None:
        log.info(
            "worker %s starting (mode=%s, outbound=%s)",
            self.worker_id,
            self.settings.mode.value,
            self.settings.outbound_enabled,
        )
        backoff_index = 0
        while not self._stop.is_set():
            try:
                if not self.db.connected and not await self.db.connect():
                    await self._sleep(backoff_index)
                    backoff_index = min(backoff_index + 1, len(self._backoff) - 1)
                    continue

                await self._maybe_boot_reconcile()

                if await self.db.is_paused():
                    self._idle_rounds += 1
                    await self._sleep_wait(self.settings.worker_poll_seconds * 5)
                    continue

                job = await self.db.claim(self.worker_id)
                if job is None:
                    self._idle_rounds += 1
                    await self.db.heartbeat(self.worker_id)
                    # An idle loop is healthy, so it does not inherit an old
                    # error backoff; it just waits its poll interval.
                    await self._sleep_wait(self.settings.worker_poll_seconds)
                    continue

                self._idle_rounds = 0
                await self._handle(job)
                # Only a fully clean pass clears backoff. Resetting it right
                # after a successful *connect* let a persistent failure (a
                # missing migration, a raised exception in _handle) re-hammer
                # the database every 2 seconds forever.
                backoff_index = 0
            except asyncio.CancelledError:
                raise
            except DatabaseUnavailable as exc:
                log.warning("worker pausing on database loss: %s", exc)
                self._errors += 1
                await self._sleep(backoff_index)
                backoff_index = min(backoff_index + 1, len(self._backoff) - 1)
            except Exception as exc:  # noqa: BLE001 - the loop must not die
                self._errors += 1
                log.exception("worker loop error: %s", exc)
                await self._sleep(backoff_index)
                backoff_index = min(backoff_index + 1, len(self._backoff) - 1)
        log.info("worker %s stopped", self.worker_id)

    async def _maybe_boot_reconcile(self) -> None:
        if self._boot_reconciled or not self.settings.reconcile_on_boot:
            return
        self._boot_reconciled = True
        day = time.strftime("%Y%m%d")
        await self.db.enqueue(
            JobKind.RECONCILIATION.value,
            f"reconciliation:boot:{day}",
            payload={"trigger": "boot"},
            priority=10,
        )

    async def _handle(self, job: dict[str, Any]) -> None:
        job_id = int(job["id"])
        kind = str(job["kind"])
        handler = self.handlers.get(kind)
        self._last_job_at = time.time()
        if handler is None:
            await self.db.fail(job_id, f"no handler registered for job kind {kind!r}", retry_after=3600)
            log.error("unrouted job kind=%s id=%s", kind, job_id)
            return

        ctx = Context(db=self.db, settings=self.settings, telegram=self.telegram)
        try:
            result = await asyncio.wait_for(
                handler(job, ctx), timeout=self.settings.job_timeout_seconds
            )
            await self.db.complete(job_id, result or {})
            self._processed += 1
            log.info("job %s (%s) completed", job_id, kind)
        except (FeatureNotImplemented, NotImplementedError) as exc:
            # Block, do not retry: a missing feature will not appear between
            # attempts, and 8 pointless attempts per hour burns the free hours.
            await self.db.fetchrow(
                """
                update app.job
                   set status = 'blocked', last_error = $2, locked_by = null,
                       locked_until = null, finished_at = now(),
                       next_attempt_at = now() + interval '1 day'
                 where id = $1
                """,
                job_id,
                str(exc)[:400],
            )
            await self.db.fetchrow(
                "insert into app.job_event (job_id, stage, status, message) values ($1, $2::app.job_stage, 'blocked', $3)",
                job_id,
                job.get("stage", "discovered"),
                str(exc)[:400],
            )
            self._errors += 1
            log.warning("job %s (%s) blocked: %s", job_id, kind, exc)
        except asyncio.TimeoutError:
            await self.db.fail(job_id, f"handler exceeded {self.settings.job_timeout_seconds}s")
            self._errors += 1
        except Exception as exc:  # noqa: BLE001
            await self.db.fail(job_id, f"{type(exc).__name__}: {exc}")
            self._errors += 1
            log.warning("job %s (%s) failed: %s", job_id, kind, exc)

    # ------------------------------------------------------------ sleeping
    @property
    def _backoff(self) -> tuple[float, ...]:
        return tuple(self.settings.worker_error_backoff) or _DEFAULT_BACKOFF

    async def _sleep(self, index: int) -> None:
        schedule = self._backoff
        await self._sleep_wait(schedule[min(index, len(schedule) - 1)])

    async def _sleep_wait(self, seconds: float) -> None:
        delay = max(self.settings.worker_poll_seconds, seconds)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=delay)
