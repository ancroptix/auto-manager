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

-- The 0008 row that describes `updates.per_episode` ends with a sentence about approval, and approval
-- is exactly what happened later the same day. A migration that already ran is never rewritten — a
-- live database will not read that file again — so the correction is a statement here, guarded on the
-- shipped phrase so an operator who edited the description keeps their own words.
update app.config
   set description =
     'One announcement per episode as it lands (the operator''s answer when asked, 2026-08-28), which '
     'is what the sampled posts already look like — each says "Episode 14 Added", never a range. Set '
     'false to announce once per batch or season instead; the shape is the same and the line names the '
     'range. The announcement box was approved the same day, below, so what holds a post back is the '
     'sender that does not exist yet, not the wording.'
 where key = 'updates.per_episode'
   and description like '%nothing is sent until the announcement text is an approved caption box%';
