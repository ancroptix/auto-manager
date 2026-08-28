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


def test_every_test_the_architecture_doc_cites_actually_exists() -> None:
    """The enforcement table is the doc that claims a promise is *proved* somewhere.

    A citation to a test that was renamed three migrations ago is worse than no citation:
    it reads like the promise is covered, and the reader has no way to check without
    grepping. So the table is checked against the suite every run.
    """
    names = set()
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        names.update(re.findall(r"^\s*(?:async )?def (test_[a-z0-9_]+)", path.read_text(encoding="utf-8"), re.M))
    assert len(names) > 200, "the scan is not seeing the suite"
    cited = set(re.findall(r"`(test_[a-z0-9_]+)`", read("docs/architecture.md")))
    assert cited, "the table stopped citing tests at all"
    missing = sorted(cited - names)
    assert not missing, f"docs/architecture.md cites tests that do not exist: {missing}"


def test_the_config_row_count_in_the_docs_is_the_real_number() -> None:
    """`select count(*) from app.config` is the operator's second sanity check, so the
    number the docs promise has to be the number the migrations actually seed."""
    from conftest import config_row_count

    count = config_row_count()
    assert count >= 30, "the scan is not seeing the inserts"
    setup = read("docs/setup-supabase.md")
    claimed = re.search(r"\*\*(\d+)\*\* config keys", setup)
    assert claimed, "setup-supabase.md stopped stating how many config keys to expect"
    assert int(claimed.group(1)) == count, f"docs promise {claimed.group(1)}, migrations seed {count}"
    checklist = re.search(r"(\d+) config rows", read("docs/launch-checklist.md"))
    assert checklist and int(checklist.group(1)) == count, "the checklist disagrees with the setup doc"


def test_the_season_and_channel_policies_describe_the_code_that_exists() -> None:
    """The operator asked for these two mechanics in prose, so the prose lives in the
    repository. It is checked against the code it documents: a doc that names a setup step
    or a verdict the code no longer has is worse than no doc, because it gets trusted."""
    from app.channels import SETUP_STEPS
    from app.seasons import Verdict

    doc = read("docs/seasons-and-channels.md")
    assert doc.count("\n## ") >= 2, "the document lost one of its two halves"
    for step in SETUP_STEPS:
        assert f"`{step.name}`" in doc, f"{step.name} is a setup step nobody documented"
    for verdict in Verdict:
        assert f"`{verdict.value}`" in doc, f"{verdict.value} is a verdict nobody documented"
    for claim in ("/declare", "observed_first", "can_invite_users"):
        assert claim in doc, f"the policy document stopped naming {claim}"
    assert "seasons-and-channels.md" in read("README.md"), "the answer is unreachable from the front page"


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


def test_the_checklist_env_block_is_the_real_settings() -> None:
    """A setup guide that names a variable the app does not read wastes an operator's evening.

    The checklist is the one document the operator follows top to bottom without
    understanding it, so it is checked like a config file: every key in its env block
    must exist in :class:`app.config.Settings`, must be a key ``render.yaml`` sets, and
    the keys that gate live mode must be present at all.
    """
    import sys

    sys.path.insert(0, str(ROOT))
    from app.config import Settings

    doc = read("docs/launch-checklist.md")
    block = re.search(r"```env\n(.*?)\n```", doc, re.S)
    assert block, "the checklist lost its ```env block"
    lines = [ln.strip() for ln in block.group(1).splitlines() if ln.strip()]
    # `KEY: value`, not `KEY=value`, because Render asks for the two separately — and
    # because scripts/check_secrets.py deliberately rejects `DATABASE_URL=<literal>`
    # anywhere in the tree. A setup guide must not need a scanner exemption to exist.
    row = re.compile(r"^([A-Z][A-Z0-9_]{2,}):\s")
    keys = [m.group(1) for ln in lines if (m := row.match(ln))]
    assert keys, "no `KEY: value` rows in the env block"
    unparsed = [ln for ln in lines if not row.match(ln)]
    assert not unparsed, f"rows without a recognisable key: {unparsed}"

    # The guide speaks environment variables, the model speaks field names, and they
    # are not always the same string: `APP_MODE` is the *alias* on field `mode`. So the
    # legal set is every field name uppercased plus every alias, or a correctly written
    # guide would fail for naming the variable Render actually sets.
    settings = {name.upper() for name in Settings.model_fields} | {
        str(info.alias).upper() for info in Settings.model_fields.values() if info.alias
    }
    assert set(keys) <= settings, f"checklist invents settings: {sorted(set(keys) - settings)}"
    blueprint = set(blueprint_env_keys())
    assert set(keys) <= blueprint, f"checklist keys the blueprint never sets: {sorted(set(keys) - blueprint)}"

    live_critical = {
        "APP_MODE",
        "DATABASE_URL",
        "DB_SSL",
        "CONTROL_TOKEN",
        "TELEGRAM_API_ID",
        "TELEGRAM_API_HASH",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_OWNER_USER_IDS",
        "TELEGRAM_MAIN_ADMIN_USER_ID",
        "TELEGRAM_SESSION_SOURCE",
        "BOT_ALLOW_LOGIN",
        "PROBE_ON_BOOT",
    }
    missing = live_critical - set(keys)
    assert not missing, f"checklist omits {sorted(missing)}"
    assert len(keys) == len(set(keys)), "a key is listed twice with different values"


def test_the_checklist_only_names_files_that_exist() -> None:
    """Every repository path in the guide must resolve, or the operator hunts for it.

    This is how a renamed migration or a moved script turns into "the docs are wrong,
    so I will guess" — and guessing is how a database gets half a schema.
    """
    doc = read("docs/launch-checklist.md")
    paths = set(
        re.findall(
            r"`((?:docs|ops|app|tests|scripts|supabase)/[A-Za-z0-9_./-]+\.(?:md|py|sql|ya?ml|txt))`"
            r"|`(render\.yaml)`",
            doc,
        )
    )
    flat = {a or b for a, b in paths}
    assert flat, "the checklist no longer names any files, which is suspicious by itself"
    for rel in sorted(flat):
        assert (ROOT / rel).exists(), f"checklist names {rel}, which is not in the repo"


def test_architecture_lists_the_new_modules() -> None:
    doc = read("docs/architecture.md")
    for name in (
        "botapi.py",
        "controlbot.py",
        "sessions.py",
        "mtproto_login.py",
        "0003_control_bot.sql",
        "seasons.py",
        "0005_seasons_and_profile.sql",
    ):
        assert name in doc, f"{name} exists but is not in the layout map"
    assert "getUpdates" in doc and "webhook" in doc.lower(), "the polling choice needs its reason on record"
