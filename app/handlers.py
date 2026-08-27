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
from typing import Any, Awaitable, Callable

from .db import Database
from .stages import JobKind

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
    JobKind.INGEST_MEDIA.value: "source-channel scanner + metadata parser (filename/caption patterns)",
    JobKind.THUMBNAIL_SCREEN.value: "thumbnail fetch + handle detection against @ycanime / @india_crunchyroll allowlist",
    JobKind.ARCHIVE_MEDIA.value: "server-side copy into the private master archive channel",
    JobKind.STORAGE_UPLOAD.value: "@anime_hindifilesbot menu protocol (needs one authenticated test run)",
    JobKind.LINK_VERIFY.value: "link liveness probe",
    JobKind.PUBLISH_POST.value: "ChannelHelpPublisher adapter",
    JobKind.EDIT_POST.value: "ChannelHelpPublisher adapter (in-place quality edits)",
    JobKind.SEASON_STICKER.value: "sticker-pack label mapping (S1, Season 2, ...)",
    JobKind.JOIN_REQUEST_CAMPAIGN.value: "owner-approved campaign sender with per-hour pacing",
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


def build_registry() -> dict[str, Handler]:
    """Job kind -> handler. The keys are the whole supported vocabulary."""
    registry: dict[str, Handler] = {JobKind.RECONCILIATION.value: reconciliation}
    for kind in JobKind:
        if kind.value not in registry:
            registry[kind.value] = _stub(kind.value)
    return registry
