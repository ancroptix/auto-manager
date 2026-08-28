#!/usr/bin/env python3
"""Refuse to commit credentials. Run as a pre-commit hook and in CI.

Why this file exists: the single most likely way this project gets destroyed is
not a bug — it is a session string or a service key landing in a public repo.
Git history is effectively permanent, and a pushed secret is a rotated secret,
which for a Telegram account means logging out of everything.

Usage:
    python scripts/check_secrets.py            # scan git-tracked files
    python scripts/check_secrets.py --all      # scan every non-ignored file
Install as a hook:
    printf '#!/bin/sh\\npython3 scripts/check_secrets.py\\n' > .git/hooks/pre-commit
    chmod +x .git/hooks/pre-commit
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".ico", ".woff", ".woff2", ".mp4", ".mkv"}
SAFE_NAMES = {".env.example", "check_secrets.py", "login.py"}

RULES: list[tuple[str, re.Pattern[str]]] = [
    ("Telegram session string", re.compile(r"1AAA[A-Za-z0-9+/=_-]{24,}")),
    ("Telethon StringSession literal", re.compile(r"StringSession\(\s*[\"'][A-Za-z0-9+/=_-]{25,}[\"']")),
    # A session *file* is what must never be committed. An attribute access is not
    # one: `client.session.as_string()` is the supported way to read a StringSession,
    # so the lookahead keeps that legal line out of the findings.
    ("MTProto session file path", re.compile(r"\b[\w.-]+\.session\b(?!\s*\.)")),
    ("api_hash literal", re.compile(r"api[_-]?hash[\"']?\s*[:=]\s*[\"'][0-9a-f]{32}[\"']", re.I)),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("Render API key", re.compile(r"\brnd_[A-Za-z0-9]{20,}\b")),
    ("Supabase service_role key", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Private key block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----")),
    ("Password in URL", re.compile(r"[a-z]+://[^/\s:@]+:[^/\s:@]{6,}@", re.I)),
    ("Bot token", re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,}\b")),
]

# Assignments that must never carry an inline literal value. A value read from
# the environment (os.environ, getenv, ${VAR}, config lookup) is fine; a literal
# is not.
DANGEROUS_KEYS = (
    "TELEGRAM_SESSION_STRING|TELEGRAM_API_HASH|CONTROL_TOKEN|DATABASE_URL|"
    "SUPABASE_SERVICE_ROLE_KEY|API_HASH"
)
DANGEROUS_ASSIGNMENT = re.compile(
    # [ \t]* rather than \s*: a newline after "=" must not let the next line be
    # mistaken for this key's value.
    rf"^[ \t]*(?:export[ \t]+)?({DANGEROUS_KEYS})[ \t]*=[ \t]*(.*?)[ \t]*$", re.M
)
# A value produced at read time — an env lookup, a subprocess/`$(...)`
# substitution, a secrets.token_urlsafe() call — is not a committed secret, so
# it must not be flagged. This keeps the "generate one yourself" instructions in
# the docs from tripping the check they are documenting.
BENIGN_VALUE = re.compile(
    r"^(?:\$\(|\$\{|os\.environ|os\.getenv|getenv|secrets\.|settings\.|config\.|"
    r"self\.|%\(|input\(|Field\(|None$|\s*$)"
)


# A test that proves the scanner catches a GitHub token has to contain something
# that looks like one. Rather than exempt whole paths (which would exempt real
# leaks too), a file or line opts itself out explicitly, so the exemption is
# reviewable in the diff.
FILE_MARKER = "# check-secrets: fixture"
LINE_MARKER = "# allowlist: secret"

# ``<your-db-password>`` in documentation is a hole to fill in, not a secret.
# Masking placeholders keeps the scanner strict about real literals without
# crying wolf at setup guides — and a scanner with false positives gets switched
# off, which is the failure mode worth avoiding here.
PLACEHOLDER = re.compile(r"<[^>\n]{1,80}>")


def _mask_placeholders(text: str) -> str:
    masked = PLACEHOLDER.sub("x", text)
    for word in ("changeme", "yourpassword", "your-password", "REPLACE_ME", "xxxx"):
        masked = masked.replace(word, "x")
    return masked


def _marker_lines(lines: list[str]) -> tuple[set[int], bool]:
    return (
        {i for i, line in enumerate(lines) if LINE_MARKER in line},
        any(FILE_MARKER in line for line in lines[:5]),
    )


def scan_text(name: str, text: str) -> list[str]:
    findings: list[str] = []
    scanned = _mask_placeholders(text)
    lines = scanned.splitlines()
    marked, file_exempt = _marker_lines(lines)
    if Path(name).name in SAFE_NAMES or file_exempt:
        # The scanner and the login helper necessarily mention these shapes.
        return findings

    def line_of(offset: int) -> int:
        return scanned.count("\n", 0, offset)

    def keep(offset: int) -> bool:
        return line_of(offset) not in marked

    for label, pattern in RULES:
        for match in pattern.finditer(scanned):
            if keep(match.start()):
                findings.append(f"{name}: {label} — `{match.group(0)[:24]}…`")

    for match in DANGEROUS_ASSIGNMENT.finditer(scanned):
        value = match.group(2).strip().strip("\"'")
        if not value or value == "x" or BENIGN_VALUE.match(match.group(2).strip()):
            continue
        if not keep(match.start()):
            continue
        findings.append(
            f"{name}: {match.group(1)} is assigned the literal {value[:12]!r}…; "
            "read it from the environment instead"
        )
    return findings


def _git_ls_files(*flags: str) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z", *flags], check=True, capture_output=True, text=True
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [f for f in out.split("\0") if f]


def tracked_files() -> list[str]:
    return _git_ls_files("--cached")


def worktree_files() -> list[str]:
    """Tracked plus untracked-but-not-ignored.

    The useful default: the whole point is to catch a secret *before* it is
    committed, and scanning only the index misses the file you are about to add.
    """
    return _git_ls_files("--cached", "--others", "--exclude-standard")


def all_files() -> list[str]:
    skip_dirs = {".git", ".venv", "node_modules", "__pycache__", ".pgdata", ".pytest_cache"}
    found: list[str] = []
    for path in Path.cwd().rglob("*"):
        if not path.is_file() or any(part in skip_dirs for part in path.parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        found.append(str(path.relative_to(Path.cwd())))
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan for committed credentials.")
    parser.add_argument("--all", action="store_true", help="scan every file, not just git-tracked ones")
    args = parser.parse_args()

    files = all_files() if args.all else worktree_files()
    if not files:
        print("check_secrets: no files to scan (not a git checkout?)")
        return 0

    findings: list[str] = []
    for name in files:
        path = Path(name)
        if not path.is_file() or path.suffix.lower() in SKIP_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if len(text) > 2_000_000:
            continue
        findings.extend(scan_text(name, text))

    if findings:
        print("check_secrets: POSSIBLE CREDENTIALS FOUND\n", file=sys.stderr)
        for finding in dict.fromkeys(findings):
            print(f"  - {finding}", file=sys.stderr)
        print(
            "\nRemove the value, then rotate it: a secret that reached a commit is "
            "compromised even if you delete the file next. For a Telegram session "
            "that means Settings → Privacy and Security → Devices → Terminate all "
            "other sessions.",
            file=sys.stderr,
        )
        return 1

    print(f"check_secrets: {len(files)} file(s) scanned, no credential patterns found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
