"""The join-request message: a setting the operator can write at any time, and the refusals.

Three things had to stop living in a chat log (2026-08-29): the sentence itself, the fact that
*silence* is the shipped default, and the rules about what such a sentence may never contain. Each is
tested against the code that enforces it, because a policy stated only in prose is a policy that
outlives the person who remembered it.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app import joinmsg

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "supabase" / "migrations" / "0010_join_message_and_updates_id.sql"
SPEC = ROOT / "docs" / "requirements-draft.md"

from test_control_bot import FakeDb, bot, say  # noqa: E402  (conftest puts tests/ on the path)


# --- the presets are drafts, and stay drafts ---------------------------------------------------


def test_the_options_are_three_drafts_each_with_a_promise() -> None:
    assert len(joinmsg.PRESETS) == 3
    names = [preset.name for preset in joinmsg.PRESETS]
    assert names == ["welcome", "setup", "brief"]
    for preset in joinmsg.PRESETS:
        assert preset.text and preset.note
        # A draft that has to be edited before it can be sent is a draft that will be sent unedited.
        assert "{name}" in preset.text or "{series}" in preset.text, f"{preset.name} names nobody"
        assert "approve" not in preset.text.casefold(), f"{preset.name} decides about the request"
    # The `setup` draft is the one the code already knows how to read an answer to.
    from app import channels  # noqa: PLC0415

    assert channels.parse_setup_reply("join") == "join"
    assert channels.parse_setup_reply("stop") == "stop"


def test_the_numbering_the_options_text_presents_matches_the_list() -> None:
    text = joinmsg.options_text()
    for index, preset in enumerate(joinmsg.PRESETS, start=1):
        assert f"{index}. `{preset.name}`" in text, f"{preset.name} is not offerable at /joinmsg use {index}"
    assert "Currently saved" not in text, "an empty setting must not be quoted as a message"
    # Nothing saved is described by the *status* line, never by an options line that looks like text.
    assert "Currently saved" not in joinmsg.options_text("")
    assert "Currently saved" in joinmsg.options_text("hello {name}")


# --- what may not be written, and where that is enforced ----------------------------------------


@pytest.mark.parametrize(
    "text,reason",
    [
        ("Here is the link: t.me/+AbCdEf", "invite"),
        ("join us at https://t.me/joinchat/xyz", "invite"),
        ("You are approved, welcome!", "approve"),
        ("Humne aapka request approve kar diya", "approve"),
        ("See you in {channel}!", "placeholder"),
        ("x" * (joinmsg.MAX_CHARS + 5), "long"),
    ],
)
def test_a_message_that_would_breach_a_rule_is_refused_before_it_is_saved(text: str, reason: str) -> None:
    problems = joinmsg.refusals(text)
    assert problems, f"{text[:30]!r} should not be savable"
    joined = " ".join(problems).casefold()
    if reason == "invite":
        assert "invite" in joined and "never approves" not in joined
        assert "campaign_never_approves" in joined, "the schema's own constraint should be named"
    elif reason == "approve":
        assert "approves or declines" in joined or "approve" in joined
    elif reason == "placeholder":
        assert "{channel}" in joined and "{name}" in joined, "say what is allowed, not only what is not"
    else:
        assert str(joinmsg.MAX_CHARS) in joined


def test_the_three_allowed_placeholders_are_all_a_template_can_use() -> None:
    assert joinmsg.PLACEHOLDERS == ("{name}", "{series}")
    for preset in joinmsg.PRESETS:
        assert not joinmsg.unknown_placeholders(preset.text)
        assert joinmsg.render(preset.text, name="Ravi", series="One piece S2")


def test_render_refuses_a_placeholder_it_cannot_fill_rather_than_sending_it_blank() -> None:
    with pytest.raises(ValueError, match="needs a value"):
        joinmsg.render("hello {series}", name="Ravi")
    with pytest.raises(ValueError, match="only "):
        joinmsg.render("hello {invite}", name="Ravi", series="x")
    # Whitespace is folded, because the text is read from a chat input and a stray newline is not
    # something a stranger should receive as a paragraph break.
    assert joinmsg.render("  {name},\n\n  welcome  ", name="Ravi") == "Ravi, welcome"


# --- the row, and the fact that it ships empty ---------------------------------------------------


def test_the_migration_seeds_silence_and_only_fills_an_unfilled_channel_id() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "('joinrequest.message', '\"\"'::jsonb," in sql, "the default must be an empty message"
    assert "on conflict (key) do nothing" in sql
    # The id the operator gave is written, but only where nothing was chosen yet.
    assert re.search(r"update app\.config\s+set value = '\"-1002072936982\"'::jsonb", sql), sql[:200]
    assert re.search(r"and value = '\"\"'::jsonb;", sql), "an operator's earlier fill must survive"
    assert "campaign_never_approves" in sql, "the row's description has to carry the rule it sits beside"
    assert "join_request_campaign" in sql, "the description must not read as 'now it will send'"


def test_the_two_config_keys_the_app_reads_both_exist_in_the_seeded_rows() -> None:
    """Every key this code reads has to be seeded, and every seeded key has to be read.

    The paired check is what keeps a config row from becoming decoration: `joinrequest.message` is
    read by the command and by `/status`, and `updates.channel` by the announcement's own status line.
    """
    from app import controlbot, linkprovider  # noqa: PLC0415

    assert joinmsg.CONFIG_KEY == "joinrequest.message"
    source = (ROOT / "app" / "controlbot.py").read_text(encoding="utf-8")
    assert f"'{joinmsg.CONFIG_KEY}'" in source, "/status stopped showing the join message"
    assert "joinmsg" in controlbot._ROUTES  # noqa: SLF001
    assert "/joinmsg" in controlbot.HELP
    assert "updates.channel" in source and hasattr(linkprovider, "status_line")


# --- the command, driven end to end ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_command_saves_picks_refuses_and_never_claims_it_sent() -> None:
    control, _api, db = bot(db=FakeDb())

    saved = await say(control, "/joinmsg set {name}, aapka request dekh liya jaa raha hai")
    assert any("saved" in line and "join_request_campaign" in line for line in saved), saved
    assert db.config_rows["joinrequest.message"].startswith("{name}")

    shown = await say(control, "/joinmsg show")
    assert "Saved text (44 chars)" in shown[0], shown[0]
    assert "{name}, aapka request dekh liya jaa raha hai" in shown[0]
    assert "nobody has gone out" in shown[0].casefold() or "nothing has gone out" in shown[0].casefold()

    picked = await say(control, "/joinmsg use 2")
    assert any("saved" in line for line in picked), picked
    assert db.config_rows["joinrequest.message"] == joinmsg.PRESETS[1].text
    assert "What it promises" in picked[0], "picking an option must repeat the promise it carries"

    refused = await say(control, "/joinmsg set welcome, join via t.me/+abcd")
    assert not any("saved" in line for line in refused), refused
    assert "invite" in refused[0]
    assert db.config_rows["joinrequest.message"] == joinmsg.PRESETS[1].text, "a refusal writes nothing"

    cleared = await say(control, "/joinmsg clear")
    assert db.config_rows["joinrequest.message"] == ""
    assert any("cleared" in line for line in cleared)

    # The round trip in JSON, exactly as the database will store it.
    payload = db.writes[0][1][1]
    assert json.loads(payload) == "{name}, aapka request dekh liya jaa raha hai"


@pytest.mark.asyncio
async def test_an_out_of_range_option_and_an_unknown_subcommand_are_answered_without_touching_the_db() -> None:
    control, _api, db = bot(db=FakeDb())
    assert any("there are 3 options" in line for line in await say(control, "/joinmsg use 9"))
    assert any("needs a number" in line for line in await say(control, "/joinmsg use"))
    assert any("needs the words" in line for line in await say(control, "/joinmsg set"))
    assert any("I do not know" in line for line in await say(control, "/joinmsg broadcast"))
    assert any("takes no arguments" in line for line in await say(control, "/joinmsg clear all"))
    assert db.writes == [], "a usage answer must not be a write"


@pytest.mark.asyncio
async def test_no_subcommand_becomes_a_send() -> None:
    """Nothing here may queue or deliver anything: there is no sender, and no word for one.

    The words below are what a future implementation would reach for — 'ready', 'start', 'send',
    'approve' — and their absence from the command surface is the point. `app.join_campaign` keeps a
    status column for a sender that does not exist yet; a control-bot command that flipped it to
    `running` would be a campaign with no throttle in front of 33k people.
    """
    from app import controlbot  # noqa: PLC0415

    source = Path(controlbot.__file__).read_text(encoding="utf-8")
    block = source[source.index("    async def _joinmsg") : source.index("    async def _sessions")]
    for forbidden in ("ready", "running", "start", "send now", "approve", "decline"):
        assert forbidden not in block.casefold(), f"/joinmsg grew {forbidden!r} and must not"
    assert "join_request_campaign" in block, "and it still has to name what is missing"


def test_the_spec_stops_saying_tbd_for_this_sentence() -> None:
    spec = " ".join(SPEC.read_text(encoding="utf-8").split())
    assert "Message template: **the operator's, whenever they choose to write it.**" in spec
    assert "Message template: **TBD by operator.**" not in spec
    assert "/joinmsg" in spec, "§15 has to point at the command rather than the one that was not built"
    assert "announcements channel's post is created **by this program's own session**" in spec, (
        "the operator's ruling that Channel Help is not used for the announcements channel belongs in §11"
    )
