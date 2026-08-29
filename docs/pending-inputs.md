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
2. Sanity check: `select count(*) from app.config;` must read **45**, and
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

## 6. What is *not* waiting on you — so nobody reads this page as "nearly running"

Eight job kinds are blocked because the code that would perform them is not written. `app/handlers.py`
carries each reason in `DEPENDENCIES`, and `/status` prints the count, so a blocked queue never looks
idle:

- `archive_media` — the copy into the private master archive channel
- `storage_upload` — the file handoff to the storage assistant (needs section 5's run)
- `link_verify` — liveness of a published link
- `publish_post` — the send itself: Channel Help's post for a destination, and the announcement
- `edit_post` — `messages.editMessage` on a bot-created post, which is the in-place caption path
- `season_sticker` — the sticker-pack label mapping
- `join_request_campaign` — the owner-approved campaign sender with pacing (the *text* is a
  setting now, `/joinmsg`; this is the delivery)
- `link_health_check` — the periodic re-check of published links

In plain words: **the reading half is built, the writing half is not.** A file is ingested, normalised,
captioned and matched today; nothing yet creates a channel, uploads to a storage service, posts to a
channel, edits a caption in place, or sends the announcement. That is the difference between "approved
text" and "posts going out", and it is why `publish_post` still says no. Filling this page changes what
the app can *tell you*, not what it can *do*.
