# How a channel is built, and how a new season is recognised

Two questions, answered the way the code actually decides. Both are about the same
thing from different sides: **the program is only ever allowed to state what it was told,
never what it inferred.**

---

## 1. The destination channel: who creates it, and with what face

### Why it is your spare account and not the bot

A Telegram *bot* (Bot API token) can send a message it is given, to a chat it is in.
That is the extent of its power. It cannot:

| The task | Bot API | User session (Telethon) |
| --- | --- | --- |
| create a channel | ✗ | `channels.createChannel` |
| set the channel photo or bio | ✗ | `channels.editPhoto`, `channels.updateChannel` |
| make another account an admin | ✗ | `channels.editAdmin` |
| generate or revoke an invite link | ✗ | `messages.exportChatInvite` / `editChatInvite` |
| read a foreign channel's history | ✗ | ✓ |
| download a file | ✗ | ✓ |
| post text with inline buttons | ✓ (but *Channel Help* is doing the posting here) | — |

So creating and furnishing a destination channel is **user-session work**, done by the
spare account, and the control bot you talk to is only the switchboard. This is also why
`@chelpbot` posts to the destination: it can write text and buttons, and it is the
account the operator chose as the publisher.

### The sequence, with the reason for each position

`app/channels.py` keeps this as an ordered list of named checkpoints rather than one
long function, because Render's free tier can kill the process *between any two of them*.
Progress lives in `app.destination.setup_state` (jsonb), so a restart resumes instead of
repeating.

| # | Step | Why here, and what it prevents |
| --- | --- | --- |
| 1 | `create_channel` | Private from the first millisecond. A channel that is briefly public and empty is discoverable, searchable and reportable before it has anything in it. |
| 2 | `set_profile` | Title, bio and picture, before anyone else is involved — nobody ever sees a half-built channel. |
| 3 | `add_channel_help` | `@chelpbot` added with post/edit/delete only. Not `can_invite_users`: a publisher that can invite is a publisher that can be abused to spam the channel. Irreversible, hence marked. |
| 4 | `invite_owner` | A **single-use** invite link sent to `TELEGRAM_MAIN_ADMIN_USER_ID`. The spare account cannot grant ownership to itself, so the owner's own tap is what makes them the owner. |
| 5 | `promote_channel_help` | Now with pin rights, because a season batch post is pinned and must stay editable. The only account that may *ever* be promoted is `chelpbot`; `may_promote()` refuses an unnamed candidate, including anything that arrived as a join request. |
| 6 | `revoke_invite` | In the same plan, not as a follow-up. An open invite on a private channel is the mistake that cannot be un-rung. |
| 7 | `season_sticker` | The first season's opening sticker, before episode 1 (see part 2). |
| 8 | `ready` | Publish jobs refuse to run against a destination that is not ready, so an interrupted setup parks work instead of posting into a channel with no publisher. |

**What this table is, and is not.** It is the plan `channels.setup_plan()` returns: ordered
steps, each with its reason, the two irreversible ones flagged, and the exact title, photo,
bio and permission set attached to the steps that need them. It is not a claim that any of
it has run: the MTProto calls that carry it out belong to the unimplemented Telegram write
layer, alongside the storage bot and Channel Help adapters. Progress would live in
`app.destination.setup_state`, which is why a half-finished setup resumes rather than
repeating — the column exists, and the code that would write to it does not yet.

### The name

`{TITLE} Anime in Hindi`, generated from the stored series title, title-cased by the same
function that renders every other title in the system so the archive, the caption and the
channel never spell a series three ways. It is generated **without asking** — the operator
was explicit that creating a destination needs no confirmation — but it is only ever
generated when the channel's own configured series and the file's title agree
(`series_agrees`, which tolerates `"Bleach TYBW"` vs `"Bleach Thousand Year Blood War"`).
On disagreement nothing is created and the owner is asked instead.

**128 characters** is Telegram's title limit, and `fit_title` shortens the *title*, never
the `Anime in Hindi` suffix: the suffix is the promise, the title is only an identifier. A
name over the limit doesn't get trimmed later — the `createChannel` call simply fails, with
an MTProto error that looks nothing like a length problem.

### The picture

Decided by `cover_choice`, in this order, and never any other way:

1. **A picture the owner chose.** Highest priority, because they can see the channel and
   we cannot.
2. **The cleanest cover already inside your master archive** — screened like everything
   else, poster-before-frame, then highest quality, then lowest season/episode so a
   re-run picks the same file. Recorded as `photo_candidate_id`.
3. **Nothing.** Telegram then shows the coloured initials tile — and a picture already on
   the channel that we did not put there is never removed: `profile_is_current` deliberately
   has no "clear the photo" branch, because the only reason the plan is empty is that the
   archive had no clean cover, so whatever is on the channel arrived by your hand.

There is deliberately no step 3½. We never take the *source* channel's photo: that is the
leech's branding, and a destination wearing a stranger's logo is the single most
convincing sign of a re-upload — which is the thing your screening gate exists to catch.
And we never download an image from a URL or re-render one: the free tier has no image
tooling, no bandwidth, and your standing rule is to pick a clean copy rather than
regenerate a dirty one.

So "no picture" is a *decision that gets reported*, not a failure: `plan_profile()` puts
the reason in its notes and `setup_plan()` carries it into the `set_profile` step.

One rule inside this is not a preference: a picture already on the channel that the plan
did not choose is **never** offered for removal. `profile_is_current` has no "clear the
photo" branch, because `editPhoto` with an empty input wipes a channel icon and the only
reason the plan is empty is that the archive had no clean cover — so whatever is on that
channel arrived by your hand.

### The bio

`templates.channel_about`, seeded as JSON `null` = **leave it empty**. A bio is the one
piece of text in this system that describes your channel rather than a file, and inventing
marketing copy for you is not this program's job. When you write it, you write it once in
`app.config` and no redeploy is needed.

### The publisher's permissions

`bots.channel_help_rights` in `app.config` is the single source of the admin rights
`@chelpbot` receives; `setup_plan` stamps the resulting dict onto the `add_channel_help`
and `promote_channel_help` steps, so an executor cannot accidentally apply a hardcoded set
instead of yours. Two rights are refused whatever that row contains — `can_add_admins` and
`can_ban_users` — and an unrecognised name raises rather than being ignored, because a
silently dropped right is how an operator ends up believing the publisher can edit posts
when it cannot.

### What never happens here

No public `@username` (that would make a private channel discoverable, and the private
destination rule is yours). No auto-delete of archive files. No promoting anyone except
`@chelpbot`. No second invite. No sticker per episode — season stickers only, from the
one approved pack, and only at a season's start or end.

---

## 2. Seasons: how "S2, episode 1" is understood, and what is not

### The signal set

`app/seasons.py::classify()` gets four facts and returns one verdict. It never reads the
calendar and never counts how long a series has been running.

| Input | Where it comes from |
| --- | --- |
| the episode number in this caption | `parse_episode` |
| a season **stated by the caption** (`S2`, `Season 2`, `2x01`) | same parse, and only this — see below |
| the season we are filing into, and the highest episode already there | one query, `season_stream()` |
| which seasons of this series already have episodes | same query |

The distinction in the second row is load-bearing. `parse_episode` fills in `season = 1`
for any accepted file so the database row can exist; that default is recorded as
`season_source = "none"`. If the pipeline passed `.season` around as if it were a
statement, then while we were filing season 2, every unlabelled file would read as "the
caption says season 1" — a rewind — and a channel-configured `season_hint` would be able
to open seasons on its own. Only `season_declared` is believed.

### The verdicts

| Your scenario | Verdict | What happens |
| --- | --- | --- |
| this series has no episodes at all yet | `first` | Season 1 opens quietly. No sticker "opens" the first season of a channel that has never had one except the one the destination setup already sent. |
| 12 filed, caption says `S2`, numbering restarts at 1 | `declared` | Season 2 opens (recorded `boundary_kind = 'declared'` with the evidence). Closing sticker for 1, opening sticker for 2, then the episode posts. |
| …caption says `S2` and numbering *continues* at 13 | `declared` | Same. A stated season beats arithmetic; channels that number across seasons are common. |
| 12 filed, caption says nothing, numbering restarts at 1 | `reset` | **Held.** It reads like a new season and probably is, but the two wrong answers (a second "Episode 01" post, or a season 1 that secretly contains season 2) are both permanent and public, so nothing is filed and you are asked. Flip `seasons.confirm_unlabelled_reset` off for a channel you have already watched do this, and the season opens as `inferred` — a different provenance, recorded as such. |
| an old number arrives (ep 7 after ep 12) | `backtrack` | Same season, another copy → `app.manifest` edits the existing post. No stickers, no new season. |
| a caption says `S1` while we are filing season 2 | `retreat` | Never acted on. Parked and asked. Re-creating season 1 because someone re-uploaded an old batch is unrecoverable in public. |
| a jump ahead (ep 9 after ep 5) | `continue` + a note | A gap says the middle is missing — nothing more. It does not end a season and it does not define its length. |
| a season number past 99 | `continue`, not confident | `S999999` is a typo or an attack, not a season. It is refused rather than filed into a season no manifest can ever finish. |
| a movie or a batch with no comparable number | `continue` | Season from the label or not at all; batch is excluded from arithmetic on purpose, because "Season 2 (1–12) batch" names a season loudly and numbers nothing comparable. |

### The stickers, and their order

The rule as you gave it: at a boundary, send the **end-of-season** sticker first, then
the **new season's** sticker, and only then continue uploading.

`transition_stickers()` encodes that, with three details that are not decoration:

* Only a *confirmed* boundary produces stickers. An unconfirmed `reset` produces none,
  because a closing sticker on a season that did not end is a false public statement sent
  to thirty thousand people.
* The closing sticker requires the season it closes to have had content. A season created
  and then abandoned gets no farewell.
* Each side has its own flag (`sticker_posted` for the opening one,
  `closing_sticker_posted` for the closing one), which is what makes a resumed run
  idempotent instead of decorative — and why there are two flags rather than one.

They are queued as `season_sticker` jobs rather than posted inline, because a sticker's
document id comes from the pack mapping, which is a live-account question. A job that
cannot run yet shows up as **blocked** in `/status`; an inline call would have to either
invent an id or fail the ingest — and failing an ingest over a divider sticker is how an
episode gets lost.

`publish_hold()` is what keeps the order honest: while the stickers for a new season are
still queued, that season's first episode post waits. It is not dropped, and it does not
jump ahead.

### What a boundary does *not* entitle anyone to say

A boundary proves **the source moved on**. It proves nothing about the show. So the
closing sticker is fine (that is a statement about the source's schedule), while "Complete
Season" is not — and `app.v_season_coverage.season_complete` now needs a *declared* span:
`first_episode`/`last_episode`, written only by your `/declare` on the control bot.

That separation is a bug fix, and it matters more than the sticker. Until migration 0005,
`ingest` wrote `last_episode` as "highest episode filed" while the view read the same
column as "how long the season is". A weekly source that paused for a week after episode
12 was therefore a finished 12-episode season, and the permanent batch post would have
gone out saying so. Now the observation lives in `observed_first`/`observed_last` and the
declaration in its own two columns, so the gap between *what arrived* and *what was
promised* is something `/status` can report instead of something the pipeline smooths over.

The two lines in the caption box read from those two places on purpose:

```text
❍ 𝗘𝗽𝗶𝘀𝗼𝗱𝗲: 01 - 12      ← observed: what this archive holds
◎ 𝗧𝗼𝘁𝗮𝗹 𝗘𝗽𝗶𝘀𝗼𝗱𝗲𝘀: 12      ← declared: how long the season is, or TBA
```

One honest boundary on that: `/declare` changes what the *rules* say — the caption line and
`season_complete`. Nothing in this repository turns an eligible season into a published
batch post yet, because the publisher (`publish_post`) is one of the unwired Telegram kinds.
`should_post_season_batch()` is the decision, written and tested, with no production caller
until the publish layer exists. So declaring today does not schedule a message; it removes
the ability to lie about one.

Say it once and the caption stops hedging:

```text
/declare dekin no mogura 1 12
```

---

## 3. When the source channel is only files

The third question the operator asked: a channel where the mp4s already sit, captioned
`episode 1`, `episode 2`, nothing else — no show name, no quality, no language. What can be
known, what must be told, and what cannot happen at all.

### What the pipeline can read from a bare file

| Field | Source | Honest answer if missing |
| --- | --- | --- |
| episode number | the text `episode 7`, `ep 7`, `[07]`, `S02E05` | park: an ingest row needs one |
| quality | the video's own pixel size — Telegram states it for every video document, and the **shorter** side is used so a vertical clip cannot turn 1080 into 1440 | `quality unknown` → the variant is recorded without a label and the caption's Quality line prints nothing fake |
| single file or batch archive | filename shape (`[01-12]`, "Batch") | treated as one episode |
| duplicate copy | file size + fingerprint, never filename | — |
| season | **only** a stated label (`S2`) | the channel's declared default, which is a starting point, not a claim (see below) |
| show name | the file's own title, or your `/source … series` | park |
| audio language | the file's own words, or your `/source … audio` | **park** — this one is not guessable |
| total episodes | never | `TBA`, until `/declare` |

### What you say once, per channel

```text
/source @anime_uploads4u series Bleach audio hindi
```

`/source` takes three facts, and each has a different consequence, which is why they are
recorded as `series_source` / `audio_source` / `season_source` rather than merged into the
file's own claims:

* **`series`** — what show this channel carries. This is the statement that lets a
  destination channel be *named*: reading a name off the channel's own title (`anime
  uploads 4u`) is one signal where the spec asks for two, so without this the files archive
  and the naming question is asked instead of answered.
* **`audio`** — what the files carry: `hindi`, `dual`, `multi`, `subbed`, `unknown`. This is
  the one that un-parks the backlog. Without it, every bare file lands on *"cannot determine
  whether the file carries Hindi audio"*, because publishing a subbed release as a Hindi one
  is the single mistake you named as unacceptable. A file whose own text contradicts the
  declaration keeps its own wording and is rejected if that wording is out of scope — your
  channel-wide statement never outvotes the file in front of it.
* **`season`** — assume this season when a file says nothing. It is a numbering default: it
  decides *which shelf* the first file goes on, and it can never open a season boundary, post
  a sticker, or state a length.

`/source @handle` with no key shows what is declared; `clear` stops assuming. Both refuse to
pick a channel when the name matches two rows. `ingest.accept_channel_audio_declaration`
turns the whole idea off globally while you work out why a caption says the wrong thing —
the declarations survive it, so it reverses without a re-scan.

### How the backlog gets filed

1. **Read**: one message → one candidate row, keyed by `(channel, message id, media index)`.
   A history sweep is the same call as a live message, so a restart resumes where the last
   message finished instead of starting over. Nothing is re-downloaded: the file stays where
   it is.
2. **Decide**: `parse_episode` + `classify`. Parked rows are re-read by the next scan;
   **decided rows never are** — that single rule is what makes "declare the channel, then
   rescan" safe on 400 files, and it is enforced by one condition in the upsert, not by
   discipline.
3. **Screen**: a bare mp4 usually has no clean cover of its own, and a missing thumbnail is
   never treated as clean. If another source channel has the same episode with a clean
   poster, that copy wins the picture; if none does, the file stays un-published and shows up
   as a review item. `thumbnail.on_no_clean_candidate` is the knob for what to do then.
4. **Log with the storage bot**: the message is *forwarded* to `@anime_hindifilesbot`, which
   makes Telegram copy the file server-side — no download, no re-upload, nothing touching the
   free-tier disk — and the bot's reply (single or batch link) is stored against that exact
   episode + quality. This is `storage_upload`, and it is one of the still-unwired kinds:
   the reply format has to be read once from the real bot (`/probe`, or a screenshot of its
   menu) before any of it is written down as code.
5. **Post**: Channel Help composes the approved box in the destination, buttons pointing at
   those stored links. `◎ Total Episodes` prints `TBA` until you `/declare`.

### The part that is not a promise

Steps 1 and 2 run today, tested against a real database. Steps 3–5 are the unwired Telegram
write layer: the rules, the ordering, the dedup and the refusal to invent anything are all
implemented and tested, but no message has ever been forwarded and no post has ever been
published by this code. So a files-only channel is a *solved metadata problem* and an
*unfinished transport problem*, and the two should not be confused when you decide how much
of the backlog to point at it first.

---

## 4. When the channel you add is the channel you publish in

Everything above assumes the pipeline *builds* a destination: a new private channel, a name, a
picture, an admin list, and posts made beside the files. There is a second shape, and it is the
one a channel full of your own files needs: **the channel you add is the destination**, and the
only thing wrong with it is the text under each video.

| | link mode (`link_post`) | in-place mode (`in_place_caption`) |
| --- | --- | --- |
| where the file lives | private master archive, then a link | already in this channel |
| what a post is | Channel Help composes text + buttons | the existing file message, edited |
| what is fetched | every accepted file | nothing, unless an episode is missing here |
| what is deleted | never | never |
| does "Hindi audio proven" gate it | yes | no — see below |
| inline buttons | yes | no (and nothing to link to) |

### Which channel is which, when two of them share a name

The rule the operator gave, implemented in `app.inplace.pair_roles`: the channel **we are admin
in** is the destination, the one where we are an ordinary **member** is a source. It is not a
trust judgement — an account with no posting rights physically cannot edit those messages, so
the rights *are* the answer, and guessing the other way round produces a job that fails on every
file. Two admin-able channels with one name is a question rather than a sort.

And **no** writable channel is not a question at all. It is the ordinary case, and the answer is
the second half of the job: the channel you joined is a **source**, and if no channel named
`{TITLE} Anime in Hindi` exists, it is **created**. This sentence is the correction the operator
made the same day the mode was built ("tab bhi channel banane wala hissa skip mat karne lag
jana"), because the tempting wrong answer is *"make me admin here and I will caption it"* — which
reads as help, and in practice leaves a finished season sitting in a source channel with nowhere
to go. So `app.inplace.route_for` returns `create_destination`, and `/inplace` refuses to record
an in-place mode on a channel it cannot write in. Rights that have never been read count as member
rights, never as admin rights, and the reply names the missing check instead of guessing. Both
outcomes are recorded — `app.source_channel.we_are_admin` and `publish_role` — so a later run
explains the decision rather than silently re-making it.

One thing not to oversell: nothing reads those rights on its own yet. The only code that asks
Telegram "what are we here" is the probe's chat inspection (`app.probe`), and the MTProto layer
that would run `GetFullChannel` on every joined channel is still unwired. Until it is, the column
is a value the operator sets once in the dashboard — which is exactly what `/inplace` says when it
refuses, rather than quietly treating an unread channel as writable.

### Twelve files there, twelve files here: twelve edits, zero copies

The comparison is `app.inplace.compare(destination_episodes, source_episodes)`, and the case the
operator described — 12 raw files in the source, the same 12 raw files in the destination — comes
back as `12 caption` and nothing else. No episode is fetched, because none is missing.

Two other answers it can give, both deliberate:

* an episode only the *source* has becomes `copy_then_caption`, and the copy is a **server-side
  forward from the master archive** — the free tier buys no download;
* equal counts with **no overlap** (here `1-12`, there `13-24`) is *not* twelve missing files. It
  is one renumbering scheme against another, and copying would paste the whole season onto itself.
  So `SeasonShape.numbering_shifted` reports the offset and the plan emits **one question and zero
  copies**. A partial overlap proves the two lists use the same scheme, and then the difference is
  acted on.

### What gets overwritten without asking

`looks_like_label` is the whole safety rule: an empty caption, or one that is nothing but an
episode marker (`episode 7`, `E07`, `07/24`, `PART 3`, `12 - mkv`, `S2 E3`), is a label and is
replaced. Anything carrying a link, an `@handle`, a date, or the words *fixed / updated / note /
source / mirror / link* is information, and it becomes `ask` with the old text preserved — in
Telegram a replaced caption is gone, so `app.destination_post.caption_previous` is the only copy.

Two more refusals, both small and both load-bearing: a caption over Telegram's **1024-character**
media-caption limit is a question, never a truncation; and a message whose text already equals the
caption we would write is a `skip`, which is what makes a restart in the middle of a 400-episode
backlog resume instead of re-editing.

### Why the audio gate does not apply here

The Hindi-audio rule guards the door through which files *enter* a channel: it is what stops a
stranger's subbed release being published to 30k members as a Hindi one. In-place mode opens no
such door — you posted the file, it is your channel, and a caption withheld from your own video is
a formatting failure rather than a scope violation. So `inplace.mode_allows_missing_audio` turns
that one gate off for this mode, and nothing else moves: a file whose own text says *subbed* still
says subbed, and a file being **brought in** from another channel is still judged by every rule in
part 3. The caption prints `〄 𝗔𝘂𝗱𝗶𝗼: Unknown` rather than inventing `Hindi`; say
`/source <channel> audio hindi` and it prints Hindi with the provenance to back it.

### What is not finished

The decision table, the plan, the caption and the mode are implemented and tested, and
`/inplace` shows the plan against the real rows today. The edit itself is one call in the MTProto
layer — `EditMessage` with the new text — which is the same unwired write path as publishing. The
job kind exists (`edit_post`), the dedup key exists (`inplace:<destination>:<message>`), and the
row to record the result exists; what is missing is a live session to send from. So `/inplace` says
"this command changed the plan, not the channel", and that is the accurate sentence until the first
real edit goes out.

---

## Where each decision is recorded

| Question | Answer lives in |
| --- | --- |
| why does this season row exist? | `app.season.boundary_kind` + `boundary_evidence` |
| what has the source actually delivered? | `app.season.observed_first` / `observed_last` |
| who said the season is 12 episodes long? | `app.season.declared_by` (`operator`) and `declared_at` |
| did the closing sticker already go out? | `app.season.closing_sticker_posted` |
| how far did channel setup get before the restart? | `app.destination.setup_state` |
| which picture is on the channel, and why that one? | `app.destination.photo_source` / `photo_candidate_id` |
| whose words were the series / the language / the quality? | `app.source_channel.declared_*`, mirrored into the candidate's `parsed` as `*_source` |
| which publishing mode a destination uses, and which source it was compared with | `app.destination.publish_mode` / `paired_source_channel_id`, plus `app.source_channel.publish_role` |
| who may write in a joined channel, and so whether a destination has to be built | `app.source_channel.we_are_admin`, read by `app.inplace.route_for` into `create_destination` |
| what the text under a file post said before we captioned it | `app.destination_post.caption_previous` (and `edits`, how often it was rewritten) |

A decision that is only in a log line is a decision nobody can audit next month, which is
why every one of these is a column rather than a comment.
