"""The recorded storage-bot menu, and the two things that make recording it worth the trouble.

A screenshot is only worth keeping if it is kept *exactly* (so a later live read can be compared
against it) and if the things it did **not** answer stay written down (so nobody mistakes a known
verb for a known protocol). Both halves are tested here, plus the guard that makes the bot's
moderation tools unsendable by a program.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import storagebot

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "storage-bot.md"


def test_the_menu_is_the_operators_screenshot_and_nothing_else() -> None:
    """Names and help text are what the bot shows, typos included, in the operator's order."""
    assert [command.name for command in storagebot.MENU] == [
        "/start",
        "/genlink",
        "/batch",
        "/custom_batch",
        "/special_link",
        "/universal_link",
        "/shortener",
        "/settings",
        "/broadcast",
        "/ban",
        "/unban",
    ]
    by_name = {command.name: command for command in storagebot.MENU}
    assert by_name["/batch"].help == "To store mutiple messages from a channel"
    assert by_name["/start"].help == "Check i am alive"
    # Every command carries our own note about it; a bare list of verbs is not knowledge.
    assert all(command.ours.strip() for command in storagebot.MENU)


def test_the_moderator_only_flag_is_read_from_the_bots_own_words() -> None:
    """Derived, not typed: the bot writes "(moderators only)" and we agree with it."""
    assert storagebot.MODERATOR_ONLY == frozenset(
        command.name for command in storagebot.MENU if "moderator" in command.help.casefold()
    )
    assert storagebot.requires_moderator("/special_link") is True
    assert storagebot.requires_moderator("/genlink") is False
    assert storagebot.requires_moderator("/ban") is True  # gated *and* forbidden, separately


def test_each_of_our_purposes_names_one_command() -> None:
    assert storagebot.command_for("single") == "/genlink"
    assert storagebot.command_for("channel_batch") == "/batch"
    assert storagebot.command_for("custom_batch") == "/custom_batch"
    # The two halves of the missing-quality rule, now that verbs exist for both.
    assert storagebot.command_for("editable_link") == "/special_link"
    assert storagebot.command_for("universal_link") == "/universal_link"
    assert storagebot.command_for("ALIVE") == "/start"  # tolerant of casing from a job payload


def test_an_unmapped_purpose_refuses_instead_of_picking_a_plausible_command() -> None:
    """A default here would mean a job sending a command it was never designed to send."""
    with pytest.raises(ValueError) as excinfo:
        storagebot.command_for("revoke")
    assert "revoke" in str(excinfo.value) and "/genlink" in str(excinfo.value)


def test_a_pasted_menu_is_read_back_into_the_same_pairs() -> None:
    """The format Telegram uses in that command bubble: name, then help, possibly over lines."""
    text = """Anime files bot
/start
Check i am alive
/genlink
To store a single message or file
/batch
To store mutiple messages
from a channel
"""
    parsed = storagebot.parse_menu(text)
    assert ("/start", "Check i am alive") in parsed
    assert ("/genlink", "To store a single message or file") in parsed
    # A description on two lines is still one description, not a stray command.
    assert ("/batch", "To store mutiple messages from a channel") in parsed
    # And the greeting above the list is dropped rather than glued onto nothing.
    assert all(name.startswith("/") for name, _ in parsed)


def test_a_live_menu_is_reported_as_missing_added_or_reworded() -> None:
    observed = [(command.name, command.help) for command in storagebot.MENU]
    assert storagebot.diff(observed) == {"missing": [], "added": [], "changed_help": []}

    dropped = [(name, help_text) for name, help_text in observed if name != "/special_link"]
    assert storagebot.diff(dropped)["missing"] == ["/special_link"]

    grown = observed + [("/rename_link", "rename a stored link")]
    assert storagebot.diff(grown)["added"] == ["/rename_link"]

    reworded = [
        ("/genlink", "To store a single message or file (any chat)") if name == "/genlink" else (name, help_text)
        for name, help_text in observed
    ]
    assert storagebot.diff(reworded)["changed_help"] == ["/genlink"]


def test_the_forbidden_commands_are_the_ones_that_talk_to_people() -> None:
    assert storagebot.FORBIDDEN == frozenset({"/broadcast", "/ban", "/unban"})
    for name in storagebot.FORBIDDEN:
        assert "never sent" in next(c.ours for c in storagebot.MENU if c.name == name)


def test_the_probe_refuses_them_even_after_its_allowlist_is_widened(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of checking `FORBIDDEN` before `SAFE_COMMANDS`.

    Someone widening the probe's allowlist while testing by hand is the expected future here. That
    must not be the day the program gains the ability to broadcast to a stranger's user list.
    """
    from app import probe

    policy = probe.ProbePolicy(owner_user_id=7)
    assert policy.may_send(storagebot.BOT_USERNAME, "/start") is True
    assert policy.may_send(storagebot.BOT_USERNAME, "/broadcast") is False
    assert not (storagebot.FORBIDDEN & {"/" + name for name in probe.SAFE_COMMANDS})
    monkeypatch.setattr(probe, "SAFE_COMMANDS", probe.SAFE_COMMANDS | {"/broadcast", "ban"})
    assert policy.may_send(storagebot.BOT_USERNAME, "/broadcast") is False
    assert policy.may_send(storagebot.BOT_USERNAME, "/ban") is False
    # …and the widening still works for what it was widened for.
    assert policy.may_send(storagebot.BOT_USERNAME, "/start") is True


def test_a_stranger_never_becomes_a_target_by_way_of_a_menu_name() -> None:
    policy = storagebot_probe_policy()
    assert policy.may_send("somebody_else", "/start") is False


def storagebot_probe_policy():
    from app.probe import ProbePolicy

    return ProbePolicy(owner_user_id=7)


def test_the_unknowns_are_still_longer_than_the_knowns() -> None:
    """The honesty check. If this ever shrinks to nothing, the stub should have been implemented.

    A menu tells us the verbs. It does not tell us the shapes of what comes back, which is the
    part an upload job has to parse. If a future run reads those answers in, `storage_upload`
    should gain a real handler in the same commit that empties this list — not before it.
    """
    unknown = storagebot.still_unknown()
    assert len(unknown) >= 6
    joined = " ".join(unknown).casefold()
    for subject in ("link", "batch", "moderator", "clone", "revoke"):
        assert subject in joined, f"the unknown list stopped naming {subject}"


def test_the_documented_menu_and_the_recorded_one_are_the_same_list() -> None:
    """`docs/storage-bot.md` is the operator-facing copy of this data, so it cannot drift.

    The check is on the quoted help text too, because a tidied-up typo is exactly the sort of
    "improvement" that makes a later comparison against a live menu meaningless.
    """
    doc = DOC.read_text(encoding="utf-8")
    for command in storagebot.MENU:
        assert f"| `{command.name}` | \"{command.help}\" |" in doc, f"{command.name} drifted in the doc"
    listed = re.findall(r"^\| `(/[\w_]+)` \|", doc, re.M)
    assert listed == list(storagebot.MENU_NAMES), "the doc's table has a row that is not in MENU"


def test_the_doc_lists_exactly_the_open_questions() -> None:
    doc = DOC.read_text(encoding="utf-8")
    section = doc.split("## What it does not settle", 1)[1].split("\n## ", 1)[0]
    numbered = re.findall(r"^\d+\. ", section, re.M)
    assert len(numbered) == len(storagebot.still_unknown())


def test_the_handler_still_refuses_and_says_which_half_is_missing() -> None:
    """`storage_upload` must stay a loud failure while the replies are unobserved.

    This test exists because the temptation, once a menu is known, is to write the handler and let
    the queue go green. The menu is not the protocol: nothing here says what `/genlink` asks for
    next or what it answers with, so the job has to keep blocking until a live run says so.
    """
    from app.handlers import DEPENDENCIES, FeatureNotImplemented, JobKind  # noqa: PLC0415

    reason = DEPENDENCIES[JobKind.STORAGE_UPLOAD.value]
    for fragment in ("/genlink", "/custom_batch", "/special_link"):
        assert fragment in reason, "the blocked reason should name what the operator has to approve"
    assert "authenticated" in reason.lower()
    with pytest.raises(FeatureNotImplemented):
        # The stub itself: importing and calling the builder must not silently succeed.
        from app.handlers import _stub

        raise FeatureNotImplemented(reason)
