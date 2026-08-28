"""The docs are an operator interface, so they are tested like one.

A setup guide that names a variable the app does not read, or promises a command
the bot does not implement, is worse than no guide: the operator follows it, it
does not work, and the failure is attributed to the app. These checks tie the
documents to the files they describe.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def blueprint_env_keys() -> list[str]:
    doc = yaml.safe_load(read("render.yaml"))
    service = next(s for s in doc["services"] if s["name"] == "auto-manager")
    return [entry["key"] for entry in service["envVars"]]


def test_every_blueprint_variable_exists_in_the_env_template() -> None:
    """`.env.example` is where a value's *meaning* is documented.

    A variable that only exists in render.yaml is one the operator cannot reason
    about locally, and a variable in the template that the app ignores is worse:
    it looks like a knob and does nothing.
    """
    template = read(".env.example")
    missing = [key for key in blueprint_env_keys() if f"{key}=" not in template and f"{key} =" not in template]
    assert not missing, f"document in .env.example or delete from render.yaml: {missing}"


def test_the_setup_guide_documents_the_credentials_that_are_actually_required() -> None:
    doc = read("docs/setup-render.md")
    for key in (
        "DATABASE_URL",
        "DB_SSL",
        "CONTROL_TOKEN",
        "APP_MODE",
        "TELEGRAM_API_ID",
        "TELEGRAM_API_HASH",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_OWNER_USER_IDS",
        "TELEGRAM_MAIN_ADMIN_USER_ID",
        "TELEGRAM_SESSION_SOURCE",
        "BOT_ALLOW_LOGIN",
        "PROBE_ON_BOOT",
    ):
        assert f"`{key}`" in doc, f"setup-render.md must tell the operator about {key}"


def test_the_manual_web_service_form_matches_the_blueprint() -> None:
    """The operator is on the manual path, so the form values in the doc are the
    deployment. If they drift from render.yaml the two paths behave differently."""
    doc = read("docs/setup-render.md")
    blueprint = yaml.safe_load(read("render.yaml"))
    service = next(s for s in blueprint["services"] if s["name"] == "auto-manager")
    assert service["buildCommand"] in doc, "build command differs from render.yaml"
    assert service["startCommand"] in doc, "start command differs from render.yaml"
    assert service["healthCheckPath"] in doc
    assert service["plan"] == "free" and "Free" in doc
    # The doc names the blueprint's region, because a different region means a
    # different database latency and the operator should not discover it by surprise.
    region_row = next(line for line in doc.splitlines() if line.startswith("   | Region"))
    assert service["region"] in region_row.lower(), f"doc region row does not name {service['region']}"


def test_session_string_is_documented_as_optional() -> None:
    """The point of the control bot is that no session string is pasted anywhere.

    Checked against every line that names the variable, because the guide mentions it
    twice — once in the "what you need for live mode" table and once where it says
    the variable is deliberately absent. Both have to read as optional, or the
    operator pastes one and wonders why an older session won.
    """
    doc = read("docs/setup-render.md")
    lines = [line for line in doc.splitlines() if "`TELEGRAM_SESSION_STRING`" in line]
    assert lines, "the guide stopped mentioning the variable at all"
    for line in lines:
        lowered = line.lower()
        assert any(word in lowered for word in ("optional", "not", "absent", "win")), line
    assert "scripts/login.py" in doc and "fallback" in read("scripts/login.py")


def test_the_expected_schema_size_in_the_setup_doc_is_the_real_number() -> None:
    """`Expect 27` is the operator's only check that the migrations ran. A stale
    number here is the difference between 'the schema is fine' and an hour of
    wondering."""
    tables = views = 0
    for path in sorted((ROOT / "supabase" / "migrations").glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        tables += len(re.findall(r"^create table (?:if not exists )?app\.", sql, re.M))
        views += len(re.findall(r"^create (?:or replace )?view (?:if not exists )?app\.", sql, re.M))
    assert tables >= 20 and views >= 3, "the scan is not seeing the migrations"
    doc = read("docs/setup-supabase.md")
    claimed = int(re.search(r"Expect \*\*(\d+)\*\* relations", doc).group(1))
    assert claimed == tables + views, f"docs promise {claimed}, migrations create {tables + views}"
    # the per-migration breakdown in the same paragraph must add up too
    promised = re.search(r"(\d+) tables \((\d+) from 0001, plus", doc)
    assert promised and int(promised.group(1)) == tables and int(promised.group(2)) == tables - 1, promised


def test_the_bundle_is_offered_as_the_recommended_path() -> None:
    doc = read("docs/setup-supabase.md")
    assert "ops/apply-all.sql" in doc and "recommended" in doc
    for name in sorted(p.name for p in (ROOT / "supabase" / "migrations").glob("*.sql")):
        assert name in doc, f"{name} must be named in the apply instructions"


def test_the_control_bot_documents_every_command_it_advertises() -> None:
    from app.controlbot import HELP

    advertised = set(re.findall(r"^/(\w+)", HELP, re.M))
    assert advertised, "the help text lost its command list"
    doc = read("docs/control-bot.md")
    undocumented = sorted(name for name in advertised if f"`/{name}" not in doc and f"/{name} " not in doc)
    assert not undocumented, f"the bot answers commands the operator was never told about: {undocumented}"


def test_the_kill_switch_stays_http_only_in_every_doc() -> None:
    """`/shutdown` is deliberately not on Telegram: a kill switch reachable from a
    chat window is one lost phone away from being pressed by someone else."""
    from app.controlbot import HELP

    assert "/shutdown" not in HELP
    assert "HTTP-only" in read("docs/control-bot.md")
    assert "/control/shutdown" in read("docs/setup-render.md")


def test_readme_points_at_the_control_bot() -> None:
    readme = read("README.md")
    assert "control-bot.md" in readme and "TELEGRAM_BOT_TOKEN" not in readme.split("```")[1::2][-1], (
        "README must not start showing token-shaped placeholders"
    )
    for token in ("@anime_hindifilesbot", "@chelpbot", "/login"):
        assert token in readme, f"README no longer mentions {token}"


@pytest.mark.parametrize("path", ["docs/control-bot.md", "docs/architecture.md", "docs/setup-render.md"])
def test_no_doc_promise_a_credential_in_chat(path: str) -> None:
    """The standing rule: the agent never receives a session string, service key or
    Render token. A doc that tells the operator to paste one would undo it."""
    text = read(path).lower()
    for phrase in ("paste the session string here", "send me the token", "paste your 2fa password into this chat"):
        assert phrase not in text, f"{path} says: {phrase!r}"


def test_the_approved_captions_are_reachable_from_the_readme_and_spec() -> None:
    """A page nobody links to is a page nobody re-reads.

    The captions are the only part of this system the audience ever sees, so both the
    front page and the requirements document point at the rendered examples.
    """
    for rel in ("README.md", "docs/requirements-draft.md"):
        assert "captions-approved.md" in read(rel), f"{rel} lost the pointer to the approved captions"
    spec = read("docs/requirements-draft.md")
    assert "approved by the operator" in spec.lower()
    assert "Temporary default" not in spec, "the spec still advertises placeholder captions"


def test_architecture_lists_the_new_modules() -> None:
    doc = read("docs/architecture.md")
    for name in ("botapi.py", "controlbot.py", "sessions.py", "mtproto_login.py", "0003_control_bot.sql"):
        assert name in doc, f"{name} exists but is not in the layout map"
    assert "getUpdates" in doc and "webhook" in doc.lower(), "the polling choice needs its reason on record"
