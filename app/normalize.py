"""Metadata normalization: a filename and caption in, a publish decision out.

This module exists for one rule in the spec: **a file may be published only if
it carries Hindi audio.** Subbed-only releases are not lower priority, they are
out of the product; dual- and multi-audio releases are in. Getting that wrong in
one direction silently drops real content, and wrong in the other posts junk into
a finished channel — so every result carries either a decision or an explicit
reason it is unresolved, and *unresolved never publishes*.

Everything here is pure: no Telegram, no database, no config reads. Policy values
(``require_hindi_audio``, ``include_subbed_only``, ``quality.order``) are
arguments, mirroring ``app.config`` so the operator can change behaviour without
a deploy and so the whole layer is testable without a cluster.

Recognition is deliberately conservative. An unrecognised name becomes ``pending``
for a human, never a guess, and episode numbers come from explicit markers only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .keys import canonical_episode_key, normalize_title, quality_rank

__all__ = [
    "AudioKind",
    "Disposition",
    "ParsedEpisode",
    "parse_episode",
    "detect_languages",
    "detect_quality",
    "detect_episode_numbers",
    "extract_series_title",
    "detect_handles",
    "language_display",
]

# ---------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------

#: Language names, matched as whole or joined tokens (``HindiDubbed``).
_LANGUAGES: dict[str, tuple[str, ...]] = {
    "hindi": ("hindi", "hin", "hindidub", "hindidubbed", "hindidub", "hin"),
    "english": ("english", "eng"),
    "tamil": ("tamil", "tam"),
    "telugu": ("telugu", "tel"),
    "malayalam": ("malayalam", "mal"),
    "kannada": ("kannada", "kan"),
    "bengali": ("bengali", "ben"),
    "marathi": ("marathi", "mar"),
    "gujarati": ("gujarati", "guj"),
    "japanese": ("japanese", "jpn", "jp"),
    "chinese": ("chinese", "chn"),
    "korean": ("korean", "kor"),
    "spanish": ("spanish", "spa"),
    "french": ("french", "fre"),
    "german": ("german", "deu"),
    "italian": ("italian", "ita"),
    "portuguese": ("portuguese", "por"),
    "russian": ("russian", "rus"),
    "arabic": ("arabic", "ara"),
}

#: Languages that are an *audio track*. Japanese is absent on purpose: in this
#: pipeline "Japanese" plus subtitles is the subbed case, not a Hindi dub.
_AUDIO_LANGUAGES = frozenset(
    {"hindi", "english", "tamil", "telugu", "malayalam", "kannada", "bengali", "marathi", "gujarati"}
)

#: Words that mean the file has dubbing (an audio track), not just subtitles.
_DUB_TOKENS = ("dub", "dubbed", "dubbing", "duba", "dubz", "audio")

#: Words that mean "original audio, foreign subtitles".
# "Subtitled" and "Sub-Dub" are how these channels actually spell it, and a
# missed token here is the difference between "out of scope, subbed" and "out of
# scope, foreign dub" in the review queue — the reason has to be the true one.
_SUB_TOKENS = (
    "sub", "subs", "subbed", "subbing", "subtitle", "subtitles", "subtitled",
    "subed", "softsub", "hardsub", "hsub", "subs-only", "subonly", "sub-only",
)

#: Explicit multi-track markers. "Dual Audio" on a Hindi file is enough to keep
#: it even when the second language is never named.
_MULTI_MARKERS = ("dual audio", "multi audio", "multi-audio", "dual", "multi")

_SEPARATORS = re.compile(r"""[\s._\-@#()\[\]{}|/,\\!?*+=~`^$%&:;"']+""")

_ARCHIVE_EXT = re.compile(r"\.(zip|rar|7z|tar|gz|cbz|cbr)$", re.I)

_BATCH_TOKENS = (
    "batch",
    "complete series",
    "complete season",
    "full season",
    "whole season",
    "all episodes",
    "all eps",
    "season pack",
    "pack",
    "collection",
    "batch download",
)

#: Single-feature markers. OVA/Special are only a movie when they carry no
#: episode number; ``Episode 0`` style specials keep their number.
_MOVIE_TOKENS = ("movie", "film", "feature film", "the movie")
_SPECIAL_TOKENS = ("ova", "special", "specials", "oad", "ONA")

_QUALITY_WORDS = {
    "hd": "720p",
    "fhd": "1080p",
    "fullhd": "1080p",
    "uhd": "2160p",
    "4k": "2160p",
    "5k": "2880p",
    "8k": "4320p",
}

#: Part/cour/volume labels are release variants, not seasons: they change the
#: dedup identity so a second cour is never mistaken for a duplicate.
_VARIANT_NUMBERED = (("part", r"\bpart\s*(\d{1,2})\b"), ("cour", r"\bcour\s*(\d{1,2})\b"), ("vol", r"\bvol(?:ume)?\.?\s*(\d{1,2})\b"))
_VARIANT_WORDS = ("uncut", "uncensored", "remux", "repack", "creditrush", "noraraw", "v2", "v3")

# ---------------------------------------------------------------------------
# enums as strings (they mirror the Postgres enums; a jsonb round-trip must not
# produce a value the database would reject mid-restart)
# ---------------------------------------------------------------------------


class AudioKind:
    HINDI = "hindi"
    DUAL_AUDIO = "dual_audio"
    MULTI_AUDIO = "multi_audio"
    SUBBED_ONLY = "subbed_only"
    NON_HINDI_DUB = "non_hindi_dub"
    UNKNOWN = "unknown"


class Disposition:
    """``app.candidate_disposition``."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


#: Language tokens used for *identity* when a file's own text named none. A file archived
#: under a channel-level audio declaration is a different release from a subbed copy of the
#: same episode, so it must not share a canonical key with it — but the token must not
#: pretend the file said something either. These markers only appear when ``languages`` is
#: empty, i.e. when ``audio_source`` is not ``caption``, and :func:`_language_tag` in
#: :mod:`app.ingest` writes the same value into the candidate row so the episode's language
#: column and its dedup key can never disagree.
_IDENTITY_LANGUAGES: dict[str, tuple[str, ...]] = {
    AudioKind.HINDI: ("hindi",),
    AudioKind.DUAL_AUDIO: ("hindi", "dual_audio"),
    AudioKind.MULTI_AUDIO: ("hindi", "multi_audio"),
    AudioKind.SUBBED_ONLY: ("subbed_only",),
    AudioKind.NON_HINDI_DUB: ("non_hindi_dub",),
}


@dataclass(frozen=True, slots=True)
class ParsedEpisode:
    """One source message, read. ``accepted`` is the only field that gates a
    publish; the rest is evidence kept for the review queue."""

    series: str | None = None
    series_slug: str | None = None
    season: int | None = None
    episode: int | None = None
    episodes: tuple[int, ...] = ()
    file_kind: str = "episode"  # episode | batch | movie | unknown
    languages: tuple[str, ...] = ()
    audio_kind: str = AudioKind.UNKNOWN
    quality: str | None = None
    quality_rank_value: int | None = None
    release_variant: str | None = None
    #: Where ``season`` came from: ``"caption"`` (a stated season), ``"hint"`` (the
    #: channel's configured default) or ``"none"`` (this module filled in 1 so the row
    #: can exist). Last, not in the middle: this dataclass is built positionally in
    #: places, and a new field must not shift anyone's arguments.
    season_source: str = "none"
    file_name: str | None = None
    file_size_bytes: int | None = None
    #: Provenance for the three fields a caption prints but a bare file cannot state.
    #: ``series_source``: ``caption`` (the file named the show), ``channel_declaration``
    #: (the operator named the channel once), ``channel_name`` (we are reading the
    #: channel's own title — one signal, not the two the spec asks for),
    #: ``caption_and_channel`` (both agree), or ``none``.
    #: ``audio_source`` / ``quality_source``: ``caption``, ``channel_declaration``,
    #: ``dimensions``, or ``none``. A channel's declaration is a *statement by you*; a
    #: missing value is not a statement by anyone, and the two must stay tellable apart
    #: months later when something looks wrong.
    series_source: str = "none"
    audio_source: str = "none"
    quality_source: str = "none"
    detected_handles: tuple[str, ...] = ()
    accepted: bool = False
    disposition: str = Disposition.PENDING
    reason: str = ""
    flags: tuple[str, ...] = field(default=())

    @property
    def needs_review(self) -> bool:
        return self.disposition == Disposition.PENDING

    @property
    def identity_languages(self) -> tuple[str, ...]:
        return self.languages or _IDENTITY_LANGUAGES.get(self.audio_kind, ())

    @property
    def series_confirmed(self) -> bool:
        """True when the series identity does not rest on the channel's own name alone.

        The spec requires *two* signals to agree before a destination channel is named
        ("cross-checking the source channel name against captions and filenames"). In a
        files-only channel — no titles, no captions, just ``episode 7`` and an mp4 — there
        is only the channel name, and channel names are frequently junk (``anime uploads
        4u``, a dated group, a renamed leech). Naming a 30k-member channel after that is
        permanent and public, so this is the flag the creation path must consult.
        ``accepted`` stays true: storing the file is safe, naming a channel is not.
        """
        return self.series_source in {"caption", "caption_and_channel", "channel_declaration"}

    @property
    def season_declared(self) -> bool:
        """True only when the source itself wrote a season number.

        This is what :func:`app.season.classify` is fed as ``labelled_season``. Passing
        ``.season`` instead would be a quiet bug with loud consequences: an accepted
        file with no label gets ``season = 1`` here, so comparing against it would look
        like "the caption says season 1" and, the moment we were filing season 2, would
        read as a rewind.
        """
        return self.season_source == "caption"

    @property
    def is_multi(self) -> bool:
        return len(self.episodes) > 1

    def episode_numbers(self) -> tuple[int, ...]:
        if self.episodes:
            return self.episodes
        return (self.episode,) if self.episode is not None else ()

    def canonical_key(self, episode: int | None = None, *, season: int | None = None) -> str:
        """Identity for one episode of one release: series + season + episode +
        languages + variant (see :mod:`app.keys`). Quality is deliberately not in
        the episode key — it lives on the variant, which is what makes a late
        1080p an edit of the same post instead of a second episode.

        ``season`` overrides the parsed number, and on a season boundary it *must*: the
        parse of an unlabelled ``Episode 1`` carries the defaulted ``1``, so keying off
        it alone would give season 2's first episode the same canonical key as season
        1's — and the dedup that protects a channel from duplicate posts would file the
        new season's opening episode as a second copy of an old one.
        """
        return canonical_episode_key(
            self.series or "unknown",
            season if season is not None else (self.season if self.season is not None else 1),
            self.episode if episode is None else episode,
            list(self.identity_languages),
            self.release_variant,
        )

    def to_payload(self) -> dict[str, Any]:
        """Stored in ``app.source_candidate.parsed`` so a re-parse can be
        compared with the original decision, and so the dashboard review shows
        why a file was kept or dropped without re-running anything."""
        return {
            "series": self.series,
            "series_slug": self.series_slug,
            "season": self.season,
            "season_source": self.season_source,
            "season_declared": self.season_declared,
            "episode": self.episode,
            "episodes": list(self.episodes),
            "file_kind": self.file_kind,
            "languages": list(self.languages),
            "audio_kind": self.audio_kind,
            "audio_source": self.audio_source,
            "quality": self.quality,
            "quality_source": self.quality_source,
            "series_source": self.series_source,
            "series_confirmed": self.series_confirmed,
            "quality_rank": self.quality_rank_value,
            "release_variant": self.release_variant,
            "detected_handles": list(self.detected_handles),
            "accepted": self.accepted,
            "disposition": self.disposition,
            "reason": self.reason,
            "flags": list(self.flags),
        }


# ---------------------------------------------------------------------------
# text views
# ---------------------------------------------------------------------------


def _tokens(text: str) -> list[str]:
    return [t for t in _SEPARATORS.split((text or "").casefold()) if t]


def _norm_words(text: str) -> str:
    """Single-spaced words, so ``Hindi   Dubbed`` and ``Hindi_Dubbed`` match."""
    return " ".join(_tokens(text))


def _flatten(text: str) -> str:
    """Lower-case, brackets/dots become spaces, **dashes between numbers kept**.

    Filename separators are noise for vocabulary but signal for ranges, and
    ``Ep 08-12`` must not become two unrelated numbers — so number patterns run
    on this view and word patterns on :func:`_norm_words`.
    """
    lowered = (text or "").casefold()
    spaced = re.sub(r"""[\[\](){}/,\\'"`|]+""", " ", lowered)
    spaced = spaced.replace(".", " ").replace("_", " ")
    spaced = re.sub(r"(?<=[a-z])[-](?=[a-z])", " ", spaced)  # "Naruto-Shippuden" -> two words
    return re.sub(r"\s+", " ", spaced).strip()


# ---------------------------------------------------------------------------
# primitives
# ---------------------------------------------------------------------------


def detect_languages(text: str) -> tuple[frozenset[str], bool, bool]:
    """(languages, saw a dub/audio marker, saw a subtitle marker)."""
    words = _tokens(text)
    flat = _flatten(text)
    found: set[str] = set()
    for name, aliases in _LANGUAGES.items():
        for alias in aliases:
            compact = alias.replace(" ", "")
            if any(w == compact or (len(compact) > 3 and compact in w) for w in words):
                found.add(name)
                break
    has_dub = any(re.search(rf"\b{re.escape(t)}\b", flat) for t in _DUB_TOKENS) or any(
        m in flat for m in _MULTI_MARKERS
    )
    has_sub = any(re.search(rf"\b{re.escape(t)}\b", flat) for t in _SUB_TOKENS)
    return frozenset(found), has_dub, has_sub


#: The words an operator may use for a channel's audio, mapped to ``AudioKind``.
#: ``app.source_channel.declared_audio`` has a CHECK over the same left-hand column, so
#: the database and this map cannot drift into accepting different values.
DECLARED_AUDIO: dict[str, str] = {
    "hindi": AudioKind.HINDI,
    "dual": AudioKind.DUAL_AUDIO,
    "dual_audio": AudioKind.DUAL_AUDIO,
    "multi": AudioKind.MULTI_AUDIO,
    "multi_audio": AudioKind.MULTI_AUDIO,
    "subbed": AudioKind.SUBBED_ONLY,
    "subbed_only": AudioKind.SUBBED_ONLY,
    "unknown": AudioKind.UNKNOWN,
}


def declared_audio_kind(value: object) -> str:
    """Normalise one declared-audio token, refusing anything not on the list.

    Raising matters here: a typo like ``hindi `` or ``Eng`` stored in the config column
    would otherwise be read as "no declaration", and the difference between those two
    states is whether five hundred files are archived or parked.
    """
    token = str(value or "").strip().casefold().replace("-", "_")
    if not token:
        return AudioKind.UNKNOWN
    try:
        return DECLARED_AUDIO[token]
    except KeyError:
        raise ValueError(
            f"unknown declared audio {value!r}; use one of {', '.join(sorted(DECLARED_AUDIO))}"
        ) from None


def quality_from_dimensions(height: object, width: object = None) -> str | None:
    """``1080`` -> ``1080p`` from a video's own pixels, no caption required.

    Two numbers are accepted because one is not enough to be safe: the "height" of a
    vertical clip is its long side, and a scanner that forwards Telethon's
    ``DocumentAttributeVideo`` without thinking hands over ``1920`` for a 1080-tall video.
    So when both are given, the *smaller* side wins — which is the side 1080p always
    meant.

    A files-only channel says nothing about quality, but Telegram does: a video document
    carries its ``DocumentAttributeVideo`` height, which is the one quality fact that needs
    no OCR and no download. Buckets are floors, because a 1080-tall encode is what people
    mean by 1080p even when it is 1070; a height below 240 is either junk or a thumbnail,
    and ``None`` keeps it out of the variant table rather than inventing a ``144p`` row.
    """
    sides: list[int] = []
    for value in (height, width):
        try:
            sides.append(int(value))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    if not sides:
        return None
    pixels = min(sides)
    if pixels < 240:
        return None
    for floor, label in _DIMENSION_BUCKETS:
        if pixels >= floor:
            return label
    return None


_DIMENSION_BUCKETS: tuple[tuple[int, str], ...] = (
    (2160, "2160p"),
    (1440, "1440p"),
    (1080, "1080p"),
    (720, "720p"),
    (480, "480p"),
    (360, "360p"),
    (240, "240p"),
)


def detect_quality(text: str) -> str | None:
    """Height label, canonicalised (``4K`` -> ``2160p``). ``None`` is honest: the
    variant table's quality column is required, so an unknown label is a review
    case, not a free-text row."""
    flat = _flatten(text)
    words = _tokens(text)
    for alias, canonical in _QUALITY_WORDS.items():
        if alias in words or alias in flat:
            return canonical
    match = re.search(r"\b(\d{3,4})\s*p\b", flat)
    if match and 200 <= int(match.group(1)) <= 8000:
        return f"{int(match.group(1))}p"
    return None


def detect_episode_numbers(text: str) -> tuple[tuple[int, ...], str, bool]:
    """(episode numbers, which pattern matched, was it a range).

    Confidence-ordered, first match wins: ``S01E02`` beats ``Episode 2`` beats a
    bare ``[08]``. Four digits because One Piece and Detective Conan are past
    999, and a range whose span exceeds 200 is treated as a parse error rather
    than a batch — otherwise one typo posts two hundred phantom episodes.

    Groups are *named*: the range tail sits at a different index in every
    pattern, and reading it by number is how ``Ep 1050-1055`` ended up parsing
    the tail as a negative episode.
    """
    flat = _flatten(text)
    raw = text or ""
    tail = r"(?:\s*[-–—~]\s*(?:ep|e|episode)?\.?\s*(?P<last>\d{1,4}))?"
    patterns: tuple[tuple[str, str], ...] = (
        (rf"\bs(?P<season>\d{{1,2}})[\s.]*e(?:p|pisode)?\.?\s*(?P<first>\d{{1,4}}){tail}", "sExx"),
        (r"\b(?P<first>\d{1,2})\s*x\s*(?P<last>\d{2,4})\b", "NxNN"),
        (rf"\b(?:episode|eps|ep)\.?\s*(?P<first>\d{{1,4}}){tail}", "episode word"),
        (rf"\be\.?\s*(?P<first>\d{{1,4}}){tail}", "e-number"),
        (r"\b(?P<first>\d{1,3})\s*[-–—~]\s*(?P<last>\d{1,3})\b", "bare range"),
        (r"\b(?:ch(?:apter)?|no|num)\.?\s*(?P<first>\d{1,4})\b", "chapter"),
    )
    for pattern, origin in patterns:
        for match in re.finditer(pattern, flat, re.I):
            groups = match.groupdict()
            if origin == "NxNN":
                # "04x12" is season 4 episode 12: the first number is the season,
                # and it also has to be visible to _detect_season.
                return (int(groups["last"]),), origin, False
            first = int(groups["first"])
            last = groups.get("last")
            return _expand(first, last), origin, bool(last)

    # Bracketed numbers are read from the raw text, where the brackets survive.
    bracketed = re.findall(r"\[\s*(\d{1,3})\s*\]", raw)
    if len(bracketed) == 1:
        return (int(bracketed[0]),), "bracket", False
    if len(bracketed) >= 2:
        first, last = int(bracketed[0]), int(bracketed[-1])
        if last >= first:
            return _expand(first, str(last)), "bracket range", True
    # "Frieren - 16 [Dual Audio]" is the most common shape on these channels and
    # has no explicit marker, so a number right after a dash is trusted only when
    # it is the first one in the name.
    dash_number = re.search(r"[-–—]\s*(\d{1,3})\b", flat)
    if dash_number:
        return (int(dash_number.group(1)),), "dash number", False
    return (), "none", False


def _expand(first: int, last: str | int | None) -> tuple[int, ...]:
    if last in (None, ""):
        return (first,)
    end = int(last)
    if end < first or end - first > 200:
        return (first,)  # a backwards or absurd range is a parse error
    return tuple(range(first, end + 1))


def _detect_season(text: str) -> int | None:
    """The season the *text itself* states. ``None`` means it said nothing.

    The distinction is not pedantry, it is the difference between evidence and a
    default: :mod:`app.season` treats a season read off the caption as a declared
    boundary signal, and everything else (channel config, the ``1`` this module fills
    in for convenience) as no statement at all. A number we invented must never be able
    to open a season.
    """
    flat = _flatten(text)
    for pattern in (
        r"\bs(\d{1,2})[\s.]*e(?:p|pisode)?\.?\s*\d",  # S04E16: no boundary after the digits
        r"\bs(\d{1,2})\b",
        r"\bseason\s*(\d{1,2})\b",
        r"\b(\d{1,2})\s*x\s*\d{2,4}\b",
    ):
        match = re.search(pattern, flat)
        if match:
            return int(match.group(1))
    return None


def _detect_variant(text: str) -> str | None:
    """Release edition label, which is part of the dedup identity.

    A channel that re-posts the ``Uncut`` or cour 2 version of an episode is not
    publishing a duplicate, so that distinction has to survive into the key
    rather than being smoothed away by a filename cleaner.
    """
    flat = _flatten(text)
    for label, pattern in _VARIANT_NUMBERED:
        match = re.search(pattern, flat)
        if match:
            return f"{label}-{match.group(1)}"
    for token in _VARIANT_WORDS:
        if re.search(rf"\b{re.escape(token)}\b", flat):
            return token
    return None


def detect_handles(*texts: str) -> tuple[str, ...]:
    """``@handle`` and ``t.me/handle`` mentions, de-duplicated, in first-seen order.

    Telegram usernames are 5-32 ``[A-Za-z0-9_]``. Both watermark forms are
    matched, which is what lets :mod:`app.thumbnails` tell our own marks from a
    leech channel's.
    """
    handles: list[str] = []
    for text in texts:
        if not text:
            continue
        for match in re.finditer(r"(?:^|[\s(,])@([A-Za-z][A-Za-z0-9_]{4,31})", text):
            handles.append(match.group(1))
        for match in re.finditer(r"(?:https?://)?t\.me/([A-Za-z][A-Za-z0-9_]{4,31})(?![/\w])", text):
            handles.append(match.group(1))
    seen: dict[str, None] = {}
    for handle in handles:
        seen.setdefault(handle.casefold(), None)
    return tuple(seen)


def extract_series_title(file_name: str | None, raw_caption: str | None = None) -> str | None:
    """Best-effort series name — a *hint*, never the authority.

    Drops the extension and leading ``[tag]`` groups (encoder/channel marks),
    then cuts at the first season, episode, quality or audio marker. The source
    channel's configured series outranks this, because cross-posted files get
    renamed constantly.
    """
    source = (file_name or "").strip() or (raw_caption or "").strip()
    if not source:
        return None
    stem = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", source)
    stem = stem.replace(".", " ").replace("_", " ")
    stem = re.sub(r"^\s*[\[(][^\])]{1,40}[\])]\s*", "", stem)
    stem = re.sub(r"^\s*(?:file\s*[:\-])?\s*", "", stem, flags=re.I)
    cut = re.search(
        r"\b(?:s\d{1,2}[\s.]*e\w*|s\d{1,2}\b|\d{1,2}x\d{2,4}|season|episode|eps|ep|ch\d|movie|ova|special|batch|pack|dual|multi|sub-?dub|dubbed|dub|subs?|hindi|english|eng|tamil|telugu|japanese|4k|8k)\b"
        r"|[\[\]()|]|\d{3,4}p",
        stem,
        re.I,
    )
    title = stem[: cut.start()] if cut else stem
    title = title.replace("-", " ")
    title = re.sub(r"\s{2,}", " ", title).strip(" -_.")
    if not title or len(title) < 3 or len(title) > 120 or title.isdigit():
        return None
    return title


def language_display(languages: tuple[str, ...] | list[str], audio_kind: str | None = None) -> str:
    """Caption text for the audio tracks, e.g. ``Hindi + English``."""
    order = ["hindi", "english", "tamil", "telugu", "malayalam", "kannada", "bengali", "marathi", "gujarati", "japanese"]
    known = [l for l in order if l in languages]
    known += [l for l in languages if l not in known]
    if not known:
        return "Hindi" if audio_kind == AudioKind.HINDI else "Unknown audio"
    return " + ".join(l.capitalize() for l in known)


# ---------------------------------------------------------------------------
# the decision
# ---------------------------------------------------------------------------


def parse_episode(
    *,
    file_name: str | None = None,
    raw_caption: str | None = None,
    source_series: str | None = None,
    source_series_declared: bool = False,
    season_hint: int | None = None,
    declared_audio: str | None = None,
    file_size_bytes: int | None = None,
    video_height: int | None = None,
    video_width: int | None = None,
    quality_order: tuple[str, ...] | list[str] | None = None,
    require_hindi_audio: bool = True,
    include_subbed_only: bool = False,
) -> ParsedEpisode:
    """Read one source message into a decision plus its evidence.

    Policy arguments mirror ``app.config`` (``ingest.require_hindi_audio``,
    ``ingest.include_subbed_only``, ``quality.order``); the caller reads the
    operator's settings from the database, which keeps this a pure function.

    ``source_series_declared`` / ``declared_audio`` carry the *channel-level statements* the
    operator makes with ``/source`` — which channel this is, and what its files carry. They
    exist for the case where a source channel is a shelf of bare mp4s captioned ``episode 7``
    and nothing else: there is no text to read a series or a language out of, so without a
    statement every one of those files parks as "cannot determine whether the file carries
    Hindi audio". A declaration is not a file's claim, so it is recorded as
    ``*_source = "channel_declaration"`` and stays distinguishable from "the caption said so"
    — including here, where a channel-wide statement must not outvote a file's own words.
    """
    combined = "\n".join(part for part in (file_name, raw_caption) if part)
    flags: list[str] = []

    languages, saw_dub, saw_sub = detect_languages(combined)
    audio_kind = _classify_audio(languages, saw_dub, saw_sub)
    declared = declared_audio_kind(declared_audio) if declared_audio is not None else AudioKind.UNKNOWN
    audio_source = "caption" if audio_kind != AudioKind.UNKNOWN else "none"
    if audio_kind == AudioKind.UNKNOWN and declared != AudioKind.UNKNOWN:
        audio_kind = declared
        audio_source = "channel_declaration"
        flags.append("audio_from_channel_declaration")
    elif audio_kind != AudioKind.UNKNOWN and declared not in (AudioKind.UNKNOWN, audio_kind):
        # The file's own words are kept and only the disagreement is recorded. The
        # eligibility block below still rejects a subbed-only file the channel claimed was
        # Hindi, which is the direction that actually damages the destination.
        flags.append(f"audio_disagrees_with_channel_declaration:{declared}")
    episodes, ep_origin, ranged = detect_episode_numbers(combined)
    season = _detect_season(combined)
    if season is None:
        season = season_hint
    quality = detect_quality(combined)
    derived_quality = quality_from_dimensions(video_height, video_width)
    quality_source = "caption" if quality else ("dimensions" if derived_quality else "none")
    if quality is None and derived_quality:
        quality = derived_quality
        flags.append(f"quality_from_dimensions:{video_height}")
    elif quality and derived_quality and derived_quality != quality:
        # The label wins, because that is what the uploader chose to call the file, but a
        # "720p" that is 1080 tall is either a mislabel or a re-encode — worth knowing
        # before a caption prints the label to thirty thousand people.
        flags.append(f"quality_label_disagrees_with_dimensions:{derived_quality}")
    variant = _detect_variant(combined)
    title = extract_series_title(file_name, raw_caption)
    handles = detect_handles(file_name or "", raw_caption or "")
    file_kind = _classify_file_kind(combined, file_name, episodes)

    if not episodes and file_kind not in ("batch", "movie"):
        flags.append("no_episode_number")
    if ranged:
        flags.append(f"episode_range:{len(episodes)}")
    if title is None:
        flags.append("no_title_in_filename")
    if ep_origin in ("bracket", "trailing number") and not source_series:
        # A lone number in a channel with no configured series is a weak signal.
        flags.append(f"weak_episode_marker:{ep_origin}")

    if title and episodes:
        # "Kaiju No. 8 - 08" is the show "Kaiju No. 8", not "Kaiju No 8 08".
        numbers = {str(n) for n in episodes} | {f"{n:02d}" for n in episodes}
        words = title.split()
        if words and words[-1] in numbers:
            title = " ".join(words[:-1]).strip() or None

    series = _reconcile_title(title, source_series, flags)
    both_agree = bool(
        title and source_series and normalize_title(title) == normalize_title(source_series)
    )
    if series is None:
        series_source = "none"
    elif both_agree:
        series_source = "caption_and_channel"
    elif title and series == title and not source_series:
        series_source = "caption"
    elif source_series and series == source_series and source_series_declared:
        series_source = "channel_declaration"
    else:
        # We are reading the channel's own title or @handle. That is one signal, and the
        # spec asks for two before a destination is named after it.
        series_source = "channel_name"
        if not title:
            flags.append("series_from_channel_name_only")

    # -------- eligibility ------------------------------------------------
    subbed_only = audio_kind == AudioKind.SUBBED_ONLY
    if require_hindi_audio and audio_kind in (AudioKind.SUBBED_ONLY, AudioKind.NON_HINDI_DUB):
        accepted, disposition = False, Disposition.REJECTED
        reason = (
            "subbed-only release: out of scope (Hindi audio required)"
            if subbed_only
            else f"dub without Hindi audio ({', '.join(sorted(languages)) or 'unknown'}): out of scope"
        )
    elif require_hindi_audio and audio_kind == AudioKind.UNKNOWN:
        # The branch that matters: unknown audio neither publishes nor drops the
        # file. It waits for a human, which is the only safe answer.
        accepted, disposition = False, Disposition.PENDING
        reason = "cannot determine whether the file carries Hindi audio"
        flags.append("needs_owner_review")
    elif subbed_only and include_subbed_only:
        accepted, disposition = True, Disposition.ACCEPTED
        reason = "subbed-only accepted because ingest.include_subbed_only is on"
    elif file_kind == "unknown":
        # Nothing here says episode, batch or movie. The ingest step could not
        # create a row from this (episode_number is NOT NULL), so accepting it
        # would fail later, in a place with less context.
        accepted, disposition = False, Disposition.PENDING
        reason = "cannot tell whether this is an episode, a batch or a movie; needs review"
    elif file_kind == "episode" and not episodes:
        accepted, disposition = False, Disposition.PENDING
        reason = "no episode number found; needs review"
    elif series is None:
        accepted, disposition = False, Disposition.PENDING
        reason = "series could not be identified from the channel or the file name"
    else:
        accepted, disposition = True, Disposition.ACCEPTED
        reason = f"{audio_kind} audio detected" + (f", quality {quality}" if quality else ", quality unknown")

    if accepted and quality is None:
        flags.append("no_quality_label")
    if accepted and file_kind == "batch" and not episodes:
        flags.append("batch_episode_count_unknown")

    return ParsedEpisode(
        series=series,
        series_slug=normalize_title(series) if series else None,
        series_source=series_source,
        season=season if season is not None else (1 if accepted else None),
        season_source=("caption" if _detect_season(combined) is not None else ("hint" if season_hint is not None else "none")),
        episode=episodes[0] if episodes else None,
        episodes=episodes,
        file_kind=file_kind,
        languages=tuple(sorted(languages)),
        audio_kind=audio_kind,
        audio_source=audio_source,
        quality=quality,
        quality_source=quality_source,
        quality_rank_value=quality_rank(quality, quality_order) if quality else None,
        release_variant=variant,
        file_name=file_name,
        file_size_bytes=file_size_bytes,
        detected_handles=handles,
        accepted=accepted,
        disposition=disposition,
        reason=reason,
        flags=tuple(flags),
    )


def _classify_audio(languages: frozenset[str], saw_dub: bool, saw_sub: bool) -> str:
    """Which audio tracks the file claims, and therefore whether it is in scope.

    ``japanese`` is excluded from the audio-language count on purpose: in this
    pipeline a Japanese-only file is the subbed case, not a second dub.
    """
    audio_present = languages & _AUDIO_LANGUAGES
    hindi = "hindi" in audio_present
    others = audio_present - {"hindi"}
    if hindi:
        if len(others) >= 2:
            return AudioKind.MULTI_AUDIO
        if len(others) == 1:
            return AudioKind.DUAL_AUDIO
        if saw_dub:
            return AudioKind.DUAL_AUDIO
        return AudioKind.HINDI
    # "English Subtitled" is a subtitle language, not an audio track. Without any
    # dub marker, the languages we found describe subs, and the correct label is
    # subbed_only — the reason the operator reads differs from a foreign dub, and
    # a wrong reason in a review queue is a wrong fix.
    if saw_sub and not saw_dub:
        return AudioKind.SUBBED_ONLY
    if others:
        return AudioKind.NON_HINDI_DUB
    if saw_sub or "japanese" in languages:
        return AudioKind.SUBBED_ONLY if not saw_dub else AudioKind.UNKNOWN
    return AudioKind.UNKNOWN


def _classify_file_kind(combined: str, file_name: str | None, episodes: tuple[int, ...]) -> str:
    flat = _flatten(combined)
    if any(marker in flat for marker in _BATCH_TOKENS):
        return "batch"
    if re.search(r"\bcomplete\b.{0,20}\bseason\b|\bseason\b.{0,20}\bcomplete\b", flat):
        return "batch"
    if _ARCHIVE_EXT.search((file_name or "").casefold()) and (not episodes or len(episodes) > 1):
        # An archive is never one playable episode: with no number it is a whole
        # season, with a range it is several files. A single-episode .rar stays an
        # episode, because people do pack one file and that must still ingest.
        return "batch"
    if any(marker in flat for marker in _MOVIE_TOKENS):
        return "movie"
    if any(token.casefold() in flat for token in _SPECIAL_TOKENS) and not episodes:
        return "movie"
    if episodes:
        return "episode"
    return "unknown"


def _reconcile_title(title: str | None, source_series: str | None, flags: list[str]) -> str | None:
    """The channel's configured series wins, but disagreement is recorded.

    The spec makes the source channel the authority because files get renamed
    constantly downstream. A filename that disagrees is still worth knowing:
    it usually means a one-off repost inside a series channel, which is exactly
    the case that should not silently create a second destination channel.
    """
    if source_series:
        if title:
            a, b = normalize_title(title), normalize_title(source_series)
            if a != b and a not in b and b not in a:
                flags.append(f"title_disagrees_with_channel:{title}")
        return source_series
    if title:
        flags.append("title_only_from_filename")
    return title
