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
