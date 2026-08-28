# The updates channel — how the link is obtained, and what is posted there

An operator asked, in three parts: *"add this channel as a destination for new posts"*, then *"ek
message with caption aaiga aur usme channel ki link hogi, wo aapko ek bot pe bhejni hogi aur uske
baad jo link ayega wo post me add hoga"*, then *"usme koi files host nahi karni hai"* — one message
with a caption and the channel's link arrives; that link is sent to a bot; the link the bot answers
with goes into the post; and the channel hosts no files.

That is a different job from every other destination in this project. **Nothing is stored on this
channel.** Files stay where they are; this channel carries a notice.

## The flow, as the operator described it, with the one unknown named

```
the destination channel's link + a caption with it            <- the operator posts this
                     |  forwarded to @Link_providerobot        <- the operator does this step
                     v
              a shareable link back                            <- this is what goes in the post
                     v
       announcement post in the updates channel
```

Two things are worth separating here, because they are easy to conflate and the app treats them
differently. **The card is made from the channel link** — the `t.me/joinchat/...` invite is the input
to `@Link_providerobot`, and its output is the shareable link that goes in the post. **The
announcement is made from the bot deep link** — `t.me/Link_providerobot?start=<token>`, which is the
link both sampled posts point at, not the `t.me/joinchat/...` invite. The card is a *device* for
spreading the invite; the post is the notice that an episode was added. They are different links and
a substitution between them is the kind of mistake that only shows up a month later when somebody
reports a dead link.

The operator also asked whether the card can be **edited** after the bot sends it, and whether the
link survives editing the caption. That is behaviour *inside another bot's chat*: this code neither
knows nor guesses. What it does guarantee is narrower and checkable — the link is read from the
bot's reply and then treated as data, so the post is built from whatever link you end up keeping.
If you edit the card and the bot hands back a different link, use that one; nothing here caches the
first answer.

The middle step is the operator's, not the app's: the bot is **not** a storage bot, so this code
never sends it a file, and it has no API or web interface to call — only a chat with a user in it.
The app therefore starts where the human hands over the result: a bot deep link.

## The two commands, spelled out for audio, exactly as the code uses them

`app.linkprovider` is the only place these strings live; there is no second copy to drift.

```
link provider bot: @Link_providerobot
first word: /genlink
then: the channel's own t.me link
link shape: t.me/<bot>?start=<token>
```

```
what is sent to the bot:
/genlink https://t.me/Link_providerobot?start=BQADAQAD0RoAAp4PgEa2qMuk0UmjRBYE

what is expected back (first line, prefix match):
Here is your link:
https://t.me/Link_providerobot?start=BQADAQAD0RoAAp4PgEa2qMuk0UmjRBYE

what is taken out of it:
https://t.me/Link_providerobot?start=BQADAQAD0RoAAp4PgEa2qMuk0UmjRBYE
or, as a Channel Help button line:
SHARE URL - https://t.me/Link_providerobot?start=BQADAQAD0RoAAp4PgEa2qMuk0UmjRBYE
```

| step | what is typed or read | what this code does with it |
| --- | --- | --- |
| 1 | `/genlink https://t.me/Link_providerobot?start=BQADAQAD0RoAAp4PgEa2qMuk0UmjRBYE` | one line: the bot's own command word, then the deep link the operator opened the card with. Nothing else, so there is nothing to reformat |
| 2 | the bot's reply | a shape check, then read the shareable link out of it |
| 3 | `SHARE URL - https://t.me/Link_providerobot?start=BQADAQAD0RoAAp4PgEa2qMuk0UmjRBYE` | what that link becomes in the post: the observed button label, pointing at the bot with the token — Channel Help syntax, so the link half is ours to supply |
| (0) | the invite link itself | fed to the bot to make the card. Nothing here does that; it is the operator's step, and its output is step 1 |

Step 1 is not free-form: the bot answers only when the forwarded message carries the words it asked
for first — `Send A Message For To Get Your Shareable Link` — which is why `parse_reply` treats a reply beginning with those same
words as *the request echoed back*, not as a link. That single distinction is what stops a
half-finished handoff from reading as a finished one:

```
the bot's own request, as the operator recorded it:
Send A Message For To Get Your Shareable Link

so a reply opening with those words means nothing has been minted:
kind = asks_for_a_message
and an answer that opens with neither marker is not harvested for a URL:
kind = unknown
```

**The first line is read, not searched for.** `parse_reply` looks at the reply's first line and
requires the prefix `Here is your link:` plus a `t.me/<bot>?start=` link; anything else comes back as
`{"kind": "unknown"}` with no link in it. A reply that begins with the wrong words is never
harvested for a plausible URL somewhere inside it: half a recognised answer that yields a
working-looking link is the worst outcome available here, because that link goes in front of 33k
people.

The reverse direction is guarded in `app.probe`, not here: `ProbePolicy.may_send` refuses the words
`genlink`, `link`, `share` *before* it consults its own allowlist, so no probe run can type the link request into
any chat however much someone widens `SAFE_COMMANDS` while testing by hand. Minting a link inside
someone else's bot is a write to a third-party service, and this project's read-only rule covers
that whether or not a file ever moves.

## The caption on the card

```
Channel link

https://t.me/+RM_bWDqzldg2OWFI
https://t.me/+RM_bWDqzldg2OWFI
```

Two facts about that text.

It repeats the link **twice**, because that is what the operator's own card does — `LINK_LINES` is
both the default and the observation, and `repeats` is a real knob for the day they decide the card
reads better with one line. A default that quietly differed from the sample would produce the wrong
post at 3 a.m. and call it a success.

The invite is **not** wrapped in `[]()`. `card_caption`'s `style` parameter exists to refuse one
thing: `"markdown"` raises, because in a plain-text caption `[label](url)` does not become a
hyperlink, it becomes square brackets. The announcement below is the opposite case — it is a
formatted post, so there the same link *is* written as a markdown link, and
`announcement_caption(style="text")` is offered for anyone who wants the bare form in that one too.

## The announcement post

```
🍓 Re Zero (S4)

😗 Episode 09 Added...✨”

[Click here to start and get episode](https://t.me/Link_providerobot?start=BQADAQAD0RoAAp4PgEa2qMuk0UmjRBYE)
[Click here to start and get episode](https://t.me/Link_providerobot?start=BQADAQAD0RoAAp4PgEa2qMuk0UmjRBYE)
```

Built from one template — the approved caption box `templates.announcement_post` in
`app.config` (and in `app/captions.py` as the fallback), so the operator can change the wording
without a redeploy:

```
templates.announcement_post =
'🍓 {title_full} (S{season})\n\n😗 Episode {episode} Added...✨”\n\n[Click here to start and get episode]({link})\n[Click here to start and get episode]({link})'
```

`announcement_caption` is the only writer of that shape, and it refuses rather than invents: no
series name, or no episode number, or a season we do not know (every sampled heading carries
`(S1)`), and any link that is not a `@Link_providerobot` deep link with a token. A missing
placeholder raises with its name in the message instead of appearing in the post: a queue with one
bad row has to be caught by the caller once, and a caption rendered with `{title_full}` still in it
is a post nobody can unpublish.

**The announcement is an approved caption box as of 2026-08-28.** Before that, `app/linkprovider`
carried the shape and `app/captions.APPROVED_TEMPLATES` did not, and `test_docs` asserted that it
must not: "we can render it, we cannot send it" was the accurate sentence at the time. So the
post is a post that may go out. What still blocks it is not approval but plumbing — the send is
`app.publish.publish_announcement`, which is unwired like every other MTProto write in this project,
and `app/handlers.py` refuses the job kind until a live channel has seen one posted.

The matcher in `announcement_matches_shape` reads a heading, a note line **whose tail must match**
(`...Added...✨”` — both samples end that way, so a message that merely mentions an episode is not
ours), a season, an episode number with or without a leading zero, and links that either all carry
the same token or carry none at all. It reports what was found and returns `None` values for what
was not, rather than guessing a season from a title. It is the parser the day comes when
this project needs to read its own past announcements; it is not a gate on sending them.

## `@anime_hindifilesbot` and `@Link_providerobot` are different tools

The storage bot hosts files and gives back a post with a link. The link provider mints a shareable
link for a channel card and hosts nothing. `summary()` keeps that distinction in one line:

/genlink on @Link_providerobot takes a forwarded message and answers with a t.me/<bot>?start= link; 4 questions still open

## What this is worth in the pipeline, honestly

The announcement is a notification. It is not a manifest, not a coverage claim, and not an approval
of anything.

Two consequences are already coded. First, an announcement carries a link to *where to ask*, not a
file: nothing may reason from its existence that a file was published. Second, the only identifier
in the text is the token, and it belongs to the link provider, so it cannot be used to find a row
in `app.episode` — which is exactly why the matcher's `missing` list is a first-class part of its
answer. Any future "what did we already announce" question gets answered by a table the app writes,
never by reading the channel back.

## Where the state lives, and the one place the app refuses to be clever

Four rows in `app.config`, seeded by `supabase/migrations/0008_updates_channel.sql`, and the fifth
(`templates.announcement_post`) by 0009. Three of them have a reader today; `app/linkprovider.status_line`
prints what it can and `/status` shows that line — a config row nothing reads is a row that lies quietly,
so this is stated rather than hidden:

- `updates.channel` — the channel to post in: an `@handle` or a marked numeric id. Empty means the
  app does not know where, and `status_line` says "announcements have nowhere to go" instead of
  inventing a name. **A private channel is named by its id, because a private channel has no
  @handle** — that is the usual spelling here, not an edge case, and both are accepted.
- `updates.per_episode` — `true` is one announcement per episode; `false` is one per batch. A
  config row, because it is a taste, not a fact.

A numeric id is a 13-digit number with `-100` in front (`marked_channel_id` builds and reads it as
text for that reason; doing it arithmetically is how a row silently stops matching its channel). To
find your channel's id: forward one of its posts to `@userinfobot`, or open the channel through
this app's session and read it from `app.rights` — the second one happens on every `/probe`. A
private channel also has no public join address, which is why the announcement's link and the
operator's invite are the only ways in, and why `joinable` stays a question this code answers with
the dialog list rather than with the id.

`app.probe` will not paste a link into a chat it has not been told to post in. `probe_account`'s
`expected` list comes from `app.source_channel` rows, the same list that guards `forward_source`,
and a destination that is neither a configured source nor a named destination in the probe's own
policy is reported as a missing channel rather than messaged. Sending an announcement to the wrong
place is not a recoverable mistake, and an account already under a limit is not the place to be
creative.

Reading rights works the same way round: `app/rights.py` never inserts a row, never matches a
channel by its title (renames happen, ids do not), and treats a configured-but-invisible channel as
"not read" — never "member". It records what it found *and* the timestamp it found it at, because
`we_are_admin` is a claim about yesterday otherwise.

## What the operator answered on 2026-08-28

Four questions were put after the flow was recorded; all four were answered. They are written here as
data, with the row or file that carries each one, so the next reader does not have to reassemble a
conversation:

| question | answer | where it lives now |
| --- | --- | --- |
| one updates channel, or one per series? | **one global channel** | `app.config` `updates.channel`, a single text value — a per-series setting would have needed a column on `app.destination_channel` instead |
| who posts there? | **my own account** (the operator's session) | `updates.posted_by`, and `status_line` prints it, because "as plain text with a link" is what a user session does and a bot does not |
| one announcement per episode, or per batch? | **per episode** | `updates.per_episode` = `true`, read by `status_line` |
| does the shareable link survive editing the card? | **the link survives** | `updates.link_survives_edits` = `true`, which is what lets a rotated invite regenerate the card without stranding the posts that already carry the old link |

Nothing new is posted by those answers, and two things are deliberately not built: no announcement
queue table (a second ledger would be a second truth; an announcement is a post and `app.job`
already answers "is one owed?"), and no `app.destination_channel` row for the updates channel (that
table's rows carry `expected_episodes`, coverage maths, and a `channel_help_post_id` — all episode
machinery, none of which applies to a notice board).

## What it does not settle

1. whether the link expires, is rate-limited, or stops working when the private invite it shows is revoked and regenerated — the question that decides whether an old announcement is a dead end
2. what @Link_providerobot's /start menu holds besides /genlink, and whether it has the same moderation verbs as the storage bot's menu (broadcast, ban, unban)
3. the exact emoji run in the card caption after the words 'Channel link' — visible, not counted
4. whether this session's account can post in the updates channel at all: /probe reads our own rights from the dialog list (app/rights.py) and writes them, and until that has run, the first announcement is a guess about a channel this account may only be able to read

Each of those has a next step that is a run on a channel we control rather than a guess. The one about
expiry decides whether an old announcement rots; the one about rights answers itself the first time
`/probe` runs, because `app/rights.py` reads our own admin status out of the dialog list and records
it. Listing them here is deliberate: a closed list of unknowns is a to-do list, and an empty one would
be a lie.

## What the guide says, and what has been seen here

Channel Help's own guide documents the posting half of this: caption and media, HTML/Markdown
formatting, button rows, scheduling, reactions, a signature, and a final step to save or schedule a
post. `docs/channel-help.md` keeps those facts and says which of them this project depends on.
The announcement post does not go through Channel Help at all — it is the operator's own account
posting plain text with a link — so none of the bot's rules bind it, and none of its free-tier limits
either.

What has been *seen* on this account's channel is the sample caption quoted above, plus the two
sample announcements the operator sent. What has not been seen is a private channel's id format
actually in use, a card being edited and keeping its link, and a link the provider bot handed back
to this session — all three are documented or operator-asserted, none is observed by this code, and
each is marked as such at the place it would matter.
