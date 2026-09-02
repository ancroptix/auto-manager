"""Whose admin we are — read from Telegram, not asked for in a dashboard.

The operator's rule, given 2026-08-28, in their words: *"hum admin hai ya nahi, ye hume khud detect
karna hoga"*. Until now the answer was a box in `app.source_channel` the operator was told to fill by
hand, which is honest but slow and easy to leave stale: the flag decides whether a channel gets
captioned in place or becomes a source with a destination built beside it. So this module reads the
rights from what the logged-in session can already see — the dialog list `app.probe` walks — and
writes the flag itself.

Three rules hold it together:

* **Only configured channels are touched.** This never creates a row, never renames a channel and
  never decides from a title. It matches a dialog to a configured channel by @handle, or by the
  numeric id when the channel is private and has no handle to match on.
* **Absence is not "member".** A channel the account cannot see keeps its old value — `null` if it
  was never read — and is listed as unseen. Writing ``false`` because we failed to look would flip a
  destination into a source on a read error, which is the exact silent wrong turn the whole
  rights model exists to prevent.
* **What is stored is what was seen.** ``we_are_admin`` means "this account holds admin rights here
  and may post": that is the pair the pipeline acts on. Editing captions additionally needs
  ``edit_messages``, and the refusal to caption where we cannot is built on that, so both are kept
  in the report even though only one becomes a column.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

_DIGITS = re.compile(r"\d+")

__all__ = [
    "RIGHT_NAMES",
    "rights_of",
    "we_are_admin",
    "marked_channel_id",
    "plan",
    "record",
    "summary",
]

#: The rights this pipeline acts on, in the order the report prints them.
RIGHT_NAMES: tuple[str, ...] = (
    "post_messages",
    "edit_messages",
    "delete_messages",
    "invite_users",
    "add_admins",
)


def rights_of(entity: Any) -> dict[str, bool] | None:
    """This account's rights on one channel entity, or None where it is not an admin.

    ``admin_rights`` is absent — not empty — for an ordinary member, so None is the honest answer
    for a member and the difference between "we can see it" and "we run it" is preserved.
    """
    rights = getattr(entity, "admin_rights", None)
    if rights is None:
        return None
    return {name: bool(getattr(rights, name, False)) for name in RIGHT_NAMES}


def we_are_admin(rights: Mapping[str, bool] | None) -> bool:
    """The column's meaning, in one expression: an admin who may post.

    Admin without posting rights is a *title* change and a pin, not a publisher, so it does not
    count as True here. ``False`` is a real answer — a channel we can see and cannot write in —
    while "nobody looked" never enters this function: an unread channel is simply absent from the
    update list, and its column stays null for ``app.inplace.route_for`` to treat as a source.
    """
    if not rights:
        return False
    return bool(rights.get("post_messages"))


def marked_channel_id(value: Any) -> int | None:
    """The ``-100…`` form Telegram uses for a channel id in a peer link.

    Ids arrive from two directions — the dashboard row and the MTProto entity — and free channels
    are addressed both ways, so matching has to accept both rather than trusting one spelling.
    """
    text = str(value or "").strip().lstrip("@")
    if not _DIGITS.fullmatch(text.lstrip("-")):
        return None
    # The marked form is `-100` in front of the raw id's digits, so it is built and read as text.
    # Doing it arithmetically (a power of ten, a subtraction) is how a 13-digit id ends up wrong in
    # a way that only shows up when a row silently stops matching its channel.
    if text.startswith("-"):
        return int(text)  # marked already, or another negative spelling the operator typed: keep it
    return int("-100" + text)


def _row_list(entries: Sequence[Mapping[str, Any]]) -> str:
    return ", ".join(str(entry.get("source_channel_id") or "?") for entry in entries)


def _key(username: Any) -> str:
    return str(username or "").lstrip("@").casefold()


def plan(dialogs: Sequence[Mapping[str, Any]], configured: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Match what the session sees against what the operator configured.

    Returns ``{"updates": [{"source_channel_id", "we_are_admin", "can_edit", "title"}…],
    "unseen": [handle or id…], "ambiguous": [title…]}``.

    ``ambiguous`` is not a failure to resolve quietly: two configured rows claiming the same
    @handle is a database that needs a human, and the last thing either channel needs is a rights
    write decided by dict ordering.
    """
    by_username: dict[str, list[Mapping[str, Any]]] = {}
    by_id: dict[int, list[Mapping[str, Any]]] = {}
    for dialog in dialogs:
        if dialog.get("left"):
            continue
        name = _key(dialog.get("username"))
        if name:
            by_username.setdefault(name, []).append(dialog)
        marked = marked_channel_id(dialog.get("id"))
        if marked is not None:
            by_id.setdefault(marked, []).append(dialog)

    updates: list[dict[str, Any]] = []
    unseen: list[str] = []
    ambiguous: list[str] = []
    # One dialog may answer for only one row. `telegram_channel_id` is unique so two rows cannot
    # claim the same *channel*, but they can claim the same *handle* while one of the ids is stale —
    # and then which row is the real one is a question about the operator's data, not about rights.
    claimed: dict[int, list[dict[str, Any]]] = {}
    for row in configured:
        name = _key(row.get("username"))
        wanted = marked_channel_id(row.get("telegram_channel_id"))
        candidates = (by_username.get(name, []) if name else []) + (by_id.get(wanted, []) if wanted is not None else [])
        # Same dialog reached by both keys is one match, not two.
        unique = {id(entry): entry for entry in candidates}.values()
        found = list(unique)
        if not found:
            unseen.append(f"@{name}" if name else str(row.get("telegram_channel_id") or "?"))
            continue
        if len(found) > 1:
            ambiguous.append(f"@{name}" if name else str(wanted))
            continue
        dialog = found[0]
        if "rights" not in dialog:
            # A caller that handed over dialog summaries with no rights key has told us nothing about
            # membership. That is "not read", not "member", so the row is left alone.
            unseen.append(f"@{name}" if name else str(row.get("telegram_channel_id") or "?"))
            continue
        claimed[id(dialog)] = claimed.get(id(dialog), []) + [
            {
                "source_channel_id": row.get("id"),
                "we_are_admin": we_are_admin(dialog.get("rights")),
                "can_edit": bool((dialog.get("rights") or {}).get("edit_messages")),
                "title": dialog.get("title"),
                "want": f"@{name}" if name else str(wanted),
            }
        ]
    for entries in claimed.values():
        if len(entries) > 1:
            # Name the row as well as the channel: the operator has to go fix a row, and "dupe"
            # appearing twice in a report tells them which two rows disagree about it.
            ambiguous.append(f"{entries[0]['want']} (rows {_row_list(entries)})")
            continue
        updates.append({k: v for k, v in entries[0].items() if k != "want"})
    return {"updates": updates, "unseen": unseen, "ambiguous": ambiguous}


async def record(db: Any, decided: Mapping[str, Any]) -> dict[str, int]:
    """Write the matched rows. One statement per channel, and never a new row.

    Only channels whose value actually *changed* are written, so a probe run on a stable setup
    leaves no audit noise and a restart does not look like a rights change.
    """
    written = 0
    for update in decided.get("updates") or []:
        if update.get("source_channel_id") is None:
            continue
        # `returning id` rather than a row count: `app.db.execute` hands back asyncpg's status string
        # ("UPDATE 1"), and a writer that parses that is a writer that breaks the day someone switches
        # to a pooler that spells it differently. The rows that actually moved are also the honest
        # number — `where ... is distinct from` means no-op writes return nothing at all.
        moved = await db.fetch(
            "update app.source_channel set we_are_admin = $2, rights_checked_at = now() "
            "where id = $1 and we_are_admin is distinct from $2 returning id",
            update["source_channel_id"],
            update["we_are_admin"],
        )
        written += len(moved or [])
    return {"considered": len(decided.get("updates") or []), "written": written}


def summary(decided: Mapping[str, Any], counted: Mapping[str, int]) -> str:
    """The report line: how many were read, how many changed, and what could not be matched.

    Deliberately names the *unseen* ones, because "nothing to report" after a probe that saw zero of
    your four channels would be the most misleading sentence this program could print.
    """
    parts = [
        f"rights read for {counted.get('considered', 0)} configured channel(s), {counted.get('written', 0)} changed"
    ]
    unseen = list(decided.get("unseen") or [])
    if unseen:
        parts.append("not visible to this session: " + ", ".join(unseen[:6]))
    ambiguous = list(decided.get("ambiguous") or [])
    if ambiguous:
        parts.append("two rows claim the same channel, left alone: " + ", ".join(ambiguous[:4]))
    return "; ".join(parts)
