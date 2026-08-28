from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402

# Tests must not be influenced by a developer's real .env (or by ambient
# DATABASE_URL pointing at a live project).
AMBIENT = [
    "APP_MODE",
    "DATABASE_URL",
    "SUPABASE_DB_URL",
    "CONTROL_TOKEN",
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "TELEGRAM_SESSION_STRING",
    "TELEGRAM_OWNER_USER_IDS",
    "TELEGRAM_MAIN_ADMIN_USER_ID",
    "WORKER_ENABLED",
    "PORT",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in AMBIENT:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def make_settings():
    def _make(**overrides):
        params = {"_env_file": None, "worker_enabled": False}
        params.update(overrides)
        return Settings(**params)

    return _make


@pytest.fixture
def settings(make_settings):
    return make_settings()


def config_row_count(root: Path = ROOT) -> int:
    """How many distinct `app.config` keys the migrations seed.

    Both the setup docs and the live-database test quote this number, so it is parsed out of
    the SQL rather than typed: a hand-counted total went stale the same afternoon it was
    written, and the only symptom is an operator staring at a sanity check that lies.

    Parsing, not regex, because the answer is *distinct keys* rather than insert rows — a
    key seeded in 0002 and re-stated with better casing in 0004 occupies one row in the
    database and two in the files, and a value full of semicolons defeats any pattern that
    tries to find statement ends textually.
    """
    from pglast import parse_sql
    from pglast.ast import A_Const, InsertStmt, SelectStmt

    keys: set[str] = set()
    for path in sorted((root / "supabase" / "migrations").glob("*.sql")):
        for raw in parse_sql(path.read_text(encoding="utf-8")):
            stmt = raw.stmt
            if not isinstance(stmt, InsertStmt) or not stmt.relation:
                continue
            if stmt.relation.relname != "config":
                continue
            select = stmt.selectStmt
            if not isinstance(select, SelectStmt) or not select.valuesLists:
                continue
            for row in select.valuesLists:
                if row and isinstance(row[0], A_Const):
                    value = getattr(row[0].val, "sval", None)  # pglast wraps strings in a node
                    if isinstance(value, str):
                        keys.add(value)
    return len(keys)


@pytest.fixture
def migrations() -> dict[str, str]:
    return {
        p.name: p.read_text(encoding="utf-8")
        for p in sorted((ROOT / "supabase" / "migrations").glob("*.sql"))
    }
