"""Generate supabase/migrations/0004_approved_captions.sql from app/captions.py.

Run this after editing app/captions.APPROVED_TEMPLATES, then re-run
ops/build_apply_all.py. ``--check`` (used by CI) fails if the committed migration
no longer matches the code, which is the same drift guard the bundle already has.

Two sources of truth are reconciled here instead of being hand-copied: the approved
strings come from :data:`app.captions.APPROVED_TEMPLATES`, and the *previous* value
of each key is read out of 0002_functions.sql so the generated UPDATE can name
exactly what it is replacing. That is what makes the migration safe to re-apply: a
row is overwritten only while it still holds the placeholder we shipped, and a
caption the operator has since tuned in the dashboard is left completely alone.

The generated file is committed as ordinary SQL — nothing regenerates it at runtime.
``tests/test_caption_templates.py`` re-derives it and fails if the two have drifted.
"""

import argparse
import io
import json
import pathlib
import re
import sys

ROOT = str(pathlib.Path(__file__).resolve().parents[1]) + "/"
sys.path.insert(0, ROOT)

OUT = "supabase/migrations/0004_approved_captions.sql"

from app.captions import APPROVED_TEMPLATES, BUTTON_ROWS, TOTAL_UNKNOWN  # noqa: E402,F401


def sql_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def seeded_values() -> dict[str, str]:
    """``key -> jsonb literal`` for every template row 0002 seeds."""
    sql = io.open(ROOT + "supabase/migrations/0002_functions.sql", encoding="utf-8").read()
    found: dict[str, str] = {}
    for match in re.finditer(r"\('([a-z_]+\.[a-z_]+)',\s*\n?\s*('(?:[^']|'')*')", sql):
        found.setdefault(match.group(1), match.group(2))
    return found


OLD = seeded_values()

DESCRIPTIONS = {
    "templates.archive_caption": (
        "Approved 2026-08-28 from the operator's sample: title line, light box with "
        "Quality/Episode/Audio, then the two handles. Editable here."
    ),
    "templates.episode_post": (
        "Approved 2026-08-28 from the operator's sample: heavy box, season bare, "
        "episode zero-padded, total episodes, footer handles. Editable here."
    ),
    "templates.season_post": (
        "Approved 2026-08-28: the same box as the episode post with "
        "'{episode_range}' instead of '{episode}'. Editable here."
    ),
    "templates.episode_button": (
        "Channel Help button syntax is 'text - url'. One label, exactly as approved, "
        "used when a post has a single link."
    ),
    "templates.episode_button_multi": (
        "Used when a post offers more than one quality: the quality is named, because "
        "four identical buttons on a post that has 480p through 2160p stopped "
        "describing the file."
    ),
    "templates.season_button": (
        "The batch post's button: the universal link, same label as an episode link."
    ),
    "caption.button_rows": (
        "one_per_line | pair. 'pair' joins buttons with && so two links share a row; "
        "one_per_line gives every quality its own row."
    ),
    "caption.total_episodes_unknown": (
        "Printed instead of a number when the season's length was never stated. Never "
        "inferred from the highest episode seen so far: that would promise a "
        "completion nobody observed."
    ),
}

HEADER = """-- ============================================================================
-- 0004_approved_captions.sql — the caption formats the operator dictated.
--
-- app.config used to carry placeholder captions marked "Temporary default.". This
-- migration replaces them with the approved text, character for character, with
-- three deliberate choices recorded here rather than hidden in a diff:
--
--   1. The box lines are single-newline separated. The samples arrived double
--      spaced, and a `╭ ┣ ┣ ╰` frame only reads as a frame when the strokes are
--      adjacent.
--   2. `{title_full}` is one stored value ("Title: Subtitle") used by both the
--      archive caption and the destination post, so the private archive can never
--      disagree with the public channel about how a series is spelled. The two
--      samples differed (": The earthbound mole" against ": the earthbound mole");
--      the capitalisation stored on the series is what gets published.
--   3. `{season}` is bare in the destination box and zero-padded in the archive
--      line, because that is what each sample showed. Padding one and tidying the
--      other would change published text on a guess.
--
-- `app.series.subtitle` (added below) holds the alternate title. The source scanner
-- fills it when that handler lands, and nothing may depend on it being present: an
-- absent subtitle drops the colon and the second half instead of inventing one.
--
-- Each statement replaces its row only while that row still holds the exact
-- placeholder 0002 shipped — the previous value is named in the WHERE clause — so
-- re-applying ops/apply-all.sql can never overwrite a caption you have edited.
--
-- The strings are asserted equal to app.captions.APPROVED_TEMPLATES by
-- tests/test_caption_templates.py; regenerate this file with
-- `python ops/build_caption_migration.py && python ops/build_apply_all.py` after
-- editing a template, and CI's --check fails if either step was skipped.
-- ============================================================================

alter table app.series add column if not exists subtitle text;

comment on column app.series.subtitle is
  'Alternate/English title as it appears in the source, e.g. "The earthbound mole". '
  'Never guessed, never translated: the caption prints it or omits it.';
"""


EXTRA_VALUES = {
    "caption.button_rows": BUTTON_ROWS,
    "caption.total_episodes_unknown": TOTAL_UNKNOWN,
}


def approved(key: str) -> str:
    return APPROVED_TEMPLATES[key] if key in APPROVED_TEMPLATES else EXTRA_VALUES[key]


def statement(key: str) -> str:
    new_literal = sql_quote(json.dumps(approved(key), ensure_ascii=False))
    description = sql_quote(DESCRIPTIONS[key])
    body = (
        f"insert into app.config (key, value, description) values\n"
        f"  ({sql_quote(key)},\n   {new_literal},\n   {description})\n"
    )
    old = OLD.get(key)
    if old is None:
        return body + "on conflict (key) do nothing;  -- new key: a value you set yourself always wins\n"
    return (
        body
        + "on conflict (key) do update set\n"
        + "  value = excluded.value,\n"
        + "  description = excluded.description,\n"
        + "  updated_at = now()\n"
        + f" where app.config.value = {old}::jsonb;  -- only replaces the placeholder 0002 shipped\n"
    )


KEY_ORDER = [
    "templates.archive_caption",
    "templates.episode_post",
    "templates.season_post",
    "templates.episode_button",
    "templates.episode_button_multi",
    "templates.season_button",
    "caption.button_rows",
    "caption.total_episodes_unknown",
]

parts = [HEADER]
for key in KEY_ORDER:
    parts.append("-- " + key + "\n" + statement(key))

text = "\n".join(parts)

if "--check" in sys.argv:
    committed = io.open(ROOT + OUT, encoding="utf-8").read()
    if committed != text:
        print(
            f"{OUT} is stale. Run: python ops/build_caption_migration.py && python ops/build_apply_all.py",
            file=sys.stderr,
        )
        raise SystemExit(1)
    print(f"{OUT} matches app/captions.py")
    raise SystemExit(0)

io.open(ROOT + OUT, "w", encoding="utf-8").write(text)
print(
    f"wrote {OUT} ({len(text)} bytes); keys replacing a 0002 placeholder: "
    + ", ".join(k for k in KEY_ORDER if k in OLD)
)
