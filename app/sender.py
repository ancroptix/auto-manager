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

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "action": self.action,
            "message_id": self.message_id,
            "detail": self.detail[:400],
            "chars": self.chars,
            "buttons": self.buttons,
            "retry_after": self.retry_after,
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

    def _gate(self, action: str, peer: Any, text: str) -> Result | None:
        """The four refusals, before any await. Returns None when the write may go ahead."""
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
        if not self.policy.live:
            # The plan is the answer, not an apology: it is what the operator reads before a live day.
            self.journal.append({"action": action, "peer": str(peer), "ok": True, "detail": "planned", "chars": len(text or "")})
            return Result(
                ok=True,
                action="planned",
                detail=f"would {action} to {peer} (APP_MODE is not live, so nothing was sent)",
                chars=len(text or ""),
            )
        return None

    async def _call(self, action: str, peer: Any, coro: Awaitable[Any], *, text: str, buttons: int = 0) -> Result:
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
        return await self._call(
            "sent", peer, self.client.send_message(peer, text, **kwargs), text=text, buttons=sum(len(r) for r in rows)
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
        return await self._call(
            "edited", peer, self.client.edit_message(peer, int(message_id), text, **kwargs), text=text
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
        return await self._call(
            "forwarded",
            to_peer,
            self.client.forward_messages(to_peer, ids, **kwargs),
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
        deadline = time.monotonic() + max(0.0, wait_seconds)
        seen: list[dict[str, Any]] = []
        while True:
            try:
                async for message in self.client.iter_messages(peer, limit=limit):
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

    async def pending_requests(self, peer: Any, *, limit: int = 50) -> tuple[Result, list[dict[str, Any]]]:
        """Still-unanswered join requests of a channel we admin.

        ``messages.getChatInviteImporters`` is the only read that answers this, and ``requested=True``
        is the filter that keeps *approved* people out of a list whose whole purpose is "who is
        waiting". Older pending entries are included: the point of this call is that a request from
        last month can still be answered today.
        """
        refused = self._gate("read_requests", peer, "")
        if refused is not None:
            return refused, []
        try:
            from telethon import functions, types  # noqa: PLC0415

            found = await self.client(
                functions.messages.GetChatInviteImportersRequest(
                    peer=await self.client.get_input_entity(peer),
                    offset_date=None,
                    offset_user=types.InputUserEmpty(),
                    limit=int(limit),
                    requested=True,
                )
            )
        except Exception as exc:  # noqa: BLE001
            mapped = _map_error(exc)
            detail = mapped[1] if mapped else f"{type(exc).__name__}: {str(exc)[:160]}"
            return Result(ok=False, action="failed", detail=f"could not read requests of {peer}: {detail}"), []
        rows = [
            {
                "user_id": int(getattr(importer, "user_id", 0) or 0),
                "about": str(getattr(importer, "about", "") or ""),
                "date": str(getattr(importer, "date", "") or ""),
            }
            for importer in list(getattr(found, "importers", []) or [])
        ]
        return (
            Result(ok=True, action="read_requests", detail=f"{len(rows)} pending request(s) on {peer}"),
            rows,
        )


_TIMEOUT = 45.0


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
