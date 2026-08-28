"""The updates channel's flow: recorded exactly, refused where it is not known, and unapproved.

Three screenshots are the whole evidence base for this module, so the tests hold three things apart
on purpose: the *observed* strings (which must survive verbatim), the *shape* the app renders (which
must equal the doc), and the boundaries that keep the flow from being acted on early — the probe will
not mint a link, and the announcement has no approved caption box.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app import captions, linkprovider, probe

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "updates-channel.md"

SAMPLE_TOKEN = "BQADAQAD0RoAAp4PgEa2qMuk0UmjRBYE"
SAMPLE_LINK = linkprovider.deep_link(SAMPLE_TOKEN)


# --- what was seen, verbatim ---------------------------------------------------------------


def test_the_observed_words_are_kept_as_the_bot_said_them() -> None:
    """Including the grammar that looks wrong: a bot's reply is data, not a draft to tidy."""
    assert linkprovider.REQUEST_TEXT == "Send A Message For To Get Your Shareable Link"
    assert linkprovider.REPLY_MARKER == "Here is your link:"
    assert linkprovider.COMMAND == "/genlink"
    assert linkprovider.BOT_USERNAME == "Link_providerobot"
    assert "  " not in linkprovider.REQUEST_TEXT, "the screenshot has one space between words"


def test_the_link_is_read_only_when_the_reply_actually_is_one() -> None:
    reply = f"{linkprovider.REPLY_MARKER}\n\n{SAMPLE_LINK}"
    parsed = linkprovider.parse_reply(reply)
    assert parsed["kind"] == "link"
    assert parsed["token"] == SAMPLE_TOKEN
    assert parsed["bot"] == linkprovider.BOT_USERNAME
    assert parsed["link"] == SAMPLE_LINK, "what we store is the token; what we print is this"

    # A link written in another case comes back in *that* case. Usernames are case-insensitive to
    # Telegram, but a public post should read like the bot's own message, not like a normalised key.
    shuffled = linkprovider.parse_reply(
        f"{linkprovider.REPLY_MARKER}\n\nhttps://t.me/LINK_ProviderBOT?start={SAMPLE_TOKEN}"
    )
    assert shuffled["link"].startswith("https://t.me/LINK_ProviderBOT?start=")
    assert shuffled["token"] == SAMPLE_TOKEN


def test_the_request_and_the_reply_are_not_confused_with_each_other() -> None:
    asked = linkprovider.parse_reply(f"https://t.me/{linkprovider.BOT_USERNAME}?start=x\n{linkprovider.REQUEST_TEXT}")
    assert asked["kind"] == "asks_for_a_message", "an ask must never be stored as a link"
    assert asked["link"] is None
    # Some other URL, no marker: unknown. A hallucinated link in an announcement reaches strangers.
    assert linkprovider.parse_reply("here you go: https://example.com/x")["kind"] == "unknown"
    assert linkprovider.parse_reply("")["kind"] == "unknown"
    assert linkprovider.parse_reply(None)["chars"] == 0


def test_the_token_is_what_is_stored_and_the_link_is_rebuilt_from_it() -> None:
    assert linkprovider.token_of(SAMPLE_LINK) == SAMPLE_TOKEN
    assert linkprovider.token_of("https://t.me/some_channel/42") is None
    assert linkprovider.is_deep_link(SAMPLE_LINK) is True
    rebuilt = linkprovider.deep_link(SAMPLE_TOKEN, bot="Other_providerbot")
    assert rebuilt == f"https://t.me/Other_providerbot?start={SAMPLE_TOKEN}", "a rename must not rot stored links"
    with pytest.raises(ValueError, match="goes nowhere"):
        linkprovider.deep_link("  ")


# --- the two shapes the operator's posts have -----------------------------------------------


def test_the_announcement_carries_series_season_and_episode() -> None:
    text = linkprovider.announcement_caption("Daemon of the shadow realm", 1, 14, SAMPLE_LINK)
    assert text.splitlines()[0] == "🍓 Daemon of the shadow realm (S1)"
    assert text.splitlines()[2] == "😗 Episode 14 Added...✨”"
    label = f"[{linkprovider.LINK_LABEL}]({linkprovider.deep_link(SAMPLE_TOKEN)})"
    assert text.splitlines()[-2:] == [label, label], "both samples repeat the link twice"


def test_a_single_digit_episode_is_padded_like_the_operator_writes_it() -> None:
    """The samples showed ``Episode 14`` and ``Episode 09``; the second one is the instruction."""
    text = linkprovider.announcement_caption("Re Zero", 4, 9, SAMPLE_LINK)
    assert "Episode 09 Added" in text


@pytest.mark.parametrize(
    "kwargs",
    [
        {"series": "", "season": 1, "episode": 3},
        {"series": "Solo Leveling", "season": None, "episode": 3},
        {"series": "Solo Leveling", "season": 1, "episode": None},
        {"series": "Solo Leveling", "season": 1, "episode": 3},
    ],
)
def test_an_announcement_refuses_to_be_built_from_less_than_it_claims(kwargs: dict) -> None:
    """No season, no episode, or a link that is not a link: refuse, do not round it out.

    A heading without ``(Sn)`` claims the whole series was added, and a link line that points
    anywhere but the bot is the one mistake this channel cannot take back.
    """
    bad_link = "https://t.me/+RM_bWDqzldg2OWFI" if len(kwargs) == 4 else kwargs.get("link", "https://example.com")
    with pytest.raises(ValueError):
        linkprovider.announcement_caption(
            kwargs["series"], kwargs["season"], kwargs["episode"], bad_link
        )


def test_a_link_that_is_not_a_bot_deep_link_is_refused_by_name() -> None:
    with pytest.raises(ValueError, match="deep link"):
        linkprovider.announcement_caption("Berserk", 1, 20, "https://t.me/+RM_bWDqzldg2OWFI")
    # And the markdown/text split is explicit, because a raw "[label](url)" in a plain-text post is
    # a visible bug in front of 33k people.
    plain = linkprovider.announcement_caption("Berserk", 1, 20, SAMPLE_LINK, style="text")
    assert "](http" not in plain and plain.count(SAMPLE_LINK) == 2
    with pytest.raises(ValueError, match="link style"):
        linkprovider.announcement_caption("Berserk", 1, 20, SAMPLE_LINK, style="html")


def test_the_card_caption_only_ever_carries_an_invite_link() -> None:
    card = linkprovider.card_caption("https://t.me/+RM_bWDqzldg2OWFI")
    assert card.splitlines()[0] == "Channel link"
    assert card.count("https://t.me/+RM_bWDqzldg2OWFI") == 2
    with pytest.raises(ValueError, match="invite link"):
        linkprovider.card_caption("https://t.me/some_public_channel")


def test_an_other_shaped_post_in_that_channel_is_not_treated_as_ours() -> None:
    """Reconciliation needs to tell an announcement from a special, a poll or somebody's note."""
    ours = linkprovider.announcement_caption("Tokyo ghoul", 4, 3, SAMPLE_LINK)
    assert linkprovider.announcement_matches_shape(ours)["is_ours"] is True
    assert linkprovider.announcement_matches_shape(ours)["episode"] == "03"
    for other in ("Poll: which season next?", "🍓 Tokyo ghoul (S4)", "A note about the schedule"):
        assert linkprovider.announcement_matches_shape(other)["is_ours"] is False


# --- the boundaries that keep it honest -----------------------------------------------------


def test_the_probe_may_ask_it_but_may_never_mint_a_link() -> None:
    """The bot is a probe target now, and the one verb that costs something stays shut."""
    policy = probe.ProbePolicy()
    assert policy.link_provider.casefold() == linkprovider.BOT_USERNAME.casefold()
    assert policy.may_send(policy.link_provider, "/start")
    assert policy.may_send(policy.link_provider, "/help")
    assert not policy.may_send(policy.link_provider, linkprovider.COMMAND), "a link costs a real request"
    assert linkprovider.NOT_FOR_PROBE <= {_strip(name) for name in probe._NEVER_SEND}


def _strip(name: str) -> str:
    return str(name).lstrip("/").casefold()


def test_widening_the_allowlist_does_not_enable_the_link_verb(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of ordering the guard before the allowlist, checked against this new verb."""
    monkeypatch.setattr(probe, "SAFE_COMMANDS", probe.SAFE_COMMANDS | {linkprovider.COMMAND.lstrip("/")})
    policy = probe.ProbePolicy()
    assert linkprovider.COMMAND.lstrip("/") in probe.SAFE_COMMANDS
    assert policy.may_send(linkprovider.BOT_USERNAME, linkprovider.COMMAND) is False


def test_the_announcement_is_not_an_approved_caption_yet() -> None:
    """The gate is a dict, and the absence of a key is the app saying "not authorised".

    If someone adds ``templates.announcement_post`` to the approved set, this test fails and they
    have to say why the operator approved it — which is the whole mechanism working.
    """
    assert "templates.announcement_post" not in captions.APPROVED_TEMPLATES
    assert "templates.announcement" not in "".join(captions.APPROVED_TEMPLATES)
    assert linkprovider.LINK_LABEL in linkprovider.announcement_caption("X", 1, 1, SAMPLE_LINK)


def test_the_blocked_jobs_name_this_flow() -> None:
    """`/status` must not be able to say "publishing works" while the announcement is unwritten."""
    from app.handlers import DEPENDENCIES

    assert "app/linkprovider.py" in DEPENDENCIES["publish_post"]
    assert "box not approved" in DEPENDENCIES["publish_post"]
    assert "Link_providerobot" in DEPENDENCIES["storage_upload"], "the sibling verb is a recorded hint"


def test_the_open_questions_stay_written_down_and_counted() -> None:
    unknown = linkprovider.still_unknown()
    assert len(unknown) >= 6
    joined = " ".join(unknown).lower()
    for subject in ("who posts", "expires", "/start menu", "emoji", "updates channel", "per episode"):
        assert subject in joined, f"the question about {subject!r} must stay on the record"
    assert "questions still open" in linkprovider.summary()
    assert str(len(unknown)) in linkprovider.summary()


def test_status_line_refuses_to_look_ready_from_one_half_alone() -> None:
    """A named channel and an unapproved box are different problems, and the line says both.

    This is the only place the two settings live, so it is also where a future reader is told that
    ``updates.channel`` being set is not the same as announcements being possible.
    """
    unset = linkprovider.status_line("", True)
    assert "not set" in unset and "nowhere to go" in unset
    named = linkprovider.status_line("@yc_updates", True)
    assert "@yc_updates" in named and "one announcement per episode" in named
    assert "NOT an approved caption box" in named, "setting the channel must not read as permission to send"
    batched = linkprovider.status_line("yc_updates", False)
    assert "one per batch" in batched and batched.startswith("updates channel:")
    # And the sentence only changes if the approval actually happens.
    captions.APPROVED_TEMPLATES["templates.announcement_post"] = "x"
    try:
        assert "box is approved" in linkprovider.status_line("@yc_updates", True)
    finally:
        del captions.APPROVED_TEMPLATES["templates.announcement_post"]


def test_the_two_config_rows_have_a_reader() -> None:
    """A config row nobody reads is documentation pretending to be a setting.

    ``/status`` is the reader today; this test is what fails loudly if the line is ever deleted and
    the rows are left behind as decoration.
    """
    source = (ROOT / "app" / "controlbot.py").read_text(encoding="utf-8")
    assert '"updates.channel"' in source and '"updates.per_episode"' in source
    assert "status_line" in source


# --- the doc cannot drift -------------------------------------------------------------------


def test_the_doc_quotes_the_observed_words_and_the_rendered_post() -> None:
    doc = DOC.read_text(encoding="utf-8")
    for text in (
        linkprovider.REQUEST_TEXT,
        linkprovider.REPLY_MARKER,
        linkprovider.BOT_USERNAME,
        linkprovider.SHARE_BUTTON,
        linkprovider.COMMAND,
    ):
        assert text in doc, f"{text!r} must appear verbatim in docs/updates-channel.md"
    sample = linkprovider.announcement_caption("Daemon of the shadow realm", 1, 14, SAMPLE_LINK)
    assert sample in doc, "the doc's announcement block has to be what the code renders"
    assert linkprovider.card_caption("https://t.me/+RM_bWDqzldg2OWFI") in doc


def test_the_doc_lists_exactly_the_open_questions() -> None:
    doc = DOC.read_text(encoding="utf-8")
    section = doc.split("## What it does not settle", 1)[1].split("\n## ", 1)[0]
    numbered = re.findall(r"^\d+\. ", section, re.M)
    assert len(numbered) == len(linkprovider.still_unknown())


def test_the_readme_links_this_flow() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/updates-channel.md" in readme
