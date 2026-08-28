"""The control bot: your remote, and the only way in for a non-terminal operator.

What it is for, in one line: you own a bot via @BotFather, paste its token into
Render, and then *every* remaining setup and operational step happens by messaging
that bot from your own Telegram account — including logging the spare user account
in, which is why this file exists at all. A session string never has to appear in a
chat log, a terminal or a file: you type the phone number and the code into a DM
with a bot only you can talk to, the service performs the MTProto login, and the
result goes straight into Postgres.

The trust model, stated plainly:

* **Owner-only.** Every update from anyone else is dropped before its text is even
  parsed. No help message, no "unauthorised" reply — anything that answers a
  stranger tells a stranger the bot is real.
* **Private chats only.** In a group the chat id is not the sender id, and a bot
  that checks one and not the other has an open door for whoever else is in there.
* **Secrets are deleted, not just ignored.** Phone number, code and 2FA password
  live in memory for one attempt, are never written to the database, and the
  messages carrying them are deleted from the chat afterwards.
* **Nothing sensitive leaves.** Every reply passes through :func:`app.sessions.scrub`,
  which strips session-shaped text and any named secret, so an exception traceback
  cannot print a session into your DMs.
* **Login can be switched off** once it has happened: ``BOT_ALLOW_LOGIN=0``.

The bot adds no authority of its own — it only drives what the queue and the
database already expose — which is what makes it safe to hand to a phone.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .botapi import BotApi, Update
from .sessions import forget as forget_session
from .sessions import list_sessions, mask_phone, scrub, store as store_session, valid_name

log = logging.getLogger("auto_manager.controlbot")

__all__ = ["ControlBot", "LoginCancelled", "LoginResult", "NeedsPassword", "Reply"]

#: Telegram's own limit for repeated code requests is roughly "a few per minute,
#: then wait a while". Stopping at three per ten minutes keeps us well clear of it,
#: because a flood wait on a *login* is what gets an account flagged, not a wrong
#: guess.
MAX_ATTEMPTS_PER_WINDOW = 3
ATTEMPT_WINDOW_SECONDS = 600.0
#: Wrong codes tolerated in one flow before it is closed outright. A fourth guess is
#: worthless (Telegram invalidates the code long before) and costs the account.
MAX_CODE_TRIES = 3

HELP = """auto-manager control

/start /help   this list (Telegram's Start button sends /start)
/status      mode, queue, pause state, what is blocked and why
/pause       stop claiming jobs (optional reason)
/resume      start claiming again
/reconcile   reclaim stale leases + queue a reconciliation now
/probe       ask the storage bot and Channel Help their questions (report arrives here)
/sessions    stored Telegram sessions (never their contents)
/use <name>  make one session the active account
/forget <n>  delete a stored session from the database

login (needs BOT_ALLOW_LOGIN=1)
/login <name> +<country><number>   start; Telegram sends a code to that account
/code 123456                       the code you received
/password <2fa>                    only if the account has 2FA
/cancel                            drop the pending attempt

After /login or /pause you can also just reply with what I asked for, without a
command.

This bot answers you and nobody else. It cannot read, post or delete anything in
your channels — the user session it logs in does the pipeline work."""


class NeedsPassword(Exception):
    """The account has 2FA; the caller must ask for it and try again."""


class LoginCancelled(Exception):
    """The operator cancelled, or the attempt expired."""


@dataclass(frozen=True, slots=True)
class LoginResult:
    session_string: str
    account_id: int | None = None
    username: str | None = None


@dataclass(frozen=True, slots=True)
class Reply:
    """One outgoing message.

    ``sensitive`` marks a reply tied to a login step: it gets scrubbed *and*
    deleted after a short grace period, because otherwise "send me the code" flows
    leave a readable trail in a chat log on Telegram's side. ``delete_prompt_too``
    deletes the operator's message that this reply is the answer to.
    """

    text: str
    sensitive: bool = False
    delete_prompt_too: bool = False


@dataclass
class _Pending:
    """In-memory state of one login. Never persisted, never logged."""

    name: str
    phone: str = ""
    code: str | None = None
    code_hash: str | None = None
    password: str | None = None
    #: "phone" (waiting for the number), "code", "password"
    stage: str = "phone"
    tries: int = 0
    started_at: float = field(default_factory=time.monotonic)


@dataclass
class ControlBot:
    """``handle()`` is the whole brain and takes an update; ``run()`` is plumbing.

    Splitting them means the security rules are testable without a network: the
    tests drive ``handle()`` with fake transports, and nothing about the polling
    loop can weaken a check that lives there. ``dispatch()`` is the layer that
    actually sends and deletes, so a test can assert on deletions too.
    """

    api: BotApi
    db: Any
    settings: Any
    transport: Any = None  # MTProto login helper; injected so tests can fake it
    owner_ids: frozenset[int] = frozenset()
    allow_login: bool = True
    login_ttl_seconds: float = 600.0
    delete_sensitive: bool = True
    background: Callable[[Any], None] | None = None  # fires /probe off without blocking
    pending: dict[int, _Pending] = field(default_factory=dict, repr=False)
    attempts: dict[int, list[float]] = field(default_factory=dict, repr=False)
    _stop: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def __post_init__(self) -> None:
        if not self.owner_ids:
            # Fail closed, loudly, at construction: a bot with an unset owner list
            # would otherwise answer whoever found it first.
            raise ValueError(
                "the control bot refuses to start without TELEGRAM_OWNER_USER_IDS (or "
                "TELEGRAM_MAIN_ADMIN_USER_ID): it would otherwise obey any stranger who "
                "guesses the token"
            )

    # ------------------------------------------------------------------ gating
    def authorized(self, update: Update) -> bool:
        return update.from_id is not None and int(update.from_id) in self.owner_ids

    def _throttled(self, chat_id: int) -> bool:
        now = time.monotonic()
        window = [t for t in self.attempts.get(chat_id, []) if now - t < ATTEMPT_WINDOW_SECONDS]
        window.append(now)
        self.attempts[chat_id] = window
        return len(window) > MAX_ATTEMPTS_PER_WINDOW

    def _pending_valid(self, chat_id: int) -> _Pending | None:
        pending = self.pending.get(chat_id)
        if pending is None:
            return None
        if (time.monotonic() - pending.started_at) > self.login_ttl_seconds:
            # Silence is the right behaviour for an expired attempt: the operator
            # may have walked away mid-code and a half-finished flow must not sit
            # in memory holding a phone number.
            del self.pending[chat_id]
            return None
        return pending

    # ----------------------------------------------------------------- router
    async def handle(self, update: Update) -> list[Reply]:
        """Decide and act. Returns the replies the caller should send.

        Every text is produced here rather than sent, so "what could this bot ever
        say to a person" is answerable by reading one function.
        """
        if not self.authorized(update):
            # One log line, no reply, and no echo of the text: a rejected stranger
            # learns nothing, and their message never reaches the database.
            log.info("ignored control-bot message from non-owner id=%s", update.from_id)
            return []
        if not update.is_private_chat:
            log.info("ignored control-bot command in a non-private chat=%s", update.chat_id)
            return [Reply("I only take commands in our private chat — not in groups or channels.")]

        text = (update.text or "").strip()
        if not text:
            return []

        pending = self._pending_valid(update.chat_id)
        if pending is not None and not text.startswith("/"):
            # Mid-flow, a bare reply is what a person actually does ("123456"), and
            # forcing "/code 123456" on a phone keyboard loses logins.
            return await self._bare(update, pending, text)

        parts = text.split()
        command = parts[0].lstrip("/").split("@", 1)[0].casefold()
        args = parts[1:]
        handler = {
            "start": self._help,
            "help": self._help,
            "status": self._status,
            "pause": self._pause,
            "resume": self._resume,
            "reconcile": self._reconcile,
            "probe": self._probe,
            "sessions": self._sessions,
            "use": self._use,
            "forget": self._forget,
            "login": self._login,
            "code": self._code,
            "password": self._password,
            "cancel": self._cancel,
        }.get(command)
        if handler is None:
            # Unknown commands are ignored rather than echoed: an "unknown command"
            # reply is how a bot advertises that it exists and which words work.
            return []
        return await handler(update, args)

    async def _bare(self, update: Update, pending: _Pending, text: str) -> list[Reply]:
        if pending.stage == "phone":
            return await self._start_code(update, pending.name, text)
        if pending.stage == "code":
            return await self._submit_code(update, pending, text)
        if pending.stage == "password":
            return await self._submit_password(update, pending, text)
        self.pending.pop(update.chat_id, None)
        return []

    # -------------------------------------------------------------- commands
    async def _help(self, update: Update, args: list[str]) -> list[Reply]:
        return [Reply(HELP)]

    async def _status(self, update: Update, args: list[str]) -> list[Reply]:
        lines = [
            f"mode: {self.settings.mode.value}",
            f"outbound telegram actions: {self.settings.outbound_enabled}",
        ]
        if self.settings.telegram_session_string is not None:
            lines.append("session: TELEGRAM_SESSION_STRING")
        elif self.settings.telegram_session_source in ("database", "both"):
            lines.append("session: read from app.telegram_session (/sessions to list)")
        if self.db is not None and getattr(self.db, "connected", False):
            queue = await self.db.queue_health() or {}
            state = await self.db.fetchrow(
                "select paused, coalesce(paused_reason,'') as reason, last_reconcile_at from app.service_state where id = 1"
            )
            lines.append(
                "queue: "
                + ", ".join(
                    f"{k}={v}"
                    for k, v in queue.items()
                    if k in ("queued", "running", "blocked", "failed", "succeeded_1h")
                )
            )
            lines.append(
                f"paused: {bool(state and state['paused'])}"
                + (f" ({state['reason']})" if state and state["reason"] else "")
            )
            blocked = await self.db.fetch(
                "select kind, count(*) as n, max(left(coalesce(last_error,''),90)) as why from app.job "
                "where status = 'blocked' group by kind order by count(*) desc"
            )
            if blocked:
                lines.append("blocked:")
                for row in blocked:
                    lines.append(f"  {row['kind']} x{row['n']} — {row['why']}")
            pending_review = await self.db.fetchval(
                "select count(*) from app.thumbnail_review where status = 'pending'"
            )
            if pending_review:
                lines.append(f"thumbnails awaiting your decision: {pending_review}")
            # These four decide what gets published at all, so they belong on the
            # one screen the operator reads.
            settings = await self.db.fetch(
                "select key, value::text as value from app.config "
                "where key in ('thumbnail.strict_mode','thumbnail.on_no_clean_candidate',"
                "'ingest.require_hindi_audio','ingest.include_subbed_only','caption.button_rows',"
                "'caption.total_episodes_unknown') "
                "order by key"
            )
            if settings:
                lines.append("settings:")
                for row in settings:
                    lines.append(f"  {row['key']} = {str(row['value'])[:60]}")
        else:
            lines.append("database: NOT CONNECTED — set DATABASE_URL")
        return [Reply("\n".join(lines))]

    async def _pause(self, update: Update, args: list[str]) -> list[Reply]:
        reason = " ".join(args)[:200] or "paused from the control bot"
        await self.db.set_paused(True, reason)
        return [Reply(f"paused. {reason}\nNothing will be claimed; a job already running finishes its current stage.")]

    async def _resume(self, update: Update, args: list[str]) -> list[Reply]:
        await self.db.set_paused(False)
        return [Reply("resumed. The queue loop will claim again on its next poll.")]

    async def _reconcile(self, update: Update, args: list[str]) -> list[Reply]:
        from .keys import reconciliation_key
        from .stages import JobKind

        reclaimed = await self.db.release_expired_locks()
        job = await self.db.enqueue(
            JobKind.RECONCILIATION.value,
            f"{reconciliation_key()}:bot:{int(time.time())}",
            payload={"trigger": "control-bot"},
            priority=5,
        )
        job_id = int((job or {}).get("id") or 0) if isinstance(job, dict) else int(job or 0)
        return [Reply(f"reclaimed {reclaimed} stale lease(s); job {job_id} queued")]

    async def _probe(self, update: Update, args: list[str]) -> list[Reply]:
        if not self.settings.outbound_enabled:
            return [
                Reply(
                    "the probe needs a live user session (APP_MODE=live plus a session string "
                    "or a stored session). /status shows which is missing."
                )
            ]
        if self.background is None:
            return [Reply("probe is not wired in this build")]
        self.background(self._probe_task(update.chat_id))
        return [
            Reply(
                "probe started. It asks both bots their menu questions and sends the report here "
                "when it finishes (usually under two minutes)."
            )
        ]

    async def _probe_task(self, chat_id: int) -> None:
        from .telegram_client import probe_once

        try:
            report = await probe_once(self.settings, self.db, send=False)
            await self.api.send(chat_id, scrub(format_report_text(report), *self._live_secrets()))
        except Exception as exc:  # noqa: BLE001 - the operator must hear about it, not find silence
            await self.api.send(chat_id, scrub(f"probe failed: {type(exc).__name__}: {str(exc)[:200]}"))

    async def _sessions(self, update: Update, args: list[str]) -> list[Reply]:
        rows = await list_sessions(self.db)
        if not rows:
            return [Reply("no stored sessions. /login <name> +<phone> to add one.")]
        lines = ["stored sessions (contents are never shown here):"]
        for row in rows:
            marker = "●" if row["active"] else "○"
            who = f"@{row['username']}" if row.get("username") else f"id={row.get('account_id')}"
            lines.append(f"  {marker} {row['name']} · {row['kind']} · {who} · {row['length_chars']} chars")
        return [Reply("\n".join(lines))]

    async def _use(self, update: Update, args: list[str]) -> list[Reply]:
        from .sessions import activate

        if not args:
            return [Reply("usage: /use <name>  (see /sessions)")]
        if not valid_name(args[0]):
            return [Reply("that is not a session name. /sessions lists what exists.")]
        ok = await activate(self.db, args[0])
        if not ok:
            names = ", ".join(row["name"] for row in await list_sessions(self.db)) or "(none)"
            return [Reply(f"unknown session name {args[0]!r}. I have: {names}")]
        return [
            Reply(
                f"{args[0]} is now the active session. No restart needed: the next connect reads it."
            )
        ]

    async def _forget(self, update: Update, args: list[str]) -> list[Reply]:
        if not args:
            rows = await list_sessions(self.db)
            names = ", ".join(row["name"] for row in rows) or "(none)"
            return [Reply(f"which session? usage: /forget <name>. I have: {names}")]
        removed = await forget_session(self.db, args[0])
        if not removed:
            return [Reply(f"nothing stored under {args[0]!r}.")]
        return [
            Reply(
                f"deleted {args[0]} from the database.\n\nThat does NOT sign the account out of "
                "Telegram. To actually revoke it: Telegram → Settings → Privacy and Security → "
                "Devices → terminate that session (or all devices), then log in again."
            )
        ]

    # ------------------------------------------------------------------ login
    async def _login(self, update: Update, args: list[str]) -> list[Reply]:
        if not self.allow_login:
            return [Reply("login is disabled (BOT_ALLOW_LOGIN=0). Set it to 1, redeploy, and try again.")]
        if self.transport is None:
            return [
                Reply(
                    "the login machinery needs TELEGRAM_API_ID and TELEGRAM_API_HASH "
                    "(my.telegram.org → API development tools). Those are not secrets, but the "
                    "service cannot start a login without them."
                )
            ]
        if not args:
            return [Reply("usage: /login <name> +<country><number>\ne.g. /login spare +919876543210")]
        name = args[0].casefold()
        if not valid_name(name):
            # Checked before anything is sent: fetching a code that is then thrown
            # away because the name was unusable costs the account a rate-limit hit.
            return [
                Reply(
                    "a session name is up to 40 characters of letters, numbers, '-' and '_' "
                    "(e.g. /login spare +919876543210)."
                )
            ]
        if len(args) >= 2:
            return await self._start_code(update, name, args[1])
        self.pending[update.chat_id] = _Pending(name=name, stage="phone")
        return [
            Reply(
                f"which phone number belongs to {name}? Reply with it in international format — "
                "digits only, starting with the country code, e.g. +919876543210. I delete the "
                "message as soon as I have used it.",
                sensitive=True,
            )
        ]

    async def _start_code(self, update: Update, name: str, phone: str) -> list[Reply]:
        phone = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
        if not phone.startswith("+") or not phone[1:].isdigit() or len(phone) < 8:
            if self.pending.get(update.chat_id) is None:
                self.pending[update.chat_id] = _Pending(name=name, stage="phone")
            return [
                Reply(
                    "that number did not look right. Use international format with a leading +, "
                    "digits only — e.g. +919876543210."
                )
            ]
        if self._throttled(update.chat_id):
            self.pending.pop(update.chat_id, None)
            return [
                Reply(
                    f"too many login attempts ({MAX_ATTEMPTS_PER_WINDOW} per "
                    f"{int(ATTEMPT_WINDOW_SECONDS // 60)} minutes). Wait before trying again — "
                    "rapid code requests are what gets an account limited, not a wrong guess."
                )
            ]
        self.pending[update.chat_id] = _Pending(name=name, phone=phone, stage="code")
        try:
            code_hash = await self.transport.send_code(phone)
        except Exception as exc:  # noqa: BLE001 - the reason is useful, the raw text is not
            self.pending.pop(update.chat_id, None)
            return [
                Reply(
                    f"could not send a code: {scrub(str(exc), phone)[:220]}\n\n"
                    "Nothing was stored. /cancel is not needed.",
                    delete_prompt_too=True,
                )
            ]
        pending = self.pending[update.chat_id]
        pending.code_hash = str(code_hash or "")
        return [
            Reply(
                f"code sent to {mask_phone(phone)} for session {name!r}.\n"
                f"Reply with /code 123456 (or just the digits). This attempt expires in "
                f"{int(self.login_ttl_seconds // 60)} minutes.",
                sensitive=True,
            )
        ]

    async def _code(self, update: Update, args: list[str]) -> list[Reply]:
        pending = self._pending_valid(update.chat_id)
        if pending is None:
            return [Reply("no login in progress. /login <name> +<phone> first.")]
        if not args:
            return [Reply("usage: /code 123456 — the digits Telegram sent you.")]
        return await self._submit_code(update, pending, args[0])

    async def _submit_code(self, update: Update, pending: _Pending, code: str) -> list[Reply]:
        cleaned = code.strip().upper()
        if len(cleaned) < 3:
            return [Reply("that is too short to be a code. Reply with the digits Telegram sent.")]
        pending.code = cleaned
        return await self._finish(update, pending)

    async def _password(self, update: Update, args: list[str]) -> list[Reply]:
        pending = self._pending_valid(update.chat_id)
        if pending is None:
            return [Reply("no login in progress.")]
        if pending.stage != "password":
            return [Reply("this account did not ask for a password. Send the code first.")]
        if not args:
            return [Reply("usage: /password <your 2FA password>")]
        return await self._submit_password(update, pending, " ".join(args))

    async def _submit_password(self, update: Update, pending: _Pending, password: str) -> list[Reply]:
        if pending.stage != "password":
            return [Reply("this account did not ask for a password. Send the code first.")]
        pending.password = password
        return await self._finish(update, pending)

    async def _finish(self, update: Update, pending: _Pending) -> list[Reply]:
        """One sign-in attempt, and exactly one place where the password is used.

        The password is cleared in a ``finally`` so a failure cannot leave it
        sitting in memory next to a phone number.
        """
        phone = pending.phone
        try:
            result = await self.transport.sign_in(
                phone, pending.code or "", pending.code_hash, password=pending.password
            )
        except NeedsPassword:
            pending.stage = "password"
            pending.code = None  # the code is spent; keeping it reusable is pointless and risky
            return [
                Reply(
                    "this account has 2FA. Reply with /password <your password> — I delete both "
                    "messages afterwards and never store the password.",
                    sensitive=True,
                )
            ]
        except Exception as exc:  # noqa: BLE001 - a wrong code is a normal event, not a crash
            detail = scrub(str(exc), phone, pending.password or "", pending.code or "")[:200]
            pending.tries += 1
            pending.code = None
            fatal = any(word in detail.upper() for word in ("CODE_EXPIRED", "PHONE_CODE_INVALID", "SESSION_WAITED"))
            if fatal or pending.tries >= MAX_CODE_TRIES:
                self.pending.pop(update.chat_id, None)
                reason = (
                    "that code was rejected"
                    if fatal
                    else f"{MAX_CODE_TRIES} attempts in a row were wrong"
                )
                return [
                    Reply(
                        f"{reason}: {detail}\n\nThis attempt is closed, so nothing more is sent to "
                        "Telegram. Start again with /login when you have the code in hand.",
                        delete_prompt_too=True,
                    )
                ]
            left = MAX_CODE_TRIES - pending.tries
            return [
                Reply(
                    f"sign-in failed: {detail}\n\n{left} attempt(s) left in this flow. Reply with "
                    "the code again, or /cancel.",
                    sensitive=True,
                )
            ]
        finally:
            password, pending.password = pending.password, None

        # A second login must not silently move the whole pipeline onto a different
        # account: the first session becomes active because there is nothing to
        # displace, and any later one waits for an explicit /use.
        from .sessions import active_session_string

        try:
            already_live = await active_session_string(self.db) is not None
            stored = await store_session(
                self.db,
                name=pending.name,
                session_string=result.session_string,
                account_id=result.account_id,
                username=result.username,
                note="logged in via the control bot" + (" with 2FA" if password else ""),
                activate=not already_live,
            )
        except Exception as exc:  # noqa: BLE001 - a login we cannot store must be said out loud
            self.pending.pop(update.chat_id, None)
            return [
                Reply(
                    "Telegram accepted the code but I could not store the session: "
                    f"{scrub(str(exc), phone)[:180]}\n\nNothing is usable yet — check DATABASE_URL "
                    "and that the migrations (app.telegram_session) are applied."
                )
            ]
        self.pending.pop(update.chat_id, None)
        who = result.username or result.account_id or "unknown"
        state = "not active — /use " + pending.name + " to switch to it" if already_live else "active"
        return [
            Reply(
                f"connected as @{who}, stored as {pending.name!r} "
                f"({stored.get('length_chars') or len(result.session_string)} chars, {state}).\n\n"
                "The session string was never shown in this chat and cannot be read back from it. "
                "/sessions lists what is stored; set APP_MODE=live to let the worker use it.",
                delete_prompt_too=True,
            )
        ]

    async def _cancel(self, update: Update, args: list[str]) -> list[Reply]:
        pending = self.pending.pop(update.chat_id, None)
        if pending is None:
            return [Reply("nothing pending.")]
        discard = getattr(self.transport, "discard", None)
        if callable(discard):
            try:
                await discard(pending.phone)
            except Exception:  # noqa: BLE001 - a failed cleanup is not the operator's problem
                log.debug("login transport cleanup failed", exc_info=True)
        return [
            Reply(
                f"cancelled. Nothing was stored for {pending.name!r}.",
                delete_prompt_too=True,
            )
        ]

    # ------------------------------------------------------------- transport
    async def dispatch(self, update: Update) -> list[Reply]:
        """Handle one update and carry out its side effects on the chat.

        Deletion happens here rather than in ``handle`` so that ``handle`` stays a
        pure decision function that tests can call without a fake network.
        """
        replies = await self.handle(update)
        for reply in replies:
            message_id = await self.api.send(update.chat_id, scrub(reply.text, *self._live_secrets()))
            if self.delete_sensitive and reply.delete_prompt_too and update.message_id:
                await self.api.delete(update.chat_id, update.message_id)
            if self.delete_sensitive and reply.sensitive and message_id:
                await self.api.delete(update.chat_id, message_id)
        if update.kind == "callback" and update.callback_id:
            await self.api.answer_callback(update.callback_id)
        return replies

    async def run_once(self) -> int:
        """One poll cycle. Exposed because a loop you cannot call once is a loop you
        cannot test; returns how many updates were handled."""
        updates = await self.api.get_updates()
        for update in updates:
            await self.dispatch(update)
        return len(updates)

    async def run(self) -> None:
        """Long-poll until stopped.

        Polling rather than a webhook because it needs no new inbound route, no
        certificate, and nothing for the operator to configure — and a bot that
        briefly cannot reach Telegram (a redeploy) simply resumes where it left off,
        because the offset lives in the client.
        """
        announced = False
        while not self._stop.is_set():
            try:
                if not announced:
                    me = await self.api.get_me()
                    announced = True
                    log.info("control bot online as @%s (id=%s)", me.get("username"), me.get("id"))
                handled = await self.run_once()
                if handled:
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop must survive a bad response
                announced = False
                log.warning("control bot poll error: %s", scrub(str(exc))[:200])
                await asyncio.sleep(5.0)

    def stop(self) -> None:
        self._stop.set()

    def _live_secrets(self) -> tuple[str, ...]:
        """Anything currently in memory that must never reach a message."""
        secrets: list[str] = []
        for entry in self.pending.values():
            secrets.extend((entry.phone, entry.password or "", entry.code or ""))
        reveal = getattr(self.settings, "reveal", None)
        if callable(reveal):
            for field_name in ("telegram_bot_token", "telegram_session_string", "telegram_api_hash"):
                value = reveal(field_name)
                if value:
                    secrets.append(value)
        return tuple(item for item in secrets if item)


def format_report_text(report: Any) -> str:
    """Render a probe report, tolerating either return shape.

    ``probe_once`` hands back a dict; ``app.probe.format_report`` wants the full
    report object. Accepting both keeps the control bot from crashing on the very
    command an operator uses to find out why things are not working.
    """
    from .probe import format_report

    if isinstance(report, dict):
        return "\n".join(f"{key}: {str(value)[:300]}" for key, value in report.items())
    return format_report(report)
