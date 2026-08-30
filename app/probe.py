"""Read-only discovery against the three bots, run from the deployed service.

Why this exists instead of me testing from the development sandbox: that sandbox
can reach two hosts on the whole internet (GitHub and PyPI). TCP to Telegram's
data centres connects and then delivers nothing, because raw MTProto is filtered
here — so no login, no probing and no protocol discovery is possible from this
environment. The Render service *does* have normal egress, so discovery runs
there, using the session the operator already put in the environment, and only the
findings come back.

Those findings are what the eight unimplemented handlers need: which menu
``@anime_hindifilesbot`` shows after ``/start``, whether its buttons are callbacks
or URLs, and what ``@chelpbot`` demands before it will publish. None of that can
be invented, and it is not stable enough to trust from documentation.

Safety is the design here, not a disclaimer, and it is *enforced* rather than
described: every outbound call goes through :func:`_send`, which raises
:class:`ProbeViolation` unless the peer is one of the three bots or the owner **and**
the text is one of a dozen harmless commands. There is no code path in this module
that can upload a file, forward a message, post in a channel, create a channel,
change a permission, or answer a join request.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any, Sequence

from . import rights as channel_rights
from .linkprovider import NOT_FOR_PROBE as LINK_MINTING_COMMANDS
from .botapi import split_for_chat
from .linkprovider import parse_reply as _parse_link_reply
from .linkprovider import summary as _link_provider_summary
from .storagebot import FORBIDDEN as FORBIDDEN_COMMANDS

log = logging.getLogger("auto_manager.probe")

__all__ = [
    "collect_dialogs",
    "DIALOG_LIMIT",
    "MAX_BUTTONS_SHOWN",
    "MAX_COMMANDS_SHOWN",
    "MAX_REPORT_CHARS",
    "ProbeBudget",
    "ProbePolicy",
    "ProbeViolation",
    "SAFE_COMMANDS",
    "format_report",
    "run_probe",
]

#: Telegram's message limit. The report has to fit in one message: a report split
#: over four messages is a report nobody pastes in full.
MAX_REPORT_CHARS = 3800

#: How much of each answer to print. A menu is skimmed for its shape, so ten buttons is plenty; a bot's
#: declared command list is the one line this whole probe exists to read, and cutting it at ten without
#: saying so is how a 14-command clone got reported as a 10-command one.
MAX_BUTTONS_SHOWN = 10
MAX_COMMANDS_SHOWN = 16

#: Commands the storage bot offers that this program may never send, whatever else changes: its
#: moderation tools act on people, which is not this pipeline's job. See ``app/storagebot.py``.
#: ``LINK_MINTING_COMMANDS`` is the second half of the same idea, for @Link_providerobot: a verb
#: that answers a forwarded message with a permanent link is not "dangerous", it is *not free*, and
#: a read-only probe has no business spending one. See ``app/linkprovider.py``.
FORBIDDEN_COMMANDS  # noqa: B018  (re-exported through the guard above; see may_send)
_NEVER_SEND = frozenset(
    {str(name).lstrip("/").casefold() for name in FORBIDDEN_COMMANDS}
    | {str(name).lstrip("/").casefold() for name in LINK_MINTING_COMMANDS}
)

#: The only text this probe may ever send. Everything here is a menu-navigation
#: command; nothing starts an upload, accepts a request, or talks to a person.
SAFE_COMMANDS: frozenset[str] = frozenset(
    {"/start", "/help", "/settings", "/menu", "/cancel", "/back", "/id", "cancel", "back", "help", "menu", "my files", "main menu", "close"}
)


class ProbeViolation(RuntimeError):
    """Raised the moment the probe tries something outside its policy.

    Deliberately *not* caught by the step-level handlers: a violation means the
    probe itself is wrong, and continuing after that would make the guard
    decorative.
    """


class ProbeBudget(ProbeViolation):
    """The probe ran out of messages or time. A stop, not a bug."""


@dataclass(frozen=True, slots=True)
class ProbePolicy:
    """Who the probe may talk to, what it may say, and how hard it may push.

    ``owner_user_id`` is an ID rather than a username because the owner is the one
    peer we must reach even when their account has no username set.
    """

    storage_bot: str = "anime_hindifilesbot"
    channel_help: str = "chelpbot"
    #: Third bot, learned from the operator's own screenshots rather than from a probe run: it is
    #: what makes the updates channel possible, and its menu is still unmapped.
    link_provider: str = "Link_providerobot"
    owner_user_id: int | None = None
    per_step_timeout: float = 25.0
    settle_seconds: float = 1.5
    max_button_probes: int = 4
    max_messages: int = 8

    @property
    def peers(self) -> set[str]:
        allowed = {
            self.storage_bot.casefold().lstrip("@"),
            self.channel_help.casefold().lstrip("@"),
            self.link_provider.casefold().lstrip("@"),
        }
        if self.owner_user_id is not None:
            allowed.add(str(self.owner_user_id))
        return allowed

    def may_send(self, peer: Any, text: str) -> bool:
        peer_key = str(peer).lstrip("@").casefold()
        wanted = str(text).strip().casefold().lstrip("/")
        if wanted in _NEVER_SEND:
            # Checked before the allowlist, not after it. `SAFE_COMMANDS` is a list someone will
            # one day widen while testing by hand, and ``/broadcast`` is a command that has to
            # stay unsendable by a program however reasonable that widening looks at the time:
            # it makes the storage bot message *people*.
            return False
        return peer_key in self.peers and str(text).strip().casefold() in SAFE_COMMANDS

    def allows_button(self, text: str | None) -> bool:
        return bool(text) and str(text).strip().casefold() in SAFE_COMMANDS


@dataclass
class _Run:
    """Mutable state for one pass: what was sent, and the step log.

    The step log exists so that a failure halfway through still produces a
    partial report — a third of the answers is worth far more than a crash.
    """

    steps: list[dict[str, Any]] = field(default_factory=list)
    sent: int = 0
    budget_seconds: float = 240.0
    started_at: float = field(default_factory=time.monotonic)

    def record(self, name: str, **fields: Any) -> dict[str, Any]:
        entry = {"step": name, **fields}
        self.steps.append(entry)
        return entry

    @property
    def out_of_time(self) -> bool:
        return (time.monotonic() - self.started_at) > self.budget_seconds

    @property
    def elapsed(self) -> float:
        return round(time.monotonic() - self.started_at, 1)


async def _send(client: Any, peer: Any, text: str, policy: ProbePolicy, run: _Run) -> Any:
    """The only sender in this module. See the module docstring for why."""
    if not policy.may_send(peer, text):
        raise ProbeViolation(f"refusing to send {text!r} to {peer!r}: peer or text outside probe policy")
    if run.sent >= policy.max_messages:
        raise ProbeBudget(f"probe message budget ({policy.max_messages}) exhausted")
    run.sent += 1
    return await asyncio.wait_for(client.send_message(peer, text), policy.per_step_timeout)


def _button_info(button: Any) -> dict[str, Any]:
    """Describe one inline button without assuming a Telethon version.

    ``data`` is captured (truncated) because it is what an automation must replay
    to reproduce the click; it is the bot's own callback token, not user-private
    material.
    """
    text = getattr(button, "text", None)
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    data = getattr(button, "data", None)
    if isinstance(data, bytes):
        data = data.decode("utf-8", "replace")
    url = getattr(button, "url", None)
    info: dict[str, Any] = {
        "text": (text or "")[:80],
        "kind": "url" if url else "game" if getattr(button, "game", None) else "callback" if (data or getattr(button, "query", None)) else "reply",
    }
    if data:
        info["data"] = str(data)[:70]
    if url:
        info["host"] = str(url).split("/")[2] if "//" in str(url) else str(url)[:40]
    return info


def _describe_buttons(message: Any) -> list[dict[str, Any]]:
    """Flatten Telethon's rows-of-buttons into a list that carries its own address.

    ``row``/``col`` are kept because clicking needs exactly those two numbers, and
    storing them with the button is what makes "we pressed the button we said we
    pressed" checkable later. Getting this wrong presses a neighbour: the first
    version of this returned the *count* of rows as the row index, which would
    have clicked the last row every time.
    """
    rows = getattr(message, "buttons", None) or []
    out: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows):
        buttons: Sequence[Any] = row if isinstance(row, (list, tuple)) else [row]
        for col_index, button in enumerate(buttons):
            info = _button_info(button)
            info["row"], info["col"] = row_index, col_index
            out.append(info)
    return out


async def _newest_message(client: Any, peer: str, *, timeout: float, limit: int = 4) -> tuple[Any | None, list[Any]]:
    messages: list[Any] = []
    async for message in _drain(client.iter_messages(peer, limit=limit), timeout):
        messages.append(message)
    return (messages[0] if messages else None), messages


async def _drain(agen: Any, timeout: float) -> Any:
    """Iterate an async generator with one deadline for the whole read."""
    deadline = time.monotonic() + timeout
    iterator = agen.__aiter__()
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            yield await asyncio.wait_for(iterator.__anext__(), remaining)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            return


def _message_text(message: Any) -> str:
    return str(getattr(message, "text", None) or getattr(message, "message", None) or "")


async def probe_bot(client: Any, username: str, *, policy: ProbePolicy, run: _Run, label: str) -> dict[str, Any]:
    """Map one bot's opening menu, then press at most a few safe buttons.

    Clicking is limited to buttons whose *own label* is a safe command
    (cancel / back / help / my files). That is enough to learn whether the bot
    drives its flow with callbacks or URLs and how it words each step, without
    ever starting an upload or agreeing to anything on the operator's behalf.
    """
    result: dict[str, Any] = {"label": label, "username": username.lstrip("@"), "error": None}
    message = None
    try:
        await _send(client, username, "/start", policy, run)
        await asyncio.sleep(policy.settle_seconds)
        message, _ = await _newest_message(client, username, timeout=policy.per_step_timeout)
        if message is None:
            result["error"] = "no reply read back from this peer"
            run.record(label, error=result["error"])
            return result
        text = _message_text(message)
        # The peer we are standing in front of is the *expected* bot here, so a link belonging to
        # one of its siblings is reported as what it is instead of flattering the report.
        recognised = _parse_link_reply(text, bot=username)
        result["first"] = {
            "reply_chars": len(text),
            "reply": " ".join(text.split())[:900],
            "buttons": _describe_buttons(message),
            "has_media": bool(getattr(message, "media", None)),
            "from_bot": not bool(getattr(message, "out", False)),
            # Recorded only when the reply is a shape we already know how to read. A bot that hands
            # back a `t.me/<bot>?start=` link during a probe is a fact about the protocol, and it
            # belongs in the report rather than in a screenshot someone has to compare by eye.
            "reply_shape": recognised["kind"] if recognised["kind"] != "unknown" else None,
        }
    except ProbeBudget:
        result["error"] = "message budget reached before this bot was asked"
        return result
    except ProbeViolation:
        raise
    except asyncio.TimeoutError:
        result["error"] = "timed out waiting for the bot to answer"
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {str(exc)[:140]}"

    pressed: list[dict[str, Any]] = []
    result["pressed"] = pressed
    # Buttons the probe walked past on purpose are counted somewhere the press budget cannot reach.
    # They used to be recorded in the same list as skips, which meant a bot with enough unpressable
    # buttons could push its interesting ones out of the report; and when they were not recorded at
    # all, "safe buttons pressed:" followed by two URL notes read as "the bot offered nothing else".
    refused: list[str] = []
    result["refused_buttons"] = refused
    if message is not None and not result["error"]:
        for button in (result["first"].get("buttons") or []):
            # The budget is about *clicking*, and it used to be a `break` at the top of the loop: a bot
            # with a long menu then stopped being read as well as stopped being pressed, and the report
            # could not tell "refused" from "never looked". The limit guards the clicks alone now, and the
            # scan runs to the end of the menu.
            if button["kind"] == "url":
                pressed.append({"button": button["text"], "skipped": "url button; opening it needs no automation"})
                continue
            if not policy.allows_button(button.get("text")):
                refused.append(str(button.get("text")))
                continue
            clicks = [entry for entry in pressed if "skipped" not in entry]
            if run.out_of_time or len(clicks) >= policy.max_button_probes:
                continue
            try:
                click = getattr(message, "click", None)
                if click is None:
                    pressed.append({"button": button["text"], "error": "this Telethon build has no click(); capture only"})
                    continue
                await asyncio.wait_for(
                    click(int(button.get("row") or 0), int(button.get("col") or 0)), policy.per_step_timeout
                )
                await asyncio.sleep(1.2)
                after, _ = await _newest_message(client, username, timeout=policy.per_step_timeout)
                pressed.append(
                    {
                        "button": button["text"],
                        "reply_chars": len(_message_text(after)) if after else 0,
                        "buttons_after": [b["text"] for b in (_describe_buttons(after) if after else [])][:8],
                    }
                )
            except ProbeViolation:
                raise
            except Exception as exc:  # noqa: BLE001
                pressed.append({"button": button["text"], "error": f"{type(exc).__name__}: {str(exc)[:100]}"})
    result["bot_profile"] = await _bot_profile(client, username, timeout=policy.per_step_timeout)
    run.record(
        label,
        buttons=len((result.get("first") or {}).get("buttons") or []),
        pressed=len(pressed),
        error=result["error"],
    )
    return result


async def _bot_profile(client: Any, username: str, *, timeout: float) -> dict[str, Any]:
    """What Telegram knows about this bot: its commands, its menu button, its profile text.

    The cheapest protocol hint there is, and it took two wrong reads to get, which says something about how
    quietly a read can fail. First attempt: ``bots.getBotInfo`` — the call a bot's *owner* uses on the bot
    they edit, since its fields are ``app_settings``, ``verifier_settings`` and ``privacy_policy_url``.
    Asked about somebody else's bot, the server answers ``BOT_INVALID: This is not a valid bot``, and in a
    report that reads as three uncooperative bots.

    Second attempt: right API, wrong level. ``users.getFullUser`` answers with a *wrapper* —
    ``users.UserFull { full_user, chats, users }`` — and the profile sits one level down in the inner
    record. Reading ``bot_info`` off the wrapper finds nothing and raises nothing, which is how three bots
    came back as having no profile when our read had simply stopped short of it.

    So the wrapper is taken apart here, both shapes are accepted, and an empty answer says *which* empty it
    was: a record with no bot block at all, or a bot that declares no commands, no menu button and no text
    (true of most clones, and worth knowing because it means the menu on screen is the whole protocol).
    ``is_bot`` is kept because it costs nothing and is the one field that says whether the server even
    considers this peer a bot.
    """
    try:
        from telethon import functions, utils

        entity = await asyncio.wait_for(client.get_entity(username), timeout)
        request = functions.users.GetFullUserRequest(id=utils.get_input_user(entity))
        answered = await asyncio.wait_for(client(request), timeout)
    except Exception as exc:  # noqa: BLE001 - an unavailable hint is not a probe failure
        # The class *and* the message: "unavailable: TypeError" sent an operator and an agent hunting for a
        # broken bot, when the sentence that mattered was about our own request.
        return {"unavailable": f"{type(exc).__name__}: {str(exc)[:120]}"}

    return _read_bot_profile(answered)


def _read_bot_profile(answered: Any) -> dict[str, Any]:
    """The wrapper apart, then the three hints out of the record inside it."""
    full = getattr(answered, "full_user", None) or answered
    peers = list(getattr(answered, "users", None) or [])
    peer = next(
        (item for item in peers if getattr(item, "id", None) == getattr(full, "id", None)),
        peers[0] if peers else None,
    )
    bot_info = getattr(full, "bot_info", None)

    hint: dict[str, Any] = {}
    commands: list[str] = []
    for item in getattr(bot_info, "commands", None) or []:
        description = _brief(getattr(item, "description", ""), 34)
        commands.append(f"/{item.command}" + (f"={description}" if description else ""))
    if commands:
        hint["commands"] = commands[:40]
    about = " ".join(str(getattr(full, "about", "") or "").split())
    if about:
        hint["about"] = about[:280]
    menu_text = str(getattr(getattr(bot_info, "menu_button", None), "text", "") or "").strip()
    if menu_text:
        hint["menu_button"] = menu_text[:60]
    if peer is not None:
        hint["is_bot"] = bool(getattr(peer, "bot", False))
    if not (commands or about or menu_text):
        marked = "yes" if hint.get("is_bot") else ("no" if "is_bot" in hint else "not reported")
        hint["empty"] = (
            "Telegram sent no bot block for this peer at all"
            if bot_info is None
            else "an empty bot block: no commands, no menu button, no profile text"
        ) + f" (peer marked as a bot: {marked})"
    return hint


#: The dialog walk, on its own, because two callers need exactly this and not the probe's menu questions.
#: `app/discover.py` reads the same entries to decide what a channel *is*, and a second walk built from a
#: second set of field choices is how a probe and a discovery screen start disagreeing about one channel.
DIALOG_LIMIT = 150


async def collect_dialogs(client: Any, *, timeout: float = 75.0, limit: int = DIALOG_LIMIT) -> list[dict[str, Any]]:
    """One pass over the account's dialog list, in the shape :func:`app.rights.plan` reads.

    Every key is always present, ``None`` included: "we are a member" and "we never looked" have to stay
    two different facts, and omitting the rights key for a member once made them the same one.
    """
    entries: list[dict[str, Any]] = []
    async for dialog in _drain(client.iter_dialogs(), timeout):
        entity = getattr(dialog, "entity", None)
        if entity is None:
            continue
        entries.append(
            {
                "title": (getattr(entity, "title", None) or getattr(entity, "first_name", None) or "")[:60],
                "username": getattr(entity, "username", None),
                # The id is what makes a private channel matchable: with no @handle to compare, the
                # marked numeric id is the only key the dashboard row and the entity share.
                "id": getattr(entity, "id", None),
                "mine": bool(getattr(entity, "creator", False)),
                "left": bool(getattr(entity, "left", False)),
                "members": getattr(entity, "participants_count", None),
                "channel": getattr(entity, "broadcast", None) is not None,
                "rights": channel_rights.rights_of(entity),
            }
        )
        if len(entries) >= limit:
            break
    return entries


async def probe_account(
    client: Any,
    *,
    policy: ProbePolicy,
    run: _Run,
    expected: Sequence[dict[str, Any]] = (),
    probe_limit: int = DIALOG_LIMIT,
) -> dict[str, Any]:
    """What the spare account can see and do right now.

    ``expected`` is the operator's configured source channels. The report names
    which of them are not joined and which lack the admin rights the pipeline
    assumes — the difference between "the scanner found nothing" and "this
    account is not a member of that channel", worth more than any amount of log
    reading later.
    """
    out: dict[str, Any] = {}
    try:
        me = await asyncio.wait_for(client.get_me(), policy.per_step_timeout)
        out["id"] = getattr(me, "id", None)
        out["username"] = getattr(me, "username", None)
        out["restricted"] = bool(getattr(me, "restricted", False))
        out["premium"] = bool(getattr(me, "premium", False))
    except ProbeViolation:
        raise
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        run.record("account", **out)
        return out

    # One walk, one field list, one cap: `collect_dialogs` is shared with `app/discover.py` so that a
    # probe's answer and a discovery screen cannot disagree about the same channel. The cap lives there for
    # the same reason — a probe that walked 400 dialogs to report 150 of them is a slow answer to a cheap
    # question, and 150 is more than any account of ours has channels in.
    try:
        dialogs: list[dict[str, Any]] = await collect_dialogs(
            client, timeout=policy.per_step_timeout * 3, limit=probe_limit
        )
    except ProbeViolation:
        raise
    except Exception as exc:  # noqa: BLE001
        dialogs = []
        out["dialogs_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"

    out["dialog_count"] = len(dialogs)
    out["owned_channels"] = [d["title"] for d in dialogs if d["mine"]][:12]
    out["top_dialogs"] = [d for d in dialogs[:10]]
    # Rights are read here rather than in a second pass because the dialog walk is the expensive
    # part, and a second walk would be a second chance for the answer to differ from the first.
    planned = channel_rights.plan(dialogs, expected)
    out["rights"] = planned
    # One matching rule, used for both lines. `missing_channels` used to compare @handles against the
    # dialogs on its own, while `plan` above matches by handle *or* by the marked channel number — so a
    # row with no @handle, which is every channel added by number with `/source … add` and every private
    # channel, appeared in the report as "configured but NOT visible" underneath the very line saying
    # its rights had just been read. The operator read that as a wrong id. It was a wrong sentence.
    out["missing_channels"] = [{"want": name} for name in list(planned.get("unseen") or [])][:12]
    run.record("account", dialog_count=len(dialogs), owned=len(out["owned_channels"]))
    return out


async def run_probe(
    client: Any,
    *,
    policy: ProbePolicy,
    db: Any = None,
    send: bool = True,
) -> dict[str, Any]:
    """Probe the account and the three bots, then report.

    ``send=False`` returns the report instead of delivering it — what the tests
    use, and what a dry run should do.
    """
    run = _Run()
    # The three bots are addressed by the rows the operator set, not by the handles compiled into the
    # policy. This matters because /probe is the operator's proof that a bot answers *at all*: a report
    # about `chelpbot` while `bots.channel_help_username` names their clone is a certainty about the
    # wrong peer, and the writers will send to the configured one.
    for field_name, config_key in (
        ("storage_bot", "bots.storage_username"),
        ("channel_help", "bots.channel_help_username"),
    ):
        read = getattr(db, "config", None) if db is not None and getattr(db, "connected", False) else None
        if read is None:
            continue
        try:
            named = str(await read(config_key, "") or "").strip().lstrip("@")
        except Exception as exc:  # noqa: BLE001 - an unreadable row is the default's turn, not a crash
            log.info("probe: %s unread (%s); the policy default stands", config_key, str(exc)[:90])
            continue
        if named and named != getattr(policy, field_name):
            log.info("probe: %s addressed as @%s (from app.config)", config_key, named)
            policy = replace(policy, **{field_name: named})
    expected: list[dict[str, Any]] = []
    if db is not None and getattr(db, "connected", False):
        try:
            expected = await db.fetch(
                "select id, username, telegram_channel_id, we_are_admin from app.source_channel "
                "where coalesce(active, true)"
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("probe could not read configured channels: %s", exc)

    report: dict[str, Any] = {
        "account": {},
        "storage_bot": {},
        "channel_help": {},
        "link_provider": {},
        "steps": run.steps,
    }
    try:
        report["account"] = await probe_account(client, policy=policy, run=run, expected=expected)
        report["storage_bot"] = await probe_bot(client, policy.storage_bot, policy=policy, run=run, label="storage_bot")
        if not run.out_of_time:
            report["channel_help"] = await probe_bot(client, policy.channel_help, policy=policy, run=run, label="channel_help")
        if not run.out_of_time:
            report["link_provider"] = await probe_bot(
                client, policy.link_provider, policy=policy, run=run, label="link_provider"
            )
    except ProbeBudget as exc:
        report["stopped"] = str(exc)
        log.info("probe stopped at its own budget: %s", exc)
    except ProbeViolation as exc:
        # The guard firing is a bug in the probe, so it is reported loudly rather
        # than retried: the account must not be used to probe twice "to be sure".
        report["violation"] = str(exc)
        log.error("probe violated its own policy and stopped: %s", exc)

    decided = (report.get("account") or {}).get("rights") or {}
    if decided and db is not None and getattr(db, "connected", False):
        try:
            report["rights_recorded"] = await channel_rights.record(db, decided)
        except Exception as exc:  # noqa: BLE001 - a failed write must not lose the report
            report["rights_recorded"] = {"considered": len(decided.get("updates") or []), "written": 0}
            # The report survives a failed write, and it says so: a run that read three channels and
            # recorded none of them must not arrive looking like a run that found nothing to change.
            report["rights_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
            log.warning("rights could not be recorded: %s", exc)

    report["messages_sent"] = run.sent
    report["elapsed_seconds"] = run.elapsed

    text = format_report(report)
    report["report"] = text
    if send:
        report["delivery"] = await _deliver(client, text, policy=policy)
    if db is not None and getattr(db, "connected", False):
        try:
            await db.execute(
                "insert into app.audit_log (actor_user_id, action, entity_type, detail) "
                "values ($1, 'probe.report', 'service_state', $2::jsonb)",
                report["account"].get("id"),
                # The uncapped render: ``format_report`` cuts the *message* at MAX_REPORT_CHARS and
                # tells the operator the whole thing is in this row, which is only true if the whole
                # thing is in this row. A pointer to a shorter copy is how a report stops being
                # falsifiable — and 1500 characters used to be all it kept, for a run whose entire
                # purpose is recording what the two bots said.
                {
                    "summary": format_report(report, limit=None),
                    "elapsed": report["elapsed_seconds"],
                    "sent": run.sent,
                },
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("probe report not audited: %s", exc)
    return report


async def _deliver(client: Any, text: str, *, policy: ProbePolicy) -> str:
    """Owner-only delivery, outside the command allowlist but inside a peer check.

    Split out so the one exception to :func:`_send` is auditable in a single
    function: the peer must be the owner id and nothing else, and the call cannot
    carry any keyword that would attach media.
    """
    if policy.owner_user_id is None:
        return "not sent: TELEGRAM_MAIN_ADMIN_USER_ID is unset"
    try:
        # One send per part, and every part: the report is the thing the operator has to read in full,
        # and ``text[:4096]`` here used to mean the last channel's answer simply never arrived.
        parts = split_for_chat(text)
        for part in parts:
            await asyncio.wait_for(client.send_message(policy.owner_user_id, part), policy.per_step_timeout)
        return f"sent to owner id={policy.owner_user_id}" + (
            f" in {len(parts)} parts" if len(parts) > 1 else ""
        )
    except Exception as exc:  # noqa: BLE001
        return f"failed: {type(exc).__name__}: {str(exc)[:120]}"


def _brief(text: Any, limit: int) -> str:
    """Shorten a bot's own wording to whole words, because a mid-word cut reads as the bot's fault.

    The operator's report carried `/ban=Ban a user (moderators o` — a fact about our formatting that looks
    like a fact about the bot. Cutting at the last space the limit allows keeps the phrase readable; no
    ellipsis is added, because in a line this dense a trailing mark costs more than it explains.
    """
    words = " ".join(str(text or "").split())
    if len(words) <= limit:
        return words
    cut = words[:limit]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut


def format_report(report: dict[str, Any], *, limit: int | None = MAX_REPORT_CHARS) -> str:
    """One paste-able message, shaped for a human to copy out of Telegram into a chat.

    It leads with the answers that unblock the most work — what the storage bot's menu actually looks
    like — and leaves the noise out, because the whole point of the shape is that the operator pastes it
    back rather than screenshotting a log.

    ``limit=None`` renders it without the cap, and that is the version ``app.audit_log`` keeps: the
    truncation note tells the operator the full text is in the database, which a row holding the capped
    copy would not make true. The structured report is what the row is *for* — a detail nobody needed
    today is still a detail somebody will need tomorrow.
    """
    account = report.get("account") or {}
    lines: list[str] = ["auto-manager · protocol probe", ""]

    if report.get("violation"):
        lines.append(f"STOPPED BY GUARD: the probe hit its own policy and halted: {report['violation']}")
        lines.append("")
    if report.get("stopped"):
        lines.append(f"(ended at its own limit: {report['stopped']})")
        lines.append("")

    lines.append(f"account: @{account.get('username') or '?'} id={account.get('id')}")
    if account.get("restricted"):
        lines.append("  WARNING: Telegram has restricted this account")
    if account.get("error") or account.get("dialogs_error"):
        lines.append(f"  read failed: {account.get('error') or account.get('dialogs_error')}")
    lines.append(f"  dialogs={account.get('dialog_count', '?')} channels I own={len(account.get('owned_channels') or [])}")
    if account.get("owned_channels"):
        lines.append("  owned: " + "; ".join(str(c) for c in account["owned_channels"][:6]))
    missing = account.get("missing_channels") or []
    if missing:
        lines.append("  configured but NOT visible: " + ", ".join(str(m.get("want")) for m in missing[:8]))
    decided = account.get("rights") or {}
    if decided:
        lines.append("  " + channel_rights.summary(decided, report.get("rights_recorded") or {}))
    if report.get("rights_error"):
        lines.append(
            "  RIGHTS NOT RECORDED: " + str(report["rights_error"])
            + " — what was read is in this report only; the database still holds the old values"
        )

    for label, key in (("storage bot", "storage_bot"), ("channel help", "channel_help"), ("link provider", "link_provider")):
        section = report.get(key) or {}
        lines.append("")
        lines.append(f"{label}: @{section.get('username') or '?'}")
        if key == "link_provider":
            lines.append(f"  already known: {_link_provider_summary()}")
        shape = (section.get("first") or {}).get("reply_shape")
        if shape:
            lines.append(f"  reply shape observed: {shape}")
        if section.get("error"):
            lines.append(f"  error: {section['error']}")
            continue
        first = section.get("first") or {}
        if first.get("reply"):
            lines.append(f"  says: {str(first['reply'])[:240]}")
        buttons = first.get("buttons") or []
        if buttons:
            kinds = sorted({str(b.get("kind")) for b in buttons})
            lines.append(f"  buttons ({len(buttons)}; kinds: {', '.join(kinds)}):")
            for button in buttons[:MAX_BUTTONS_SHOWN]:
                note = f" [{button['kind']}]"
                if button.get("data"):
                    note += f" data={str(button['data'])[:26]}"
                if button.get("host"):
                    note += f" host={button['host']}"
                lines.append(f"    - {button.get('text')}{note}")
        else:
            lines.append("  buttons: none reported")
        pressed = [p for p in (section.get("pressed") or []) if p]
        if pressed:
            lines.append("  safe buttons pressed:")
            for entry in pressed[:4]:
                if entry.get("error"):
                    lines.append(f"    - {entry.get('button')}: {entry['error']}")
                elif entry.get("skipped"):
                    lines.append(f"    - {entry.get('button')}: {entry['skipped']}")
                else:
                    lines.append(f"    - {entry.get('button')} -> {entry.get('reply_chars')} chars; then {entry.get('buttons_after')}")
        if section.get("refused_buttons"):
            named = ", ".join(str(b)[:24] for b in section["refused_buttons"][:8])
            lines.append(f"  left alone by policy ({len(section['refused_buttons'])}): {named}")
        profile = section.get("bot_profile") or {}
        declared = profile.get("commands") or []
        if declared:
            shown = " ".join(str(c) for c in declared[:MAX_COMMANDS_SHOWN])
            hidden = len(declared) - MAX_COMMANDS_SHOWN
            lines.append(
                f"  commands ({len(declared)}): {shown}"
                + (f" \u2026and {hidden} more, all of them in app.audit_log" if hidden > 0 else "")
            )
        if profile.get("menu_button"):
            lines.append(f"  menu button the user must press first: {profile['menu_button']}")
        if profile.get("about"):
            lines.append(f"  profile text: {profile['about']}")
        if profile.get("empty"):
            lines.append(f"  the bot declares nothing beyond its menu: {profile['empty']}")
        if profile.get("unavailable"):
            lines.append(f"  profile could not be read: {profile['unavailable']}")

    lines.append("")
    lines.append(f"messages sent={report.get('messages_sent', 0)} elapsed={report.get('elapsed_seconds')}s")
    lines.append("Paste this whole message back to the agent.")
    text = "\n".join(lines)
    if limit is None or len(text) <= limit:
        return text
    note = "\n…truncated; the full version is in app.audit_log"
    # Cut by the note's real length: "leave 40 chars" overshoots the moment
    # the sentence changes, and an over-long report fails to send at 4096.
    return text[: limit - len(note)] + note
