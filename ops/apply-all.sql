-- ============================================================================
-- Auto Manager — ONE-FILE INSTALLER
-- Paste this whole file into Supabase -> SQL Editor -> Run. It is exactly the
-- migrations below, in filename order, concatenated by ops/build_apply_all.py
-- so the bundle and supabase/migrations/ can never drift apart:
--   0001_init.sql
--   0002_functions.sql
--   0003_control_bot.sql
--   0004_approved_captions.sql
--   0005_seasons_and_profile.sql
--   0006_source_declarations.sql
--   0007_inplace_captions.sql
--   0008_updates_channel.sql
--   0009_announcement_approved.sql
--
--
-- If you prefer the normal Supabase flow instead, apply the 9 files in
-- supabase/migrations/ in filename order (or run `supabase db push`).
-- Re-running this file is safe: every object is created IF NOT EXISTS or inside
-- an existence check.
-- ============================================================================

-- ============================================================================
-- Auto Manager — 0001_init.sql
-- Schema, enums, tables, constraints, indexes, RLS enablement.
--
-- Apply by pasting the whole file into Supabase → SQL Editor → Run, or with
-- `supabase db push` after `supabase link`. Idempotent where practical.
--
-- SECURITY MODEL (deliberate, read before changing):
--   * All tables live in schema `app`, never `public`, so the auto-generated
--     PostgREST Data API does not expose them.
--   * RLS is ENABLED on every table with NO policies for anon/authenticated.
--     That is default-deny: a leaked anon key sees nothing.
--   * The worker connects as `postgres`/`service_role`, which are table owner
--     or BYPASSRLS, so RLS is deliberately NOT `FORCE`-d — forcing it would
--     lock the application out of its own tables.
-- ============================================================================

create schema if not exists app;

-- ---------------------------------------------------------------------------
-- Enums. Wrapped so re-running the migration does not abort.
-- ---------------------------------------------------------------------------
-- The job_stage ladder below is a contract: the Python enum in app/stages.py
-- must match, and tests enforce that against this file.
do $enum$
declare
  specs text[] := array[
    'job_kind:ingest_media,thumbnail_screen,archive_media,storage_upload,link_verify,publish_post,edit_post,season_sticker,join_request_campaign,reconciliation,link_health_check',
    'job_status:queued,running,succeeded,failed,blocked,cancelled',
    'job_stage:discovered,thumbnail_checked,archived,sent_to_storage_bot,link_received,destination_posted,completed',
    'thumbnail_status:unchecked,clean,watermarked,ambiguous,review_required,owner_approved,owner_rejected',
    'variant_status:pending,archived,linked,published,failed,skipped,review',
    'episode_status:incomplete,complete,review,hold,published',
    'candidate_disposition:pending,accepted,rejected,superseded',
    'link_kind:single,batch,universal',
    'link_status:active,superseded,broken,revoked,unknown',
    'review_status:pending,approved,rejected,deferred',
    'channel_mode:full,monitor_only,ignore',
    'campaign_status:draft,ready,running,paused,completed,aborted',
    'contact_status:queued,sent,failed,skipped,already_contacted,restricted',
    'request_status:pending,approved,declined,expired'
  ];
  item      text;
  tname     text;
  raw_vals  text;
  lit_vals  text;
begin
  foreach item in array specs loop
    tname    := split_part(item, ':', 1);
    raw_vals := substring(item from position(':' in item) + 1);

    if exists (
      select 1 from pg_type t
      join pg_namespace n on n.oid = t.typnamespace
      where t.typname = tname and n.nspname = 'app'
    ) then
      continue;
    end if;

    select string_agg(quote_literal(v), ',' order by ord)
      into lit_vals
      from unnest(string_to_array(raw_vals, ',')) with ordinality as u(v, ord);

    execute 'create type app.' || quote_ident(tname) || ' as enum (' || lit_vals || ')';
  end loop;
end
$enum$;

-- ---------------------------------------------------------------------------
-- Configuration: operator-editable settings and templates. JSONB values mean a
-- template change never needs a migration.
-- ---------------------------------------------------------------------------
create table if not exists app.config (
  key           text primary key,
  value         jsonb not null,
  description   text,
  updated_at    timestamptz not null default now()
);

-- Singleton runtime state. Render's filesystem is ephemeral, so nothing that
-- must survive a restart may live on disk; the pause flag lives here so the
-- kill switch survives an instance replacement.
create table if not exists app.service_state (
  id                  int primary key default 1 check (id = 1),
  worker_id           text,
  started_at          timestamptz,
  heartbeat_at        timestamptz,
  paused              boolean not null default false,
  paused_reason       text,
  last_reconcile_at   timestamptz,
  app_version         text,
  constraint service_state_paused_reason check (
    paused or paused_reason is null
  )
);
insert into app.service_state (id) values (1) on conflict (id) do nothing;

-- ---------------------------------------------------------------------------
-- Catalogue
-- ---------------------------------------------------------------------------
create table if not exists app.series (
  id                bigint generated always as identity primary key,
  title             text not null,
  normalized_title  text not null unique,
  notes             text,
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  constraint series_title_len check (length(trim(title)) >= 1)
);

-- One private destination channel per complete series.
create table if not exists app.destination (
  id                       bigint generated always as identity primary key,
  series_id                bigint not null references app.series (id) on delete restrict,
  telegram_channel_id      bigint unique,
  title                    text,
  description              text,
  is_private               boolean not null default true,
  channel_help_added       boolean not null default false,
  channel_help_confirmed_at timestamptz,
  owner_promoted           boolean not null default false,
  temp_invite_revoked      boolean not null default false,
  created_at               timestamptz not null default now(),
  updated_at               timestamptz not null default now()
);

create table if not exists app.source_channel (
  id                   bigint generated always as identity primary key,
  series_id            bigint references app.series (id) on delete set null,
  destination_id       bigint references app.destination (id) on delete set null,
  telegram_channel_id  bigint not null unique,
  username             text,
  title                text,
  priority             int not null default 100,
  mode                 app.channel_mode not null default 'full',
  active               boolean not null default true,
  is_joined            boolean not null default false,
  joined_at            timestamptz,
  include_subbed       boolean not null default false,
  require_hindi_audio  boolean not null default true,
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),
  constraint source_channel_priority_range check (priority between 1 and 10000)
);
-- Priority order drives "look for channel 2 if channel 1 is watermarked".
create index if not exists source_channel_priority_ix
  on app.source_channel (destination_id, priority) where active;

create table if not exists app.archive_channel (
  id                   bigint generated always as identity primary key,
  telegram_channel_id  bigint not null unique,
  title                text,
  is_primary           boolean not null default true,
  created_at           timestamptz not null default now()
);

create table if not exists app.season (
  id                    bigint generated always as identity primary key,
  series_id             bigint not null references app.series (id) on delete cascade,
  season_number         int not null,
  first_episode         int,
  last_episode          int,
  sticker_label         text,
  sticker_document_id   text,
  sticker_pack_name     text,
  sticker_posted        boolean not null default false,
  sticker_message_id    bigint,
  created_at            timestamptz not null default now(),
  updated_at            timestamptz not null default now(),
  unique (series_id, season_number),
  constraint season_number_positive check (season_number >= 0),
  constraint season_episode_range check (
    first_episode is null or last_episode is null or last_episode >= first_episode
  )
);

-- ---------------------------------------------------------------------------
-- Episodes and variants. canonical_key is the deduplication contract; arrival
-- order never decides display order, quality_rank does.
-- ---------------------------------------------------------------------------
create table if not exists app.episode (
  id             bigint generated always as identity primary key,
  season_id      bigint not null references app.season (id) on delete cascade,
  episode_number int not null,
  canonical_key  text not null unique,
  title_hint     text,
  languages      text[] not null default '{}',
  audio_kind     text,
  status         app.episode_status not null default 'incomplete',
  review_reason  text,
  created_at     timestamptz not null default now(),
  updated_at     timestamptz not null default now(),
  unique (season_id, episode_number),
  constraint episode_number_positive check (episode_number >= 0),
  constraint episode_canonical_key_len check (length(canonical_key) between 4 and 512)
);
create index if not exists episode_status_ix on app.episode (status);

create table if not exists app.media_variant (
  id                  bigint generated always as identity primary key,
  episode_id          bigint not null references app.episode (id) on delete cascade,
  quality             text not null,
  quality_rank        int not null,
  release_variant     text,
  language_tag        text,
  status              app.variant_status not null default 'pending',
  thumbnail_status    app.thumbnail_status not null default 'unchecked',
  source_candidate_id bigint,
  archive_chat_id     bigint,
  archive_message_id  bigint,
  file_name           text,
  file_size_bytes     bigint,
  fingerprint         text,
  error               text,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),
  constraint variant_quality_len check (length(quality) between 2 and 32),
  constraint variant_size_positive check (file_size_bytes is null or file_size_bytes > 0)
);
-- "Same episode, quality and release variant already posted -> skip it."
create unique index if not exists media_variant_unique_uq
  on app.media_variant (episode_id, lower(quality), coalesce(lower(release_variant), ''));
create index if not exists media_variant_order_ix
  on app.media_variant (episode_id, quality_rank);
create index if not exists media_variant_fingerprint_ix
  on app.media_variant (fingerprint) where fingerprint is not null;

-- ---------------------------------------------------------------------------
-- Source candidates and thumbnail screening
-- ---------------------------------------------------------------------------
create table if not exists app.source_candidate (
  id                  bigint generated always as identity primary key,
  source_channel_id   bigint not null references app.source_channel (id) on delete cascade,
  message_id          bigint not null,
  media_idx           int not null default 0,
  media_type          text,
  file_name           text,
  raw_caption         text,
  parsed              jsonb not null default '{}',
  season_number       int,
  episode_number      int,
  language_tag        text,
  quality             text,
  quality_rank        int,
  file_size_bytes     bigint,
  fingerprint         text,
  thumbnail_status    app.thumbnail_status not null default 'unchecked',
  detected_handles    text[] not null default '{}',
  disposition         app.candidate_disposition not null default 'pending',
  reason              text,
  discovered_at       timestamptz not null default now(),
  unique (source_channel_id, message_id, media_idx)
);
create index if not exists source_candidate_lookup_ix
  on app.source_candidate (quality, episode_number, thumbnail_status);

create table if not exists app.thumbnail_review (
  id               bigint generated always as identity primary key,
  candidate_id     bigint not null unique references app.source_candidate (id) on delete cascade,
  detected_handles text[] not null default '{}',
  ocr_text         text,
  confidence       numeric(4,3),
  status           app.review_status not null default 'pending',
  owner_choice     text,
  decided_at       timestamptz,
  created_at       timestamptz not null default now(),
  constraint review_confidence_range check (confidence is null or confidence between 0 and 1)
);

-- Cross-source duplicate detection by media fingerprint, not filename.
create table if not exists app.dupe_fingerprint (
  fingerprint  text primary key,
  variant_id   bigint not null references app.media_variant (id) on delete cascade,
  created_at   timestamptz not null default now()
);

-- Idempotent discovery: a scan that re-reads history must not re-ingest.
create table if not exists app.processed_message (
  id                bigint generated always as identity primary key,
  source_channel_id bigint not null references app.source_channel (id) on delete cascade,
  message_id        bigint not null,
  created_at        timestamptz not null default now(),
  unique (source_channel_id, message_id)
);

-- ---------------------------------------------------------------------------
-- Links and destination posts
-- ---------------------------------------------------------------------------
create table if not exists app.storage_link (
  id              bigint generated always as identity primary key,
  url             text not null,
  kind            app.link_kind not null,
  episode_id      bigint references app.episode (id) on delete cascade,
  season_id       bigint references app.season (id) on delete cascade,
  destination_id  bigint references app.destination (id) on delete set null,
  batch_ref       text,
  active          boolean not null default true,
  link_status     app.link_status not null default 'active',
  checked_at      timestamptz,
  check_error     text,
  created_at      timestamptz not null default now(),
  superseded_at   timestamptz,
  constraint storage_link_target check (
    (kind = 'universal' and (episode_id is not null or season_id is not null))
    or kind in ('single','batch')
  ),
  constraint storage_link_url_check check (url ~ '^https?://')
);
-- One active universal link per episode, and per season, at a time.
create unique index if not exists storage_link_episode_universal_uq
  on app.storage_link (episode_id)
  where active and kind = 'universal' and episode_id is not null;
create unique index if not exists storage_link_season_universal_uq
  on app.storage_link (season_id)
  where active and kind = 'universal' and season_id is not null;

-- Telegram cannot insert a message into the middle of channel history, so each
-- episode owns exactly ONE permanent post that gets edited in place when a
-- missing quality arrives later. The partial unique index enforces that rule in
-- the database rather than by convention.
create table if not exists app.destination_post (
  id               bigint generated always as identity primary key,
  destination_id   bigint not null references app.destination (id) on delete cascade,
  kind             text not null check (kind in ('episode','season_batch','season_sticker','info')),
  episode_id       bigint references app.episode (id) on delete cascade,
  season_id        bigint references app.season (id) on delete cascade,
  message_id       bigint,
  channel_help_ref text,
  body             text,
  buttons          jsonb,
  quality_summary  jsonb,
  published_at     timestamptz,
  edited_at        timestamptz,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  constraint post_episode_needs_episode check (kind <> 'episode' or episode_id is not null),
  constraint post_season_batch_needs_season check (kind <> 'season_batch' or season_id is not null)
);
create unique index if not exists destination_post_episode_uq
  on app.destination_post (episode_id) where kind = 'episode';
create unique index if not exists destination_post_season_batch_uq
  on app.destination_post (season_id) where kind = 'season_batch';
create unique index if not exists destination_post_season_sticker_uq
  on app.destination_post (season_id) where kind = 'season_sticker';
create index if not exists destination_post_message_ix
  on app.destination_post (destination_id, message_id) where message_id is not null;

-- ---------------------------------------------------------------------------
-- Job queue: stage checkpoints, leases, retries
-- ---------------------------------------------------------------------------
create table if not exists app.job (
  id             bigint generated always as identity primary key,
  kind           app.job_kind not null,
  status         app.job_status not null default 'queued',
  stage          app.job_stage not null default 'discovered',
  dedup_key      text not null unique,
  episode_id     bigint references app.episode (id) on delete cascade,
  variant_id     bigint references app.media_variant (id) on delete cascade,
  candidate_id   bigint references app.source_candidate (id) on delete cascade,
  season_id      bigint references app.season (id) on delete cascade,
  destination_id bigint references app.destination (id) on delete cascade,
  payload        jsonb not null default '{}',
  priority       int not null default 100,
  attempts       int not null default 0,
  max_attempts   int not null default 8,
  next_attempt_at timestamptz not null default now(),
  locked_by      text,
  locked_until   timestamptz,
  last_error     text,
  result         jsonb,
  created_at     timestamptz not null default now(),
  started_at     timestamptz,
  finished_at    timestamptz,
  constraint job_priority_range check (priority between 1 and 1000),
  constraint job_attempts_check check (attempts >= 0 and max_attempts between 1 and 1000),
  -- A running job must carry a lease: that is what stops two processes
  -- uploading the same file after a Render restart.
  constraint job_lease_required check (
    status <> 'running' or (locked_by is not null and locked_until is not null)
  ),
  constraint job_finished_check check (
    status not in ('succeeded','failed','cancelled') or finished_at is not null
  )
);
create index if not exists job_claim_ix
  on app.job (priority, next_attempt_at, id)
  where status in ('queued','running');
create index if not exists job_episode_ix on app.job (episode_id) where episode_id is not null;
create index if not exists job_blocked_ix on app.job (status, kind) where status = 'blocked';

create table if not exists app.job_event (
  id         bigint generated always as identity primary key,
  job_id     bigint not null references app.job (id) on delete cascade,
  stage      app.job_stage,
  status     app.job_status,
  message    text,
  data       jsonb not null default '{}',
  created_at timestamptz not null default now()
);
create index if not exists job_event_job_ix on app.job_event (job_id, id);

-- ---------------------------------------------------------------------------
-- Join requests and campaigns.
--
-- The two CHECK constraints below are the schema-level encoding of a promise
-- from the spec: messaging a requester never approves or declines them, and no
-- user can be spammed twice by the same campaign.
-- ---------------------------------------------------------------------------
create table if not exists app.join_request (
  id                bigint generated always as identity primary key,
  destination_id    bigint not null references app.destination (id) on delete cascade,
  telegram_user_id  bigint not null,
  user_name         text,
  request_ref       text,
  status            app.request_status not null default 'pending',
  requested_at      timestamptz,
  resolved_at       timestamptz,
  raw               jsonb not null default '{}',
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now(),
  unique (destination_id, telegram_user_id, request_ref)
);
create index if not exists join_request_pending_ix
  on app.join_request (destination_id, requested_at) where status = 'pending';

create table if not exists app.join_campaign (
  id                       bigint generated always as identity primary key,
  destination_id           bigint not null references app.destination (id) on delete cascade,
  name                     text not null,
  message_template         text not null,
  status                   app.campaign_status not null default 'draft',
  rate_per_hour            int not null default 20,
  per_message_delay_seconds numeric(6,2) not null default 3,
  confirm_required         boolean not null default true,
  approve_after_send       boolean not null default false,
  started_at               timestamptz,
  finished_at              timestamptz,
  created_at               timestamptz not null default now(),
  updated_at               timestamptz not null default now(),
  constraint campaign_rate_check check (rate_per_hour between 1 and 500),
  constraint campaign_delay_check check (per_message_delay_seconds >= 0),
  constraint campaign_template_len check (length(trim(message_template)) >= 1),
  -- HARD RULE: sending a DM must never be coupled to approval/decline.
  constraint campaign_never_approves check (approve_after_send is not true)
);

create table if not exists app.join_campaign_contact (
  campaign_id       bigint not null references app.join_campaign (id) on delete cascade,
  telegram_user_id  bigint not null,
  status            app.contact_status not null default 'queued',
  attempts          int not null default 0,
  error             text,
  flood_wait_until  timestamptz,
  queued_at         timestamptz not null default now(),
  sent_at           timestamptz,
  primary key (campaign_id, telegram_user_id),
  constraint contact_attempts_check check (attempts between 0 and 100)
);
create index if not exists campaign_contact_pending_ix
  on app.join_campaign_contact (campaign_id, status);

create table if not exists app.reply_log (
  id                bigint generated always as identity primary key,
  campaign_id       bigint references app.join_campaign (id) on delete set null,
  telegram_user_id  bigint not null,
  telegram_message_id bigint,
  body              text,
  forwarded_to_main boolean not null default false,
  forwarded_at      timestamptz,
  created_at        timestamptz not null default now()
);

create table if not exists app.audit_log (
  id             bigint generated always as identity primary key,
  actor_user_id  bigint,
  action         text not null,
  entity_type    text,
  entity_id      bigint,
  detail         jsonb not null default '{}',
  created_at     timestamptz not null default now()
);
create index if not exists audit_log_recent_ix on app.audit_log (created_at desc);

-- ---------------------------------------------------------------------------
-- Row Level Security: enabled everywhere, zero policies for API roles.
-- ---------------------------------------------------------------------------
do $rls$
declare
  t text;
begin
  foreach t in array array[
    'config','service_state','series','destination','source_channel','archive_channel',
    'season','episode','media_variant','source_candidate','thumbnail_review',
    'dupe_fingerprint','processed_message','storage_link','destination_post',
    'job','job_event','join_request','join_campaign','join_campaign_contact',
    'reply_log','audit_log'
  ]
  loop
    execute format('alter table app.%I enable row level security', t);
  end loop;
end
$rls$;

-- ============================================================================
-- Auto Manager — 0002_functions.sql
-- Triggers, queue functions, derived views, grants, default configuration.
-- Run immediately after 0001_init.sql.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- updated_at maintenance
-- ---------------------------------------------------------------------------
create or replace function app.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
  -- Guarded, because this trigger is attached generically and not every table
  -- in app has an updated_at column (storage_link, for one).
  if to_jsonb(new) ? 'updated_at' then
    new.updated_at := now();
  end if;
  return new;
end;
$$;

do $triggers$
declare
  tbl text;
begin
  foreach tbl in array array[
    'series','destination','source_channel','season','episode','media_variant',
    'storage_link','destination_post','join_request','join_campaign'
  ]
  loop
    if not exists (
      select 1 from information_schema.columns
      where table_schema = 'app' and table_name = tbl and column_name = 'updated_at'
    ) then
      continue;
    end if;

    if not exists (
      select 1 from pg_trigger tg
      join pg_class c on c.oid = tg.tgrelid
      join pg_namespace n on n.oid = c.relnamespace
      where tg.tgname = 'set_updated_at' and n.nspname = 'app' and c.relname = tbl
    ) then
      execute format(
        'create trigger set_updated_at before update on app.%I
           for each row execute function app.touch_updated_at()', tbl);
    end if;
  end loop;
end
$triggers$;

-- ---------------------------------------------------------------------------
-- Stage machine. Kept in SQL so the database rejects a checkpoint that would
-- skip or rewind a stage, independently of whatever the Python client does.
-- ---------------------------------------------------------------------------
create or replace function app.stage_rank(p_stage app.job_stage)
returns int
language sql
immutable
as $$
  select case p_stage
    when 'discovered'          then 1
    when 'thumbnail_checked'   then 2
    when 'archived'            then 3
    when 'sent_to_storage_bot' then 4
    when 'link_received'       then 5
    when 'destination_posted'  then 6
    when 'completed'           then 7
  end;
$$;

create or replace function app.stage_is_valid_transition(
  p_from app.job_stage,
  p_to   app.job_stage
)
returns boolean
language sql
immutable
as $$
  -- Forward by at most one step, or stay put (a retry must be replayable).
  select app.stage_rank(p_to) - app.stage_rank(p_from) between 0 and 1;
$$;

-- ---------------------------------------------------------------------------
-- Queue primitives
-- ---------------------------------------------------------------------------

-- Idempotent enqueue. Re-scanning a source channel after a restart is safe:
-- an existing dedup_key is returned instead of creating a duplicate job.
create or replace function app.enqueue_job(
  p_kind       app.job_kind,
  p_dedup_key  text,
  p_stage      app.job_stage default 'discovered',
  p_payload    jsonb default '{}'::jsonb,
  p_priority   int default 100,
  p_episode_id bigint default null,
  p_variant_id bigint default null,
  p_candidate_id bigint default null,
  p_season_id  bigint default null,
  p_destination_id bigint default null
)
returns app.job
language sql
as $$
  insert into app.job as j (
    kind, dedup_key, stage, payload, priority,
    episode_id, variant_id, candidate_id, season_id, destination_id
  ) values (
    p_kind, p_dedup_key, p_stage, coalesce(p_payload, '{}'::jsonb), p_priority,
    p_episode_id, p_variant_id, p_candidate_id, p_season_id, p_destination_id
  )
  on conflict (dedup_key) do nothing
  returning j.*;
$$;

-- Lease-based claim. `for update skip locked` means two overlapping processes
-- (e.g. an old instance still draining while a new one boots) never receive
-- the same row. Expired leases are reclaimed by release_expired_locks().
create or replace function app.claim_next_job(
  p_worker         text,
  p_lease_seconds  int default 120
)
returns app.job
language plpgsql
security definer
set search_path = app, pg_temp
as $$
declare
  j app.job%rowtype;
begin
  if p_lease_seconds < 5 or p_lease_seconds > 3600 then
    raise exception 'p_lease_seconds must be between 5 and 3600';
  end if;

  -- Respect the operator kill switch: claim nothing while paused.
  if exists (select 1 from app.service_state s where s.id = 1 and s.paused) then
    return null;
  end if;

  select row_for_update.* into j
  from app.job row_for_update
  where row_for_update.status in ('queued','running')
    and row_for_update.next_attempt_at <= now()
    and (row_for_update.locked_until is null or row_for_update.locked_until < now())
    and row_for_update.attempts < row_for_update.max_attempts
    -- An episode the owner put on hold is not processed.
    and not exists (
      select 1 from app.episode e
      where e.id = row_for_update.episode_id and e.status in ('hold','review')
    )
  order by row_for_update.priority, row_for_update.next_attempt_at, row_for_update.id
  for update of row_for_update
  skip locked
  limit 1;

  if not found then
    return null;
  end if;

  update app.job
     set status      = 'running',
         locked_by   = p_worker,
         locked_until = now() + make_interval(secs => p_lease_seconds),
         attempts    = attempts + 1,
         started_at  = coalesce(started_at, now())
   where id = j.id
  returning * into j;

  return j;
end;
$$;

-- Persist a completed stage immediately; this is what makes a Render restart
-- resume instead of restart.
create or replace function app.checkpoint_job(
  p_job_id bigint,
  p_stage  app.job_stage,
  p_data   jsonb default '{}'::jsonb,
  p_message text default null
)
returns app.job
language plpgsql
as $$
declare
  j app.job%rowtype;
begin
  select * into j from app.job where id = p_job_id for update;
  if not found then
    raise exception 'job % not found', p_job_id;
  end if;

  if not app.stage_is_valid_transition(j.stage, p_stage) then
    raise exception 'invalid stage transition for job %: % -> %', p_job_id, j.stage, p_stage;
  end if;

  update app.job set stage = p_stage where id = p_job_id returning * into j;

  insert into app.job_event (job_id, stage, status, message, data)
  values (p_job_id, p_stage, j.status, p_message, coalesce(p_data, '{}'::jsonb));

  return j;
end;
$$;

create or replace function app.complete_job(
  p_job_id bigint,
  p_result jsonb default '{}'::jsonb
)
returns app.job
language plpgsql
as $$
declare
  j app.job%rowtype;
begin
  update app.job
     set status = 'succeeded', stage = 'completed', result = coalesce(p_result, '{}'::jsonb),
         last_error = null, locked_by = null, locked_until = null, finished_at = now()
   where id = p_job_id
  returning * into j;

  if not found then
    raise exception 'job % not found', p_job_id;
  end if;

  insert into app.job_event (job_id, stage, status, message, data)
  values (p_job_id, 'completed', 'succeeded', 'completed', coalesce(p_result, '{}'::jsonb));

  return j;
end;
$$;

-- Retry with backoff, or block once the budget is spent. A blocked job is
-- visible to /status and never silently dropped.
create or replace function app.fail_job(
  p_job_id       bigint,
  p_error        text,
  p_retry_after  int default 60
)
returns app.job
language plpgsql
as $$
declare
  j app.job%rowtype;
begin
  update app.job
     set status = (case when attempts >= max_attempts then 'blocked' else 'failed' end)::app.job_status,
         last_error = left(coalesce(p_error, 'unknown error'), 4000),
         locked_by = null,
         locked_until = null,
         next_attempt_at = case
           when attempts >= max_attempts then now() + interval '1 hour'
           else now() + make_interval(secs => greatest(5, p_retry_after * power(2, attempts - 1)::int))
         end,
         finished_at = now()
   where id = p_job_id
  returning * into j;

  if not found then
    raise exception 'job % not found', p_job_id;
  end if;

  insert into app.job_event (job_id, stage, status, message)
  values (p_job_id, j.stage, j.status, j.last_error);

  return j;
end;
$$;

-- Called on boot and periodically: a process died mid-job, so its lease is
-- stale. Reset it to queued while keeping its stage, which is what makes
-- resume-from-last-checkpoint work.
create or replace function app.release_expired_locks()
returns bigint
language sql
as $$
  with reclaimed as (
    update app.job
       set status = 'queued', locked_by = null, locked_until = null,
           next_attempt_at = now()
     where status = 'running' and locked_until < now()
    returning 1
  )
  select count(*) from reclaimed;
$$;

create or replace function app.set_pause(p_paused boolean, p_reason text default null)
returns app.service_state
language plpgsql
as $$
declare
  s app.service_state%rowtype;
begin
  if not p_paused then
    p_reason := null;
  end if;
  update app.service_state
     set paused = p_paused, paused_reason = p_reason
   where id = 1
  returning * into s;
  return s;
end;
$$;

create or replace function app.record_heartbeat(p_worker text)
returns app.service_state
language sql
as $$
  update app.service_state
     set heartbeat_at = now(), worker_id = p_worker,
         started_at = coalesce(started_at, now())
   where id = 1
  returning *;
$$;

-- ---------------------------------------------------------------------------
-- Dedup helpers
-- ---------------------------------------------------------------------------

create or replace function app.find_existing_variant(
  p_episode_id bigint,
  p_quality    text,
  p_release_variant text default null
)
returns app.media_variant
language sql
stable
as $$
  select v.* from app.media_variant v
  where v.episode_id = p_episode_id
    and lower(v.quality) = lower(p_quality)
    and coalesce(lower(v.release_variant), '') = coalesce(lower(p_release_variant), '')
  limit 1;
$$;

create or replace function app.is_seen_fingerprint(p_fingerprint text)
returns boolean
language sql
stable
as $$
  select exists (select 1 from app.dupe_fingerprint f where f.fingerprint = p_fingerprint);
$$;

-- ---------------------------------------------------------------------------
-- Campaign guards. These are enforced here as well as in code so that no
-- future feature can accidentally bypass them.
-- ---------------------------------------------------------------------------

-- (campaign_id, user_id) is the primary key of join_campaign_contact, so a
-- duplicate send is impossible at the storage layer. This checks the same rule
-- from the sender side and reports why a user must be skipped.
create or replace function app.contact_allowed(
  p_campaign_id bigint,
  p_user_id     bigint
)
returns table (allowed boolean, reason text)
language plpgsql
stable
as $$
declare
  c app.join_campaign%rowtype;
  k app.join_campaign_contact%rowtype;
  sent_last_hour bigint;
begin
  select * into c from app.join_campaign where id = p_campaign_id;
  if not found then
    return query select false, 'campaign_not_found';
    return;
  end if;

  if c.status <> 'running' then
    return query select false, 'campaign_not_running';
    return;
  end if;

  if exists (select 1 from app.service_state s where s.id = 1 and s.paused) then
    return query select false, 'service_paused';
    return;
  end if;

  select * into k from app.join_campaign_contact
   where campaign_id = p_campaign_id and telegram_user_id = p_user_id;

  if found and k.status in ('sent','already_contacted') then
    return query select false, 'duplicate_user';
    return;
  end if;

  if found and k.flood_wait_until is not null and k.flood_wait_until > now() then
    return query select false, 'flood_wait_active';
    return;
  end if;

  select count(*) into sent_last_hour
    from app.join_campaign_contact k2
   where k2.campaign_id = p_campaign_id
     and k2.sent_at > now() - interval '1 hour';

  if sent_last_hour >= c.rate_per_hour then
    return query select false, 'hourly_rate_exhausted';
    return;
  end if;

  return query select true, 'ok';
end;
$$;

-- ---------------------------------------------------------------------------
-- Views (the "database manifest" that decides display order)
-- ---------------------------------------------------------------------------
create or replace view app.v_episode_manifest as
  select
    e.id                      as episode_id,
    e.canonical_key,
    s.series_id,
    ser.title                 as series_title,
    s.season_number,
    e.episode_number,
    e.status                  as episode_status,
    count(v.*)                as variant_count,
    coalesce(
      jsonb_agg(
        jsonb_build_object(
          'variant_id', v.id,
          'quality', v.quality,
          'status', v.status::text,
          'thumbnail_status', v.thumbnail_status::text,
          'archive_message_id', v.archive_message_id,
          'release_variant', v.release_variant
        ) order by v.quality_rank, v.id
      ) filter (where v.id is not null),
      '[]'::jsonb
    ) as qualities
  from app.episode e
  join app.season s    on s.id = e.season_id
  join app.series ser  on ser.id = s.series_id
  left join app.media_variant v on v.episode_id = e.id
  group by e.id, e.canonical_key, s.series_id, ser.title, s.season_number,
           e.episode_number, e.status;

-- Drives the "1080p appeared months later" flow: an episode is never closed.
-- season_complete requires both full episode coverage and at least one
-- archived file per episode, so an empty or half-ingested season never triggers
-- a "Complete Season" batch post.
create or replace view app.v_season_coverage as
  select
    s.id                                            as season_id,
    ser.title,
    s.season_number,
    count(distinct e.id)                            as episodes,
    count(distinct e.id) filter (where v.id is not null) as episodes_with_files,
    count(distinct v.id)                            as archived_variants,
    count(distinct v.id) filter (where v.status = 'published') as published_variants,
    bool_and(
      exists (
        select 1 from app.media_variant q
        where q.episode_id = e.id and lower(q.quality) = '480p'
      )
    ) as every_episode_has_480p,
    max(s.last_episode)                             as declared_last_episode,
    (s.last_episode is not null
      and count(distinct e.id) = s.last_episode - coalesce(s.first_episode, 1) + 1
      and count(distinct e.id) filter (where v.id is not null) = count(distinct e.id)) as season_complete
  from app.season s
  join app.series ser on ser.id = s.series_id
  left join app.episode e on e.season_id = s.id
  left join app.media_variant v on v.episode_id = e.id
  group by s.id, ser.title, s.season_number, s.last_episode, s.first_episode;

create or replace view app.v_queue_health as
  select
    count(*) filter (where j.status = 'queued')  as queued,
    count(*) filter (where j.status = 'running') as running,
    count(*) filter (where j.status = 'blocked') as blocked,
    count(*) filter (where j.status = 'failed')  as failed,
    count(*) filter (
      where j.status = 'succeeded' and j.finished_at > now() - interval '1 hour'
    ) as succeeded_1h,
    extract(epoch from (now() - min(j.next_attempt_at) filter (where j.status = 'queued')))
      as oldest_queued_age_seconds,
    count(*) filter (
      where j.status = 'running' and j.locked_until < now()
    ) as expired_leases
  from app.job j;

create or replace view app.v_campaign_progress as
  select
    c.id                                        as campaign_id,
    c.name,
    c.status,
    c.rate_per_hour,
    count(k.*)                                  as contacts,
    count(k.*) filter (where k.status = 'sent') as sent,
    count(k.*) filter (where k.status = 'queued') as pending,
    count(k.*) filter (where k.status = 'failed')  as failed,
    count(k.*) filter (where k.status = 'skipped') as skipped,
    count(k.*) filter (where k.status = 'restricted') as restricted,
    max(k.sent_at)                              as last_sent_at,
    greatest(
      0,
      c.rate_per_hour - count(k.*) filter (
        where k.sent_at > now() - interval '1 hour'
      )
    ) as remaining_this_hour
  from app.join_campaign c
  left join app.join_campaign_contact k on k.campaign_id = c.id
  group by c.id;

-- ---------------------------------------------------------------------------
-- Grants: service_role may operate the queue; API roles get nothing.
-- ---------------------------------------------------------------------------
-- Supabase's API roles (anon, authenticated, service_role) do not exist on a
-- generic PostgreSQL server, and a GRANT naming an absent role aborts the whole
-- installer. So each grant is applied only where the role is present: on
-- Supabase everything below runs; on Render's own Postgres or a bare cluster
-- the schema still installs, and the connecting role works through ownership.
-- (PostgreSQL has no "ALL VIEWS" grant target; ALL TABLES already covers views.)
do $grants$
declare
  rname text;
  roles text[];
begin
  select coalesce(array_agg(rolname), '{}'::text[]) into roles
    from pg_roles
   where rolname in ('anon','authenticated','service_role');

  foreach rname in array roles loop
    if rname = 'service_role' then
      execute 'grant usage on schema app to service_role';
      execute 'grant select, insert, update, delete on all tables in schema app to service_role';
      execute 'grant usage, select on all sequences in schema app to service_role';
      execute 'grant execute on all functions in schema app to service_role';
    else
      execute format('revoke all on all tables in schema app from %I', rname);
      execute format('revoke all on all sequences in schema app from %I', rname);
      execute format('revoke execute on all functions in schema app from %I', rname);
      execute format('revoke usage on schema app from %I', rname);
    end if;
  end loop;

  -- Belt and braces for the public role, which always exists.
  execute 'revoke all on all tables in schema app from public';
  execute 'revoke execute on all functions in schema app from public';
end
$grants$;

-- ---------------------------------------------------------------------------
-- Default configuration. Editable in the dashboard; no migration needed.
-- ---------------------------------------------------------------------------
insert into app.config (key, value, description) values
  ('branding.primary_handles', '["ycanime","india_crunchyroll"]',
   'Both handles are primary and always appear together.'),
  ('branding.footer', '"@ycanime | @india_crunchyroll"',
   'Replacement text for disallowed usernames in editable captions.'),
  ('branding.filename_separator', '"_"',
   'Unsupported filename characters (e.g. the | in the handle pair) are replaced.'),
  ('quality.order', '["360p","480p","720p","1080p","2160p"]',
   'Display order. Arrival order never controls it.'),
  ('ingest.include_subbed_only', 'false', 'Subbed-only releases are excluded.'),
  ('ingest.require_hindi_audio', 'true', 'Hindi plus other languages is still in scope.'),
  ('thumbnail.strict_mode', 'true', 'Uncertain thumbnails are never published.'),
  ('thumbnail.on_no_clean_candidate', '"ask_owner"',
   'ask_owner | wait_and_rescan | manual_select | skip_quality'),
  ('destination.visibility', '"private"', 'Private by default.'),
  ('destination.name_template', '"{TITLE} Anime in Hindi"',
   'Only when source channel name and file metadata agree.'),
  ('destination.description_template',
   '"Watch or download {TITLE} in Hindi. Available seasons, episodes, and qualities are organized below.\nUpdates: @ycanime | @india_crunchyroll"',
   'Suggested default; editable.'),
  ('destination.channel_per_series', 'true', 'One destination per complete series.'),
  ('destination.auto_create', 'true', 'No interactive confirmation when signals agree.'),
  ('destination.keep_individual_and_batch', 'true',
   'Season batch post is added; individual episode posts are never deleted.'),
  ('bots.storage_username', '"@anime_hindifilesbot"', 'File storage / link generator.'),
  ('bots.channel_help_username', '"@chelpbot"', 'Destination post publisher only.'),
  ('stickers.pack_url', '"https://t.me/addstickers/OCtbqTQ_by_sticbot"', 'Approved pack.'),
  ('stickers.mapping_mode', '"auto_detect_then_ask"',
   'Recognize S1/Season 1 labels; ambiguous ones are asked once and remembered.'),
  ('templates.archive_caption',
   '"🎬 {title}\n📺 Season {season} • Episode {episode}\n🎙 Audio: {languages}\n💾 Quality: {quality}\n\n@ycanime | @india_crunchyroll\n\n#{title_tag} #S01E01 #{quality_tag}"',
   'Temporary default.'),
  ('templates.episode_post',
   '"🎬 {title}\n\n📺 Season {season} • Episode {episode}\n🎙 Available in Hindi\n💾 Qualities: {quality_list}\n\nChoose the button below to get this episode.\n\n@ycanime | @india_crunchyroll"',
   'Temporary default.'),
  ('templates.episode_button', '"📥 Get Episode {episode} - {storage_link}"',
   'Channel Help button syntax: text - url'),
  ('templates.season_post',
   '"🎬 {title} — Season {season} Complete\n\n📺 Episodes: {first_episode}–{last_episode}\n🎙 Available in Hindi\n💾 Qualities: {quality_summary}\n\nChoose the button below to get the complete season.\n\n@ycanime | @india_crunchyroll"',
   'Temporary default.'),
  ('templates.season_button', '"📥 Get Complete Season {season} - {storage_link}"', null),
  ('campaign.mode', '"command_triggered"',
   'Automatic | command_triggered. Default is owner-triggered campaigns.'),
  ('campaign.rate_per_hour', '20', 'Conservative default; not tuned to evade enforcement.'),
  ('campaign.forward_replies_to_main', 'true', 'Replies from contacted users go to the main account.'),
  ('jobs.max_attempts', '8', 'Then the job blocks and the owner is alerted.'),
  ('worker.lease_seconds', '120', 'Stale leases are reclaimed after restart.')
on conflict (key) do nothing;

-- ============================================================================
-- 0003_control_bot.sql — sessions the control bot can log in, and the settings
-- that make that safe.
--
-- Why a table at all: the operator is not going to run Python on a laptop to
-- produce a session string, and a session string must never be pasted into a
-- chat. So the service performs the login itself, over Telegram, and stores the
-- result here — behind RLS with zero policies, reachable only by the roles that
-- already hold the database password.
--
-- The string is stored in plain text on purpose. There is no second secret to
-- encrypt it with that would not itself live in this same deployment; pretending
-- otherwise would add a key-management problem while providing no real
-- protection. The controls that matter are: nothing here is exposed to the
-- public API roles, the value is never sent back over the bot, and revoking a
-- session is one command (/forget) plus Telegram's own "terminate all sessions".
-- ============================================================================

create table if not exists app.telegram_session (
  name            text primary key,
  kind            text not null default 'user',
  session_string  text not null,
  account_id      bigint,
  username        text,
  active          boolean not null default false,
  created_at      timestamptz not null default now(),
  last_used_at    timestamptz,
  note            text,
  constraint telegram_session_name_shape check (name ~ '^[a-z0-9][a-z0-9_-]{0,39}$'),
  constraint telegram_session_kind_check check (kind in ('user','bot')),
  -- A session string is ~350 chars of base64-ish text. Anything shorter is a
  -- paste mistake, and a truncated session fails later in a confusing way.
  constraint telegram_session_length check (length(session_string) between 64 and 4096)
);

-- Exactly one *active* session per kind: two live user sessions means two
-- workers claiming jobs and double-posting the same episode.
create unique index if not exists telegram_session_one_active_uq
  on app.telegram_session (kind) where active;
create index if not exists telegram_session_kind_ix on app.telegram_session (kind, active);

comment on column app.telegram_session.session_string is
  'MTProto StringSession. Account-equivalent: reading it is owning the account. Never selected into a bot reply or a log line.';

-- Same posture as every other table in this schema: RLS on, zero policies, so
-- the exposed API roles cannot read it even by guessing the name.
alter table app.telegram_session enable row level security;

do $grants$
declare
  rname text;
  roles text[];
begin
  select coalesce(array_agg(rolname), '{}'::text[]) into roles
    from pg_roles
   where rolname in ('anon','authenticated','service_role');

  foreach rname in array roles loop
    if rname = 'service_role' then
      execute 'grant select, insert, update, delete on table app.telegram_session to service_role';
    else
      execute format('revoke all on table app.telegram_session from %I', rname);
      execute format('revoke usage on schema app from %I', rname);
    end if;
  end loop;
end
$grants$;

insert into app.config (key, value, description) values
  ('bot.allow_login', 'true',
   'Let the control bot run the MTProto login flow (phone, code, 2FA) and store the resulting session here. Turn off once the account is connected.'),
  ('bot.login_ttl_seconds', '600',
   'How long a pending login may wait for its code before the attempt is discarded. Codes expire on Telegram''s side anyway; this stops a half-finished attempt lingering in memory.'),
  ('bot.delete_sensitive', 'true',
   'Delete the operator''s own messages containing the phone number, code and 2FA password after use. Best effort: the operator should delete them too.'),
  ('bot.enabled_commands',
   '["help","status","pause","resume","reconcile","probe","sessions","use","forget","login","code","password","cancel"]',
   'Commands the bot answers. Unknown names are ignored rather than echoed back.')
on conflict (key) do nothing;

-- ============================================================================
-- 0004_approved_captions.sql — the caption formats the operator dictated.
--
-- app.config used to carry placeholder captions marked "Temporary default.". This
-- migration replaces them with the approved text, character for character, with
-- three deliberate choices recorded here rather than hidden in a diff:
--
--   1. The box lines are single-newline separated. The samples arrived double
--      spaced, and a `╭ ┣ ┣ ╰` frame only reads as a frame when the strokes are
--      adjacent.
--   2. `{title_full}` is one stored value ("Title: Subtitle") used by both the
--      archive caption and the destination post, so the private archive can never
--      disagree with the public channel about how a series is spelled. The two
--      samples differed (": The earthbound mole" against ": the earthbound mole");
--      the capitalisation stored on the series is what gets published.
--   3. `{season}` is bare in the destination box and zero-padded in the archive
--      line, because that is what each sample showed. Padding one and tidying the
--      other would change published text on a guess.
--
-- `app.series.subtitle` (added below) holds the alternate title. The source scanner
-- fills it when that handler lands, and nothing may depend on it being present: an
-- absent subtitle drops the colon and the second half instead of inventing one.
--
-- Each statement replaces its row only while that row still holds the exact
-- placeholder 0002 shipped — the previous value is named in the WHERE clause — so
-- re-applying ops/apply-all.sql can never overwrite a caption you have edited.
--
-- The strings are asserted equal to app.captions.APPROVED_TEMPLATES by
-- tests/test_caption_templates.py; regenerate this file with
-- `python ops/build_caption_migration.py && python ops/build_apply_all.py` after
-- editing a template, and CI's --check fails if either step was skipped.
-- ============================================================================

alter table app.series add column if not exists subtitle text;

comment on column app.series.subtitle is
  'Alternate/English title as it appears in the source, e.g. "The earthbound mole". '
  'Never guessed, never translated: the caption prints it or omits it.';

-- branding.footer
insert into app.config (key, value, description) values
  ('branding.footer',
   '"@YCAnime | @India_crunchyroll"',
   'Display casing of the signature that replaces a foreign handle in an edited caption. Matching stays casefolded against branding.primary_handles; the underscore in @YC_Anime was a typo, corrected by the operator 2026-08-28.')
on conflict (key) do update set
  value = excluded.value,
  description = excluded.description,
  updated_at = now()
 where app.config.value = '"@ycanime | @india_crunchyroll"'::jsonb;  -- only replaces the placeholder 0002 shipped

-- templates.archive_caption
insert into app.config (key, value, description) values
  ('templates.archive_caption',
   '"‣ {title_full} (S - {season})\n\n╭────────────────────\n┣Quality: {quality}\n┣Episode: {episode}\n┣Audio: {audio} #O𝖿𝖿𝗂𝖼𝗂𝖺𝗅\n╰────────────────────\n\n‣ Powered By: @india_crunchyroll\n@YCAnime"',
   'Approved 2026-08-28 from the operator''s sample: title line, light box with Quality/Episode/Audio, then the two handles. Editable here.')
on conflict (key) do update set
  value = excluded.value,
  description = excluded.description,
  updated_at = now()
 where app.config.value = '"🎬 {title}\n📺 Season {season} • Episode {episode}\n🎙 Audio: {languages}\n💾 Quality: {quality}\n\n@ycanime | @india_crunchyroll\n\n#{title_tag} #S01E01 #{quality_tag}"'::jsonb;  -- only replaces the placeholder 0002 shipped

-- templates.episode_post
insert into app.config (key, value, description) values
  ('templates.episode_post',
   '"✦ {title_full} ✦\n\n╔━━━━━━━━━━━━━━━━━━━━━╗\n⌲ 𝗦𝗲𝗮𝘀𝗼𝗻: {season}\n❍ 𝗘𝗽𝗶𝘀𝗼𝗱𝗲: {episode}\n〄 𝗔𝘂𝗱𝗶𝗼: {audio}\n◎ 𝗧𝗼𝘁𝗮𝗹 𝗘𝗽𝗶𝘀𝗼𝗱𝗲𝘀: {total_episodes}\n♡ 𝗣𝗼𝘄𝗲𝗿𝗲𝗱 𝗯𝘆: @YCAnime , @India_crunchyroll\n╚━━━━━━━━━━━━━━━━━━━━━╝"',
   'Approved 2026-08-28 from the operator''s sample: heavy box, season bare, episode zero-padded, total episodes, footer handles. Editable here.')
on conflict (key) do update set
  value = excluded.value,
  description = excluded.description,
  updated_at = now()
 where app.config.value = '"🎬 {title}\n\n📺 Season {season} • Episode {episode}\n🎙 Available in Hindi\n💾 Qualities: {quality_list}\n\nChoose the button below to get this episode.\n\n@ycanime | @india_crunchyroll"'::jsonb;  -- only replaces the placeholder 0002 shipped

-- templates.season_post
insert into app.config (key, value, description) values
  ('templates.season_post',
   '"✦ {title_full} ✦\n\n╔━━━━━━━━━━━━━━━━━━━━━╗\n⌲ 𝗦𝗲𝗮𝘀𝗼𝗻: {season}\n❍ 𝗘𝗽𝗶𝘀𝗼𝗱𝗲: {episode_range}\n〄 𝗔𝘂𝗱𝗶𝗼: {audio}\n◎ 𝗧𝗼𝘁𝗮𝗹 𝗘𝗽𝗶𝘀𝗼𝗱𝗲𝘀: {total_episodes}\n♡ 𝗣𝗼𝘄𝗲𝗿𝗲𝗱 𝗯𝘆: @YCAnime , @India_crunchyroll\n╚━━━━━━━━━━━━━━━━━━━━━╝"',
   'Approved 2026-08-28: the same box as the episode post with ''{episode_range}'' instead of ''{episode}''. Editable here.')
on conflict (key) do update set
  value = excluded.value,
  description = excluded.description,
  updated_at = now()
 where app.config.value = '"🎬 {title} — Season {season} Complete\n\n📺 Episodes: {first_episode}–{last_episode}\n🎙 Available in Hindi\n💾 Qualities: {quality_summary}\n\nChoose the button below to get the complete season.\n\n@ycanime | @india_crunchyroll"'::jsonb;  -- only replaces the placeholder 0002 shipped

-- templates.episode_button
insert into app.config (key, value, description) values
  ('templates.episode_button',
   '"❐ 𝗪𝗮𝘁𝗰𝗵/𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱 ❐ - {storage_link}"',
   'Channel Help button syntax is ''text - url''. One label, exactly as approved, used when a post has a single link.')
on conflict (key) do update set
  value = excluded.value,
  description = excluded.description,
  updated_at = now()
 where app.config.value = '"📥 Get Episode {episode} - {storage_link}"'::jsonb;  -- only replaces the placeholder 0002 shipped

-- templates.episode_button_multi
insert into app.config (key, value, description) values
  ('templates.episode_button_multi',
   '"❐ 𝗪𝗮𝘁𝗰𝗵/𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱 {quality} ❐ - {storage_link}"',
   'Used when a post offers more than one quality: the quality is named, because four identical buttons on a post that has 480p through 2160p stopped describing the file.')
on conflict (key) do nothing;  -- new key: a value you set yourself always wins

-- templates.season_button
insert into app.config (key, value, description) values
  ('templates.season_button',
   '"❐ 𝗪𝗮𝘁𝗰𝗵/𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱 ❐ - {storage_link}"',
   'The batch post''s button: the universal link, same label as an episode link.')
on conflict (key) do update set
  value = excluded.value,
  description = excluded.description,
  updated_at = now()
 where app.config.value = '"📥 Get Complete Season {season} - {storage_link}"'::jsonb;  -- only replaces the placeholder 0002 shipped

-- caption.button_rows
insert into app.config (key, value, description) values
  ('caption.button_rows',
   '"one_per_line"',
   'one_per_line | pair. ''pair'' joins buttons with && so two links share a row; one_per_line gives every quality its own row.')
on conflict (key) do nothing;  -- new key: a value you set yourself always wins

-- caption.total_episodes_unknown
insert into app.config (key, value, description) values
  ('caption.total_episodes_unknown',
   '"TBA"',
   'Printed instead of a number when the season''s length was never stated. Never inferred from the highest episode seen so far: that would promise a completion nobody observed.')
on conflict (key) do nothing;  -- new key: a value you set yourself always wins

-- ============================================================================
-- 0005_seasons_and_profile.sql — season boundaries, and what a channel shows.
--
-- Three gaps, each found by reading the operator's own scenario back as a test
-- case rather than as prose.
--
-- (1) SEASON STICKERS. The spec said "when the source starts a new season, post the
--     closing sticker, then the new season's sticker, then continue". app.season had
--     exactly one sticker flag — the opening one — so the closing sticker had nowhere
--     to be recorded, and there was no way to answer "did we already farewell season 1?"
--     after a restart. The boundary columns are the other half: without a recorded
--     reason, nobody can tell a season the source declared from a season we inferred.
--
-- (2) THE COMPLETENESS LIE. ``first_episode``/``last_episode`` were written by ingest
--     from whatever had arrived, while ``app.v_season_coverage.season_complete`` read
--     them as the season's declared length. So a weekly show whose source paused after
--     12 episodes of 26 looked *finished*: a permanent "Complete Season" post went out
--     on the strength of a one-week break. These columns are now the declaration only —
--     written by the owner's /declare — and what the source delivered goes into two new
--     observed columns. No view had to change, which is the point: the definition was
--     right all along, the writer was wrong.
--
-- (3) PROFILE. channels.py has described a checkpointed setup sequence since the first
--     commit and named app.destination.setup_state as where the checkpoints live. That
--     column did not exist, so "resume" was a promise the database could not keep: after
--     a restart mid-setup every step looked unfinished, which is how a channel gets a
--     second invite sent and the same bot added twice. The photo columns are what
--     plan_profile()'s decisions get written to.
--
-- Everything here is additive and re-runnable, and every migration file in this
-- directory must stay re-runnable on its own — that is why this file only adds columns
-- and never replaces a view.
-- ============================================================================

-- ---------------------------------------------------------------------------
-- 1. Seasons: why a row exists, and what the source has actually delivered.
-- ---------------------------------------------------------------------------
alter table app.season
  add column if not exists boundary_kind text,
  add column if not exists boundary_evidence jsonb not null default '{}'::jsonb,
  add column if not exists observed_first int,
  add column if not exists observed_last int,
  add column if not exists declared_at timestamptz,
  add column if not exists declared_by text,
  add column if not exists closing_sticker_posted boolean not null default false,
  add column if not exists closing_sticker_message_id bigint,
  add column if not exists closing_sticker_at timestamptz;

comment on column app.season.first_episode is
  'DECLARED start of the season — the owner''s statement (control bot: /declare), never a '
  'value copied from an upload. Together with last_episode it defines the season''s length, '
  'which is what a "Complete Season" post is a promise about. Null means undeclared, and '
  'undeclared means the caption prints TBA and the batch post does not fire.';

comment on column app.season.last_episode is
  'DECLARED final episode number of the season. Not the highest episode filed: the source '
  'pausing at 12 of 26 is indistinguishable from a finished 12-episode season by '
  'observation alone, and only one of those two is safe to announce.';

comment on column app.season.observed_first is
  'Lowest episode number actually filed. Observation, kept beside the declaration so the '
  'gap between "what arrived" and "what was promised" is visible instead of being smoothed '
  'over — that gap is exactly what /status should be reporting.';

comment on column app.season.observed_last is
  'Highest episode number actually filed. Feeds the caption''s episode range ("01 - 12", '
  'which describes the archive we hold) and never the Total Episodes line, which describes '
  'the season.';

comment on column app.season.boundary_kind is
  'Why this season exists at all: declared (the source caption stated the season) or '
  'inferred (a numbering restart accepted because seasons.confirm_unlabelled_reset is off). '
  'Null for the first season of a series, and for any season the owner wrote by hand. '
  'Never set from the passage of time, a gap in numbering, or an episode count.';

comment on column app.season.boundary_evidence is
  'The exact facts classify() used — episode, labelled_season, current_season, '
  'highest_in_season, file_kind — so a decision can be re-examined without re-parsing a '
  'source caption that may since have been edited.';

comment on column app.season.closing_sticker_posted is
  'The end-of-season sticker for THIS season, recorded on the season it closes rather than '
  'on the one that follows. Two flags exist because there are two stickers per boundary and '
  'posting either twice is visible forever in a channel of 30,000 subscribers.';

alter table app.season drop constraint if exists season_boundary_kind_known;
alter table app.season
  add constraint season_boundary_kind_known
  check (boundary_kind is null or boundary_kind in ('declared', 'inferred'));

comment on column app.season.declared_by is
  'Who stated the length: "operator" for /declare on the control bot, "sql" for a direct '
  'edit. A season length is never learned from the source — an airing show looks exactly '
  'like a finished one from the inside, which is the whole reason this column is here.';

-- The observed span may be empty (nothing filed yet) but never inverted.
alter table app.season drop constraint if exists season_observed_range;
alter table app.season
  add constraint season_observed_range check (
    observed_first is null or observed_last is null or observed_last >= observed_first
  );

-- ---------------------------------------------------------------------------
-- 2. The destination's own face: picture, bio, and the setup checkpoints.
-- ---------------------------------------------------------------------------
alter table app.destination
  add column if not exists setup_state jsonb not null default '{}'::jsonb,
  add column if not exists about text,
  add column if not exists photo_candidate_id bigint,
  add column if not exists photo_file_id text,
  add column if not exists photo_source text,
  add column if not exists photo_set_at timestamptz;

comment on column app.destination.setup_state is
  'Which of channels.SETUP_STEPS finished, keyed by step name. Render free tier kills the '
  'process between any two of them, and without this the only safe resume is to do '
  'nothing. The column was referenced by the code long before it existed here.';

comment on column app.destination.about is
  'The channel bio, from templates.channel_about. Null means "left blank on purpose": this '
  'program does not invent marketing copy for your channel.';

comment on column app.destination.photo_source is
  'owner | archive_cover | none. "none" is a decision, not a failure: with no clean cover in '
  'the archive, Telegram shows its initials, because the alternatives are a leech logo or a '
  're-rendered image and both are visible in the wrong way.';

comment on column app.destination.photo_candidate_id is
  'Which archive copy became the picture, so the same cover is picked again after a restart '
  'instead of flapping between two equally clean frames.';

create index if not exists destination_series_idx on app.destination (series_id);

-- ---------------------------------------------------------------------------
-- 3. Three knobs. Two of them change behaviour; the third resolves a contradiction
-- between the spec and this implementation, by making it yours rather than mine.
-- ---------------------------------------------------------------------------
insert into app.config (key, value, description) values
  ('seasons.confirm_unlabelled_reset', 'true'::jsonb,
   'When a source restarts its numbering with no season label in the caption, hold the '
  'episode and ask instead of opening a new season. Turning this off makes the service '
  'guess — reasonable only for a channel you have already watched do this, and any season '
  'it opens that way is recorded as inferred, not declared.'),
  ('templates.channel_about', 'null'::jsonb,
   'Destination channel bio. Null leaves the field empty on purpose: a bio describes your '
  'channel, so it is yours to write. Editable here, no redeploy needed.'),
  ('bots.channel_help_rights',
   $$["can_post_messages", "can_edit_messages", "can_delete_messages", "can_pin_messages"]$$::jsonb,
   'Which admin rights @chelpbot receives on a destination. The requirements document '
  'lists "invite/add users" among the rights Channel Help asks for; this default withholds '
  'it, because a publisher that can invite is a publisher that can be used to spam the '
  'channel. Put "can_invite_users" in the list to follow the bot''s own setup '
  'instructions instead. can_add_admins and can_ban_users are refused whatever is written '
  'here, and an unrecognised name is an error rather than a silent no-op.')
on conflict (key) do nothing;  -- operator edits always win; these are new keys

-- 0006_source_declarations.sql
-- What the operator states about a *source channel*, once, so a shelf of bare files can be
-- filed at all.
--
-- The scenario this exists for: a channel where the files already live, each message just
-- "episode 7" and an mp4. There is no series title, no quality label, no language claim in
-- that text, and the file itself cannot be asked (free tier: no download, no probe). Under
-- 0001-0005 every one of those files lands as "cannot determine whether the file carries
-- Hindi audio" and parks — which is the correct *safety* answer and a useless operating
-- answer, because it means a 400-episode backlog has to be decided one message at a time.
--
-- So the decision is moved up to the channel, where it belongs, and it is stored as what it
-- is: a declaration by you, not a property we measured. `app.normalize` keeps
-- audio_source/series_source provenance for exactly this reason, and the same three columns
-- are what `/status` will quote back at you if a caption ever looks wrong.
--
-- Deliberately absent: a declared *season*. A channel default for the season number stays
-- `season_source = 'hint'` and can never open a season by itself (see 0005 and
-- docs/seasons-and-channels.md) — `declared_season` below only says "assume this season
-- unless the caption says otherwise", which is a default for numbering, not a statement that
-- a new season has begun.

-- ---------------------------------------------------------------------------
-- 1. The declarations.
-- ---------------------------------------------------------------------------
alter table app.source_channel
  add column if not exists declared_series text,
  add column if not exists declared_audio text,
  add column if not exists declared_season int,
  add column if not exists declared_by text,
  add column if not exists declared_at timestamptz;

-- The audio tokens are the ones app.normalize.DECLARED_AUDIO accepts, spelled the same way,
-- so the database cannot store a value the parser would refuse. A CHECK that disagrees with
-- the code is a bug that only appears when someone edits the row by hand in the dashboard —
-- which is exactly how these will be edited.
alter table app.source_channel
  drop constraint if exists source_channel_declared_audio_check;
alter table app.source_channel
  add constraint source_channel_declared_audio_check
  check (declared_audio is null or declared_audio in (
    'hindi', 'dual', 'dual_audio', 'multi', 'multi_audio', 'subbed', 'subbed_only', 'unknown'
  ));

alter table app.source_channel
  drop constraint if exists source_channel_declared_season_check;
alter table app.source_channel
  add constraint source_channel_declared_season_check
  check (declared_season is null or (declared_season between 0 and 99));

-- An empty string is how "I changed my mind, stop assuming" looks when typed by hand. The
-- parser treats '' as no declaration, so the column must agree rather than storing a value
-- that means two things depending on which code path reads it.
alter table app.source_channel
  drop constraint if exists source_channel_declared_series_not_blank;
alter table app.source_channel
  add constraint source_channel_declared_series_not_blank
  check (declared_series is null or length(btrim(declared_series)) > 0);

comment on column app.source_channel.declared_series is
  'The operator''s statement of what show this channel carries. When set it outranks the '
  'channel''s own title or @handle as the series identity, and it is the only channel-level '
  'value that may found a destination channel name — reading a name off the channel title is '
  'one signal where the spec asks for two. Set from /source on the control bot.';
comment on column app.source_channel.declared_audio is
  'What every file in this channel claims to carry: hindi | dual | multi | subbed_only | '
  'unknown. It is what lets a bare "episode 7" mp4 pass the Hindi-in-scope rule at all. A '
  'file whose own text contradicts it still wins on its own wording, and a subbed-only file '
  'in a Hindi-declared channel is rejected rather than quietly relabelled.';
comment on column app.source_channel.declared_season is
  'Assume this season when a file says nothing. A numbering default only: it is recorded as '
  'season_source = ''hint'' and can never open a new season or a sticker.';
comment on column app.source_channel.declared_by is
  'Who said so (the control bot writes ''operator''), and when. Provenance matters because '
  'these columns are the difference between "the file claimed Hindi" and "you told me to '
  'assume Hindi here".';

-- Series resolution by declared name: /source looks a channel up by @handle or numeric id,
-- and the review surface asks "which channels carry Bleach".
create index if not exists source_channel_declared_series_ix
  on app.source_channel (lower(declared_series))
  where declared_series is not null;

-- ---------------------------------------------------------------------------
-- 2. The one knob this needs.
-- ---------------------------------------------------------------------------
-- A global off switch for the whole idea, because the rule "a channel-level statement
-- licenses a file's language" is exactly the sort of rule someone will want paused while
-- they work out why a caption says Hindi on a subbed file. With it off, the declarations
-- still record and still resolve the *series*; they just stop granting scope.
insert into app.config (key, value, description) values
  ('ingest.accept_channel_audio_declaration', 'true'::jsonb,
   'When true, app.source_channel.declared_audio decides whether a file with no language text '
  'is in scope. Set it to false to park every such file for review again without editing the '
  'channels one by one; the declarations stay recorded either way, so this is reversible with '
  'no re-scan.')
on conflict (key) do nothing;  -- an operator edit always wins; this is a new key

-- ---------------------------------------------------------------------------
-- 3. Nothing else. No view is touched, on purpose: 0005 learned that replacing a view by
--    `create or replace` fails the moment a column is renamed, and that drop-and-recreate
--    breaks re-applying 0002. The columns above need no new view — /status reads them.
-- ---------------------------------------------------------------------------

-- 0007_inplace_captions.sql
-- The second shape of publishing: the channel you add *is* the channel we publish in.
--
-- Everything before this migration assumes the pipeline's shape: read a source channel,
-- archive to a private master channel, hand a link to Channel Help, and let it compose a post
-- in a destination channel we created. That shape needs the Hindi-audio gate, needs a clean
-- thumbnail, and needs a link — because it is *moving* someone's file.
--
-- The other shape needs none of that. The files are already posted, by you, in your own
-- channel, and the only thing wrong with them is the text under the video: nothing but
-- "episode 7". So the job is one edit per file and nothing else. No second channel is joined,
-- no copy is made, and — settled explicitly — nothing is deleted afterwards, because an edit
-- that keeps the file keeps the post working and a deletion has no upside here.
--
-- The columns below exist to make that mode *data* rather than a code path, because the mode
-- changes which rules apply: `app.inplace.mode_allows_missing_audio` reads
-- `app.destination.publish_mode`, and the review queue has to be able to answer "why does this
-- channel have unlabelled files" without anyone reading source.

-- ---------------------------------------------------------------------------
-- 1. A destination has a publish mode.
-- ---------------------------------------------------------------------------
alter table app.destination
  add column if not exists publish_mode text not null default 'link_post',
  add column if not exists paired_source_channel_id bigint references app.source_channel (id) on delete set null;

alter table app.destination
  drop constraint if exists destination_publish_mode_check;
alter table app.destination
  add constraint destination_publish_mode_check
  check (publish_mode in ('link_post', 'in_place_caption'));

comment on column app.destination.publish_mode is
  'link_post: Channel Help composes a text-plus-buttons post and the file lives in the master '
  'archive. in_place_caption: the files are already in this channel and the approved caption is '
  'written onto those very posts — no copy, no new post beside them, no deletion, no inline '
  'buttons (a user session cannot attach a keyboard to a media message, and there is no link to '
  'put in one). Set from /inplace on the control bot. The mode decides policy, not style: '
  'see app/inplace.py for what each mode does and does not gate on.';
comment on column app.destination.paired_source_channel_id is
  'The source channel that this in-place destination was compared against, when the operator '
  'named both. It is a pointer, not a copy job: the only time it produces a file move is when '
  'the source holds an episode this channel does not, and even then it is a server-side forward '
  'from the master archive. Nullable because a files-only channel is a complete destination on '
  'its own — most /inplace runs will never set it.';

-- ---------------------------------------------------------------------------
-- 2. How a joined channel was classified when it was added.
-- ---------------------------------------------------------------------------
-- The operator's rule for two same-named channels: admin here and ordinary member there means
-- *here* is the destination. It is not a trust judgement — a user account with no posting
-- rights physically cannot edit those messages, so the rights are the answer. Storing the
-- observation is what lets a later run explain a decision instead of silently re-making it.
alter table app.source_channel
  add column if not exists we_are_admin boolean,
  add column if not exists publish_role text;

alter table app.source_channel
  drop constraint if exists source_channel_publish_role_check;
alter table app.source_channel
  add constraint source_channel_publish_role_check
  check (publish_role is null or publish_role in ('source', 'destination', 'source_and_destination'));

comment on column app.source_channel.we_are_admin is
  'null = never checked. true/false = what the session''s own rights were the last time we '
  'looked (GetFullChannel / the admin log). In-place pairing is decided from this: the channel '
  'we can post in is the destination, the one where we are a member is a source. Two '
  'admin-able channels of one name is a question for the operator, not a sort.';
comment on column app.source_channel.publish_role is
  'The role the operator confirmed for this channel. source_and_destination is the in-place '
  'case, where the channel we read is the channel we write: the files are already posted, so '
  'nothing is fetched, only captioned.';

-- ---------------------------------------------------------------------------
-- 3. An overwritten caption is kept, and the edit is counted.
-- ---------------------------------------------------------------------------
-- Telegram holds no history of a media caption: the moment the text under a video is replaced,
-- the old text is gone. `caption_previous` is the only undo available, so the plan that
-- proposes a replacement (`app.inplace.Decision.previous_caption`) has to be stored with the
-- result rather than thrown away in the job log. `edits` makes a re-run visible: one episode
-- with edits = 9 is a loop, and a loop is worth noticing before it is 400 of them.
alter table app.destination_post
  add column if not exists caption_previous text,
  add column if not exists edits int not null default 0;

alter table app.destination_post
  drop constraint if exists destination_post_edits_check;
alter table app.destination_post
  add constraint destination_post_edits_check check (edits >= 0);

comment on column app.destination_post.caption_previous is
  'The text that was on the message before our caption, for in-place posts. Kept verbatim, '
  'including a note we would rather not have replaced: "we changed it" is only safe if "it" '
  'still exists somewhere.';
comment on column app.destination_post.edits is
  'How many times this post''s caption has been written, including the first time in in-place '
  'mode. A restart-resume should leave it at 1 for every episode; anything higher is either a '
  'template change or a bug, and the difference matters at 400 episodes.';

-- An in-place destination is rare and worth an index for: the worker has to find "which
-- channels am I captioning, not linking" without reading every destination row.
create index if not exists destination_inplace_ix
  on app.destination (series_id, publish_mode)
  where publish_mode = 'in_place_caption';

-- ---------------------------------------------------------------------------
-- 4. Two knobs, and no more.
-- ---------------------------------------------------------------------------
insert into app.config (key, value, description) values
  ('inplace.overwrite_notes', '"ask"'::jsonb,
   'What to do when the file post already carries text that is more than an episode label (a '
  'note, a mirror link, a date). "ask" leaves the message untouched and queues one question — '
  'the default, because a caption replaced by mistake is unrecoverable in Telegram. "replace" '
  'writes the approved caption anyway, keeping the old text in caption_previous; only choose '
  'it for a channel whose messages you know you wrote yourself.'),
  ('inplace.copy_missing', 'true'::jsonb,
   'When true, episodes present in the paired source but absent from an in-place destination '
  'are forwarded in from the master archive and then captioned. Set false to caption only what '
  'is already posted and report the gaps instead — which is also what happens automatically '
  'when the two channels hold the same number of episodes under different numbering, since '
  'that reads as renumbering and copying would duplicate the whole season onto itself.')
on conflict (key) do nothing;  -- an operator edit always wins; these are new keys

-- 0008: the updates channel, named instead of assumed.
--
-- A fourth flow exists, and the operator described it on 2026-08-28 with three screenshots: every
-- series channel has a card post (the art with the brand handle on it, the channel's own invite link
-- in its caption), that message is forwarded to @Link_providerobot, and the shareable link it hands
-- back goes into an announcement in one channel for the whole brand. The protocol is recorded in
-- app/linkprovider.py and docs/updates-channel.md; this migration carries only the two things that
-- are settings rather than knowledge.
--
-- Nothing here creates a table. An announcement is a post, and the queue that makes posts
-- (app.job, kind publish_post) already exists; a second queue for the same act would be a second
-- answer to "is this owed?", and two answers is how a job gets run twice. When the four decisions
-- the operator gave are wired to a sender, the sender reads these two rows and the destination's own
-- card link — no new state is needed to know what to say, only the permission to say it.

-- ---------------------------------------------------------------------------
-- 1. Which channel, and how often. Two rows.
-- ---------------------------------------------------------------------------
insert into app.config (key, value, description) values
  ('updates.channel', '""'::jsonb,
   'The one announcements channel every series posts into: a @handle, or the numeric id of a '
  'private channel. Empty is not "announce nowhere" — it is "the app does not know where", and '
  '/status says so in those words. It is deliberately a single brand-wide value rather than a '
  'column per destination: the operator''s flow is one noticeboard for every show, and a per-series '
  'channel would mean a per-series card to maintain for no audience gain. Change it only here; the '
  'app never picks a channel by guessing from a name.'),
  ('updates.per_episode', 'true'::jsonb,
   'One announcement per episode as it lands (the operator''s answer when asked, 2026-08-28), which '
  'is what the sampled posts already look like — each says "Episode 14 Added", never a range. Set '
  'false to announce once per batch or season instead; the shape is the same and the line names the '
  'range. Either way nothing is sent until the announcement text is an approved caption box, and '
  'that is a separate decision from this one.')
on conflict (key) do nothing;  -- an operator edit always wins; these are new keys

-- ---------------------------------------------------------------------------
-- 2. What this migration deliberately does not do.
-- ---------------------------------------------------------------------------
-- It does not add `templates.announcement_post` to the approved caption rows. The announcement's
-- exact text is known — it was read off the operator's own posts and `app.linkprovider` renders it
-- character for character — and describing a post is not the same as being allowed to send it into a
-- channel of 33k subscribers. The gate that guards every other caption guards this one, and the
-- absence is reported in /status rather than being worked around silently.

-- 0009: the announcement box, approved; and the moment the rights were read.
--
-- Two things the operator decided on 2026-08-28, both recorded as data rather than as a comment:
--
-- * "announcement text approved" — the updates-channel post becomes a caption box like every
--   other one in this project, editable in app.config, and its absence from APPROVED_TEMPLATES is
--   what stopped it being sendable until now. Nothing about the shape changed on approval: it is
--   the text read off the operator's own posts, both samples agreeing on the season suffix, the
--   zero-padded episode and the link written twice.
-- * "hum admin hai ya nahi, ye hume khud detect karna hoga" — the app reads its own rights from
--   the dialog list (app/rights.py, run at the end of every probe) instead of asking the operator
--   to fill a box in the dashboard. A timestamp travels with it, because a rights flag that decided
--   a publishing shape two months ago is a different claim from one read today.

alter table app.source_channel
  add column if not exists rights_checked_at timestamptz;

comment on column app.source_channel.rights_checked_at is
  'When a session last read this channel''s rights for us (app.rights.record, driven by /probe). '
  'null means nobody ever has, which app.inplace.route_for treats as "member, and unverifiable" — '
  'the narrow answer, never the convenient one. The flag and the timestamp move together: a write '
  'that changed nothing still proves we looked.';

-- templates.announcement_post — the approved text, in one JSON string like its siblings.
-- One statement per key, on one line per field: `tests/test_caption_templates.py` reads these back
-- with a regex, and a wrapped description is a row that would silently escape that check.
insert into app.config (key, value, description) values
  ('templates.announcement_post',
   '"🍓 {title_full} (S{season})\n\n😗 Episode {episode} Added...✨”\n\n[Click here to start and get episode]({link})\n[Click here to start and get episode]({link})"',
   'Approved 2026-08-28 from the operator''s own posts in the updates channel: strawberry heading, series and season in parentheses, the note line with the zero-padded episode, the link written twice, and no file hosted there. The link is a bot deep link (app/linkprovider), never the invite itself; the post is made by the operator''s own account as plain text with a link, not by Channel Help, and never to a channel that is not named in updates.channel.')
on conflict (key) do nothing;  -- an operator edit here always wins, and this file never rewrites it
