"""Thumbnail screening — the hard gate between "archived" and "published".

The spec is unusually firm about this ("a clean thumbnail is a hard publish
gate"), because the thumbnail is the only part of a post a leech channel cannot
strip without the file looking wrong: it is where their watermark lives, and it
is what makes a republished file look like *theirs* in a chat preview.

So the rule set is:

* a file whose image carries **either** primary handle, **both**, or **neither**
  passes — the pair ``@ycanime`` / ``@india_crunchyroll`` is ours and either one
  is fine;
* an image carrying **any other** handle fails and goes to the review queue;
* no image at all is not "clean", it is *unknown*, and in strict mode unknown
  never publishes.

The watermark itself is not removed by re-encoding here: the chosen response is
to prefer a clean copy from another source channel (see
:func:`select_clean_candidate`), because that costs no upload. Regenerating a
thumbnail by full download/re-upload is an explicit "ask the owner first"
action, not something to do by default on a free tier.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .keys import quality_rank, normalize_title
from .normalize import detect_handles

__all__ = [
    "Screen",
    "ThumbnailStatus",
    "Disposition",
    "screen",
    "handles_from",
    "select_clean_candidate",
    "no_clean_action",
    "is_publishable",
]

#: ``app.thumbnail_status``.
class ThumbnailStatus:
    UNCHECKED = "unchecked"
    CLEAN = "clean"
    WATERMARKED = "watermarked"
    AMBIGUOUS = "ambiguous"
    REVIEW_REQUIRED = "review_required"
    OWNER_APPROVED = "owner_approved"
    OWNER_REJECTED = "owner_rejected"


class Disposition:
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


#: Both are primary; a file with either one is fine. Editable via
#: ``app.config`` -> ``branding.primary_handles``.
PRIMARY_HANDLES = ("ycanime", "india_crunchyroll")

#: Statuses that may appear in a destination channel.
PUBLISHABLE: frozenset[str] = frozenset({ThumbnailStatus.CLEAN, ThumbnailStatus.OWNER_APPROVED})


@dataclass(frozen=True, slots=True)
class Screen:
    """One screening result. ``needs_review`` rows land in
    ``app.thumbnail_review`` so an owner decision is recorded, not remembered."""

    status: str
    disposition: str
    reason: str
    foreign_handles: tuple[str, ...] = ()
    primary_handles: tuple[str, ...] = ()
    needs_review: bool = False

    @property
    def publishable(self) -> bool:
        return self.status in PUBLISHABLE


def is_publishable(status: str) -> bool:
    return str(status or "").casefold() in PUBLISHABLE


def handles_from(*texts: str) -> tuple[str, ...]:
    """Every ``@handle``/``t.me/handle`` in the caption, filename and OCR text."""
    return detect_handles(*texts)


def screen(
    *,
    image_present: bool,
    handles: Iterable[str] = (),
    ocr_text: str | None = None,
    primary: Iterable[str] = PRIMARY_HANDLES,
    strict: bool = True,
    evidence: str = "caption_only",
) -> Screen:
    """Classify one thumbnail.

    ``evidence`` says how much was actually looked at. ``"caption_only"`` means
    no image was ever opened (which is the case until the Telegram media layer
    exists), and in strict mode that is *not* a pass: publishing on unviewed
    pixels is exactly what the hard gate forbids, so the candidate goes to the
    review queue instead. ``"image_analysed"`` is what the future OCR/handle
    detection step will pass, and it is what lets a verdict be final.

    ``ocr_text`` is folded in because a burned-in watermark often has no
    ``@handle`` form at all in the caption: text recognition turns "ycinime
    files" into a handle we can compare. When OCR found text but no recognizable
    handle, strict mode treats that as ambiguous rather than clean — an
    unrecognisable mark is exactly what a lazy re-post leaves behind.
    """
    allowed = {h.lstrip("@").casefold() for h in primary if h}
    seen = {h.casefold() for h in handles if h}
    if ocr_text:
        seen |= set(detect_handles(ocr_text))

    if not image_present:
        return Screen(
            status=ThumbnailStatus.AMBIGUOUS,
            disposition=Disposition.PENDING,
            reason="no thumbnail on the source message, so nothing was verified",
            needs_review=True,
        )

    foreign = tuple(sorted(seen - allowed))
    primary_found = tuple(sorted(seen & allowed))
    if foreign:
        return Screen(
            status=ThumbnailStatus.WATERMARKED,
            disposition=Disposition.REJECTED,
            foreign_handles=foreign,
            primary_handles=primary_found,
            reason=(
                "thumbnail carries " + ", ".join("@" + h for h in foreign) + " — not a primary handle; "
                "prefer a clean copy from another source, or ask the owner before regenerating"
            ),
            needs_review=True,
        )
    if evidence != "image_analysed" and strict:
        return Screen(
            status=ThumbnailStatus.REVIEW_REQUIRED,
            disposition=Disposition.PENDING,
            primary_handles=primary_found,
            reason=(
                "no foreign handle in the text, but the image itself was never opened "
                "(evidence=caption_only); strict mode parks it for review instead of passing it"
            ),
            needs_review=True,
        )
    if ocr_text and not primary_found and strict:
        return Screen(
            status=ThumbnailStatus.AMBIGUOUS,
            disposition=Disposition.PENDING,
            reason="image has readable text but no primary handle; strict mode will not publish on a guess",
            needs_review=True,
        )
    return Screen(
        status=ThumbnailStatus.CLEAN,
        disposition=Disposition.ACCEPTED,
        primary_handles=primary_found,
        reason="no foreign handle on the image"
        + (f" (carries {', '.join('@' + h for h in primary_found)})" if primary_found else " (no watermark at all)"),
    )


def select_clean_candidate(
    candidates: Sequence[Mapping[str, Any]],
    *,
    quality_order: Sequence[str] | None = None,
    trusted_channels: Sequence[Any] | None = None,
) -> dict[str, Any] | None:
    """Pick which accepted copy to actually use.

    Priority order, and why:

    1. **trusted channel first** — ``trusted_channels`` is the operator's own
       ranking of source channels (the ones whose uploads look right);
    2. **highest quality** — a clean 1080p beats a clean 480p of the same
       episode, and asking twice for one episode is the failure we are avoiding;
    3. **earliest message** — deterministic, so a re-run picks the same copy
       after a restart instead of flapping between two identical files.

    Rows are returned as plain dicts so the caller can write the chosen id
    straight back to ``app.media_variant.source_candidate_id``.
    """
    ranked = {normalize_title(str(c)): i for i, c in enumerate(trusted_channels or ())}
    eligible = [
        dict(candidate)
        for candidate in candidates
        if str(candidate.get("thumbnail_status") or "") in PUBLISHABLE
        and str(candidate.get("disposition") or "pending") in (Disposition.ACCEPTED, Disposition.SUPERSEDED)
    ]
    if not eligible:
        return None
    eligible.sort(
        key=lambda c: (
            ranked.get(normalize_title(str(c.get("source_channel") or "")), len(ranked) + 1),
            -quality_rank(str(c.get("quality") or "unknown"), quality_order),
            int(c.get("message_id") or 0),
        )
    )
    return eligible[0]


def no_clean_action(policy: str) -> str:
    """Translate ``thumbnail.on_no_clean_candidate`` into one worker behaviour.

    Any unrecognised value becomes ``ask_owner``: silently skipping an episode
    because a setting was spelled wrong is the worse failure.
    """
    mapping = {
        "ask_owner": "enqueue an owner review request and leave the episode incomplete",
        "wait_and_rescan": "re-queue the scan for a later pass; do not publish",
        "manual_select": "hold the job until the owner names a candidate id",
        "skip_quality": "mark only this quality skipped and continue with the rest",
    }
    key = str(policy or "ask_owner").casefold()
    return mapping.get(key, mapping["ask_owner"])
