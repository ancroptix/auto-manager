"""The control bot: who may talk to it, and what a login is allowed to leave behind.

These tests drive :meth:`ControlBot.handle` / :meth:`~ControlBot.dispatch` directly
— no network, no Telegram, no database (the SQL is covered on a real Postgres in
``test_migrations_on_postgres.py``). The properties that matter are the dangerous
ones:

* a stranger's message produces *nothing* — no reply, no echo, no hint the bot exists;
* a login stores a session and produces a reply that cannot contain that session,
  the phone number, the code or the password;
* the messages carrying those secrets get deleted;
* every command refuses to run outside a private chat;
* the Bot API client cannot print the token.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.botapi import BotApi, BotTokenError, parse_update, redact
from app.config import Settings
from app.controlbot import MAX_CODE_TRIES, ControlBot, LoginResult, NeedsPassword, Reply
from app.sessions import mask_phone, scrub, valid_name
from app.telegram_client import TelegramNotConfigured

OWNER = 7
STRANGER = 99
PHONE = "+919876543210"
TOKEN = "123456789:TEST-token_value_abcdefghijklmnop"
SESSION = "1AAAAABcd" + "E" * 150 + "fg"


# --------------------------------------------------------------------- fakes
@dataclass
class FakeApi:
    """The Bot API methods the control bot uses, recorded for assertions."""

    sent: list[tuple[int, str]] = field(default_factory=list)
    deleted: list[tuple[int, tuple[int, ...]]] = field(default_factory=list)
    callbacks: list[tuple[str, str]] = field(default_factory=list)
    updates: list[dict[str, Any]] = field(default_factory=list)
    _offset: int = 0
    poll_error: Exception | None = None

    async def get_me(self) -> dict[str, Any]:
        return {"id": 123456789, "username": "auto_manager_control_bot"}

    async def get_updates(self, *, timeout: float | None = None):
        if self.poll_error is not None:
            raise self.poll_error
        pending, self.updates = self.updates, []
        for raw in pending:
            self._offset = max(self._offset, int(raw["update_id"]))
        return [parse_update(raw) for raw in pending]

    async def send(self, chat_id: int, text: str, *, reply_to=None, parse_mode=None) -> int:
        self.sent.append((chat_id, text))
        return 500 + len(self.sent)

    async def delete(self, chat_id: int, *message_ids: int | None) -> int:
        ids = tuple(mid for mid in message_ids if mid)
        if ids:
            self.deleted.append((chat_id, ids))
        return len(ids)

    async def answer_callback(self, callback_id: str | None, text: str = "") -> None:
        self.callbacks.append((callback_id or "", text))

    async def close(self) -> None:
        return None

    @property
    def texts(self) -> str:
        return "\n".join(text for _, text in self.sent)


class FakeDb:
    """Only the shapes the bot uses, routed by the SQL it sends."""

    def __init__(self, *, stored: list[dict] | None = None, blocked: list[dict] | None = None):
        self.stored = stored if stored is not None else []
        self.blocked = blocked or []
        self.audit: list[tuple[str, tuple]] = []
        self.paused: list[tuple[bool, str | None]] = []
        self.queued: list[tuple[str, Any, int]] = []
        self.leases_released = 0
        self.review_count = 2
        self.series = [
            {"id": 7, "title": "Dekin no mogura", "normalized_title": "dekin no mogura"},
        ]
        # A files-only source channel: no title that reads as a series, nothing declared yet.
        self.source_channels = [
            {
                "id": 3,
                "username": "anime_uploads4u",
                "title": None,
                "telegram_channel_id": -1001112223334,
                "declared_series": "",
                "declared_audio": "",
                "declared_season": -1,
                # Read from the row like a real one: the in-place mode is refused until rights are
                # known, so a fake row without this key is a fake row nobody can caption in.
                "we_are_admin": True,
            }
        ]
        self.declared_history: list[dict] = []
        # /inplace: the destination row for the channel being switched, the messages it would
        # edit, and the episode numbers of the channel it is compared against.
        self.destination: dict | None = None
        self.inplace_rows: list[dict] = []
        self.inplace_source: list[int] = []
        self.config_rows: dict[str, Any] = {}
        # What `select reason, count(*) ... where disposition = 'pending'` would answer.
        self.parked: list[dict] = []
        self.writes: list[tuple[str, tuple]] = []

    @property
    def connected(self) -> bool:
        return True

    async def fetch(self, sql: str, *args: Any):
        if "from app.telegram_session" in sql:
            rows = [dict(row) for row in self.stored]
            if "name = $" in sql and args:
                rows = [row for row in rows if row["name"] == args[0]]
            return rows
        if "from app.config" in sql:
            return [
                {"key": "caption.button_rows", "value": "one_per_line"},
                {"key": "caption.total_episodes_unknown", "value": "TBA"},
                {"key": "ingest.require_hindi_audio", "value": "true"},
                {"key": "thumbnail.strict_mode", "value": "true"},
            ]
        if "from app.series" in sql:
            needle = str(args[0] or "")
            hits = [row for row in self.series if needle and needle in row["normalized_title"]]
            return hits or ([] if needle else [dict(row) for row in self.series])
        if "as is_media" in sql:  # /inplace's plan query
            return [dict(row) for row in self.inplace_rows]
        if "select distinct episode_number" in sql:
            return [{"episode_number": number} for number in self.inplace_source]
        if "from app.source_candidate" in sql:
            return list(self.parked)
        if "from app.source_channel" in sql:
            handle, numeric = args[0], args[1]
            hits = [
                dict(row)
                for row in self.source_channels
                if (handle and str(row["username"] or "").casefold() == str(handle).casefold())
                or (handle and str(row["title"] or "").casefold() == str(handle).casefold())
                or (numeric is not None and row["telegram_channel_id"] == numeric)
            ]
            return hits
        if "app.job" in sql:
            return list(self.blocked)
        return []

    async def fetchrow(self, sql: str, *args: Any):
        if "from app.destination" in sql:
            return dict(self.destination) if self.destination else None
        if "delete from app.telegram_session" in sql:
            removed = [row for row in self.stored if row["name"] == args[0]]
            self.stored = [row for row in self.stored if row["name"] != args[0]]
            return {"name": removed[0]["name"]} if removed else None
        if "from app.telegram_session" in sql:
            return dict(self.stored[-1]) if self.stored else None
        if "app.service_state" in sql:
            paused = self.paused[-1] if self.paused else (False, None)
            return {"paused": paused[0], "reason": paused[1] or "", "last_reconcile_at": None}
        if "insert into app.telegram_session" in sql:
            return dict(self.stored[-1]) if self.stored else None
        return {"id": 42}

    async def fetchval(self, sql: str, *args: Any):
        if "session_string" in sql:
            return next((row.get("session_string") for row in self.stored if row.get("active")), None)
        if "thumbnail_review" in sql:
            return self.review_count
        return None

    async def config(self, key: str, default: Any = None) -> Any:
        return self.config_rows.get(key, default)

    async def execute(self, sql: str, *args: Any) -> int:
        if "publish_role" in sql:
            import re as _re

            match = _re.search(r"publish_role = (?:'([^']+)'|\$(\d+))", sql)
            role = match.group(1) or args[int(match.group(2)) - 1]
            row = next((r for r in self.source_channels if r["id"] == args[0]), None)
            if row is not None:
                row["publish_role"] = role
            self.writes.append((sql, args))
            return 1
        if "app.destination set" in sql:
            import re as _re

            mode = _re.search(r"publish_mode = '([^']+)'", sql)
            if mode and self.destination is not None:
                self.destination["publish_mode"] = mode.group(1)
            if "paired_source_channel_id" in sql and self.destination is not None:
                self.destination["paired_source_channel_id"] = args[1]
            self.writes.append((sql, args))
            return 1
        if "update app.source_channel set" in sql:
            import re as _re

            columns = _re.findall(r"declared_(\w+) = \$", sql)
            row = next((r for r in self.source_channels if r["id"] == args[0]), None)
            if row is None:
                return 0
            for position, name in enumerate(columns):
                value = args[position + 1]
                if value is None:
                    # Mirror the fake's own encoding: this row has no NULLs, and -1 is what
                    # the SELECT's coalesce() turns a NULL season into.
                    row[f"declared_{name}"] = -1 if name == "season" else ""
                else:
                    row[f"declared_{name}"] = str(value)
            self.declared_history.append(dict(row))
            self.writes.append((sql, args))
            return 1
        if "insert into app.season" in sql:
            # /declare's write, asserted as (series id, season, count) by those tests.
            self.writes.append((sql, args))
            return 1
        if "insert into app.config" in sql and "on conflict (key) do update" in sql:
            # /joinmsg writes wording, and the write has to come back out on the next read: the
            # round trip is the only way a test can prove the command saves what it says it saved.
            import json as _json

            key, value = args[0], _json.loads(args[1])
            self.writes.append((sql, args))
            self.config_rows[key] = value
            return 1
        if "insert into app.audit_log" in sql:
            self.audit.append((sql, args))
            return 1
        if "insert into app.telegram_session" in sql:
            kind, active = args[1], bool(args[5])
            if active:
                self.stored = [
                    {**row, "active": False} for row in self.stored if row.get("kind") == kind and row["name"] != args[0]
                ]
            row = {
                "name": args[0],
                "kind": kind,
                "active": active,
                "account_id": args[3],
                "username": args[4],
                "length_chars": len(args[2]),
                "session_string": args[2],
            }
            self.stored = [r for r in self.stored if r["name"] != args[0]] + [row]
            return 1
        if "set active = false" in sql and "and active" in sql and "name <>" not in sql:
            self.stored = [{**r, "active": False} for r in self.stored]
            return len(self.stored)
        if "set active = true" in sql:
            self.stored = [{**r, "active": r["name"] == args[0]} for r in self.stored]
            return 1
        if "delete from app.telegram_session" in sql:
            before = len(self.stored)
            self.stored = [r for r in self.stored if r["name"] != args[0]]
            return before - len(self.stored)
        return 1

    async def queue_health(self) -> dict[str, Any]:
        return {"queued": 3, "running": 1, "blocked": len(self.blocked), "failed": 0, "succeeded_1h": 12}

    async def set_paused(self, value: bool, reason: str | None = None) -> None:
        self.paused.append((value, reason))

    async def release_expired_locks(self) -> int:
        self.leases_released += 1
        return 2

    async def enqueue(self, kind: str, dedup_key: str, *, stage=None, payload=None, priority=100, **kw):
        self.queued.append((kind, payload, priority))
        return {"id": 42}


class FakeTransport:
    """Stands in for app.mtproto_login.MTProtoLogin."""

    def __init__(
        self,
        *,
        require_password: bool = False,
        error: str | None = None,
        result: LoginResult | None = None,
    ):
        self.require_password = require_password
        self.error = error
        self.result = result or LoginResult(session_string=SESSION, account_id=4242, username="spare_account")
        self.codes: list[str] = []
        self.sign_ins: list[dict] = []
        self.discarded: list[Any] = []

    async def send_code(self, phone: str) -> str:
        if self.error == "invalid_phone":
            raise RuntimeError(f"PhoneNumberInvalidError: {phone} is not valid")
        self.codes.append(phone)
        return "codehash123"

    async def sign_in(self, phone, code, code_hash, *, password=None) -> LoginResult:
        self.sign_ins.append({"phone": phone, "code": code, "hash": code_hash, "password": password})
        if self.error == "boom":
            raise RuntimeError(f"connection reset while using {SESSION} for {phone}")
        if self.error == "expired":
            raise RuntimeError("PHONE_CODE_INVALID: the code has expired")
        if self.require_password and password is None:
            raise NeedsPassword("two-factor protection is on")
        return self.result

    async def discard(self, phone) -> None:
        self.discarded.append(phone)


_UNSET = object()


def settings(**overrides: Any) -> Settings:
    params: dict[str, Any] = {
        "_env_file": None,
        "app_name": "auto-manager",
        "worker_enabled": False,
        "telegram_api_id": 12345,
        "telegram_api_hash": "0123456789abcdef012456789abcdef",  # allowlist: fake credential
        "telegram_owner_user_ids": str(OWNER),
        "telegram_bot_token": TOKEN,
    }
    params.update(overrides)
    return Settings(**params)


def raw_update(text: str, *, chat: int = OWNER, sender: int = OWNER, message_id: int = 11) -> dict[str, Any]:
    return {
        "update_id": message_id,
        "message": {
            "message_id": message_id,
            "chat": {"id": chat},
            "from": {"id": sender},
            "text": text,
        },
    }


def update(text: str, **kwargs: Any):
    parsed = parse_update(raw_update(text, **kwargs))
    assert parsed is not None
    return parsed


def bot(*, db: FakeDb | None = None, transport: Any = _UNSET, api: FakeApi | None = None, **overrides: Any):
    """Build a ControlBot plus its fakes. `transport=None` means 'no login machinery'."""
    api = api or FakeApi()
    db = db or FakeDb()
    extra = dict(overrides)
    settings_overrides = {k: extra.pop(k) for k in [k for k in list(extra) if k in Settings.model_fields]}
    control_kwargs = extra
    control = ControlBot(
        api=api,  # type: ignore[arg-type]
        db=db,  # type: ignore[arg-type]
        settings=settings(**settings_overrides),
        transport=FakeTransport() if transport is _UNSET else transport,  # type: ignore[arg-type]
        owner_ids=frozenset({OWNER}),
        **control_kwargs,
    )
    return control, api, db


async def say(control: ControlBot, text: str, **kwargs: Any) -> list[str]:
    replies = await control.handle(update(text, **kwargs))
    return [reply.text for reply in replies]


# --------------------------------------------------------------------- access control
@pytest.mark.asyncio
async def test_a_stranger_gets_nothing_at_all() -> None:
    control, api, db = bot()
    replies = await control.handle(update("/status", sender=STRANGER, chat=STRANGER))
    assert replies == []
    assert api.sent == [], "answering a stranger teaches them that the bot is live"


@pytest.mark.asyncio
async def test_dispatch_of_a_strangers_login_attempt_stores_nothing() -> None:
    transport = FakeTransport()
    control, api, db = bot(transport=transport)
    await control.dispatch(update(f"/login spare {PHONE}", sender=STRANGER, chat=STRANGER))
    assert transport.codes == [] and db.stored == [] and api.sent == []


@pytest.mark.asyncio
async def test_a_group_chat_never_runs_a_command() -> None:
    """The owner id alone is not enough: in a group, the sender may be anyone and
    even a genuine owner's message could be replayed by another member."""
    transport = FakeTransport()
    control, api, db = bot(transport=transport)
    replies = await control.handle(update("/status", chat=-1001234))
    assert len(replies) == 1 and "private" in replies[0].text.lower()
    assert db.paused == [] and transport.codes == []


@pytest.mark.asyncio
async def test_the_bot_refuses_to_be_built_without_owners() -> None:
    with pytest.raises(ValueError) as excinfo:
        ControlBot(api=FakeApi(), db=FakeDb(), settings=settings(), owner_ids=frozenset())  # type: ignore[arg-type]
    assert "TELEGRAM_OWNER_USER_IDS" in str(excinfo.value)


@pytest.mark.asyncio
async def test_an_unknown_command_is_ignored_rather_than_answered() -> None:
    control, api, db = bot()
    assert await control.handle(update("/frobnicate")) == []
    assert await control.handle(update("hello?")) == []
    assert api.sent == []


@pytest.mark.asyncio
async def test_callback_query_data_is_treated_as_text_from_the_sender() -> None:
    """A tapped button must pass the same gate as a typed command."""
    control, api, db = bot()
    button = {
        "id": "cb1",
        "from": {"id": OWNER},
        "message": {"message_id": 3, "chat": {"id": OWNER}},
        "data": "/pause",
    }
    parsed = parse_update({"update_id": 4, "callback_query": button})
    assert parsed is not None and parsed.kind == "callback"
    replies = await control.handle(parsed)
    assert replies and "paused" in replies[0].text
    assert db.paused and db.paused[-1][0] is True


@pytest.mark.asyncio
async def test_a_strangers_button_press_is_dropped() -> None:
    control, api, db = bot()
    button = {
        "id": "cb1",
        "from": {"id": STRANGER},
        "message": {"message_id": 3, "chat": {"id": STRANGER}},
        "data": "/pause",
    }
    parsed = parse_update({"update_id": 5, "callback_query": button})
    assert parsed is not None
    assert await control.handle(parsed) == []
    assert db.paused == []


# --------------------------------------------------------------------- read-only commands
@pytest.mark.asyncio
async def test_status_reports_mode_queue_and_settings_without_secrets() -> None:
    db = FakeDb(blocked=[{"kind": "storage_upload", "n": 7, "why": "FeatureNotImplemented: storage bot menu"}])
    control, api, _ = bot(db=db)
    replies = await control.handle(update("/status"))
    text = replies[0].text
    assert "shadow" in text and "queued=3" in text
    assert "storage_upload x7" in text and "thumbnails awaiting" in text
    assert "thumbnail.strict_mode" in text and "caption.button_rows" in text
    assert TOKEN not in text and SESSION not in text and "session_string" not in text


@pytest.mark.asyncio
async def test_status_names_the_updates_channel_state() -> None:
    """The noticeboard is a real destination, so /status admits when it cannot be reached.

    With nothing set, the honest answer is "nowhere to go" — not silence, and not a count of
    announcements that would be queued if someone had named the channel.
    """
    db = FakeDb()
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/status")
    assert "updates channel: not set" in text, text

    db2 = FakeDb()
    db2.config_rows.update({"updates.channel": "@yc_updates", "updates.per_episode": True})
    control2, _api2, _db2 = bot(db=db2)
    (text2,) = await say(control2, "/status")
    assert "@yc_updates" in text2, text2
    # Approved is not wired. The line has to carry both halves or the operator reads "approved" as
    # "announcements are going out", which is the one thing this status must never imply.
    assert "the box is approved" in text2 and "unwired" in text2, text2
    assert "not set" not in text2


@pytest.mark.asyncio
async def test_status_says_so_when_the_database_is_not_connected() -> None:
    class NotConnected(FakeDb):
        @property
        def connected(self) -> bool:  # type: ignore[override]
            return False

    control, api, _ = bot(db=NotConnected())
    assert "NOT CONNECTED" in (await control.handle(update("/status")))[0].text


@pytest.mark.asyncio
async def test_pause_and_resume_reach_the_database() -> None:
    control, api, db = bot()
    await say(control, "/pause operator asked for a stop")
    assert db.paused[-1] == (True, "operator asked for a stop")
    await say(control, "/resume")
    assert db.paused[-1][0] is False


@pytest.mark.asyncio
async def test_reconcile_queues_the_reconciler_with_a_unique_dedup_key() -> None:
    control, api, db = bot()
    replies = await control.handle(update("/reconcile"))
    assert db.queued and db.queued[0][0] == "reconciliation" and db.queued[0][2] == 5
    assert "reclaimed 2" in replies[0].text
    # Two presses must not collapse into one job...
    await say(control, "/reconcile")
    assert len(db.queued) == 2


@pytest.mark.asyncio
async def test_probe_is_refused_without_a_session_and_runs_in_the_background_otherwise() -> None:
    scheduled: list[Any] = []
    shadow, _, _ = bot()
    replies = await shadow.handle(update("/probe"))
    assert "needs a live user session" in replies[0].text and scheduled == []

    control, api, db = bot(
        mode="live",
        database_url="postgresql://u:p@host:5432/db",
        control_token="x" * 40,
        telegram_session_string=SESSION,
        background=scheduled.append,
    )
    replies = await control.handle(update("/probe"))
    assert len(scheduled) == 1, "a two-minute probe must not block the poll loop"
    scheduled[0].close()
    assert "probe started" in replies[0].text.lower()


@pytest.mark.asyncio
async def test_sessions_are_listed_without_their_contents() -> None:
    db = FakeDb(stored=[{"name": "spare", "kind": "user", "active": True, "account_id": 42, "username": "x", "length_chars": 302}])
    control, api, _ = bot(db=db)
    replies = await control.handle(update("/sessions"))
    text = replies[0].text
    assert "spare" in text and "302 chars" in text and text.lstrip().startswith("stored sessions")
    assert SESSION not in text


@pytest.mark.asyncio
async def test_forget_says_that_losing_our_copy_is_not_revoking_the_session() -> None:
    db = FakeDb(stored=[{"name": "spare", "kind": "user", "active": True, "length_chars": 302}])
    control, api, _ = bot(db=db)
    replies = await control.handle(update("/forget spare"))
    assert "does NOT sign the account out" in replies[0].text and "Devices" in replies[0].text
    assert db.stored == []


@pytest.mark.asyncio
async def test_forget_without_a_name_lists_what_exists_instead_of_choosing() -> None:
    db = FakeDb(stored=[{"name": "spare", "kind": "user", "active": True, "length_chars": 302}])
    control, api, _ = bot(db=db)
    replies = await control.handle(update("/forget"))
    assert "spare" in replies[0].text
    assert db.stored, "guessing which session to delete would eventually be wrong"


# --------------------------------------------------------------------- login
@pytest.mark.asyncio
async def test_login_without_a_number_asks_for_one_and_remembers_the_name() -> None:
    transport = FakeTransport()
    control, api, db = bot(transport=transport)
    replies = await control.handle(update("/login spare"))
    assert "phone number" in replies[0].text.lower() or "which phone" in replies[0].text.lower()
    assert control.pending[OWNER].name == "spare" and control.pending[OWNER].stage == "phone"
    assert transport.codes == [], "no code is requested until a number exists"


@pytest.mark.asyncio
async def test_a_bare_phone_number_continues_the_login() -> None:
    transport = FakeTransport()
    control, api, db = bot(transport=transport)
    await say(control, "/login spare")
    replies = await control.handle(update(PHONE))
    assert transport.codes == [PHONE]
    assert control.pending[OWNER].stage == "code"
    assert PHONE not in replies[0].text, "the number must not be echoed back in full"
    assert "+91…3210" in replies[0].text, "recognisable, not dialable"


@pytest.mark.asyncio
async def test_an_unusable_number_is_asked_for_again_without_touching_telegram() -> None:
    transport = FakeTransport()
    control, api, db = bot(transport=transport)
    replies = await control.handle(update("/login spare 0987 654"))
    assert "did not look right" in replies[0].text and transport.codes == []


@pytest.mark.asyncio
async def test_login_rejects_a_name_that_is_not_safe_to_use() -> None:
    transport = FakeTransport()
    control, api, db = bot(transport=transport)
    # An unusable name must be rejected before a code is fetched: store() would
    # only notice afterwards, and the account has already spent a login request.
    replies = await control.handle(update("/login bad;name +919876543210"))
    assert "letters, numbers" in replies[0].text.lower()
    assert transport.codes == [] and db.stored == []


@pytest.mark.asyncio
async def test_a_good_code_stores_the_session_and_deletes_the_operators_message() -> None:
    transport = FakeTransport()
    control, api, db = bot(transport=transport)
    await say(control, f"/login spare {PHONE}")
    replies = await control.dispatch(update("482913", message_id=12))
    text = "\n".join(reply.text for reply in replies)
    assert "connected as @spare_account" in text and "stored as 'spare'" in text
    assert db.stored and db.stored[0]["name"] == "spare" and db.stored[0]["active"] is True
    assert control.pending == {}, "a finished flow must not linger in memory"
    assert api.deleted and 12 in api.deleted[-1][1], "the code message has to disappear"
    assert all(SESSION not in reply.text for reply in replies)
    assert transport.sign_ins[0] == {"phone": PHONE, "code": "482913", "hash": "codehash123", "password": None}


@pytest.mark.asyncio
async def test_two_factor_asks_for_a_password_and_clears_it_afterwards() -> None:
    transport = FakeTransport(require_password=True)
    control, api, db = bot(transport=transport)
    await say(control, f"/login spare {PHONE}")
    replies = await control.handle(update("482913"))
    assert "2FA" in replies[0].text and "never store the password" in replies[0].text
    assert control.pending[OWNER].stage == "password"
    assert control.pending[OWNER].code is None, "a spent code must not stay reusable"

    pending = control.pending[OWNER]
    await control.dispatch(update("hunter2 secret", message_id=13))
    assert transport.sign_ins[-1]["password"] == "hunter2 secret"
    assert transport.sign_ins[-1]["code"] == "", "the code must not be resent after it was spent"
    assert pending.password is None, "the password must be cleared even on the success path"
    assert control.pending == {}, "a finished flow must not keep the phone number"
    assert db.stored and db.stored[0]["username"] == "spare_account"
    assert any(13 in ids for _, ids in api.deleted)
    assert all("hunter2" not in text for _, text in api.sent)


@pytest.mark.asyncio
async def test_wrong_codes_are_allowed_three_times_then_the_flow_closes() -> None:
    transport = FakeTransport(error="boom")
    control, api, db = bot(transport=transport)
    await say(control, f"/login spare {PHONE}")
    for attempt in range(MAX_CODE_TRIES - 1):
        replies = await control.handle(update(f"00000{attempt}"))
        assert "sign-in failed" in replies[0].text.lower()
        assert OWNER in control.pending
    replies = await control.handle(update("999999"))
    assert f"{MAX_CODE_TRIES} attempts in a row" in replies[0].text
    assert control.pending == {}, "the fourth guess must not be offered"
    assert db.stored == []


@pytest.mark.asyncio
async def test_an_expired_code_closes_the_flow_immediately() -> None:
    transport = FakeTransport(error="expired")
    control, api, db = bot(transport=transport)
    await say(control, f"/login spare {PHONE}")
    replies = await control.handle(update("482913"))
    assert "that code was rejected" in replies[0].text and control.pending == {}


@pytest.mark.asyncio
async def test_a_transport_failure_cannot_leak_the_session_or_the_number() -> None:
    """Error text from the network is untrusted: it is scrubbed before it is shown."""
    transport = FakeTransport(error="boom")
    control, api, db = bot(transport=transport)
    await say(control, f"/login spare {PHONE}")
    text = "\n".join(reply.text for reply in await control.handle(update("482913")))
    assert SESSION not in text and PHONE not in text
    assert "‹redacted-session›" in text


@pytest.mark.asyncio
async def test_a_failed_code_request_is_reported_without_storing_anything() -> None:
    transport = FakeTransport(error="invalid_phone")
    control, api, db = bot(transport=transport)
    replies = await control.handle(update(f"/login spare {PHONE}"))
    assert "could not send a code" in replies[0].text
    assert PHONE not in replies[0].text, "even an error echo must not repeat the number"
    assert control.pending == {} and db.stored == []


@pytest.mark.asyncio
async def test_a_login_that_cannot_be_stored_is_said_out_loud() -> None:
    class BrokenDb(FakeDb):
        async def execute(self, sql: str, *args: Any) -> int:
            if "insert into app.telegram_session" in sql:
                raise RuntimeError("relation app.telegram_session does not exist")
            return await super().execute(sql, *args)

    control, api, _ = bot(db=BrokenDb())
    await say(control, f"/login spare {PHONE}")
    replies = await control.handle(update("482913"))
    assert "could not store the session" in replies[0].text and "migrations" in replies[0].text
    assert SESSION not in replies[0].text


@pytest.mark.asyncio
async def test_a_second_login_is_stored_without_hijacking_the_live_account() -> None:
    """Silently switching which account posts to a 30k-subscriber channel is the
    worst failure this flow could have, so only the first login takes the live slot."""
    db = FakeDb(stored=[{"name": "spare", "kind": "user", "active": True, "length_chars": 300, "session_string": SESSION}])
    transport = FakeTransport(result=LoginResult(session_string="1" + "Z" * 300, account_id=9, username="second_account"))
    control, api, _ = bot(db=db, transport=transport)
    await say(control, f"/login second {PHONE}")
    replies = await control.handle(update("482913"))
    text = replies[0].text
    assert "not active" in text and "/use second" in text
    assert db.stored[-1]["name"] == "second" and db.stored[-1]["active"] is False
    assert db.stored[0]["active"] is True, "the live account stays live"


@pytest.mark.asyncio
async def test_the_first_login_does_take_the_live_slot() -> None:
    db = FakeDb(stored=[])
    control, api, _ = bot(db=db)
    await say(control, f"/login spare {PHONE}")
    replies = await control.handle(update("482913"))
    assert "chars, active)" in replies[0].text
    assert db.stored[0]["active"] is True


@pytest.mark.asyncio
async def test_three_code_requests_in_ten_minutes_then_a_block() -> None:
    transport = FakeTransport()
    control, api, db = bot(transport=transport)
    for _ in range(3):
        await say(control, f"/login spare {PHONE}")
        await say(control, "/cancel")
    replies = await control.handle(update(f"/login spare {PHONE}"))
    assert "too many login attempts" in replies[0].text
    assert len(transport.codes) == 3, "the block must stop the request, not just the reply"


@pytest.mark.asyncio
async def test_login_is_refused_when_the_operator_closed_the_door() -> None:
    transport = FakeTransport()
    control, api, db = bot(transport=transport, allow_login=False)
    replies = await control.handle(update(f"/login spare {PHONE}"))
    assert "BOT_ALLOW_LOGIN=0" in replies[0].text
    assert transport.codes == [] and db.stored == []


@pytest.mark.asyncio
async def test_a_login_without_api_credentials_explains_what_to_set() -> None:
    control, api, db = bot(transport=None, telegram_api_id=None, telegram_api_hash=None)
    replies = await control.handle(update(f"/login spare {PHONE}"))
    assert "TELEGRAM_API_ID" in replies[0].text and db.stored == []


@pytest.mark.asyncio
async def test_a_code_without_a_pending_login_is_refused() -> None:
    control, api, db = bot()
    assert await control.handle(update("482913")) == [], "no pending flow means no bare-code handling"
    replies = await control.handle(update("/code 482913"))
    assert "no login in progress" in replies[0].text


@pytest.mark.asyncio
async def test_password_is_only_accepted_when_the_account_asked_for_it() -> None:
    control, api, db = bot(transport=FakeTransport(require_password=True))
    await say(control, f"/login spare {PHONE}")
    replies = await control.handle(update("/password guesser"))
    assert "did not ask for a password" in replies[0].text


@pytest.mark.asyncio
async def test_cancel_clears_the_flow_and_tells_the_transport_to_disconnect() -> None:
    transport = FakeTransport()
    control, api, db = bot(transport=transport)
    await say(control, f"/login spare {PHONE}")
    replies = await control.dispatch(update("/cancel", message_id=21))
    assert "cancelled" in replies[0].text.lower() and "Nothing was stored" in replies[0].text
    assert control.pending == {} and transport.discarded == [PHONE]
    assert any(21 in ids for _, ids in api.deleted)


@pytest.mark.asyncio
async def test_an_expired_attempt_is_dropped_before_the_code_is_tried() -> None:
    transport = FakeTransport()
    control, api, db = bot(transport=transport, login_ttl_seconds=0)
    await say(control, f"/login spare {PHONE}")
    replies = await control.handle(update("482913"))
    assert replies == [] and transport.sign_ins == [] and db.stored == []
    # and the flow is gone, so a later /code says "no login in progress"
    assert "no login in progress" in (await control.handle(update("/code 482913")))[0].text


@pytest.mark.asyncio
async def test_use_switches_the_active_session() -> None:
    db = FakeDb(
        stored=[
            {"name": "spare", "kind": "user", "active": True, "length_chars": 300},
            {"name": "backup", "kind": "user", "active": False, "length_chars": 300},
        ]
    )
    control, api, _ = bot(db=db)
    replies = await control.handle(update("/use backup"))
    assert "backup is now the active session" in replies[0].text
    by_name = {row["name"]: row["active"] for row in db.stored}
    assert by_name == {"spare": False, "backup": True}


@pytest.mark.asyncio
async def test_use_names_the_sessions_it_does_know_when_given_one_it_does_not() -> None:
    db = FakeDb(stored=[{"name": "spare", "kind": "user", "active": True, "length_chars": 300}])
    control, api, _ = bot(db=db)
    replies = await control.handle(update("/use ghost"))
    assert "unknown session name" in replies[0].text.lower() and "spare" in replies[0].text


# --------------------------------------------------------------------- the poll loop
@pytest.mark.asyncio
async def test_run_once_advances_the_offset_even_for_a_stranger() -> None:
    api = FakeApi(updates=[raw_update("/status", sender=STRANGER, chat=STRANGER, message_id=17)])
    control, _, db = bot(api=api)
    assert await control.run_once() == 1
    assert api._offset == 17, "an unprocessed update would be replayed forever"
    assert api.sent == []


@pytest.mark.asyncio
async def test_run_once_processes_an_owners_command() -> None:
    api = FakeApi(updates=[raw_update("/pause", message_id=18)])
    control, _, db = bot(api=api)
    assert await control.run_once() == 1
    assert db.paused and db.paused[-1][0] is True


@pytest.mark.asyncio
async def test_replies_are_scrubbed_on_the_way_out() -> None:
    """Even a reply written by a handler that forgot to be careful is filtered."""

    class LeakierBot(ControlBot):
        async def _status(self, update, args):  # type: ignore[override]
            return [Reply(f"session is {SESSION}, token {self.settings.reveal('telegram_bot_token')}")]

    api = FakeApi()
    control = LeakierBot(
        api=api,  # type: ignore[arg-type]
        db=FakeDb(),  # type: ignore[arg-type]
        settings=settings(),
        transport=FakeTransport(),  # type: ignore[arg-type]
        owner_ids=frozenset({OWNER}),
    )
    await control.dispatch(update("/status"))
    assert SESSION not in api.texts and TOKEN not in api.texts
    assert "‹redacted" in api.texts


# --------------------------------------------------------------------- botapi surface
def test_a_malformed_token_is_rejected_before_any_request() -> None:
    for bad in ("", "123:short", "not-a-token", "123456789:TOKEN"):
        with pytest.raises(BotTokenError):
            BotApi(bad)


def test_the_token_never_appears_in_a_repr_or_an_error() -> None:
    api = BotApi(TOKEN)
    assert TOKEN not in repr(api) and TOKEN not in str(api)
    leaked = f"Client error '400 Bad Request' for url 'https://api.telegram.org/bot{TOKEN}/getMe'"
    cleaned = redact(leaked)
    assert TOKEN.split(":", 1)[1] not in cleaned and "/bot‹redacted›/getMe" in cleaned
    assert redact("") == ""


def test_private_chat_requires_the_sender_to_own_the_chat() -> None:
    owner_dm = parse_update(raw_update("/status"))
    group = parse_update(raw_update("/status", chat=-1001234))
    other_dm = parse_update(raw_update("/status", chat=4242))
    assert owner_dm is not None and owner_dm.is_private_chat
    assert group is not None and not group.is_private_chat
    assert other_dm is not None and not other_dm.is_private_chat


# --------------------------------------------------------------------- helpers
def test_session_names_are_constrained_to_what_the_sql_can_quote_safely() -> None:
    assert valid_name("spare") and valid_name("my-account_2")
    # Case is normalised, not forbidden: store(), activate() and forget() all
    # casefold before touching SQL, so "Spare" and "spare" are the same session.
    assert valid_name("Spare") and valid_name("SPARE")
    for bad in ("", "a b", "na;me", "x" * 41, "drop table", "ünïcode", "-lead", "a.b"):
        assert not valid_name(bad), bad


def test_mask_phone_keeps_only_enough_to_recognise_the_number() -> None:
    assert mask_phone(PHONE) == "+91…3210"
    assert mask_phone(None) == "‹phone›"
    # A too-short input is not worth a mask, but it must not be echoed either.
    assert mask_phone("+91") == "‹phone›" and mask_phone("+919876") == "‹phone›"
    # The middle digits are what make a number dialable, and they are not shown.
    assert PHONE[3:-4] not in mask_phone(PHONE)


def test_scrub_removes_named_secrets_and_session_shaped_text() -> None:
    text = f"failed for {PHONE} with {SESSION} and code 482913"
    cleaned = scrub(text, PHONE, "482913")
    assert PHONE not in cleaned and "482913" not in cleaned
    assert SESSION not in cleaned, "a session string is redacted by shape, not only by name"
    # A real StringSession is "1" plus arbitrary base64; the shape matcher has to
    # survive that, which the old 1[12]-anchored pattern did not.
    assert "A" * 60 not in scrub(f"x 1{'A' * 60}y")
    assert scrub("nothing sensitive here", "x") == "nothing sensitive here"
    assert scrub(None) == ""


def test_help_lists_the_commands_that_exist_and_nothing_invented() -> None:
    from app.controlbot import HELP

    for name in ("/status", "/login", "/code", "/password", "/sessions", "/forget", "/use", "/pause", "/resume", "/probe", "/reconcile", "/cancel"):
        assert name in HELP
    for absent in ("/delete", "/post", "/download", "/promote"):
        assert absent not in HELP, f"{absent} is not implemented and must not be advertised"


@pytest.mark.asyncio
async def test_help_answers_the_owner_only_after_gate() -> None:
    control, api, db = bot()
    replies = await control.handle(update("/help"))
    text = "\n".join(reply.text for reply in replies)
    assert TOKEN not in text and "answers you and nobody else" in text


def test_settings_safe_dump_reports_the_bot_token_as_configured() -> None:
    dump = settings().safe_dump()
    assert dump["telegram_bot_token"] == "configured"
    assert TOKEN not in json.dumps(dump)


# --------------------------------------------------------------------- advertised == implemented
def _advertised_commands() -> list[str]:
    from app.controlbot import HELP

    # not line-anchored: "/start /help" share a line in the help text
    return sorted({f"/{name}" for name in re.findall(r"(?<![\w`])/(\w+)\b", HELP)})


ADVERTISED_ARGS = {
    "/use": "spare",
    "/forget": "spare",
    "/login": f"spare {PHONE}",
    "/code": "482913",
    "/password": "guess",
    "/pause": "for a reason",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("command", _advertised_commands())
async def test_every_command_the_help_text_advertises_is_routed(command: str) -> None:
    """A command in `/help` that answers nothing is worse than an absent one."""
    import re

    db = FakeDb(stored=[{"name": "spare", "kind": "user", "active": True, "length_chars": 300}])
    control, api, _ = bot(db=db)
    text = f"{command} {ADVERTISED_ARGS.get(command, '')}".strip()
    replies = await control.handle(update(text))
    assert replies, f"{command} is advertised in HELP but returns nothing"
    assert replies[0].text.strip()


# ------------------------------------------------------------------ /declare
async def test_declare_is_refused_before_any_query_without_a_database() -> None:
    control = ControlBot(api=FakeApi(), db=None, settings=settings(), owner_ids=frozenset({OWNER}))  # type: ignore[arg-type]
    texts = await say(control, "/declare bleach 1 12")
    assert texts and "database" in texts[0].lower()


async def test_declare_writes_the_number_and_does_not_invent_one() -> None:
    """The whole point of the command: a season length enters the system by being said."""
    db = FakeDb()
    control, _api, _db = bot(db=db)
    texts = await say(control, "/declare dekin no mogura 1 12")
    assert "Dekin no mogura" in texts[0] and "12" in texts[0]
    sql, args = db.writes[-1]
    assert "first_episode" in sql and "last_episode" in sql
    # series 7, season 1, and the span twelve episodes implies when nothing says otherwise
    assert args == (7, 1, 1, 12), args


async def test_declare_reads_the_numbers_from_the_end_because_titles_have_spaces() -> None:
    """"/declare dekin no mogura 2 12" is season two, twelve episodes — the title is
    whatever is left, which is the only parsing that survives ``No. 8``."""
    db = FakeDb()
    control, _api, _db = bot(db=db)
    await say(control, "/declare dekin no mogura 2 12")
    assert db.writes[-1][1] == (7, 2, 1, 12), db.writes[-1][1]


async def test_declare_tba_clears_the_claim() -> None:
    db = FakeDb()
    control, _api, _db = bot(db=db)
    texts = await say(control, "/declare dekin no mogura 1 tba")
    assert db.writes[-1][1] == (7, 1, None, None), db.writes[-1][1]
    assert "undeclared" in texts[0]


async def test_declare_refuses_to_pick_between_two_series() -> None:
    """A guessed series puts a wrong number on somebody else's season."""
    db = FakeDb()
    db.series = [
        {"id": 7, "title": "Dekin no mogura", "normalized_title": "dekin no mogura"},
        {"id": 8, "title": "Dekin no mogura movie", "normalized_title": "dekin no mogura movie"},
    ]
    control, _api, _db = bot(db=db)
    texts = await say(control, "/declare dekin no mogura 1 12")
    assert "more than one" in texts[0]
    assert not db.writes, "it must ask, not write"


async def test_declare_refuses_a_non_number_rather_than_guessing_one() -> None:
    db = FakeDb()
    control, _api, _db = bot(db=db)
    texts = await say(control, "/declare dekin no mogura 1 twelve")
    assert "not a number" in texts[0]
    assert not db.writes


async def test_declare_names_a_series_that_has_never_been_filed() -> None:
    """Declaring a length for an unknown series would have to create the series row,
    which is ingest's job; the answer says the row is missing instead."""
    db = FakeDb()
    db.series = []
    control, _api, _db = bot(db=db)
    texts = await say(control, "/declare unknown show 1 12")
    assert "no series stored" in texts[0]
    assert not db.writes


async def test_declare_shows_usage_before_touching_the_database() -> None:
    db = FakeDb()
    control, _api, _db = bot(db=db)
    texts = await say(control, "/declare")
    assert "usage" in texts[0].lower() and "/declare" in texts[0]
    assert not db.writes


def test_the_help_text_advertises_exactly_the_routed_commands() -> None:
    """Guard against the router and the help text drifting apart in either
    direction: an undocumented command is undiscoverable, a documented non-command
    is a lie.

    Both sides are read from the code rather than typed into this test, because a hand-kept
    list of commands is exactly how a new command ends up undocumented for a whole session.
    """
    from app.controlbot import HELP, _ROUTES

    advertised = {f"/{name}" for name in re.findall(r"(?<![\w`])/(\w+)\b", HELP)}
    expected = {f"/{name}" for name in _ROUTES}
    assert advertised == expected, (
        f"advertised but not routed: {sorted(advertised - expected)}; "
        f"routed but undiscoverable: {sorted(expected - advertised)}"
    )
    # The four the operator actually uses day to day, so a deleted HELP block fails here too.
    assert {"/declare", "/source", "/status", "/pause"} <= advertised


# --------------------------------------------------------------------------- /source
# A source channel that is a shelf of bare files ("episode 7" + an mp4) has no series, no
# language and no season in any text to read. These tests are the one command that supplies
# those three facts, and the refusals that stop it from becoming a licence to guess.


async def test_source_shows_what_is_declared_before_changing_anything() -> None:
    db = FakeDb()
    control, _api, _db = bot(db=db)
    texts = await say(control, "/source @anime_uploads4u")
    assert "not declared" in texts[0] and "series" in texts[0]
    assert not db.writes, "a lookup must not write"


async def test_source_records_the_series_and_the_audio_together() -> None:
    db = FakeDb()
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/source @anime_uploads4u series Bleach audio hindi")
    assert "Bleach" in text and "hindi" in text
    row = db.source_channels[0]
    assert row["declared_series"] == "Bleach" and row["declared_audio"] == "hindi", row
    assert "channel_declaration" in text, "the reply must say whose statement this is"
    assert "next scan" in text and "left alone" in text, "and that it re-decides nothing itself"


async def test_source_keeps_a_series_name_with_spaces_in_one_value() -> None:
    db = FakeDb()
    control, _api, _db = bot(db=db)
    await say(control, "/source @anime_uploads4u series dekin no mogura audio dual")
    row = db.source_channels[0]
    assert row["declared_series"] == "dekin no mogura", row
    assert row["declared_audio"] == "dual", row


async def test_source_refuses_a_language_it_cannot_store() -> None:
    """A typo here is not cosmetic: 'Eng' stored in the column reads as *no declaration*,
    and the difference is whether four hundred files are archived or parked."""
    db = FakeDb()
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/source @anime_uploads4u audio english")
    assert "cannot record" in text.lower()
    assert not db.writes


async def test_source_refuses_a_season_number_that_could_not_be_a_season() -> None:
    db = FakeDb()
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/source @anime_uploads4u season 999")
    assert "between 0 and 99" in text and not db.writes


async def test_source_names_the_key_it_did_not_understand_instead_of_guessing() -> None:
    db = FakeDb()
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/source @anime_uploads4u language hindi")
    assert "language" in text and "usage" in text.lower()
    assert not db.writes


async def test_source_refuses_to_write_a_value_it_was_not_given() -> None:
    db = FakeDb()
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/source @anime_uploads4u series")
    assert "needs a value" in text and not db.writes


async def test_source_clears_every_declaration_at_once() -> None:
    db = FakeDb()
    control, _api, _db = bot(db=db)
    await say(control, "/source @anime_uploads4u series Bleach audio hindi season 2")
    db.writes.clear()
    (text,) = await say(control, "/source @anime_uploads4u clear")
    row = db.source_channels[0]
    assert row["declared_series"] == "" and row["declared_audio"] == "" and row["declared_season"] == -1, row
    assert "not declared" in text
    assert len(db.writes) == 1, "one statement, so the columns cannot be cleared one at a time"


async def test_source_refuses_to_pick_a_channel_when_the_name_is_ambiguous() -> None:
    db = FakeDb()
    db.source_channels.append(
        {
            "id": 4,
            "username": "anime_uploads4u",
            "title": "second row",
            "telegram_channel_id": -100999,
            "declared_series": "",
            "declared_audio": "",
            "declared_season": -1,
        }
    )
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/source @anime_uploads4u series Bleach")
    assert "matches 2 channels" in text and not db.writes


async def test_source_says_how_to_add_a_channel_it_does_not_know() -> None:
    """The reply has to be actionable from a phone: 'not found' alone sends the operator
    looking for a command that does not exist."""
    db = FakeDb()
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/source @never_configured series Bleach")
    assert "not a configured source channel" in text and "app.source_channel" in text
    assert not db.writes


async def test_source_finds_a_channel_by_its_numeric_id_too() -> None:
    db = FakeDb()
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/source -1001112223334 series Bleach")
    assert "declarations updated" in text, text
    assert db.source_channels[0]["declared_series"] == "Bleach"


async def test_status_groups_parked_files_by_the_reason_that_parked_them() -> None:
    """Four hundred files parked by one missing statement must not read as four hundred
    problems — and the fix is one line, so the reply says which line."""
    db = FakeDb()
    db.parked = [{"why": "cannot determine whether the file carries Hindi audio", "n": 402}]
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/status")
    assert "402" in text and "Hindi audio" in text
    assert "/source" in text, "and the answer, not just the symptom"


async def test_status_stays_quiet_about_parking_when_there_is_none() -> None:
    db = FakeDb()
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/status")
    assert "parked" not in text


async def test_source_without_a_database_says_so_instead_of_pretending() -> None:
    control = ControlBot(api=FakeApi(), db=None, settings=settings(), owner_ids=frozenset({OWNER}))  # type: ignore[arg-type]
    (text,) = await say(control, "/source @anime_uploads4u series Bleach")
    assert "database is not reachable" in text
    # and the usage text, with no arguments at all, needs no database either
    control2 = ControlBot(api=FakeApi(), db=None, settings=settings(), owner_ids=frozenset({OWNER}))  # type: ignore[arg-type]
    assert "usage" in (await say(control2, "/source"))[0].lower()


# ----------------------------------------------------------------------- /inplace
# The second publishing shape: the channel that is added is the channel we publish in, its
# files are already posted, and the only thing missing is the caption under them. These tests
# are the command that records that mode and shows the plan it implies — including the two
# replies that must not happen: a preview that writes, and an undo that rewrites messages.


def _inplace_row(message_id: int = 901, episode: int | None = 1, caption: str | None = "episode 1", **extra: Any) -> dict:
    row = {
        "message_id": message_id,
        "episode": episode,
        "caption": caption,
        "is_media": True,
        "audio_kind": "hindi",
        "languages": [],
        "title": "Bleach",
        "season": 1,
        "declared_first": 1,
        "declared_episodes": 12,
        "observed_first": 1,
        "observed_last": 12,
        "caption_previous": None,
        "unknown_label": "TBA",
    }
    row.update(extra)
    return row


async def test_inplace_explains_itself_before_touching_anything() -> None:
    db = FakeDb()
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/inplace")
    assert "usage" in text.lower() and "nothing is deleted" in text
    # The usage line has to keep the pipeline in the sentence. "no copying" read on its own
    # sounds like "no storing, no linking, no post", which is not what the mode means.
    assert "a destination channel is still created" in text, text
    assert not db.writes


async def test_inplace_shows_the_plan_and_writes_nothing_on_a_preview() -> None:
    db = FakeDb()
    db.inplace_rows = [
        _inplace_row(901, 1, "episode 1"),
        _inplace_row(902, 2, "episode 2 fixed, mirror link added"),
    ]
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/inplace @anime_uploads4u plan")
    assert "1 caption, 1 ask" in text, text
    assert "plan only" in text and not db.writes, "a preview that writes is not a preview"
    assert "msg 902" in text, "the question has to name the message it is about"
    assert "no new channel, no copy, no deletion" in text
    assert "unwired" in text, "the reply must not imply the edit has been sent"


async def test_inplace_records_the_mode_on_the_channel_when_no_destination_row_exists() -> None:
    """The channel row is where the answer has to live until a destination does.

    An in-place channel is often added before any destination exists for it — that is the whole
    point of the mode — and the audio gate is read per file, so a mode that only lived in
    app.destination would silently do nothing for weeks.
    """
    db = FakeDb()
    db.inplace_rows = [_inplace_row(901, 1)]
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/inplace @anime_uploads4u")
    assert "in-place captioning is ON" in text
    assert "no row in app.destination" in text
    assert db.source_channels[0]["publish_role"] == "source_and_destination", db.source_channels[0]
    assert len(db.writes) == 1, db.writes


async def test_inplace_switches_the_destination_row_when_there_is_one() -> None:
    db = FakeDb()
    db.destination = {
        "id": 6,
        "telegram_channel_id": -1001112223334,
        "title": "Bleach Anime in Hindi",
        "series_id": 7,
        "publish_mode": "link_post",
        "paired_source_channel_id": -1,
    }
    db.source_channels[0]["destination_id"] = 6
    db.inplace_rows = [_inplace_row(901, 1)]
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/inplace @anime_uploads4u")
    assert db.destination["publish_mode"] == "in_place_caption", db.destination
    assert "no row in app.destination" not in text, text
    assert "1 caption" in text
    assert "inplace:6:901" in text, "the reply must name the job a run would enqueue"


async def test_inplace_off_reverts_the_mode_and_leaves_the_posts_alone() -> None:
    """Undoing the mode is not undoing the work: an edited caption stays edited."""
    db = FakeDb()
    db.source_channels[0]["publish_role"] = "source_and_destination"
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/inplace @anime_uploads4u off")
    assert "link route again" in text
    assert "I did not change any message" in text
    assert db.source_channels[0]["publish_role"] == "source", db.source_channels[0]


async def test_inplace_paires_the_source_it_was_told_about() -> None:
    db = FakeDb()
    db.source_channels.append(
        {
            "id": 5,
            "username": "anime_backup_src",
            "title": "Bleach Source",
            "telegram_channel_id": -1005556667778,
            "declared_series": "",
            "declared_audio": "",
            "declared_season": -1,
        }
    )
    db.destination = {
        "id": 6,
        "telegram_channel_id": -1001112223334,
        "title": "Bleach Anime in Hindi",
        "series_id": 7,
        "publish_mode": "link_post",
        "paired_source_channel_id": -1,
    }
    db.inplace_rows = [_inplace_row(901, 1), _inplace_row(902, 2)]
    db.inplace_source = [1, 2, 3]  # the source has an episode this channel does not
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/inplace @anime_uploads4u from @anime_backup_src")
    assert "2 caption, 1 copy_then_caption" in text, text
    assert "1 only in the source" in text, text
    assert db.source_channels[1]["publish_role"] == "source", db.source_channels[1]
    assert db.destination["paired_source_channel_id"] == 5, db.destination


async def test_inplace_refuses_to_copy_on_a_suspected_renumbering() -> None:
    """Equal counts with no overlap is a numbering difference, not twelve missing files."""
    db = FakeDb()
    db.destination = {
        "id": 6,
        "telegram_channel_id": -1001112223334,
        "title": "Bleach",
        "series_id": 7,
        "publish_mode": "link_post",
        "paired_source_channel_id": -1,
    }
    db.source_channels.append(
        {
            "id": 5,
            "username": "anime_backup_src",
            "title": "Bleach Other",
            "telegram_channel_id": -1005556667778,
            "declared_series": "",
            "declared_audio": "",
            "declared_season": -1,
        }
    )
    db.inplace_rows = [_inplace_row(900 + n, n) for n in (1, 2, 3, 4)]
    db.inplace_source = [5, 6, 7, 8]
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/inplace @anime_uploads4u from @anime_backup_src plan")
    assert "shifted by +4" in text, text
    assert "copy_then_caption" not in text, text
    assert "nothing is copied until you confirm" in text or "renumbering" in text, text


async def test_inplace_honours_the_overwrite_knob_and_says_which_one_it_used() -> None:
    db = FakeDb()
    db.inplace_rows = [_inplace_row(901, 1, "episode 1 fixed, mirror link added")]
    control, _api, _db = bot(db=db)
    (asked,) = await say(control, "/inplace @anime_uploads4u plan")
    assert "1 ask" in asked and "inplace.overwrite_notes" in asked, asked

    db.config_rows["inplace.overwrite_notes"] = "replace"
    (replaced,) = await say(control, "/inplace @anime_uploads4u plan")
    assert "1 caption" in replaced, replaced
    assert "inplace.overwrite_notes" in replaced, "the reply has to name the knob that made it"


async def test_inplace_reports_a_post_it_would_only_skip() -> None:
    """Already carrying the approved box is the one outcome that must cost nothing.

    The test renders the caption with the real template rather than typing a lookalike: "is
    this message published?" is answered by comparing text, and a hand-typed box would pass the
    test while the production comparison failed on a single em-dash.
    """
    from app import inplace

    db = FakeDb()
    caption, missing = inplace.caption_for(
        title="Bleach", episode=1, audio_kind="hindi", declared_episodes=12
    )
    assert missing == ()
    db.inplace_rows = [_inplace_row(901, 1, caption, caption_previous="episode 1")]
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/inplace @anime_uploads4u plan")
    assert "1 skip" in text, text
    assert not db.writes, "a plan with nothing to do must not write even the mode twice"


async def test_inplace_names_a_flag_it_did_not_understand() -> None:
    db = FakeDb()
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/inplace @anime_uploads4u delete")
    assert "did not understand" in text and "usage" in text.lower()
    assert not db.writes


async def test_inplace_refuses_to_pick_between_two_channels_of_one_name() -> None:
    db = FakeDb()
    db.source_channels.append(
        {
            "id": 9,
            "username": "anime_uploads4u",
            "title": "second row",
            "telegram_channel_id": -100777,
            "declared_series": "",
            "declared_audio": "",
            "declared_season": -1,
        }
    )
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/inplace @anime_uploads4u")
    assert "matches 2 channels" in text and not db.writes


async def test_inplace_refuses_a_series_it_cannot_name_yet_and_says_what_it_needs() -> None:
    """No declared series means no destination name, and a placeholder is not a name.

    `Untitled Series Anime in Hindi` is a channel title somebody could create by accident, so the
    refusal says the name is what is missing — and puts `/source` in front of creation, in that
    order, because the order is the fix.
    """
    db = FakeDb()
    db.source_channels[0]["we_are_admin"] = False
    db.inplace_rows = [_inplace_row(901, 1)]
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/inplace @anime_uploads4u")
    assert "a destination channel is created" in text, text
    assert "needs the series named first" in text, text
    assert "Untitled" not in text, "no placeholder may reach a reply that doubles as an instruction"


async def test_inplace_refuses_a_member_only_channel_and_says_what_happens_instead() -> None:
    """The correction, in one reply: no rights here ⇒ this is a source, and a destination is built.

    The old answer was "make me admin and ask again", which reads like a permission problem and
    hides the real one: the channel they named was never meant to be written to. Being able to
    caption in place must never become a reason to skip creating the destination.
    """
    db = FakeDb()
    db.source_channels[0]["we_are_admin"] = False
    db.source_channels[0]["declared_series"] = "Bleach"
    db.inplace_rows = [_inplace_row(901, 1)]
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/inplace @anime_uploads4u")
    assert "I did not switch this channel to in-place mode" in text, text
    assert "this channel stays a source" in text and "is created" in text, text
    assert "Anime in Hindi" in text, "the reply has to name what is going to be created"
    assert "skip" in text and "/source" in text, "and say creation is not skipped, plus how to name it"
    assert not db.writes, "a refusal writes nothing"


async def test_inplace_off_is_never_blocked_by_a_rights_check() -> None:
    """Leaving the mode is a step that asks nothing of the channel, so it is always allowed."""
    db = FakeDb()
    db.source_channels[0]["we_are_admin"] = None
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/inplace @anime_uploads4u off")
    assert "link route again" in text, text
    assert len(db.writes) == 1, db.writes


async def test_inplace_records_the_mode_for_a_channel_nobody_has_scanned_yet() -> None:
    """The mode is a setting; the plan is a reading. Only the second one needs messages.

    A channel added before its first scan is the normal case, so refusing to record anything
    until a scan has run would make the setup order matter for no reason — and inventing a count
    of zero-captioned files would be worse.
    """
    db = FakeDb()
    db.inplace_rows = []
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/inplace @anime_uploads4u")
    assert "no plan to show" in text and "next scan" in text, text
    assert db.source_channels[0]["publish_role"] == "source_and_destination", db.source_channels[0]


async def test_a_channel_of_text_posts_is_still_recorded_and_its_posts_left_alone() -> None:
    """Nothing to caption today, no refusal: the mode is a setting, and text messages stay text."""
    db = FakeDb()
    db.destination = {
        "id": 6,
        "telegram_channel_id": -1001112223334,
        "title": "Bleach Anime in Hindi",
        "series_id": 7,
        "publish_mode": "link_post",
        "paired_source_channel_id": -1,
    }
    db.source_channels[0]["destination_id"] = 6
    db.inplace_rows = [_inplace_row(901, 1, "welcome", is_media=False)]
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/inplace @anime_uploads4u plan")
    assert "1 ignore" in text, text
    assert "1 text message" not in text  # the summary names the action, not a count of messages
    assert db.destination["publish_mode"] == "link_post", "a plan preview must not flip the mode"
