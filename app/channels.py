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
from typing import Any, Sequence



from .keys import normalize_title
from .manifest import display_title

__all__ = [
    "ChannelHelp",
    "MAX_ABOUT_CHARS",
    "MAX_TITLE_CHARS",
    "Profile",
    "cover_choice",
    "FORBIDDEN_HELP_RIGHTS",
    "rights_are_safe",
    "fit_title",
    "plan_profile",
    "profile_is_current",
    "render_about",
    "SETUP_STEPS",
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
        name="set_profile",
        what="set the picture and bio that createChannel could not carry, and confirm the title",
        why="an empty channel with a stranger's logo is what a leech looks like; the profile is "
            "finished before the first post so the channel never presents itself half-built",
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


def setup_plan(
    done: object = (),
    *,
    series: str | None = None,
    allowed_rights: Sequence[str] | None = None,
    covers: Sequence[dict[str, object]] = (),
    owner_photo: str | None = None,
    about: object = None,
    profile: Profile | None = None,
) -> list[dict[str, object]]:
    """The whole checklist with progress, and the *arguments* each step needs.

    For ``/status``, for the docs, and for the day the MTProto side is written: the steps
    that change admin rights carry the exact permission dict to apply, so the operator's
    ``bots.channel_help_rights`` row is read in one place and an executor cannot
    accidentally apply a hardcoded set instead of the configured one. Same for the
    profile: ``set_profile`` ships the title, bio and picture decision rather than
    leaving a future caller to recompute it differently from ``/status``.

    Note what this is and is not. It is the *plan* — an ordered, resumable description with
    reasons attached. Executing it needs ``channels.createChannel`` and friends, which
    belong to the unimplemented Telegram write layer, so no step here has ever run against
    a real channel.
    """
    if isinstance(done, dict):
        finished = {name for name, value in done.items() if value}
    elif done is None:
        finished = set()
    else:
        finished = {str(name) for name in done}
    decided = profile or (
        plan_profile(
            series,
            covers=covers,  # type: ignore[arg-type]
            owner_photo=owner_photo,
            about=about,
        )
        if series is not None or covers or owner_photo or about is not None
        else None
    )
    plan: list[dict[str, object]] = []
    for step in SETUP_STEPS:
        entry: dict[str, object] = {
            "name": step.name,
            "what": step.what,
            "why": step.why,
            "done": step.name in finished,
            "reversible": step.reversible,
        }
        if step.name == "create_channel" and decided is not None:
            # createChannel carries the title and the privacy flag; it cannot carry a photo
            entry["title"] = decided.title
            entry["private"] = decided.private
        elif step.name == "set_profile" and decided is not None:
            entry["about"] = decided.about
            entry["photo_file_id"] = (decided.photo or {}).get("file_id")
            entry["photo_source"] = (decided.photo or {}).get("source")
            entry["notes"] = list(decided.notes)
        elif step.name == "add_channel_help":
            entry["rights"] = channel_help_rights(stage="create", allowed=allowed_rights)
            entry["username"] = ChannelHelp.USERNAME
        elif step.name == "promote_channel_help":
            entry["rights"] = channel_help_rights(stage="publish", allowed=allowed_rights)
            entry["username"] = ChannelHelp.USERNAME
        plan.append(entry)
    return plan


def destination_name(series: str | None, *, template: str = "{TITLE} Anime in Hindi") -> str:
    """``bleach`` → ``Bleach Anime in Hindi``.

    The name is generated, not asked about (the spec is explicit that creating a
    destination needs no confirmation) — but it is only ever generated when
    :func:`series_agrees` says the channel name and the file metadata agree,
    Note this is the *naming* rule without Telegram's length cap; the creation path uses
    :func:`fit_title`, which shortens a long title instead of handing the API a name it
    will reject.
    which is the condition attached to that rule.
    """
    title = display_title((series or "").strip()) or "Untitled Series"
    text = template.replace("{TITLE}", title).replace("{title}", title)
    # A destination name is user-visible and permanent-ish: collapse the junk a
    # hand-edited template can contain rather than posting it verbatim.
    return re.sub(r"\s{2,}", " ", text).strip()



#: Telegram's own limits on a channel. A title over 128 characters is not "trimmed
#: later": the create call fails, and the error that comes back from MTProto looks
#: nothing like a length problem, so an operator would spend an evening debugging a
#: fifteen-word fan-translated title.
# Granting these to a publisher bot is how a compromised bot account becomes the owner's
# problem in one message, so no configuration can ask for them.
FORBIDDEN_HELP_RIGHTS = frozenset({"can_add_admins", "can_ban_users"})

MAX_TITLE_CHARS = 128
MAX_ABOUT_CHARS = 255
_TITLE_SUFFIX = "Anime in Hindi"


def fit_title(series: str | None, *, template: str = "{TITLE} " + _TITLE_SUFFIX, limit: int = MAX_TITLE_CHARS) -> str:
    """``{TITLE} Anime in Hindi``, shortened the *right* way when the title is long.

    The suffix is the promise the channel makes — "this is where the Hindi uploads
    live" — and the title is only an identifier, so when the two do not fit, the title
    gives up characters, not the suffix. Truncation cuts at a word boundary and adds no
    ellipsis: Telegram renders a trailing ``…`` in a channel name as a broken string,
    not as a truncation marker.
    """
    title = destination_name(series, template=template)
    if len(title) <= limit:
        return title
    # Rebuild from the parts so the suffix survives exactly.
    head = title[: -len(_TITLE_SUFFIX)].rstrip() if title.endswith(_TITLE_SUFFIX) else title
    room = limit - len(_TITLE_SUFFIX) - 1
    if room >= 4 and head:
        cut = head[:room]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        return f"{cut.rstrip(' -,:.;')} {_TITLE_SUFFIX}"
    return title[:limit].rstrip()


def render_about(
    value: object,
    *,
    title: str | None = None,
    limit: int = MAX_ABOUT_CHARS,
) -> tuple[str, str | None]:
    """The channel bio, from the config row — or nothing.

    ``templates.channel_about`` is seeded as JSON ``null`` on purpose. A bio is the one
    piece of text in this system that describes *the operator's channel* rather than a
    file, and inventing marketing copy for it is not this program's job: an absent value
    leaves the field empty, and the empty field is what the owner notices and fills.

    ``{TITLE}`` is filled when a title is supplied, because the bio template written into
    the requirements document (`Watch or download {TITLE} in Hindi. …`) is the text the
    operator is likeliest to paste into that config row — and a bio that publishes literal
    braces on a channel with thirty thousand members is the kind of defect nobody notices
    until someone screenshots it. Anything else left in braces is reported instead of
    quietly deleted, in the same spirit as the caption renderer.
    """
    if value is None:
        return "", None
    if not isinstance(value, str):
        return "", "bio ignored: templates.channel_about is not a string"
    from app.captions import render_template  # local: captions is a leaf, but keep the cycle impossible

    text, missing = render_template(value, {"title": title or "", "TITLE": title or ""})
    note = None
    if missing:
        note = f"bio has unresolved placeholder(s): {', '.join(sorted(set(missing)))}"
    text = re.sub(r"[ \t]{2,}", " ", text.strip())
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > limit:
        cut = text[:limit]
        if "\n" in cut:
            cut = cut.rsplit("\n", 1)[0]
        shortened = f"bio shortened from {len(text)} to {len(cut.rstrip())} characters"
        return cut.rstrip(), f"{note}; {shortened}" if note else shortened
    return text, note


def cover_choice(
    covers: Sequence[dict[str, Any]] = (),
    *,
    owner_photo: str | None = None,
    photo_already_set: bool = False,
) -> dict[str, Any]:
    """Which picture the destination gets, in priority order.

    1. **a picture the owner chose**, recorded in ``app.destination.photo_file_id`` —
       always wins, because they can see the channel and we cannot. Today that column is
       written from the dashboard; the day file uploads are wired it becomes a bot command,
       and until then nothing here pretends a bot can set a channel picture (it cannot: a
       Bot API token has no power over a channel it does not own);
    2. **the cleanest cover already inside the master archive** — an image-only post
       beats an episode frame, then the highest quality rank, then the earliest
       season/episode so a re-run picks the same file instead of flapping;
    3. **nothing at all**, which leaves Telegram's coloured initials.

    There is no step 3½. We never take the *source* channel's photo — that is the
    leech's own branding, and a destination wearing a stranger's logo is the single most
    convincing sign of a re-upload — and we never fetch an image from a URL or
    re-render one: the free tier has no image tooling and no bandwidth to spend, and the
    operator's standing rule is to choose a clean copy rather than regenerate a dirty one.
    """
    if owner_photo:
        return {"file_id": owner_photo, "source": "owner", "reason": "sent by the operator", "change": not photo_already_set}
    ranked = [
        dict(item)
        for item in covers
        if str(item.get("thumbnail_status") or "") in ("clean", "owner_approved")
    ]
    if not ranked:
        return {
            "file_id": None,
            "source": "none",
            "reason": "no clean cover in the archive; leaving the default initials rather than using a watermarked one",
            "change": False,
        }
    ranked.sort(
        key=lambda item: (
            0 if item.get("kind") == "poster" or item.get("is_cover") else 1,
            -(item.get("quality_rank") or 0),
            item.get("season") or 1,
            item.get("episode") or 1,
            item.get("candidate_id") or 0,
        )
    )
    chosen = ranked[0]
    return {
        "file_id": chosen.get("file_id"),
        "source": "archive_cover",
        "candidate_id": chosen.get("candidate_id"),
        "reason": f"cleanest cover in the archive (candidate {chosen.get('candidate_id')})",
        "change": not photo_already_set or chosen.get("candidate_id") != chosen.get("current_candidate_id"),
    }


@dataclass(frozen=True, slots=True)
class Profile:
    """Everything the destination channel shows about itself, decided before it exists."""

    title: str
    about: str
    photo: dict[str, Any] | None
    private: bool
    notes: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "about": self.about,
            "photo": self.photo,
            "private": self.private,
            "username": None,
            "notes": list(self.notes),
        }


def plan_profile(
    series: str | None,
    *,
    covers: Sequence[dict[str, Any]] = (),
    owner_photo: str | None = None,
    about: object = None,
    title_template: str = "{TITLE} " + _TITLE_SUFFIX,
    photo_already_set: bool = False,
) -> Profile:
    """The naming + picture + bio decision for one destination, in one place.

    Called *before* ``createChannel`` because the three fields are set by that call and
    fixing them afterwards is a visible edit: members who joined in the first hour saw a
    differently-named channel. The username is deliberately always ``None`` — a private
    channel that is given a public ``@link`` becomes discoverable, which the operator's
    rule forbids ("one private destination per series").
    """
    notes: list[str] = []
    title = fit_title(series, template=title_template)
    if len(title) >= MAX_TITLE_CHARS:
        notes.append("title hit Telegram's 128-character limit")
    # A bio talks about the *show*, so {TITLE} in it means the series title and not the
    # channel's name: substituting the latter turns the sentence the spec suggests into
    # "Watch or download Bleach Anime in Hindi in Hindi".
    series_title = display_title((series or "").strip()) or "this series"
    text, warning = render_about(about, title=series_title)
    if warning:
        notes.append(warning)
    photo = cover_choice(covers, owner_photo=owner_photo, photo_already_set=photo_already_set)
    if photo["source"] == "none":
        notes.append(str(photo["reason"]))
    return Profile(title=title, about=text, photo=photo, private=True, notes=tuple(notes))


def profile_is_current(plan: Profile, *, actual_title: str | None = None, actual_about: str | None = None, actual_photo: bool | None = None) -> tuple[bool, tuple[str, ...]]:
    """What the ``set_profile`` step would still change, given what the channel has now.

    A restart in the middle of setup must not rename a channel that is already correct,
    and must not skip the one field that never got written. So this compares field by
    field and returns the differences; an unknown (``None``) means "not read yet", which
    is treated as needing a write rather than as a match.
    """
    diffs: list[str] = []
    if actual_title is None or actual_title != plan.title:
        diffs.append("title")
    if actual_about is None or (actual_about or "") != plan.about:
        diffs.append("about")
    if plan.photo and plan.photo.get("change") and actual_photo is not True:
        diffs.append("photo")
    # A picture the plan did not ask for is not a difference to fix. `editPhoto` with an
    # empty input *clears* a channel icon, and the only reason this plan has no photo is
    # that the archive held no clean cover — so the photo on the channel arrived by the
    # operator's own hand, and the one thing this step must never do is delete that.
    # Nothing here returns a "remove the picture" diff, and that is a rule, not an
    # omission: see docs/seasons-and-channels.md.
    return (not diffs, tuple(diffs))


def channel_help_rights(
    *,
    stage: str = "publish",
    allowed: Sequence[str] | None = None,
) -> dict[str, bool]:
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
    if allowed is not None:
        # ``bots.channel_help_rights`` in app.config. The spec's own §3 lists
        # "invite/add users" among the rights Channel Help asks for, while this
        # implementation withholds it by default — a publisher that can invite is a
        # publisher that can be used to spam the channel. Both positions are defensible and
        # the choice is the operator's, so the tension is settled by a config row rather
        # than by a comment in my code. Anything not named here stays off.
        wanted = {name.strip() for name in allowed if str(name).strip()}
        unknown = wanted - set(base)
        if unknown:
            raise ValueError(
                f"unknown admin permission(s) in bots.channel_help_rights: {sorted(unknown)}"
            )
        refused = sorted(FORBIDDEN_HELP_RIGHTS & wanted)
        if refused:
            # Loud, because silently dropping it would leave the operator believing they
            # had granted a right the code then withheld.
            raise ValueError(
                f"bots.channel_help_rights may not contain {refused}: the publisher must "
                "never be able to promote a stranger or clear the channel"
            )
        for name in base:
            base[name] = name in wanted or name == "can_post_messages"
    return base


def rights_are_safe(rights: dict[str, bool]) -> bool:
    """The two rights that must never be on, whatever the config row says.

    ``can_add_admins`` would let the publisher promote a stranger, and ``can_ban_users``
    would let it clear the channel; either one turns a compromised bot account into the
    owner's problem in one message. So the config row is *narrowing* only — it can add
    post/edit/delete/invite/pin, and it cannot remove the two refusals.
    """
    return not rights.get("can_add_admins") and not rights.get("can_ban_users")


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
