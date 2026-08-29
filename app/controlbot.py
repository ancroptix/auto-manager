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
  operator's message carrying each one is deleted from the chat once it has been
  used. Our own replies are never deleted: they hold the instruction being followed,
  they are masked before they are sent, and a question that erases itself mid-flow
  reads as a broken bot rather than a careful one.
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

__all__ = ["ControlBot", "LoginCancelled", "LoginResult", "LoginUnstored", "NeedsPassword", "Reply"]

#: Telegram's own limit for repeated code requests is roughly "a few per minute,
#: then wait a while". Stopping at three per ten minutes keeps us well clear of it,
#: because a flood wait on a *login* is what gets an account flagged, not a wrong
#: guess.
MAX_ATTEMPTS_PER_WINDOW = 3
ATTEMPT_WINDOW_SECONDS = 600.0
#: How long a /login reply may wait for the writer to adopt the new session. Telegram's own connect is
#: seconds, not minutes, and a hand-off that cannot answer is better reported as a timeout than as silence.
_ADOPT_TIMEOUT = 20.0

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
/declare     say how long a season is (Total Episodes, and the batch post)
/source      say what a source channel carries (series, audio, season) — for bare-file channels
/inplace     caption the files already posted in your own channel (no delete; link + post still run)
/joinmsg     what a join requester is told: options, your own words, or switch it off
/card        name the post a shareable link is made from, per destination channel (the announcement)
/sticker     which sticker message opens a season, and from where
/campaign    draft, plan, confirm a join-request campaign: two steps before anyone is messaged
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


#: Command -> handler method name. The help text is checked against this table by the tests,
#: so neither a routed-but-undocumented command nor a documented-but-dead one survives a
#: refactor. Mapped by *name* rather than by bound method because this file is read by a
#: person more often than by Python, and the router stays three lines long.
_ROUTES: dict[str, str] = {
    "start": "_help",
    "help": "_help",
    "status": "_status",
    "pause": "_pause",
    "resume": "_resume",
    "reconcile": "_reconcile",
    "probe": "_probe",
    "declare": "_declare",
    "source": "_source",
    "inplace": "_inplace",
    "joinmsg": "_joinmsg",
    "card": "_card",
    "sticker": "_sticker",
    "campaign": "_campaign",
    "sessions": "_sessions",
    "use": "_use",
    "forget": "_forget",
    "login": "_login",
    "code": "_code",
    "password": "_password",
    "cancel": "_cancel",
}


def _col(row: Any, name: str, default: Any = None) -> Any:
    """Read one column, tolerating a row that does not carry it.

    A couple of these commands read columns added by later migrations, and a fake row in the
    tests answers with the shape the earlier commands needed. Reading ``None`` there is the
    right failure, because "not recorded" is also what it means in the database.
    """
    try:
        value = row[name]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _int_or_none(token: str | None) -> int | None:
    """A positive episode/season count, or None. Deliberately strict: ``"12 eps"`` is a
    human being casual, not a number to store, and a wrong season length is a public,
    permanent claim about how much of a show exists."""
    text = (token or "").strip()
    if not text.isdigit():
        return None
    return int(text)


class NeedsPassword(Exception):
    """The account has 2FA; the caller must ask for it and try again."""


class LoginCancelled(Exception):
    """The operator cancelled, or the attempt expired."""


class LoginUnstored(Exception):
    """Telegram accepted the credentials and the service could not hand over a session.

    Its own exception because the two login failures need opposite answers. A wrong code means *try the
    code again*; this means the account is signed in right now, there is no code left to retry, and the
    only useful thing to say is that a live session exists which nobody stored — so it should be
    terminated on the account before anyone tries again. Answering it with "2 attempt(s) left, reply with
    the code" sends the operator to do something that cannot work, over a login that already worked.
    """


@dataclass(frozen=True, slots=True)
class LoginResult:
    session_string: str
    account_id: int | None = None
    username: str | None = None


@dataclass(frozen=True, slots=True)
class Reply:
    """One outgoing message.

    ``delete_prompt_too`` marks the reply that answers a secret: the operator's
    own message — a phone number, a code, a password — is deleted once it has been
    used, because that is the copy that must not sit in a chat log.

    The reply itself is never deleted, and nothing here marks a message as
    "sensitive, delete mine too". A bot that erases its own instructions mid-flow is
    unreadable: the operator sees a question appear and vanish and cannot tell whether
    to answer it, and every line we send has already been scrubbed and masked, so there
    is nothing in it worth hiding.
    """

    text: str
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
    #: Called after a session is stored, so the account a login just produced reaches the writer
    #: without a redeploy. Injected (``app/main.py``) because the bot must not own that connection.
    on_session_stored: Callable[[], Any] | None = None
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
        method = _ROUTES.get(command)
        if method is None:
            # Unknown commands are ignored rather than echoed: an "unknown command"
            # reply is how a bot advertises that it exists and which words work.
            return []
        return await getattr(self, method)(update, args)

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
        # Stated before anything else about the queue, because almost every "nothing happened" in this
        # project has been this line: a container that cannot reach the database still answers the chat,
        # so a report that lists queues would look healthy while writing to any of them is impossible.
        if self.db is None:
            lines.append("database: this process has no connection at all")
        elif not getattr(self.db, "connected", False):
            why = getattr(self.db, "last_error", None)
            lines.append("database: not connected" + (f" ({str(why)[:120]})" if why else ""))
        else:
            lines.append("database: connected")
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
            # The noticeboard the operator runs beside every series channel. It is in /status
            # rather than only in the docs because the two halves of "can we announce" are easy to
            # satisfy one at a time — a channel named, a box unapproved — and a line that prints
            # both refuses to look ready when it is not.
            from .linkprovider import status_line as _updates_status  # noqa: PLC0415

            lines.append(
                _updates_status(
                    await self.db.config("updates.channel", ""),
                    await self.db.config("updates.per_episode", True),
                )
            )
            # The other queue the operator has to drain by hand, grouped by *why*: a
            # files-only channel parks four hundred candidates for one missing statement,
            # and "400 × cannot determine Hindi audio" is answered by one /source command,
            # while 400 separate rows would look like 400 separate problems.
            parked = await self.db.fetch(
                "select coalesce(reason, '(no reason recorded)') as why, count(*) as n"
                "  from app.source_candidate where disposition = 'pending'"
                " group by why order by count(*) desc limit 3"
            )
            if parked:
                lines.append("files parked, waiting on you:")
                for row in parked:
                    lines.append(f"  x{row['n']} — {str(row['why'])[:70]}")
                if any("Hindi audio" in str(row["why"]) for row in parked):
                    lines.append(
                        "  if this channel is a shelf of bare files: /source <@handle> audio hindi"
                    )
            # These decide what gets published at all, and the last one decides whether anybody
            # is contacted at all, so they belong on the one screen the operator reads.
            settings = await self.db.fetch(
                "select key, value::text as value from app.config "
                "where key in ('thumbnail.strict_mode','thumbnail.on_no_clean_candidate',"
                "'ingest.require_hindi_audio','ingest.include_subbed_only','caption.button_rows',"
                "'caption.total_episodes_unknown','joinrequest.message') "
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
                "probe started. It asks the bots their menu questions and sends the report here "
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


    _JOINMSG_USAGE = (
        "usage: /joinmsg [show|options|use <n>|set <text>|clear]\n"
        "  /joinmsg              what is saved now, and what that does and does not allow\n"
        "  /joinmsg options      three drafts to pick from, each with what it promises\n"
        "  /joinmsg use 2        save one of them as the message\n"
        "  /joinmsg set <text>   save your own words ({name} and {series} are filled in)\n"
        "  /joinmsg clear        empty it — the app may contact nobody\n\n"
        "Saving a message does not send one. A campaign is owner-triggered, per channel, and the\n"
        "sender that would carry it is not built yet (job kind join_request_campaign), so this\n"
        "command only ever changes what would be said. It never approves or declines a request."
    )

    async def _joinmsg(self, update: Update, args: list[str]) -> list[Reply]:
        """``/joinmsg [show|options|use <n>|set <text>|clear]`` — the join-request wording.

        The operator asked for options in the bot rather than a row to edit by hand, on 2026-08-29:
        *"jisse mujhe kabhi bhi kuch bhi bolna ho to mai bol paau sabhi se"*. So the setting lives
        here and the rules live in :mod:`app.joinmsg`, which refuses an invite link in a DM, refuses
        wording that reads like an approval, and refuses a placeholder it cannot fill.

        What this command pointedly does not do is send. The message is a *setting*; the act of
        contacting people is the blocked job kind, and the refusal is printed in the same reply as
        the confirmation, so "saved" can never be misread as "on its way".
        """
        from . import joinmsg  # noqa: PLC0415  (one-way import, like .inplace above)

        action = (args[0].strip().casefold() if args else "show")
        if action in {"help", "?"}:
            return [Reply(self._JOINMSG_USAGE)]

        if action in {"show", "options"}:
            current = await self.db.config(joinmsg.CONFIG_KEY, "") if self.db is not None else ""
            if action == "options":
                return [Reply(joinmsg.options_text(current))]
            note = joinmsg.status_note(current)
            body = " ".join(str(current or "").split())
            if not body:
                return [Reply(f"{note}\n\n{self._JOINMSG_USAGE}")]
            return [
                Reply(
                    f"{note}\n\nSaved text ({len(body)} chars):\n{body}\n\n"
                    "Placeholders filled at send time: " + ", ".join(joinmsg.PLACEHOLDERS)
                )
            ]

        if self.db is None or not getattr(self.db, "connected", False):
            return [Reply("the database is not reachable, so I cannot save a message.")]

        if action == "use":
            if len(args) < 2 or not args[1].strip().isdigit():
                return [Reply(f"`use` needs a number from the options list.\n{self._JOINMSG_USAGE}")]
            index = int(args[1].strip())
            if not 1 <= index <= len(joinmsg.PRESETS):
                return [
                    Reply(
                        f"there are {len(joinmsg.PRESETS)} options, not {index}. "
                        "Run /joinmsg options to see them."
                    )
                ]
            preset = joinmsg.PRESETS[index - 1]
            text, note = preset.text, f"\nWhat it promises: {preset.note}"
        elif action == "set":
            text = " ".join(args[1:]).strip()
            note = ""
            if not text:
                return [Reply(f"`set` needs the words themselves.\n{self._JOINMSG_USAGE}")]
        elif action == "clear":
            if len(args) > 1:
                return [Reply("`clear` takes no arguments — it is the one that stops everything.")]
            text, note = "", ""
        else:
            return [Reply(f"I do not know `/joinmsg {args[0]}`.\n{self._JOINMSG_USAGE}")]

        if text:
            problems = joinmsg.refusals(text)
            if problems:
                return [Reply("I will not save that:\n" + "\n\n".join(f"• {p}" for p in problems))]
        import json  # noqa: PLC0415  (only the writer needs the encoder)

        await self.db.execute(
            "insert into app.config (key, value, description) values ($1, $2::jsonb, $3)"
            " on conflict (key) do update set value = excluded.value, updated_at = now()",
            joinmsg.CONFIG_KEY,
            json.dumps(text),
            "Set from the control bot with /joinmsg on 2026-08-29; the wording and its rules are "
            "app/joinmsg.py, and an empty value means the app may contact no requester.",
        )
        if not text:
            return [Reply("cleared. nobody is contacted, and a campaign would have nothing to say.")]
        return [
            Reply(
                f"saved ({len(' '.join(text.split()))} chars). It goes to nobody yet: sending a "
                f"join-request campaign is still the blocked job kind join_request_campaign, "
                f"because there is no sender wired.{note}"
            )
        ]

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

    async def _declare(self, update: Update, args: list[str]) -> list[Reply]:
        """``/declare <series> <season> <count|tba>`` — the owner states a season's length.

        This command exists because of one line in the caption box: ``◎ Total Episodes``.
        The database column is the only honest source for that number, and nothing can
        learn it from a source channel — a show on a one-week break looks exactly like a
        finished one from the inside. So the number is *said*, here, and until it is said
        the caption prints the hedge and the season batch post does not fire.

        Refuses to guess which series is meant, in both directions: a substring that
        matches two rows gets listed instead of written, and a season with no episodes yet
        is created rather than dropped, because declaring "25" before episode 1 arrives is
        the useful direction for that number to travel in.
        """
        if self.db is None or not getattr(self.db, "connected", False):
            return [Reply("the database is not reachable, so I cannot record a season length.")]
        if len(args) < 2:
            return [
                Reply(
                    "usage: /declare <series> <season> <episodes>\n"
                    "  /declare dekin no mogura 1 12\n"
                    "  /declare dekin no mogura 2 tba   (back to not claiming a length)\n\n"
                    "Until a season is declared, the caption says TBA and the complete-season "
                    "batch post stays held — I will not infer a total from the highest episode number."
                )
            ]
        # Series titles contain spaces, so the two numbers are read from the *end*:
        # count last, season before it, everything else is the title. `/declare bleach 13`
        # is season 1 with thirteen episodes, because the alternative — refusing until the
        # operator learns a separator — is a command nobody sends correctly on a phone.
        count_token = args[-1]
        season_number, series_words = 1, args[:-1]
        if len(args) >= 3:
            trailing = _int_or_none(args[-2])
            if trailing is not None:
                season_number, series_words = trailing, args[:-2]
        series = " ".join(series_words).strip()
        if not series:
            return [Reply("which series? usage: /declare <series> <season> <episodes>")]
        if season_number is None or season_number < 0:
            return [Reply("the season has to be a number, e.g. /declare bleach 2 13")]
        from .keys import normalize_title

        slug = normalize_title(series.lstrip("@"))
        rows = await self.db.fetch(
            """
            select id, title, normalized_title from app.series
             where normalized_title = $1 or normalized_title like '%' || $2 || '%'
             order by id limit 6
            """,
            slug,
            slug,
        )
        if not rows:
            return [
                Reply(
                    f"no series stored that matches {series!r}. I only declare against a series "
                    "I have already filed episodes for; /status shows what exists."
                )
            ]
        if len(rows) > 1:
            names = "\n".join(f"  · {row['title']}" for row in rows)
            return [Reply(f"that matches more than one series, so I am not picking:\n{names}")]
        series_id = int(rows[0]["id"])
        clear = count_token.strip().lower() in {"tba", "none", "clear", "-"}
        count = None if clear else _int_or_none(count_token)
        if count is not None and count <= 0:
            count = None
            clear = True
        if count is None and not clear:
            return [Reply(f"{count_token!r} is not a number of episodes. Use /declare {series} {season_number} 12")]
        declared_first, declared_last = await self._declaration_bounds(series_id, season_number, count)
        await self.db.execute(
            """
            insert into app.season (series_id, season_number, first_episode, last_episode, declared_by)
            values ($1, $2, $3, $4, 'operator')
            on conflict (series_id, season_number) do update
               set first_episode = excluded.first_episode,
                   last_episode  = excluded.last_episode,
                   declared_by   = excluded.declared_by,
                   declared_at   = now(),
                   updated_at    = now()
            """,
            series_id,
            season_number,
            declared_first,
            declared_last,
        )
        title = rows[0]["title"]
        count_label = "tba" if clear else str(count)
        if clear:
            return [
                Reply(
                    f"{title} season {season_number}: length undeclared again. Captions print the "
                    "TBA line and no batch post goes out for it."
                )
            ]
        span = "" if clear else f" (season {season_number}, episodes {declared_first}-{declared_last})"
        return [
            Reply(
                f"{title} season {season_number}: declared {count_label} episodes{span}.\n\n"
                "◎ Total Episodes prints that from the next post, and the season now *counts as "
                "complete* once every episode in the span has a file behind it. This command "
                "publishes nothing by itself: turning that eligibility into the permanent batch "
                "post is the publisher's job, and the publish layer is still unwired, so the post "
                "will not appear silently ahead of the code that is meant to send it. If the source "
                f"later delivers episode {count + 1}, I will tell you rather than quietly rewriting "
                "your number."
            )
        ]

    async def _source(self, update: Update, args: list[str]) -> list[Reply]:
        """``/source <@handle|channel id> [series <name>] [audio <kind>] [season <n>]``.

        For the channels that are a shelf of files rather than a captioned source: each
        message says ``episode 7`` and nothing else, so the series, the language and the
        season cannot be read out of anything. The pipeline will not guess them — a guessed
        series names a 30k-member channel, and a guessed language publishes a subbed file as
        a Hindi one — so this command is where those three facts come from: stated once per
        channel instead of once per file.

        The reply also says what this command does *not* do. It re-decides nothing by itself:
        parked files are re-read on the next scan, decided files are never rewritten.
        """
        handle = (args[0].strip() if args else "")
        if not handle or handle.lower() in {"help", "?"}:
            # Usage first, and with no database required: someone asking how the command
            # works must not be answered with an infrastructure error.
            return [Reply(self._SOURCE_USAGE)]
        if self.db is None or not getattr(self.db, "connected", False):
            return [Reply("the database is not reachable, so I cannot record a channel declaration.")]

        keys = self._SOURCE_KEYS
        tokens = args[1:]
        wanted: dict[str, object] = {}
        index = 0
        while index < len(tokens):
            key = tokens[index].strip().casefold()
            if key == "clear":
                wanted = {name: None for name in keys}
                index = len(tokens)
                break
            if key not in keys:
                return [
                    Reply(
                        f"I only take {', '.join(keys)} (or clear) after the channel — not "
                        f"`{tokens[index]}`.\n{self._SOURCE_USAGE}"
                    )
                ]
            pieces: list[str] = []
            index += 1
            while index < len(tokens) and tokens[index].strip().casefold() not in keys:
                pieces.append(tokens[index])
                index += 1
            if not pieces:
                return [Reply(f"`{key}` needs a value.\n{self._SOURCE_USAGE}")]
            if key in wanted:
                return [Reply(f"you gave me `{key}` twice — which one did you mean?")]
            wanted[key] = " ".join(pieces).strip()

        rows = await self._find_source_channel(handle)
        if isinstance(rows, str):  # a message to send instead of a lookup result
            return [Reply(rows)]
        if len(rows) > 1:
            listed = "\n".join(
                f"  {row['id']}: @{row['username'] or '?'} — {row['title'] or 'no title'}" for row in rows
            )
            return [Reply(f"`{handle}` matches {len(rows)} channels, so I will not pick one:\n{listed}")]
        channel = rows[0]

        problems = await self._check_declarations(wanted)
        if problems:
            return [Reply(problems)]

        if wanted:
            columns = [name for name in keys if name in wanted]
            sets = ", ".join(f"declared_{name} = ${position + 2}" for position, name in enumerate(columns))
            await self.db.execute(
                f"update app.source_channel set {sets}, declared_by = 'operator', declared_at = now(),"
                " updated_at = now() where id = $1",
                channel["id"],
                *[wanted[name] for name in columns],
            )
        return [Reply(self._source_summary(channel, wanted))]

    _SOURCE_KEYS = ("series", "audio", "season")
    _SOURCE_USAGE = (
        "usage: /source <@handle or channel id> [series <name>] [audio <kind>] [season <n>]\n"
        "  /source @anime_uploads4u series Bleach audio hindi\n"
        "  /source @anime_uploads4u season 2      (a numbering default, never a season claim)\n"
        "  /source @anime_uploads4u               (show what is declared)\n"
        "  /source @anime_uploads4u clear         (stop assuming anything)\n\n"
        "audio is one of: hindi, dual, multi, subbed, subbed_only, unknown."
    )

    _INPLACE_USAGE = (
        "usage: /inplace <@handle or channel id> [from <@other>] [plan|off]\n"
        "  /inplace @naruto_hindi                  caption the files already in that channel\n"
        "  /inplace @naruto_hindi plan             show the plan, change nothing\n"
        "  /inplace @naruto_hindi from @uploads4u  also compare with that source, to fill gaps\n"
        "  /inplace @naruto_hindi off              back to the link route\n\n"
        "in-place means one thing only: the approved caption is written on the message that "
        "already holds the file, instead of onto a copy. nothing is deleted, and nothing is "
        "skipped — storage, the link and the post all still happen, and a destination channel is "
        "still created when it does not exist, because a channel of bare files is not one."
    )

    async def _inplace(self, update: Update, args: list[str]) -> list[Reply]:
        """``/inplace <channel> [from <other>] [plan|off]`` — caption the file messages in place.

        The other shape of this service, for the case the operator described: a channel whose
        messages already are the posts, each saying nothing but ``episode 7``. This command
        records the mode and shows the plan it implies. It never touches Telegram, and it says so:
        the edits themselves are the user session's job, and the publish layer is unwired.

        What this command deliberately does *not* offer is a way out of the rest of the job. The
        mode adds an act (the caption, on the message that exists) and removes none: the file is
        still handed to storage, a link still comes back, and a post is still made in the channel
        named from the series — which is built when missing, since bare files sitting in a channel
        never make it a destination.
        """
        from . import inplace  # noqa: PLC0415  (see the module's import policy: one-way, no cycle)

        handle = (args[0].strip() if args else "")
        if not handle or handle.lower() in {"help", "?"}:
            return [Reply(self._INPLACE_USAGE)]
        if self.db is None or not getattr(self.db, "connected", False):
            return [Reply("the database is not reachable, so I cannot record a publish mode.")]

        tokens = [token.strip() for token in args[1:]]
        other = ""
        if tokens and tokens[0].casefold() == "from":
            if len(tokens) < 2 or not tokens[1]:
                return [Reply(f"`from` needs the other channel's @handle or id.\n{self._INPLACE_USAGE}")]
            other = tokens[1]
            tokens = tokens[2:]
        if len(tokens) > 1:
            return [Reply(f"one of plan or off, not both.\n{self._INPLACE_USAGE}")]
        flag = tokens[0].casefold() if tokens else ""
        if flag not in {"", "plan", "off"}:
            return [Reply(f"I did not understand `{tokens[0]}`.\n{self._INPLACE_USAGE}")]

        rows = await self._find_source_channel(handle)
        if isinstance(rows, str):
            return [Reply(rows)]
        if len(rows) > 1:
            listed = "\n".join(
                f"  {row['id']}: @{_col(row, 'username', '?')} — {_col(row, 'title', 'no title')}" for row in rows
            )
            return [Reply(f"`{handle}` matches {len(rows)} channels, so I will not pick one:\n{listed}")]
        channel = rows[0]

        destination = await self.db.fetchrow(
            """
            select id, telegram_channel_id, title, series_id, publish_mode,
                   coalesce(paired_source_channel_id, -1) as paired_source_channel_id
              from app.destination
             where id = $1 or ($2::bigint is not null and telegram_channel_id = $2)
             order by id
             limit 1
            """,
            _col(channel, "destination_id", -1),
            _col(channel, "telegram_channel_id"),
        )
        if destination is not None and "id" not in destination:  # a fake row with no destination
            destination = None

        plan_rows = await self._inplace_rows(channel, destination)
        if flag == "off":
            # Turning the mode off is always allowed and never blocked by a rights check: it asks
            # for no write at all, and a refusal here would strand a channel in a mode the
            # operator has just decided against.
            return await self._inplace_off(channel, destination, handle)

        # "files already posted here" is about the media, not about the row count: a channel of
        # text posts is not an in-place destination no matter how many messages it has.
        files_already_there = any(bool(row.get("is_media")) for row in plan_rows)
        # Rights first, mode second. The operator's rule is that being able to caption in place
        # never replaces building a destination: if we are a member here, or have never read our
        # own rights, this channel is a source and the destination is created (or already exists)
        # — so the command says that and writes nothing, rather than switching a mode that no
        # publisher could honour.
        route = inplace.route_for(
            we_are_admin=_col(channel, "we_are_admin"),
            files_already_there=files_already_there,
            destination_exists=destination is not None,
            series=_col(channel, "declared_series") or _col(channel, "title"),
        )
        source_row = None
        shape = None
        if other:
            source_rows = await self._find_source_channel(other)
            if isinstance(source_rows, str):
                return [Reply(source_rows)]
            if len(source_rows) > 1:
                return [Reply(f"`{other}` matches {len(source_rows)} channels — name one exactly.")]
            source_row = source_rows[0]
            shape = await self._inplace_shape(plan_rows, source_row)

        if not route.may_caption:
            return [Reply(self._inplace_refusal(channel, route))]

        overwrite = str(await self.db.config("inplace.overwrite_notes", "ask") or "ask").casefold()
        allow_copy = bool(await self.db.config("inplace.copy_missing", True))
        if plan_rows:
            decisions = inplace.plan(
                plan_rows,
                shape=shape,
                allow_copy=allow_copy,
                replace_notes=overwrite == "replace",
                destination_id=destination["id"] if destination else None,
            )
            preview = self._inplace_text(channel, decisions, shape, overwrite=overwrite, route=route)
        else:
            # Rights are fine and there is simply nothing read yet. The mode is a setting rather
            # than a plan, so recording it is right — inventing a count is not. A channel created
            # before its first scan is the normal case, and this is the reply that says what will
            # happen to it instead of refusing for lack of a number.
            preview = (
                "nothing from this channel has been read into the database yet, so there is no plan "
                "to show. the mode is still the right thing to record: every file the next scan finds "
                "gets its caption as it is filed, and /inplace "
                f"{handle} plan will count them then."
            )

        if flag == "plan":
            return [Reply(preview + "\n\n(plan only — nothing was changed.)")]

        await self._inplace_apply(channel, destination, source_row)
        note = ""
        if not destination:
            note = (
                "\n\nthis channel has no row in app.destination yet, so the mode is recorded on the "
                "channel itself; the destination row inherits it when it is created or linked, and "
                "until then nothing here is published either way."
            )
        return [Reply(f"in-place captioning is ON for {handle}.\n\n{preview}{note}")]

    async def _inplace_apply(self, channel: Any, destination: Any, source_row: Any) -> None:
        """Record the mode, on the channel and — when there is one — on the destination row.

        Two writes rather than one because the two rows answer different readers: the pipeline
        asks its destination what it does, and a channel with no destination row yet still has to
        know how its own files are treated. Pairing is recorded on the destination side, since a
        source is only "paired" from the point of view of the channel it feeds.
        """
        await self.db.execute(
            "update app.source_channel set publish_role = $2, updated_at = now() where id = $1",
            channel["id"],
            "destination" if source_row is not None else "source_and_destination",
        )
        if destination:
            await self.db.execute(
                "update app.destination set publish_mode = 'in_place_caption', updated_at = now() "
                "where id = $1",
                destination["id"],
            )
        if source_row is not None:
            await self.db.execute(
                "update app.source_channel set publish_role = 'source', updated_at = now() where id = $1",
                source_row["id"],
            )
            if destination:
                await self.db.execute(
                    "update app.destination set paired_source_channel_id = $2, updated_at = now() "
                    "where id = $1",
                    destination["id"],
                    source_row["id"],
                )

    async def _inplace_off(self, channel: Any, destination: Any, handle: str) -> list[Reply]:
        """Back to the link route, leaving every already-edited post exactly as it is."""
        await self.db.execute(
            "update app.source_channel set publish_role = 'source', updated_at = now() where id = $1",
            channel["id"],
        )
        if destination:
            await self.db.execute(
                "update app.destination set publish_mode = 'link_post', updated_at = now() where id = $1",
                destination["id"],
            )
        return [
            Reply(
                f"link route again for {handle}: nothing will be written onto the posts that are "
                "already there. I did not change any message either — an episode that already "
                "carries our caption keeps it, and /inplace plan lists those."
            )
        ]

    async def _inplace_rows(self, channel: Any, destination: Any) -> list:
        """The channel's own file messages, in the shape ``app.inplace.plan`` reads.

        One query per channel, not per episode: a 400-episode backlog has to be planable in a
        single round trip, because the plan is what the operator sees before any edit is tried.
        Every disposition is included — in this mode a file parked for want of an audio claim
        still has a caption missing, and that is exactly what the mode is for.
        """
        import json

        rows = await self.db.fetch(
            """
            select c.message_id,
                   c.episode_number as episode,
                   c.raw_caption    as caption,
                   (c.media_type is not null or c.file_name is not null) as is_media,
                   c.parsed ->> 'audio_kind' as audio_kind,
                   c.parsed -> 'languages'   as languages,
                   coalesce(nullif(sc.declared_series, ''), nullif(sr.title, ''), sc.title) as title,
                   sr.subtitle as subtitle,
                   c.season_number as season,
                   s.first_episode as declared_first,
                   s.last_episode  as declared_episodes,
                   s.observed_first,
                   s.observed_last,
                   dp.caption_previous
              from app.source_candidate c
              join app.source_channel sc on sc.id = c.source_channel_id
              left join app.series sr on sr.id = coalesce($2::bigint, sc.series_id)
              left join app.season s on s.series_id = sr.id and s.season_number = coalesce(c.season_number, 1)
              left join app.destination_post dp
                     on dp.destination_id = $3 and dp.message_id = c.message_id and dp.kind = 'episode'
             where c.source_channel_id = $1
             order by c.episode_number nulls last, c.message_id
            """,
            channel["id"],
            destination["series_id"] if destination else None,
            destination["id"] if destination else None,
        )
        unknown = await self.db.config("caption.total_episodes_unknown", "TBA")
        out = []
        for row in rows:
            item = dict(row)
            languages = item.pop("languages", None)
            if isinstance(languages, str):
                try:
                    languages = json.loads(languages)
                except ValueError:
                    languages = None
            item["languages"] = [str(value) for value in (languages or []) if str(value).strip()]
            item["unknown_label"] = unknown
            out.append(item)
        return out

    async def _inplace_shape(self, plan_rows: list, source_channel: Any) -> Any:
        """Both channels' episode numbers, as :func:`app.inplace.compare` reads them."""
        from . import inplace

        theirs = await self.db.fetch(
            """
            select distinct episode_number
              from app.source_candidate
             where source_channel_id = $1 and episode_number is not null
             order by episode_number
            """,
            source_channel["id"],
        )
        return inplace.compare(
            [row.get("episode") for row in plan_rows],
            [row["episode_number"] for row in theirs],
        )

    def _inplace_refusal(self, channel: Any, route: Any) -> str:
        """"I cannot write here" is not the same as "nothing will happen".

        A refusal that stops at "no rights" is how a season ends up stranded: the operator reads
        it as a dead end and goes looking for a way to grant access, when the actual answer is
        that a source channel needs no access at all and a destination needs building. So the
        reply names the thing that will happen, the name it will use, and the one command that
        makes the naming safe.
        """
        name = route.name or "the destination for this series"
        lines = ["I did not switch this channel to in-place mode.", ""]
        lines.append(f"  {route.reason}")
        lines.append(f"  {route.consequence()}")
        lines.append("")
        if route.create_destination:
            lines.append(
                f"what happens instead: this channel stays a source, and the destination "
                f"`{name}` is created — private, with the profile set before anyone is involved, "
                "@chelpbot added as admin, the one-use invite sent to you, then revoked. that step "
                "is not skipped because an in-place mode exists, and it is not asked for "
                "permission first: you already said a channel per finished series is automatic."
            )
            lines.append("")
            lines.append(
                "to get there, the series has to be named from two agreeing signals, so state it "
                "once: /source <this channel> series <name> audio hindi"
            )
        else:
            lines.append(
                "what happens instead: the destination already exists for this series, so posts "
                "go there through Channel Help, and this channel only feeds it files. nothing to "
                "create, nothing to caption here."
            )
        if not route.rights_verified:
            lines.append("")
            lines.append(
                "(this session has never read its rights in this channel, so I am taking the narrow "
                "answer. /probe reads them out of the dialog list and records them in "
                "app.source_channel.we_are_admin — run it and this line goes away by itself. if this "
                "really is your own channel and you want it asserted before then, set that column to "
                "true in the dashboard and run this again: I would rather look than guess, and rather "
                "be told than be wrong.)"
            )
        return "\n".join(lines)

    def _inplace_text(self, channel: Any, decisions: list, shape: Any, *, overwrite: str, route: Any = None) -> str:
        """The plan in the operator's words, with the questions on top instead of buried."""
        from . import inplace

        lines = [
            f"what I would do with the {len(decisions)} messages of this channel:",
            "  " + inplace.summary(decisions),
            "  " + inplace.shape_note(shape),
        ]
        if route is not None:
            lines.append(f"  mode: {route.mode}, destination created: {route.create_destination}")
        asks = [decision for decision in decisions if decision.action == inplace.Action.ASK]
        if asks:
            lines.append("")
            lines.append(f"{len(asks)} need you before I touch them:")
            for decision in asks[:3]:
                where = f"msg {decision.message_id}" if decision.message_id else "a file with no number on it"
                lines.append(f"  {where}: {decision.reason}")
            if len(asks) > 3:
                lines.append(f"  … and {len(asks) - 3} more like that")
        overwritten = [
            decision for decision in decisions if "overwrite_notes" in (decision.reason or "")
        ]
        if overwritten:
            lines.append("")
            lines.append(
                f"{len(overwritten)} of these carry a note rather than a label, and "
                'inplace.overwrite_notes = "replace" is what wrote over it. the text it replaced '
                "is in app.destination_post.caption_previous, which is the only copy Telegram "
                "does not have."
            )
        elif any("note" in (decision.reason or "") for decision in asks):
            lines.append("")
            lines.append(
                "(if every one of those notes is your own text and you want it gone, set "
                '"inplace.overwrite_notes" to "replace" in app.config — the old caption is kept '
                "either way.)"
            )
        keys = [
            str(decision.details.get("dedup_key"))
            for decision in decisions
            if decision.details.get("dedup_key")
        ]
        if keys:
            lines.append("")
            lines.append(
                f"each edit is its own job, keyed once per message ({keys[0]}): running this twice "
                "on the same channel cannot edit the same post twice, and a restart in the middle "
                "resumes at the first message that still needs it."
            )
        lines.append("")
        lines.append(
            "no new channel, no copy, no deletion, and no buttons under the post: a user session "
            "cannot attach a keyboard to a video, and there is no link to put in one here, so the "
            "caption has to stand on its own. it does — the approved box, powered-by line included."
        )
        lines.append(
            "this command changed the plan, not the channel. the edits go out through the user "
            "session, and while the publish layer is unwired that is the part still missing."
        )
        return "\n".join(lines)

    # --- the three things a write job cannot do without ------------------------------------------
    #
    # `/card`, `/sticker` and `/campaign` exist because `app/writers.py` refuses to guess the same
    # three facts: which post a shareable link was made for, which message carries a season's sticker,
    # and when a campaign of DMs to strangers is allowed to run. Each command writes a row, shows what
    # it wrote, and never sends anything itself.

    _CARD_USAGE = (
        "usage: /card <@handle, channel id or title> <message id>\n"
        "  /card -1001234567890 42      the card post in that destination, message 42\n"
        "  /card @bleach_hindi 42       same, by the channel's own handle\n"
        "  /card @bleach_hindi show     what is recorded, and whether a link was ever returned\n"
        "  /card @bleach_hindi clear    stop announcing that channel\n"
        "  /card                        every destination and its card state\n\n"
        "the card is the post we forward to the link bot to get a shareable link; the announcements "
        "channel carries that link and never the invite itself. The message id is a number inside "
        "that channel — copy it from Copy Link, where it is the digits after /."
    )

    async def _find_destination(self, handle: str) -> list | str:
        """Look a destination channel up by id, title, or the handle of the source paired with it.

        `app.destination` stores no username of its own, so a `@handle` is matched through the source
        channel that publishes into it — which is how the operator names these channels anyway.
        """
        stripped = handle.lstrip("@")
        numeric = int(stripped) if stripped.lstrip("-").isdigit() and stripped != "-" else None
        rows = await self.db.fetch(
            """
            select d.id, d.title, d.telegram_channel_id, d.publish_mode, d.card_message_id,
                   d.announcement_link, d.announcement_link_at, sr.title as series
              from app.destination d
              join app.series sr on sr.id = d.series_id
              left join app.source_channel sc on sc.destination_id = d.id
             where ($1::bigint is not null and d.telegram_channel_id = $1)
                or ($2::text is not null and lower(btrim(coalesce(d.title, ''))) = lower($2))
                or ($2::text is not null and lower(btrim(coalesce(sc.username, ''), '@')) = lower($2))
             order by d.id
            """,
            numeric,
            stripped,
        )
        if not rows:
            return (
                f"`{handle}` matches no destination channel, by its own id or title or the source "
                "channel that publishes into it. /destinations lists what exists; a destination row "
                "is written by the ingest side when a series first arrives."
            )
        return list(rows)

    @staticmethod
    def _card_line(row: dict) -> str:
        card = row.get("card_message_id")
        link = row.get("announcement_link")
        state = f"card message {card}" if card else "no card message named"
        got = f", link recorded {link}" if link else ", no link recorded yet"
        return f"• {row.get('title') or row.get('series')} ({row.get('telegram_channel_id')}): {state}{got}"

    async def _card(self, update: Update, args: list[str]) -> list[Reply]:
        """``/card <channel> <message id|show|clear>`` — the post the shareable link is made from.

        One number, per destination, chosen by the operator. Everything about the announcement
        follows from it, and nothing here asks the link bot a second time in the same run: the bot
        answers once, to the forward, so the link that comes back is stored (`app.destination
        .announcement_link`) and the job that could not reach it blocks instead of posting the invite.
        """
        if self.db is None or not getattr(self.db, "connected", False):
            return [Reply("the database is not reachable, so I cannot record a card post.")]
        if not args:
            rows = await self.db.fetch(
                "select d.id, d.title, d.telegram_channel_id, d.card_message_id, d.announcement_link,"
                " d.announcement_link_at, null::text as series from app.destination d order by d.id limit 25"
            )
            if not rows:
                return [Reply("no destination channels exist yet, so there is nothing to name.")]
            return [Reply("\n".join(self._card_line(row) for row in rows))]
        if args[0].strip().casefold() in {"help", "-h", "?"}:
            return [Reply(self._CARD_USAGE)]
        handle, rest = args[0].strip(), [a.strip() for a in args[1:]]
        found = await self._find_destination(handle)
        if isinstance(found, str):
            return [Reply(f"{found}\n\n{self._CARD_USAGE}")]
        if len(found) > 1:
            names = ", ".join(str(row.get("title") or row.get("id")) for row in found)
            return [Reply(f"`{handle}` matches {len(found)} destinations ({names}). Use the channel id.")]
        row = found[0]
        action = (rest[0] if rest else "show").casefold()
        if action == "show" or not rest:
            return [Reply(self._card_line(row))]
        if action == "clear":
            await self.db.execute(
                "update app.destination set card_message_id = null, announcement_link = null,"
                " announcement_link_at = now() where id = $1",
                int(row["id"]),
            )
            return [Reply(
                "card message cleared for " + str(row.get("title") or row["id"])
                + ". The stored link is left in place on purpose: deleting is not this program's "
                "verb, and a link that exists can still be announced until you say otherwise."
            )]
        if not action.isdigit():
            return [Reply(f"`{rest[0]}` is not a message id.\n\n{self._CARD_USAGE}")]
        await self.db.execute(
            "update app.destination set card_message_id = $2 where id = $1",
            int(row["id"]),
            int(action),
        )
        return [Reply(
            f"card post for {row.get('title') or row['id']} is now message {action}.\n\n"
            "the next publish for that channel asks @Link_providerobot for a shareable link to it, "
            "in shadow mode by planning the ask. Nothing was sent just now — this recorded a number."
        )]

    _STICKER_USAGE = (
        "usage: /sticker <series> <season> from <@handle or channel id> <message id>\n"
        "  /sticker Bleach 2 from @anime_uploads4u 8812\n"
        "  /sticker Bleach 2 show          what is recorded\n"
        "  /sticker Bleach 2 clear         no sticker for that season\n\n"
        "Telegram addresses a sticker by the message that carries it, so a pack name or an install "
        "link is not enough: name one message in one channel and the season sticker is forwarded from "
        "it, before that season's first episode post."
    )

    async def _sticker(self, update: Update, args: list[str]) -> list[Reply]:
        """``/sticker <series> <season> from <peer> <message id>`` — which sticker opens a season.

        The mapping this program will not do for you: which of a pack's stickers means "season 2
        starts here". The command stores the address and the writer forwards the message; the pack
        url from `/sticker-pack` stays what the post *links to*, which is a different thing.
        """
        from . import keys  # noqa: PLC0415  (the dedup key for the job this queues)
        from .stages import JobKind  # noqa: PLC0415

        if self.db is None or not getattr(self.db, "connected", False):
            return [Reply("the database is not reachable, so I cannot record a sticker source.")]
        text = [a.strip() for a in args if a.strip()]
        if not text or text[0].casefold() in {"help", "-h", "?"}:
            return [Reply(self._STICKER_USAGE)]
        lowered = [a.casefold() for a in text]
        try:
            tail = lowered.index("from")
        except ValueError:
            tail = -1
        if tail < 1 or tail + 2 >= len(text):
            if lowered[-1] in {"show", "clear"} and len(text) >= 2:
                head, action = text[:-1], lowered[-1]
                peers = None
            else:
                return [Reply(f"that is not a sticker address.\n\n{self._STICKER_USAGE}")]
        else:
            head, action, peers = text[:tail], "set", text[tail + 1 : tail + 3]

        *series_words, season_token = head
        series = " ".join(str(word) for word in series_words).strip()
        if not series or not season_token.isdigit():
            return [Reply(f"need a series name and a season number.\n\n{self._STICKER_USAGE}")]
        season_number = int(season_token)
        row = await self.db.fetchrow(
            "select s.id as season_id, s.season_number, s.sticker_source_chat_id, s.sticker_source_message_id,"
            " s.sticker_posted, sr.title, sr.id as series_id, d.id as destination_id"
            " from app.season s join app.series sr on sr.id = s.series_id"
            " left join app.destination d on d.series_id = sr.id"
            " where lower(sr.title) = lower($1) and s.season_number = $2 order by s.id limit 1",
            series,
            season_number,
        )
        if row is None:
            return [Reply(
                f"no season {season_number} for a series called {series!r} yet. Seasons come from the "
                "files that arrive (or from /declare); I do not create a season to hold a sticker."
            )]
        if action == "show":
            state = (
                f"source {row['sticker_source_chat_id']}#{row['sticker_source_message_id']}"
                if row.get("sticker_source_message_id")
                else "no source message named"
            )
            posted = " already posted" if row.get("sticker_posted") else ""
            return [Reply(f"{row['title']} S{season_number}: {state}{posted}")]
        if action == "clear":
            await self.db.execute(
                "update app.season set sticker_source_chat_id = null, sticker_source_message_id = null,"
                " updated_at = now() where id = $1",
                int(row["season_id"]),
            )
            return [Reply(f"sticker source cleared for {row['title']} S{season_number}.")]
        peer_text, message_id = peers
        numeric = peer_text.lstrip("@")
        if not numeric.lstrip("-").isdigit():
            found = await self.db.fetchrow(
                "select telegram_channel_id from app.source_channel"
                " where lower(btrim(coalesce(username, ''), '@')) = lower($1) order by id limit 1",
                numeric,
            )
            if found is None:
                return [Reply(
                    f"I do not know a channel called {peer_text!r}, and a sticker must be forwarded "
                    "from a channel this program already reads or publishes to. Give me the numeric id "
                    "if the handle is not one of those."
                )]
            chat_id = int(found["telegram_channel_id"])
        else:
            chat_id = int(numeric)
        await self.db.execute(
            "update app.season set sticker_source_chat_id = $2, sticker_source_message_id = $3,"
            " updated_at = now() where id = $1",
            int(row["season_id"]),
            chat_id,
            int(message_id),
        )
        queued = None
        if row.get("destination_id") is not None:
            queued = await self.db.enqueue(
                JobKind.SEASON_STICKER.value,
                keys.sticker_key(int(row["season_id"])),
                payload={"season_id": int(row["season_id"]), "destination_id": int(row["destination_id"])},
                season_id=int(row["season_id"]),
                destination_id=int(row["destination_id"]),
            )
        return [Reply(
            f"{row['title']} S{season_number} will open with the sticker in {chat_id}#{message_id}."
            + (
                "\n\nthe sticker job is queued; in shadow mode it plans the forward and blocks with "
                "the plan, which is the point of the run."
                if queued
                else "\n\nnothing is queued: that series has no destination channel row to post into."
            )
        )]

    _CAMPAIGN_USAGE = (
        "usage: /campaign <channel> [new <name> | text <name> <words…> | plan <name> |"
        " confirm <name> <code> | pause <name> | abort <name>]\n"
        "  /campaign @bleach_hindi new wave1      draft it from the saved /joinmsg wording\n"
        "  /campaign @bleach_hindi plan wave1       who would be contacted, and the code\n"
        "  /campaign @bleach_hindi confirm wave1 4F2A   let it run\n\n"
        "a campaign messages people whose join request is still pending, one message each, at "
        "campaign.rate_per_hour at most. It is the only job kind here that contacts a stranger, so it "
        "takes two deliberate steps and an unreadable-by-accident code; aborting leaves every row in "
        "place, and a contact is never contacted twice."
    )

    async def _campaign(self, update: Update, args: list[str]) -> list[Reply]:
        """``/campaign`` — draft, plan, confirm. Two human steps before any DM goes out.

        The plan is computed from the same two sources the job will use — `app.sender`'s read of the
        channel's pending requests and `app.joinmsg`'s refusal rules — so what the operator reads is
        not a promise about the run but the first half of it. The read is real even in shadow mode,
        because reading a list is not a write; sending still needs `confirm`, and the sending itself
        plans and blocks until the deployment is live.
        """
        from . import joinmsg, keys  # noqa: PLC0415
        from .stages import JobKind, JobStage  # noqa: PLC0415

        if self.db is None or not getattr(self.db, "connected", False):
            return [Reply("the database is not reachable, so I cannot draft a campaign.")]
        text = [a.strip() for a in args if a.strip()]
        if not text or text[0].casefold() in {"help", "-h", "?"}:
            return [Reply(self._CAMPAIGN_USAGE)]
        handle, rest = text[0], text[1:]
        found = await self._find_destination(handle)
        if isinstance(found, str):
            return [Reply(f"{found}\n\n{self._CAMPAIGN_USAGE}")]
        if len(found) > 1:
            return [Reply("that handle matches more than one destination; use the channel id.")]
        destination = found[0]
        action = (rest[0] if rest else "list").casefold()

        async def _row(name: str) -> dict | None:
            return await self.db.fetchrow(
                "select id, name, status, message_template, rate_per_hour, confirm_required"
                " from app.join_campaign where destination_id = $1 and lower(name) = lower($2)",
                int(destination["id"]),
                name,
            )

        if action == "list":
            rows = await self.db.fetch(
                "select c.name, c.status, c.rate_per_hour,"
                " (select count(*) from app.join_campaign_contact k where k.campaign_id = c.id"
                "   and k.status = 'sent') as sent"
                " from app.join_campaign c where c.destination_id = $1 order by c.id",
                int(destination["id"]),
            )
            if not rows:
                return [Reply(f"no campaigns for {destination.get('title') or destination['id']} yet.")]
            lines = [
                f"• {row['name']}: {row['status']}, {row['sent']} sent, {row['rate_per_hour']}/hour"
                for row in rows
            ]
            return [Reply("\n".join(lines))]

        if action in {"new", "text"}:
            if len(rest) < 2:
                return [Reply(f"`{action}` needs a name.\n\n{self._CAMPAIGN_USAGE}")]
            name = rest[1]
            body = " ".join(rest[2:]).strip() if action == "text" else ""
            if action == "new":
                body = str(await self.db.config(joinmsg.CONFIG_KEY, "") or "").strip()
                if not body:
                    return [Reply(
                        "there is no saved wording to draft from — /joinmsg options, then /joinmsg use "
                        "<n> or /joinmsg set <text>. A campaign with no text is not an empty campaign, "
                        "it is no campaign."
                    )]
            problems = joinmsg.refusals(body)
            if problems:
                return [Reply("I will not save that:\n" + "\n\n".join(f"• {p}" for p in problems))]
            existing = await _row(name)
            if existing is not None and action == "new":
                return [Reply(
                    f"`{name}` already exists ({existing['status']}). Use `text {name} <words>` to "
                    "rewrite it, or pick another name — a campaign is not overwritten by accident."
                )]
            if existing is not None:
                await self.db.execute(
                    "update app.join_campaign set message_template = $2, updated_at = now()"
                    " where id = $1",
                    int(existing["id"]),
                    body,
                )
                campaign_id = int(existing["id"])
            else:
                inserted = await self.db.fetchrow(
                    "insert into app.join_campaign (destination_id, name, message_template, status)"
                    " values ($1, $2, $3, 'draft') returning id",
                    int(destination["id"]),
                    name,
                    body,
                )
                campaign_id = int(inserted["id"])
            return [Reply(
                f"campaign `{name}` saved as a draft (id {campaign_id}), {len(body)} chars.\n\n"
                f"plan it: /campaign {handle} plan {name}"
            )]

        if action in {"plan", "confirm", "pause", "abort"}:
            if len(rest) < 2:
                return [Reply(f"`{action}` needs a name.\n\n{self._CAMPAIGN_USAGE}")]
            name = rest[1]
            campaign = await _row(name)
            if campaign is None:
                return [Reply(f"no campaign called `{name}` for that channel. /campaign {handle} list")]

            if action == "plan":
                pending = ""
                if getattr(self.settings, "outbound_enabled", False) and self.telegram is not None:
                    from . import sender  # noqa: PLC0415

                    client = getattr(self.telegram, "client", None)
                    if client is not None:
                        reader = sender.Sender(
                            client,
                            db=None,
                            policy=sender.WritePolicy(mode="plan", allow_peers=()),
                        )
                        ok, requests = await reader.pending_requests(
                            str(destination.get("telegram_channel_id") or ""), limit=100
                        )
                        waiting = [row for row in requests if not row.get("approved_by")]
                        pending = (
                            f"\n\n{len(waiting)} request(s) are pending right now"
                            + ("" if ok.ok else f" (the read said: {ok.detail})")
                        )
                else:
                    pending = (
                        "\n\nI cannot count the pending requests without a live session; the job reads "
                        "them again when it runs, so this plan shows the rules and not the headcount."
                    )
                code = joinmsg.confirm_code(campaign["id"], campaign["message_template"])
                return [Reply(
                    f"campaign `{name}` ({campaign['status']}) on "
                    f"{destination.get('title') or destination['id']}:\n\n"
                    f"• text: {campaign['message_template']}\n"
                    f"• at most {campaign['rate_per_hour']} people per hour, one message each\n"
                    f"• nobody is contacted twice, and a message never approves or declines the request"
                    f"{pending}\n\n"
                    f"to run it: /campaign {handle} confirm {name} {code}\n"
                    f"the code is that campaign and that exact text; change the wording and it changes."
                )]

            if action == "confirm":
                if len(rest) < 3:
                    return [Reply(f"`confirm` needs the code `plan` printed.\n\n{self._CAMPAIGN_USAGE}")]
                wanted = joinmsg.confirm_code(campaign["id"], campaign["message_template"])
                if rest[2].strip().upper() != wanted:
                    return [Reply(
                        f"that is not the code for `{name}`. Run /campaign {handle} plan {name} — and "
                        "no, I will not tell you the code here: the point of typing it is that you read "
                        "the plan first."
                    )]
                problems = joinmsg.refusals(campaign["message_template"])
                if problems:
                    return [Reply("that text breaks a rule, so it will not be sent:\n" + "\n".join(f"• {p}" for p in problems))]
                await self.db.execute(
                    "update app.join_campaign set status = 'ready', updated_at = now() where id = $1",
                    int(campaign["id"]),
                )
                queued = await self.db.enqueue(
                    JobKind.JOIN_REQUEST_CAMPAIGN.value,
                    keys.campaign_key(int(destination["id"]), str(campaign["name"])),
                    stage=JobStage.DISCOVERED,
                    payload={"campaign_id": int(campaign["id"]), "destination_id": int(destination["id"])},
                    destination_id=int(destination["id"]),
                )
                return [Reply(
                    f"`{name}` is ready and the job is {'queued' if queued else 'already queued'}. "
                    + (
                        "In shadow mode each message plans and blocks, which is the read-only version "
                        "of this run."
                        if not getattr(self.settings, "outbound_enabled", False)
                        else "It will send at the campaign's own rate and stop at the ceiling."
                    )
                )]

            status = {"pause": "paused", "abort": "aborted"}[action]
            # Two parameters, not one used twice. `set status = $2 ... case when $2 = 'aborted'` asks
            # Postgres to make $2 the enum type and a text value at the same time, and it refuses rather
            # than guessing: the statement never parses, so /campaign pause was dead on arrival. The
            # explicit cast names the type on the way in, and the flag is a plain boolean.
            await self.db.execute(
                "update app.join_campaign set status = $2::app.campaign_status, updated_at = now(),"
                " finished_at = case when $3 then now() else finished_at end where id = $1",
                int(campaign["id"]),
                status,
                action == "abort",
            )
            return [Reply(
                f"`{name}` is {status}. Contacts already sent stay sent — this program does not "
                "un-send a message to a stranger, and it does not delete the record of one."
            )]

        return [Reply(f"I do not know `/campaign {handle} {rest[0]}`.\n\n{self._CAMPAIGN_USAGE}")]



    async def _find_source_channel(self, handle: str) -> list | str:
        """Look a source channel up by @handle or numeric Telegram id.

        Both, because the operator reads handles in Telegram and the database keys on the
        numeric id, and the one case where a numeric-looking handle is actually a username
        (`@1000hours`) is handled by matching the text form too. A stored ``@`` is trimmed on
        our side as well: these rows are also edited by hand in the dashboard, and a row saved
        as ``@some_channel`` must still answer to ``/source @some_channel``.
        """
        stripped = handle.lstrip("@")
        # Negative on purpose: every Telegram channel id the operator will copy out of a
        # t.me link or a dashboard row is -100xxxxxxxxxx, and `str.isdigit()` calls that a
        # string. The lookup matches the text form too, so `@1000hours` is still found.
        numeric = int(stripped) if stripped.lstrip("-").isdigit() and stripped != "-" else None
        rows = await self.db.fetch(
            """
            select id, username, title, telegram_channel_id, series_id, destination_id,
                   we_are_admin, publish_role,
                   coalesce(declared_series, '') as declared_series,
                   coalesce(declared_audio, '') as declared_audio,
                   coalesce(declared_season, -1) as declared_season
              from app.source_channel
             where ($1::text is not null and (lower(btrim(coalesce(username, ''), '@')) = lower($1)
                                              or lower(coalesce(title, '')) = lower($1)))
                or ($2::bigint is not null and telegram_channel_id = $2)
             order by id
            """,
            stripped,
            numeric,
        )
        if not rows:
            return (
                f"`{handle}` is not a configured source channel, so there is nothing to declare "
                "about it.\nThe row itself is created in the dashboard table app.source_channel — "
                "I can read and update it, not create it."
            )
        return list(rows)

    async def _check_declarations(self, wanted: dict[str, object]) -> str | None:
        """Validate before writing. A half-recorded declaration is worse than none."""
        from .normalize import DECLARED_AUDIO, declared_audio_kind
        from .seasons import MAX_PLAUSIBLE_SEASON

        if "series" in wanted and wanted["series"] is not None:
            text = str(wanted["series"]).strip()
            if not text:
                return "an empty series name would put us back to guessing from the channel title."
            wanted["series"] = text
        if "audio" in wanted and wanted["audio"] is not None:
            try:
                declared_audio_kind(str(wanted["audio"]))
            except ValueError as error:
                return f"I cannot record that audio value. {error}"
            wanted["audio"] = str(wanted["audio"]).strip().casefold().replace("-", "_")
        if "season" in wanted and wanted["season"] is not None:
            value = _int_or_none(str(wanted["season"]))
            if value is None or value < 0 or value > MAX_PLAUSIBLE_SEASON:
                return (
                    f"the season default has to be a number between 0 and {MAX_PLAUSIBLE_SEASON}. "
                    "It is a numbering default only — it can never open a season; /declare is the "
                    "command that states things about seasons."
                )
            wanted["season"] = value
        if "audio" in wanted and wanted["audio"] is not None and str(wanted["audio"]) not in DECLARED_AUDIO:
            return f"audio must be one of: {', '.join(sorted(DECLARED_AUDIO))}"
        return None

    def _source_summary(self, channel, wanted: dict[str, object]) -> str:
        """What is declared now, and — the part that matters — what it does and does not unlock."""
        current = {
            "series": channel["declared_series"] or None,
            "audio": channel["declared_audio"] or None,
            # -1 is the sentinel for NULL, because a row of a fake db has no type info
            "season": None if int(channel["declared_season"]) < 0 else int(channel["declared_season"]),
        }
        for name in self._SOURCE_KEYS:
            if name in wanted:
                current[name] = wanted[name]
        label = channel["title"] or f"@{channel['username'] or channel['telegram_channel_id']}"
        lines = [f"{label}: {'declarations updated' if wanted else 'as they stand'}"]
        for name in self._SOURCE_KEYS:
            value = current[name]
            lines.append(f"  {name}: {value if value not in (None, '') else 'not declared'}")
        lines.append("")
        if current["audio"]:
            lines.append(
                "bare files here count as carrying that audio, and the caption's Audio line prints it "
                "as *your* statement — recorded as audio_source = channel_declaration, so a month from "
                "now you can tell 'the file said Hindi' from 'you told me to assume it'. A file whose "
                "own text says otherwise keeps its own wording, and a subbed one is still rejected."
            )
        else:
            lines.append(
                'with no audio declared, a file whose text says nothing about language parks as "cannot '
                "determine whether the file carries Hindi audio\u201d. That is deliberate: it waits for you "
                "rather than choosing a scope for you."
            )
        lines.append("")
        if current["series"]:
            lines.append("the series name is yours, so a destination channel may be named from it.")
        else:
            lines.append(
                "with no series declared, this channel's own title is one signal where the spec wants two: "
                "files will archive, but I will ask before naming a destination after a channel name."
            )
        if wanted:
            lines.append(
                "\nThis command re-decides nothing by itself: files still parked are re-read on the next "
                "scan of this channel, and files already decided are left alone."
            )
        return "\n".join(lines)

    async def _declaration_bounds(
        self, series_id: int, season_number: int, count: int | None
    ) -> tuple[int | None, int | None]:
        """Translate "twelve episodes" into the span the schema declares.

        A season numbered 3..14 (a cour split, or a source that starts at 0) means the
        count has to be added to whatever the season *starts* at, not to 1 — so the start
        is read from the season row when one exists. If nothing has been filed yet, the
        ordinary case holds and the season starts at 1.
        """
        if count is None:
            return None, None
        first = 1
        rows = await self.db.fetch(
            "select first_episode from app.season where series_id = $1 and season_number = $2",
            series_id,
            season_number,
        )
        if rows and rows[0].get("first_episode"):
            first = int(rows[0]["first_episode"])
        return first, first + count - 1

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
                "digits only, starting with the country code, e.g. +919876543210. I delete that "
                "message as soon as I have used it, so keep the number to hand.",
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
            # Close the half-finished attempt on the way out. `MTProtoLogin` does this itself now,
            # and so does the transport here, because a login client that stays connected in a
            # free-tier container is a connection nobody will ever use or close.
            discard = getattr(self.transport, "discard", None)
            if callable(discard):
                await discard(phone)
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
                delete_prompt_too=True,
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
                    "this account has 2FA. Reply with /password <your password>. Your code message is "
                    "deleted now, and your password message as soon as it is used — I never store the "
                    "password and never repeat it.",
                    delete_prompt_too=True,
                )
            ]
        except LoginUnstored as exc:
            # The account is in. No code try is spent and nothing is retried: the next step is on the
            # account (terminate the stray session), not in this chat, and asking for the code again over
            # a code Telegram has already burned is how an account gets rate-limited for no reason.
            self.pending.pop(update.chat_id, None)
            return [
                Reply(
                    f"{scrub(str(exc), phone, pending.password or '')[:400]}\n\nNothing was stored, so "
                    "this service cannot use the account yet.",
                    delete_prompt_too=True,
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
                    delete_prompt_too=True,
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
                    f"{scrub(str(exc), phone)[:180]}\n\nNothing is usable yet, and the code is spent — "
                    "check DATABASE_URL (session-mode pooler, port 5432) and that the migrations "
                    "(app.telegram_session) are applied, then run /login again.",
                    delete_prompt_too=True,
                )
            ]
        self.pending.pop(update.chat_id, None)
        who = result.username or result.account_id or "unknown"
        state = "not active — /use " + pending.name + " to switch to it" if already_live else "active"
        handoff = "This service has no writer to hand it to, so nothing writes until APP_MODE=live."
        if self.on_session_stored is not None:
            # Hand the account to the connection that writes, so a login takes effect without a redeploy —
            # and wait for it, because the one place the operator learns whether that worked is this
            # reply. Awaiting costs a couple of seconds on the poll loop; guessing costs a silent queue
            # that says "stored" over a session nobody adopted.
            try:
                note = await asyncio.wait_for(self.on_session_stored(), timeout=_ADOPT_TIMEOUT)
                handoff = str(note) if note else "The service took this session for its writes."
            except asyncio.TimeoutError:
                handoff = (
                    f"the writer did not answer within {int(_ADOPT_TIMEOUT)}s. The session is stored and "
                    "will be read at the next connect; /status shows what it thinks it has"
                )
            except Exception as exc:  # noqa: BLE001 - the storage is the success, this is the footnote
                log.warning("the session is stored but could not be handed to the writer", exc_info=True)
                handoff = (
                    f"the session is stored, but this service could not start using it yet "
                    f"({type(exc).__name__}: {str(exc)[:120]})"
                )
        return [
            Reply(
                f"connected as @{who}, stored as {pending.name!r} "
                f"({stored.get('length_chars') or len(result.session_string)} chars, {state}).\n\n"
                "The session string was never shown in this chat and cannot be read back from it. "
                f"/sessions lists what is stored. {handoff}.",
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
        pure decision function that tests can call without a fake network, and only
        ever removes the operator's spent message — never the reply that tells them
        what to do next.
        """
        try:
            replies = await self.handle(update)
        except Exception as exc:  # noqa: BLE001 - a chat command that fails has to say so
            # Silence here was misread as a bot that ignored its owner, and the one failure it hid was a
            # database the container cannot reach. The reason is worth a message even when it is a type
            # name; the secrets are scrubbed out of it first, like every other line we send.
            log.exception("control command failed")
            replies = [
                Reply(
                    "this command could not finish: "
                    f"{type(exc).__name__}: {str(exc)[:200]}\n\n"
                    "Nothing was changed. If the line above mentions the database, check DATABASE_URL "
                    "(it has to be the session-mode pooler on port 5432 — the transaction pooler on 6543 "
                    "refuses the prepared statements this service uses) and that the migrations ran."
                )
            ]
        for reply in replies:
            await self.api.send(update.chat_id, scrub(reply.text, *self._live_secrets()))
            if self.delete_sensitive and reply.delete_prompt_too and update.message_id:
                # The spent secret goes; the reply above it stays.
                await self.api.delete(update.chat_id, update.message_id)
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
