"""Deployment-config invariants.

These read like pedantic string checks, but each one corresponds to a failure
that is invisible until production breaks: a health path that does not exist, a
hard-coded port, a secret pasted into a committed file, or a Render Postgres
that silently deletes the data after 30 days.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def blueprint() -> dict:
    return yaml.safe_load((ROOT / "render.yaml").read_text())


@pytest.fixture(scope="module")
def service(blueprint) -> dict:
    services = blueprint["services"]
    assert len(services) == 1, "one process serves both the API and the queue loop"
    return services[0]


def test_uses_the_free_plan(service) -> None:
    assert service["plan"] == "free"
    assert service["type"] == "web", "free tier exists only for web services; workers are paid-only"


def test_health_check_path_is_served_by_the_app(service) -> None:
    """Render holds a deploy until the health path answers; it must exist."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    # Checked through the OpenAPI surface rather than app.routes: FastAPI keeps
    # an included router lazy, so route introspection is not a reliable proxy.
    paths = create_app().openapi()["paths"]
    declared = service["healthCheckPath"]
    assert declared in paths, f"{declared} is not a real endpoint"

    with TestClient(create_app()) as client:
        response = client.get(declared)
        assert response.status_code == 200, "health endpoint must succeed with no database"


def test_start_command_lets_render_choose_the_port(service) -> None:
    start = service["startCommand"]
    assert "${PORT}" in start, "hard-coding the port makes Render unable to route traffic"
    assert "--host 0.0.0.0" in start
    assert "--workers 1" in start, "more workers means multiple queue loops competing"


def test_no_render_database_is_declared(blueprint) -> None:
    assert "databases" not in blueprint, "Render free Postgres expires after 30 days; persistence is Supabase"


def test_secrets_are_not_hardcoded(service) -> None:
    for env in service["envVars"]:
        key = env["key"]
        if "sync" in env:
            assert env["sync"] is False, f"{key} marked sync must never carry a committed value"
            continue
        value = str(env.get("value", ""))
        assert value, f"{key} has neither a value nor sync: false"
        assert not re.search(r"(postgresql|postgres)://", value), "connection string committed"
        assert len(value) < 60, f"{key} value looks like a credential, not a setting"


def test_required_secret_slots_exist(service) -> None:
    keys = {env["key"] for env in service["envVars"]}
    for required in {
        "DATABASE_URL",
        "CONTROL_TOKEN",
        "TELEGRAM_API_ID",
        "TELEGRAM_API_HASH",
        "TELEGRAM_SESSION_STRING",
        "TELEGRAM_OWNER_USER_IDS",
        # The operator's whole interface is this bot: /status, /pause, /probe and
        # /login. The last one is why a non-coder can connect an account at all.
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_SESSION_SOURCE",
        "BOT_ALLOW_LOGIN",
    }:
        assert required in keys


def test_shadow_mode_is_the_committed_default(service) -> None:
    mode = next(env for env in service["envVars"] if env["key"] == "APP_MODE")
    assert mode["value"] == "shadow", "the first deploy must not be able to message anyone"


def test_build_installs_only_runtime_requirements(service) -> None:
    assert "requirements-dev.txt" not in service["buildCommand"]
    assert "pip install -r requirements.txt" in service["buildCommand"]


def test_no_dockerfile_present() -> None:
    """Render prefers a Dockerfile over runtime: python if one exists.

    Keeping the Python runtime explicit until ffmpeg is actually needed avoids
    the "it built but ran the wrong image" class of failure. When the thumbnail
    pipeline lands, add the Dockerfile and switch the blueprint in the same
    commit.
    """
    assert not (ROOT / "Dockerfile").exists()


# Fields the operator must know about: a missing entry here means an unread
# setting, and this project is configured entirely through the environment.
MUST_DOCUMENT = [
    "APP_MODE", "DATABASE_URL", "DB_SSL", "CONTROL_TOKEN", "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH", "TELEGRAM_SESSION_STRING", "TELEGRAM_OWNER_USER_IDS",
    "TELEGRAM_MAIN_ADMIN_USER_ID", "WORKER_ENABLED", "CLAIM_LEASE_SECONDS",
    "CAMPAIGN_RATE_PER_HOUR", "GRACEFUL_SHUTDOWN_SECONDS", "RECONCILE_ON_BOOT",
]


@pytest.mark.parametrize("key", MUST_DOCUMENT)
def test_env_example_documents_operator_settings(key) -> None:
    assert key in (ROOT / ".env.example").read_text()


SECRET_KEYS = {
    "DATABASE_URL", "CONTROL_TOKEN", "TELEGRAM_API_HASH", "TELEGRAM_SESSION_STRING",
}


def test_env_example_holds_no_live_secrets() -> None:
    """Every credential line in the template must be empty.

    This is the file people copy, so a filled-in example is how a real session
    string ends up committed.
    """
    for line in (ROOT / ".env.example").read_text().splitlines():
        if "=" not in line or line.strip().startswith("#"):
            continue
        key, _, value = line.partition("=")
        if key.strip().upper() in SECRET_KEYS:
            assert value.strip() == "", f"{key.strip()} must be left blank in the template"


def test_gitignore_protects_session_files() -> None:
    text = (ROOT / ".gitignore").read_text()
    assert "*.session" in text
    assert ".env" in text.split("\n")[0] or "\n.env\n" in f"\n{text}\n"
    assert "!.env.example" in text


def test_pool_kwargs_match_asyncpg_signature(make_settings) -> None:
    """asyncpg takes no **kwargs, so an unknown key = a service that never connects."""
    import inspect

    import asyncpg

    from app.db import Database

    settings = make_settings(database_url="postgresql://u:p@pooler.supabase.com:6543/postgres")
    kwargs = Database(settings).pool_kwargs()
    accepted = set(inspect.signature(asyncpg.create_pool).parameters) | set(
        inspect.signature(asyncpg.connect).parameters
    )
    unknown = set(kwargs) - accepted
    assert not unknown, f"asyncpg rejects these keys: {sorted(unknown)}"


def test_application_name_is_a_server_setting_not_a_kwarg(make_settings) -> None:
    from app.db import Database

    kwargs = Database(make_settings()).pool_kwargs()
    assert "application_name" not in kwargs
    assert kwargs["server_settings"]["application_name"] == "auto-manager"
    assert kwargs["server_settings"]["search_path"] == "app, public"


def test_transaction_pooler_gets_statement_cache_disabled(make_settings) -> None:
    from app.db import Database

    pooled = Database(make_settings(database_url="postgresql://u:p@h:6543/db")).pool_kwargs()
    direct = Database(make_settings(database_url="postgresql://u:p@h:5432/db")).pool_kwargs()
    assert pooled["statement_cache_size"] == 0
    assert "statement_cache_size" not in direct


def test_apply_all_installer_is_in_sync_with_migrations() -> None:
    """ops/apply-all.sql is what an operator pastes by hand, so a stale copy
    would silently install the previous version of the schema."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "ops/build_apply_all.py", "--check"],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_generated_sql_artifacts_are_all_in_sync() -> None:
    """Every generated SQL file has a ``--check`` and CI runs all of them.

    Two artefacts today: ops/apply-all.sql against supabase/migrations/, and
    0004_approved_captions.sql against app/captions.APPROVED_TEMPLATES. A stale
    caption migration would mean the text you approved is not the text that gets
    published, and a stale bundle would mean the installer skips 0004 entirely.
    """
    import subprocess
    import sys

    for script in ("ops/build_caption_migration.py", "ops/build_apply_all.py"):
        result = subprocess.run(
            [sys.executable, script, "--check"], cwd=ROOT, capture_output=True, text=True
        )
        assert result.returncode == 0, f"{script} --check failed: {result.stdout}{result.stderr}"


def test_migration_bundle_ships_every_migration_in_order() -> None:
    """ops/apply-all.sql is what first boot executes, so it must contain every
    migration, functions after tables, and it must parse.

    The name used to say "both files"; the scan is directory-driven now, so a fifth
    migration is covered the moment it exists instead of being quietly absent.
    """
    import pglast

    from app.db import MIGRATION_BUNDLE, REPO_ROOT

    text = MIGRATION_BUNDLE.read_text()
    migrations = sorted(path.name for path in (REPO_ROOT / "supabase" / "migrations").glob("*.sql"))
    assert len(migrations) >= 3, "the scan should see every migration, not just the early ones"
    positions = []
    for name in migrations:
        stem = name[: -len(".sql")]
        assert stem in text, f"{name} is missing from the bundle — run ops/build_apply_all.py"
        positions.append(text.index(stem))
    assert positions == sorted(positions), "the bundle concatenates migrations out of order"
    # The header names the files it contains, so a reader can tell a stale bundle
    # from a complete one without diffing.
    for name in migrations:
        assert name in text.split("ONE-FILE INSTALLER")[1][:600], f"{name} not advertised in the header"
    assert text.index("create schema if not exists app") < text.index("create or replace function app.claim_next_job")
    pglast.parse_sql(text)
