"""Editable caption text: handle hygiene and template rendering.

The spec's rule is small but specific — *disallowed* usernames inside captions
we can edit get replaced with the primary pair, and links must survive that
substitution. Both halves matter: a naive replace destroys the storage links
(``t.me/anime_hindifilesbot/1234`` contains a username), and no replace leaves a
leech channel's handle in our channel forever.

So URLs are masked first, then mentions are rewritten, then the mask is
restored. Templates likewise never raise on an unknown placeholder: a partially
filled caption still posts, and the missing keys come back to the caller so the
job can log them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

__all__ = [
    "APPROVED_TEMPLATES",
    "BUTTON_ROWS",
    "TOTAL_UNKNOWN",
    "Cleaned",
    "archive_values",
    "audio_label",
    "button_lines",
    "clean_handles",
    "episode_range",
    "pad_number",
    "placeholder_keys",
    "post_values",
    "primary_footer",
    "render_caption",
    "render_template",
    "safe_filename",
    "title_with_subtitle",
    "total_episodes",
]

PRIMARY_HANDLES = ("ycanime", "india_crunchyroll")

#: Anything that looks like a URL, including bare ``t.me/<bot>/<message>`` deep
#: links. These are left completely alone — a "cleaned" caption with a broken
#: download button is worse than one with a stray handle.
_URL = re.compile(
    r"(?:https?://|tg://)[^\s]+"
    r"|t\.me/(?:[A-Za-z][A-Za-z0-9_]{2,32}/\S*|\+\S+|joinchat/\S+)",
    re.I,
)
_MENTION = re.compile(r"(?<![\w/~])@([A-Za-z][A-Za-z0-9_]{4,31})")
_ME = re.compile(r"\bt\.me/([A-Za-z][A-Za-z0-9_]{4,31})(?![/\w])", re.I)

#: Characters Telegram/Bot API filenames cannot carry, plus ones that break
#: shells and the ``|`` that appears in our own footer text.
_UNSAFE = re.compile(r"""[\\/:*?"'<>|\r\n\t]+""")


@dataclass(frozen=True, slots=True)
class Cleaned:
    text: str
    removed: tuple[str, ...] = ()
    changed: bool = False

    def __bool__(self) -> bool:  # truthy when a rewrite happened
        return self.changed


def clean_handles(
    text: str | None,
    *,
    allowed: tuple[str, ...] | list[str] = PRIMARY_HANDLES,
    replacement: str = "@ycanime | @india_crunchyroll",
) -> Cleaned:
    """Replace every non-allowed handle with the primary pair, exactly once.

    ``replacement`` mirrors ``branding.footer`` in ``app.config``. When several
    foreign handles sit on one line, only the first becomes the footer and the
    rest are dropped: two footers stacked under one post is what a naive
    per-match replace produces, and it looks broken.
    """
    if not text:
        return Cleaned(text="")
    allowed_set = {h.lstrip("@").casefold() for h in allowed}

    # Mask URLs so their path segments are never treated as mentions.
    stash: list[str] = []

    def _hide(match: re.Match[str]) -> str:
        stash.append(match.group(0))
        return f"\0url{len(stash) - 1}\0"

    working = _URL.sub(_hide, text)

    removed: list[str] = []

    placed = {"done": False}

    def _swap(match: re.Match[str]) -> str:
        handle = match.group(1).casefold()
        if handle in allowed_set:
            return match.group(0)
        removed.append(handle)
        # The footer goes in once. A caption where three handles were removed and
        # three footers appeared in their place is not "cleaned", and stacked
        # signatures under one post is exactly what a per-match replace produces.
        if placed["done"]:
            return ""
        placed["done"] = True
        return "\0footer\0"

    working = _MENTION.sub(_swap, working)
    working = _ME.sub(_swap, working)
    working = working.replace("\0footer\0", replacement)
    for index, url in enumerate(stash):
        working = working.replace(f"\0url{index}\0", url)

    # Collapse the artefacts of dropping handles: double spaces, empty lines.
    working = re.sub(r"[ \t]{2,}", " ", working)
    working = re.sub(r"\n{3,}", "\n\n", working)
    changed = bool(removed)
    return Cleaned(text=working.strip(), removed=tuple(dict.fromkeys(removed)), changed=changed)


def placeholder_keys(template: str) -> tuple[str, ...]:
    """``{title}`` -> ``('title',)``; used to validate a template before use."""
    return tuple(dict.fromkeys(re.findall(r"\{([a-z_][a-z0-9_]*)\}", template or "", re.I)))


def render_template(template: str | None, values: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    """Fill ``{placeholders}`` and report which ones were missing.

    An unknown placeholder is left intact rather than raising, because the
    template is operator-editable and a typo in ``templates.episode_post`` must
    degrade to an odd-looking caption plus a logged warning, not to a failed
    publish. ``{{`` and ``}}`` pass through as literal braces.
    """
    if not template:
        return "", ()
    missing: list[str] = []
    out: list[str] = []
    i = 0
    while i < len(template):
        char = template[i]
        if char == "{" and template[i + 1 : i + 2] == "{":
            out.append("{")
            i += 2
            continue
        if char == "}" and template[i + 1 : i + 2] == "}":
            out.append("}")
            i += 2
            continue
        if char == "{":
            end = template.find("}", i)
            name = template[i + 1 : end]
            if end != -1 and re.fullmatch(r"[a-z_][a-z0-9_]*", name, re.I):
                if name in values and values[name] not in (None, ""):
                    out.append(str(values[name]))
                else:
                    missing.append(name)
                    out.append("{" + name + "}")
                i = end + 1
                continue
        out.append(char)
        i += 1
    return "".join(out).strip(), tuple(dict.fromkeys(missing))


def safe_filename(name: str | None, *, separator: str = "_", limit: int = 120) -> str:
    """A filename for the archive channel.

    ``branding.filename_separator`` exists because the footer contains ``|`` and
    ``|`` is illegal in a filename: the separator is configurable rather than
    hardcoded so an operator can change the style without a deploy.
    """
    if not name:
        return "untitled"
    text = _UNSAFE.sub(separator, name)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if len(text) > limit:
        stem, _, ext = text.rpartition(".")
        if ext and len(ext) <= 5:
            text = f"{stem[: limit - len(ext) - 1]}.{ext}"
        else:
            text = text[:limit]
    return text or "untitled"


def primary_footer(handles: Sequence[str] | tuple[str, ...] | list[str] = PRIMARY_HANDLES) -> str:
    """The one-line signature every caption ends with."""
    return " | ".join("@" + h for h in handles)


# ---------------------------------------------------------------------------
# The approved caption formats (operator-supplied, 2026-08-28)
#
# These are the *code's* copy of what the operator typed. The database wins — the
# publish path reads ``app.config`` first and falls back to these — but having
# them here means a fresh database, a reviewable migration and a test all agree on
# one string. ``tests/test_caption_templates.py`` asserts exactly that, so the SQL
# and Python cannot drift apart silently.
#
# Two deliberate normalisations, both stated to the operator rather than hidden:
#   * the box-drawing lines use single newlines (double-spaced lines break the
#     ``╭ ┣ ┣ ╰`` shape, which only reads as a box when the strokes are adjacent);
#   * the archive and the destination render one stored title, so the private
#     archive and the public post can never disagree about how a series is spelled.
# ---------------------------------------------------------------------------

#: What ``◎ Total Episodes`` shows when the season's length was never stated. The
#: number is only known from ``season.last_episode``; inferring it from the highest
#: episode seen so far would promise a completion we have not observed.
TOTAL_UNKNOWN = "TBA"

APPROVED_TEMPLATES: dict[str, str] = {
    "templates.archive_caption": (
        "‣ {title_full} (S - {season})\n\n"
        "╭────────────────────\n"
        "┣Quality: {quality}\n"
        "┣Episode: {episode}\n"
        "┣Audio: {audio} #O𝖿𝖿𝗂𝖼𝗂𝖺𝗅\n"
        "╰────────────────────\n\n"
        "‣ Powered By: @india_crunchyroll\n"
        "@YC_Anime"
    ),
    "templates.episode_post": (
        "✦ {title_full} ✦\n\n"
        "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
        "⌲ 𝗦𝗲𝗮𝘀𝗼𝗻: {season}\n"
        "❍ 𝗘𝗽𝗶𝘀𝗼𝗱𝗲: {episode}\n"
        "〄 𝗔𝘂𝗱𝗶𝗼: {audio}\n"
        "◎ 𝗧𝗼𝘁𝗮𝗹 𝗘𝗽𝗶𝘀𝗼𝗱𝗲𝘀: {total_episodes}\n"
        "♡ 𝗣𝗼𝘄𝗲𝗿𝗲𝗱 𝗯𝘆: @YC_Anime , @India_crunchyroll\n"
        "╚━━━━━━━━━━━━━━━━━━━━━╝"
    ),
    "templates.season_post": (
        "✦ {title_full} ✦\n\n"
        "╔━━━━━━━━━━━━━━━━━━━━━╗\n"
        "⌲ 𝗦𝗲𝗮𝘀𝗼𝗻: {season}\n"
        "❍ 𝗘𝗽𝗶𝘀𝗼𝗱𝗲: {episode_range}\n"
        "〄 𝗔𝘂𝗱𝗶𝗼: {audio}\n"
        "◎ 𝗧𝗼𝘁𝗮𝗹 𝗘𝗽𝗶𝘀𝗼𝗱𝗲𝘀: {total_episodes}\n"
        "♡ 𝗣𝗼𝘄𝗲𝗿𝗲𝗱 𝗯𝘆: @YC_Anime , @India_crunchyroll\n"
        "╚━━━━━━━━━━━━━━━━━━━━━╝"
    ),
    # Channel Help's inline-button syntax is "text - url"; "&&" puts buttons on
    # one row, a newline starts the next. The fancy ❐ brackets are the operator's,
    # and the label stays identical for a single link — the quality is only named
    # when there is more than one to choose between.
    "templates.episode_button": "❐ 𝗪𝗮𝘁𝗰𝗵/𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱 ❐ - {storage_link}",
    "templates.episode_button_multi": "❐ 𝗪𝗮𝘁𝗰𝗵/𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱 {quality} ❐ - {storage_link}",
    "templates.season_button": "❐ 𝗪𝗮𝘁𝗰𝗵/𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱 ❐ - {storage_link}",
}

#: One button per row. ``"pair"`` would join them with ``&&``; kept as a setting
#: rather than a code change because it is a taste decision the operator may flip
#: after seeing a real post, and a redeploy should not be the price of that.
BUTTON_ROWS = "one_per_line"


def pad_number(value: int | str | None, width: int = 2) -> str:
    """``11 -> "11"``, ``1 -> "01"``, anything else unchanged.

    The operator's two samples showed ``Episode: 11`` and ``Episode: 01``, so
    zero-padding is what makes both of them right at once.
    """
    if value is None:
        return ""
    text = str(value).strip()
    if text.isdigit():
        return text.zfill(width)
    return text


def title_with_subtitle(title: str | None, subtitle: str | None = None) -> str:
    """``("Dekin no mogura", "The earthbound mole")`` -> one string, with or without
    the subtitle. The separator disappears entirely when there is no subtitle, so
    an incomplete record never prints a dangling colon in a public channel.
    """
    main = (title or "").strip()
    extra = (subtitle or "").strip()
    if not main:
        return extra
    if not extra:
        return main
    return f"{main}: {extra}"


def audio_label(audio_kind: str | None, languages: Sequence[str] | None = None) -> str:
    """The ``〄 𝗔𝘂𝗱𝗶𝗼:`` value: ``Hindi``, or ``Hindi + English`` for dual audio.

    Unknown is printed as ``Unknown`` rather than left empty or guessed: an empty
    line under a box border looks like a formatting bug, and an invented language
    claim is a false statement about the file. Either way the publish gate is
    elsewhere (``thumbnail.is_publishable`` + ``normalize``'s disposition), so this
    function has no opinion to be wrong about.
    """
    names = [str(lang).strip().title() for lang in (languages or ()) if str(lang).strip()]
    if names:
        return " + ".join(dict.fromkeys(names))
    kind = (audio_kind or "").strip().casefold()
    return {
        "hindi": "Hindi",
        "dual_audio": "Hindi + English",
        "multi_audio": "Hindi + English",
        "subbed_only": "Subbed",
        "non_hindi_dub": "Non-Hindi dub",
    }.get(kind, "Unknown")


def episode_range(
    first: int | str | None, last: int | str | None, *, unknown_label: str | None = None
) -> str:
    """``(1, 12) -> "01 - 12"``; a season of unknown length prints the open end.

    A batch post is only due when the season is complete, so a missing ``last``
    here means the record is incomplete rather than that the show is unfinished —
    the caption says so instead of guessing a number.
    """
    start = pad_number(first)
    end = pad_number(last)
    unknown = unknown_label or TOTAL_UNKNOWN
    if not start:
        return end or unknown
    if not end:
        return f"{start} - {unknown}"
    return start if start == end else f"{start} - {end}"


def total_episodes(last: int | str | None, *, unknown_label: str | None = None) -> str:
    """A declared length, or ``TBA``.

    ``0`` counts as "not declared" on purpose: a season that believes it has zero
    episodes has lost its `season.last_episode`, and printing the arithmetic result
    in a public channel is a claim the record cannot support.
    """
    if last in (None, "", 0, "0"):
        return unknown_label or TOTAL_UNKNOWN
    return pad_number(last, width=0)


def archive_values(
    *,
    title: str | None,
    subtitle: str | None = None,
    season: int | str | None = None,
    episode: int | str | None = None,
    quality: str | None = None,
    audio_kind: str | None = None,
    languages: Sequence[str] | None = None,
) -> dict[str, str]:
    """Everything ``templates.archive_caption`` can ask for, and nothing else."""
    return {
        "title": (title or "").strip(),
        "title_full": title_with_subtitle(title, subtitle),
        # Padded here: the approved archive line is "(S - 01)".
        "season": pad_number(season),
        "episode": pad_number(episode),
        "quality": (quality or "").strip(),
        "audio": audio_label(audio_kind, languages),
    }


def post_values(
    *,
    title: str | None,
    subtitle: str | None = None,
    season: int | str | None = None,
    episode: int | str | None = None,
    first_episode: int | str | None = None,
    last_episode: int | str | None = None,
    audio_kind: str | None = None,
    languages: Sequence[str] | None = None,
    quality_list: Sequence[str] | None = None,
    unknown_label: str | None = None,
) -> dict[str, str]:
    """Shared payload for the per-episode and the season-batch posts.

    Both templates use the same placeholder names on purpose: the batch differs
    only in ``{episode_range}``, so an operator editing one box's lines does not
    have to remember which keys the other one expects.

    ``unknown_label`` is the caller's hook for ``caption.total_episodes_unknown``:
    the value lives in ``app.config`` so the wording of a hedge is yours, and this
    module's constant is only the fallback when that row is missing.
    """
    values = {
        "title": (title or "").strip(),
        "title_full": title_with_subtitle(title, subtitle),
        # Bare, not zero-padded: the operator's destination box reads
        # "𝗦𝗲𝗮𝘀𝗼𝗻: 1" while the archive line reads "(S - 01)". Padding one and not
        # the other is their style choice, so it is preserved rather than tidied —
        # a reviewer who "fixed" either one would change published text.
        "season": "" if season in (None, "") else str(season).strip(),
        "episode": pad_number(episode),
        "episode_range": episode_range(first_episode, last_episode, unknown_label=unknown_label),
        "total_episodes": total_episodes(last_episode, unknown_label=unknown_label),
        "audio": audio_label(audio_kind, languages),
        "quality_list": ", ".join(str(q) for q in (quality_list or ()) if str(q).strip()),
    }
    return {key: value for key, value in values.items() if value != ""}


def render_caption(
    template: str | None,
    values: Mapping[str, Any],
    *,
    key: str = "",
) -> tuple[str, tuple[str, ...]]:
    """``render_template`` with the approved fallback and a named key for errors.

    A caption is never posted with a literal ``{quality}`` in it: a missing value
    means the payload is wrong, and publishing the placeholder is how a template
    edit turns into hundreds of broken posts. The caller is expected to refuse on a
    non-empty ``missing`` and route the job to review instead.
    """
    text, missing = render_template(template or APPROVED_TEMPLATES.get(key, ""), values)
    return text, tuple(dict.fromkeys(missing))


def button_lines(
    links: Sequence[Mapping[str, Any]],
    *,
    single: str | None = None,
    multi: str | None = None,
    rows: str = BUTTON_ROWS,
) -> tuple[str, tuple[str, ...]]:
    """Build the Channel Help button block from ``[{"link":…, "quality":…}]``.

    ``links`` arrives already in display order — the manifest decides that, never
    the order the files turned up in. One quality gets the plain
    ``❐ 𝗪𝗮𝘁𝗰𝗵/𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱 ❐`` label the operator wrote; two or more get their quality
    named, because four identical buttons on a post that offers 480p through 2160p
    is a caption that has stopped describing the file.

    Returns the text to place after the caption and any placeholder names that had
    no value, so a link we failed to receive blocks the post instead of publishing
    a button that goes nowhere.
    """
    chosen = [entry for entry in links if str(entry.get("link") or entry.get("storage_link") or "").strip()]
    if not chosen:
        return "", ("storage_link",)
    single = single or APPROVED_TEMPLATES["templates.episode_button"]
    multi = multi or APPROVED_TEMPLATES["templates.episode_button_multi"]
    missing: list[str] = []
    built: list[str] = []
    for entry in chosen:
        template = single if len(chosen) == 1 else multi
        values = {
            "storage_link": str(entry.get("link") or entry.get("storage_link")),
            "quality": str(entry.get("quality") or "").strip(),
            "episode": pad_number(entry.get("episode")),
            "season": pad_number(entry.get("season")),
        }
        text, lost = render_template(template, values)
        # A multi-quality template with no quality on one entry would print the
        # literal {quality}; fall back to the unlabelled form for that button.
        if lost and "quality" in lost and len(chosen) > 1:
            text, lost2 = render_template(single, values)
            lost = tuple(k for k in lost2 if k != "quality")
        missing.extend(lost)
        built.append(text)
    joiner = " && " if str(rows).casefold() in {"pair", "same_row", "one_row"} else "\n"
    return joiner.join(built), tuple(dict.fromkeys(missing))
