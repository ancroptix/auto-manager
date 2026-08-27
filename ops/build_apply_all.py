#!/usr/bin/env python3
"""Regenerate ops/apply-all.sql from supabase/migrations/*.sql.

The concatenated installer exists for one reason: an operator who does not
want to juggle two paste-and-run cycles still applies the migrations in the
correct order. Run this after editing any migration.
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def build() -> str:
    header = """-- ============================================================================
-- Auto Manager — ONE-FILE INSTALLER
-- Paste this whole file into Supabase -> SQL Editor -> Run. It is exactly
-- 0001_init.sql followed by 0002_functions.sql, concatenated by
-- ops/build_apply_all.py so the two never drift apart.
--
-- If you prefer the normal Supabase flow instead, apply the two files in
-- supabase/migrations/ in filename order (or run `supabase db push`).
-- Re-running this file is safe: every object is created IF NOT EXISTS or inside
-- an existence check.
-- ============================================================================

"""
    files = sorted((ROOT / "supabase" / "migrations").glob("*.sql"))
    if not files:
        raise SystemExit("no migrations found")
    return header + "\n\n".join(f.read_text().strip() for f in files) + "\n"


def main() -> int:
    if "--check" in sys.argv:
        current = (ROOT / "ops" / "apply-all.sql").read_text()
        if current != build():
            print(
                "ops/apply-all.sql is stale. Run: python ops/build_apply_all.py",
                file=sys.stderr,
            )
            return 1
        print("ops/apply-all.sql matches the migrations")
        return 0
    (ROOT / "ops" / "apply-all.sql").write_text(build())
    print("regenerated ops/apply-all.sql")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
