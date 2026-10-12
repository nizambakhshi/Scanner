"""Unit tests for the daily/weekly Wilder RSI (worker.analytics.rsi).

Every expected number is hand-computed from the definition stated in the
module docstring — never copied from the implementation's own output — so a
formula change fails here instead of silently rewriting the operator's bias.
"""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from worker.analytics.rsi import (
    BEARISH,
    BEARISH_THRESHOLD,
    BULLISH,
    BULLISH_THRESHOLD,
    NEUTRAL,
    classify,
    latest_rsi,
    resample_and_compute,
    resample_weekly,
    week_start_of,
    wilder_rsi,
)


def bar(day: date, o: float, h: float, lo: float, c: float, v: int) -> SimpleNamespace:
    return SimpleNamespace(trading_date=day, open=o, high=h, low=lo, close=c, volume=v)


# ── wilder_rsi ──────────────────────────────────────────────────────────────


def test_rsi_period_two_hand_computed():
    # closes 10, 11, 10.5, 12, 13; period 2.
    # seed (bar 2): gains (1, 0) → avg 0.5; losses (0, 0.5) → avg 0.25; RS=2 → 66.667
    # bar 3 (+1.5): avg_gain (0.5+1.5)/2=1.0; avg_loss 0.25/2=0.125; RS=8 → 88.889
    # bar 4 (+1.0): avg_gain (1.0+1.0)/2=1.0; avg_loss 0.125/2=0.0625; RS=16 → 94.118
    series = wilder_rsi([10, 11, 10.5, 12, 13], period=2)
    assert series[0] is None and series[1] is None
    assert series[2] == pytest.approx(66.6667, abs=1e-3)
    assert series[3] == pytest.approx(88.8889, abs=1e-3)
    assert series[4] == pytest.approx(94.1176, abs=1e-3)


def test_rsi_period_three_recursion_hand_computed():
    # closes 1, 2, 3, 2, 4; period 3.
    # seed (bar 3): gains (1,1,0) → avg 2/3; losses (0,0,1) → avg 1/3; RS=2 → 66.667
    # bar 4 (+2): avg_gain (2/3·2 + 2)/3 = 10/9; avg_loss (1/3·2 + 0)/3 = 2/9;
    #             RS = 5 → 100 − 100/6 = 83.333
    series = wilder_rsi([1, 2, 3, 2, 4], period=3)
    assert series[:3] == [None, None, None]
    assert series[3] == pytest.approx(66.6667, abs=1e-3)
    assert series[4] == pytest.approx(83.3333, abs=1e-3)


def test_rsi_monotonic_rise_is_one_hundred():
    series = wilder_rsi([1, 2, 3, 4, 5, 6, 7, 8], period=3)
    assert series[-1] == pytest.approx(100.0)


def test_rsi_monotonic_fall_is_zero():
    series = wilder_rsi([8, 7, 6, 5, 4, 3, 2, 1], period=3)
    assert series[-1] == pytest.approx(0.0)


def test_rsi_flat_series_is_fifty():
    series = wilder_rsi([100.0] * 6, period=3)
    assert series[-1] == pytest.approx(50.0)


def test_rsi_too_short_is_all_none():
    assert wilder_rsi([10, 11, 12], period=14) == [None, None, None]
    assert wilder_rsi([], period=14) == []
    # period+1 closes is exactly when the first value appears (at index period).
    assert wilder_rsi([10, 11], period=2) == [None, None]
    assert wilder_rsi([10, 11, 12], period=2)[2] is not None


def test_rsi_stays_within_bounds_on_a_realistic_walk():
    # fmt: off
    closes = [
        100, 101, 99, 102, 103, 101, 98, 99, 104, 105, 103, 102, 106, 107, 100, 96, 97, 95, 99, 101,
    ]
    # fmt: on
    for value in wilder_rsi(closes, period=14):
        if value is not None:
            assert 0.0 <= value <= 100.0


def test_rsi_rejects_bad_closes():
    with pytest.raises(ValueError):
        wilder_rsi([10, 0, 12, 13], period=2)
    with pytest.raises(ValueError):
        wilder_rsi([10, -1, 12, 13], period=2)
    with pytest.raises(ValueError):
        wilder_rsi([10, float("nan"), 12, 13], period=2)
    with pytest.raises(ValueError):
        wilder_rsi([10, float("inf"), 12, 13], period=2)


def test_rsi_rejects_period_below_two():
    with pytest.raises(ValueError):
        wilder_rsi([10, 11, 12], period=1)


def test_latest_rsi_helper():
    assert latest_rsi([10, 11, 10.5, 12, 13], period=2) == pytest.approx(94.1176, abs=1e-3)
    assert latest_rsi([10], period=14) is None
    assert latest_rsi([], period=14) is None


# ── weekly resample ─────────────────────────────────────────────────────────


def test_week_start_of_is_monday():
    assert week_start_of(date(2026, 10, 12)) == date(2026, 10, 12)  # Monday
    assert week_start_of(date(2026, 10, 14)) == date(2026, 10, 12)  # Wednesday
    assert week_start_of(date(2026, 10, 18)) == date(2026, 10, 12)  # Sunday


def test_resample_weekly_aggregates_ohlcv():
    days = [
        bar(date(2026, 10, 5), 100, 104, 99, 103, 1000),  # Mon
        bar(date(2026, 10, 6), 103, 108, 102, 107, 1200),  # Tue
        bar(date(2026, 10, 7), 107, 110, 101, 102, 900),  # Wed
        bar(date(2026, 10, 8), 102, 105, 100, 104, 800),  # Thu
        bar(date(2026, 10, 9), 104, 106, 98, 99, 1100),  # Fri
        bar(date(2026, 10, 12), 99, 101, 97, 100, 700),  # next Mon
    ]
    weeks = resample_weekly(days)
    assert len(weeks) == 2
    first, second = weeks
    assert first.week_start == date(2026, 10, 5)
    assert first.open == 100 and first.close == 99
    assert first.high == 110 and first.low == 98
    assert first.volume == 5000
    # The newest week is partial by design: one Monday bar so far.
    assert second.week_start == date(2026, 10, 12)
    assert second.open == 99 and second.high == 101 and second.low == 97
    assert second.close == 100 and second.volume == 700


def test_resample_weekly_accepts_unsorted_input():
    days = [
        bar(date(2026, 10, 7), 107, 110, 101, 102, 900),
        bar(date(2026, 10, 5), 100, 104, 99, 103, 1000),
        bar(date(2026, 10, 6), 103, 108, 102, 107, 1200),
    ]
    weeks = resample_weekly(days)
    assert len(weeks) == 1
    assert weeks[0].open == 100 and weeks[0].close == 102  # chronological order applied


def test_resample_and_compute_alignment():
    # 25 consecutive days from Mon 2026-09-28 → 4 ISO weeks, enough for period 3.
    days = [
        bar(date(2026, 9, 28) + timedelta(days=i), 100 + i, 101 + i, 99 + i, 100 + i, 10)
        for i in range(25)
    ]
    weeks, series = resample_and_compute(days, period=3)
    assert len(series) == len(weeks) == 4
    assert series[-1] is not None and weeks[-1].close == days[-1].close


# ── classification ──────────────────────────────────────────────────────────


def test_classify_both_above_sixty_is_bullish():
    assert classify(65.0, 70.0) == BULLISH
    assert classify(BULLISH_THRESHOLD + 0.0001, 99.0) == BULLISH


def test_classify_both_below_forty_is_bearish():
    assert classify(30.0, 35.0) == BEARISH
    assert classify(1.0, BEARISH_THRESHOLD - 0.0001) == BEARISH


def test_classify_mixed_disagreement_is_neutral():
    assert classify(65.0, 50.0) == NEUTRAL  # daily says up, weekly does not
    assert classify(45.0, 30.0) == NEUTRAL  # weekly says down, daily does not


def test_classify_exact_thresholds_are_neutral():
    # "above 60" / "below 40" are strict; 60.0 and 40.0 sit in the neutral band.
    assert classify(60.0, 60.0) == NEUTRAL
    assert classify(40.0, 40.0) == NEUTRAL
    assert classify(60.0, 41.0) == NEUTRAL


def test_classify_missing_value_is_not_neutral():
    assert classify(None, 70.0) is None
    assert classify(70.0, None) is None
    assert classify(None, None) is None
