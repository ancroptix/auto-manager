"""What a person is told when they asked to join — and the rules around saying it.

The operator deferred this sentence for a reason: *"the message they get is mine to write"*. What
they asked for on 2026-08-29 was not a policy change but a place to keep it — *"bot me options de
dena, jisse mujhe kabhi bhi kuch bhi bolna ho to mai bol paau sabhi se"* — options in the control
bot, so the wording can be set at any time without editing a database row by hand.

So this module owns three things and nothing else:

* **the presets** (`PRESETS`), which are *our drafts*. They are offered so a decision can be made
  by picking rather than by writing, and none of them is approved until the operator chooses it.
  `tests/test_joinmsg.py` fails if a preset ever enters `APPROVED_TEMPLATES`-style approval on its
  own.
* **the placeholders** (`PLACEHOLDERS`), which are the only words a template may contain that get
  substituted. A placeholder nobody knows is refused rather than left in, because a DM that says
  ``{series}`` to a stranger is a bug nobody notices until someone screenshots it.
* **the refusals**, which is where the judgement lives. See :func:`refusals` — the short version is
  that no campaign text may carry an invite link, and no command here can approve a request.

What is **not** here: a sender. Contacting people is the job kind ``join_request_campaign``, and it
is still blocked for the same reason as ``publish_post`` — there is no MTProto write path yet. A
saved message is a *setting*, not a queue of DMs, and :func:`status_note` says so in the same
breath as the character count, so nobody reads "saved" as "sent".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "CONFIG_KEY",
    "PRESETS",
    "Preset",
    "PLACEHOLDERS",
    "MAX_CHARS",
    "contact_allowed",
    "describe_preset",
    "find_placeholders",
    "options_text",
    "refusals",
    "render",
    "status_note",
    "stored_value",
    "unknown_placeholders",
]

#: The one row that holds it. A setting in ``app.config`` rather than a column because the text is
#: wording, not state: it changes on a Tuesday afternoon with no migration and no redeploy.
CONFIG_KEY = "joinrequest.message"

#: A Telegram message is 4096 characters; 700 is the policy ceiling, and it is deliberately far
#: below it. A private note to someone who asked to join a fan channel is not a newsletter, and
#: every character past the first screen is a reason to report the account for spam.
MAX_CHARS = 700

#: The only substitutions a template may use. ``{invite}`` is *absent on purpose* — see
#: :func:`refusals`, item 1, and do not add it back without reading that.
PLACEHOLDERS: tuple[str, ...] = ("{name}", "{series}")

_PLACEHOLDER_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


@dataclass(frozen=True, slots=True)
class Preset:
    """A suggested message, with the sentence about what it promises."""

    name: str
    text: str
    #: What this wording commits the channel to. Shown to the operator, never to the user.
    note: str


#: Three ways to answer a join request, drafted here so the choice is a pick rather than a blank
#: box. They are *drafts*: choosing one writes it into the setting, and choosing is the operator's
#: act — nothing in this file sends anything, and nothing in it counts as approval.
PRESETS: tuple[Preset, ...] = (
    Preset(
        "welcome",
        "🍉 {name}, aap {series} channel ke join request par hain. "
        "Approval ke baad episodes isi channel par aayenge — koi extra DM nahi, koi link force nahi.",
        "says what happens next, and promises no more DMs. It does not tell them they are in: the "
        "request is still pending when this is sent, and a message must not pretend otherwise",
    ),
    Preset(
        "setup",
        "🍉 {name}, ek baat puchni thi: {series} ke episodes ka notification chahiye? "
        "Jawab me `join` likhein to list me daal deinge, `stop` likhein to kabhi nahi aayega.",
        "a question rather than an announcement, so the reply means something: `join` or `stop`, "
        "both already understood (app/channels.py), which is what keeps the list opt-in",
    ),
    Preset(
        "brief",
        "🍉 {name}, aapka request dekh liya ja raha hai. Thoda intezaar karein.",
        "the shortest safe answer: no promise, no question, nothing to keep honouring later",
    ),
)


def describe_preset(preset: Preset) -> str:
    """One numbered option, as `/joinmsg options` prints it."""
    return f"{PRESETS.index(preset) + 1}. `{preset.name}` — {preset.text}\n   {preset.note}"


def options_text(current: str | None = None) -> str:
    """The options screen: the list, the numbering, and where the current text came from."""
    lines = ["Pick one, or write your own:", ""]
    for preset in PRESETS:
        lines.append(describe_preset(preset))
        lines.append("")
    lines.append("`/joinmsg use 1|2|3` saves one of these. `/joinmsg set <text>` saves yours.")
    lines.append("`/joinmsg show` prints what is saved now, `/joinmsg clear` stops all sending.")
    # `contact_allowed`, not "is the string non-empty": stored_value describes even an empty
    # setting, and a description that reads like a saved message is the bug this guards.
    if contact_allowed(current):
        lines.append(f"Currently saved: {stored_value(current)}")
    return "\n".join(lines).strip()


def stored_value(value: object) -> str:
    """How the setting is quoted back, without pretending an empty one is a message."""
    text = " ".join(str(value or "").split())
    if not text:
        return "nothing — nobody is contacted"
    return f'"{text[:180]}" ({len(text)} chars)'


def find_placeholders(text: str | None) -> tuple[str, ...]:
    """Every ``{word}`` in a template, whether or not we can fill it."""
    return tuple(f"{{{name}}}" for name in _PLACEHOLDER_RE.findall(str(text or "")))


def unknown_placeholders(text: str | None) -> tuple[str, ...]:
    return tuple(p for p in find_placeholders(text) if p not in PLACEHOLDERS)


def refusals(text: str | None) -> tuple[str, ...]:
    """Why a message is refused before it is ever saved. Strings, so the bot can say them.

    Each of these is a rule the schema or the operator already stated; the function exists so the
    rule is enforced on the way *in* rather than discovered by a user on the way out.
    """
    body = str(text or "")
    flat = body.casefold()
    out: list[str] = []
    if any(marker in flat for marker in ("{invite}", "{link}", "t.me/+", "t.me/joinchat")):
        out.append(
            "no invite link in a join-request message: an invite in a DM lets the person in past the "
            "approval you have not given yet, and the schema forbids coupling a send with an approval "
            "(app.join_campaign.campaign_never_approves)"
        )
    for word in ("approved", "you are in", "accept kar diya", "approve kar diya"):
        if word in flat:
            out.append(
                f"`{word}` reads as a decision about their request: this message never approves or "
                "declines anyone (spec §15), and a sentence that implies it will be quoted back at you"
            )
            break
    if unknown_placeholders(body):
        out.append(
            f"I can only fill {', '.join(PLACEHOLDERS)} — not "
            f"{', '.join(unknown_placeholders(body))}. A placeholder nobody knows reaches the user as-is"
        )
    if len(" ".join(body.split())) > MAX_CHARS:
        out.append(f"too long for a private note: keep it under {MAX_CHARS} characters")
    return tuple(out)


def render(text: str, *, name: str = "", series: str = "") -> str:
    """Fill one template, refusing a placeholder we cannot honour.

    Raises instead of substituting a blank: a DM that opens ``", aap  channel ke join request par"``
    is not a cosmetic defect, it is the message a stranger screenshots.
    """
    problems = unknown_placeholders(text)
    if problems:
        raise ValueError(f"cannot fill {', '.join(problems)}; only {', '.join(PLACEHOLDERS)} exist")
    missing = []
    if "{name}" in text and not str(name or "").strip():
        missing.append("{name}")
    if "{series}" in text and not str(series or "").strip():
        missing.append("{series}")
    if missing:
        raise ValueError(f"this message needs a value for {', '.join(missing)}")
    out = str(text or "")
    out = out.replace("{name}", str(name or "").strip())
    out = out.replace("{series}", str(series or "").strip())
    return " ".join(out.split())


def contact_allowed(value: object) -> bool:
    """Is there a message to send at all? Empty means the app may not contact anyone."""
    return bool(" ".join(str(value or "").split()))


def status_note(value: object) -> str:
    """The one line `/status` and `/joinmsg` both print, so neither can over-promise."""
    if not contact_allowed(value):
        return (
            "join requests: no message saved, so nobody is contacted (set one with /joinmsg — "
            "`/joinmsg options` shows three drafts to pick from)"
        )
    return (
        f"join requests: a message is saved ({len(' '.join(str(value).split()))} chars), and sending "
        "is still blocked on the campaign sender (join_request_campaign), so nothing has gone out"
    )
