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


async def _build_control_bot(settings: Settings, db: Any, *, user_client: Any = None) -> Any:
    """Assemble the control bot. Separate so the wiring itself can be tested."""
    from .botapi import BotApi
    from .controlbot import ControlBot

    transport = None
    if settings.bot_allow_login and settings.telegram_api_id and settings.telegram_api_hash:
        from .mtproto_login import MTProtoLogin

        transport = MTProtoLogin(
            api_id=int(settings.telegram_api_id),
            api_hash=settings.telegram_api_hash.get_secret_value(),
        )
    api = BotApi(settings.reveal("telegram_bot_token") or "")

    # The two login settings that exist as `app.config` rows, read once here instead of per message. They
    # are the ones an operator may want mid-troubleshooting — a code that arrives late wants a longer
    # window, a chat that must keep its history wants no deletions at all — and neither is worth an
    # environment variable and a redeploy. A database that cannot answer is not an emergency: the
    # ControlBot defaults are the same numbers, and the log says which one was unreadable.
    login_ttl = 600.0
    delete_sensitive = True
    if db is not None:
        try:
            login_ttl = float(await db.config("bot.login_ttl_seconds", 600) or 600)
            delete_sensitive = bool(await db.config("bot.delete_sensitive", True))
        except Exception as exc:  # noqa: BLE001 - the defaults are the answer
            log.info("the bot's login settings are unreadable (%s); defaults stand", type(exc).__name__)

    async def adopt_session() -> None:
        """Give the freshly logged-in account to the client that performs the writes.

        ``TelegramUserClient.start()`` keeps the client it built the first time, so adopting a new
        session means putting the old one down first — that is the whole reason this is a function and
        not a line in /login's handler. A failure here is not fatal: the session is stored, the queue
        keeps reading, and the write jobs block with the one sentence the operator can act on.
        """
        if user_client is None:
            log.info("a session was stored while live writes are off; nothing to hand it to")
            return "APP_MODE is not live, so this session waits until the service is switched on"
        await user_client.stop()
        try:
            await user_client.start()
        except Exception as exc:  # noqa: BLE001 - the operator reads this line, so it has to be a sentence
            log.warning("the session is stored but the writer could not use it yet (%s)", type(exc).__name__)
            return f"the writer could not connect with it yet ({type(exc).__name__}: {str(exc)[:120]})"
        log.info("the user client reconnected with the session the control bot just stored")
        return "the service reconnected with it, so the write jobs can run"

    return ControlBot(
        api=api,
        db=db,
        settings=settings,
        transport=transport,
        owner_ids=settings.owner_ids,
        allow_login=settings.bot_allow_login,
        background=lambda coro: asyncio.create_task(coro),
        on_session_stored=adopt_session,
        login_ttl_seconds=login_ttl,
        delete_sensitive=delete_sensitive,
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
        for note in settings.boot_audit():
            # Names what is missing and to which effect, without ever printing a
            # value: this is the line an operator pastes when "nothing happens".
            log.warning("startup check: %s", note) if "NOT set" in note else log.info("startup check: %s", note)
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
        if settings.bot_should_run:
            try:
                app.state.control_bot = await _build_control_bot(
                    settings, db, user_client=app.state.user_client
                )
                app.state.control_bot_task = asyncio.create_task(app.state.control_bot.run())
            except Exception as exc:  # noqa: BLE001 - /health must survive a bad bot token
                app.state.control_bot = None
                app.state.bot_error = str(exc)
                log.error(
                    "control bot could not start (%s); queue and HTTP surface unaffected", exc
                )

        if app.state.user_client is not None:
            # A stored session is enough to connect; nothing here asks for a code. A failure to
            # connect is not fatal: the queue keeps reading, and the write jobs block with the one
            # reason the operator can act on (/login).
            try:
                await app.state.user_client.start()
                log.info("user session connected; write jobs may run")
            except Exception as exc:
                log.warning(
                    "no user session yet (%s): reads and reconciliation continue, write jobs block "
                    "until /login stores a session",
                    type(exc).__name__,
                )

        if settings.probe_on_boot:
            # Deliberately not awaited: a probe talks to two third-party bots and
            # waits for their replies, and health checks must not queue behind it.
            if settings.outbound_enabled:
                from .telegram_client import probe_once

                log.warning("PROBE_ON_BOOT is set: running read-only protocol discovery once")
                app.state.probe_task = asyncio.create_task(probe_once(settings, db))
            else:
                log.warning("PROBE_ON_BOOT is set but outbound Telegram is unavailable; nothing to probe")
        try:
            yield
        finally:
            bot = getattr(app.state, "control_bot", None)
            if bot is not None:
                bot.stop()
            task = getattr(app.state, "control_bot_task", None)
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
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
    # The user session the write jobs use. Built here, not inside a job, so exactly one thing owns
    # the connection to the account; None when outbound Telegram is off, and every writer then
    # refuses with "no session is open" rather than pretending.
    app.state.user_client = None
    if settings.outbound_enabled:
        from .telegram_client import TelegramUserClient

        # ...and it is given the pool, because `resolve_session_string()` reads the stored session from
        # it. Without a db the client can only ever use TELEGRAM_SESSION_STRING, which made a /login
        # succeed and the writer stay blind to it — the exact mismatch the control bot now refuses to say.
        app.state.user_client = TelegramUserClient(settings, db=app.state.db)
    app.state.worker = (
        Worker(db=app.state.db, settings=settings, telegram=app.state.user_client)
        if settings.worker_enabled
        else None
    )

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
