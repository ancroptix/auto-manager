"""The protocol probe and its guard.

The probe is the only place where the service uses a *user* session to talk to
third parties, so these tests are mostly about what it is forbidden to do. A
guard that only exists in a docstring is the failure mode this file exists to
prevent: the fake client records every call, and the assertions fail if the probe
ever reaches outside two bot chats and a dozen menu commands.
"""

from __future__ import annotations

import asyncio
import pathlib
from types import SimpleNamespace
from typing import Any

import pytest

from app.probe import (
    MAX_REPORT_CHARS,
    ProbeBudget,
    ProbePolicy,
    ProbeViolation,
    _describe_buttons,
    _send,
    format_report,
    run_probe,
)


class FakeButton:
    def __init__(self, text: str, *, data: bytes | None = None, url: str | None = None) -> None:
        self.text = text
        self.data = data
        self.url = url
        self.game = None


class FakeMessage:
    def __init__(self, text: str, buttons: list[list[FakeButton]], *, out: bool = False) -> None:
        self.text = text
        self.message = text
        self.buttons = buttons
        self.media = None
        self.out = out
        self.clicks: list[tuple[int, int]] = []

    async def click(self, row: int = 0, col: int = 0, **_kw: Any) -> Any:
        self.clicks.append((row, col))
        return True


class FakeClient:
    """Records everything it is asked to do, and refuses nothing — that is the point."""

    def __init__(self, buttons: list[list[FakeButton]] | None = None) -> None:
        self.sent: list[tuple[Any, str, dict[str, Any]]] = []
        self.requests: list[Any] = []
        self.buttons = buttons if buttons is not None else [[FakeButton("Cancel", data=b"cancel")]]
        self.message = FakeMessage("Welcome! Send me a file to store.", self.buttons)
        self.authorized = True

    async def send_message(self, peer: Any, text: str = "", **kwargs: Any) -> FakeMessage:
        self.sent.append((peer, text, kwargs))
        return self.message

    async def get_me(self) -> Any:
        class Me:
            id = 555
            username = "spare_account"
            restricted = False
            premium = False

        return Me()

    def iter_messages(self, peer: Any, limit: int = 1):
        async def gen():
            yield self.message

        return gen()

    def iter_dialogs(self):
        class Entity:
            def __init__(self, title: str, username: str | None, *, mine: bool = False, left: bool = False) -> None:
                self.title, self.username = title, username
                self.creator, self.left = mine, left
                self.participants_count = 1234
                self.broadcast = True
                self.admin_rights = None

        class Dialog:
            def __init__(self, entity: Any) -> None:
                self.entity = entity

        dialogs = [
            Dialog(Entity("YC Anime", "ycanime_bleach")),
            Dialog(Entity("Master Archive", "ycanime_archive", mine=True)),
            Dialog(Entity("Some Stranger", None, left=True)),
        ]

        async def gen():
            for dialog in dialogs:
                yield dialog

        return gen()

    async def get_entity(self, username: Any) -> Any:
        """What a handle resolves to, shaped just enough for ``utils.get_input_user``.

        ``0xe669bf46`` is that helper's test for "already an InputUser", and the probe needs an
        ``InputUser`` — not a username string — in ``users.getFullUser``.
        """
        return SimpleNamespace(id=77, access_hash=99, username=username, SUBCLASS_OF_ID=0xE669BF46)

    async def __call__(self, request: Any) -> Any:  # any typed MTProto request
        name = type(request).__name__
        if name == "GetFullUserRequest":
            self.requests.append(request)
            # The shape the server really answers with: a *wrapper* around the record, so anything read
            # off this object directly (as `bot_info` once was) is silently missing rather than wrong.
            return SimpleNamespace(
                full_user=SimpleNamespace(
                    id=77,
                    about="Send me a file and I give back a permanent link. /batch stores a whole set.",
                    bot_info=SimpleNamespace(
                        commands=[
                            SimpleNamespace(command="batch", description="Store files"),
                            SimpleNamespace(command="start", description=""),
                        ],
                        menu_button=SimpleNamespace(text="Open store"),
                    ),
                ),
                chats=[],
                users=[SimpleNamespace(id=77, bot=True)],
            )
        raise RuntimeError(f"unexpected typed request: {name}")

    # -- helpers for assertions
    @property
    def peers(self) -> list[Any]:
        return [peer for peer, _text, _kw in self.sent]

    @property
    def texts(self) -> list[str]:
        return [text for _peer, text, _kw in self.sent]


def policy(**overrides: Any) -> ProbePolicy:
    base = dict(storage_bot="anime_hindifilesbot", channel_help="chelpbot", owner_user_id=999, settle_seconds=0.0)
    base.update(overrides)
    return ProbePolicy(**base)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class TestGuard:
    def test_a_stranger_is_never_messaged(self) -> None:
        client = FakeClient()
        with pytest.raises(ProbeViolation):
            run(_send(client, "some_subscriber", "/start", policy(), _run_state()))
        assert client.sent == []

    def test_free_text_is_refused_even_to_an_allowed_peer(self) -> None:
        # The allowed peers are bots, but "hello, do you have ep 12?" would still
        # be a human-shaped conversation started by an automation.
        with pytest.raises(ProbeViolation):
            run(_send(FakeClient(), "anime_hindifilesbot", "hello there", policy(), _run_state()))

    def test_menu_navigation_is_allowed(self) -> None:
        client = FakeClient()
        run(_send(client, "@anime_hindifilesbot", "/start", policy(), _run_state()))
        run(_send(client, "chelpbot", "Cancel", policy(), _run_state()))
        assert client.texts == ["/start", "Cancel"]

    def test_media_and_forwarding_are_not_reachable_through_send(self) -> None:
        client = FakeClient()
        run(_send(client, "chelpbot", "/help", policy(), _run_state()))
        # The one sender in the module takes no file/forward kwargs, so an added
        # argument has nowhere to go; assert that stays true.
        assert client.sent[0][2] == {}
        import inspect

        from app.probe import run_probe as _rp

        source = inspect.getsource(inspect.getmodule(_rp) or _rp)
        for forbidden in ("send_file", "send_read_photo", "forward_messages", "create_channel", "send_inline"):
            assert forbidden not in source, f"probe must not contain {forbidden}"

    def test_message_budget_stops_the_probe_without_calling_it_a_bug(self) -> None:
        state = _run_state()
        client = FakeClient()
        with pytest.raises(ProbeBudget):
            for _ in range(20):
                run(_send(client, "chelpbot", "/start", policy(max_messages=2), state))
        assert len(client.sent) == 2

    def test_owner_id_is_an_allowed_peer_but_other_ids_are_not(self) -> None:
        assert policy().may_send(999, "/start")
        assert not policy().may_send(1000, "/start")

    def test_no_other_send_path_exists_in_the_module(self) -> None:
        """Structural check: the guard is the only sender.

        A second ``client.send_message`` call site would be a policy hole that no
        behavioural test is guaranteed to notice, so it is checked by syntax tree:
        exactly two functions may send, and both are audited by name.
        """
        import ast
        import inspect

        from app import probe as module

        tree = ast.parse(inspect.getsource(module))
        senders = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
            and "send_message" in ast.get_source_segment(inspect.getsource(module), node)
        }
        assert senders == {"_send", "_deliver"}, senders

    def test_the_probe_never_touches_media_channels_or_permissions(self) -> None:
        """Names that would cause the harm this probe is not allowed to cause."""
        import inspect

        from app import probe as module

        source = inspect.getsource(module)
        for forbidden in (
            "send_file",
            "send_message(peer, text, file",
            "forward_messages",
            "download_media",
            "create_channel",
            "edit_admin",
            "import_chat_invite",
            "approve",
            "decline",
            "delete_dialog",
            "send_inline",
        ):
            assert forbidden not in source, f"probe must not contain {forbidden!r}"


def _run_state():  # noqa: ANN202 - private test helper
    from app.probe import _Run

    return _Run()


class TestProbeBehaviour:
    def test_only_start_is_sent_and_the_report_comes_back(self) -> None:
        client = FakeClient()
        report = run(run_probe(client, policy=policy(), send=False))
        # Three bots now, one command each: the link provider joined the list because its menu is
        # the last unread half of a flow the operator already runs by hand.
        assert client.texts == ["/start"] * 3
        assert client.peers == ["anime_hindifilesbot", "chelpbot", "Link_providerobot"]
        assert report["storage_bot"]["first"]["buttons"][0]["text"] == "Cancel"
        assert report["messages_sent"] == 3
        assert "delivery" not in report

    def test_scary_buttons_are_never_pressed(self) -> None:
        client = FakeClient(
            buttons=[
                [FakeButton("⬆️ Upload files now", data=b"upload")],
                [FakeButton("Delete all my files", data=b"wipe")],
                [FakeButton("Cancel", data=b"cancel")],
            ]
        )
        report = run(run_probe(client, policy=policy(), send=False))
        pressed = [p for p in report["storage_bot"]["pressed"] if "button" in p]
        assert [p["button"] for p in pressed if not p.get("skipped")] == ["Cancel"]
        # The click coordinates are the real assertion: the probe must have asked
        # to press row 2 (Cancel), and only that.
        # One click per probed bot, all three on the Cancel row. The same fake message
        # object answers every bot, so the count is the check that nothing else
        # was pressed while the set is the check that only that button was.
        assert set(client.message.clicks) == {(2, 0)}
        assert len(client.message.clicks) == 3

    def test_url_buttons_are_reported_not_visited(self) -> None:
        client = FakeClient(buttons=[[FakeButton("Open store", url="https://example.com/premium")]])
        report = run(run_probe(client, policy=policy(), send=False))
        pressed = report["storage_bot"]["pressed"]
        assert pressed and pressed[0].get("skipped") and "host" in report["storage_bot"]["first"]["buttons"][0]

    def test_a_bot_that_does_not_answer_is_recorded_not_raised(self) -> None:
        class Silent(FakeClient):
            def iter_messages(self, peer: Any, limit: int = 1):
                async def gen():
                    if False:  # pragma: no cover - generator that yields nothing
                        yield None

                return gen()

        report = run(run_probe(Silent(), policy=policy(), send=False))
        assert "no reply" in report["storage_bot"]["error"]
        assert "delivery" not in report  # a dry run never messages anyone

    def test_dialog_scan_marks_what_the_account_cannot_see(self) -> None:
        client = FakeClient()
        report = run(
            run_probe(
                client,
                policy=policy(),
                send=False,
            )
        )
        account = report["account"]
        assert account["id"] == 555 and account["dialog_count"] == 3
        assert account["owned_channels"] == ["Master Archive"]

    def test_missing_expected_channel_is_reported(self) -> None:
        client = FakeClient()
        from app.probe import probe_account, _Run

        report = run(
            probe_account(
                client,
                policy=policy(),
                run=_Run(),
                expected=[{"username": "@ycanime_bleach"}, {"username": "@absent_channel"}],
            )
        )
        assert [m["want"] for m in report["missing_channels"]] == ["@absent_channel"]

    def test_report_is_delivered_only_to_the_owner(self) -> None:
        client = FakeClient()
        report = run(run_probe(client, policy=policy(), send=True))
        assert report["delivery"].startswith("sent to owner id=999")
        peers = {str(peer) for peer in client.peers}
        assert peers == {"anime_hindifilesbot", "chelpbot", "Link_providerobot", "999"}

    def test_no_owner_means_no_delivery(self) -> None:
        client = FakeClient()
        report = run(run_probe(client, policy=policy(owner_user_id=None), send=True))
        assert "unset" in report["delivery"]
        assert "999" not in {str(p) for p in client.peers}


class TestReport:
    def test_report_is_bounded_no_matter_how_messy_the_input_is(self) -> None:
        report = format_report(
            {
                "storage_bot": {
                    "username": "b",
                    "first": {
                        "reply": "y" * 5000,
                        "buttons": [{"text": f"b{i}", "kind": "callback", "data": "d" * 400} for i in range(200)],
                    },
                    "pressed": [{"button": f"p{i}", "reply_chars": 1, "buttons_after": ["x"] * 50} for i in range(50)],
                    "bot_profile": {"commands": [f"/c{i}" for i in range(60)]},
                },
                "channel_help": {"username": "c", "first": {"reply": "", "buttons": []}},
                "account": {
                    "id": 1,
                    "username": "u",
                    "dialog_count": 0,
                    "owned_channels": [f"chan {i}" for i in range(200)],
                    "missing_channels": [{"want": f"@c{i}"} for i in range(200)],
                },
            }
        )
        assert len(report) <= MAX_REPORT_CHARS

    def test_the_truncation_note_points_at_the_full_copy(self) -> None:
        report = format_report(
            {"account": {"id": 1, "username": "u" * (MAX_REPORT_CHARS + 500)}, "storage_bot": {}, "channel_help": {}}
        )
        assert len(report) <= MAX_REPORT_CHARS and "audit_log" in report

    def test_report_leads_with_the_answers_that_unblock_work(self) -> None:
        text = format_report(
            {
                "storage_bot": {
                    "username": "anime_hindifilesbot",
                    "first": {
                        "reply": "Send me a file and I will make a link.",
                        "buttons": [{"text": "My files", "kind": "callback", "data": "list:1"}],
                    },
                    "pressed": [{"button": "My files", "reply_chars": 220, "buttons_after": ["Back"]}],
                    "bot_profile": {"commands": ["/start", "/links"]},
                },
                "channel_help": {"username": "chelpbot", "first": {"reply": "Forward me a post.", "buttons": []}},
                "account": {"id": 7, "username": "spare", "dialog_count": 4, "owned_channels": ["Archive"]},
            }
        )
        assert "anime_hindifilesbot" in text and "My files" in text and "data=list:1" in text
        assert "Paste this whole message back" in text
        assert text.index("storage bot") < text.index("channel help")

    def test_guard_violation_is_the_first_thing_said(self) -> None:
        text = format_report({"violation": "refusing to send", "account": {}, "storage_bot": {}, "channel_help": {}})
        assert text.startswith("auto-manager") and "STOPPED BY GUARD" in text

    def test_button_shape_is_described_without_telethon(self) -> None:
        message = type(
            "M",
            (),
            {"buttons": [[FakeButton("A", data=b"bytes-text"), FakeButton("U", url="https://files.example/x")]]},
        )()
        buttons = _describe_buttons(message)
        assert [b["kind"] for b in buttons] == ["callback", "url"]
        assert buttons[1]["host"] == "files.example"
        assert buttons[0]["data"] == "bytes-text"


class TestWiring:
    def test_probe_endpoint_is_documented(self) -> None:
        from app.api import router

        paths = {route.path for route in router.routes}
        assert "/control/probe" in paths

    def test_probe_endpoint_refuses_without_a_live_session(self, make_settings) -> None:
        from fastapi.testclient import TestClient

        from app.main import create_app

        app = create_app(make_settings(control_token="t" * 40, app_mode="shadow"), start_worker=False)
        with TestClient(app) as client:
            response = client.post("/control/probe", headers={"Authorization": "Bearer " + "t" * 40})
        assert response.status_code == 503
        assert "live" in response.json()["detail"]

    def test_probe_on_boot_is_off_by_default_and_listed_in_the_blueprint(self) -> None:
        from pathlib import Path

        from app.config import Settings

        assert Settings(_env_file=None, worker_enabled=False).probe_on_boot is False
        blueprint = Path("render.yaml").read_text(encoding="utf-8")
        assert "PROBE_ON_BOOT" in blueprint and 'value: "false"' in blueprint


class TestRightsWiring:
    """The probe is the only code that ever sees the dialog list, so it is also where our own rights
    are read and written (``app/rights.py``). These tests pin the three seams: the SELECT shape, the
    write, and the line the operator actually reads."""

    class Db:
        def __init__(self, rows: list[dict], *, fail: bool = False) -> None:
            self.rows, self.fail = rows, fail
            self.sql: list[tuple[str, tuple]] = []
            self.connected = True

        async def fetch(self, sql: str, *args: Any) -> list[dict]:
            # `app.rights.record` writes with `update … returning id`, so a write is a fetch here.
            # Failing on any statement would also fail the SELECT, and the test wants to distinguish
            # "the read happened" from "the write landed".
            self.sql.append((sql, args))
            if self.fail and sql.lstrip().lower().startswith("update"):
                raise RuntimeError("read-only replica")
            if sql.lstrip().lower().startswith("update"):
                return [{"id": args[0]}]
            return list(self.rows)

        async def execute(self, sql: str, *args: Any) -> int:  # pragma: no cover - unused by record
            self.sql.append((sql, args))
            if self.fail:
                raise RuntimeError("read-only replica")
            return 1

    def test_the_expected_query_selects_the_keys_the_matcher_needs(self) -> None:
        client = FakeClient()
        db = self.Db([])
        run(run_probe(client, policy=policy(), db=db, send=False))
        selects = [sql for sql, _ in db.sql if sql.strip().startswith("select")]
        assert selects, "the probe stopped reading the configured channels"
        for column in ("id", "username", "telegram_channel_id", "we_are_admin"):
            assert column in selects[0], f"{column} is how app.rights finds the row; the SELECT lost it"

    def test_a_configured_channel_it_can_see_is_reported_with_its_rights(self) -> None:
        client = FakeClient()
        db = self.Db([{"id": 3, "username": "@ycanime_bleach", "telegram_channel_id": -100111, "we_are_admin": None}])
        report = run(run_probe(client, policy=policy(), db=db, send=False))
        decided = report["account"]["rights"]
        updates = decided["updates"]
        # The fake entity has no `admin_rights`, so the honest answer is "member" — False, written,
        # and never None: a probe that read the channel and found nothing must change the row.
        assert [u["source_channel_id"] for u in updates] == [3]
        assert updates[0]["we_are_admin"] is False and updates[0]["can_edit"] is False
        assert decided["unseen"] == []
        writes = [sql for sql, _ in db.sql if sql.strip().startswith("update")]
        assert writes and "app.source_channel" in writes[0] and "rights_checked_at = now()" in writes[0]

    def test_a_configured_channel_it_cannot_see_is_said_out_loud(self) -> None:
        client = FakeClient()
        db = self.Db([{"id": 9, "username": "@never_seen", "telegram_channel_id": -1009, "we_are_admin": True}])
        report = run(run_probe(client, policy=policy(), db=db, send=False))
        assert report["account"]["rights"]["unseen"] == ["@never_seen"]
        assert report["account"]["rights"]["updates"] == [], "absence must not write anything"
        assert not [sql for sql, _ in db.sql if sql.strip().startswith("update")]
        text = format_report(report)
        assert "@never_seen" in text, "the report has to name the channel the probe could not read"

    def test_a_failed_write_keeps_the_report(self) -> None:
        client = FakeClient()
        db = self.Db(
            [{"id": 3, "username": "@ycanime_bleach", "telegram_channel_id": -100111, "we_are_admin": None}],
            fail=True,
        )
        report = run(run_probe(client, policy=policy(), db=db, send=False))
        assert report["account"]["rights"]["updates"], "the read still happened"
        assert report["rights_recorded"] == {"considered": 1, "written": 0}
        assert "could not be recorded" in format_report(report) or report["rights_error"]


class TestConfiguredBotHandles:
    """`/probe` has to stand in front of the same bot the writers will write to.

    `bots.storage_username` and `bots.channel_help_username` are rows for a reason — a re-cloned bot is
    a different handle and the same protocol — and `ProbePolicy`'s own defaults are the last resort, not
    the answer. A probe that reported on the default while the job sent to the configured peer would have
    been a confident, wrong document.
    """

    class Db:
        connected = True

        def __init__(self, values: dict[str, Any]) -> None:
            self.values = values
            self.asked: list[str] = []

        async def config(self, key: str, default: Any = None) -> Any:
            self.asked.append(key)
            return self.values.get(key, default)

        async def fetch(self, sql: str, *args: Any) -> list[dict]:
            return []

    def test_the_handles_named_in_the_database_are_the_ones_probed(self) -> None:
        db = self.Db({"bots.storage_username": "@my_clone_bot", "bots.channel_help_username": "help_clone"})
        client = FakeClient()
        report = run(run_probe(client, policy=policy(), db=db, send=False))

        peers = [str(peer) for peer, _text, _kw in client.sent]
        assert "my_clone_bot" in peers and "help_clone" in peers, peers
        assert report["storage_bot"]["username"] == "my_clone_bot", report["storage_bot"]
        assert report["channel_help"]["username"] == "help_clone", report["channel_help"]
        assert sorted(db.asked) == ["bots.channel_help_username", "bots.storage_username"]

    def test_an_absent_row_leaves_the_policy_default_alone(self) -> None:
        client = FakeClient()
        report = run(run_probe(client, policy=policy(), db=self.Db({}), send=False))

        assert report["channel_help"]["username"] == policy().channel_help.lstrip("@")
        assert not [p for p, _t, _k in client.sent if "clone" in str(p)], "an empty row must not invent a peer"

    def test_a_database_that_cannot_answer_does_not_stop_the_probe(self) -> None:
        class Grumpy(self.Db):
            async def config(self, key: str, default: Any = None) -> Any:
                raise RuntimeError("relation app.config does not exist")

        client = FakeClient()
        report = run(run_probe(client, policy=policy(), db=Grumpy({}), send=False))

        assert report["storage_bot"]["username"] == policy().storage_bot.lstrip("@")
        assert "steps" in report


class TestDeliveryKeepsEveryCharacter:
    """The report is the one message nobody may receive half of.

    ``MAX_REPORT_CHARS`` exists so the whole thing fits in a single Telegram message, and the cap in
    ``format_report`` is what enforces it — which means the cap and the cut used to be the same bug
    wearing two hats: whatever the cap dropped was also what the operator never saw, and the note
    pointing at ``app.audit_log`` promised a copy that was not there.
    """

    def test_the_report_budget_fits_under_the_message_limit_it_is_sent_through(self) -> None:
        from app.sender import MAX_MESSAGE_CHARS

        assert MAX_REPORT_CHARS < MAX_MESSAGE_CHARS, (
            "the cap is what keeps the report in one send; if it grows past Telegram's limit the "
            "transport has to split, and then a cut message and a capped message are the same loss"
        )

    def test_the_audited_copy_is_the_uncapped_one_the_note_promises(self) -> None:
        from app.probe import run_probe

        rows: list[tuple[str, tuple]] = []

        class AuditDb:
            connected = True

            async def execute(self, sql: str, *params: Any) -> None:
                rows.append((sql, params))

            async def fetch(self, sql: str, *params: Any) -> list[Any]:
                return []

            async def fetchrow(self, sql: str, *params: Any) -> None:
                return None

            async def fetchval(self, sql: str, *params: Any) -> None:
                return None

        client = FakeClient()
        client.authorized = True
        report = run(run_probe(client, policy=policy(), db=AuditDb(), send=False))
        inserts = [row for row in rows if "audit_log" in row[0]]
        assert inserts, "a probe report that is not audited is a probe nobody can check afterwards"
        detail = inserts[0][1][1]
        assert detail["summary"] == report["report"], (
            "the message and the row have to be the same rendering or 'the full version is in the "
            "database' is a sentence about a different document"
        )

    def test_a_delivery_too_long_for_one_message_is_sent_in_parts(self) -> None:
        from app.probe import _deliver

        client = FakeClient()
        long_text = "\n".join(f"row {i} " + "z" * 80 for i in range(120))
        assert len(long_text) > 4096
        note = run(_deliver(client, long_text, policy=policy()))
        assert note.startswith("sent to owner id=") and "parts" in note, note
        delivered = [text for peer, text in ((str(p), t) for p, t in zip(client.peers, client.texts)) if peer == "999"]
        assert all(len(part) <= 4096 for part in delivered)
        assert "\n".join(delivered) == long_text, "split, never shortened"


class TestBotProfile:
    """The one line of the report that is about what a bot *accepts*, and the API that can say it.

    ``commands:`` used to end every bot section as ``(unavailable: BotInvalidError: This is not a valid
    bot)`` — which is a true fact about the wrong request: ``bots.getBotInfo`` belongs to a bot's owner,
    not to someone talking to their bot. ``users.getFullUser`` is the one Telegram's own clients use.
    """

    def test_the_profile_request_is_the_one_that_answers_for_someone_elses_bot(self) -> None:
        import pytest

        pytest.importorskip("telethon")
        client = FakeClient()
        report = run(run_probe(client, policy=policy(), send=False))

        assert client.requests, "the probe asked nothing, so the report is a guess"
        request = client.requests[0]
        assert type(request).__name__ == "GetFullUserRequest", (
            "bots.getBotInfo answers BOT_INVALID for any bot the account does not own, which is every "
            "third-party bot this pipeline talks to"
        )
        assert request.id.id == 77, "the field is an InputUser, and a username string is not one"

        profile = report["storage_bot"]["bot_profile"]
        assert profile["commands"] == ["/batch=Store files", "/start"], (
            "a command with no description is still a command"
        )
        assert profile["menu_button"] == "Open store"
        assert "permanent link" in profile["about"]
        assert profile["is_bot"] is True, "the record says whether the peer is a bot at all"

        text = format_report(report)
        assert "commands: /batch=Store files /start" in text
        assert "menu button the user must press first: Open store" in text
        assert "profile text: Send me a file" in text

    def test_an_empty_answer_and_a_failed_read_are_two_different_sentences(self) -> None:
        """The distinction this report exists to make.

        A bot that declares nothing is a fact about the bot — most clones have no BotFather command list,
        and their menu is the whole protocol. A read that came back empty one level too high is a bug in
        us. Same silence in Telegram's reply, two very different things to tell the operator.
        """

        class NoBlock(FakeClient):
            async def __call__(self, request: Any) -> Any:
                return SimpleNamespace(
                    full_user=SimpleNamespace(id=77, about="", bot_info=None),
                    chats=[],
                    users=[SimpleNamespace(id=77, bot=True)],
                )

        class EmptyBlock(FakeClient):
            async def __call__(self, request: Any) -> Any:
                return SimpleNamespace(
                    full_user=SimpleNamespace(
                        id=77, about="", bot_info=SimpleNamespace(commands=[], menu_button=None)
                    ),
                    chats=[],
                    users=[SimpleNamespace(id=77, bot=True)],
                )

        missing_record = run(run_probe(NoBlock(), policy=policy(), send=False))
        declared_nothing = run(run_probe(EmptyBlock(), policy=policy(), send=False))

        assert "no bot block for this peer" in missing_record["storage_bot"]["bot_profile"]["empty"]
        assert (
            "no commands, no menu button, no profile text" in declared_nothing["storage_bot"]["bot_profile"]["empty"]
        )
        for report in (missing_record, declared_nothing):
            assert report["storage_bot"]["error"] is None, "an empty hint is not a probe failure"
            assert "unavailable" not in report["storage_bot"]["bot_profile"]
            assert "the bot declares nothing beyond its menu" in format_report(report)
            assert "peer marked as a bot: yes" in format_report(report)

    def test_a_flat_answer_is_read_too_because_layers_differ(self) -> None:
        """Both shapes are the record: the unwrap is an `or`, not a cast to the newest wrapper."""

        class Flat(FakeClient):
            async def __call__(self, request: Any) -> Any:
                return SimpleNamespace(
                    id=77,
                    about="I store files.",
                    bot_info=SimpleNamespace(
                        commands=[SimpleNamespace(command="genlink", description="")], menu_button=None
                    ),
                )

        profile = run(run_probe(Flat(), policy=policy(), send=False))["storage_bot"]["bot_profile"]
        assert profile["commands"] == ["/genlink"] and profile["about"] == "I store files.", profile
        assert "empty" not in profile

    def test_an_unavailable_hint_names_the_real_reason(self) -> None:
        class Broken(FakeClient):
            async def get_entity(self, username: Any) -> Any:
                raise TypeError("Cannot find any entity corresponding to no-such-bot")

        report = run(run_probe(Broken(), policy=policy(), send=False))
        reason = report["storage_bot"]["bot_profile"]["unavailable"]
        assert reason.startswith("TypeError:") and "no-such-bot" in reason, reason


def test_nothing_builds_the_owner_only_bot_info_request():
    """``bots.getBotInfo`` is a bot owner's call; this program talks to other people's bots.

    Pinned because the wrong request is not loud: it produced ``BOT_INVALID`` three times in the
    operator's report and read as three uncooperative bots. Any future code that reaches for the same
    class is asking the same question of the wrong API.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = [
        path.name
        for path in sorted((root / "app").rglob("*.py"))
        if "GetBotInfoRequest" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, f"these files use the owner-only bot info request: {offenders}"


class TestRefusedButtons:
    def test_buttons_the_policy_walks_past_are_still_reported(self) -> None:
        """Not pressing something is a decision; the report used to keep it to itself.

        The storage bot spells its menu in decorative letters — ``ʜᴇʟᴘ``, ``ᴀʙᴏᴜᴛ`` — and the allowlist is
        made of plain words it trusts, which is the right way to refuse a string a bot chose. What was
        wrong is that a reader saw "safe buttons pressed:" and two URL notes, and could only conclude the
        bot had nothing else on screen.
        """
        client = FakeClient([[FakeButton("ʙțȜȠ", data=b"help"), FakeButton("ᴀȚȏȢȜ", data=b"about")]])
        report = run(run_probe(client, policy=policy(), send=False))

        assert report["storage_bot"]["refused_buttons"] == ["ʙțȜȠ", "ᴀȚȏȢȜ"]
        assert report["storage_bot"]["pressed"] == [], "a refused button must not spend the press budget"
        assert "left alone by policy (2):" in format_report(report)

    def test_a_allowed_button_is_still_pressed(self) -> None:
        client = FakeClient()
        report = run(run_probe(client, policy=policy(), send=False))
        assert report["storage_bot"]["refused_buttons"] == []
        assert report["storage_bot"]["pressed"][0]["reply_chars"] > 0

    def test_a_full_press_budget_stops_the_clicks_and_not_the_reading(self) -> None:
        """The limit exists to bound what gets *pressed*; it must not also bound what gets seen.

        It used to be a break at the top of the loop, so a menu longer than the budget simply stopped
        being read, and the report could not tell a refusal from a button nobody looked at.
        """
        client = FakeClient(
            [
                [
                    FakeButton("help", data=b"help"),
                    FakeButton("ᴅᴄᴀᴏᴛᴄᴇ ᴀᴡᴇ", data=b"del"),
                    FakeButton("menu", data=b"menu"),
                    FakeButton("ᴜᴘᴅᴀᴛᴏᴟ", url="https://t.me/x"),
                ]
            ]
        )
        report = run(run_probe(client, policy=policy(max_button_probes=1), send=False))
        section = report["storage_bot"]
        clicks = [entry for entry in section["pressed"] if "skipped" not in entry]
        assert len(clicks) == 1, "one click, as budgeted - and a url note is not a click"
        assert section["refused_buttons"] == ["ᴅᴄᴀᴏᴛᴄᴇ ᴀᴡᴇ"], "the scan ran to the end of the menu"
