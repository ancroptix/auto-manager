-- 0010: the join-request wording becomes a setting, and the announcements channel gets its id.
--
-- Two things the operator settled on 2026-08-29, in the same message, which is why they land in the
-- same file. Both are "the app was waiting for a value" rather than "the app needed a new idea":
--
-- * the sentence a join requester receives had been deferred since §15 of the spec was written —
--   "Message template: TBD by operator" — and the operator's answer to "what should it say" was to
--   ask for a way to say it at any time: "bot me options de dena, jisse mujhe kabhi bhi kuch bhi
--   bolna ho to mai bol paau sabhi se". So it is a control-bot command (/joinmsg, app/joinmsg.py)
--   writing one app.config row, not a table of campaign copy.
-- * the updates channel is `-1002072936982`, given in the same message. It is written here rather
--   than left as a `psql` step in the operator's list because a value nobody has to paste is a value
--   nobody has to paste wrong; the update is guarded on the row still being empty, so if you had
--   already filled it, your fill is what stays.
--
-- The scope note that belongs with the second half, because it changes which code path may post
-- there: the announcement is made by **this program's own session**, as plain text with a link, and
-- **never through @chelpbot**. Channel Help posts only in the series destination channels, with
-- only what it is configured to do there (docs/channel-help.md, docs/updates-channel.md). No row here
-- grants Channel Help a right in the announcements channel, and nothing here makes it possible.

-- ---------------------------------------------------------------------------
-- 1. What a join requester is told. One row, empty by default.
-- ---------------------------------------------------------------------------
insert into app.config (key, value, description) values
  ('joinrequest.message', '""'::jsonb,
   'The message a person gets when the owner runs a campaign at the still-pending join requests of '
   'one of our channels. Empty means the app may contact nobody, which is the default and the safe '
   'answer: /joinmsg in the control bot writes this row (options and your own words; '
   'app/joinmsg.py holds the rules, including that {name} and {series} are the only placeholders). '
   'It is wording, not state, so it lives here rather than in a column: a sentence can change on a '
   'Tuesday with no migration. Saving a text does not queue a send — delivery is the blocked job '
   'kind join_request_campaign, because there is no MTProto sender yet. Two rules are schema-level '
   'and are repeated here so the row is not read as a loophole: a message never carries an invite '
   'link, and sending never approves or declines the request '
   '(app.join_campaign.campaign_never_approves).')
on conflict (key) do nothing;  -- an operator edit always wins; this is a new key

-- ---------------------------------------------------------------------------
-- 2. The announcements channel, now that it has been named.
-- ---------------------------------------------------------------------------
update app.config
   set value = '"-1002072936982"'::jsonb, updated_at = now()
 where key = 'updates.channel'
   and value = '""'::jsonb;
-- The id is quoted as a string rather than a number because that is how app.config holds text elsewhere,
-- and because a bigint-looking JSON number invites a cast that a @handle could never survive.
