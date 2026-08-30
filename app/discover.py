"""What the spare account can already see, turned into the two kinds of channel this program uses.

The operator's ask, 2026-08-30, in their words: *"in my spare session it auto detects all the channels and
set them as source and destination channels. if it is a member only then set them as source and if it is
admin or owner then set as destination and if it is now member but later becomes admin then the channel
should instantly switch from source to destination."*

That is the whole design of this module. The role is read, never asked about (their other standing rule:
*"hum admin hai ya nahi, ye hume khud detect karna hoga"*), and :mod:`app.rights` stays the only place that
decides what "admin" means — this module reads the same dialog entries :func:`app.probe.probe_account`
builds, so a discovery answer and a probe answer cannot drift apart.

Four rules keep it from becoming a machine that fills the database with nonsense:

* **A role decides the kind of channel, and nothing else does.** Member means "read files from here";
  an admin who may post means "publish here". A group, a chat, a user, or a channel the account has left
  is skipped and *counted* — a silent skip is how a channel goes missing for a week.
* **A destination needs a series, and this program will not invent one.** `app.destination.series_id` is not
  null, so a channel can only become a destination when its name says which series it is — checked against
  :func:`app.channels.destination_name`, the same function that generates the name, not against a copy of
  the rule. An admin channel nobody can name a series for is reported as such, and stays unconfigured.
* **Nothing is created in Telegram here.** A row in `app.destination` is the record that a channel is a
  series' destination; the channel itself, its bot and its invite are the pipeline's act, and that half is
  still the operator's `publish.route` decision. Discovery links what already exists.
* **A flip off a source needs that source to be replaceable.** Making the only channel a series reads from
  into its publishing place would leave the series with nothing to read. That case is proposed and refused,
  never applied.

Writing happens in :mod:`app.controlbot` through :mod:`app.sourcecfg` — the module that owns the source and
destination inserts — because a second way to write a row is a second way to get it wrong.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from . import rights as channel_rights
from . import sourcecfg
#: The switch auto mode lives behind, and the only key this module reads for itself. `app/handlers.py`
#: reads it on every reconciliation, so the toggle on the screen is read by production code rather than
#: echoed back at the operator who set it.
AUTO_KEY = "discover.auto"

#: What a sweep needs from the two tables, and no more: the same columns the console's screens read, so a
#: sweep and a screen cannot disagree about which channel is which.
_SOURCES_SQL = (
    "select id, username, title, telegram_channel_id, mode, coalesce(declared_series, '') as declared_series"
    "  from app.source_channel order by id limit 200"
)
_DESTINATIONS_SQL = "select id, telegram_channel_id, title, series_id from app.destination order by id limit 200"

#: A series is matched by the same rule `/declare` uses — exact normalised title, or a stored title that
#: contains it — because a discovery answer that resolved names differently would report a channel as
#: addable and then refuse to add it.
_SERIES_SQL = (
    "select id, title from app.series where normalized_title = $1 or normalized_title like '%' || $1 || '%'"
    " order by id limit 3"
)


from .channels import destination_name
from .keys import normalize_title
from .rights import marked_channel_id

__all__ = [
    "AUTO_KEY",
    "ADMIN",
    "MEMBER",
    "OWNER",
    "role_of",
    "series_from_name",
    "classify",
    "group_for_linking",
    "flips_to_apply",
    "add_source",
    "add_destination",
    "sweep",
    "report",
]

#: The three roles this program can tell apart from a dialog entry. Anything beyond these (a moderator who
#: may not post, a restricted account) is decided by `we_are_admin`, which already folds the right question
#: — "may this account post here" — out of the rights it is given.
OWNER = "owner"
ADMIN = "admin"
MEMBER = "member"


def role_of(dialog: Mapping[str, Any]) -> str:
    """Owner, admin, or member — in that order, from what the dialog entry carries.

    `mine` is the `creator` flag and wins over rights because a creator's own rights object can be sparse
    while their authority is absolute. Admin is `post_messages`, not "some admin flag": the difference
    between the two is a channel where we hold a title and cannot publish, and that channel is a source.
    """
    if dialog.get("mine"):
        return OWNER
    rights = dialog.get("rights") or {}
    if rights.get("post_messages"):
        return ADMIN
    return MEMBER


def series_from_name(title: Any, *, template: str = "{TITLE} Anime in Hindi") -> str | None:
    """``"Bleach Anime in Hindi"`` → ``"Bleach"``, and ``None`` for anything else.

    Inverse of the generator, not a regex over a remembered pattern: the candidate series name is passed
    through :func:`app.channels.destination_name` and only a name that comes back identical is accepted.
    That is what lets the template change in one place — a hand-written pattern here would start calling a
    renamed channel "not a destination", which is a silent misclassification of exactly the kind this file
    exists to avoid.
    """
    text = str(title or "").strip()
    if not text:
        return None
    words = text.split()
    for take in range(len(words), 0, -1):
        candidate = " ".join(words[:take])
        if destination_name(candidate, template=template).casefold() == text.casefold():
            return candidate
    return None


def classify(
    dialogs: Sequence[Mapping[str, Any]],
    *,
    sources: Sequence[Mapping[str, Any]] = (),
    destinations: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Sort what the session sees into findings, and say what was skipped.

    Returns ``{"findings": […], "skipped": {"left": n, "not_channels": n}, "configured": [title…],
    "duplicates": [title…]}``.

    A finding is a *proposal*, so it carries the reason next to the verdict: the operator reads both in one
    line and can see why a channel they expected did not appear. ``configured`` is not padding — "nothing
    was found" and "everything you own is already set up" have to look different on the screen.
    """
    by_id = {_marked(row.get("telegram_channel_id")): row for row in sources if _marked(row.get("telegram_channel_id"))}
    dest_by_id = {
        _marked(row.get("telegram_channel_id")): row for row in destinations if _marked(row.get("telegram_channel_id"))
    }
    findings: list[dict[str, Any]] = []
    skipped = {"left": 0, "not_channels": 0}
    configured: list[str] = []
    seen: set[int] = set()
    duplicates: list[str] = []
    for index, dialog in enumerate(dialogs, start=1):
        if dialog.get("left"):
            skipped["left"] += 1
            continue
        if not dialog.get("channel"):
            # A group, a user, a saved-message chat: none of them can hold a series' files in the sense the
            # pipeline means, and calling one a source would make the scanner read a conversation.
            skipped["not_channels"] += 1
            continue
        channel = _marked(dialog.get("id"))
        if channel is None:
            skipped["not_channels"] += 1
            continue
        if channel in seen:
            duplicates.append(str(dialog.get("title") or channel))
            continue
        seen.add(channel)
        role = role_of(dialog)
        row = by_id.get(channel)
        destination = dest_by_id.get(channel)
        title = str(dialog.get("title") or "")
        if row is not None or destination is not None:
            which = destination if destination is not None else row
            what = "destination" if destination is not None else "source"
            configured.append(f"{title or channel} · {what} row {int(which['id'])}")
            continue
        series = series_from_name(title)
        finding: dict[str, Any] = {
            "index": index,
            "channel": channel,
            # Telegram's own unmarked id, kept beside the marked one: `sourcecfg.plan_new` compares the
            # number against the entity it was given, and it can only do that check if both spellings arrive.
            "raw_id": dialog.get("id"),
            "username": str(dialog.get("username") or "").lstrip("@") or None,
            "title": title or None,
            "members": dialog.get("members"),
            "role": role,
            "use": None,
            "series": series,
            "why": "",
        }
        if role in (OWNER, ADMIN):
            if series is None:
                finding["why"] = (
                    "we can post here, so this is a publishing channel — but its name does not follow the "
                    "destination rule, so no series can be read out of it. Name the series and it can be "
                    "added; a title is not a series."
                )
            else:
                finding["use"] = "destination"
                finding["why"] = f"we run this channel and its name is the destination name for {series}"
        else:
            finding["use"] = "source"
            finding["why"] = "we can only read it, so its files are something to take in, not something to post"
        findings.append(finding)
    return {
        "findings": findings,
        "skipped": skipped,
        "configured": configured,
        "duplicates": duplicates,
        "read": len(dialogs),
    }


def group_for_linking(findings: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Findings by series: which channels would read into which publishing channel.

    Only findings with a series are grouped; a member channel with no declared series is still worth adding
    as a source (its own group, keyed by its channel id) but cannot be linked anywhere yet. ``destination``
    is None until an admin channel named for that series turns up, which is the normal state on day one.
    """
    groups: dict[str, dict[str, Any]] = {}
    for finding in findings:
        if finding.get("use") not in ("source", "destination"):
            continue
        key = str(finding.get("series") or f"#{finding['channel']}")
        group = groups.setdefault(key, {"series": finding.get("series"), "sources": [], "destination": None})
        if finding["use"] == "destination":
            if group["destination"] is not None:
                # Two channels named for one series is the operator's data to fix, not a coin toss.
                group.setdefault("conflicts", []).append(finding)
                continue
            group["destination"] = finding
        else:
            group["sources"].append(finding)
    return groups


def flips_to_apply(
    sources: Sequence[Mapping[str, Any]],
    dialogs_by_channel: Mapping[int, Mapping[str, Any]],
    *,
    watched_by_series: Mapping[str, int],
) -> list[dict[str, Any]]:
    """The channels that were sources when last read and are publishing channels now.

    ``watched_by_series`` is how many *other* watched rows read for each series; a flip is refused when that
    number is zero for the series involved, because "stop reading here" with nothing else to read from turns
    a live series into a silent one. The refusal is returned as a finding with ``ok=False`` rather than
    dropped, so the operator can see the case and say what to do about it.
    """
    out: list[dict[str, Any]] = []
    for row in sources:
        channel = _marked(row.get("telegram_channel_id"))
        if channel is None:
            continue
        dialog = dialogs_by_channel.get(channel)
        if dialog is None or dialog.get("left"):
            # Absence is not "member" — the same rule `app/rights.py` holds to. A channel the session lost
            # sight of keeps its old mode instead of being switched off by a read that never happened.
            continue
        if role_of(dialog) == MEMBER:
            continue
        if str(row.get("mode")) == "ignore":
            # Already switched: reading stopped here on an earlier sweep, so there is nothing to say again.
            # The mode and not the rights flag is the test, because the flag is what this same sweep just
            # rewrote and a decision that reads its own write back as news is a loop that never settles.
            continue
        series = str(row.get("declared_series") or row.get("title") or "").strip()
        others = int(watched_by_series.get(series, 1)) - (1 if str(row.get("mode")) == "full" else 0)
        finding: dict[str, Any] = {
            "row_id": int(row["id"]),
            "channel": channel,
            "title": str(row.get("title") or dialog.get("title") or channel),
            "series": series or None,
            "name_series": series_from_name(dialog.get("title")),
            "use": "flip",
            "ok": others > 0 or not series,
        }
        if not finding["ok"]:
            finding["why"] = (
                f"this is the only watched channel reading {series}, so making it a publishing channel would "
                "leave the series with nothing to read. add another source for it first, then this switches "
                "by itself on the next sweep."
            )
        else:
            finding["why"] = (
                "you can post in a channel this service reads files from, so it is where posts go rather "
                "than where they come from: reading stops here, and the series' destination points at it."
            )
        out.append(finding)
    return out


async def add_source(db: Any, finding: Mapping[str, Any]) -> dict[str, Any]:
    """Write the source row for a channel the account can read, through the command's own writer.

    `app/sourcecfg.plan_new` decides the defaults and refuses what it cannot address, so discovery writes a
    row exactly the way a typed `/source <channel> add` does — including the refusal for a handle with no
    number, which cannot happen here (the dialog carries Telegram's own id) but must not need a second rule.
    """
    entity = {
        "id": finding.get("raw_id"),
        "username": finding.get("username"),
        "title": finding.get("title"),
    }
    plan = sourcecfg.plan_new(str(finding.get("channel") or ""), entity=entity)
    if isinstance(plan, str):
        return {"ok": False, "use": "row", "what": "source", "title": finding.get("title") or finding.get("channel"), "text": plan}
    new_id = await sourcecfg.insert_channel(db, plan)
    if new_id is None:
        return {
            "ok": False,
            "use": "row",
            "title": finding.get("title") or finding.get("channel"),
            "what": "source",
            "text": "that channel already has a source row — the table keeps one per channel, so nothing "
            "was written twice",
        }
    return {
        "ok": True,
        "use": "row",
        "what": "source",
        "row_id": new_id,
        "title": finding.get("title") or f"@{finding.get('username') or finding.get('channel')}",
        "text": sourcecfg.render_plan(plan),
    }


async def add_destination(db: Any, finding: Mapping[str, Any]) -> dict[str, Any]:
    """Point a series at the channel we administer, once the series exists to be pointed.

    The series is *looked up*, never founded: `app.destination.series_id` is not null, and creating a series
    from a channel title is the one guess this feature would pay for later — a wrong series name on a
    destination decides where every future post of a season lands.
    """
    series_name = str(finding.get("series") or "").strip()
    if not series_name:
        return {
            "ok": False,
            "use": "row",
            "title": finding.get("title") or finding.get("channel"),
            "what": "destination",
            "text": "no series can be read out of that channel's name, so there is nothing to point it at. "
            "Name the series on the source channel that feeds it (/source <channel> series <name>) and the "
            "destination follows the same rule.",
        }
    slug = normalize_title(series_name)
    rows = list(await db.fetch(_SERIES_SQL, slug) or [])
    if not rows:
        return {
            "ok": False,
            "use": "row",
            "title": finding.get("title") or finding.get("channel"),
            "what": "destination",
            "text": f"no series called {series_name!r} is stored yet, so this channel has no series to be "
            "the destination of. A series is founded by the first file filed for it — until then nothing "
            "here can be pointed at it, and inventing the row would put the cart before the files.",
        }
    if len(rows) > 1:
        names = ", ".join(str(row.get("title")) for row in rows)
        return {
            "ok": False,
            "use": "row",
            "title": finding.get("title") or finding.get("channel"),
            "what": "destination",
            "text": f"{series_name!r} matches more than one stored series ({names}), so I am not picking "
            "one for a channel that will hold every post of it",
        }
    plan = sourcecfg.plan_new_destination(rows[0], finding.get("channel"), title=finding.get("title"))
    if isinstance(plan, str):
        return {
            "ok": False,
            "use": "row",
            "title": finding.get("title") or finding.get("channel"),
            "what": "destination",
            "text": plan,
        }
    new_id = await sourcecfg.insert_destination(db, plan)
    if new_id is None:
        return {
            "ok": False,
            "use": "row",
            "title": finding.get("title") or finding.get("channel"),
            "what": "destination",
            "text": "that channel is already a destination row, so nothing was written twice",
        }
    return {
        "ok": True,
        "use": "row",
        "what": "destination",
        "row_id": new_id,
        "text": (
            f"destination row {new_id} written for {plan['series_title']}: `{plan['title'] or finding.get('channel')}` "
            f"(`{plan['telegram_channel_id']}`), publishing as a link post. Nothing was created in Telegram "
            "and nothing was posted — the row is the record that this channel is where that series goes."
        ),
    }


async def sweep(db: Any, dialogs: Sequence[Mapping[str, Any]], *, auto: bool) -> dict[str, Any]:
    """Read the roles, write the rights column, and — when told to — switch what a role change means.

    Takes the dialog entries rather than a client, so the worker's reconciliation loop and the control bot's
    `/discover` screen run the same code on the same read. The rights record happens whether or not `auto`
    is on: noticing is free, and a switch is the only part that needs permission.
    """
    from .probe import DIALOG_LIMIT  # noqa: PLC0415  (probe imports rights; this keeps that one-way)

    sources = list(await db.fetch(_SOURCES_SQL) or [])
    destinations = list(await db.fetch(_DESTINATIONS_SQL) or [])
    plan = classify(dialogs[:DIALOG_LIMIT], sources=sources, destinations=destinations)
    counted = await channel_rights.record(db, channel_rights.plan(dialogs, sources))
    watched: dict[str, int] = {}
    for row in sources:
        if str(row.get("mode")) == sourcecfg.MODE_WATCHING:
            key = str(row.get("declared_series") or row.get("title") or "").strip()
            watched[key] = watched.get(key, 0) + 1
    by_channel = {
        marked_channel_id(entry.get("id")): entry
        for entry in dialogs
        if marked_channel_id(entry.get("id")) is not None
    }
    flips = flips_to_apply(sources, by_channel, watched_by_series=watched)
    applied: list[dict[str, Any]] = []
    for flip in flips:
        if not (auto and flip.get("ok")):
            applied.append({**flip, "applied": False})
            continue
        await sourcecfg.set_flag(db, int(flip["row_id"]), sourcecfg.TOGGLES["watch"], False)
        applied.append({**flip, "applied": True})
    return {"plan": plan, "rights": counted, "flips": applied, "watched_by_series": watched}


def report(plan: Mapping[str, Any], *, auto: bool, applied: Sequence[Mapping[str, Any]] = ()) -> str:
    """The message: what was seen, what would be written, and what was refused — in that order.

    The counts come first because "12 channels read, 4 worth adding" is what tells the operator whether to
    keep reading at all, and because a report that led with a list would look complete while skipping half
    the dialogs.
    """
    findings = list(plan.get("findings") or [])
    skipped = plan.get("skipped") or {}
    lines = [
        f"{plan.get('read', 0)} dialogs read: {len(findings)} worth a decision, "
        f"{len(plan.get('configured') or [])} already configured"
    ]
    counted = {label: count for label, count in (skipped or {}).items() if count}
    if counted:
        lines.append(
            "skipped, on purpose: " + ", ".join(f"{count} {label.replace('_', ' ')}" for label, count in counted.items())
        )
    for finding in findings:
        who = finding.get("title") or (f"@{finding['username']}" if finding.get("username") else finding["channel"])
        verdict = finding.get("use") or "nothing yet"
        lines.append(f"  • {who} — {finding['role']}, {verdict}")
        if finding.get("why"):
            lines.append(f"    {finding['why']}")
    configured = list(plan.get("configured") or [])
    if configured:
        # Named rather than counted, because "1 already configured" is the sentence an operator reads as
        # "it is missing from the list by accident" and then goes looking for a bug. The cap is said, with
        # the place the rest are listed: this screen is a page, not the whole table.
        lines.append("already configured: " + ", ".join(configured[:6]))
        if len(configured) > 6:
            lines.append(f"  … and {len(configured) - 6} more, which /sources lists")
    if plan.get("duplicates"):
        lines.append(
            "the session listed one of these twice, so it was decided from the first entry only: "
            + ", ".join(str(one) for one in plan["duplicates"][:4])
        )
    for item in applied:
        # Two kinds of outcome share this list, and they are not the same verb: a row was *written*, a
        # channel was *switched*. Printing one word for both is how a report claims it created a channel.
        verb = "switched" if item.get("use") == "flip" else "written"
        lines.append(
            f"  • {item.get('title')}: {verb if item.get('applied') else 'not ' + verb} — {item.get('why', '')}"
        )
    if auto:
        lines.append(
            "auto is on: the roles are read again by every reconciliation — which runs at boot and "
            "periodically after it — and whenever this screen is opened. A channel you start administering "
            "is switched the first time any of those notices, and never before."
        )
    else:
        lines.append("auto is off, so nothing above was written by itself and nothing will be until you tap it.")
    return "\n".join(lines)


def _marked(value: Any) -> int | None:
    """The channel id in the form both sides agree on, or None."""
    return marked_channel_id(value)
