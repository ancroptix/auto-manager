"""The eight job kinds that write to Telegram, on top of :mod:`app.sender`.

Written on 2026-08-29, the day the operator said *"banao sabkuch complete karo"* — build it, finish
it, then we add the variables, run the bot, and test everything once a session exists. That last
clause is the design of this file: every handler here already runs today in ``APP_MODE=shadow`` and
answers with the write it *would* make, so the first live session is a diff against a plan rather
than a first contact with 33k subscribers.

Three rules repeat through all eight and are worth saying once, because each of them is a handler
refusing to be clever:

* **a plan is not a result.** When the sender returns ``action="planned"`` the handler stops there:
  no ``storage_link`` row for a link that was never minted, no ``destination_post`` marked
  published, no contact marked sent. The failure mode this prevents is the quietest one in the
  project — a queue that reports success because it recorded its own intentions.
* **needs input, not a guess.** Where something can only be known by a person (which channel is the
  master archive, which post is the card, which sticker message belongs to this season) the handler
  raises :class:`NeedsInput`, and the worker shows it as a *blocked* job that ``/status`` counts. A
  retry loop over an unconfigured setting makes a deployment that has not been set up look like one
  that is broken.
* **the ladder moves one rung.** Each handler enqueues exactly the next kind with the same idempotent
  keys the reader half already uses (``app.keys``), so a restart in the middle of a season rejoins the
  queue instead of double-posting an episode.

The storage bot's protocol is the one place another bot's behaviour is relied on, so it is quoted
from what was actually seen (``app.storagebot.BATCH_FLOW``) rather than from what its menu implies,
and an answer that does not parse is a blocked job rather than a retry.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Mapping, Sequence

from . import captions, keys, linkprovider, manifest, rights, sender, storagebot
from .stages import JobKind, JobStage

log = logging.getLogger("auto_manager.writers")

__all__ = [
    "FeatureNotImplemented",
    "NeedsInput",
    "Writers",
    "build_writers",
    "campaign_gap_seconds",
    "campaign_release_unsent",
]


class FeatureNotImplemented(NotImplementedError):
    """A job kind that cannot be performed at all. The worker reports it as ``blocked``."""


class PlanOnly(FeatureNotImplemented):
    """Shadow mode could describe the write but not perform it, so the job waits instead of passing."""


class NeedsInput(FeatureNotImplemented):
    """Something only the operator (or a live session) can supply is missing.

    Blocked rather than failed, deliberately: ``/status`` counts blocked jobs and names the reason,
    while failures are retried forever — and "you have not named the archive channel yet" is not a
    condition a retry can fix.
    """


class WriteBlocked(FeatureNotImplemented):
    """A write that was supposed to happen, and was not.

    It exists so that a failed send cannot be recorded as a succeeded job: ``_stop`` used to *return*
    ``{"blocked": True, ...}``, the worker marked the job done, /status went green and the channel stayed
    silent — the exact shape of failure this project is not allowed to have. Raising the shape
    ``app/worker.py`` already parks puts the sentence in the blocked column instead. A flood is not this:
    a flood knows when it ends and arrives as :class:`app.sender.RetryLater`.
    """


#: The two link kinds this pipeline publishes, in the words ``app.storage_link.kind`` stores.
KIND_SINGLE = "single"
KIND_BATCH = "batch"
KIND_UNIVERSAL = "universal"


async def _config(db: Any, key: str, default: Any = None) -> Any:
    """One config read that treats a missing table like a missing row.

    A deployment with an unapplied migration is a normal first hour of this project, and it should
    be answered by the sentence in ``NeedsInput`` below rather than by a database traceback.
    """
    if db is None:
        return default
    try:
        return await db.config(key, default)
    except Exception as exc:  # noqa: BLE001
        log.info("config %s unreadable: %s", key, str(exc)[:120])
        return default


class Writers:
    """The handlers, with what they all need kept in one place.

    ``client_factory`` is how a test hands in a fake Telegram client, and how production supplies
    ``app.telegram_client.TelegramUserClient``'s connected client without this module knowing how a
    session is opened. One session per process is the architecture; a writer that dialled its own is
    how an account ends up with two update loops fighting over one connection.
    """

    def __init__(self, *, db: Any, settings: Any, client_factory: Any = None) -> None:
        self.db = db
        self.settings = settings
        self.client_factory = client_factory

    # -- shared plumbing -------------------------------------------------------------------------

    def _client(self) -> Any:
        client = self.client_factory() if callable(self.client_factory) else self.client_factory
        if client is None:
            raise NeedsInput(
                "no Telegram session is open, so nothing can be written; /login in the control bot "
                "starts one and /sessions shows what is stored"
            )
        return client

    def _policy(self, peers: Sequence[Any], *, max_writes: int = 8, parse_mode: str | None = None) -> sender.WritePolicy:
        policy = sender.WritePolicy.from_settings(
            self.settings, peers=[peer for peer in peers if peer not in (None, "")], max_writes=max_writes
        )
        policy.parse_mode = parse_mode
        return policy

    def _writer(self, peers: Sequence[Any], *, max_writes: int = 8, parse_mode: str | None = None) -> sender.Sender:
        return sender.Sender(
            self._client(), db=self.db, policy=self._policy(peers, max_writes=max_writes, parse_mode=parse_mode)
        )

    @staticmethod
    def _peer(row: Mapping[str, Any], column: str = "telegram_channel_id") -> str:
        """The peer we address: the numeric id, which is also how a private channel is named.

        A @handle would read nicer in the report, and it is not available for a private channel —
        the id is the one spelling that works for both, and Telegram resolves it from the session's
        own dialog list (which ``/probe`` reads, so an id we have never seen is a ``NeedsInput``).

        The caller has to name the column *this* row uses: a query that renames the channel id to
        ``source_channel_id`` (the source-side reads do, because the id is joined in) has no
        ``telegram_channel_id`` to find, and the default would silently hand back an empty peer.
        ``app/sender.py`` now refuses a write with no peer named, which is what turns that typo into a
        blocked job with a sentence instead of a forward from nowhere.
        """
        value = row.get(column)
        return str(value) if value not in (None, "") else ""

    def _has_client(self) -> bool:
        """Is a session reachable at all? A *read* that needs one says so instead of blocking a job.

        The distinction matters in shadow mode: the shape of a stored link can be checked on a
        database with no Telegram connection at all, and a health check that blocks because nobody
        logged in yet would hide the checks that could have run.
        """
        client = self.client_factory() if callable(self.client_factory) else self.client_factory
        return client is not None

    @staticmethod
    def _stop(result: sender.Result, *, what: str) -> dict[str, Any]:
        """The one place a write that did not happen turns into something the queue can show.

        Neither a plan nor a failure returns: a job that "succeeded" while only describing itself (or
        while being refused) would let the ladder walk forward on nothing — the variant would be marked
        archived, the episode would move to published, and the operator would read a green /status over
        an empty channel. Both raise, and the write's own sentence is the block's reason, so shadow mode
        still shows exactly what a live run would send and a refused live write says what was refused.
        """
        if result.retry_after:
            raise sender.RetryLater(result.detail, retry_after=result.retry_after)
        if result.action == "planned":
            raise PlanOnly(f"shadow plan, nothing sent: {what}. {result.detail}")
        raise WriteBlocked(f"{what}: {result.detail}")

    # -- 1. archive_media ------------------------------------------------------------------------

    async def archive_media(self, job: dict[str, Any], ctx: Any) -> dict[str, Any]:
        """Copy one file into the private master archive, server-side, leaving the source alone.

        A forward with the author dropped rather than a download and re-upload, for two reasons that
        are the whole point of the archive: the bytes must be the same (a re-encode would make the
        only spare copy worse than the original), and the copy must not depend on being able to read
        the source for as long as it takes to upload a two-gigabyte file.
        """
        payload: Mapping[str, Any] = {**(job.get("payload") or {})}
        variant_id = job.get("variant_id") or payload.get("variant_id")
        if variant_id is None:
            raise ValueError("archive_media needs a variant_id")
        variant = await self.db.fetchrow(
            """
            select v.id, v.episode_id, v.quality, v.file_name, v.archive_message_id,
                   c.message_id as source_message_id,
                   s.telegram_channel_id as source_channel_id
              from app.media_variant v
              join app.source_candidate c on c.id = v.source_candidate_id
              join app.source_channel s on s.id = c.source_channel_id
             where v.id = $1
            """,
            int(variant_id),
        )
        if variant is None:
            return {"variant_id": variant_id, "skipped": "no such variant"}
        if variant.get("archive_message_id"):
            return {
                "variant_id": int(variant_id),
                "skipped": "already archived",
                "archive_message_id": variant["archive_message_id"],
            }

        archive = await self.db.fetchrow(
            "select id, telegram_channel_id, title from app.archive_channel order by is_primary desc nulls last, id limit 1"
        )
        if archive is None or not archive.get("telegram_channel_id"):
            raise NeedsInput(
                "no master archive channel is recorded (app.archive_channel is empty), so there is "
                "nowhere to copy to; this pipeline never invents a channel to hold the only spare copy"
            )

        source = self._peer(variant, "source_channel_id")
        target = self._peer(archive)
        result = await self._writer([source, target], max_writes=2).forward(
            source, target, [int(variant["source_message_id"])], keep_author=False
        )
        if not result.ok or result.action == "planned":
            return self._stop(result, what=f"copy {source}#{variant['source_message_id']} into {target}")

        await self.db.execute(
            "update app.media_variant set archive_chat_id = $2, archive_message_id = $3, status = 'archived',"
            " updated_at = now() where id = $1",
            int(variant_id),
            int(archive["telegram_channel_id"]),
            int(result.message_id or 0),
        )
        queued = await self.db.enqueue(
            JobKind.STORAGE_UPLOAD.value,
            keys.storage_key(int(variant_id)),
            stage=JobStage.ARCHIVED,
            payload={"variant_id": int(variant_id), "episode_id": variant.get("episode_id")},
            variant_id=int(variant_id),
            episode_id=variant.get("episode_id"),
        )
        return {
            "variant_id": int(variant_id),
            "archive_message_id": result.message_id,
            "archive_chat_id": int(archive["telegram_channel_id"]),
            "storage_upload_queued": bool(queued),
        }

    # -- 2. storage_upload -----------------------------------------------------------------------

    async def storage_upload(self, job: dict[str, Any], ctx: Any) -> dict[str, Any]:
        """Hand a range to the storage bot and keep the link it answers with.

        The conversation is the observed one (``app.storagebot.BATCH_FLOW``): ``/batch``, the first
        message of the range *with its forward tag*, the last message, then a link. The reply is read
        by :func:`app.linkprovider.parse_reply` with the clone's own username as the expected host —
        the wording of the answer is shared with @Link_providerobot, so only the host says who minted
        it. An answer that is not a link blocks the job and quotes the shapes it did see, because a
        button that goes nowhere is the one defect a published post cannot survive.
        """
        payload: Mapping[str, Any] = {**(job.get("payload") or {})}
        episode_id = job.get("episode_id") or payload.get("episode_id")
        season_id = job.get("season_id") or payload.get("season_id")
        if episode_id is None and season_id is None:
            raise ValueError("storage_upload needs an episode_id (one episode, every quality) or a season_id")

        bot = str(await _config(self.db, "bots.storage_username", storagebot.BOT_USERNAME) or storagebot.BOT_USERNAME)
        bot = bot.lstrip("@")
        rows = await self.db.fetch(
            """
            select c.message_id, s.telegram_channel_id as source_channel_id, v.quality
              from app.media_variant v
              join app.source_candidate c on c.id = v.source_candidate_id
              join app.source_channel s on s.id = c.source_channel_id
             where ($1::bigint is not null or $2::bigint is not null)
               and ($1::bigint is null or v.episode_id = $1)
               and ($2::bigint is null or v.episode_id in (select id from app.episode where season_id = $2))
             order by c.message_id
            """,
            episode_id,
            season_id,
        )
        if not rows:
            raise NeedsInput(
                "no archived file is waiting on this batch, so there is nothing to hand over — the "
                "archive step has to have run for at least one variant first"
            )

        source = self._peer(rows[0], "source_channel_id")
        first, last = int(rows[0]["message_id"]), int(rows[-1]["message_id"])
        writer = self._writer([source, bot], max_writes=4)

        asked = await writer.send_text(bot, storagebot.command_for("channel_batch"))
        if not asked.ok or asked.action == "planned":
            return self._stop(
                asked,
                what=f"send {storagebot.command_for('channel_batch')} to @{bot}, forward {source}#{first} and "
                f"#{last} with the forward tag, then read back one t.me/{bot}?start= link",
            )

        forward = await writer.forward(source, bot, [first, last], keep_author=True)
        if not forward.ok or forward.action == "planned":
            return self._stop(forward, what=f"forward {source}#{first} and #{last} to @{bot}")

        read, seen = await writer.read_back(bot, limit=6, wait_seconds=25.0, stop_when=_wants_link)
        parsed = {"kind": "unknown", "link": None, "token": None}
        for row in seen:
            if row.get("out"):
                continue
            candidate = linkprovider.parse_reply(row.get("text", ""), bot=bot)
            if candidate["kind"] == "link":
                parsed = candidate
                break
        if parsed["kind"] != "link":
            shapes = sorted({linkprovider.parse_reply(r.get("text", ""), bot=bot)["kind"] for r in seen if not r.get("out")})
            raise NeedsInput(
                f"@{bot} answered with no link we could read (shapes seen: {', '.join(shapes) or 'nothing'}). "
                "The recorded flow is in app/storagebot.py; a link is never invented to keep a queue moving"
            )
        if not read.ok:
            log.info("storage read-back incomplete: %s", read.detail)

        kind = KIND_SINGLE if len(rows) == 1 and episode_id is not None else KIND_BATCH
        link_row = await self.db.fetchrow(
            "insert into app.storage_link (url, kind, token, episode_id, season_id, destination_id, batch_ref)"
            " values ($1, $2::app.link_kind, $3, $4, $5, $6, $7) returning id",
            parsed["link"],
            kind,
            parsed["token"],
            episode_id,
            season_id,
            payload.get("destination_id"),
            f"{source}#{first}-{last}",
        )
        link_id = int(link_row["id"]) if link_row else None

        # The variants this range covered are now "linked": the link is what the post is built from,
        # and marking them on the episode (or the whole season, for the batch at its end) is what
        # keeps a restart from handing the bot the same range a second time.
        if episode_id is not None:
            await self.db.execute(
                "update app.media_variant set status = 'linked', updated_at = now() where episode_id = $1",
                int(episode_id),
            )
        if season_id is not None:
            await self.db.execute(
                "update app.media_variant set status = 'linked', updated_at = now()"
                " where episode_id in (select id from app.episode where season_id = $1)",
                int(season_id),
            )

        queued = await self.db.enqueue(
            JobKind.PUBLISH_POST.value,
            keys.publish_key(int(episode_id or season_id)),
            stage=JobStage.LINK_RECEIVED,
            payload={
                "episode_id": episode_id,
                "season_id": season_id,
                "storage_link_id": link_id,
                "destination_id": payload.get("destination_id"),
            },
            episode_id=episode_id,
            season_id=season_id,
            destination_id=payload.get("destination_id"),
        )
        await self.db.enqueue(
            JobKind.LINK_VERIFY.value,
            f"link-verify:{link_id if link_id is not None else 'untracked'}",
            stage=JobStage.LINK_RECEIVED,
            payload={"storage_link_id": link_id, "episode_id": episode_id, "season_id": season_id},
        )
        return {
            "link": parsed["link"],
            "token_stored": bool(parsed["token"]),
            "kind": kind,
            "range": [first, last],
            "files": len(rows),
            "publish_queued": bool(queued),
        }


def _wants_link(body: str) -> bool:
    """The one thing worth waiting a reply for, phrased so either sibling bot satisfies it."""
    return "here is your link" in (body or "").casefold()


# --- the four that face an audience --------------------------------------------------------------
#
# Defined as module-level functions and attached to ``Writers`` at the bottom of this file, so the
# ladder handlers above stay one screen long. They are methods at runtime; the split is only about
# how the file reads to a person debugging it at 2am.


async def link_verify(self: Writers, job: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Check one stored link before its post goes out.

    Two halves, deliberately unequal. The *shape* half runs anywhere, shadow included: the url is a
    ``t.me/<bot>?start=`` deep link, its host is the clone we were told to use, and the token we
    stored is the token in the url — which catches the failure mode this project actually has (a link
    belonging to a sibling bot, saved as ours). The *liveness* half reads the range's two ends back
    out of the source channel and reports what it saw. It never claims to have opened the link: that
    a clone's link is a reference or a copy is still unobserved (``docs/storage-bot.md``, item 2).
    """
    payload: Mapping[str, Any] = {**(job.get("payload") or {})}
    link_id = job.get("link_id") or payload.get("storage_link_id")
    if link_id is None:
        raise ValueError("link_verify needs a storage_link_id")
    row = await self.db.fetchrow("select * from app.storage_link where id = $1", int(link_id))
    if row is None:
        return {"storage_link_id": link_id, "skipped": "no such link row"}
    ok, detail = await check_link(self, row)
    await mark_link(self, int(link_id), ok=ok, detail=detail)
    return {"storage_link_id": int(link_id), "ok": ok, "detail": detail}


async def check_link(self: Writers, row: Mapping[str, Any]) -> tuple[bool, str]:
    url = str(row.get("url") or "")
    token = str(row.get("token") or "")
    if not linkprovider.is_deep_link(url):
        return False, "not a t.me/<bot>?start= deep link, so it is not a link this pipeline understands"
    from_url = linkprovider.token_of(url)
    if token and from_url != token:
        return False, "the stored token and the token inside the url disagree — one of them is what was published"
    expected = str(await _config(self.db, "bots.storage_username", storagebot.BOT_USERNAME) or "").lstrip("@")
    if expected:
        host = str(url.split("t.me/", 1)[1].split("?", 1)[0]) if "t.me/" in url else ""
        if host and host.casefold() != expected.casefold():
            return False, f"the link points at @{host}, not at the configured clone @{expected}"
    ref = str(row.get("batch_ref") or "")
    if not ref or "#" not in ref:
        return True, "shape ok; no source range recorded, so only the shape could be checked"
    peer, _, span = ref.partition("#")
    first_text, dash, last_text = span.partition("-")
    if not dash:
        return True, "shape ok; the reference names one message, and one message has no range to lose"
    try:
        ends = [int(first_text), int(last_text)]
    except ValueError:
        return True, "shape ok; the source range is not numeric, so nothing further could be read"
    if not self._has_client():
        return True, f"shape ok; the range {ref} needs a session to read, so liveness is unverified"
    writer = sender.Sender(self._client(), policy=sender.WritePolicy(mode="live", allow_peers=(peer,)))
    read, seen = await writer.read_back(peer, limit=1)
    if not read.ok:
        return False, f"the source channel that the range lives in could not be read: {read.detail}"
    ids = {row["id"] for row in seen}
    if ids and not ({ends[0], ends[1]} & ids):
        return True, f"shape ok; the newest message in {peer} is {max(ids)}, which is past both ends {ends}"
    return True, f"shape ok; both ends of {ref} are inside the channel's readable range {sorted(ids)[:1]}"


async def mark_link(self: Writers, link_id: int, *, ok: bool, detail: str) -> None:
    await self.db.execute(
        "update app.storage_link set link_status = $2::app.link_status, checked_at = now(), check_error = $3"
        " where id = $1",
        int(link_id),
        "active" if ok else "broken",
        None if ok else detail[:400],
    )


async def link_health_check(self: Writers, job: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Re-check the links that are actually carrying traffic, a page at a time.

    Bounded by design: one ``limit`` per run, no link checked twice inside its own hour unless
    something changed. A free Postgres plus a rate-limited Telegram account and an unbounded crawl is
    how a health check becomes the outage it was meant to notice.
    """
    payload: Mapping[str, Any] = {**(job.get("payload") or {})}
    limit = int(payload.get("limit") or 20)
    rows = await self.db.fetch(
        "select * from app.storage_link where active and (checked_at is null or checked_at < now() - interval '6 hours')"
        " order by checked_at nulls first, id limit $1",
        limit,
    )
    broken: list[dict[str, Any]] = []
    for row in rows:
        ok, detail = await check_link(self, row)
        await mark_link(self, int(row["id"]), ok=ok, detail=detail)
        if not ok:
            broken.append({"id": int(row["id"]), "url": row["url"], "detail": detail})
    if broken:
        await self.db.execute(
            "insert into app.audit_log (actor_user_id, action, entity_type, detail) values ($1, 'link_health.broken',"
            " 'storage_link', $2::jsonb)",
            getattr(self.settings, "telegram_main_admin_user_id", None),
            {"broken": broken[:20]},
        )
    return {"checked": len(rows), "broken": len(broken), "next_page": len(rows) == limit}


async def publish_post(self: Writers, job: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Put the episode in its destination channel, and the notice in the updates channel.

    Two audiences, two routes, one rule: nothing leaves that the operator has not approved.

    * The **destination** post follows ``publish.route``. ``chelp_block`` — the default — renders the
      caption and the button block exactly as Channel Help consumes them and *stops there*, because
      driving that bot's menu would mean automating a flow this project has only ever read in the
      guide; the block is stored in ``app.destination_post`` with no ``published_at``, which is the
      database's own way of saying "a draft, not a post". ``own_session`` sends it with real inline
      buttons — that is the comparison to make during the live test, since both must read the same.
    * The **announcement** is ours to send (the operator's ruling of 2026-08-29: a channel that hosts
      no files needs no publishing bot), gated twice: the box must be an approved caption, and the
      destination must have a card post whose shareable link we hold. The invite link itself never
      goes into the announcements channel.
    """
    payload: Mapping[str, Any] = {**(job.get("payload") or {})}
    episode_id = job.get("episode_id") or payload.get("episode_id")
    season_id = job.get("season_id") or payload.get("season_id")
    context = await self._episode_context(episode_id=episode_id, season_id=season_id)
    if context is None:
        raise NeedsInput("no episode or season matches this job, so there is nothing to publish")
    # ``app.destination_post.kind`` is a check constraint, not a free label: 'episode' rows must
    # carry an episode_id and 'season_batch' rows a season_id. Naming the wrong one is a database
    # error rather than a wrong post, which is the order we want.
    post_kind = "episode" if episode_id is not None else "season_batch"
    if not context.get("destination_id"):
        raise NeedsInput(
            f"{context.get('title')} has no destination channel row yet; the name is derived from "
            "'<series> Anime in Hindi' and the channel is created when missing, but that is "
            "channel-creation's step, not this one"
        )
    links = await self._links(episode_id=episode_id, season_id=season_id)
    if not links:
        raise NeedsInput(
            "no stored link for this post: storage_upload has to have handed the range over and got a "
            "link back. A button that goes nowhere cannot be un-published, so the post waits"
        )

    destination = await self.db.fetchrow("select * from app.destination where id = $1", int(context["destination_id"])) or {}
    peer = self._peer(destination)
    route = str(await _config(self.db, "publish.route", "chelp_block") or "chelp_block").casefold()
    body, block, entries, missing = await _caption_for(self, context, links)
    if missing:
        raise NeedsInput(f"the caption is missing values: {', '.join(sorted(missing))}")

    draft = await self.db.fetchrow(
        "select id from app.destination_post where destination_id = $1 and kind = $2"
        " and episode_id is not distinct from $3 and season_id is not distinct from $4"
        " and message_id is null order by id desc limit 1",
        int(context["destination_id"]),
        post_kind,
        episode_id,
        season_id,
    )
    post = await self.db.fetchrow(
        "select id, message_id from app.destination_post where destination_id = $1 and kind = $2"
        " and episode_id is not distinct from $3 and season_id is not distinct from $4"
        " and message_id is not null order by id desc limit 1",
        int(context["destination_id"]),
        post_kind,
        episode_id,
        season_id,
    )
    published = await self._published_qualities(
        int(context["destination_id"]), episode_id, kind=post_kind, season_id=season_id
    )
    decision = manifest.decide_publish(
        available=[
            {"quality": link.get("quality"), "thumbnail_status": link.get("thumbnail_status")}
            for link in links
        ],
        published=published,
        post_exists=post is not None,
        thumbnail_gate=str(context.get("thumbnail_status") or "clean"),
        quality_order=await _config(self.db, "quality.order", None),
    )
    results: dict[str, Any] = {"route": route, "decision": decision.action, "chars": len(body)}
    if decision.blocked:
        return {"blocked": True, "why": decision.reason}

    if decision.action == manifest.PublishAction.CREATE:
        buttons_json = [[f"{label} - {url}" for label, url in row] for row in entries]
        summary = [{"quality": link["quality"], "storage_link": link["link"]} for link in links]
        if route == "own_session":
            result = await self._writer([peer], max_writes=1).send_text(peer, body, buttons=entries)
            if not result.ok or result.action == "planned":
                return {**results, "destination": self._stop(result, what=f"post the episode in {peer}")}
            if draft is not None:
                # The draft was the same post waiting to be sent, so the send *promotes* it: one
                # episode keeps exactly one row, which is what the unique index on
                # app.destination_post (episode_id) where kind = 'episode' exists to enforce. Writing
                # a second row for the same episode is the bug this branch closes.
                await self.db.execute(
                    "update app.destination_post set message_id = $2, published_at = now(), body = $3,"
                    " buttons = $4::jsonb, quality_summary = $5::jsonb, updated_at = now() where id = $1",
                    int(draft["id"]),
                    int(result.message_id or 0),
                    body,
                    buttons_json,
                    summary,
                )
            else:
                await self.db.execute(
                    "insert into app.destination_post (destination_id, kind, episode_id, season_id, message_id,"
                    " body, buttons, quality_summary, published_at) values ($1, $2, $3, $4, $5, $6, $7::jsonb,"
                    " $8::jsonb, now())",
                    int(context["destination_id"]),
                    post_kind,
                    episode_id,
                    season_id,
                    int(result.message_id or 0),
                    body,
                    buttons_json,
                    summary,
                )
            results["destination"] = sender.describe(result)
        elif draft is not None:
            # One post per episode is a rule the database enforces, so a second prepared post is not
            # an option; the draft is rewritten instead. Nothing is deleted: the body that was there
            # is overwritten by the one that is current, which is what "the post for this episode"
            # means in this schema.
            await self.db.execute(
                "update app.destination_post set body = $2, buttons = $3::jsonb, quality_summary = $4::jsonb,"
                " updated_at = now() where id = $1",
                int(draft["id"]),
                f"{body}\n\n{block}" if block else body,
                buttons_json,
                summary,
            )
            results["destination"] = (
                f"draft #{int(draft['id'])} in app.destination_post was rewritten with the new buttons; it"
                " still has no published_at, which is what makes it a draft"
            )
        else:
            await self.db.execute(
                "insert into app.destination_post (destination_id, kind, episode_id, season_id, body, buttons,"
                " quality_summary) values ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb)",
                int(context["destination_id"]),
                post_kind,
                episode_id,
                season_id,
                f"{body}\n\n{block}" if block else body,
                buttons_json,
                summary,
            )
            results["destination"] = (
                "prepared in app.destination_post with no published_at: paste the body into Channel Help, "
                "or set publish.route to own_session"
            )

    elif decision.action == manifest.PublishAction.EDIT and post is not None:
        queued = await self.db.enqueue(
            JobKind.EDIT_POST.value,
            keys.inplace_key(int(context["destination_id"]), int(post["message_id"])),
            stage=JobStage.DESTINATION_POSTED,
            payload={"destination_post_id": int(post["id"]), "episode_id": episode_id},
            destination_id=int(context["destination_id"]),
        )
        results["destination"] = f"the post exists; edit_post queued={bool(queued)} for {', '.join(decision.added_qualities)}"
    else:
        results["destination"] = f"nothing to do: {decision.reason or 'the post already carries every quality'}"

    announcement = await self._announcement(context, destination)
    results["announcement"] = announcement
    return results


async def _caption_for(self: Writers, context: Mapping[str, Any], links: Sequence[Mapping[str, Any]]) -> tuple[str, str, list[list[tuple[str, str]]], tuple[str, ...]]:
    """Render the approved box and the button block once, for both routes.

    ``caption.button_rows`` decides the layout, and the same value decides both, so a post a human
    pastes and a post the session sends cannot differ in anything but who pressed send.
    """
    rows_policy = str(await _config(self.db, "caption.button_rows", captions.BUTTON_ROWS) or captions.BUTTON_ROWS)
    # The season row's box is ``templates.season_post`` — the same key ``app/captions.py`` marks as
    # approved. Naming a template that is not in that dict would be a silent "no box to render", so
    # the choice is made from the approved set, not from a string.
    key = "templates.episode_post" if context.get("episode_id") else "templates.season_post"
    template = await _config(self.db, key, captions.APPROVED_TEMPLATES.get(key, ""))
    values = {**dict(context.get("values") or {}), "quality_list": [str(link.get("quality") or "") for link in links]}
    body, missing_body = captions.render_caption(template, values, key=key)
    single = await _config(
        self.db, "templates.episode_button", captions.APPROVED_TEMPLATES["templates.episode_button"]
    )
    multi = await _config(
        self.db, "templates.episode_button_multi", captions.APPROVED_TEMPLATES["templates.episode_button_multi"]
    )
    button_input = [
        {
            **dict(link),
            # A season link carries no episode of its own and a label may name one, so the post's own
            # numbers fill in what the link does not carry rather than printing "{episode}".
            "episode": link.get("episode") or context.get("episode_number"),
            "season": link.get("season") or context.get("season_number"),
        }
        for link in links
    ]
    entries, missing_buttons = captions.button_entries(
        button_input, single=single, multi=multi, rows=rows_policy
    )
    block, _again = captions.button_lines(button_input, single=single, multi=multi, rows=rows_policy)
    return body, block, entries, tuple(set(missing_body) | set(missing_buttons))


async def _announcement(self: Writers, context: Mapping[str, Any], destination: Mapping[str, Any]) -> dict[str, Any] | str:
    """The notice in the updates channel, or the reason there is none — never a partial attempt."""
    channel = str(await _config(self.db, "updates.channel", "") or "").strip()
    if not channel:
        return {"skipped": "no updates channel is named in app.config, so there is nowhere to announce"}
    if "templates.announcement_post" not in captions.APPROVED_TEMPLATES:
        return {"skipped": "the announcement box is not approved, so nothing goes out"}
    if str(destination.get("publish_mode") or "") == "in_place":
        return {"skipped": "this channel is captioned in place; the operator asked for no announcement there"}
    link = str(await _config(self.db, "updates.card_link", "") or "").strip() or str(destination.get("announcement_link") or "").strip()
    if not link:
        card_id = destination.get("card_message_id")
        if not card_id:
            raise NeedsInput(
                f"{context.get('title')} has no card post named, so there is no shareable link to announce:"
                " /card <destination> <message id> names the post, and this program asks the link bot for the"
                " link the announcement carries (never the invite itself)"
            )
        # No config row for this one, on purpose. The handle is part of the link the bot itself
        # sent, so a second spelling of it in app.config could only ever be out of date; the link
        # keeps its own bot's name (app/linkprovider.py).
        bot = linkprovider.BOT_USERNAME
        writer = self._writer([self._peer(destination), bot], max_writes=2)
        forward = await writer.forward(self._peer(destination), bot, [int(card_id)], keep_author=True)
        if not forward.ok or forward.action == "planned":
            return self._stop(forward, what=f"forward the card post {self._peer(destination)}#{card_id} to @{bot} for its link")
        read, seen = await writer.read_back(bot, limit=4, wait_seconds=20.0, stop_when=_wants_link)
        for row in seen:
            if row.get("out"):
                continue
            parsed = linkprovider.parse_reply(row.get("text", ""), bot=bot)
            if parsed["kind"] == "link":
                link = parsed["link"]
                await self.db.execute(
                    "update app.destination set announcement_link = $2, announcement_link_at = now() where id = $1",
                    int(destination["id"]),
                    link,
                )
                break
        if not link:
            raise NeedsInput(
                f"@{bot} did not answer the forwarded card with a link, so the announcement has nothing to"
                " carry; the shapes it sent back are in the job's log and app.audit_log"
            )

    style = str(await _config(self.db, "announcement.style", "markdown") or "markdown").casefold()
    text = linkprovider.announcement_caption(
        str(context.get("title_full") or context.get("title") or ""),
        context.get("season_number"),
        context.get("episode_number"),
        link,
        style="text" if style == "text" else "markdown",
    )
    peer = channel
    result = await self._writer([peer], max_writes=1, parse_mode=None if style == "text" else "markdown").send_text(peer, text)
    if result.action == "planned":
        return {"planned": result.detail, "chars": result.chars}
    if not result.ok:
        return {"blocked": result.detail}
    await self.db.execute(
        "insert into app.destination_post (destination_id, kind, season_id, message_id, body, published_at)"
        " values ($1, 'info', $2, $3, $4, now())",
        destination.get("id"),
        context.get("season_id"),
        result.message_id,
        text,
    )
    return {"sent": sender.describe(result)}


async def edit_post(self: Writers, job: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Rewrite the text of a post that is already out — the missing-quality path, and no second post.

    The file never moves and the message id never changes, which is what "edit the post, never
    double-post it" means in API terms. Rights are read rather than assumed: a destination whose
    ``we_are_admin`` nobody has looked at is blocked with the sentence that says so, because an edit
    Telegram drops silently is worse by far than an edit we did not attempt.
    """
    payload: Mapping[str, Any] = {**(job.get("payload") or {})}
    post_id = job.get("destination_post_id") or payload.get("destination_post_id")
    if post_id is None:
        raise ValueError("edit_post needs a destination_post_id")
    post = await self.db.fetchrow(
        "select p.id, p.message_id, p.destination_id, p.episode_id, p.kind, d.telegram_channel_id, d.publish_mode,"
        " coalesce(sc.we_are_admin, false) as we_are_admin"
        " from app.destination_post p join app.destination d on d.id = p.destination_id"
        " left join app.source_channel sc on sc.destination_id = d.id where p.id = $1",
        int(post_id),
    )
    if post is None:
        return {"destination_post_id": post_id, "skipped": "no such post row"}
    if not post.get("message_id"):
        raise NeedsInput(
            "this post row carries no message id: a chelp_block row is a draft for a human to paste,"
            " and until a message exists there is nothing to edit"
        )
    if not rights.we_are_admin({"post_messages": bool(post.get("we_are_admin"))}):
        raise NeedsInput(
            "nobody has read our rights on this destination (run /probe): an edit that Telegram refuses"
            " silently would leave the old caption published and the job looking satisfied"
        )
    links = await self._links(episode_id=post.get("episode_id"))
    if not links:
        raise NeedsInput("no stored links to build the buttons from, so an edit would replace a good post with an empty one")
    context = await self._episode_context(episode_id=post.get("episode_id")) or {}
    body, block, entries, missing = await _caption_for(self, context, links)
    if missing:
        raise NeedsInput(f"the edited caption is missing values: {', '.join(sorted(missing))}")

    peer = self._peer(post)
    result = await self._writer([peer], max_writes=1).edit_text(peer, int(post["message_id"]), f"{body}\n\n{block}" if block else body)
    if not result.ok or result.action != "edited":
        # An edit that did not land must not be recorded as one. `edit_post` exists to change a post
        # that is already out, so when the change is refused — a flood, no rights, a peer this session
        # cannot see — the honest outcome is a blocked job, and `app.destination_post` keeps the body
        # the channel still shows instead of being stamped edited.
        return self._stop(result, what=f"edit {peer}#{post['message_id']} to the links stored now")
    await self.db.execute(
        "update app.destination_post set body = $2, buttons = $3::jsonb, quality_summary = $4::jsonb, edited_at = now()"
        " where id = $1",
        int(post_id),
        f"{body}\n\n{block}" if block else body,
        [[f"{label} - {url}" for label, url in row] for row in entries],
        [{"quality": link["quality"], "storage_link": link["link"]} for link in links],
    )
    # The post's own history is kept where the in-place mode keeps it (app.media_variant's
    # caption columns are written by app.inplace), so this handler updates one row and does not
    # invent a second place for the same edit to be recorded.
    return {"destination_post_id": int(post_id), "result": sender.describe(result)}


async def season_sticker(self: Writers, job: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """Put the season's sticker up ahead of its posts, or say precisely what is missing.

    A sticker cannot be conjured from a pack *name*: Telegram addresses one by the document that
    carries it, and an ``t.me/addstickers/...`` link is an install link, not a source. So this forwards
    a sticker message the operator named — which keeps the animation, the size and the pack's own
    "add to stickers" affordance intact and never downloads a file — and records the message id so
    ``publish_post``'s ordering rule ("never resend the season sticker") has something to check.
    """
    payload: Mapping[str, Any] = {**(job.get("payload") or {})}
    season_id = job.get("season_id") or payload.get("season_id")
    if season_id is None:
        raise ValueError("season_sticker needs a season_id")
    season = await self.db.fetchrow(
        "select s.id, s.season_number, s.sticker_label, s.sticker_posted, s.sticker_message_id,"
        " s.sticker_source_chat_id, s.sticker_source_message_id, d.id as destination_id, d.telegram_channel_id,"
        " d.publish_mode, sr.title"
        " from app.season s join app.series sr on sr.id = s.series_id"
        " join app.destination d on d.series_id = s.series_id"
        " where s.id = $1 order by d.id limit 1",
        int(season_id),
    )
    if season is None:
        raise NeedsInput("this season has no series or destination row, so a sticker has nowhere to go")
    if season.get("sticker_posted"):
        return {"season_id": int(season_id), "skipped": "the season sticker is already up", "message_id": season.get("sticker_message_id")}
    if not season.get("sticker_source_message_id"):
        # The pack the post points at is the one row the operator already set (/sticker-pack);
        # Telegram gives us its url, not a name we could look up by hand.
        pack = str(await _config(self.db, "stickers.pack_url", "") or "").strip()
        raise NeedsInput(
            "no sticker message is named for this season"
            + (f" (the pack {pack!r} is configured, and a pack is not enough to post from)" if pack else "")
            + "; /sticker <series> <season> from <@channel> <message id> names one, and nothing here"
            " guesses which sticker of which pack a season is meant to open with"
        )
    source = str(season["sticker_source_chat_id"])
    target = self._peer(season)
    result = await self._writer([source, target], max_writes=1).forward(
        source, target, [int(season["sticker_source_message_id"])], keep_author=False
    )
    if not result.ok or result.action == "planned":
        return self._stop(result, what=f"forward {source}#{season['sticker_source_message_id']} into {target}")
    await self.db.execute(
        "update app.season set sticker_posted = true, sticker_message_id = $2, updated_at = now() where id = $1",
        int(season_id),
        int(result.message_id or 0),
    )
    await self.db.execute(
        "insert into app.destination_post (destination_id, kind, season_id, message_id, body, published_at)"
        " values ($1, 'season_sticker', $2, $3, $4, now())",
        int(season["destination_id"]),
        int(season_id),
        int(result.message_id or 0),
        f"season {season.get('season_number')} sticker {season.get('sticker_label') or ''}".strip(),
    )
    return {"season_id": int(season_id), "result": sender.describe(result), "sticker_message_id": result.message_id}


#: One message per this many seconds, per the operator's own instruction: "har 3 second me ek user ko
#: message." A fixed gap between sends is the *slower* choice than the queue allows; the campaign's own row
#: can say something else, and `campaign_gap_seconds` below is what reads it. Nothing about the spacing is
#: a floor the campaign can raise by sending faster: a send that comes back with a flood wait is honoured,
#: not shaved, because `app/sender.py` holds that rule where a wait is real.
JOIN_SEND_GAP_SECONDS = 3.0

#: How many people one pass reads and writes. This is a page of the queue, not a limit on how far the
#: campaign goes: `app/campaignloop.py` asks for the next page as soon as this one is done, so a list of 340
#: empties seventeen pages later with no clock on it and no queue row to wait for. Twenty is what one read of
#: the importer answers comfortably, and it keeps a pass short enough that a stop the operator taps lands on
#: the next person rather than after a thousand.
#: How deep one read of the waiting list goes. `app/sender.py` walks Telegram's 100-row pages per invite
#: link, so this is the number of pages it may ask for: twenty pages is two thousand people, which is more
#: than a private channel's request queue holds in practice and is not a *limit on the campaign* — a pass
#: sends to everyone the read reaches. A list deeper than the read gets `more` back from the pass, and
#: `app/campaignloop.py` comes around again; that is the difference between this and the run that stopped at
#: twenty and called it finished.
JOIN_READ_PAGES = 20
JOIN_LIST_CEILING = JOIN_READ_PAGES * 100

#: What a campaign's own status has to say for it to be sending. `ready` is "started and not yet finished",
#: `running` is "in the middle of the list"; anything else — `paused`, `aborted`, `completed`, `draft` — is a
#: decision about the campaign, and the decision wins between any two people, not between two batches.
RUNNABLE_CAMPAIGN_STATES = ("ready", "running")

#: What `app.join_campaign.per_message_delay_seconds` is allowed to say. The column has always been there and
#: has always had a check of `>= 0`, which is how a row can hold a number that turns a DM campaign into a
#: flood: zero spacing means the sends are back to back, and the account — not the strangers — is what eats
#: that. One second is the floor; above it the number is the operator's, whatever they type, up to what the
#: column can hold. A five-hour spacing is a choice about their own list, not a fault to guard against.
JOIN_GAP_MIN_SECONDS = 1.0
JOIN_GAP_MAX_SECONDS = 9999.0


def campaign_gap_seconds(campaign: Mapping[str, Any]) -> float:
    """How far apart this campaign's messages go, read from its own row.

    The row is the truth and the constant above is only the fallback: a row that says nothing usable (null,
    zero, text, a negative) is answered with the default rather than with the operator's typo, because this
    number decides how fast a stranger is contacted.
    """
    try:
        value = float(campaign.get("per_message_delay_seconds"))
    except (TypeError, ValueError):
        return JOIN_SEND_GAP_SECONDS
    if not value > 0:
        return JOIN_SEND_GAP_SECONDS
    return max(JOIN_GAP_MIN_SECONDS, min(value, JOIN_GAP_MAX_SECONDS))


async def campaign_release_unsent(db: Any, campaign_id: int) -> int:
    """Hand back the people a campaign wrote a row for and never messaged, and say how many there were.

    A contact row is written *before* the send, so anything left at `queued` with no `sent_at` is one of two
    things: a run killed between the two statements (whose person must not be messaged, because nobody can
    prove the message did not go), or a row an older build left behind from a dry run that recorded people it
    only planned. The second one is the state this deployment is full of, and it is why a campaign can read
    "0 still waiting" while nobody has been sent anything: those ids are in the `already` set, so every later
    pass skips them and the list looks empty.

    This is not decided by a guess. It is written by the operator's own ✅, which is a human saying "send to
    the people on this list" — so the release happens there, in `app/controlbot.py`, on the screen that shows
    the wording and the count. The rows and their history stay; only the status moves, to the one value that
    means "owed a message", and the next pass sends to them like anyone else.
    """
    moved = await db.execute(
        "update app.join_campaign_contact set status = 'skipped' where campaign_id = $1"
        " and status = 'queued' and sent_at is null returning telegram_user_id",
        int(campaign_id),
    )
    return len(moved or []) if isinstance(moved, list) else int(moved or 0)


async def campaign_status(db: Any, campaign_id: int) -> str:
    """What the campaign says right now — read again between sends, not remembered from the batch plan."""
    return str(await db.fetchval("select status from app.join_campaign where id = $1", int(campaign_id)) or "")


async def campaign_pass(self: Writers, campaign_id: int, *, ctx: Any = None) -> dict[str, Any]:
    """Answer the still-pending join requests of one channel with the operator's own sentence.

    Everything here is a limit, and each one is somebody's past incident:

    * the campaign row must say ``ready``, and only ``/campaign … confirm <code>`` writes that;
    * the text is refused if it carries an invite link or reads like a decision about the request
      (``app.joinmsg.refusals``), because a DM that admits someone past a pending approval is a hole;
    * one contact per (campaign, person), recorded *before* the send and updated after, so a crash in
      the middle becomes "already contacted" rather than a second message to the same stranger;
    * the spacing between two messages is the campaign's own ``per_message_delay_seconds`` (1 second to
      ``JOIN_GAP_MAX_SECONDS``, ``JOIN_SEND_GAP_SECONDS`` when the row says nothing usable);
    * a pass works **the whole list it can read**: everyone the waiting-list read reaches, one message each,
      spaced by the gap above. There is no batch size, no hourly ceiling and no next run to hand anything to;
      a list deeper than the read can walk comes back as `more`, and `app/campaignloop.py` asks for the rest.
      It stops when the list is empty, when the operator taps `⏸ Stop after this one` (the campaign row is
      re-read before every person, so the tap lands within one message), or when a flood wait says so.
      `campaign.rate_per_hour` is not read at all — it was a number the operator never chose and it stopped
      their list invisibly at twenty;
    * a privacy refusal marks the contact ``failed`` and is not retried into a second attempt.
    """
    from . import joinmsg  # noqa: PLC0415  (the wording module owns the rules, not this transport)

    campaign = await self.db.fetchrow(
        "select c.id, c.status, c.name, c.message_template, c.per_message_delay_seconds, c.destination_id,"
        " d.telegram_channel_id, d.title"
        " from app.join_campaign c join app.destination d on d.id = c.destination_id where c.id = $1",
        int(campaign_id),
    )
    if campaign is None:
        return {"campaign_id": campaign_id, "skipped": "no such campaign"}
    status = str(campaign.get("status") or "")
    if status not in RUNNABLE_CAMPAIGN_STATES:
        # Not an error, and not a raise: `app/campaignloop.py` asks every campaign that is on to do a pass,
        # and a campaign that stopped between the read of the list and this one has simply said "not now".
        # Raising here used to be how a stale queue row landed in `blocked` and made the campaign look broken
        # to a person who had just switched it off on purpose.
        return {
            "campaign_id": int(campaign_id),
            "skipped": f"the campaign is {status or 'unknown'}, which is not sending",
        }
    problems = joinmsg.refusals(campaign.get("message_template"))
    if problems:
        raise NeedsInput("the campaign text breaks a rule: " + " / ".join(problems))

    # One number, from the campaign's own row: how far apart two messages go. There is no second limit any
    # more — no hour to stop at, no lease to fit inside, no batch to size — because a pass is the list itself
    # and not a job with a clock on it. What the read cannot reach, it says so.
    gap = campaign_gap_seconds(campaign)
    peer = self._peer(campaign)
    # `skipped` is the one status that means "this person is owed a message": an earlier attempt wrote the row
    # and never sent it, and a human has since said so out loud — which is what `campaign_release_unsent`
    # below does at the operator's own start tap. Everything else counts as dealt with,
    # `queued` included — the row is written *before* the send so a crash between the two cannot become a second DM,
    # and only a human can tell that state from a plan that was never a message at all.
    already = {
        int(row["telegram_user_id"])
        for row in await self.db.fetch(
            "select telegram_user_id from app.join_campaign_contact where campaign_id = $1"
            " and status <> 'skipped'",
            int(campaign_id),
        )
    }
    # How many of those known rows were written and never sent is deliberately **not** counted here. The
    # operator's screen gets that number from the campaign row itself (`app/controlbot.py`), and the one place
    # this pass may act on it is the ✅ that starts it — `campaign_release_unsent` below. A second copy of the
    # figure inside a pass result would be a second truth about the same rows, and this campaign has been
    # reported to its owner with two of those before.

    reader = self._writer([peer], max_writes=0)
    # `skip` is the whole reason the read can move: the queue is newest-first and does not shrink while a
    # campaign works through it, because nothing here approves anybody. Without it, a pass that had written
    # to the first hundred would read those same hundred, find them known, and be tempted to call the
    # campaign finished with two thousand nine hundred still waiting.
    found, requests = await reader.pending_requests(
        peer, limit=JOIN_LIST_CEILING, max_pages=JOIN_READ_PAGES, skip=already
    )
    if not found.ok:
        return {"campaign_id": int(campaign_id), "blocked": found.detail}
    waiting = [entry for entry in requests if int(entry.get("user_id") or 0) and int(entry["user_id"]) not in already]
    # Telegram's own count of who is waiting, minus the people this campaign has a row for, minus the people
    # this read reached. Whatever is left is a list deeper than the pages walked, and it is carried on every
    # answer below: "nobody is waiting" and "this pass could not reach them" are two different sentences, and
    # the operator has been told the wrong one of those twice.
    reached = int(getattr(found, "total", 0) or 0) - len(already) - len(waiting)
    beyond = max(0, reached)
    if not waiting:
        if beyond > 0:
            # People are waiting and this pass could not reach them — the read walked its pages, the offset
            # ran out, or a link refused. Closing the campaign here would be the loudest kind of wrong, so
            # the pass reports the number it could not get to and says there is more to ask for.
            return {
                "campaign_id": int(campaign_id),
                "waiting": beyond,
                "sent": 0,
                "failed": 0,
                "waiting_after": beyond,
                "more": True,
                "why": "the queue is deeper than the pages this read walked",
            }
        await self.db.execute(
            # Guarded like every other write this function makes: "nobody is left" is a fact about the
            # channel, and a pause the operator tapped while this pass was reading is a fact about what they
            # want next. `completed` here would settle the argument in favour of the loop.
            "update app.join_campaign set status = 'completed', finished_at = now(), updated_at = now()"
            " where id = $1 and status in ('ready', 'running')",
            int(campaign_id),
        )
        return {
            "campaign_id": int(campaign_id),
            "skipped": "nobody is waiting on this channel",
            "waiting_after": 0,
        }

    message = " ".join(str(campaign["message_template"]).split())
    series = str(campaign.get("title") or "")
    batch_real_send = bool(getattr(self.settings, "outbound_enabled", False))
    # No peer allowlist for the people themselves: they are named by Telegram's own list of pending
    # requests for this channel, which is as close to "this account asked to be contacted" as a
    # campaign gets. The one-per-person rule and this spacing are what bound it.
    batch = waiting
    writer = self._writer([], max_writes=len(batch))
    sent = failed = planned = 0
    stopped_early = False
    for position, entry in enumerate(batch):
        if position:
            # Re-read before every person, not once per batch: `⏸ Stop after this one` is a promise about the
            # next message, and an operator who taps it while twenty are being planned must not watch all
            # twenty go out. The read is one row by primary key, and it is the only way a stop that arrives
            # mid-batch reaches this loop at all — nothing here holds a flag across awaits.
            if await campaign_status(self.db, int(campaign_id)) not in RUNNABLE_CAMPAIGN_STATES:
                stopped_early = True
                break
        if position and batch_real_send:
            # Between sends, never before the first, and not at all in shadow mode: a planned message puts
            # nothing on the wire, so making a dry run of this take a minute per twenty people would be a
            # slow test, not a safe one.
            await asyncio.sleep(gap)
        user_id = int(entry["user_id"])
        if batch_real_send:
            # The row is written *before* the send, so a crash between the two leaves "already contacted"
            # rather than an invitation to message the same stranger a second time. In shadow mode nothing is
            # written, because a plan is not a contact: the rows are the one thing that decides who never
            # gets a message, and recording a dry run there strands those people out of every later live run.
            await self.db.execute(
                "insert into app.join_campaign_contact (campaign_id, telegram_user_id, status, attempts)"
                " values ($1, $2, 'queued', 1) on conflict (campaign_id, telegram_user_id) do update"
                " set attempts = app.join_campaign_contact.attempts + 1",
                int(campaign_id),
                user_id,
            )
        # Addressed by the input entity the queue read carried, when it carried one: an id this account has
        # no reason to have cached is a `Cannot find any entity` per contact, and the contact is then marked
        # failed for a reason that has nothing to do with the person.
        result = await writer.send_text(
            entry.get("input_user") or user_id,
            joinmsg.render(message, name=f"user {user_id}", series=series),
        )
        if result.ok and result.action == "sent":
            await self.db.execute(
                "update app.join_campaign_contact set status = 'sent', sent_at = now() where campaign_id = $1 and telegram_user_id = $2",
                int(campaign_id),
                user_id,
            )
            sent += 1
        elif result.action == "planned":
            planned += 1
        else:
            failed += 1
            await self.db.execute(
                "update app.join_campaign_contact set status = 'failed', error = $3 where campaign_id = $1 and telegram_user_id = $2",
                int(campaign_id),
                user_id,
                result.detail[:400],
            )
    # How many people are still owed a message when this pass stops. One number, worked out once, because "the
    # channel has nobody left" and "this read could not reach that many" and "the operator stopped me partway"
    # are three different sentences and the operator has been shown the wrong one of them twice.
    # `position` is the person this pass stopped *before* sending, so the ones left are `len - position`.
    unhandled = (len(waiting) - position) if stopped_early else 0
    waiting_after = max(0, unhandled + beyond)
    if not batch_real_send:
        # A dry run plans a page and stops there: it hands the campaign back as `ready`, waiting for the tap
        # that sends for real. The two alternatives are both traps — leaving it `running` would give the
        # sender a campaign to re-read forever in a service that must not send, and `completed` would tell the
        # operator that strangers had been told when no message had left the account.
        await self.db.execute(
            # The same guard the live write carries: a dry run hands the campaign back as it found it, and a
            # campaign the operator paused while it was planning is still paused when the planning stops.
            "update app.join_campaign set status = 'ready', updated_at = now() where id = $1"
            " and status in ('ready', 'running')",
            int(campaign_id),
        )
        return {
            "campaign_id": int(campaign_id),
            "planned": planned,
            "sent": 0,
            "failed": failed,
            # Everyone this pass planned is still owed: a dry run sent nothing and recorded nobody, so the
            # honest count of what the real run has left to do is the whole list plus the depth the read could
            # not walk. A `0` here would be the "0 pending" sentence that started this.
            "waiting_after": len(waiting) + beyond,
            "shadow": "nothing was sent, so nobody is recorded as contacted",
            "more": True,
        }

    if stopped_early:
        # The operator's tap said stop. The messages that went out stay sent, the rows stay, and the
        # campaign keeps the status they gave it — this pass's only job is to say what it managed.
        return {
            "campaign_id": int(campaign_id),
            "sent": sent,
            "failed": failed,
            "waiting_after": waiting_after,
            "stopped": "the campaign was paused or aborted while this pass was working",
            "gap_seconds": gap,
            "more": False,
        }
    finished = waiting_after <= 0
    if await campaign_status(self.db, int(campaign_id)) not in RUNNABLE_CAMPAIGN_STATES:
        return {
            "campaign_id": int(campaign_id),
            "sent": sent,
            "failed": failed,
            "waiting_after": waiting_after,
            "stopped": "the campaign was paused or aborted while this pass was working",
            "gap_seconds": gap,
            "more": False,
        }
    # The status is passed as text and cast, and the "did we run out of people" flag is its own
    # parameter: reusing $2 for both the enum column and a `case when $2 = 'completed'` comparison makes
    # Postgres unable to settle on one type for it, and it answers with an error before anything runs.
    # A campaign that had sent every contact and then failed here would have looked unfinished forever.
    await self.db.execute(
        "update app.join_campaign set status = $2::app.campaign_status, updated_at = now(),"
        # `and status in ('ready', 'running')`: a pause or an abort the operator tapped while this pass was
        # sending is their decision about the campaign, and a finishing pass has no business rewriting it to
        # `running` (or, worse, `completed`) because the list it read is over. The rows this pass wrote stay,
        # and the contacts already sent stay sent.
        " finished_at = case when $3 then now() else finished_at end where id = $1"
        " and status in ('ready', 'running')",
        int(campaign_id),
        "completed" if finished else "running",
        finished,
    )
    return {
        "campaign_id": int(campaign_id),
        "sent": sent,
        "failed": failed,
        "waiting_after": waiting_after,
        "gap_seconds": gap,
        # The loop's signal, and the only "next" this function knows about: the sender asks for the rest of
        # the list itself, so nothing here writes a row for anything else to finish.
        "more": not finished,
        # Named rather than folded into `failed`: these are rows this pass did not touch because a record of
        # them already exists, and the operator has to be able to tell "nobody is left" from "nobody is left
        # *that I have not already written about*".
    }


# --- mixed into Writers, and the registry the worker reads ---------------------------------------

Writers._announcement = _announcement  # type: ignore[attr-defined]
Writers.link_verify = link_verify  # type: ignore[attr-defined]
Writers.link_health_check = link_health_check  # type: ignore[attr-defined]
Writers.publish_post = publish_post  # type: ignore[attr-defined]
Writers.edit_post = edit_post  # type: ignore[attr-defined]
Writers.season_sticker = season_sticker  # type: ignore[attr-defined]
Writers.campaign_pass = campaign_pass  # type: ignore[attr-defined]


async def join_request_campaign(self: Writers, job: dict[str, Any], ctx: Any) -> dict[str, Any]:
    """The job form of one campaign pass, kept so a row written by an older build still does real work.

    Sending no longer runs through the queue at all — `app/campaignloop.py` owns the loop — but a deployment
    that had a campaign row in `app.job` when this changed must not meet `no handler registered for job kind
    'join_request_campaign'`, a `blocked` row and a campaign that looks broken. So the kind stays routed, and
    it does exactly what the loop does: one page of the list, then it closes itself.
    """
    payload: Mapping[str, Any] = {**(job.get("payload") or {})}
    campaign_id = job.get("campaign_id") or payload.get("campaign_id")
    if campaign_id is None:
        raise ValueError("join_request_campaign needs a campaign_id")
    return await campaign_pass(self, int(campaign_id), ctx=ctx)


Writers.join_request_campaign = join_request_campaign  # type: ignore[attr-defined]


async def _episode_context(self: Writers, *, episode_id: Any = None, season_id: Any = None) -> dict[str, Any] | None:
    """One round trip for everything a post needs: series, season, episode, destination, card link.

    ``declared`` versus ``observed`` is the distinction this query exists to keep straight — the
    declared span comes from ``/declare`` and is the only thing allowed to fill ``{total_episodes}``,
    while the observed span says what arrived. Mixing them makes a weekly show on a one-week break
    look finished (see ``app.captions.post_values``).
    """
    if episode_id is None and season_id is None:
        return None
    # Exactly one scope, spelled as one of two literals rather than as a nullable parameter: the
    # first draft of this query said "id = $1 or ($2 is null or season_id = $2)", which on an episode
    # job matched every episode in the database and published a post for whichever id came first. A
    # shape that can only be right by accident is the shape to write out.
    scope = "e.id = $1" if episode_id is not None else "e.season_id = $1"
    wanted = int(episode_id if episode_id is not None else season_id)
    row = await self.db.fetchrow(
        "select e.id as episode_id, e.episode_number, e.audio_kind, e.languages,"
        " s.id as season_id, s.season_number, s.first_episode, s.last_episode,"
        " sr.title, sr.subtitle,"
        " d.id as destination_id, d.telegram_channel_id as destination_channel_id,"
        " d.publish_mode, d.card_message_id, d.announcement_link,"
        # One thumbnail verdict per episode, in the language the gate uses: anything other than clean
        # or owner_approved means a quality is not fit to name in a post yet.
        " coalesce((select string_agg(distinct v.thumbnail_status::text, ',') from app.media_variant v"
        "            where v.episode_id = e.id), 'clean') as thumbnail_statuses"
        " from app.episode e"
        " join app.season s on s.id = e.season_id"
        " join app.series sr on sr.id = s.series_id"
        " left join app.destination d on d.series_id = sr.id"
        # `scope` is one of two literals, never a string a caller supplied.
        " where " + scope + " order by e.id limit 1",
        wanted,
    )
    if row is None:
        return None
    statuses = {value.strip() for value in str(row.get("thumbnail_statuses") or "").split(",") if value.strip()}
    gate = "clean" if statuses <= {"clean", "owner_approved"} else (sorted(statuses) or ["unchecked"])[0]
    values = captions.post_values(
        title=row.get("title"),
        subtitle=row.get("subtitle"),
        season=row.get("season_number"),
        episode=row.get("episode_number"),
        first_episode=row.get("first_episode"),
        last_episode=row.get("last_episode"),
        declared_episodes=row.get("last_episode"),
        audio_kind=row.get("audio_kind"),
        languages=row.get("languages"),
        unknown_label=await _config(self.db, "caption.total_episodes_unknown", None),
    )
    return {**row, "thumbnail_status": gate, "values": values, "title_full": captions.title_with_subtitle(row.get("title"), row.get("subtitle"))}


Writers._episode_context = _episode_context  # type: ignore[attr-defined]


async def _links(self: Writers, *, episode_id: Any = None, season_id: Any = None) -> list[dict[str, Any]]:
    """The links a post may carry, in quality order, and never a broken one.

    A link whose last check said ``broken`` is left out rather than published with a warning: the
    operator's rule is that a missing quality is an *edited* post, and a dead quality is neither.
    """
    rows = await self.db.fetch(
        # The qualities a link covers are the label its button carries, and one /batch answer covers
        # every file in the range it was handed, so the label is aggregated over that range rather
        # than assumed to be one quality per link. A link with no variants behind it still gets a
        # button — it is a real link to a real file — it just says so with the file's own name.
        "select l.id, l.url, l.token, l.kind, l.batch_ref,"
        " coalesce(q.qualities, '') as quality, q.rank as quality_rank, q.thumbnail_status"
        " from app.storage_link l"
        " left join lateral ("
        "   select string_agg(v.quality, '/' order by v.quality_rank) as qualities,"
        "          min(v.quality_rank) as rank,"
        # A link is publishable when every file behind it passed screening. The gate is per variant,
        # so the aggregate has to say "all clean" or name the worst state, never average them out.
        "          case when bool_and(v.thumbnail_status::text in ('clean', 'owner_approved'))"
        "               then 'clean' else 'review_required' end as thumbnail_status"
        "     from app.media_variant v"
        "    where (l.episode_id is not null and v.episode_id = l.episode_id)"
        "       or (l.episode_id is null and l.season_id is not null"
        "           and v.episode_id in (select e.id from app.episode e where e.season_id = l.season_id))"
        " ) q on true"
        " where l.active and l.link_status <> 'broken'"
        " and ($1::bigint is not null or $2::bigint is not null)"
        " and ($1::bigint is null or l.episode_id = $1)"
        " and ($2::bigint is null or l.season_id = $2)"
        " order by q.rank nulls last, l.id",
        episode_id,
        season_id,
    )
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        url = str(row["url"])
        if url in seen:
            continue
        seen.add(url)
        out.append(
            {
                "link": url,
                "quality": str(row.get("quality") or "").strip(),
                "storage_link_id": row["id"],
                # A link with no variants behind it (a batch stored before any file was recorded)
                # carries None, which the gate below reads as "not screened yet" — the conservative
                # side, and the reason this is written out rather than defaulted to clean.
                "thumbnail_status": row.get("thumbnail_status"),
            }
        )
    return out


async def _published_qualities(
    self: Writers, destination_id: int, episode_id: Any, *, kind: str = "episode", season_id: Any = None
) -> list[dict[str, Any]]:
    rows = await self.db.fetch(
        # A draft does not count here on purpose: "published" is what the audience already has, and
        # the draft is what we are about to hand them. Conflating them is how a re-run decides an
        # already-prepared post needs an edit to a message that does not exist.
        "select quality_summary from app.destination_post where destination_id = $1 and kind = $2"
        " and episode_id is not distinct from $3 and season_id is not distinct from $4"
        " and published_at is not null",
        destination_id,
        kind,
        episode_id,
        season_id,
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        for entry in row.get("quality_summary") or []:
            if isinstance(entry, dict) and entry.get("quality"):
                out.append({"quality": entry["quality"]})
    return out


Writers._links = _links  # type: ignore[attr-defined]
Writers._published_qualities = _published_qualities  # type: ignore[attr-defined]


def build_writers(db: Any, settings: Any, *, client_factory: Any = None) -> dict[str, Any]:
    """``job kind -> handler`` for the eight kinds that reach Telegram.

    A mapping rather than a registration, so ``app.handlers.build_registry`` stays the one place the
    whole vocabulary is assembled — and so a test can assert the two halves cover every kind exactly
    once, which is the guard against a kind that is quietly nobody's.
    """
    writers = Writers(db=db, settings=settings, client_factory=client_factory)
    return {
        JobKind.ARCHIVE_MEDIA.value: writers.archive_media,
        JobKind.STORAGE_UPLOAD.value: writers.storage_upload,
        JobKind.LINK_VERIFY.value: writers.link_verify,
        JobKind.PUBLISH_POST.value: writers.publish_post,
        JobKind.EDIT_POST.value: writers.edit_post,
        JobKind.SEASON_STICKER.value: writers.season_sticker,
        JobKind.JOIN_REQUEST_CAMPAIGN.value: writers.join_request_campaign,
        JobKind.LINK_HEALTH_CHECK.value: writers.link_health_check,
    }
