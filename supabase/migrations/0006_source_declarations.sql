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
