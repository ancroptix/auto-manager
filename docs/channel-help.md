# Channel Help — what the guide says, and which of it this project may rely on

Channel Help is the bot that creates the destination posts (`@chelpbot`; the official guide is at
`botguide.me/s/ch-en`, read on 2026-08-28 and quoted below). This file exists because the project kept
saying a behaviour was "unobserved" when the tool's own documentation settles it — so the documented
half is written down here, labelled as documented, and the half that only our own channels can
settle stays flagged in code.

## The plan line, because it decides what we may depend on

| tier | price | what it adds |
| --- | --- | --- |
| free | 0 | the whole posting flow: captions, media, formatting modes, buttons, scheduling one post, reactions, signature |
| PLUS | 2.99 USD/month | recurring posts (up to 5), Auto-complete, Multipost, Welcome messages, join filters, timed approval, an event log, forwarding to 1 group |
| PREMIUM | 7.49 | 50 recurring posts, 5 Auto-complete sets, word replacement, forward to 5 groups, force-join 3 chats, bulk approval, virtual admins |
| BUSINESS | 14.99 | 100 recurring posts, 100 Auto-complete sets, 10 groups, top send priority |

Prices are paid in Telegram Stars, renewal is **not** automatic, and an upgrade is prorated (the
guide's own example: PLUS then PREMIUM = 375 − 150 = 225 ⭐). A free trial is one month at Business
level with a single recurring post.

That is the load-bearing fact for us: **everything this project depends on has to work on the free
tier.** Our needs are the post itself — caption, media, buttons, and the ability to fix a post later —
all of which the guide puts in the free tier. Auto-complete, Multipost, recurring and forwarding are
paid, and nothing in this repo may be designed as though they were on.

## The post-creation flow, as documented

1. The bot is added to the channel as admin, then `/chelp` (or its menu) opens a channel menu.
2. The initial menu offers: **Notifications**, **Link preview**, **Protected** (no copying or
   forwarding of the post), **Formatting**.
3. **Formatting** chooses how the text is interpreted: Telegram's own rendering, **HTML** (`<b> <i>
   <u> <s> <a href> <code> <sp> <m> <f> <br>`, `<dot>` for a bullet, `<inv>` invisible; tags combine,
   and malformed markup is rejected rather than posted), **Markdown (old)**, or **Markdown (new)**.
4. Then the post's content: text, **or** media. Choosing media asks whether to **send a caption** with
   it or "No caption", and offers **Attach Media** to hang extra media under the text. Media may be
   **forwarded from another chat**, keeping or replacing its caption — that is the documented route by
   which a file the storage bot already posted arrives with *our* caption on it.
5. The final menu: **Reply Message** (the bot is given the link of a message to reply to, which is how
   a post is anchored to an existing file message), **Save Post** (a draft for later), **Recurring**,
   **Schedule Sending**, **Schedule Deletion**, then **Send Post** or **Send to more channels**.
6. After that: **Reactions** (up to 10 emoji, one may be the voting option) and **Signature**.

Where our code touches this: `app/captions.py` renders the text and the button block that steps 3–4
consume, `app/config.py:destination_post_links` describes what a created post must carry, and
`app/handlers.py:DEPENDENCIES` keeps `publish_post` and `edit_post` blocked until this flow has been
walked once on a real channel of ours. Nothing here automates the bot's chat: no code drives its menu.

## Buttons, in the syntax the bot reads

One line per row: `Button text - https://url`. `&&` puts several buttons on one row; a newline starts
the next row. A button may instead carry `text - popup:shown when pressed`, `text - alert:short text`,
`text - copy:text to copy`, `text - share:text`, or `text - comments` (the last needs a discussion
group with the bot in it). `text - popup:subscribed %% not-subscribed` shows one of two texts. The
button text may be tinted by prefixing the line with `#g` (green), `#r` (red) or `#p` (premium/purple),
and one emoji is allowed in the button text. **Albums get no buttons** — the guide says so, and it is
Telegram's limitation rather than the bot's.

`app/captions.py` builds this block (`BUTTON_ROWS`, the `#g #r #p` prefixes, `&&` between two links on
one row), and the guide is why a quality pair shares a row while three qualities split across rows:
rows are one line each, so the layout is a consequence of the syntax rather than a taste.

## Auto-complete: what it can and cannot do to a caption

Auto-complete is a **paid** feature (PLUS and above, 1 set on PLUS). It **adds** blocks to messages
that match its trigger — Signature, Header, Buttons, Attach media, a separator sticker, Reactions —
filtered by trigger words, block words, a media filter, or a command. Albums get the signature only,
no buttons.

The distinction matters here: it **never replaces** an existing caption. So the in-place mode of this
project — rewriting the caption on a file message that is already sitting in a channel — is not
something the bot does for us, and `app/inplace.py` is our own session's `messages.editMessage` path
instead. That is the answer to "why don't you just let Channel Help fix the captions": the documented
feature set has no such verb.

## My posts, editing, and the deletion limit

The bot keeps a list per channel of what it has sent and what is scheduled. From there a post can have
its **media replaced**, its **content edited**, an attachment added or removed, be resent, or be shared
again. Reactions on a sent post can be removed or the voting closed, but not edited into a different
set.

Deletion is the part that reads like a policy and is actually a limit:
- **"Only from the bot"** unmanages the post — the bot forgets it — and **the message stays in the
  channel**.
- Deleting the message from the channel works **only within 48 hours** of it being sent. A scheduled
  deletion beyond that turns into a reminder instead.

So this project's zero-deletion rule costs nothing to keep: a post older than 48 hours is not ours to
delete either way. It also means `edit_post` — editing a post the bot created — is the documented way
to correct a published episode, which is why that job kind exists at all, and why it is blocked until
we have walked it once rather than guessed at.

## Getting the bot into a channel

The guide's own requirements: the bot must be an **admin** of the channel, otherwise it can only see
new messages and cannot manage old ones. The rights it asks for are **Post messages**, **Edit
messages of others**, **Delete messages of others**, and **Add members**; the "of others" rights are
what makes managing any post possible. Format errors in a post are reported back by the bot rather
than posted broken.

Our side refuses one of those: `app/channels.py:FORBIDDEN_HELP_RIGHTS` keeps `Add admins` off the list
we accept, and `rights_are_safe()` is what `add_channel_to_help` will not proceed past. Deleting other
people's messages is accepted for the help bot alone, on the strength of this documented need, and it
is still the right we are least happy about — `docs/requirements-draft.md` carries that note.

## What is still ours to observe

Everything above is **documented, not observed on our channels**. What has not been seen by this
project's own session: a post created through this flow end to end, the bot's reaction list on a real
episode post, what "Protected" does to a file post people want to forward, and whether the storage
bot's thumbnail survives the bot's own media pipeline. Those four are why `publish_post` is blocked;
each is a test on a channel we control, not a question for the guide.

## Sources

- Channel Help guide, "Create posts" — `https://botguide.me/s/ch-en/doc/create-posts-o7DmqM0oIB`
- Buttons — `https://botguide.me/s/ch-en-buttons`
- Auto-complete — `https://botguide.me/s/ch-en/doc/auto-complete-KOaYuIEkXd`
- My posts (edit and delete) — `https://botguide.me/s/ch-en/doc/my-posts-7phybImZJP`
- Settings — `https://botguide.me/s/ch-en/doc/settings-GWPnttdhbf`
- Plans and payment — `https://botguide.me/s/ch-en/doc/plans-AB051OL9TA`
- Getting started and add-bot — `https://botguide.me/s/ch-en/doc/getting-started-Gz9vRfL6h7`

All read 2026-08-28. Free of charge to cite; the guide is the vendor's, so anything here that later
contradicts the live product is the guide's change, not this repo's guess — which is the reason the
URLs and the date are in the file.
