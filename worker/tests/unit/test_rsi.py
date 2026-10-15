"""Unit tests for the RSI module: Wilder series, streaming reader, both rules.

Every expected number is hand-computed from the definition stated in the
module docstring — never copied from the implementation's own output — so a
formula change fails here instead of silently rewriting the operator's bias.

The two rules the module carries are tested apart, because they answer different
questions: ``classify`` is the nightly daily+weekly pair, and ``classify_intraday``
is this morning's five-minute reading beside the live trend.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from worker.analytics.rsi import (
    ENTRY,
    ENTRY_THRESHOLD,
    NEUTRAL,
    OVERBOUGHT,
    OVERBOUGHT_THRESHOLD,
    TREND_DOWN,
    TREND_MIXED,
    TREND_UP,
    WilderRsi,
    classify,
    classify_intraday,
    closes_bucket,
    latest_rsi,
    resample_and_compute,
    resample_weekly,
    trend_state,
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


def test_classify_both_above_sixty_is_overbought():
    assert classify(65.0, 70.0) == OVERBOUGHT
    assert classify(OVERBOUGHT_THRESHOLD + 0.0001, 99.0) == OVERBOUGHT


def test_classify_both_below_forty_is_entry():
    assert classify(30.0, 35.0) == ENTRY
    assert classify(1.0, ENTRY_THRESHOLD - 0.0001) == ENTRY


def test_classify_mixed_disagreement_is_neutral():
    assert classify(65.0, 50.0) == NEUTRAL  # daily says expensive, weekly does not
    assert classify(45.0, 30.0) == NEUTRAL  # weekly says entry, daily does not


def test_classify_exact_thresholds_are_neutral():
    # "above 60" / "below 40" are strict; 60.0 and 40.0 sit in the neutral band.
    assert classify(60.0, 60.0) == NEUTRAL
    assert classify(40.0, 40.0) == NEUTRAL
    assert classify(60.0, 41.0) == NEUTRAL


def test_classify_missing_value_is_not_neutral():
    assert classify(None, 70.0) is None
    assert classify(70.0, None) is None
    assert classify(None, None) is None


# ── the live trend ──────────────────────────────────────────────────────────


def test_trend_needs_both_direction_conditions_to_agree():
    # Up = close above VWAP *and* fast EMA above slow: the scanner's own two tests,
    # and the relative-volume gate is deliberately not one of them.
    assert trend_state(close=101.0, vwap=100.0, ema_fast=100.5, ema_slow=100.0) == TREND_UP
    assert trend_state(close=99.0, vwap=100.0, ema_fast=99.5, ema_slow=100.0) == TREND_DOWN


def test_trend_eyes_are_a_chop_not_a_direction():
    # Above VWAP with the EMAs crossed down, and the mirror image: neither side.
    assert trend_state(close=101.0, vwap=100.0, ema_fast=99.0, ema_slow=100.0) == TREND_MIXED
    assert trend_state(close=99.0, vwap=100.0, ema_fast=100.0, ema_slow=99.0) == TREND_MIXED


def test_trend_missing_input_is_mixed_not_up():
    # A young session has no VWAP. Inventing a direction from the parts that do
    # exist would let an unjudgeable candle be reported as an entry.
    assert trend_state(close=101.0, vwap=None, ema_fast=100.0, ema_slow=99.0) == TREND_MIXED
    assert trend_state(close=None, vwap=100.0, ema_fast=100.0, ema_slow=99.0) == TREND_MIXED


# ── the intraday entry rule ─────────────────────────────────────────────────


def test_intraday_oversold_in_an_uptrend_is_the_entry():
    assert classify_intraday(35.0, TREND_UP) == ENTRY


def test_intraday_overbought_in_a_downtrend_is_the_warning():
    assert classify_intraday(65.0, TREND_DOWN) == OVERBOUGHT


def test_intraday_extreme_without_a_trend_is_not_news():
    # The whole point of pairing them: an oversold print in a falling tape is a
    # falling tape, and the same print in a chop is nobody's entry.
    assert classify_intraday(35.0, TREND_DOWN) == NEUTRAL
    assert classify_intraday(35.0, TREND_MIXED) == NEUTRAL
    assert classify_intraday(65.0, TREND_UP) == NEUTRAL
    assert classify_intraday(65.0, TREND_MIXED) == NEUTRAL


def test_intraday_band_is_neutral_and_exact_levels_are_not_extremes():
    assert classify_intraday(50.0, TREND_UP) == NEUTRAL
    assert classify_intraday(40.0, TREND_UP) == NEUTRAL  # "below 40" is strict
    assert classify_intraday(60.0, TREND_DOWN) == NEUTRAL


def test_intraday_no_reading_is_none_rather_than_neutral():
    # A young session has fewer than fifteen buckets. NEUTRAL is an observation the
    # alert engine acts on; None means the rule was never given a number.
    assert classify_intraday(None, TREND_UP) is None


def test_intraday_thresholds_are_the_ones_the_operator_edited():
    # The levels on /alerts are what the rule reads; the shipped 40/60 are defaults,
    # not constants baked into the verdict.
    assert classify_intraday(55.0, TREND_UP, entry_threshold=60.0) == ENTRY
    assert classify_intraday(55.0, TREND_DOWN, entry_threshold=60.0) == NEUTRAL
    assert classify_intraday(75.0, TREND_DOWN, overbought_threshold=70.0) == OVERBOUGHT
    # …and the same reading under the untouched defaults.
    assert classify_intraday(55.0, TREND_UP) == NEUTRAL
    assert classify_intraday(75.0, TREND_DOWN) == OVERBOUGHT


# ── bucket boundaries ───────────────────────────────────────────────────────


def test_bucket_close_is_the_fifth_minute_of_the_session():
    # 09:15 opens the session on an exact five-minute boundary, so the candle that
    # *starts* at :19 is the one whose close ends the 09:15–09:20 bucket.
    assert closes_bucket(datetime(2026, 10, 15, 9, 19))
    assert closes_bucket(datetime(2026, 10, 15, 9, 24))
    assert closes_bucket(datetime(2026, 10, 15, 15, 29))  # the day's last one
    assert not closes_bucket(datetime(2026, 10, 15, 9, 15))
    assert not closes_bucket(datetime(2026, 10, 15, 9, 20))
    assert not closes_bucket(datetime(2026, 10, 15, 9, 19, 30))  # not a whole minute


# ── streaming RSI agrees with the series ────────────────────────────────────


def test_streaming_rsi_equals_the_batch_series():
    # Two implementations of one formula is how a dashboard starts disagreeing with
    # itself, so the running value must land on the series' last entry every step.
    closes = [100, 101, 100.5, 102, 101.5, 103, 104, 103.5, 105, 104.5]
    closes += [105, 106, 105.5, 107, 106.5, 108, 107.5, 109, 110, 108]
    engine = WilderRsi()
    series = wilder_rsi(closes)
    for index, close in enumerate(closes):
        running = engine.update(close)
        assert running == series[index]
    assert engine.value == series[-1]
    assert engine.observations == len(closes)


def test_streaming_rsi_is_silent_until_the_period_is_full():
    engine = WilderRsi(period=3)
    assert [engine.update(close) for close in (10, 11, 12)] == [None, None, None]
    assert engine.update(11) is not None


def test_streaming_rsi_checkpoint_round_trips():
    closes = list(range(100, 140)) + [135 - i for i in range(10)]
    engine = WilderRsi()
    for close in closes:
        engine.update(float(close))
    restored = WilderRsi.from_dict(engine.to_dict())
    assert restored.value == engine.value
    # The next close must follow the *history*, not the checkpoint's tail window:
    # re-seeding from the tail alone would silently restart the recursion.
    nxt = 121.0
    assert restored.update(nxt) == engine.update(nxt)
    assert restored.to_dict() == engine.to_dict()


def test_streaming_rsi_rejects_a_bad_close():
    with pytest.raises(ValueError):
        WilderRsi().update(0.0)
    with pytest.raises(ValueError):
        WilderRsi().update(-1.0)
    with pytest.raises(ValueError):
        WilderRsi().update(float("nan"))
    with pytest.raises(ValueError):
        WilderRsi(period=1)
