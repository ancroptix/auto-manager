"""A minimal Telegram Bot API client, over plain HTTPS.

Why not Telethon for this part: the bot needs sendMessage/getUpdates/deleteMessage/
answerCallbackQuery and nothing else, and a Bot-API token is not a user session. Using
MTProto here would mean booting a full client (and every method it exposes) for four
calls —
`app/probe.py` shows how carefully a raw client has to be fenced.

Three properties are baked in:

* the token never appears in an exception, a log line or a repr — `httpx` puts the
  full request URL (token included) into its error text, so every error passes
  through :func:`redact`;
* long polling with a bounded timeout, so a wedged connection cannot stall the
  service's startup;
* no `sendFile`-style helper exists at all. A control bot that can send text and an
  inline keyboard of its own commands cannot be tricked into leaking a file from the
  server it runs on — and the keyboard is capped that way on purpose: every button
  carries a command string this same bot accepts typed, so it opens no new action
  (`app/keyboards.py` builds them, and its tests say so).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

__all__ = ["BotApi", "BotTokenError", "Update", "parse_update", "redact", "split_for_chat"]

_TOKEN_SHAPE = re.compile(r"^(\d{4,12}):([A-Za-z0-9_-]{20,})$")
_UPDATE_URL = re.compile(r"/bot\d+:[A-Za-z0-9_-]+")


class BotTokenError(ValueError):
    """The token is missing or malformed. Never carries the token itself."""


def split_for_chat(text: str, *, limit: int = 4096) -> list[str]:
    """Telegram-sized pieces of one long message, cut on line breaks.

    The control bot answers with things the operator has to read in full — a probe report, a queue
    blocking list — and the transport used to hand Telegram ``text[:4096]``, which means the tail of an
    answer simply did not arrive and nothing said so. Refusing an over-long text is the right rule for a
    *published* post (see ``sender.MAX_MESSAGE_CHARS``: half a caption in a channel is worse than no
    post), and the wrong one for a private chat, so here the message is split instead. Cuts land on
    newlines whenever a newline fits; only a single line longer than the limit is split mid-line, and
    then it is split exactly, so a character is never dropped.

    Empty input is no message at all, not one empty send: Telegram rejects an empty ``sendMessage`` and
    the caller learns more from getting nothing back than from a 400 about our own bookkeeping.
    """
    body = text or ""
    if not body:
        return []
    if len(body) <= limit:
        return [body]
    units: list[str] = []
    for line in body.split("\n"):
        while len(line) > limit:
            units.append(line[:limit])
            line = line[limit:]
        units.append(line)
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for unit in units:
        extra = len(unit) + (1 if current else 0)
        if current and size + extra > limit:
            parts.append("\n".join(current))
            current, size = [], 0
            extra = len(unit)
        current.append(unit)
        size += extra
    if current:
        parts.append("\n".join(current))
    return parts


def redact(text: str | None) -> str:
    """Strip a bot token out of arbitrary text (error messages, tracebacks)."""
    if not text:
        return ""
    return _UPDATE_URL.sub("/bot‹redacted›", str(text))


@dataclass(frozen=True, slots=True)
class Update:
    """One inbound update, flattened.

    ``chat_id`` and ``from_id`` are kept separate because the whole security model
    is "answer the owner only": in a group they differ, and trusting the chat
    would let anyone in that group drive the account.
    """

    update_id: int
    chat_id: int
    from_id: int | None
    from_username: str | None
    text: str
    message_id: int | None
    kind: str = "message"
    callback_id: str | None = None

    @property
    def is_private_chat(self) -> bool:
        """True only when the update came from a one-to-one chat *with that same user*.

        Positive ids are private chats in Telegram; groups and channels are
        negative. This is what lets the bot accept "/pause" from the owner in
        their own DM and refuse it from a group the bot happens to be in, where
        the chat id would say "allowed" while the sender says "anyone".
        """
        return self.chat_id > 0 and self.chat_id == self.from_id


def parse_update(raw: dict[str, Any]) -> Update | None:
    """Decode a getUpdates/webhook payload, ignoring anything that is not text."""
    source = raw.get("message") or raw.get("edited_message") or raw.get("callback_query")
    if not source:
        return None
    if "callback_query" in raw:
        message = source.get("message") or {}
        return Update(
            update_id=int(raw.get("update_id", 0)),
            chat_id=int((message.get("chat") or {}).get("id") or 0),
            from_id=(source.get("from") or {}).get("id"),
            from_username=(source.get("from") or {}).get("username"),
            text=str(source.get("data") or ""),
            message_id=message.get("message_id"),
            kind="callback",
            # The callback's own id, which lives inside `callback_query`. `raw["id"]` is nothing at all —
            # an update's number is `update_id` — and reading it here meant every button on a phone kept
            # its "loading" shimmer for ever, because `answerCallbackQuery` was never sent.
            callback_id=str(source.get("id") or ""),
        )
    chat = source.get("chat") or {}
    return Update(
        update_id=int(raw.get("update_id", 0)),
        chat_id=int(chat.get("id") or 0),
        from_id=(source.get("from") or {}).get("id"),
        from_username=(source.get("from") or {}).get("username"),
        text=str(source.get("text") or source.get("caption") or ""),
        message_id=source.get("message_id"),
    )


@dataclass
class BotApi:
    # repr=False is not decoration: an unhandled exception prints local variables,
    # and this object is in scope when that happens.
    token: str = field(repr=False)
    base_url: str = "https://api.telegram.org"
    client: httpx.AsyncClient | None = None
    request_timeout: float = 35.0
    _offset: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        token = (self.token or "").strip()
        if not _TOKEN_SHAPE.match(token):
            raise BotTokenError(
                "TELEGRAM_BOT_TOKEN must look like '123456:ABC-DEF…' (create one with @BotFather)"
            )
        self.token = token
        if self.client is None:
            # No proxy, no auth header: the token is in the path, and httpx would
            # otherwise be free to echo it into an exception.
            self.client = httpx.AsyncClient(timeout=self.request_timeout, follow_redirects=False)

    @property
    def bot_user_id(self) -> int:
        return int(self.token.split(":", 1)[0])

    @property
    def api_id_hint(self) -> int:
        """The `bot_auth_key_id` for MTProto login-as-bot, if that is ever needed."""
        return self.bot_user_id

    async def close(self) -> None:
        if self.client is not None:
            await self.client.aclose()

    async def _call(self, method: str, **params: Any) -> dict[str, Any]:
        url = f"{self.base_url}/bot{self.token}/{method}"
        payload = {k: v for k, v in params.items() if v is not None}
        assert self.client is not None
        try:
            response = await self.client.post(url, json=payload)
        except Exception as exc:  # noqa: BLE001 - the message would contain the URL
            raise RuntimeError(f"{type(exc).__name__}: {redact(str(exc))}") from None
        try:
            data = response.json()
        except (json.JSONDecodeError, ValueError):
            if response.status_code == 401:
                raise RuntimeError("bot token rejected (401)") from None
            raise RuntimeError(f"unparsable Bot API response ({response.status_code})") from None
        if not data.get("ok", False):
            raise RuntimeError(f"{method} failed: {redact(str(data.get('description')))[:160]}")
        return data

    async def get_updates(self, *, timeout: float | None = None) -> list[Update]:
        """Long-poll one batch. Malformed entries are skipped, not fatal."""
        data = await self._call("getUpdates", timeout=int(timeout or 25), offset=self._offset + 1)
        updates: list[Update] = []
        for raw in data.get("result") or []:
            try:
                self._offset = max(self._offset, int(raw.get("update_id", 0)))
                parsed = parse_update(raw)
            except (TypeError, ValueError):
                continue
            if parsed is not None:
                updates.append(parsed)
        return updates

    async def send(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to: int | None = None,
        parse_mode: str | None = None,
        markup: dict[str, Any] | None = None,
    ) -> int | None:
        """Send text, in as many messages as Telegram's limit needs.

        The reply-to id of the *first* part is what comes back, so a caller that deletes its own prompt
        (the login flow) still deletes the message it asked about. Those prompts are a line or two long
        and never split; an answer that does split is a report nobody is deleting.

        ``markup`` is an inline keyboard, and it goes on the first part only. A button block repeated on
        every piece of a split report would be the same tap offered three times with no third of a
        message to act on, and the caller that builds a keyboard builds it for the screen the operator
        reads first.
        """
        first: int | None = None
        for part in split_for_chat(text):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": part,
                "disable_web_page_preview": True,
                "reply_to_message_id": reply_to if first is None else None,
                "parse_mode": parse_mode,
            }
            if markup and first is None:
                # The object, not a JSON string. `_call` posts `json=`, and Telegram accepts a
                # JSON-serialized keyboard only in a *form-encoded* body: in a JSON body a string there
                # is a 400 about a wrong inline keyboard, so the string form is the one shape that
                # cannot work here.
                payload["reply_markup"] = markup
            data = await self._call("sendMessage", **payload)
            message_id = (data.get("result") or {}).get("message_id")
            if first is None:
                first = message_id
        return first

    async def delete(self, chat_id: int, *message_ids: int | None) -> int:
        """Best-effort deletion. Returns how many it removed.

        Best effort is the honest name: deletion can fail because the message is
        already gone, and that must never abort a login or a reply.
        """
        removed = 0
        for message_id in message_ids:
            if not message_id:
                continue
            try:
                await self._call("deleteMessage", chat_id=chat_id, message_id=int(message_id))
                removed += 1
            except Exception:  # noqa: BLE001 - nothing to learn from a failed delete
                continue
        return removed

    async def answer_callback(self, callback_id: str | None, text: str = "") -> None:
        if not callback_id:
            return
        try:
            await self._call("answerCallbackQuery", callback_query_id=callback_id, text=text[:190])
        except Exception:  # noqa: BLE001 - the button press is already handled
            pass

    async def get_me(self) -> dict[str, Any]:
        data = await self._call("getMe")
        return data.get("result") or {}
