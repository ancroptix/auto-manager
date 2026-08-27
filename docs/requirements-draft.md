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

## 12. Templates (current defaults, configurable)

Archive file caption:

```text
🎬 {title}
📺 Season {season} • Episode {episode}
🎙 Audio: {languages}
💾 Quality: {quality}

@ycanime | @india_crunchyroll

#{title_tag} #S01E01 #{quality_tag}
```

Individual episode destination post:

```text
🎬 {title}

📺 Season {season} • Episode {episode}
🎙 Available in Hindi
💾 Qualities: {quality_list}

Choose the button below to get this episode.

@ycanime | @india_crunchyroll
```

Button: `📥 Get Episode {episode} - {storage_link}`

Complete-season destination post:

```text
🎬 {title} — Season {season} Complete

📺 Episodes: {first_episode}–{last_episode}
🎙 Available in Hindi
💾 Qualities: {quality_summary}

Choose the button below to get the complete season.

@ycanime | @india_crunchyroll
```

Button: `📥 Get Complete Season {season} - {storage_link}`

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

## 16. Owner control interface

```text
/status            /health            /sources
/addsource         /removesource      /destinations
/createchannel     /archive           /pending
/dmrequests        /campaigns         /queue
/retry             /pause             /resume
/watermark_review  /approve_candidate /reject_candidate
/logs              /shutdown
```

- Restricted to the configured owner Telegram user ID allowlist.
- Destructive actions, mass messaging, and privilege changes require explicit confirmation.
- `/shutdown` acts as an emergency kill switch alongside Render's own stop.

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
- The MTProto session is an encrypted Render environment secret — never in GitHub, never in Supabase as plaintext.
  The operator enters phone number, login code, and 2FA password themselves; these are never shared with the agent.
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
4. Template refinements beyond the §12 defaults, if desired.

## 19. Repository status

Requirements and architecture planning only; implementation has not started. See `README.md` for the component list.
