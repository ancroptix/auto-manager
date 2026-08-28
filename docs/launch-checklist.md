# Launch checklist — everything left, in order

Nothing here needs code, a terminal, or typing a password to anyone. It is eight
copy-pastes in the right order. The order matters in three places only, and each one
says why.

**Who does what.** Every step below is yours, because every one of them is a click in a
browser where *you* are logged in. I cannot do any of them for you: this workspace can
reach GitHub and nothing else. That is measured, not assumed — from inside the sandbox,
`api.render.com`, `api.telegram.org` and `api.supabase.com` all complete a TCP
connection and then have their TLS handshake cut, and a Postgres packet to your pooler
is reset on read. So if anyone offers to run your Render or Supabase setup for you,
including me, the honest answer is that it does not work from here.

**Which is also why you must not send keys into this chat.** Not because I would
misuse them — because they would be permanently readable in a log for no benefit. Three
credentials have already been pasted into a chat during this project:

| Leaked | Status | Do this |
| --- | --- | --- |
| Render API key | revoked ✅ | nothing |
| `sb_secret_...` Supabase secret key | **still live** | step 1 |
| Supabase database password | **still live** | step 2 |

That app never uses the Supabase API key at all (it talks to Postgres directly), so
deleting it cannot break anything.

---

## 1. Delete the exposed Supabase secret key — 1 minute

Supabase dashboard → bottom of the left sidebar: **Project Settings** → **API Keys**.

- In the **Secret keys** list: **Delete** on the `sb_secret_...` row you pasted earlier.
  Irreversible, and you do not need a replacement.
- In **Legacy API Keys**: turn off (or delete) `anon` and `service_role` if they are
  still enabled. Same reason: nothing in this app uses them.

Keep the `sb_publishable_...` key if it exists; it is public by design and unused here too.

## 2. Change the database password — 1 minute

**Project Settings** → **Database**. Under **Connection string** there is **Reset
database password**. (Supabase renames panels often; the field you want is the password
sitting next to the username `postgres.qxvkowedsmlzjkmodapv`.)

Generate a long one, paste it into your password manager, then keep the tab open — step 4
needs it. Every connection using the old password drops, which is fine because nothing is
connected yet.

## 3. Create the Telegram bot — 2 minutes

In Telegram, **@BotFather**:

1. `/newbot` → display name `Auto Manager Control` → username ending in `bot`, e.g.
   `ycanime_control_bot`.
2. It replies with a token like `7123456789:AAH...`. **Put it straight into Render in
   step 4. Do not paste it here, and do not paste it into a note app.**
3. `/setprivacy` → your bot → **Disable**. Without this the bot cannot see your
   commands in groups and looks broken.
4. `/setjoingroups` → your bot → **Disable**. It has no business being in a group at all.

Then **@userinfobot** (or any ID bot): it replies with your numeric ID. You need two IDs
and both are safe to type into chat if you want me to repeat them back to you:

- the ID of your **main** account (the one that will own the channels) →
  `TELEGRAM_MAIN_ADMIN_USER_ID`
- the ID of the **spare** account that will do the posting → `TELEGRAM_OWNER_USER_IDS`
  (that one takes a list; for now it is just the spare, and you can add your main ID
  later separated by a comma)

If both IDs are the same account, stop and tell me — the design assumes the poster is not
the admin who approves things.

## 4. Render: 12 environment variables, then save

Render dashboard → your `auto-manager` service → **Environment** → **Add Environment
Value** per row: the name before the colon goes in **Key**, the rest in **Value**. The
angle-bracket entries are the ones you fill in.

```env
APP_MODE: shadow
DATABASE_URL: postgresql://postgres.qxvkowedsmlzjkmodapv:<NEW-PASSWORD-FROM-STEP-2>@aws-0-ap-northeast-1.pooler.supabase.com:5432/postgres?sslmode=require
DB_SSL: require
CONTROL_TOKEN: <forty-characters-you-never-show-anyone>
TELEGRAM_API_ID: <number from step 5>
TELEGRAM_API_HASH: <hex string from step 5>
TELEGRAM_BOT_TOKEN: <the BotFather token>
TELEGRAM_OWNER_USER_IDS: <spare account's numeric id>
TELEGRAM_MAIN_ADMIN_USER_ID: <main account's numeric id>
TELEGRAM_SESSION_SOURCE: both
BOT_ALLOW_LOGIN: true
PROBE_ON_BOOT: 0
```

Three things about that list, because each has bitten someone:

* **`POOLER`, port 5432, session mode.** Port 6543 is transaction mode and breaks the
  prepared statements this app uses. The direct connection
  (`db.<ref>.supabase.co`) is IPv6-only on the free plan and Render's free instances have
  no IPv4 route to it — the pooler is not a workaround, it is the only working door.
* **`DATABASE_URL` must be `Secret`.** Render shows a toggle per row; anything not
  marked secret is readable in the dashboard's plaintext.
* **`CONTROL_TOKEN`**: mash the keyboard in that field for 40 characters. Nobody has to
  read it or type it until an emergency, and it is the only way to press the kill switch
  without Telegram, so a sloppy random string is exactly what you want.

While you are here, check **Settings → Info** at the top: **Repository** should be
`ancroptix/auto-manager` and **Branch** should be
`arena/01a04370-auto-manager`. All 61 files of this project live on that branch; `main`
is a single placeholder file until PR #1 is merged. If Render is pointed at `main` you
will deploy a repository that does not contain this app and every step below will fail
confusingly. Say "merge it" and I will merge the PR, then you can switch Branch to
`main` — the choice is yours and neither breaks anything.

## 5. Telegram API credentials — 3 minutes

<https://my.telegram.org> → log in **as the spare account** (it sends a code to that
account's phone) → **API development tools** → fill in an app title, leave the URL blank
→ **Create application**.

You get `api_id` (a number) and `api_hash` (32 hex characters). Both go in Render's
Environment, `api_hash` marked secret. They authorize the *app*, not the account, and
they are what makes the login in step 7 possible.

## 6. Give the database its tables — 2 minutes, one paste

Open <https://raw.githubusercontent.com/ancroptix/auto-manager/arena/01a04370-auto-manager/ops/apply-all.sql>
→ `Ctrl+A`, `Ctrl+C` (it is ~1,320 lines; do not retype any of it) → Supabase dashboard →
**SQL Editor** → **New query** → paste → **Run**.

One run, once. It creates 27 tables and views, 14 functions and 35 config rows,
including the caption text you approved. Re-running it is safe by design — every
statement is `if not exists`, `or replace`, or guarded by "only if the value still
equals the placeholder we shipped", which is why your caption edits survive a re-apply
while an untouched placeholder still gets filled in.

If paste fails with a size limit, tell me and I will split it into the four migrations
under `supabase/migrations/` for you to run in filename order.

## 7. Log the spare account in — from your phone

Wait for the deploy to finish (Render → Events ends with `deploy complete`), then open
your bot in Telegram and send:

```text
/login spare +91XXXXXXXXXX
```

Telegram texts a code to that phone. Reply with `/code 123456`. If the account has a
2FA password, the bot asks for it next. Then `/sessions` to see it listed, and `/status`
to see what the service thinks is blocking it.

The bot deletes your code message and never repeats the number back. The session string
goes into `app.telegram_session` in your own database and is never printed anywhere —
that is the entire reason this path exists instead of pasting a session string into a
dashboard.

If `BOT_ALLOW_LOGIN` is still `true` an hour later, that is fine while you set up; put it
to `false` once the session works. And if the bot says nothing at all: the token, the
owner ID, or the branch is wrong — `/status` cannot answer, which is the tell that
`TELEGRAM_BOT_TOKEN` did not make it in.

## 8. Teach the app the two third-party bots — this is the real finish line

Right now three jobs work for real (`reconciliation`, `ingest_media`, `thumbnail_screen`)
and eight exist as deliberate errors: the storage bot's menus and Channel Help's
inline-button form are private protocols that nobody can read from documentation. They
have to be observed once, from inside your deployment, with your logged-in spare
account.

1. In Render → Environment, set `PROBE_ON_BOOT=1` and save (restarts the service).
2. DM the bot `/probe`. It messages both bots the safe questions only — `/start`, `/help`,
   `/id` — never anything that uploads, deletes or spends quota, and it refuses to send
   a second time in one run.
3. A report comes back in the chat (it is capped at ~3,800 characters; if the menu is
   longer, ask `/probe` again after the app has been idle and it will re-run). **Paste
   that report here.** It contains no secrets: it is button labels and replies.
4. Set `PROBE_ON_BOOT=0` afterwards, so a restart never re-probes.

Once I have the report I implement `storage_upload`, `archive_media`, `publish_post` and
the rest against what the bots actually said. That is the last code in this project.

**A faster version of step 8, if you are willing:** open `@anime_hindifilesbot`, press
Start, and **screenshot its reply**, then do the same for `@chelpbot`'s post-composer
after its `/start`. Screenshots are images, not credentials — you can paste them into
this chat safely, and they answer the same questions the probe does. The spare account
must be able to see those bots' menus, so do it from the account that will run the
pipeline. If you can send me those two images, I can start on the handlers immediately
and the probe becomes a confirmation rather than a requirement.

---

## Then, and only then

1. `APP_MODE=live` in Render, save.
2. UptimeRobot (free) → a 5-minute HTTP monitor on
   `https://<your-render-service>.onrender.com/health`. Render's free tier sleeps after
   15 idle minutes; the worker is a background loop with no traffic of its own, so
   without a pinger it sleeps mid-job. Note the arithmetic: pinging 24/7 burns ~744 of
   your 750 instance-hours a month, which is the whole free allowance. That is a choice
   you made when you picked the free tier, and it is worth knowing the ceiling is that
   close.
3. Tell me the two user IDs and paste the probe report, and I will finish the handlers.

## What is deliberately *not* here

* **No session string pasted anywhere.** Step 7 replaced that.
* **No bot token, DB password, API key or 2FA password typed into this chat.** The login
  codes are the only credentials you type to the bot, and only in your own DM with your
  own bot.
* **Nothing auto-deletes from the archive channel**, ever — there is no config row for it.
* **No DM pacing tricks.** Join requests get one forwarded reply, nothing more.
* `scripts/login.py` still exists as an offline fallback for the day the bot path is
  broken; you do not need it, and if you ever run it, its output is a secret.
