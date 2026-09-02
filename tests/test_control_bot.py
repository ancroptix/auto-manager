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

from app import discover
from app.botapi import BotApi, BotTokenError, parse_update, redact
from app.config import Settings
from app.controlbot import MAX_CODE_TRIES, ControlBot, LoginResult, LoginUnstored, NeedsPassword, Reply
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
    #: What each send carried as `reply_markup`, in order: a test for a button is a test that the
    #: button exists *and* that it says the command the operator would otherwise have typed.
    markups: list[Any] = field(default_factory=list)
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

    async def send(
        self, chat_id: int, text: str, *, reply_to=None, parse_mode=None, markup=None
    ) -> int:
        self.sent.append((chat_id, text))
        self.markups.append(markup)
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
        self.rows_changed: int | None = None
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
        # /archive: the private master archive list, empty by default because that is the state the
        # archive job blocks in, and the one the command's reply has to describe rather than guess.
        self.archive_channels: list[dict] = []
        # /inplace: the destination row for the channel being switched, the messages it would
        # edit, and the episode numbers of the channel it is compared against.
        self.destination: dict | None = None
        # /card, /sticker and /campaign: one destination and one drafted campaign, shaped like the
        # rows those queries select, so the commands can be tested on their parsing and their
        # promises rather than on a database they do not need.
        self.destinations: list[dict] = [
            {
                "id": 21,
                "title": "Dekin no mogura Anime in Hindi",
                "telegram_channel_id": -1001234,
                "publish_mode": "link_post",
                "card_message_id": None,
                "announcement_link": None,
                "announcement_link_at": None,
                "series": "Dekin no mogura",
                "source_username": "bleach_hindi",
            }
        ]
        self.campaign: dict | None = {
            "id": 9,
            "name": "wave1",
            "status": "draft",
            "message_template": "welcome {name}",
            "rate_per_hour": 20,
            "confirm_required": True,
        }
        self.seasons: list[dict] = []
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
            if not args:
                return [dict(row) for row in self.series]
            needle = str(args[0] or "")
            hits = [row for row in self.series if needle and needle in row["normalized_title"]]
            return hits or ([] if needle else [dict(row) for row in self.series])
        if "from app.destination order by id" in sql:
            # `app/discover.py`'s sweep: the whole list, no alias, because it is matching channels rather
            # than addressing one. Without this arm the sweep would see "no destinations" and offer rows for
            # channels that are already destinations.
            return [dict(row) for row in self.destinations]
        if "from app.destination d" in sql:  # /card and /campaign name a channel three ways
            if "where d.id = $1" in sql:
                # The console resolves a tap's row id, and `/destination` re-reads the row after a write, so
                # a fake that could not answer this would hide a real fault in the newest half of the bot.
                return [dict(row) for row in self.destinations if row.get("id") == args[0]]
            if not args:
                return [dict(row) for row in self.destinations]  # /card's bare listing
            numeric, needle = args[0], str(args[1] or "").casefold()
            # `#21` addresses the row by its own number, which is what a button knows: the real query ORs
            # that third predicate in, and a fake that ignored it would answer "no such channel" to every
            # tap that came from the console.
            by_row = args[2] if len(args) > 2 else None

            def _hit(row: dict) -> bool:
                if by_row is not None and row.get("id") == by_row:
                    return True
                if numeric is not None and row.get("telegram_channel_id") == numeric:
                    return True
                if not needle:
                    return False
                return needle in {
                    str(row.get("title") or "").casefold(),
                    str(row.get("source_username") or "").casefold(),
                    str(row.get("series") or "").casefold(),
                }

            return [dict(row) for row in self.destinations if _hit(row)]
        if "as is_media" in sql:  # /inplace's plan query
            return [dict(row) for row in self.inplace_rows]
        if "select distinct episode_number" in sql:
            return [{"episode_number": number} for number in self.inplace_source]
        if "from app.source_candidate" in sql:
            return list(self.parked)
        if "from app.archive_channel" in sql:
            return [dict(row) for row in self.archive_channels]
        if "from app.source_channel" in sql:
            if "where id = $1" in sql:
                # The console resolves a tap's row id to a handle, because a button carries the id and a
                # command takes the handle. A fake that could not answer that hides a real fault.
                return [dict(row) for row in self.source_channels if row["id"] == args[0]]
            if not args:
                # The list screen asks for every row and nothing else.
                return [dict(row) for row in self.source_channels]
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
        if "update app.join_campaign_contact set" in sql:
            # The release, read the only way it can be read: `update … returning`, whose rows are the count.
            # The fake applies the write as well, because a reply that says "3 released" over a statement that
            # changed nothing is the sentence an operator would tap twice.
            self.writes.append((sql, args))
            count = (
                int((self.campaign or {}).get("unreleased") or 0)
                if self.rows_changed is None
                else int(self.rows_changed)
            )
            if self.campaign is not None:
                self.campaign["unreleased"] = 0
            return [{"telegram_user_id": 900 + one} for one in range(count)]
        return []

    async def fetchrow(self, sql: str, *args: Any):
        if "sticker_source_chat_id" in sql:  # /sticker's season lookup
            needle = str(args[0] or "").casefold()
            hit = next(
                (
                    row
                    for row in self.seasons
                    if str(row.get("title") or "").casefold() == needle and row.get("season_number") == args[1]
                ),
                None,
            )
            return dict(hit) if hit else None
        if "select telegram_channel_id from app.source_channel" in sql:
            return None
        if "from app.join_campaign" in sql:
            return dict(self.campaign) if self.campaign else None
        if "telegram_channel_id = $1" in sql:
            # asyncpg refuses to bind a str to a bigint column, and every fake in this file used to accept
            # it. A lookup that "works" against a lenient fake is how `int` vs `str` shipped to the operator
            # as a crash, so the fakes are now as strict as the driver about the one argument the app most
            # often gets wrong.
            if not isinstance(args[0], int):
                raise RuntimeError(f"fetchrow: {args[0]!r} cannot be bound to telegram_channel_id")
        if "from app.destination where telegram_channel_id" in sql:
            # A pairing looks its rows back up by channel number, because `insert_destination` answers None
            # when the row was already there and the link still has to be made. Without this arm the generic
            # id below would let it link everything to row 42.
            hit = next(
                (row for row in self.destinations if str(row.get("telegram_channel_id")) == str(args[0])), None
            )
            return {"id": hit["id"]} if hit else None
        if "select mode from app.source_channel where id" in sql:
            hit = next((row for row in self.source_channels if row.get("id") == args[0]), None)
            return {"mode": hit.get("mode")} if hit else None
        if "from app.source_channel where telegram_channel_id" in sql:
            hit = next(
                (row for row in self.source_channels if str(row.get("telegram_channel_id")) == str(args[0])), None
            )
            return {"id": hit["id"]} if hit else None
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
        if "join_request_campaign" in sql or "campaign:" in str(args):
            # A campaign has no `app.job` row to ask about: sending is a loop over `app.join_campaign`, so
            # any read that mentions the old shape is the queue design creeping back. The fake refuses it
            # loudly, because an answer of `None` here would let a leftover path look like a working one.
            raise AssertionError(f"a campaign touched app.job again: {sql[:90]}")
        return {"id": 42}

    @staticmethod
    def _binding_is_int(sql: str, args: tuple) -> None:
        """Every channel id the fake is asked to store has to be an integer, like the column is.

        The insert statements name their columns, so the check reads the id's position out of the SQL rather
        than trusting the caller: a writer that starts passing `str(id)` would otherwise plan a row the
        production database refuses, and the test for it would pass.
        """
        match = re.search(r"insert into app\.\w+ \((.*?)\)", sql, re.S)
        if not match or "telegram_channel_id" not in match.group(1):
            return
        names = [one.strip() for one in match.group(1).split(",")]
        index = names.index("telegram_channel_id")
        if index < len(args) and not isinstance(args[index], int):
            raise RuntimeError(f"{args[index]!r} cannot be bound to telegram_channel_id")

    async def fetchval(self, sql: str, *args: Any):
        if "insert into app.archive_channel" in sql:
            names = [
                name.strip()
                for name in re.search(r"insert into app\.archive_channel \((.*?)\)", sql, re.S)
                .group(1)
                .split(",")
                if name.strip() and "_at" not in name
            ]
            row = {"id": 950 + len(self.archive_channels)}
            row.update(dict(zip(names, args)))
            self.archive_channels.append(row)
            self.writes.append((sql, args))
            return row["id"]
        self._binding_is_int(sql, tuple(args))
        if "insert into app.series" in sql:
            # `app/ingest.ensure_series`, and the only arm that may answer for a series row: the upsert's
            # `on conflict` means a real database hands back the existing id, so a name already stored must
            # not add a second row here or a pairing test could pass against a table the app never writes.
            existing = next((row for row in self.series if str(row.get("title")) == str(args[0])), None)
            if existing is not None:
                return existing["id"]
            row = {"id": 70 + len(self.series), "title": args[0]}
            self.series.append(row)
            self.writes.append((sql, args))
            return row["id"]
        if "insert into app.destination" in sql:
            # Discovery filing a channel we administer as a series' destination. Same shape as the source
            # insert: the column list from the statement, zipped onto the arguments.
            names = [
                name.strip()
                for name in re.search(r"insert into app\.destination \((.*?)\)", sql, re.S)
                .group(1)
                .split(",")
                if name.strip() and "_at" not in name
            ]
            row = {"id": 920 + len(self.destinations), "series": next(
                (one["title"] for one in self.series if one["id"] == (args[0] if args else None)), None
            )}
            row.update(dict(zip(names, args)))
            self.destinations.append(row)
            self.writes.append((sql, args))
            return row["id"]
        if "insert into app.source_channel" in sql:
            # Mirror the row the way the table would: the column list in the statement, zipped onto
            # the arguments, so a test reads back `mode` and not a string it has to squint at.
            import re as _re

            names = [
                name.strip()
                for name in _re.search(r"insert into app.source_channel \((.*?)\)", sql, _re.S)
                .group(1)
                .split(",")
                if name.strip() and "_at" not in name
            ]
            row = {
                "id": 900 + len(self.source_channels),
                "declared_series": "",
                "declared_audio": "",
                "declared_season": -1,
                "we_are_admin": None,
            }
            row.update(dict(zip(names, args)))
            self.source_channels.append(row)
            self.writes.append((sql, args))
            return row["id"]
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
            if "set card_message_id = $2" in sql or "card_message_id = null" in sql:
                # The console re-reads the row after a write, so a fake that let the write vanish would test
                # the screen against a lie — and the card line on that screen is the whole point of `/card`.
                target = args[0]
                value = args[1] if "set card_message_id = $2" in sql else None
                for row in ([self.destination] if self.destination is not None else []) + list(
                    self.destinations
                ):
                    if row.get("id") == target:
                        row["card_message_id"] = value
                        if value is None:
                            row["announcement_link"] = None  # `/card clear` nulls both, in one statement
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
            if not columns:
                # A switch: one named column, one value. `/source <ch> gate off` writes exactly that
                # shape, and the fake that ignores it would let a test pass on a command that wrote
                # nothing at all.
                single = _re.search(r"set (\w+) = \$", sql)
                if single is not None:
                    row[single.group(1)] = args[1]
                    self.writes.append((sql, args))
                return 1
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
        if "update app.join_campaign_contact set" in sql:
            # The release is read back from `fetch … returning`, never from this call: asyncpg answers
            # `execute` with the statement's status tag, and a fake that returned a neat `2` here is what let
            # `int(await db.execute(…))` pass every test while raising `ValueError: invalid literal for int()
            # with base 10: 'UPDATE 0'` on the operator's own ✅ tap. A fake must not be nicer than the driver.
            raise AssertionError(
                "a campaign release may not be counted from `execute` — see app.writers.campaign_release_unsent"
            )
        if "update app.join_campaign set" in sql:
            # /campaign's state changes, mirrored onto the fake row so a test can read the status back
            # instead of pattern-matching the SQL it was written with. Both spellings the commands use: a
            # literal (`status = 'ready'`) and a cast parameter (`status = $2::app.campaign_status`, which
            # `/campaign pause` writes because one statement sets the status *and* the finished_at flag).
            # A fake that read only the literal would let a pause that never parses look like a pass.
            status = re.search(r"status = '([a-z_]+)'", sql)
            value = status.group(1) if status else None
            if value is None and "::app.campaign_status" in sql and len(args) > 1:
                value = str(args[1])
            if value is not None and self.campaign is not None:
                self.campaign["status"] = value
            if "message_template = $" in sql and self.campaign is not None and args:
                self.campaign["message_template"] = str(args[-1])
            if "per_message_delay_seconds = $" in sql and self.campaign is not None and len(args) > 1:
                # `gap` writes the spacing on the row, and the next read has to say the new number: the fake
                # mirrors it so a test asserts on the row rather than on the SQL string.
                self.campaign["per_message_delay_seconds"] = float(args[1])
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
        # Every queue write in the product lands here, so `db.queued` is the one place a test can check that
        # starting a campaign wrote nothing: the list has to stay empty for the loop design to be true.
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
        if self.error == "unstored":
            # The account is signed in; only the session string failed to materialise. The transport's
            # own tests cover *why*; this covers what the operator is told about it.
            raise LoginUnstored(
                "the account answered and the credentials were accepted, but this service could not read "
                "a session string out of it (RuntimeError: StringSession produced no session string to "
                "store). The login may be live on the account without being stored here — open Telegram "
                "→ Settings → Privacy and Security → Devices, terminate the auto-manager session, then "
                "run /login again"
            )
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


@pytest.mark.asyncio
async def test_card_records_the_post_the_announcement_is_built_from(make_settings) -> None:
    """`/card` writes one number per destination and says plainly that it wrote only that.

    The card is the whole difference between an announcement that carries a shareable link and one
    that carries the channel invite — which is the thing that gets an invite revoked. So the promise
    under test is twofold: the row is updated, and nothing was sent. Replies are read off the command
    itself, because a command that answers the owner is not the same act as one that posts anywhere.
    """
    db = FakeDb()
    control = ControlBot(
        api=FakeApi(), db=db, settings=make_settings(), owner_ids=frozenset({OWNER})  # type: ignore[arg-type]
    )

    def text_of(replies) -> str:
        return "\n".join(reply.text for reply in replies)

    first = text_of(await control._card(None, ["-1001234", "42"]))
    assert "message 42" in first, first
    assert db.writes, "the card message id was never written"
    sql, args = db.writes[-1]
    assert "card_message_id = $2" in sql and args == (21, 42), (sql, args)

    shown = text_of(await control._card(None, ["-1001234", "show"]))
    assert "card message 42" in shown, (
        "show reads the row back — and the fake now applies the write, so a read that disagreed with the "
        "database would be caught here rather than on a screen"
    )

    listed = text_of(await control._card(None, []))
    assert "Dekin no mogura" in listed, "the bare command lists what exists"

    await control._card(None, ["-1001234", "clear"])
    clear_sql = db.writes[-1][0]
    assert "card_message_id = null" in clear_sql, clear_sql

    unknown = text_of(await control._card(None, ["@nope_here", "42"]))
    assert "matches no destination" in unknown, unknown


@pytest.mark.asyncio
async def test_a_campaign_needs_the_code_that_the_plan_printed(make_settings) -> None:
    """`/campaign … confirm` refuses without the code, and switches the campaign on with it.

    The code is derived from the campaign row and its exact text, so the test asserts the mismatch
    first (nothing queued) and then the match (the job is queued, status ready). A campaign that could
    be started by typing a name is a campaign that gets started by accident.
    """
    from app import joinmsg

    db = FakeDb()
    control = ControlBot(
        api=FakeApi(), db=db, settings=make_settings(), owner_ids=frozenset({OWNER})  # type: ignore[arg-type]
    )

    def text_of(replies) -> str:
        return "\n".join(reply.text for reply in replies)

    wrong = text_of(await control._campaign(None, ["-1001234", "confirm", "wave1", "0000"]))
    assert db.queued == [], "a wrong code must not queue anything"
    assert "not the code" in wrong, wrong
    assert "not tell you the code" in wrong, "and it must not simply print the real one"

    planned = text_of(await control._campaign(None, ["-1001234", "plan", "wave1"]))
    assert "one message every 3 seconds" in planned, planned
    code = joinmsg.confirm_code(9, "welcome {name}")
    assert code in planned, "the plan prints the code it will accept"

    confirmed = text_of(await control._campaign(None, ["-1001234", "confirm", "wave1", code.lower()]))
    assert "ready" in confirmed, confirmed
    assert db.queued == [], "starting a campaign writes no job row — the loop reads the campaign table"
    assert db.campaign["status"] == "ready", db.campaign
    assert any("status = 'ready'" in sql for sql, _ in db.writes), db.writes


@pytest.mark.asyncio
async def test_the_sticker_command_needs_a_season_that_exists(make_settings) -> None:
    """`/sticker` refuses to invent a season, and refuses a peer it cannot name.

    Both refusals are the interesting half of this command: the write itself is a forward, and the
    judgement about *which* sticker opens a season is entirely the operator's.
    """
    db = FakeDb()
    db.seasons = []
    control = ControlBot(
        api=FakeApi(), db=db, settings=make_settings(), owner_ids=frozenset({OWNER})  # type: ignore[arg-type]
    )

    def text_of(replies) -> str:
        return "\n".join(reply.text for reply in replies)

    usage = text_of(await control._sticker(None, []))
    assert "usage: /sticker" in usage, usage

    no_season = text_of(await control._sticker(None, ["Dekin", "no", "mogura", "2", "from", "@x", "88"]))
    assert "no season 2" in no_season, no_season
    assert "/declare" in no_season, "and say where a season comes from"

    bad_shape = text_of(await control._sticker(None, ["Dekin", "2", "@x", "88"]))
    assert "not a sticker address" in bad_shape, bad_shape
    assert db.writes == [], "a refused command writes nothing"


def test_the_three_write_commands_are_reachable_and_documented() -> None:
    """The commands exist in the table, in the help text and in the refusal sentences.

    `app/writers.py` names `/card`, `/sticker` and `/campaign` in the reason each blocked job shows, so
    a command that drifted out of the registry would leave the operator with an instruction to run
    something that answers `unknown command`. That is what this guards, and it costs nothing.
    """
    from pathlib import Path

    from app.controlbot import HELP, _ROUTES

    for name in ("card", "sticker", "campaign"):
        assert name in _ROUTES, f"/{name} is not wired"
        assert f"/{name}" in HELP, f"/{name} is wired but nobody will find it"

    doc = (Path(__file__).resolve().parents[1] / "docs" / "control-bot.md").read_text(encoding="utf-8")
    for name in ("card", "sticker", "campaign"):
        assert f"`/{name}" in doc, f"docs/control-bot.md does not document /{name}"


# --------------------------------------------------------------------- what the operator can actually read

@pytest.mark.asyncio
async def test_the_login_flow_never_eats_its_own_messages() -> None:
    """Every prompt stays in the chat; only the operator's spent secrets are deleted.

    The first live login deleted the "code sent" line and the 2FA question the instant it sent them,
    which looked like a bot that had crashed. A reply of ours carries no secret — the number is masked
    and everything is scrubbed — so there is nothing in it to hide, and a question that erases itself
    is worse than useless mid-flow.
    """
    transport = FakeTransport(require_password=True)
    control, api, db = bot(transport=transport)

    await control.dispatch(update(f"/login spare {PHONE}", message_id=11))  # the number
    await control.dispatch(update("/code 482913", message_id=12))  # the code
    await control.dispatch(update("/password hunter2", message_id=13))  # the 2FA password

    deleted = [mid for _, ids in api.deleted for mid in ids]
    assert deleted == [11, 12, 13], "the three secrets, in order, and nothing else"
    assert all(mid < 500 for mid in deleted), "FakeApi numbers our replies from 500 up: ours were touched"
    texts = "\n".join(text for _, text in api.sent)
    assert "code sent to" in texts, "the instruction that says what to type next must survive"
    assert "2FA" in texts
    assert PHONE not in texts and "482913" not in texts and "hunter2" not in texts


@pytest.mark.asyncio
async def test_a_broken_database_answers_instead_of_going_silent() -> None:
    """A command whose query raises has to reply with the reason.

    Silence is the failure that was hardest to diagnose from the outside: /sessions looked ignored,
    and what had actually happened was a connection the container could not open.
    """

    class Unreachable:
        async def fetch(self, sql, *args):
            raise RuntimeError("connection refused")

        fetchrow = fetch
        execute = fetch

        connected = False

        async def config(self, key, default=None):
            return default

    control, api, _ = bot(db=Unreachable())
    replies = await control.dispatch(update("/sessions", message_id=31))

    assert "could not finish" in replies[0].text.lower()
    assert "5432" in replies[0].text, "the sentence has to name the thing they can go and change"
    assert api.sent, "the operator must see it, not just the log"
    assert 31 not in [mid for _, ids in api.deleted for mid in ids], "nothing was spent, nothing is deleted"


@pytest.mark.asyncio
async def test_a_stored_session_is_handed_to_the_thing_that_writes() -> None:
    """Otherwise a login needs a redeploy before the queue can use it, and that is not stated anywhere."""
    adopted: list[str] = []

    async def on_stored():
        adopted.append("session")
        return "the service reconnected with it, so the write jobs can run"

    transport = FakeTransport()
    control, api, db = bot(transport=transport, on_session_stored=on_stored)
    await say(control, f"/login spare {PHONE}")
    await control.dispatch(update("482913", message_id=14))

    assert adopted == ["session"]
    # The reply waits for the hand-off and repeats its sentence, because "stored" and "in use" are two
    # different claims and the operator has to be told which one happened.
    assert "the service reconnected with it" in api.sent[-1][1], api.sent[-1]


@pytest.mark.asyncio
async def test_without_a_writer_the_success_line_says_nothing_writes_yet() -> None:
    transport = FakeTransport()
    control, api, db = bot(transport=transport)
    await say(control, f"/login spare {PHONE}")
    await control.dispatch(update("482913", message_id=14))

    assert "no writer to hand it to" in api.sent[-1][1], "no hook means no hand-off, and the reply must not imply one"


@pytest.mark.asyncio
async def test_a_failed_hand_off_does_not_undo_the_login() -> None:
    """The stored session is the success; a writer that cannot take it yet is a footnote, not a failure."""

    async def broken():
        raise RuntimeError("session string is not authorized on this account")

    transport = FakeTransport()
    control, api, db = bot(transport=transport, on_session_stored=broken)
    await say(control, f"/login spare {PHONE}")
    await control.dispatch(update("482913", message_id=14))

    text = api.sent[-1][1]
    assert "stored as 'spare'" in text and "could not start using it yet" in text, text
    assert PHONE not in text


@pytest.mark.asyncio
async def test_a_login_that_cannot_be_stored_is_not_reported_as_a_bad_code() -> None:
    """The worst possible mix-up: Telegram said yes, and the chat says "try the code again".

    That is what happened on the operator's account when the session string was read under a name this
    Telethon does not have. The flow must close (the code is spent), the account must be told where to
    look for the stray session, and nothing may be recorded as stored.
    """
    transport = FakeTransport(error="unstored")
    control, api, db = bot(transport=transport)
    await say(control, f"/login spare {PHONE}")

    replies = await control.dispatch(update("482913", message_id=44))
    text = "\n".join(reply.text for reply in replies)

    assert "credentials were accepted" in text, text
    assert "Devices" in text, "the sentence has to name where a stray session can be ended"
    assert "Nothing was stored" in text
    assert "attempt" not in text and "sign-in failed" not in text, "this is not a code problem"
    assert control.pending == {}, "the flow closes: there is nothing left to retry here"
    assert db.stored == []
    assert 44 in [mid for _, ids in api.deleted for mid in ids], "the spent code still goes"


@pytest.mark.asyncio
async def test_the_probe_refusal_teaches_the_safe_order_instead_of_naming_a_mode() -> None:
    """"Set APP_MODE=live" alone is a trap, because going live also starts the queue.

    The operator wants to check the two bots *before* trusting this service with their channels, and the
    probe is the only way to do that. So the refusal has to carry the half that makes the answer safe —
    the worker switch — rather than send them to flip a mode and watch announcements start.
    """
    shadow, _, _ = bot()
    (text,) = await say(shadow, "/probe")
    assert "needs a live user session" in text, "the pinned wording: it is the session that is missing"
    assert "APP_MODE=live" in text and "WORKER_ENABLED=false" in text, text
    assert "nothing can reach your channels" in text, "the sentence that says why the second half matters"
    assert "does send" in text or "It opens the two bots" in text, "the probe is not a free read"

    # The other sentence is for a deployment whose session source moved after boot: live, keys
    # present, nothing left to send from. Settings refuse to start that way, so it can only arrive by
    # the environment changing underneath a running service -- which is exactly when a bare "set
    # APP_MODE=live" would send the operator in circles.
    moved, _, _ = bot()
    from app.config import AppMode

    moved.settings.mode = AppMode.LIVE
    moved.settings.telegram_session_source = "env"
    moved.settings.telegram_session_string = None
    (text2,) = await say(moved, "/probe")
    assert "needs an open user session" in text2, text2
    assert "WORKER_ENABLED" not in text2, "no advice about the queue when the queue is not the problem"
    assert "/status" in text2, "it must still point at the one thing that can answer which half moved"


# ------------------------------------------------------- the probe report, as the operator reads it
def test_a_probe_report_is_the_message_the_probe_wrote_for_a_human() -> None:
    """Repr dumps are not a report, and this is the one reply the operator pastes back.

    The first live ``/probe`` answered with ``account: {'id': …, 'username': 'Turvei', 'restricted'…`` —
    every field cut at 300 characters, with the human summary last, where the transport then ate it.
    Both halves of that are pinned here: use the rendered text, and if there is none, *render* it.
    """
    from app.controlbot import format_report_text

    human = "auto-manager · protocol probe\n\nstorage bot: @anime_hindifilesbot\n  says: press Start"
    assert format_report_text({"account": {"id": 1}, "steps": [{"step": "account"}], "report": human}) == human

    rendered = format_report_text({"account": {"id": 1, "username": "Turvei"}, "storage_bot": {}, "channel_help": {}})
    assert "auto-manager · protocol probe" in rendered
    assert "{'" not in rendered and "dialog_count" not in rendered, "no dictionary in a human's chat"
    assert format_report_text(None) == "None", "a report that is not a dict is still answered, not crashed"


@pytest.mark.asyncio
async def test_the_report_sent_to_the_operator_is_readable_from_first_line_to_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What ``/probe`` puts in the chat: the report, whole, no machine keys in front of it."""
    from app import telegram_client

    human = "auto-manager · protocol probe\n\naccount: @Turvei id=8992934034\n  dialogs=46 channels I own=5"

    async def fake_probe_once(settings, db=None, *, send=True):  # noqa: ANN001, ARG001
        assert send is False, "the bot writes the report into this chat; the probe must not also message the owner"
        return {"account": {"id": 8992934034, "username": "Turvei"}, "report": human, "messages_sent": 3}

    monkeypatch.setattr(telegram_client, "probe_once", fake_probe_once)
    control, api, _db = bot()
    await control._probe_task(OWNER)  # noqa: SLF001

    assert [text for _chat, text in api.sent] == [human], api.sent
    joined = api.texts
    assert "messages_sent" in joined or "dialogs=46" in joined
    assert "'username':" not in joined and "reply_chars" not in joined, "the repr path is back"


@pytest.mark.asyncio
async def test_a_reply_over_telegrams_limit_arrives_in_pieces_instead_of_cut_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"Half a message, silently" is the failure mode of every long answer this bot writes.

    ``/status``, a probe report, a blocked-jobs list — all of them can outgrow 4096 characters, and the
    transport used to slice. Splitting is safe in a private chat (it is not a published post, where
    ``sender.MAX_MESSAGE_CHARS`` refuses instead and should); losing the tail is not.
    """
    from app.botapi import BotApi as _BotApi, split_for_chat

    body = "\n".join(f"line {i} " + "x" * 60 for i in range(120))
    assert len(body) > 4096
    assert "\n".join(split_for_chat(body)) == body, "split on lines, lose nothing"
    assert len("".join(split_for_chat("y" * 10_001))) == 10_001, "even one absurd line survives whole"

    api = _BotApi(TOKEN)
    calls: list[dict[str, Any]] = []

    async def fake_call(method: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"method": method, **kwargs})
        return {"result": {"message_id": len(calls)}}

    monkeypatch.setattr(api, "_call", fake_call)

    first = await api.send(OWNER, body)
    assert first == 1, "the id of the first part is the handle a caller deletes by"
    assert [call["method"] for call in calls] == ["sendMessage"] * 3, calls
    assert all(len(call["text"]) <= 4096 for call in calls)
    assert "\n".join(call["text"] for call in calls) == body

    calls.clear()
    assert await api.send(OWNER, "") is None and calls == [], "an empty reply is no reply at all"
    await api.send(OWNER, "one line")
    assert len(calls) == 1 and calls[0]["text"] == "one line", "short answers are untouched by this"


@pytest.mark.asyncio
async def test_a_missing_source_channel_is_answered_with_the_command_that_adds_it() -> None:
    """The refusal the operator hit, and where it sends them now.

    `/source -1002575861262 …` used to answer "the row itself is created in the dashboard table
    app.source_channel — I can read and update it, not create it": true, and no use to someone on a
    phone. The bot can write that row, and the operator said in one sentence why the dashboard was not
    the answer (*"mai baar baar supabase nhi kholne wala"*), so the refusal's first line is the command
    that works and the dashboard is the fallback for whoever wants more than the defaults.
    """
    control, _api, db = bot()
    (text,) = await say(control, "/source -1002575861262 series Bleach")

    assert "/source -1002575861262 add" in text, "the next click, in the first breath"
    assert "Table editor" in text and "app.source_channel" in text, "the fallback path, still named"
    assert "telegram_channel_id" in text, "and the one column that table needs, still by name"
    assert "the decision to start reading a channel" in text, "why it stays its own command"
    assert db.queued == [] and db.writes == [], "a refusal writes nothing and queues nothing"


# --------------------------------------------------------------------------- /source add
# The row itself, from the chat window: what the operator asked for on 2026-08-29, and the one write in
# this command that changes what the service reads rather than what it says about a file.


@pytest.mark.asyncio
async def test_add_writes_the_row_and_prints_the_defaults_it_chose() -> None:
    db = FakeDb()
    db.source_channels = []
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/source -1002575861262 add series Bleach")

    assert "watching it" in text, text[:120]
    row = db.source_channels[0]
    assert row["telegram_channel_id"] == -1002575861262
    assert row["mode"] == "full" and row["active"] is True
    assert row["require_hindi_audio"] is True, "the gate is on until the operator switches it off"
    assert row["include_subbed"] is False, "subbed-only files are not in scope by default"
    assert row["declared_series"] == "Bleach"
    assert "priority" not in row and "is_joined" not in row, "only columns something reads are named"
    assert "not checked against Telegram" in text, "a number nobody looked up is said, not smoothed over"
    assert "nothing was deleted" in text


@pytest.mark.asyncio
async def test_add_never_configures_one_channel_twice() -> None:
    db = FakeDb()
    before = [dict(row) for row in db.source_channels]
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/source @anime_uploads4u add")

    assert "already configured" in text, "and it says so instead of adding a second source"
    assert db.source_channels == before, "nothing written, nothing changed"
    assert "gate" in text and "watch" in text, "the row it found is shown with its switches"


@pytest.mark.asyncio
async def test_a_switch_writes_one_column_and_names_what_it_changes() -> None:
    db = FakeDb()
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/source @anime_uploads4u gate off")

    row = db.source_channels[0]
    assert row["require_hindi_audio"] is False, "the switch reached the row, not just the reply"
    assert "switched" in text and "gate" in text
    assert "Hindi-audio" in text, "what the word means, not only the word for it"

    (back,) = await say(control, "/source @anime_uploads4u gate on")
    assert db.source_channels[0]["require_hindi_audio"] is True, "the same words switch it back"
    assert "gate" in back


@pytest.mark.asyncio
async def test_watch_off_is_the_pause_that_ingest_reads() -> None:
    """`watch` writes `mode`, because `mode` is the column with a reader.

    `active` sits next to it in the table and would read like the natural switch — nothing that claims a
    job looks at it, so a toggle there would be a pause button that pauses nothing.
    """
    db = FakeDb()
    control, _api, _db = bot(db=db)
    (_text,) = await say(control, "/source @anime_uploads4u watch off")

    assert db.source_channels[0]["mode"] == "ignore"
    assert "active" not in db.source_channels[0], "the row was not touched where nothing reads it"


@pytest.mark.asyncio
async def test_a_switch_that_is_not_a_switch_says_so() -> None:
    """One word, and a choice of what to type instead — never a half-applied change.

    `subs on on`, `gate maybe`, and an unknown flag all answer with the usage; what they must not do is
    write one column and refuse the next, which is how a config command earns distrust.
    """
    db = FakeDb()
    control, _api, _db = bot(db=db)
    texts = await say(control, "/source @anime_uploads4u gate maybe")
    assert "on or off" in texts[0] and db.writes == []
    texts = await say(control, "/source @anime_uploads4u volume up")
    assert "gate" in texts[0] and "subs" in texts[0] and db.writes == []


# --------------------------------------------------------------------------- /archive
# The other row the setup was waiting on, and the one place this program refuses to pick a channel:
# the archive holds the only spare copy of an episode, so it is named or the job blocks.


@pytest.mark.asyncio
async def test_archive_without_a_row_says_what_is_missing_and_how_to_write_it() -> None:
    db = FakeDb()
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/archive")

    assert "no archive channel is recorded" in text
    assert "add title" in text, "the answer names the command that fills it in"
    assert db.writes == [], "reading the list writes nothing"


@pytest.mark.asyncio
async def test_the_first_archive_row_becomes_the_primary_one() -> None:
    db = FakeDb()
    control, _api, _db = bot(db=db)
    (text,) = await say(control, '/archive -1002072936982 add title "Master archive"')

    row = db.archive_channels[0]
    assert row["telegram_channel_id"] == -1002072936982
    assert row["is_primary"] is True, "the first one is the primary one, and the reply says so"
    assert "primary: yes" in text and "archive row" in text
    assert "not checked against Telegram" in text, "a number we could not ask about is called what it is"


@pytest.mark.asyncio
async def test_a_second_archive_row_waits_its_turn() -> None:
    db = FakeDb()
    db.archive_channels = [{"id": 1, "telegram_channel_id": -1001, "title": "First", "is_primary": True}]
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/archive -1002 add title Second")

    assert db.archive_channels[1]["is_primary"] is False
    assert "waits its turn" in text
    (listed,) = await say(control, "/archive")
    assert "primary" in listed and "spare" in listed, "and the list says which is which"


@pytest.mark.asyncio
async def test_an_archive_channel_is_not_listed_twice() -> None:
    db = FakeDb()
    db.archive_channels = [{"id": 1, "telegram_channel_id": -100777, "title": "First", "is_primary": True}]
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/archive -100777 add title Renamed")

    assert "already the archive" in text
    assert len(db.archive_channels) == 1, "the row it found is left alone"


@pytest.mark.asyncio
async def test_an_archive_row_without_a_name_is_refused() -> None:
    """Titles are decoration in a source channel and the whole description of an archive.

    Nobody reads messages out of an archive, so `title` is the only thing that tells a person which
    channel number they trusted with the spare copies. An empty one is refused rather than stored.
    """
    db = FakeDb()
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/archive -100777 add")

    assert "needs a title" in text and "/archive -100777 add title" in text
    assert db.archive_channels == [] and db.writes == []


@pytest.mark.asyncio
async def test_a_handle_is_never_written_as_a_row_with_a_guessed_number() -> None:
    """Shadow mode cannot ask Telegram who owns a @handle, so the command stops instead of filling it in.

    A row's `telegram_channel_id` is the only thing that says which channel this service reads, and an id
    invented from a username means watching somebody else's channel — the one failure a config command
    must not be able to cause, so it is refused loudly and the number is asked for instead.
    """
    db = FakeDb()
    db.source_channels = []
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/source @bleach_hindi add")

    assert "cannot write a row" in text and "shadow mode" in text, text[:160]
    assert db.source_channels == [] and db.writes == [], "no row, no id, no promise"

    (archive,) = await say(control, '/archive @bleach_master add title "Master"')
    assert "cannot write an archive row" in archive
    assert db.archive_channels == []


# --------------------------------------------------------------------------- buttons
# A button is a command typed for the operator. These three tests are the whole claim: the payload a
# tap sends is the payload the router already serves, the write is the same write, and a stranger
# pressing the same button gets exactly what a stranger typing it gets — nothing.


def _press(api, data: str):
    """The raw `callback_query` Telegram would send when the operator taps that button."""
    return {
        "update_id": 900 + len(api.sent),
        "callback_query": {
            "id": "cb-1",
            "from": {"id": OWNER},
            "message": {"message_id": 12, "chat": {"id": OWNER}},
            "data": data,
        },
    }


@pytest.mark.asyncio
async def test_tapping_a_switch_button_writes_what_typing_the_words_writes() -> None:
    """The equivalence the module's docstring promises, checked on the row rather than on the label."""
    from app import keyboards

    typed_db = FakeDb()
    typed_db.source_channels[0]["require_hindi_audio"] = True
    typed, _api, _db = bot(db=typed_db)
    await say(typed, "/source @anime_uploads4u gate off")

    pressed_db = FakeDb()
    pressed_db.source_channels[0]["require_hindi_audio"] = True
    pressed, api, _db = bot(db=pressed_db)
    payload = keyboards.source_switches("@anime_uploads4u", pressed_db.source_channels[0])
    gate = next(one for one in payload["inline_keyboard"][0] if "gate" in one["text"])
    replies = await pressed.handle(parse_update(_press(api, gate["callback_data"])))

    assert gate["callback_data"] == "/source @anime_uploads4u gate off"
    assert pressed_db.source_channels[0]["require_hindi_audio"] is False
    assert typed_db.source_channels[0] == pressed_db.source_channels[0], "the same row state, either way"
    assert "switched" in replies[0].text and replies[0].markup, "and the fresh buttons come with it"


@pytest.mark.asyncio
async def test_the_summary_under_a_source_reply_offers_the_three_switches() -> None:
    """`/source <channel>` used to be a wall of text about flags. Now the flags are the reply."""
    control, api, _db = bot()
    (reply,) = await control.handle(update("/source @anime_uploads4u"))
    rows = reply.markup["inline_keyboard"]
    assert [row[0]["text"].split(" ")[0] for row in rows] == ["gate", "subs", "watch"]
    assert all(row[0]["callback_data"].startswith("/source @anime_uploads4u ") for row in rows)

    # The keyboard only means anything if it reaches the transport — a `Reply.markup` that the send loop
    # drops is a test that passes and a bot with no buttons.
    await control.dispatch(parse_update(_press(api, rows[1][0]["callback_data"])))
    sent = [one for one in api.markups if one]
    assert sent, "dispatch passed a keyboard to the transport"
    assert [row[0]["text"].split(" ")[0] for row in sent[-1]["inline_keyboard"]] == [
        "gate",
        "subs",
        "watch",
    ], "and the screen after the press offers the switches again"


@pytest.mark.asyncio
async def test_a_stranger_cannot_press_a_button_that_was_shown_to_the_owner() -> None:
    from app import keyboards

    control, api, db = bot()
    payload = keyboards.source_switches("@anime_uploads4u", db.source_channels[0])
    data = payload["inline_keyboard"][0][0]["callback_data"]
    raw = {
        "update_id": 901,
        "callback_query": {
            "id": "cb-x",
            "from": {"id": STRANGER},
            "message": {"message_id": 12, "chat": {"id": STRANGER}},
            "data": data,
        },
    }
    parsed = parse_update(raw)
    assert parsed is not None

    assert await control.handle(parsed) == [], "the same gate, whichever way the text arrived"
    assert "require_hindi_audio" not in db.source_channels[0] or db.source_channels[0][
        "require_hindi_audio"
    ] is not False, "and the flag never moved"
    assert db.writes == []


@pytest.mark.asyncio
async def test_the_keyboard_travels_as_an_object_on_the_first_part_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the transport may put in the body, learned from the shape of the call.

    `_call` posts JSON. Telegram accepts a JSON-*string* keyboard only in a form-encoded body, so the
    tempting `json.dumps(markup)` here answers with a 400 about a wrong inline keyboard — a button that
    works in every test and fails on the wire. And a keyboard repeated on all three parts of a split
    report would be the same tap offered three times under two thirds of a message.
    """
    from app.botapi import BotApi as _BotApi

    api = _BotApi(TOKEN)
    calls: list[dict[str, Any]] = []

    async def fake_call(method: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"method": method, **kwargs})
        return {"result": {"message_id": len(calls)}}

    monkeypatch.setattr(api, "_call", fake_call)
    keyboard = {"inline_keyboard": [[{"text": "gate → off", "callback_data": "/source @c gate off"}]]}

    await api.send(OWNER, "the screen\\n" + ("y" * 5000), markup=keyboard)
    assert [call["method"] for call in calls] == ["sendMessage"] * 2
    assert calls[0]["reply_markup"] == keyboard and isinstance(calls[0]["reply_markup"], dict)
    assert not isinstance(calls[0]["reply_markup"], str), "not JSON-encoded: this body is already JSON"
    assert "reply_markup" not in calls[1], "one keyboard per screen, on the part the operator reads"

    calls.clear()
    await api.send(OWNER, "no buttons here")
    assert "reply_markup" not in calls[0], "a plain reply must not carry an empty keyboard"


@pytest.mark.asyncio
async def test_a_channel_added_by_number_can_be_named_afterwards() -> None:
    """`add` is the only command that writes a row, and a row is the only place a title can live.

    Without `title` the operator's one unnamed channel had to go back to the dashboard to fix a label —
    the exact trip this half of the bot exists to end — and `/status` would print `-1002575861262` where
    a name goes.
    """
    db = FakeDb()
    control, _api, _db = bot(db=db)
    (text,) = await say(control, "/source @anime_uploads4u title Bleach in Hindi")

    assert db.source_channels[0]["title"] == "Bleach in Hindi"
    assert "declarations updated" in text, "a write says it wrote"
    (shown,) = await say(control, "/source @anime_uploads4u")
    assert "Bleach in Hindi" in shown, "and the row is called that from now on"

    (missing,) = await say(control, "/source @anime_uploads4u title")
    assert "`title` needs a value" in missing and db.source_channels[0]["title"] == "Bleach in Hindi"


# --------------------------------------------------------------------- the console, tapped
#
# `tests/test_console.py` checks what a screen is made of. These check the other half: that a press is
# answered by the same handler the words go through, and that the row ends up where the tap said it would.
# A keyboard is only safe while both statements hold, and only this file can hold them at once.


@pytest.mark.asyncio
async def test_a_tap_opens_the_menu_with_no_command_typed() -> None:
    control, api, db = bot()
    await control.dispatch(parse_update(_press(api, "n:main")))
    assert "mode:" in api.sent[-1][1]
    first = api.markups[-1]["inline_keyboard"][0][0]
    assert first["text"] == "📊 Status" and first["callback_data"] == "x:/status"
    assert db.writes == [], "a screen is read-only, even the one that looks like a dashboard"


@pytest.mark.asyncio
async def test_the_source_list_names_every_configured_channel_by_name() -> None:
    control, api, db = bot()
    await control.dispatch(parse_update(_press(api, "n:sources")))
    assert "anime_uploads4u" in api.sent[-1][1], "the handle, which is what the operator knows it as"
    rows = api.markups[-1]["inline_keyboard"]
    assert rows[0][0]["callback_data"] == "r:s3:open", (
        "the payload names the table as well as the row: `app.destination` has its own row 3, and a button "
        "that could not tell them apart would be one tap away from editing the wrong channel"
    )


@pytest.mark.asyncio
async def test_a_switch_on_a_screen_writes_the_row_and_returns_to_the_screen() -> None:
    """Two replies, and the second is the truth rather than the intention.

    The audit line names the command the tap became, and it is not decoration: `/source … gate off` is the
    line that goes in a bug report, and an operator who cannot see it has no way to tell a screen that lied
    from a write that worked.
    """
    control, api, db = bot()
    db.source_channels[0]["require_hindi_audio"] = True
    await control.dispatch(parse_update(_press(api, "r:3:gate:off")))
    assert db.source_channels[0]["require_hindi_audio"] is False
    assert "gate: off" in api.texts
    assert "ran: `/source @anime_uploads4u gate off`" in api.sent[-1][1]
    assert "○ Hindi-audio check: off" in api.sent[-1][1], "and the screen already reflects it"


@pytest.mark.asyncio
async def test_tapping_a_switch_from_a_screen_and_typing_the_words_leave_the_same_row() -> None:
    """The same claim as the `app/keyboards.py` parity test, through the console's own translator."""
    typed, typed_api, typed_db = bot()
    await say(typed, "/source @anime_uploads4u audio dual")
    tapped, tapped_api, tapped_db = bot()
    await tapped.dispatch(parse_update(_press(tapped_api, "r:3:audio:dual")))
    assert tapped_db.source_channels[0]["declared_audio"] == typed_db.source_channels[0]["declared_audio"]
    assert tapped_db.declared_history == typed_db.declared_history


@pytest.mark.asyncio
async def test_add_a_channel_asks_for_the_one_thing_a_button_cannot_carry() -> None:
    """`p:add`, then a bare id: the only text in the flow, requested by the bot and answered once.

    The negative half is in the same test on purpose — once the row exists there is no question outstanding,
    so the next thing typed is not quietly taken as another channel to create.
    """
    control, api, db = bot()
    db.source_channels = []
    await control.dispatch(parse_update(_press(api, "p:add")))
    labels = [one["text"] for one in api.markups[-1]["inline_keyboard"][-1]]
    assert "its @handle" in api.sent[-1][1] and "✖ Stop here" in labels
    replies = await say(control, "-1002575861262")
    assert db.source_channels, "the row was created by the tap's answer"
    assert any("watching it" in line for line in replies), replies
    assert "ran: `/source -1002575861262 add`" in replies[-1], "and the list came back with the audit line"
    assert await say(control, "-1002575861263") == [], "the question was asked once, and no more"


@pytest.mark.asyncio
async def test_pressing_anything_else_abandons_an_open_question() -> None:
    control, api, db = bot()
    await control.dispatch(parse_update(_press(api, "p:series:3")))
    await control.dispatch(parse_update(_press(api, "n:main")))
    assert await say(control, "Naruto") == []
    assert db.source_channels[0]["declared_series"] == "", "and the half-finished rename was not written"


@pytest.mark.asyncio
async def test_a_command_typed_while_a_question_is_open_is_not_taken_as_the_answer() -> None:
    """The operator stopped to check something, and the question is still waiting when they come back.

    Taking any message as the answer would turn `/status` into a series name — which the parser would
    refuse, so the harm would be only the lost rename. Losing it silently is the harm.
    """
    control, api, db = bot()
    await control.dispatch(parse_update(_press(api, "p:series:3")))
    assert any("queue:" in line for line in await say(control, "/status"))
    await say(control, "Bleach")
    assert db.source_channels[0]["declared_series"] == "Bleach"


@pytest.mark.asyncio
async def test_a_row_that_vanished_between_the_question_and_the_answer_is_refused_loudly() -> None:
    control, api, db = bot()
    await control.dispatch(parse_update(_press(api, "p:series:3")))
    db.source_channels.clear()
    replies = await say(control, "Bleach")
    assert replies and "not configured any more" in replies[0]
    assert db.declared_history == []


@pytest.mark.asyncio
async def test_a_button_for_a_row_that_is_gone_writes_nothing() -> None:
    """Stale screens happen — the tab from this morning, a second device. The reply says what to do about it.

    A button nobody is looking at any more must not take a wrong action, and the way out has to be in the
    refusal: the list to re-read, or the button that starts over.
    """
    control, api, db = bot()
    db.source_channels[0]["require_hindi_audio"] = True
    replies = await control.dispatch(parse_update(_press(api, "r:999:gate:off")))
    assert replies and "not configured any more" in replies[0].text
    assert "➕ Add a channel" in replies[0].text, "and the button that fixes it is named"
    assert db.source_channels[0]["require_hindi_audio"] is True, "the real row was left alone"


@pytest.mark.asyncio
async def test_a_stranger_pressing_a_console_button_gets_nothing_at_all() -> None:
    """The tap is not a login, and the row id in the payload is not a capability.

    Worth pinning again on this path specifically: the console's payloads name rows by number, which is
    shorter and easier to guess than a command, so this is the version an attacker would try.
    """
    control, api, db = bot()
    db.source_channels[0]["require_hindi_audio"] = True
    update_ = {
        "update_id": 1,
        "callback_query": {
            "id": "cb-x",
            "from": {"id": STRANGER},
            "message": {"message_id": 3, "chat": {"id": STRANGER}},
            "data": "r:3:gate:off",
        },
    }
    from app.botapi import parse_update

    replies = await control.handle(parse_update(update_))
    assert replies == [] and api.sent == []
    assert db.source_channels[0]["require_hindi_audio"] is True


@pytest.mark.asyncio
async def test_the_bare_command_and_the_prefixed_one_are_the_same_tap() -> None:
    """`app/keyboards.py` sends `/pause` as callback data; the console sends `x:/pause`.

    Both have to land on the same method with the same args, or the previous round's buttons are dead buttons
    in this round's build — which is the specific way a layered interface breaks.
    """
    prefixed, api_a, db_a = bot()
    await prefixed.handle(parse_update(_press(api_a, "x:/pause")))
    bare, api_b, db_b = bot()
    await bare.handle(parse_update(_press(api_b, "/pause")))
    assert api_a.sent == api_b.sent
    assert db_a.paused[-1][0] is True and db_b.paused[-1][0] is True


@pytest.mark.asyncio
async def test_help_offers_the_menu_beside_the_command_list() -> None:
    control, api, db = bot()
    await control.dispatch(update("/help"))
    assert api.markups[-1]["inline_keyboard"][0][0]["callback_data"] == "n:main"
    assert "/help" in api.sent[-1][1], "the text is not replaced by the button"


@pytest.mark.asyncio
async def test_a_screen_this_build_does_not_have_says_so() -> None:
    control, api, db = bot()
    replies = await control.dispatch(parse_update(_press(api, "n:sources_but_nicer")))
    assert replies and "does not exist in this build" in replies[0].text


@pytest.mark.asyncio
async def test_the_joinmsg_screen_shows_the_saved_wording_or_says_it_is_empty() -> None:
    control, api, db = bot()
    await control.dispatch(parse_update(_press(api, "n:joinmsg")))
    assert "saved now: nothing" in api.sent[-1][1]
    await say(control, "/joinmsg set waiting for you, {name}")
    await control.dispatch(parse_update(_press(api, "n:joinmsg")))
    assert "waiting for you, {name}" in api.sent[-1][1]


@pytest.mark.asyncio
async def test_the_queue_screen_names_the_state_it_cannot_read_rather_than_picking_one() -> None:
    """`paused: no` offers Pause, `paused: yes` offers Resume, and the buttons come from the read.

    A screen that guessed at a state is the one place a button can be built that runs the command the
    operator did not want; `console.queue_screen` deliberately has no third case, and this keeps it that way.
    """
    control, api, db = bot()
    await control.dispatch(parse_update(_press(api, "n:queue")))
    assert "paused: no" in api.sent[-1][1]
    labels = [one["text"] for row in api.markups[-1]["inline_keyboard"] for one in row]
    assert labels == ["⏸ Pause", "♻ Reconcile now", "↻ Refresh", "◀ Menu"], labels
    db.paused.append((True, "maintenance"))
    await control.dispatch(parse_update(_press(api, "n:queue")))
    labels = [one["text"] for row in api.markups[-1]["inline_keyboard"] for one in row]
    assert labels == ["▶ Resume", "♻ Reconcile now", "↻ Refresh", "◀ Menu"], labels
    assert "maintenance" in api.sent[-1][1]


@pytest.mark.asyncio
async def test_a_tap_answers_the_callback_so_the_button_stops_spinning() -> None:
    """Telegram keeps a button in its pressed state until `answerCallbackQuery` says otherwise.

    A screen that redraws itself while the old copy still spins looks like a bot still thinking about it,
    and an operator who waits for thinking that already finished is an operator who stops tapping — which
    is how a queue pause gets missed.
    """
    control, api, db = bot()
    await control.dispatch(parse_update(_press(api, "n:main")))
    assert api.callbacks[-1] == ("cb-1", ""), "answered, with no alert text to explain a screen that worked"


@pytest.mark.asyncio
async def test_no_tap_deletes_the_message_it_was_pressed_on() -> None:
    """Screens are not the operator's spent secrets — and this catches it if a handler starts thinking so.

    `dispatch` deletes the message a reply was answering, which is right for a pairing code and wrong for a
    tap: a callback's `message_id` is the message the button sits in, i.e. the screen itself. Every command
    a button can reach is run here, and the chat is checked for deletions.
    """
    for data in ("n:main", "n:sources", "n:queue", "n:bots", "n:joinmsg", "n:help", "r:3:open", "r:3:subs:on"):
        control, api, db = bot()
        await control.dispatch(parse_update(_press(api, data)))
        assert api.deleted == [], f"{data} deleted a message"
    control, api, db = bot()
    await control.dispatch(parse_update(_press(api, "p:series:3")))
    assert api.deleted == [], "the question a screen asked is not a secret either"


# --------------------------------------------------------------------- the destinations half
#
# `/destination` and its screens came out of one line in this bot's own refusals: `_find_destination` told
# the operator that "/destinations lists what exists", and nothing served `/destinations`. A promise of a
# command that is not there reads as a bot that ignores its owner, so the promise is now kept — and held by
# `tests/test_console.py::test_no_reply_promise_a_command_the_router_does_not_serve`, which fails for any
# `/word` shown to a human that the router does not take.


@pytest.mark.asyncio
async def test_destination_list_is_the_same_thing_as_the_screen() -> None:
    """Typed words and a tap reach one builder, so they cannot disagree about a column.

    The keyboard arrives with the list too: a screen that only the menu can produce would make the typed
    command a worse answer than the button, which is the hierarchy this whole layer is trying to remove.
    """
    control, api, db = bot()
    typed = await control.handle(update("/destination"))
    await control.dispatch(parse_update(_press(api, "n:destinations")))
    (reply,) = typed
    assert reply.text == api.sent[-1][1], "the same text, either way"
    assert reply.markup == api.markups[-1], "and the same buttons"
    assert "Dekin no mogura" in reply.text


@pytest.mark.asyncio
async def test_a_destination_tap_opens_its_own_screen_and_names_the_command_it_ran() -> None:
    control, api, db = bot()
    await control.dispatch(parse_update(_press(api, "r:d21:card:show")))
    assert "Dekin no mogura" in api.texts
    assert "ran: `/destination -1001234 card show`" in api.sent[-1][1]
    assert "not named" in api.sent[-1][1], "and the screen says what the card line means"


@pytest.mark.asyncio
async def test_naming_a_card_post_from_a_screen_writes_the_row_it_was_asked_about() -> None:
    """The whole chain: prompt → one typed number → the command `/card` owns → the screen, re-read.

    The write is checked on the database rather than on the reply, because the reply is the part this bot
    could get wrong cheerfully. `announcement_link` is not touched by naming a card — the stored link is
    left alone on purpose, and that decision belongs to `/card`, not to the console.
    """
    control, api, db = bot()
    await control.dispatch(parse_update(_press(api, "p:card:d21")))
    assert "message number" in api.sent[-1][1]
    replies = await say(control, "512")
    assert db.destinations[0]["card_message_id"] == 512, "the row was written"
    assert "update app.destination set card_message_id = $2" in db.writes[-1][0]
    assert "card post for Dekin no mogura" in replies[0], "the command answers in its own words"
    assert "card post: message 512" in replies[-1], "and the screen arrives re-read, not remembered"


@pytest.mark.asyncio
async def test_a_question_asked_about_the_wrong_table_is_refused_before_any_typing() -> None:
    """`card` is a destination's column, so a button may not ask for one on a source row.

    The payload is forgeable — that is what makes it worth refusing. `parse_prompt` rejects the shape and
    the operator gets a sentence about the button rather than a question they can answer into nowhere.
    """
    control, api, db = bot()
    replies = await control.handle(parse_update(_press(api, "p:card:s3")))
    assert replies and "no longer knows how to ask" in replies[0].text
    assert api.sent == [] and db.writes == [], "nothing was asked, so nothing could be written"


@pytest.mark.asyncio
async def test_a_tap_on_a_destination_never_writes_a_source_row() -> None:
    """The table letter in the payload is not decoration: it is the only thing separating row 21 from row 3.

    Asserted on the untouched row rather than on the reply, because a wrong write is precisely the failure a
    screen cannot report about itself.
    """
    control, api, db = bot()
    before = dict(db.source_channels[0])
    await control.dispatch(parse_update(_press(api, "r:d21:inplace:off")))
    assert db.source_channels[0] == before
    assert not any("app.source_channel" in sql for sql, _ in db.writes), (
        "a destination tap writes a destination row, or nothing at all"
    )


@pytest.mark.asyncio
async def test_a_destination_with_no_channel_yet_is_refused_with_a_way_forward() -> None:
    """The row can exist before the channel does, and "there is nothing to point a card at" must say why.

    An honest refusal is actionable: it names who builds the channel and which command shows that job, so
    the operator is not left looking for a setting that is not missing.
    """
    control, api, db = bot()
    db.destinations.append(
        {"id": 22, "title": None, "telegram_channel_id": None, "series": "Bleach", "publish_mode": "link_post"}
    )
    replies = await say(control, "/destination Bleach card 512")
    assert replies and "no channel id stored" in replies[0]
    assert db.writes == [], "and a refusal writes nothing"


@pytest.mark.asyncio
async def test_episode_counts_come_from_the_destination_that_knows_the_series() -> None:
    """`/destination <one> episodes 2 12` is `/declare <series> 2 12` with the name already filled in.

    The point is the typing that disappears, not a new write path: `_declare` still owns the column, the
    refusal when two series match, and the `tba` spelling that stops claiming a length.
    """
    control, api, db = bot()
    (text,) = await say(control, "/destination -1001234 episodes 2 12")
    assert "declared 12 episodes" in text, "the season length was recorded, by the command that owns it"
    assert any("insert into app.season" in sql for sql, _ in db.writes), "onto the season row, not a new one"
    assert "12" in text or "season" in text.lower()
    (noanswer,) = await say(control, "/destination -1001234 episodes")
    assert "how many episodes" in noanswer


@pytest.mark.asyncio
async def test_campaigns_are_listed_from_the_destination_they_belong_to() -> None:
    """`/destination <one> campaigns` and `/campaign <one>` are one handler, because the rows are one table.

    A destination is what a campaign sends from, so `/campaign` already takes a destination handle — the new
    command only spares the operator the remembering of which channel a series publishes into.
    """
    control, api, db = bot()
    (a,) = await say(control, "/destination -1001234 campaigns")
    (b,) = await say(control, "/campaign -1001234")
    assert a == b, "the same answer, reached either way"


@pytest.mark.asyncio
async def test_a_source_can_jump_to_the_destination_it_feeds() -> None:
    """The one cross-table tap, and it rides on `destination_id` rather than on a title match.

    Without the link the button still has to say something true, so both halves are checked here: the jump
    when the ingest side recorded one, and the refusal that explains what records it when it has not.
    """
    control, api, db = bot()
    replies = await control.handle(parse_update(_press(api, "r:s3:dest")))
    assert replies and "not linked to a destination row yet" in replies[0].text

    db.source_channels[0]["destination_id"] = 21
    await control.dispatch(parse_update(_press(api, "r:s3:dest")))
    assert "destination ·" in api.sent[-1][1], "and once it is linked, the jump lands on that row"


@pytest.mark.asyncio
async def test_the_gone_row_message_points_at_a_command_that_exists() -> None:
    """/sources is routed, which is the only reason that sentence is allowed to say it.

    The refusal used to name a command nothing served. The wording is kept because it is the right
    instruction — the list is the thing to re-read — and this test is what stops it decaying back.
    """
    control, api, db = bot()
    from app import controlbot as module

    assert "sources" in module._ROUTES, "the promise in the text is kept by the router"
    replies = await control.handle(parse_update(_press(api, "r:999:gate:off")))
    assert "/sources" in replies[0].text
    (listing,) = await say(control, "/sources")
    assert "anime_uploads4u" in listing and "👁" in listing


@pytest.mark.asyncio
async def test_the_archive_can_be_pointed_at_without_leaving_the_menu() -> None:
    """`📦 Point at an archive` then one number: the archive row used to need two commands and a dashboard.

    The rename is refused while there is nothing to name — "which archive?" is a question the operator can
    only answer if the bot asks it, and asking it after a wrong write is too late.
    """
    control, api, db = bot()
    await control.dispatch(parse_update(_press(api, "p:archive_title")))
    (refused,) = await say(control, "Master copies")
    assert "no archive channel recorded" in refused and db.archive_channels == []

    await control.dispatch(parse_update(_press(api, "p:archive")))
    assert "on one line" in api.sent[-1][1], "the question says what shape the answer has"
    await say(control, "-100999888777 Master copies")
    assert db.archive_channels, "the archive row was written"
    assert db.archive_channels[0]["title"] == "Master copies"
    assert "archive" in api.sent[-1][1].lower(), "and the bots screen came back showing it"


@pytest.mark.asyncio
async def test_the_sessions_screen_offers_only_the_two_verbs_that_exist() -> None:
    """Use one session, forget one — and never a session string, never even its length.

    `list_sessions` refuses to select the string; the screen repeats the refusal one layer up, because a
    value that reaches a chat window stays in the cloud forever, which is the opposite of what this bot
    promises about logins.
    """
    db = FakeDb(stored=[{"name": "spare", "kind": "user", "active": True, "length_chars": 300}])
    control, api, db2 = bot(db=db)
    db.stored[0]["username"] = "Turvei"
    await control.dispatch(parse_update(_press(api, "n:sessions")))
    text = api.sent[-1][1]
    assert "spare" in text and "@Turvei" in text
    assert "300" not in text and "AAAA" not in text
    labels = [one["text"] for one in api.markups[-1]["inline_keyboard"][0]]
    assert labels == ["▶ Use spare", "🧹 Forget spare"], labels
    (forgot,) = await say(control, "/forget nosuch")
    assert "nothing stored under" in forgot


# ---------------------------------------------------------------- what the account can see, filed by a tap
#
# `/discover` is the one command in this bot that writes a row nobody typed. Everything it may write, it
# writes through `app/sourcecfg.py`, and these tests are mostly about the parts where it must not: a channel
# it cannot name a series for, a series that does not exist yet, and a switch that would leave a season
# reading nothing. The dialog walk is replaced with a list, because a test that needed a logged-in account
# could never be run by anyone reviewing this file.


def discovery_dialogs() -> list[dict]:
    """One account: a chat, a shelf we only read, a destination we named, and a source we already file."""
    return [
        {
            "title": "friends chat",
            "username": None,
            "id": 555,
            "mine": False,
            "left": False,
            "channel": False,
            "members": 4,
            "rights": None,
        },
        {
            "title": "shelf of files",
            "username": "file_shelf",
            "id": 444555,
            "mine": False,
            "left": False,
            "channel": True,
            "members": 30000,
            "rights": None,
        },
        {
            "title": "Dekin no mogura Anime in Hindi",
            "username": None,
            "id": 2575861262,
            "mine": True,
            "left": False,
            "channel": True,
            "members": 12,
            "rights": {"post_messages": True, "edit_messages": True},
        },
        {
            "title": "anime_uploads4u",
            "username": "anime_uploads4u",
            "id": 1112223334,
            "mine": False,
            "left": False,
            "channel": True,
            "members": 9,
            "rights": None,
        },
    ]


def discovery_bot(*, auto: bool = False, dialogs=None):
    """A bot whose Telegram read is this list, and whose auto switch starts where the test says.

    `_discover_dialogs` is replaced rather than the client beneath it, so the walk's own failure modes stay
    tested by their own test below instead of being hidden behind a fake connection.
    """
    control, api, db = bot()
    walk = discovery_dialogs() if dialogs is None else dialogs

    seen: dict[str, bool] = {}

    async def fake_walk(*, verify_rights: bool = False):
        # The flag is recorded rather than ignored: whether this read asked Telegram what the account may do
        # is the difference between "member" meaning "Telegram said member" and "member" meaning "the
        # session's cached list had no rights on it", and a fake that dropped the argument would let the
        # product stop asking without a single test noticing.
        seen["verify_rights"] = verify_rights
        return list(walk)

    control._discover_dialogs = fake_walk  # type: ignore[method-assign]
    control.walk_flags = seen  # type: ignore[attr-defined]
    if auto:
        db.config_rows[discover.AUTO_KEY] = True
    return control, api, db, walk


@pytest.mark.asyncio
async def test_the_discovery_read_asks_telegram_what_the_account_may_do() -> None:
    """/discover verifies rights per channel; a dialog list alone is how "my own channel" read as a member.

    The flag is asserted on the way out of the fake, because the whole complaint was that the bot could not
    tell where the account is admin — and the answer to that is not in the cached dialog list.
    """
    control, _api, _db, _walk = discovery_bot()
    await say(control, "/discover")
    assert control.walk_flags.get("verify_rights") is True, control.walk_flags


@pytest.mark.asyncio
async def test_a_channel_whose_rights_telegram_would_not_confirm_is_said_so() -> None:
    """An unread channel is reported as unread, not decided from the weaker source and left looking like fact."""
    walk = discovery_dialogs()
    walk[1]["rights_source"] = "participant"
    walk[2]["rights_source"] = "dialog"
    walk[2]["rights_error"] = "ChannelPrivateError: no access"
    control, _api, _db, _w = discovery_bot(dialogs=walk)
    (text,) = await say(control, "/discover")
    assert "rights asked of Telegram directly for 1 channel" in text, text
    assert "kept the session's older answer" in text


@pytest.mark.asyncio
async def test_one_tap_pairs_the_two_channels_by_name_and_links_them() -> None:
    """The operator's setup, tapped: source `X`, destination `X`, one series, one link, nothing invented twice.

    Every write goes through the same two modules the typed commands use, so this asserts the statements
    rather than a paraphrase of them: a series row founded by `app/ingest.ensure_series`, a destination, a
    source, and `destination_id` carrying the ids those two inserts just returned — which is the one
    assertion a fake that only counts statements cannot fake.
    """
    walk = discovery_dialogs()
    walk[1]["title"] = "Dekin no mogura"  # the same show, named like its source: the pairing rule
    control, api, db, _w = discovery_bot(dialogs=walk)
    before = len(db.destinations)
    await control.dispatch(parse_update(_press(api, "x:/discover pair 1")))
    text = api.sent[-1][1]

    assert "publishing channel" in text, text
    assert len(db.destinations) == before + 1, "the destination row this series did not have yet"
    assert any("insert into app.series" in sql for sql, _ in db.writes) or db.series, "the series row exists"
    link = [(sql, args) for sql, args in db.writes if "set destination_id" in sql]
    assert link, "and the source was pointed at it"
    destination_row = db.destinations[-1]
    source_row = next(row for row in db.source_channels if row["telegram_channel_id"] == -100444555)
    assert link[0][1] == (source_row["id"], destination_row["id"]), link[0][1]
    assert source_row.get("declared_series") == "Dekin no mogura", "the name came from the channel, on the row"
    assert source_row.get("declared_by") == "discovered from this channel's name"


@pytest.mark.asyncio
async def test_a_pair_number_that_is_not_on_the_page_writes_nothing() -> None:
    control, _api, db, _walk = discovery_bot()
    (text,) = await say(control, "/discover pair 9")
    assert "nothing on this page can be paired as 9" in text, text
    assert db.writes == []


@pytest.mark.asyncio
async def test_discovery_lists_what_it_found_and_what_it_refused_to_decide() -> None:
    """Four dialogs in, two findings out — and the other two are named, not dropped.

    A list that only showed the addable rows would read as "these two are nothing to you", which is the
    opposite of the truth for a channel that is already configured and only true for a chat of four people.
    """
    control, _api, _db, _walk = discovery_bot()
    (reply,) = await control.handle(update("/discover", chat=OWNER))
    text = reply.text
    assert "4 dialogs read: 2 worth a decision, 1 already configured" in text, text
    assert "skipped, on purpose: 1 not channels" in text
    assert "shelf of files — member, source" in text, text
    assert "Dekin no mogura Anime in Hindi — owner, destination" in text, text
    assert "source row 3" in text, "the already-configured one is named with the row it already has"
    assert "auto is off" in text
    assert "📥 shelf of files" in str(reply.markup), "the tap carries the emoji, not the sentence"


@pytest.mark.asyncio
async def test_tapping_the_menu_opens_the_same_discovery_screen_the_command_prints() -> None:
    """Typed and tapped paths run one command, so they cannot show two different facts.

    Two bots with the same fixture, because `/discover` keeps no state between calls: a difference between the
    two replies could only come from the path the update took. Compared as the whole pair — text and
    keyboard — since a screen that matched its typed twin in prose but not in buttons is half a feature.
    """
    typed_control, _a, _b, _w = discovery_bot()
    tap_control, _c, _d, _e = discovery_bot()
    (typed,) = await typed_control.handle(update("/discover", chat=OWNER))
    (tapped,) = await tap_control.handle(parse_update(_press(FakeApi(), "n:discover")))
    assert tapped.text == typed.text and tapped.markup == typed.markup, (tapped.text, typed.text)
    rows = typed.markup["inline_keyboard"]
    labels = [one["text"] for row in rows for one in row]
    assert "✅ Add the rest on this page" in labels and "✨ Let it switch on its own" in labels
    assert [one["callback_data"] for row in rows[:3] for one in row] == [
        "x:/discover add 2",
        "x:/discover add 3",
        "x:/discover add all",
    ]


@pytest.mark.asyncio
async def test_a_tap_files_the_member_channel_as_a_source_through_the_command_writer() -> None:
    control, api, db, _walk = discovery_bot()
    await control.dispatch(parse_update(_press(api, "x:/discover add 2")))
    added = db.source_channels[-1]
    assert added["telegram_channel_id"] == -100444555 and added["mode"] == "full"
    assert "watching: on (mode full)" in api.sent[-1][1]
    assert any("insert into app.source_channel" in sql for sql, _ in db.writes)


@pytest.mark.asyncio
async def test_a_tap_files_the_channel_we_run_as_that_series_destination() -> None:
    """The row `writers.py` has been blocking on, written by pointing at a channel that already exists.

    Nothing is created in Telegram and nothing is posted: `series_id`, the channel number, the title and the
    link-post mode are all this write touches, which is exactly what the publisher joins on.
    """
    control, api, db, _walk = discovery_bot()
    await control.dispatch(parse_update(_press(api, "x:/discover add 3")))
    added = db.destinations[-1]
    assert added["telegram_channel_id"] == -1002575861262
    assert added["publish_mode"] == "link_post" and added["series_id"] == 7
    assert "Nothing was created in Telegram" in api.sent[-1][1]


@pytest.mark.asyncio
async def test_a_destination_with_no_series_row_yet_names_its_series_after_the_channel() -> None:
    """The refusal this used to be is what made a fresh install undetectable, so the tap founds the row.

    Renaming the channel to a title the fake has no series for is still the whole test — it just asserts the
    other half now: the series is founded by the statement the ingest pipeline files with, the reply says the
    name came from the channel, and no channel is created in Telegram by any of it.
    """
    walk = discovery_dialogs()
    walk[2]["title"] = "Bleach Anime in Hindi"
    control, api, db, _w = discovery_bot(dialogs=walk)
    before = len(db.destinations)
    await control.dispatch(parse_update(_press(api, "x:/discover add 3")))
    text = api.sent[-1][1]

    assert len(db.destinations) == before + 1, text
    assert "founded from the channel's own name" in text and "'Bleach'" in text, text
    assert "Nothing was created in Telegram" in text


@pytest.mark.asyncio
async def test_auto_mode_is_written_to_the_key_the_worker_reads() -> None:
    """One tap moves the switch, and the screen that comes back says it is on in the report's own words.

    The key is the one `app/handlers.py` reads on every reconciliation — asserted by name, because a config
    key the bot writes and the worker does not read would be a toggle that only changes a row.
    """
    control, api, db, _walk = discovery_bot()
    await control.dispatch(parse_update(_press(api, "x:/discover auto on")))
    assert "auto is on" in api.sent[-1][1], api.sent[-1][1]
    sql, args = db.writes[-1]
    assert "insert into app.config" in sql and args[0] == discover.AUTO_KEY and args[1] == "true"


@pytest.mark.asyncio
async def test_a_word_that_is_neither_on_nor_off_writes_nothing() -> None:
    control, _api, db, _walk = discovery_bot()
    before = len(db.writes)
    (text,) = await say(control, "/discover auto maybe")
    assert "needs on or off" in text and len(db.writes) == before


@pytest.mark.asyncio
async def test_discovery_says_what_is_missing_instead_of_showing_an_empty_list() -> None:
    """No session is a fact about the deployment, and the reply names the two commands that can fix it.

    This is the path every fresh install hits first: an empty screen would read as "you own no channels",
    which is a different sentence and a wrong one.
    """
    control, api, db = bot()
    await control.dispatch(parse_update(_press(api, "n:discover")))
    text = api.sent[-1][1]
    assert "could not be read from this account" in text, text
    assert "/sessions" in text and "/status" in text
    assert db.writes == [], "and a failed read writes no row"


@pytest.mark.asyncio
async def test_a_role_change_switches_a_source_only_when_the_series_keeps_one_to_read() -> None:
    """The flip, in both directions, from the same screen.

    Two watched channels read `Naruto`, so stopping at the one the operator can now post in strands nothing
    and the switch is applied — with `sourcecfg`'s own writer, so `mode` cannot end up spelled a way the
    scanner ignores. The single-source case is the one that must be refused, and it is refused in words that
    say what to do next.
    """
    dialogs = [
        {
            "title": "Naruto HQ",
            "username": None,
            "id": 888,
            "mine": True,
            "left": False,
            "channel": True,
            "members": 9,
            "rights": {"post_messages": True, "edit_messages": True},
        }
    ]
    sources = [
        {
            "id": 3,
            "username": "naruto_hq",
            "title": "Naruto HQ",
            "telegram_channel_id": -100888,
            "mode": "full",
            "declared_series": "Naruto",
            "declared_audio": "",
            "declared_season": -1,
            "destination_id": None,
            "active": True,
            "require_hindi_audio": True,
            "include_subbed": False,
        },
        {
            "id": 4,
            "username": "naruto_uploads",
            "title": "Naruto uploads",
            "telegram_channel_id": -100999,
            "mode": "full",
            "declared_series": "Naruto",
            "declared_audio": "",
            "declared_season": -1,
            "destination_id": None,
            "active": True,
            "require_hindi_audio": True,
            "include_subbed": False,
        },
    ]
    control, api, db, _w = discovery_bot(auto=True, dialogs=dialogs)
    # Copies, both times: the fake applies an update to the row it holds, so the two bots below must not
    # share one dict — the second would otherwise find the mode already switched by the first and report
    # nothing, which is a fixture accident wearing the costume of a rule.
    db.source_channels = [dict(row) for row in sources]
    await control.dispatch(parse_update(_press(api, "n:discover")))
    assert any("set mode = " in sql for sql, _ in db.writes), "reading stopped, through the switch writer"
    assert "Naruto HQ: switched —" in api.sent[-1][1], api.sent[-1][1]

    control2, api2, db2, _w2 = discovery_bot(auto=True, dialogs=dialogs)
    db2.source_channels = [dict(sources[0])]
    await control2.dispatch(parse_update(_press(api2, "n:discover")))
    assert "not switched —" in api2.sent[-1][1], api2.sent[-1][1]
    assert not any("set mode = " in sql for sql, _ in db2.writes), "and nothing was written to prove the point"


@pytest.mark.asyncio
async def test_a_discovery_number_that_is_not_on_the_page_is_refused_not_guessed() -> None:
    control, _api, db, _walk = discovery_bot()
    (text,) = await say(control, "/discover add 99")
    assert "nothing on this page is numbered 99" in text and db.writes == []


@pytest.mark.asyncio
async def test_an_unknown_discovery_action_writes_nothing_and_shows_the_usage() -> None:
    control, _api, db, _walk = discovery_bot()
    (text,) = await say(control, "/discover demolish")
    assert "not something /discover does" in text and "usage: /discover" in text
    assert db.writes == []


@pytest.mark.asyncio
async def test_a_source_with_no_series_is_refused_an_episode_count() -> None:
    """`📅 Episodes in a season` on a row that has no series name has nowhere to put the number.

    The refusal names the button to press first, which is the difference between "that was wrong" and a
    screen the operator can actually finish. And nothing was asked, so no answer is waiting to be typed.
    """
    control, api, db = bot()
    db.source_channels[0]["declared_series"] = ""
    await control.dispatch(parse_update(_press(api, "p:episodes:s3")))
    replies = await say(control, "2 12")
    assert replies and "no series name is set" in replies[0]
    assert db.writes == [], "and the answer was not written anywhere"


@pytest.mark.asyncio
async def test_the_inplace_button_on_a_source_screen_is_the_command_itself() -> None:
    """`🖼 Show the plan` runs `/inplace <channel> plan`, and `🔗 Links only` runs `… off`.

    There is deliberately no `/inplace <channel> on`: the bare command already means "do it", and inventing a
    word for the button that the typed line rejects is the collision this project has been burned by before.
    """
    control, api, db = bot()
    await control.dispatch(parse_update(_press(api, "r:s3:inplace:plan")))
    assert "plan" in api.texts.lower()
    assert "ran: `/inplace @anime_uploads4u plan`" in api.sent[-1][1]
    assert db.writes == [], "a plan changes nothing, tapped or typed"


# --------------------------------------------------------------------------- /joinreq, the campaign by button
def joinreq_bot(*, dialogs=None, campaign=...):
    """The wizard, on the discovery fake's dialog read: rights are asked now, and the screen shows them.

    The campaign row is passed through rather than always seeded, because the whole point of the flow is the
    difference between "not started" and "running", and a fake that could not answer "there is no row yet"
    would hide the step that drafts one.
    """
    control, api, db, walk = discovery_bot(dialogs=dialogs) if dialogs else discovery_bot()
    if campaign is not ...:
        db.campaign = campaign
    # The headcount read opens a session of its own, which is the whole point of the helper — and a test
    # that let it run for real would be testing the network. The stub is explicit about the number so the
    # flow tests assert the sentence the plan prints, not the exception it used to raise.
    # `joinreq_waiting` is module-level so one test can ask for a queue deeper than what a look reached,
    # which is the only way the difference between the two numbers can be pinned from outside.
    async def _count(peer: str) -> tuple[int | None, int | None, str | None]:
        return (*joinreq_waiting, None)

    control._pending_requests = _count  # type: ignore[method-assign]
    # The campaign screens ask the service whether its sender is awake. A bot assembled in a test has no
    # service, and "no sender" is the wrong default for a screen test: it would hide every sentence the
    # operator sees when things are fine. So the helper says "awake", and the tests that want the broken
    # answer overwrite this one line.
    control.sender_state = lambda: {"absent": False, "running": True}  # type: ignore[attr-defined]
    return control, api, db, walk


joinreq_waiting: tuple[int | None, int | None] = (3, 3)


@pytest.mark.asyncio
async def test_joinreq_lists_only_the_channels_this_account_can_post_in() -> None:
    """/joinreq answers "which channel do the requests come into" from the live read, not from a list.

    The destination row exists for both channels here, but a campaign sends *from this account*, so the one
    where the account turned out to be a member gets a sentence and no button. A button that starts a campaign
    in a channel the account cannot even post in would fail a hundred DMs later, in a log the operator never
    reads.
    """
    dialogs = discovery_dialogs()
    dialogs.append(
        {
            "title": "Naruto HQ",
            "username": "naruto_hq",
            "id": 444555,
            "mine": False,
            "left": False,
            "channel": True,
            "members": 400,
            "rights": None,
        }
    )
    control, api, db, _w = joinreq_bot(dialogs=dialogs)
    db.destinations = [
        {
            "id": 21,
            "title": "Dekin no mogura Anime in Hindi",
            "telegram_channel_id": -1002575861262,
            "publish_mode": "link_post",
            "series": "Dekin no mogura",
        },
        {
            "id": 22,
            "title": "Naruto HQ",
            "telegram_channel_id": -100444555,
            "publish_mode": "link_post",
            "series": "Naruto",
        },
        {
            "id": 23,
            "title": "Somewhere else",
            "telegram_channel_id": -100999999,
            "publish_mode": "link_post",
            "series": "Else",
        },
    ]
    await control.dispatch(parse_update(_press(api, "n:joinreq")))
    text = api.sent[-1][1]
    labels = [one["text"] for row in api.markups[-1]["inline_keyboard"] for one in row]

    assert "Naruto HQ" in text and "only a member" in text, text
    assert not any("Naruto HQ" in one for one in labels), "no start button on a channel it cannot post in"
    assert any(one.startswith("📨 Dekin no mogura") for one in labels), labels
    assert any(one.startswith("⚠️ Somewhere else") for one in labels), "unread rights, so a marked tap"
    assert "➕ Add a channel" in labels


@pytest.mark.asyncio
async def test_add_a_channel_files_the_destination_row_through_the_same_writer_as_discover() -> None:
    """The wizard does not have its own idea of what a destination row is.

    The pick list comes from `app/discover.py`'s classify (so a channel that already has a row cannot be
    offered twice), and the write is `add_destination` — including the series row founded from the channel's
    own name when nothing else could name it, which is what makes a channel the ingest side has never seen
    usable for campaigns at all.
    """
    dialogs = discovery_dialogs()
    dialogs.append(
        {
            "title": "One Piece",
            "username": None,
            "id": 777888,
            "mine": True,
            "left": False,
            "channel": True,
            "members": 15,
            "rights": None,
        }
    )
    control, api, db, _w = joinreq_bot(dialogs=dialogs)
    await control.dispatch(parse_update(_press(api, "x:/joinreq add")))
    listed = api.sent[-1][1]
    labels = [one["text"] for row in api.markups[-1]["inline_keyboard"] for one in row]
    assert any(one.startswith("✅ One Piece") for one in labels), labels

    tapped = next(
        one["callback_data"]
        for row in api.markups[-1]["inline_keyboard"]
        for one in row
        if one["text"].startswith("✅ One Piece")
    )
    assert tapped.startswith("x:/joinreq file "), tapped
    await control.dispatch(parse_update(_press(api, tapped)))
    text = api.sent[-1][1]

    assert "One Piece" in listed and "One Piece" in text, (listed, text)
    assert any("insert into app.destination" in sql for sql, _ in db.writes), db.writes
    assert "founded from the channel's own name" in text or "already a destination row" in text, text
    assert "✏️ Change the message" in [one["text"] for row in api.markups[-1]["inline_keyboard"] for one in row]


@pytest.mark.asyncio
async def test_start_shows_the_words_then_the_tap_switches_it_on() -> None:
    """Two taps, no typing: the plan on screen, then ✅ Yes — and the code that gates `ready` is computed.

    `/campaign` keeps its own confirm step for the operator who types it; what the wizard removes is the
    reading of a code, not the reading of the plan. The plan text — the message, the spacing, the promise
    that nobody is contacted twice — is the reply to the first tap, so the second one is a decision made with
    the words in front of them.
    """
    campaign = {
        "id": 7,
        "name": "default",
        "status": "draft",
        "message_template": "{name}, aapka request dekh liya jaa raha hai",
        "rate_per_hour": 20,
        "confirm_required": True,
    }
    control, api, db, _w = joinreq_bot(campaign=campaign)
    await control.dispatch(parse_update(_press(api, "x:/joinreq start #21")))
    planned = api.sent[-1][1]
    labels = [one["text"] for row in api.markups[-1]["inline_keyboard"] for one in row]
    assert "aapka request dekh liya jaa raha hai" in planned, planned
    assert "✅ Yes, start sending" in labels, labels
    assert db.queued == [], "the plan alone started nothing"

    await control.dispatch(parse_update(_press(api, "x:/joinreq go #21")))
    started = api.sent[-1][1]
    assert db.queued == [], "the start tap queues nothing, so nothing can swallow it"
    assert "is on" in started and "ready" in started, started
    assert "sending: awake" in started, started
    assert db.campaign["status"] == "ready"
    assert "one person every 3 seconds" in api.sent[-1][1].casefold() or "3 seconds" in api.sent[-1][1]


@pytest.mark.asyncio
async def test_stop_pauses_after_the_message_in_flight() -> None:
    """`⏸ Stop` is a pause, and the sent list stays: this program does not un-send or delete anything."""
    campaign = {
        "id": 7,
        "name": "default",
        "status": "running",
        "message_template": "{name}, aapka request dekh liya jaa raha hai",
        "rate_per_hour": 20,
        "confirm_required": True,
    }
    control, api, db, _w = joinreq_bot(campaign=campaign)
    await control.dispatch(parse_update(_press(api, "x:/joinreq stop #21")))
    assert db.campaign["status"] == "paused"
    assert "stay sent" in api.sent[-1][1]


@pytest.mark.asyncio
async def test_an_empty_wording_is_refused_before_anybody_is_messaged() -> None:
    """A campaign with no text is not a campaign that sends nothing; it is a bug waiting for `{name}`.

    The refusal comes from `/campaign new`'s own check, and the wizard passes it through instead of drafting
    an empty row of its own — which is why `start` on a channel with no saved wording ends at that sentence.
    """
    control, api, db, _w = joinreq_bot(campaign=None)
    db.config_rows["joinrequest.message"] = ""
    await control.dispatch(parse_update(_press(api, "x:/joinreq start #21")))
    text = api.sent[-1][1]
    assert "no campaign" in text.casefold() or "empty" in text.casefold() or "nothing" in text.casefold(), text
    assert db.queued == []


@pytest.mark.asyncio
async def test_the_plan_counts_the_people_waiting_now() -> None:
    """/campaign … plan promises a headcount, so the wizard shows the real one.

    A number read from the same channel the job will read is the difference between "start it" meaning
    three taps and "start it" meaning two hundred.
    """
    campaign = {
        "id": 7,
        "name": "default",
        "status": "draft",
        "message_template": "{name}, aapka request dekh liya jaa raha hai",
        "rate_per_hour": 20,
        "confirm_required": True,
    }
    control, api, db, _w = joinreq_bot(campaign=campaign)
    await control.dispatch(parse_update(_press(api, "x:/joinreq start #21")))
    assert "3 join request(s) are waiting right now" in api.sent[-1][1], api.sent[-1][1]


@pytest.mark.asyncio
async def test_the_plan_shows_the_queue_and_not_just_the_page(monkeypatch) -> None:
    """The number the operator decides on is the channel's total, and the second number says so honestly.

    A queue of 250 in which this look reached 3 people prints both, because both are true. Printing only "3"
    reads as "only three people want in" — which is how a campaign gets cancelled out of a page boundary —
    and printing only "250" reads as "this run will message 250 now", which is not what the per-run batch
    allows. The sentence has to carry the whole fact; two halves of it are each a wrong answer.
    """
    campaign = {
        "id": 7,
        "name": "default",
        "status": "draft",
        "message_template": "{name}, aapka request dekh liya jaa raha hai",
        "rate_per_hour": 20,
        "confirm_required": True,
    }
    monkeypatch.setitem(globals(), "joinreq_waiting", (250, 3))
    control, api, db, _w = joinreq_bot(campaign=campaign)

    await control.dispatch(parse_update(_press(api, "x:/joinreq start #21")))
    text = api.sent[-1][1]

    assert "250 join request(s) are waiting right now" in text, text
    assert "this look reached 3 of them" in text, text
    assert "a page at a time without stopping" in text, text


CAMPAIGN_READY = {
    "id": 7,
    "name": "default",
    "status": "draft",
    "message_template": "{name}, aapka request dekh liya jaa raha hai",
    "rate_per_hour": 20,
    "per_message_delay_seconds": 3,
    "confirm_required": True,
}
@pytest.mark.asyncio
async def test_people_recorded_but_never_messaged_are_released_by_starting() -> None:
    """The screen names the rows that make a campaign look finished, and the ✅ tap clears them.

    An older build wrote contact rows for people it only planned, and those rows are what tells every later
    pass to skip them — so the operator saw "0 still waiting" while nobody had been messaged at all. The bot
    cannot tell that state from a live run killed between the row and the send (the second is why the row is
    written first at all), so a machine never decides it. But asking the operator for a *second* tap about the
    same list was its own confusion, and starting a campaign is already a human saying "send to these people":
    so ✅ does the release, says how many it released, and no row is deleted. `free` stays for the case where
    the operator wants them released without starting.
    """
    campaign = dict(CAMPAIGN_READY)
    campaign["unreleased"] = 2
    control, api, db, _w = joinreq_bot(campaign=campaign)

    await control.dispatch(parse_update(_press(api, "x:/joinreq open #21")))
    text = api.sent[-1][1]
    assert "2 person(s) have a row and no message from an earlier attempt" in text, text
    assert "✅ below includes them" in text, text
    labels = [
        str(one.get("text") or one.get("label") or "")
        for row in api.markups[-1]["inline_keyboard"]
        for one in row
    ]
    assert not any(label.startswith("🔁") for label in labels), labels

    await control.dispatch(parse_update(_press(api, "x:/joinreq go #21")))
    reply = api.sent[-1][1]
    assert "2 of them had a row and no message" in reply, reply
    release = [sql for sql, _ in db.writes if "update app.join_campaign_contact set" in sql]
    assert release, db.writes
    assert "status = 'skipped'" in release[0], release[0]
    assert "delete from" not in release[0].lower(), "nobody is deleted from this history"


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_the_release_tap_says_nothing_changed_when_nothing_matched() -> None:
    """A tap that changes nothing has to say so, in the same breath it would have promised a send."""
    control, api, db, _w = joinreq_bot(campaign=dict(CAMPAIGN_READY))
    db.rows_changed = 0

    await control.dispatch(parse_update(_press(api, "x:/joinreq free #21")))

    text = api.sent[-1][1]
    assert "nothing changed" in text, text
    assert "released" not in text, text


@pytest.mark.asyncio
async def test_a_channel_with_no_campaign_row_has_nothing_to_release() -> None:
    control, api, db, _w = joinreq_bot(campaign=None)

    await control.dispatch(parse_update(_press(api, "x:/joinreq free #21")))

    assert "no campaign on this channel" in api.sent[-1][1], api.sent[-1][1]
    assert not [sql for sql, _ in db.writes if "update app.join_campaign_contact" in sql], db.writes


class WorkerSwitch:
    """The in-process worker switch, as `app/main.py` wires it: takes a bool, returns a sentence."""

    def __init__(self, *, running: bool = False) -> None:
        self.running = running
        self.calls: list[bool] = []
        self.refusal = "there is no queue worker in this service to stop."

    async def __call__(self, on: bool) -> str:
        self.calls.append(bool(on))
        if bool(on) == self.running:
            return ("the queue worker is already running in this service, so nothing changed." if on else self.refusal)
        self.running = bool(on)
        return (
            "the queue worker is running in this service now — queued jobs are being claimed."
            if self.running
            else "the queue worker is stopped: jobs are still written, and nothing runs them."
        )


def text_of(replies) -> str:
    return "\n".join(reply.text for reply in replies)


@pytest.mark.asyncio
async def test_the_worker_switch_lives_in_the_chat_because_the_dashboard_is_not_where_the_mistake_was_made() -> None:
    """**This deployment's last silence: a service whose queue worker is off, with a job politely queued.**

    `WORKER_ENABLED=false` is step 1 of the documented probe procedure, and step 3 — putting it back — is a
    dashboard edit the operator never came back for. Every screen then says "queued" and nothing runs, which
    is indistinguishable from a broken bot. The command exists so the fix is a tap; what it deliberately does
    *not* do is touch the environment, so a restart still obeys the service's own setting.
    """
    switch = WorkerSwitch(running=False)
    control, api, db = bot(worker_enabled=False, worker_switch=switch)

    text = text_of(await control.dispatch(update("/worker on")))

    assert switch.calls == [True], switch.calls
    assert "queued jobs are being claimed" in text, text
    assert "running: 1" in text, "the reply has to say what the queue looks like, not just that it moved"
    assert api.texts.lower().count("environment") == 0, "no dashboard step is asked of the operator"

    again = text_of(await control._worker(None, ["status"]))
    assert "running in this service" in again, again
    assert switch.calls == [True], "a status read must not reach for the switch"


@pytest.mark.asyncio
async def test_turning_the_worker_off_says_what_it_costs_and_who_it_is_for() -> None:
    """Off is not neutral: from that moment every job is written and never run. The reply says so."""
    switch = WorkerSwitch(running=True)
    control, api, db = bot(worker_enabled=True, worker_switch=switch)

    text = text_of(await control.dispatch(update("/worker off")))

    assert switch.calls == [False], switch.calls
    assert "nothing runs them" in text, text
    assert "/worker on" in text, "a state that can be left by accident has to say how to undo it"


@pytest.mark.asyncio
async def test_a_service_with_no_switch_says_so_instead_of_pretending_to_change_anything() -> None:
    """The bot is built in tests (and in a bare `create_app`) without a place to hold a worker.

    Refusing is the only honest answer: a reply that said "the worker is running" while nothing was started
    is exactly the sentence that made this campaign look healthy for a day.
    """
    control, api, db = bot(worker_enabled=False)

    text = text_of(await control.dispatch(update("/worker on")))

    assert "no worker switch wired up" in text, text
    assert "WORKER_ENABLED" in text, text


@pytest.mark.asyncio
async def test_the_worker_command_refuses_a_word_it_does_not_know() -> None:
    control, api, db = bot(worker_switch=WorkerSwitch())

    text = text_of(await control.dispatch(update("/worker restart")))

    assert "on` runs" in text and "off` stops" in text, text


@pytest.mark.asyncio
async def test_the_campaign_screen_has_no_queue_button_on_it() -> None:
    """**The operator's rule, pinned: a screen about DMs does not offer a queue control.**

    `▶️ Run the queue` was added when campaign sending still ran through `app.job`, and once it did not the
    button stayed on the screen with a sentence about workers next to it. Every "nothing is sending" report
    since has had one of two causes: a queue row that swallowed the start, or an operator who was shown a
    control they did not ask for and had to understand anyway. So the campaign screen is the campaign's own:
    start, stop, the delay, the wording, the channel list — and the sender that obeys none of the queue's
    switches.
    """
    control, api, db, _w = joinreq_bot(campaign=dict(CAMPAIGN_READY))
    control.settings = control.settings.model_copy(update={"worker_enabled": False})

    await control.dispatch(parse_update(_press(api, "x:/joinreq open #21")))
    labels = [
        str(one.get("text") or "") for row in api.markups[-1]["inline_keyboard"] for one in row
    ]
    assert not any("queue" in label.casefold() for label in labels), labels
    assert not any("worker" in label.casefold() for label in labels), labels
    text = api.sent[-1][1]
    assert "queue worker is OFF" not in text, text

    await control.dispatch(parse_update(_press(api, "x:/joinreq go #21")))
    reply = api.sent[-1][1]
    assert "is on" in reply, reply
    assert "queue" not in reply.casefold(), reply


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_the_sender_says_what_it_last_did_about_this_campaign() -> None:
    """"Sending" is a claim about a process, so the screen quotes the process.

    The loop keeps its own tally (`CampaignLoop.snapshot()`), and the screen prints it: sent, waiting, and
    the delay in force. The old screen quoted a queue row's `next_attempt_at`, and when the row was not the
    campaign's the operator got a number that described nothing.
    """
    control, api, db, _w = joinreq_bot(campaign={**CAMPAIGN_READY, "status": "running", "sent": 20})
    control.sender_state = lambda: {
        "absent": False,
        "running": True,
        "campaigns": {"7": {"sent": 20, "waiting": 320, "gap": 3.0, "more": True}},
    }

    await control.dispatch(parse_update(_press(api, "x:/joinreq open #21")))
    text = api.sent[-1][1]

    assert "20 sent in its last pass" in text, text
    assert "320 people are past what one read can reach" in text, text
    assert "it goes back for them" in text, text
    assert "one message every 3 s" in text, text


@pytest.mark.asyncio
async def test_a_campaign_that_is_on_with_no_sender_says_that_too() -> None:
    """The one sentence the operator needed when the run stopped at twenty and nothing said why.

    A campaign row that says `ready` and a service with no worker were two facts nobody joined up. Here the
    screen joins them and names the tap that fixes it, because the other half of "it does not stop by itself"
    is "unless the thing that sends it is off".
    """
    control, api, db, _w = joinreq_bot(campaign={**CAMPAIGN_READY, "status": "ready"})
    control.sender_state = lambda: {"absent": True}

    await control.dispatch(parse_update(_press(api, "x:/joinreq open #21")))
    text = api.sent[-1][1]

    assert "no sender running" in text, text
    assert "/status" in text, text
    assert "wakes in about" not in text, "there is no timer to wait for any more"
    assert "queue" not in text.casefold(), "and the reason is not explained with the queue's vocabulary"


@pytest.mark.asyncio
async def test_a_fault_in_the_last_pass_is_printed_rather_than_swallowed() -> None:
    """A loop that caught an exception has to say so somewhere the operator will look.

    The loop never dies and never blames a person for it, which is right, and it would be quietly wrong if
    nothing showed: "it is on, nothing is happening, and the log is on another machine" is the report that
    started this redesign. So the last fault rides on the campaign's own screen.
    """
    control, api, db, _w = joinreq_bot(campaign={**CAMPAIGN_READY, "status": "ready"})
    control.sender_state = lambda: {
        "absent": False,
        "running": True,
        "campaigns": {"7": {"error": "no Telegram session is open", "waiting": 320}},
    }

    await control.dispatch(parse_update(_press(api, "x:/joinreq open #21")))
    text = api.sent[-1][1]

    assert "the last pass faulted: no Telegram session is open" in text, text
    # Two short lines, not one run-on sentence: whether the sender is awake and what its last pass did, then
    # the reason nothing moved. The screen has already printed the headcounts and the delay above, so a third
    # copy of them here is what made these screens unreadable.
    assert "sending: awake, nothing went out in its last pass." in text, text


@pytest.mark.asyncio
async def test_the_delay_is_one_button_that_shows_what_is_set_today() -> None:
    """One control for one number, and the label answers the question the tap would otherwise ask.

    The pace is `⏱ Set delay (3 s)` and nothing else: no row of preset seconds, no ceiling taps, no button
    that changes a number nobody picks. The payload is the console's own prompt (`p:delay:<destination>`), so
    arming the question, the ✖ that abandons it and the clearing of a stale question are all behaviour this
    bot already had — a screen that wants a number should ask for one, not become a keypad.
    """
    control, api, db, _w = joinreq_bot(campaign={**CAMPAIGN_READY, "status": "ready"})

    await control.dispatch(parse_update(_press(api, "x:/joinreq open #21")))
    rows = api.markups[-1]["inline_keyboard"]
    labels = [str(one.get("text") or "") for row in rows for one in row]
    taps = [str(one.get("callback_data") or "") for row in rows for one in row]

    assert "⏱ Set delay (3 s)" in labels, labels
    assert "p:delay:d21" in taps, taps
    assert not [label for label in labels if "an hour" in label], labels
    assert not [label for label in labels if label.startswith("♾")], labels
    assert sum(1 for label in labels if label.startswith("⏱")) == 1, labels


@pytest.mark.asyncio
async def test_tapping_set_delay_asks_for_a_number_and_the_number_answered_is_the_number_set() -> None:
    """Tap, type `7`, done — the whole flow in two actions, with no command to remember.

    The answer runs through `/campaign … gap`, the same command the typed path runs, so the tap cannot accept
    a value the keyboard would reject. And the screen that comes back is the one the operator was on, with the
    new spacing printed on it: a change they cannot see is a change they will tap again.
    """
    control, api, db, _w = joinreq_bot(campaign={**CAMPAIGN_READY, "status": "ready"})

    await control.dispatch(parse_update(_press(api, "p:delay:d21")))
    asked = api.sent[-1][1]

    assert "Send the number of seconds to leave between two messages" in asked, asked
    assert "1 second is the fastest" in asked, asked
    assert "Dekin no mogura Anime in Hindi" in asked, "the question has to say which channel it is about"

    await control.dispatch(update("7"))

    assert db.campaign["per_message_delay_seconds"] == 7, db.campaign
    text = api.sent[-1][1]
    assert "one message every 7 seconds" in text, text
    assert "keeps going until you tap \u23f8" in text, text

    await control.dispatch(parse_update(_press(api, "x:/joinreq open #21")))

    assert "⏱ Set delay (7 s)" in [
        str(one.get("text") or "") for row in api.markups[-1]["inline_keyboard"] for one in row
    ], api.markups[-1]


@pytest.mark.asyncio
async def test_a_delay_below_one_second_is_refused_rather_than_written() -> None:
    """Whatever the operator types, one second stays the floor — and the row is left exactly as it was.

    The column's own check allows 0, and the bot refuses it anyway: back-to-back DMs to a few hundred
    strangers is the shape that gets an account restricted, and a campaign that has to keep running cannot
    be bought with the account that runs it. The refusal says why, and says the number it did not write.
    """
    control, api, db, _w = joinreq_bot(campaign={**CAMPAIGN_READY, "status": "ready"})

    await control.dispatch(parse_update(_press(api, "p:delay:d21")))
    await control.dispatch(update("0"))

    assert db.campaign["per_message_delay_seconds"] == 3, db.campaign
    # The refusal is the first half of the answer, and the screen that follows still says 3 seconds: a value
    # that was refused has to be invisible in the state as well as in the reply.
    text = "\n".join(one[1] for one in api.sent[-2:])
    assert "not something I will write" in text, text
    assert "One second is the floor" in text, text
    assert "one message every 3 seconds" in text, text
    assert not [sql for sql, _ in db.writes if "per_message_delay_seconds" in sql], db.writes


@pytest.mark.asyncio
async def test_answering_a_question_with_a_command_leaves_the_question_open() -> None:
    """The operator stopped to check something, and a half-asked question must not be eaten by it.

    A bare number is what an answer looks like; a `/cancel` is the way out, and after it the next number is
    *not* read as the answer — which is the difference between a prompt and a trap.
    """
    control, api, db, _w = joinreq_bot(campaign={**CAMPAIGN_READY, "status": "ready"})

    await control.dispatch(parse_update(_press(api, "p:delay:d21")))
    await control.dispatch(update("/status"))
    await control.dispatch(update("7"))

    assert db.campaign["per_message_delay_seconds"] == 7, db.campaign

    await control.dispatch(parse_update(_press(api, "p:delay:d21")))
    cancelled = text_of(await control._cancel(update("/cancel"), []))
    assert "the question that screen asked is dropped" in cancelled, cancelled
    before = len(api.sent)
    await control.dispatch(update("99"))
    assert len(api.sent) == before, "an abandoned question still ate the next message"
    assert db.campaign["per_message_delay_seconds"] == 7, db.campaign


@pytest.mark.asyncio
async def test_the_plan_prints_the_spacing_and_says_what_stops_the_run() -> None:
    """The plan is the preview of the run, so it prints the row's own delay and the only stop there is.

    Nothing about an hour appears on this screen any more, because nothing about an hour stops the list any
    more: a plan that named a ceiling the code no longer honours would be the exact defect this campaign has
    had twice already — a screen promising a limit, and a queue that never applied it.
    """
    control, api, db, _w = joinreq_bot(
        campaign={**CAMPAIGN_READY, "status": "ready", "per_message_delay_seconds": 30}
    )

    text = text_of(await control._campaign(None, ["-1001234", "plan", "default"]))

    assert "one message every 30 seconds" in text, text
    assert "until the list is empty or you tap \u23f8 Stop after this one" in text, text
    assert "per hour" not in text, text
@pytest.mark.asyncio
async def test_the_start_screen_names_the_switch_that_would_silence_it() -> None:
    """A campaign that is on in a service that will not send needs the reason on the same screen.

    Three settings can each stop a campaign on their own — the service-wide pause, the deployment's mode, and
    an account with no stored session. The screen reads all three and prints only the ones that are actually
    wrong, each with the tap that fixes it. The queue worker is not on this list any more, because sending
    does not go through the queue: an operator who had to start a worker to make their DMs move was shown a
    control that belonged to another feature.
    """
    from app import joinmsg

    control, api, db, _w = joinreq_bot(campaign=dict(CAMPAIGN_READY))
    await db.set_paused(True, "nightly maintenance")
    code = joinmsg.confirm_code(7, CAMPAIGN_READY["message_template"])

    text = "\n".join(r.text for r in await control._campaign(None, ["-1001234", "confirm", "default", code]))

    assert "service is PAUSED (nightly maintenance)" in text, text
    assert "/resume" in text, text
    assert "no Telegram session is active" in text, text


@pytest.mark.asyncio
async def test_a_plan_that_cannot_read_the_list_says_so_instead_of_showing_zero() -> None:
    """"0 pending" and "the list could not be read" are two different decisions.

    Zero tells the operator there is nobody to write to and the campaign looks pointless; the reason tells
    them the session is not reachable and the run will read the list again when it goes out. The count is a
    read, so it is attempted in shadow mode too — but a failed read is never reported as a number.
    """
    campaign = {
        "id": 7,
        "name": "default",
        "status": "draft",
        "message_template": "{name}, aapka request dekh liya jaa raha hai",
        "rate_per_hour": 20,
        "confirm_required": True,
    }
    control, api, db, _w = joinreq_bot(campaign=campaign)

    async def _broken(peer: str) -> tuple[int | None, int | None, str | None]:
        return None, None, "SessionNotFoundError: no stored session for this account"

    control._pending_requests = _broken  # type: ignore[method-assign]
    await control.dispatch(parse_update(_press(api, "x:/joinreq start #21")))
    text = api.sent[-1][1]
    assert "could not be read from this account" in text and "SessionNotFoundError" in text, text
    assert "0 request(s)" not in text


def test_the_control_bot_never_reaches_for_a_client_it_does_not_hold() -> None:
    """`self.telegram` did not exist on this class, and the crash it caused was the operator's whole feature.

    `/campaign … plan` read `self.telegram.client` under an `outbound_enabled` guard, so every test in
    shadow mode took the other branch and the bug only appeared once the deployment was live — which is
    exactly the shape this audit exists to catch: a code path that production settings enable and the suite
    never walks. The control bot opens its own short-lived session (`_pending_requests`, `_discover_dialogs`),
    so there is nothing to hold and nothing to reach for.
    """
    import ast  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    from app import controlbot as module  # noqa: PLC0415

    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    held = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.attr == "telegram"
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    ]
    assert not held, f"ControlBot holds no `telegram`; the reads build a session (lines {held})"
    # The replacement has to stay, or the audit above would be satisfied by deleting the feature.
    named = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
    }
    assert "_pending_requests" in named
