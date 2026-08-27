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

__all__ = ["Cleaned", "clean_handles", "render_template", "safe_filename", "primary_footer", "placeholder_keys"]

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
