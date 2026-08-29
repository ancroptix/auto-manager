"""The updates channel's flow: recorded exactly, refused where it is not known, and unapproved.

Three screenshots are the whole evidence base for this module, so the tests hold three things apart
on purpose: the *observed* strings (which must survive verbatim), the *shape* the app renders (which
must equal the doc), and the boundaries that keep the flow from being acted on early — the probe will
not mint a link, and the announcement renders from the approved box rather than from a
copy of the sample someone kept in a comment.
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
    # The spelling has to be the same *name*, only folded differently: a different bot is a
    # different subject (see the test below), not a casing question.
    shuffled = linkprovider.parse_reply(
        f"{linkprovider.REPLY_MARKER}\n\nhttps://t.me/link_providerobot?start={SAMPLE_TOKEN}"
    )
    assert shuffled["kind"] == "link"
    assert shuffled["link"].startswith("https://t.me/link_providerobot?start=")
    assert shuffled["token"] == SAMPLE_TOKEN


def test_a_sibling_clone_with_the_same_words_is_not_the_answer() -> None:
    """The bug this check exists for: both bots in this family say "Here is your link:".

    @anime_hindifilesbot is the storage clone and @Link_providerobot mints the announcement link, and
    they are clones of one manager, so the sentence and the ``BQADAQAD`` token family are shared. A
    reply that carries a link to *some other* bot is therefore not the reply we asked for, and storing
    it as ours would publish a stranger's deep link to 33k people. The host is the identity.
    """
    sibling = f"{linkprovider.REPLY_MARKER}\n\nhttps://t.me/anime_hindifilesbot?start={SAMPLE_TOKEN}"
    parsed = linkprovider.parse_reply(sibling)
    assert parsed["kind"] == "link_from_another_bot"
    assert parsed["link"] is None and parsed["token"] is None
    assert parsed["bot"] == "anime_hindifilesbot", "the name of who really answered is kept"

    # Asked the other way round, the same text is a perfectly good link.
    storage = linkprovider.parse_reply(sibling, bot="anime_hindifilesbot")
    assert storage["kind"] == "link" and storage["token"] == SAMPLE_TOKEN


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


def test_the_announcement_box_is_approved_and_is_the_only_way_that_post_is_written() -> None:
    """Approved on 2026-08-28, in the same conversation that described the flow.

    The assertion is about *where the words live*, not about whether they are nice: the text must be
    in ``captions.APPROVED_TEMPLATES`` (the gate) and the renderer must read it from there, so an
    operator who edits the row in ``app.config`` changes the post without a redeploy. A test that only
    checked the rendered string would keep passing while the box stopped being editable.
    """
    assert "templates.announcement_post" in captions.APPROVED_TEMPLATES
    assert linkprovider.LINK_LABEL in linkprovider.announcement_caption("X", 1, 1, SAMPLE_LINK)
    assert "{title_full}" in captions.APPROVED_TEMPLATES["templates.announcement_post"]
    edited = captions.APPROVED_TEMPLATES["templates.announcement_post"].replace("Added", "ho gaya")
    try:
        assert "ho gaya" in linkprovider.announcement_caption("X", 1, 1, SAMPLE_LINK, template=edited)
    finally:
        assert "ho gaya" not in linkprovider.announcement_caption("X", 1, 1, SAMPLE_LINK)


def test_the_blocked_jobs_name_this_flow() -> None:
    """`/status` must not be able to say "publishing works" while the announcement is unwritten."""
    from app.handlers import DEPENDENCIES

    # The blocker must name the missing *plumbing*, never the wording: the box is approved now, and a
    # refusal that says "not approved" after approval is a refusal nobody can act on.
    for kind in ("publish_post", "edit_post"):
        assert "box not approved" not in DEPENDENCIES[kind]
        assert "docs/channel-help.md" in DEPENDENCIES[kind], f"{kind} must point at the documented path"
    assert "app/linkprovider.py" in DEPENDENCIES["publish_post"]
    assert "approved" in DEPENDENCIES["publish_post"], "and say plainly that the text is signed off"
    assert "Link_providerobot" in DEPENDENCIES["storage_upload"], "the sibling verb is a recorded hint"


def test_the_open_questions_stay_written_down_and_counted() -> None:
    unknown = linkprovider.still_unknown()
    assert len(unknown) >= 3
    joined = " ".join(unknown).lower()
    for subject in ("expires", "/start menu", "emoji", "rights"):
        assert subject in joined, f"the question about {subject!r} must stay on the record"
    # And the four things the operator answered must NOT still be listed as unknowns: an answered
    # question left in the list is how a doc ends up asking for a decision that was made months ago.
    for settled in ("who posts", "per episode", "one per show"):
        assert settled not in joined, f"{settled!r} was answered on 2026-08-28 and belongs in config"
    assert "questions still open" in linkprovider.summary()
    assert str(len(unknown)) in linkprovider.summary()


def test_status_line_refuses_to_look_ready_from_one_half_alone() -> None:
    """A named channel and an unapproved box are different problems, and the line says both.

    This is the only place the two settings live, so it is also where a future reader is told that
    ``updates.channel`` being set is not the same as announcements being possible.
    """
    saved = captions.APPROVED_TEMPLATES["templates.announcement_post"]
    unset = linkprovider.status_line("", True)
    assert "not set" in unset and "nowhere to go" in unset
    named = linkprovider.status_line("@yc_updates", True)
    assert "@yc_updates" in named and "one announcement per episode" in named
    assert "the box is approved" in named
    assert "unwired" in named, "approved must never read as wired: there is no sender yet"
    batched = linkprovider.status_line("yc_updates", False)
    assert "one per batch" in batched and batched.startswith("updates channel:")
    # The sentence only changes if the approval is withdrawn — which is a real thing an operator may do
    # by editing app.config, so the line has to follow the gate rather than a memory of it.
    del captions.APPROVED_TEMPLATES["templates.announcement_post"]
    try:
        unmade = linkprovider.status_line("@yc_updates", True)
        assert "approved" in unmade and "the box is approved" not in unmade
    finally:
        captions.APPROVED_TEMPLATES["templates.announcement_post"] = saved
    assert "the box is approved" in linkprovider.status_line("@yc_updates", True)


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
    # The same call the doc's generator makes, so this catches drift in either direction. A second
    # sample proves the template is not a typed copy of one post: the numbers move, the shape holds.
    assert linkprovider.announcement_caption("Re Zero", 4, 9, SAMPLE_LINK) in doc
    other = linkprovider.announcement_caption("Daemon of the shadow realm", 1, 14, SAMPLE_LINK)
    assert "Daemon of the shadow realm (S1)" in other and "Episode 14 Added" in other
    assert other.count(SAMPLE_LINK) == 2, "both samples repeat the link, so the template must too"
    assert linkprovider.card_caption("https://t.me/+RM_bWDqzldg2OWFI") in doc


def test_the_doc_lists_exactly_the_open_questions() -> None:
    doc = DOC.read_text(encoding="utf-8")
    section = doc.split("## What it does not settle", 1)[1].split("\n## ", 1)[0]
    numbered = re.findall(r"^\d+\. ", section, re.M)
    assert len(numbered) == len(linkprovider.still_unknown())


def test_the_readme_links_this_flow() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/updates-channel.md" in readme


# --- private channels and the id that names them -------------------------------------------


def test_a_private_channel_is_named_by_its_marked_id_not_invented_as_a_handle() -> None:
    """The operator's updates channel is private, so a numeric id is the *normal* spelling here.

    Two separate failure modes are being closed. A `status_line` that wrapped a number in `@` would
    tell the operator to look for a handle that does not exist; and an id built by arithmetic
    (`-100` times a power of ten, or an offset) is how a 13-digit channel silently stops matching its
    own row — so the exact number is pinned, in both directions.
    """
    # `marked_channel_id` is `app.rights`'s business — it is how a row and a channel are proved to be
    # the same place — but `/status` prints the same value, so the two spellings are pinned together.
    from app.rights import marked_channel_id

    assert marked_channel_id(2072936982) == -1002072936982
    assert marked_channel_id("2072936982") == -1002072936982
    assert marked_channel_id("-1002072936982") == -1002072936982, "already-marked stays as written"
    assert marked_channel_id("@yc_updates") is None and marked_channel_id(None) is None
    assert marked_channel_id("") is None and marked_channel_id("-abc") is None

    line = linkprovider.status_line("-1002072936982", True)
    assert line.startswith("updates channel: private channel -1002072936982")
    assert "no @handle, which is what private means" in line
    assert "@-1002072936982" not in line, "a number never becomes a handle"
    assert "one announcement per episode" in line
    assert linkprovider.status_line("yc_updates", False).startswith("updates channel: @yc_updates")


def test_the_approved_box_is_the_one_the_operator_signed_off() -> None:
    """The rendered post must equal the observed shape byte for byte, emoji, tail and both links."""
    text = linkprovider.announcement_caption("Re Zero", 4, 9, linkprovider.deep_link(SAMPLE_TOKEN))
    assert text == "\n".join(
        [
            "🍓 Re Zero (S4)",
            "",
            "😗 Episode 09 Added...✨”",
            "",
            f"[Click here to start and get episode]({linkprovider.deep_link(SAMPLE_TOKEN)})",
            f"[Click here to start and get episode]({linkprovider.deep_link(SAMPLE_TOKEN)})",
        ]
    )
    assert linkprovider.announcement_matches_shape(text)["is_ours"] is True
    # A post that only mentions an episode is not ours, even in that channel.
    assert linkprovider.announcement_matches_shape("😗 Episode 09 added!\n" + linkprovider.deep_link(SAMPLE_TOKEN))["is_ours"] is not True


def test_the_builder_refuses_to_invent_the_number_it_prints() -> None:
    link = linkprovider.deep_link(SAMPLE_TOKEN)
    for args in (("", 1, 5, link), ("Solo", None, 5, link), ("Solo", 1, None, link), ("Solo", 1, "", link)):
        with pytest.raises(ValueError):
            linkprovider.announcement_caption(*args)
    # `link` must be the provider's deep link: an invite or an unrelated URL would print a link 33k
    # people click and that this app never verified.
    for bad in ("https://t.me/+RM_bWDqzldg2OWFI", "https://example.com/x", ""):
        with pytest.raises(ValueError):
            linkprovider.announcement_caption("Solo", 1, 5, bad)


def test_the_card_caption_refuses_the_style_that_would_print_brackets() -> None:
    invite = "https://t.me/+RM_bWDqzldg2OWFI"
    assert linkprovider.card_caption(invite).count(invite) == 2
    assert linkprovider.card_caption(invite, repeats=1).count(invite) == 1
    with pytest.raises(ValueError, match="plain text"):
        linkprovider.card_caption(invite, style="markdown")
    with pytest.raises(ValueError, match="link style"):
        linkprovider.card_caption(invite, style="html")
    # The announcement can be flattened to plain text from the same box; the card cannot be inflated
    # to markdown from it. Same care, opposite directions, because the two fields are different.
    assert "](" not in linkprovider.announcement_caption("Re Zero", 4, 9, linkprovider.deep_link(SAMPLE_TOKEN), style="text")


def test_the_announcement_title_is_the_same_title_the_episode_post_uses() -> None:
    """``{title_full}`` means one thing in every template, or a series ends up spelled two ways in
    two channels that are supposed to agree. The announcement is a caption box like any other, so it
    takes the same optional alternate title — and drops the separator, not the line, when there is none.
    """
    link = linkprovider.deep_link(SAMPLE_TOKEN)
    with_subtitle = linkprovider.announcement_caption("Re Zero", 3, 4, link, subtitle="Zero kara Hajimeru")
    assert "Re Zero: Zero kara Hajimeru (S3)" in with_subtitle.splitlines()[0]
    assert ": " not in linkprovider.announcement_caption("Re Zero", 3, 4, link).splitlines()[0]
