"""Season boundaries: the operator's scenario, and the version of it that hurts.

The scenario as told to me: a source uploads to episode 12, then its next caption says
``S2`` and the numbering restarts at 1. Expected: recognise season 2, send the *end of
season* sticker, then the season 2 sticker, then continue uploading.

The scenarios below add the variations that decide whether a channel survives contact
with a real upload schedule — a source that never writes ``S2``, a re-upload of an old
season, a numbering gap in the middle of a season — because each of them, treated as the
easy case, produces a wrong public claim that cannot be quietly withdrawn.
"""

from __future__ import annotations

from app import seasons
from app.normalize import parse_episode
from app.seasons import Verdict, classify, publish_hold, transition_stickers

# --------------------------------------------------------------- the easy case
def test_twelve_episodes_then_s2_episode_one_is_a_boundary() -> None:
    """Exactly what was described: a stated season and a restart, agreeing with each other."""
    boundary = classify(episode=1, labelled_season=2, current_season=1, highest=12, populated=[1])
    assert boundary.verdict is Verdict.DECLARED
    assert boundary.season == 2
    assert boundary.previous_season == 1
    assert boundary.confident and not boundary.ask_owner
    assert "season 2" in boundary.reason and "episode 12" in boundary.reason
    assert boundary.evidence["highest_in_season"] == 12


def test_the_stickers_come_in_the_operators_order() -> None:
    """Closing sticker, then opening sticker, then uploading continues — that order."""
    boundary = classify(episode=1, labelled_season=2, current_season=1, highest=12, populated=[1])
    steps = transition_stickers(boundary, previous_has_content=True)
    assert [step.kind for step in steps] == ["closing", "opening"]
    assert [step.season for step in steps] == [1, 2]
    # The closing one belongs to the season it closes, not to the one that follows.
    assert steps[0].as_payload(series_id=9)["season"] == 1
    assert steps[1].as_payload(series_id=9)["kind"] == "opening"


def test_publishing_waits_for_the_stickers_that_are_still_queued() -> None:
    """Otherwise the divider lands *below* episode 1 and stops being a divider."""
    boundary = classify(episode=1, labelled_season=2, current_season=1, highest=12, populated=[1])
    assert "must post before" in publish_hold(boundary, pending_stickers=2)
    assert publish_hold(boundary, pending_stickers=0) is None


def test_numbering_that_continues_across_the_boundary_is_still_a_boundary() -> None:
    """Some channels count 1…12 then ``S2`` 13. A stated season beats arithmetic."""
    boundary = classify(episode=13, labelled_season=2, current_season=1, highest=12, populated=[1])
    assert boundary.verdict is Verdict.DECLARED and boundary.season == 2


# --------------------------------------------------- the case that used to destroy things
def test_unlabelled_restart_is_a_boundary_we_do_not_act_on_alone() -> None:
    """``Episode 1`` after episode 12, no label: probably a new season, and *probably* is
    why this asks instead of posting. Filing it into season 1 collides with the existing
    episode 1; filing it into an invented season 2 is a claim about the source that the
    source never made."""
    boundary = classify(episode=1, labelled_season=None, current_season=1, highest=12, populated=[1])
    assert boundary.verdict is Verdict.RESET
    assert boundary.season == 2  # what it *would* become, recorded but not trusted
    assert boundary.ask_owner and not boundary.confident
    assert "never said so" in boundary.reason
    assert boundary.review == boundary.reason
    assert "confirmation" in publish_hold(boundary)


def test_an_unconfirmed_reset_opens_no_stickers_at_all() -> None:
    """A closing sticker on a season that is not over is a false public statement, and
    it is posted to an audience of 30,000 people who will remember it."""
    boundary = classify(episode=1, labelled_season=None, current_season=1, highest=12, populated=[1])
    assert transition_stickers(boundary) == ()


def test_the_defaulted_season_number_never_counts_as_a_declaration() -> None:
    """``parse_episode`` fills in season 1 for an accepted file so the row can exist. If
    ingest passed that through as "the caption says season 1", then while we were filing
    season 2 every unlabelled file would read as a rewind. This is the test that keeps
    those two meanings apart."""
    parsed = parse_episode(file_name="Dekin no mogura - 01 [Hindi].mkv")
    assert parsed.season == 1 and not parsed.season_declared
    labelled = parse_episode(file_name="Dekin no mogura S02 - 01 [Hindi].mkv")
    assert labelled.season == 2 and labelled.season_declared

    # And a channel's configured season hint is a default, not a statement.
    hinted = parse_episode(raw_caption="Episode 1 [Hindi]", season_hint=3)
    assert hinted.season == 3 and not hinted.season_declared


def test_a_boundary_is_only_built_from_an_actual_statement() -> None:
    """Feeding the hint in as a label would open seasons out of configuration."""
    boundary = classify(episode=1, labelled_season=None, current_season=3, highest=None, populated=[3])
    assert boundary.verdict is not Verdict.DECLARED


# ------------------------------------------------------------------- the re-upload traps
def test_re_upload_of_a_known_episode_is_not_a_new_season() -> None:
    """Episode 7 arriving after episode 12 is a second copy or a better quality, and
    app.manifest turns that into an edit of the existing post."""
    boundary = classify(episode=7, labelled_season=None, current_season=1, highest=12, populated=[1])
    assert boundary.verdict is Verdict.BACKTRACK
    assert boundary.season == 1
    assert not boundary.is_boundary
    assert transition_stickers(boundary) == ()
    assert publish_hold(boundary) is None


def test_a_second_copy_of_a_season_we_already_have_files_into_it() -> None:
    """``S2 E05`` arriving late, when season 2 already has episodes, is a backfill."""
    boundary = classify(episode=5, labelled_season=2, current_season=2, highest=9, populated=[1, 2])
    assert boundary.verdict is Verdict.BACKTRACK and boundary.season == 2


def test_numbering_going_backwards_never_rewinds_the_channel() -> None:
    """A leech re-posting an old "S1" batch mid-season-2 must not create a second season 1
    or move the cursor back. We park it and ask."""
    for populated in ([1, 2], [2]):
        boundary = classify(episode=4, labelled_season=1, current_season=2, highest=9, populated=populated)
        assert boundary.verdict is Verdict.RETREAT, populated
        assert boundary.ask_owner and not boundary.confident
        assert transition_stickers(boundary) == ()


def test_a_gap_ahead_is_not_a_boundary_and_not_a_length() -> None:
    """Out-of-order uploads are normal. The only thing a jump says is that the middle is
    missing — it does not end the season and it certainly does not define it."""
    boundary = classify(episode=9, labelled_season=None, current_season=1, highest=5, populated=[1])
    assert boundary.verdict is Verdict.CONTINUE and boundary.season == 1
    assert boundary.evidence["gap"] == 3
    assert "missing" in boundary.reason


# ------------------------------------------------------------------------ first & odd rows
def test_the_first_episode_of_a_series_is_not_a_transition() -> None:
    boundary = classify(episode=1, labelled_season=None, current_season=1, highest=None, populated=[])
    assert boundary.verdict is Verdict.FIRST
    assert transition_stickers(boundary) == ()
    assert publish_hold(boundary) is None


def test_a_file_with_no_number_is_not_evidence_of_anything() -> None:
    """A movie or an unparsable caption cannot arithmetic. Season comes from the label or
    not at all, and ``confident=False`` says so out loud for the log."""
    boundary = classify(episode=None, labelled_season=None, current_season=1, highest=12, populated=[1])
    assert boundary.verdict is Verdict.CONTINUE
    assert boundary.season == 1 and not boundary.confident


def test_a_batch_is_resolved_by_its_label_only() -> None:
    """``Season 2 (1-12) batch`` names a season loudly and numbers nothing comparable.
    The range arithmetic must not be consulted for a file that is one archive."""
    boundary = classify(
        episode=1, labelled_season=2, current_season=1, highest=12, populated=[1], file_kind="batch"
    )
    assert boundary.verdict is Verdict.DECLARED


# --------------------------------------------------------------------- sticker idempotency
def test_stickers_are_never_posted_twice_for_one_boundary() -> None:
    """A restart mid-transition used to re-queue both stickers; the flags say otherwise."""
    boundary = classify(episode=1, labelled_season=2, current_season=1, highest=12, populated=[1])
    assert transition_stickers(boundary, opening_posted=True, closing_posted=True) == ()
    only_opening = transition_stickers(boundary, opening_posted=False, closing_posted=True)
    assert [step.kind for step in only_opening] == ["opening"]


def test_a_season_nobody_ever_saw_gets_no_farewell() -> None:
    boundary = classify(episode=1, labelled_season=2, current_season=1, highest=None, populated=[1])
    steps = transition_stickers(boundary, previous_has_content=False)
    assert [step.kind for step in steps] == ["opening"]


def test_no_sticker_sequence_exists_without_a_boundary() -> None:
    for verdict_kwargs in (
        {"episode": 13, "labelled_season": None, "current_season": 1, "highest": 12, "populated": [1]},
        {"episode": 1, "labelled_season": None, "current_season": 1, "highest": None, "populated": []},
    ):
        assert transition_stickers(classify(**verdict_kwargs)) == ()


# -------------------------------------------------------------------------- helpers & typing
def test_season_of_agrees_with_classify() -> None:
    kwargs = {"episode": 1, "labelled_season": 2, "current_season": 1, "highest": 12, "populated": [1]}
    assert seasons.season_of(**kwargs) == classify(**kwargs).season


def test_highest_seen_reads_rows_dicts_and_gaps_alike() -> None:
    assert seasons.highest_seen([2, 5, 12, None]) == 12
    assert seasons.highest_seen([{"episode_number": 3}, {"episode_number": 9}]) == 9
    assert seasons.highest_seen([(4,), (7,)]) == 7
    assert seasons.highest_seen([]) is None
    assert seasons.highest_seen([None, "x", -3]) is None


def test_populated_seasons_counts_episodes_not_rows() -> None:
    assert seasons.populated_seasons([2, 3, None]) == {2, 3}


def test_negative_and_junk_inputs_cannot_forge_a_season() -> None:
    """The label is free text from a caption: ``S-1``, ``S9999`` and ``Sx`` must not be
    able to make us open a season with a negative or absurd number."""
    for junk in (-1, "-2", "banana", None, True, 10**9):
        boundary = classify(episode=1, labelled_season=junk, current_season=1, highest=12, populated=[1])
        assert boundary.season >= 1
        assert boundary.verdict in {Verdict.RESET, Verdict.CONTINUE, Verdict.BACKTRACK}, junk


def test_an_absurd_season_label_is_ignored_rather_than_filed() -> None:
    """"S999999" in a caption is a typo or an attack, not season 999999. Filing it would
    strand the episode in a season no manifest will ever finish, and the review queue is
    where a mistake like that belongs."""
    boundary = classify(episode=1, labelled_season=999999, current_season=1, highest=12, populated=[1])
    assert boundary.verdict is Verdict.CONTINUE
    assert boundary.season == 1 and not boundary.confident
    assert "cannot exist" in boundary.reason
    assert transition_stickers(boundary) == ()


def test_review_reason_is_none_when_nothing_is_blocked() -> None:
    boundary = classify(episode=13, labelled_season=None, current_season=1, highest=12, populated=[1])
    assert boundary.review is None and boundary.verdict is Verdict.CONTINUE
