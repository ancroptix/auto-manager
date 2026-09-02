"""Season boundaries: telling "season 2 has started" from "they re-uploaded ep 3".

The source channel is the only clock this pipeline has, and it is a sloppy clock. The
operator's scenario is the easy case — twelve episodes in a row, then the caption says
``S2`` and the numbering restarts at 1. Two signals agree, and a season boundary is a
fact about the *source*, not a guess about the show.

The hard case is the one that silently destroys a channel. Some channels never write
``S2`` at all: they just go back to ``Episode 1``. If that number is trusted as a
continuation, one of two things happens, and both are bad:

* the file is filed into **season 1**, where an episode 1 already exists, so it becomes a
  duplicate variant of an episode nobody asked to re-post — and the season 1 batch post
  then quietly claims the new season's episodes; or
* if the identity check is loose enough to allow it, the destination ends up with two
  "Episode 01" posts, which is the exact thing this project exists to prevent.

So the rule encoded here: **a season boundary needs either a label or an unambiguous
restart — and an unambiguous restart still asks the owner before anything is posted.**
The one thing we never do is infer a season from the passage of time, from a gap in
episode numbers, or from how many episodes we have seen.

Everything here is a pure function of (what we already filed) and (what the incoming
caption says). That is deliberate: the classification must be testable without a
Telegram account, and the same function answers for ingest as for the reconciliation
sweep over a channel's whole history.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Iterable, Sequence

__all__ = [
    "Boundary",
    "StickerStep",
    "Verdict",
    "classify",
    "highest_seen",
    "populated_seasons",
    "accept_as_inferred",
    "publish_hold",
    "season_of",
    "transition_stickers",
]


class Verdict(str, Enum):
    """What the incoming episode means for the season we are in."""

    FIRST = "first"          # nothing filed for this series yet
    CONTINUE = "continue"    # numbering advanced: same season
    DECLARED = "declared"    # caption labelled a later season: a real boundary
    RESET = "reset"          # numbering restarted with no label: a boundary to confirm
    BACKTRACK = "backtrack"  # a number we already have: re-upload or extra quality
    RETREAT = "retreat"      # a labelled season we already passed: never act


#: The largest season number a caption is believed to state. ``_detect_season`` in
#: :mod:`app.normalize` only reads one or two digits, so a larger value can only arrive
#: from a hand-fed hint or a bug — and letting it through would create a season
#: ``999999`` row whose episodes then look "missing" forever. A ceiling is cheaper to
#: reason about than an index that grows without bound.
MAX_PLAUSIBLE_SEASON = 99

#: Verdicts where posting proceeds without waiting for anything.
QUIET = frozenset({Verdict.FIRST, Verdict.CONTINUE, Verdict.BACKTRACK})


@dataclass(frozen=True, slots=True)
class Boundary:
    """The decision, with the reason and the evidence that produced it.

    ``reason`` is written for the human reading the review queue; ``evidence`` is the
    machine-readable version of the same facts, stored on the season row. Both exist
    because "why did it decide this was season 2?" has to be answerable six months later
    without reading the code.
    """

    verdict: Verdict
    season: int
    previous_season: int | None = None
    confident: bool = True
    ask_owner: bool = False
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def is_boundary(self) -> bool:
        return self.verdict in (Verdict.DECLARED, Verdict.RESET)

    @property
    def review(self) -> str | None:
        """Why to park the candidate for a human, or ``None`` to carry on."""
        return self.reason if self.ask_owner else None


def _pick(item: Any) -> Any:
    """An episode number out of whatever a query returned."""
    if isinstance(item, dict):
        return item.get("episode_number", item.get("episode"))
    if isinstance(item, (tuple, list)) and item:
        return item[0]
    return item


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def highest_seen(episodes: Iterable[Any]) -> int | None:
    """The largest episode number filed for the season we are in.

    Accepts ints, rows with an ``episode_number``/``episode`` key, and ``None``s in any
    position, because this is read straight out of Postgres where a season may contain
    ``{2, 5, 12}`` — the source simply had not delivered the rest yet.
    """
    numbers = [n for n in (_as_int(_pick(item)) for item in episodes) if n is not None]
    return max(numbers) if numbers else None


def populated_seasons(rows: Sequence[Any]) -> set[int]:
    """Season numbers that already contain episodes.

    Feeds the ``RETREAT`` check: a channel announcing ``S1`` again after we filed ``S2``
    is re-uploading, not rewinding, and we must not open a second season 1.
    """
    found: set[int] = set()
    for row in rows:
        number = _as_int(_pick(row))
        if number is not None:
            found.add(number)
    return found


def classify(
    *,
    episode: int | None,
    labelled_season: int | None = None,
    current_season: int = 1,
    highest: int | None = None,
    populated: Sequence[int] = (),
    file_kind: str = "episode",
    default_season: int | None = None,
) -> Boundary:
    """Decide what an incoming episode number means.

    ``current_season`` is the season we have been filing into, ``highest`` the largest
    episode number already filed there, ``populated`` the season numbers of this series
    that contain anything at all, and ``labelled_season`` what the caption said —
    ``None`` when the source wrote no season, which is the whole difficulty.

    The order of the checks is the policy, so it is spelled out:

    1. **Nothing filed yet** → ``FIRST``. A series' first episode is not a boundary: no
       sticker sequence and no closing marker, because there is nothing to close.
    2. **No number to compare** (a movie, a batch, an unparsable caption) →
       ``CONTINUE`` at the current season, not confident. Batch is excluded from
       boundary logic on purpose: one archive covering "episodes 1-12 of season 2" says
       the season loudly and the numbering not at all, so it must be resolved by the
       label alone, never by arithmetic.
    3. **A label naming a later season** → ``DECLARED``. This wins over every
       arithmetic signal, including "the number kept going": channels that number across
       seasons (…, 12, then ``S2`` episode 13) are common, and a stated season beats an
       inferred one.
    4. **A label naming an earlier, already-populated season** → ``RETREAT``. Parked,
       never acted on: re-creating season 1 because a leech re-watermarked an old batch
       is unrecoverable in public.
    5. **No label, number came back to the start** (``episode <= 1`` after we had at
       least two episodes) → ``RESET``. Almost certainly a new season, but *almost* is
       what makes it dangerous, so it asks and holds rather than posting.
    6. **No label, number at or below what we have** → ``BACKTRACK`` in the same season:
       a second quality, a remux, a corrected file. ``app.manifest`` turns that into an
       edit of the existing post, which is the correct outcome.
    7. Otherwise → ``CONTINUE``.
    """
    current = _as_int(current_season) or 1
    # Where a series with *nothing* filed starts. A channel that carries only season 2
    # (renamed leech channels do this constantly) has to be able to say so without that
    # statement opening a season boundary or claiming a length: it is a starting point, and
    # the very first file of the series is the only moment it applies.
    opening = _as_int(default_season) or current
    filled = {n for n in (_as_int(s) for s in populated) if n is not None}
    label = _as_int(labelled_season)
    number = _as_int(episode)
    implausible = label is not None and label > MAX_PLAUSIBLE_SEASON
    if implausible:
        label = None
    evidence: dict[str, Any] = {
        "episode": number,
        "labelled_season": label,
        "current_season": current,
        "highest_in_season": highest,
        "file_kind": file_kind,
        "default_season": _as_int(default_season),
    }

    if highest is None and not filled:
        return Boundary(
            verdict=Verdict.FIRST,
            season=label if label is not None else opening,
            reason=(
                "nothing filed for this series yet"
                if opening == current
                else f"nothing filed for this series yet; starting at the channel's declared season {opening}"
            ),
            evidence=evidence,
        )

    if number is None:
        return Boundary(
            verdict=Verdict.CONTINUE,
            season=label if label is not None else current,
            confident=False,
            reason="no episode number to compare; season taken from the caption label only",
            evidence=evidence,
        )

    if implausible:
        return Boundary(
            verdict=Verdict.CONTINUE,
            season=current,
            confident=False,
            reason=(
                f"caption named season {evidence['labelled_season']}, past the {MAX_PLAUSIBLE_SEASON} this "
                "service believes; ignored rather than filed into a season that cannot exist"
            ),
            evidence=evidence,
        )

    top = _as_int(highest)

    if label is not None and label > current:
        # A stated later season is a boundary whatever the numbers do — but if that
        # season already has episodes, this is a re-upload of it, not a new one.
        if label in filled:
            return Boundary(
                verdict=Verdict.BACKTRACK,
                season=label,
                previous_season=current,
                reason=f"caption says season {label}, which we already have episodes for: filing as another variant",
                evidence=evidence,
            )
        why = (
            f"source caption declares season {label}"
            + ("" if top is None else f" after season {current} reached episode {top}")
        )
        return Boundary(
            verdict=Verdict.DECLARED,
            season=label,
            previous_season=current,
            reason=why,
            evidence=evidence,
        )

    if label is not None and label < current:
        if label in filled:
            return Boundary(
                verdict=Verdict.RETREAT,
                season=label,
                previous_season=current,
                confident=False,
                ask_owner=True,
                reason=(
                    f"caption says season {label}, but we are already filing season {current} "
                    "and that season already has episodes — refused to rewind; needs an owner's word"
                ),
                evidence=evidence,
            )
        # A gap *backwards* to an empty season (the source skipped a season's numbering,
        # or an OVA slot) is odd enough to ask about, but it is not a re-upload.
        return Boundary(
            verdict=Verdict.RETREAT,
            season=label,
            previous_season=current,
            confident=False,
            ask_owner=True,
            reason=(
                f"caption labels season {label} after we filed season {current}; "
                "numbers went backwards, so nothing is posted until you confirm"
            ),
            evidence=evidence,
        )

    # From here there is no *later* season named, so arithmetic decides — including
    # when the caption confirmed the current season, because "season 2, episode 5" after
    # episode 9 is a backfill in season 2 and must say so rather than sound like progress.
    if top is not None and top >= 2 and number <= 1:
        return Boundary(
            verdict=Verdict.RESET,
            season=current + 1,
            previous_season=current,
            confident=False,
            ask_owner=True,
            reason=(
                f"season {current} was at episode {top} and the source restarted at episode {number}; "
                "reads like a new season but the caption never said so — held for your confirmation"
            ),
            evidence={**evidence, "would_be_season": current + 1},
        )

    if top is not None and number <= top:
        return Boundary(
            verdict=Verdict.BACKTRACK,
            season=current,
            reason=(
                f"episode {number} is not past season {current}'s highest ({top}): "
                "treated as another copy of an episode we have, so it edits that post"
            ),
            evidence=evidence,
        )

    if top is not None and number - top > 1:
        # A gap ahead of us is not a boundary and not an error: sources release out of
        # order constantly. Recorded in the reason so a human can see it was noticed.
        return Boundary(
            verdict=Verdict.CONTINUE,
            season=current,
            reason=(
                f"season {current} jumped from episode {top} to {number}; missing episodes are "
                "still missing, and nothing here infers the season's length"
            ),
            evidence={**evidence, "gap": number - top - 1},
        )

    confirmed = "" if label != current else ", exactly as the caption said"
    return Boundary(
        verdict=Verdict.CONTINUE,
        season=current,
        reason=f"episode {number} continues season {current}{confirmed}",
        evidence=evidence,
    )


@dataclass(frozen=True, slots=True)
class StickerStep:
    """One sticker to post, in order, with the season it names."""

    kind: str  # "closing" | "opening"
    season: int
    job: str = "season_sticker"

    def as_payload(self, *, series_id: int, destination_id: int | None = None) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "season": self.season,
            "series_id": series_id,
            "destination_id": destination_id,
        }


def transition_stickers(
    boundary: Boundary,
    *,
    opening_posted: bool = False,
    closing_posted: bool = False,
    previous_has_content: bool = True,
) -> tuple[StickerStep, ...]:
    """The stickers a boundary owes, in the order they must be posted.

    The operator's rule, verbatim: when the source starts a new season, the *end of
    season* sticker goes first, then the new season's sticker, and only then does the
    uploading continue. Three details in here are not decoration:

    * Only a boundary produces stickers, and only a **declared** one acts on its own.
      An unconfirmed ``RESET`` returns nothing, because a closing sticker on a season
      that is not over is a public statement that is false.
    * ``closing`` requires the previous season to actually have content. A season that
      was created and then abandoned gets no farewell.
    * ``opening`` is skipped when that season's sticker already went out, which is what
      makes a resumed run idempotent instead of decorative.
    """
    if not boundary.is_boundary:
        return ()
    if boundary.verdict is Verdict.RESET:
        return ()

    steps: list[StickerStep] = []
    if boundary.previous_season is not None and previous_has_content and not closing_posted:
        steps.append(StickerStep(kind="closing", season=boundary.previous_season))
    if not opening_posted:
        steps.append(StickerStep(kind="opening", season=boundary.season))
    return tuple(steps)


def publish_hold(boundary: Boundary, *, pending_stickers: int = 0) -> str | None:
    """Why episode posts must wait, or ``None`` to publish now.

    The sticker is the season's divider, so it has to be *above* the first episode of the
    new season. If a sticker job is still queued, the episode post waits — it does not
    get dropped, and it does not jump ahead. ``RESET`` holds for a different reason: not
    ordering, but that we do not know what season we would be opening.
    """
    if boundary.ask_owner:
        return boundary.reason or "season boundary needs the owner's confirmation"
    if boundary.is_boundary and pending_stickers:
        return (
            f"{pending_stickers} season sticker(s) must post before season {boundary.season}'s "
            "first episode, so the divider stays above it"
        )
    return None


def accept_as_inferred(boundary: Boundary) -> Boundary:
    """Act on an unlabelled restart, because the operator said to trust this channel.

    ``seasons.confirm_unlabelled_reset`` off means exactly one thing: "when the numbering
    restarts with no label, treat it as the new season". This is where that decision is
    turned into a verdict, rather than each caller re-deciding it, and it keeps the
    provenance honest — the boundary is recorded as ``inferred``, and the reason still
    says the caption never stated a season.

    It is also the only path on which stickers get posted for a restart nobody declared.
    The operator chose that; the log says so.
    """
    if boundary.verdict is not Verdict.RESET:
        return boundary
    return replace(
        boundary,
        verdict=Verdict.DECLARED,
        confident=False,
        ask_owner=False,
        reason=(
            f"season {boundary.season} inferred from a numbering restart "
            f"(season {boundary.previous_season} reached episode {boundary.evidence.get('highest_in_season')}); "
            "the caption never stated a season"
        ),
        evidence={**boundary.evidence, "accepted": "inferred"},
    )


def season_of(
    *,
    episode: int | None,
    labelled_season: int | None = None,
    current_season: int = 1,
    highest: int | None = None,
    populated: Sequence[int] = (),
    file_kind: str = "episode",
) -> int:
    """Which season row this episode belongs in.

    A one-call convenience for ingest, because "the label wins, else the boundary's
    season, else where we already are" is easy to write wrongly and expensive to get
    wrong: the season number is part of every canonical episode key.
    """
    boundary = classify(
        episode=episode,
        labelled_season=labelled_season,
        current_season=current_season,
        highest=highest,
        populated=populated,
        file_kind=file_kind,
    )
    return boundary.season
