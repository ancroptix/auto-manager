# auto-manager

A modular Telegram media-management system for content the operator owns or is
authorized to distribute.

**Status:** the runtime skeleton and the whole decision layer are built and
tested (376 tests): parsing, the Hindi-scope rule, thumbnail screening, manifest
order, the create-vs-edit choice, and destination-channel setup. Source scanning,
archive copies, storage-bot menus and Channel Help publishing are **not**
implemented — they need a logged-in session and fail loudly into a `blocked`
queue state rather than pretending to run.

* Spec and agreed decisions: [`docs/requirements-draft.md`](docs/requirements-draft.md)
* What exists and where each rule is enforced: [`docs/architecture.md`](docs/architecture.md)
* Setup: [`docs/setup-supabase.md`](docs/setup-supabase.md) · [`docs/setup-render.md`](docs/setup-render.md)
* From your phone: [`docs/control-bot.md`](docs/control-bot.md) — and the published wording
  you approved, rendered: [`docs/captions-approved.md`](docs/captions-approved.md)

## Pipeline

```text
authorized source channels
        ↓  detect + normalize series/season/episode/language/quality
clean-thumbnail screening (multi-source priority order)
        ↓
private master archive channel          ← canonical backup of every selected file
        ↓
@anime_hindifilesbot  (single / batch / universal links)
        ↓  generated retrieval link
Channel Help (@chelpbot)  →  text post + inline buttons in destination channel
        ↓
Supabase (manifests, jobs, message IDs, links, audit log)
        ↑
Render web service  (health endpoint, watchdog, retry queue, restart reconciliation)
        ↑
Owner private Telegram chat  (control commands, review queues, approvals)
```

## Component status

| Component | State |
| --- | --- |
| Postgres schema: 23 tables + 4 views, constraints, RLS | built, executed against real Postgres in tests |
| Queue: lease-based claim, stage checkpoints, exponential retry, blocked state | built, tested |
| Restart recovery (`release_expired_locks` + boot reconciliation) | built, tested, verified live |
| HTTP surface: `/health` `/ready` `/status` `/control/pause\|resume\|reconcile\|shutdown` | built, tested, verified live |
| Config: fail-closed live mode, masked secrets, pooler detection | built, tested |
| Deployment: `render.yaml` Blueprint, UptimeRobot `/health`, Dockerfile deliberately absent | built, tested |
| Credential guard (`scripts/check_secrets.py`) + CI (`ops/ci.yml`) | built, tested |
| Control bot: `/status` `/pause` `/probe` `/login` `/sessions` (owner-only, [docs](docs/control-bot.md)) | built, 49 tests; Telegram-side behaviour unverified from this network |
| Local login helper (`scripts/login.py`) | kept as the offline fallback; the bot is the default path |
| Source scanning / metadata parsing | **not built** — needs filename patterns |
| Thumbnail screening (allowlist: `@ycanime`, `@india_crunchyroll`) | **not built** — the rule is agreed, the detector is not |
| Archive copy, `@anime_hindifilesbot` adapter, Channel Help adapter | **not built** — needs one authenticated test run |
| Season sticker mapping, join-request campaigns | **not built** — template and mapping still open |

## Run it locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

python scripts/devdb.py --reset       # disposable Postgres, migrations applied
eval "$(python scripts/devdb.py --print-url)"   # exports the local socket URL
# Generate a throwaway control token locally rather than reusing one:
export CONTROL_TOKEN=$(python -c 'import secrets; print(secrets.token_urlsafe(32))') WORKER_POLL_SECONDS=1
uvicorn app.main:app --reload --port 10000

curl -s localhost:10000/status | python -m json.tool
```

To prove a migration change before touching Supabase, apply the two files in
`supabase/migrations/` in order — `devdb.py` does exactly that on every start.

## Tests

```bash
python -m pytest              # 166 tests; Postgres integration included
python -m pytest -m "" --ignore=tests/test_migrations_on_postgres.py   # fast set only
```

The suite asserts the behaviour, not the code: quality order survives arrival
order, a paused service claims nothing, a restart resumes at the checkpoint,
`anon` can read nothing, and no credential is committed.

## Security

Never commit Telegram login codes, 2FA passwords, MTProto sessions, bot tokens,
Supabase service keys, or Render credentials. Runtime secrets live in the
deployment platform only. `scripts/check_secrets.py` runs in CI (`ops/ci.yml`) and as a
pre-commit hook, and `TELEGRAM_SESSION_STRING` is read from the environment —
it is never written to disk, because Render's filesystem is ephemeral.

If a session string is ever exposed: Telegram → Settings → Privacy and Security
→ Devices → **Terminate all other sessions**.
