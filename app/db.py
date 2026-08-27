"""Asyncpg access layer.

Design constraints, in order of importance:

* **Boot without a database.** On Render free tier the service must answer
  ``/health`` even if Supabase is unreachable, or Render marks the deploy
  failed and the operator sees a dead app instead of a diagnosable one.
  :meth:`Database.ensure` therefore never raises; it reports state.
* **Everything correctness-critical lives in SQL.** Claiming a job, validating a
  stage transition, retrying, and releasing stale leases are database functions
  (0002_functions.sql), so two processes cannot corrupt each other and a job's
  position survives a hard kill.
* **No secrets in logs.** Errors are truncated; no query params are echoed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .stages import JobStage

log = logging.getLogger("auto_manager.db")

__all__ = ["Database", "DatabaseUnavailable", "DatabaseState"]

_MAX_ERROR_LEN = 400


class DatabaseUnavailable(RuntimeError):
    """Raised by query helpers when there is no live pool."""


class DatabaseState:
    DISCONNECTED = "not_configured"
    CONNECTING = "connecting"
    CONNECTED = "up"
    DEGRADED = "down"


@dataclass
class Database:
    settings: Settings
    _pool: Any = None
    _state: str = DatabaseState.DISCONNECTED
    _last_error: str | None = None
    _last_attempt: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    # ------------------------------------------------------------ lifecycle
    @property
    def state(self) -> str:
        if self.settings.database_url is None:
            return DatabaseState.DISCONNECTED
        return self._state

    @property
    def connected(self) -> bool:
        return self._pool is not None and self._state == DatabaseState.CONNECTED

    @property
    def last_error(self) -> str | None:
        return self._last_error

    def pool_kwargs(self) -> dict[str, Any]:
        """Arguments for ``asyncpg.create_pool``.

        Kept as a method so a test can check every key against asyncpg's real
        signature: it takes no ``**kwargs``, so one unexpected name is a
        TypeError at connect time — i.e. a service that boots but never reaches
        Supabase.
        """
        kwargs: dict[str, Any] = {
            "min_size": self.settings.db_pool_min,
            "max_size": max(self.settings.db_pool_min + 1, self.settings.db_pool_max),
            "timeout": self.settings.db_connect_timeout,
            "ssl": self.settings.db_ssl,
            "server_settings": {
                "search_path": self.settings.db_search_path,
                # application_name is a GUC, so it belongs in server_settings.
                "application_name": self.settings.app_name,
            },
        }
        if self.settings.uses_transaction_pooler:
            # Supabase's port-6543 pooler multiplexes sessions; asyncpg's
            # prepared-statement cache then errors on nearly every query.
            kwargs["statement_cache_size"] = 0
            kwargs["max_cached_statement_lifetime"] = 0
        return kwargs

    async def connect(self) -> bool:
        """Create the pool. Never raises: a boot without a DB is a supported state."""
        if self.settings.database_url is None:
            log.warning("DATABASE_URL not set — running with persistence disabled.")
            self._state = DatabaseState.DISCONNECTED
            return False
        async with self._lock:
            if self.connected:
                return True
            if time.monotonic() - self._last_attempt < 5:
                return False
            self._last_attempt = time.monotonic()
            self._state = DatabaseState.CONNECTING
            try:
                import asyncpg  # imported lazily so tests run without the driver
            except ImportError as exc:  # pragma: no cover - env dependent
                self._fail(f"asyncpg is not installed: {exc}")
                return False

            try:
                self._pool = await asyncpg.create_pool(
                    self.settings.database_url.get_secret_value(),
                    init=self._prepare_connection,
                    **self.pool_kwargs(),
                )
                self._state = DatabaseState.CONNECTED
                self._last_error = None
                log.info("database connected (pooler=%s)", self.settings.uses_transaction_pooler)
                return True
            except Exception as exc:  # noqa: BLE001 - report, do not crash boot
                self._fail(str(exc))
                return False

    @staticmethod
    def _encode_json(value: Any) -> str | None:
        return None if value is None else json.dumps(value)

    @staticmethod
    def _decode_json(value: str | None) -> Any:
        # Tolerate a value that never went through the codec (e.g. a raw text
        # column) instead of raising on the first non-JSON string.
        if value is None or value == "":
            return None
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return value

    _SEARCH_PATH_TOKEN = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

    def _quoted_search_path(self) -> str:
        """Quote each schema name, rejecting anything that is not an identifier.

        ``search_path`` cannot be parameterised, so the value from the
        environment is validated here rather than interpolated raw.
        """
        parts = [part.strip() for part in self.settings.db_search_path.split(",") if part.strip()]
        for part in parts:
            if not self._SEARCH_PATH_TOKEN.match(part):
                raise ValueError(f"DB_SEARCH_PATH contains an unsafe schema name: {part!r}")
        return ", ".join(f'"{part}"' for part in parts)

    async def _prepare_connection(self, conn: Any) -> None:
        """Per-connection setup, run for every connection the pool opens.

        asyncpg returns JSON and JSONB as raw *text* unless the built-in codecs
        are enabled. Without this, ``job.payload`` and every ``app.config``
        value arrive as strings and the queue silently mis-reads its own state —
        which no SQL-level test can catch, because psycopg decodes jsonb for
        you. This is exactly that bug.
        """
        for type_name in ("json", "jsonb"):
            # Explicit encoder/decoder: asyncpg leaves these as str by default,
            # and passing only the type name raises. Do not re-add a broad
            # suppress() here — it once swallowed this exact TypeError and the
            # queue shipped reading jsonb as text.
            await conn.set_type_codec(
                type_name,
                schema="pg_catalog",
                encoder=self._encode_json,
                decoder=self._decode_json,
            )
        if self.settings.db_search_path:
            await conn.execute(f"set search_path to {self._quoted_search_path()}")

    def _fail(self, message: str) -> None:
        self._state = DatabaseState.DEGRADED if self.settings.database_url else DatabaseState.DISCONNECTED
        self._last_error = message[:_MAX_ERROR_LEN]
        log.warning("database unavailable: %s", self._last_error)

    async def close(self) -> None:
        if self._pool is not None:
            try:
                await self._pool.close()
            finally:
                self._pool = None
                self._state = DatabaseState.DISCONNECTED

    # ------------------------------------------------------------ plumbing
    async def _acquire(self):
        if not self.connected:
            if not await self.connect():
                raise DatabaseUnavailable(self._last_error or "database unavailable")
        return self._pool.acquire()

    async def _run(self, coro_name: str, fn, *args: Any, many: bool = False) -> Any:
        """Run one query under a timeout, translating connection loss into a
        reconnect attempt rather than a crash."""
        attempts = 0
        while attempts < 2:
            attempts += 1
            holder = await self._acquire()
            try:
                async with holder as conn:
                    return await asyncio.wait_for(
                        fn(conn, *args), timeout=self.settings.db_query_timeout
                    )
            except asyncio.TimeoutError as exc:
                raise TimeoutError(f"{coro_name} exceeded {self.settings.db_query_timeout}s") from exc
            except (
                ConnectionError,
                OSError,
            ) as exc:  # pool went stale (Render restart / network blip)
                self._fail(f"{coro_name}: {exc}")
                self._state = DatabaseState.CONNECTING
                if attempts >= 2:
                    raise DatabaseUnavailable(self._last_error) from exc
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"{coro_name} failed: {str(exc)[:_MAX_ERROR_LEN]}") from exc
        raise DatabaseUnavailable(self._last_error or "database unavailable")

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        async def _fn(conn: Any) -> list[dict[str, Any]]:
            rows = await conn.fetch(sql, *args)
            return [dict(r) for r in rows]

        return await self._run("fetch", _fn)

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        async def _fn(conn: Any) -> dict[str, Any] | None:
            row = await conn.fetchrow(sql, *args)
            return dict(row) if row else None

        return await self._run("fetchrow", _fn)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        async def _fn(conn: Any) -> Any:
            return await conn.fetchval(sql, *args)

        return await self._run("fetchval", _fn)

    # ------------------------------------------------------------ health
    async def ping(self) -> tuple[bool, str | None]:
        if self.settings.database_url is None:
            return False, "not_configured"
        try:
            value = await self.fetchval("select 1")
            return (value == 1, None)
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)[:_MAX_ERROR_LEN]

    async def schema_ready(self) -> tuple[bool, str]:
        row = await self.fetchrow(
            """
            select
              to_regclass('app.job')            is not null as has_job,
              to_regproc('app.claim_next_job')  is not null as has_queue_functions,
              (select count(*) from app.config) as config_rows
            """
        )
        if not row or not row.get("has_job"):
            return False, "migrations_not_applied"
        if not row.get("has_queue_functions"):
            return False, "functions_missing"
        return True, f"config_rows={row.get('config_rows')}"

    # ------------------------------------------------------------ queue API
    async def heartbeat(self, worker_id: str) -> bool:
        try:
            await self.fetchval("select to_jsonb(app.record_heartbeat($1))", worker_id)
            return True
        except Exception as exc:  # noqa: BLE001
            log.debug("heartbeat skipped: %s", exc)
            return False

    async def is_paused(self) -> bool:
        value = await self.fetchval("select paused from app.service_state where id = 1")
        return bool(value)

    async def set_paused(self, paused: bool, reason: str | None = None) -> dict[str, Any] | None:
        return await self.fetchval("select to_jsonb(app.set_pause($1, $2))", paused, reason)

    async def release_expired_locks(self) -> int:
        return int(await self.fetchval("select app.release_expired_locks()") or 0)

    async def enqueue(
        self,
        kind: str,
        dedup_key: str,
        *,
        stage: JobStage = JobStage.DISCOVERED,
        payload: dict[str, Any] | None = None,
        # dicts go straight to jsonb params; the pool encoder serialises them.
        priority: int = 100,
        episode_id: int | None = None,
        variant_id: int | None = None,
        candidate_id: int | None = None,
        season_id: int | None = None,
        destination_id: int | None = None,
    ) -> dict[str, Any] | None:
        """Insert a job, or return None if that dedup_key already exists.

        Re-scanning a source channel after a restart must never double-queue a
        file, so duplicates are collapsed on insert (ON CONFLICT DO NOTHING).
        """
        # to_jsonb(...) rather than `row_to_json(j) as job ...`: the latter
        # returns {"job": {...}}, and every caller then KeyErrors on "id".
        return await self.fetchval(
            """
            select to_jsonb(app.enqueue_job(
              $1::app.job_kind, $2, $3::app.job_stage, $4::jsonb, $5,
              $6, $7, $8, $9, $10
            ))
            """,
            kind,
            dedup_key,
            stage.value,
            payload or {},
            priority,
            episode_id,
            variant_id,
            candidate_id,
            season_id,
            destination_id,
        )

    async def claim(self, worker_id: str) -> dict[str, Any] | None:
        return await self.fetchval(
            "select to_jsonb(app.claim_next_job($1, $2))",
            worker_id,
            self.settings.claim_lease_seconds,
        )

    async def checkpoint(
        self, job_id: int, stage: JobStage, data: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        return await self.fetchval(
            """
            select to_jsonb(
              app.checkpoint_job($1, $2::app.job_stage, $3::jsonb)
            )
            """,
            job_id,
            stage.value,
            data or {},
        )

    async def complete(self, job_id: int, result: dict[str, Any] | None = None) -> None:
        await self.fetchval(
            "select to_jsonb(app.complete_job($1, $2::jsonb))", job_id, result or {}
        )

    async def fail(self, job_id: int, error: str, retry_after: int = 60) -> None:
        await self.fetchval(
            "select to_jsonb(app.fail_job($1, $2, $3))",
            job_id,
            error[:_MAX_ERROR_LEN],
            retry_after,
        )

    async def queue_health(self) -> dict[str, Any] | None:
        return await self.fetchrow("select * from app.v_queue_health")

    async def config(self, key: str, default: Any = None) -> Any:
        """Read one row of app.config, decoded.

        Templates live here as JSONB precisely so that changing a caption never
        needs a migration.
        """
        raw = await self.fetchval("select value from app.config where key = $1", key)
        if raw is None:
            return default
        return self._decode_json(raw) if isinstance(raw, str) else raw

    async def describe(self) -> dict[str, Any]:
        ok, detail = await self.ping()
        info: dict[str, Any] = {"state": self.state, "reachable": ok}
        if detail:
            info["error"] = detail
        if ok:
            ready, why = await self.schema_ready()
            info["schema"] = "ok" if ready else why
            health = await self.queue_health()
            if health:
                info["queue"] = {k: int(v or 0) for k, v in health.items()}
        return info
