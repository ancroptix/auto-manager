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

## 3. Destination channels

- One **private** destination channel per complete series.
- Default name: `{TITLE} Anime in Hindi`.
- Title is derived by cross-checking the source channel name against captions and filenames in that channel. Both
  signals must agree before a name is generated (e.g. source `berserk` + episodes of *Berserk* → `Berserk Anime in Hindi`).
- Destination channels are created automatically when both signals agree; no interactive confirmation is required.
- Description template:

  ```text
  Watch or download {TITLE} in Hindi. Available seasons, episodes, and qualities are organized below.
  Updates: @ycanime | @india_crunchyroll
  ```

- On creation, add `@chelpbot` (Channel Help) as administrator with the permissions its documentation requires:
  post messages, edit messages, delete messages, invite/add users. If confirmation is not received, remove and
  re-add once, then alert the owner.
- Create a short-lived, **one-use** invitation link and send it to the configured main Telegram account. Promote
  only the exact configured numeric `MAIN_ADMIN_USER_ID` with all supported channel admin rights, then revoke the
  invite immediately. An unexpected account using the link is never promoted and the owner is notified.
- The spare automation account remains the channel creator; administrator promotion is not ownership transfer.

## 4. Individual and complete-season posts

- If only some episodes of a season are available, publish one post per episode.
- When a complete season becomes available, additionally create one season batch/universal-link post covering the
  available episode range (for example, `Episode 1 - 24`).
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

## 10. Storage bot

- Bot: `@anime_hindifilesbot` (advanced: single, batch, universal links, and more commands).
- Exact command/menu protocol, link validation, batch-update and revocation behavior require authenticated
  integration testing after the operator completes a secure Telegram login. The operator will not screen-record the
  flow, so the agent discovers it live at runtime.
- Files go to the archive/storage workflow; destination channels receive text/link posts only.
- Missing-quality flow: upload only the new variant → add to the ordered manifest → rebuild or extend the batch /
  universal link → edit the existing destination post in place. Never resend the season sticker or create a second
  post for the same episode.
- If link revocation is supported, revoke superseded links after the new one is verified.

## 11. Destination publishing (Channel Help)

- Channel Help is used **only** to create and edit destination text posts with inline URL buttons. It does not handle
  source monitoring, files, archive backup, storage links, deduplication, or join requests.
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
  seen is never treated as the total.
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
- Default to owner-triggered campaigns (`/dmrequests <channel> <campaign>`) rather than auto-messaging every new
  requester, until the operator finalizes the policy.
- Deduplicate with a unique `(campaign_id, user_id)` record so one campaign cannot message the same user twice. A
  user may be contacted by different campaigns.
- Replies from contacted users are forwarded to the configured main account.
- Respect every Telegram FloodWait and stop on restriction. Privacy settings may block DMs, so failures are reported
  and never retried aggressively. No member scraping, no bulk unsolicited messaging, no account rotation or timing
  tuned to evade Telegram anti-spam enforcement — there is no safe promise that tens of thousands of private
  messages complete quickly.
- Log successful / failed / skipped / already-contacted outcomes.
- Message template: **TBD by operator.**

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
2. Join-request message template — operator deferred this.
3. Storage bot command protocol — requires authenticated integration testing.
4. ~~Template refinements beyond the §12 defaults~~ — the operator dictated the three caption formats and
   the button label on 2026-08-28; §12 and `0004_approved_captions.sql` carry them.
5. ~~whether `@YC_Anime` and `@ycanime` are the same channel~~ — **answered 2026-08-28:** the
   handle is `@YCAnime`, which casefold-matches `branding.primary_handles`, so the footers and the
   publish gate agree and the allowlist did not need widening. `@India_crunchyroll` differs from
   the stored `india_crunchyroll` in case only, which Telegram ignores; `branding.footer` was
   updated to the operator's casing in the same migration.
6. **New:** hashtag policy. The old draft had `#S01E01`-style tags per caption; the approved samples carry none,
   so no tags are emitted. If hashtags are wanted they belong in the template text, not in code.

## 19. Repository status

Requirements are agreed. The **runtime skeleton is now built and tested**:
Supabase schema and queue functions, the checkpoint/resume worker loop, the
health/status/kill-switch HTTP surface, config with fail-closed live-mode
validation, and Render deployment as code. 376 tests pass, including the
migrations executed against a real PostgreSQL cluster.

What is **not** built: all Telegram I/O — source scanning, thumbnail screening,
archive copies, the storage-bot and Channel Help adapters, sticker mapping, and
campaign sending. Each unimplemented job kind fails loudly into a `blocked`
state that `/status` reports, so no feature can pretend to have run.

See [`architecture.md`](architecture.md) for where each promise above is
enforced and which test proves it.
