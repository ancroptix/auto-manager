"""In-place publishing: caption the file that is already there, and touch nothing else.

The operator described a second shape of the job. Not "read a stranger's channel and build
a clean one beside it", but: *this* channel already holds the episodes as files, each message
saying nothing but ``episode 7`` — and it is the channel they want published, because they own
it. In that shape there is nothing to copy, nothing to name, and nothing to delete. The file
post **is** the post. The whole job is to put the approved caption on it.

That distinction is the spine of this module, and it is why the rules here are looser in one
place and stricter in three:

* looser on **scope**: the "Hindi audio only" gate guards the door through which files *enter*
  your channel. In-place work opens no such door — the file is already there, posted by you —
  so a file nobody can prove is Hindi still gets its caption. Withholding the label is a
  formatting failure; deleting your content over a formatting failure is not one.
* stricter about **overwriting**: the existing text may be the only thing between that message
  and confusion (a note like ``dub added 12/8, source fixed``, a download mirror). So only a
  caption that is clearly a *label* gets replaced; anything that carries real information is
  left alone and asked about.
* stricter about **buttons**: a user session cannot attach an inline keyboard to a media
  message, and Channel Help can only compose what it is given. In this mode there is no link
  to put in a button anyway — the file is in the channel, members tap it. So a caption here
  must stand on its own, and this module refuses to produce one that assumes a button exists.
* stricter about **deleting**: nothing in this file deletes. There is no reason to. When the
  caption is wrong, the fix is another edit, not a removal.

The cross-channel case the operator asked about — a source and a destination sharing a name,
admin here and member there, both full of bare files — is a *comparison*, and
:class:`SeasonShape` is what a comparison looks like. Two equal sets of twelve means twelve
edits and zero copies. It does **not** mean "twelve episodes, all present": only ``/declare``
says that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

__all__ = [
    "MODE_LINK",
    "MODE_IN_PLACE",
    "Action",
    "Decision",
    "SeasonShape",
    "compare",
    "looks_like_label",
    "caption_is_too_long",
    "MAX_CAPTION_CHARS",
    "caption_for",
    "decision_for",
    "plan",
    "summary",
    "shape_note",
    "mode_allows_missing_audio",
    "pair_roles",
    "Route",
    "route_for",
]

#: ``app.destination.publish_mode``. Two modes, because the operator uses both: a link
#: destination (Channel Help composes text + buttons, files live in the master archive) and an
#: in-place destination (the files are already here; their captions are the publishing).
MODE_LINK = "link_post"
MODE_IN_PLACE = "in_place_caption"

#: Telegram's own limit for a media caption. A long edited template must fail here, in a
#: sentence, and not mid-publish with a generic MTProto error.
MAX_CAPTION_CHARS = 1024

#: A destination whose files are already posted never needs the audio gate to stop it: see the
#: module docstring. The gate still applies to files being *brought in*.
def mode_allows_missing_audio(mode: str | None) -> bool:
    return str(mode or MODE_LINK).strip().casefold() == MODE_IN_PLACE


class Action:
    """What to do with one message. Each name is a different amount of confidence."""

    CAPTION = "caption"  # write the approved caption; nobody is asked first
    SKIP = "skip"  # already exactly right — the restart-safe path
    ASK = "ask"  # something here needs the operator, and nothing is touched meanwhile
    COPY_THEN_CAPTION = "copy_then_caption"  # the file is missing here but present in the source
    IGNORE = "ignore"  # this destination post is not ours to change (extra episode, note, ad)


@dataclass(frozen=True, slots=True)
class Decision:
    """One message, one verdict, with the reason a human would accept."""

    action: str
    reason: str
    episode: int | None = None
    message_id: int | None = None
    #: The text already on the message, kept so an overwrite can be undone by hand. Stored in
    #: ``app.destination_post.caption_previous`` — "we replaced it" is only safe if "it" survives.
    previous_caption: str | None = None
    caption: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def changes_anything(self) -> bool:
        return self.action in (Action.CAPTION, Action.COPY_THEN_CAPTION)

    def to_row(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reason": self.reason,
            "episode": self.episode,
            "message_id": self.message_id,
            "previous_caption": self.previous_caption,
            "caption": self.caption,
        }


# --- what counts as "just a label" -------------------------------------------------------

#: Every shape a files-only uploader uses to mark an episode. Deliberately boring patterns,
#: because the cost of a false "this is only a label" is overwriting someone's real note, and
#: the cost of a false "this is a real note" is one question in the review queue.
_LABEL_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.I)
    for pattern in (
        r"^\s*(?:episode|episod[e]?|ep\.?|e)\s*[-_:#]?\s*\d{1,4}\s*(?:/\s*\d{1,4})?\s*$",
        r"^\s*\d{1,4}\s*(?:/\s*\d{1,4})?\s*$",
        r"^\s*\d{1,4}\s*[-–—]\s*(?:mp4|mkv|files?|video)\s*$",
        r"^\s*(?:part|p)\.?\s*\d{1,3}\s*$",
        r"^\s*(?:episode|ep\.?|e)?\s*s\s*\d{1,2}\s*e\s*\d{1,3}\s*$",
        # A bare range ("12 - 34") is deliberately NOT here: in a files-only channel that is
        # usually one video holding twenty-three episodes, and writing a single-episode caption
        # over it would describe the file wrongly. It asks instead.
    )
)

#: Anything carrying one of these is information, not a label, no matter how short it is. A
#: date or a second sentence does not need its own pattern here: the label patterns are anchored
#: to the whole string, so text with anything beside the number fails to match them anyway.
_MEANINGFUL_MARKS: tuple[re.Pattern[str], ...] = (
    re.compile(r"https?://|t\.me/|@[\w_]{4,}", re.I),
    re.compile(r"(?:fixed|updated|re-?upload|corrected|note|readme|source|mirror|link|remaster)", re.I),
)


def looks_like_label(caption: str | None) -> bool:
    """True when a message's text is only an episode marker, so replacing it loses nothing.

    An empty caption is a label with nothing in it. A caption that mentions a link, a handle,
    a date, or a second sentence is not — and after a wrong guess here, the old text is gone
    from Telegram, recoverable only from the copy we kept. That asymmetry is why the patterns
    above are boring on purpose.
    """
    text = (caption or "").strip()
    if not text:
        return True
    if any(mark.search(text) for mark in _MEANINGFUL_MARKS):
        return False
    return any(pattern.match(text) for pattern in _LABEL_PATTERNS)


def caption_is_too_long(text: str | None) -> bool:
    return len((text or "").strip()) > MAX_CAPTION_CHARS


# --- comparison --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SeasonShape:
    """What two channels hold, expressed as episode numbers.

    ``offset`` is the trap this exists for. Fansub channels renumber constantly — one starts
    at 0, one skips a recap, one labels its specials 13-24 while the other calls them 1-12.
    A naive set difference on those two channels reads as "12 missing here, 12 missing there"
    and would forward the *entire season* back onto itself as duplicates. So when the two sets
    line up perfectly after a constant shift, that shift is reported instead of acted on, and
    the operator decides which numbering is right.
    """

    here: frozenset[int]
    there: frozenset[int]
    missing_here: frozenset[int]
    extra_here: frozenset[int]
    offset: int = 0
    same_size: bool = False

    @property
    def numbering_shifted(self) -> bool:
        """Equal counts, disjoint episodes, explainable by one constant shift."""
        return bool(self.offset) and self.same_size and not (self.here & self.there)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "here": len(self.here),
            "there": len(self.there),
            "missing_here": len(self.missing_here),
            "extra_here": len(self.extra_here),
            "common": len(self.here & self.there),
        }


def _as_numbers(values: Iterable[Any]) -> frozenset[int]:
    out: set[int] = set()
    for value in values:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number >= 0:
            out.add(number)
    return frozenset(out)


def compare(here: Iterable[Any], there: Iterable[Any]) -> SeasonShape:
    """Diff two channels' episode numbers, with the renumbering trap handled.

    ``here`` is the destination (the channel being published), ``there`` the source. The
    caller gets sets back, not messages, because the messages are looked up per decision — a
    400-episode channel should not have 400 dicts alive at once.
    """
    mine = _as_numbers(here)
    theirs = _as_numbers(there)
    missing = theirs - mine
    extra = mine - theirs
    offset = 0
    if mine and theirs and mine != theirs and not (mine & theirs) and len(mine) == len(theirs):
        shift = sorted(theirs)[0] - sorted(mine)[0]
        if shift and {n + shift for n in mine} == theirs:
            offset = shift
    return SeasonShape(
        here=mine,
        there=theirs,
        missing_here=missing,
        extra_here=extra,
        offset=offset,
        same_size=len(mine) == len(theirs),
    )


# --- the caption itself ------------------------------------------------------------------


def caption_for(
    *,
    title: str | None,
    episode: int | str | None,
    season: int | str | None = 1,
    audio_kind: str | None = None,
    languages: Sequence[str] | None = None,
    quality_list: Sequence[str] | () = (),
    declared_episodes: int | str | None = None,
    unknown_label: str | None = None,
    subtitle: str | None = None,
    first_episode: int | str | None = None,
    last_episode: int | str | None = None,
    template: str | None = None,
) -> tuple[str, tuple[str, ...]]:
    """Render the approved episode caption for a message that already carries the file.

    Returns ``(text, missing_placeholders)`` rather than raising, so a caller with 300 files
    can report "3 of these had no series name" instead of dying on the first one. The template
    is ``app.config`` -> ``templates.episode_post``, which is the point of this mode: the box
    the operator approved is the box that appears, with no buttons after it.
    """
    from .captions import render_caption, post_values

    values = post_values(
        title=title,
        subtitle=subtitle,
        season=season,
        episode=episode,
        first_episode=first_episode,
        last_episode=last_episode,
        declared_episodes=declared_episodes,
        audio_kind=audio_kind,
        languages=languages,
        quality_list=list(quality_list or ()),
        unknown_label=unknown_label,
    )
    from .captions import APPROVED_TEMPLATES  # local import: captions imports nothing from here

    text, missing = render_caption(
        template or APPROVED_TEMPLATES["templates.episode_post"], values, key="templates.episode_post"
    )
    return text, missing


def decision_for(
    *,
    message_id: int | None,
    episode: int | None,
    existing_caption: str | None,
    our_caption: str | None = None,
    ours_last_time: str | None = None,
    is_media: bool = True,
    replace_notes: bool = False,
) -> Decision:
    """One message: replace, leave, or ask.

    The order is the safety order, not the cheap order. ``ours_last_time`` is the text we wrote
    on this message the last time we touched it, and it changes the answer in two ways: it makes
    a repeat run a no-op, and it makes a *template change* propagate — the box currently on the
    post is ours, so replacing it with the newer box loses nothing.
    """
    current = (existing_caption or "").strip()
    wanted = (our_caption or "").strip()
    was_ours = bool(ours_last_time) and current == ours_last_time.strip()

    if not is_media:
        return Decision(
            Action.IGNORE, "text message, not a file post — nothing to caption here", message_id=message_id
        )
    if current and current == wanted:
        return Decision(
            Action.SKIP,
            "already carries exactly this caption",
            episode=episode,
            message_id=message_id,
            previous_caption=current or None,
        )
    if not (looks_like_label(current) or was_ours):
        if replace_notes and wanted:
            # ``inplace.overwrite_notes = "replace"``: the operator's own statement that this
            # channel's texts are theirs to lose. The previous caption is still recorded, which
            # is the only reason this flag is allowed to exist.
            return Decision(
                Action.CAPTION,
                "replaced over a note because inplace.overwrite_notes is \"replace\" (the old text is kept)",
                episode=episode,
                message_id=message_id,
                previous_caption=current or None,
                caption=wanted,
            )
        return Decision(
            Action.ASK,
            "the existing caption looks like a note, not an episode label — say the word and I will replace it",
            episode=episode,
            message_id=message_id,
            previous_caption=current or None,
        )
    if not wanted:
        return Decision(
            Action.ASK,
            "this file has nothing to build a caption from (no episode number, or no series name yet)",
            episode=episode,
            message_id=message_id,
            previous_caption=current or None,
        )
    if caption_is_too_long(wanted):
        return Decision(
            Action.ASK,
            f"the caption is over Telegram's {MAX_CAPTION_CHARS}-character limit for a media caption; "
            "shorten the template or the title",
            episode=episode,
            message_id=message_id,
            previous_caption=current or None,
            details={"length": len(wanted)},
        )
    return Decision(
        Action.CAPTION,
        "replacing the label with the approved caption" if not was_ours else "re-capturing with the current template",
        episode=episode,
        message_id=message_id,
        previous_caption=current or None,
        caption=wanted,
    )


def plan(
    rows: Sequence[Mapping[str, Any]],
    *,
    shape: SeasonShape | None = None,
    allow_copy: bool = True,
    replace_notes: bool = False,
    destination_id: int | None = None,
) -> list[Decision]:
    """Turn the destination's own messages into a list of decisions.

    ``rows`` is one entry per candidate message, as read from
    ``app.source_candidate`` joined to the episode rows: ``message_id``, ``episode``,
    ``caption`` (what is on the message now), ``caption_previous`` (what we wrote last time, if
    we ever did), ``is_media``. When ``shape`` is given, episodes present only in the source
    become ``copy_then_caption`` entries — that is the only case in this mode where a file
    moves anywhere, and it moves by server-side copy, never by download.
    """
    decisions: list[Decision] = []
    seen_episodes: set[int] = set()
    from .keys import inplace_key  # same helper the publisher enqueues with, by design
    for row in rows:
        episode = row.get("episode")
        try:
            number = int(episode) if episode is not None else None
        except (TypeError, ValueError):
            number = None
        if number is not None:
            seen_episodes.add(number)
        caption, _missing = _caption_from_row(row)
        decisions.append(
            decision_for(
                message_id=row.get("message_id"),
                episode=number,
                existing_caption=row.get("caption"),
                our_caption=caption,
                ours_last_time=row.get("caption_previous"),
                is_media=bool(row.get("is_media", True)),
                replace_notes=replace_notes,
            )
        )
    if shape is not None and allow_copy and not shape.numbering_shifted:
        for number in sorted(shape.missing_here):
            if number in seen_episodes:
                continue
            decisions.append(
                Decision(
                    Action.COPY_THEN_CAPTION,
                    "present in the source channel, absent here; forward it in (no download), then caption it",
                    episode=number,
                )
            )
    if shape is not None and shape.numbering_shifted:
        decisions.append(
            Decision(
                Action.ASK,
                (
                    f"the two channels hold the same count of episodes shifted by {shape.offset:+d} "
                    "— that reads as renumbering, not as missing files, so nothing is copied until you say "
                    "which numbering this channel should use"
                ),
                details={"offset": shape.offset, **shape.counts},
            )
        )
    if destination_id is not None:
        # A plan that names the job key is a plan the run cannot drift away from: "idempotent"
        # means the publisher and the preview computed the same string, and that is only true if
        # one function produced it.
        stamped: list[Decision] = []
        for decision in decisions:
            if decision.changes_anything and decision.message_id is not None:
                details = dict(decision.details)
                details["dedup_key"] = inplace_key(destination_id, decision.message_id)
                decision = Decision(**{**decision.to_row(), "details": details})
            stamped.append(decision)
        decisions = stamped
    return decisions


def shape_note(shape: SeasonShape | None) -> str:
    """The one line that goes above a plan, in the operator's words rather than ours.

    Episodes only this channel has are reported, never acted on: there is no such thing as
    removing one from this mode. A copy the source would need to give us is counted, because
    that is the one number in this whole operation the operator cannot see for themselves.
    """
    if shape is None:
        return "no source channel to compare with: everything here is captioned from this channel alone"
    counts = shape.counts
    parts = [
        f"{counts['here']} episode here",
        f"{counts['there']} in the source",
    ]
    if counts["missing_here"]:
        parts.append(f"{counts['missing_here']} only in the source (would be forwarded in, not downloaded)")
    if counts["extra_here"]:
        parts.append(f"{counts['extra_here']} only here (kept, and still captioned)")
    if shape.numbering_shifted:
        parts.append(f"the numbering looks shifted by {shape.offset:+d}, so nothing is copied until you confirm")
    return ", ".join(parts)


def _caption_from_row(row: Mapping[str, Any]) -> tuple[str | None, tuple[str, ...]]:
    """Build the caption for one row, or ``(None, ...)`` when the row cannot support one."""
    title = row.get("title") or row.get("series")
    episode = row.get("episode")
    if not title or episode is None:
        return None, ("title",) if not title else ("episode",)
    return caption_for(
        title=str(title),
        episode=episode,
        season=row.get("season") or 1,
        audio_kind=row.get("audio_kind"),
        languages=row.get("languages") or (),
        quality_list=row.get("quality_list") or (),
        declared_episodes=row.get("declared_episodes"),
        unknown_label=row.get("unknown_label"),
        subtitle=row.get("subtitle"),
        first_episode=row.get("observed_first"),
        last_episode=row.get("observed_last"),
    )


@dataclass(frozen=True, slots=True)
class Route:
    """Which publishing shape a channel gets, and whether a channel still has to be built.

    The invariant this exists to protect: ``create_destination`` is false **only** when a
    destination already exists, or when the files we are captioning are already in the channel
    that *is* the destination. "I can caption in place here" has never been a reason to skip
    building a destination that does not exist yet — and if it were allowed to be, the failure
    would be silent: a shelf of source files, no published channel, and nothing in the log
    naming what is missing. The operator said this twice; the second time it came with "tab bhi
    channel banane wala hissa skip mat karne lag jana".
    """

    mode: str
    create_destination: bool
    reason: str
    #: The name the destination would be created with, when it must be created.
    name: str | None = None
    #: False when we have never read our own rights in this channel. The mode is then decided
    #: the safe way rather than the convenient way, and the reason says so.
    rights_verified: bool = True
    #: Whether this account may write in the channel at all, as read from its own rights.
    can_write: bool = False

    def consequence(self) -> str:
        """One sentence: what follows from the finding.

        Kept apart from ``reason`` because the two callers want different lengths — the control bot
        expands it into the whole creation sequence, ``pair_roles`` appends it as it stands, and a
        reply that printed both would say "the destination is created" twice in one breath.
        """
        from .channels import SETUP_STEPS

        if self.create_destination:
            # A name that cannot be chosen yet is said out loud rather than filled with a
            # placeholder: `Untitled Series Anime in Hindi` is a channel name somebody might create.
            who = f"the destination `{self.name}`" if self.name else "a destination channel"
            needs = "" if self.name else ", and its name needs the series named first (/source)"
            return f"{who} is created from scratch, starting with `{SETUP_STEPS[0].name}`{needs}"
        if self.mode == MODE_IN_PLACE:
            return "nothing is created and nothing is fetched: these posts are the destination"
        return "posts go to the destination that already exists for this series"


    @property
    def may_caption(self) -> bool:
        """Whether captions may be written onto this channel's own posts.

        Rights decide it, not "are the files here yet": a channel set up before its first scan is
        perfectly legal to record, and a channel where we are an ordinary member never is — no
        matter how many files are sitting in it, we cannot edit a message we cannot post in.
        """
        return self.can_write


def route_for(
    *,
    we_are_admin: bool | None,
    files_already_there: bool,
    destination_exists: bool,
    series: str | None = None,
) -> Route:
    """Decide the publishing shape for one joined channel.

    Read the two facts, in this order:

    * **rights** — admin means we could write here; member means we could not, whatever else is
      true. This is physics, not preference: a user account without posting rights cannot edit a
      message, so "caption it where it already is" is not on the table for that channel.
    * **whether the destination exists** — a channel named ``{TITLE} Anime in Hindi`` for this
      series, already created by us or already added by you as an admin.

    A member-only channel with no destination is therefore *not* a dead end and *not* a question
    about rights. It is the ordinary case: the channel you sent is a source, and the destination
    is built, starting at ``create_channel``.
    """
    from .channels import destination_name

    # No series means no name, and inventing one here is exactly what the naming rule exists to
    # prevent: a destination channel's name is public and semi-permanent.
    name = destination_name(series) if series else None
    if we_are_admin and files_already_there:
        return Route(
            MODE_IN_PLACE,
            False,
            "these posts are already in a channel we can write in, so the file message is the "
            "post: the caption goes on it, nothing is fetched and no channel is created. a row in "
            "app.destination is still recorded — a season needs an owner even when nobody is copied",
            name=name,
            can_write=True,
        )
    if we_are_admin is False:
        return Route(
            MODE_LINK,
            not destination_exists,
            "we are an ordinary member here, so nothing can be written onto these messages — that "
            "is not a restriction we can talk our way past, it is what the rights say. this "
            "channel is a source, and nothing in it is edited",
            name=name if not destination_exists else None,
        )
    if we_are_admin is None:
        return Route(
            MODE_LINK,
            not destination_exists,
            "our rights in this channel have never been read, and guessing them is how a season "
            "ends up captioned in a channel we cannot post in — or, worse, how a destination gets "
            "skipped because someone assumed we could. treated as a source until a session that "
            "can read them has run",
            name=name if not destination_exists else None,
            rights_verified=False,
        )
    return Route(
        MODE_LINK,
        not destination_exists,
        "we can post here, but these messages are not the files, so this is a destination to write "
        "into rather than a shelf to caption",
        name=name if not destination_exists else None,
        can_write=True,
    )


def pair_roles(channels: Sequence[Mapping[str, Any]], *, destination_exists: bool = False) -> dict[str, Any]:
    """Who is the destination when two joined channels share a name.

    The rule the operator gave: the channel we are **admin** in is the destination, because
    that is the only one we are allowed to edit; a channel where we are an ordinary member is
    a source, and nothing may be written there. That is not a preference about trust — a user
    account with no posting rights physically cannot caption those messages, so guessing the
    other way round would produce a job that fails on every file.

    Two answers, one of them a correction the operator made:

    * *two* channels of one name that we both admin is a genuine ambiguity — that is a question,
      because picking one silently puts posts in the wrong place.
    * *zero* such channels is **not** a question about rights. It used to answer "add the session
      as admin here", which reads as a request and is actually a dead end: when the destination
      channel by that name does not exist, the answer is to create it. A channel we merely joined
      is a source, and the creation steps run. ``create_destination`` says so, and nothing is
      skipped because in-place mode exists.

    Returns ``{"destination": [...], "source": [...], "ask": reason|None,
    "create_destination": bool, "note": str|None}``.
    """
    rows = [dict(row) for row in channels]
    admin = [row for row in rows if bool(row.get("we_are_admin"))]
    member = [row for row in rows if not bool(row.get("we_are_admin"))]
    ask = None
    if len(admin) > 1:
        ask = (
            f"{len(admin)} of these channels are ones we admin, so the rights rule cannot pick "
            "one on its own; tell me which is the destination and I will remember it"
        )
    if admin and not ask:
        return {
            "destination": admin,
            "source": member,
            "ask": None,
            "create_destination": False,
            "note": None,
        }
    series = next((row.get("declared_series") or row.get("title") for row in rows), None)
    route = route_for(
        we_are_admin=False if member else None,
        files_already_there=False,
        destination_exists=destination_exists,
        series=series,
    )
    return {
        "destination": [],
        "source": rows,
        "ask": ask,
        "create_destination": route.create_destination,
        "note": None if ask else f"{route.reason} {route.consequence()}",
    }


def summary(decisions: Iterable[Decision]) -> str:
    """One line per action, for /status and the preview a job leaves in its result."""
    counts: dict[str, int] = {}
    for decision in decisions:
        counts[decision.action] = counts.get(decision.action, 0) + 1
    if not counts:
        return "nothing to do"
    order = (Action.CAPTION, Action.COPY_THEN_CAPTION, Action.ASK, Action.SKIP, Action.IGNORE)
    return ", ".join(f"{counts[action]} {action}" for action in order if action in counts)
