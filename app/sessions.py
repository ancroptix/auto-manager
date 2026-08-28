"""Storing, naming and revoking Telegram sessions.

A session string *is* an account: whoever holds it can read every DM, delete
posts, and message strangers as that person. Changing the password does not take
it away — only terminating sessions in the app does. So this module exists to keep
that value in exactly one place, and to make it hard to leak by accident:

* only :func:`active_session_string` returns the value, and it is named so loudly
  that a reviewer sees it at the call site;
* everything else hands back :func:`describe` output — name, username, age,
  whether it is active — which is what a status message needs and cannot leak;
* a stored value is never logged, never sent by the bot, and never included in an
  error message. The bot's replies go through :func:`scrub` for that reason.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "SESSION_NAME_RE",
    "active_session_string",
    "activate",
    "describe",
    "forget",
    "list_sessions",
    "mask_phone",
    "masked",
    "scrub",
    "store",
    "valid_name",
]

SESSION_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

#: Any plausible Telethon StringSession: a leading ``1`` then a long base64-alphabet
#: run, no spaces. Deliberately over-broad — this is a tripwire for replies and
#: logs, not a validator. A false positive (some unrelated base64 blob getting
#: redacted inside a status message) costs a line of clarity; a false negative costs
#: an account. The previous ``1[12][A-Za-z0-9+/=]{40,}`` looked precise and matched
#: nothing, because the character after the leading ``1`` is arbitrary base64.
_SESSION_SHAPE = re.compile(r"1[A-Za-z0-9+/=]{48,}")


def valid_name(name: str | None) -> bool:
    return bool(name) and bool(SESSION_NAME_RE.fullmatch(str(name).strip().casefold()))


def mask_phone(phone: str | None) -> str:
    """``+919876543210`` -> ``+91…3210``.

    A phone number is personal data with no business appearing in a log line, a
    traceback or a screenshot of the bot's chat. The country code and the last four
    digits are what a person uses to recognise a number; everything between them is
    what a leaked log would hand to anyone. An earlier version kept six of the
    twelve digits, which is not a mask — the middle of a mobile number is a few
    thousand candidates, and the first three plus last four already pin them down.
    """
    digits = "".join(ch for ch in (phone or "") if ch.isdigit() or ch == "+")
    if len(digits) < 9:
        return "‹phone›"
    return f"{digits[:3]}…{digits[-4:]}"


def masked(session_string: str | None) -> str:
    """`1.23.45.AAAA…` -> `1.23.45.<312 chars>`. Enough to compare, useless to steal."""
    if not session_string:
        return "(empty)"
    head = session_string[:8]
    return f"{head}…<{len(session_string)} chars>"


def scrub(text: str | None, *secrets: str | None) -> str:
    """Remove any session-shaped or explicitly-secret substring from `text`.

    Used on every outgoing bot message and every log line the bot writes. The
    shape-based pass matters because a session can arrive via an exception
    traceback, where nobody thought about naming it as a secret.
    """
    out = text or ""
    for secret in secrets:
        # 5 characters or more: a Telegram login code is 5-8 digits and must go,
        # while stripping a shorter "secret" would mangle ordinary text (job ids,
        # byte counts) for no safety gain.
        if secret and len(secret) >= 5:
            out = out.replace(secret, "‹redacted›")
    return _SESSION_SHAPE.sub("‹redacted-session›", out)


async def store(
    db: Any,
    *,
    name: str,
    session_string: str,
    kind: str = "user",
    account_id: int | None = None,
    username: str | None = None,
    note: str | None = None,
    activate: bool = True,
) -> dict[str, Any]:
    """Insert or replace one named session, and return its *description*.

    Activation demotes the previous active session of the same kind in the same
    statement: two live user sessions would mean two queue loops claiming work
    and posting the same episode twice, and the partial unique index in 0003
    makes that state impossible to write by hand either.
    """
    key = str(name).strip().casefold()
    if not valid_name(key):
        raise ValueError(f"session name {name!r} must match {SESSION_NAME_RE.pattern}")
    if not session_string or len(session_string) < 64:
        raise ValueError("session string is missing or too short to be a StringSession")

    if activate:
        await db.execute(
            "update app.telegram_session set active = false where kind = $1 and active and name <> $2",
            kind,
            key,
        )
    await db.execute(
        """
        insert into app.telegram_session
            (name, kind, session_string, account_id, username, active, note, created_at, last_used_at)
        values ($1, $2, $3, $4, $5, $6, $7, now(), now())
        on conflict (name) do update set
            kind = excluded.kind,
            session_string = excluded.session_string,
            account_id = excluded.account_id,
            username = excluded.username,
            active = excluded.active,
            note = coalesce(excluded.note, app.telegram_session.note),
            last_used_at = now()
        """,
        key,
        kind,
        session_string,
        account_id,
        username,
        activate,
        note,
    )
    row = await describe_one(db, key)
    return row or {"name": key, "stored": True}


async def describe_one(db: Any, name: str) -> dict[str, Any] | None:
    rows = await list_sessions(db, name=name)
    return rows[0] if rows else None


async def list_sessions(db: Any, *, name: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
    """Session metadata only. The `session_string` column is never selected here."""
    clauses, args = [], []
    if name:
        args.append(name.strip().casefold())
        clauses.append(f"name = ${len(args)}")
    if kind:
        args.append(kind)
        clauses.append(f"kind = ${len(args)}")
    where = (" where " + " and ".join(clauses)) if clauses else ""
    rows = await db.fetch(
        f"""
        select name, kind, account_id, username, active, note, created_at, last_used_at,
               length(session_string) as length_chars
          from app.telegram_session
        {where}
         order by active desc, kind, name
        """,
        *args,
    )
    return [dict(row) for row in rows]


async def activate(db: Any, name: str) -> bool:
    """Make one session the live one. False when the name does not exist."""
    row = await describe_one(db, str(name).strip().casefold())
    if row is None:
        return False
    await db.execute(
        "update app.telegram_session set active = false where kind = $1 and active",
        row["kind"],
    )
    await db.execute(
        "update app.telegram_session set active = true, last_used_at = now() where name = $1",
        row["name"],
    )
    return True


async def forget(db: Any, name: str) -> bool:
    """Delete the stored row. Telegram's own session stays alive until the
    operator terminates it in the app, and callers must say so — deleting our
    copy is not revocation, and pretending it is would be the dangerous lie."""
    result = await db.fetchrow(
        "delete from app.telegram_session where name = $1 returning name", str(name).strip().casefold()
    )
    return bool(result)


async def active_session_string(db: Any, *, kind: str = "user") -> str | None:
    """THE reader of the secret. Callers must not put the result in a reply, a
    log line, or an exception message."""
    return await db.fetchval(
        "select session_string from app.telegram_session where kind = $1 and active limit 1", kind
    )
