# Auto Manager — Requirements Draft

Last updated: 2026-08-27 (reconstructed from operator conversation history; this is the authoritative spec record)

## 1. Scope and authorization

The system processes only channels, media, and branding owned by or licensed to the operator. It must not scrape
members, send unsolicited bulk advertising, evade Telegram safeguards, or bypass access controls. Every publishing
and deduplication rule below assumes the operator holds distribution rights to the source material.

## 2. Content classification

- Support individual episodes and complete-season batches.
- Exclude subbed-only releases.
- Include a file when Hindi audio is indicated, even if it also contains other languages (Hindi dual/multi-audio is
  in scope).
- Recognize series, season, episode, language, quality, and release variant from source channel name, caption,
  filename, and known mappings.
- Ambiguous metadata is held for review instead of being guessed.

## 2b. Files-only sources (added 2026-08-28)

Some source channels carry no captions at all: one message per file, text like `episode 7`.
For those, three facts may come from the operator's per-channel declaration instead of the
file — which series, which audio, which season by default (`app.source_channel.declared_*`,
set with `/source`) — and each is recorded with its provenance so a declaration can never be
mistaken for the file's own statement. A declaration may not: invent a quality label, state a
season length, open a season boundary, or outvote a file whose own text contradicts it.
Quality may come from the video's pixel size (shorter side), because Telegram reports that
without a download; language may not come from anything but text or a declaration.

## 2c. In-place publishing (added 2026-08-28, supersedes parts of 2b, 5 and 6)

The channel the operator adds is sometimes the channel to be published: it already holds the
episodes as files, each message saying nothing but `episode 7`. In that shape the job is
**captioning, not fetching** — the operator's words were "unhe theek karoge, mtlb post bana doge".

- The existing file post is **edited in place**. No new post beside it, no re-post, and the earlier
  idea of deleting the file afterwards is set aside: an edit keeps the post working and a deletion
  buys nothing. Nothing in this mode deletes.
- When two same-named channels are joined and we are **admin in one, member in the other**: admin
  is the destination, member is the source. Rights decide it, because a channel we cannot post in
  cannot be captioned. Both holding the same 12 episodes means **12 edits and nothing else**.
- The approved caption is written on the file itself; Channel Help is not involved (it posts text
  plus buttons, and here there is no link to put in a button), and no inline keyboard is claimed —
  a user session cannot attach one to a media message.
- The **Hindi-audio gate does not apply** to it: that gate guards files *entering* a channel. A file
  whose own text says `subbed` still says subbed; a file being brought in from elsewhere is still
  judged by every rule above.
- A caption replaces existing text only when that text is an episode **label**. Anything with a link,
  a handle, a date or a second sentence in it is left alone and asked about, and the replaced text is
  stored (`app.destination_post.caption_previous`) because Telegram keeps no copy.
- **Channel creation is never skipped by this mode.** If the operator hands over a channel link and we are only a
  *member* there — or have never read our own rights — then that channel is a source, and a destination named
  `{TITLE} Anime in Hindi` is created when no such channel exists. `app.inplace.route_for` returns
  `create_destination` and `/inplace` refuses to switch a channel it cannot write in. The ability to caption in place
  is not an alternative to building the destination, and the standing rule that creation needs no confirmation still
  applies to the created one.
- **In-place mode replaces no step of the pipeline.** The order stays: caption the file, hand it to the storage
  helper, take the link back, create the post that carries that link. What the mode decides is *which message a
  caption is written on* — the file message that already exists, rather than a copy — and nothing else. It is not a
  way to finish at "now the files have captions", and it lifts no rule about the files: the Hindi-audio gate, the
  approved template, and the ask-before-overwrite rule all apply in both modes, because in both modes something
  reaches an audience. (Stated by the operator after the mode was first read too broadly: *"there should be no
  destination channels with nude files"*. The relaxation it killed was `inplace.mode_allows_missing_audio`, and the
  gate states `relaxed-for-in-place-captioning` / `captioned_without_audio_claim` no longer exist anywhere.)
- Corrections that apply to both modes: a source channel's files and thumbnails need not be
  watermark-free to be usable — screening **ranks** copies and picks the best, and a channel with no
  clean candidate is flagged and published with the least-bad copy rather than blocked (content is
  the operator's own; leech channels re-watermark it). Re-rendering a thumbnail by downloading and
  re-uploading still requires asking first.

### 2d. The updates channel (a fourth thing the app must know about, not a fifth pipeline)

- The operator runs a separate **announcements channel** for the whole brand. When episodes land in a
  series channel, that channel gets a post saying which series, which season, and which episode was added —
  and a link. Nothing else happens there: no files, no requests.
- The link is not the invite. It is a bot deep link — `@Link_providerobot` answers `/genlink` with "send me a
  message", and the message it wants is a **forward of the series channel's card post** (the picture carrying
  that channel's private invite in its caption). The bot replies with one `t.me/<bot>?start=` link, plus a
  `SHARE URL` button.
- So the object stored per series channel is that link, and the announcement is rendered from it. The token is
  stored, not the URL, so a bot rename cannot rot a published post.
- Four decisions, given 2026-08-28 and recorded in `app.config`: **one** updates channel for the whole brand
  (`updates.channel`), posted by **the app's own account** as plain text, **one announcement per episode** as it
  lands (`updates.per_episode`), and the shareable link **survives its card**, so one token per series channel is
  reusable and a rotated invite can regenerate the card. That last answer changes no deletion rule: nothing is
  deleted because of it, it only means an old announcement cannot be broken by a new card.
- Recorded in `app/linkprovider.py` with its open questions, in [docs/updates-channel.md](docs/updates-channel.md).
  The announcement shape is **not** in the approved caption templates, deliberately: describing a post and being
  allowed to send it are two different permissions in this project.
- Whether this mode changes anything about storage, publishing or deletion: it does not. The pipeline is the one
  in §2c — caption the file, store it, take the link, post it — and an announcement is what follows a post.

## 3. Destination channels

- A destination holds **captioned posts with links**, never the files themselves. A channel whose messages are bare
  files is a source — even when we are admin there and could caption it, which is what makes it a source *we may
  also write on*, and never what makes it the destination.
- One **private** destination channel per complete series.
- Default name: `{TITLE} Anime in Hindi`.
- Title is derived by cross-checking the source channel name against captions and filenames in that channel. Both
  signals must agree before a name is generated (e.g. source `berserk` + episodes of *Berserk* → `Berserk Anime in Hindi`).
  This two-signal rule is about **naming a channel that does not exist yet**. A channel that already exists — an
  in-place destination, or a caption on a file — takes its title from one signal (the declared series, the channel
  name, or the filename), records that as `series_source = 'channel_name'`, and parks only when the signals
  contradict each other (operator correction, 2026-08-28).
- Destination channels are created automatically when both signals agree; no interactive confirmation is required.
- The name is capped at Telegram's 128 characters by shortening the title and never the `Anime in Hindi` suffix.
- **Profile, at creation, before anyone else is involved:** title, photo and bio are set as one ordered step
  (`set_profile`) so a viewer never meets a half-built channel.
  - Photo: an operator-chosen image wins; otherwise the cleanest cover already inside the master archive
    (screened, poster before frame, highest quality, earliest episode); otherwise **nothing**, and the reason is
    reported. The source channel's picture is never copied — that is a leech's branding, and a destination
    wearing it is the exact look the screening gate exists to prevent. No image is ever downloaded or re-rendered.
  - Bio: `templates.channel_about`, seeded empty. A bio describes the operator's channel, so the program leaves it
    blank rather than inventing marketing copy; editing that config row is the only way text appears.
  - No public `@username` is ever assigned: a private destination with a handle is a discoverable one.
- Description template:

  ```text
  Watch or download {TITLE} in Hindi. Available seasons, episodes, and qualities are organized below.
  Updates: @ycanime | @india_crunchyroll
  ```

- On creation, add `@chelpbot` (Channel Help) as administrator with the permissions its documentation requires:
  post messages, edit messages, delete messages, invite/add users (the guide's list, transcribed in
  docs/channel-help.md; the first draft of that file was written before it was fetched, and said only that a channel
  has to add the bot first). If confirmation is not received, remove and re-add once, then alert the owner.
  - **Unresolved tension, made configurable rather than decided for the operator:** the implementation's default
    withholds `can_invite_users` (a publisher that can invite is a publisher that can be used to spam the channel).
    `bots.channel_help_rights` in `app.config` lists the rights to grant, so following Channel Help's own setup
    instructions is a config edit; `channels.setup_plan()` reads it and stamps the resulting permission dict onto
    both admin steps, which is the only place the row is read. `can_add_admins` and `can_ban_users` are refused whatever that row says, and an
    unrecognised right is an error rather than a silent no-op.
  - The right this project is least comfortable about is `can_delete_messages`, and it is here on the strength of
    Channel Help's own setup instructions (docs/channel-help.md), which require it so the bot can manage a post it
    did not write. Nothing else may hold it: no publisher, no storage assistant, and this app's own session, whose
    zero-deletion rule is also the tool's real limit — a message older than 48 hours cannot be deleted by anyone.
- Create a short-lived, **one-use** invitation link and send it to the configured main Telegram account. Promote
  only the exact configured numeric id in `telegram.main_admin_user_id` with all supported channel admin rights, then revoke the
  invite immediately. An unexpected account using the link is never promoted and the owner is notified.
- The spare automation account remains the channel creator; administrator promotion is not ownership transfer.

## 4. Individual and complete-season posts

- If only some episodes of a season are available, publish one post per episode.
- When a complete season becomes available, additionally create one season batch/universal-link post covering the
  available episode range (for example, `Episode 1 - 24`).
- Completeness is decided by `manifest.should_post_season_batch` over `v_season_coverage`, which needs a declared
  span *and* a file per episode. The function is built and tested; the code that turns an eligible season into a
  queued publish job belongs to the unwired publish layer, so no batch post can appear from a config edit alone.
- **Both forms are retained permanently.** Individual episode posts are never deleted or replaced when the season
  batch post is created, so viewers can still retrieve a single episode.

## 5. Multi-source selection

- Multiple authorized source channels may supply the same series; they are alternative sources, never separate
  destinations.
- Canonical key: `series + season + episode + Hindi-language eligibility + quality + release variant`.
- Search sources in configured priority order and merge missing qualities across sources. Different qualities of the
  same episode may legitimately come from different sources.
- Arrival order never controls display order; the database manifest does.
- Default quality order: `360p, 480p, 720p, 1080p, 2160p`.
- Duplicate detection uses episode metadata, file size, Telegram file identifiers, and media fingerprints — never
  filenames alone.
- Higher-quality replacement or conflicting captions/filenames are held for owner review.

## 6. Thumbnail policy

- A clean thumbnail is a **hard publication requirement**; nothing is published while uncertain in strict mode.
- Allowed handles: `@ycanime` and `@india_crunchyroll` — either, both, or neither is acceptable.
- Any other OCR-detected text beginning with `@` is disallowed; the candidate is rejected and sent to the review queue.
- Download only the small thumbnail for screening, not the video, whenever possible.
- Prefer a clean candidate from another configured source before doing any heavy work.
- If no clean candidate exists: ask the owner first, then (only on approval) attempt full download/reupload with a
  freshly generated clean thumbnail from an authorized video frame. Watermarks burned into video frames cannot be
  removed reliably and remain a review case.
- Fallback when the owner declines: `wait and rescan`, `manual source selection`, or `skip that quality` — owner's choice.

## 7. Branding replacement rules

Both handles are primary and always appear together.

- Disallowed usernames found in editable captions are replaced with:

  ```text
  @ycanime | @india_crunchyroll
  ```

- Channel descriptions and post footers use the same pair.
- Filenames replace unsupported characters (such as `|`) with a safe separator, for example:

  ```text
  Bleach_S01E01_1080p_@ycanime_@india_crunchyroll.mkv
  ```

- A destination channel inherits the source channel profile picture because the operator confirms the sources are
  their own channels.

## 8. Canonical archive

- Maintain one **private master archive channel** holding every selected/processed file — the canonical backup.
- Store one canonical archive message per media variant, with `archive_chat_id`, `archive_message_id`, and media
  fingerprint in Supabase.
- Caption uses the template in §12; add searchable internal tags.
- Source stickers are never copied into the archive or the destination.
- Archive files are never auto-deleted.
- A destination post is published only after the archive copy and the generated storage link are both verified.
- Telegram content-protection must remain disabled on the archive so files can be forwarded to the storage bot.
- Telegram is a working backup, not the only permanent backup; operator keeps external copies of critical originals.

## 9. Season stickers

- Approved pack: `https://t.me/addstickers/OCtbqTQ_by_sticbot` (`@YCAnime`).
- Post the mapped season sticker as the first message of a new destination channel, and immediately before the first
  episode of each subsequent season — not before every episode.
- Season → sticker mapping is auto-detected from sticker labels (`S1`, `Season 1`, `S2`, …) once the account is
  connected; unclear or missing mappings are presented to the owner for a one-time selection and remembered
  permanently. If a season has no configured sticker, that season's import pauses rather than posting a wrong sticker.
- Duplicate season stickers are prevented across restarts and repeated scans.
- When a channel is created it starts with the operator's sticker pack.
- A season boundary is decided by `app/seasons.py::classify()`, which is given four facts: the episode number,
  whether the caption itself named a season, the season being filed and its highest episode, and which seasons of
  the series already exist. It returns one verdict and never reads the calendar.

  | Verdict | Trigger | Action |
  | --- | --- | --- |
  | `first` | the series has no episodes | open season 1 |
  | `continue` | number above the highest seen in the same season | file it (a gap is recorded, never used as an ending) |
  | `declared` | the caption names a different season | open that season, and record `boundary_kind = 'declared'` with the evidence |
  | `reset` | numbering restarts with no caption statement | hold: park the candidate and ask, unless `seasons.confirm_unlabelled_reset` is off, which files it as `boundary_kind = 'inferred'` |
  | `backtrack` | an old number in the current season | same season; `app/manifest.py` edits the existing post |
  | `retreat` | a caption naming an older season | never acted on; parked and asked |

  A season number above `MAX_PLAUSIBLE_SEASON` (99) is treated as a typo or an attack, not a season.
- At a confirmed boundary the closing sticker for the old season is queued **before** the opening sticker for the
  new one, and the new season's first episode post is held (`publish_hold`) until both have run. Only a confirmed
  boundary produces stickers, and a season with no content gets no farewell.
- Stickers are `season_sticker` jobs rather than inline calls: the document id comes from the pack mapping, which is
  a live-account question, and a job that cannot run yet shows up as blocked instead of failing the ingest.

## 10. Storage bot

- Bot: `@anime_hindifilesbot`. Its command menu was observed from the operator's screenshots on 2026-08-28 and is
  recorded verbatim in [`docs/storage-bot.md`](storage-bot.md) / `app/storagebot.py`: `/genlink` (one message or file),
  `/batch` (many messages from a channel), `/custom_batch` (an explicit list), `/special_link` (an **editable** link,
  moderators only), `/universal_link` (one link across the bot's clones, moderator only), `/shortener`, `/settings`,
  plus `/broadcast`, `/ban`, `/unban`.
- **Never sent by this program:** `/broadcast`, `/ban`, `/unban`. They act on people, and the refusal is checked
  before the probe's own allowlist so widening that list during testing cannot grant the capability.
- What the `/batch` flow **does** answer (operator's screenshots, 2026-08-29, recorded in `docs/storage-bot.md` and
  `app.storagebot.BATCH_FLOW`): `/batch` asks for the first message of a range and then the last, as a tagged forward
  or a link, and answers `Here is your link:` with one `t.me/<bot>?start=` link plus a `SHARE URL` button. Opening it
  re-sends the whole range verbatim, source labels and `❌ END OF SEASON ❌` included.
- **Batch granularity, decided by the operator on 2026-08-29:** one batch per episode holding every quality of that
  episode, plus one final batch covering the whole season. A batch is a range, so neighbouring files are one batch and
  no re-indexing is needed; `/custom_batch` is the fallback for a season whose qualities are scattered.
- **Link lifetime:** the operator's word (2026-08-29) is that a link works forever, and that "all messages will be
  deleted after 5 minutes" is the clone's autodelete acting on the *delivered copy* — a switch the owner can change,
  present so that a copyright claim does not land on the bot, which is why the bot tells users to save the files. Two
  rules follow: nothing published may reference a message id inside the bot chat, and this program never deletes
  anything whatever the clone is set to do.
- **These bots are our own clones** of `@Md_Files_Store_Bot`, made with `@Md_CloneManagerBot` (vendor channel
  `t.me/venombotupdates`). So "moderators only" is our own moderator list, "your clones" is our own set (up to three
  per Telegram account), and the clone's `@username` is load-bearing: a published link embeds it, and the vendor's
  parent bots get renamed or deleted every year or so while clones keep serving. Never rename a clone that has
  published links, and keep the clone as a member with access in every private source channel.
- What still keeps `storage_upload` blocked instead of implemented is the write layer (forward, read the reply back,
  store the token) plus `app.storagebot.still_unknown()`: whether a link is a reference to the source post or a copy
  the clone made, whether a batch can be appended to, whether the clone is in Public or Private Mode, whether "No
  Forward" is on, and the rate limit. `/probe` re-reads the menu and reports any drift against the recorded copy.
- Files go to the archive/storage workflow; destination channels receive text/link posts only.
- Missing-quality flow: upload only the new variant → add to the ordered manifest → rebuild or extend the batch /
  universal link → edit the existing destination post in place. Never resend the season sticker or create a second
  post for the same episode.
- If link revocation is supported, revoke superseded links after the new one is verified.

## 11. Destination publishing (Channel Help)

- Channel Help is used **only** to create and edit destination text posts with inline URL buttons. It does not handle
  source monitoring, files, archive backup, storage links, deduplication, or join requests.
- Restated and narrowed by the operator on 2026-08-29: the announcements channel's post is created **by this
  program's own session**, so Channel Help is not needed there and is not used there; Channel Help posts only in the
  series destination channels, doing only what it was configured to do in them. `docs/updates-channel.md` carries the
  announcement's text and `app/linkprovider.py` renders it; the sender that would place it is unwired.
- Official button syntax: `Button text - https://url`, `&&` joins multiple buttons on one row, new lines create new
  button rows.
- Buttons by the plan: one per quality (`480p - LINK_480 && 720p - LINK_720`) or a single `Get Episode NN` link.
- The spare user client drives Channel Help's live inline menus; buttons are located by visible label rather than
  fixed position so menu reshuffles don't cause misclicks.
- The adapter is isolated as `ChannelHelpPublisher` because third-party menus can change. If it breaks, completed
  posts queue safely and the owner is notified — file processing continues regardless.

## 12. Templates (approved by the operator, 2026-08-28)

The three captions below are the operator's own samples, adopted verbatim and stored
in `app.config` (`templates.archive_caption`, `templates.episode_post`,
`templates.season_post`, plus the two `templates.*_button` rows). `0004_approved_captions.sql`
is the migration that replaced the placeholders; rendered examples and the
placeholder table are in [captions-approved.md](captions-approved.md).

Archive file caption:

```text
‣ Dekin no mogura: The earthbound mole (S - 01)

╭────────────────────
┣Quality: 480p
┣Episode: 11
┣Audio: Hindi #O𝖿𝖿𝗂𝖼𝗂𝖺𝗅
╰────────────────────

‣ Powered By: @india_crunchyroll
@YCAnime
```

Individual episode destination post:

```text
✦ Dekin no mogura: The earthbound mole ✦

╔━━━━━━━━━━━━━━━━━━━━━╗
⌲ 𝗦𝗲𝗮𝘀𝗼𝗻: 1
❍ 𝗘𝗽𝗶𝘀𝗼𝗱𝗲: 01
〄 𝗔𝘂𝗱𝗶𝗼: Hindi
◎ 𝗧𝗼𝘁𝗮𝗹 𝗘𝗽𝗶𝘀𝗼𝗱𝗲𝘀: 12
♡ 𝗣𝗼𝘄𝗲𝗿𝗲𝗱 𝗯𝘆: @YCAnime , @India_crunchyroll
╚━━━━━━━━━━━━━━━━━━━━━╝
```

Complete-season batch post:

```text
✦ Dekin no mogura: The earthbound mole ✦

╔━━━━━━━━━━━━━━━━━━━━━╗
⌲ 𝗦𝗲𝗮𝘀𝗼𝗻: 1
❍ 𝗘𝗽𝗶𝘀𝗼𝗱𝗲: 01 - 12
〄 𝗔𝘂𝗱𝗶𝗼: Hindi
◎ 𝗧𝗼𝘁𝗮𝗹 𝗘𝗽𝗶𝘀𝗼𝗱𝗲𝘀: 12
♡ 𝗣𝗼𝘄𝗲𝗿𝗲𝗱 𝗯𝘆: @YCAnime , @India_crunchyroll
╚━━━━━━━━━━━━━━━━━━━━━╝
```

Inline buttons, in Channel Help's `text - url` syntax (`&&` = same row, newline = new
row), one per available quality in manifest order:

```text
❐ 𝗪𝗮𝘁𝗰𝗵/𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱 ❐ - https://t.me/anime_hindifilesbot/111
```

```text
❐ 𝗪𝗮𝘁𝗰𝗵/𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱 480p ❐ - https://t.me/anime_hindifilesbot/1
❐ 𝗪𝗮𝘁𝗰𝗵/𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱 720p ❐ - https://t.me/anime_hindifilesbot/2
❐ 𝗪𝗮𝘁𝗰𝗵/𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱 1080p ❐ - https://t.me/anime_hindifilesbot/3
```

Rules that the formatting has to keep honourable:

- `{title_full}` is a single stored value (`title` + optional `: subtitle`), used by
  the archive and the destination alike, so the private record and the public post
  can never disagree about a series name.
- `{total_episodes}` and the batch's `{episode_range}` come from the season's
  declared length only. An unknown length prints `TBA`; the highest episode number
  seen is never treated as the total. The declared span is `app.season.first_episode` / `last_episode`,
  written **only** by `/declare <series> <season> <count|tba>` on the control bot; ingest writes the
  span it observed into `observed_first` / `observed_last` and never touches the declared pair.
  `season_complete` in `app.v_season_coverage` therefore needs a declaration, not a pause in uploads.
- Every frame line is single-newline separated, because `╭ ┣ ┣ ╰` only reads as a box
  when the strokes are adjacent.
- A placeholder with no value is reported by name to the publishing job, and the job
  does not post a caption containing a literal `{quality}`.

These blocks show one state of a post, not a template that is appended to. When
a quality arrives later the whole message is re-rendered from the manifest and
the existing post is **edited in place** (see §13) — no second "Episode 1"
message, and no line silently added out of order. The button list is therefore
generated from the manifest every time, in `quality_rank` order.

## 13. Missing-quality updates

- Maintain an ordered manifest per episode:

  ```json
  {
    "series": "Bleach",
    "season": 1,
    "episode": 1,
    "files": [
      {"quality": "480p", "archive_message_id": 0},
      {"quality": "720p", "archive_message_id": 0},
      {"quality": "1080p", "archive_message_id": 0}
    ]
  }
  ```

- Every episode stays "open for upgrades" indefinitely; a missing quality may be added days or months later.
- Editing an existing post preserves its position in the channel history (Telegram cannot insert messages between
  older ones), which is why each episode owns exactly one permanent index post.

## 14. New-source onboarding

- A periodic reconciliation scan detects channels the account joined while the service was offline, and the client
  reacts to join events in near-real-time.
- On detection the agent asks privately: map to an existing destination, create a new destination, monitor only, or
  ignore.
- Nothing starts copying merely because the account joined a channel. Processing begins only after the operator
  confirms source, destination mapping, allowed languages/seasons, templates, and backfill range.
- Joining happens only through owner commands and an allowlist — never uncontrolled automatic discovery.

## 15. Join-request handling

- Operates only on channels selected by the owner, using `messages.getChatInviteImporters` (requires channel admin)
  for still-pending requests, including older pending ones. Requests already approved/declined/withdrawn cannot be
  reconstructed; from deployment onward everything is logged in Supabase for a permanent history.
- Sending a message **never** approves or declines a request. Requests stay pending until a separate, disabled-by-
  default command changes them.
- Default to owner-triggered campaigns rather than auto-messaging every new requester, until the operator
  finalizes the policy. The command the spec predicted (`/dmrequests`) is **not** what got built: on
  2026-08-29 the operator asked for the wording to be settable at any time from the assistant, so the half
  that exists is `/joinmsg` (three drafts to pick from, or your own words; `app/joinmsg.py`) writing the
  `joinrequest.message` config row. A campaign row stays `draft` — starting one is a later decision, and
  the row's own `campaign_never_approves` check is what makes "we approved them by other means" impossible
  to spell.
- Deduplicate with a unique `(campaign_id, user_id)` record so one campaign cannot message the same user twice. A
  user may be contacted by different campaigns.
- Replies from contacted users are forwarded to the configured main account.
- Respect every Telegram FloodWait and stop on restriction. Privacy settings may block DMs, so failures are reported
  and never retried aggressively. No member scraping, no bulk unsolicited messaging, no account rotation or timing
  tuned to evade Telegram anti-spam enforcement — there is no safe promise that tens of thousands of private
  messages complete quickly.
- Log successful / failed / skipped / already-contacted outcomes.
- Message template: **the operator's, whenever they choose to write it.** Empty by default, which means nobody is
  contacted; `/joinmsg show` prints what is saved and says in the same breath that nothing has been sent.
  `{name}` and `{series}` are the only placeholders, and `app.joinmsg.refusals` is the one place that says
  why an invite link may not appear in this message (a DM carrying an invite admits the person past an
  approval that was never given).

## 16. Owner control interface (revised: a control bot is in scope)

*Superseded decision.* An earlier draft said "no bot of our own — the third-party
bots are services we message, and the operator uses HTTP". That was reversed on the
grounds that the operator does not run Python: a command list reachable only by
`curl` with a bearer token is not an interface they can use. The third-party bots
(`@anime_hindifilesbot`, `@chelpbot`) are still services we message; what is new is
**our own bot as the front-end** — including for logging the user account in.

Two surfaces, one set of switches. Neither has authority the other lacks:

| In Telegram (private chat with our bot) | Over HTTP (`/control/*`, bearer `CONTROL_TOKEN`) |
| --- | --- |
| `/status` `/pause` `/resume` `/reconcile` `/probe` | `GET /health` `GET /status` |
| `/sessions` `/use <name>` `/forget <name>` | `POST /control/pause` `.../resume` |
| `/login <name> +<phone>` → `/code` → `/password` | `POST /control/reconcile`, `POST /control/probe` |
| `/cancel` | `POST /control/shutdown` (no bot equivalent by design) |

Rules agreed for the bot:

- **Owner-only, fail-closed.** `TELEGRAM_OWNER_USER_IDS` (plus
  `TELEGRAM_MAIN_ADMIN_USER_ID`) is required; with none set the bot refuses to
  start rather than answering whoever finds it. A message from any other id is
  dropped before its text is parsed — no reply, no echo, no "unauthorised".
- **Private chats only.** In a group or channel it refuses, because a chat id that
  contains the owner is not a chat the owner controls.
- **It cannot touch content.** The Bot API gives it no read access to foreign
  channels, no media download, no channel creation and no permission changes — and
  this build gives it no file-sending method at all. It cannot be tricked into
  leaking a file from the machine it runs on.
- **Login secrets are transient.** Phone number, code and 2FA password exist in
  memory for one attempt, their chat messages are deleted after use, the password is
  cleared in a `finally`, and every outgoing line passes a scrubber that removes
  session-shaped text — so an exception cannot print a session into the DMs.
- **Bounded against Telegram's own limits.** 3 code requests per 10 minutes, 3 wrong
  codes per flow; the third failure closes the flow instead of trying the account's
  patience.
- **Switchable off.** `BOT_ALLOW_LOGIN=0` once the session is stored: a deployment
  that cannot start a login has one fewer door.
- Destructive actions, mass messaging and privilege changes still require explicit
  confirmation, and `/shutdown` remains HTTP-only: a kill switch that can be pressed
  from a chat window is one lost phone away from being pressed by someone else.

## 17. Persistence, deployment, and continuity

- Supabase stores configuration, manifests, jobs, archive/destination message IDs, links, deduplication keys, audit
  logs, and watermark-review entries. The free tier is sufficient for metadata only — never media blobs.
- Render runs a web service exposing `/health` for UptimeRobot, plus a Telegram reconnect watchdog, retry queue, and
  startup reconciliation scan.
- Processing stages persist immediately:

  ```text
  DISCOVERED → THUMBNAIL_CHECKED → ARCHIVED → SENT_TO_STORAGE_BOT → LINK_RECEIVED → DESTINATION_POSTED → COMPLETED
  ```

- Jobs are idempotent and resume from the last completed stage; a database lock prevents two workers uploading the
  same file. No correctness depends on the ephemeral local filesystem.
- The MTProto session never passes through a terminal, a file, or a chat with the agent: the control bot asks for
  the phone number and code inside the deployment and stores the result in `app.telegram_session` (RLS on, zero
  policies, `service_role` only, never selected into a reply or a log line). This replaces "an encrypted Render
  environment secret" — there is no second secret in this architecture to encrypt it with, and a value that cannot
  be read back by the operator is worse than one held in a table only the app can reach. `TELEGRAM_SESSION_STRING`
  still wins when set, so an operator who prefers the environment route loses nothing.
  Revocation is Telegram's own: Settings → Privacy and Security → Devices. `/forget` deletes our copy and says out
  loud that it has not signed anything out.
  The operator enters phone number, code and 2FA password themselves; these are never shared with the agent.
  The client cannot access Saved Messages, change 2FA, delete the account, or export unrelated private chats.
- Honest limitation: Render free web services spin down after ~15 minutes of inactivity, may restart at any time, and
  are capped at ~750 instance hours per month. UptimeRobot reduces idle spin-down but cannot guarantee continuous
  uptime. The guarantee provided is therefore **eventual, lossless processing** rather than uninterrupted
  connectivity: anything published while offline is discovered and processed on wake, with no duplicates. Genuinely
  uninterrupted throughput requires a paid always-on service or VPS.

## 18. Open decisions

1. Season → sticker mapping (auto-detect on first run, else owner picks once) — pending live account connection.
2. ~~Join-request message template — operator deferred this.~~ **answered 2026-08-29:** the operator asked to be
   able to set it at any time instead of once in a chat, so it is a setting (`/joinmsg`,
   `app.config` key `joinrequest.message`, rules in `app/joinmsg.py`) and it ships empty, which still means
   "contact nobody". Picking the sentence is open; being able to write it is not.
3. Storage bot command protocol — requires authenticated integration testing.
4. ~~Template refinements beyond the §12 defaults~~ — the operator dictated the three caption formats and
   the button label on 2026-08-28; §12 and `0004_approved_captions.sql` carry them.
5. ~~whether `@YC_Anime` and `@ycanime` are the same channel~~ — **answered 2026-08-28:** the
   handle is `@YCAnime`, which casefold-matches `branding.primary_handles`, so the footers and the
   publish gate agree and the allowlist did not need widening. `@India_crunchyroll` differs from
   the stored `india_crunchyroll` in case only, which Telegram ignores; `branding.footer` was
   updated to the operator's casing in the same migration.
6. ~~Whether a season that has stopped receiving files is a finished season~~ — **answered 2026-08-28, and it
   turned out to be a bug rather than a decision.** Observed and declared spans are now separate columns, so a
   weekly source taking a week off no longer satisfies `season_complete` and no "complete season" batch post can go
   out on the strength of the uploader pausing.
7. **New:** whether `@chelpbot` receives `can_invite_users`. The spec says Channel Help asks for it; the default
   withholds it. Settled by `bots.channel_help_rights` rather than by preference — the operator edits one config row.
8. **New:** hashtag policy. The old draft had `#S01E01`-style tags per caption; the approved samples carry none,
   so no tags are emitted. If hashtags are wanted they belong in the template text, not in code.

## 19. Repository status

Requirements are agreed. The **runtime is built and tested against a real PostgreSQL
cluster**: the schema, queue and checkpoint functions; source ingest with series/season
resolution; the thumbnail publish gate; manifest ordering and create-vs-edit; caption
rendering from the approved templates; destination channel planning; the owner's control
bot, including logging the spare account in from a chat; and the Render/Supabase deployment
surface with docs and a CI job.

Those eight job kinds — `archive_media`, `storage_upload`, `link_verify`, `link_health_check`,
`publish_post`, `edit_post`, `season_sticker` and `join_request_campaign` — are **implemented** in
`app/writers.py`, and each was written only after the protocol it depends on (the storage bot's menus, Channel
Help's message shape, the copy-message call, the sticker pack's document ids, the join-request read) had been
observed against a real bot rather than guessed at. What still raises `FeatureNotImplemented` and lands as
`blocked`, which `/status` reports, is a missing human fact inside an otherwise runnable job: the archive
channel nobody named, the sticker message nobody pointed at, the campaign text nobody approved.
`app/probe.py` is what obtains those observations from inside the deployment,
where the network exists; screenshots of the menus work too.

Nothing here is silently skipped: a feature that cannot run yet is a loud blocked job, not
a green log line.

See [`architecture.md`](architecture.md) for where each promise above is
enforced and which test proves it.
