"""`app/sourcecfg.py` — the switches, the row, and the two rules that keep them honest.

This module exists because the operator said, on 2026-08-29, that they were not going to keep opening a
database dashboard to configure one channel. Anything that replaces a form a person can fill in wrongly
in both directions has to be pinned by tests rather than by confidence, and the two things worth pinning
are not the strings — they are the rules about which columns may be touched at all.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from app import controlbot, ingest, normalize, sourcecfg

APP_DIR = Path(inspect.getsourcefile(ingest)).parent


# --------------------------------------------------------------------------- the rules
def test_every_column_either_insert_writes_is_read_somewhere() -> None:
    """The same rule, applied to the rows rather than only to the switches.

    Both insert statements are read out of the module's own source instead of typed here, because the
    list a hand-kept test asserts is the list that goes stale the day a column is added — and an added
    column nobody reads is precisely the bug this rule exists to stop.
    """
    text = Path(inspect.getsourcefile(sourcecfg)).read_text(encoding="utf-8")
    blocks = re.findall(r"insert into (app\.\w+) \(([^)]*)\)", text, re.S)
    assert sorted(table for table, _ in blocks) == ["app.archive_channel", "app.destination", "app.source_channel"], (
        "the source row, the archive row, and the destination row — the three things this bot may create"
    )

    # A row needs its own identity and its own timestamps whether or not anything reads them, and the
    # `id` is generated. Everything else has to earn its place in the statement.
    structural = {"id", "created_at", "updated_at"}
    for table, block in blocks:
        names = [name.strip() for name in block.replace("\n", " ").split(",") if name.strip()]
        assert names, table
        for column in names:
            if column in structural:
                continue
            readers = [
                path.name
                for path in sorted(APP_DIR.glob("*.py"))
                if path.name not in {"sourcecfg.py", "controlbot.py"}
                and re.search(rf"\b{re.escape(column)}\b", path.read_text(encoding="utf-8"))
            ]
            assert readers, f"{table}.{column} is written by an insert and read by nothing"


@pytest.mark.parametrize("column", sorted(sourcecfg.FLAG_COLUMNS))
def test_every_column_a_switch_writes_has_a_reader_outside_the_switch(column: str) -> None:
    """No toggle for a value nobody acts on.

    `app.source_channel` carries ``priority``, ``is_joined`` and a ``monitor_only`` mode that no code
    compares against. A switch for one of those would look exactly like the others on screen and do
    nothing — which is worse than not offering it, because the operator would then believe the setting
    is why a file did or did not move. The check is mechanical and deliberately blunt: the column name
    has to appear in some module that is neither the switch's own file nor the command that prints it.
    """
    readers = []
    for path in sorted(APP_DIR.glob("*.py")):
        if path.name in {"sourcecfg.py", "controlbot.py"}:
            continue
        if re.search(rf"\b{re.escape(column)}\b", path.read_text(encoding="utf-8")):
            readers.append(path.name)
    assert readers, f"{column} is written by a switch and read by nothing — remove the switch"


def test_no_switch_word_is_a_value_the_same_command_takes() -> None:
    """`audio hindi` and `hindi off` cannot share a word.

    Naming a switch after an audio kind was tried, and it ate the value out of
    `/source <channel> series Bleach audio hindi`: the parser stopped collecting the value the moment it
    saw a word it recognised as a switch, and the command reported "audio needs a value" for a line that
    was correct. Both lists are read from the code, so a rename trips this and not a screenshot.
    """
    value_words = set(normalize.DECLARED_AUDIO) | {"clear"}
    collision = sorted(set(sourcecfg.TOGGLES) & value_words)
    assert not collision, f"a switch name doubles as a value word: {collision}"
    assert not set(sourcecfg.TOGGLES) & set(controlbot.ControlBot._SOURCE_KEYS), (
        "a switch name doubles as a parameter name"
    )


def test_the_module_never_deletes_anything() -> None:
    """Zero-deletion is a rule about this program, not about one screen in it."""
    text = Path(inspect.getsourcefile(sourcecfg)).read_text(encoding="utf-8").lower()
    for forbidden in ("delete from", "truncate ", "drop table", "drop column"):
        assert forbidden not in text, f"{forbidden} has no business in this module"


# --------------------------------------------------------------------------- planning a row
def test_a_number_is_written_as_the_number_with_the_check_flagged_missing() -> None:
    plan = sourcecfg.plan_new("-1002575861262", title="Bleach HQ")

    assert plan["telegram_channel_id"] == -1002575861262
    assert plan["verified"] is False
    assert "not checked against Telegram" in sourcecfg.render_plan(plan)


def test_the_defaults_are_the_ones_the_pipeline_reads() -> None:
    plan = sourcecfg.plan_new("-1001")

    assert plan["mode"] == sourcecfg.MODE_WATCHING and plan["active"] is True
    assert plan["require_hindi_audio"] is True, "the Hindi-audio gate is on until it is switched off"
    assert plan["include_subbed"] is False, "subbed-only files are never in scope by default"
    assert plan["series"] is None, "a row with no series declared cannot name a destination"


@pytest.mark.parametrize("typed", ["@anime_uploads4u", "anime_uploads4u", "-100abc", "seven", ""])
def test_a_handle_without_a_channel_number_is_refused_not_invented(typed: str) -> None:
    """No Telegram answer, no number, no row — because the number is the whole identity of the row."""
    result = sourcecfg.plan_new(typed)
    assert isinstance(result, str), f"{typed!r} cannot be written into a row"
    assert "-100xxxxxxxxxx" in result or "add needs the channel" in result


def test_the_two_spellings_of_one_channel_have_to_agree() -> None:
    """A handle whose number contradicts the number typed is a refusal, not a coin toss."""
    entity = {"id": 2575861262, "username": "bleach_hindi", "title": "Bleach in Hindi"}

    agreed = sourcecfg.plan_new("-1002575861262", entity=entity)
    assert agreed["telegram_channel_id"] == -1002575861262 and agreed["verified"] is True
    assert agreed["username"] == "bleach_hindi" and agreed["title"] == "Bleach in Hindi"

    fought = sourcecfg.plan_new("-1009999999999", entity=entity)
    assert isinstance(fought, str) and "I will not pick" in fought


def test_what_telegram_said_beats_what_was_typed() -> None:
    """The title from the entity, the operator's `title …` only when there is no entity.

    Otherwise a typo in the dashboard would outlive the channel it describes, and the row is the thing
    `/status` prints from.
    """
    plan = sourcecfg.plan_new(
        "@bleach_hindi", entity={"id": 1, "username": "bleach_hindi", "title": "Bleach in Hindi"},
        title="my typo",
    )
    assert plan["title"] == "Bleach in Hindi"
    # A channel Telegram describes with no title keeps the operator's words instead of an empty cell,
    # and a handle with no Telegram answer at all is the refusal above, not a row built from a guess.
    quiet = {"id": 1, "username": "bleach_hindi", "title": None}
    assert sourcecfg.plan_new("@bleach_hindi", entity=quiet, title="my typo")["title"] == "my typo"


def test_a_user_is_not_a_source_channel() -> None:
    class _User:
        id = 42
        username = "somebody"

        def __init__(self) -> None:
            pass

    answer = sourcecfg.channel_entity(_User())
    assert isinstance(answer, str) and "not a channel" in answer
    assert sourcecfg.channel_entity(None) == "Telegram did not answer."


# --------------------------------------------------------------------------- reading state back
def test_an_unread_switch_is_reported_as_unread() -> None:
    """`None`, not "off". A screen that renders an unknown as a position is a screen that will be wrong."""
    assert sourcecfg.toggle_state({"mode": "monitor_only"}, sourcecfg.TOGGLES["watch"]) is None
    assert sourcecfg.toggle_state({"require_hindi_audio": None}, sourcecfg.TOGGLES["gate"]) is None
    assert "?? " in sourcecfg.flags_line({"mode": "monitor_only"})


@pytest.mark.parametrize(
    "column,value",
    [
        ("require_hindi_audio", True),
        ("require_hindi_audio", False),
        ("include_subbed", True),
        ("mode", "full"),
        ("mode", "ignore"),
    ],
)
def test_each_switch_reads_both_positions(column: str, value: object) -> None:
    toggle = sourcecfg.BY_COLUMN[column]
    expected = value in (True, "full")
    assert sourcecfg.toggle_state({column: value}, toggle) is expected


def test_the_words_it_accepts_are_on_and_off_in_the_languages_it_sees() -> None:
    for word in ("on", "ON", " haan ", "kar", "chalu", "1"):
        assert word.strip().casefold() in sourcecfg.ON_WORDS, word
    for word in ("off", "nahi", "band", "mat", "0", "no"):
        assert word.strip().casefold() in sourcecfg.OFF_WORDS, word
    for word in ("maybe", "later", ""):
        assert word not in sourcecfg.ON_WORDS | sourcecfg.OFF_WORDS


# --------------------------------------------------------------------------- what it writes
@pytest.mark.asyncio
async def test_the_insert_names_only_the_columns_this_program_reads() -> None:
    class Db:
        def __init__(self) -> None:
            self.sql: list[tuple[str, tuple]] = []

        async def fetchval(self, sql, *args):
            self.sql.append((sql, args))
            return 11

        async def execute(self, sql, *args):
            self.sql.append((sql, args))
            return 1

    db = Db()
    plan = sourcecfg.plan_new("-1001", title="x", series="Bleach")
    written = await sourcecfg.insert_channel(db, plan)

    assert written == 11
    insert, args = db.sql[0]
    names = re.search(r"insert into app\.source_channel \((.*?)\)", insert, re.S).group(1)
    listed = [name.strip() for name in names.split(",")]
    assert listed == [
        "telegram_channel_id",
        "username",
        "title",
        "mode",
        "active",
        "require_hindi_audio",
        "include_subbed",
        "created_at",
        "updated_at",
    ], listed
    assert args[0] == -1001 and args[2] == "x"
    assert "on conflict (telegram_channel_id) do nothing" in insert, "the unique index is the guard"
    assert "returning id" in insert

    series_sql, series_args = db.sql[1]
    assert "declared_series = $2" in series_sql and series_args[0] == 11 and series_args[1] == "Bleach"


@pytest.mark.asyncio
async def test_a_plan_without_a_series_writes_only_the_row() -> None:
    class Db:
        def __init__(self) -> None:
            self.calls = 0

        async def fetchval(self, sql, *args):
            self.calls += 1
            return 12

        async def execute(self, sql, *args):
            self.calls += 1
            return 1

    db = Db()
    await sourcecfg.insert_channel(db, sourcecfg.plan_new("-1001"))
    assert db.calls == 1, "no declaration was asked for, so none is written"


@pytest.mark.asyncio
async def test_a_race_that_loses_says_so_instead_of_claiming_a_row() -> None:
    class Db:
        async def fetchval(self, sql, *args):
            return None  # `on conflict do nothing` found the channel number taken

    assert await sourcecfg.insert_channel(Db(), sourcecfg.plan_new("-1001")) is None


@pytest.mark.asyncio
async def test_a_switch_updates_one_column_and_nothing_else() -> None:
    seen: list[tuple[str, tuple]] = []

    class Db:
        async def execute(self, sql, *args):
            seen.append((sql, args))
            return 1

    await sourcecfg.set_flag(Db(), 5, sourcecfg.TOGGLES["watch"], False)

    (sql, args), = seen
    assert args == (5, "ignore")
    assert re.search(r"set \w+ = \$2,", sql), f"one column assigned, then updated_at: {sql}"
    assert "where id = $1" in sql and "returning" not in sql
