"""Performing a Telegram user login on the operator's behalf.

This is the piece that lets a non-terminal operator connect the spare account: the
control bot asks for a phone number and a code over a private chat, and this module
turns that into a ``StringSession`` that goes into Postgres. No terminal, no
``pip install``, no session string ever appearing in a chat.

It is deliberately the *only* module that can create a session from a code, and it
holds two rules:

* the client it builds is used for nothing else — no message sending, no channel
  reads — because the sole job here is authentication. The session string it
  returns is then used by the normal client, whose guardrails are elsewhere;
* a password (2FA) is passed in, used once, and cleared by the caller. It is never
  stored, logged or returned.

Telethon's own ``start()`` is not used, because it is interactive by design: it
prompts on a terminal, and there is no terminal in a container.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .controlbot import LoginResult, LoginUnstored, NeedsPassword
# Single implementation of mask_phone lives in app.sessions, next to the scrubbing
# the bot applies to every reply: two definitions of "masked" always drift, and the
# drift is a leak.
from .sessions import mask_phone

log = logging.getLogger("auto_manager.login")

__all__ = ["MTProtoLogin", "LoginUnstored", "read_session_string", "mask_phone"]


def read_session_string(client: Any) -> str:
    """The serialised session of one client, under whichever name the installed Telethon writes it out.

    It is handed the *client* rather than the session object, on purpose: the attribute read that can produce a
    session string then lives in exactly one place in this module, which is one line to audit for "could
    this leak a session?" — and it keeps `scripts/check_secrets.py`, whose job is to catch committed
    session *files*, from reading a legitimate attribute access as a path.

    Telethon 1.44's ``StringSession`` has no ``as_string()``: its method is ``save()``, and
    ``as_string()`` is the name older releases used. This project called the older name, and the cost was
    not an exception in a log — it was the operator's own login: Telegram accepted the code and the
    password, ``sign_in`` then died on the way out, the authorized connection was discarded, and nothing
    could be stored. So both names are tried, in that order, and an empty answer counts as no answer.

    ``StringSession.save()`` returns ``''`` when there is no auth key. That is a session which was never
    authorised, and writing it into the table would fail later in a way no log line explains.
    """
    session = getattr(client, "session", None)
    for name in ("save", "as_string"):
        method = getattr(session, name, None)
        if not callable(method):
            continue
        value = method()
        if isinstance(value, str) and value:
            return value
        if isinstance(value, str):
            # The method exists and answered honestly. Asking the same session the same question under
            # another name would only produce the same empty string twice.
            break
    raise RuntimeError(f"{type(session).__name__} produced no session string to store")



@dataclass
class MTProtoLogin:
    api_id: int
    api_hash: str
    device_model: str = "auto-manager"
    system_version: str = "render"
    app_version: str = "0.1.0"
    lang_code: str = "en"
    _clients: dict[str, Any] = field(default_factory=dict, repr=False)

    async def send_code(self, phone: str) -> str:
        """Ask Telegram for a login code. Returns the ``phone_code_hash``.

        Two things this has to get right, and both were learned the hard way on the first live
        attempt — the operator's own /login, which is what this module exists for. Telethon's
        ``send_code_request`` takes only ``phone`` in 1.44: the old ``force`` argument is gone, and
        passing it raises ``TypeError``
        before a request is ever made — and the request needs a *connected* client, because
        ``TelegramClient(...)`` on its own answers "Cannot send requests while disconnected".

        A failed attempt closes its own connection. Leaving one open in a free-tier container means
        both a leaked socket and, worse, a half-finished auth that the next attempt would reuse.
        """
        client = await self._client()
        try:
            sent = await client.send_code_request(phone)
        except Exception as exc:  # noqa: BLE001 - the message is useful, the class is not
            await self._drop(client)
            raise RuntimeError(self._explain(exc)) from None
        self._clients[phone] = client
        return getattr(sent, "phone_code_hash", None) or ""

    async def sign_in(self, phone: str, code: str, code_hash: str | None, *, password: str | None = None) -> LoginResult:
        """Exchange the code (and password, if 2FA) for a session string."""
        client = self._clients.get(phone) or await self._client()
        self._clients[phone] = client
        try:
            if password is not None:
                await client.sign_in(password=password)
            else:
                if not code:
                    raise RuntimeError("no code supplied")
                await client.sign_in(phone=phone, code=code, phone_code_hash=code_hash or "")
        except Exception as exc:  # noqa: BLE001
            name = type(exc).__name__
            if "SessionPasswordNeeded" in name:
                # Keep the connection: the password belongs to this same auth attempt, and a new
                # client would start the exchange over — which costs a second code.
                raise NeedsPassword("2FA required") from None
            # A wrong or expired code is spent; the client is not reusable for this attempt, so it
            # is closed here rather than left for /cancel to find.
            await self.discard(phone)
            raise RuntimeError(self._explain(exc)) from None

        # Everything from here is *after* Telegram said yes, so a failure is no longer about the code.
        # Reporting it as "sign-in failed, 2 attempts left" was the second half of the operator's bug: the
        # code is spent, the account is signed in, and the one useful thing to say is that a session
        # exists which nobody stored.
        try:
            me = await client.get_me()
            session = read_session_string(client)
        except Exception as exc:  # noqa: BLE001 - the login worked; losing the session still must be said
            await self.discard(phone)
            raise LoginUnstored(
                "the account answered and the credentials were accepted, but this service could not read "
                f"a session string out of it ({type(exc).__name__}: {str(exc)[:120]}). The login may be "
                "live on the account without being stored here — open Telegram → Settings → Privacy and "
                "Security → Devices, terminate the auto-manager session, then run /login again"
            ) from None
        # Disconnect immediately: holding an authorized connection open after the
        # string is produced would mean two live connections for one account, and
        # Telegram treats that as suspicious behaviour.
        try:
            await client.disconnect()
        finally:
            self._clients.pop(phone, None)
        log.info("login succeeded for %s (id=%s)", mask_phone(phone), getattr(me, "id", "?"))
        return LoginResult(
            session_string=session,
            account_id=int(getattr(me, "id", 0) or 0) or None,
            username=getattr(me, "username", None),
        )

    async def discard(self, phone: str | None) -> None:
        """Drop a half-finished attempt so a stale client cannot be reused."""
        client = self._clients.pop(phone or "", None)
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001 - nothing to learn from a failed disconnect
                pass

    async def _drop(self, client: Any) -> None:
        """Close one connection without letting the close itself become the error."""
        try:
            await client.disconnect()
        except Exception:  # noqa: BLE001 - nothing to learn from a failed disconnect
            pass

    async def _client(self) -> Any:
        """A connected, throwaway client: this module authenticates and nothing else.

        ``connect()`` is part of building it, not an extra step at each call site. Both the code
        request and the sign-in need a live connection, and ``sign_in`` is reached without a prior
        ``send_code`` whenever the process restarted between the two commands — so a client that is
        merely *constructed* would fail one login in three, in a way that looks like Telegram's fault.
        """
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        client = TelegramClient(
            StringSession(),
            self.api_id,
            self.api_hash,
            device_model=self.device_model,
            system_version=self.system_version,
            app_version=self.app_version,
            lang_code=self.lang_code,
            request_retries=1,
            connection_retries=1,
            auto_reconnect=False,
        )
        await client.connect()
        return client

    @staticmethod
    def _explain(exc: Exception) -> str:
        """Translate the failure without repeating anything secret.

        Telegram's own error text can contain the phone number and, for a flood
        wait, is otherwise the only thing the operator has to go on — so the class
        name and seconds are surfaced and the raw message is not.
        """
        name = type(exc).__name__
        seconds = getattr(exc, "seconds", None)
        hints = {
            "PhoneNumberInvalidError": "that phone number was rejected as invalid",
            "PhoneNumberBannedError": "Telegram will not send a login code to that number",
            "PhoneNumberOccupiedError": "that number is not a new account; the code was sent to the app instead",
            "SessionCodeInvalidError": "the code was wrong or has expired",
            "SessionWaitedError": "too many wrong codes; wait a few minutes and start again",
            "FloodWaitError": "Telegram is rate-limiting this action",
            "ApiIdInvalidError": "the API id/hash pair is not valid for this account",
            "AuthKeyUnregisteredError": "the session was terminated; log in again",
            "ConnectionError": "the service could not reach Telegram's servers",
            "OSError": "the service could not open a connection to Telegram's servers",
        }
        text = hints.get(name, f"login step failed ({name})")
        if seconds:
            text += f" — wait {int(seconds)}s before retrying"
        return text
