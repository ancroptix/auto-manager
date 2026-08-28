"""Handle hygiene in captions, the thumbnail gate, and channel setup.

Everything in here is a rule the spec states in words ("replace disallowed
handles", "clean thumbnail is a hard gate", "never promote strangers"), so each
test is one of those sentences with the code on the other side.
"""

from __future__ import annotations

from app import thumbnails
from app.captions import (
    APPROVED_FOOTER,
    clean_handles,
    placeholder_keys,
    primary_footer,
    render_template,
    safe_filename,
)
from app.channels import (
    SETUP_STEPS,
    channel_help_rights,
    destination_name,
    may_promote,
    next_setup_step,
    parse_setup_reply,
    reply_is_join_request,
    series_agrees,
    setup_plan,
    sticker_status,
)


class TestCaptionHygiene:
    def test_foreign_handle_is_replaced_with_the_primary_pair(self) -> None:
        result = clean_handles("Episode 12 out now, from @some_leech_group")
        assert result.changed
        assert "@some_leech_group" not in result.text
        assert APPROVED_FOOTER in result.text
        assert result.removed == ("some_leech_group",)

    def test_bare_t_me_handle_link_is_treated_as_a_handle(self) -> None:
        result = clean_handles("more at t.me/other_channel_here")
        assert "t.me/other_channel_here" not in result.text
        assert result.changed

    def test_storage_links_survive_the_rewrite(self) -> None:
        # The whole reason URLs are masked first: t.me/<bot>/<id> contains a
        # username, and a "cleaned" caption with a dead download button is worse
        # than one carrying a stray handle.
        text = "Get it: https://t.me/anime_hindifilesbot/5512 and t.me/anime_hindifilesbot/99"
        assert clean_handles(text).text == text

    def test_invite_links_are_not_touched_either(self) -> None:
        text = "invite: https://t.me/+AbCdEf123 and https://t.me/joinchat/AAAAAE"
        assert clean_handles(text).text == text

    def test_primary_handles_are_left_alone(self) -> None:
        text = "ours: @ycanime and @india_crunchyroll, plus t.me/ycanime"
        result = clean_handles(text)
        assert not result.changed and result.text == text

    def test_several_foreign_handles_produce_one_footer(self) -> None:
        result = clean_handles("leak from @aaa_leech, @bbb_leech, @ccc_leech")
        assert result.text.count(APPROVED_FOOTER) == 1
        assert len(result.removed) == 3

    def test_no_stray_whitespace_left_behind(self) -> None:
        result = clean_handles("posted by @x_leech  on  telegram\n\n\n\nnext line")
        assert "  " not in result.text and "\n\n\n" not in result.text

    def test_configured_allowlist_is_honoured(self) -> None:
        text = "@second_brand_channel"
        assert not clean_handles(text, allowed=("second_brand_channel",)).changed

    def test_empty_input_is_safe(self) -> None:
        assert clean_handles(None).text == "" and clean_handles("").changed is False


class TestTemplates:
    def test_known_placeholders_are_filled_and_unknowns_reported(self) -> None:
        text, missing = render_template(
            "🎬 {title}\n📺 Season {season} • Episode {episode}\n💾 {quality_list}",
            {"title": "Bleach", "season": 1, "episode": 9, "quality_list": "480p, 1080p"},
        )
        assert missing == ()
        assert text == "🎬 Bleach\n📺 Season 1 • Episode 9\n💾 480p, 1080p"

    def test_a_typo_in_a_template_degrades_instead_of_failing_the_publish(self) -> None:
        text, missing = render_template("Ep {episde} of {title}", {"title": "Bleach"})
        assert text == "Ep {episde} of Bleach"
        assert missing == ("episde",)  # reported, so the job can log it

    def test_missing_values_are_reported_not_silently_blank(self) -> None:
        text, missing = render_template("{title} — {quality_list}", {"title": "Bleach"})
        assert missing == ("quality_list",) and "{quality_list}" in text

    def test_literal_braces_pass_through(self) -> None:
        text, missing = render_template("{{{title}}}", {"title": "Bleach"})
        assert text == "{Bleach}" and missing == ()

    def test_placeholder_keys_are_discoverable_before_use(self) -> None:
        assert placeholder_keys("🎬 {title} {quality_list}") == ("title", "quality_list")

    def test_footer_matches_the_branding_pair(self) -> None:
        # the operator's own spelling, not a lowercased rebuild of the allow-list
        assert primary_footer() == APPROVED_FOOTER
        assert primary_footer(("@YCAnime", "@India_crunchyroll")) == "@YCAnime | @India_crunchyroll"
        assert primary_footer(["ycanime"]) == "@ycanime"

    def test_filename_separator_handles_the_footer_pipe(self) -> None:
        # The footer contains '|', which is illegal in a filename: hence the
        # configurable separator rather than a hardcoded one.
        assert "|" not in safe_filename("Bleach | S01E09 [Hindi Dub].mkv")
        assert safe_filename("a/b:c?.mkv", separator="-").startswith("a-b-c")
        assert safe_filename(None) == "untitled"

    def test_long_names_keep_the_extension(self) -> None:
        name = safe_filename("x" * 300 + ".mkv", limit=60)
        assert name.endswith(".mkv") and len(name) <= 60


class TestThumbnailGate:
    def test_either_primary_handle_passes(self) -> None:
        for handles in (("ycanime",), ("india_crunchyroll",), ("ycanime", "india_crunchyroll"), ()):
            verdict = thumbnails.screen(
                image_present=True, handles=handles, strict=False, evidence="image_analysed"
            )
            assert verdict.publishable and verdict.disposition == thumbnails.Disposition.ACCEPTED

    def test_any_other_handle_fails_and_asks_for_review(self) -> None:
        verdict = thumbnails.screen(
            image_present=True, handles=("ycanime", "random_leech"), evidence="image_analysed"
        )
        assert verdict.status == thumbnails.ThumbnailStatus.WATERMARKED
        assert verdict.foreign_handles == ("random_leech",)
        assert verdict.needs_review and not verdict.publishable

    def test_missing_image_is_never_clean(self) -> None:
        verdict = thumbnails.screen(image_present=False, handles=("ycanime",))
        assert verdict.status == thumbnails.ThumbnailStatus.AMBIGUOUS and verdict.needs_review

    def test_strict_mode_parks_caption_only_evidence(self) -> None:
        # Until the media layer can open the image, a candidate with nothing wrong
        # in its text is *not* passed. That is the difference between a publish
        # gate and a rubber stamp.
        verdict = thumbnails.screen(image_present=True, handles=("ycanime",), strict=True, evidence="caption_only")
        assert verdict.status == thumbnails.ThumbnailStatus.REVIEW_REQUIRED
        assert not verdict.publishable and verdict.needs_review

    def test_ocr_text_counts_as_evidence(self) -> None:
        verdict = thumbnails.screen(
            image_present=True,
            handles=(),
            ocr_text="watermark: t.me/copy_of_channel",
            evidence="image_analysed",
        )
        assert verdict.status == thumbnails.ThumbnailStatus.WATERMARKED
        assert "copy_of_channel" in verdict.foreign_handles

    def test_readable_text_without_our_marks_is_ambiguous(self) -> None:
        verdict = thumbnails.screen(
            image_present=True, ocr_text="anime 480p file", strict=True, evidence="image_analysed"
        )
        assert verdict.status == thumbnails.ThumbnailStatus.AMBIGUOUS and verdict.needs_review

    def test_cleaner_copy_wins_over_higher_quality(self) -> None:
        chosen = thumbnails.select_clean_candidate(
            [
                {"id": 1, "thumbnail_status": "watermarked", "quality": "1080p", "message_id": 2, "disposition": "rejected"},
                {"id": 2, "thumbnail_status": "clean", "quality": "480p", "message_id": 3, "disposition": "accepted"},
            ]
        )
        assert chosen and chosen["id"] == 2

    def test_among_clean_copies_the_higher_quality_wins(self) -> None:
        chosen = thumbnails.select_clean_candidate(
            [
                {"id": 1, "thumbnail_status": "clean", "quality": "480p", "message_id": 1, "disposition": "accepted"},
                {"id": 2, "thumbnail_status": "clean", "quality": "1080p", "message_id": 9, "disposition": "accepted"},
            ],
            quality_order=("360p", "480p", "720p", "1080p"),
        )
        assert chosen and chosen["id"] == 2

    def test_trusted_channel_beats_quality(self) -> None:
        chosen = thumbnails.select_clean_candidate(
            [
                {"id": 1, "source_channel": "@ycanime", "thumbnail_status": "clean", "quality": "480p", "message_id": 5, "disposition": "accepted"},
                {"id": 2, "source_channel": "@random_leech", "thumbnail_status": "clean", "quality": "1080p", "message_id": 1, "disposition": "accepted"},
            ],
            trusted_channels=("@ycanime",),
        )
        assert chosen and chosen["id"] == 1

    def test_owner_approved_status_is_publishable(self) -> None:
        assert thumbnails.is_publishable("owner_approved") and not thumbnails.is_publishable("review_required")

    def test_unrecognised_policy_falls_back_to_asking_the_owner(self) -> None:
        assert "owner review" in thumbnails.no_clean_action("typo_in_dashboard")
        assert "skipped" in thumbnails.no_clean_action("skip_quality")


class TestChannelSetup:
    def test_name_is_generated_from_the_template(self) -> None:
        assert destination_name("bleach") == "Bleach Anime in Hindi"
        assert destination_name("jujutsu kaisen", template="{TITLE} Anime in Hindi ✅") == "Jujutsu Kaisen Anime in Hindi ✅"

    def test_agreement_rules(self) -> None:
        assert series_agrees("Bleach", "Bleach TYBW")
        assert series_agrees("Bleach TYBW", "Bleach Thousand Year Blood War")  # abbreviation form
        assert not series_agrees("Naruto", "Bleach")
        assert not series_agrees(None, "Bleach")
        assert not series_agrees("Bleach", None)

    def test_channel_help_gets_the_smaller_permission_set_first(self) -> None:
        create = channel_help_rights(stage="create")
        promote = channel_help_rights(stage="publish")
        assert create["can_post_messages"] and not create["can_invite_users"]
        assert promote["can_pin_messages"]
        for rights in (create, promote):
            assert not rights["can_add_admins"] and not rights["can_ban_users"]

    def test_only_the_publisher_may_be_promoted(self) -> None:
        assert may_promote("@chelpbot")
        assert not may_promote("@some_member")
        assert not may_promote(None)
        assert not may_promote("chelpbot", allow=())

    def test_setup_sequence_is_ordered_and_resumable(self) -> None:
        names = [step.name for step in SETUP_STEPS]
        assert names.index("add_channel_help") < names.index("promote_channel_help")
        assert names.index("invite_owner") < names.index("revoke_invite")
        assert names.index("revoke_invite") < names.index("season_sticker") < names.index("ready")
        assert next_setup_step(()).name == "create_channel"
        assert next_setup_step(names) is None
        assert next_setup_step({"create_channel": True, "add_channel_help": False}).name == "add_channel_help"

    def test_irreversible_steps_are_marked(self) -> None:
        assert {step.name for step in SETUP_STEPS if not step.reversible} == {"add_channel_help", "promote_channel_help"}
        assert all(step.why for step in SETUP_STEPS)
        assert len(setup_plan(["create_channel"])) == len(SETUP_STEPS)
        assert setup_plan(["create_channel"])[0]["done"] is True

    def test_sticker_goes_before_the_first_episode_of_a_season(self) -> None:
        assert sticker_status(sticker_posted=False, season_episodes=[1, 2, 3], published_episodes=[]).due
        late = sticker_status(sticker_posted=False, season_episodes=[1, 2], published_episodes=[1])
        assert not late.due and "next season" in late.reason
        assert not sticker_status(sticker_posted=True, season_episodes=[1]).due

    def test_setup_replies_are_classified_but_never_acted_on(self) -> None:
        assert parse_setup_reply("Join") == "join"
        assert parse_setup_reply("ji haan") == "join"
        assert parse_setup_reply("no thanks") == "stop"
        assert parse_setup_reply("नहीं") == "stop"
        assert parse_setup_reply("what is this channel about?") == "other"
        assert parse_setup_reply(None) == "other"

    def test_a_join_request_is_never_mistaken_for_a_setup_answer(self) -> None:
        assert reply_is_join_request("Rohan wants to join your channel")
        assert not reply_is_join_request("join me please")

    def test_join_request_text_cannot_be_read_as_consent(self) -> None:
        # Both checks run over the same inbox, and a join request literally
        # contains the word "join": classifying that text as a setup "yes" would
        # mark a stranger as opted in, from a message never addressed to them.
        assert reply_is_join_request("User wants to join the channel")
        assert parse_setup_reply("User wants to join the channel") == "other"
