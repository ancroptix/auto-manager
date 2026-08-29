# The control bot (your remote, and how the account gets logged in)

This is the part you use from your phone. Everything else in this repository is
plumbing; this is the interface.

It exists so that **you never have to run Python, open a terminal, or paste a
session string anywhere**. The bot asks you for a phone number and the code
Telegram sends, performs the login itself inside the deployed service, and stores
the result in your database.

## What the bot can and cannot do

| It can | It cannot |
| --- | --- |
| tell you what the queue is doing (`/status`) | post, edit or delete anything in your channels |
| pause and resume the worker (`/pause`, `/resume`) | download or re-upload media |
| queue a reconciliation (`/reconcile`) | create a channel or change permissions |
| discover the two third-party bots' menus (`/probe`) | drive another bot's inline menus for you |
| log your spare account in and store its session (`/login`) | read the archive, because a bot cannot read a foreign channel's history |

That asymmetry is the design, not a gap: the *user session* the bot obtains is what
does pipeline work, and the bot is only the switchboard. A Bot API token can send
messages it is given; it has no power over your channels.

There is one more thing it will not do: **`/shutdown` is HTTP-only**
(`POST /control/shutdown`). A kill switch that can be pressed from a chat window is
one lost phone away from being pressed by someone else, so the emergency stop stays
behind a bearer token you type into a terminal you control. `/pause` — which stops
work without stopping the process, and is reversible from either surface — is the
one you will ever need from Telegram.

## 1. Create the bot (2 minutes, in Telegram)

1. Open **@BotFather** → `/newbot`.
2. Display name: `Auto Manager Control`.
3. Username: must end in `bot`, e.g. `ycanime_control_bot`.
4. BotFather replies with a token that looks like `7123456789:AAH…` — **that token
   goes into Render's environment and nowhere else**. Not into a chat with anyone,
   not into this repository, not into an issue.
5. In @BotFather, send `/setprivacy` → choose your bot → **Disable**. This does not
   make the bot private; it lets it *see* messages in groups. Not required for the
   control bot (it refuses group commands anyway), but harmless to set.
6. Optional and recommended: `/setdescription` → "Owner-only control for the
   auto-manager pipeline. It answers nobody else." And `/setuserpic` so a lookalike
   bot is obvious at a glance.

Then in Render → your service → **Environment**:

| Key | Value |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | the token from BotFather |
| `TELEGRAM_OWNER_USER_IDS` | your numeric user ID, comma-separated if more than one |
| `TELEGRAM_SESSION_SOURCE` | `both` |
| `BOT_ALLOW_LOGIN` | `true` while setting up, `false` afterwards |

Your numeric user ID: message **@userinfobot** and it replies with a number like
`712345678`. That number is not a secret; it is the only address the bot will ever
reply to.

The bot starts by itself on the next deploy. If `TELEGRAM_BOT_TOKEN` is set but no
owner IDs are, it refuses to start at all and the log says so — a bot that answers
wherever its token leaks is a worse outcome than a bot that stays silent.

## 2. Log the spare account in

Message your bot privately. Nothing below leaves your phone except as a message you
will have deleted.

```
/login spare +919876543210
```

* `spare` is just the name you want this session stored under — letters, numbers,
  `-` or `_`.
* The number must be international format: `+`, country code, then the digits.

The bot replies `code sent to +91…3210` — the middle of your number is not echoed
back, so a screenshot of the chat cannot be used to dial you.

Then reply with the digits Telegram sent you:

```
/code 482913
```

(You may also send just `482913`. Same thing.)

If the account has 2FA it will ask once more:

```
/password your-2fa-password
```

On success:

```
connected as @your_spare_account, stored as 'spare' (312 chars, active).

The session string was never shown in this chat and cannot be read back from it.
/sessions lists what is stored. the service reconnected with it, so the write jobs
can run
```

What happened underneath: the service connected to Telegram's MTProto API from
inside the container, completed `auth.sendCode` → `auth.signIn`, and wrote the
resulting `StringSession` into `app.telegram_session` — a table with row-level
security on and zero policies, reachable only by the role the app itself uses. The
three messages *you* typed — the phone number, the code, the password — are
deleted from the chat as soon as each one has been used, the password is cleared
from memory in a `finally`, and nothing sensitive is ever included in a reply:
every outgoing line is passed through a scrubber that removes session-shaped text
even inside an error message, and a phone number is masked before it is repeated.

The bot's own replies are never deleted. An earlier version deleted them after a
"short grace period" and the grace period was zero, so the question "reply with
/code 123456" appeared and vanished in the same second — which reads as a broken
bot, not a careful one. Deleting the secret means deleting the copy that holds the
secret, and the only such copies are your messages.

If you would rather keep those messages — an audit you want to hold — set
`bot.delete_sensitive` to `false` in `app.config`. The service reads that row, and
`bot.login_ttl_seconds` (how long a code stays acceptable), when it builds this bot;
both take effect on the next boot, and nothing else in the login flow moves.

The connection that request runs on belongs to the attempt: it is opened before the
code is asked for, kept alive only while your code (and 2FA password, if you use
one) is being typed, and closed when the attempt ends. An abandoned or failed login
therefore leaves no half-open session on the account and nothing sitting in the
container's memory — which matters on a free Render instance that restarts on its own.

With `APP_MODE=live` already set, nothing else has to happen: the reply above is
the service's own reconnect attempt, and its last sentence is the result. With
shadow mode, the session waits until the mode is switched on, and the reply says so
instead of implying that writes are running. The service also reads the stored
session at boot, if it restarts on its own.

## 3. Everything else you can ask it

| Command | Result |
| --- | --- |
| `/start` or `/help` | this list — the Start button in Telegram sends `/start`, so it works even on a fresh chat |
| `/status` | mode, queue depth, paused state, what is blocked and the first line of why, thumbnails waiting on you, the four settings that decide publishing |
| `/pause` *(reason)* | stops claiming jobs. The reason is stored and shown in `/status` |
| `/resume` | claims again on the next poll |
| `/reconcile` | reclaims expired leases and queues a reconciliation pass |
| `/probe` | discovery against the storage bot and Channel Help: it opens both from the logged-in account and reads their menus back, and it posts nothing in your channels. But it *does* send, so it refuses to run in shadow mode. To probe without launching the pipeline: `APP_MODE=live` **and** `WORKER_ENABLED=false` in Render, save, `/probe`, then put `WORKER_ENABLED` back — with the worker off no job is claimed, so nothing reaches your channels while the questions are asked. The report is written to this chat, never to a channel |
| `/source <@handle\|channel id> [series <name>] [audio <kind>] [season <n>]` | what a *files-only* source channel carries, stated once for the whole channel instead of guessed per file. With no arguments it shows what is declared; `clear` stops assuming. It never re-decides a file that was already decided — parked ones are re-read on the next scan |
| `/joinmsg [show\|options\|use <n>\|set <text>\|clear]` | what a join requester is told, when you later run a campaign at them. `options` lists three drafts with the promise each one makes; `use 2` saves one, `set …` saves your own words (`{name}` and `{series}` are filled at send time), `clear` empties it so the app may contact nobody. Saving is not sending, and the reply says so — the sender is the blocked job kind `join_request_campaign`. It cannot approve or decline anyone, and it refuses a message that carries an invite link |
| `/inplace <@handle\|channel id> [from <@other>] [plan\|off]` | publish by editing instead of posting: the channel's own file messages get the approved caption written onto them. No new channel, no copy, no delete, no buttons. `plan` shows what it would do and changes nothing; `from` compares with a second channel so episodes only *it* has are forwarded in; `off` goes back to the link route |
| `/declare <series> <season> <episodes>` | state how long a season is — the only thing that can fill **◎ Total Episodes**, and the only thing that can make a season count as complete. It publishes nothing itself; the batch post stays the publisher's decision. `/declare bleach 2 tba` takes the claim back |
| `/card <channel> <message id\|show\|clear>` | the post in that destination channel whose link the announcements channel carries. `show` reports whether a shareable link was ever returned; `clear` un-names it (the stored link stays, because deleting is not this program's verb). One number, per channel: without it `publish_post` blocks rather than announcing the invite |
| `/sticker <series> <season> from <channel> <message id>` | which sticker opens a season. Telegram addresses a sticker by the message carrying it, so a pack name or an install link is not an address; the job forwards that message into the destination before the season's first episode post |
| `/campaign <channel> [new\|text\|plan\|confirm\|pause\|abort] <name>` | a join-request campaign, in two deliberate steps: `new` drafts it from the saved `/joinmsg` wording (or `text` writes its own), `plan` shows the rules, the rate and the pending count and prints a code, `confirm <name> <code>` makes it `ready` and queues the job. The code is derived from the row and its exact text, so editing the wording invalidates an old confirmation. `abort` stops it and leaves every row in place |
| `/sessions` | name, kind, account, age, character count — never contents |
| `/use backup` | make another stored session the live one |
| `/forget spare` | delete the row |
| `/cancel` | drop a pending login |

Every one of these is a thin wrapper over something the HTTP control surface
already exposes; the bot adds no authority of its own.

### A long answer is split, never cut

Telegram's limit is 4096 characters per message, and a probe report or a blocked-jobs list
can pass it. `BotApi.send` then splits the text at line boundaries and sends the parts in
order, returning the id of the first — so the login flow, which deletes its own prompt,
still deletes the message it asked about. A published *post* is the opposite case:
`app/sender.py` refuses to send an over-long caption rather than publish half of one, which
is why the same number means "split" in a private chat and "stop" in a channel.

The probe report keeps its own smaller budget (`MAX_REPORT_CHARS`, deliberately under the
transport's limit) so the normal case is one paste-able message; what is *audited* into
`app.audit_log` is the uncapped render, because the truncation note tells the operator the
full version is in the database and that sentence has to be true.

### What `/inplace` answers before you ask it twice

The preview line it prints is not decoration — it is the same plan the publisher
will act on, computed from the rows in the database:

```
what I would do with the 12 messages of this channel:
  12 caption
  no source channel to compare with: everything here is captioned from this channel alone

no new channel, no copy, no deletion, and no buttons under the post …
```

Three replies to expect, all deliberate:

* **a question instead of an edit** — if a message's existing text is more than an
  episode label (a note, a mirror link, a date), it is left alone and named:
  `msg 902: the existing caption looks like a note`. Telegram keeps no history of a
  caption, so guessing costs you the text. Setting `inplace.overwrite_notes` to
  `"replace"` in `app.config` makes it write anyway, and the replaced text is still
  stored in `app.destination_post.caption_previous` — the only copy that exists.
* **"I did not switch this channel to in-place mode"** — when we are an ordinary member there, or when no live
  session has ever read our rights. The channel you named is then a source, and the reply says the destination
  `… Anime in Hindi` will be created: being unable to caption in place is never a reason to skip building it. It
  names the command that makes the naming safe (`/source … series <name> audio hindi`) and, only when rights are
  unread, the one column you can set by hand if this really is your own channel. And switching the mode on
  changes where a caption is written, never whether the job runs: the file still goes to storage, the link
  still comes back, the post is still made. "There should be no destination channels with nude files" is the
  rule this command refuses to quietly drop.
* **"this command changed the plan, not the channel"** — true until the MTProto write
  layer is wired on the live account. `/inplace` is safe to run today; the edits follow
  once the session can send `EditMessage`.

## 4. Closing the door afterwards

Once the session is stored and `APP_MODE=live` works, set `BOT_ALLOW_LOGIN=false`.
A deployment that cannot start a new login has one fewer way in, and `/use` and
`/sessions` keep working.

## 5. If you want it to stop being able to log in *at all*

Removing the credential from Telegram's side is a manual step, and it is the only
one that actually revokes a session:

1. On the spare account: **Settings → Privacy and Security → Devices** → terminate
   the `auto-manager` session (or *Terminate all other sessions*).
2. In Render: delete `TELEGRAM_BOT_TOKEN` to silence the control bot entirely.

Changing the account's password does **not** invalidate a session string. Do not
rely on it as a revocation step.

## 6. What it does when something is wrong

* `TELEGRAM_BOT_TOKEN` missing → no bot, everything else keeps running. `/health`
  and the queue are unaffected; the log says the bot could not start.
* Token wrong or revoked → the bot logs a poll error and retries with a delay; the
  service stays healthy because one credential must not be able to take the whole
  app down.
* Owners unset → the bot refuses to be constructed. `/status` and `/login` are not
  "restricted"; they are simply not reachable by anyone, including you.
* Telegram rejects the code three times → the flow is closed and *nothing further is
  sent to Telegram*. A fourth guess would be rejected anyway, and repeated failures
  are what get an account limited.
* Three `/login` starts in ten minutes → refused locally, before asking Telegram for
  a code. That limit exists to keep us on the right side of Telegram's own flood
  control, not to be user-hostile.
* Telegram accepted the code (and the password) but the service could not read a
  session string out of the client → the reply says exactly that: the credentials
  worked, nothing was stored, and the flow is closed rather than offering another try
  at a code that is already spent. It names the one place to check for a login nobody
  stored — **Settings → Privacy and Security → Devices** — because that session is
  live on the account until you end it. This happened once for real, when the code
  asked Telethon for `as_string()`, which version 1.44 does not have (`save()` is the
  name); `tests/test_mtproto_login.py` now binds every session attribute this project
  calls to the installed class, so the next rename fails a test instead of a login.
* `DATABASE_URL` missing or migrations not applied → the login completes at
  Telegram and then says plainly that it could not store the session. It never
  pretends to have succeeded.
* A code request that Telegram answers with a rate-limit (`FloodWaitError`) → the
  reply carries the number of seconds Telegram itself named, and nothing is retried
  before then. Pushing through that wait is how an account gets restricted further.
* A write Telegram refuses — a channel this session cannot post in, a peer it has
  never met — → the job is parked as blocked with that sentence, and `/status` shows
  it. The queue does not call a silent channel a success.
