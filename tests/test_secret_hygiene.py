# check-secrets: fixture
"""Repo-level hygiene: no credentials, and the docs match reality.

Run against whatever git tracks, so it catches an accidental `git add .env`
or `git add bleach.session` on the next commit — before it reaches GitHub,
where deleting it does not un-leak it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

check_secrets = pytest.importorskip(
    "check_secrets", reason="scripts/check_secrets.py must exist"
)


def test_worktree_scan_clean() -> None:
    """Covers files that are not committed yet — the only time this can help."""
    tracked = check_secrets.worktree_files()
    assert tracked, "expected a git checkout"
    findings: list[str] = []
    for name in tracked:
        path = ROOT / name
        if not path.is_file():
            continue
        findings.extend(check_secrets.scan_text(name, path.read_text(encoding="utf-8", errors="ignore")))
    assert not findings, "committed credential candidates:\n  " + "\n  ".join(findings)


@pytest.mark.parametrize(
    "snippet,label",
    [
        ("TELEGRAM_SESSION_STRING=1AAAvf3Kj2xQ9mLpRsTuVw", "session string"),
        ('api_hash = "0f9e8d7c6b5a4321fedcba9876543210"', "api hash"),
        ("ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123", "github token"),
        ("postgresql://postgres:RealPassword@aws-0.db.supabase.co:5432/postgres", "password in url"),
        ("-----BEGIN RSA PRIVATE KEY-----", "private key"),
        ("SUPABASE_SERVICE_ROLE_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.abcdef", "service key"),
        ("xoxb-1234567890:AAHabcdefghijABCDEFGHIJ0123456789", "bot token"),
    ],
)
def test_scanner_detects_common_leaks(snippet: str, label: str) -> None:
    findings = check_secrets.scan_text("some_file.py", snippet)
    assert findings, f"{label} was not detected"


def test_scanner_ignores_placeholders() -> None:
    safe = 'DATABASE_URL = os.environ["DATABASE_URL"]\nCONTROL_TOKEN=\nTELEGRAM_API_ID=12345\n'
    assert check_secrets.scan_text("config.py", safe) == []


def test_session_files_are_not_in_the_worktree() -> None:
    stray = [p.name for p in ROOT.rglob("*.session") if ".venv" not in p.parts]
    assert not stray, f"MTProto session files must not exist here: {stray}"


def test_readme_documents_the_pipeline_and_points_at_the_spec() -> None:
    readme = (ROOT / "README.md").read_text()
    for token in ("anime_hindifilesbot", "chelpbot", "requirements-draft.md"):
        assert token in readme, f"README no longer mentions {token}"


def test_spec_records_the_confirmed_operator_decisions() -> None:
    spec = (ROOT / "docs" / "requirements-draft.md").read_text()
    for agreed in (
        "@ycanime",
        "@india_crunchyroll",
        "Anime in Hindi",
        "OCtbqTQ_by_sticbot",
        "never** approves or declines",
    ):
        assert agreed in spec, f"the spec lost the decision about {agreed!r}"


def test_no_stale_todos_about_already_decided_items() -> None:
    """Guard against the doc silently re-opening something already settled."""
    spec = (ROOT / "docs" / "requirements-draft.md").read_text()
    assert "primary username is TBD" not in spec
    assert "which handle is primary" not in spec.lower()


# Weakening the scanner to make a test pass is itself a risk: the check is only
# worth having if it still catches what it was written for. This table pins both
# directions.
REAL_LEAKS = [
    "TELEGRAM_SESSION_STRING=1AAAvf3Kj2xQ9mLpRsTuVwabcdefgh",
    'DATABASE_URL="postgresql://postgres:R3alP4ssw0rd@aws-0.pooler.supabase.com:6543/postgres"',
    "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    "xoxb-1234567890:AAHabcdefghijABCDEFGHIJ0123456789",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEow",
    "AKIA3FJ2K5M7NQRT8VWZ",
    'api_hash = "0f9e8d7c6b5a4321fedcba9876543210"',
    "SUPABASE_SERVICE_ROLE_KEY=eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZSJ9.abcdef",
    'CONTROL_TOKEN = "a-hardcoded-token-value"',
    "client = TelegramClient('bleach.session', api_id, api_hash)",
    "path = os.path.expanduser('~/.config/auto-manager/spare.session')",
]

BENIGN_EXAMPLES = [
    'DATABASE_URL = os.environ["DATABASE_URL"]',
    'export CONTROL_TOKEN=$(python -c "import secrets; print(secrets.token_urlsafe(32))")',
    "postgresql://postgres:<your-db-password>@aws-0-ap-south-1.pooler.supabase.com:6543/postgres",
    "TELEGRAM_SESSION_STRING=\nTELEGRAM_API_ID=12345",
    "CONTROL_TOKEN=changeme",
    "gpg --recv-keys 0x1234567890abcdef",
    # reading a session object's string is code, not a file on disk
    "    session = client.session.as_string()",
    "    await client.session.connect()",
]


@pytest.mark.parametrize("snippet", REAL_LEAKS)
def test_scanner_still_catches_real_leaks(snippet: str) -> None:
    assert check_secrets.scan_text("x.py", snippet), f"scanner stopped catching: {snippet[:32]}"


@pytest.mark.parametrize("snippet", BENIGN_EXAMPLES)
def test_scanner_ignores_placeholders_and_generated_values(snippet: str) -> None:
    assert check_secrets.scan_text("x.py", snippet) == []
