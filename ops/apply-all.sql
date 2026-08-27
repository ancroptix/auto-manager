-- ============================================================================
-- Auto Manager — ONE-FILE INSTALLER
-- Paste this whole file into Supabase -> SQL Editor -> Run. It is exactly
-- 0001_init.sql followed by 0002_functions.sql, concatenated by
-- ops/build_apply_all.py so the two never drift apart.
--
-- If you prefer the normal Supabase flow instead, apply the two files in
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
