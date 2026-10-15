"""Unit tests for the level inventory: folds, the swing window, the merge, lookups.

The cases map to the Phase 2C contract: session extremes exist from the first
candle, the swing window slides (an extreme falls out with its candle), the
swing/session duplication is collapsed, the merged inventory is sorted and
omits unknown inputs, the nearest-above/below lookups exclude the exact price,
and bad bars surface as data problems.
"""

from __future__ import annotations

import pytest

from worker.analytics.levels import (
    DEFAULT_SWING_WINDOW,
    ORB_HIGH,
    PRIOR_DAY_HIGH,
    SESSION_HIGH,
    SESSION_LOW,
    SWING_HIGH,
    SWING_LOW,
    VWAP,
    Level,
    LevelBook,
    next_resistance,
    next_support,
)

# ── the candle-driven half ─────────────────────────────────────────────────


def test_session_extremes_exist_from_the_first_candle():
    book = LevelBook()
    assert book.session_high is None and book.session_low is None
    book.fold(high=105, low=95)
    assert book.session_high == 105 and book.session_low == 95
    book.fold(high=103, low=99)  # inside: no change
    assert book.session_high == 105 and book.session_low == 95
    book.fold(high=107, low=93)  # both extremes extended
    assert book.session_high == 107 and book.session_low == 93


def test_swing_window_slides_and_the_old_extreme_falls_out():
    book = LevelBook(swing_window=3)
    book.fold(high=10, low=9)
    assert book.swing_high == 10 and book.swing_low == 9
    book.fold(high=20, low=15)
    assert book.swing_high == 20 and book.swing_low == 9
    book.fold(high=5, low=4)
    assert book.swing_high == 20 and book.swing_low == 4
    book.fold(high=6, low=5)
    # Window is now [20/15, 5/4, 6/5]: still contains both extremes.
    assert book.swing_high == 20 and book.swing_low == 4
    book.fold(high=7, low=6)
    # Window is [5/4, 6/5, 7/6]: the 20-spike is gone, the 20 with it.
    assert book.swing_high == 7 and book.swing_low == 4
    book.fold(high=8, low=7)
    # Window is [6/5, 7/6, 8/7]: the 4-low is gone too — a level describes
    # the recent tape, while the session extremes still remember both.
    assert book.swing_high == 8 and book.swing_low == 5
    assert book.session_high == 20 and book.session_low == 4


def test_swing_extreme_equal_to_the_session_extreme_is_not_listed_twice():
    book = LevelBook(swing_window=2)
    book.fold(high=12, low=10)
    book.fold(high=15, low=11)  # the day high is inside the window
    kinds = [level.kind for level in book.inventory()]
    assert SESSION_HIGH in kinds and SWING_HIGH not in kinds
    assert SESSION_LOW in kinds and SWING_LOW not in kinds
    book.fold(high=13, low=11)
    book.fold(high=14, low=12)  # window [13, 14]: the 15 fell out
    swing = [level for level in book.inventory() if level.kind == SWING_HIGH]
    assert swing == [Level(SWING_HIGH, 14)]


# ── the merged inventory ───────────────────────────────────────────────────


def test_inventory_merges_inputs_and_sorts_by_price_then_kind():
    book = LevelBook(swing_window=1)
    book.fold(high=105, low=95)  # swing duplicates session here
    book.fold(high=102, low=98)  # window [102/98]: both inside the session
    levels = book.inventory(
        vwap=100.0,
        orb_high=104.0,
        orb_low=None,
        prior_day_high=110.0,
        prior_day_low=None,
    )
    assert levels == [
        Level(SESSION_LOW, 95),
        Level(SWING_LOW, 98),
        Level(VWAP, 100.0),
        Level(SWING_HIGH, 102),
        Level(ORB_HIGH, 104.0),
        Level(SESSION_HIGH, 105),
        Level(PRIOR_DAY_HIGH, 110.0),
    ]
    # Unknown inputs (None) are omitted, never guessed.
    kinds = {level.kind for level in book.inventory()}
    assert kinds == {SESSION_HIGH, SESSION_LOW, SWING_HIGH, SWING_LOW}


def test_equal_external_prices_keep_their_own_kinds():
    # A session high that equals the prior day's high is two facts about two
    # sessions; both stay, and ordering is deterministic.
    book = LevelBook()
    book.fold(high=500, low=400)
    levels = book.inventory(prior_day_high=500.0)
    assert levels == [
        Level(SESSION_LOW, 400),
        Level(PRIOR_DAY_HIGH, 500.0),  # 'p' < 's': sorted by kind at equal price
        Level(SESSION_HIGH, 500),
    ]


# ── nearest lookups ────────────────────────────────────────────────────────


LEVELS = [
    Level(SWING_LOW, 98),
    Level(VWAP, 100),
    Level(ORB_HIGH, 104),
    Level(SESSION_HIGH, 105),
]


def test_next_resistance_and_support_are_the_nearest_strict_neighbours():
    assert next_resistance(LEVELS, 101) == Level(ORB_HIGH, 104)
    assert next_resistance(LEVELS, 104) == Level(SESSION_HIGH, 105)  # strictly above
    assert next_support(LEVELS, 101) == Level(VWAP, 100)
    assert next_support(LEVELS, 100) == Level(SWING_LOW, 98)  # strictly below
    assert next_resistance(LEVELS, 105) is None
    assert next_support(LEVELS, 98) is None
    assert next_resistance([], 100) is None and next_support([], 100) is None


def test_equal_priced_levels_resolve_alphabetically():
    tied = [Level(VWAP, 103), Level(SWING_HIGH, 103)]
    assert next_resistance(tied, 100) == Level(SWING_HIGH, 103)
    assert next_support(tied, 110) == Level(SWING_HIGH, 103)
    # Input order must not matter.
    assert next_resistance(list(reversed(tied)), 100) == Level(SWING_HIGH, 103)


# ── session boundary + validation ──────────────────────────────────────────


def test_reset_clears_the_session_but_keeps_the_window_width():
    book = LevelBook(swing_window=5)
    book.fold(high=105, low=95)
    book.reset()
    assert book.session_high is None and book.session_low is None
    assert book.swing_high is None and book.swing_low is None
    assert book.swing_window == 5
    assert book.inventory() == []
    book.fold(high=90, low=80)  # yesterday's levels must not leak in
    assert book.session_high == 90 and book.session_low == 80


def test_default_window_is_fifteen_minutes():
    assert DEFAULT_SWING_WINDOW == 15
    assert LevelBook().swing_window == DEFAULT_SWING_WINDOW


def test_window_below_one_is_rejected():
    with pytest.raises(ValueError, match="swing window must be >= 1"):
        LevelBook(swing_window=0)
    LevelBook(swing_window=1)  # the boundary itself is fine


def test_bad_bars_surface_as_data_problems():
    book = LevelBook()
    with pytest.raises(ValueError, match="level bar high must be a positive finite number"):
        book.fold(high=float("nan"), low=100)
    with pytest.raises(ValueError, match="level bar low must be a positive finite number"):
        book.fold(high=100, low=-1)
    with pytest.raises(ValueError, match="below low"):
        book.fold(high=99, low=100)
    assert book.session_high is None and book.session_low is None
    assert book.swing_high is None
    # A zero-range bar is odd but never negative: accepted.
    book.fold(high=100, low=100)
    assert book.session_high == 100
