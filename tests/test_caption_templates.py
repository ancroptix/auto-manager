"""The approved caption formats, pinned to the operator's own samples.

A caption is the only part of this system the audience ever sees, so these tests
compare against the text the operator dictated, character for character — including
the box-drawing strokes, the fancy Unicode labels, and which numbers are padded. A
future "cleanup" of one of those lines is a change to published text, and it should
have to be deliberate.

The migration is checked against the code as well: the SQL the operator reviews and
runs in Supabase must contain the same strings the renderer uses, or the review
artifact is fiction.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.captions import (
    APPROVED_TEMPLATES,
    TOTAL_UNKNOWN,
    archive_values,
    audio_label,
    button_lines,
    episode_range,
    pad_number,
    placeholder_keys,
    post_values,
    render_caption,
    title_with_subtitle,
    total_episodes,
)

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = (ROOT / "supabase/migrations/0004_approved_captions.sql").read_text(encoding="utf-8")

# Exactly the operator's archive sample, modulo the documented normalisations:
# single-newline box, and one stored title for both surfaces.
ARCHIVE_SAMPLE = """‣ Dekin no mogura: The earthbound mole (S - 01)

╭────────────────────
┣Quality: 480p
┣Episode: 11
┣Audio: Hindi #O𝖿𝖿𝗂𝖼𝗂𝖺𝗅
╰────────────────────

‣ Powered By: @india_crunchyroll
@YC_Anime"""

EPISODE_SAMPLE = """✦ Dekin no mogura: The earthbound mole ✦

╔━━━━━━━━━━━━━━━━━━━━━╗
⌲ 𝗦𝗲𝗮𝘀𝗼𝗻: 1
❍ 𝗘𝗽𝗶𝘀𝗼𝗱𝗲: 01
〄 𝗔𝘂𝗱𝗶𝗼: Hindi
◎ 𝗧𝗼𝘁𝗮𝗹 𝗘𝗽𝗶𝘀𝗼𝗱𝗲𝘀: 12
♡ 𝗣𝗼𝘄𝗲𝗿𝗲𝗱 𝗯𝘆: @YC_Anime , @India_crunchyroll
╚━━━━━━━━━━━━━━━━━━━━━╝"""

BATCH_SAMPLE = """✦ Dekin no mogura: The earthbound mole ✦

╔━━━━━━━━━━━━━━━━━━━━━╗
⌲ 𝗦𝗲𝗮𝘀𝗼𝗻: 1
❍ 𝗘𝗽𝗶𝘀𝗼𝗱𝗲: 01 - 12
〄 𝗔𝘂𝗱𝗶𝗼: Hindi
◎ 𝗧𝗼𝘁𝗮𝗹 𝗘𝗽𝗶𝘀𝗼𝗱𝗲𝘀: 12
♡ 𝗣𝗼𝘄𝗲𝗿𝗲𝗱 𝗯𝘆: @YC_Anime , @India_crunchyroll
╚━━━━━━━━━━━━━━━━━━━━━╝"""

BUTTON_ONE = "❐ 𝗪𝗮𝘁𝗰𝗵/𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱 ❐ - https://t.me/anime_hindifilesbot/111"


def archive(**overrides):
    values = {
        "title": "Dekin no mogura",
        "subtitle": "The earthbound mole",
        "season": 1,
        "episode": 11,
        "quality": "480p",
        "audio_kind": "hindi",
    }
    values.update(overrides)
    return archive_values(**values)


def post(**overrides):
    values = {
        "title": "Dekin no mogura",
        "subtitle": "The earthbound mole",
        "season": 1,
        "episode": 1,
        "last_episode": 12,
        "audio_kind": "hindi",
    }
    values.update(overrides)
    return post_values(**values)


# --------------------------------------------------------------------- golden text
def test_archive_caption_is_the_sample() -> None:
    text, missing = render_caption(None, archive(), key="templates.archive_caption")
    assert text == ARCHIVE_SAMPLE
    assert missing == ()


def test_episode_post_is_the_sample() -> None:
    text, missing = render_caption(None, post(), key="templates.episode_post")
    assert text == EPISODE_SAMPLE
    assert missing == ()


def test_season_batch_is_the_sample_with_a_range() -> None:
    text, missing = render_caption(
        None, post(episode=None, first_episode=1, last_episode=12), key="templates.season_post"
    )
    assert text == BATCH_SAMPLE
    assert missing == ()


def test_the_box_is_not_double_spaced() -> None:
    """A frame only reads as a frame when its strokes are adjacent.

    The samples arrived double-spaced between every line, which is how a box looks
    typed into a chat message but not how it renders in a caption: an empty row
    between ``╭────`` and ``┣Quality`` leaves both corners floating.
    """
    for key in ("templates.archive_caption", "templates.episode_post", "templates.season_post"):
        lines = APPROVED_TEMPLATES[key].split("\n")
        # `line[:1] in "╭╰╔╚"` would also match an empty line, because "" is a
        # substring of everything — which is exactly the "blank line inside the box"
        # this test exists to forbid.
        strokes = [index for index, line in enumerate(lines) if line and line[0] in "╭╰╔╚"]
        assert len(strokes) == 2, f"{key} is missing a top or bottom border"
        top, bottom = strokes
        between = lines[top + 1 : bottom]
        assert between and all(line.strip() for line in between), (
            f"{key} has a blank line inside the box:\n{APPROVED_TEMPLATES[key]!r}"
        )
        if key == "templates.archive_caption":
            # the archive box is the one built out of `┣` field rows; the destination
            # box uses icon rows and has no `┣` at all
            assert all(line.startswith(("┣", "┃")) for line in between), f"{key}: unexpected line in the box"


def test_the_archive_and_the_post_cannot_disagree_about_the_title() -> None:
    """One placeholder, one value: the private record and the public channel."""
    fields = archive()
    same = post(episode=fields["episode"])
    assert fields["title_full"] == same["title_full"] == "Dekin no mogura: The earthbound mole"


# --------------------------------------------------------------------- value rules
def test_season_is_bare_in_the_box_and_padded_in_the_archive_line() -> None:
    """Both come from the operator's samples, so neither is "tidied"."""
    assert post_values(title="X", season=1)["season"] == "1"
    assert archive_values(title="X", season=1)["season"] == "01"
    assert archive_values(title="X", season=12)["season"] == "12"


def test_only_episode_numbers_are_zero_padded() -> None:
    assert pad_number(1) == "01" and pad_number(11) == "11" and pad_number(None) == ""
    assert pad_number(1, width=0) == "1"
    # a non-numeric episode label (e.g. "OVA") is passed through, never dropped
    assert pad_number("OVA") == "OVA"


def test_total_episodes_is_never_guessed() -> None:
    assert total_episodes(12) == "12"
    assert total_episodes(None) == TOTAL_UNKNOWN
    # the season's *first* episode is not the total; 12 seen so far is not 12 long
    assert total_episodes(0) == TOTAL_UNKNOWN, "a zero length is no length"


def test_episode_range_covers_the_open_end() -> None:
    assert episode_range(1, 12) == "01 - 12"
    assert episode_range(1, None) == f"01 - {TOTAL_UNKNOWN}"
    assert episode_range(1, 1) == "01", "a one-episode season must not read '01 - 01'"
    assert episode_range(None, None) == TOTAL_UNKNOWN


def test_audio_label_describes_what_was_detected() -> None:
    assert audio_label("hindi") == "Hindi"
    assert audio_label("dual_audio") == "Hindi + English"
    assert audio_label("unknown") == "Unknown"
    assert audio_label(None) == "Unknown"
    # explicit languages win over the enum, and never list a duplicate
    assert audio_label("hindi", ["Hindi", "hindi", "English"]) == "Hindi + English"


def test_title_drops_the_separator_when_there_is_no_subtitle() -> None:
    assert title_with_subtitle("Dekin no mogura", "The earthbound mole") == "Dekin no mogura: The earthbound mole"
    assert title_with_subtitle("Dekin no mogura", None) == "Dekin no mogura"
    assert title_with_subtitle("Dekin no mogura", "   ") == "Dekin no mogura"
    assert title_with_subtitle(None, "Only a subtitle") == "Only a subtitle"


def test_a_missing_value_is_reported_rather_than_posted_literally() -> None:
    """The caller must refuse on a non-empty ``missing`` — but the name has to come
    back, or a typo in an operator-edited template is undiagnosable."""
    text, missing = render_caption(None, archive(quality=None), key="templates.archive_caption")
    assert "quality" in missing
    assert "{quality}" in text  # left intact by render_template on purpose
    text2, missing2 = render_caption(None, archive(), key="templates.archive_caption")
    assert missing2 == () and text2 == ARCHIVE_SAMPLE


def test_unknown_placeholders_in_a_template_are_visible() -> None:
    edited = "✦ {title_full} ✦\n❍ {eposide}: {episode}"
    text, missing = render_caption(edited, post(), key="templates.episode_post")
    assert "eposide" in missing and "episode" not in missing
    assert "{eposide}" in text and "❍ 01" not in text


def test_every_placeholder_a_template_uses_has_a_value_source() -> None:
    """Guards the reverse direction too: a template that asks for `{quality_list}`
    on an archive caption would silently print the braces forever."""
    known = set(post_values(title="x")) | set(archive_values(title="x")) | {"storage_link", "quality_list"}
    for key, template in APPROVED_TEMPLATES.items():
        for name in placeholder_keys(template):
            assert name in known, f"{key} asks for {{{name}}}, which no payload provides"


# --------------------------------------------------------------------- buttons
def test_one_link_gets_the_approved_label_without_a_quality() -> None:
    text, missing = button_lines([{"link": "https://t.me/anime_hindifilesbot/111", "quality": "480p"}])
    assert text == BUTTON_ONE
    assert missing == ()


def test_several_links_are_ordered_as_given_and_named() -> None:
    """Order is the manifest's business, not this function's — but it must not
    reshuffle what it is handed."""
    text, _ = button_lines(
        [
            {"link": "https://t.me/a/1", "quality": "480p"},
            {"link": "https://t.me/a/2", "quality": "1080p"},
        ]
    )
    lines = text.split("\n")
    assert len(lines) == 2
    assert lines[0].endswith("https://t.me/a/1") and "480p" in lines[0]
    assert lines[1].endswith("https://t.me/a/2") and "1080p" in lines[1]


def test_row_style_pair_joins_with_channel_helps_separator() -> None:
    text, _ = button_lines(
        [{"link": "https://t.me/a/1", "quality": "480p"}, {"link": "https://t.me/a/2", "quality": "720p"}],
        rows="pair",
    )
    assert " && " in text and "\n" not in text


def test_a_link_we_never_received_blocks_the_button_block() -> None:
    """An empty button that says "Watch/Download" and goes nowhere is worse than no
    post: the audience's first experience of the channel would be a dead link."""
    text, missing = button_lines([{"link": "", "quality": "480p"}])
    assert text == "" and missing == ("storage_link",)
    text2, missing2 = button_lines([])
    assert text2 == "" and missing2 == ("storage_link",)


def test_one_incomplete_multi_quality_set_still_posts_the_complete_buttons() -> None:
    """A missing quality label must not poison the whole row — that entry falls back
    to the unlabelled form and nothing is dropped."""
    text, missing = button_lines(
        [{"link": "https://t.me/a/1", "quality": "480p"}, {"link": "https://t.me/a/2", "quality": ""}]
    )
    assert missing == (), missing
    assert len(text.split("\n")) == 2 and "480p" in text


# --------------------------------------------------------------------- the migration
def _sql_literals(text: str) -> dict[str, tuple[str, str | None]]:
    """``key -> (new jsonb literal, old jsonb literal or None)`` from the migration."""
    found: dict[str, tuple[str, str | None]] = {}
    insert = re.compile(
        r"insert into app\.config \(key, value, description\) values\n"
        r"  \('([a-z_.]+)',\n   '((?:[^']|'')*)',\n   '(?:[^']|'')*'\)\n"
        r"(.*?);\s*(?:--[^\n]*)?\n",
        re.S,
    )
    for match in insert.finditer(text):
        key, literal, tail = match.group(1), match.group(2), match.group(3)
        old = re.search(r"where app\.config\.value = '((?:[^']|'')*)'::jsonb", tail)
        found[key] = (literal, old.group(1) if old else None)
    return found


def test_the_migration_carries_the_same_strings_the_renderer_uses() -> None:
    rows = _sql_literals(MIGRATION)
    assert set(rows) >= set(APPROVED_TEMPLATES), "a template is missing from the migration"
    for key, template in APPROVED_TEMPLATES.items():
        literal = rows[key][0]
        assert json.loads(literal.replace("''", "'")) == template, f"{key} differs between SQL and app.captions"


def test_the_migration_names_the_placeholder_it_replaces() -> None:
    """The WHERE clause is the whole safety story: a row the operator has already
    edited must be left alone, and "we only overwrite the exact thing we shipped" is
    the only rule that guarantees that without trusting a marker."""
    seeded = {
        key: literal
        for key, literal in re.findall(
            r"\('([a-z_]+\.[a-z_]+)',\s*\n?\s*('(?:[^']|'')*')",
            (ROOT / "supabase/migrations/0002_functions.sql").read_text(encoding="utf-8"),
        )
    }
    rows = _sql_literals(MIGRATION)

    def payload(literal: str) -> object:
        """A SQL string literal -> the jsonb text it holds, quotes and all handled."""
        text = (literal or "").strip()
        if text.startswith("'") and text.endswith("'"):
            text = text[1:-1]
        return json.loads(text.replace("''", "'"))

    for key, (_new, old) in rows.items():
        if key in seeded:
            assert payload(old) == payload(seeded[key]), (
                f"{key} was seeded by 0002, but the WHERE guard does not name that exact value"
            )
        else:
            assert old is None, f"{key} is new and must not pretend to replace anything"


def test_new_keys_never_clobber_an_operator_value() -> None:
    for key in ("caption.button_rows", "caption.total_episodes_unknown", "templates.episode_button_multi"):
        block = MIGRATION.split(f"-- {key}\n", 1)[1].split("\n\n", 1)[0]
        assert "on conflict (key) do nothing" in block, block


def test_the_subtitle_column_lives_on_series_not_season() -> None:
    """The alternate title is a property of the show. Putting it on the season would
    make it editable per season and re-derive per post, which is how a series ends
    up spelled three ways in one channel."""
    assert "alter table app.series add column if not exists subtitle text;" in MIGRATION
    assert "app.season" not in MIGRATION


@pytest.mark.parametrize("key", sorted(APPROVED_TEMPLATES))
def test_no_approved_template_exceeds_telegrams_caption_budget(key: str) -> None:
    """1024 characters for a caption, 4096 for a message. Rendering with generous
    values is how you find out the box survives a long title."""
    values = post(title="A very long anime title indeed with extra words", last_episode=1000)
    text, _ = render_caption(APPROVED_TEMPLATES[key], values, key=key)
    assert len(text) < 1024, f"{key} is {len(text)} chars"


def test_docs_show_the_same_text_the_code_renders() -> None:
    """The page the operator reads to approve wording must be generated truth.

    If a template changes and the doc does not, the review artifact lies, and the
    next complaint will be "but the docs said".
    """
    doc = (ROOT / "docs/captions-approved.md").read_text(encoding="utf-8")
    blocks = [block.strip() for block in re.findall(r"```text\n(.*?)\n```", doc, re.S)]
    assert ARCHIVE_SAMPLE in blocks, "the doc's archive example is not what the code renders"
    assert EPISODE_SAMPLE in blocks, "the doc's episode example is not what the code renders"
    assert BATCH_SAMPLE in blocks, "the doc's batch example is not what the code renders"
    assert any(BUTTON_ONE in block for block in blocks), "the approved button label is not shown in the doc"
