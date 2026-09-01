"""The queue loop's contract, tested without Telegram or Postgres.

These are the behaviours that make "it never loses work" true: pause stops
claiming, an unimplemented feature blocks loudly instead of succeeding
quietly, a handler crash retries, and a stale lease is reclaimed at boot.
"""

from __future__ import annotations

import asyncio

import pytest

from app.db import DatabaseUnavailable
from app.handlers import FeatureNotImplemented
from app.main import create_app
from app.stages import JobKind
from app.worker import Worker


class FakeDB:
    state = "up"
    connected = True

    def __init__(self, jobs=None, paused=False) -> None:
        self.jobs = list(jobs or [])
        self.paused = paused
        self.claims = 0
        self.completed: list[int] = []
        self.failed: list[tuple[int, str]] = []
        self.blocked: list[tuple[int, str]] = []
        self.heartbeats = 0
        self.enqueued: list[tuple[str, str]] = []
        self.released = 0
        self.reconcile_attempts: list[tuple] = []

    async def connect(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    async def is_paused(self) -> bool:
        return self.paused

    async def claim(self, worker_id: str) -> dict | None:
        self.claims += 1
        return self.jobs.pop(0) if self.jobs else None

    async def heartbeat(self, worker_id: str) -> bool:
        self.heartbeats += 1
        return True

    async def release_expired_locks(self) -> int:
        self.released += 1
        return 3

    async def enqueue(self, kind, dedup_key, **kwargs):
        self.enqueued.append((kind, dedup_key))
        return {"id": 1}

    async def complete(self, job_id, result=None) -> None:
        self.completed.append(job_id)

    async def fail(self, job_id, error, retry_after=60) -> None:
        self.failed.append((job_id, error))

    async def queue_health(self):
        return {"queued": 0, "running": 0, "blocked": 0}

    async def fetchrow(self, sql, *args):
        if "status = 'blocked'" in sql:
            self.blocked.append((args[0], args[1]))
        return None


def make_worker(settings, db, handlers=None) -> Worker:
    return Worker(db=db, settings=settings, handlers=handlers or {}, telegram=None)


async def run_briefly(worker: Worker, seconds: float = 0.15) -> None:
    worker.start()
    await asyncio.sleep(seconds)
    await worker.stop(drain_seconds=1)


@pytest.mark.asyncio
async def test_paused_service_claims_nothing(make_settings) -> None:
    db = FakeDB(jobs=[{"id": 1, "kind": "ingest_media", "stage": "discovered"}], paused=True)
    worker = make_worker(make_settings(), db)
    await run_briefly(worker)
    assert db.claims == 0, "claimed work while the operator had paused the service"


@pytest.mark.asyncio
async def test_reconciliation_runs_on_boot(make_settings) -> None:
    """A restart must re-synchronise, because that is when work gets stranded."""
    db = FakeDB()
    worker = make_worker(make_settings(), db)
    await run_briefly(worker)
    assert db.enqueued and db.enqueued[0][0] == JobKind.RECONCILIATION.value
    from app.keys import reconciliation_key

    assert db.enqueued[0][1] == reconciliation_key()


@pytest.mark.asyncio
async def test_a_row_the_handler_rearms_is_left_exactly_as_the_handler_wrote_it(make_settings) -> None:
    """The loop writes no verdict over a wait the handler just scheduled, and counts no error either.

    A campaign that has spent its hour re-queues its own row and raises :class:`app.writers.Requeued`.
    Marking that row succeeded would delete the wake-up time sitting on it, which is the whole reason the
    campaign still has somewhere to come back to; failing it would park it in a status
    `app.claim_next_job` never reads. So the only correct thing the loop can do is nothing at all to the
    row, and say what happened in the log.
    """
    from app.writers import Requeued

    db = FakeDB(jobs=[{"id": 11, "kind": "join_request_campaign", "stage": "discovered"}])

    async def waiting(job, ctx):
        raise Requeued("the 20/hour ceiling is spent; this run wakes again in about 40 minute(s)")

    worker = make_worker(make_settings(), db, handlers={"join_request_campaign": waiting})
    await run_briefly(worker)
    assert db.completed == [], "the verdict overwrote the wait"
    assert db.failed == [], "a run that is waiting is not a run that failed"
    assert db.blocked == [], db.blocked
    snap = worker.snapshot()
    assert snap["processed"] == 1 and snap["errors"] == 0, snap


@pytest.mark.asyncio
async def test_unimplemented_feature_blocks_instead_of_succeeding(make_settings) -> None:
    db = FakeDB(jobs=[{"id": 7, "kind": "archive_media", "stage": "discovered"}])

    async def not_done(job, ctx):
        raise FeatureNotImplemented("needs storage bot protocol")

    worker = make_worker(make_settings(), db, handlers={"archive_media": not_done})
    await run_briefly(worker)
    assert db.completed == [], "a stub must never report success"
    assert db.blocked and db.blocked[0][0] == 7


@pytest.mark.asyncio
async def test_handler_exception_retries(make_settings) -> None:
    db = FakeDB(jobs=[{"id": 8, "kind": "publish_post", "stage": "link_received"}])

    async def boom(job, ctx):
        raise RuntimeError("Channel Help menu changed")

    worker = make_worker(make_settings(), db, handlers={"publish_post": boom})
    await run_briefly(worker)
    assert db.failed and db.failed[0][0] == 8
    assert "Channel Help menu changed" in db.failed[0][1]
    assert db.completed == []


@pytest.mark.asyncio
async def test_successful_handler_completes(make_settings) -> None:
    db = FakeDB(jobs=[{"id": 9, "kind": "reconciliation", "stage": "discovered"}])

    async def ok(job, ctx):
        return {"reclaimed_locks": 2}

    worker = make_worker(make_settings(), db, handlers={"reconciliation": ok})
    await run_briefly(worker)
    assert db.completed == [9]


@pytest.mark.asyncio
async def test_unknown_job_kind_is_failed_not_swallowed(make_settings) -> None:
    db = FakeDB(jobs=[{"id": 10, "kind": "time_travel", "stage": "discovered"}])
    worker = make_worker(make_settings(), db, handlers={})
    await run_briefly(worker)
    assert db.failed and "no handler registered" in db.failed[0][1]


@pytest.mark.asyncio
async def test_worker_survives_database_loss(make_settings) -> None:
    """A dropped connection is a sleep-and-retry, not a dead service.

    If the loop task died here, the instance would look healthy to Render's
    health check while doing nothing at all — the quietest possible failure.
    """

    class DyingDB(FakeDB):
        async def claim(self, worker_id):
            raise DatabaseUnavailable("connection reset by peer")

    db = DyingDB()
    worker = make_worker(make_settings(), db)
    worker.start()
    await asyncio.sleep(0.15)
    assert worker.alive, "worker loop died on a database error"
    assert worker._errors > 0, "the failure was swallowed instead of counted"
    await worker.stop(drain_seconds=1)


@pytest.mark.asyncio
async def test_slow_job_is_stopped_within_the_drain_window(make_settings) -> None:
    """On SIGTERM we wait one stage, not the whole upload.

    A job left 'running' with an expired lease is fine: boot reconciliation
    reclaims it and the stage checkpoint means it resumes mid-pipeline.
    """
    db = FakeDB(jobs=[{"id": 11, "kind": "reconciliation", "stage": "discovered"}])

    async def slow(job, ctx):
        await asyncio.sleep(5)
        return {}

    worker = make_worker(
        make_settings(graceful_shutdown_seconds=0.2, job_timeout_seconds=10),
        db,
        {"reconciliation": slow},
    )
    worker.start()
    await asyncio.sleep(0.1)
    await worker.stop(drain_seconds=0.2)
    assert worker.alive is False
    assert db.completed == [], "shutdown waited for a 5s handler to finish"


def test_registry_covers_every_job_kind(make_settings) -> None:
    """No job kind may exist in the enum without a handler entry, otherwise a
    queued job of that kind would sit unreachable forever."""
    from app.handlers import build_registry

    registry = build_registry()
    assert set(registry) == {kind.value for kind in JobKind}


@pytest.mark.asyncio
async def test_reconciliation_handler_reclaims_and_timestamps(make_settings) -> None:
    db = FakeDB()
    db.last_reconcile = None

    async def fetchrow(sql, *args):
        db.last_reconcile = sql

    db.fetchrow = fetchrow  # type: ignore[assignment]
    from app.handlers import Context, reconciliation

    ctx = Context(db=db, settings=make_settings(), telegram=None)
    result = await reconciliation({"id": 1, "kind": "reconciliation", "payload": {}}, ctx)
    assert db.released == 1
    assert "last_reconcile_at = now()" in db.last_reconcile
    assert result["reclaimed_locks"] == 3


def test_app_boots_without_telegram(make_settings) -> None:
    """The control plane must be deployable before any Telegram secret exists."""
    from fastapi.testclient import TestClient

    with TestClient(create_app(make_settings(worker_enabled=False), start_worker=False)) as client:
        assert client.get("/health").status_code == 200


@pytest.mark.asyncio
async def test_repeated_errors_back_off_instead_of_hammering(make_settings) -> None:
    """A persistent failure must widen its interval, not poll every 2s forever.

    This matters most when the schema is missing or broken: the loop should wait,
    because it is the one component that can still make things worse.
    """

    class ExplodingDB(FakeDB):
        def __init__(self):
            super().__init__()
            self.claim_times: list[float] = []

        async def claim(self, worker_id):
            import time as _t

            self.claim_times.append(_t.monotonic())
            raise RuntimeError("relation app.job does not exist")

    db = ExplodingDB()
    worker = make_worker(
        make_settings(worker_poll_seconds=0.1, worker_error_backoff=(0.15, 0.5, 1.5)), db
    )
    worker.start()
    await asyncio.sleep(1.6)
    await worker.stop(drain_seconds=1)

    gaps = [b - a for a, b in zip(db.claim_times, db.claim_times[1:])]
    assert gaps, "worker never attempted a claim"
    assert len(gaps) < 8, f"polled {len(gaps) + 1}x in 1.6s; backoff is not widening"
    assert max(gaps) > min(gaps), f"interval never grew: {[round(g, 2) for g in gaps]}"
