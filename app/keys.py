"""Deduplication and identity keys.

Why this module exists instead of f-strings at each call site: `dedup_key` is a
unique constraint, and it is the *only* thing standing between a re-scan and a
duplicate upload to the storage bot. Two call sites that spell the same key
slightly differently produce two jobs, and two jobs that both think they own a
file is how you end up with two "Episode 1" posts. Boot reconciliation did
exactly that (one key from ``main``, another from ``worker``).

Every key here is deterministic and idempotent per intent, not per attempt: a
retry must reuse the key, a genuinely new event must not.
"""

from __future__ import annotations

import datetime as dt
import re

__all__ = [
    "normalize_title",
    "canonical_episode_key",
    "variant_identity",
    "quality_rank",
    "discovery_key",
    "archive_key",
    "storage_key",
    "publish_key",
    "sticker_key",
    "reconciliation_key",
    "campaign_key",
]

_WS = re.compile(r"\s+")
_SEPARATORS = re.compile(r"[\s./\\|:_]+")
_KEEP = re.compile(r"[^a-z0-9]+")


def normalize_title(text: str) -> str:
    """`Bleach  S01` -> `bleach s01`: what the source channel's name is compared
    against, so 'Berserk' and 'berserk ' map to the same series."""
    return _WS.sub(" ", (text or "").strip()).casefold()


def _slug(text: str) -> str:
    return _KEEP.sub("", _SEPARATORS.sub("-", (text or "").strip().casefold())).strip("-")


def canonical_episode_key(
    series: str,
    season: int,
    episode: int,
    languages: list[str] | tuple[str, ...] | None = None,
    release_variant: str | None = None,
) -> str:
    """The episode's identity, stored in ``app.episode.canonical_key``.

    Language eligibility is part of the key rather than the episode number
    alone, which is what keeps an English-only upload from being treated as a
    Hindi episode 1 of the same show.
    """
    langs = "+".join(sorted({(l or "").strip().casefold() for l in (languages or []) if l})) or "unknown"
    parts = [
        _slug(series) or "unknown",
        f"s{int(season):02d}",
        f"e{int(episode):02d}",
        langs,
    ]
    variant = _slug(release_variant or "")
    if variant:
        parts.append(variant)
    return "|".join(parts)


def variant_identity(quality: str, release_variant: str | None = None) -> tuple[str, str]:
    """(quality, release_variant) as the database's uniqueness pair sees them:
    case-folded, so '1080P' cannot sneak past the index as a new quality."""
    return (quality or "").strip().casefold(), (release_variant or "").strip().casefold()


def quality_rank(quality: str, order: list[str] | tuple[str, ...] | None = None) -> int:
    """Display rank for a quality label. Unknown labels sort last but stably,
    so an unexpected '4K' release still posts — in a predictable place."""
    sequence = [q.strip().casefold() for q in (order or ("360p", "480p", "720p", "1080p", "2160p"))]
    label = (quality or "").strip().casefold()
    if label in sequence:
        return sequence.index(label) + 1
    digits = re.search(r"(\d{3,5})p?", label)
    if digits:
        return len(sequence) + int(digits.group(1))
    return len(sequence) + 1_000_000


def _bucket(when: dt.datetime | None, fmt: str) -> str:
    return (when or dt.datetime.now(dt.UTC)).strftime(fmt)


def reconciliation_key(when: dt.datetime | None = None) -> str:
    """One reconciliation per hour, however many times the instance restarts.

    Startup lease-reclaim is a direct call in ``main`` and always runs; this job
    is the periodic sweep, so collapsing a restart storm into one job is the
    correct behaviour, not a lost event.
    """
    return f"reconciliation:{_bucket(when, '%Y%m%d%H')}"


def discovery_key(source_channel_id: int, message_id: int, media_idx: int = 0) -> str:
    return f"discover:{int(source_channel_id)}:{int(message_id)}:{int(media_idx)}"


def archive_key(variant_id: int) -> str:
    return f"archive:{int(variant_id)}"


def storage_key(variant_id: int) -> str:
    return f"storage:{int(variant_id)}"


def publish_key(episode_id: int) -> str:
    """Publishing is keyed to the episode, not the quality list, because the
    post is edited in place: a new quality reuses the same job rather than
    creating a second post out of order in the channel."""
    return f"publish:episode:{int(episode_id)}"


def sticker_key(season_id: int) -> str:
    return f"sticker:season:{int(season_id)}"


def campaign_key(destination_id: int, name: str) -> str:
    return f"campaign:{int(destination_id)}:{_slug(name)}"
