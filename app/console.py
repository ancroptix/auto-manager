"""The button console: every screen this service has, reachable without typing a word.

Why this exists is one sentence from the operator, on 2026-08-29, quoted as it was written: *"ye sab itna
userfriendly aur easy banaado ke bot me kuch bhi code likhkar ya command dekar na karna pade, sabke liye
buttons ho aur unki madad se hi sab ho … interface kisi million dollar project ki tarah dikhna chahiye."* A person on a phone should not have to remember that the
Hindi-audio gate is driven by a word called `gate`, or that a channel can be named with `title`.

Four things hold it together, and each is a rule rather than a style preference:

* **Every screen ends in buttons, and every button is one of four verbs**: open a screen (`n:`), run a
  command (`x:`, or the command text itself, which is what `app/keyboards.py` sends), run a command against
  one configured channel (`r:`), or ask for the one kind of text a button cannot carry (`p:`). There is no
  fifth, because a fifth would be an action with no words.
* **`x:` and `r:` payloads are commands, not keys into a private table of actions.** `x:` holds the command
  text itself; `r:` holds a row id plus a verb, and the bot resolves that id into the same `/source …` line
  and prints that line in the reply. A tap therefore goes through the router the keyboard goes through —
  the owner check, the private-chat check, the refusals — so a bug is a bug in both and not a surprise in
  one.
* **Nothing is truncated to fit.** Telegram gives ``callback_data`` 64 bytes; a payload that does not fit
  is *not built*, and the screen keeps its text. A button that silently ran a shortened command would be
  a different command wearing someone else's label.
* **Resolution failures are loud.** A tap on a row that no longer exists answers "that channel is not
  configured any more" and writes nothing. It does not guess the nearest row, and it does not answer with
  silence.

The console is built here — plain data in, text and buttons out — because the alternative is a screen
defined by whichever SQL queries happened to be in scope where it was rendered. `app/controlbot.py` owns
what the rows mean and which commands exist; this file owns what a person sees and taps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from . import keyboards, normalize, sourcecfg

__all__ = [
    "NAV",
    "NAV_PREFIX",
    "PROMPTS",
    "PROMPT_PREFIX",
    "ROW_PREFIX",
    "RULE",
    "RUN_PREFIX",
    "Button",
    "Screen",
    "bots_screen",
    "button",
    "channel_name",
    "screen_note",
    "encode",
    "help_screen",
    "joinmsg_screen",
    "main_screen",
    "nav",
    "parse_prompt",
    "ROW_SLOTS",
    "parse_row",
    "prompt_line",
    "prompt_payload",
    "queue_screen",
    "render",
    "row_payload",
    "source_screen",
    "sources_screen",
    "waiting_line",
    "waiting_screen",
]

#: The screens a button may open. `app/controlbot.py` answers each one; a test in tests/test_console.py
#: fails if the two lists ever disagree in either direction, because a screen nobody can reach is dead code
#: and a button that leads nowhere is a 404 in a chat window.
NAV: frozenset[str] = frozenset({"main", "sources", "queue", "bots", "joinmsg", "help"})

#: The thin line between a title and its facts. Box-drawing rather than markdown on purpose: the bot sends
#: no `parse_mode`, and half-rendered formatting in a chat that also carries a probe report is worse than
#: plain text that always looks the same on every client.
RULE = "─" * 22

NAV_PREFIX = "n:"
RUN_PREFIX = "x:"
ROW_PREFIX = "r:"
PROMPT_PREFIX = "p:"

#: The text a button cannot carry, per slot: what to ask. The reply it produces is built by
#: `app/controlbot.py`, which is also the only thing that knows the channel a row id points at.
#: The slots that change a configured row, and so have to say which one. A prompt without a row id would
#: have to guess, and the answer would land on whichever row the bot last looked at — a silent wrong turn
#: in the one place a screen is allowed to take input.
ROW_SLOTS = frozenset({"series", "title", "season", "audio"})

PROMPTS: dict[str, str] = {
    "add": (
        "Now send me the channel: its @handle, or its number like -1002575861262.\n"
        "For a private channel the number is in any link to a post inside it — "
        "`t.me/c/2575861262/5` is channel -1002575861262."
    ),
    "series": "Send the series name exactly as it should be written (for example: Bleach).",
    "title": "Send the words this channel should be called by, here and in every reply.",
    "season": (
        "Send the season number this channel's files continue from.\n"
        "It is a numbering default, never a claim that a season has started."
    ),
    "joinmsg": (
        "Write the message in your own words. {name} and {series} are filled in when it is sent.\n"
        "It cannot carry an invite link, and it cannot say yes or no to anybody."
    ),
}


@dataclass(frozen=True)
class Button:
    """One tap. ``label`` is what is shown, ``payload`` is what runs."""

    label: str
    payload: str


def encode(payload: str) -> str | None:
    """The payload, or None when Telegram would reject it.

    Bytes, not characters: an emoji in a channel title is one character and four of them, and the limit is
    on what goes on the wire.
    """
    text = str(payload or "")
    if not text or len(text.encode("utf-8")) > keyboards.MAX_CALLBACK_BYTES:
        return None
    return text


def button(label: str, payload: str | None) -> dict[str, str] | None:
    """A Telegram button dict, or None when either half is too long. Never shortened, never guessed."""
    data = encode(payload or "")
    text = str(label or "").strip()
    if data is None or not text or len(text) > keyboards.MAX_LABEL_CHARS:
        return None
    return {"text": text, "callback_data": data}


def payload_for(command: str) -> str | None:
    """`x:` plus a command the operator could have typed, ready to ride on a button."""
    text = str(command or "").strip()
    if not text.startswith("/"):
        return None
    return encode(f"{RUN_PREFIX}{text}")


def row_payload(row_id: Any, verb: str, arg: str | None = None) -> str | None:
    """`r:` plus the row id, the verb and any argument — short by design.

    The row id and not the channel handle, because a private channel with a long title would spend its
    whole byte budget on a name. `app/controlbot.py` turns the id back into a handle at tap time.
    """
    try:
        ident = int(row_id)
    except (TypeError, ValueError):
        return None
    text = f"{ROW_PREFIX}{ident}:{verb}"
    if arg is not None and str(arg) != "":
        text = f"{text}:{arg}"
    return encode(text)


def parse_row(payload: str) -> tuple[int, str, str | None] | None:
    """`r:<id>:<verb>[:<arg>]` → ``(id, verb, arg)``. None for anything else.

    The verb is returned un-checked on purpose: whether `gate` is a switch and whether `open` means
    anything are questions about the row and the modules that own it, and a screen that silently dropped an
    unknown verb would be a button that does nothing.
    """
    text = str(payload or "").strip()
    if not text.startswith(ROW_PREFIX):
        return None
    parts = text[len(ROW_PREFIX) :].split(":")
    if len(parts) < 2:
        return None
    try:
        ident = int(parts[0])
    except ValueError:
        return None
    verb = parts[1].strip().casefold()
    if not verb:
        return None
    arg = ":".join(parts[2:]).strip() or None
    return ident, verb, arg


def prompt_payload(slot: str) -> str | None:
    return encode(f"{PROMPT_PREFIX}{slot}")


def parse_prompt(payload: str) -> tuple[str, int | None] | None:
    """`p:<slot>` or `p:<slot>:<row id>` → ``(slot, id)``. None when it is neither.

    The id rides along because a prompt has to become a command about one channel, and a question asked on
    a screen may be answered minutes later — after the operator went back, opened another channel, and came
    back. The id is what keeps the answer on the row the question was about.
    """
    text = str(payload or "").strip()
    if not text.startswith(PROMPT_PREFIX):
        return None
    parts = text[len(PROMPT_PREFIX) :].split(":")
    slot = parts[0].strip().casefold()
    if slot not in PROMPTS:
        return None
    row_id: int | None = None
    if len(parts) > 1:
        if not parts[1].strip().lstrip("-").isdigit():
            return None
        row_id = int(parts[1])
    if (slot in ROW_SLOTS) != (row_id is not None):
        # The four row slots must name a row; `add` and `joinmsg` must not, because they have none to name.
        return None
    return slot, row_id


def prompt_line(slot: str) -> str:
    return PROMPTS.get(str(slot or "").split(":")[0].casefold(), PROMPTS["add"])


def waiting_screen(slot: str, row: Mapping[str, Any] | None) -> tuple[str, dict[str, Any] | None]:
    """The prompt, with the two ways out of it drawn next to it.

    A screen that asks for text has to say how to *stop* answering it, or the operator is standing in a
    doorway: a later bare message would be read as the answer they never meant to give. So the question
    comes with ✖ Stop here, and that tap is what clears the pending slot.
    """
    row_id = (row or {}).get("id")
    buttons = [
        [
            button("↩ That channel", row_payload(row_id, "open")),
            button("✖ Stop here", f"{NAV_PREFIX}main"),
        ]
    ]
    if row_id is None:
        buttons = [[button("✖ Stop here", f"{NAV_PREFIX}main")]]
    return render("waiting", "one more thing", [waiting_line(slot, row)], buttons)


def waiting_line(slot: str, row: Mapping[str, Any] | None) -> str:
    """What the bot says when a tap has asked for text — including how to get out of it."""
    named = channel_name(row) if row else None
    where = f" for {named}" if named else ""
    return (
        f"waiting for your text{where}:\n\n{prompt_line(slot)}\n\n"
        "reply here with it, or tap ✖ Stop here to drop this without changing anything. "
        "Nothing is written until the text arrives."
    )


def screen_note(command: str, detail: str | None = None) -> str:
    """The line that tells an operator which command their tap actually ran.

    One function because the wording is the audit trail: the console writes through `_console_run`, so the
    only place a button's effect is recorded as words is this line, and an operator filing a bug report
    copies it. `detail` is the running command's own first line, when there is one to show.
    """
    text = f"ran: `{str(command or '').strip()}`"
    lines = [one.strip() for one in str(detail or "").splitlines() if one.strip()]
    return f"{text} — {lines[0]}" if lines else text


def channel_name(row: Mapping[str, Any] | None) -> str:
    """How a screen refers to one channel: the name it has, then its handle, then its number."""
    if not row:
        return "a channel"
    for key in ("title", "username"):
        value = str(row.get(key) or "").strip().lstrip("@")
        if value:
            return value
    return str(row.get("telegram_channel_id") or row.get("id") or "a channel")


@dataclass(frozen=True)
class Screen:
    """A title, the facts under it, and the taps that follow.

    ``note`` is the freshest thing on the screen and sits above the facts: it is where the result of the
    tap that got the operator here lands ("watching: off", "saved"), because a screen that re-renders
    without saying what changed reads like the tap did nothing at all.
    """

    key: str
    title: str
    lines: tuple[str, ...] = ()
    rows: tuple[tuple[dict[str, str], ...], ...] = ()
    note: str | None = None

    @property
    def text(self) -> str:
        parts = [self.title, RULE]
        if self.note:
            parts.extend([self.note.strip(), ""])
        parts.extend(line for line in self.lines)
        return "\n".join(parts).strip()

    @property
    def markup(self) -> dict[str, Any] | None:
        if not self.rows:
            return None
        return {"inline_keyboard": [list(row) for row in self.rows]}


def _rows(
    rows: Sequence[Sequence[Button | dict[str, str] | None | Any]],
) -> tuple[tuple[dict[str, str], ...], ...]:
    built: list[tuple[dict[str, str], ...]] = []
    for row in rows:
        cells: list[dict[str, str]] = []
        for one in row:
            if isinstance(one, Button):
                made = button(one.label, one.payload)
            elif one is None:
                made = None
            else:
                made = one
            if made is not None:
                cells.append(made)
        if cells:
            built.append(tuple(cells))
    return tuple(built)


def render(
    key: str,
    title: str,
    lines: Iterable[str] = (),
    rows: Sequence[Sequence[Any]] = (),
    note: str | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """The ``(text, reply_markup)`` pair a control-bot reply should carry.

    Returning the pair rather than a Screen keeps `app/controlbot.py` free of a type it only forwards, and
    keeps the layout decisions — where the note goes, what a rule looks like — in one file.
    """
    built = Screen(key=key, title=title, lines=tuple(lines), rows=_rows(rows), note=note)
    return built.text, built.markup


def nav(label: str, key: str) -> Button | None:
    """A jump to another screen. An unknown key is refused here, not at tap time."""
    if key not in NAV:
        return None
    return Button(label, f"{NAV_PREFIX}{key}")


def _nav(label: str, key: str) -> dict[str, str] | None:
    made = nav(label, key)
    return None if made is None else button(made.label, made.payload)


def _tail(key: str, back: str | None = "main") -> list[list[dict[str, str] | None]]:
    """`↻ Refresh`, and the way back — the same last row on every screen.

    The refresh is not a courtesy. A screen is a snapshot, and its counts were true when it was drawn, so a
    menu left open in a tab is a menu that has to be re-read before anything is believed. `main` gets the
    refresh alone: it is where every other screen's `◀` leads, and a back button on the root would be a
    button that goes nowhere.
    """
    if back is None:
        return [[button("↻ Refresh", f"{NAV_PREFIX}{key}")]]
    label = "◀ Menu" if back == "main" else "◀ Back"
    return [[button("↻ Refresh", f"{NAV_PREFIX}{key}"), _nav(label, back)]]


# --------------------------------------------------------------------------- the screens
def main_screen(facts: Mapping[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """The first screen, and the one every other screen can get back to.

    ``facts`` is what the bot has already read. Nothing here invents a headline: a count that could not be
    read arrives as ``?``, which is a different sentence from ``0`` and is printed as itself.
    """
    lines = [
        f"mode: {facts.get('mode', '?')} · sending: {facts.get('outbound', '?')}",
        f"queue: ready {facts.get('ready', '?')} · blocked {facts.get('blocked', '?')}",
        f"source channels: {facts.get('sources', '?')} · sessions: {facts.get('sessions', '?')}",
    ]
    if facts.get("paused"):
        lines.append(f"paused: {facts.get('pause_reason') or 'yes'}")
    if facts.get("blocked") not in (None, 0, "0"):
        lines.append(f"{facts['blocked']} job kind(s) are waiting on you — Status says which.")
    rows = [
        [button("📊 Status", payload_for("/status")), button("🎙 Sources", f"{NAV_PREFIX}sources")],
        [button("🔌 Queue", f"{NAV_PREFIX}queue"), button("🤖 Bots", f"{NAV_PREFIX}bots")],
        [button("📩 Join message", f"{NAV_PREFIX}joinmsg"), button("❓ Help", f"{NAV_PREFIX}help")],
        *_tail("main", None),
    ]
    return render("main", "auto-manager · control", lines, rows)


def sources_screen(rows: Sequence[Mapping[str, Any]], *, note: str | None = None) -> tuple[str, dict | None]:
    """The list, one button per channel, and the one button that adds a row.

    State is printed on the label, because a list of names is not a list of *states*: the question is
    always "which of these am I actually reading right now", and making somebody tap each row to find out
    is the complication this file exists to remove.
    """
    lines: list[str] = []
    buttons: list[list[Any]] = []
    if not rows:
        lines.append("nothing yet — no source channel is configured, so no files are being read.")
    for row in rows:
        watching = str(row.get("mode") or "").casefold() != sourcecfg.MODE_IGNORED
        declared = str(row.get("declared_series") or "").strip()
        flag = "👁" if watching else "💤"
        what = f" · {declared}" if declared else " · no series yet"
        name = channel_name(row)
        lines.append(f"{flag} {name}{what}")
        buttons.append([button(f"{flag} {name}{what}", row_payload(row.get("id"), "open"))])
    buttons.append([button("➕ Add a channel", prompt_payload("add"))])
    buttons.extend(_tail("sources"))
    return render("sources", "source channels", lines or ["—"], buttons, note=note)


def source_screen(
    row: Mapping[str, Any],
    *,
    note: str | None = None,
    back: str = "sources",
) -> tuple[str, dict | None]:
    """One channel: its facts on top, its switches under them, and a ◀ Back nobody has to guess at.

    Every button here writes something the pipeline reads. That is why there is no delete and no knob for
    ``priority``, ``active`` or ``is_joined``: `app/sourcecfg.py` keeps the same list, and a screen with a
    switch for an unread column is a screen lying about what it controls.
    """
    row_id = row.get("id")
    lines = [f"channel: {channel_name(row)} · id {row.get('telegram_channel_id', '?')}"]
    for toggle in sourcecfg.TOGGLES.values():
        state = sourcecfg.toggle_state(row, toggle)
        word = "?" if state is None else ("on" if state else "off")
        dot = {"on": "●", "off": "○", "?": "?"}[word]
        lines.append(f"{dot} {toggle.label}: {word}")
    series = str(row.get("declared_series") or "").strip() or "not declared"
    audio = str(row.get("declared_audio") or "").strip() or "not declared"
    lines.append(f"series: {series} · audio: {audio}")
    season = row.get("declared_season")
    if season is not None and int(season) >= 0:
        lines.append(f"season default: {int(season)}")

    buttons: list[list[Any]] = []
    for toggle in sourcecfg.TOGGLES.values():
        target = "off" if sourcecfg.toggle_state(row, toggle) else "on"
        buttons.append([button(f"{toggle.name} → {target}", row_payload(row_id, toggle.name, target))])
    audio_cells = [button(kind, row_payload(row_id, "audio", kind)) for kind in sorted(normalize.DECLARED_AUDIO)]
    buttons.extend([audio_cells[:3], audio_cells[3:]])
    for slot, label in (("series", "🎬 Series name"), ("title", "🏷 Rename"), ("season", "📅 Season")):
        buttons.append([button(label, prompt_payload(f"{slot}:{row_id}"))])
    buttons.append(
        [
            button("↻ Refresh", row_payload(row_id, "open")),
            _nav("◀ Back", back),
        ]
    )
    return render("source", f"channel · {channel_name(row)}", lines, buttons, note=note)


def queue_screen(
    *,
    paused: bool | None = None,
    reason: str | None = None,
    ready: Any = "?",
    note: str | None = None,
) -> tuple[str, dict | None]:
    """The queue, with the two switches that decide whether it moves.

    ``paused`` is ``None`` when the state could not be read, and the screen then says ``?`` rather than
    ``no`` — an operator who believes the queue is running when its state is unknown is worse off than one
    who sees a question mark.
    """
    mark = "?" if paused is None else ("yes" if paused else "no")
    lines = [f"ready jobs: {ready}", f"paused: {mark}"]
    if paused and reason:
        lines.append(f"reason: {reason}")
    lines.append("pausing stops claiming work. Nothing already running is stopped, and nothing is undone.")
    # The state decides which of the pair is offered, because a button that cannot change anything is a
    # button that teaches the operator to tap without reading. Both appear only when the state is unknown —
    # and then the screen has already said `paused: ?`, so neither tap is a guess about what it will do.
    controls: list[dict | None]
    if paused is None:
        controls = [button("⏸ Pause", payload_for("/pause")), button("▶ Resume", payload_for("/resume"))]
    elif paused:
        controls = [button("▶ Resume", payload_for("/resume"))]
    else:
        controls = [button("⏸ Pause", payload_for("/pause"))]
    buttons = [
        controls,
        [button("♻ Reconcile now", payload_for("/reconcile"))],
        *_tail("queue"),
    ]
    return render("queue", "the queue", lines, buttons, note=note)


def bots_screen(
    *,
    storage: str | None = None,
    help_bot: str | None = None,
    link: str | None = None,
    note: str | None = None,
) -> tuple[str, dict | None]:
    lines = [
        f"storage bot: @{storage or '?'}",
        f"channel help: @{help_bot or '?'}",
        f"link provider: @{link or '?'}",
        "/probe opens all three from the logged-in account and reads their menus back. It does send —",
        "so it refuses to run while this deployment is in shadow mode, and it never posts in your channels.",
    ]
    buttons = [
        [button("🔍 Run /probe", payload_for("/probe")), button("👤 Sessions", payload_for("/sessions"))],
        *_tail("bots"),
    ]
    return render("bots", "the three bots", lines, buttons, note=note)


def joinmsg_screen(
    *,
    current: str | None = None,
    presets: Sequence[Mapping[str, Any]] = (),
    note: str | None = None,
) -> tuple[str, dict | None]:
    """The join-request wording: pick a draft, write your own, or say nothing.

    The wording stays the operator's and only theirs — this screen can save it and cannot send it, and it
    says so in the same breath, because the sender is still a job kind waiting to be wired.
    """
    body = " ".join(str(current or "").split())
    lines = [f"saved now: {body[:220]}" if body else "saved now: nothing — the app may contact nobody."]
    buttons: list[list[Any]] = []
    for number, preset in enumerate(presets, start=1):
        name = str(preset.get("name") or f"draft {number}") if isinstance(preset, Mapping) else str(preset)
        buttons.append([button(f"{number} · {name}", payload_for(f"/joinmsg use {number}"))])
    buttons.append(
        [
            button("✍️ Write your own", prompt_payload("joinmsg")),
            button("🚫 Say nothing", payload_for("/joinmsg clear")),
        ]
    )
    buttons.append([button("👁 Show the rules", payload_for("/joinmsg"))])
    buttons.extend(_tail("joinmsg"))
    return render("joinmsg", "what a join requester is told", lines, buttons, note=note)


def help_screen(text: str, *, note: str | None = None) -> tuple[str, dict | None]:
    """The command list, because buttons are not a wall built in front of it.

    Everything on every screen here is also a command in this text, and the reply to a tap names the
    command it ran — so an operator who would rather type never has to guess what a button meant, and a
    screenshot of this one screen still describes everything the bot can do.
    """
    buttons = [[button("📊 Status", payload_for("/status"))], *_tail("help")]
    return render("help", "commands", text.split("\n"), buttons, note=note)
