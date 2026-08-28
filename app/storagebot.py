"""``@anime_hindifilesbot``, as observed — the menu, not a guess about the menu.

Until 2026-08-28 this whole module was a question mark. The bot's protocol could not be read
from any documentation, and the operator would not screen-record the flow, so the honest state
was "we will discover it live". The discovery then happened the cheapest possible way: the
operator sent four screenshots of the bot's own command list, which is *menu text*, not a
recorded session, and is exactly the kind of observation that costs nothing to obtain and pays
for itself the first time a menu changes.

What that fixes: which verb does which job. What it does **not** fix, and what therefore keeps
``storage_upload`` blocked instead of quietly implemented: what each command asks for next,
whether its answer is a text message or a button, what a finished link looks like, whether a
batch can be appended to, and whether a link can be revoked. `still_unknown` is that list, and
it is deliberately longer than the list of things we now know.

Two safety rules live here because they belong to this bot and nowhere else:

* **we never send** ``/broadcast``, ``/ban`` or ``/unban``. Those are the bot's own moderation
  tools aimed at *people*. Our pipeline's job is to store files and hand back links; a program
  that can broadcast to a stranger's user list is a different program, and one the operator
  would have to audit forever. :mod:`app.probe` refuses them even if its allowlist is widened.
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
    "OBSERVED_ON",
]

#: The bot we talk to. Stored without the ``@`` because that is how every other username in
#: this repository is stored, and a mismatch here means a lookup that never hits.
BOT_USERNAME = "anime_hindifilesbot"

#: When this was observed, and by what means. If a live probe ever disagrees with `MENU`, that
#: date is the first thing worth quoting.
OBSERVED_ON = "2026-08-28"


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
        "a whole channel or season at once, which is what a season-batch link should be built "
        "from; whether it stores *forwarded copies* or references the source is unobserved",
    ),
    Command(
        "/custom_batch",
        "To store multiple random messages",
        "an explicit list of messages — the shape the ordered manifest actually wants, because a "
        "season is 360p through 2160p in a chosen order and not whatever a channel happens to "
        "contain",
    ),
    Command(
        "/special_link",
        "store multiple messages and get an editable link (moderators only)",
        "the edit-in-place half of the missing-quality rule: add a quality later, edit the link "
        "the post already carries, instead of posting a second time",
    ),
    Command(
        "/universal_link",
        "stores multiple messages that can be accessed from any of your clones (moderator only).",
        "one link that survives a clone being dropped, which is what a permanent season post "
        "needs; 'your clones' means the bot's own mirror set, and ours is not configured yet",
    ),
    Command("/shortener", "To shorten any shareable links", "cosmetic; a short link in a caption is not a shorter file"),
    Command(
        "/settings",
        "Customize Your settings as your need",
        "where retention and naming live, if they live anywhere; unread so far",
    ),
    Command(
        "/broadcast",
        "Broadcast a messages to users (moderators only)",
        "never sent. this is the bot messaging *people*, which is not this pipeline's job and is "
        "the sort of capability that gets an account restricted",
    ),
    Command(
        "/ban",
        "Ban a user (moderators only)",
        "never sent, for the same reason as /broadcast",
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
    """The questions the menu does not answer — i.e. why ``storage_upload`` is still a stub.

    Returning them as data keeps the stub's message honest and checkable: it is not "we have not
    gotten to it", it is "these specific facts are missing, and here they are".
    """
    return (
        "what each command asks for after it is sent (a forwarded message id? a file? a channel "
        "handle? a number of messages?)",
        "whether the answer is a text message with a URL, or a button, or both",
        "the shape and lifetime of a link: does it expire, and is it a t.me link to a stored copy "
        "or a redirect the bot serves itself?",
        "whether a batch can be appended to later, or whether adding one quality means a new link "
        "and therefore an edited destination post",
        "what 'moderators only' means for /special_link and /universal_link: moderator of the bot's "
        "service, or of the channel the messages come from",
        "what 'your clones' are for /universal_link, and whether we get one, several, or none",
        "whether a link can be revoked, and what a revoked link does to a post that already carries it",
        "rate limits per account, since the free tier cannot afford a retry storm",
    )
