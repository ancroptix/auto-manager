"""HTTP surface: liveness, readiness, operator status, kill switch.

``/health`` is intentionally the most boring endpoint on the service: it must
return 200 while the process is alive so Render keeps the deploy and UptimeRobot
counts it as up, even when Supabase is down or every feature is blocked.

``/control/*`` is bearer-token protected and is the operator's remote
stop button when the service is doing something it should not.
"""

from __future__ import annotations

import hmac
import logging
import asyncio
import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from .stages import LADDER

log = logging.getLogger("auto_manager.api")

__all__ = ["router"]

router = APIRouter()
_STARTED = time.monotonic()


class PauseBody(BaseModel):
    reason: str = Field(default="paused by operator", max_length=500)


# The dependency needs app.state, so build it with a closure in create_app()
# rather than pretending Request and DI are free here.
def control_dependency():
    async def _check(request: Request, authorization: str | None = Header(default=None)) -> None:
        settings = request.app.state.settings
        token = settings.control_token
        if token is None:
            raise HTTPException(
                status_code=503,
                detail="CONTROL_TOKEN is not configured; control endpoints are disabled.",
            )
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="Provide 'Authorization: Bearer <CONTROL_TOKEN>'.")
        supplied = authorization.split(" ", 1)[1].strip()
        if not hmac.compare_digest(supplied, token.get_secret_value()):
            log.warning("rejected control request from %s", request.client.host if request.client else "?")
            raise HTTPException(status_code=403, detail="Invalid control token.")

    return _check


@router.get("/", include_in_schema=False)
async def index() -> dict[str, Any]:
    """A human-facing root, because the first thing the operator does is open
    the Render URL in a browser and see something that is not an error."""
    return {
        "service": "auto-manager",
        "status": "alive",
        "endpoints": {
            "health": "/health",
            "readiness": "/ready",
            "status": "/status",
            "openapi": "/docs",
        },
    }


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    db = request.app.state.db
    worker = request.app.state.worker
    payload: dict[str, Any] = {
        "status": "ok",
        "mode": request.app.state.settings.mode.value,
        "version": request.app.state.settings.app_version,
        "uptime_seconds": round(time.monotonic() - _STARTED, 1),
        "database": db.state,
        "worker": "alive" if worker and worker.alive else ("disabled" if not request.app.state.settings.worker_enabled else "stopped"),
    }
    if db.state != "up":
        # Liveness stays green on purpose: Render must not kill the instance
        # because an external dependency is briefly unreachable. /ready is the
        # endpoint that reports the degradation.
        payload["note"] = "process healthy; database not reachable"
    return payload


@router.get("/ready")
async def ready(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    db = request.app.state.db
    problems: list[str] = []
    if settings.database_url is None:
        problems.append("DATABASE_URL not configured")
    else:
        reachable, error = await db.ping()
        if not reachable:
            problems.append(f"database unreachable: {error}")
        else:
            ok, detail = await db.schema_ready()
            if not ok:
                problems.append(f"schema: {detail}")
    if settings.worker_enabled and not request.app.state.worker.alive:
        problems.append("worker loop is not running")
    if problems:
        raise HTTPException(status_code=503, detail={"ready": False, "problems": problems})
    return {"ready": True}


@router.get("/status")
async def status(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    db = request.app.state.db
    body: dict[str, Any] = {
        "mode": settings.mode.value,
        "outbound_telegram_actions": settings.outbound_enabled,
        "stage_ladder": [s.value for s in LADDER],
        "config": settings.safe_dump(),
        "owner_ids": sorted(settings.owner_ids),
        "worker": request.app.state.worker.snapshot() if request.app.state.worker else None,
        "database": await db.describe() if db.connected else {"state": db.state},
    }
    if db.connected:
        try:
            row = await db.fetchrow(
                "select paused, paused_reason, last_reconcile_at, heartbeat_at, worker_id from app.service_state where id = 1"
            )
            body["service_state"] = row
            body["blocked_features"] = await db.fetchrow(
                """
                select coalesce(jsonb_object_agg(kind, n), '{}'::jsonb) as blocked
                from (
                  select kind::text, count(*) as n
                  from app.job where status = 'blocked' group by kind
                ) s
                """
            )
        except Exception as exc:  # noqa: BLE001
            body["status_error"] = str(exc)[:300]
    return body


@router.post("/control/pause", dependencies=[Depends(control_dependency())])
async def pause(request: Request, body: PauseBody | None = None) -> dict[str, Any]:
    reason = (body.reason if body else "paused by operator")[:500]
    row = await request.app.state.db.set_paused(True, reason)
    log.warning("service PAUSED: %s", reason)
    return {"paused": True, "reason": reason, "state": row}


@router.post("/control/resume", dependencies=[Depends(control_dependency())])
async def resume(request: Request) -> dict[str, Any]:
    row = await request.app.state.db.set_paused(False, None)
    log.warning("service RESUMED")
    return {"paused": False, "state": row}


@router.post("/control/probe", dependencies=[Depends(control_dependency())])
async def probe(request: Request) -> dict[str, Any]:
    """Run read-only protocol discovery against the storage bot and Channel Help.

    Started in the background and never awaited: the probe deliberately waits on
    two bots' replies, which can take a minute, and a request that times out would
    read as a failure while the probe was still running. The report is delivered to
    the owner as a DM, so nothing sensitive has to travel through an HTTP response.
    """
    settings = request.app.state.settings
    if not settings.outbound_enabled:
        raise HTTPException(
            status_code=503,
            detail="probe needs a live Telegram session (APP_MODE=live + TELEGRAM_* set)",
        )
    from .telegram_client import probe_once

    task = asyncio.create_task(probe_once(settings, request.app.state.db))
    request.app.state.probe_task = task
    task.add_done_callback(lambda t: _log_probe_result(t))
    return {
        "started": True,
        "delivery": "the report arrives as a DM to the configured owner id",
        "guard": "read-only: only /start-style menu commands, never media or channel posts",
    }


def _log_probe_result(task: "asyncio.Task[None]") -> None:
    """A probe that dies with an exception must still be visible in the logs."""
    try:
        result = task.result()
    except asyncio.CancelledError:
        return
    except Exception as exc:  # noqa: BLE001
        log.error("probe task failed: %s: %s", type(exc).__name__, exc)
        return
    log.info("probe finished: sent=%s elapsed=%ss delivery=%s",
             result.get("messages_sent"), result.get("elapsed_seconds"), result.get("delivery"))


@router.post("/control/reconcile", dependencies=[Depends(control_dependency())])
async def reconcile(request: Request) -> dict[str, Any]:
    reclaimed = await request.app.state.db.release_expired_locks()
    from .keys import reconciliation_key
    from .stages import JobKind

    # A manual /control/reconcile must actually run, so it is not collapsed into
    # the hourly boot key.
    job = await request.app.state.db.enqueue(
        JobKind.RECONCILIATION.value,
        f"{reconciliation_key()}:manual:{int(time.time())}",
        payload={"trigger": "manual"},
        priority=5,
    )
    return {"reclaimed_locks": reclaimed, "queued_job_id": (job or {}).get("id")}
