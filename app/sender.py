"""One writer for every outbound Telegram act, so the rules are written down once.

Until 2026-08-29 this program only ever *read* Telegram. Eight job kinds waited on the same missing
muscle — a place where "send", "edit", "forward" and "read the pending join requests" live next to
each other, because every one of them needs the same four guards and a guard duplicated in eight
handlers is a guard that differs in eight handlers.

The guards, in order of how much they have cost somebody somewhere:

* **plan before act.** :class:`WritePolicy` carries a mode. In ``plan`` every method returns what it
  *would* do and the client is never touched — that is what ``APP_MODE=shadow`` means for writes, and
  it is the reason a first live day cannot become a first live disaster.
* **stop, never wait it off.** A :class:`~telethon.errors.FloodWaitError` becomes a ``blocked``
  result carrying ``retry_after``. Nobody sleeps through a flood wait here and nobody retries a
  shorter interval: the account is already flagged, and "no rate-limit evasion" is the operator's
  rule, not a suggestion. The caller fails the job with that number so the queue, not a loop, owns
  the delay.
* **this module cannot delete, revoke, ban or re-promote.** See :data:`FORBIDDEN_METHODS`. It is a
  list of names rather than a comment because a test checks the source of this file for them, and
  zero-deletion is a promise the project has kept since the first draft.
* **the audience is named before it is addressed.** Every write takes a peer, and
  :meth:`WritePolicy.may` refuses anything outside the peers that the job was built for. A handler
  that computes a destination by name-matching is a handler that posts to the wrong 30k people.

What this module deliberately is *not*: an abstraction over the whole API. It exposes the five calls
this pipeline has actually been designed around — send text, send with buttons, edit text, forward,
read pending join requests — and nothing else. A wrapper nobody needs is a wrapper nobody tests.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence

log = logging.getLogger("auto_manager.sender")

__all__ = [
    "FORBIDDEN_METHODS",
    "resolve_peer",
    "RetryLater",
    "MAX_MESSAGE_CHARS",
    "Result",
    "Sender",
    "WriteBudget",
    "WritePolicy",
    "buttons_from_rows",
    "describe",
    "mapping_summary",
]

#: Telegram's own limit for one message. Over it the send is refused rather than cut: half a caption
#: on a published post is worse than no post, and the difference is one line in the operator's day.
MAX_MESSAGE_CHARS = 4096

#: Names that must never appear as a call in this file, nor in any handler that reaches Telegram
#: through it. ``revoke=True`` is here because deleting for *everyone* is the one undoable act.
FORBIDDEN_METHODS: tuple[str, ...] = (
    "delete_messages",
    "delete_chat",
    "delete_folder",
    "edit_admin_permissions",
    "import_chat_invite",
    "leave_chat",
    "remove_chat_user",
    "revoke_invite",
    "revoke=True",
)


class RetryLater(Exception):
    """Telegram asked for a wait, so this job is *re-queued* rather than retried or abandoned.

    The worker turns it into a failure with the delay Telegram named (and nothing but that delay):
    sleeping in the handler would tie up a lease and a retry loop, and shortening the wait would be
    the evasion the operator ruled out. The queue owns the clock.
    """

    def __init__(self, message: str, *, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = max(60, int(retry_after))


class WriteBudget(Exception):
    """One run has used the number of outbound messages it was allowed."""


@dataclass(frozen=True, slots=True)
class Result:
    """What a write did, in the shape a job handler and an audit row both want."""

    ok: bool
    action: str
    message_id: int | None = None
    detail: str = ""
    chars: int = 0
    buttons: int = 0
    #: Seconds Telegram asked us to wait. Present on ``blocked`` for a flood, and the caller's job
    #: is failed with it — the queue owns the delay so no loop ever owns it.
    retry_after: int | None = None
    #: How many are waiting in total, from Telegram's own counters rather than from the length of a page.
    #: A read that filled one page of a three-thousand-person queue reports 3 000 here and 100 rows, and
    #: only the first number is safe to show an operator or to decide "is the campaign finished" with.
    total: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "action": self.action,
            "message_id": self.message_id,
            "detail": self.detail[:400],
            "chars": self.chars,
            "buttons": self.buttons,
            "retry_after": self.retry_after,
            "total": self.total,
        }


@dataclass
class WritePolicy:
    """The three questions every outbound call is asked before it is made."""

    mode: str = "plan"
    #: The peers this run may write to, as ids or @handles. Anything else is refused, including a
    #: peer a handler "meant" — a typo in a channel id is not caught by good intentions.
    allow_peers: tuple[Any, ...] = ()
    max_writes: int = 8
    #: How the text reaches Telegram. ``None`` means "no parser": our captions are plain text with
    #: unicode bold, and letting markdown interpret a filename's underscores would silently italicise
    #: half a post. The announcement box is the one exception, and its handler says why.
    parse_mode: str | None = None
    writes: int = field(default=0, repr=False)

    @classmethod
    def from_settings(cls, settings: Any, *, peers: Sequence[Any], max_writes: int | None = None) -> "WritePolicy":
        """``live`` only when the deployment says so; everything else is a plan.

        ``outbound_enabled`` is the single switch the operator already trusts (it is what
        ``APP_MODE=shadow`` is *for*), so this derives from it instead of inventing a second flag
        that could disagree with the first.
        """
        live = bool(getattr(settings, "outbound_enabled", False))
        return cls(
            mode="live" if live else "plan",
            allow_peers=tuple(peers),
            max_writes=int(max_writes if max_writes is not None else 8),
        )

    @property
    def live(self) -> bool:
        return self.mode == "live"

    def may(self, peer: Any) -> bool:
        if not self.allow_peers:
            return True
        wanted = {str(peer).casefold(), _numeric(peer)}
        for allowed in self.allow_peers:
            if str(allowed).casefold() in wanted or _numeric(allowed) in wanted:
                return True
        return False

    def take(self) -> None:
        self.writes += 1
        if self.writes > self.max_writes:
            raise WriteBudget(f"this run may send {self.max_writes} message(s); it has used them")


def resolve_peer(peer: Any) -> Any:
    """Cast a peer to the spelling Telethon resolves.

    Telethon turns the *integer* ``-1001234567890`` into a ``PeerChannel``, and treats the *string*
    ``"-1001234567890"`` as a username to look up — which fails with "Cannot find any entity
    corresponding to …". This project reads channel ids out of jsonb config rows and command arguments,
    where they arrive as text, so the cast happens here, once, at the boundary: a writer can hand a
    string or an int and both mean the same channel.
    """
    if peer is None:
        return ""
    text = str(peer).strip()
    if text.startswith("-") and text[1:].isdigit():
        return int(text)
    if text.isdigit():
        return int(text)
    return text


def _numeric(peer: Any) -> str:
    try:
        return str(int(str(peer).lstrip("@")))
    except (TypeError, ValueError):
        return ""


def buttons_from_rows(rows: Sequence[Sequence[tuple[str, str]]]) -> list[Any]:
    """``[[("480p", url), ("720p", url)], [("season", url)]]`` -> Telethon button rows.

    One nested list per row is the whole layout language, and it is the documented shape of Channel
    Help's block too (`&&` joins a row), so a caption this module builds reads the same in either
    place. An empty row is dropped instead of sent: an empty row is a gap in the post.
    """
    from telethon import Button  # noqa: PLC0415  (telethon is optional at import time everywhere)

    out: list[Any] = []
    for row in rows or ():
        built = [Button.url(str(label), str(url)) for label, url in row if str(url or "").strip()]
        if built:
            out.append(built)
    return out


def describe(result: Result) -> str:
    """One line for a job result and for the operator's report."""
    where = f" message_id={result.message_id}" if result.message_id else ""
    size = f" {result.chars} chars" if result.chars else ""
    marks = f" {result.buttons} button(s)" if result.buttons else ""
    tail = f" — {result.detail}" if result.detail else ""
    return f"{result.action}{where}{size}{marks}{tail}"


class Sender:
    """Five verbs, each carrying the guards. See the module docstring for why there are five.

    The client is injected rather than built here: one logged-in session per process is the
    architecture (`app/telegram_client.py`), and a writer that opens its own connection is how an
    account gets two update loops fighting over one session.
    """

    def __init__(
        self,
        client: Any,
        *,
        policy: WritePolicy | None = None,
        db: Any = None,
        actor_user_id: int | None = None,
    ) -> None:
        self.client = client
        self.policy = policy or WritePolicy()
        self.db = db
        self.actor_user_id = actor_user_id
        #: Everything this writer did, planned or real, for the caller to keep in `app.audit_log`.
        self.journal: list[dict[str, Any]] = []

    # -- the guards, in one place ---------------------------------------------------------------

    def _refuse(self, action: str, peer: Any, text: str, why: str) -> Result:
        record = {"action": action, "peer": str(peer), "ok": False, "detail": why, "chars": len(text or "")}
        self.journal.append(record)
        log.warning("sender refused %s to %s: %s", action, peer, why)
        return Result(ok=False, action="blocked", detail=why, chars=len(text or ""))

    def _gate(self, action: str, peer: Any, text: str, *, read: bool = False) -> Result | None:
        """The refusals, before any await. Returns None when the call may go ahead.

        `read` is the second half of what a shadow deployment is allowed to do. The plan short-circuit is
        there so that no *message* can leave the process while `APP_MODE` says shadow, and it is applied to
        reads as well if the caller does not say otherwise — which is exactly how a campaign plan came to
        print "0 request(s) are pending right now": the read was never sent, `rows` was empty by
        construction, and an empty list of pending requests looked like a fact about the channel. A read is
        not a write; "nothing is sent" is the promise, not "nothing is asked".
        """
        if peer in (None, ""):
            return self._refuse(action, peer, text, "no peer named; a write without a destination is refused")
        if not self.policy.may(peer):
            return self._refuse(
                action,
                peer,
                text,
                "this peer is not in the run's allowed list, so the job that asked for it is the bug",
            )
        if len(text or "") > MAX_MESSAGE_CHARS:
            return self._refuse(
                action, peer, text, f"{len(text)} chars is over Telegram's {MAX_MESSAGE_CHARS}-character limit"
            )
        if not self.policy.live and not read:
            # The plan is the answer, not an apology: it is what the operator reads before a live day.
            # A plan names the peer as it was written and resolves nothing — shadow mode must not be
            # able to fail because a connection is missing, and the two spellings read the same anyway.
            self.journal.append({"action": action, "peer": str(peer), "ok": True, "detail": "planned", "chars": len(text or "")})
            return Result(
                ok=True,
                action="planned",
                detail=f"would {action} to {peer} (APP_MODE is not live, so nothing was sent)",
                chars=len(text or ""),
            )
        return None

    async def _target(self, peer: Any, *, force: bool = False) -> tuple[Any, str | None]:
        """The entity Telethon should be handed, or the reason this session cannot reach it.

        Two jobs, both learned from the way this deployment fails rather than from the docs: cast the
        id to the one form Telethon resolves (see :func:`resolve_peer`), and resolve it through the
        session's own entity lookup. The second is what turns "we have never met this channel" into a
        sentence about the channel instead of a Telethon ValueError deep inside a send — and it is a
        read, so it costs no write budget and never happens in shadow mode.
        """
        target = resolve_peer(peer)
        if not target:
            return target, "no peer was named"
        if not (self.policy.live or force):
            return target, None
        try:
            entity = await self.client.get_entity(target)
        except Exception as exc:  # noqa: BLE001  (a peer that will not resolve is a blocked job)
            mapped = _map_error(exc)
            detail = mapped[1] if mapped else f"{type(exc).__name__}: {str(exc)[:160]}"
            return target, f"this session cannot see {peer} ({detail})"
        return entity, None

    async def _call(
        self, action: str, peer: Any, coro: Awaitable[Any], *, text: str, buttons: int = 0, read: bool = False
    ) -> Result:
        if not read:
            # A read spends no budget, and must not be refused for want of it: a campaign plan needs to look
            # at the queue without touching the one number that decides whether messages go out.
            try:
                self.policy.take()
            except WriteBudget as exc:
                return self._refuse(action, peer, text, str(exc))
        try:
            sent = await asyncio.wait_for(coro, timeout=_TIMEOUT)
        except asyncio.TimeoutError:
            return Result(
                ok=False,
                action="blocked",
                detail=f"{action} did not answer in {_TIMEOUT:.0f}s; the job is re-queued rather than retried inline",
                retry_after=int(_TIMEOUT),
                chars=len(text or ""),
            )
        except Exception as exc:  # noqa: BLE001  (mapped below, never swallowed)
            mapped = _map_error(exc)
            if mapped is not None:
                log.info("sender %s to %s blocked: %s", action, peer, mapped[1])
                return Result(
                    ok=False,
                    action="blocked",
                    detail=mapped[1],
                    retry_after=mapped[0],
                    chars=len(text or ""),
                )
            log.exception("sender %s to %s failed", action, peer)
            return Result(
                ok=False,
                action="failed",
                detail=f"{type(exc).__name__}: {str(exc)[:200]}",
                chars=len(text or ""),
            )
        message_id = getattr(sent, "id", None)
        if message_id is None and isinstance(sent, Sequence):
            message_id = getattr(sent[0], "id", None) if len(sent) else None
        self.journal.append(
            {
                "action": action,
                "peer": str(peer),
                "ok": True,
                "detail": "sent",
                "message_id": int(message_id) if message_id is not None else None,
                "chars": len(text or ""),
                "buttons": buttons,
            }
        )
        await self._audit(action, peer, message_id)
        return Result(
            ok=True,
            action=action,
            message_id=int(message_id) if message_id is not None else None,
            chars=len(text or ""),
            buttons=buttons,
        )

    async def _audit(self, action: str, peer: Any, message_id: Any) -> None:
        """One row per real write. A plan is not audited, because nothing happened."""
        if self.db is None:
            return
        try:
            await self.db.execute(
                "insert into app.audit_log (actor_user_id, action, entity_type, entity_id, detail)"
                " values ($1, $2, 'telegram_write', $3, $4::jsonb)",
                self.actor_user_id,
                f"sender.{action}",
                int(message_id) if message_id is not None else None,
                {"peer": str(peer)},
            )
        except Exception as exc:  # noqa: BLE001  (an audit failure must not unsent a post)
            log.warning("could not audit %s to %s: %s", action, peer, str(exc)[:120])

    # -- the five verbs -------------------------------------------------------------------------

    async def send_text(
        self,
        peer: Any,
        text: str,
        *,
        buttons: Sequence[Sequence[tuple[str, str]]] | None = None,
        reply_to: int | None = None,
    ) -> Result:
        """Post text, optionally under inline buttons, to one named peer.

        ``buttons`` rows are built by :func:`buttons_from_rows`; a send carrying more than one
        *media* would lose them (Telegram allows no buttons on an album — the same limit Channel
        Help's guide states), and this writer only ever sends one message at a time, so that trap
        does not open here.
        """
        refused = self._gate("sent", peer, text)
        if refused is not None:
            return refused
        rows = buttons_from_rows(buttons or ())
        kwargs: dict[str, Any] = {"link_preview": True}
        if self.policy.parse_mode:
            kwargs["parse_mode"] = self.policy.parse_mode
        if reply_to:
            kwargs["reply_to"] = reply_to
        if rows:
            kwargs["buttons"] = rows
        target, problem = await self._target(peer)
        if problem is not None:
            return self._refuse("sent", peer, text, problem)
        return await self._call(
            "sent",
            peer,
            self.client.send_message(target, text, **kwargs),
            text=text,
            buttons=sum(len(r) for r in rows),
        )

    async def edit_text(self, peer: Any, message_id: int, text: str) -> Result:
        """Replace a message's text — the in-place half of the caption rule.

        Only the caption changes; the file stays where it is. That is the whole point: a post edited
        in place is the same post, so no second copy of an episode ever reaches a channel and no
        message id that somebody saved stops meaning what it meant.
        """
        refused = self._gate("edited", peer, text)
        if refused is not None or message_id in (None, ""):
            return refused or self._refuse("edited", peer, text, "no message id to edit")
        kwargs: dict[str, Any] = {}
        if self.policy.parse_mode:
            kwargs["parse_mode"] = self.policy.parse_mode
        target, problem = await self._target(peer)
        if problem is not None:
            return self._refuse("edited", peer, text, problem)
        return await self._call(
            "edited", peer, self.client.edit_message(target, int(message_id), text, **kwargs), text=text
        )

    async def forward(
        self,
        from_peer: Any,
        to_peer: Any,
        message_ids: Sequence[int] | int,
        *,
        keep_author: bool = True,
    ) -> Result:
        """Server-side move of messages that already exist. Nothing is downloaded or re-uploaded.

        ``keep_author`` is the flag this project cares about, because it splits the two uses:
        the storage bot's `/batch` flow *asks* for the forwarded tag ("With Forward Tag"), while
        the private master archive wants a clean copy of the file. Telethon spells the clean one
        ``drop_author=True``; this wrapper names it after what it keeps, so a caller cannot invert
        the boolean by accident at 2am.
        """
        ids = [int(x) for x in (message_ids if isinstance(message_ids, (list, tuple)) else [message_ids])]
        refused = self._gate("forwarded", to_peer, f"forward {len(ids)} message(s)")
        if refused is not None:
            return refused
        kwargs: dict[str, Any] = {"from_peer": from_peer}
        if not keep_author:
            kwargs["drop_author"] = True
        source, source_problem = await self._target(from_peer)
        destination, destination_problem = await self._target(to_peer)
        problem = source_problem or destination_problem
        if problem is not None:
            return self._refuse("forwarded", to_peer, f"forward {len(ids)} message(s)", problem)
        kwargs["from_peer"] = source
        return await self._call(
            "forwarded",
            to_peer,
            self.client.forward_messages(destination, ids, **kwargs),
            text=f"forward {ids}",
        )

    async def read_back(
        self,
        peer: Any,
        *,
        limit: int = 4,
        wait_seconds: float = 0.0,
        stop_when: Callable[[str], bool] | None = None,
    ) -> tuple[Result, list[dict[str, Any]]]:
        """Newest-first read of one chat, optionally polled until a predicate is satisfied.

        This is how a bot's answer is caught without a webhook: poll a few times, keep every message
        as text, and let the *caller* decide which one answers it. The predicate is passed in rather
        than baked in here because the two bots we talk to answer with the same sentence, and which
        sentence belongs to which bot is `app.storagebot` / `app.linkprovider`'s business, not the
        transport's.
        """
        target, problem = await self._target(peer)
        if problem is not None:
            return Result(ok=False, action="blocked", detail=problem), []
        deadline = time.monotonic() + max(0.0, wait_seconds)
        seen: list[dict[str, Any]] = []
        while True:
            try:
                async for message in self.client.iter_messages(target, limit=limit):
                    seen.append(
                        {
                            "id": int(getattr(message, "id", 0) or 0),
                            "text": " ".join(str(getattr(message, "text", "") or "").split()),
                            "out": bool(getattr(message, "out", False)),
                        }
                    )
            except Exception as exc:  # noqa: BLE001  (a read that fails is a blocked job, not a crash)
                mapped = _map_error(exc)
                detail = mapped[1] if mapped else f"{type(exc).__name__}: {str(exc)[:160]}"
                return (
                    Result(ok=False, action="blocked" if mapped else "failed", detail=f"could not read {peer}: {detail}"),
                    seen,
                )
            body = " \n ".join(row["text"] for row in seen if not row["out"])
            if stop_when is None or stop_when(body) or time.monotonic() >= deadline:
                return (Result(ok=True, action="read", detail=f"{len(seen)} message(s) read from {peer}"), seen)
            await asyncio.sleep(min(1.5, max(0.2, deadline - time.monotonic())))

    async def pending_requests(
        self,
        peer: Any,
        *,
        limit: int = 100,
        skip: Sequence[int] = (),
        max_pages: int = 6,
        max_links: int = 12,
    ) -> tuple[Result, list[dict[str, Any]]]:
        """Who is waiting to be let in, read from the invite links the requests came through.

        ``messages.getChatInviteImporters`` answers "who is waiting **on this link**". Asked with no link
        at all, it comes back empty for the setup almost every channel has — a private channel whose
        requests all sit on its primary `+ABCDEF` link — and a queue of twenty people was reported to the
        operator as "0 request(s) are pending". So the read starts from the channel's own
        `full_chat.exported_invite` (whose `requested` field is Telegram's pending count for that link),
        then walks the other exported links the admins made, each of which can hold its own queue. The
        linkless query is still tried, last, because a public channel has no exported primary link and may
        answer it; it is no longer the only thing asked.

        The count the operator is shown is never the length of a page: `ChatInviteImporters.count` and the
        per-link `requested` numbers are the totals, and they ride back on the result as ``total``.
        Otherwise a queue of 3 000 with 100 on the first page would say "100", and "everyone is done"
        would be inferred from a page boundary.

        ``skip`` exists for the size of a real queue: a campaign that already wrote to the first hundred
        must not read the same hundred, find them known, and conclude nobody is waiting. The read pages
        forward past the ids it is given, up to ``max_pages`` pages per link.
        """
        from datetime import datetime, timezone  # noqa: PLC0415  (only the pagination offsets need it)

        # `read=True` so a shadow deployment still answers, and `force=True` so the id becomes an entity
        # this session has actually seen — an unresolvable channel must read as a sentence about the
        # channel, not as an empty queue.
        refused = self._gate("read_requests", peer, "", read=True)
        target, problem = await self._target(peer, force=True)
        if refused is not None:
            return refused, []
        if problem is not None:
            return Result(ok=False, action="blocked", detail=problem), []

        from telethon import functions, types  # noqa: PLC0415

        known: set[int] = {int(one) for one in skip}
        want = max(1, int(limit))
        rows: list[dict[str, Any]] = []
        seen: set[int] = set()
        totals: list[int] = []
        notes: list[str] = []
        asked = 0

        try:
            full = await asyncio.wait_for(
                self.client(functions.channels.GetFullChannelRequest(channel=target)), timeout=_TIMEOUT
            )
        except asyncio.TimeoutError:
            return Result(ok=False, action="failed", detail=f"the channel itself did not answer in {_TIMEOUT:.0f}s"), []
        except Exception as exc:  # noqa: BLE001 - one unread channel is a sentence, never a crash
            return Result(ok=False, action="failed", detail=f"the channel itself could not be read: {_reason(exc)}"), []
        exported = getattr(getattr(full, "full_chat", None), "exported_invite", None)
        links: list[str] = []
        if exported is not None:
            totals.append(int(getattr(exported, "requested", 0) or 0))
            links.extend(_link_spellings(getattr(exported, "link", None)))
        if not links:
            # The links the admins made by hand. `getExportedChatInvites` needs an admin to list them
            # *for*, which is this account, and each entry carries its own pending count.
            try:
                listing = await asyncio.wait_for(
                    self.client(
                        functions.messages.GetExportedChatInvitesRequest(
                            peer=target, admin_id=types.InputUserSelf(), limit=max_links, revoked=False
                        )
                    ),
                    timeout=_TIMEOUT,
                )
                for entry in list(getattr(listing, "invites", []) or []):
                    one = getattr(entry, "invite", entry)
                    if bool(getattr(one, "revoked", False)):
                        continue
                    totals.append(int(getattr(one, "requested", 0) or 0))
                    links.extend(_link_spellings(getattr(one, "link", None)))
            except Exception as exc:  # noqa: BLE001
                notes.append(f"the channel's other invite links could not be listed ({_reason(exc)})")
        # A public channel has no exported primary link, and the linkless query is what the older code
        # relied on. Keep asking it - as the last thing, and never as the only thing.
        links.append("")

        for link in dict.fromkeys(links):
            offset_date: datetime | None = None
            offset_user: Any = types.InputUserEmpty()
            for _page in range(max_pages):
                if len(rows) >= want:
                    break
                args: dict[str, Any] = {
                    "peer": target,
                    "requested": True,
                    "offset_date": offset_date,
                    "offset_user": offset_user,
                    # A page, not "what is left": the queue is ordered newest first and the caller's
                    # `skip` set is what decides whether this page is worth anything.
                    "limit": 100,
                }
                if link:
                    args["link"] = link
                asked += 1
                try:
                    found = await asyncio.wait_for(
                        self.client(functions.messages.GetChatInviteImportersRequest(**args)), timeout=_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    notes.append(f"the waiting list did not answer in {_TIMEOUT:.0f}s")
                    break
                except Exception as exc:  # noqa: BLE001 - a link that refuses is skipped, not fatal
                    notes.append(f"{_reason(exc)}")
                    break
                importers = list(getattr(found, "importers", []) or [])
                totals.append(int(getattr(found, "count", 0) or 0))
                if not importers:
                    break
                last = importers[-1]
                for importer in importers:
                    user_id = int(_field(importer, "user_id", 0) or 0)
                    if not user_id or user_id in seen:
                        continue
                    seen.add(user_id)
                    if user_id in known:
                        continue
                    rows.append(
                        {
                            "user_id": user_id,
                            "about": str(_field(importer, "about", "") or ""),
                            "date": str(_field(importer, "date", "") or ""),
                            "approved_by": _field(importer, "approved_by", None),
                        }
                    )
                if len(importers) < 100:
                    break  # the short page is the end of this link's queue
                # The offset has to be a date the server can encode, so a row that carries no real one (a
                # test's dict, an old layer) is answered with "now" rather than with a string.
                stamp = _field(last, "date", None)
                offset_date = stamp if isinstance(stamp, datetime) else datetime.now(timezone.utc)
                offset_user = _input_user(
                    int(_field(last, "user_id", 0) or 0), list(getattr(found, "users", []) or [])
                )
                if offset_user is None:
                    notes.append("the next page could not be asked for (the reader of the last person was unknown)")
                    break
        # The biggest number any of the reads claimed, and never less than the people actually looked at:
        # a link that reports no `requested` count but hands over 40 importers is still 40 waiting.
        total = max([one for one in totals if one] + [len(seen)])
        detail = f"{len(rows)} request(s) read"
        detail += f" of {total} waiting" if total else " (nothing is waiting)"
        if notes:
            detail += "; " + "; ".join(notes[:3])
        return (
            Result(ok=True, action="read_requests", detail=detail[:400], total=total),
            rows,
        )


def _field(row: Any, name: str, default: Any = None) -> Any:
    """One field of a Telegram row, whether the row is an object or a test's dict.

    A real response hands over `ChatInviteImporter` instances and `getattr` would be enough; the fakes in
    `tests/` hand over dicts, and a read that answered "nobody" to a dict would leave the paging and the
    skipping untested. Two lines buys that difference.
    """
    value = getattr(row, name, None)
    if value is None and isinstance(row, dict):
        value = row.get(name)
    return default if value is None else value


def _link_spellings(link: Any) -> list[str]:
    """The one or two strings a channel's `https://t.me/+ABC` link can be named by in `getChatInviteImporters`.

    `link` is documented as the invite link, and clients pass the hash rather than the URL. Which of the two
    a given deployment answers to is not written down anywhere honest, so both are tried - the tail after the
    last slash, with and without the `+` - and the first one that returns people wins. Nothing is invented
    here: it is the same value the channel's own `exported_invite.link` carries, cut two ways.
    """
    text = str(link or "").strip()
    if not text:
        return []
    tail = text.rsplit("/", 1)[-1]
    if not tail:
        return []
    return list(dict.fromkeys([tail.lstrip("+"), tail] if tail.startswith("+") else [tail]))


def _input_user(user_id: Any, users: Sequence[Any]) -> Any:
    """`InputUser` for the last reader on a page, because the next page has to name one to start after.

    The ids Telegram returns in `users` are the only place the access hashes come from; asking with an empty
    user would page from the start again, and a caller that read the same hundred people forever looks like a
    campaign that finished. So no hash, no next page - the caller is told, in the result's own sentence.
    """
    from telethon import types  # noqa: PLC0415

    wanted = int(user_id or 0)
    for one in users:
        # `types.User` spells the number `id`, and anything already input-shaped spells it `user_id`;
        # `types.InputUser` itself takes `user_id`, which is not the name a response carries.
        found_id = int(getattr(one, "id", None) or getattr(one, "user_id", 0) or 0)
        if found_id == wanted and getattr(one, "access_hash", None) is not None:
            return types.InputUser(user_id=wanted, access_hash=int(one.access_hash))
    return None


_TIMEOUT = 45.0


def _reason(exc: Exception) -> str:
    """One line about a failed read, in the shape a chat reply can carry.

    Read paths need this and cannot use `_map_error`'s retry arithmetic: a flood wait on a *read* is still
    just a reason the operator has to see, not a queue instruction.
    """
    mapped = _map_error(exc)
    if mapped is not None and mapped[1]:
        return str(mapped[1])[:180]
    return f"{type(exc).__name__}: {str(exc)[:140]}"


def _map_error(exc: Exception) -> tuple[int | None, str] | None:
    """Turn the Telegram errors we can act on into a wait and a sentence; others re-raise-as-failed.

    Deliberately short. A mapped error is one where the right move is "stop, tell the operator, let
    the queue come back later" — which is every flood, a private channel we can no longer see, and a
    user whose privacy makes DMs impossible. Anything else is a real bug and has to look like one.
    """
    try:
        from telethon import errors  # noqa: PLC0415
    except Exception:  # pragma: no cover  - telethon absent means no live writes anyway
        return None
    if isinstance(exc, errors.FloodWaitError):
        seconds = int(getattr(exc, "seconds", 0) or 0)
        return max(seconds, 60), (
            f"Telegram asked for a {seconds}s flood wait; the job is re-queued for that long and "
            "nothing here sleeps through it or shortens it"
        )
    if isinstance(exc, (errors.PeerFloodError, errors.FloodError)):
        return 3600, "Telegram says this account is rate-limited for now; the run stops rather than pushing"
    if isinstance(exc, errors.UserPrivacyRestrictedError):
        return None, (
            "this user's privacy does not allow a DM; the contact is marked failed and never retried "
            "into a second attempt at the same thing"
        )
    if isinstance(exc, errors.ChannelPrivateError):
        return None, "this channel is not visible to this account any more; nothing is retried against it"
    if isinstance(exc, errors.ChannelBannedError):
        return None, "this account is not an admin of that channel any more, so the write is refused"
    return None


def mapping_summary(results: Mapping[str, Result]) -> str:
    """Compact multi-result line for a job's ``result`` jsonb."""
    return "; ".join(f"{name}: {describe(result)}" for name, result in results.items())
