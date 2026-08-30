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
* **A destination needs a series, and the only name a channel has is its own.** `app.destination.series_id`
  is not null, so a channel becomes a destination when a series can be named for it — the channel title with
  :func:`app.channels.destination_name`'s suffix removed when the title is in that form (checked against that
  function, the one that generates the name, never a copy of the rule), and otherwise the title as it
  stands. A channel we post in that nothing readable matches is still reported, with its role and the reason
  it is not wired yet; the series name it would be filed under is said out loud before anything is written.
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
#: How many channels the automatic sweep re-reads rights for. The screen asks about 80, because the answer
#: to "where am I admin" is the whole reason to open it; this one runs inside the worker's loop, where
#: spending thirty seconds on rights would delay the files. A channel past the cap keeps the session's cached
#: answer and the screen reports it as unverified — never quietly re-decided into a different role here.
AUTO_RIGHTS_LIMIT = 25

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
    "AUTO_RIGHTS_LIMIT",
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
    "name_key",
    "pair_findings",
    "_row_id",
    "apply_pair",
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


def name_key(title: Any) -> tuple[str, str] | None:
    """The name a channel is filed under, in both spellings: ``(normalised key, name to show)``.

    Two channels belong to the same series when the *titles* match once the destination suffix is removed —
    which is the operator's own rule, stated as "if i have 1 source channel of mob sycho 100 then that source
    channel should auto connect with its destination channel that name is also mob psycho 100, the differrnce
    is only that the account is admin or owner". The old pairing signal, `app/channels.py`'s template, is
    folded in here rather than dropped: `{TITLE} Anime in Hindi` strips down to `{TITLE}` and lands in the
    same group as a channel that is just called `{TITLE}`. The template stays the *only* place a name is
    spelled out, so changing it in the dashboard moves both sides together instead of pairing one half.
    """
    text = str(title or "").strip()
    if not text:
        return None
    bare = series_from_name(text) or text
    return normalize_title(bare), bare


def pair_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group findings by channel name, and say which of them are the two halves of one series.

    A group is pairable when one side can be posted in and the other can only be read: the whole
    distinction between a source and a destination in this program is what *this account* may do there, so a
    pair is not a guess about the operator's intent, it is the intent. A group with one side only is still
    useful and stays a single finding — an unread channel is worth watching even with nowhere to publish,
    and a channel we administer is worth filing as a destination even with nothing feeding it yet.

    A group with two postable channels is left unpaired on purpose. Which of them is "the" channel of that
    series is the operator's decision, and picking the first one would put files in a channel they did not
    choose — the same reasoning that keeps a destination with two names in it out of the auto path.
    """
    from collections import defaultdict

    groups: dict[str, dict[str, Any]] = defaultdict(lambda: {"post": [], "read": []})
    for finding in findings:
        key = name_key(finding.get("title"))
        finding["key"] = key[0] if key else None
        finding["series_name"] = key[1] if key else None
        if key is None:
            continue
        groups[key[0]]["post" if finding["use"] == "destination" else "read"].append(finding)
    pairs: list[dict[str, Any]] = []
    for key, group in sorted(groups.items()):
        posts, reads = group["post"], group["read"]
        if not reads or not posts:
            continue
        if len(posts) > 1:
            # Which of two identical-named channels holds the series is the operator's data to fix. Picking
            # the first one would put every future post of a season into a channel they did not choose, so
            # the group is refused and the reason is attached where they will read it.
            for post in posts:
                post["ambiguous"] = (
                    f"{len(posts)} channels of this name can be posted in, so which one is the destination "
                    "is your call — add the one you mean with its number"
                )
            continue
        destination = posts[0]
        index = len(pairs) + 1
        series = (reads[0].get("series_name") or destination.get("series_name") or "").strip()
        pair = {
            "index": index,
            "key": key,
            "series": series,
            "destination": destination,
            "sources": reads,
            # Spelled out on the screen, because the channel name is where the series name came from and the
            # operator has to be able to see that before accepting it — not after, in a wrong channel.
            "line": (
                f"{series} — {reads[0].get('title') or reads[0].get('channel')} is read, "
                f"{destination.get('title') or destination.get('channel')} can be posted in"
            ),
        }
        destination["pair"] = index
        destination["series"] = series
        for source in reads:
            source["pair"] = index
            source["series"] = series
        pairs.append(pair)
    for finding in findings:
        if finding.get("series") is None and finding.get("series_name"):
            # Sources and destinations alike: the channel's own name is the only name it has. For a source
            # that is what the operator asked for ("auto detect the series name … by what is uploaded on
            # source channel and what the channel name is"); for a channel we post in and cannot pair, it
            # means the tap says "add this" to a proposal that reads the series out loud, not to a blank.
            # Nothing is wired up either way until a tap says so.
            # A source with nothing to pair with still gets its name from its own channel title, because
            # that is the only name it has and it is the operator's stated rule. What it must not do is
            # *publish*: `link_for_source` below still refuses to invent a destination for it.
            finding["series"] = finding["series_name"]
        finding.setdefault("pair", None)
        finding.setdefault("ambiguous", None)
    return pairs


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
            # Being able to post is the whole test for "destination", so a channel we run is a candidate even
            # when its name is not `{TITLE} Anime in Hindi`. That was the bug the operator hit: their
            # publishing channel is called exactly what its source is called, and a rule that only read the
            # template backwards had no verdict for it at all. `pair_findings` names it from the channel that
            # pairs with it, and `add_destination` is where a group with no name to file it under is refused.
            finding["use"] = "destination"
            finding["why"] = (
                f"we run this channel and its name is the destination name for {series}"
                if series is not None
                else "we can post here, so files could be published into it"
            )
        else:
            finding["use"] = "source"
            finding["why"] = "we can only read it, so its files are something to take in, not something to post"
        findings.append(finding)
    return {
        "findings": findings,
        # Built after the loop, because a pair is a fact about two findings and cannot be decided by one.
        "pairs": pair_findings(findings),
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
    # The channel's own name goes onto the row as its series, because that is what the operator's setup says
    # a name means, and `declared_by` says so too: `app/commands`' `/status` prints where a declaration came
    # from, and "discovered" must never be able to read as "the operator typed this".
    plan = sourcecfg.plan_new(
        str(finding.get("channel") or ""),
        entity=entity,
        title=finding.get("title"),
        series=str(finding.get("series") or "").strip() or None,
        declared_by="discovered from this channel's name",
    )
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
    """Point a series at a channel we can post in, founding the series row when nothing has named it yet.

    The series used to be *looked up only*, on the reasoning that a wrong name on a destination decides where
    every future post of a season lands. That reasoning held while the only name available was one read out of
    the `{TITLE} Anime in Hindi` template; the operator's setup is a pair of channels that are both called
    `Mob Psycho 100`, so refusing left a channel they own, posting, and named after a show permanently
    undetectable. A series row is now founded from the name the *channels* carry — through
    `app/ingest.ensure_series`, the statement the pipeline itself files with, so `normalized_title` is still
    spelled one way in this database — and every reply says the name came from the channel, which is the part
    the operator has to be able to check before they accept a second one. What stays refused: a group with
    two channels of that name in `app.series`, and any channel with no name at all.
    """
    series_name = str(finding.get("series") or "").strip()
    refused = {"use": "row", "what": "destination", "title": finding.get("title") or finding.get("channel")}
    if not series_name:
        return {
            **refused,
            "ok": False,
            "text": "nothing here says what series this channel is for: no channel of the same name is "
            "readable by this account, and this name is not the destination name of anything — so there is "
            "no series to point it at, and a channel title that says nothing is not one either",
        }
    slug = normalize_title(series_name)
    rows = list(await db.fetch(_SERIES_SQL, slug) or [])
    if len(rows) > 1:
        names = ", ".join(str(row.get("title")) for row in rows)
        return {
            **refused,
            "ok": False,
            "text": f"{series_name!r} matches more than one stored series ({names}), so I am not picking "
            "one for a channel that will hold every post of it",
        }
    founded = False
    if rows:
        series_id = int(rows[0]["id"])
    else:
        from .ingest import ensure_series  # noqa: PLC0415  (only this path needs the filing module)

        series_id = int(await ensure_series(db, series_name))
        founded = True
    plan = sourcecfg.plan_new_destination(
        {"id": series_id, "title": series_name}, finding.get("channel"), title=finding.get("title")
    )
    if isinstance(plan, str):
        return {**refused, "ok": False, "text": plan}
    new_id = await sourcecfg.insert_destination(db, plan)
    if new_id is None:
        return {
            **refused,
            "ok": False,
            "text": "that channel is already a destination row, so nothing was written twice",
        }
    text = (
        f"destination row {new_id} written for {plan['series_title']}: `{plan['title'] or finding.get('channel')}` "
        f"(`{plan['telegram_channel_id']}`), publishing as a link post. Nothing was created in Telegram "
        "and nothing was posted — the row is the record that this channel is where that series goes."
    )
    if founded:
        # The one sentence that makes this different from a lookup: the name was not typed by anyone, so the
        # operator has to be able to see it and correct it before the second pair inherits the same mistake.
        text += (
            f"\nseries {series_id} was founded from the channel's own name ({series_name!r}) — check the "
            "spelling, because this is the name every future post of it is filed under"
        )
    return {
        "ok": True,
        "use": "row",
        "what": "destination",
        "row_id": new_id,
        "series_id": series_id,
        "title": finding.get("title") or str(finding.get("channel")),
        "text": text,
    }


async def _row_id(db: Any, table: str, channel: str) -> int | None:
    """The row this channel already has, if any. Both insert paths answer None for that, and a link needs
    the number: re-running a pairing that is half done must finish it, not refuse to touch it."""
    row = await db.fetchrow(f"select id from {table} where telegram_channel_id = $1", channel)
    return None if row is None else int(row["id"])


async def apply_pair(db: Any, pair: Mapping[str, Any]) -> dict[str, Any]:
    """Wire one group up in one go: the series, the channel its files are published into, the channel they
    are read from, and the link between the last two.

    The order is the dependency, not a preference: `app.destination.series_id` is NOT NULL, so the series row
    has to exist before the destination does, and `app.source_channel.destination_id` points at a destination
    row that has to be there to be pointed at. Every write is `app/sourcecfg.py`'s; the series row goes
    through `app/ingest.ensure_series`. Nothing else in this program is touched — no channel is created in
    Telegram, no file is moved, no row is deleted, and the two halves are linked without either one's already
    published post being rewritten.

    An existing row is not an error: `insert_channel` and `insert_destination` answer None for it, the id is
    then looked up so the link can still be made, and a source that had been switched off (by a role flip or
    by hand) is switched back on — the operator's tap is the authority for that, and it is the only change
    this path makes to a row it did not create.
    """
    title = str(pair.get("series") or "").strip()
    destination = pair.get("destination") or {}
    sources = list(pair.get("sources") or [])
    if not title or not destination:
        return {
            "ok": False, "use": "pair", "title": title or "?", "what": "pair",
            "text": "this pair has no name to file it under",
        }
    outcome = await add_destination(db, {**destination, "series": title})
    destination_id = await _row_id(db, "app.destination", str(destination.get("channel")))
    if destination_id is None:
        # Either the write was refused or the row is not readable back; in both cases there is nothing to link
        # sources to, and half a setup is worse than none — a watched channel with no destination posts
        # nothing and looks like the pipeline is broken.
        return {
            "ok": False, "use": "pair", "title": title, "what": "pair",
            "text": str(outcome.get("text") or "the publishing channel could not be filed, so nothing was linked"),
        }
    lines = [
        f"{destination.get('title') or destination['channel']} is {title}'s publishing channel"
        + ("" if outcome.get("ok") else " (its row was already there)"),
        str(outcome.get("text") or ""),
    ]
    linked = 0
    for finding in sources:
        added = await add_source(db, finding)
        source_id = await _row_id(db, "app.source_channel", str(finding.get("channel")))
        if source_id is None:
            lines.append(f"{finding.get('title') or finding.get('channel')}: {added.get('text')}")
            continue
        await sourcecfg.link_destination(db, source_id, destination_id)
        row = await db.fetchrow("select mode from app.source_channel where id = $1", source_id)
        if row is not None and str(row.get("mode") or "") != sourcecfg.MODE_WATCHING:
            await sourcecfg.set_flag(db, source_id, sourcecfg.TOGGLES["watch"], True)
            lines.append(f"{finding.get('title') or finding.get('channel')}: was switched off, and is watching again")
        linked += 1
        lines.append(
            f"{finding.get('title') or finding.get('channel')} is watched, and what it posts is published "
            f"into {destination.get('title') or destination['channel']}"
            + ("" if added.get("ok") else " (its row was already there)"),
        )
    if not linked:
        return {
            "ok": False, "use": "pair", "title": title, "what": "pair",
            "text": "\n".join([line for line in lines if line] + [
                "no readable channel of this name could be filed, so nothing will be published yet"
            ]),
        }
    return {
        "ok": True,
        "use": "pair",
        "what": "pair",
        "title": title,
        "destination_id": destination_id,
        "sources": linked,
        "text": "\n".join(line for line in lines if line),
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
    pairs = list(plan.get("pairs") or [])
    lines = [
        f"{plan.get('read', 0)} dialogs read: {len(findings)} worth a decision, "
        f"{len(plan.get('configured') or [])} already configured"
    ]
    counted = {label: count for label, count in (skipped or {}).items() if count}
    if counted:
        lines.append(
            "skipped, on purpose: " + ", ".join(f"{count} {label.replace('_', ' ')}" for label, count in counted.items())
        )
    if pairs:
        # One line per series, because a show is the unit the operator thinks in and a channel is only half
        # of it; the screen offers one tap per pair, and one tap writes both rows and the link.
        lines.append("pairs I can set up (one tap does both sides and links them):")
        for pair in pairs[:6]:
            readers = list(pair.get("sources") or [])
            titles = ", ".join(str(one.get("title") or one.get("channel")) for one in readers[:3])
            more = len(readers) - 3
            lines.append(
                f"  {pair['index']}. {pair['series']} — reads from {titles}"
                + (f" and {more} more" if more > 0 else "")
                + f", publishes into {pair['destination'].get('title') or pair['destination'].get('channel')}"
            )
        if len(pairs) > 6:
            lines.append(f"  … and {len(pairs) - 6} more pairs, on the screen below")
    for finding in findings:
        who = finding.get("title") or (f"@{finding['username']}" if finding.get("username") else finding["channel"])
        verdict = finding.get("use") or "nothing yet"
        extra = f", pair {finding['pair']}" if finding.get("pair") else ""
        lines.append(f"  • {who} — {finding['role']}, {verdict}{extra}")
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
