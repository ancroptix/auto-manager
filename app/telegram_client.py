"""Telethon wrapper for the spare user account.

Import of ``telethon`` is lazy on purpose: the FastAPI service, its tests, and
the whole health/queue layer must work on a machine with no Telegram
credentials at all. Connecting is an explicit opt-in.

The session is a ``StringSession`` read from the environment. It is never
written to disk, because Render's filesystem is ephemeral and a stray
``*.session`` file is the single most common way these accounts get leaked.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from .config import Settings

log = logging.getLogger("auto_manager.telegram")

__all__ = ["TelegramUserClient", "TelegramNotConfigured", "probe_once"]

# Flood-wait courtesy ceiling: exceeding this pauses the whole service rather
# than pushing on. Telegram restricts unwanted messaging; the account, not the
# queue, is the scarce resource here.
MAX_FLOOD_WAIT_SECONDS = 900


class TelegramNotConfigured(RuntimeError):
    pass


@dataclass
class TelegramUserClient:
    settings: Settings
    db: Any = None  # Database | None; only needed to read a stored session
    _client: Any = field(default=None, repr=False)
    _connected: bool = False

    async def resolve_session_string(self) -> str:
        """The session to authenticate with: from the environment, or the database.

        The database path exists so a session string never has to pass through a
        terminal, a file or a chat log — the control bot logs the account in and
        stores the result, and this is where it is read back. Precedence is
        explicit: an environment value wins, so a deliberately set string is never
        silently overridden by an older stored one.
        """
        from_env = self.settings.reveal("telegram_session_string")
        if from_env:
            return from_env
        if self.settings.telegram_session_source not in ("database", "both"):
            raise TelegramNotConfigured(
                "TELEGRAM_SESSION_STRING is unset and TELEGRAM_SESSION_SOURCE="
                + self.settings.telegram_session_source
                + " forbids reading a stored session"
            )
        if self.db is None or not getattr(self.db, "connected", False):
            raise TelegramNotConfigured(
                "TELEGRAM_SESSION_STRING is unset and there is no database to read "
                "a stored session from"
            )
        from .sessions import active_session_string

        stored = await active_session_string(self.db)
        if not stored:
            raise TelegramNotConfigured(
                "no active session is stored yet: message the control bot "
                "'/login spare +<phone>' and complete the code"
            )
        return stored

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def configured(self) -> bool:
        """Live mode plus credentials.

        A *stored* session counts as configured here and is checked for existence
        at connect time, because existence needs a query and this property is read
        from request paths and the worker loop."""
        return self.settings.outbound_enabled

    async def start(self) -> Any:
        if not self.configured:
            raise TelegramNotConfigured(
                "Telegram client needs TELEGRAM_API_ID, TELEGRAM_API_HASH and "
                "TELEGRAM_SESSION_STRING, and APP_MODE=live."
            )
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        session_string = await self.resolve_session_string()
        if self._client is None:
            self._client = TelegramClient(
                StringSession(session_string),
                self.settings.telegram_api_id,
                self.settings.telegram_api_hash.get_secret_value(),
                flood_sleep_threshold=min(MAX_FLOOD_WAIT_SECONDS, 60),
                request_retries=2,
                connection_retries=3,
                auto_reconnect=True,
            )
        await self._client.connect()
        self._connected = bool(await self._client.is_user_authorized())
        if not self._connected:
            raise TelegramNotConfigured(
                "session string is not authorized on this account; re-run scripts/login.py"
            )
        me = await self._client.get_me()
        log.info("telegram user client online as %s (id=%s)", getattr(me, "username", "?"), getattr(me, "id", "?"))
        return self._client

    @property
    def client(self) -> Any:
        """The live Telethon client, or None.

        Exposed for :mod:`app.probe`, which brings its own guard: giving the probe
        a raw client is safer than wrapping every method, because the guard is one
        function to audit instead of a facade to remember.
        """
        return self._client if self._connected else None

    async def probe(self, *, db: Any = None, send: bool = True) -> dict[str, Any]:
        """Discover the storage bot and Channel Help protocols. Read-only."""
        from .probe import ProbePolicy, run_probe

        client = self.client
        if client is None:
            raise TelegramNotConfigured("cannot probe: the user client is not connected")
        policy = ProbePolicy(
            owner_user_id=self.settings.telegram_main_admin_user_id,
        )
        return await run_probe(client, policy=policy, db=db, send=send)

    async def stop(self) -> None:
        if self._client is not None:
            with_suppress = getattr(self._client, "disconnect", None)
            if with_suppress is not None:
                try:
                    await with_suppress()
                except Exception as exc:  # noqa: BLE001
                    log.debug("disconnect during shutdown: %s", exc)
        self._connected = False
        self._client = None

    def is_owner(self, entity_or_id: Any) -> bool:
        user_id = getattr(entity_or_id, "id", entity_or_id)
        try:
            return self.settings.is_owner(int(user_id))
        except (TypeError, ValueError):
            return False

    async def guard_owner(self, event: Any) -> bool:
        """Single gate for every inbound owner command.

        Anything not sent by a configured owner ID is ignored and logged, so a
        random user cannot drive the account by guessing command names.
        """
        sender = getattr(getattr(event, "sender", None), "id", None) or getattr(
            getattr(event, "chat", None), "id", None
        )
        if self.is_owner(sender):
            return True
        log.warning("ignored command from non-owner id=%s", sender)
        return False

    async def wait_flood(self, error: Exception) -> bool:
        """Honour FloodWait by pausing the service instead of retrying harder.

        Returns True when a pause was requested. Never subtracts from the wait,
        never retries around it, and never switches accounts: those are the
        behaviours Telegram treats as spam, and they would cost the account.
        """
        seconds = getattr(error, "seconds", None)
        if not seconds:
            return False
        seconds = int(seconds)
        log.error("flood wait of %ss reported; pausing service", seconds)
        if seconds > MAX_FLOOD_WAIT_SECONDS:
            log.error("flood wait exceeds %ss; leaving service paused", MAX_FLOOD_WAIT_SECONDS)
        await asyncio.sleep(min(seconds, MAX_FLOOD_WAIT_SECONDS))
        return True


async def probe_once(settings: Settings, db: Any = None, *, send: bool = True) -> dict[str, Any]:
    """Connect, discover, disconnect — the entrypoint for ``PROBE_ON_BOOT`` and
    ``POST /control/probe``.

    It builds its own client on purpose. Reusing the worker's connection would
    mean a probe failure could take the queue loop down with it, and the probe is
    by design the riskiest thing this service does with a user session.
    """
    client = TelegramUserClient(settings=settings, db=db)
    await client.start()
    try:
        return await client.probe(db=db, send=send)
    finally:
        await client.stop()
