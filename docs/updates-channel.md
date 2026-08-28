# The updates channel, and the link that makes it work

Everything else in this project ends at a series channel: files stored, links made, posts written.
This is the flow that happens **after** that, in a channel of its own, where the audience is not the
people who already follow one show but everyone who follows the brand. The operator has been doing it
by hand. It is recorded here in their words and their screenshots (2026-08-28), not in ours.

## The four steps, as the operator does them

1. In the **series channel**, post a card: the series art with the brand handle on it, captioned with
   the channel's own private invite link.
2. **Forward that message** to `Link_providerobot`.
3. It replies with **one shareable link** — `https://t.me/Link_providerob…` in shape, a bot deep link, with a
   `SHARE URL` button under it.
4. Put that link in the **announcement** in the updates channel. The post says which series, which
   season, and which episode was added. Mechanically the link opens the bot and the bot shows the
   card, so nobody in a 33k-subscriber channel is handed a join link directly. *Why* it is done this
   way — a restriction on invite links in public channels, or simply how it has always worked — is the
   first question below, because the answer changes what the app is allowed to automate.

The card, as posted (the arrow emoji after `Channel link` are visible in the screenshot and are not
counted here — see the open questions):

```text
Channel link

https://t.me/+RM_bWDqzldg2OWFI
https://t.me/+RM_bWDqzldg2OWFI
```

The announcement, as posted:

```text
🍓 Daemon of the shadow realm (S1)

😗 Episode 14 Added...✨”

[Click here to start and get episode](https://t.me/Link_providerobot?start=BQADAQAD0RoAAp4PgEa2qMuk0UmjRBYE)
[Click here to start and get episode](https://t.me/Link_providerobot?start=BQADAQAD0RoAAp4PgEa2qMuk0UmjRBYE)
```

`app.linkprovider.announcement_caption` renders that second block character for character, and
`tests/test_linkprovider.py` fails if the doc and the function ever disagree. Two details are in the
function because they are in **both** samples, so they are a habit and not a typo: the season in
parentheses after the series, and the link written twice on two lines.

## What was seen, and how it is known

| thing | observed |
| --- | --- |
| the bot | `@Link_providerobot` (the name as it appears in the link it sent, which is the spelling that is evidence) |
| what it is asked | `/genlink` |
| what it answers first | "Send A Message For To Get Your Shareable Link" |
| what that means | it wants a *forwarded message*, not a URL typed in |
| the reply | begins "Here is your link:" then the link on its own line |
| under the reply | a `SHARE URL` button |
| the link's shape | `https://t.me/<bot>?start=<token>` |

`app.linkprovider.parse_reply` reads that reply and nothing else counts: a message is a link only
when the marker and a `?start=` deep link are both there. A reply that asks for a message comes back
as `asks_for_a_message`, and anything else comes back `unknown` — because the failure mode this
guards against is a wrong link published to a channel of strangers.

The token, not the whole URL, is what belongs in the database: `L.deep_link(token)` rebuilds the
link from the bot's current username, so a bot that renames itself cannot invalidate links already
posted.

## Where this plugs in — and where it deliberately does not

* **The probe knows it exists.** `app.probe.ProbePolicy` has `link_provider` as a third peer, and
  `run_probe` maps its menu like the other two. If a reply during a probe is already link-shaped, the
  report says `reply shape observed`.
* **The probe will never use `/genlink`.** `/genlink` is not forbidden the way a moderation verb
  is; it is *not free* — it mints a permanent link on your account and only answers once something has
  been forwarded to it. A read-only run does neither, and that is enforced before the allowlist, the
  same guard `app.storagebot.FORBIDDEN` sits behind.
* **`publish_post` now names this leg.** `app/handlers.py` says the publisher owes an announcement after
  an episode post, so `/status` cannot report "publishing" while half of it is unwritten.
* **The announcement shape is *not* an approved caption.** There is no `templates.announcement_post`
  in `captions.APPROVED_TEMPLATES`, and that is the rule working, not a missing row: nothing is posted
  to that channel until you approve the exact text. Say "the announcement box is approved" (or give me
  the line you want changed) and it becomes one row.

## What the operator answered, 2026-08-28

Four questions were asked and four answers came back, so they are recorded as decisions rather than
left in a chat log:

| asked | answered | what that means in the code |
| --- | --- | --- |
| one global channel, or one per series? | **one, for the whole brand** | `app.config` row `updates.channel` holds it — a `@handle` or the numeric id. Empty does not mean "announce nowhere", it means "the app does not know where", and `/status` says that in those words instead of staying quiet |
| who posts it? | **your own account**, through the logged-in session | plain text links and no button row, like both samples; no new permission is involved, only the write layer that is unwired today |
| when is one owed? | **per episode, as it lands** | `updates.per_episode = true`, which is what the samples look like anyway (`Episode 14 Added`, never a range). Set it false and the same shape names the range instead |
| does the link survive its card? | **yes, it keeps working** | the token is reusable, so one per series channel is enough, and rotating a private invite can regenerate the card without orphaning a published announcement. The app still never deletes — this answer only says a deleted card would not break what was already posted |

The `/status` line prints the whole state in one sentence because the two halves are easy to satisfy
one at a time:

```text
updates channel: not set — announcements have nowhere to go, so one announcement per episode is a plan with no audience; …
updates channel: @yc_updates, one announcement per episode, sent by your own account as plain text with a link; the announcement text is recorded but NOT an approved caption box, so nothing sends yet
```

## Why there is no queue table for this

An announcement is a post, and `app.job` already queues posts (`publish_post`). A second table asking
"is an announcement owed?" would be a second answer to the same question, and two answers is how one
episode gets announced twice to 33k people. So 0008 adds two settings and nothing else: when the
sender exists, it reads `updates.channel`, `updates.per_episode`, the destination's stored card token,
and the approved-caption gate. Every one of those already has a reader.

## What it does not settle

Recording a flow is not authorising one. These are the questions the three screenshots cannot answer,
and every one of them is a question about either the bot's protocol or your intent:

1. who posts the announcement: your own account, @chelpbot, or the app's session — the sampled posts carry text links and no button row, which is not how Channel Help composes one
2. whether the shareable link lives forever, or dies with the card message it was made from (this decides whether the app may ever delete a card, and the answer is presumably no)
3. whether the link expires, is rate-limited, or stops working when the private invite it shows is revoked and regenerated
4. what @Link_providerobot's /start menu holds besides /genlink, and whether it has the same moderation verbs as the storage bot's menu (broadcast, ban, unban)
5. the exact emoji run in the card caption after the words 'Channel link' — visible, not counted
6. whether one updates channel serves every series (as it appears to now) or one per show, and which channel it is by handle: the screenshots show it open by id, not by username
7. whether the announcement is owed per episode, per batch, or only when you say so

Until the first of those is answered, the app can *describe* an announcement — and will, in a plan —
but it will not send one.

## Same verb, different bot: what this says about the storage bot

`/genlink` is also the first verb on `@anime_hindifilesbot`'s menu ("To store a single message or
file"), and the reply shape recorded here — "send me a message, get a link" — is exactly the shape
that job needs. That is a strong hint that the two bots come from the same family of tool. It is
**not** proof: `storage_upload` stays blocked, and `app/storagebot.py`'s own list of open questions is
unchanged, because a guess about one bot is not an observation of the other. The probe re-reading
`@anime_hindifilesbot`'s replies is still what closes it.
