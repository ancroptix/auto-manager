"""The in-place mode: caption the file that is already there.

These tests are the contract for the second shape of publishing the operator asked for —
their own channel, its files already posted, each message saying only ``episode 7``. The
behaviour that matters is not the caption text (``test_caption_templates.py`` owns that) but
the *decisions*: what gets overwritten without asking, what is left alone, and what is never
copied twice.
"""

from __future__ import annotations

import pytest

from app import captions, inplace


# --- modes -------------------------------------------------------------------------------


def test_in_place_mode_skips_the_audio_gate_but_link_mode_keeps_it() -> None:
    """The gate guards the door files come *in* through; in-place opens no door."""
    assert inplace.mode_allows_missing_audio(inplace.MODE_IN_PLACE) is True
    assert inplace.mode_allows_missing_audio(inplace.MODE_LINK) is False
    # A destination with no mode set yet is a link destination, gate and all.
    assert inplace.mode_allows_missing_audio(None) is False
    assert inplace.mode_allows_missing_audio("In_Place_Caption") is True  # tolerant of casing


# --- what is only a label ----------------------------------------------------------------


@pytest.mark.parametrize(
    "caption",
    [
        None,
        "",
        "   ",
        "episode 7",
        "Episode 7",
        "ep 7",
        "E07",
        "7",
        "07/24",
        "PART 07",
        "S2 E3",
        "12 - mkv",
    ],
)
def test_an_episode_marker_is_replaceable(caption: str | None) -> None:
    assert inplace.looks_like_label(caption) is True


@pytest.mark.parametrize(
    "caption",
    [
        "episode 7 fixed 12/8",
        "check https://t.me/foo",
        "source: @someone",
        "note: re-encode",
        "07.12.2024",
        "episode 7 (12/8)",
        "12 - 34",
        "Watch in the group, mirror links pinned",
    ],
)
def test_anything_with_information_in_it_is_not(caption: str) -> None:
    """A bare range and a parenthesised date included: overwriting those loses real data.

    ``12 - 34`` is usually one video holding twenty-three episodes, so a caption naming a
    single episode would describe the file wrongly rather than clean it up.
    """
    assert inplace.looks_like_label(caption) is False


def test_length_is_checked_against_telegrams_own_media_caption_limit() -> None:
    assert inplace.MAX_CAPTION_CHARS == 1024
    assert inplace.caption_is_too_long("x" * 1024) is False
    assert inplace.caption_is_too_long("x" * 1025) is True


# --- comparing the two channels ----------------------------------------------------------


def test_twelve_files_against_twelve_files_is_twelve_edits_and_no_copies() -> None:
    """The operator's own scenario, stated back as an assertion.

    Source has 12 episodes, the destination has the same 12 as raw files: the work is to fix
    those twelve posts. Nothing is fetched, nothing is copied, nothing is deleted.
    """
    shape = inplace.compare(list(range(1, 13)), list(range(1, 13)))
    assert shape.counts == {
        "here": 12,
        "there": 12,
        "missing_here": 0,
        "extra_here": 0,
        "common": 12,
    }
    assert shape.numbering_shifted is False
    rows = [
        {
            "message_id": 900 + episode,
            "episode": episode,
            "caption": f"episode {episode}",
            "is_media": True,
            "title": "Bleach",
            "quality_list": ["720p"],
        }
        for episode in range(1, 13)
    ]
    decisions = inplace.plan(rows, shape=shape)
    assert [decision.action for decision in decisions] == [inplace.Action.CAPTION] * 12
    assert inplace.summary(decisions) == "12 caption"


def test_an_episode_only_the_source_has_is_brought_in_by_copy_not_by_download() -> None:
    shape = inplace.compare([1, 2], [1, 2, 3])
    decisions = inplace.plan(
        [
            {"message_id": 1, "episode": 1, "caption": "ep 1", "is_media": True, "title": "B"},
            {"message_id": 2, "episode": 2, "caption": "ep 2", "is_media": True, "title": "B"},
        ],
        shape=shape,
    )
    copies = [decision for decision in decisions if decision.action == inplace.Action.COPY_THEN_CAPTION]
    assert [decision.episode for decision in copies] == [3]
    assert "forward" in copies[0].reason


def test_copying_can_be_switched_off_and_the_destination_extras_stay_put() -> None:
    shape = inplace.compare([1, 2, 7], [1, 2])
    assert shape.counts["extra_here"] == 1
    decisions = inplace.plan(
        [{"message_id": n, "episode": n, "caption": None, "is_media": True, "title": "B"} for n in (1, 2, 7)],
        shape=shape,
        allow_copy=False,
    )
    assert inplace.Action.COPY_THEN_CAPTION not in [decision.action for decision in decisions]
    # Episode 7 exists only here. It is kept, and it still gets its caption: "in-place" is
    # about not moving files, not about leaving some of them unlabelled.
    assert [decision.action for decision in decisions] == [inplace.Action.CAPTION] * 3
    assert "1 only here (kept, and still captioned)" in inplace.shape_note(shape)


def test_the_renumbering_trap_copies_nothing_and_asks_instead() -> None:
    """Equal counts, no overlap, one constant shift = two numbering schemes, not missing files."""
    shape = inplace.compare([0, 1, 2, 3], [4, 5, 6, 7])
    assert shape.numbering_shifted is True
    assert shape.offset == 4
    decisions = inplace.plan(
        [{"message_id": n, "episode": n, "caption": None, "is_media": True, "title": "B"} for n in (0, 1, 2, 3)],
        shape=shape,
    )
    assert inplace.Action.COPY_THEN_CAPTION not in [decision.action for decision in decisions]
    asks = [decision for decision in decisions if decision.action == inplace.Action.ASK]
    assert len(asks) == 1
    assert asks[0].details["offset"] == 4
    assert "renumbering" in asks[0].reason


def test_partial_overlap_is_not_treated_as_a_shift() -> None:
    """One shared episode number is enough to prove the two lists use the same scheme."""
    shape = inplace.compare([1, 2, 3], [2, 3, 4, 5])
    assert shape.numbering_shifted is False
    assert sorted(shape.missing_here) == [4, 5]
    assert sorted(shape.extra_here) == [1]


def test_junk_and_negative_numbers_are_ignored_when_reading_a_channel() -> None:
    shape = inplace.compare([1, 2, None, "x", -1001234567890, 2], [1, 2])
    assert shape.counts["here"] == 2


# --- roles -------------------------------------------------------------------------------


def test_admin_here_member_there_makes_here_the_destination() -> None:
    """The rule as the operator gave it: the channel we can post in is the one we publish."""
    pair = inplace.pair_roles(
        [
            {"id": 1, "title": "Naruto", "we_are_admin": True},
            {"id": 2, "title": "Naruto", "we_are_admin": False},
        ]
    )
    assert [row["id"] for row in pair["destination"]] == [1]
    assert [row["id"] for row in pair["source"]] == [2]
    assert pair["ask"] is None


def test_two_admin_channels_is_a_question_not_a_sort() -> None:
    pair = inplace.pair_roles(
        [{"id": 1, "we_are_admin": True}, {"id": 2, "we_are_admin": True}],
    )
    assert pair["destination"] == []
    assert "cannot pick" in pair["ask"]


def test_no_admin_channel_means_nothing_can_be_written_anywhere() -> None:
    pair = inplace.pair_roles([{"id": 1, "we_are_admin": False}])
    assert pair["destination"] == []
    assert "rights" in pair["ask"]


# --- the caption on the file -------------------------------------------------------------


def test_the_caption_is_the_approved_box_with_no_buttons_after_it() -> None:
    text, missing = inplace.caption_for(
        title="Bleach",
        episode=7,
        audio_kind="hindi",
        quality_list=["1080p"],
        declared_episodes=12,
    )
    assert missing == ()
    assert "❍ 𝗘𝗽𝗶𝘀𝗼𝗱𝗲: 07" in text
    assert "♡ 𝗣𝗼𝘄𝗲𝗿𝗲𝗱 𝗯𝘆: @YCAnime , @India_crunchyroll" in text
    # No link exists in this mode, so a button line would be a button to nothing.
    assert "\u2750" not in text  # the button glyph the link mode uses
    assert "http" not in text


def test_an_unproven_audio_claim_prints_unknown_instead_of_inventing_hindi() -> None:
    text, missing = inplace.caption_for(title="Bleach", episode=1, audio_kind=None)
    assert missing == ()
    assert "〄 𝗔𝘂𝗱𝗶𝗼: Unknown" in text


def test_the_operators_edited_template_is_the_one_used() -> None:
    text, missing = inplace.caption_for(
        title="Bleach", episode=3, template="{title_full} #{episode}"
    )
    assert missing == ()
    assert text == "Bleach #03"


def test_a_missing_value_is_reported_rather_than_posted_as_a_placeholder() -> None:
    text, missing = inplace.caption_for(title=None, episode=3)
    assert missing  # the caller turns this into a question, not a publish
    assert "{title_full}" in text or "title" in missing


# --- one decision at a time -------------------------------------------------------------


def test_an_exact_match_is_skipped_which_is_what_makes_a_resume_safe() -> None:
    text, _ = inplace.caption_for(title="Bleach", episode=4, audio_kind="hindi")
    decision = inplace.decision_for(
        message_id=77, episode=4, existing_caption=text, our_caption=text
    )
    assert decision.action == inplace.Action.SKIP
    assert decision.changes_anything is False


def test_a_note_is_asked_about_and_the_old_text_is_kept_in_the_decision() -> None:
    decision = inplace.decision_for(
        message_id=77,
        episode=4,
        existing_caption="episode 4 fixed, mirror link added",
        our_caption="whatever",
    )
    assert decision.action == inplace.Action.ASK
    assert decision.previous_caption == "episode 4 fixed, mirror link added"


def test_a_file_with_nothing_to_say_about_itself_is_asked_about() -> None:
    decision = inplace.decision_for(message_id=5, episode=None, existing_caption=None, our_caption=None)
    assert decision.action == inplace.Action.ASK
    assert "nothing to build a caption from" in decision.reason


def test_an_overlong_caption_asks_instead_of_truncating() -> None:
    decision = inplace.decision_for(
        message_id=5,
        episode=2,
        existing_caption="ep 2",
        our_caption="x" * (inplace.MAX_CAPTION_CHARS + 5),
    )
    assert decision.action == inplace.Action.ASK
    assert decision.details["length"] == inplace.MAX_CAPTION_CHARS + 5


def test_a_text_message_in_the_channel_is_not_a_file_post() -> None:
    decision = inplace.decision_for(
        message_id=9, episode=None, existing_caption="welcome", is_media=False
    )
    assert decision.action == inplace.Action.IGNORE
    assert decision.changes_anything is False


def test_a_caption_we_wrote_ourselves_is_replaced_silently_when_the_template_moves() -> None:
    """Otherwise every template edit turns the operator's own channel into a review queue."""
    current, _ = inplace.caption_for(title="Bleach", episode=2, audio_kind="hindi")
    older = "✦ Bleach ✦\n\nOLD BOX ❍ 𝗘𝗽𝗶𝘀𝗼𝗱𝗲: 02"
    decision = inplace.decision_for(
        message_id=3, episode=2, existing_caption=older, our_caption=current, ours_last_time=older
    )
    assert decision.action == inplace.Action.CAPTION
    assert "current template" in decision.reason
    assert decision.previous_caption == older


def test_a_replacement_carries_the_text_it_burned() -> None:
    """Telegram keeps no history of a caption; the row is the only undo."""
    decision = inplace.decision_for(
        message_id=3, episode=2, existing_caption="ep 2", our_caption="new box"
    )
    row = decision.to_row()
    assert row["previous_caption"] == "ep 2"
    assert row["caption"] == "new box"
    assert set(row) == {
        "action",
        "reason",
        "episode",
        "message_id",
        "previous_caption",
        "caption",
    }


def test_the_plan_carries_the_job_key_the_publisher_will_use() -> None:
    """Not a decoration: the preview and the queue must compute one string, or "resume safely"
    is a promise about a key nobody checked."""
    from app.keys import inplace_key

    rows = [
        {"message_id": 901, "episode": 1, "caption": "ep 1", "is_media": True, "title": "B"},
        {"message_id": 902, "episode": 2, "caption": "ep 2", "is_media": True, "title": "B"},
    ]
    decisions = inplace.plan(rows, destination_id=6)
    assert [decision.details["dedup_key"] for decision in decisions] == [
        inplace_key(6, 901),
        inplace_key(6, 902),
    ]
    # A skip is not work, so it gets no job to run.
    caption, _ = inplace.caption_for(title="B", episode=3, audio_kind="hindi")
    only_skip = inplace.plan(
        [{"message_id": 903, "episode": 3, "caption": caption, "is_media": True, "title": "B"}],
        destination_id=6,
    )
    assert "dedup_key" not in only_skip[0].details


def test_summary_counts_actions_and_says_so_when_there_is_nothing_to_do() -> None:
    assert inplace.summary([]) == "nothing to do"
    decisions = [
        inplace.Decision(inplace.Action.CAPTION, "x"),
        inplace.Decision(inplace.Action.CAPTION, "x"),
        inplace.Decision(inplace.Action.ASK, "x"),
        inplace.Decision(inplace.Action.SKIP, "x"),
    ]
    assert inplace.summary(decisions) == "2 caption, 1 ask, 1 skip"
