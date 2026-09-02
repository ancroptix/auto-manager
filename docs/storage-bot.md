# The storage bot, as seen

`@anime_hindifilesbot` is not documented anywhere public, and its protocol cannot be read from the
outside: it answers a menu, takes a forwarded message, and hands back a link. So the rule was
"discover it live, once, from inside the deployment" — and the first piece of that discovery
arrived cheaper than expected: the operator sent screenshots of the bot's own command list on
2026-08-28.

That is *menu text*: worth recording exactly, worth re-checking, and not the protocol. The
protocol arrived the next day — five screenshots of the operator running `/batch` in their own clone
of this bot — and the answer to the question behind it ("whose bot is this, anyway?") turned out to be
the more useful half.

This document is the difference between a menu, a conversation, and a vendor's claim, kept in the repo
because a screenshot in a chat is how a protocol ends up half-remembered.

`/probe` re-reads the menu on request, and it reads it from `users.getFullUser` — the record Telegram's
own clients draw a bot's command list from — rather than from the screenshots. It is a cross-check, not a
source of protocol: a declared command list says `/batch` exists and what the bot says it does, and says
nothing about what the answer looks like, which is the half the screenshots carry. The first probe asked
the wrong counter instead — `bots.getBotInfo` — and all three bots answered `BOT_INVALID: This is not a
valid bot`. That is a fact about the request, not about the bots: the fields that response carries are
the ones an owner edits (`app_settings`, `verifier_settings`, `privacy_policy_url`), which is not what a
program talking *to* somebody else's bot is entitled to read. In a report, though, it read as three
uncooperative bots, and that is the cost of an unavailable hint printed without its reason.

Getting to the right answer took one more round, for a reason worth writing down: `users.getFullUser`
replies with a *wrapper* (`full_user`, `chats`, `users`), and the profile is inside it. Reading `bot_info`
off the wrapper raises nothing and returns nothing, so the report said three bots had no profile when our
read had stopped one level short. The two sentences the report keeps apart now are the two facts that
look alike from the outside: **this bot declares nothing** (no command list, no menu button, no profile
text — common for a clone, and itself the answer, because it means the menu on screen is the whole
protocol) and **we could not read the record**.

## The menu, verbatim

The middle column is the bot's wording, quoted rather than tidied — including its spelling. The
last column is ours, and is a plan, not a fact about the bot.

| Command | The bot's own words | What this pipeline would use it for |
| --- | --- | --- |
| `/start` | "Check i am alive" | the only command we may send during a probe: it opens the menu and proves the bot answers this account |
| `/genlink` | "To store a single message or file" | one episode file, one link — the single-variant case, and the one every quality of an episode goes through before the manifest is built |
| `/batch` | "To store mutiple messages from a channel" | a whole channel or season at once, which is what a season-batch link should be built from; whether it stores *forwarded copies* or references the source is unobserved |
| `/custom_batch` | "To store multiple random messages" | an explicit list of messages — the shape the ordered manifest actually wants, because a season is 360p through 2160p in a chosen order and not whatever a channel happens to contain |
| `/special_link` | "store multiple messages and get an editable link (moderators only)" | the edit-in-place half of the missing-quality rule: add a quality later, edit the link the post already carries, instead of posting a second time |
| `/universal_link` | "stores multiple messages that can be accessed from any of your clones (moderator only)." | one link that survives a clone being dropped, which is what a permanent season post needs; 'your clones' means the bot's own mirror set, and ours is not configured yet |
| `/shortener` | "To shorten any shareable links" | cosmetic; a short link in a caption is not a shorter file |
| `/settings` | "Customize Your settings as your need" | where retention and naming live, if they live anywhere; unread so far |
| `/broadcast` | "Broadcast a messages to users (moderators only)" | never sent. this is the bot messaging *people*, which is not this pipeline's job and is the sort of capability that gets an account restricted |
| `/ban` | "Ban a user (moderators only)" | never sent, for the same reason as /broadcast |
| `/unban` | "Unban a user (moderators only)" | never sent, for the same reason as /broadcast |

The same list lives in code as `app.storagebot.MENU`, and a test fails if this table stops
matching it, because the useful half of a recorded menu is knowing the day it changes.

## The `/batch` flow, as it was run

From the operator's five screenshots of 2026-08-29, in their own clone of the bot (chat title *Anime
files*, username `Anime_hindifilesbot`, web.telegram.org). The two prompts are quoted the way the bot
wrote them — capitalisation and doubled dots included — because they are the strings a future handler
has to recognise, and `app.storagebot.BATCH_FLOW` holds the same two lines.

1. `/batch`.
2. The bot asks: "Forward The Batch First Message From your Batch Channel (With Forward Tag).. or Give Me Batch First Message link from your batch channel"
3. The operator forwards a tagged post from the source channel — the screenshot shows
   `➡️ Forwarded from 🍉 The beginning after the end in hindi Episode 10`.
4. The bot asks: "Forward The Batch Last Message From Your Batch Channel (With Forward Tag).. or Give Me Batch last message link from your batch channel"
5. The operator forwards the last post of the range, which in their run was `❌ END OF SEASON ❌`.
6. The bot answers `Here is your link:` with `https://t.me/Anime_hindifilesbot?start=BQADAQAD…` and a
   green `⤴️ SHARE URL` button.

A single file took the same shape, with the answer labelled `Episode10Hindi.mp4`.

**What a range is.** Opening the link sends that token back to the bot, and the bot re-sends *every
message in the range, verbatim*: the video messages (each captioned with the source filename, e.g.
`[@Anime_Hindi_Files] season 01 episode 10 480p.mkv`, one of them missing its season number), the
source channel's own plain-text `Episode 10` label messages, and the closing `❌ END OF SEASON ❌`
post. A batch is a replay of a stretch of a channel, not a set of files re-indexed by the bot. Two
things follow:

- Four qualities of one episode sitting next to each other *are* one range, which is why the
  granularity the operator chose on 2026-08-29 — one batch per episode holding every quality, plus
  one final batch for the whole season — needs no new machinery, and why `/custom_batch` (an explicit
  message list) is the fallback for a season whose qualities do not sit together rather than the
  default.
- Whatever the source channel wrote rides along to the user. Our captions live in the destination
  post; nothing inside the bot's chat is ours to tidy, and nothing there is fixed by editing our post.

**What is ephemeral, and what is not.** One screenshot's side panel warns: `⚠️ Important: All
Messages will be deleted after 5 minutes. Please save or forward these messages to avoid losing them!`
That is the clone's autodelete setting acting on the *delivered copy* in a user's chat — a switch on
the owner's `/settings` screen, not a property of the link. The link's own future is the opposite: the
operator's word on 2026-08-29 is that it works forever. The rule for us comes straight out of that
split: **a post we publish may carry the `?start=` link and never a reference to a message id inside
the bot chat**, because the chat's contents are the part on a timer.

**Which bot answered comes from the link, not the sentence.** `Here is your link:` and the
`BQADAQAD…` token family belong to `@Link_providerobot` too (see
[docs/updates-channel.md](updates-channel.md)), and the storage bot's menu offers the same verb
(`/genlink`) as that bot. Wording is evidence about a protocol; only `t.me/<bot>` is evidence about an
identity. `app.linkprovider.parse_reply` therefore checks the link's host against the bot that was
asked and reports `link_from_another_bot` instead of pretending.

## What this settles

* **the verbs.** One file is `/genlink`; a whole channel is `/batch`; an explicit list of messages
  is `/custom_batch`; an *editable* link is `/special_link`; a link that survives a mirror being
  dropped is `/universal_link`. Our missing-quality rule ("edit the post, never double-post it")
  needs an editable link, and now we know one exists.
* **what we must never send.** `/broadcast`, `/ban` and `/unban` are the bot talking to *people*.
  They are in `app.storagebot.FORBIDDEN`, and :func:`app.probe.ProbePolicy.may_send` refuses them
  before it consults its own allowlist — so widening that allowlist by hand during testing does not
  make the probe able to broadcast.
* **that two of the useful ones are moderator-gated, and the gate is ours.** `/special_link` and
  `/universal_link` both say so in their own help text. The vendor's channel answers what that means:
  *"Moderators have access to all your clone features, include broadcasting"* — a moderator is somebody
  the **owner of the clone** appoints, not a moderator of the channel the files came from. So the gate
  is not a stranger's permission to wait for. What is still unread is whether this account is actually
  on that list (item 3 below).
* **whose service this is.** Every storing and linking bot on this deployment is the operator's *own
  clone*, made and managed by `@Md_CloneManagerBot`. That sentence is worth its weight: the deletion
  timer, force-subscribe, "No Forward", the moderator list, and Public vs Private Mode are settings we
  own, not limits somebody else imposes. The details and the dates are in the next section.
* **one link per range, and its lifetime.** A batch is one range between two messages and produces one
  link; the link does not expire (the operator, 2026-08-29). Both halves of "publish a link and never
  touch it again" are therefore allowed to be lazy — the *durable* object is the token, and the
  destination post is the archive.
* **whether the bot needs to be an admin.** A public channel does not: the vendor says it in one line,
  *"able to generate shareable link for messages from any public channel without bot admin in there.
  (For private channel bot must be admin in there)"*, and the operator said the same thing
  independently. Our private sources are the case that binds us — the clone stays a member with the
  rights it has, because that is what the links read through.

## What the bot declares about itself, and what its owner was asked

Two sources of fact live in this file, and they are not the same kind of thing. The table above is
*menu text* — the operator's screenshots of what the bot prints. The paragraph below is what Telegram
itself reports, read by `/probe` on 2026-08-29 from the pipeline account, as the bot's own declared
command list:

`/start` "Check i am alive" · `/genlink` "To store a single message" · `/batch` "To store mutiple
message…" · `/custom_batch` "To store multiple random…" · `/special_link` "store multiple messages" ·
`/universal_link` "stores multiple messages" · `/shortener` "To shorten any shareable…" · `/settings`
"Customize Your settings" · `/broadcast` "Broadcast a messages to…" · `/ban` "Ban a user (moderators
only…)".

Every verb `app.storagebot` maps a purpose onto is in that list, with the bot's own spelling and its own
typos intact — `mutiple`, `Broadcast a messages`. That is the confirmation the table needed: a screenshot
can show a menu that was edited the week it was taken, and Telegram's declared list is what the bot tells
every account it talks to. It does not add a single fact about *what comes back* from any of them, which
is why the blocked job kind stays blocked.

Four switches on that bot's `/settings` screen are also answered now, by the operator, on 2026-08-29, and
they are recorded here because they are configuration — they can change without anything in this repo
changing, and a stale answer is worse than a question:

* Public Mode, not Private: any Telegram user can mint a link from this clone. What that costs is written
  in the open-questions list below.
* The auto-delete timer is **5 minutes**. It removes the copy delivered to whoever opened the link; the
  stored message stays. This is the fact behind the rule that no post of ours may reference a message id
  inside the bot chat.
* The moderator list holds the owner's **main account only**. The pipeline account is not on it, so the
  two verbs marked "moderators only" (`/special_link`, `/universal_link`) are not today this service's to
  use. Adding it is one entry on that screen, and the open question is whether that is all the bot checks.
* Forwarding: answered in one word, and that word could be read two ways. It stays a question, in the
  list below, until it is written out.

## What it does not settle

The questions below are why `storage_upload` stays a blocked job instead of a confident guess.
Most of them are what one authenticated run can answer. Three used to be a switch on the owner's
`/settings` screen, and those switches were read on 2026-08-29 (above) — what is left is the question
each answer opens, which is a different thing and is written as one. `tests/test_storagebot.py` checks that this list and `app.storagebot.still_unknown()` stay the
same length — a doc that says nine while the code lists eight is the kind of lie that only ever
helps nobody:

1. whether a batch link can be appended to later, or whether /special_link's "edit" means re-issuing the range and then editing the destination post that carries the new link
2. whether a link is a *reference* to the post in the source channel or a copy the bot made for itself: the vendor advertises "no db channel required", which points at a reference, and a reference is only as durable as the message it points at — this is the question that decides how much our zero-deletion rule is protecting other people's links
3. whether adding the pipeline account to the clone's moderator list is what /special_link and /universal_link actually check for. The list itself is read: it holds the owner's main account alone, so those two verbs are not this service's to use today — and whether the bot looks at the list per request or only when a link is minted is a live-run question
4. whether every source this service is ever pointed at is a public channel. The mode is answered (Public, 2026-08-29), and what follows from it is not: a link minted from a *private* source on an open clone is a private channel anyone can read, so a private source means changing the mode first, and nobody has asked for one yet
5. whether "No Forward" or content protection is on. One word arrived on 2026-08-29 and it could be read either way; this is the switch our first step runs on — a forward out of the source channel — so it stays open until it is written out in a sentence
6. what the five-minute timer does to a link someone clicks late. What the copy goes and the stored message stays is answered, along with the value (5 minutes, 2026-08-29); unanswered is whether a link opened after five minutes still serves the stored message — which is the reason no post of ours may reference a message id inside the bot chat
7. whether a link can be revoked, and what a revoked link does to a post that already carries it
8. rate limits per account, since the free tier cannot afford a retry storm. One half of the link's future is answered already: the operator's word (2026-08-29) is that a link does not expire, and that the deletion notice covers only the copy delivered to a user and not the stored message — so what a published post still risks is a rate limit, and a regenerated invite behind the card while every old post goes on pointing at the old one

## Where this bot comes from, in its vendor's own words

The operator's answer to "whose bot is this?" was a name: their storing and linking bots are clones of
`@Md_Files_Store_Bot`, created and managed by **`@Md_CloneManagerBot`**, whose announcements channel is
<https://t.me/venombotupdates>. Read on 2026-08-29, posts spanning Dec 2022 → Apr 2025. **This is the
vendor's word, not an observation of ours** — nothing below is treated as a fact about *our* clone, and
the four open questions that depend on it are in the list above rather than in code.

* **The manager is not the file bot.** "@Md_CloneManagerBot can only be used to create and manage your
  file store bot clones", and *"up to 3 clones per Telegram account"*. So there is no third-party
  service to petition: the bot our links point at is one we made.
* **The parent handles keep dying; the clones do not.** The channel's own history names bot after bot
  as deleted (`@md_filestore_bot` in Dec 2023, `@MdFileStoreBot` and `@MdFileStore_Bot` in Oct 2024,
  `@Md_FilesStore_Bot`, `@MdFileStorev2_Bot`) and each time repeats *"Clones are still functioning as
  before"* — including twice for the manager itself, whose current handle is the one the operator
  quoted. Our design consequence: depend only on our clone's username and token, never on the manager
  or a parent bot.
* **A link is a username plus a token.** `t.me/<bot>?start=…`. Nothing in Telegram re-points a link
  already published when a bot is renamed or its handle is freed and taken by somebody else, so the
  clone's @username is *permanent from the first published post*: rename it, and every old post points
  at a stranger. `app.linkprovider.token_of` keeps the token so we can rebuild links for **future**
  posts; it cannot rescue a past one.
* **Public Mode vs Private Mode.** The vendor: *"Public Mode: Any telegram user can generate shareable
  & shorten links using clone. Private Mode: Only the owner and moderators can."* A clone that can read
  a private channel and sits in Public Mode is a private channel anyone can mint links from. Private
  Mode is the setting that matches what this pipeline needs.
* **"No Forward"** (Oct 2025) restricts clone users from forwarding messages shared via links, and
  **24h token verification** (Sep 2024) makes a user pass a shortened link for "special access" to
  messages. Both sit between our button and the file. "No Forward" is also the setting that would
  contradict the bot's own advice to *"save or forward these messages"* — and our pipeline's first step
  is itself a forward out of the source channel, so whatever is on in the clone has to be read, not
  assumed.
* **Autodelete** is described as deleting *"media messages sent to clone users after 30 minutes"* — the
  same knob as the 5-minute warning in the screenshot, on the owner's `/settings`, about the delivered
  copy. Nothing about it changes what we store or publish.
* **Force subscribe** (up to six channels), a custom start message, per-clone user stats,
  ban/unban of the clone's own users, and ownership transfer to a moderator are the other clone
  features. `app.storagebot.FORBIDDEN` refuses the three that talk to an audience whatever the
  vendor says about them, because the point of that refusal was never someone else's terms of
  service — it is that a queued job should not be able to reach people.
* **"no db channel required"** — the vendor's own feature list, and the opposite of the upstream
  fork family (CodeXBotz/File-Sharing-Bot) this software descends from, whose README tells you to make
  the bot admin in your "DB channel". Which of the two our clone actually runs is unanswered and it is
  item 2 above, because it decides whether a link is a pointer at the source post or a copy of it.
* Support channel `@MdBotzSupport`, setup tutorial `youtu.be/l1PuwqKqsbA`, and *"Google Backup"* (Oct
  2024) for recovering a lost account's clones. Recorded because "the operator's Telegram account is
  the only place these clones exist" is a single point of failure for every link we publish.

## How a live run checks this page

`/probe` sends `/start` to the bot and reads back the menu. `app.storagebot.parse_menu` turns the
answer into ``(command, help)`` pairs and `app.storagebot.diff` compares them with the table
above, reporting three lists rather than one boolean:

* **missing** — a command we build a plan around is gone; the job kind that names it will fail, and
  that is worth knowing before a season is queued, not after.
* **added** — a new verb may be the thing that makes `/custom_batch` unnecessary. It changes
  nothing until a human says so; a bot's menu growing is not an instruction.
* **changed_help** — the wording moved. Usually cosmetic; occasionally the sentence that said
  "(moderators only)" is the one that changed.

A probe also reads *the peer's* replies with the link bot's parser: `app.probe.probe_bot` calls
`parse_reply(text, bot=username)`, so a link belonging to a sibling clone is reported as
`link_from_another_bot` in the report instead of flattering it into `"link"`.

Three things on the operator's `/settings` screen are worth reading while the session is open, because
they are the cheap version of items 4, 5 and 6: which mode the clone is in, whether "No Forward" is
on, and what the deletion timer is set to. None of them is a code change; all three change what a
published post promises.

The bot's replies, links and buttons stay in `app.audit_log` and reach the operator as one
paste-able message; nothing in this document is inferred from a reply we have not read.

One exception to that last sentence, stated so it stays a hint and not a fact: `@Link_providerobot`
answers the *same verb* (`/genlink`) by asking for a forwarded message and replying with a
`t.me/<bot>?start=` link. That is recorded in [docs/updates-channel.md](docs/updates-channel.md) about
*that* bot. It makes the storage bot's likely behaviour easier to predict and it settles nothing —
the questions above stay open until `/probe` reads this bot's own replies.
