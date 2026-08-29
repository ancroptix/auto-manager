"""Buttons for the control bot — each one a command the operator could have typed.

This module exists because the operator asked for exactly this, on 2026-08-29: *"bot me hi toggle on/off
jaise options jod do"*, right after being told to fill a form in a database dashboard. What the bot could
already do by words, it can now do by a tap.

One rule holds every button here, and it is the reason the module is a builder rather than a feature:

**``callback_data`` is a command string, never a key into a private table of actions.**

* A button and its words cannot drift apart, because there is only one path: `ControlBot.handle` reads
  the text and routes it, whether it arrived as a message or as a press.
* The security gate is the same gate. The owner-only check, the private-chat check and the "unknown
  commands are ignored" rule all run on `update.text`, and a press carries its text through them — so a
  button cannot be a wider door than the keyboard.
* The audit trail reads as what it did. A logged callback is a command, not a number somebody has to
  look up in this file to interpret.

And one refusal, at the top: Telegram caps ``callback_data`` at 64 bytes, so a button whose command would
not fit is **not built**. The words still work; a tap that Telegram rejects with a 400 is a broken button,
and a button that silently sends a *shortened* command is a lie about what the operator pressed.

Nothing here edits a message in place. A press sends a fresh reply, because the previous line is the
record of what that flag said before the press, and this program does not overwrite its own history to
save a message.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from . import joinmsg, sourcecfg

__all__ = [
    "MAX_CALLBACK_BYTES",
    "MAX_LABEL_CHARS",
    "button",
    "joinmsg_choices",
    "markup",
    "encode",
    "source_switches",
]

#: Telegram's own limit on `callback_data`, in bytes of UTF-8 — not characters.
MAX_CALLBACK_BYTES = 64
#: Telegram's limit on a button's visible text, in characters.
MAX_LABEL_CHARS = 64


def encode(command: str) -> str | None:
    """The command as it will travel inside the button, or None when it does not fit.

    The leading slash is what makes a press indistinguishable from a typed line, so a command that would
    arrive without one is refused here rather than repaired: silently prefixing it would hide a caller
    that had built the wrong string.
    """
    text = str(command or "").strip()
    if not text.startswith("/"):
        return None
    if len(text.encode("utf-8")) > MAX_CALLBACK_BYTES:
        return None
    return text


def button(label: str, command: str) -> dict[str, str] | None:
    """One button, or None when either half of it is too long for Telegram."""
    data = encode(command)
    text = str(label or "").strip()
    if data is None or not text or len(text) > MAX_LABEL_CHARS:
        return None
    return {"text": text, "callback_data": data}


def markup(rows: Sequence[Sequence[dict[str, str] | None]]) -> dict[str, Any] | None:
    """The `reply_markup` payload, with empty rows dropped.

    Returns None — not `{"inline_keyboard": []}` — when no button survived. Telegram rejects an empty
    keyboard, and every caller of this is a reply that is perfectly readable without one: the loss of a
    button on a long channel handle has to cost a tap, never the message.
    """
    built = [[one for one in row if one is not None] for row in rows]
    built = [row for row in built if row]
    if not built:
        return None
    return {"inline_keyboard": built}


def source_switches(handle: str, row: Mapping[str, Any] | None = None) -> dict[str, Any] | None:
    """One button per switch `app/sourcecfg.py` knows, labelled with where it is going.

    The rows are derived from `sourcecfg.TOGGLES`, so a switch added next week appears here without a
    second edit — and a test fails if this file ever hard-codes a fourth button nobody declared. Each
    label states the *target*, not the current state: `gate → off` means "press me and it is off", which
    stays true whatever the row held before, and reads better on a phone than a button whose name changes
    under the thumb.
    """
    rows: list[list[dict[str, str] | None]] = []
    for toggle in sourcecfg.TOGGLES.values():
        state = sourcecfg.toggle_state(row or {}, toggle)
        # An unread switch flips *on*, because "off" would be a claim about a value nobody saw.
        target = False if state else True
        label = f"{toggle.name} → {'on' if target else 'off'}"
        rows.append([button(label, f"/source {handle} {toggle.name} {'on' if target else 'off'}")])
    return markup(rows)


def joinmsg_choices() -> dict[str, Any] | None:
    """The preset list as buttons, numbered the way `/joinmsg options` numbers them.

    Choosing one writes it into `app.config`; it sends nothing, and the reply that follows says whose
    wording it is. The numbering comes from `joinmsg.PRESETS` itself so a fourth draft cannot appear in
    the text and be missing from the buttons.
    """
    rows = [
        [
            button(
                f"{number} · {preset.name}",
                f"/joinmsg use {number}",
            )
            for number, preset in enumerate(joinmsg.PRESETS, start=1)
        ]
    ]
    return markup(rows)
