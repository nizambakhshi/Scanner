"""Unit tests for the ATR engine: seeding, exactness, gaps, checkpoints, validation.

Every expected number is hand-computed from the definition in the module
docstring — never copied from the implementation's own output — so a formula
change fails here instead of silently rewriting the scale the setup engine's
stop distance and extension checks will later read. Long walks are additionally
compared against an independent reference implementation written from the
definition.

The cases map to the Phase 2C contract: initialization (first TR is H−L, SMA
seed for the first ``period`` true ranges), warm-up (first reading on the
14th fed candle), known values, flat bars, missing minutes, session resets and
the rebuild-by-replay pattern (engine level, in test_indicators), checkpoint
round-trips, and input validation.
"""

from __future__ import annotations

import pytest

from worker.analytics.atr import DEFAULT_ATR_PERIOD, AtrState

# ── small fully hand-computed cases ────────────────────────────────────────


def test_atr_seeds_with_sma_of_first_period_true_ranges():
    # period=3 over bars whose true ranges are 2, 2, 3:
    #   bar1 (102,100,101): first TR = H−L = 2
    #   bar2 (103,101,102): max(2, |103−101|, |101−101|) = 2
    #   bar3 (101,99,100):  max(2, |101−102|, |99−102|) = 3
    # seed = (2+2+3)/3 = 7/3 at the third bar — None before that.
    state = AtrState(period=3)
    assert state.update(high=102, low=100, close=101) is None
    assert state.update(high=103, low=101, close=102) is None
    assert state.update(high=101, low=99, close=100) == pytest.approx(7 / 3)


def test_atr_recursion_hand_computed_on_a_walk():
    # Continuing the series above:
    #   bar4 (104,100,103): TR = max(4, |104−100|, |100−100|) = 4
    #     ATR4 = (2·(7/3) + 4)/3 = 26/9
    #   bar5 (105,104,104.5): TR = max(1, |105−103|, |104−103|) = 2
    #     ATR5 = (2·(26/9) + 2)/3 = 70/27
    state = AtrState(period=3)
    for high, low, close in ((102, 100, 101), (103, 101, 102), (101, 99, 100)):
        state.update(high=high, low=low, close=close)
    assert state.update(high=104, low=100, close=103) == pytest.approx(26 / 9)
    assert state.update(high=105, low=104, close=104.5) == pytest.approx(70 / 27)
    assert state.observations == 5


def test_first_true_range_is_the_span_and_never_invents_a_prior_close():
    # The session's first candle has no previous close in scope: its TR is
    # H−L = 10, whatever yesterday did. Bar2 then gaps down hard, so if a
    # cross-session close had leaked in, TR2 would differ.
    state = AtrState(period=2)
    assert state.update(high=110, low=100, close=105) is None
    value = state.update(high=105, low=90, close=91)
    # TR2 = max(15, |105−105|, |90−105|) = 15 → ATR2 = (10+15)/2
    assert value == pytest.approx(12.5)


def test_true_range_takes_the_largest_of_the_three_spans():
    # Gap up: |H − prev close| beats the bar's own range.
    gap_up = AtrState(period=2)
    gap_up.update(high=100.5, low=100, close=100.0)  # TR = 0.5
    # bar2: max(4, |112−100|, |108−100|) = 12 → ATR2 = (0.5+12)/2
    assert gap_up.update(high=112, low=108, close=110) == pytest.approx((0.5 + 12) / 2)
    # Gap down: |L − prev close| beats it instead:
    # max(7, |95−110|, |88−110|) = 22 → ATR3 = (6.25 + 22)/2
    assert gap_up.update(high=95, low=88, close=90) == pytest.approx((6.25 + 22) / 2)


def test_flat_bars_hold_a_constant_atr():
    # Every bar (101, 100, 100.5): TR = max(1, 0.5, 0.5) = 1 forever, so the
    # seed and every recursion step stay exactly 1.
    state = AtrState(period=3)
    for _ in range(7):
        value = state.update(high=101, low=100, close=100.5)
    assert value == pytest.approx(1.0)
    assert state.value == pytest.approx(1.0)


def test_missing_minutes_are_absent_not_interpolated():
    # ATR counts fed candles, not wall-clock minutes: with period=4 the first
    # reading is on the fourth *fed* candle even though four wall-clock minutes
    # were skipped between three of them.
    state = AtrState(period=4)
    readings = [
        state.update(high=102, low=100, close=101),
        state.update(high=103, low=101, close=102),
        state.update(high=104, low=102, close=103),
        state.update(high=105, low=103, close=104),
    ]
    assert readings[:3] == [None, None, None]
    assert readings[3] == pytest.approx(2.0)  # four TRs of 2 each
    assert state.observations == 4


# ── validation ─────────────────────────────────────────────────────────────


def test_period_below_two_is_rejected():
    with pytest.raises(ValueError, match="ATR period must be >= 2"):
        AtrState(period=1)
    AtrState(period=2)  # the boundary itself is fine


def test_bad_prices_surface_as_data_problems():
    state = AtrState(period=3)
    with pytest.raises(ValueError, match="ATR close must be a positive finite number"):
        state.update(high=101, low=100, close=0)
    with pytest.raises(ValueError, match="ATR low must be a positive finite number"):
        state.update(high=101, low=-1, close=100)
    with pytest.raises(ValueError, match="ATR high must be a positive finite number"):
        state.update(high=float("nan"), low=100, close=100)
    with pytest.raises(ValueError, match="ATR high must be a positive finite number"):
        state.update(high=float("inf"), low=100, close=100)
    with pytest.raises(ValueError, match="ATR high must be a number"):
        state.update(high=None, low=100, close=100)
    # None of the bad feeds advanced the series.
    assert state.observations == 0


def test_high_below_low_is_corrupt_not_a_negative_range():
    state = AtrState(period=3)
    with pytest.raises(ValueError, match="below low"):
        state.update(high=99, low=100, close=100)
    assert state.observations == 0
    # A zero-range bar (high == low) is odd but never negative: accepted.
    assert state.update(high=100, low=100, close=100) is None


# ── independent reference walk ─────────────────────────────────────────────


def _walk_bars(count: int) -> list[tuple[float, float, float]]:
    """Deterministic (high, low, close) triples with natural gaps and shapes."""
    bars: list[tuple[float, float, float]] = []
    for index in range(count):
        close = 100.0 + ((index * 7) % 23) - 11 + (index % 3) * 0.5
        high = close + 0.8 + (index % 4) * 0.3
        low = close - 0.7 - (index % 5) * 0.2
        bars.append((round(high, 4), round(low, 4), round(close, 4)))
    return bars


def _reference_atr(bars: list[tuple[float, float, float]], period: int) -> list[float | None]:
    """ATR series written from the module docstring, not from the module."""
    true_ranges: list[float] = []
    prev_close: float | None = None
    for high, low, close in bars:
        if prev_close is None:
            span = high - low
        else:
            span = max(high - low, abs(high - prev_close), abs(low - prev_close))
        prev_close = close
        true_ranges.append(span)
    out: list[float | None] = [None] * len(bars)
    if len(true_ranges) < period:
        return out
    value = sum(true_ranges[:period]) / period
    out[period - 1] = value
    for index in range(period, len(true_ranges)):
        value = ((period - 1) * value + true_ranges[index]) / period
        out[index] = value
    return out


@pytest.mark.parametrize("period", [3, DEFAULT_ATR_PERIOD])
def test_streaming_atr_matches_an_independent_reference_on_a_walk(period: int):
    # Two implementations of one definition must agree step by step — the house
    # rule from the RSI and MACD tests, applied to the same walk shape.
    bars = _walk_bars(60)
    expected = _reference_atr(bars, period)
    state = AtrState(period=period)
    for index, (high, low, close) in enumerate(bars):
        value = state.update(high=high, low=low, close=close)
        if expected[index] is None:
            assert value is None
        else:
            assert value == pytest.approx(expected[index], rel=1e-12, abs=1e-12)


def test_warmup_timeline_is_the_period_th_candle():
    # The 13th fed candle is still silent with period=14; the 14th speaks.
    state = AtrState(period=DEFAULT_ATR_PERIOD)
    bars = _walk_bars(DEFAULT_ATR_PERIOD)
    for high, low, close in bars[:-1]:
        assert state.update(high=high, low=low, close=close) is None
    high, low, close = bars[-1]
    assert state.update(high=high, low=low, close=close) is not None
    assert state.initialized is True
    assert state.observations == DEFAULT_ATR_PERIOD


# ── checkpoints ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("fed_before_checkpoint", [3, 7])
def test_checkpoint_roundtrip_resumes_the_exact_series(fed_before_checkpoint: int):
    # Both mid-seed (3 of 5: the seed list must survive) and initialized (7 of
    # 5: value + prev_close must survive) round-trips continue identically.
    bars = _walk_bars(12)
    state = AtrState(period=5)
    for high, low, close in bars[:fed_before_checkpoint]:
        state.update(high=high, low=low, close=close)
    restored = AtrState.from_dict(state.to_dict())
    assert restored.period == 5
    assert restored.observations == fed_before_checkpoint
    for high, low, close in bars[fed_before_checkpoint:]:
        live_value = state.update(high=high, low=low, close=close)
        restored_value = restored.update(high=high, low=low, close=close)
        if live_value is None:
            assert restored_value is None
        else:
            assert restored_value == pytest.approx(live_value, rel=1e-12, abs=1e-12)


def test_checkpoint_carries_the_previous_close():
    # The TR of the first candle after a restore needs the real previous close;
    # a missing one would silently fall back to H−L and drift the average.
    state = AtrState(period=2)
    state.update(high=100.5, low=100, close=100.2)  # TR = 0.5
    restored = AtrState.from_dict(state.to_dict())
    # Next bar gaps up: with prev_close=100.2 the TR is |112−100.2| = 11.8.
    live = state.update(high=112, low=108, close=110)
    replayed = restored.update(high=112, low=108, close=110)
    assert live == pytest.approx((0.5 + 11.8) / 2)
    assert replayed == pytest.approx(live)
