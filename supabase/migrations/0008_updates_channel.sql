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