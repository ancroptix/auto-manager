#!/usr/bin/env python3
"""Disposable local PostgreSQL for developing and testing the migrations.

    python scripts/devdb.py            # start, apply migrations, keep running
    python scripts/devdb.py --reset    # throw away the data directory first

Prints a DATABASE_URL you can paste into a local .env. Nothing here talks to
Supabase: it exists so a migration can be proven to run before it is applied to
a real project. Uses the bundled pgserver package (dev dependency only).
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PGDATA = ROOT / ".pgdata"
ROLES = (("anon", "nologin"), ("authenticated", "nologin"), ("service_role", "nologin bypassrls"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="delete the local cluster first")
    parser.add_argument(
        "--print-url",
        action="store_true",
        help="print an `export DATABASE_URL=...` line and exit, for `eval $(...)`",
    )
    args = parser.parse_args()

    try:
        import pgserver
    except ImportError:
        print("pip install -r requirements-dev.txt first (needs pgserver)", file=sys.stderr)
        return 2

    if args.reset and PGDATA.exists():
        import shutil

        shutil.rmtree(PGDATA, ignore_errors=True)
        print(f"removed {PGDATA}")

    server = pgserver.get_server(PGDATA, cleanup_mode=None)
    import psycopg

    with psycopg.connect(server.get_uri(), autocommit=True) as conn:
        for name, attrs in ROLES:
            exists = conn.execute("select 1 from pg_roles where rolname = %s", (name,)).fetchone()
            if not exists:
                conn.execute(f"create role {name} {attrs}")
        for path in sorted((ROOT / "supabase" / "migrations").glob("*.sql")):
            conn.execute(path.read_text(encoding="utf-8"))
            print(f"applied {path.name}", flush=True)
        tables = conn.execute(
            "select count(*) from information_schema.tables where table_schema='app'"
        ).fetchone()[0]
        print(f"schema app ready: {tables} relations", flush=True)

    uri = server.get_uri()
    if args.print_url:
        # Quoted for eval; local socket only, no password, nothing secret.
        print(f'export DATABASE_URL="{uri}"')
        print(f'export DB_SSL=disable', file=sys.stderr)
        return 0
    print("\nDATABASE_URL=" + uri, flush=True)
    print("Ctrl-C to stop.", flush=True)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
