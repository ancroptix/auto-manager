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


def test_the_menu_page_names_every_screen_the_console_has() -> None:
    """The screen map in `docs/control-bot.md` is `console.NAV`, in both directions.

    A table a developer can forget to update is worse than no table: the operator reads `bots` where the
    module now says `settings`, taps the row that is not there, and finds out that the documentation is the
    part that was lying. So the row keys are compared to the module's own tuple, and a row for a screen that
    does not exist is as much a failure as a missing one.
    """
    import sys

    sys.path.insert(0, str(ROOT))
    from app import console, normalize

    doc = read("docs/control-bot.md")
    assert "### The menu" in doc, "the page lost the section"
    section = doc.split("### The menu", 1)[1].split("\n## ", 1)[0]
    named = set(re.findall(r"^\| `([a-z]+)` \|", section, re.M))
    assert named == set(console.NAV), (
        f"the page does not describe: {sorted(set(console.NAV) - named)}; "
        f"the page invents: {sorted(named - set(console.NAV))}"
    )
    for kind in sorted(normalize.DECLARED_AUDIO):
        assert f"`{kind}`" in section, f"the audio picks are documented without {kind}"
    for promise in ("`↻ Refresh`", "✖ Stop here", "ran: ", "drops the button"):
        assert promise in section, f"the page no longer states the rule behind {promise!r}"


def test_the_kill_switch_stays_http_only_in_every_doc() -> None:
    """`/shutdown` is deliberately not on Telegram: a kill switch reachable from a
    chat window is one lost phone away from being pressed by someone else."""
    from app.controlbot import HELP

    assert "/shutdown" not in HELP
    assert "HTTP-only" in read("docs/control-bot.md")
    assert "/control/shutdown" in read("docs/setup-render.md")


def test_the_spec_records_this_weeks_storage_decisions() -> None:
    """Decisions that arrived after the spec was written have to be *in* the spec.

    Three of them change what the storage job must do, so a reader of §10 alone should get them:
    the batch granularity, what a link's permanence does to our publishing rules, and the fact
    that the bots are our own clones (which is why the clone's @username is now load-bearing).
    """
    spec = " ".join(read("docs/requirements-draft.md").split())
    for decision in (
        "one batch per episode holding every quality",
        "a link works forever",
        "nothing published may reference a message id inside the bot chat",
        "our own clones",
        "Never rename a clone that has published links",
    ):
        assert decision in spec, f"§10 lost the decision: {decision!r}"
    assert "deleted after 5 minutes" in spec, (
        "the deletion notice needs its scope stated next to the permanence claim"
    )
    assert "Public or Private Mode" in spec, "the mode question has to be visible in the spec too"


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


def test_the_channel_help_notes_cite_their_source_and_draw_the_free_tier_line() -> None:
    """The operator's instruction was that Channel Help is documented on the internet, so the notes
    have to look like research and not like a guess: a source, a date, the price tier each feature sits
    on, and the two things we are *not* allowed to depend on.

    The 48-hour deletion limit and the button syntax are here because our own rules were written
    against them: zero-deletion is only honest if it is also what the tool can do, and `#g #r #p` in
    `app/captions.py` is only defensible if someone can point at where it came from.
    """
    doc = read("docs/channel-help.md")
    assert "botguide.me" in doc, "the notes stopped citing the guide"
    assert "2026-08-28" in doc, "and stopped dating the reading"
    assert "\n## Sources" in doc, "the source list has to be its own section, not a stray link"
    for claim in ("free", "PLUS", "48 hours", "Auto-complete", "no buttons", "Add members", "Attach Media"):
        assert claim in doc, f"the note about {claim!r} is gone, and our design leans on it"
    # The two facts that change what we may build, said in the words a reader will search for.
    assert "never replaces" in doc, "auto-complete adds: that is why in-place captioning is our own code"
    assert "the guide's change, not this repo's guess" in doc, "the notes admit they are secondary sources"


def test_channel_help_is_named_as_the_reason_two_job_kinds_are_blocked() -> None:
    """A blocked job must name the document that says what the finished thing looks like."""
    from app.handlers import DEPENDENCIES

    for kind in ("publish_post", "edit_post"):
        assert "docs/channel-help.md" in DEPENDENCIES[kind]
    doc = read("docs/channel-help.md")
    for kind in ("publish_post", "edit_post"):
        assert f"`{kind}`" in doc, f"{kind} is blocked and the note does not say so"
    assert "no code drives its menu" in doc, "and the note must not imply the bot is automated"


def test_the_pending_inputs_page_lists_everything_the_code_can_ask_for() -> None:
    """`docs/pending-inputs.md` is the one page that answers "what do I still owe you?".

    It is generated-in-spirit, so it is checked that way: the blueprint's unfilled env rows, the
    settings with no default, the config row that is seeded empty, and the blocked job kinds are each
    compared against the page. A list a human maintains by memory is a list that quietly keeps
    promising something the code stopped needing — or hiding something it still wants.
    """
    from app.config import Settings

    doc = read("docs/pending-inputs.md")
    blueprint = read("render.yaml")

    # `sync: false` with no `value:` is how this blueprint says "the operator fills this in".
    left_empty = set(re.findall(r"- key: (\w+)\n\s+sync: false", blueprint))
    assert left_empty, "the blueprint stopped marking which values are left to the operator"
    unnamed = sorted(key for key in left_empty if f"`{key}`" not in doc)
    assert not unnamed, f"the pending-inputs page does not name: {unnamed}"

    # And the two sources must agree with each other: a setting that gained a default should not keep
    # a row on the operator's list, and a required setting must never be missing from the blueprint.
    no_default = {name.upper() for name, field in Settings.model_fields.items() if field.default in (None, (), "")}
    assert no_default == left_empty, (
        f"app/config.py needs {sorted(no_default)}, render.yaml leaves empty {sorted(left_empty)}"
    )

    # Every blocked job kind is listed, with its reason — the page has to say what is not the
    # operator's fault, or "fill this in and it works" is the message they take away.
    from app.handlers import DEPENDENCIES

    blocked = sorted(DEPENDENCIES)
    unlisted = [kind for kind in blocked if f"`{kind}`" not in doc]
    assert len(blocked) == 8, "the blocked list grew or shrank; the page and /status both quote it"
    assert not unlisted, f"blocked job kinds the page does not admit to: {unlisted}"
    assert "the writing half is not" in doc, "and the page must not let 'almost running' stand"

    assert "`updates.channel`" in doc, "the one config row seeded empty has to be named"
    assert "select count(*) from app.config" in doc
    from conftest import config_row_count

    claimed = re.search(r"must read \*\*(\d+)\*\*", doc)
    assert claimed and int(claimed.group(1)) == config_row_count(), "the row count on the page went stale"
