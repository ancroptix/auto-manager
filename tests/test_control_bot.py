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
        if "app.job" in sql:
            return list(self.blocked)
        return []

    async def fetchrow(self, sql: str, *args: Any):
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

    async def execute(self, sql: str, *args: Any) -> int:
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


def test_the_help_text_advertises_exactly_the_routed_commands() -> None:
    """Guard against the router and the help text drifting apart in either
    direction: an undocumented command is undiscoverable, a documented non-command
    is a lie."""
    from app.controlbot import HELP

    advertised = {f"/{name}" for name in re.findall(r"(?<![\w`])/(\w+)\b", HELP)}
    assert advertised == {
        "/start",
        "/help",
        "/status",
        "/pause",
        "/resume",
        "/reconcile",
        "/probe",
        "/sessions",
        "/use",
        "/forget",
        "/login",
        "/code",
        "/password",
        "/cancel",
    }, advertised ^ {"/start", "/help", "/status", "/pause", "/resume", "/reconcile", "/probe", "/sessions", "/use", "/forget", "/login", "/code", "/password", "/cancel"}
