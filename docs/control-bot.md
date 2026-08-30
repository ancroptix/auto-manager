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

Where a choice is genuinely yours, the reply arrives with buttons under it — and **a button is a command
the bot typed for you**, nothing more. Every `callback_data` in `app/keyboards.py` is a `/command` string
that goes through the same owner check, the same private-chat check and the same router as the words, so
tapping `gate → off` is `… gate off` with the typing done. That is what keeps the friendly half of this
bot from being the wide half: there is no action reachable by a thumb that is not reachable from the
keyboard, and no button carries a link the operator could be sent to.

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
| `/source <@handle\|channel id> [series <name>] [audio <kind>] [season <n>] [title <text>]` | what a *files-only* source channel carries, stated once for the whole channel instead of guessed per file. `title` renames the row itself — what this bot calls it back to you, and, when no series is declared, one of the two signals `app/ingest.py` may read a series name out of (one signal is not two: it never founds a channel on its own). A channel added by number arrives with none, which is why the word exists here at all. With no arguments it shows what is declared **and what is switched**; `clear` stops assuming. It never re-decides a file that was already decided — parked ones are re-read on the next scan |
| `/source <@handle\|channel id> add [series <name>] [title <text>]` | start watching that channel: the row in `app.source_channel` is written here, with its defaults printed back. Its own verb on purpose — a row is the decision to read a channel, so no other command makes one, and a lookup that finds nothing says `add` instead of naming a table |
| `/source <channel> gate\|subs\|watch on\|off` | the three switches whose columns production code reads: `gate` is `require_hindi_audio`, `subs` is `include_subbed`, `watch` is `mode` (`on` is `full`, `off` is `ignore`). One column per command, and the same words switch it back |
| `/discover [plan\|add <n>\|add all\|pair <n>\|pair all\|auto on\|off]` | what the spare account can see, sorted into the two kinds of channel this program has: a channel we can only read is a **source**, a channel we can post in is a **destination**, and two channels with the same name are the same **series** — `Mob Psycho 100` and `Mob Psycho 100` are one show with two halves, which is what the `{TITLE} Anime in Hindi` spelling strips down to. Rights are asked of Telegram per channel (`channels.getParticipant`), not read out of the session's cached list, because that list often has no rights on it for a channel the account owns. `plan` (bare `/discover`) writes nothing; `pair 1` sets one series up in one go — its `app.series` row if nothing has named it yet, the destination row, the source row, and `destination_id` linking them; `add 3` writes one row through the same `app/sourcecfg` writer `/source … add` uses; `auto on` lets the reconciliation job re-read the roles and apply the switch by itself. It creates no channel in Telegram, sends nothing, and refuses to stop reading a series' only source |
| `/sources` | every source channel with its switches as buttons — the same list the menu's `🎙 Sources` screen renders, from the same builder, so a typed line and a tap cannot disagree about a column. It is also what the console's "this row is gone" reply points at, which is the only reason that sentence is allowed to say it |
| `/destination [<series\|@handle\|channel id>] [card <id\|show\|clear>\|campaigns\|campaign new <name>\|episodes <season> <count\|tba>\|inplace [plan\|off]]` | the channel a series publishes into, and the four things it can be set to. Bare `/destination` (or `/destinations`) lists them, 📤 for a channel that exists and 🏗 for a row whose channel is not built yet. Every action is delegated: `/card`, `/campaign`, `/declare` and `/inplace` stay the only paths that touch those columns, so this command spares the remembering and adds no new write. A destination is addressed by its own number, its title, or the handle of the source that feeds it — `app.destination` stores no username of its own |
| `/archive [<@handle\|channel id> add [title <name>]]` | which private channel holds the master copy: bare `/archive` reads the list, `add` writes the missing row the archive job blocks on. The first row added is `is_primary`, because two primaries would make `app/writers.py` pick between them |
| `/joinmsg [show\|options\|use <n>\|set <text>\|clear]` | what a join requester is told, when you later run a campaign at them. `options` lists three drafts with the promise each one makes; `use 2` saves one, `set …` saves your own words (`{name}` and `{series}` are filled at send time), `clear` empties it so the app may contact nobody. Saving is not sending, and the reply says so — sending is a campaign per channel, started from `/joinreq`. It cannot approve or decline anyone, and it refuses a message that carries an invite link |
| `/joinreq [plan\|open <ref>\|start <ref>\|go <ref>\|stop <ref>\|add\|file <n>]` | the join-request campaign by button: the channels this account can **post in** (rights asked of Telegram on the spot, not read from a cached list), the exact words and who is waiting, then one tap to start. Sending goes to one person every **3 seconds**, nobody twice, up to the campaign's hourly ceiling; a run that does not finish hands the rest to the next one, so a restart or a spin-down resumes the list instead of stranding it. `add` lists the postable channels that have no row yet and `file <n>` writes one through discovery's writer (founding the series row from the channel's own name when nothing else names it). It never approves or declines a request, and no message is sent before the plan has been read on screen |
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

### Configuring a channel from this chat, not from the dashboard

On 2026-08-29 the operator put the reason plainly: *"mai baar baar supabase nhi kholne wala … bot me hi
toggle on/off jaise options jod do"*. The bot already writes this database — that is what `/card`,
`/sticker`, `/declare` and `/source … series` have always done — so making it able to write the two rows
the whole setup was waiting on (`app.source_channel`, `app.archive_channel`) moves no authority, it only
moves the form. `app/sourcecfg.py` is where those rules live, and three of them are held by tests rather
than by intent:

* **A row that starts something gets its own verb.** `/source <channel> add` exists as a command because
  adding a source channel is the decision to read that channel. A `/source` that finds no row now names
  `add` rather than a table, and nothing in the bot creates a row as a side effect of a question.
* **Only a column something reads may be switched.** `app.source_channel` also holds `priority`,
  `is_joined`, `active` and the `monitor_only` mode, and no code outside a probe query acts on them — so
  there is deliberately no switch for them, and `watch` writes `mode` rather than `active` for exactly
  that reason: `app/ingest.py` compares against `ignore`, and nothing at all compares against `false` in
  `active`. A toggle that looks like a pause button and is not one is worse than no toggle.
  `tests/test_sourcecfg.py` checks each column named in an insert against the rest of `app/`.
* **Nothing destructive, and nothing invented.** One insert with `on conflict do nothing`, updates that
  name a single column, no delete anywhere in the module. A `@handle` is resolved with one Telegram call
  and the title comes from the answer; if this deployment cannot ask Telegram, a channel *number* is
  written with the reply saying "not checked against Telegram" out loud, and a handle is refused outright
  rather than given an invented id.

The dashboard still works, and is still the only way to set a column with no switch. What changed is that
it stopped being the first step.

And the switches come as buttons now, under the message that explains them: `gate → off`, `subs → on`,
`watch → off`, one per `sourcecfg.TOGGLES` entry, each carrying the exact `on`/`off` command it stands
for. A label names the state the press *writes*, never the state it found, because a button that renames
itself under the thumb is a button whose second press does the opposite of what the first appeared to
promise. A `callback_data` longer than Telegram's 64 bytes drops the **button**, not the message: no
keyboard is readable, and a truncated command is a different command. `/joinmsg options` puts the three
drafts on one row for the same reason — a choice that takes typing is a choice that gets skipped.

### The menu: every one of those, tappable

`app/console.py` is the screen layer, and it is a layer: the words in §3 still work, and every button runs
the same handler the words run through. That is not a shortcut, it is the safety argument — a console with
its own write path would be a second bot to keep correct, and the second one is always the one that lies.

Open it with `🏠 Open the menu` under `/help`. Six screens:

| screen | what it holds | how you reach it |
| --- | --- | --- |
| `main` | mode, whether sending is on, queue depth, how many channels and sessions exist | the first thing `/help` offers |
| `sources` | one row per configured channel, each labelled 👁 watching or 💤 ignored, with the series name after it | `🎙 Sources` |
| `queue` | ready jobs and the pause state, with only the button that changes it (Pause when running, Resume when paused) | `🔌 Queue` |
| `bots` | which storage bot, Channel Help bot and link provider are configured, plus `/probe` and `/sessions` | `🤖 Bots` |
| `discover` | one `✅ Set up <series>` per show this account has both halves of, the channels it would wire with the reason under each, and the auto switch | `🔎 Find channels` |
| `destinations` | one row per series' own channel — 📤 when the channel exists in Telegram, 🏗 when only the row does — and each one opens its own screen | `📤 Destinations` |
| `sessions` | the stored logins, by account name and username, with `▶ Use` and `🧹 Forget`. Never a session string, never even its length | `👤 Sessions` |
| `joinmsg` | the three drafts numbered as `/joinmsg` numbers them, the wording now saved, and the way to say nothing | `📩 Join message` |
| `joinreq` | one line per publishing channel with what this account may do there, and `➕ Add a channel` for the ones with no row yet — `📨` on a channel opens its message, pace and start tap | `📨 Who is waiting` |
| `help` | the command list from §3, unchanged, because a button that hides its own words is a button with no manual | `❓ Help` |

Tapping a channel's name on `sources` opens that channel's own screen: the three switches, the audio
vocabulary as direct picks — one button per spelling `normalize.DECLARED_AUDIO` accepts (`hindi`, `dual`,
`dual_audio`, `multi`, `multi_audio`, `subbed`, `subbed_only`, `unknown`), so nobody has to remember how the
column is spelled — plus the season, the two names, the season's episode count, and `🖼 Show the plan` /
`🖼 Caption in place` / `🔗 Links only` for the in-place half. `📤 Where its files are published` crosses to
the destination screen for that series, and it crosses on the `destination_id` the ingest side recorded
rather than on a title match; when the two are not linked yet, that is what the button says.

`🔎 Find channels` is the one screen that starts with what Telegram says rather than with what you type: it
walks the spare account's dialog list and sorts it by role — a channel this account can only **read** is
offered as a source, a channel it can **post in** is offered as a destination — and it pairs the two by name,
because a source called `Mob Psycho 100` and a destination called `Mob Psycho 100` are the same show with the
account on either side of it. The `{TITLE} Anime in Hindi` spelling is not a second rule to satisfy: it
strips down into that same name, and the normalisation used is the one the file pipeline files series under.
One `✅ Set up <series>` writes the whole setup — `app.series` if nothing has named it yet (founded by
`app/ingest.ensure_series`, the statement the pipeline itself uses, with the reply saying the name came from
the channel), the destination row, the source row, and the link — and `✅ Set up every pair on this page` does
it for all of them; a leftover channel with nothing to pair with still files alone through the same
`app/sourcecfg.py` writers `/source … add` uses. What it will not do is in the reply, not in a footnote: it
creates no channel in Telegram, it names no series for a channel whose title says nothing, and it refuses to
stop reading a series' only source — a switch that would leave a season with nothing to read is proposed and
left alone. `✨ Let it switch on its own` is the one part that keeps working by itself: with it on, every
reconciliation (which runs at boot) re-reads the roles and applies a role change the first time it notices.

`📨 Who is waiting` (`/joinreq`) is the same campaign `/campaign` drives, with the typing taken out. It lists
only the channels this account can post in — rights asked of Telegram on the spot, because a campaign writes
from that session — and `➕ Add a channel` files one that has no row yet through the same writer discovery
uses, founding the series row from the channel's own name and saying so in the reply. The campaign is drafted
from the saved `/joinmsg` wording under one fixed name, so no name has to be invented, and what `confirm`
gates with a code the wizard computes instead of asking for: the plan (the exact words, the ceiling, who is
waiting right now) is the screen before the start tap, so the tap is the reading of it. That headcount is
read on a session of the bot's own (`_pending_requests`), because a plan that counts people is only worth
tapping if the number is today's — and when the read fails the plan says what failed instead of showing `0`,
since zero reads as "nobody is waiting" and would be the one wrong number on the screen. `app/writers.py` then
writes to **one person every 3 seconds** and stops at 20 per run so a run cannot outlive its job lease; the
rest is handed to the next run under a key counted by the contacts already recorded, which is the same key the
boot sweep uses, so a Render spin-down resumes a half-sent list instead of stranding it — and the two paths
cannot queue the same send twice. `⏸ Stop` pauses after the message in flight; what was sent stays sent, and
nothing in this flow approves or declines a join request.

A destination's own screen is where the announcement is built: what it publishes, the card post a shareable
link is made from and whether one was ever returned, the three in-place taps, `📣 Campaigns` and
`➕ Draft one`, and `📅 Episodes in a season`. The bio and the channel picture are deliberately **not**
buttons: they are written while the channel is built, and a screen that offered a knob for something no
command reads is a knob that lies.

`↻ Refresh` is the last row of every screen, and every screen but the menu has a `◀` beside it: a screen is a
snapshot, and the refresh is the only honest way to make it true again. Nothing on a screen is a knob the app
does not read.

**Typing is left for the one thing a button cannot carry: a name, a number, or your own wording.** A screen
that needs one sends `one more thing` with the question, and the message that asks it has the exit in it —
`✖ Stop here`, which drops the answer and changes nothing. A tap of any other button also drops it, because
a question left open would otherwise take whatever you type next as its answer.

Two more rules, both earned:

- **After a write, the screen is read back, not remembered.** The refreshed screen is sent as the second
  message and its first line is `ran: `/source @something gate off` — the exact command your tap became.
  It is not decoration: it is the line that goes in a bug report, and the only way to tell a screen that
  lied from a write that worked.
- **A tap may not be sent a word the router does not serve.** `/destination` exists because two refusals in
  this bot used to end in `the command is /destinations` and `/sources`, and neither was routed: the operator
  typed them, the bot stayed silent, and silence after a promise is how a bot stops being trusted.
  `tests/test_console.py::test_no_reply_promise_a_command_the_router_does_not_serve` now reads every string
  literal in the two files and fails for any `/word` that is shown to a human and not routed.
- **A row reference names its table.** `r:s3:gate:off` is a source row and `r:d21:card:show` a destination
  row, because both tables have a row 3 and a button that could not tell them apart is one typo away from
  editing the wrong channel. The older letter-less spelling still means the source row it always meant, so
  the buttons under an open message do not die when this ships.
- **A long name costs a button, never a fact.** `callback_data` has 64 bytes and labels have their own cap.
  Over either, `app/keyboards.py` drops the button and the screen keeps the whole sentence in its text — no
  truncation, because a truncated command is a different command and a truncated label is a promise about
  something else. A row id, not a channel title, is what rides in the payload, which is why a 60-character
  private-channel title costs you nothing but the tap.

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
