"""The updates channel and the bot that makes its posts possible: @Link_providerobot.

Three screenshots from the operator on 2026-08-28 showed a fourth flow, one this project had not
named yet. Every series channel gets an announcement in a *separate, big* channel — the one with
the audience — saying which episode was just added, with a link that opens the card holding that
channel's invite. The steps, in the order the operator does them:

1. The destination channel holds a **card post**: a picture (the series art with ``@YCANIME`` over
   it) whose caption says ``Channel link`` and then carries the channel's own invite link.
2. That message is **forwarded to @Link_providerobot**, which replies with one shareable link.
3. The link goes into the **announcement** in the updates channel: series, season, which episode
   was added, and the link.
4. Nobody in the updates channel has to join anything to read it; the link opens the bot, and the
   bot shows the card.

Why the detour: the announcement channel has 33k strangers in it and the destination channel is
private. The invite link therefore travels *inside a picture*, and the picture travels behind a
bot link. Whether that is necessity or habit is one of the open questions below — this module
records what was seen, not a theory about it.

One hazard deserves naming, because it is the reason :func:`parse_reply` checks the link's *host*
and not just its sentence: this bot and @anime_hindifilesbot are clones of the same family, so both
answer with the words ``Here is your link:`` and both hand back a ``BQADAQAD…`` token. Wording
proves a protocol, never an identity — only ``t.me/<bot>`` says which bot minted a link.

What is certain here is copied out of the screenshots verbatim, typo included (``Send A Message
For To Get Your Shareable Link``). What is not certain is listed in :func:`still_unknown`, and the
announcement shape is deliberately **absent** from ``captions.APPROVED_TEMPLATES``: no post is
made from this module until the operator approves the box, exactly like every other caption.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "BOT_USERNAME",
    "BOT_DISPLAY_NAME",
    "COMMAND",
    "REQUEST_TEXT",
    "REPLY_MARKER",
    "SHARE_BUTTON",
    "NOT_FOR_PROBE",
    "deep_link",
    "token_of",
    "is_deep_link",
    "parse_reply",
    "card_caption",
    "announcement_caption",
    "announcement_matches_shape",
    "still_unknown",
    "summary",
    "status_line",
]

#: The username as it appears in the link the bot itself sent (``t.me/Link_providerobot?start=…``),
#: which is the only spelling that is evidence rather than a reading of a chat header.
BOT_USERNAME = "Link_providerobot"
BOT_DISPLAY_NAME = "Link provider"

#: The verb the operator typed. It is the same word the storage bot lists for "store a single
#: message or file" — see ``app/storagebot.py`` — which is a hint about that bot, not proof.
COMMAND = "/genlink"

#: Verbatim, including the article that should not be there. A bot's own wording is data.
REQUEST_TEXT = "Send A Message For To Get Your Shareable Link"
REPLY_MARKER = "Here is your link:"
SHARE_BUTTON = "SHARE URL"

#: Never sent by the probe. ``/genlink`` is not dangerous the way a moderation verb is, but it is
#: not free either: it makes the bot mint a permanent link to something on the operator's account,
#: and it only answers once a message has been forwarded to it — a probe does neither.
NOT_FOR_PROBE = frozenset({"genlink", "share", "link"})

# A bot username is only a bot username when it ends in ``bot``, so the suffix is part of the group
# rather than a literal outside it: the recorded spelling has to survive a round trip. Telegram treats
# the username case-insensitively, so the *match* ignores case and the *capture* keeps it — a link
# written `t.me/LINK_ProviderBOT` is the same bot, and the post should print it as it was given.
_LINK_RE = re.compile(
    r"https?://t\.me/(?P<bot>[A-Za-z0-9_]*bot)\?start=(?P<token>[A-Za-z0-9_\-]+)", re.IGNORECASE
)
_INVITE_RE = re.compile(r"https?://t\.me/\+(?P<code>[A-Za-z0-9_\-]+)")

#: The heading and note markers as they appear in both sampled announcements. The trailing ``”`` is
#: in both of them, so it is a habit and not a slip of one message; it is kept for that reason and
#: it is also the kind of thing to delete with one config row the day the operator says so.
HEADING_MARK = "\U0001f353"  # strawberry, opens every heading line in that channel
NOTE_MARK = "\U0001f617"
NOTE_TAIL = "✨”"
LINK_LABEL = "Click here to start and get episode"
#: The observed post carries the link twice, on two lines, in the caption. Both samples do it.
LINK_LINES = 2


def deep_link(token: str, *, bot: str = BOT_USERNAME) -> str:
    """The shareable URL for one ``start`` token."""
    clean = str(token or "").strip()
    if not clean:
        raise ValueError("an empty start token would make a link that goes nowhere")
    # The username is kept as recorded rather than folded to lowercase: this string is pasted
    # into a public post, and it should read exactly like the one the bot sent.
    return f"https://t.me/{bot.lstrip('@')}?start={clean}"


def token_of(link: str | None) -> str | None:
    """The ``start`` payload of a bot deep link, or None if this is not one.

    The token is what has to be stored, because it is the part that survives us rebuilding a link
    for the *next* post. It cannot rescue a link already published: an announcement in a channel
    carries the username as typed that day, and on this vendor's account bot handles are deleted and
    re-taken regularly — a freed @username can belong to somebody else. Hence the rule in
    ``docs/storage-bot.md`` that the clone's username is permanent once anything points at it.
    """
    match = _LINK_RE.search(str(link or ""))
    return match.group("token") if match else None


def is_deep_link(link: str | None) -> bool:
    return token_of(link) is not None


def parse_reply(text: str | None, *, bot: str = BOT_USERNAME) -> dict[str, Any]:
    """Read one reply from the link bot into a link, or say it is not that yet.

    Deliberately narrow. A reply is only a link when it carries the marker *and* a
    ``t.me/<bot>`` deep link **for the bot that was asked**, because the marker and the token family
    are shared between this bot and the storage bot's clones: a link belonging to some other bot is
    evidence about that bot, and storing it as ours would send people somewhere else. A bot that
    answers "Send A Message…" is recorded as the request, and
    anything else is ``unknown`` — never a link guessed from a stray URL, because a wrong link in
    an announcement is a link 33k people are handed.
    """
    body = " ".join(str(text or "").split())
    out: dict[str, Any] = {
        "kind": "unknown",
        "link": None,
        "token": None,
        "bot": None,
        "chars": len(body),
    }
    if not body:
        return out
    if REQUEST_TEXT.casefold() in body.casefold():
        out["kind"] = "asks_for_a_message"
        return out
    match = _LINK_RE.search(body)
    if REPLY_MARKER.casefold() in body.casefold() and match:
        host = match.group("bot")
        if host.casefold() != str(bot).casefold():
            # Recorded, never stored as ours. The name of the bot that really answered is the fact
            # worth keeping when two siblings answer with the same sentence.
            out["kind"] = "link_from_another_bot"
            out["bot"] = host
            return out
        out["kind"] = "link"
        out["token"] = match.group("token")
        out["bot"] = host
        # The bot's own spelling of its username wins over the default, so a renamed bot does not
        # turn every reply we already read into a link to somewhere else.
        out["link"] = deep_link(match.group("token"), bot=match.group("bot"))
    return out


def card_caption(
    invite_link: str,
    *,
    style: str = "text",
    repeats: int = LINK_LINES,
    words: str = "Channel link",
) -> str:
    """The caption of the card post inside a destination channel (image 2's shape).

    ``words`` is the only part of that line this module claims to know; the pointing-hand emoji that
    follow it are visible in the screenshot but their exact count is listed as unread, so they are
    left to the operator's own post rather than invented here.

    ``style`` exists to refuse one thing. The operator's own words for this caption are "plain text
    with a link", so ``"markdown"`` — wrapping the link in ``[]()``, which is how the *announcement*
    carries it — raises instead of producing a caption whose square brackets would be visible. A
    default that silently produces the wrong format in a private channel is worse than no default.
    """
    if style == "markdown":
        raise ValueError(
            "a card caption is plain text with a link: `[]()` in a text field shows as brackets, not "
            "as a hyperlink — keep style='text' and let the announcement carry the markdown form"
        )
    if style != "text":
        raise ValueError(f"unknown link style {style!r}: 'markdown' or 'text'")
    link = str(invite_link or "").strip()
    if not _INVITE_RE.fullmatch(link):
        raise ValueError("a card caption carries a t.me/+ invite link, nothing else")
    return "\n".join([words, ""] + [link] * max(1, int(repeats)))


def announcement_values(
    series: str,
    season: int | str | None,
    episode: int | str | None,
    link: str,
    *,
    subtitle: str | None = None,
) -> dict[str, str]:
    """The values an announcement needs, checked before anything is rendered.

    Two refusals, both learned from the samples. An empty ``season`` is refused: every heading
    carried ``(S1)``/``(S4)``, so a bare series line reads as a claim that the whole show was added.
    And an unrecognised ``link`` is refused: this is the one place a hallucinated URL reaches
    33k people, so it has to be a real ``?start=`` deep link, not merely a URL.
    """
    name = str(series or "").strip()
    if not name:
        raise ValueError("an announcement names the series it is about")
    if season is None or str(season).strip() == "":
        raise ValueError("an announcement names the season; without it the line claims the series")
    token = token_of(link)
    if token is None:
        raise ValueError("an announcement's link must be a t.me/<bot>?start= deep link, not any URL")
    number = str(episode).strip().zfill(2) if str(episode or "").strip().isdigit() else str(episode or "").strip()
    if not number:
        raise ValueError("an announcement says which episode was added")
    from .captions import title_with_subtitle  # same helper the episode captions use

    return {
        # `{title_full}` means one thing across every template in this project: the series with its
        # alternate title when it has one. The announcement is the exception only when the caller has
        # no subtitle to pass, because `title_with_subtitle` drops the separator rather than printing
        # a dangling colon into a channel of 33k people.
        "title_full": title_with_subtitle(name, subtitle),
        "season": str(season).strip().lstrip("Ss"),
        "episode": number,
        "link": deep_link(token),
    }


_MD_LINK_RE = re.compile(r"\[(?P<label>[^\]]*)\]\((?P<url>https?://[^)\s]+)\)")


def announcement_caption(
    series: str,
    season: int | str | None,
    episode: int | str | None,
    link: str,
    *,
    style: str = "markdown",
    subtitle: str | None = None,
    template: str | None = None,
) -> str:
    """Render the announcement through the approved box, and refuse anything the box cannot fill.

    ``app.captions.render_caption`` is the only way a caption is ever built here, approved box or
    not, so the operator can change this post in one ``app.config`` row — including deleting the
    repeated link line, which is the other thing they might well decide to do.

    A placeholder the values cannot fill raises with the name of it rather than rendering a post
    that says ``{title_full}`` to 33k people: this function is called once per episode, so the
    caller's error handling is one ``try`` around the loop, and a queue that dies on the first bad
    row is a queue that never reaches the good ones. ``build``-style soft failure belongs to the
    caption paths that scan hundreds of messages at once (``app/inplace/caption_for``), not here.

    ``style`` is a rendering of the *same* text, not a second template: ``"text"`` rewrites each
    ``[label](url)`` into a label line over the bare url, which is what a plain-text post in a
    private channel looks like when the client is not asked to parse markdown.
    """
    # Local import: captions owns the approved boxes, and this module is imported by the probe.
    from .captions import APPROVED_TEMPLATES, render_caption

    values = announcement_values(series, season, episode, link, subtitle=subtitle)
    text, missing = render_caption(
        template or APPROVED_TEMPLATES["templates.announcement_post"],
        values,
        key="templates.announcement_post",
    )
    if missing:
        raise ValueError(f"the announcement box wants {', '.join(missing)} and the caller passed none")
    if style == "text":
        text = _MD_LINK_RE.sub(lambda m: f"{m.group('label')}\n{m.group('url')}", text)
    elif style != "markdown":
        raise ValueError(f"unknown link style {style!r}: 'markdown' or 'text'")
    return text


def announcement_matches_shape(text: str) -> dict[str, Any]:
    """What a caption already in the updates channel looks like to us.

    ``reconciliation`` needs this in one direction only: when a post in that channel is *not* this
    shape it is somebody's announcement (a special, a collab, a poll) and the app must not treat it
    as ours or offer to edit it. It is not a matcher that decides a post is stale.
    """
    body = str(text or "")
    heading = next((line for line in body.splitlines() if line.strip().startswith(HEADING_MARK)), None)
    # The tail is part of the recognition, not decoration: the note line ends with the sparkle and
    # the stray quote in both samples, so a message that merely mentions an episode is not ours.
    note = next(
        (line for line in body.splitlines() if NOTE_MARK in line and NOTE_TAIL in line),
        None,
    )
    return {
        "is_ours": bool(heading and note and LINK_LABEL in body),
        "series_line": (heading or "")[len(HEADING_MARK) :].strip() or None,
        "episode": (re.search(r"Episode\s+(\d+)", note or "").group(1) if note and re.search(r"Episode\s+(\d+)", note) else None),
        "links": len(_LINK_RE.findall(body)),
    }


def still_unknown() -> tuple[str, ...]:
    """What the three screenshots do not answer, and what ``storage_upload`` still waits on.

    Kept as data because the doc prints this list and ``tests/test_linkprovider.py`` fails if the
    two drift apart: an honest "we do not know yet" is the difference between a blocked job and a
    guessed one. Four questions from the earlier list were answered on 2026-08-28 (who posts, whether
    the link survives editing the card, one channel for every series, one announcement per episode);
    they live as ``app.config`` rows now, because a thing we know does not belong in a list of
    unknowns and a thing we merely believe does not belong in code that posts to 33k people. A fifth
    was answered on 2026-08-29, by the operator's own word about their clone, and is written into the
    first item below rather than removed, because "the link is permanent" is only useful if the thing
    *behind* the link is permanent too.
    """
    return (
        "whether a link is rate-limited, or stops working when the private invite it shows is revoked "
        "and regenerated. Whether it expires was answered by the operator on 2026-08-29 — it does not, "
        "\"the link works forever\" — which is the only reason a token is worth publishing at all; "
        "what still bites is that the invite inside the card can be regenerated while every old "
        "announcement keeps pointing at the old one",
        "what @Link_providerobot's /start menu holds besides /genlink, and whether it has the same "
        "moderation verbs as the storage bot's menu (broadcast, ban, unban)",
        "the exact emoji run in the card caption after the words 'Channel link' — visible, not counted",
        "whether this session's account can post in the updates channel at all: /probe reads our own "
        "rights from the dialog list (app/rights.py) and writes them, and until that has run, the first "
        "announcement is a guess about a channel this account may only be able to read",
    )


def status_line(channel: Any, per_episode: Any = True) -> str:
    """The one line ``/status`` prints about the updates channel, from the two config rows.

    Written as a function rather than inline in the control bot because both halves of the answer
    have to be true at once for an announcement to be possible: a channel to post in, and an
    approved box to post. Only the first is a setting, so the second is repeated here every time,
    in the same sentence, so the line can never read as "ready to send".
    """
    from .captions import APPROVED_TEMPLATES  # local import: captions owns the approval set

    approved = "templates.announcement_post" in APPROVED_TEMPLATES
    named = str(channel or "").strip().lstrip("@")
    if not named:
        where = "not set"
    elif named.lstrip("-").isdigit():
        # A private channel has no @handle to name it by, so a number is the *expected* spelling
        # here, not a mistake to correct: the operator's own updates channel is private.
        where = f"private channel {named} (no @handle, which is what private means)"
    else:
        where = f"@{named}"
    rhythm = "one announcement per episode" if str(per_episode).casefold() in {"true", "1", "yes"} else "one per batch"
    if not named:
        return (
            f"updates channel: not set — announcements have nowhere to go, so {rhythm} is a plan "
            "with no audience; name it in app.config (updates.channel) and /status will say so here"
        )
    return (
        f"updates channel: {where}, {rhythm}, sent by your own account as plain text "
        + (
            "with a link; the box is approved, and the send path is still unwired, so nothing goes out on "
            "its own"
            if approved
            else "with a link; the announcement text is recorded but NOT an approved caption box, so nothing sends yet"
        )
    )


def summary() -> str:
    """One line, read by ``app.probe.format_report``.

    It belongs in the probe report because the operator is the one who has to answer the open
    questions, and a report that asks without saying what is already known wastes their answers.
    """
    return (
        f"{COMMAND} on @{BOT_USERNAME} takes a forwarded message and answers with a "
        f"t.me/<bot>?start= link; {len(still_unknown())} questions still open"
    )
