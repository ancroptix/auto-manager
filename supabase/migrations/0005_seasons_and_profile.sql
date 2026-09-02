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
