# Runtime architecture

What exists in this repository today, and which promises are enforced where.

## Layout

```text
app/
  config.py            pydantic-settings; fail-closed validation; secrets never printable
  stages.py            the 7-stage ladder (contract with SQL)
  db.py                asyncpg pool: lazy connect, jsonb codecs, query timeouts, retry-on-stale
  worker.py            the queue loop: claim -> handle -> checkpoint, pause-aware, drain-on-stop
  handlers.py          the registry: reconciliation, ingest_media, thumbnail_screen, plus the eight
                       writers below; DEPENDENCIES says what each kind still waits on
  sender.py            the ONE write path: plan/live, peer allowlist, write budget, audit row,
                       flood waits turned into a re-queue (no handler sleeps on Telegram's clock)
  writers.py           archive, storage handoff, the two link checks, publish, edit, season sticker,
                       join-request campaign — each refuses what only a human can supply
  normalize.py         filename/caption parsing + the Hindi-eligibility decision
  ingest.py            one message -> candidate, episode and variant rows
  manifest.py          display order, create-vs-edit, season coverage
  seasons.py           what entitles the pipeline to open a season, and in what order
  thumbnails.py        the publish gate: handle allowlist, candidate selection
  captions.py          handle hygiene, template rendering, safe filenames
  channels.py          destination setup sequence, naming, sticker/reply rules
  keys.py              every dedup/identity key in one place (used by all of the above)
  api.py               /health /ready /status /control/*
  main.py              ASGI entrypoint; boots db, reclaims leases, starts loop
  telegram_client.py   Telethon StringSession wrapper, owner gate, flood-wait policy
  botapi.py            the whole Bot API surface we need: sendMessage, getUpdates,
                       deleteMessage — no file sending exists here by omission
  controlbot.py        the owner's remote: owner gate, login flow, every command
  sessions.py          one reader of the session string; everything else is metadata
  mtproto_login.py     sendCode/signIn on the operator's behalf (login only, nothing else)
supabase/migrations/
  0001_init.sql        enums, 22 tables, constraints, indexes, RLS enabled
  0002_functions.sql   triggers, queue functions, views, grants, default config
  0003_control_bot.sql app.telegram_session + the bot.* settings the operator tunes
  0004_approved_captions.sql the three caption formats as they were dictated, and the series subtitle
  0005_seasons_and_profile.sql season boundary provenance + declared-vs-observed span, and the
                       destination's setup_state / photo / about columns; no new relation, which
                       is what keeps every file here re-runnable on its own
scripts/
  login.py             operator-side one-time login -> session string
  check_secrets.py     refuses to let a credential be committed
  devdb.py             disposable local Postgres that applies the migrations
tests/                 the whole suite, including the migrations and handlers run on real Postgres
ops/ci.yml             the CI job; copy into .github/workflows/ to activate it
docs/                  requirements-draft.md (the spec), setup guides, this file,
                       seasons-and-channels.md (the two policies the operator asked to have
                       explained: channel furnishing, and season boundaries)
render.yaml            the whole deployment, as code
```

## The operator's side: the control bot

`app/main.py` starts `ControlBot.run()` when `TELEGRAM_BOT_TOKEN` and owner ids are
both present, and nothing else about the service depends on it: a revoked token
costs the remote, never the queue. Long polling (`getUpdates`) instead of a webhook
because it needs no inbound route, no certificate and no operator configuration,
and an offset in the client means a redeploy resumes rather than replays.

Three boundaries make it safe enough to hand to a phone:

* **`handle()` is the whole brain** and takes a parsed update. Authorization and the
  private-chat check happen before any command text is looked at, so there is no
  path where a handler runs for a stranger. `dispatch()` adds the side effects
  (send, delete) and `run()` is only the loop — which is why the security rules are
  testable without a network.
* **The login path owns the only dangerous capability.** `app/mtproto_login.py` is
  the sole module that can turn a phone number and code into a session, it disconnects
  immediately after, and it can do nothing else — no `send_message`, no `GetHistory`.
  The value goes to `app/sessions.py:store()` and from there to
  `telegram_client.resolve_session_string()`, which reads the environment first, so a
  hand-set string is never silently overridden by an older stored one.
* **Nothing sensitive is printable.** `BotApi` keeps the token out of `repr()` and out
  of every error message (`httpx` puts the full URL, token included, into its
  exceptions); `sessions.scrub()` runs over every outgoing reply and removes named
  secrets *and* session-shaped text, because an exception that carries a session was
  written by nobody who meant to leak one.

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
| DMing a requester never approves/declines them | `CHECK (approve_after_send is not true)` — the column cannot be set true | `test_messaging_never_approves_a_request` |
| Same campaign cannot message a user twice | `PRIMARY KEY (campaign_id, user_id)` | `test_same_campaign_cannot_message_a_user_twice` |
| Same episode+quality never posted twice | unique index on `(episode_id, lower(quality), release_variant)` | `test_duplicate_quality_is_rejected_case_insensitively` |
| One permanent post per episode (so a late 1080p is an **edit**) | partial unique index `where kind = 'episode'` | `test_one_permanent_post_per_episode` |
| One active universal link per episode | partial unique index | `test_one_active_universal_link_per_episode` |
| Display order is 480p, 720p, 1080p regardless of arrival order | `v_episode_manifest` ordered by `quality_rank` | `test_manifest_orders_by_quality_not_arrival` |
| A half-imported season never gets a "Complete Season" post | `v_season_coverage.season_complete` requires a file per episode **and** a declared span | `test_season_coverage_needs_files_not_just_row_counts` |
| In-place captioning never overwrites a message's real note | `app.inplace.looks_like_label` (whole-string label patterns) + `ASK` | `test_anything_with_information_in_it_is_not` |
| The updates channel's link is only ever a link the bot actually sent | `app.linkprovider.parse_reply` (marker **and** `?start=` link) | `test_the_request_and_the_reply_are_not_confused_with_each_other` |
| A verb that mints a permanent link is not a probe's business | `app.linkprovider.NOT_FOR_PROBE`, checked before the allowlist | `test_widening_the_allowlist_does_not_enable_the_link_verb` |
| The text a caption replaced still exists somewhere | `app.destination_post.caption_previous` | `test_a_replacement_carries_the_text_it_burned` |
| Twelve files here and twelve there is twelve edits, not twelve copies | `SeasonShape.counts` -> `plan` | `test_twelve_files_against_twelve_files_is_twelve_edits_and_no_copies` |
| A suspected renumbering copies nothing at all | `SeasonShape.numbering_shifted` short-circuits the copy branch | `test_the_renumbering_trap_copies_nothing_and_asks_instead` |
| A source pausing is not read as a season finishing | ingest writes `observed_first/last`; the view reads `first_episode/last_episode`, which only `/declare` writes | `test_season_completeness_needs_a_declaration_not_a_pause` |
| An unlabelled numbering restart cannot open a season on a guess | `seasons.classify` returns an `ask_owner` reset and `ingest` parks the candidate instead of writing a season row | `test_an_unlabelled_restart_is_held_and_files_nothing`, `test_unlabelled_restart_is_a_boundary_we_do_not_act_on_alone` |
| Season stickers go out in the operator's order, once | `transition_stickers()` + one flag per side, and `publish_hold()` | `test_the_stickers_come_in_the_operators_order`, `test_stickers_are_never_posted_twice_for_one_boundary` |
| A destination is never left half-furnished on a restart | named `SETUP_STEPS` with progress in `app.destination.setup_state` | `test_setup_sequence_is_ordered_and_resumable`, `test_irreversible_steps_are_marked` |
| The publisher cannot promote strangers or clear the channel | `FORBIDDEN_HELP_RIGHTS` refused by `channel_help_rights` whatever `bots.channel_help_rights` says | `test_the_rights_knob_narrows_and_never_opens_the_forbidden_two` |
| Two processes never upload the same file | `FOR UPDATE SKIP LOCKED` + lease + `job_lease_required` CHECK | `test_claim_grants_an_exclusive_lease` |
| A restart resumes instead of restarting | stage kept by `release_expired_locks()` | `test_expired_leases_are_reclaimed_and_stage_is_kept` |
| Pause really stops work | `claim_next_job` returns NULL while paused | `test_claim_respects_the_kill_switch` |
| Unclean thumbnails cannot publish | `thumbnail_status` + review table, and `thumbnail_screen` is the only writer of the archive job | `test_thumbnail_screen_handler_rejects_parks_and_advances` |
| Subbed-only releases never enter the library | `normalize._classify_audio` + the candidate's disposition; ingest stops at a rejection | `test_subbed_only_is_recognised_as_such`, `test_ingest_writes_the_rows_the_ladder_reads` |
| A late quality edits the post instead of duplicating it | `manifest.decide_publish` returns `noop` unless a *new* identity appears | `test_unchanged_availability_is_a_noop` |
| A batch never fabricates per-episode uploads | `ingest` records episodes but no variants for `file_kind = 'batch'` | `test_ingest_writes_the_rows_the_ladder_reads` |
| An unviewed image is not "clean" | strict mode parks `evidence="caption_only"` as `review_required` | `test_strict_mode_parks_caption_only_evidence` |
| A join request is never read as consent | `channels.parse_setup_reply` refuses text matching the request form | `test_join_request_text_cannot_be_read_as_consent` |
| Nothing is exposed to the public API | all tables in `app` schema, RLS on, zero policies, `anon` has no grants | `test_anon_role_cannot_read_anything` |
| No credential reaches GitHub | `scripts/check_secrets.py` | `test_worktree_scan_clean` |

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

* No Telegram I/O yet: the scanner, archive copies, storage-bot menus, Channel
  Help publishing, `createChannel`/`editPhoto`/`editAdmin` on a destination, and
  sticker posting are unwired. Everything that *decides* what they should do is
  implemented and tested — `ingest.record_message` already takes the exact payload
  a scanner will hand it, `setup_plan()` already carries the exact title, photo, bio
  and permission set the creation path must apply — so the missing piece is
  transport, not behaviour. Nothing in this repository has ever renamed a real
  channel, and no doc here should be read as saying it has.
* Season completeness is a *decision*, not a workflow: `season_coverage()` and
  `should_post_season_batch()` are built and tested, with no production caller, because
  the publish layer that would act on them is unwired. Declaring a length with `/declare`
  therefore changes captions and the completeness rule today, not a message that goes out.
* Thumbnail screening currently reads handles from text (caption/filename/OCR
  output). Opening the actual image bytes needs the Telethon media layer, which is
  why strict mode parks a caption-only verdict instead of passing it.
* `@anime_hindifilesbot`'s command protocol and the season-sticker mapping are
  still unknown and need one authenticated run.
* The `@handle` detector is the part most likely to need tuning against real
  thumbnails: burned-in text without a handle form is invisible until OCR exists.
* Free-tier Render can still interrupt a multi-gigabyte copy mid-flight; stages
  make it resume, not prevent.
