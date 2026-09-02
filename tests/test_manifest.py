"""Manifest order, the create/edit decision, and season coverage.

Two behaviours the operator judges by eye live here: links listed in the
configured quality order rather than the order files finished, and a late
quality editing the existing post rather than appearing as a second message.
"""

from __future__ import annotations

from app.manifest import (
    PublishAction,
    decide_publish,
    manifest_table,
    ordered_variants,
    progress_line,
    quality_display_list,
    season_coverage,
    should_post_season_batch,
    sort_episode_numbers,
)

ORDER = ("360p", "480p", "720p", "1080p", "2160p")


def variant(quality: str, *, status: str = "linked", thumb: str = "clean", release: str | None = None, link: str | None = None) -> dict:
    return {
        "quality": quality,
        "status": status,
        "thumbnail_status": thumb,
        "release_variant": release,
        "link": link or f"https://t.me/bot/{quality}",
    }


class TestOrdering:
    def test_display_order_is_not_arrival_order(self) -> None:
        arrived = [variant("1080p"), variant("360p"), variant("720p"), variant("480p")]
        assert [v["quality"] for v in ordered_variants(arrived, ORDER)] == ["360p", "480p", "720p", "1080p"]

    def test_caption_list_follows_the_same_order(self) -> None:
        assert quality_display_list([variant("2160p"), variant("480p"), variant("720p")], ORDER) == ["480p", "720p", "2160p"]

    def test_unknown_label_still_applies_deterministically(self) -> None:
        rows = ordered_variants([variant("8k"), variant("1080p"), variant("480p")], ORDER)
        assert [r["quality"] for r in rows] == ["480p", "1080p", "8k"]
        again = ordered_variants(list(reversed([variant("8k"), variant("1080p"), variant("480p")])), ORDER)
        assert [r["quality"] for r in again] == [r["quality"] for r in rows]

    def test_episode_numbers_sort_numerically(self) -> None:
        assert sort_episode_numbers([9, 10, 1, 2, 1]) == [1, 2, 9, 10]
        assert sort_episode_numbers(["4", 3, None]) == [3, 4]  # type: ignore[list-item]

    def test_manifest_table_marks_pending_links_rather_than_hiding_them(self) -> None:
        pending = {**variant("1080p"), "link": None}
        rows = manifest_table([pending, variant("480p")], order=ORDER)
        assert [r["quality"] for r in rows] == ["480p", "1080p"]
        assert rows[1]["ready"] is False and rows[1]["position"] == 2


class TestPublishDecision:
    def test_first_publish_creates(self) -> None:
        decision = decide_publish(available=[variant("480p"), variant("1080p")], post_exists=False, quality_order=ORDER)
        assert decision.action == PublishAction.CREATE
        assert decision.qualities == ("480p", "1080p")
        assert decision.should_send

    def test_late_quality_edits_in_place(self) -> None:
        decision = decide_publish(
            available=[variant("480p"), variant("1080p")],
            published=[variant("480p")],
            post_exists=True,
            quality_order=ORDER,
        )
        assert decision.action == PublishAction.EDIT
        assert decision.added_qualities == ("1080p",)

    def test_unchanged_availability_is_a_noop(self) -> None:
        decision = decide_publish(
            available=[variant("1080p")], published=[variant("1080p")], post_exists=True, quality_order=ORDER
        )
        assert decision.action == PublishAction.NOOP
        assert not decision.should_send
        assert "duplicate" in decision.reason or "editing it again" in decision.reason

    def test_case_differences_do_not_look_like_a_new_quality(self) -> None:
        decision = decide_publish(
            available=[variant("1080P")], published=[variant("1080p")], post_exists=True, quality_order=ORDER
        )
        assert decision.action == PublishAction.NOOP

    def test_watermarked_variants_are_never_publishable(self) -> None:
        decision = decide_publish(
            available=[variant("1080p", thumb="watermarked")], post_exists=False, quality_order=ORDER
        )
        assert decision.action == PublishAction.NOOP
        assert decision.blocked and "hard gate" in decision.reason

    def test_a_failed_link_is_skipped_not_posted_broken(self) -> None:
        decision = decide_publish(
            available=[variant("480p"), variant("1080p", status="failed")], post_exists=False, quality_order=ORDER
        )
        assert decision.qualities == ("480p",)

    def test_owner_approval_counts_as_clean(self) -> None:
        decision = decide_publish(
            available=[variant("720p", thumb="owner_approved")], post_exists=False, quality_order=ORDER
        )
        assert decision.action == PublishAction.CREATE

    def test_review_rows_are_not_published_by_the_back_door(self) -> None:
        decision = decide_publish(
            available=[variant("720p", status="review")], post_exists=False, quality_order=ORDER
        )
        assert decision.action == PublishAction.NOOP and decision.blocked


class TestCoverage:
    def test_a_season_with_an_unknown_end_is_never_complete(self) -> None:
        coverage = season_coverage([1, 2, 3])
        assert not coverage.complete
        assert coverage.expected is None
        assert coverage.ratio == "3 so far"

    def test_explicit_expected_count_governs_completeness(self) -> None:
        coverage = season_coverage([1, 2, 3], expected_episodes=12)
        assert not coverage.complete
        assert coverage.missing == (4, 5, 6, 7, 8, 9, 10, 11, 12)
        assert "9 still missing" in progress_line(coverage)

    def test_complete_season_allows_exactly_one_batch_post(self) -> None:
        coverage = season_coverage(list(range(1, 13)), expected_episodes=12)
        assert coverage.complete
        assert should_post_season_batch(coverage) is True
        assert should_post_season_batch(coverage, batch_post_exists=True) is False

    def test_batch_post_is_not_made_from_nothing(self) -> None:
        assert should_post_season_batch(season_coverage([], expected_episodes=12)) is False

    def test_progress_line_names_the_series_when_asked(self) -> None:
        line = progress_line(season_coverage([1, 2], expected_episodes=4), series="bleach", season=1)
        assert line == "Bleach Season 1: 2 of 4 episodes · 2 still missing"
