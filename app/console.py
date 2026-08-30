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
    "destination_screen",
    "destinations_screen",
    "button",
    "channel_name",
    "LIST_LIMIT",
    "screen_note",
    "encode",
    "parse_ref",
    "destination_screen",
    "help_screen",
    "joinmsg_screen",
    "main_screen",
    "nav",
    "parse_prompt",
    "ROW_SLOTS",
    "ROW_SLOT_TABLES",
    "parse_row",
    "prompt_line",
    "sessions_screen",
    "prompt_payload",
    "queue_screen",
    "sessions_screen",
    "render",
    "row_payload",
    "source_screen",
    "discover_screen",
    "joinreq_screen",
    "sources_screen",
    "TABLES",
    "row_ref",
    "waiting_line",
    "waiting_screen",
]

#: The screens a button may open. `app/controlbot.py` answers each one; a test in tests/test_console.py
#: fails if the two lists ever disagree in either direction, because a screen nobody can reach is dead code
#: and a button that leads nowhere is a 404 in a chat window.
NAV: frozenset[str] = frozenset(
    {"main", "sources", "destinations", "discover", "joinreq", "queue", "bots", "sessions", "joinmsg", "help"}
)

#: Which table a row of the console lives in, as the one letter that rides in a payload. `app.destination`
#: has its own row 3 as surely as `app.source_channel` does, and a button that showed them as the same
#: bare number would either be ambiguous or be a bug waiting for the second table to arrive.
TABLES = {"s": "source", "d": "destination"}

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
ROW_SLOTS = frozenset({"series", "title", "season", "audio", "card", "episodes", "campaign"})

#: Which table each question can be asked about. `card` belongs to a destination and `series` to a source,
#: and a payload that asked for one on the other would have to be refused by the translator two calls later
#: — with the operator already holding the keyboard, having typed the answer for nothing.
ROW_SLOT_TABLES: dict[str, str] = {
    "series": "s",
    "title": "s",
    "season": "s",
    "audio": "s",
    "card": "d",
    "campaign": "d",
    "episodes": "sd",
}

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
    "card": (
        "Send the message number of the post in that channel that a shareable link should be made from.\n"
        "It is the number at the end of a link to that post — `t.me/c/2575861262/512` is 512. Tap 🧹 "
        "instead if you meant to stop using a card."
    ),
    "episodes": (
        "Send the season and how many episodes it has: `2 12`.\n"
        "Send `tba` as the number to go back to not claiming a length — while a season is undeclared the "
        "caption says TBA and the complete-season post is held."
    ),
    "campaign": (
        "Send one word to call this campaign, like `wave1`.\n"
        "Drafting changes nothing on its own: nothing is sent to anyone until you plan it and confirm it "
        "with the code the plan prints."
    ),
    "archive": (
        "Send the channel and what to call it, on one line — `-1002575861262 Master copies` or "
        "`@my_archive Master copies`.\nThe name is not decoration: the archive is the one channel nobody "
        "reads messages from, so without it there is nothing to call it by in a reply or a report."
    ),
    "archive_title": (
        "Send the words to call that archive channel in every reply. Nothing else about it changes."
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


def row_ref(kind: str, row_id: Any) -> str:
    """`s3` or `d7`: the table and the row, in the three characters a payload can spare."""
    return f"{str(kind or 's').strip().casefold()[:1]}{int(row_id)}"


def parse_ref(text: str) -> tuple[str, int] | None:
    """`s3` → `("s", 3)`, and a bare `3` → `("s", 3)`. None when it is neither.

    The bare number is the spelling every button built before the destination screen existed is carrying
    on the operator's phone right now. Screens are not re-drawn retroactively, so an update that started
    refusing that form would leave the buttons under yesterday's message dead — and "dead buttons" is
    exactly the complaint this round is here to end.
    """
    token = str(text or "").strip()
    if not token:
        return None
    if token[0] in TABLES:
        rest = token[1:]
        if not rest.isdigit():
            return None
        return token[0], int(rest)
    if not token.isdigit():
        return None
    return "s", int(token)


def row_payload(row_id: Any, verb: str, arg: str | None = None, *, kind: str = "s") -> str | None:
    """`r:` plus the row reference, the verb and any argument — short by design.

    A row reference and not the channel handle, because a private channel with a long title would spend its
    whole byte budget on a name. `app/controlbot.py` turns the reference back into a command at tap time.
    """
    try:
        ref = row_ref(kind, row_id)
    except (TypeError, ValueError):
        return None
    text = f"{ROW_PREFIX}{ref}:{verb}"
    if arg is not None and str(arg) != "":
        text = f"{text}:{arg}"
    return encode(text)


def parse_row(payload: str) -> tuple[str, int, str, str | None] | None:
    """`r:<ref>:<verb>[:<arg>]` → ``(kind, id, verb, arg)``. None for anything else.

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
    parsed = parse_ref(parts[0])
    if parsed is None:
        return None
    kind, ident = parsed
    verb = parts[1].strip().casefold()
    if not verb:
        return None
    arg = ":".join(parts[2:]).strip() or None
    return kind, ident, verb, arg


def prompt_payload(slot: str) -> str | None:
    return encode(f"{PROMPT_PREFIX}{slot}")


def parse_prompt(payload: str) -> tuple[str, str | None] | None:
    """`p:<slot>` or `p:<slot>:<row ref>` → ``(slot, ref)``. None when it is neither.

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
    ref: str | None = None
    if len(parts) > 1 and parts[1].strip():
        parsed = parse_ref(parts[1])
        if parsed is None:
            return None
        ref = row_ref(*parsed)
    if (slot in ROW_SLOTS) != (ref is not None):
        # A row slot must name a row; `add`, `joinmsg` and the two archive slots must not, because they have
        # none to name — the archive ones address the single recorded archive row, which the bot reads.
        return None
    if ref is not None and ref[0] not in ROW_SLOT_TABLES.get(slot, ref[0]):
        # A question can only be asked about a row that can answer it: `card` on a source row, or `series`
        # on a destination row, is a screen bug, and it is cheaper to refuse the button than to let the
        # operator type an answer that lands nowhere.
        return None
    return slot, ref


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
        (
            f"source channels: {facts.get('sources', '?')}"
            f" · destinations: {facts.get('destinations', '?')}"
            f" · sessions: {facts.get('sessions', '?')}"
        ),
    ]
    if facts.get("paused"):
        lines.append(f"paused: {facts.get('pause_reason') or 'yes'}")
    if facts.get("blocked") not in (None, 0, "0"):
        lines.append(f"{facts['blocked']} job kind(s) are waiting on you — Status says which.")
    rows = [
        [button("📊 Status", payload_for("/status")), button("❓ Help", f"{NAV_PREFIX}help")],
        [button("🎙 Sources", f"{NAV_PREFIX}sources"), button("📤 Destinations", f"{NAV_PREFIX}destinations")],
        [button("🔎 Find channels", f"{NAV_PREFIX}discover")],
        [button("🔌 Queue", f"{NAV_PREFIX}queue"), button("🤖 Bots", f"{NAV_PREFIX}bots")],
        [button("📨 Who is waiting", f"{NAV_PREFIX}joinreq"), button("📩 Join message", f"{NAV_PREFIX}joinmsg")],
        [button("👤 Sessions", f"{NAV_PREFIX}sessions")],
        *_tail("main", None),
    ]
    return render("main", "auto-manager · control", lines, rows)


#: How many rows a list screen prints, and so how many it says it printed. A screen is a message, and a
#: message that is split in two puts its buttons on the first half — so a list is capped, and the cap is
#: admitted in the same breath as the rows.
LIST_LIMIT = 50


def _cap_note(truncated: bool) -> str | None:
    """The line that says the list ends here, and what to do about it."""
    if not truncated:
        return None
    return (
        f"showing the first {LIST_LIMIT}, and the list is capped there on purpose: a screen split in two "
        "puts its buttons on the top half only. Name the one you want to change and it opens on its own."
    )


def discover_screen(
    lines: Sequence[str],
    findings: Sequence[Mapping[str, Any]],
    *,
    auto: bool,
    note: str | None = None,
    pairs: Sequence[Mapping[str, Any]] = (),
) -> tuple[str, dict | None]:
    """What the spare account can see, one button per series that can be wired in one tap.

    The findings are the proposals `app/discover.py` decided and ``lines`` is its own report of them: this
    screen adds no verdict of its own, only a tap for each channel that can be added and the two decisions
    that are not per-row — take the whole page, or let the bot keep watching for a role change. ``auto`` is
    printed as the report said it, never as this screen would like it to be.

    A row whose name is too long for a button keeps its line and gains the command underneath it. The
    button is dropped rather than the name sliced, because a label cut mid-word describes a different channel
    than the one it opens.
    """
    buttons: list[list[dict[str, str] | None]] = []
    body = list(lines)
    for pair in pairs or ():
        # The tap an operator actually wants: not "add this channel" for each of three channels, but "set
        # this show up", which writes the series, both rows and the link between them. The channel lines stay
        # on the screen as what it is about to do, and lose their own buttons while they are inside a pair —
        # two ways to start the same write is how a half-configured channel gets made by accident.
        made = button(
            f"✅ Set up {str(pair.get('series') or pair.get('key') or '?')[:32]}",
            payload_for(f"/discover pair {pair['index']}"),
        )
        if made is None:
            body.append(f"  too long for a button — type it: /discover pair {pair['index']}")
            continue
        buttons.append([made])
    if len(list(pairs or ())) > 1:
        # Only when there is more than one to do: with a single pair the bulk button would be the same tap
        # twice, and two buttons for one decision is the clutter this screen was rebuilt to remove.
        buttons.append([button("✅ Set up every pair on this page", payload_for("/discover pair all"))])
    for finding in findings:
        if finding.get("use") not in ("source", "destination") or finding.get("pair"):
            continue
        who = (
            str(finding.get("title") or "").strip()
            or (f"@{finding['username']}" if finding.get("username") else "")
            or str(finding.get("channel"))
        )
        verb = "📥" if finding["use"] == "source" else "📤"
        made = button(f"{verb} {who}", payload_for(f"/discover add {finding['index']}"))
        if made is None:
            body.append(f"  too long for a button — type it: /discover add {finding['index']}")
            continue
        buttons.append([made])
    unpaired = [one for one in findings if one.get("use") in ("source", "destination") and not one.get("pair")]
    if len(unpaired) > 1:
        # Only for what is left over: bulk-adding a page of channels a pair already covers would ask the same
        # question twice, and the pair tap is the one that also links them.
        buttons.append([button("✅ Add the rest on this page", payload_for("/discover add all"))])
    switch = (
        button("🧿 Stop switching on its own", payload_for("/discover auto off"))
        if auto
        else button("✨ Let it switch on its own", payload_for("/discover auto on"))
    )
    if switch is not None:
        buttons.append([switch])
    buttons.extend(_tail("discover"))
    return render("discover", "what this account can see", body, buttons, note=note)


def sources_screen(
    rows: Sequence[Mapping[str, Any]], *, note: str | None = None, truncated: bool = False
) -> tuple[str, dict | None]:
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
    cap = _cap_note(truncated)
    return render("sources", "source channels", (lines or ["—"]) + ([cap] if cap else []), buttons, note=note)


def destinations_screen(
    rows: Sequence[Mapping[str, Any]], *, note: str | None = None, truncated: bool = False
) -> tuple[str, dict | None]:
    """Every destination channel — the ones the audience actually reads — as one button each.

    They are listed by series rather than by id because that is how the operator thinks about them ("where
    did Bleach go"), and each line says whether the channel exists in Telegram yet: a series can have a row
    here before its channel is built, and a screen that showed both as the same thing would hide the one
    difference that matters.
    """
    lines: list[str] = []
    buttons: list[list[Any]] = []
    if not rows:
        lines.append(
            "no destination channel yet. One is made per series when the first episode is filed — /status "
            "shows what the queue is doing about that."
        )
    for row in rows:
        series = str(row.get("series") or row.get("title") or "").strip() or f"row {row.get('id')}"
        built = row.get("telegram_channel_id") not in (None, "", 0)
        flag = "📤" if built else "🏗"
        extra = "" if built else " · channel not built yet"
        lines.append(f"{flag} {series}{extra}")
        buttons.append([button(f"{flag} {series}{extra}", row_payload(row.get("id"), "open", kind="d"))])
    buttons.extend(_tail("destinations"))
    cap = _cap_note(truncated)
    return render(
        "destinations", "destination channels", (lines or ["—"]) + ([cap] if cap else []), buttons, note=note
    )


def destination_screen(row: Mapping[str, Any], *, note: str | None = None) -> tuple[str, dict | None]:
    """One destination: what it publishes, what a link would be made from, and the taps that change that.

    The card post and the campaign belong here and nowhere else, because both are addressed by destination —
    and both were, until this screen, a command line to remember. Nothing here offers a knob for a column no
    job reads: the bio and the picture are written while the channel is built and are not switches, so they
    are not buttons.
    """
    row_id = row.get("id")
    series = str(row.get("series") or "").strip()
    title = str(row.get("title") or "").strip() or series or f"row {row_id}"
    mode = str(row.get("publish_mode") or "link_post").casefold()
    card = row.get("card_message_id")
    link = str(row.get("announcement_link") or "").strip()
    lines = [
        f"channel: {title} · id {row.get('telegram_channel_id') if row.get('telegram_channel_id') is not None else '?'}",
        f"series: {series or 'not linked to one'}",
        "publishing: "
        + (
            "by captioning the channel's own file messages (nothing new is posted there)"
            if mode == "in_place_caption"
            else "as a link post for every file the pipeline stores"
        ),
        "card post: "
        + (f"message {card}" if card not in (None, "", 0) else "not named — an announcement has nothing to link"),
        "shareable link: " + (link if link else "none recorded yet"),
    ]
    # Only printed when the row carries the columns, because `_find_destination` reads a narrower row than
    # the screen's own query does, and a missing flag must not become "no".
    if "channel_help_added" in row:
        lines.append(
            "channel help bot: "
            + ("added" if row.get("channel_help_added") else "not added — the buttons on posts need it")
            + " · owner promoted: "
            + ("yes" if row.get("owner_promoted") else "no")
        )
    buttons = [
        [
            button("📌 Name the card post", prompt_payload(f"card:d{row_id}")),
            button("👁 Show it", row_payload(row_id, "card", "show", kind="d")),
        ],
        [button("🧹 Stop using a card", row_payload(row_id, "card", "clear", kind="d"))],
        [button("📅 Episodes in a season", prompt_payload(f"episodes:d{row_id}"))],
        [
            button("📣 Campaigns", row_payload(row_id, "campaigns", kind="d")),
            button("➕ Draft one", prompt_payload(f"campaign:d{row_id}")),
        ],
        [
            button("🖼 Show the plan", row_payload(row_id, "inplace", "plan", kind="d")),
            button("🖼 Caption in place", row_payload(row_id, "inplace", "on", kind="d")),
            button("🔗 Links only", row_payload(row_id, "inplace", "off", kind="d")),
        ],
        [
            button("↻ Refresh", row_payload(row_id, "open", kind="d")),
            _nav("◀ Back", "destinations"),
        ],
    ]
    return render("destination", f"destination · {title}", lines, buttons, note=note)


def source_screen(
    row: Mapping[str, Any],
    *,
    note: str | None = None,
    back: str = "sources",
    kind: str = "s",
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
        buttons.append([button(label, prompt_payload(f"{slot}:{kind}{row_id}"))])
    buttons.append([button("📅 Episodes in a season", prompt_payload(f"episodes:{kind}{row_id}"))])
    # The three in-place taps, on the row they apply to: a plan that changes nothing, the captioning itself,
    # and the way back to links. They sit here rather than on a screen of their own because `/inplace` is
    # addressed by a source channel, and this screen is the one that knows which channel that is.
    buttons.append(
        [
            button("🖼 Show the plan", row_payload(row_id, "inplace", "plan", kind=kind)),
            button("🖼 Caption in place", row_payload(row_id, "inplace", "on", kind=kind)),
            button("🔗 Links only", row_payload(row_id, "inplace", "off", kind=kind)),
        ]
    )
    buttons.append([button("📤 Where its files are published", row_payload(row_id, "dest", kind=kind))])
    buttons.append(
        [
            button("↻ Refresh", row_payload(row_id, "open", kind=kind)),
            _nav("◀ Back", back),
        ]
    )
    return render("source", f"channel · {channel_name(row)}", lines, buttons, note=note)


def sessions_screen(
    rows: Sequence[Mapping[str, Any]], *, note: str | None = None, truncated: bool = False
) -> tuple[str, dict | None]:
    """The stored logins, with the two things an operator does with one: make it current, or drop it.

    Only the metadata is on this screen — never a session string, and not even its length: the account's
    username and when it was last used is what decides which of two logins to pick, and everything else
    about a session is a secret that has no business being printed in a chat window.
    """
    lines: list[str] = []
    buttons: list[list[Any]] = []
    if not rows:
        lines.append("no stored session, so nothing can be sent to Telegram yet.")
        lines.append("/login <name> +<phone> is how one is added — it is the one flow that still needs text,")
        lines.append("because it is the phone number, and a button cannot know yours.")
    for row in rows:
        name = str(row.get("name") or "").strip()
        who = str(row.get("username") or "").strip() or "no username read"
        active = " · this one is in use" if row.get("active") else ""
        lines.append(f"👤 {name} · @{who}{active}")
        buttons.append(
            [
                button(f"▶ Use {name}", payload_for(f"/use {name}")),
                button(f"🧹 Forget {name}", payload_for(f"/forget {name}")),
            ]
        )
    buttons.extend(_tail("sessions"))
    cap = _cap_note(truncated)
    return render(
        "sessions", "stored sessions", (lines or ["—"]) + ([cap] if cap else []), buttons, note=note
    )


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
    archive: Mapping[str, Any] | None = None,
    note: str | None = None,
) -> tuple[str, dict | None]:
    """The three bots this program talks to, and the channel the master copies live in.

    The archive is on this screen because it is the same kind of fact — an outside place the pipeline needs
    named — and because without a row there the archive job blocks on a refusal. Both taps are here so that
    pointing at a channel and naming it never needs a remembered line.
    """
    lines = [
        f"storage bot: @{storage or '?'}",
        f"channel help: @{help_bot or '?'}",
        f"link provider: @{link or '?'}",
        "archive: "
        + (
            f"{archive.get('title') or archive.get('telegram_channel_id')} "
            f"({'primary' if archive.get('is_primary') else 'spare'})"
            if archive
            else "none recorded — the archive job refuses to pick one for you"
        ),
        "/probe opens all three from the logged-in account and reads their menus back. It does send —",
        "so it refuses to run while this deployment is in shadow mode, and it never posts in your channels.",
    ]
    buttons = [
        [button("🔍 Run /probe", payload_for("/probe")), button("👤 Sessions", f"{NAV_PREFIX}sessions")],
        [
            button("📦 Point at an archive", prompt_payload("archive")),
            button("🏷 Rename the archive", prompt_payload("archive_title")),
        ],
        [button("👁 What is recorded", payload_for("/archive"))],
        *_tail("bots"),
    ]
    return render("bots", "the bots and the archive", lines, buttons, note=note)


def joinreq_screen(
    lines: Sequence[str],
    choices: Sequence[Mapping[str, str]],
    *,
    note: str | None = None,
) -> tuple[str, dict | None]:
    """Join requests, one channel per line and one tap to start it.

    `choices` is the whole interface rule of this screen: the *command* decides which taps exist (it is the
    one that read the account's rights and the campaign rows), and this function only turns them into
    buttons. A label too long for a button is not sliced — the line stays and the command is written out
    under it, because a cut label describes a different channel than the one it would open.
    """
    body = list(lines)
    buttons: list[list[dict[str, str] | None]] = []
    for choice in choices:
        made = button(str(choice.get("label") or ""), payload_for(str(choice.get("command") or "")))
        if made is None:
            body.append(f"  too long for a button — type it: {choice.get('command')}")
            continue
        buttons.append([made])
    buttons.extend(_tail("joinreq"))
    return render("joinreq", "who is waiting to join", body, buttons, note=note)


def joinmsg_screen(
    *,
    current: str | None = None,
    presets: Sequence[Mapping[str, Any]] = (),
    note: str | None = None,
) -> tuple[str, dict | None]:
    """The join-request wording: pick a draft, write your own, or say nothing.

    The wording stays the operator's and only theirs — this screen saves it and sends nothing. Sending is a
    campaign per channel, started from `📨 Who is waiting to join`, which is the button below.
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
    buttons.append([button("📨 Who is waiting to join", f"{NAV_PREFIX}joinreq")])
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
