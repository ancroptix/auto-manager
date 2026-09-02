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
