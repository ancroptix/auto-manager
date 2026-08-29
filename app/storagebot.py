"""``@anime_hindifilesbot``, as observed — the menu, not a guess about the menu.

Until 2026-08-28 this whole module was a question mark. The bot's protocol could not be read
from any documentation, and the operator would not screen-record the flow, so the honest state
was "we will discover it live". The discovery then happened the cheapest possible way: the
operator sent four screenshots of the bot's own command list, which is *menu text*, not a
recorded session, and is exactly the kind of observation that costs nothing to obtain and pays
for itself the first time a menu changes.

What that fixes: which verb does which job. The next day the operator ran ``/batch`` in their own
clone and sent five more screenshots, so the conversation is recorded too — `BATCH_FLOW`, two
prompts quoted exactly, and one ``t.me/<bot>?start=`` link as the answer. Their answer to *whose bot
is this* matters as much as the flow: every storing and linking bot on this deployment is their own
**clone**, made and managed by ``@Md_CloneManagerBot``, which turns several questions that looked like
someone else's service limits into settings on a screen we own (``docs/storage-bot.md`` keeps the
vendor's claims, dated and labelled as claims rather than observations).

What still keeps ``storage_upload`` blocked is the write layer and `still_unknown`: whether a link is
a reference to the source post or a copy the clone made, whether a batch can be appended to, Public vs
Private Mode, "No Forward", and the rate limit.

Two safety rules live here because they belong to this bot and nowhere else:

* **we never send** ``/broadcast``, ``/ban`` or ``/unban``. They turn out to be moderation tools
  for *our own* clone's users rather than a stranger's list, and the rule did not change: a queued
  job that can reach an audience, or remove a person from it, is a different program than this one,
  and one the operator would have to audit forever. :mod:`app.probe` refuses them even if its
  allowlist is widened.
* **the moderator-only commands are recorded as moderator-only.** ``/special_link`` and
  ``/universal_link`` are the two we actually want (an editable link, and one link for a whole
  season across clones), and both said "(moderators only)" in the screenshot — so the first run
  has to find out what that means for us (moderator of the *source channel*, presumably) before
  a season batch is designed around them. ``requires_moderator`` is a flag, not a prohibition.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "BOT_USERNAME",
    "BATCH_FLOW",
    "Step",
    "Command",
    "MENU",
    "MENU_NAMES",
    "FORBIDDEN",
    "MODERATOR_ONLY",
    "command_for",
    "requires_moderator",
    "parse_menu",
    "diff",
    "still_unknown",
    "flow_note",
    "OBSERVED_ON",
    "FLOW_OBSERVED_ON",
    "PARENT_CHANNEL",
]

#: The bot we talk to. Stored without the ``@`` because that is how every other username in
#: this repository is stored, and a mismatch here means a lookup that never hits.
BOT_USERNAME = "anime_hindifilesbot"

#: When this was observed, and by what means. If a live probe ever disagrees with `MENU`, that
#: date is the first thing worth quoting.
OBSERVED_ON = "2026-08-28"

#: The day the operator walked through `/batch` in their own clone and sent five screenshots of it.
#: A menu is a list of verbs; this is the conversation, so it gets its own date.
FLOW_OBSERVED_ON = "2026-08-29"

#: Where this bot comes from. The operator told us their storing and linking bots are *clones* of
#: @Md_Files_Store_Bot, created and managed by @Md_CloneManagerBot, and that vendor's public channel
#: is the only documentation any of it has. Everything read from there is quoted, dated, and held as
#: "the vendor's word", never as an observation of ours: see `docs/storage-bot.md`.
PARENT_CHANNEL = "https://t.me/venombotupdates"



@dataclass(frozen=True, slots=True)
class Step:
    """One question the bot asks in its own words, and what we would answer it with."""

    #: The prompt exactly as the bot wrote it, capitalisation and doubled dots included. It is a
    #: match target one day, and a tidied copy of somebody's UI text never matches their UI text.
    verbatim: str
    #: What this program would send, in our words.
    ours: str


#: The `/batch` conversation, as the operator ran it on 2026-08-29: two messages asked for, one
#: link handed back. The last line is quoted because the wording of that answer is shared with
#: @Link_providerobot — both bots say "Here is your link:" — so which bot answered can only be read
#: from the username inside the link. `app.linkprovider.parse_reply` refuses a reply whose link
#: belongs to a different bot; that check exists because of this sentence.
BATCH_FLOW: tuple[Step, ...] = (
    Step(
        "Forward The Batch First Message From your Batch Channel (With Forward Tag).. or Give Me Batch First Message link from your batch channel",
        "the first message of the range, as a tagged forward or a link to that message",
    ),
    Step(
        "Forward The Batch Last Message From Your Batch Channel (With Forward Tag).. or Give Me Batch last message link from your batch channel",
        "the last message of the range — everything between the two rides along, labels included",
    ),
    Step(
        'Here is your link: <t.me/<bot>?start=<token>>, with a "SHARE URL" button',
        "one link for the whole range, which is what a destination post carries",
    ),
)


def flow_note() -> str:
    """The known half of the protocol, in one line, for the blocked job's reason.

    `/status` quotes this, and `docs/storage-bot.md` quotes the prompts themselves, so the sentence
    an operator reads about what is still missing comes from the same data as the doc. A summary
    written twice is a summary that goes stale in one of the two places.
    """
    asks = ", then ".join(f"it asks for {step.ours}" for step in BATCH_FLOW[:2])
    return (
        f"the /batch flow is observed ({FLOW_OBSERVED_ON}): after /batch {asks}, and the bot "
        f"replies {BATCH_FLOW[2].verbatim}"
    )


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    help: str
    #: What *our* pipeline would use it for, or why we would not. Kept beside the bot's own
    #: wording so a future reader can tell the two apart: `help` is theirs, `ours` is ours.
    ours: str = ""

    @property
    def requires_moderator(self) -> bool:
        return "moderator" in self.help.casefold()


#: Verbatim from the operator's screenshots, including the bot's spelling ("mutiple"). The
#: wording is quoted rather than tidied for the same reason the approved captions are: this is
#: somebody else's interface, and a test that compares our parse against a live menu has to be
#: comparing the *same string*.
MENU: tuple[Command, ...] = (
    Command(
        "/start",
        "Check i am alive",
        "the only command we may send during a probe: it opens the menu and proves the bot "
        "answers this account",
    ),
    Command(
        "/genlink",
        "To store a single message or file",
        "one episode file, one link — the single-variant case, and the one every quality of an "
        "episode goes through before the manifest is built",
    ),
    Command(
        "/batch",
        "To store mutiple messages from a channel",
        "one batch per episode, holding every quality of it, and one last batch for the whole "
        "season — the operator's decision of 2026-08-29, which the flow supports: a batch is a "
        "range between two messages, so four files sitting next to each other are one range and "
        "no re-indexing is needed. Whether it stores *forwarded copies* or references the source "
        "is still unobserved",
    ),
    Command(
        "/custom_batch",
        "To store multiple random messages",
        "an explicit list of messages instead of a range: the fallback for a season whose qualities "
        "do *not* sit next to each other in the source channel, which is the case /batch cannot "
        "express",
    ),
    Command(
        "/special_link",
        "store multiple messages and get an editable link (moderators only)",
        "the edit-in-place half of the missing-quality rule: add a quality later, edit the link "
        "the post already carries, instead of posting a second time. 'Moderators only' turned out to "
        "mean *our own clone's* moderator list, which the owner appoints — not a moderator of the "
        "source channel, so nothing here depends on a stranger's permission",
    ),
    Command(
        "/universal_link",
        "stores multiple messages that can be accessed from any of your clones (moderator only).",
        "one link that survives a clone being dropped, which is what a permanent season post "
        "needs. 'Your clones' are ours after all — up to three per Telegram account, made with "
        "@Md_CloneManagerBot — and this vendor's *parent* bots do get taken down every year or so "
        "while the clones keep serving, which is exactly the failure this verb is for",
    ),
    Command("/shortener", "To shorten any shareable links", "cosmetic; a short link in a caption is not a shorter file"),
    Command(
        "/settings",
        "Customize Your settings as your need",
        "where retention and naming live, and several of the open questions below are switches on "
        "this screen rather than facts about the service: the autodelete timer, No Forward, and "
        "Public vs Private Mode. Ours to set, not to wait for",
    ),
    Command(
        "/broadcast",
        "Broadcast a messages to users (moderators only)",
        "never sent from a queue. This is our own clone messaging our own users, so it is not "
        "someone else's service to abuse — but a job that can reach an audience is still the sort "
        "of thing that gets a user account restricted, and an announcement belongs in a channel as "
        "a post, where a human chose to be",
    ),
    Command(
        "/ban",
        "Ban a user (moderators only)",
        "never sent, for the same reason as /broadcast: the right to remove a person from our clone "
        "is a human's decision, made after reading a complaint, not a step in a pipeline",
    ),
    Command(
        "/unban",
        "Unban a user (moderators only)",
        "never sent, for the same reason as /broadcast",
    ),
)

MENU_NAMES: tuple[str, ...] = tuple(command.name for command in MENU)

#: Commands the program is never allowed to send, whatever a config row or an allowlist says.
FORBIDDEN: frozenset[str] = frozenset(
    command.name for command in MENU if "never sent" in command.ours
)

MODERATOR_ONLY: frozenset[str] = frozenset(
    command.name for command in MENU if command.requires_moderator
)

#: Our purposes, in the words the rest of the code uses, mapped onto the bot's verbs. A job kind
#: that cannot name its command is a job that will improvise one at runtime.
_PURPOSES: dict[str, str] = {
    "single": "/genlink",
    "channel_batch": "/batch",
    "custom_batch": "/custom_batch",
    "editable_link": "/special_link",
    "universal_link": "/universal_link",
    "shorten": "/shortener",
    "alive": "/start",
}


def command_for(purpose: str) -> str:
    """Which of the bot's commands serves this need.

    Raises on an unknown purpose on purpose. A default here would mean a job silently picking a
    plausible-looking command it was never designed to send.
    """
    try:
        return _PURPOSES[str(purpose).strip().casefold()]
    except KeyError:
        # The whole map, not just the left column: whoever reads this in a blocked job's reason is
        # deciding whether to add a purpose or to fix a typo, and both answers need the verbs.
        known = ", ".join(f"{name} -> {command}" for name, command in sorted(_PURPOSES.items()))
        raise ValueError(f"no storage-bot command is mapped for purpose {purpose!r}; known: {known}") from None


def requires_moderator(name: str) -> bool:
    return str(name).strip() in MODERATOR_ONLY


def parse_menu(text: str | None) -> list[tuple[str, str]]:
    """Read a bot menu message into ``[(command, help), ...]``.

    Telegram renders a bot's commands as ``/word`` on one line and its description on the next,
    which is also exactly what a plain-text paste of the menu looks like. Anything that does not
    start a line with a slash is treated as the continuation of the previous description, so the
    multi-line help above survives a round trip through a screenshot.
    """
    out: list[list[str]] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip().lstrip("\u200b")
        if not line:
            continue
        head = line.split(" ", 1)[0]
        if head.startswith("/") and len(head) > 1:
            out.append([head, line[len(head) :].strip()])
        elif out:
            out[-1][1] = (out[-1][1] + " " + line).strip()
        # A stray line before the first command (a greeting, a bot header) is dropped: it is
        # not part of any command, and guessing which one it belongs to would corrupt that one.
    return [(name, help_text) for name, help_text in out]


def diff(observed: list[tuple[str, str]] | tuple[tuple[str, str], ...]) -> dict[str, list[str]]:
    """How a live menu differs from the one recorded here.

    Returns ``{"missing": [...], "added": [...], "changed_help": [...]}`` — and the whole point of
    returning three lists instead of a bool is that ``/broadcast`` disappearing and ``/genlink``
    *appearing* are different pieces of news. A removed command is a job kind that will fail; a
    new one may be the thing that finally makes the batch flow unnecessary.
    """
    seen_names = {name for name, _ in observed}
    recorded = {command.name: command.help for command in MENU}
    changed = [
        name for name, help_text in observed if name in recorded and help_text and help_text != recorded[name]
    ]
    return {
        "missing": sorted(set(recorded) - seen_names),
        "added": sorted(seen_names - set(recorded)),
        "changed_help": sorted(changed),
    }


def still_unknown() -> tuple[str, ...]:
    """What the menu and the vendor's own channel still do not answer.

    Returning them as data keeps the stub's message honest and checkable: it is not "we have not
    gotten to it", it is "these specific facts are missing, and here they are".

    Four questions this list used to carry are answered and were removed: what `/batch` asks for
    next (`BATCH_FLOW`), whether its answer is text or a button (both, and the text carries the
    link), what "moderators only" means (our clone's own moderator list), and what "your clones"
    means (up to three per Telegram account, made by @Md_CloneManagerBot). What is left is either
    behaviour only a live run can see, or a switch on the operator's `/settings` screen.
    """
    return (
        "whether a batch link can be appended to later, or whether /special_link's \"edit\" means "
        "re-issuing the range and then editing the destination post that carries the new link",
        "whether a link is a reference to the post in the source channel or a copy the bot made for "
        "itself: the vendor advertises \"no db channel required\", which points at a reference, "
        "and a reference is only as durable as the message it points at. This is the question that "
        "decides how much our zero-deletion rule is protecting *other people's* links",
        "which rights our account actually has on our own clone — the moderator list that "
        "/special_link and /universal_link are gated by is ours to fill, and nobody has read it yet",
        "whether the clone is in Public Mode (\"any telegram user can generate shareable & shorten "
        "links using clone\") or Private Mode, since a clone that can read a private channel and is "
        "open to strangers is a private channel anyone can hand out links from",
        "whether \"No Forward\" or content protection is on, which would contradict the bot's own "
        "advice to save files, and would have to be checked against our first step, which is a "
        "forward out of the source channel",
        "what the deletion timer really covers — the operator can set it, and what it takes away is "
        "the delivered copy, not the stored message — which is why no post of ours may ever "
        "reference a message id inside the bot chat",
        "whether a link can be revoked, and what a revoked link does to a post that already carries it",
        "rate limits per account, since the free tier cannot afford a retry storm. One half of the "
        "link's future is answered already: the operator's word (2026-08-29) is that a link does not "
        "expire, and that the deletion notice covers only the copy delivered to a user and not the "
        "stored message — so what a published post still risks is a rate limit, and a regenerated "
        "invite behind the card while every old post goes on pointing at the old one",
    )
