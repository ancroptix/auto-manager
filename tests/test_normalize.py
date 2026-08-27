"""Filename/caption parsing and the Hindi-scope decision.

These tests are written as real strings from source channels, because that is
the input the parser must survive: dotted names, bracket tags, "Dual Audio" with
no language named, and ranges written five different ways.
"""

from __future__ import annotations

import pytest

from app.normalize import (
    AudioKind,
    Disposition,
    detect_episode_numbers,
    detect_handles,
    detect_languages,
    detect_quality,
    extract_series_title,
    language_display,
    parse_episode,
)


class TestQuality:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Bleach S01E01 1080p Hindi.mkv", "1080p"),
            ("Bleach.S01E01.720p.Hindi.mkv", "720p"),
            ("Bleach S01E01 [480P] Hindi.mkv", "480p"),
            ("Bleach S01E01 4K Hindi.mkv", "2160p"),
            ("Bleach S01E01 2160p Hindi.mkv", "2160p"),
            ("Bleach S01E01 Hindi.mkv", None),
            # A resolution-like number that is not one must not become a quality.
            ("Bleach S01E01 1080p x265 10bit.mkv", "1080p"),
            ("Bleach 1080 Hindi.mkv", None),
        ],
    )
    def test_quality_labels(self, name: str, expected: str | None) -> None:
        assert detect_quality(name) == expected

    def test_rank_is_config_order_not_numeric(self) -> None:
        parsed = parse_episode(file_name="Bleach S01E01 1080p Hindi.mkv", quality_order=("480p", "720p", "1080p"))
        assert parsed.quality_rank_value == 3


class TestLanguages:
    def test_hindi_plus_english_is_dual(self) -> None:
        languages, dub, sub = detect_languages("Hindi English Dual Audio Subtitles")
        assert languages >= {"hindi", "english"} and dub and sub

    def test_subbed_only_is_recognised_as_such(self) -> None:
        parsed = parse_episode(file_name="Dandadan S01E04 English Subtitled 1080p.mkv")
        assert parsed.audio_kind == AudioKind.SUBBED_ONLY
        assert parsed.disposition == Disposition.REJECTED
        assert "out of scope" in parsed.reason

    def test_a_tamil_dub_without_hindi_is_out_of_scope(self) -> None:
        parsed = parse_episode(file_name="Naruto S01E01 Tamil Dubbed 720p.mkv")
        assert parsed.audio_kind == AudioKind.NON_HINDI_DUB
        assert not parsed.accepted

    def test_multi_audio_with_hindi_is_in_scope(self) -> None:
        parsed = parse_episode(file_name="Naruto S01E01 Hindi Tamil Telugu 1080p.mkv")
        assert parsed.audio_kind == AudioKind.MULTI_AUDIO
        assert parsed.accepted

    def test_dubbed_counts_as_hindi_audio(self) -> None:
        parsed = parse_episode(file_name="Bleach S03E12 720p Hindi Dubbed.mkv")
        assert parsed.accepted and parsed.audio_kind in (AudioKind.HINDI, AudioKind.DUAL_AUDIO)

    def test_subbed_only_can_be_allowed_by_policy(self) -> None:
        parsed = parse_episode(
            file_name="Dandadan S01E04 English Subtitled 1080p.mkv",
            require_hindi_audio=False,
            include_subbed_only=True,
        )
        assert parsed.accepted and parsed.disposition == Disposition.ACCEPTED

    def test_display_text_is_caption_ready(self) -> None:
        assert language_display(("english", "hindi"), None) == "Hindi + English"
        assert language_display((), AudioKind.HINDI) == "Hindi"


class TestEpisodes:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Bleach S01E09 720p.mkv", (9,)),
            ("Bleach - Episode 108.mkv", (108,)),
            ("Bleach.Ep.108.mkv", (108,)),
            ("Bleach E108.mkv", (108,)),
            ("One Piece Episode 1050-1055 720p.mkv", tuple(range(1050, 1056))),
            ("Naruto Shippuden 04x12.mkv", (12,)),
            ("Frieren - 16 [Dual Audio].mkv", (16,)),
            ("Bleach [12].mkv", (12,)),
            ("Bleach S02 [01-12].zip", (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12)),
            ("Detective Conan Episode 1101.mkv", (1101,)),
        ],
    )
    def test_episode_markers(self, name: str, expected: tuple[int, ...]) -> None:
        assert detect_episode_numbers(name)[0] == expected

    def test_sxxexx_also_carries_the_season(self) -> None:
        parsed = parse_episode(file_name="Attack.on.Titan.S04E16.1080p.Hindi.Dub.mkv")
        assert (parsed.season, parsed.episode) == (4, 16)

    def test_a_part_number_is_a_variant_not_a_season(self) -> None:
        parsed = parse_episode(file_name="Attack on Titan The Final Season Part 4 S04E16 Hindi.mkv")
        assert parsed.season == 4 and parsed.release_variant == "part-4"

    def test_absurd_range_is_treated_as_a_parse_error(self) -> None:
        # "ep 1-99999" must not queue ninety-nine thousand phantom episodes.
        numbers, origin, _ = detect_episode_numbers("Bleach Episode 1-99999")
        assert numbers == (1,) and origin == "episode word"

    def test_no_episode_number_is_review_not_reject(self) -> None:
        parsed = parse_episode(file_name="Bleach Opening Theme Hindi.mkv", source_series="Bleach")
        assert parsed.disposition == Disposition.PENDING
        assert "needs review" in parsed.reason
        assert parsed.accepted is False

    def test_range_is_flagged_for_the_caller(self) -> None:
        parsed = parse_episode(file_name="Bleach S01E01-12 1080p Hindi.mkv", source_series="Bleach")
        assert parsed.is_multi and "episode_range:12" in parsed.flags
        assert parsed.episode_numbers() == tuple(range(1, 13))


class TestFileKind:
    @pytest.mark.parametrize(
        ("name", "kind"),
        [
            ("Bleach S01 Batch 720p.zip", "batch"),
            ("Bleach Complete Series.zip", "batch"),
            ("Bleach S01 All Episodes Pack.rar", "batch"),
            ("Bleach.the.Movie.1080p.Hindi.mkv", "movie"),
            ("Bleach S01E01 1080p.mkv", "episode"),
            ("bleach", "unknown"),
        ],
    )
    def test_kinds(self, name: str, kind: str) -> None:
        assert parse_episode(file_name=name).file_kind == kind

    def test_archive_without_a_number_is_a_batch_not_a_stuck_episode(self) -> None:
        parsed = parse_episode(file_name="Bleach S01 Hindi.zip", source_series="Bleach")
        assert parsed.file_kind == "batch" and parsed.accepted


class TestTitle:
    def test_tags_are_not_the_title(self) -> None:
        assert extract_series_title("[CR] Chainsaw Man S01 E02 [1080p].mkv") == "Chainsaw Man"

    def test_underscores_and_dots_become_spaces(self) -> None:
        assert extract_series_title("Jujutsu.Kaisen.S02.Ep12.720p.Hindi.mkv") == "Jujutsu Kaisen"

    def test_trailing_episode_number_is_not_part_of_the_title(self) -> None:
        parsed = parse_episode(file_name="Kaiju No. 8 - 08 (1080p) [Hindi Dubbed].mkv")
        assert parsed.series == "Kaiju No 8"

    def test_channel_series_wins_over_the_filename(self) -> None:
        parsed = parse_episode(file_name="Random Rename Ep05 Hindi.mkv", source_series="Bleach TYBW")
        assert parsed.series == "Bleach TYBW"

    def test_disagreement_is_recorded_not_hidden(self) -> None:
        parsed = parse_episode(file_name="Naruto S01E01 Hindi.mkv", source_series="Bleach")
        assert parsed.series == "Bleach"
        assert any(flag.startswith("title_disagrees_with_channel") for flag in parsed.flags)

    def test_an_unidentified_series_parks_the_candidate(self) -> None:
        parsed = parse_episode(file_name="1080p Hindi Episode 4.mkv")
        assert parsed.disposition == Disposition.PENDING
        assert "series" in parsed.reason


class TestHandlesAndKeys:
    def test_mention_and_link_forms_are_both_found(self) -> None:
        assert detect_handles("from @some_leech on t.me/other_chan") == ("some_leech", "other_chan")

    def test_storage_deep_links_are_not_handles(self) -> None:
        # t.me/<bot>/<id> is a message link; treating it as a handle would let a
        # download link look like a watermark and reject our own posts.
        assert detect_handles("get it: https://t.me/anime_hindifilesbot/9911") == ()

    def test_dedup_key_is_stable_across_separator_styles(self) -> None:
        a = parse_episode(file_name="Jujutsu.Kaisen.S02.Ep12.Hindi.mkv")
        b = parse_episode(file_name="Jujutsu Kaisen S02 Ep12 [Hindi].mkv")
        assert a.canonical_key() == b.canonical_key()

    def test_different_quality_is_the_same_episode_key(self) -> None:
        # Quality lives on the variant, so a late 1080p edits the same post
        # instead of creating a second episode.
        low = parse_episode(file_name="Bleach S01E01 480p Hindi.mkv")
        high = parse_episode(file_name="Bleach S01E01 1080p Hindi.mkv")
        assert low.canonical_key() == high.canonical_key()

    def test_different_variant_is_a_different_key(self) -> None:
        cut = parse_episode(file_name="Bleach S01E01 Uncut Hindi.mkv")
        normal = parse_episode(file_name="Bleach S01E01 Hindi.mkv")
        assert cut.canonical_key() != normal.canonical_key()

    def test_subbed_release_never_collides_with_a_hindi_one(self) -> None:
        hindi = parse_episode(file_name="Bleach S01E01 Hindi.mkv")
        subbed = parse_episode(file_name="Bleach S01E01 English Subtitles.mkv")
        assert hindi.canonical_key() != subbed.canonical_key()


class TestPayload:
    def test_payload_is_json_round_trippable(self) -> None:
        import json

        parsed = parse_episode(file_name="Bleach S01E01 1080p Hindi.mkv", file_size_bytes=42)
        restored = json.loads(json.dumps(parsed.to_payload()))
        assert restored["episode"] == 1 and restored["quality"] == "1080p"
        assert restored["disposition"] == Disposition.ACCEPTED

    def test_caption_text_counts_when_the_filename_is_generic(self) -> None:
        parsed = parse_episode(
            file_name="video.mp4",
            raw_caption="Bleach TYBW S03E14\n🎙 Hindi + English\n💾 1080p",
            source_series="Bleach TYBW",
        )
        assert parsed.accepted
        assert parsed.episode == 14 and parsed.quality == "1080p" and parsed.season == 3

    def test_unknown_audio_never_publishes_and_never_drops(self) -> None:
        parsed = parse_episode(file_name="Vagabond OVA [Multi Audio].mkv")
        assert parsed.audio_kind == AudioKind.UNKNOWN
        assert parsed.accepted is False
        assert parsed.disposition == Disposition.PENDING  # a human decides, not the parser
