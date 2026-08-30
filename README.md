# auto-manager

A modular Telegram media-management system for content the operator owns or is
authorized to distribute.

**Status:** the decision layer and the runtime around it are built and tested —
parsing, the Hindi-scope rule, season boundaries, manifest order, the create-vs-edit
choice, caption rendering from the formats you approved, the ordered destination-channel
setup plan,
scanning into the database, and the owner's control bot (including logging the spare
account in from a chat). What is **not** implemented is the eight job kinds that need
another bot's protocol observed once — archive copies, the storage bot's menus,
Channel Help publishing, link checks, sticker posting, join-request campaigns. Each
one fails loudly into a `blocked` queue state that `/status` reports, rather than
pretending to run. [`docs/launch-checklist.md`](docs/launch-checklist.md) is the
short list of what needs your accounts.

* Spec and agreed decisions: [`docs/requirements-draft.md`](docs/requirements-draft.md)
* What exists and where each rule is enforced: [`docs/architecture.md`](docs/architecture.md)
* Setup: [`docs/setup-supabase.md`](docs/setup-supabase.md) · [`docs/setup-render.md`](docs/setup-render.md)
* Starting out, in order, click by click: [`docs/launch-checklist.md`](docs/launch-checklist.md)
* What Channel Help's own guide says, and which of it we may depend on: [`docs/channel-help.md`](docs/channel-help.md)
* Everything still outstanding — seven values, one config row, two chores, and what is *not* your list:
  [`docs/pending-inputs.md`](docs/pending-inputs.md)
  From your phone: [`docs/control-bot.md`](docs/control-bot.md) — and the published wording
  you approved, rendered: [`docs/captions-approved.md`](docs/captions-approved.md)
* How a destination channel is created and furnished, and how a new season is recognised:
  [`docs/seasons-and-channels.md`](docs/seasons-and-channels.md)

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
| Postgres schema: 23 tables + 4 views (27 relations), constraints, RLS, 44 seeded config rows (`docs/setup-supabase.md` says which ones the app reads) | built, executed against real Postgres in tests |
| Queue: lease-based claim, stage checkpoints, exponential retry, blocked state | built, tested |
| Restart recovery (`release_expired_locks` + boot reconciliation) | built, tested, verified live |
| HTTP surface: `/health` `/ready` `/status` `/control/pause\|resume\|reconcile\|shutdown` | built, tested, verified live |
| Config: fail-closed live mode, masked secrets, pooler detection | built, tested |
| Deployment: `render.yaml` Blueprint, UptimeRobot `/health`, Dockerfile deliberately absent | built, tested |
| Credential guard (`scripts/check_secrets.py`) + CI (`ops/ci.yml`) | built, tested |
| Control bot: `/status` `/pause` `/probe` `/login` `/sessions` `/declare` `/source` `/destination` `/discover` (owner-only, [docs](docs/control-bot.md)) | built; tested against a fake transport, so Telegram-side behaviour is unverified from this network |
| Local login helper (`scripts/login.py`) | kept as the offline fallback; the bot is the default path |
| Source scanning and metadata parsing (series, season, episode, language, quality) | built (`normalize.py`, `ingest.py`); the live listener that feeds it needs a session |
| Season boundaries: declared vs inferred, sticker ordering, publish hold | built (`seasons.py`, [docs](docs/seasons-and-channels.md)) |
| Thumbnail screening (allowlist: `@YCAnime`, `@india_crunchyroll`) | built as a **gate** (`thumbnails.py`): with no image evidence it parks for review. Your correction — screening should rank and flag rather than block — is **not built yet** |
| In-place publishing: caption the file post that is already there, pipeline unchanged (`/inplace`, [docs](docs/seasons-and-channels.md)) | policy, plan, mode and command built and tested; the `EditMessage` call itself is the unwired write layer |
| Storage bot verbs (`/genlink`, `/batch`, `/custom_batch`, `/special_link`, `/universal_link`) **and the `/batch` flow** | observed and recorded, with a drift check, an unsendable list, and the vendor's own clone-manager claims kept apart from them ([docs](docs/storage-bot.md)) |
| Updates channel: card post → forward to `@Link_providerobot` → one shareable link → announcement ([docs](docs/updates-channel.md)) | flow and both post shapes recorded and tested; one global target channel, a per-episode rhythm, and a private channel named by id are settings (`updates.channel`, `updates.per_episode`, both read by `/status`); the announcement box is approved as `templates.announcement_post`; the probe may read that bot but never mint a link; the send itself is the unwired write layer |
| Channel Help's documented behaviour — flow, buttons, plans, the 48-hour deletion limit ([docs](docs/channel-help.md)) | transcribed from the official guide on 2026-08-28 with sources, and separated from what this account has observed |
| Our own rights in a channel, detected instead of typed (`app/rights.py`, run by `/probe`) | built and tested against fakes; the dialog walk is the only evidence it accepts, and a channel it cannot see stays unread and is named |
| Discovery: the spare account's channels sorted by role — read-only becomes a source, a channel we post in becomes that series' destination (`/discover`, `app/discover.py`) | built and tested against fakes; it creates no channel, invents no series name, refuses to strand a series' only source, and switches on its own only when `discover.auto` is on |
| Archive copy, the storage bot's *write layer*, Channel Help adapter | **not built** — the menu and the `/batch` conversation are both recorded (two forwards in, one permanent `?start=` link out); what is missing is the code that performs them, plus what only a live run can settle — whether a link is a reference to the source post or a copy. See [docs](docs/storage-bot.md) |
| Season sticker *posting*, join-request campaigns | **not built** — the pack's document ids and the request template are still open; the boundary logic above is not |

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
