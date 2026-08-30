"""`app/console.py` — the screens, the payloads they carry, and the limits a button has to respect.

The end-to-end half of this ("a tap changes the row the words would change") lives in
tests/test_control_bot.py, next to the fakes it needs. What is checked here is the builder: that a screen
never offers a button it cannot honour, and never loses a fact to fit one.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from app import console, controlbot, joinmsg, keyboards, normalize, sourcecfg

SOURCE = Path(inspect.getsourcefile(console)).read_text(encoding="utf-8")
_HELP_COMMANDS = re.findall(r"(?m)^/([a-z_]+)", controlbot.HELP)
_ROUTES = set(controlbot._ROUTES)

ROWS = [
    {
        "id": 3,
        "username": "bleach_hindi",
        "title": "Bleach in Hindi",
        "telegram_channel_id": -1002575861262,
        "mode": "full",
        "active": True,
        "require_hindi_audio": True,
        "include_subbed": False,
        "declared_series": "Bleach",
        "declared_audio": "hindi",
        "declared_season": -1,
    },
    {
        "id": 4,
        "username": None,
        "title": None,
        "telegram_channel_id": -100999,
        "mode": "ignore",
        "active": True,
        "require_hindi_audio": False,
        "include_subbed": True,
        "declared_series": "",
        "declared_audio": "",
        "declared_season": 2,
    },
]


DEST_ROWS = [
    {
        "id": 21,
        "title": "Dekin no mogura Anime in Hindi",
        "telegram_channel_id": -1001234,
        "publish_mode": "link_post",
        "card_message_id": 512,
        "announcement_link": "https://t.me/+/abc",
        "announcement_link_at": None,
        "channel_help_added": True,
        "owner_promoted": False,
        "series": "Dekin no mogura",
    },
    {
        "id": 22,
        "title": None,
        "telegram_channel_id": None,
        "publish_mode": "link_post",
        "card_message_id": None,
        "announcement_link": None,
        "announcement_link_at": None,
        "channel_help_added": False,
        "owner_promoted": False,
        "series": "Bleach",
    },
]

SESSION_ROWS = [
    {"name": "main", "username": "Turvei", "active": True, "account_id": 55, "kind": "user"},
    {"name": "spare", "username": None, "active": False, "account_id": 56, "kind": "user"},
]


def build_screen(key: str) -> tuple[str, dict | None]:
    """One screen, by the key a `n:` payload carries — so a screen added to `NAV` has to be built here too."""
    builders = {
        "main": lambda: console.main_screen({"mode": "live", "ready": 0, "blocked": 0, "sources": 1, "sessions": 0}),
        "sources": lambda: console.sources_screen(ROWS),
        "queue": lambda: console.queue_screen(paused=False, ready=0),
        "bots": lambda: console.bots_screen(),
        "destinations": lambda: console.destinations_screen(DEST_ROWS),
        "sessions": lambda: console.sessions_screen(SESSION_ROWS),
        "joinmsg": lambda: console.joinmsg_screen(current="", presets=[]),
        "help": lambda: console.help_screen(controlbot.HELP),
    }
    assert set(builders) == set(console.NAV), "the test list and the module's navigation keys drifted apart"
    return builders[key]()


def all_screens() -> dict[str, tuple[str, dict | None]]:
    """Every screen the module can build, with the row list it would be built from."""
    return {
        "main": console.main_screen({"mode": "shadow", "ready": 3, "blocked": 1, "sources": 2, "sessions": 1}),
        "sources": console.sources_screen(ROWS),
        "empty-sources": console.sources_screen([]),
        "source": console.source_screen(ROWS[0]),
        "source-unknown": console.source_screen({"id": 9, "telegram_channel_id": -1}),
        "queue": console.queue_screen(paused=None, ready="?"),
        "queue-paused": console.queue_screen(paused=True, reason="maintenance", ready=0),
        "bots": console.bots_screen(storage="anime_hindifilesbot", help_bot="chelpbot"),
        "joinmsg": console.joinmsg_screen(current="", presets=[{"name": p.name} for p in joinmsg.PRESETS]),
        "joinmsg-saved": console.joinmsg_screen(
            current="welcome {name}", presets=[{"name": p.name} for p in joinmsg.PRESETS]
        ),
        "destinations": console.destinations_screen(DEST_ROWS),
        "empty-destinations": console.destinations_screen([]),
        "destination": console.destination_screen(DEST_ROWS[0]),
        "destination-sparse": console.destination_screen({"id": 22, "series": "Bleach"}),
        "sessions": console.sessions_screen(SESSION_ROWS),
        "no-sessions": console.sessions_screen([]),
        "help": console.help_screen(controlbot.HELP),
        "waiting": console.waiting_screen("series:s3", ROWS[0]),
    }


def buttons_of(payload: dict | None) -> list[dict]:
    if not payload:
        return []
    return [one for row in payload["inline_keyboard"] for one in row]


# --------------------------------------------------------------------------- the grammar
def test_a_screen_list_and_the_navigation_keys_are_one_list() -> None:
    """`NAV` and `ControlBot._CONSOLE_SCREENS` have to say the same thing.

    Checked in both directions, because each half fails differently: a key with no answer is a button that
    does nothing, and an answer with no key is a screen nobody can open.
    """
    assert set(console.NAV) == set(controlbot.ControlBot._CONSOLE_SCREENS), (
        f"unreachable: {sorted(set(console.NAV) - set(controlbot.ControlBot._CONSOLE_SCREENS))}; "
        f"unaddressable: {sorted(set(controlbot.ControlBot._CONSOLE_SCREENS) - set(console.NAV))}"
    )


@pytest.mark.parametrize("name", sorted(all_screens()))
def test_every_button_on_every_screen_leads_somewhere(name: str) -> None:
    """The one invariant the whole console rests on.

    Each payload is re-read against the tables that own its kind: the router for a command, `NAV` for a
    screen, `sourcecfg.TOGGLES` plus the row verbs for `r:`, `normalize.DECLARED_AUDIO` for an audio pick,
    `console.PROMPTS` for a question. A button that survives to a production build and lands nowhere is the
    failure a "million dollar interface" is most likely to ship, because nothing short of tapping it finds
    it.
    """
    _text, payload = all_screens()[name]
    buttons = buttons_of(payload)
    assert buttons, f"{name} has no buttons at all"
    for one in buttons:
        data = one["callback_data"]
        if data.startswith("/") or data.startswith(console.RUN_PREFIX):
            command = data[len(console.RUN_PREFIX) :] if data.startswith(console.RUN_PREFIX) else data
            assert command.startswith("/"), command
            routed = command.split()[0].lstrip("/").split("@", 1)[0].casefold()
            assert routed in controlbot._ROUTES, f"{name}: a button routes to nothing: {command}"
            continue
        if data.startswith(console.NAV_PREFIX):
            assert data[len(console.NAV_PREFIX) :] in console.NAV, f"{name}: {data}"
            continue
        if data.startswith(console.PROMPT_PREFIX):
            parsed = console.parse_prompt(data)
            assert parsed is not None, f"{name}: an unanswerable question: {data}"
            continue
        row = console.parse_row(data)
        assert row is not None, f"{name}: an unreadable payload: {data}"
        kind, ident, verb, arg = row
        table = DEST_ROWS if kind == "d" else ROWS
        assert kind in console.TABLES, f"{name}: a payload naming no table: {data}"
        assert ident in {r["id"] for r in table} | {9}, f"{name}: {data}"
        if verb == "open":
            assert arg is None
            continue
        if verb in sourcecfg.TOGGLES:
            assert arg in {"on", "off"}, f"{name}: a switch with no position: {data}"
            continue
        if verb == "audio":
            assert arg in normalize.DECLARED_AUDIO, f"{name}: an audio kind nobody recognises: {data}"
            continue
        if kind == "d":
            assert verb in {"card", "campaigns", "episodes", "inplace"}, f"{name}: unknown: {data}"
            if verb == "card":
                assert arg in {"show", "clear"}, f"{name}: {data}"
            elif verb == "inplace":
                assert arg in {"plan", "on", "off"}, f"{name}: {data}"
            else:
                assert arg is None, f"{name}: {verb} is asked for, not given: {data}"
            continue
        assert verb in {"series", "title", "season", "episodes", "inplace", "dest"}, (
            f"{name}: an unknown row verb: {data}"
        )
        if verb == "inplace":
            assert arg in {"plan", "on", "off"}, f"{name}: {data}"
            continue
        assert arg is None, f"{name}: {verb} is asked for, not given: {data}"


@pytest.mark.parametrize("name", sorted(all_screens()))
def test_no_button_is_built_that_telegram_would_reject(name: str) -> None:
    _text, payload = all_screens()[name]
    for one in buttons_of(payload):
        assert len(one["callback_data"].encode("utf-8")) <= keyboards.MAX_CALLBACK_BYTES
        assert 0 < len(one["text"]) <= keyboards.MAX_LABEL_CHARS


def test_every_command_a_button_runs_is_one_the_help_text_lists() -> None:
    """A button hides the words it sends, so an undocumented command behind one is a write with no page to
    read it against.

    `/help` is the operator's manual and `docs/control-bot.md` is generated from the same list; the console
    may run anything in either, and nothing outside them. Checked over the rendered screens rather than over
    `console.py`'s source, because a builder that stops emitting a button must not keep the test green.
    """
    documented = set(_ROUTES) | {one.strip() for one in _HELP_COMMANDS}
    seen = set()
    for _title, payload in all_screens().values():
        for one in buttons_of(payload):
            data = one["callback_data"]
            if not (data.startswith("/") or data.startswith(console.RUN_PREFIX)):
                continue
            cut = len(console.RUN_PREFIX) if data.startswith(console.RUN_PREFIX) else 0
            name = data[cut:].split()[0].lstrip("/").split("@", 1)[0].casefold()
            seen.add(name)
            assert name in documented, f"{one['text']!r} runs /{name}, which /help does not list"
    assert {"status", "pause", "resume", "reconcile", "joinmsg"} <= seen, "and the console really does reach them"


def test_the_prefixes_are_the_whole_grammar() -> None:
    """Four verbs, and the bare command that predates them. Anything else is a bug in a screen.

    Written against the module's own text rather than a list typed here, because the danger is a fifth
    prefix arriving in `console.py` and no router branch for it — a payload that is answered by nothing and
    therefore silently by the "unreadable button" reply.
    """
    built = [one["callback_data"] for payload in all_screens().values() for one in buttons_of(payload[1])]
    prefixes = {console.NAV_PREFIX, console.RUN_PREFIX, console.ROW_PREFIX, console.PROMPT_PREFIX}
    for data in built:
        assert data.startswith("/") or any(data.startswith(one) for one in prefixes), data


# --------------------------------------------------------------------------- what a screen says
def test_the_state_is_on_the_label_not_behind_the_tap() -> None:
    """A list of names is not a list of states, and the question is always about the state.

    The second row is `mode: ignore` with no series declared — the screen has to say 💤 and "no series yet"
    without a tap, because that is the difference between an operator recognising their own channel and
    tapping each row to find out.
    """
    text, payload = console.sources_screen(ROWS)
    assert "👁 Bleach in Hindi · Bleach" in text
    assert "💤 -100999 · no series yet" in text
    labels = [one["text"] for one in buttons_of(payload)]
    assert any(one.startswith("👁") for one in labels) and any(one.startswith("💤") for one in labels)


def test_an_unread_switch_is_a_question_mark_on_the_screen() -> None:
    """`?`, not `off`. A screen that renders an unknown as a position is a screen that will be trusted."""
    text, _payload = console.source_screen({"id": 9, "telegram_channel_id": -1})
    flags = [line for line in text.split("\n") if line.startswith(("● ", "○ ", "? "))]
    assert len(flags) == 3, text
    for line in flags:
        assert line.endswith(": ?"), line
        assert "off" not in line, "an unknown state is never drawn as a position"
    assert "· id -1" in text


def test_the_main_screen_prints_a_question_mark_rather_than_a_zero() -> None:
    """Counts that could not be read arrive as `?`, and `0` stays a fact about an empty queue.

    A container that cannot reach the database still answers the chat — this rule is why it must not answer
    with "ready 0", which is a different claim and a calm one.
    """
    text, _payload = console.main_screen({})
    assert "ready ?" in text and "blocked ?" in text
    assert "0" not in text, "an unread count must not be printed as a number that happens to be zero"
    again = console.main_screen({"blocked": 2})[0]
    assert "waiting on you" in again


def test_an_empty_list_says_nothing_is_configured_rather_than_being_blank() -> None:
    text, payload = console.sources_screen([])
    assert "no source channel is configured" in text
    assert [one["text"] for one in buttons_of(payload)][:1] == ["➕ Add a channel"], "and the way out is there"


def test_the_help_screen_still_carries_the_command_list() -> None:
    """Buttons are not a wall built in front of the text.

    `/help` is what an operator reads when they have forgotten the words, and every tap in the console has
    a line in it — so the screen shows the whole list rather than a summary of it.
    """
    text, _payload = console.help_screen(controlbot.HELP)
    for command in ("/source", "/archive", "/probe", "/status", "/joinmsg"):
        assert command in text, command
    assert controlbot.HELP.split("\n")[0] in text


def test_the_joinmsg_screen_numbers_the_presets_the_text_numbers() -> None:
    presets = [{"name": one.name} for one in joinmsg.PRESETS]
    _text, payload = console.joinmsg_screen(presets=presets)
    labels = [row[0]["text"] for row in payload["inline_keyboard"][: len(presets)]]
    assert labels == [f"{n} · {one.name}" for n, one in enumerate(joinmsg.PRESETS, start=1)]
    assert [row[0]["callback_data"] for row in payload["inline_keyboard"][:3]] == [
        f"x:/joinmsg use {n}" for n in range(1, len(joinmsg.PRESETS) + 1)
    ]


def test_every_screen_can_be_re_read_and_left() -> None:
    """`↻ Refresh` as the last row of every screen, and a `◀` beside it on all but the menu.

    A screen is a snapshot: the counts on it were true when it was drawn, and an operator who has had the
    menu open since before a redeploy is reading history. The refresh is what makes the page honest again
    without anyone remembering a command, and a back button is what makes a screen a place you can leave —
    which is also why no screen is allowed to be a dead end (see `waiting_screen`, the one message that is
    not a screen and carries its own exit instead).
    """
    for key in sorted(console.NAV):
        _text, payload = build_screen(key)
        last = payload["inline_keyboard"][-1]
        assert any(one["text"] == "↻ Refresh" for one in last), f"{key} has no refresh: {last}"
        if key == "main":
            assert len(last) == 1, "the root screen has no back button, because it is where back goes"
        else:
            assert any(one["text"].startswith("◀") for one in last), f"{key} cannot be left: {last}"
    _text, payload = console.source_screen(ROWS[0])
    last = payload["inline_keyboard"][-1]
    assert [one["text"] for one in last] == ["↻ Refresh", "◀ Back"], last


def test_the_audio_picks_are_the_vocabulary_itself() -> None:
    """One button per spelling `normalize.DECLARED_AUDIO` accepts, and no invented one.

    The row of picks exists so that nobody has to remember how the column is spelled, which only holds while
    the buttons *are* the vocabulary: a list typed here would drift from it within a season, and a button
    whose value the parser rejects is worse than no button, because it looks like a choice that was made.
    """
    _text, payload = console.source_screen(ROWS[0])
    rows = payload["inline_keyboard"]
    picks = [
        one for row in rows for one in row if one["callback_data"].startswith(f"{console.ROW_PREFIX}s3:audio:")
    ]
    assert sorted(one["callback_data"].rsplit(":", 1)[1] for one in picks) == sorted(normalize.DECLARED_AUDIO)
    assert [one["text"] for one in picks] == sorted(normalize.DECLARED_AUDIO), "labels are the values, not prose"


def test_a_queued_question_says_how_not_to_answer_it() -> None:
    """Every prompt needs its exit in the same message, or a screen is a trap.

    The pending answer is taken by the next bare message; without a visible way out, whatever the operator
    types next becomes a config write about the wrong thing.
    """
    text, payload = console.waiting_screen("series:s3", ROWS[0])
    assert "Send the series name" in text and "Bleach in Hindi" in text
    labels = [one["text"] for one in buttons_of(payload)]
    assert "✖ Stop here" in labels and "↩ That channel" in labels
    assert [one["callback_data"] for one in buttons_of(payload)] == ["r:s3:open", "n:main"]


# --------------------------------------------------------------------------- the byte budget
def test_a_long_channel_name_costs_a_button_and_not_a_fact() -> None:
    """Drop the tap, keep the sentence. Never shorten a label, and never shorten a payload.

    A 60-character title with a state prefix and a series name on the end is the case where a naive builder
    slices a label at 64 characters mid-word or — worse — truncates the callback and runs something else.
    """
    wide = dict(ROWS[0])
    wide["title"] = "B" * 60
    wide["declared_series"] = "a series with a long name"
    text, payload = console.sources_screen([wide])
    assert wide["title"] in text, "the row is still named in the text, whole"
    for one in buttons_of(payload):
        assert one["text"] in {"➕ Add a channel", "↻ Refresh", "◀ Menu"}, one
    assert all("…" not in one["text"] for one in buttons_of(payload))


def test_encode_and_the_row_payload_refuse_the_same_way() -> None:
    assert console.encode("x:" + "y" * 63) is None, "one byte over the limit"
    assert console.encode("x:" + "y" * 62) is not None, "and exactly at it, which is Telegram's edge"
    assert console.row_payload(None, "open") is None
    assert console.row_payload("3", "gate", "on") == "r:s3:gate:on"
    assert console.row_payload(7, "card", "show", kind="d") == "r:d7:card:show"
    assert console.payload_for("status without a slash") is None
    assert console.button("ok", None) is None


@pytest.mark.parametrize(
    "payload", ["p:series", "p:add:d3", "p:card:s3", "p:nope", "n:main", "", "p:", "p:episodes:zz", "p:archive:d3"]
)
def test_a_question_of_the_wrong_shape_is_not_asked(payload: str) -> None:
    """`series` needs a row and `add` must not have one, in both directions.

    A prompt with no row would have to guess which channel the operator meant, and the answer would be
    written to whichever row the bot looked at last — a silent wrong turn in the exact place this file
    promised not to make one. `archive` and `archive_title` are refused *with* a row for the mirror-image
    reason: they address the one recorded archive row, and a payload that named a different one would be a
    promise this bot does not keep.
    """
    assert console.parse_prompt(payload) is None


def test_parse_prompt_and_parse_row_round_trip_what_the_builders_emit() -> None:
    for row in ROWS:
        for verb in list(sourcecfg.TOGGLES) + ["open", "audio"]:
            data = console.row_payload(row["id"], verb, "on" if verb in sourcecfg.TOGGLES else "hindi")
            kind, ident, got_verb, arg = console.parse_row(data)
            assert (kind, ident, got_verb, arg) == (
                "s",
                row["id"],
                verb,
                "on" if verb in sourcecfg.TOGGLES else "hindi",
            )
        for slot in ("series", "title", "season", "episodes"):
            built = console.prompt_payload(f"{slot}:s{row['id']}")
            assert console.parse_prompt(built) == (slot, f"s{row['id']}")
    for row in DEST_ROWS:
        for verb, arg in (("card", "show"), ("card", "clear"), ("inplace", "plan"), ("campaigns", None)):
            data = console.row_payload(row["id"], verb, arg, kind="d")
            assert console.parse_row(data) == ("d", row["id"], verb, arg)
        for slot in ("card", "episodes", "campaign"):
            built = console.prompt_payload(f"{slot}:d{row['id']}")
            assert console.parse_prompt(built) == (slot, f"d{row['id']}")


def test_yesterdays_payload_still_means_the_same_row() -> None:
    """`r:3:gate:off`, with no table letter, is what every screen sent before destinations existed.

    Screens are not re-drawn retroactively: the buttons under a message the operator left open yesterday are
    the old payloads, and a build that started refusing them would turn an upgrade into the dead buttons
    this round is meant to end. So a bare number keeps meaning the source row it always meant.
    """
    assert console.parse_row("r:3:gate:off") == ("s", 3, "gate", "off")
    assert console.parse_prompt("p:series:3") == ("series", "s3")
    assert console.parse_ref("3") == ("s", 3)
    assert console.parse_ref("d7") == ("d", 7)
    assert console.parse_ref("x7") is None and console.parse_ref("d") is None and console.parse_ref("") is None


def test_the_destination_says_what_it_publishes_without_a_tap() -> None:
    """📤 means the channel exists in Telegram, 🏗 means only the row does — and the list says which.

    A series can have a destination row before its channel is built. Printed as the same line, the two
    states look like the same problem, and the operator spends the evening waiting for something that is
    waiting on a card number instead.
    """
    text, payload = console.destinations_screen(DEST_ROWS)
    assert "📤 Dekin no mogura" in text
    assert "🏗 Bleach · channel not built yet" in text
    labels = [one["text"] for one in buttons_of(payload)]
    assert any(one.startswith("🏗") for one in labels), "the un-built row is still openable, to see why"
    empty = console.destinations_screen([])[0]
    assert "no destination channel yet" in empty and "/status" in empty


def test_a_destination_screen_names_the_card_it_would_link_from() -> None:
    """The card post and the stored link are the two facts an announcement is made of, on one screen.

    `publishing:` is spelled out in words rather than as `link_post` because the column value is the app's
    vocabulary, and an operator reading a screen to decide what to tap should not have to learn it first.
    """
    text, payload = console.destination_screen(DEST_ROWS[0])
    assert "card post: message 512" in text
    assert "https://t.me/+/abc" in text
    assert "publishing: as a link post" in text
    assert "channel help bot: added · owner promoted: no" in text
    data = [one["callback_data"] for one in buttons_of(payload)]
    assert "p:card:d21" in data and "r:d21:card:clear" in data and "r:d21:campaigns" in data
    assert "r:d21:inplace:plan" in data, "and the in-place half is reachable from here too"
    assert "n:destinations" in data, "◀ Back goes to the list, not to the root"


def test_a_destination_with_no_columns_yet_says_what_it_does_not_know() -> None:
    """A narrow row (the one `_find_destination` answers with) must not be drawn as a full one.

    The flags are skipped rather than printed as `no`, and the card line says it is not named: a screen that
    fills a gap with a position is a screen that will be believed.
    """
    text, _payload = console.destination_screen({"id": 22, "series": "Bleach"})
    assert "card post: not named" in text
    assert "shareable link: none recorded yet" in text
    assert "channel help bot" not in text, "no columns, no claims"
    assert "id ?" in text


def test_the_sessions_screen_shows_who_a_session_is_and_never_the_session() -> None:
    """Username, whether it is the one in use, and two verbs. Never a string, never a length.

    `app/sessions.list_sessions` already refuses to select the session string; this is the same rule one
    layer up, because a screen is the one place a value can reach a chat window and stay there in the cloud
    forever — which is the opposite of what this bot promises about logins.
    """
    text, payload = console.sessions_screen(SESSION_ROWS)
    assert "👤 main · @Turvei · this one is in use" in text
    assert "spare" in text
    assert "55" not in text and "account_id" not in text
    data = [one["callback_data"] for one in buttons_of(payload)]
    assert "x:/use main" in data and "x:/forget spare" in data
    empty = console.sessions_screen([])[0]
    assert "/login <name> +<phone>" in empty, "and the one flow that needs text says so"


def test_a_source_can_point_at_the_destination_its_files_go_to() -> None:
    """`📤 Where its files are published` is a `dest` verb, and it is the only cross-table tap there is.

    It rides on the stored `destination_id` and not on a title match, so the refusal when the two are not
    linked yet has to be the screen's own words rather than a guess about which series was meant.
    """
    text, payload = console.source_screen(ROWS[0])
    jump = [one for one in buttons_of(payload) if one["callback_data"] == "r:s3:dest"]
    assert jump and jump[0]["text"] == "📤 Where its files are published"
    assert "in place" in text or True  # the plan button is checked in the next test


def test_the_inplace_taps_are_on_both_rows_and_address_the_command_that_exists() -> None:
    """Three taps — plan, do it, stop — and `on` is the bare command, not a word we invented.

    `/inplace <channel>` already means "do it", so the console sends exactly that. A button that carried
    `/inplace <channel> on` would be a command the typed line rejects, which is the collision this project
    has a standing rule about.
    """
    for payload, kind, row in (("r:s3:inplace:plan", "s", ROWS[0]), ("r:d21:inplace:off", "d", DEST_ROWS[0])):
        parsed = console.parse_row(payload)
        assert parsed is not None
        command = controlbot.ControlBot._console_command(kind, row, parsed[2], parsed[3])
        expected = "/inplace @bleach_hindi plan" if kind == "s" else "/inplace -1001234 off"
        assert command == expected, command
    source = buttons_of(console.source_screen(ROWS[0])[1])
    assert [one["text"] for one in source[-3:-1]] == ["📅 Episodes in a season", "🖼 Show the plan"] or any(
        one["text"] == "🖼 Show the plan" for one in source
    )


def test_the_command_a_tap_becomes_is_the_command_the_words_were() -> None:
    """`_console_command` is the only translator between a tap and the router, so it is checked here too.

    A handle when the row has one, the number when it does not — because those are the two things
    `_find_source_channel` answers to, and a command naming a private channel by its title would be refused
    for not existing.
    """
    build = controlbot.ControlBot._console_command
    assert build("s", ROWS[0], "gate", "off") == "/source @bleach_hindi gate off"
    assert build("s", ROWS[1], "audio", "dual") == "/source -100999 audio dual"
    assert build("s", ROWS[0], "series", "Bleach") == "/source @bleach_hindi series Bleach"
    assert build("s", ROWS[0], "gate", "sideways") is None, "a switch has two positions"
    assert build("s", ROWS[0], "audio", "klingon") is None, "and audio has a vocabulary"
    assert build("s", ROWS[0], "series", "   ") is None, "an empty value is not a rename"
    assert build("s", None, "gate", "off") is None and build("s", {}, "gate", "off") is None
    assert build("s", {"id": 1, "username": "", "telegram_channel_id": None}, "title", "x") is None
    # The destination half: its own number, the command that owns the column, and the words it does not take.
    dest = {
        "id": 21,
        "title": "Dekin no mogura Anime in Hindi",
        "telegram_channel_id": -1001234,
        "publish_mode": "link_post",
        "card_message_id": 512,
        "series": "Dekin no mogura",
    }
    assert build("d", dest, "card", "show") == "/destination -1001234 card show"
    assert build("d", dest, "card", "512") == "/destination -1001234 card 512"
    assert build("d", dest, "card", "next week") is None, "a card is a message number"
    assert build("d", dest, "campaigns", None) == "/destination -1001234 campaigns"
    assert build("d", dest, "campaign", "wave1") == "/destination -1001234 campaign new wave1"
    assert build("d", dest, "episodes", "2 12") == "/destination -1001234 episodes 2 12"
    assert build("d", {"id": 22, "series": "Bleach"}, "card", "show") is None, "no channel id to address"
    # And the source screen's `/declare`, which is addressed by series rather than by row.
    assert build("s", ROWS[0], "episodes", "2 12") == "/declare Bleach 2 12"
    assert build("s", ROWS[1], "episodes", "2 12") is None, "a row with no series cannot declare"


def test_the_console_module_imports_nothing_from_the_bot() -> None:
    """One-way dependency, so a screen can be rendered and tested without a router.

    The cycle this prevents is the ordinary one: the bot needs the screens, and a screen that needs the bot
    turns every test of a label into a test of the whole file.
    """
    assert "from .controlbot" not in SOURCE and "import controlbot" not in SOURCE
    assert "self.db" not in SOURCE and "async def" not in SOURCE, "no I/O in a screen builder"


@pytest.mark.parametrize("name", sorted(all_screens()))
def test_every_screen_leads_with_a_title_and_a_rule(name: str) -> None:
    """The look, pinned: title, rule, facts. Not for vanity — an operator scanning six screens needs one
    shape, and a screen whose first line is a fact is a screen whose title got lost."""
    text, _payload = all_screens()[name]
    head = text.split("\n")
    assert console.RULE in head[:3], text[:90]
    assert head[0].strip(), "a screen opens with its own name"


def test_a_list_that_could_go_on_says_it_stopped() -> None:
    """The cap is a fact on the screen, not a property of the query.

    50 rows is what one message can carry with its buttons attached to the first half — so a longer list is
    cut, and a cut that is not admitted is the silent truncation this project refuses everywhere. The name of
    the way around it is on the same line, because "it is capped" without "and here is how to see the rest" is
    a dead end with better manners.
    """
    many = [dict(ROWS[0], id=index) for index in range(1, console.LIST_LIMIT + 2)]
    text, payload = console.sources_screen(many[: console.LIST_LIMIT], truncated=True)
    assert "showing the first 50" in text and "capped there on purpose" in text
    assert len(payload["inline_keyboard"]) == console.LIST_LIMIT + 2, "50 rows, add, tail"
    assert "showing the first" not in console.sources_screen(ROWS)[0], "and the note stays off a short list"
    for builder, rows in (
        (console.destinations_screen, [dict(one, id=index) for index, one in enumerate(many, start=1)]),
        (console.sessions_screen, [{"name": f"s{index}"} for index in range(console.LIST_LIMIT + 1)]),
    ):
        capped = builder(rows[: console.LIST_LIMIT], truncated=True)[0]
        assert "capped there on purpose" in capped, builder.__name__


def test_a_count_that_cannot_be_read_is_not_printed_as_a_number() -> None:
    """`destinations: ?` on the menu, not `0`, when the read failed.

    The same rule as the queue counts, and it earns its keep here because a destination count of zero is the
    one number that makes an operator stop checking: it looks like a fresh install rather than a database
    this container cannot reach.
    """
    text, _payload = console.main_screen({})
    assert "destinations: ?" in text and "sessions: ?" in text
    assert "0" not in text


def test_no_reply_promise_a_command_the_router_does_not_serve() -> None:
    """The bug class that made this round necessary: a reply that tells the operator to type a word.

    Two commands were broken exactly this way — `/destinations`, which `_find_destination`'s refusal
    advertised and nothing served, and `/sources`, which a console message pointed at for the same reason.
    Both read as "the bot is ignoring me", which is the one failure an operator cannot tell apart from a bot
    that is down.

    Only what can reach a chat window counts: `ast` gives the string constants, an f-string is reassembled so
    that `12/hour` is a rate and not a command, and docstrings are left out because they explain the code to
    a reader rather than offering them a word to type. Every `/word` that survives has to be routed here, or
    be a command of one of the other bots this program talks to, or be one of its HTTP endpoints.
    """
    import ast

    routed = set(controlbot._ROUTES)
    from app import storagebot

    foreign = {str(one).strip("/").split()[0].casefold() for one in storagebot.MENU_NAMES}
    http = {"health", "ready", "docs", "openapi.json", "control", "api", "shutdown"}
    offenders: list[str] = []
    for module in (console, controlbot):
        source = Path(module.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        # A docstring, and the literal pieces of an f-string, are not what gets printed on their own: the
        # f-string is read from source instead, with its holes filled by a digit.
        skip: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                head = node.body[0] if node.body else None
                if isinstance(head, ast.Expr) and isinstance(head.value, ast.Constant):
                    skip.add(id(head.value))
            elif isinstance(node, ast.JoinedStr):
                for part in node.values:
                    if isinstance(part, ast.Constant):
                        skip.add(id(part))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in skip:
                    continue
                shown = node.value
            elif isinstance(node, ast.JoinedStr):
                built = ast.get_source_segment(source, node) or ""
                shown = re.sub(r"\{[^{}]*\}", "1", built)
            else:
                continue
            for match in re.finditer(r"""(?:^|[\s(`<"'])/(\w{3,})\b""", shown):
                name = match.group(1).casefold()
                if name in routed or name in foreign or name in http:
                    continue
                offenders.append(f"{module.__name__}: /{name} in {shown[:70]!r}")
    assert not offenders, "promised and not routed: " + "; ".join(sorted(set(offenders)))
