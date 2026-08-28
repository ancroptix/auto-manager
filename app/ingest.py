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

import json
from typing import Any, Mapping

from . import inplace, normalize, seasons
from .keys import (
    discovery_key,
    normalize_title,
    screening_key,
)
from .stages import JobKind, JobStage

__all__ = ["record_message", "season_stream", "SOURCE_MODE_IGNORED"]

SOURCE_MODE_IGNORED = "ignore"

#: What :func:`_audio_gate` reports. Three states, because "who decided" is the question a
#: review queue asks, and a relaxed gate has to be distinguishable from a disabled one.
GATE_REQUIRED = "hindi-audio-required"
GATE_RELAXED_IN_PLACE = "relaxed-for-in-place-captioning"
GATE_OFF = "off-for-this-channel"
GATE_FORCED_ON = "forced-on-by-caller"
GATE_FORCED_OFF = "forced-off-by-caller"


def _audio_gate(channel: Mapping[str, Any]) -> str:
    """Whether the Hindi-audio rule applies to a file read from this channel.

    Read as data, not as a special case buried in the parser: the destination row's
    ``publish_mode`` (or the channel's own confirmed ``publish_role``) is what says "these are
    our files, already posted", and ``normalize.parse_episode`` keeps its one gate rule.
    """
    if not bool(channel["require_hindi_audio"]):
        return GATE_OFF
    role = channel["publish_role"]
    mode = channel["destination_publish_mode"]
    if inplace.mode_allows_missing_audio(mode) or str(role or "") == "source_and_destination":
        return GATE_RELAXED_IN_PLACE
    return GATE_REQUIRED


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
    video_height: int | None = None,
    video_width: int | None = None,
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
               require_hindi_audio, include_subbed,
               declared_series, declared_audio, declared_season, publish_role,
               coalesce(
                   (select d.publish_mode from app.destination d where d.id = sc.destination_id),
                   (select d.publish_mode from app.destination d
                     where d.telegram_channel_id = sc.telegram_channel_id limit 1)
               ) as destination_publish_mode
          from app.source_channel sc
         where sc.id = $1
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
    # Two different kinds of "this channel is Bleach": the operator said so (0006's
    # declared columns, set from /source), and we are reading the channel's own title or
    # handle. The first is a statement and may name a destination channel; the second is one
    # signal where the spec asks for two, so it may archive files but not found a channel.
    declared_series = channel["declared_series"]
    # The global veto on the whole idea: with this off, a channel's audio declaration is
    # still recorded (and reported as ignored, below) but it no longer decides whether a file
    # with no language text is in scope. Turning it off must not require editing twenty
    # channel rows, and undoing it must not require a re-scan either.
    trust_declaration = bool(await db.config("ingest.accept_channel_audio_declaration", True))
    declared_audio = channel["declared_audio"] if trust_declaration else None
    # In-place publishing relaxes one gate and only one. If this channel is the operator's own
    # destination and the file is already posted there, "prove this carries Hindi audio" has
    # nothing to protect: nothing is entering the channel, and a caption withheld from your own
    # video is a formatting failure, not a scope violation. A file being *brought in* from
    # elsewhere is still judged by the flags above. See app/inplace.py.
    if require_hindi_audio is None:
        audio_gate = _audio_gate(channel)
        require_hindi_audio = audio_gate is GATE_REQUIRED
    else:  # an explicit argument (a strict re-run, a test) outranks the channel's own flags
        audio_gate = GATE_FORCED_ON if require_hindi_audio else GATE_FORCED_OFF
    parsed = normalize.parse_episode(
        file_name=file_name,
        raw_caption=raw_caption,
        source_series=declared_series or channel["title"] or _strip_at(channel["username"]),
        source_series_declared=bool(declared_series),
        season_hint=channel["declared_season"],
        declared_audio=declared_audio,
        video_height=video_height,
        video_width=video_width,
        quality_order=quality_order,
        require_hindi_audio=require_hindi_audio,
        include_subbed_only=bool(channel["include_subbed"])
        if include_subbed_only is None
        else include_subbed_only,
        # In-place mode: a file whose own text says nothing about audio is captioned anyway,
        # because the caption describes the operator's own post rather than licensing a copy
        # into the channel. A file that says "subbed" still says subbed, and still fails.
        unknown_audio_allowed=audio_gate is GATE_RELAXED_IN_PLACE,
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
        on conflict (source_channel_id, message_id, media_idx) do update set
            file_name = excluded.file_name,
            raw_caption = excluded.raw_caption,
            parsed = excluded.parsed,
            season_number = excluded.season_number,
            episode_number = excluded.episode_number,
            language_tag = excluded.language_tag,
            quality = excluded.quality,
            quality_rank = excluded.quality_rank,
            disposition = excluded.disposition,
            reason = excluded.reason
          -- Only a row that is *still parked for want of information* may be re-read, and
          -- the thumbnail verdict is never reset by a rescan. That single condition is what
          -- makes "declare the channel, then rescan" a way to file a 400-file backlog
          -- without letting any later scan quietly rewrite what has already been decided —
          -- or silently remove the publish gate by marking a screened image unchecked.
        where app.source_candidate.disposition = 'pending'
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
            # The difference between this and a bug is the word *decided*: a parked row is
            # re-read on the next scan, a decided row is not, and `/source` relies on that to
            # un-park a backlog without rewriting history.
            "skipped": "already ingested and decided (only a parked row is re-read)",
            "candidate_id": existing["id"] if existing else None,
            "disposition": existing["disposition"] if existing else None,
        }

    report: dict[str, Any] = {
        "candidate_id": candidate_id,
        "disposition": parsed.disposition,
        "reason": parsed.reason,
        "file_kind": parsed.file_kind,
        "flags": list(parsed.flags),
        "series_source": parsed.series_source,
        "series_confirmed": parsed.series_confirmed,
        "audio_source": parsed.audio_source,
        "quality_source": parsed.quality_source,
        # Only when a declaration existed and was deliberately not used, so /status can
        # say "340 files parked because the audio knob is off" instead of looking broken.
        "audio_declaration_ignored": bool(declared_audio is None and channel["declared_audio"]),
        # Named in the report because a file accepted with no audio claim is otherwise
        # indistinguishable from a file accepted with a real one, and /status would be reading
        # tea leaves about a caption that says `Unknown`.
        "audio_gate": audio_gate,
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

    # Is this the start of a new season? Answered before any row is written, because
    # the season number is part of every episode's canonical key: file one season-2
    # episode 1 into season 1 and the damage is a duplicate post, not a typo.
    # A channel that states "everything here is season 2" (0006's declared_season, or a
    # configured season_hint) is a *starting point*, not a boundary: look at that shelf's
    # arithmetic instead of "wherever we got to", or its episode 1 is filed as a duplicate
    # copy of season 1's episode 1 and the canonical key never complains.
    hinted_season = parsed.season if parsed.season_source == "hint" else None
    stream = await season_stream(db, series_id, season_number=hinted_season)
    boundary = seasons.classify(
        episode=parsed.episode,
        labelled_season=parsed.season if parsed.season_declared else None,
        current_season=stream["current_season"],
        default_season=parsed.season if parsed.season_source == "hint" else None,
        highest=stream["highest"],
        populated=stream["populated"],
        file_kind=parsed.file_kind,
    )
    confirm_first = bool(await db.config("seasons.confirm_unlabelled_reset", True))
    if not confirm_first:
        # The operator has said this channel may be trusted: an unlabelled restart
        # becomes an *inferred* boundary instead of a question. Everything downstream —
        # stickers, the season row, the caption's Total Episodes — then behaves exactly
        # as it does for a declared one, with the provenance recorded as inferred.
        boundary = seasons.accept_as_inferred(boundary)
    if confirm_first and boundary.verdict is seasons.Verdict.RESET:
        report["season"] = {
            "verdict": boundary.verdict.value,
            "season": boundary.season,
            "reason": boundary.reason,
            "evidence": boundary.evidence,
        }
        # Held, not filed. The source restarted its numbering without ever writing a
        # season label, which reads like a new season and might be one — but the two
        # wrong answers here (duplicate "Episode 01" posts, or a season 1 that quietly
        # contains season 2) are both permanent and public.
        await db.execute(
            "update app.source_candidate set disposition = 'pending', reason = $2 where id = $1",
            candidate_id,
            f"season boundary unconfirmed: {boundary.reason}",
        )
        report["disposition"] = "pending"
        report["needs_review"] = True
        report["held_reason"] = boundary.reason
        return report

    # Reported after the knob has been applied, because /status must describe the decision
    # that was taken, not the one that was overridden on the way to it.
    report["season"] = {
        "verdict": boundary.verdict.value,
        "season": boundary.season,
        "reason": boundary.reason,
        "evidence": boundary.evidence,
        "asked_owner_first": boundary.verdict is seasons.Verdict.RESET,
    }
    season_id = await _resolve_season(db, series_id, parsed, boundary=boundary)
    season_number = int(boundary.season)
    report["series_id"], report["season_id"], report["season_number"] = series_id, season_id, season_number
    if boundary.is_boundary:
        await _queue_transition_stickers(db, boundary, season_id=season_id, stream=stream, report=report)

    if parsed.file_kind == "batch":
        # Episodes are recorded (coverage is real information), but no variant is
        # invented from one archive — see the module docstring.
        for number in parsed.episode_numbers():
            episode_id = await _resolve_episode(db, season_id, parsed, number, season_number=season_number)
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
        episode_id = await _resolve_episode(db, season_id, parsed, number, season_number=season_number)
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


async def season_stream(db: Any, series_id: int, *, season_number: int | None = None) -> dict[str, Any]:
    """What this series looks like from the filing cabinet's point of view.

    One query, because ingest runs per message and must not fan out into three: the
    highest season number we have a row for, the largest episode number filed inside it,
    and which seasons actually contain episodes. ``current_season`` deliberately takes
    the highest *season number* rather than "the most recently touched" — after a
    restart mid-backfill, recency would point at whatever the sweep reached last and a
    season could be opened twice.

    ``season_number`` retargets the arithmetic at one season. It exists for the channel
    whose entire backlog is season 2 while the cabinet already holds season 1: asking
    "where are we?" would answer 1, and its episode 1 would be filed as a *duplicate copy*
    of season 1's episode 1 — the canonical key is what makes that collision silent, so the
    query has to be able to look at the right shelf. A season with no row yet reports
    ``highest=None`` and zero episodes, which is how the caller learns it is starting one.
    """
    rows = await db.fetch(
        """
        select s.season_number,
               count(e.id) as episodes,
               max(e.episode_number) as highest
          from app.season s
          left join app.episode e on e.season_id = s.id
         where s.series_id = $1
         group by s.season_number
         order by s.season_number
        """,
        series_id,
    )
    if not rows:
        return {
            "current_season": int(season_number) if season_number is not None else 1,
            "highest": None,
            "episodes_in_current": 0,
            "populated": [],
            "seasons": [],
        }
    seasons_ = [dict(row) for row in rows]
    numbers = [int(row["season_number"]) for row in seasons_]
    if season_number is not None:
        current = int(season_number)
        row = next(
            (r for r in seasons_ if int(r["season_number"]) == current),
            {"season_number": current, "episodes": 0, "highest": None},
        )
    else:
        current = max(numbers)
        row = next(r for r in seasons_ if int(r["season_number"]) == current)
    return {
        "current_season": current,
        "highest": int(row["highest"]) if row["highest"] is not None else None,
        "episodes_in_current": int(row["episodes"] or 0),
        "populated": [int(r["season_number"]) for r in seasons_ if int(r["episodes"] or 0) > 0],
        "seasons": numbers,
    }


def _boundary_kind(boundary: seasons.Boundary | None) -> str | None:
    """`declared` or `inferred`, for ``app.season.boundary_kind`` — or NULL.

    A season opened by a caption is a different kind of fact from one opened by a
    numbering restart we accepted on the operator's word, and the column is where that
    difference survives after the logs have rotated.
    """
    if boundary is None or not boundary.is_boundary:
        return None
    return "inferred" if boundary.evidence.get("accepted") == "inferred" else "declared"


async def _queue_transition_stickers(
    db: Any,
    boundary: seasons.Boundary,
    *,
    season_id: int,
    stream: dict[str, Any],
    report: dict[str, Any],
) -> None:
    """The closing sticker first, then the new season's opening one, then uploads.

    Queued as jobs rather than posted inline: the sticker's document id comes from the
    pack mapping, which is a live-account question (see ``stickers.mapping_mode``), and a
    job that cannot run yet shows up as *blocked* in /status. An inline call would have to
    either invent an id or fail the ingest, and failing the ingest over a divider sticker
    is how an episode gets lost.
    """
    steps = seasons.transition_stickers(
        boundary,
        previous_has_content=bool(stream.get("episodes_in_current")),
    )
    queued = []
    for step in steps:
        dedup = f"season-sticker:{boundary.evidence.get('current_season') or stream['current_season']}:{step.kind}:{step.season}"
        job = await db.enqueue(
            JobKind.SEASON_STICKER.value,
            dedup,
            payload={**step.as_payload(series_id=0), "reason": boundary.reason},
            season_id=season_id,
            priority=60,
        )
        queued.append({"kind": step.kind, "season": step.season, "job": bool(job)})
    report["stickers"] = queued


async def _resolve_season(
    db: Any,
    series_id: int,
    parsed: normalize.ParsedEpisode,
    *,
    boundary: seasons.Boundary | None = None,
) -> int:
    """Create or widen the season row, and record *why* it exists when this is a boundary.

    Note what this function never writes: ``first_episode``/``last_episode``, the declared
    span. Those are the owner's statement (``/declare``), and filling them from an upload
    is the bug that used to publish a "Complete Season" post the week a source paused. The
    ``least``/``greatest`` here widen the *observed* span, which is a different question; the ``coalesce`` on the boundary columns is what makes them non-repeating:
    once a season's reason is written it never changes, so re-scanning a channel cannot
    rewrite "declared by the source" into "inferred" and make a real decision look like a
    guess.
    """
    if boundary is not None:
        season_number = boundary.season
    else:
        season_number = parsed.season if parsed.season is not None else 1  # filing, not declaring
    numbers = parsed.episode_numbers() or (parsed.episode,)
    first = min((n for n in numbers if n is not None), default=None)
    last = max((n for n in numbers if n is not None), default=None)
    return int(
        await db.fetchval(
            """
            insert into app.season (series_id, season_number, observed_first, observed_last,
                                    boundary_kind, boundary_evidence)
            values ($1, $2, $3, $4, $5, coalesce($6::jsonb, '{}'::jsonb))
            on conflict (series_id, season_number) do update
               set observed_first = least(coalesce(app.season.observed_first, excluded.observed_first),
                                          coalesce(excluded.observed_first, app.season.observed_first)),
                   observed_last  = greatest(coalesce(app.season.observed_last, 0),
                                             coalesce(excluded.observed_last, app.season.observed_last)),
                   boundary_kind = coalesce(app.season.boundary_kind, excluded.boundary_kind),
                   boundary_evidence = case
                     when app.season.boundary_evidence = '{}'::jsonb then excluded.boundary_evidence
                     else app.season.boundary_evidence
                   end,
                   updated_at = now()
            returning id
            """,
            series_id,
            season_number,
            first,
            last,
            _boundary_kind(boundary),
            json.dumps(boundary.evidence) if boundary is not None and boundary.is_boundary else None,
        )
    )


async def _resolve_episode(
    db: Any,
    season_id: int,
    parsed: normalize.ParsedEpisode,
    number: int,
    *,
    season_number: int | None = None,
) -> int:
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
            parsed.canonical_key(number, season=season_number),
            parsed.series,
            # Not ``parsed.languages``: an episode filed under a channel-level audio
            # declaration has no language *text*, and `app.episode.languages` is NOT NULL,
            # so writing the raw tuple would crash the ingest of every bare file. The
            # identity tuple is the one the canonical key used, so the columns and the key
            # can never disagree about which release this is.
            list(parsed.identity_languages),
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
    """The candidate's language column, from the same tokens as its dedup key.

    Reading ``parsed.languages`` directly would leave a file archived under a channel-level
    audio declaration with no language tag while its canonical key carried one — two views
    of the same episode disagreeing, which is how "duplicate" and "missing" both get
    reported about the same file.
    """
    languages = parsed.identity_languages
    if not languages:
        return None
    return "+".join(languages)[:32]


def _strip_at(username: str | None) -> str | None:
    return username.lstrip("@") if username else None
