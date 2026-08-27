"""ASGI entrypoint: ``uvicorn app.main:app``.

One process serves both roles on Render free tier — there is no worker dyno to
spare. The HTTP side answers health checks (which also keeps the instance from
idling down), and an asyncio task runs the queue loop.

Startup order matters and is deliberate:

1. bind configuration (fails loudly in live mode if a guard is missing),
2. try the database *without* blocking boot,
3. reclaim any lease left behind by the previous instance,
4. only then start claiming work.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from .api import PauseBody, control_dependency, router
from .config import Settings, load_settings
from .db import Database
from .stages import LADDER, stage_labels
from .worker import Worker

log = logging.getLogger("auto_manager")

DESCRIPTION = """
Telegram media pipeline control plane. The queue loop and the HTTP surface share
one process; every state change is persisted in Postgres before the next stage
begins, so a Render restart resumes instead of restarting.
"""

__all__ = ["create_app", "app", "configure_logging"]


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def create_app(settings: Settings | None = None, *, start_worker: bool | None = None) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings.log_level)
    run_worker = settings.worker_enabled if start_worker is None else start_worker

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        log.info(
            "starting %s v%s mode=%s outbound_telegram=%s",
            settings.app_name,
            settings.app_version,
            settings.mode.value,
            settings.outbound_enabled,
        )
        db: Database = app.state.db
        connected = await db.connect()
        if connected:
            if settings.migrate_on_boot:
                try:
                    if await db.schema_missing():
                        log.warning("schema not found; applying migrations on first boot")
                        outcome = await db.migrate()
                        log.info("first-boot migration: %s", outcome)
                    else:
                        log.info("schema already present; skipping migrations")
                except Exception as exc:  # noqa: BLE001 - boot must still serve /health
                    log.error(
                        "first-boot migration failed (%s); /ready will report the "
                        "schema problem until it is resolved", exc,
                    )
            with contextlib.suppress(Exception):
                reclaimed = await db.release_expired_locks()
                if reclaimed:
                    log.warning("reclaimed %s stale job lease(s) from a previous instance", reclaimed)
            # No boot job is enqueued here: the worker's own boot hook does it
            # with the shared key. Enqueuing from both places once created two
            # reconciliation jobs per start, because the keys differed.
        else:
            log.warning(
                "running without a database connection; /ready reports 503 until "
                "DATABASE_URL is set and the migrations are applied"
            )
        if run_worker and app.state.worker:
            app.state.worker.start()
        try:
            yield
        finally:
            if app.state.worker:
                await app.state.worker.stop()
            await db.close()

    app = FastAPI(
        title=settings.app_name,
        description=DESCRIPTION.strip(),
        version=settings.app_version,
        docs_url="/docs",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.db = Database(settings)
    app.state.worker = Worker(db=app.state.db, settings=settings) if settings.worker_enabled else None

    app.include_router(router)

    @app.post("/control/shutdown", dependencies=[Depends(control_dependency())])
    async def shutdown(request: Request, body: PauseBody | None = None) -> dict[str, Any]:
        """Pause first, then exit.

        Order is the whole point: exiting without pausing would let Render
        restart the instance within seconds and resume exactly what the operator
        just tried to stop. The pause flag lives in Postgres, so it survives the
        restart.
        """
        reason = (body.reason if body else "shutdown requested by operator")[:500]
        with contextlib.suppress(Exception):
            await request.app.state.db.set_paused(True, f"shutdown: {reason}")
        if request.app.state.worker:
            await request.app.state.worker.stop()

        async def _exit() -> None:
            await asyncio.sleep(0.25)
            os._exit(0)  # let the response flush before the process dies

        asyncio.get_running_loop().create_task(_exit())
        return {"paused": True, "exiting": True, "reason": reason}

    @app.get("/api/stages", tags=["meta"])
    async def stages() -> dict[str, Any]:
        return {"stages": stage_labels(), "count": len(LADDER)}

    @app.exception_handler(RuntimeError)
    async def _runtime_error(_: Request, exc: RuntimeError) -> JSONResponse:
        # Never leak a connection string through an unhandled error body.
        log.exception("unhandled runtime error")
        return JSONResponse({"detail": "internal error"}, status_code=500)

    return app


app = create_app()
