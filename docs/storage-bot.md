# The storage bot, as seen

`@anime_hindifilesbot` is not documented anywhere public, and its protocol cannot be read from the
outside: it answers a menu, takes a forwarded message, and hands back a link. So the rule was
"discover it live, once, from inside the deployment" — and the first piece of that discovery
arrived cheaper than expected: the operator sent screenshots of the bot's own command list on
2026-08-28.

That is *menu text*, which is worth recording exactly and worth re-checking, and it is not the
protocol. This document is the difference between the two, kept in the repo because a screenshot
in a chat is how a protocol ends up half-remembered.

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

## What this settles

* **the verbs.** One file is `/genlink`; a whole channel is `/batch`; an explicit list of messages
  is `/custom_batch`; an *editable* link is `/special_link`; a link that survives a mirror being
  dropped is `/universal_link`. Our missing-quality rule ("edit the post, never double-post it")
  needs an editable link, and now we know one exists.
* **what we must never send.** `/broadcast`, `/ban` and `/unban` are the bot talking to *people*.
  They are in `app.storagebot.FORBIDDEN`, and :func:`app.probe.ProbePolicy.may_send` refuses them
  before it consults its own allowlist — so widening that allowlist by hand during testing does not
  make the probe able to broadcast.
* **that two of the useful ones are moderator-gated.** `/special_link` and `/universal_link` both
  say so in their own help text. What "moderator" means there — of the bot's service, or of the
  channel the messages come from — is the first thing the authenticated run has to find out,
  because a season-batch design that assumes an editable link cannot fall back to one that does
  not exist.

## What it does not settle

The questions below are why `storage_upload` stays a blocked job instead of a confident guess.
They are the exact list one authenticated run can answer, and `tests/test_storagebot.py` checks
that this list and `app.storagebot.still_unknown()` stay the same length — a doc that says "eight"
while the code lists nine is the kind of lie that only ever helps nobody:

1. what each command asks for after it is sent (a forwarded message id? a file? a channel handle? a number of messages?)
2. whether the answer is a text message with a URL, or a button, or both
3. the shape and lifetime of a link: does it expire, and is it a t.me link to a stored copy or a redirect the bot serves itself?
4. whether a batch can be appended to later, or whether adding one quality means a new link and therefore an edited destination post
5. what 'moderators only' means for /special_link and /universal_link: moderator of the bot's service, or of the channel the messages come from
6. what 'your clones' are for /universal_link, and whether we get one, several, or none
7. whether a link can be revoked, and what a revoked link does to a post that already carries it
8. rate limits per account, since the free tier cannot afford a retry storm

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

The bot's replies, links and buttons stay in `app.audit_log` and reach the operator as one
paste-able message; nothing in this document is inferred from a reply we have not read.
