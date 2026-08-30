"""Channel rows the operator used to be sent to a dashboard for: the source channel, the archive, and
the switches on them — all from the control bot instead.

Why this moved here. On 2026-08-29 the operator was told to add a channel by hand in the Supabase
table editor and answered, in their words: *"mai baar baar supabase nhi kholne wala … bot me hi toggle
on/off jaise options jod do"*. They own the database, this bot already writes to it, and the two
writers in this service (this bot and the worker) are the only ones there are — so a config screen in
the chat window is not a new authority, it is the same one with better manners.

What was worth keeping from the refusal it replaced: a row in `app.source_channel` is what starts this
service watching a channel, so making one has to be a stated decision. Hence `add` being its own verb
rather than something `/source` does when it fails to find the channel it was asked about, and hence the
defaults being printed back rather than applied quietly.

Two rules hold this module together:

* **Only a column production code reads may be touched here.** `app.source_channel` also holds
  ``priority``, ``is_joined``, ``active`` and the ``monitor_only`` mode, and nothing outside a probe
  query acts on any of them. A switch that writes a value nobody reads is how a config screen starts
  lying about itself, so the three switches below are all a test can name a reader for — and
  ``tests/test_sourcecfg.py`` checks that, in this repository, rather than trusting this sentence.
* **Nothing destructive.** One insert, and updates that each name a single column. No delete, no
  overwrite of a row that exists: ``add`` on a channel that is already configured says so and writes
  nothing, and every switch here can be switched back by the same command.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .rights import marked_channel_id

__all__ = [
    "FLAG_COLUMNS",
    "BY_COLUMN",
    "TOGGLES",
    "Toggle",
    "flags_line",
    "DESTINATION_MODE",
    "insert_archive",
    "insert_channel",
    "insert_destination",
    "link_destination",
    "plan_new_destination",
    "plan_archive",
    "parse_toggle",
    "plan_new",
    "render_plan",
    "set_flag",
    "channel_entity",
    "setup_refusal",
    "toggle_state",
]

#: The mode column's two values this program acts on. ``monitor_only`` exists in the enum and is
#: deliberately absent: no code compares against it, so offering it would be offering a decoration.
MODE_WATCHING = "full"
MODE_IGNORED = "ignore"


#: A destination found by discovery starts on the link route: its channel already exists, and whether the
#: files in it should be captioned in place instead is `/inplace`'s decision, not a guess this module can
#: make on the operator's behalf.
DESTINATION_MODE = "link_post"


@dataclass(frozen=True)
class Toggle:
    """One switch the operator can flip, and exactly what flipping it changes.

    ``effect`` is not decoration. Each line has to be something a reader can check against the code,
    because the failure this module is built to avoid is a config screen that promises more than the
    column it writes delivers.
    """

    name: str
    column: str
    # ``name`` is what the operator types, and it is chosen to collide with nothing the same command
    # already takes as a *value*: DECLARED_AUDIO in app/normalize.py offers `hindi`, `subbed`, `dual`,
    # and `unknown`, so a switch called `hindi` swallowed the word out of `/source x audio hindi` and
    # left that command reporting "audio needs a value". A test in tests/test_sourcecfg.py pins the
    # two lists apart, because a collision of that kind is invisible until a working command breaks.
    label: str
    on_text: str
    off_text: str
    effect: str
    on_value: Any
    off_value: Any

    def value(self, on: bool) -> Any:
        return self.on_value if on else self.off_value


TOGGLES: dict[str, Toggle] = {
    toggle.name: toggle
    for toggle in (
        Toggle(
            name="gate",
            column="require_hindi_audio",
            label="Hindi-audio check",
            on_text="on — a file with no language in its name or caption is not published",
            off_text="off — a file with no language text is accepted as it arrives",
            effect="the gate that stops an unsubbed file going out as a Hindi one",
            on_value=True,
            off_value=False,
        ),
        Toggle(
            name="subs",
            column="include_subbed",
            label="subbed-only files",
            on_text="on — subbed-only files from this channel are in scope",
            off_text="off — subbed-only files are left alone",
            effect="what a source with no Hindi track in it is still worth reading",
            on_value=True,
            off_value=False,
        ),
        Toggle(
            name="watch",
            column="mode",
            label="watching this channel",
            on_text="on — files are read as they arrive",
            off_text="off — files from this channel are recorded as nothing to do",
            effect="the difference between this channel being a source and being a row",
            on_value=MODE_WATCHING,
            off_value=MODE_IGNORED,
        ),
    )
}

#: The same switches indexed by the column they write: a plan is built from column names, and a
#: lookup by the word an operator types would let a rename here change what a row means.
BY_COLUMN: dict[str, Toggle] = {toggle.column: toggle for toggle in TOGGLES.values()}

#: Every column this module may write, for the test that asks who reads them.
FLAG_COLUMNS: tuple[str, ...] = tuple(toggle.column for toggle in TOGGLES.values())

ON_WORDS = frozenset({"on", "haan", "ha", "yes", "true", "1", "chalu", "start", "kar"})
OFF_WORDS = frozenset({"off", "nahi", "no", "false", "0", "band", "stop", "mat"})


def channel_entity(entity: Any) -> dict[str, Any] | str:
    """What Telegram said about an entity, in the three fields a row needs — or a refusal.

    Only a channel may become a source. ``get_entity`` happily answers with a user, a bot or a group,
    and ``broadcast`` is the one flag that tells those apart, so the check is made here rather than in
    the handler: a ``User`` written into `app.source_channel` would sit there looking configured until
    the first forward from it failed in a job nobody was watching.
    """
    if entity is None:
        return "Telegram did not answer."
    if getattr(entity, "broadcast", None) is None:
        kind = type(entity).__name__
        return (
            f"`{getattr(entity, 'username', None) or getattr(entity, 'id', '?')}` is a {kind} on "
            "Telegram, not a channel, so it cannot be a source. A source channel is one you post files "
            "into; a group or an account is not."
        )
    return {
        "id": getattr(entity, "id", None),
        "username": getattr(entity, "username", None),
        "title": getattr(entity, "title", None),
    }


def parse_toggle(word: str) -> Toggle | None:
    return TOGGLES.get(str(word or "").strip().casefold())


def toggle_state(row: Mapping[str, Any], toggle: Toggle) -> bool | None:
    """Where one switch stands for one channel, or None when the row does not answer.

    ``None`` matters. A stored ``mode`` of something other than the two values here, or a column that
    came back null, is reported as unread rather than defaulted to "off" — a screen that renders an
    unknown as a position is a screen that will be trusted.
    """
    raw = row.get(toggle.column)
    if raw is None:
        return None
    if toggle.column == "mode":
        text = str(raw).strip().casefold()
        if text == MODE_WATCHING:
            return True
        if text == MODE_IGNORED:
            return False
        return None
    return bool(raw)


def flags_line(row: Mapping[str, Any]) -> str:
    """The three switches, as one block of text, in the order the command prints them."""
    lines = []
    for toggle in TOGGLES.values():
        state = toggle_state(row, toggle)
        mark = "on " if state else ("off" if state is False else "?? ")
        lines.append(f"  {mark} {toggle.name}: {toggle.label}")
    return "\n".join(lines)


def setup_refusal(handle: str) -> str:
    """The refusal a lookup failure gets, now that the next click is a command and not a dashboard.

    Kept as its own function because `app/controlbot.py` returns it from three places and the sentence
    has to stay one sentence: "I do not create rows" was true, useful and un-actionable when it was all
    it said.
    """
    return (
        f"`{handle}` is not a configured source channel, so there is nothing to declare about it.\n"
        "To start watching it: /source "
        f"{handle} add\n"
        "That writes the row, with the defaults printed back, and /source <channel> shows the switches "
        "you can flip afterwards. Nothing else in this program creates a source channel: adding one is "
        "the decision to start reading a channel, so it stays its own command.\n\n"
        "The dashboard still works if you would rather set more than the defaults there — Supabase → "
        "Table editor → app.source_channel → Insert row, and `telegram_channel_id` is the only field "
        "the table needs."
    )


def _channel_id_of(typed: Any, entity: Mapping[str, Any] | None) -> tuple[int | None, str | None]:
    """The channel number, from whichever of the two sources is allowed to answer, and why not.

    Shared by the source row and the archive row on purpose: a row whose job is naming a channel must
    be named the same way in both tables, and a second copy of this logic is how one table ends up
    accepting a spelling the other refuses.
    """
    from_id: int | None = None
    if entity is not None:
        from_id = marked_channel_id(entity.get("id"))
        if from_id is None:
            return None, (
                "Telegram answered, but the answer had no channel number in it, so I will not write one "
                "from a guess."
            )
    numeric = marked_channel_id(str(typed or "").strip())
    if from_id is not None and numeric is not None and from_id != numeric:
        # Both spellings present and disagreeing is the one case where picking one would be guessing at
        # which channel the operator meant, in a row whose whole job is naming a channel.
        return None, (
            f"that @handle belongs to channel `{from_id}` on Telegram, and you also gave me `{numeric}`. "
            "I will not pick — send the handle or the number, and I will use the one you sent."
        )
    channel_id = from_id if from_id is not None else numeric
    if channel_id is None:
        return None, (
            f"`{typed}` is not a channel I can address. Either its @handle (`@my_channel`) or the number "
            "Telegram gives it (`-100xxxxxxxxxx`, minus sign and all). A private channel's number is in "
            "any link to a post inside it: `t.me/c/2575861262/5` is channel `-1002575861262`."
        )
    return channel_id, None


def plan_new(
    typed: str,
    *,
    entity: Mapping[str, Any] | None = None,
    title: str | None = None,
    series: str | None = None,
    declared_by: str = "operator",
) -> dict[str, Any] | str:
    """Decide what the new row should hold. Returns a message when it cannot decide honestly.

    ``entity`` is what Telegram said about the channel — a mapping with ``id``, ``username``, ``title``,
    and nothing invented in it. When it is None, the operator typed a channel number we could not ask
    Telegram about, and ``verified`` comes back False so the reply can say so: an id that was never
    looked up is a hope, not a check, and the difference shows up the first time a forward fails.
    """
    text = str(typed or "").strip()
    if not text:
        return "add needs the channel: /source <@handle or channel id> add"

    channel_id, problem = _channel_id_of(text, entity)
    if problem:
        return problem

    username = None
    if entity is not None and entity.get("username"):
        username = str(entity["username"]).strip().lstrip("@") or None
    elif not text.lstrip("-").isdigit():
        username = text.lstrip("@") or None

    stored_title = None
    if entity is not None and entity.get("title"):
        stored_title = str(entity["title"]).strip()[:120] or None
    elif title:
        stored_title = str(title).strip()[:120] or None

    return {
        "telegram_channel_id": channel_id,
        "username": username,
        "title": stored_title,
        "mode": MODE_WATCHING,
        "active": True,
        "require_hindi_audio": True,
        "include_subbed": False,
        "series": str(series).strip() if series else None,
        # Who is standing behind the series name. `operator` is the truth for a typed line and a lie for a
        # name read off a channel title, and `/status` prints the difference: "the file said Hindi" has to
        # stay tellable apart from "you told me to assume it".
        "declared_by": str(declared_by or "operator").strip()[:60],
        "verified": entity is not None,
    }


def render_plan(plan: Mapping[str, Any]) -> str:
    """What is about to be written, in the same words the reply will use afterwards."""
    who = plan.get("username") or plan.get("title")
    named = f"source channel {who} (`{plan['telegram_channel_id']}`):" if who else (
        f"source channel `{plan['telegram_channel_id']}`:"
    )
    lines = [named,
        "  watching: on (mode full) — a channel that is not watched is a row, not a source",
        f"  {BY_COLUMN['require_hindi_audio'].label}: {BY_COLUMN['require_hindi_audio'].on_text}",
        f"  {BY_COLUMN['include_subbed'].label}: {BY_COLUMN['include_subbed'].off_text}",
    ]
    if plan.get("series"):
        lines.append(f"  series declared for everything from it: {plan['series']}")
    if not plan.get("verified"):
        lines.append(
            "  not checked against Telegram: you gave me a number and this deployment could not ask "
            "about it. The first file from that channel is the check — if the number is wrong it will "
            "name the channel it could not find, and this row is switched off by /source "
            f"{who} watch off."
        )
    lines.append("  nothing was deleted, and no file has been touched by writing this row.")
    return "\n".join(lines)


def plan_new_destination(
    series: Mapping[str, Any] | None,
    typed: Any,
    *,
    entity: Mapping[str, Any] | None = None,
    title: str | None = None,
) -> dict[str, Any] | str:
    """A destination row for a channel that already exists, or a message saying why there cannot be one.

    ``series`` is the row from ``app.series`` — its id and title, read by the caller. Nothing here looks a
    series up or creates one: `series_id` is not null on this table, and inventing a series to satisfy a
    constraint is the exact silent guess the whole discovery feature exists to refuse.
    """
    if series is None or series.get("id") is None:
        return (
            "no series row matches that name, so there is nothing to point the channel at. A series is "
            "founded by the first file filed for it, or declared with /declare — and until one exists, "
            "this channel has no series to be the destination of."
        )
    channel_id, problem = _channel_id_of(str(typed or ""), entity)
    if problem:
        return problem
    stored_title = None
    if entity is not None and entity.get("title"):
        stored_title = str(entity["title"]).strip()[:120] or None
    elif title:
        stored_title = str(title).strip()[:120] or None
    return {
        "series_id": int(series["id"]),
        "series_title": str(series.get("title") or ""),
        "telegram_channel_id": channel_id,
        "title": stored_title,
        "publish_mode": DESTINATION_MODE,
        "verified": entity is not None,
    }


async def link_destination(db: Any, source_id: int, destination_id: int) -> None:
    """Point a source row at the channel its files are published into.

    One column, and the pipeline already reads it: `app/controlbot.py`'s `/inplace` looks a destination up by
    `destination_id` first and by channel number second, and the console's cross-row button refuses until it
    is set. Nothing else in this program wrote it before now, which is the reason a paired channel looked
    unpaired on screen.
    """
    await db.execute(
        "update app.source_channel set destination_id = $2, updated_at = now() where id = $1",
        int(source_id),
        int(destination_id),
    )


async def insert_destination(db: Any, plan: Mapping[str, Any]) -> int | None:
    """Write the destination row. One per channel number, and never a second for the same series.

    ``on conflict (telegram_channel_id) do nothing`` matches how the source row is inserted, for the same
    reason: the unique index is the only thing that can see two of these at once. The series check is a
    *read* by the caller rather than a constraint here, because the pipeline joins a destination to a
    series by ``series_id`` and a second row for one series is a decision the operator should see being
    refused rather than a row that quietly changes which of two channels a season publishes into.
    """
    return await db.fetchval(
        """
        insert into app.destination (
            series_id, telegram_channel_id, title, publish_mode, created_at, updated_at
        ) values (
            $1, $2, $3, $4, now(), now()
        )
        on conflict (telegram_channel_id) do nothing
        returning id
        """,
        plan["series_id"],
        plan["telegram_channel_id"],
        plan["title"],
        plan["publish_mode"],
    )


async def insert_channel(db: Any, plan: Mapping[str, Any]) -> int | None:
    """Write the row. Returns its id, or None when the channel number was already taken.

    ``on conflict do nothing`` rather than a read-then-write: two of these commands can arrive while a
    third is in flight, and the unique index is the only thing that can see both. The caller has already
    looked, so None here is a rare race and not the normal path — which is why the caller still has to
    handle it as "someone else configured it" and not as an error.
    """
    new_id = await db.fetchval(
        """
        insert into app.source_channel (
            telegram_channel_id, username, title, mode, active, require_hindi_audio, include_subbed,
            created_at, updated_at
        ) values (
            $1, $2, $3, $4::app.channel_mode, $5, $6, $7, now(), now()
        )
        on conflict (telegram_channel_id) do nothing
        returning id
        """,
        plan["telegram_channel_id"],
        plan["username"],
        plan["title"],
        plan["mode"],
        plan["active"],
        plan["require_hindi_audio"],
        plan["include_subbed"],
    )
    if new_id is None:
        return None
    if plan.get("series"):
        await db.execute(
            "update app.source_channel set declared_series = $2, declared_by = $3,"
            " declared_at = now(), updated_at = now() where id = $1",
            int(new_id),
            str(plan["series"]),
            str(plan.get("declared_by") or "operator"),
        )
    return int(new_id)


def plan_archive(
    typed: str,
    *,
    entity: Mapping[str, Any] | None = None,
    title: str | None = None,
    primary: bool = True,
) -> dict[str, Any] | str:
    """The private master archive's row, with the same rule as the source row: nothing invented.

    ``primary`` is not a style choice. `app/writers.py` picks the archive with
    ``order by is_primary desc nulls last, id limit 1``, so a second row marked primary makes the
    destination of the only spare copy of an episode depend on which row Postgres happens to read
    first. So the caller answers one question — is there an archive row yet — and this writes True
    only for the first, and says which one it chose.
    """
    channel_id, problem = _channel_id_of(typed, entity)
    if problem:
        return problem
    stored_title = None
    if entity is not None and entity.get("title"):
        stored_title = str(entity["title"]).strip()[:120] or None
    elif title:
        stored_title = str(title).strip()[:120] or None
    if stored_title is None:
        return (
            "the archive channel needs a title in the row, because it is the one channel nobody is "
            "reading messages from and so nothing else names it. add it: /archive "
            f"{typed} add title <what you call it>"
        )
    return {
        "telegram_channel_id": channel_id,
        "title": stored_title,
        "is_primary": bool(primary),
        "verified": entity is not None,
    }


def render_archive_plan(plan: Mapping[str, Any]) -> str:
    lines = [
        f"archive channel `{plan['title']}` (`{plan['telegram_channel_id']}`):",
        "  primary: "
        + ("yes — this is where the spare copy of every file goes" if plan["is_primary"] else
           "no — an archive row is already primary, so this one waits its turn"),
        "  nothing is read *from* here, and nothing here is ever deleted by this program.",
    ]
    if not plan["verified"]:
        lines.append(
            "  not checked against Telegram: the number is yours, not one the session confirmed."
        )
    return "\n".join(lines)


async def insert_archive(db: Any, plan: Mapping[str, Any]) -> int | None:
    """Write the archive row. None when that channel number is already listed as an archive."""
    new_id = await db.fetchval(
        "insert into app.archive_channel (telegram_channel_id, title, is_primary, created_at)"
        " values ($1, $2, $3, now())"
        " on conflict (telegram_channel_id) do nothing"
        " returning id",
        plan["telegram_channel_id"],
        plan["title"],
        plan["is_primary"],
    )
    return None if new_id is None else int(new_id)


async def set_flag(db: Any, channel_id: int, toggle: Toggle, on: bool) -> None:
    """Flip one switch on one channel. One column per statement, so no update can half-apply two.

    ``active`` is left alone on purpose. It exists on the row and the probe reads it, but nothing that
    claims jobs does, so a switch there would look like a pause button and not be one. ``watch`` writes
    ``mode``, which `app/ingest.py` compares against — that is the pause button that works today.
    """
    await db.execute(
        f"update app.source_channel set {toggle.column} = $2, updated_at = now() where id = $1",
        int(channel_id),
        toggle.value(on),
    )
