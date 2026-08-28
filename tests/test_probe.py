"""The protocol probe and its guard.

The probe is the only place where the service uses a *user* session to talk to
third parties, so these tests are mostly about what it is forbidden to do. A
guard that only exists in a docstring is the failure mode this file exists to
prevent: the fake client records every call, and the assertions fail if the probe
ever reaches outside two bot chats and a dozen menu commands.
"""

from __future__ import annotations

import asyncio
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

    async def __call__(self, request: Any) -> Any:  # any typed MTProto request
        raise RuntimeError("typed requests are not needed by the probe")

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
                    "command_list": [f"/c{i}" for i in range(60)],
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
                    "command_list": ["/start", "/links"],
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
