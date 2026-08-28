#!/usr/bin/env python3
"""Regenerate ops/apply-all.sql from supabase/migrations/*.sql.

The concatenated installer exists for one reason: an operator who does not want
to juggle several paste-and-run cycles still applies the migrations in the
correct order. Run this after editing any migration — the header names the files
it contains, so it cannot rot into a lie about "the two files".
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]


def header_for(files: list[pathlib.Path]) -> str:
    listed = "".join(f"--   {f.name}\n" for f in files)
    plural = "file" if len(files) == 1 else f"{len(files)} files"
    return (
        "-- ============================================================================\n"
        "-- Auto Manager — ONE-FILE INSTALLER\n"
        "-- Paste this whole file into Supabase -> SQL Editor -> Run. It is exactly the\n"
        "-- migrations below, in filename order, concatenated by ops/build_apply_all.py\n"
        "-- so the bundle and supabase/migrations/ can never drift apart:\n"
        f"{listed}--\n"
        "--\n"
        f"-- If you prefer the normal Supabase flow instead, apply the {plural} in\n"
        "-- supabase/migrations/ in filename order (or run `supabase db push`).\n"
        "-- Re-running this file is safe: every object is created IF NOT EXISTS or inside\n"
        "-- an existence check.\n"
        "-- ============================================================================\n\n"
    )


def build() -> str:
    files = sorted((ROOT / "supabase" / "migrations").glob("*.sql"))
    if not files:
        raise SystemExit("no migrations found")
    return header_for(files) + "\n\n".join(f.read_text().strip() for f in files) + "\n"


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
    (ROOT / "ops/apply-all.sql").write_text(build())
    print("regenerated ops/apply-all.sql")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
