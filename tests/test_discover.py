"""`app/discover.py` — the sort, the two writers it borrows, and the four rules it must not break.

Discovery is the one feature in this project that *proposes rows* from something other than an operator's
typed line, so the tests are mostly about what it refuses: a channel it cannot name a series for, a series
that does not exist yet, a switch that would leave a season with nothing to read, and a role it never got to
see. The classification tests are here for the same reason — a wrong sort is a wrong row, and a wrong row is
a pipeline reading a friend's chat as an anime source.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from app import console, discover, handlers, sourcecfg
from app.channels import destination_name


def dialog(
    title: str,
    *,
    did: int,
    username: str | None = None,
    mine: bool = False,
    post: bool | None = None,
    left: bool = False,
    channel: bool = True,
    members: int | None = 10,
) -> dict[str, Any]:
    """One entry in the shape `app/probe.collect_dialogs` produces, rights spelled the same way."""
    return {
        "title": title,
        "username": username,
        "id": did,
        "mine": mine,
        "left": left,
        "channel": channel,
        "members": members,
        # The dict `app.rights.rights_of` returns: absent for a member, spelled out when we hold rights.
        "rights": None if post is None else {"post_messages": post, "edit_messages": post},
    }


class Db:
    """Two tables, one insert, and every statement remembered — which is all a sweep needs to be honest.

    Deliberately not `test_control_bot.FakeDb`: that fake answers the whole bot's queries, and a discovery
    test that leans on it would inherit its branch ordering. Here a query nobody expected returns `[]`,
    which is the answer that makes a wrong SQL string fail loudly instead of quietly.
    """

    def __init__(self, *, sources: list[dict] = (), destinations: list[dict] = (), series: list[dict] = ()) -> None:
        self.sources, self.destinations, self.series = sources, destinations, series
        self.sql: list[tuple[str, tuple]] = []
        self.config_value: Any = False
        # What a re-run finds when a row is already there: the mode the existing source row holds.
        self.mode = "full"

    async def fetch(self, statement: str, *args: Any) -> list[dict]:
        self.sql.append((statement, args))
        if "from app.source_channel" in statement:
            return list(self.sources)
        if "from app.destination" in statement:
            return list(self.destinations)
        if "from app.series" in statement:
            return list(self.series)
        if "returning id" in statement:  # `app/rights.record`, counting the rows that actually moved
            return [{"id": 3}]
        return []

    async def fetchval(self, statement: str, *args: Any) -> Any:
        self.sql.append((statement, args))
        if "insert into app.source_channel" in statement:
            return 901
        if "insert into app.destination" in statement:
            return 902
        if "insert into app.series" in statement:
            return 77
        return None

    async def fetchrow(self, statement: str, *args: Any) -> Any:
        """The three reads a pairing makes after it writes. The ids are the ones `fetchval` handed out,
        which is the point: a link built from a number nobody returned would pass a test that only counts
        statements and still put every source in the database on one channel."""
        self.sql.append((statement, args))
        if "from app.destination" in statement:
            return {"id": 902}
        if "select mode from app.source_channel" in statement:
            return {"mode": self.mode}
        if "from app.source_channel" in statement:
            return {"id": 901}
        return None

    async def execute(self, statement: str, *args: Any) -> int:
        self.sql.append((statement, args))
        return 1

    async def config(self, key: str, default: Any = None) -> Any:
        return self.config_value if key == discover.AUTO_KEY else default

    def names(self, needle: str) -> list[str]:
        return [statement for statement, _ in self.sql if needle in statement]

    def args(self, needle: str) -> list[tuple]:
        return [args for statement, args in self.sql if needle in statement]


# --------------------------------------------------------------------------- reading the role
def test_the_role_is_read_from_the_flags_and_nothing_else() -> None:
    assert discover.role_of(dialog("x", did=1, mine=True)) == discover.OWNER
    assert discover.role_of(dialog("x", did=1, post=True)) == discover.ADMIN
    assert discover.role_of(dialog("x", did=1)) == discover.MEMBER


def test_an_admin_who_may_not_post_is_a_member_for_everything_this_program_cares_about() -> None:
    """A title is not a publisher. The same definition `app/rights.we_are_admin` already uses.

    The case is real rather than hypothetical: channels hand out "admin" to whoever posts a schedule, and a
    bot that filed one as a destination would then be told to publish into a channel it cannot write in.
    """
    entry = dialog("x", did=1)
    entry["rights"] = {name: False for name in ("post_messages", "edit_messages", "add_admins")}
    assert discover.role_of(entry) == discover.MEMBER


def test_absence_from_the_dialog_list_is_not_read_as_member() -> None:
    """A channel this sweep could not see keeps its old mode, in both directions.

    `app/rights.py` holds the same rule for the flag it owns. Writing "member" because a read failed would
    turn a destination into a source on a network hiccup — the exact flip that costs a season its posts.
    """
    flips = discover.flips_to_apply(
        [{"id": 3, "telegram_channel_id": -100555, "declared_series": "Bleach", "mode": "full"}],
        {},
        watched_by_series={"Bleach": 2},
    )
    assert flips == []


# --------------------------------------------------------------------------- the name rule
def test_the_series_is_read_out_of_the_name_by_running_the_generator_backwards() -> None:
    assert discover.series_from_name("Bleach Anime in Hindi") == "Bleach"
    assert discover.series_from_name("Dekin no mogura Anime in Hindi") == "Dekin no mogura"
    assert discover.series_from_name("Bleach in Hindi") is None
    assert discover.series_from_name(None) is None


def test_a_renamed_template_moves_the_answer_instead_of_splitting_it() -> None:
    """The rule is not a remembered pattern, so a template change cannot leave discovery behind.

    `app/config.py` may hand the publisher a different `destination.template`; a regex here would keep
    matching the old words and call the renamed channels "not destinations", which is a silent
    misclassification of every channel the operator already made by hand.
    """
    named = destination_name("Bleach", template="{TITLE} in Hindi")
    assert discover.series_from_name(named, template="{TITLE} in Hindi") == "Bleach"
    assert discover.series_from_name(named) is None


# --------------------------------------------------------------------------- the sort
def test_a_channel_we_cannot_write_in_becomes_a_source_and_that_is_said() -> None:
    plan = discover.classify([dialog("anime_uploads4u", did=111, username="anime_uploads4u")])
    finding = plan["findings"][0]
    assert finding["use"] == "source" and finding["role"] == discover.MEMBER
    assert "we can only read it" in finding["why"] and "something to take in" in finding["why"]


def test_being_able_to_post_is_what_makes_a_channel_a_destination() -> None:
    """The bug the operator reported, as a test: their publishing channel is named like its source.

    `Naruto HQ` is a channel they run, so it is a destination — a name that is not `{TITLE} Anime in Hindi`
    stopped being a reason for no verdict at all, because their setup has both sides named `Mob Psycho 100`
    and discovery answered "nothing". What is *not* conceded here: a title that names no series and pairs with
    nothing is refused at the write, not filed under a guess.
    """
    plan = discover.classify(
        [
            dialog("Bleach Anime in Hindi", did=222, mine=True),
            dialog("Naruto HQ", did=888, post=True),
        ]
    )
    named, unnamed = plan["findings"]
    assert (named["use"], named["series"]) == ("destination", "Bleach")
    assert unnamed["use"] == "destination", "we can post there — that is what the word means"
    assert unnamed["series"] == "Naruto HQ", "named by its own title, which the reply has to say out loud"
    assert "we can post here" in unnamed["why"]


@pytest.mark.asyncio
async def test_a_channel_with_no_name_to_file_it_under_is_refused_at_the_write() -> None:
    db = Db()
    plan = discover.classify([dialog("", did=888, post=True)])
    refused = await discover.add_destination(db, plan["findings"][0])

    assert not refused["ok"] and "nothing here says what series this channel is for" in refused["text"]
    assert db.names("insert into app.series") == [], "and no series row was founded from an empty title"
    assert db.names("insert into app.destination") == []


def test_a_group_and_a_left_channel_are_skipped_out_loud() -> None:
    """Counted, not dropped. "Nothing to add" and "everything you own is already set up" have to differ.

    A chat turned into a source row would make the scanner read a conversation, and a channel the account
    left is not a channel to watch again — but the operator still has to see that both were *looked at*.
    """
    plan = discover.classify(
        [
            dialog("friends", did=5, channel=False),
            dialog("old shelf", did=6, username="old_shelf", left=True),
        ]
    )
    assert plan["findings"] == []
    assert plan["skipped"] == {"left": 1, "not_channels": 1}
    text = discover.report(plan, auto=False)
    assert "skipped, on purpose: 1 left, 1 not channels" in text
    assert "skipped" not in discover.report(discover.classify([]), auto=False), "and the line stays off a clean list"


def test_an_already_configured_channel_is_reported_as_configured_rather_than_being_offered_again() -> None:
    plan = discover.classify(
        [dialog("anime_uploads4u", did=111, username="anime_uploads4u")],
        sources=[{"id": 3, "telegram_channel_id": -100111, "username": "anime_uploads4u"}],
    )
    assert plan["findings"] == [] and len(plan["configured"]) == 1
    assert "source row 3" in plan["configured"][0]


def test_one_dialog_answering_twice_is_decided_once_and_named() -> None:
    plan = discover.classify(
        [dialog("shelf", did=777, username="shelf"), dialog("shelf again", did=777, username="shelf")]
    )
    assert len(plan["findings"]) == 1
    assert plan["duplicates"] == ["shelf again"]
    assert "listed one of these twice" in discover.report(plan, auto=False)


def test_the_grouping_never_picks_between_two_channels_named_for_one_series() -> None:
    groups = discover.group_for_linking(
        [
            {"index": 1, "use": "destination", "series": "Bleach", "channel": -1, "title": "a"},
            {"index": 2, "use": "destination", "series": "Bleach", "channel": -2, "title": "b"},
            {"index": 3, "use": "source", "series": None, "channel": -3, "title": "files"},
        ]
    )
    assert groups["Bleach"]["destination"]["index"] == 1
    assert [one["index"] for one in groups["Bleach"]["conflicts"]] == [2]
    assert "#-3" in groups, "a source with no series still gets its own group, keyed by its channel"


# --------------------------------------------------------------------------- the switch
def test_a_role_change_stops_the_reading_and_says_what_it_did() -> None:
    flips = discover.flips_to_apply(
        [{"id": 3, "telegram_channel_id": -100888, "title": "Naruto HQ", "declared_series": "Naruto", "mode": "full"}],
        {-100888: dialog("Naruto HQ", did=888, post=True)},
        watched_by_series={"Naruto": 2},
    )
    assert len(flips) == 1 and flips[0]["ok"] and flips[0]["row_id"] == 3
    assert "where posts go" in flips[0]["why"] and "reading stops here" in flips[0]["why"]


def test_the_only_source_of_a_series_is_never_switched_off() -> None:
    """The guard that makes auto mode safe: a series with one watched channel keeps it.

    Without this, becoming admin of the very channel a series is read from would silently stop that series
    forever — and the operator would find out from an empty channel weeks later.
    """
    flips = discover.flips_to_apply(
        [{"id": 3, "telegram_channel_id": -100888, "title": "Naruto HQ", "declared_series": "Naruto", "mode": "full"}],
        {-100888: dialog("Naruto HQ", did=888, post=True)},
        watched_by_series={"Naruto": 1},
    )
    assert flips and flips[0]["ok"] is False
    assert "only watched channel reading Naruto" in flips[0]["why"]
    assert "add another source for it first" in flips[0]["why"]


def test_a_channel_already_stopped_reading_is_not_reported_as_a_change_again() -> None:
    flips = discover.flips_to_apply(
        [{"id": 3, "telegram_channel_id": -100888, "declared_series": "Naruto", "mode": "ignore"}],
        {-100888: dialog("Naruto HQ", did=888, post=True)},
        watched_by_series={"Naruto": 2},
    )
    assert flips == []


# --------------------------------------------------------------------------- the rows it writes
@pytest.mark.asyncio
async def test_a_source_finding_is_written_by_the_command_writer_not_a_copy_of_it() -> None:
    db = Db()
    finding = discover.classify([dialog("shelf", did=111, username="shelf")])["findings"][0]
    result = await discover.add_source(db, finding)

    assert result["ok"] and result["what"] == "source" and result["row_id"] == 901
    insert = db.names("insert into app.source_channel")[0]
    assert "-100111" not in insert  # the id is a parameter, not a literal
    assert "on conflict (telegram_channel_id) do nothing" in insert
    assert "watching: on (mode full)" in result["text"], "the plan is rendered by sourcecfg, unchanged"


@pytest.mark.asyncio
async def test_a_series_that_is_not_stored_yet_is_founded_by_the_statement_the_pipeline_files_with() -> None:
    """Reversed on the operator's ask, and still one writer: a fresh install must not be stuck forever.

    Refusing until "the first file" existed was defensible while the only name on offer was one read out of
    the destination template. Their two channels are both named the show itself, so that refusal meant the
    setup could never be detected at all. The row is founded through `app/ingest.ensure_series` — asserted by
    the statement, because a second spelling of `normalized_title` is how one show ends up in two rows — and
    the reply says where the name came from, so a wrong spelling is visible before the second pair inherits it.
    """
    db = Db(series=[])
    finding = discover.classify([dialog("Bleach Anime in Hindi", did=222, mine=True)])["findings"][0]
    written = await discover.add_destination(db, finding)

    assert written["ok"] and written["series_id"] == 77
    assert db.names("insert into app.series"), "the series was founded, not waited for"
    assert "on conflict (normalized_title) do update" in db.names("insert into app.series")[0]
    assert db.args("insert into app.series")[0][0] == "Bleach", "the name from the channel title, stripped"
    assert "founded from the channel's own name" in written["text"]
    assert "Nothing was created in Telegram" in written["text"]


@pytest.mark.asyncio
async def test_a_series_that_matches_two_rows_is_refused_rather_than_picked() -> None:
    db = Db(series=[{"id": 7, "title": "Bleach"}, {"id": 8, "title": "Bleach movies"}])
    finding = discover.classify([dialog("Bleach Anime in Hindi", did=222, mine=True)])["findings"][0]
    refused = await discover.add_destination(db, finding)
    assert not refused["ok"] and "more than one stored series" in refused["text"]
    assert db.names("insert into app.destination") == []


@pytest.mark.asyncio
async def test_the_destination_row_carries_the_series_and_the_channel_and_nothing_invented() -> None:
    db = Db(series=[{"id": 7, "title": "Bleach"}])
    finding = discover.classify([dialog("Bleach Anime in Hindi", did=222, mine=True)])["findings"][0]
    written = await discover.add_destination(db, finding)

    assert written["ok"] and written["row_id"] == 902
    statement, args = db.names("insert into app.destination")[0], db.sql[-1][1]
    names = re.search(r"insert into app\.destination \((.*?)\)", statement, re.S).group(1)
    listed = [one.strip() for one in names.replace("\n", " ").split(",")]
    assert listed == ["series_id", "telegram_channel_id", "title", "publish_mode", "created_at", "updated_at"]
    assert args == (7, -100222, "Bleach Anime in Hindi", sourcecfg.DESTINATION_MODE)
    assert "Nothing was created in Telegram" in written["text"]


@pytest.mark.asyncio
async def test_a_channel_that_is_already_a_destination_is_not_written_twice() -> None:
    class Taken(Db):
        async def fetchval(self, statement, *args):  # `on conflict do nothing` found the channel taken
            self.sql.append((statement, args))
            return None

    db = Taken(series=[{"id": 7, "title": "Bleach"}])
    finding = discover.classify([dialog("Bleach Anime in Hindi", did=222, mine=True)])["findings"][0]
    result = await discover.add_destination(db, finding)
    assert not result["ok"] and "already a destination row" in result["text"]


# --------------------------------------------------------------------------- the sweep, end to end
@pytest.mark.asyncio
async def test_the_sweep_reads_roles_even_when_auto_is_off() -> None:
    """Noticing is free and switching is not, so the two are separable.

    The rights column is what `app/inplace.route_for` reads to decide whether a channel's own posts get the
    caption written onto them; leaving it stale because auto mode is off would cost the operator a fact they
    asked `/discover` for.
    """
    db = Db(sources=[{"id": 3, "telegram_channel_id": -100111, "username": "shelf", "mode": "full"}])
    out = await discover.sweep(db, [dialog("shelf", did=111, username="shelf")], auto=False)
    assert db.names("set we_are_admin"), "the read happened"
    assert out["flips"] == [] and out["plan"]["findings"] == []


@pytest.mark.asyncio
async def test_the_sweep_applies_a_switch_only_when_auto_is_on() -> None:
    dialogs = [dialog("Naruto HQ", did=888, post=True)]
    sources = [
        {"id": 3, "telegram_channel_id": -100888, "declared_series": "Naruto", "mode": "full"},
        # The other channel that reads this series: with it, stopping here strands nothing, and the switch
        # is allowed. Without it the guard above refuses — which is what that test is for.
        {"id": 4, "telegram_channel_id": -100999, "declared_series": "Naruto", "mode": "full"},
    ]

    quiet = await discover.sweep(Db(sources=sources), dialogs, auto=False)
    assert quiet["flips"] and not quiet["flips"][0]["applied"]
    assert quiet["flips"][0]["use"] == "flip"

    loud_db = Db(sources=sources)
    loud = await discover.sweep(loud_db, dialogs, auto=True)
    assert loud["flips"][0]["applied"]
    write = loud_db.names("update app.source_channel set mode")[0]
    assert "'ignore'" in write or "$2" in write, write
    assert "switched —" in discover.report(quiet["plan"], auto=True, applied=loud["flips"])


def test_the_switch_is_written_by_the_same_switch_writer_the_buttons_use() -> None:
    """No second path to `mode`: discovery flips the row with `app/sourcecfg.set_flag`.

    Asserted on the source text rather than on a call, because the thing being pinned is which module owns
    the write — a discovery that assembled its own `update … set mode` would be free to write a value the
    toggle's own validation refuses to accept.
    """
    text = Path(discover.__file__).read_text(encoding="utf-8")
    assert "sourcecfg.set_flag(" in text
    assert not re.search(r"update app\.source_channel set mode", text), "no private copy of the write"


def test_the_auto_switch_has_a_reader_outside_the_chat() -> None:
    """A screen's toggle must be read by production code, or it is a decoration.

    `app/handlers.py` reads `discover.auto` inside the reconciliation job — the same job that runs at boot —
    which is what makes "it switches on its own later" a sentence the bot is allowed to print.
    """
    text = Path(handlers.__file__).read_text(encoding="utf-8")
    assert "discover.AUTO_KEY" in text and "discover.sweep(" in text
    assert "ctx.telegram is not None" in text, "and it does nothing at all without a session to ask"


def test_auto_mode_is_explained_in_the_words_it_actually_runs_on() -> None:
    """No promise of "instantly" that the code cannot keep.

    The operator asked for the switch to happen instantly. What happens is: on every reconciliation, on every
    boot with it, and whenever the screen is opened — so the report says those three, and a test holds the
    sentence against the modules that read the flag so a future rewording cannot quietly oversell it.
    """
    text = discover.report(discover.classify([]), auto=True)
    assert "every reconciliation" in text and "whenever this screen is opened" in text
    assert "instant" not in text.lower()
    handlers_source = Path(handlers.__file__).read_text(encoding="utf-8")
    assert "reconciliation" in text and "reconciliation" in handlers_source


# --------------------------------------------------------------------------- the screen's wording
def test_a_refused_switch_is_printed_as_refused_rather_than_left_out() -> None:
    flips = discover.flips_to_apply(
        [{"id": 3, "telegram_channel_id": -100888, "declared_series": "Naruto", "mode": "full"}],
        {-100888: dialog("Naruto HQ", did=888, post=True)},
        watched_by_series={"Naruto": 1},
    )
    lines = discover.report(discover.classify([]), auto=True, applied=[dict(flips[0], applied=False)]).splitlines()
    text, payload = console.discover_screen(lines, [], auto=True)
    assert any("not switched" in line for line in lines), lines
    assert "only watched channel reading Naruto" in text, "the reason travels to the screen, not just the log"
    assert payload["inline_keyboard"][-1][0]["text"] == "↻ Refresh"


# --------------------------------------------------------------------------- one name, two channels
def test_two_channels_of_the_same_name_are_one_series_and_one_pair() -> None:
    """The operator's rule as written: `Mob Psycho 100` and `Mob Psycho 100` are one show, and what tells
    them apart is that this account can post in one of them. The template spelling is not a third thing — it
    strips down into the same group, so a channel named either way pairs with the other.
    """
    plan = discover.classify(
        [
            dialog("Mob Psycho 100", did=111, username="mps100"),
            dialog("Mob Psycho 100 Anime in Hindi", did=222, mine=True),
        ]
    )
    pairs = plan["pairs"]
    assert [pair["series"] for pair in pairs] == ["Mob Psycho 100"]
    source, destination = plan["findings"]
    assert (source["pair"], destination["pair"]) == (1, 1)
    assert source["series"] == destination["series"] == "Mob Psycho 100"
    assert (destination["channel"], source["channel"]) == (-100222, -100111)
    report = discover.report(plan, auto=False)
    assert "reads from Mob Psycho 100" in report and "publishes into Mob Psycho 100 Anime in Hindi" in report


def test_the_pairing_key_is_the_name_the_series_is_filed_under() -> None:
    """`app/normalize.normalize_title` decides both sides of every comparison in this program, so a pair key
    that used its own idea of "same name" would be a third spelling of a series title. That is the one thing
    a pairing cannot afford: a channel that pairs with nothing is a bug the operator can see, and a channel
    that pairs with the wrong show is not.
    """
    assert discover.name_key("Mob Psycho 100")[0] == discover.name_key("MOB PSYCHO 100")[0]
    assert discover.name_key("Mob Psycho 100 Anime in Hindi") == discover.name_key("Mob Psycho 100")
    assert discover.name_key("") is None, "a channel with no title has no name to pair on"


def test_two_channels_of_one_name_that_can_both_be_posted_in_are_left_alone() -> None:
    """Which of two identical-named channels holds the series is the operator's data to fix, and a pair
    picked by list order would put files where they did not ask for them.
    """
    plan = discover.classify(
        [
            dialog("Mob Psycho 100", did=111, username="mpsanime"),
            dialog("Mob Psycho 100", did=222, mine=True),
            dialog("Mob Psycho 100", did=333, post=True),
        ]
    )
    assert plan["pairs"] == [], "no group with two postable channels is pairable"
    posts = [one for one in plan["findings"] if one["use"] == "destination"]
    assert len(posts) == 2
    assert plan["findings"][0]["pair"] == 1 or plan["findings"][0]["ambiguous"] is None
    assert all("2 channels of this name can be posted in" in one["ambiguous"] for one in posts)


@pytest.mark.asyncio
async def test_one_tap_writes_the_series_both_rows_and_the_link_between_them() -> None:
    """What `✅ Set up <series>` is allowed to do, checked statement by statement.

    The order is the point as much as the set: the series row has to exist before the destination can point at
    it (`app.destination.series_id` is NOT NULL), and the link is the last write because a source that names a
    destination which is not there yet would publish nothing and look like the pipeline broke.
    """
    db = Db()
    plan = discover.classify(
        [
            dialog("Mob Psycho 100", did=111, username="mpsanime"),
            dialog("Mob Psycho 100 Anime in Hindi", did=222, mine=True),
        ]
    )
    result = await discover.apply_pair(db, plan["pairs"][0])

    assert result["ok"] and result["what"] == "pair" and result["sources"] == 1
    written = [needle for needle in ("insert into app.series", "insert into app.destination",
                                    "insert into app.source_channel") if db.names(needle)]
    assert len(written) == 3, written
    link = db.args("update app.source_channel set destination_id")
    assert link == [(901, 902)], "the source row and the destination row the writes just returned"
    assert "-100111" not in "".join(db.names("destination_id = $2"))
    assert "is watched, and what it posts is published into" in result["text"]
    assert "Nothing was created in Telegram" in result["text"], "the half this path never does"


@pytest.mark.asyncio
async def test_a_pair_that_is_half_written_is_finished_rather_than_refused() -> None:
    """Idempotence is the whole reason one tap can be tapped twice: `insert_channel` answers None for a row
    that exists, and if that ended the run the source would stay unlinked forever — the state the operator
    was stuck in, described back to them as "already configured".
    """
    db = Db()
    db.mode = "ignore"  # switched off earlier, by a role flip or by hand
    plan = discover.classify(
        [
            dialog("Mob Psycho 100", did=111, username="mpsanime"),
            dialog("Mob Psycho 100 Anime in Hindi", did=222, mine=True),
        ]
    )
    result = await discover.apply_pair(db, plan["pairs"][0])

    assert result["ok"]
    assert db.names("update app.source_channel set mode"), "the switch back on"
    assert "was switched off, and is watching again" in result["text"]
    assert db.args("update app.source_channel set destination_id") == [(901, 902)]


@pytest.mark.asyncio
async def test_the_series_name_read_off_a_channel_is_recorded_as_read_off_a_channel() -> None:
    """/status prints where a declaration came from, so a discovered name must not be filed as a typed one.

    The words "operator" on that line are what tells an operator that a mistake is theirs to fix in the
    dashboard; "discovered" is what tells them to check the spelling the screen just showed them.
    """
    db = Db()
    finding = discover.classify([dialog("Mob Psycho 100", did=111, username="mpsanime")])["findings"][0]
    written = await discover.add_source(db, finding)

    assert written["ok"]
    assert db.args("declared_by = $3")[0][2] == "discovered from this channel's name"
    assert db.args("declared_by = $3")[0][1] == "Mob Psycho 100"


def test_the_auto_sweep_asks_about_fewer_channels_than_the_screen_does() -> None:
    """A worker loop that spent half a minute on rights would delay the files it is there to move; the
    screen, whose only job is to answer "where am I admin", can afford the full read. The numbers are pinned
    so the two paths stay deliberately different instead of drifting into one.
    """
    from app import probe

    assert discover.AUTO_RIGHTS_LIMIT < probe.RIGHTS_LIMIT
