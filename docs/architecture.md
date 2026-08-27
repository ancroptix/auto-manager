# Runtime architecture

What exists in this repository today, and which promises are enforced where.

## Layout

```text
app/
  config.py            pydantic-settings; fail-closed validation; secrets never printable
  stages.py            the 7-stage ladder (contract with SQL)
  db.py                asyncpg pool: lazy connect, jsonb codecs, query timeouts, retry-on-stale
  worker.py            the queue loop: claim -> handle -> checkpoint, pause-aware, drain-on-stop
  handlers.py          one handler per job kind; 10 are explicit "not implemented"
  api.py               /health /ready /status /control/*
  main.py              ASGI entrypoint; boots db, reclaims leases, starts loop
  telegram_client.py   Telethon StringSession wrapper, owner gate, flood-wait policy
supabase/migrations/
  0001_init.sql        enums, 22 tables, constraints, indexes, RLS enabled
  0002_functions.sql   triggers, queue functions, views, grants, default config
scripts/
  login.py             operator-side one-time login -> session string
  check_secrets.py     refuses to let a credential be committed
  devdb.py             disposable local Postgres that applies the migrations
tests/                 150 tests, including the migrations executed on real Postgres
docs/                  requirements-draft.md (the spec), setup guides, this file
render.yaml            the whole deployment, as code
```

## One job, start to finish

```text
enqueue_job(kind, dedup_key)          ON CONFLICT DO NOTHING  -> re-scans cannot double-queue
  v_queue.status = 'queued'
claim_next_job(worker_id, lease)      FOR UPDATE SKIP LOCKED  -> two instances never share a file
  v_status = 'running', attempts += 1
handler runs, calling checkpoint_job(stage) after each real step
  v each stage is a row in app.job_event, with its jsonb payload
  v an illegal transition raises (you cannot skip 'archived' and claim you archived)
complete_job(result) | fail_job(error, retry_after)
  v failure waits exponentially; after max_attempts the job is 'blocked', not dropped
```

If the process dies anywhere in that ladder, boot does two things:
`release_expired_locks()` puts the job back to `queued` **while keeping its
stage**, and a `reconciliation` job is enqueued. So it resumes at
`archived`/`link_received` instead of re-uploading.

## Where each promise is actually enforced

| Promise | Enforced in | Proved by |
| --- | --- | --- |
| DMing a requester never approves/declines them | `CHECK (approve_after_send is not true)` — the column cannot be set true | `test_migrations_never_approves` |
| Same campaign cannot message a user twice | `PRIMARY KEY (campaign_id, user_id)` | `test_same_campaign_cannot_message_a_user_twice` |
| Same episode+quality never posted twice | unique index on `(episode_id, lower(quality), release_variant)` | `test_duplicate_quality_is_rejected_case_insensitively` |
| One permanent post per episode (so a late 1080p is an **edit**) | partial unique index `where kind = 'episode'` | `test_one_permanent_post_per_episode` |
| One active universal link per episode | partial unique index | `test_one_active_universal_link_per_episode` |
| Display order is 480p, 720p, 1080p regardless of arrival order | `v_episode_manifest` ordered by `quality_rank` | `test_manifest_orders_by_quality_not_arrival` |
| A half-imported season never gets a "Complete Season" post | `v_season_coverage.season_complete` requires a file per episode | `test_season_coverage_needs_files_not_just_row_counts` |
| Two processes never upload the same file | `FOR UPDATE SKIP LOCKED` + lease + `job_lease_required` CHECK | `test_claim_grants_an_exclusive_lease` |
| A restart resumes instead of restarting | stage kept by `release_expired_locks()` | `test_expired_leases_are_reclaimed_and_stage_is_kept` |
| Pause really stops work | `claim_next_job` returns NULL while paused | `test_claim_respects_the_kill_switch` |
| Unclean thumbnails cannot publish | `thumbnail_status` + review table; screening handler is the gate | enforced in `thumbnail_screen` (not yet implemented) |
| Nothing is exposed to the public API | all tables in `app` schema, RLS on, zero policies, `anon` has no grants | `test_anon_role_cannot_read_anything` |
| No credential reaches GitHub | `scripts/check_secrets.py` | `test_tracked_files_scan_clean` |

## Deliberate choices that look like omissions

* **Stubs raise instead of returning.** An empty handler would make the queue
  report success while nothing was archived or published. `FeatureNotImplemented`
  blocks the job and surfaces it in `/status` → `blocked_features`. The backlog is
  visible, which is the whole point of building the runtime first.
* **`APP_MODE=shadow` by default.** Live mode refuses to start unless the
  session, owner IDs, control token and database are all present. A first deploy
  therefore cannot message a stranger.
* **No `Dockerfile`.** Render prefers it over `runtime: python` when present, so
  adding one early would silently change the build. Add it together with the
  ffmpeg dependency for thumbnails, in the commit that needs it.
* **No Render Postgres in `render.yaml`.** Free Render Postgres is deleted after
  30 days; Supabase is the store.
* **The stage ladder is duplicated in Python and SQL on purpose.** Both sides
  need it (the app for flow control, the database for validation), and
  `test_stage_contract.py` fails if they ever disagree.
* **`search_path` is quoted, not interpolated**, and jsonb codecs are set per
  connection in `db.py`. Both are places where the "obvious" code silently
  misreads data instead of erroring.

## Known limits of this phase

* No Telegram I/O yet: no source scanning, no thumbnail screening, no archive
  copies, no storage-bot menus, no Channel Help publishing.
* `@anime_hindifilesbot`'s command protocol and the season-sticker mapping are
  still unknown and need one authenticated run.
* The `thumbnail_screen` "contains a disallowed `@handle`" detector is the part
  most likely to need tuning against real thumbnails.
* Free-tier Render can still interrupt a multi-gigabyte copy mid-flight; stages
  make it resume, not prevent.
