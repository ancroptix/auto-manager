"""A minimal Telegram Bot API client, over plain HTTPS.

Why not Telethon for this part: the bot needs sendMessage/getUpdates/deleteMessage
and nothing else, and a Bot-API token is not a user session. Using MTProto here
would mean booting a full client (and every method it exposes) for three calls —
`app/probe.py` shows how carefully a raw client has to be fenced.

Three properties are baked in:

* the token never appears in an exception, a log line or a repr — `httpx` puts the
  full request URL (token included) into its error text, so every error passes
  through :func:`redact`;
* long polling with a bounded timeout, so a wedged connection cannot stall the
  service's startup;
* no `sendFile`-style helper exists at all. A control bot that can only send text
  cannot be tricked into leaking a file from the server it runs on.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

__all__ = ["BotApi", "BotTokenError", "Update", "parse_update", "redact"]

_TOKEN_SHAPE = re.compile(r"^(\d{4,12}):([A-Za-z0-9_-]{20,})$")
_UPDATE_URL = re.compile(r"/bot\d+:[A-Za-z0-9_-]+")


class BotTokenError(ValueError):
    """The token is missing or malformed. Never carries the token itself."""


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
            callback_id=str(raw.get("id") or ""),
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
    ) -> int | None:
        """Send text. That is the whole sending surface, by design.

        4096 is Telegram's limit; the reply-to id is returned so a caller can
        delete its own message afterwards (the login flow does exactly that).
        """
        data = await self._call(
            "sendMessage",
            chat_id=chat_id,
            text=text[:4096],
            disable_web_page_preview=True,
            reply_to_message_id=reply_to,
            parse_mode=parse_mode,
        )
        return (data.get("result") or {}).get("message_id")

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
