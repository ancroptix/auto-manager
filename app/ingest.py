"""Ingest: turn one source message into candidates, episodes and variants.

This is the half of ``ingest_media`` that does not depend on Telegram — give it
message metadata (whatever a scanner or a manual re-run produces) and it writes
the rows the rest of the ladder reads. Splitting it this way is what makes the
pipeline testable now: the only thing left to wire up is the event loop that
supplies these fields, and the database behaviour below is already verified.

Three properties matter more than the rest:

* **Idempotent.** ``app.source_candidate (source_channel_id, message_id,
  media_idx)`` is unique, so re-scanning a channel's history after a restart
  cannot create a second job for the same file. A duplicate message is not an
  error, it is a no-op that says so.
* **Nothing invented.** A batch or range archive records the episodes it names
  but never fabricates one variant per episode — that would make the archive
  step copy one file twelve times. Batch handling is a live-protocol decision
  (split into per-episode links, or one season link), so the row is parked with
  that reason instead of guessed here.
* **The dedup key is the same one everywhere.** Keys come from :mod:`app.keys`,
  so the episode an ingest creates and the post a publisher edits share one
  identity.
"""

from __future__ import annotations

from typing import Any, Mapping

from . import normalize
from .keys import (
    discovery_key,
    normalize_title,
    screening_key,
)
from .stages import JobKind, JobStage

__all__ = ["record_message", "SOURCE_MODE_IGNORED"]

SOURCE_MODE_IGNORED = "ignore"


async def record_message(
    db: Any,
    *,
    source_channel_id: int,
    message_id: int,
    media_idx: int = 0,
    media_type: str | None = None,
    file_name: str | None = None,
    raw_caption: str | None = None,
    file_size_bytes: int | None = None,
    fingerprint: str | None = None,
    quality_order: list[str] | tuple[str, ...] | None = None,
    require_hindi_audio: bool | None = None,
    include_subbed_only: bool | None = None,
) -> dict[str, Any]:
    """Read one media item from one source message and persist what it implies.

    Returns a small report rather than raising, because the caller is a queue
    loop: "nothing to do, and here is why" has to be visible in the job result.
    """
    channel = await db.fetchrow(
        """
        select id, series_id, username, title, mode, priority,
               require_hindi_audio, include_subbed
          from app.source_channel
         where id = $1
        """,
        source_channel_id,
    )
    if channel is None:
        return {"skipped": f"source channel {source_channel_id} is not configured"}
    if str(channel["mode"]) == SOURCE_MODE_IGNORED:
        return {"skipped": "channel mode is 'ignore'", "source_channel_id": source_channel_id}

    # The channel's own flags beat the global defaults: which releases count as
    # in scope is decided per source, since one channel is subs-only and another
    # is dual-audio.
    parsed = normalize.parse_episode(
        file_name=file_name,
        raw_caption=raw_caption,
        source_series=channel["title"] or _strip_at(channel["username"]),
        quality_order=quality_order,
        require_hindi_audio=channel["require_hindi_audio"] if require_hindi_audio is None else require_hindi_audio,
        include_subbed_only=bool(channel["include_subbed"])
        if include_subbed_only is None
        else include_subbed_only,
    )

    candidate_id = await db.fetchval(
        """
        insert into app.source_candidate (
            source_channel_id, message_id, media_idx, media_type, file_name, raw_caption,
            parsed, season_number, episode_number, language_tag, quality, quality_rank,
            file_size_bytes, fingerprint, thumbnail_status, disposition, reason
        ) values (
            $1, $2, $3, $4, $5, $6,
            $7::jsonb, $8, $9, $10, $11, $12, $13, $14,
            'unchecked', $15::app.candidate_disposition, $16
        )
        on conflict (source_channel_id, message_id, media_idx) do nothing
        returning id
        """,
        source_channel_id,
        message_id,
        media_idx,
        media_type,
        file_name,
        raw_caption,
        parsed.to_payload(),
        parsed.season,
        parsed.episode,
        _language_tag(parsed),
        parsed.quality,
        parsed.quality_rank_value,
        file_size_bytes,
        fingerprint,
        parsed.disposition,
        parsed.reason[:400],
    )
    if candidate_id is None:
        existing = await db.fetchrow(
            """
            select id, disposition from app.source_candidate
             where source_channel_id = $1 and message_id = $2 and media_idx = $3
            """,
            source_channel_id,
            message_id,
            media_idx,
        )
        return {
            "skipped": "already ingested (this channel, message and media index)",
            "candidate_id": existing["id"] if existing else None,
            "disposition": existing["disposition"] if existing else None,
        }

    report: dict[str, Any] = {
        "candidate_id": candidate_id,
        "disposition": parsed.disposition,
        "reason": parsed.reason,
        "file_kind": parsed.file_kind,
        "flags": list(parsed.flags),
        "episodes": [],
        "variants": [],
    }
    await db.execute(
        "insert into app.processed_message (source_channel_id, message_id) values ($1, $2) on conflict do nothing",
        source_channel_id,
        message_id,
    )

    if not parsed.accepted or parsed.series is None:
        # Rejected/parked candidates stop here, but the row stays: the review
        # queue and /status are how an owner finds out a source has odd naming.
        report["needs_review"] = parsed.needs_review
        return report

    series_id = await _resolve_series(db, channel["series_id"], parsed)
    season_id = await _resolve_season(db, series_id, parsed)
    report["series_id"], report["season_id"] = series_id, season_id

    if parsed.file_kind == "batch":
        # Episodes are recorded (coverage is real information), but no variant is
        # invented from one archive — see the module docstring.
        for number in parsed.episode_numbers():
            episode_id = await _resolve_episode(db, season_id, parsed, number)
            report["episodes"].append(episode_id)
        report["needs_batch_handling"] = True
        report["batch_reason"] = (
            "one archive covering episodes "
            + ", ".join(str(n) for n in parsed.episode_numbers()[:6])
            + ("" if len(parsed.episode_numbers()) <= 6 else f" … ({len(parsed.episode_numbers())} total)")
            + "; whether it becomes one season link or per-episode links is a storage-bot decision"
        )
        await db.execute(
            "update app.source_candidate set reason = coalesce(reason, '') || ' | batch: needs a season-link decision' where id = $1",
            candidate_id,
        )
        return report

    if fingerprint and await db.fetchval("select app.is_seen_fingerprint($1)", fingerprint):
        await db.execute(
            "update app.source_candidate set disposition = 'superseded', reason = 'identical media already ingested from another source (fingerprint match)' where id = $1",
            candidate_id,
        )
        report["disposition"] = "superseded"
        report["reason"] = "duplicate media by fingerprint, not by filename"
        return report

    for number in parsed.episode_numbers():
        episode_id = await _resolve_episode(db, season_id, parsed, number)
        report["episodes"].append(episode_id)
        variant_id = await _resolve_variant(
            db,
            episode_id=episode_id,
            candidate_id=candidate_id,
            parsed=parsed,
            file_size_bytes=file_size_bytes,
            fingerprint=fingerprint,
        )
        if variant_id is not None:
            report["variants"].append(variant_id)

    await db.enqueue(
        JobKind.THUMBNAIL_SCREEN.value,
        screening_key(int(candidate_id)),
        stage=JobStage.DISCOVERED,
        payload={"candidate_id": int(candidate_id), "message_id": message_id, "file_name": file_name},
        candidate_id=int(candidate_id),
        episode_id=report["episodes"][0] if report["episodes"] else None,
        priority=int(channel["priority"]),
    )
    report["queued"] = JobKind.THUMBNAIL_SCREEN.value
    return report


# ---------------------------------------------------------------------------
# row helpers
# ---------------------------------------------------------------------------


async def _resolve_series(db: Any, channel_series_id: int | None, parsed: normalize.ParsedEpisode) -> int:
    if channel_series_id:
        return int(channel_series_id)
    title = parsed.series or "Untitled"
    return int(
        await db.fetchval(
            """
            insert into app.series (title, normalized_title)
            values ($1, $2)
            on conflict (normalized_title) do update
               set title = app.series.title, updated_at = now()
            returning id
            """,
            title,
            normalize_title(title),
        )
    )


async def _resolve_season(db: Any, series_id: int, parsed: normalize.ParsedEpisode) -> int:
    season_number = parsed.season if parsed.season is not None else 1
    numbers = parsed.episode_numbers() or (parsed.episode,)
    first = min((n for n in numbers if n is not None), default=None)
    last = max((n for n in numbers if n is not None), default=None)
    return int(
        await db.fetchval(
            """
            insert into app.season (series_id, season_number, first_episode, last_episode)
            values ($1, $2, $3, $4)
            on conflict (series_id, season_number) do update
               set first_episode = least(coalesce(app.season.first_episode, excluded.first_episode),
                                         coalesce(excluded.first_episode, app.season.first_episode)),
                   last_episode  = greatest(coalesce(app.season.last_episode, 0),
                                            coalesce(excluded.last_episode, 0)),
                   updated_at = now()
            returning id
            """,
            series_id,
            season_number,
            first,
            last,
        )
    )


async def _resolve_episode(db: Any, season_id: int, parsed: normalize.ParsedEpisode, number: int) -> int:
    """One row per (season, episode). The canonical key belongs to the episode's
    *identity*, not to each release: a second language track on the same episode
    widens ``languages`` rather than creating a parallel episode, which is what
    keeps "the Hindi ep 1" and "the English-sub ep 1" from becoming two posts.
    """
    return int(
        await db.fetchval(
            """
            insert into app.episode (season_id, episode_number, canonical_key, title_hint, languages, audio_kind)
            values ($1, $2, $3, $4, $5::text[], $6)
            on conflict (season_id, episode_number) do update
               set languages = (
                       select array_agg(distinct l order by l)
                         from unnest(array_cat(app.episode.languages, excluded.languages)) as l
                     ),
                   audio_kind = coalesce(app.episode.audio_kind, excluded.audio_kind),
                   updated_at = now()
            returning id
            """,
            season_id,
            number,
            parsed.canonical_key(number),
            parsed.series,
            list(parsed.languages),
            parsed.audio_kind,
        )
    )


async def _resolve_variant(
    db: Any,
    *,
    episode_id: int,
    candidate_id: int,
    parsed: normalize.ParsedEpisode,
    file_size_bytes: int | None,
    fingerprint: str | None,
) -> int | None:
    """Insert the quality row, or return None if that quality already exists.

    The conflict target is the *expression* index, which is what makes
    ``1080p`` and ``1080P`` the same variant — a case difference must never look
    like a new quality to publish.
    """
    quality = parsed.quality or "unknown"
    variant_id = await db.fetchval(
        """
        insert into app.media_variant (
            episode_id, quality, quality_rank, release_variant, language_tag,
            source_candidate_id, file_name, file_size_bytes, fingerprint, status
        ) values (
            $1, $2, $3, $4, $5, $6, $7, $8, $9, 'pending'
        )
        on conflict (episode_id, lower(quality), coalesce(lower(release_variant), '')) do nothing
        returning id
        """,
        episode_id,
        quality,
        parsed.quality_rank_value if parsed.quality_rank_value is not None else 999,
        parsed.release_variant,
        _language_tag(parsed),
        candidate_id,
        parsed.file_name,
        file_size_bytes,
        fingerprint,
    )
    if variant_id is None:
        return None
    if fingerprint:
        await db.execute(
            "insert into app.dupe_fingerprint (fingerprint, variant_id) values ($1, $2) on conflict do nothing",
            fingerprint,
            int(variant_id),
        )
    return int(variant_id)


def _language_tag(parsed: normalize.ParsedEpisode) -> str | None:
    if not parsed.languages:
        return None
    return "+".join(parsed.languages)[:32]


def _strip_at(username: str | None) -> str | None:
    return username.lstrip("@") if username else None
