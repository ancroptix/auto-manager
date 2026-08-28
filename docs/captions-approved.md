# Approved captions and buttons

The operator's own samples, as implemented. What is on this page is what a post
will say — no code reading required to check it. If a line below is wrong, say
which line: the fix is one row in `app.config`, not a deploy.

Rendered with: series *Dekin no mogura* / subtitle *The earthbound mole*, season 1,
episode 11 at 480p for the archive, episode 01 of 12 for the posts.

## Archive caption — `templates.archive_caption`

The private master archive channel. This is the text on the stored file, so it is
also what a later re-scan reads back: `Quality:`, `Episode:` and `Audio:` are the
lines the parser recognises, which is why they are kept on their own line each.

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

## Per-episode destination post — `templates.episode_post`

Posted by Channel Help into `{TITLE} Anime in Hindi`.

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

```text
❐ 𝗪𝗮𝘁𝗰𝗵/𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱 ❐ - https://t.me/anime_hindifilesbot/111
```

## Complete-season batch post — `templates.season_post`

The same box with `{episode}` swapped for the range `{episode_range}`, and the
storage bot's **universal link** behind the button. Permanent: the individual
episode posts are never deleted when this one appears.

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

## Placeholders

| Name | Value |
| --- | --- |
| `{title_full}` | the series title, plus `: <subtitle>` when an alternate title is stored |
| `{season}` | bare in the destination box (`1`), zero-padded in the archive line (`01`) — as in the two samples |
| `{episode}` | zero-padded, `01` |
| `{episode_range}` | `01 - 12`, or `01 - TBA` when the season's length was never stated |
| `{total_episodes}` | `12`, or `TBA` — never inferred from the highest episode seen |
| `{audio}` | `Hindi`, or `Hindi + English` for dual/multi audio; `Unknown` if the file said nothing |
| `{quality}` | the one quality this post/button is about |
| `{storage_link}` | the link the storage bot returned for exactly this file |

Any placeholder you put in a template that the renderer does not know is reported by
name instead of being published literally — a caption is never posted containing
`{quality}` because the value was misspelled.

## Buttons

`caption.button_rows` decides the layout:

| One link | More than one link (480p → 2160p, manifest order) |
| --- | --- |
| one row | `one_per_line`: one row each (current default) |
| label as approved, quality unnamed | quality named, because four identical buttons stopped describing the file |

```text
❐ 𝗪𝗮𝘁𝗰𝗵/𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱 480p ❐ - https://t.me/anime_hindifilesbot/1
❐ 𝗪𝗮𝘁𝗰𝗵/𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱 720p ❐ - https://t.me/anime_hindifilesbot/2
❐ 𝗪𝗮𝘁𝗰𝗵/𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱 1080p ❐ - https://t.me/anime_hindifilesbot/3
```

Set `caption.button_rows` to `pair` to join buttons two-to-a-row with `&&` instead.

## One thing to confirm, and one thing already corrected

1. **Handle spelling — settled.** The captions as first sent said `@YC_Anime`; you
   confirmed the same day that the handle is **`@YCAnime`**. That matters more than
   appearance: `branding.primary_handles` holds `ycanime` and `india_crunchyroll`, the
   gate compares case-insensitively, so `@YCAnime` is recognised as ours while
   `@YC_Anime` would have read as a *third*, unapproved channel — the very test a
   leech's watermark has to fail. The allow-list was therefore not widened. The
   display footer (`branding.footer`, now `@YCAnime | @India_crunchyroll`) and both
   template footers were corrected in the same migration; `@India_crunchyroll` was
   always fine, differing from the stored `india_crunchyroll` only in case.
2. **The subtitle's case.** The archive sample said `: The earthbound mole`, the post
   sample said `: the earthbound mole`. One stored value is printed in both places
   now, so they cannot disagree — the version published is whatever the source
   caption had. Say the word if you want posts always lower-case after the colon and
   the archive left as-is.
