"""`app/keyboards.py` — the buttons a control bot is allowed to offer.

The property all of these tests circle is one sentence: a button is a command, typed for you. Anything
that makes that untrue — a button that runs an action no words can reach, a label that promises one state
and writes another, a payload Telegram will reject — turns the friendliest half of this interface into the
most dangerous, because a tap is how you press something without reading it.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from app import controlbot, joinmsg, keyboards, sourcecfg

SOURCE = Path(inspect.getsourcefile(keyboards)).read_text(encoding="utf-8")

ROW = {"require_hindi_audio": True, "include_subbed": False, "mode": "full"}


def _buttons(payload) -> list[dict]:
    if not payload:
        return []
    return [one for row in payload["inline_keyboard"] for one in row]


# --------------------------------------------------------------------- the derivation
def test_the_switch_buttons_are_the_switches_and_nothing_else() -> None:
    """Four buttons here, three in `app/sourcecfg.py`, is a bug in one of them."""
    payload = keyboards.source_switches("@anime_uploads4u", ROW)
    names = [one["callback_data"].rsplit(" ", 2)[1] for one in _buttons(payload)]
    assert names == list(sourcecfg.TOGGLES), "one button per declared switch, in the declared order"
    assert "active" not in " ".join(names) and "priority" not in " ".join(names), (
        "a button for a column nothing reads is the thing this module refuses to build"
    )


def test_no_button_is_hardcoded_in_the_module_source() -> None:
    """The names come from `sourcecfg`, so the words cannot drift from the columns.

    Read as a text rather than a call because the bug being prevented is a literal in this file: someone
    adding a fourth `button("mode → full", …)` beside the derived ones.
    """
    assert "TOGGLES.values()" in SOURCE, "the rows are built by walking the declared switches"
    assert SOURCE.count("f\"/source {handle}") == 1, "one place builds a source command, for all switches"
    assert SOURCE.count("f\"/joinmsg use {number}") == 1, "and one place builds the option buttons"


@pytest.mark.parametrize("column,on_label", [("require_hindi_audio", "off"), ("include_subbed", "on")])
def test_a_label_promises_the_state_the_press_writes(column: str, on_label: str) -> None:
    """`gate → off` means "after this, off" — never "this is off".

    A button named for the value it holds would change its own label under the operator's thumb the
    moment it is pressed, and the second press would then do the opposite of what the first one appeared
    to promise.
    """
    toggle = sourcecfg.BY_COLUMN[column]
    payload = keyboards.source_switches("@c", ROW)
    mine = next(one for one in _buttons(payload) if f"{toggle.name} " in one["text"])
    assert mine["text"].endswith(on_label)
    assert mine["callback_data"].endswith(on_label)


def test_an_unread_switch_offers_on_and_says_nothing_about_the_rest() -> None:
    """`mode = monitor_only` is a value no code reads, and the button cannot pretend it is "off"."""
    payload = keyboards.source_switches("@c", {"mode": "monitor_only", "require_hindi_audio": None})
    by_name = {one["text"].split(" ")[0]: one for one in _buttons(payload)}
    assert by_name["watch"]["callback_data"].endswith("on")
    assert by_name["gate"]["callback_data"].endswith("on")
    assert all(one["callback_data"].startswith("/source @c ") for one in by_name.values())


# --------------------------------------------------------------------- the 64-byte wall
def test_a_command_too_long_for_a_button_drops_the_button_not_the_message() -> None:
    """Never shorten what a press will send: a truncated command does something else."""
    long_handle = "@" + "u" * 60
    assert keyboards.encode(f"/source {long_handle} gate off") is None
    assert keyboards.source_switches(long_handle, ROW) is None, "no keyboard, and the reply still sends"

    payload = keyboards.source_switches("@ok_handle", ROW)
    for one in _buttons(payload):
        assert len(one["callback_data"].encode("utf-8")) <= keyboards.MAX_CALLBACK_BYTES


def test_encode_refuses_a_command_and_a_label_for_what_they_are() -> None:
    """No silent slash, no half a label.

    `button()` returning None is the only failure mode, and every caller drops the button rather than
    substituting something — a fallback that "fixed" a long handle by pointing at a different channel
    would be the worst possible recovery.
    """
    assert keyboards.encode("source x gate off") is None, "a command without its slash is a string"
    assert keyboards.encode("/joinmsg use 1") == "/joinmsg use 1"
    assert keyboards.button("", "/x") is None, "an invisible button is no button"
    assert keyboards.button("z" * (keyboards.MAX_LABEL_CHARS + 1), "/x") is None
    assert keyboards.markup([[], [None]]) is None


# --------------------------------------------------------------------- what a button may be
def test_every_button_says_a_command_this_bot_routes() -> None:
    """The whole safety argument in one check.

    Each payload in this module is built from a command string, so `callback_data` has to start with a
    slash whose first token is in the router. If a button ever carried a bare id like `sw:3:gate`, the
    tap would reach code the text path never sees — and that is exactly how a friendly interface ends up
    with a wider door than the careful one.
    """
    payloads = [
        keyboards.source_switches("@anime_uploads4u", ROW),
        keyboards.joinmsg_choices(),
    ]
    for payload in payloads:
        assert payload, "the short-handle case must build something to check"
        for one in _buttons(payload):
            data = one["callback_data"]
            assert data.startswith("/"), data
            command = data.lstrip("/").split()[0].split("@", 1)[0]
            assert command in controlbot._ROUTES, f"a button routes to nothing: {data}"


def test_no_button_carries_a_url() -> None:
    """A control-bot button that opens a link is how a private admin chat starts sending people places."""
    for one in _buttons(keyboards.source_switches("@c", ROW)) + _buttons(keyboards.joinmsg_choices()):
        assert set(one) == {"text", "callback_data"}, one
        assert "url" not in one


# --------------------------------------------------------------------- the option list
def test_the_option_buttons_number_the_drafts_the_text_numbers() -> None:
    """Same order, same numbering, same names — so the text and the buttons cannot disagree.

    The presets are drafts and choosing one is the operator's act; the `use <n>` handler is what writes
    it. A button that picked the wrong draft would be a wrong message to strangers, which is why this
    checks the number against the list rather than trusting either.
    """
    payload = keyboards.joinmsg_choices()
    buttons = _buttons(payload)

    assert [one["text"] for one in buttons] == [
        f"{number} · {preset.name}" for number, preset in enumerate(joinmsg.PRESETS, start=1)
    ]
    assert [one["callback_data"] for one in buttons] == [
        f"/joinmsg use {number}" for number in range(1, len(joinmsg.PRESETS) + 1)
    ]


@pytest.mark.parametrize("payload", [keyboards.source_switches("@c", ROW), keyboards.joinmsg_choices()])
def test_the_payload_is_what_telegrams_api_wants(payload) -> None:
    """`inline_keyboard` of rows of buttons, and nothing else in it."""
    assert list(payload) == ["inline_keyboard"]
    for row in payload["inline_keyboard"]:
        assert isinstance(row, list) and row
        for one in row:
            assert list(one) == ["text", "callback_data"]
