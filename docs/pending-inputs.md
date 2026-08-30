# What the app is waiting on from you

One page, everything outstanding. It is deliberately derived rather than remembered:
`tests/test_docs.py::test_the_pending_inputs_page_lists_everything_the_code_can_ask_for` fails if a
value appears in `render.yaml` or `app/config.py` or `app/handlers.py` and is missing from this page.
So if a row here is wrong, the test says so — and if you fill everything below and the app still will
not post, section 6 is the reason, and it is not your list.

## 1. Seven values in Render's dashboard (paste once, then deploy)

These are the environment variables the blueprint leaves **empty on purpose** (`sync: false`), which
is the same set as the settings in `app/config.py` that have no default. Everything else
(`APP_MODE=shadow`, `LOG_LEVEL=info`, `DB_SSL=require`, `TELEGRAM_SESSION_SOURCE=both`,
`BOT_ALLOW_LOGIN=true`, `PROBE_ON_BOOT=false`) already ships with the right value; change them later
from the dashboard, not here.

| variable | where you get it | what it is for | what happens while it is missing |
| --- | --- | --- | --- |
| `DATABASE_URL` | Supabase → **Project Settings → Connect** → the **Session** pooler string (host starts `aws-`, port `5432`, IPv4). Add `?sslmode=require` if it is not there. | the whole database | `/ready` fails and the app refuses to enter live mode at all — fail-closed, not degraded |
| `CONTROL_TOKEN` | any long random string you keep (40+ characters; `openssl rand -hex 32` on any machine) | signs the HTTP control calls (`/control/probe`, `/control/shutdown`) | those endpoints answer 503; Telegram-side control keeps working |
| `TELEGRAM_API_ID` | https://my.telegram.org → API development tools | this account's identity with Telegram | no session can be created, so no channel can be read |
| `TELEGRAM_API_HASH` | same page, same form | the second half of that identity | same as above |
| `TELEGRAM_BOT_TOKEN` | @BotFather → `/newbot` | the assistant you talk to (`/status`, `/probe`, `/login`) | the assistant does not exist; control is HTTP-only |
| `TELEGRAM_OWNER_USER_IDS` | your own numeric Telegram id (`/id` in the assistant once it is running) | who the assistant obeys — a list, comma separated | the assistant answers nobody, on purpose. An owner-less control bot is a bot that takes orders from strangers |
| `TELEGRAM_MAIN_ADMIN_USER_ID` | the numeric id of the account that is supposed to be promoted in a new destination channel | the one-use invite's recipient | channel creation stops before the invite step rather than promoting the wrong person |

**Leave `TELEGRAM_SESSION_STRING` empty.** It is in the blueprint so a machine-with-a-terminal can set
it once, but here the session arrives through the assistant: `/login` in chat, and the string is stored
in `app.telegram_session` in your own database. Pasting an account-equivalent credential into a
deployment's environment is the one move that turns a stolen dashboard login into a stolen Telegram
account. `TELEGRAM_SESSION_SOURCE=both` means the app prefers the environment value and falls back to
the stored one, so the empty case is the normal case.

## 2. The row that used to be only yours — now shipped, still worth a look

`updates.channel` was seeded empty (`0008_updates_channel.sql`) because empty means "the app does not
know where", and `/status` prints exactly that rather than picking a channel by name. On 2026-08-29
you gave the id: `-1002072936982`, a private channel's numeric form, which is the normal spelling for a
private channel and not a fallback.

That value is now carried by `supabase/migrations/0010_join_message_and_updates_id.sql`, so applying
`ops/apply-all.sql` (§4) fills it — there is nothing to paste. The statement is guarded on the row
still being empty, so if you had already set it yourself, yours stays. After §4 step 2, this should
print the id and nothing else:

```sql
select value from app.config where key = 'updates.channel';   -- expect "-1002072936982"
```

What 0010 does *not* do is decide who posts there. The announcement is made by **this program's own
session**, as plain text with a link; `@chelpbot` posts only in the series destination channels, with
only what it is configured to do there. That is your ruling of 2026-08-29, and no row in the database
grants Channel Help a right in the announcements channel.

Change the number to the one you confirm, then run `/status` — the line must change from "not set" to
`updates channel: private channel -100…, one announcement per episode, …`. Two ways to read your own
channel's id: forward one of its posts to @userinfobot, or connect the session and run `/probe`, which
walks the dialog list (`app/rights.py`) and prints what it found.

While you are in there, the one other row with a choice baked into it is
`bots.channel_help_rights` (section 5).

## 3. Two chores on your side, both about a leak

The Supabase **`sb_secret_…`** key was pasted into a chat once. Anything that has appeared in a chat
window has to be treated as public.

1. Supabase → **Project Settings → API Keys (legacy)** → delete that `sb_secret_` key and create a new
   one. This app never uses it — it connects to Postgres directly — so deleting it breaks nothing here.
2. **Project Settings → Database** → reset the password. Then update `DATABASE_URL` in Render with the
   new one (the pooler string embeds it).

Nothing in the repository holds either secret; `scripts/check_secrets.py` runs in CI for exactly that
reason (85 files scanned clean as of this writing — the count is what the script prints, so it moves when a file is added and nobody has to remember).

## 4. The live steps, in order, once the values are in

1. Supabase → **SQL Editor** → paste all of [`ops/apply-all.sql`](../ops/apply-all.sql) → **Run**. Expect
   `Success. No rows returned`. This is how `0009_announcement_approved.sql` and
   `0010_join_message_and_updates_id.sql` land on a database that was built before they existed.
2. Sanity check: `select count(*) from app.config;` must read **48**, and
   `select count(*) from information_schema.tables where table_schema='app';` must read **27**.
3. Render → deploy, `APP_MODE` still `shadow` for the first day: real reads, no posts.
4. Talk to the assistant: `/login` (pairing code in chat, nothing to paste into a terminal), then
   `/sessions` to see it stored.
5. `/probe`, and read the message it sends back: the storage assistant's menu, the link provider's
   reply shape, and the rights line — `rights read for N configured channel(s), M changed; not visible
   to this session: …`. Any channel named on that second half is a channel to look at, not an error.
6. Teach it the source: `/source <@handle> series <name> audio hindi`, then `/inplace <@handle> plan`
   and read the route it says out loud.
7. Only then: `APP_MODE=live`.

## 4b. Five things the write layer cannot do without you

These are not code work. Each one is a fact only you have, and each is refused in the same words by
the job that needs it, so `/status` names the missing thing rather than a stack trace:

1. **One session, live.** Done on 2026-08-29: the account is logged in and its session is stored, and
   the probe has since run against it four times. `BOT_ALLOW_LOGIN=false` is the loose end, and it is a
   switch on your side, not a row here — leave the login door shut now that you are through it.
2. **The private master archive.** `/archive <@handle|channel id> add title <name>` writes the row in
   `app.archive_channel`, and the first row added is the primary one — so what is still yours to supply is
   the *channel*: this program will not choose where the only spare copy of an episode lives.
3. **`/card <destination> <message id>`**, once per destination channel — the post that gets forwarded
   to the link bot so the announcement carries its shareable link. No card, no announcement; the
   invite link never goes out on its own.

   The destination *row* is no longer yours to insert by hand: `/discover` (or the menu's
   `🔎 Find channels`) files a channel your own account administers as the destination of the series its
   name names. The message number is still yours, because only you know which post to forward.
4. **`/sticker <series> <season> from <channel> <message id>`**, per season, if you want a sticker to
   open it. Nothing here decides which sticker means "season 2".
5. **`publish.route`.** Leave it at `chelp_block` and every destination post is prepared for Channel
   Help to paste, exactly as before; set it to `own_session` and the same rendered post is sent from
   your account with real buttons. It is one row, and the first live run is the time to compare the
   two — which is also the moment to look at whether `announcement.style` renders the box the way you
   want it.

## 4c. Your clone's `/settings` screen: three answers in, two things left

These are not code work and `/probe` cannot fetch them: they are switches on the clone, which the bot
shows only to the account that owns it. Read on 2026-08-29, and recorded in `docs/storage-bot.md` so the
protocol file holds both the verbs and the settings behind them:

1. **Public Mode**, not Private — answered, and it is not a comfortable answer: any Telegram user can mint
   a link from this clone. Nothing breaks while every source is a public channel. A *private* source on
   an open clone is a private channel anyone can read, so if you ever want one, the mode changes first.
2. **Auto-delete timer: 5 minutes** — answered. It takes the copy delivered to whoever opens the link; the
   stored message stays. This is why no post of ours may reference a message id inside the bot chat, and
   it is a rule in the code, not advice.
3. **Moderators: your main account only** — answered, and it costs us two verbs. `/special_link` and
   `/universal_link` are gated on that list, and the pipeline account is not on it.

Left for you, in either order:

* **One sentence, not one word, about forwarding.** "No Forward" (content protection) decides whether our
  first step — forwarding a file out of the source channel — works at all. Your reply of 2026-08-29 could
  be read either way, and this is the one switch where guessing is expensive, so it stays open until you
  write it out: which of the two is it, on or off?
* **Whether to add `@Turvei` to that moderator list.** One entry on the screen. It would put the
  add-a-quality-later link and the survives-a-dropped-clone link within reach of the account that actually
  runs the pipeline. It is your call because it hands that account power over your clone, and nothing here
  will assume you gave it: `app/storagebot.py` marks those two verbs as moderator-gated in `MODERATOR_ONLY`,
  and that is a flag rather than a lock — the design says so out loud — so what changes when you add the
  account is what a live run can prove, not what a doc may promise.

## 5. Four things that started as decisions and are now partly build work (from §18)

- **Season → sticker mapping.** Auto-detected on the first season that goes out; you pick once if the
  channel already has stickers that do not follow the pattern. Waiting on the live session, not on an
  answer from you today.
- **The join-request message text — yours to write, whenever you want.** It stopped being a question
  in the chat log on 2026-08-29: `/joinmsg options` in the assistant shows three drafts, `/joinmsg use 2`
  saves one, `/joinmsg set <your own words>` saves those instead, and `/joinmsg clear` puts the gate back
  up (empty means the app may contact nobody — that is still the shipped default, so nothing has been put
  in your mouth). Saving is not sending: the sender is still the blocked job kind, and the message can
  never carry an invite link or read like an approval — `/joinmsg` refuses both, in writing. What is still
  a decision rather than a build task is **which** of those three sentences, or your own.
- **The storage assistant's write layer, and three switches on your clone.** `/batch` and its two
  prompts are recorded, so what is left is the code that performs the flow and four readings: whether
  a link is a reference to the source post or a copy, and — on the clone's own `/settings` — whether
  it is in **Public Mode** (any Telegram user can mint links through it) or **Private Mode**, whether
  **No Forward** is on (it would fight your own "save these messages" advice), and what the
  **deletion timer** is set to. Public Mode on a clone that can read a private channel is the one
  answer here that is urgent; and whatever you do, do not rename the clone once a link is published.
- **`bots.channel_help_rights`** — whether `@chelpbot` gets `can_invite_users`. The guide asks for it,
  the default withholds it (a publisher that can invite is a publisher that can be used to spam your
  channel). Grant it by editing that one row; `can_add_admins` and `can_ban_users` are refused whatever
  the row says.

## 6. What the write layer is, and what it still waits on

Eight job kinds reach Telegram. They were built on 2026-08-29 and every one of them is *routed
through one writer* (`app/sender.py`), which refuses to send unless the deployment says live, keeps a
per-job write budget, and records each real write in `app.audit_log`. Nothing sends in shadow: a
shadow run returns the sentence of what it would have done and **blocks the job with it**, so a plan
can never be mistaken for a result.

The list below is the residual — what still stops each kind from completing, in the same words
`app/handlers.py:DEPENDENCIES` and `/status` print:

- `archive_media` — needs a row in `app.archive_channel` naming the private master archive. This
  program never picks the channel that holds the only spare copy of an episode.
- `storage_upload` — `/batch` is recorded step for step (`app/storagebot.py`); the handler sends it,
  forwards the first and last message with the tag, and reads the reply back. It refuses to store a
  link it did not receive, and blocks with the shapes it saw. `/genlink`, `/custom_batch` and
  `/special_link` are not driven: the other verbs stay the operator's choice, not our default.
- `link_verify` — the url's shape, its host and the token stored beside it run today with no session
  at all. The half that reads the source range back needs an authenticated session.
- `link_health_check` — the same check, bounded per run (a health check must not become the outage it
  was meant to notice), and its result says plainly that the clone's link was never opened.
- `publish_post` — renders the approved caption plus the button block, and `publish.route` decides who
  presses send: `chelp_block` (the default) stores the post in `app.destination_post` with no
  `published_at`, which is this schema's own word for "draft"; `own_session` sends it with real inline
  buttons. The updates-channel notice is separate and needs `/card` for that destination — the link
  the provider bot made for the forwarded card, never the bare invite.
- `edit_post` — the same box and block, applied to the post that already exists; gated on our own
  rights in that channel, read by `/probe` (`app/rights.py`).
- `season_sticker` — forwards the sticker message named by `/sticker <series> <season> from <channel>
  <message id>`, before any episode post of that season. A pack name or an install link is not an
  address, and which sticker opens a season is not this program's call.
- `join_request_campaign` — reads the still-pending requests of one channel, sends the wording of
  `/joinmsg`, and stops at `campaign.rate_per_hour` by pausing the campaign rather than pushing past
  it. A campaign runs only after `/campaign … confirm <code>` — or after the plan and the start tap on
  `/joinreq`, which computes that code instead of asking you to type it, and paces one person every 3
  seconds so a restart can resume the list.

Three config rows carry the switches that decide the rest: `publish.route` (who presses send),
`announcement.style` (how the notice is rendered, because the sampled posts were read and not asked
about) and `updates.card_link` (a link you pasted by hand, which then goes unchecked — the normal path
is `/card` naming the message). The sanity check must read **48** rows in `app.config`.

### Which rows change behaviour, and which ones only record it

Reading is not uniform across those rows, and an operator who edits a decorative one loses an evening
figuring out why nothing changed, so the split is written down:

- **Live knobs, read while the work runs:** `templates.*`, `branding.*`, `caption.*`, `quality.order`,
  `ingest.*`, `inplace.*`, `thumbnail.*`, `seasons.confirm_unlabelled_reset`, `publish.route`,
  `announcement.style`, `updates.*`, `stickers.*`, `joinrequest.message`, `campaign.rate_per_hour`,
  `bots.storage_username` and `bots.channel_help_username` (the latter is the peer `/probe` stands in
  front of, so a re-cloned help bot needs no redeploy).
- **Read once, when the service starts:** `worker.lease_seconds` (how long a claimed job stays claimed),
  `bot.login_ttl_seconds` and `bot.delete_sensitive`. Editing one takes effect on the next boot.
- **A record of a policy enforced elsewhere:** `jobs.max_attempts`, which lives on each `app.job` row
  (default 8) and is what `app.fail_job` turns into `blocked`; and the `destination.*` rows — name
  template, visibility, per-series, keep-both, description, auto-create — which describe the channel a
  *human* opens, because nothing here calls `channels.createChannel`.
- **A record of a decision with no code to obey it yet:** `bots.channel_help_rights` and
  `templates.channel_about` (both read only by `app.channels.setup_plan`, the plan no executor runs
  today), `campaign.mode`, `campaign.forward_replies_to_main` (forwarding a stranger's reply to you is a
  Telegram action nobody has asked for), and `bot.enabled_commands`.

The rule the last two groups are held to: a row that nothing reads says so here, instead of sitting in a
table that looks like a control panel.

In plain words: **the reading half is built; the writing half is not yet *run*.** Each of those eight
handlers has been executed against the real schema with a client that records instead of sending —
which proves the SQL and the refusal paths, and proves nothing about Telegram's answer. The first run
with your session is the test of that, and it starts in shadow: the queue tells you what it intends to
post, button for button, before anything reaches a channel.