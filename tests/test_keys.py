"""Key construction is the duplicate-work defence, so it gets its own tests.

A dedup key that varies by spelling, case, or argument order silently creates a
second job for the same file.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.keys import (
    archive_key,
    canonical_episode_key,
    discovery_key,
    normalize_title,
    publish_key,
    quality_rank,
    reconciliation_key,
    sticker_key,
    variant_identity,
)


def test_title_normalization_matches_channel_names_to_captions() -> None:
    assert normalize_title("  Berserk ") == normalize_title("berserk")
    assert normalize_title("Bleach   S01") == "bleach s01"
    assert normalize_title("") == ""


def test_canonical_key_is_stable_and_readable() -> None:
    key = canonical_episode_key("Bleach", 1, 1, ["Hindi"])
    assert key == "bleach|s01|e01|hindi"
    assert key == canonical_episode_key("  bleach ", 1, 1, ["hindi"])


def test_language_order_does_not_create_a_second_episode() -> None:
    assert canonical_episode_key("B", 1, 2, ["hindi", "english"]) == canonical_episode_key(
        "B", 1, 2, ["english", "hindi"]
    )


def test_english_only_is_a_different_episode_than_hindi() -> None:
    """Otherwise a subtitled/English upload could satisfy a Hindi episode."""
    assert canonical_episode_key("B", 1, 1, ["english"]) != canonical_episode_key("B", 1, 1, ["hindi"])


def test_release_variant_is_part_of_identity() -> None:
    base = canonical_episode_key("B", 1, 1, ["hindi"])
    assert base != canonical_episode_key("B", 1, 1, ["hindi"], "remux")
    assert canonical_episode_key("B", 1, 1, ["hindi"], "REMUX") == canonical_episode_key(
        "B", 1, 1, ["hindi"], "remux"
    )


def test_variant_identity_case_folds_like_the_index_does() -> None:
    assert variant_identity("1080P") == variant_identity("1080p")
    assert variant_identity(" 480p ", None) == variant_identity("480p", "")


def test_quality_rank_follows_the_configured_order() -> None:
    """This ordering is what makes 1080p appear *after* 720p even when it
    arrives six months later."""
    order = ["360p", "480p", "720p", "1080p", "2160p"]
    shuffled = ["2160p", "480p", "1080p", "360p", "720p"]
    ranks = {q: quality_rank(q, order) for q in shuffled}
    assert [q for q, _ in sorted(ranks.items(), key=lambda item: item[1])] == order
    assert len(set(ranks.values())) == 5


def test_unknown_quality_sorts_last_but_stably() -> None:
    order = ["480p", "1080p"]
    assert quality_rank("4K HDR", order) > quality_rank("1080p", order)
    assert quality_rank("", order) == quality_rank("", order)


def test_resolution_in_an_unknown_label_still_orders_sensibly() -> None:
    order = ["480p", "1080p"]
    assert quality_rank("1440p", order) > quality_rank("1080p", order)


@pytest.mark.parametrize(
    "factory,sample",
    [
        (discovery_key, (7, 4321, 0)),
        (archive_key, (99,)),
        (publish_key, (12,)),
        (sticker_key, (3,)),
    ],
)
def test_keys_are_deterministic(factory, sample) -> None:
    assert factory(*sample) == factory(*sample)
    assert " " not in factory(*sample)


def test_discovery_key_separates_album_items_and_messages() -> None:
    assert discovery_key(7, 100, 0) != discovery_key(7, 100, 1)
    assert discovery_key(7, 100, 0) != discovery_key(8, 100, 0)


def test_reconciliation_collapses_a_restart_storm_into_one_job() -> None:
    moment = dt.datetime(2026, 8, 27, 14, 5, tzinfo=dt.UTC)
    later_same_hour = moment.replace(minute=58)
    next_hour = moment.replace(hour=15)
    assert reconciliation_key(moment) == reconciliation_key(later_same_hour)
    assert reconciliation_key(moment) != reconciliation_key(next_hour)


def test_manual_reconciliation_is_never_collapsed() -> None:
    """An operator who asks for a sweep must get one, even minutes after boot."""
    base = reconciliation_key(dt.datetime(2026, 8, 27, 14, 5, tzinfo=dt.UTC))
    manual = f"{base}:manual:1753650000"
    assert manual != base
