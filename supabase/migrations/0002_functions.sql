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
