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


@pytest.fixture
def migrations() -> dict[str, str]:
    return {
        p.name: p.read_text(encoding="utf-8")
        for p in sorted((ROOT / "supabase" / "migrations").glob("*.sql"))
    }
