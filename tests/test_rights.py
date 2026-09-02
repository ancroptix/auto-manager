"""Reading our own place in a channel, from what the session can see.

The operator's instruction was blunt: *hum admin hai ya nahi, ye hume khud detect karna hoga* — the app
has to work this out instead of asking. That makes three things worth pinning down, and every test here
is about one of them:

* a channel is matched by @handle or by its marked id, never by its title (renames happen; ids do not),
* "we are a member" and "we never looked" are two different facts, and only the first may be written,
* a write that changed nothing must not look like a rights change in the audit trail.

The last one is why ``record`` returns counts rather than None: the report line is the only place an
operator learns that the probe saw two of their four channels.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app import rights as R


ADMIN = {"post_messages": True, "edit_messages": True, "delete_messages": True}
READ_ONLY = {"post_messages": False, "edit_messages": False}


class FakeDb:
    """Records the statements it is handed and reports a plausible row count."""

    def __init__(self, affected: int = 1) -> None:
        self.affected = affected
        self.statements: list[tuple[Any, ...]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        """`record` writes with `update … returning id`, so the fake answers like the driver does."""
        self.statements.append((sql, args))
        return [{"id": args[0]}] * (self.affected if sql.lstrip().lower().startswith("update") else 0)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# --- what the entity means ------------------------------------------------------------------


def test_rights_are_read_from_the_entity_and_not_invented() -> None:
    class Entity:
        def __init__(self, admin_rights: Any = None) -> None:
            if admin_rights is not None:
                self.admin_rights = admin_rights

    assert R.rights_of(Entity()) is None, "no admin_rights is a member, and that has to stay sayable"

    class Rights:
        def __init__(self, **kw: bool) -> None:
            self.__dict__.update(kw)

    got = R.rights_of(Entity(Rights(post_messages=True, edit_messages=False)))
    assert got is not None
    assert got["post_messages"] is True and got["edit_messages"] is False
    assert set(got) == set(R.RIGHT_NAMES), "the report names exactly the rights this project acts on"


def test_admin_without_posting_is_not_admin_for_this_pipeline() -> None:
    """A title change and a pin is not a publisher, so it is not `true` here.

    This is the whole reason the column is called what it is: a channel where we are admin but cannot
    post is a channel where in-place captioning would fail on the first edit, and a route that trusted
    `is admin` would pick exactly that.
    """
    assert R.we_are_admin({"post_messages": True}) is True
    assert R.we_are_admin(READ_ONLY) is False
    assert R.we_are_admin({}) is False
    assert R.we_are_admin(None) is False


def test_the_marked_id_is_built_as_text_and_both_spellings_are_accepted() -> None:
    """The `-100` prefix is string surgery. Arithmetic on it produced a wrong 15-digit number once
    already, and the failure mode was a private channel that matched nothing and was reported unseen."""
    assert R.marked_channel_id(2072936982) == -1002072936982
    assert R.marked_channel_id("  2072936982 ") == -1002072936982
    assert R.marked_channel_id("-1002072936982") == -1002072936982
    assert R.marked_channel_id(-2072936982) == -2072936982, "a negative that is not marked stays itself"
    for junk in (None, "", "@ycanime", "channel", "-100abc", 0):
        assert R.marked_channel_id(junk) is None


# --- matching -------------------------------------------------------------------------------


def test_a_configured_channel_is_matched_by_handle_or_by_marked_id() -> None:
    dialogs = [
        {"title": "Bleach HQ", "username": "ycanime_bleach", "id": 111, "rights": ADMIN},
        {"title": "Private Hindi", "username": None, "id": 2072936982, "rights": None},
    ]
    configured = [
        {"id": 1, "username": "@ycanime_bleach", "telegram_channel_id": -100111},
        {"id": 2, "username": None, "telegram_channel_id": "-1002072936982"},
    ]
    decided = R.plan(dialogs, configured)
    assert [(u["source_channel_id"], u["we_are_admin"], u["can_edit"]) for u in decided["updates"]] == [
        (1, True, True),
        (2, False, False),
    ]
    assert decided["unseen"] == [] and decided["ambiguous"] == []


def test_a_title_alone_never_matches_a_row() -> None:
    """The operator may name a channel anything they like; the app may not conclude anything from it.

    A destination is created from `{TITLE} Anime in Hindi` and renamed by hand all the time, so title
    matching would eventually write rights onto the wrong series' channel.
    """
    dialogs = [{"title": "Re:Zero Anime in Hindi", "username": None, "id": 999, "rights": ADMIN}]
    configured = [{"id": 7, "username": None, "telegram_channel_id": -1001234}]
    decided = R.plan(dialogs, configured)
    assert decided["updates"] == []
    assert decided["unseen"] == ["-1001234"], "named in the report, not silently skipped"


def test_a_channel_the_session_cannot_see_keeps_whatever_was_stored() -> None:
    """Absence is not membership. Writing `false` because a dialog was missing would turn a probe that
    read nothing into a claim that the operator is wrong about their own channel."""
    configured = [
        {"id": 1, "username": "@gone", "telegram_channel_id": -1005},
        {"id": 2, "username": None, "telegram_channel_id": -1006},
    ]
    decided = R.plan([], configured)
    assert decided["updates"] == []
    assert decided["unseen"] == ["@gone", "-1006"]
    line = R.summary(decided, run_record({"updates": []}))
    assert "rights read for 0 configured channel(s), 0 changed" in line
    assert "@gone" in line and "-1006" in line


def run_record(decided: dict) -> dict:
    return run(R.record(FakeDb(), decided))


def test_two_rows_claiming_the_same_handle_are_left_for_a_human() -> None:
    dialogs = [{"title": "Dup", "username": "dupe", "id": 42, "rights": ADMIN}]
    configured = [
        {"id": 1, "username": "@dupe", "telegram_channel_id": -10042},
        {"id": 2, "username": "dupe", "telegram_channel_id": -10099},
    ]
    decided = R.plan(dialogs, configured)
    assert decided["updates"] == [], "dict order must not decide which series gets a rights write"
    assert len(decided["ambiguous"]) == 1 and "rows 1, 2" in decided["ambiguous"][0], (
        "one line naming both rows, because the fix is in the database, not in the channel"
    )
    assert "two rows claim the same channel" in R.summary(decided, {"considered": 0, "written": 0})


def test_a_left_channel_is_not_evidence_of_anything() -> None:
    dialogs = [{"title": "Used to be ours", "username": "exited", "id": 8, "rights": ADMIN, "left": True}]
    decided = R.plan(dialogs, [{"id": 3, "username": "@exited", "telegram_channel_id": -1008}])
    assert decided["updates"] == [] and decided["unseen"] == ["@exited"]


def test_a_dialog_summary_without_rights_is_treated_as_unread() -> None:
    """`probe_account` always fills `rights`; a *different* caller must not silently mean "member"."""
    dialogs = [{"title": "No rights key", "username": "nrk", "id": 9}]
    decided = R.plan(dialogs, [{"id": 4, "username": "@nrk", "telegram_channel_id": -1009}])
    assert decided["updates"] == [] and decided["unseen"] == ["@nrk"]


# --- writing --------------------------------------------------------------------------------


def test_the_write_names_the_column_it_touches_and_never_inserts() -> None:
    db = FakeDb(affected=1)
    counted = run(
        R.record(
            db,
            {
                "updates": [
                    {"source_channel_id": 1, "we_are_admin": True, "can_edit": True, "title": "A"},
                    {"source_channel_id": None, "we_are_admin": False, "can_edit": False, "title": "B"},
                ]
            },
        )
    )
    assert counted == {"considered": 2, "written": 1}, "a row with no id is skipped, not defaulted"
    assert len(db.statements) == 1, "the None row must not even be attempted"
    sql, args = db.statements[0]
    assert sql.lstrip().lower().startswith("update"), "rights are recorded on an existing row, never inserted"
    assert "returning id" in sql, "and the count of rows that moved is the honest number"
    assert "we_are_admin = $2" in sql and "rights_checked_at = now()" in sql
    assert "is distinct from" in sql, "only a changed value is written, so a stable probe leaves no noise"
    assert args == (1, True)
    assert "title" not in sql and "A" not in args, "the title is report text, never a key"


def test_the_summary_counts_and_names_without_softening() -> None:
    decided = {
        "updates": [{"source_channel_id": i} for i in (1, 2, 3)],
        "unseen": ["@gone"],
        "ambiguous": [],
    }
    assert R.summary(decided, {"considered": 3, "written": 2}) == (
        "rights read for 3 configured channel(s), 2 changed; not visible to this session: @gone"
    )
    # An empty plan is not "all good": say so in the same shape.
    assert "0 configured channel(s)" in R.summary({"updates": [], "unseen": [], "ambiguous": []}, {})


@pytest.mark.parametrize("value", [True, False, None])
def test_the_column_reads_as_the_route_thinks(value: bool | None) -> None:
    """`app.inplace.route_for` is the consumer, so the three states are checked where they are used:
    `None` is the narrow answer (source), `True` opens in-place, `False` stays a source."""
    from app.inplace import MODE_IN_PLACE, MODE_LINK, route_for

    route = route_for(we_are_admin=value, files_already_there=True, destination_exists=True, series="X")
    assert (route.mode == MODE_IN_PLACE) is (value is True)
    assert route.can_write is True or value is not True
    if value is not True:
        assert route.mode == MODE_LINK
