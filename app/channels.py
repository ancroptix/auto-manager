"""Destination channels: naming, the admin dance, and the setup question.

Creating a channel sounds trivial and is not. This is the only place in the
pipeline where the service acts on the operator's *account* in a way that is
visible to strangers, and the spec pins down the sequence exactly:

    create private → add Channel Help with posting rights only → send a
    one-use invite to the owner → promote Channel Help fully → revoke the
    invite → season sticker → posts

Each step is a named checkpoint in :data:`SETUP_STEPS`, because a Render
instance can be killed between any two of them. Recording which steps finished
is what makes "resume" mean resume rather than "add a second admin and send the
owner another invite".

Two safety rules are encoded here rather than left to the caller:
Channel Help is the *only* account that may ever be promoted, and the invite is
revoked as part of the same plan that created it, never as a follow-up someone
forgets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any



from .keys import normalize_title
from .manifest import display_title

__all__ = [
    "ChannelHelp",
    "SetupStep",
    "SETUP_STEPS",
    "next_setup_step",
    "setup_plan",
    "destination_name",
    "channel_help_rights",
    "may_promote",
    "series_agrees",
    "sticker_is_due",
    "sticker_status",
    "StickerDecision",
    "parse_setup_reply",
    "reply_is_join_request",
]


class ChannelHelp:
    """The publisher. It posts to destinations and touches nothing else."""

    USERNAME = "chelpbot"
    PACK_URL = "https://t.me/addstickers/OCtbqTQ_by_sticbot"


@dataclass(frozen=True, slots=True)
class SetupStep:
    name: str
    what: str
    why: str
    reversible: bool = True


#: Ordered. ``app.destination.setup_state`` (jsonb) records which of these are
#: done, keyed by ``name``.
SETUP_STEPS: tuple[SetupStep, ...] = (
    SetupStep(
        name="create_channel",
        what="create the private destination channel named from the template",
        why="private first: a channel must never be discoverable before it has content",
    ),
    SetupStep(
        name="add_channel_help",
        what="add @chelpbot with post/edit/delete rights only",
        why="it needs to publish, and nothing more; invite rights come later",
        reversible=False,
    ),
    SetupStep(
        name="invite_owner",
        what="generate a one-use invite and send it to MAIN_ADMIN_USER_ID",
        why="the owner must own the channel; a spare account cannot grant itself ownership",
    ),
    SetupStep(
        name="promote_channel_help",
        what="promote @chelpbot with the rights it needs to edit posts later",
        why="quality edits happen after the fact, so edit rights are needed from the start",
        reversible=False,
    ),
    SetupStep(
        name="revoke_invite",
        what="revoke the one-use invite link",
        why="an open invite on a private channel is the mistake that cannot be un-ring twice",
    ),
    SetupStep(
        name="season_sticker",
        what="post the season sticker for the first season",
        why="the sticker opens the season, so it must precede episode 1 of that season",
    ),
    SetupStep(
        name="ready",
        what="mark the destination ready for publishing",
        why="publish jobs refuse to run against a destination that is not ready",
    ),
)

_STEP_NAMES = tuple(step.name for step in SETUP_STEPS)


def _int_set(values: object) -> frozenset[int]:
    if values is None:
        return frozenset()
    try:
        return frozenset(int(value) for value in values)  # type: ignore[union-attr]
    except (TypeError, ValueError):
        return frozenset()


def next_setup_step(done: object) -> SetupStep | None:
    """The first unfinished step, or ``None`` when the channel is ready.

    Accepts a list, set, dict (``setup_state`` jsonb style) or None, because the
    caller may hand it a half-written payload after a restart.
    """
    if isinstance(done, dict):
        finished = {name for name, value in done.items() if value}
    elif done is None:
        finished = set()
    else:
        finished = {str(name) for name in done}
    for step in SETUP_STEPS:
        if step.name not in finished:
            return step
    return None


def setup_plan(done: object = ()) -> list[dict[str, object]]:
    """The whole checklist with progress, for ``/status`` and the docs."""
    if isinstance(done, dict):
        finished = {name for name, value in done.items() if value}
    elif done is None:
        finished = set()
    else:
        finished = {str(name) for name in done}
    return [
        {
            "name": step.name,
            "what": step.what,
            "why": step.why,
            "done": step.name in finished,
            "reversible": step.reversible,
        }
        for step in SETUP_STEPS
    ]


def destination_name(series: str | None, *, template: str = "{TITLE} Anime in Hindi") -> str:
    """``bleach`` → ``Bleach Anime in Hindi``.

    The name is generated, not asked about (the spec is explicit that creating a
    destination needs no confirmation) — but it is only ever generated when
    :func:`series_agrees` says the channel name and the file metadata agree,
    which is the condition attached to that rule.
    """
    title = display_title((series or "").strip()) or "Untitled Series"
    text = template.replace("{TITLE}", title).replace("{title}", title)
    # A destination name is user-visible and permanent-ish: collapse the junk a
    # hand-edited template can contain rather than posting it verbatim.
    return re.sub(r"\s{2,}", " ", text).strip()


def channel_help_rights(*, stage: str = "publish") -> dict[str, bool]:
    """The exact permission set, per stage of the setup sequence.

    ``create`` (added as admin before the channel has content) and ``publish``
    (after promotion) are different on purpose: nothing in this pipeline needs
    Channel Help to be able to add members, and a publisher that can invite is a
    publisher that can be abused to spam the channel.
    """
    base = {
        "can_post_messages": True,
        "can_edit_messages": True,
        "can_delete_messages": True,
        "can_manage_topics": False,
        "can_invite_users": False,
        "can_pin_messages": False,
        "can_change_info": False,
        "can_add_admins": False,
        "can_ban_users": False,
    }
    if stage == "publish":
        # Pinning is what keeps a season batch post at the top.
        base["can_pin_messages"] = True
    return base


def may_promote(candidate_username: str | None, *, allow: tuple[str, ...] = (ChannelHelp.USERNAME,)) -> bool:
    """The gate that makes "promote @chelpbot with all permissions, never
    promote strangers" a checked fact rather than a comment.

    Anything unnamed — including an admin candidate that arrived through a join
    request or a forwarded contact — is refused.
    """
    if not candidate_username:
        return False
    return normalize_title(candidate_username.lstrip("@")) in {normalize_title(a.lstrip("@")) for a in allow}


def series_agrees(channel_series: str | None, file_series: str | None) -> bool:
    """Do the channel's configured series and the file's own title agree?

    Substring agreement counts, because channels are titled loosely
    ("Bleach TYBW") while files carry the long form. This decides
    auto-creation: on disagreement the pipeline must ask, not invent a channel
    name nobody wanted.
    """
    if not channel_series or not file_series:
        return False
    a, b = normalize_title(channel_series), normalize_title(file_series)
    if a == b or a in b or b in a:
        return True
    # "Bleach TYBW" vs "Bleach Thousand Year Blood War": same words, abbreviated.
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    long_words = long.split()
    if not short:
        return False
    for word in short.split():
        if word in long_words:
            continue
        # Abbreviation form: "tybw" is the initials of "thousand year blood war".
        # Channels really are named like that, and refusing here would mean
        # asking the owner to confirm a series we already know.
        if not _is_initials_of_run(word, long_words):
            return False
    return True


def _is_initials_of_run(token: str, words: list[str]) -> bool:
    if len(token) < 2:
        return False
    for start in range(len(words)):
        initials = ""
        for end in range(start, min(start + 8, len(words))):
            initials += words[end][:1]
            if initials == token:
                return True
    return False


@dataclass(frozen=True, slots=True)
class StickerDecision:
    due: bool
    reason: str


def sticker_status(
    *,
    sticker_posted: bool = False,
    season_episodes: object = (),
    published_episodes: object = (),
) -> StickerDecision:
    """The season sticker opens a season, so it is due before that season's first
    episode post — never per episode, and never retroactively.

    A season that already has posts gets ``due=False`` with a reason instead of
    a late sticker: dropping one on top of live content buries it, and a
    half-applied "fix" is worse than leaving the season as it is and saying why.
    """
    season = _int_set(season_episodes)
    published = _int_set(published_episodes)
    if sticker_posted:
        return StickerDecision(False, "season sticker already posted for this season")
    if not season:
        return StickerDecision(False, "season has no episodes yet")
    if season & published:
        return StickerDecision(
            False,
            "this season already has posts, so a sticker would land after them; "
            "it goes at the start of the next season instead",
        )
    return StickerDecision(True, "no sticker and no posts for this season yet — sticker goes first")


def sticker_is_due(**kwargs: Any) -> bool:
    return sticker_status(**kwargs).due


def parse_setup_reply(text: str | None) -> str:
    """Read a reply to the one-time setup question sent to new joiners.

    Returns ``join`` / ``stop`` / ``other``. This only classifies intent: a
    ``join`` result means "the owner should add this person", never that the
    service may add them — the same separation that keeps join-request replies
    from ever approving anyone.
    """
    if not text:
        return "other"
    if reply_is_join_request(text):
        # A join request says "wants to join your channel", which contains the
        # word "join". Reading that as a setup answer would mark a stranger as
        # having opted in from the very text that was not addressed to them.
        return "other"
    words = re.findall(r"[a-z0-9\u0900-\u097F]+", text.casefold())
    if not words:
        return "other"
    stop = {"stop", "no", "nope", "unsubscribe", "leave", "remove", "block", "delete", "nahi", "nahin", "na", "मत", "नहीं"}
    join = {"join", "yes", "yeah", "yep", "ok", "okay", "haan", "han", "ji", "jihaan", "add", "please", "send", "ha", "चालू", "हाँ"}
    for word in words:
        if word in stop:
            return "stop"
        if word in join:
            return "join"
    return "other"


def reply_is_join_request(text: str | None) -> bool:
    """Recognise the join-request *form* so its text is never mistaken for a
    setup answer.

    Telegram puts fixed phrases in those messages; matching them keeps a user
    replying "I want to join" to a request from being read as a setup opt-in.
    """
    if not text:
        return False
    flat = " ".join(text.casefold().split())
    markers = (
        "wants to join",
        "request to join",
        "join your channel",
        "approval to join",
        "wants to join the channel",
    )
    return any(marker in flat for marker in markers)
