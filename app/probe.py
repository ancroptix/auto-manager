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
from dataclasses import dataclass, field
from typing import Any, Sequence

from .linkprovider import NOT_FOR_PROBE as LINK_MINTING_COMMANDS
from .linkprovider import parse_reply as _parse_link_reply
from .linkprovider import summary as _link_provider_summary
from .storagebot import FORBIDDEN as FORBIDDEN_COMMANDS

log = logging.getLogger("auto_manager.probe")

__all__ = ["ProbeBudget", "ProbePolicy", "ProbeViolation", "format_report", "run_probe", "MAX_REPORT_CHARS", "SAFE_COMMANDS"]

#: Telegram's message limit. The report has to fit in one message: a report split
#: over four messages is a report nobody pastes in full.
MAX_REPORT_CHARS = 3800

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
        recognised = _parse_link_reply(text)
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
    if message is not None and not result["error"]:
        for button in (result["first"].get("buttons") or []):
            if len(pressed) >= policy.max_button_probes or run.out_of_time:
                break
            if button["kind"] == "url":
                pressed.append({"button": button["text"], "skipped": "url button; opening it needs no automation"})
                continue
            if not policy.allows_button(button.get("text")):
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
    result["command_list"] = await _bot_commands(client, username, timeout=policy.per_step_timeout)
    run.record(
        label,
        buttons=len((result.get("first") or {}).get("buttons") or []),
        pressed=len(pressed),
        error=result["error"],
    )
    return result


async def _bot_commands(client: Any, username: str, *, timeout: float) -> list[str]:
    """The bot's declared command list — the cheapest protocol hint available."""
    try:
        from telethon import functions

        result = await asyncio.wait_for(client(functions.bots.GetBotInfoRequest(bot=username.lstrip("@"))), timeout)
        commands = []
        for item in getattr(result, "commands", None) or []:
            description = (getattr(item, "description", "") or "").strip()
            commands.append(f"/{item.command}" + (f"={description[:24]}" if description else ""))
        return commands[:20]
    except Exception as exc:  # noqa: BLE001 - an unavailable hint is not a probe failure
        return [f"(unavailable: {type(exc).__name__})"]


async def probe_account(
    client: Any, *, policy: ProbePolicy, run: _Run, expected: Sequence[dict[str, Any]] = ()
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

    dialogs: list[dict[str, Any]] = []
    by_username: dict[str, dict[str, Any]] = {}
    try:
        async for dialog in _drain(client.iter_dialogs(), policy.per_step_timeout * 3):
            entity = getattr(dialog, "entity", None)
            if entity is None:
                continue
            entry: dict[str, Any] = {
                "title": (getattr(entity, "title", None) or getattr(entity, "first_name", None) or "")[:60],
                "username": getattr(entity, "username", None),
                "mine": bool(getattr(entity, "creator", False)),
                "left": bool(getattr(entity, "left", False)),
                "members": getattr(entity, "participants_count", None),
                "channel": getattr(entity, "broadcast", None) is not None,
            }
            rights = getattr(entity, "admin_rights", None)
            if rights is not None:
                entry["rights"] = {
                    name: bool(getattr(rights, name, False))
                    for name in ("post_messages", "edit_messages", "delete_messages", "invite_users", "add_admins")
                }
            dialogs.append(entry)
            if entry["username"]:
                by_username[str(entry["username"]).casefold()] = entry
            if len(dialogs) >= 150:
                break
    except ProbeViolation:
        raise
    except Exception as exc:  # noqa: BLE001
        out["dialogs_error"] = f"{type(exc).__name__}: {str(exc)[:120]}"

    out["dialog_count"] = len(dialogs)
    out["owned_channels"] = [d["title"] for d in dialogs if d["mine"]][:12]
    out["missing_channels"] = [
        {"want": row.get("username") or row.get("telegram_channel_id")}
        for row in expected
        if str(row.get("username") or "").lstrip("@").casefold() not in by_username
    ][:12]
    out["top_dialogs"] = [d for d in dialogs[:10]]
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
    expected: list[dict[str, Any]] = []
    if db is not None and getattr(db, "connected", False):
        try:
            expected = await db.fetch(
                "select username, telegram_channel_id from app.source_channel where coalesce(active, true)"
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
                {"summary": text[:1500], "elapsed": report["elapsed_seconds"], "sent": run.sent},
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
        await asyncio.wait_for(client.send_message(policy.owner_user_id, text[:4096]), policy.per_step_timeout)
        return f"sent to owner id={policy.owner_user_id}"
    except Exception as exc:  # noqa: BLE001
        return f"failed: {type(exc).__name__}: {str(exc)[:120]}"


def format_report(report: dict[str, Any]) -> str:
    """One paste-able message.

    Shaped for a human to copy out of Telegram into a chat, so it leads with the
    answers that unblock the most work — what the storage bot's menu actually
    looks like — and leaves the noise out. The structured version stays in
    ``app.audit_log`` in case a detail matters later.
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
            for button in buttons[:10]:
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
        if section.get("command_list"):
            lines.append("  commands: " + " ".join(str(c) for c in section["command_list"][:10]))

    lines.append("")
    lines.append(f"messages sent={report.get('messages_sent', 0)} elapsed={report.get('elapsed_seconds')}s")
    lines.append("Paste this whole message back to the agent.")
    text = "\n".join(lines)
    if len(text) > MAX_REPORT_CHARS:
        note = "\n…truncated; the full version is in app.audit_log"
        # Cut by the note's real length: "leave 40 chars" overshoots the moment
        # the sentence changes, and an over-long report fails to send at 4096.
        text = text[: MAX_REPORT_CHARS - len(note)] + note
    return text
