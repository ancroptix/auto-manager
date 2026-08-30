"""Job handlers: the two that need only a database, plus the eight that reach Telegram.

Readers first. ``reconciliation`` and ``ingest_media``/``thumbnail_screen`` work on rows and files
and cannot put anything in front of an audience, so they were written first and are verified against
the real schema.

:mod:`app.writers` carries the eight that write — archive, storage handoff, the two link checks, the
destination post, the edit, the season sticker and the join-request campaign. They landed on
2026-08-29, the day the operator stopped asking for an explanation of the gap and said *"banao
sabkuch complete karo"*; what keeps them safe is not a stub, it is `app.sender`'s plan mode, the
approved-caption gate and the rights check, so the first run a session makes is a read of what the
queue *would* have sent.

``DEPENDENCIES`` used to mean "this kind is not implemented". It now means something narrower and
more useful: **what each kind still waits on from the operator or a live session**, in the same
sentence the job raises when it blocks. A test (``tests/test_writers.py``) keeps the two identical,
because a block reason that drifts away from the documented one is how "we are waiting on you" turns
into "the app is broken".
"""


from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from . import ingest, storagebot, thumbnails
from .db import Database
from . import discover

#: Campaigns the boot sweep picks up again. A run of `app/writers.py`'s campaign handler stops after what
#: fits inside its job lease and asks for its next run under `keys.campaign_run_key`, counted by the contacts
#: already recorded — so this query asks for the *same* key, and `app.enqueue_job`'s unique dedup key
#: swallows one of the two. `queued` and `running` are the states in which a job already exists, and a
#: `blocked` campaign is left alone on purpose: that status means a human has to answer something.
RESUMABLE_CAMPAIGNS_SQL = (
    "select c.id, c.destination_id,"
    " (select count(*) from app.join_campaign_contact k where k.campaign_id = c.id) as contacts"
    " from app.join_campaign c"
    " where c.status in ('ready', 'running')"
    " and c.message_template is not null and btrim(c.message_template) <> ''"
    " and not exists ("
    "   select 1 from app.job j"
    "    where j.kind = 'join_request_campaign'"
    "      and j.status in ('queued', 'running')"
    "      and (j.payload ->> 'campaign_id')::int = c.id"
    " ) order by c.id limit 10"
)
from .keys import archive_key
from .stages import JobKind, JobStage
from .writers import FeatureNotImplemented, NeedsInput, build_writers

log = logging.getLogger("auto_manager.handlers")

__all__ = [
    "Context",
    "DEPENDENCIES",
    "FeatureNotImplemented",
    "NeedsInput",
    "build_registry",
]

Handler = Callable[[dict[str, Any], "Context"], Awaitable[dict[str, Any] | None]]


@dataclass
class Context:
    db: Database
    settings: Any
    telegram: Any = None  # TelegramClient | None; injected once the session exists

    @property
    def outbound_enabled(self) -> bool:
        return bool(self.settings.outbound_enabled and self.telegram is not None)


# --- feature modules that land next -----------------------------------------

#: What each write kind still waits on. Empty means "nothing: it runs the moment a session does".
DEPENDENCIES: dict[str, str] = {
    JobKind.ARCHIVE_MEDIA.value: (
        "a row in app.archive_channel naming the private master archive; this program never invents a "
        "channel to hold the only spare copy of an episode"
    ),
    JobKind.STORAGE_UPLOAD.value: (
        "an authenticated session, because the handoff is a conversation with @anime_hindifilesbot "
        "and not a description of one. The verbs it drives are exactly the ones the operator walked "
        "through: "
        + storagebot.flow_note()
        + ". /genlink, /custom_batch and /special_link exist on that bot's menu and stay un-driven — "
        "a single-message link, a custom-caption batch and a special link are each the operator's "
        "call, and a handler that picked one would be picking their post format for them. What a menu "
        "and a screenshot cannot say is whether the reply's link survives the source post, so the "
        "handler reads the answer, refuses to store a link it did not receive, and blocks with the "
        "shapes it saw. Which bot answered is read from the link's host, not its wording: "
        "@Link_providerobot is a sibling clone that says the same sentence (app/linkprovider.py)"
    ),
    JobKind.LINK_VERIFY.value: (
        "a session, for the half that reads the source range back; the shape half runs today and is "
        "the half that catches a link belonging to another bot"
    ),
    JobKind.LINK_HEALTH_CHECK.value: (
        "the same read as link_verify, plus the rate limit of a free deployment: it is bounded per run "
        "on purpose, so a health check never becomes the outage it was meant to notice"
    ),
    JobKind.PUBLISH_POST.value: (
        "the caption box is approved (app/captions.py), so the only gate left is who presses send: "
        "publish.route — chelp_block renders the approved caption and the button block for Channel "
        "Help to paste (docs/channel-help.md is the documented flow it must match), own_session sends "
        "it from our account with real inline buttons. Either way the announcement half needs a card "
        "post named per destination (/card) so the notice carries a link-provider deep link rather "
        "than the invite itself (app/linkprovider.py)"
    ),
    JobKind.EDIT_POST.value: (
        "our own rights on that destination, read by /probe (app/rights.py) — and the same approved "
        "box plus button block as publish_post, because an edit rewrites the post rather than "
        "printing a second one (docs/channel-help.md)"
    ),
    JobKind.SEASON_STICKER.value: (
        "/sticker <series> <season> from <channel> <message id>: Telegram addresses a sticker by the "
        "document that carries it, so a pack name or an install link is not enough to post from, and "
        "guessing which sticker opens a season is not this program's call"
    ),
    JobKind.JOIN_REQUEST_CAMPAIGN.value: (
        "the campaign text (app/joinmsg.py, /joinmsg) and a campaign row set to ready by "
        "/campaign … confirm with the code that command shows. The text is refused if it carries an "
        "invite link or reads like a decision about the request, and the per-hour ceiling pauses the "
        "campaign rather than pushing past it"
    ),
}


def _stub(kind: str) -> Handler:
    """A handler that refuses, for a job kind nothing has claimed yet.

    Marked with ``is_stub`` so a test can assert that no *supported* kind is served by one: "the
    registry covers the vocabulary" is only worth saying if something checks it, and a kind that
    quietly falls back to a stub would otherwise look like a queue that is merely idle.
    """

    async def _handler(job: dict[str, Any], ctx: Context) -> dict[str, Any] | None:
        raise FeatureNotImplemented(
            f"nothing performs {kind!r} yet — {DEPENDENCIES.get(kind, 'awaiting design input')}"
        )

    _handler.is_stub = True  # type: ignore[attr-defined]
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
    result: dict[str, Any] = {
        "reclaimed_locks": reclaimed,
        "queue": {k: int(v or 0) for k, v in health.items()},
    }
    # The role sweep, and the reason it hangs off *this* job: reconciliation already runs at boot and
    # periodically after it, it is the one job whose whole meaning is "look at the world again", and a new
    # job kind would need a queue enum value for a check that costs one dialog walk. `discover.auto` is
    # off by default, so nothing here happens until the operator says it may.
    try:
        if bool(await ctx.db.config(discover.AUTO_KEY, False)) and ctx.telegram is not None:
            from .probe import collect_dialogs  # noqa: PLC0415  (the walk lives with the probe that owns it)

            entries = await collect_dialogs(
                ctx.telegram, verify_rights=True, rights_limit=discover.AUTO_RIGHTS_LIMIT
            )
            swept = await discover.sweep(ctx.db, entries, auto=True)
            flipped = [one for one in swept["flips"] if one.get("applied")]
            result["discover"] = {
                "read": len(entries),
                "rights_re_read": sum(
                    1 for one in entries if isinstance(one, dict) and one.get("rights_source") == "participant"
                ),
                "switched": len(flipped),
                "rights_written": int((swept.get("rights") or {}).get("written") or 0),
            }
            for one in flipped:
                log.info(
                    "reconciliation: %s stopped being read as a source — %s",
                    one.get("title") or one.get("row_id"),
                    one.get("why"),
                )
    except Exception as exc:  # noqa: BLE001 - a sweep that fails must not fail the reconciliation
        log.warning("reconciliation: channel roles could not be re-read (%s)", str(exc)[:160])
    # A campaign that was half-sent when the instance stopped. Render's free tier spins down, and a
    # join-request campaign that quietly stays at `running` with nobody assigned to it is the worst of both:
    # the operator believes people are being written to, and they are not. Same key as the runner's own
    # hand-off, so the two paths cannot queue the same run twice.
    try:
        from . import keys  # noqa: PLC0415
        from .stages import JobKind, JobStage  # noqa: PLC0415

        resumed = 0
        for row in list(await ctx.db.fetch(RESUMABLE_CAMPAIGNS_SQL) or []):
            queued = await ctx.db.enqueue(
                JobKind.JOIN_REQUEST_CAMPAIGN.value,
                keys.campaign_run_key(int(row["id"]), int(row.get("contacts") or 0)),
                stage=JobStage.DISCOVERED,
                payload={"campaign_id": int(row["id"]), "destination_id": row.get("destination_id")},
                destination_id=row.get("destination_id"),
            )
            resumed += 1 if queued else 0
        if resumed:
            log.info("reconciliation: %s join-request campaign run(s) queued", resumed)
            result["campaigns_resumed"] = resumed
    except Exception as exc:  # noqa: BLE001 - a resume that fails must not fail the reconciliation
        log.warning("reconciliation: join campaigns could not be re-queued (%s)", str(exc)[:160])
        result["discover"] = {"error": f"{type(exc).__name__}: {str(exc)[:160]}"}
    return result


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


def build_registry(db: Any = None, settings: Any = None, *, client_factory: Any = None) -> dict[str, Handler]:
    """Job kind -> handler. The keys are the whole supported vocabulary.

    The writers are built here rather than at import time because they hold a database handle and a
    session factory: a registry assembled at import would either need a global connection or hand
    every handler ``None``, and a handler that quietly carries a None client is the bug this whole
    module exists to avoid. Called with no arguments it still returns the two readers, so the
    ``build_registry()[kind]`` lookups in the tests and in ``/status`` keep working.
    """
    registry: dict[str, Handler] = {
        JobKind.RECONCILIATION.value: reconciliation,
        JobKind.INGEST_MEDIA.value: ingest_media,
        JobKind.THUMBNAIL_SCREEN.value: thumbnail_screen,
    }
    if db is not None:
        registry.update(build_writers(db, settings, client_factory=client_factory))
    for kind in JobKind:
        if kind.value not in registry:
            registry[kind.value] = _stub(kind.value)
    return registry
