"""Episode manifest: display order, and the create-vs-edit decision.

Two rules from the spec live here, and both are about what a *subscriber* sees:

1. **Order comes from the manifest, never from arrival.** A 1080p uploaded on
   Tuesday and a 480p uploaded on Wednesday must appear 480p-first, in the
   sequence the operator configured (``quality.order``), not in the order files
   happened to finish.
2. **A quality that arrives later is an edit, never a new message.** So the
   decision this module returns is ``create`` / ``edit`` / ``noop`` against the
   post that already exists — and ``noop`` is a real outcome, not a failure,
   because re-publishing an unchanged episode is how a channel ends up with
   three copies of episode 9.

Pure functions over plain dicts, so the same code serves a live job and a test
with no database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .keys import quality_rank, variant_identity

__all__ = [
    "PublishAction",
    "PublishDecision",
    "Coverage",
    "ordered_variants",
    "quality_display_list",
    "decide_publish",
    "season_coverage",
    "progress_line",
    "should_post_season_batch",
    "manifest_table",
    "sort_episode_numbers",
]


class PublishAction:
    CREATE = "create"
    EDIT = "edit"
    NOOP = "noop"


@dataclass(frozen=True, slots=True)
class PublishDecision:
    """What to do with the destination post for one episode.

    ``added_qualities`` is what the edit will report as new, which keeps the
    caption honest ("Now also in 1080p") instead of silently rewriting it.
    """

    action: str
    qualities: tuple[str, ...] = ()
    added_qualities: tuple[str, ...] = ()
    reason: str = ""
    blocked: bool = False

    @property
    def should_send(self) -> bool:
        return self.action in (PublishAction.CREATE, PublishAction.EDIT)


@dataclass(frozen=True, slots=True)
class Coverage:
    """Season completeness — the only thing that may trigger a batch post."""

    first: int | None
    last: int | None
    expected: int | None
    present: tuple[int, ...]
    missing: tuple[int, ...] = ()

    @property
    def complete(self) -> bool:
        """True only when the expected count is *declared* and met.

        An unknown end (a series still airing) is deliberately not complete:
        posting "Season Complete" for an airing show is a claim that cannot be
        taken back cleanly, whereas waiting costs nothing.
        """
        if self.expected is None or self.first is None:
            return False
        return self.expected > 0 and len(self.present) >= self.expected

    @property
    def ratio(self) -> str:
        return f"{len(self.present)} of {self.expected}" if self.expected else f"{len(self.present)} so far"


def ordered_variants(
    variants: Iterable[Mapping[str, Any]],
    order: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Stable manifest order: configured quality rank, then label, then variant.

    ``quality_rank`` sorts unknown labels last but deterministically, so a
    surprise "4K" release still appears — in a predictable place rather than
    wherever the database returned it.
    """
    rows = [dict(v) for v in variants]
    rows.sort(
        key=lambda v: (
            quality_rank(str(v.get("quality") or ""), order),
            str(v.get("quality") or "").casefold(),
            str(v.get("release_variant") or "").casefold(),
        )
    )
    return rows


def quality_display_list(
    variants: Iterable[Mapping[str, Any]],
    order: Sequence[str] | None = None,
) -> list[str]:
    """The ``{quality_list}`` of a caption: display order, de-duplicated."""
    seen: dict[str, None] = {}
    for variant in ordered_variants(variants, order):
        label = str(variant.get("quality") or "").strip()
        if label:
            seen.setdefault(label.casefold(), None)
    ordered = sorted(seen, key=lambda label: quality_rank(label, order))
    return [label for label in ordered]


def decide_publish(
    *,
    available: Sequence[Mapping[str, Any]],
    published: Sequence[Mapping[str, Any]] = (),
    post_exists: bool = False,
    thumbnail_gate: str = "clean",
    quality_order: Sequence[str] | None = None,
) -> PublishDecision:
    """Create, edit in place, or leave the post alone.

    ``thumbnail_gate`` is enforced here rather than at send time: nothing with a
    watermarked or unchecked thumbnail reaches a destination channel even if a
    caller got as far as asking. A variant whose thumbnail status is not in
    ``('clean', 'owner_approved')`` is simply not publishable.
    """
    if thumbnail_gate not in ("clean", "owner_approved"):
        return PublishDecision(
            action=PublishAction.NOOP,
            blocked=True,
            reason=f"thumbnail status {thumbnail_gate!r} cannot be published (clean thumbnail is a hard gate)",
        )

    def identity(variant: Mapping[str, Any]) -> tuple[str, str]:
        return variant_identity(str(variant.get("quality") or ""), variant.get("release_variant"))

    publishable = [
        v
        for v in available
        if str(v.get("thumbnail_status") or "unchecked") in ("clean", "owner_approved")
        and str(v.get("status") or "pending") not in ("skipped", "review", "failed")
    ]
    if not publishable:
        gate_blocked = [
            v
            for v in available
            if str(v.get("thumbnail_status") or "unchecked") not in ("clean", "owner_approved")
        ]
        if gate_blocked and len(gate_blocked) == len(list(available)):
            return PublishDecision(
                action=PublishAction.NOOP,
                blocked=not post_exists,
                reason=(
                    "every candidate variant is blocked on its thumbnail: a clean thumbnail is "
                    "a hard gate, so nothing here may be published"
                ),
            )
        return PublishDecision(
            action=PublishAction.NOOP,
            blocked=not post_exists,
            reason="no publishable variant yet (nothing archived with a clean thumbnail)",
        )

    ordered = ordered_variants(publishable, quality_order)
    qualities = tuple(str(v.get("quality") or "").strip() for v in ordered if v.get("quality"))
    already = {identity(v) for v in published}
    added = tuple(
        str(v.get("quality") or "").strip()
        for v in ordered
        if identity(v) not in already and str(v.get("quality") or "").strip()
    )

    if not post_exists:
        return PublishDecision(
            action=PublishAction.CREATE,
            qualities=qualities,
            added_qualities=added,
            reason=f"first publish for this episode ({len(qualities)} quality link(s))",
        )
    if not added:
        return PublishDecision(
            action=PublishAction.NOOP,
            qualities=qualities,
            reason="post already lists every available quality; editing it again would duplicate content",
        )
    return PublishDecision(
        action=PublishAction.EDIT,
        qualities=qualities,
        added_qualities=added,
        reason=f"quality {', '.join(added)} became available after the post went out; edit in place",
    )


def season_coverage(
    episode_numbers: Iterable[int],
    *,
    expected_episodes: int | None = None,
    first: int | None = None,
    last: int | None = None,
) -> Coverage:
    """Which episodes exist, and whether the season is finished.

    ``expected`` prefers an explicit count, then ``last - first + 1``. Without
    either, completeness is unknowable and the caller must not claim it.
    """
    present = sort_episode_numbers(episode_numbers)
    low = first if first is not None else (present[0] if present else None)
    high = last if last is not None else (present[-1] if present else None)
    expected = expected_episodes
    if expected is None and last is not None and low is not None and high is not None:
        # Only an *explicit* last-episode (app.season.last_episode) declares a
        # season's length. Deriving it from the highest number we happen to have
        # would call every airing show complete and send out a "Season
        # Complete" post that is wrong by next Tuesday.
        expected = int(high) - int(low) + 1
    missing: tuple[int, ...] = ()
    if expected and low is not None:
        wanted = set(range(int(low), int(low) + int(expected)))
        missing = tuple(sorted(wanted - set(present)))
    return Coverage(first=low, last=high, expected=expected, present=tuple(present), missing=missing)


def progress_line(coverage: Coverage, *, series: str | None = None, season: int | None = None, style: str = "compact") -> str:
    """Human progress text for /status, a channel description or a report."""
    body = f"{len(coverage.present)} of {coverage.expected} episodes" if coverage.expected else f"{len(coverage.present)} episodes so far"
    if coverage.missing:
        body += f" · {len(coverage.missing)} still missing"
    if style == "plain" or not (series or season):
        return body
    prefix = " ".join(part for part in (_display_title(series) if series else None, f"Season {season}" if season else None) if part)
    return f"{prefix}: {body}"


def should_post_season_batch(coverage: Coverage, *, batch_post_exists: bool = False, allow_incomplete: bool = False) -> bool:
    """The permanent season post goes up once, when the season is complete.

    A re-run must not add a second batch post, so the existing post is checked
    before anything else.
    """
    if batch_post_exists:
        return False
    return coverage.complete or (allow_incomplete and bool(coverage.present))


def manifest_table(
    variants: Sequence[Mapping[str, Any]],
    *,
    order: Sequence[str] | None = None,
    link_key: str = "link",
) -> list[dict[str, Any]]:
    """Rows for a caption or an in-channel manifest, in display order, with the
    rank made explicit so a re-edit cannot shuffle the list.

    ``link_key`` names the field holding the storage link, because a variant
    without one must still appear (as ``pending``) — a quality silently missing
    from the list is how users conclude the channel stopped updating.
    """
    rows: list[dict[str, Any]] = []
    for position, variant in enumerate(ordered_variants(variants, order), start=1):
        rows.append(
            {
                "position": position,
                "quality": str(variant.get("quality") or "unknown"),
                "release_variant": variant.get("release_variant") or "",
                "link": variant.get(link_key),
                "ready": bool(variant.get(link_key)),
                "status": str(variant.get("status") or "pending"),
            }
        )
    return rows


def sort_episode_numbers(numbers: Iterable[int]) -> list[int]:
    """Unique, ascending, numeric — so ``[9, 10, 1]`` never renders as
    ``1, 10, 9`` the way a string sort does."""
    seen: dict[int, None] = {}
    for value in numbers:
        try:
            seen.setdefault(int(value), None)
        except (TypeError, ValueError):
            continue
    return sorted(seen)


_SMALL = frozenset({"no", "of", "the", "and", "a", "an", "de", "x", "on", "in", "at", "for", "to"})


def _display_title(text: str) -> str:
    """``jujutsu kaisen`` -> ``Jujutsu Kaisen`` without breaking ``No. 8``."""
    words = text.split()
    out: list[str] = []
    for index, word in enumerate(words):
        stripped = word.strip("()[]{}\"'")
        if not stripped:
            out.append(word)
            continue
        if stripped.casefold() in _SMALL and index:
            out.append(word.replace(stripped, stripped.casefold()))
        elif re.fullmatch(r"[IVXivx]+", stripped) and len(stripped) > 1:
            out.append(word.replace(stripped, stripped.upper()))
        elif re.fullmatch(r"[A-Z]{2,}(\d+)?", stripped):
            out.append(word)  # an acronym the source already capitalised
        else:
            out.append(word[:1].upper() + word[1:])
    return " ".join(out).strip()


display_title = _display_title  # exported for channel naming
