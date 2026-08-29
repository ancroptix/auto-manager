"""Job handlers.

Every media-path handler in this module is an explicit unimplemented marker, not
an empty function. That is deliberate: an empty handler would let the queue
"successfully" process episodes while nothing was archived, published, or
linked — the worst possible failure mode for this project, because it looks
healthy in /status.

Each marker raises :class:`FeatureNotImplemented`, which the worker turns into a
blocked job that /status reports. So the feature list is visible as a live
backlog instead of a silently green pipeline.

Implemented for real here: ``reconciliation`` — the restart-safety job, which
needs nothing but the database and is what guarantees nothing is lost while the
free Render instance was asleep.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from . import ingest, storagebot, thumbnails
from .db import Database
from .keys import archive_key
from .stages import JobKind, JobStage

log = logging.getLogger("auto_manager.handlers")

__all__ = ["FeatureNotImplemented", "Context", "build_registry", "JobHandler"]

Handler = Callable[[dict[str, Any], "Context"], Awaitable[dict[str, Any] | None]]


class FeatureNotImplemented(NotImplementedError):
    """Raised by a stub handler so the job blocks visibly rather than passing."""


@dataclass
class Context:
    db: Database
    settings: Any
    telegram: Any = None  # TelegramClient | None; injected once the session exists

    @property
    def outbound_enabled(self) -> bool:
        return bool(self.settings.outbound_enabled and self.telegram is not None)


# --- feature modules that land next -----------------------------------------

DEPENDENCIES: dict[str, str] = {
    JobKind.ARCHIVE_MEDIA.value: "server-side copy into the private master archive channel",
    JobKind.STORAGE_UPLOAD.value: (
        "the @anime_hindifilesbot menu is known (app/storagebot.py: /genlink, /batch, "
        "/custom_batch, /special_link, /universal_link) and so is the first conversation: "
        + storagebot.flow_note()
        + ". What is missing now is the write layer — the code that forwards, reads the reply back "
        "and stores the token — and the behaviour only an authenticated run can settle, listed in "
        "app.storagebot.still_unknown(). Which bot answered is read from the link's host, not from "
        "its wording: @Link_providerobot is a sibling clone that says the same sentence "
        "(app/linkprovider.py)"
    ),
    JobKind.LINK_VERIFY.value: "link liveness probe",
    # The publisher has two audiences, and only one of them is a series channel: the episode post
    # goes to the destination, and the updates channel is owed a short announcement carrying the
    # link-provider deep link. That announcement's box is approved (templates.announcement_post,
    # 2026-08-28), and this job kind is *still* blocked, because approval is not a transport: what is
    # missing is the send path. ``docs/channel-help.md`` is the documented flow the episode half has
    # to match, and no part of it has been walked on a channel of ours yet.
    JobKind.PUBLISH_POST.value: (
        "the MTProto send path — Channel Help's documented post flow for a destination episode "
        "(docs/channel-help.md), and the updates-channel announcement, whose text is approved and "
        "whose sender does not exist yet (app/linkprovider.py)"
    ),
    JobKind.EDIT_POST.value: (
        "the session's messages.editMessage path: replacing the media on a post Channel Help made, or "
        "editing its caption, is documented in docs/channel-help.md under My posts — which is a "
        "description of the tool, not a test of ours"
    ),
    JobKind.SEASON_STICKER.value: "sticker-pack label mapping (S1, Season 2, ...)",
    JobKind.JOIN_REQUEST_CAMPAIGN.value: (
        "the owner-triggered sender itself. The wording is a setting now (/joinmsg writes "
        "app.config key joinrequest.message; app/joinmsg.py holds the presets and the refusals), "
        "and app.join_campaign already carries the pacing columns and the check that a send never "
        "approves. What does not exist is the code that reads still-pending requests, honours every "
        "FloodWait and stops on a restriction — the same missing MTProto write path that blocks "
        "publish_post and edit_post"
    ),
    JobKind.LINK_HEALTH_CHECK.value: "periodic re-check of published storage links",
}


def _stub(kind: str) -> Handler:
    async def _handler(job: dict[str, Any], ctx: Context) -> dict[str, Any] | None:
        raise FeatureNotImplemented(
            f"handler for {kind!r} is not implemented yet — {DEPENDENCIES.get(kind, 'awaiting design input')}"
        )

    return _handler


async def reconciliation(job: dict[str, Any], ctx: Context) -> dict[str, Any]:
    """Reclaim stale leases and record that we re-synced after a restart.

    Runs on every boot and periodically thereafter. On Render free tier a
    mid-upload kill is routine, so this is the mechanism that turns "it stopped"
    into "it resumed": jobs keep their stage and get re-queued here.
    """
    reclaimed = await ctx.db.release_expired_locks()
    await ctx.db.fetchrow(
        "update app.service_state set last_reconcile_at = now() where id = 1"
    )
    health = await ctx.db.queue_health() or {}
    log.info("reconciliation: reclaimed %s stale lease(s), queue=%s", reclaimed, health)
    return {"reclaimed_locks": reclaimed, "queue": {k: int(v or 0) for k, v in health.items()}}


async def ingest_media(job: dict[str, Any], ctx: Context) -> dict[str, Any]:
    """Write the rows one source message implies: candidate, episode, variant.

    The *scanning* half of ingest still needs a logged-in session — that is the
    Telethon event loop, not a decision — but everything downstream of a message
    lives here and is verified against the real schema. When the scanner exists it
    will call the same :func:`app.ingest.record_message` with the same payload
    shape, so nothing about this handler is throwaway.
    """
    payload: Mapping[str, Any] = {**(job.get("payload") or {})}
    channel_id = job.get("source_channel_id") or payload.get("source_channel_id")
    message_id = job.get("message_id") or payload.get("message_id")
    if channel_id is None or message_id is None:
        raise ValueError(
            "ingest_media needs source_channel_id and message_id in its payload; "
            "the source-channel scanner is what supplies them"
        )
    return await ingest.record_message(
        ctx.db,
        source_channel_id=int(channel_id),
        message_id=int(message_id),
        media_idx=int(payload.get("media_idx", job.get("media_idx", 0)) or 0),
        media_type=payload.get("media_type"),
        file_name=payload.get("file_name"),
        raw_caption=payload.get("caption") or payload.get("raw_caption"),
        file_size_bytes=payload.get("file_size_bytes"),
        fingerprint=payload.get("fingerprint"),
        quality_order=await ctx.db.config("quality.order", None),
    )


async def thumbnail_screen(job: dict[str, Any], ctx: Context) -> dict[str, Any]:
    """Judge one source candidate's thumbnail and act on the verdict.

    This is the first handler that does real work end-to-end, and it is written
    to be honest about its own evidence: until the Telegram media layer can open
    the image, a candidate with nothing wrong in its text is *parked* for review
    rather than passed. A hard publish gate that quietly degrades into "we found
    no evidence of a problem" is not a gate.

    What it does:

    1. read the candidate row,
    2. screen it with the operator's policy from ``app.config``,
    3. persist ``thumbnail_status`` + ``disposition`` + the reason,
    4. queue an owner review when the verdict needs one,
    5. on a publishable verdict, queue the archive step for each variant that
       came from this candidate — the only place the ladder may move forward.
    """
    payload = job.get("payload") or {}
    candidate_id = job.get("candidate_id") or payload.get("candidate_id")
    if candidate_id is None:
        raise ValueError("thumbnail_screen job carries no candidate_id")

    row = await ctx.db.fetchrow("select * from app.source_candidate where id = $1", candidate_id)
    if row is None:
        return {"candidate_id": candidate_id, "skipped": "candidate row no longer exists"}

    primary = tuple(
        await ctx.db.config("branding.primary_handles", list(thumbnails.PRIMARY_HANDLES))
        or list(thumbnails.PRIMARY_HANDLES)
    )
    strict = bool(await ctx.db.config("thumbnail.strict_mode", True))
    handles = tuple(row.get("detected_handles") or ()) or tuple(
        thumbnails.handles_from(row.get("file_name") or "", row.get("raw_caption") or "")
    )
    media_type = str(row.get("media_type") or "").casefold()
    verdict = thumbnails.screen(
        image_present=media_type in {"photo", "video", "document", "animation", "gif", "round_video"},
        handles=handles,
        primary=primary,
        strict=strict,
        evidence="caption_only",
    )

    await ctx.db.execute(
        """
        update app.source_candidate
           set thumbnail_status = $2::app.thumbnail_status,
               disposition      = $3::app.candidate_disposition,
               detected_handles = $4::text[],
               reason           = $5
         where id = $1
        """,
        candidate_id,
        verdict.status,
        verdict.disposition,
        list(verdict.foreign_handles + verdict.primary_handles),
        verdict.reason[:400],
    )

    if verdict.needs_review:
        await ctx.db.execute(
            """
            insert into app.thumbnail_review (candidate_id, detected_handles, status)
            values ($1, $2::text[], 'pending')
            on conflict (candidate_id) do update
               set detected_handles = excluded.detected_handles,
                   status          = 'pending',
                   decided_at      = null
            """,
            candidate_id,
            list(verdict.foreign_handles or handles),
        )
    else:
        # A previously parked candidate that now passes must not stay in the queue.
        await ctx.db.execute(
            "delete from app.thumbnail_review where candidate_id = $1 and status = 'pending'",
            candidate_id,
        )

    variants = await ctx.db.fetch(
        """
        select id, episode_id, quality, status
          from app.media_variant
         where source_candidate_id = $1
        """,
        candidate_id,
    )
    queued: list[int] = []
    if verdict.publishable:
        for variant in variants:
            await ctx.db.execute(
                "update app.media_variant set thumbnail_status = $2::app.thumbnail_status, updated_at = now() where id = $1",
                variant["id"],
                verdict.status,
            )
            job_row = await ctx.db.enqueue(
                JobKind.ARCHIVE_MEDIA.value,
                archive_key(int(variant["id"])),
                stage=JobStage.THUMBNAIL_CHECKED,
                payload={"candidate_id": candidate_id, "thumbnail_status": verdict.status},
                variant_id=int(variant["id"]),
                episode_id=variant.get("episode_id"),
                # candidate_id too, so "which jobs came from this message" is one
                # query when something has to be traced by hand at 2am.
                candidate_id=candidate_id,
            )
            if job_row:
                queued.append(int(variant["id"]))
    else:
        for variant in variants:
            await ctx.db.execute(
                "update app.media_variant set thumbnail_status = $2::app.thumbnail_status, status = 'review', updated_at = now() where id = $1",
                variant["id"],
                verdict.status,
            )
        policy = await ctx.db.config("thumbnail.on_no_clean_candidate", "ask_owner")
        return {
            "candidate_id": candidate_id,
            "status": verdict.status,
            "disposition": verdict.disposition,
            "reason": verdict.reason,
            "foreign_handles": list(verdict.foreign_handles),
            "variants_parked": len(variants),
            "no_clean_action": thumbnails.no_clean_action(str(policy)),
        }

    return {
        "candidate_id": candidate_id,
        "status": verdict.status,
        "disposition": verdict.disposition,
        "reason": verdict.reason,
        "archive_jobs_queued": queued,
    }


def build_registry() -> dict[str, Handler]:
    """Job kind -> handler. The keys are the whole supported vocabulary."""
    registry: dict[str, Handler] = {
        JobKind.RECONCILIATION.value: reconciliation,
        JobKind.INGEST_MEDIA.value: ingest_media,
        JobKind.THUMBNAIL_SCREEN.value: thumbnail_screen,
    }
    for kind in JobKind:
        if kind.value not in registry:
            registry[kind.value] = _stub(kind.value)
    return registry
