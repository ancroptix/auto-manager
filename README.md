# auto-manager

A modular Telegram media-management system for content the operator owns or is authorized to distribute.

**Status:** requirements and architecture planning. Implementation has not started.

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

## Planned components

- Telegram MTProto user client for source/archive workflows (Telethon)
- Multi-source media discovery and deduplication
- Strict thumbnail screening and owner review queue
- Private canonical archive channel
- Storage/link-bot adapter
- Channel Help publishing adapter
- One private destination channel per complete series
- Season and episode manifests
- Join-request support with consent, anti-spam controls, and audit logs
- Supabase persistence
- Render deployment, health checks, retries, and restart reconciliation

See [`docs/requirements-draft.md`](docs/requirements-draft.md) for the agreed specification and unresolved decisions.

## Security

Never commit Telegram login codes, 2FA passwords, MTProto sessions, bot tokens, Supabase service keys, or Render
credentials. Runtime secrets are configured directly in the deployment platform.
