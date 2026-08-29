-- 0011: the columns and rows the write layer needs.
--
-- Every job that only *reads* was already able to run. The eight that write (app/writers.py, added
-- the same day) need four facts the schema had nowhere to keep, and each one exists because a handler
-- reads it — not because it looked like a sensible column:
--
--   storage_link.token        the deep link's payload, kept beside the url so "does this link still
--                             point at the message we stored" is a comparison and not a memory test.
--   destination.card_message_id
--                             which post in the destination channel was forwarded to the link bot.
--   destination.announcement_link(+_at)
--                             what the bot answered with, and when — the answer is not reproducible
--                             on demand, so it is kept the first time it is seen.
--   season.sticker_source_*   the message the season's sticker is forwarded *from*. A sticker cannot
--                             be addressed by a pack name; Telegram wants the document, and the
--                             document lives in one specific message.
--
-- The three config rows are the operator's switches, defaulted to the safe setting. ``publish.route``
-- is the important one: the default ``chelp_block`` renders the caption plus the button block for
-- Channel Help to paste (docs/channel-help.md) and stores the result as an unpublished draft, exactly
-- like every destination post before it; only ``own_session`` lets the program press send in a public
-- channel itself.

-- The token inside a t.me/<bot>?start= link. Nullable: a link stored before this column existed has
-- no token, and link_verify then says "shape ok, unverified" rather than calling a live link broken.
alter table app.storage_link add column if not exists token text;
comment on column app.storage_link.token is
  'the start payload of the storage bot''s deep link, kept so link_verify can compare the stored link with the url it published; null for links recorded before this column existed';
create index if not exists storage_link_token_ix
  on app.storage_link (token)
  where token is not null;

alter table app.destination add column if not exists card_message_id bigint;
comment on column app.destination.card_message_id is
  'message id, inside this destination channel, of the card post that was forwarded to the link bot to get a shareable link (control bot: /card). Named by the operator because the announcement must carry that link and never the invite itself';

alter table app.destination add column if not exists announcement_link text;
comment on column app.destination.announcement_link is
  'the link the bot answered with for card_message_id, verbatim. Kept because the answer is not reproducible: the bot replies once, to the forward, and a re-run that asks again gets nothing';
alter table app.destination add column if not exists announcement_link_at timestamptz;
comment on column app.destination.announcement_link_at is
  'when announcement_link was seen. The link itself is described as permanent by the vendor; this timestamp is only for the human reading /status';

alter table app.season add column if not exists sticker_source_chat_id bigint;
comment on column app.season.sticker_source_chat_id is
  'chat the season''s opening sticker is forwarded from, paired with sticker_source_message_id (control bot: /sticker). The pack itself is addressed by app.season.sticker_document_id, which comes from the message, not from a name';
alter table app.season add column if not exists sticker_source_message_id bigint;
comment on column app.season.sticker_source_message_id is
  'the message holding that sticker. Both columns are required together — one without the other is not an addressable message — which is why nothing here defaults them';

insert into app.config (key, value, description) values
  ('publish.route', '"chelp_block"'::jsonb,
   'how a destination post is produced: chelp_block renders the approved caption and the button block and leaves the post for Channel Help to paste (the documented flow, docs/channel-help.md), own_session sends it from the logged-in account with real inline buttons. Anything else is refused rather than guessed'),
  ('announcement.style', '"markdown"'::jsonb,
   'parse mode for the notice in the updates channel: markdown renders the [label](url) the sampled posts use, text writes the label on its own line over the bare url. Chosen from two posts read off the operator''s own channel, so the first live announcement should be looked at, not assumed'),
  ('updates.card_link', '""'::jsonb,
   'an already-made shareable link for the card post, used instead of asking the link bot again. Left empty on purpose: the normal path is /card naming the message, so a non-empty value here means the operator pasted a link by hand and it will not be re-checked')
on conflict (key) do nothing;
