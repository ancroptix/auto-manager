-- ============================================================================
-- 0004_approved_captions.sql — the caption formats the operator dictated.
--
-- app.config used to carry placeholder captions marked "Temporary default.". This
-- migration replaces them with the approved text, character for character, with
-- three deliberate choices recorded here rather than hidden in a diff:
--
--   1. The box lines are single-newline separated. The samples arrived double
--      spaced, and a `╭ ┣ ┣ ╰` frame only reads as a frame when the strokes are
--      adjacent.
--   2. `{title_full}` is one stored value ("Title: Subtitle") used by both the
--      archive caption and the destination post, so the private archive can never
--      disagree with the public channel about how a series is spelled. The two
--      samples differed (": The earthbound mole" against ": the earthbound mole");
--      the capitalisation stored on the series is what gets published.
--   3. `{season}` is bare in the destination box and zero-padded in the archive
--      line, because that is what each sample showed. Padding one and tidying the
--      other would change published text on a guess.
--
-- `app.series.subtitle` (added below) holds the alternate title. The source scanner
-- fills it when that handler lands, and nothing may depend on it being present: an
-- absent subtitle drops the colon and the second half instead of inventing one.
--
-- Each statement replaces its row only while that row still holds the exact
-- placeholder 0002 shipped — the previous value is named in the WHERE clause — so
-- re-applying ops/apply-all.sql can never overwrite a caption you have edited.
--
-- The strings are asserted equal to app.captions.APPROVED_TEMPLATES by
-- tests/test_caption_templates.py; regenerate this file with
-- `python ops/build_caption_migration.py && python ops/build_apply_all.py` after
-- editing a template, and CI's --check fails if either step was skipped.
-- ============================================================================

alter table app.series add column if not exists subtitle text;

comment on column app.series.subtitle is
  'Alternate/English title as it appears in the source, e.g. "The earthbound mole". '
  'Never guessed, never translated: the caption prints it or omits it.';

-- branding.footer
insert into app.config (key, value, description) values
  ('branding.footer',
   '"@YCAnime | @India_crunchyroll"',
   'Display casing of the signature that replaces a foreign handle in an edited caption. Matching stays casefolded against branding.primary_handles; the underscore in @YC_Anime was a typo, corrected by the operator 2026-08-28.')
on conflict (key) do update set
  value = excluded.value,
  description = excluded.description,
  updated_at = now()
 where app.config.value = '"@ycanime | @india_crunchyroll"'::jsonb;  -- only replaces the placeholder 0002 shipped

-- templates.archive_caption
insert into app.config (key, value, description) values
  ('templates.archive_caption',
   '"‣ {title_full} (S - {season})\n\n╭────────────────────\n┣Quality: {quality}\n┣Episode: {episode}\n┣Audio: {audio} #O𝖿𝖿𝗂𝖼𝗂𝖺𝗅\n╰────────────────────\n\n‣ Powered By: @india_crunchyroll\n@YCAnime"',
   'Approved 2026-08-28 from the operator''s sample: title line, light box with Quality/Episode/Audio, then the two handles. Editable here.')
on conflict (key) do update set
  value = excluded.value,
  description = excluded.description,
  updated_at = now()
 where app.config.value = '"🎬 {title}\n📺 Season {season} • Episode {episode}\n🎙 Audio: {languages}\n💾 Quality: {quality}\n\n@ycanime | @india_crunchyroll\n\n#{title_tag} #S01E01 #{quality_tag}"'::jsonb;  -- only replaces the placeholder 0002 shipped

-- templates.episode_post
insert into app.config (key, value, description) values
  ('templates.episode_post',
   '"✦ {title_full} ✦\n\n╔━━━━━━━━━━━━━━━━━━━━━╗\n⌲ 𝗦𝗲𝗮𝘀𝗼𝗻: {season}\n❍ 𝗘𝗽𝗶𝘀𝗼𝗱𝗲: {episode}\n〄 𝗔𝘂𝗱𝗶𝗼: {audio}\n◎ 𝗧𝗼𝘁𝗮𝗹 𝗘𝗽𝗶𝘀𝗼𝗱𝗲𝘀: {total_episodes}\n♡ 𝗣𝗼𝘄𝗲𝗿𝗲𝗱 𝗯𝘆: @YCAnime , @India_crunchyroll\n╚━━━━━━━━━━━━━━━━━━━━━╝"',
   'Approved 2026-08-28 from the operator''s sample: heavy box, season bare, episode zero-padded, total episodes, footer handles. Editable here.')
on conflict (key) do update set
  value = excluded.value,
  description = excluded.description,
  updated_at = now()
 where app.config.value = '"🎬 {title}\n\n📺 Season {season} • Episode {episode}\n🎙 Available in Hindi\n💾 Qualities: {quality_list}\n\nChoose the button below to get this episode.\n\n@ycanime | @india_crunchyroll"'::jsonb;  -- only replaces the placeholder 0002 shipped

-- templates.season_post
insert into app.config (key, value, description) values
  ('templates.season_post',
   '"✦ {title_full} ✦\n\n╔━━━━━━━━━━━━━━━━━━━━━╗\n⌲ 𝗦𝗲𝗮𝘀𝗼𝗻: {season}\n❍ 𝗘𝗽𝗶𝘀𝗼𝗱𝗲: {episode_range}\n〄 𝗔𝘂𝗱𝗶𝗼: {audio}\n◎ 𝗧𝗼𝘁𝗮𝗹 𝗘𝗽𝗶𝘀𝗼𝗱𝗲𝘀: {total_episodes}\n♡ 𝗣𝗼𝘄𝗲𝗿𝗲𝗱 𝗯𝘆: @YCAnime , @India_crunchyroll\n╚━━━━━━━━━━━━━━━━━━━━━╝"',
   'Approved 2026-08-28: the same box as the episode post with ''{episode_range}'' instead of ''{episode}''. Editable here.')
on conflict (key) do update set
  value = excluded.value,
  description = excluded.description,
  updated_at = now()
 where app.config.value = '"🎬 {title} — Season {season} Complete\n\n📺 Episodes: {first_episode}–{last_episode}\n🎙 Available in Hindi\n💾 Qualities: {quality_summary}\n\nChoose the button below to get the complete season.\n\n@ycanime | @india_crunchyroll"'::jsonb;  -- only replaces the placeholder 0002 shipped

-- templates.episode_button
insert into app.config (key, value, description) values
  ('templates.episode_button',
   '"❐ 𝗪𝗮𝘁𝗰𝗵/𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱 ❐ - {storage_link}"',
   'Channel Help button syntax is ''text - url''. One label, exactly as approved, used when a post has a single link.')
on conflict (key) do update set
  value = excluded.value,
  description = excluded.description,
  updated_at = now()
 where app.config.value = '"📥 Get Episode {episode} - {storage_link}"'::jsonb;  -- only replaces the placeholder 0002 shipped

-- templates.episode_button_multi
insert into app.config (key, value, description) values
  ('templates.episode_button_multi',
   '"❐ 𝗪𝗮𝘁𝗰𝗵/𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱 {quality} ❐ - {storage_link}"',
   'Used when a post offers more than one quality: the quality is named, because four identical buttons on a post that has 480p through 2160p stopped describing the file.')
on conflict (key) do nothing;  -- new key: a value you set yourself always wins

-- templates.season_button
insert into app.config (key, value, description) values
  ('templates.season_button',
   '"❐ 𝗪𝗮𝘁𝗰𝗵/𝗗𝗼𝘄𝗻𝗹𝗼𝗮𝗱 ❐ - {storage_link}"',
   'The batch post''s button: the universal link, same label as an episode link.')
on conflict (key) do update set
  value = excluded.value,
  description = excluded.description,
  updated_at = now()
 where app.config.value = '"📥 Get Complete Season {season} - {storage_link}"'::jsonb;  -- only replaces the placeholder 0002 shipped

-- caption.button_rows
insert into app.config (key, value, description) values
  ('caption.button_rows',
   '"one_per_line"',
   'one_per_line | pair. ''pair'' joins buttons with && so two links share a row; one_per_line gives every quality its own row.')
on conflict (key) do nothing;  -- new key: a value you set yourself always wins

-- caption.total_episodes_unknown
insert into app.config (key, value, description) values
  ('caption.total_episodes_unknown',
   '"TBA"',
   'Printed instead of a number when the season''s length was never stated. Never inferred from the highest episode seen so far: that would promise a completion nobody observed.')
on conflict (key) do nothing;  -- new key: a value you set yourself always wins
