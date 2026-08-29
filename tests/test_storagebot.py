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


def test_the_handler_drives_only_the_verb_that_was_walked() -> None:
    """`storage_upload` exists now, and what it refuses is the *undriven* half of that bot's menu.

    This test used to say "the handler must stay a loud failure". The handler is written, so the
    promise moved: `/batch` is what the operator walked through on 2026-08-29 and it is the only verb
    this program sends. `/genlink`, `/custom_batch` and `/special_link` are real — the menu proves
    that — but each one is a choice about how the operator's post reads, and a handler that picked one
    would be making that choice on their behalf. The sentence `/status` prints is checked against the
    data it is built from, so a docstring cannot drift away from the queue's own words.
    """
    from app.handlers import DEPENDENCIES, JobKind, _stub, build_registry  # noqa: PLC0415

    reason = DEPENDENCIES[JobKind.STORAGE_UPLOAD.value]
    for fragment in ("/genlink", "/custom_batch", "/special_link"):
        assert fragment in reason, "the reason must name the verbs this program refuses to drive"
    assert "authenticated" in reason.lower(), "and name the session the conversation needs"
    # The observed flow reaches the operator through this same string, not through a summary someone
    # wrote by hand next to it: `/status` prints what the data says.
    assert storagebot.flow_note() in reason
    assert "Link_providerobot" in reason, "the sibling clone that answers the same sentence is a trap"

    # No kind in the supported vocabulary is served by a stub any more — and the marker that proves
    # that has to exist, or the assertion below would pass on a typo. The registry is built with
    # placeholders because a registry built *without* a database is the shadow-of-a-shadow: it holds
    # only the readers, and every write kind would fall back to a stub for a reason that has nothing
    # to do with this assertion.
    registry = build_registry(db=object(), settings=object())
    assert not any(getattr(handler, "is_stub", False) for handler in registry.values()), (
        "a job kind the queue can claim but nobody performs"
    )
    assert getattr(_stub(JobKind.STORAGE_UPLOAD.value), "is_stub", False) is True
    assert len(registry) == len(list(JobKind))


# --- the observed /batch flow, and the vendor's claims ----------------------------------------


def test_the_flow_is_recorded_as_data_and_not_as_prose() -> None:
    """`BATCH_FLOW` is the two prompts, in the bot's own words, plus what it answered with.

    Quoted verbatim on purpose: a tidied copy of somebody's UI text is a string that will never
    match their UI text again, and this is the pair a future handler has to recognise.
    """
    steps = storagebot.BATCH_FLOW
    assert len(steps) == 3
    assert steps[0].verbatim.startswith("Forward The Batch First Message From your Batch Channel")
    assert "or Give Me Batch First Message link" in steps[0].verbatim
    # The bot's own inconsistent capitalisation of "Your"/"last" is part of the quote.
    assert "From Your Batch Channel" in steps[1].verbatim
    assert "Batch last message link" in steps[1].verbatim
    assert steps[1].verbatim != steps[0].verbatim.replace("First", "Last"), "quoted, not generated"
    assert "Here is your link:" in steps[2].verbatim
    assert "?start=" in steps[2].verbatim and "SHARE URL" in steps[2].verbatim
    for step in steps:
        assert step.ours, "every prompt needs our side of it, or it is a quote with no use"
    assert storagebot.FLOW_OBSERVED_ON == "2026-08-29"


def test_the_doc_carries_the_flow_and_the_ephemeral_warning() -> None:
    """The screenshots exist once; the doc is what keeps them readable in a year.

    Both prompts are pinned from the code rather than re-typed, so a doc that quietly "improves"
    the wording fails here instead of teaching the next reader the wrong strings.
    """
    # Collapsed, because a doc that wraps a sentence over two lines is still the same sentence.
    doc = " ".join(DOC.read_text(encoding="utf-8").split())
    for step in storagebot.BATCH_FLOW[:2]:
        assert " ".join(step.verbatim.split()) in doc, f"{step.verbatim[:32]}… drifted out of the doc"
    for fact in (
        "END OF SEASON",  # what a range re-sends, labels included
        "deleted after 5 minutes",  # the warning, and what it does *not* apply to
        "never a reference to a message id inside the bot chat",
        "link_from_another_bot",
        "one batch per episode holding every quality",
    ):
        assert fact in doc, f"the doc lost: {fact}"


def test_the_vendor_claims_are_labelled_as_claims() -> None:
    """Everything known about the clone ecosystem comes from the vendor's channel, not from us.

    The label is the point: these sentences make four design decisions defensible (Private Mode,
    the permanent username, the admin rule, the moderator list) and not one of them is evidence
    about *our* clone. A test is the only thing that keeps a confident doc from turning into code.
    """
    doc = " ".join(DOC.read_text(encoding="utf-8").split())
    section = doc.split("## Where this bot comes from", 1)[1].split("## ", 1)[0]
    assert "vendor's word" in section and "not an observation of ours" in section
    assert storagebot.PARENT_CHANNEL in section, "the source URL is the one thing to re-check"
    for claim in (
        "@Md_CloneManagerBot",
        "up to 3 clones per Telegram account",
        "Private Mode",
        "No Forward",
        "no db channel required",
        "Clones are still functioning as before",
    ):
        assert claim in section, f"the provenance section lost {claim!r}"


def test_ownership_of_the_clone_does_not_unlock_the_people_verbs() -> None:
    """These three are *our* verbs now, aimed at *our* users, and still unsendable.

    The operator's answer changed why the rule exists and not the rule: a job that can broadcast to
    an audience or ban a person is a capability this program has no reason to hold, whatever the
    menu calls it. The count is pinned because a fourth name appearing here means someone reclassified
    a verb, which deserves a conversation.
    """
    assert sorted(storagebot.FORBIDDEN) == ["/ban", "/broadcast", "/unban"]
    for name in storagebot.FORBIDDEN:
        command = next(c for c in storagebot.MENU if c.name == name)
        assert "never sent" in command.ours
    assert "/broadcast" in storagebot.MODERATOR_ONLY, "the vendor's own moderator list gates it"


def test_still_unknown_is_answered_in_the_right_direction() -> None:
    """Four questions were answered this week; the list has to show it.

    The phrases below are what the screenshots and the vendor's channel settled. If one of them
    reappears in `still_unknown()` then either the answer was lost or a doc was rewritten to be
    more modest than the evidence, and both are worth failing over.
    """
    joined = " ".join(storagebot.still_unknown()).casefold()
    for settled in (
        "a forwarded message id?",
        "a text message with a URL, or a button",
        "moderator of the bot's service, or of the channel",
        "whether we get one, several, or none",
    ):
        assert settled not in joined, f"already answered, still listed as unknown: {settled!r}"
    # And the questions that *are* open must not be quietly dropped from the doc or the code.
    for open_item in ("reference", "public mode", "no forward", "revoke", "rate limit"):
        assert open_item in joined, f"the unknown list lost {open_item!r}"
