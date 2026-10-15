"""Unit tests for the MACD engine: seeding, warm-up, exactness, gaps, checkpoints.

Every expected number is hand-computed from the definition in the module
docstring — never copied from the implementation's own output — so a formula
change fails here instead of silently rewriting the reading the intraday engine
will later act on. Long walks are additionally compared against an independent
reference implementation written from the definition.

The cases map 1:1 to the Phase 2B contract: initialization (SMA seed for three
EMAs), warm-up (26th/34th bucket timeline), known values, flat prices, missing
buckets, session resets (engine level, in test_indicators), reconnect/checkpoint
round-trips, and the rebuild pattern a recovery replay produces.
"""

from __future__ import annotations

import pytest

from worker.analytics.macd import DEFAULT_FAST_PERIOD, DEFAULT_SLOW_PERIOD, MacdState

# ── small fully hand-computed cases ────────────────────────────────────────


def test_macd_line_is_fast_minus_slow_after_both_seed():
    # fast=2 (α=2/3), slow=3 (α=1/2), closes 10..15.
    # fast: seed(10,11)=10.5; then 11.5, 12.5, 13.5, 14.5
    # slow: seed(10,11,12)=11; then 12, 13, 14
    # macd line exists from the 3rd close: 11.5−11 = 12.5−12 = 13.5−13 = 14.5−14 = 0.5
    state = MacdState(fast_period=2, slow_period=3, signal_period=2)
    readings = [state.update(close) for close in (10.0, 11.0, 12.0, 13.0, 14.0, 15.0)]
    assert readings[0].macd is None and readings[1].macd is None  # slow not seeded
    assert readings[0].signal is None and readings[1].signal is None
    for reading in readings[2:]:
        assert reading.macd == pytest.approx(0.5)
    assert state.min_observations_for_signal == 4  # 3 + 2 − 1


def test_signal_seeds_with_sma_of_first_signal_period_macd_values():
    # Same series: signal EMA (2, α=2/3) over macd [0.5, 0.5, ...]:
    # 3rd close seeds [0.5] → None; 4th close seeds SMA(0.5,0.5) = 0.5; stays 0.5.
    state = MacdState(fast_period=2, slow_period=3, signal_period=2)
    for close in (10.0, 11.0, 12.0):
        state.update(close)
    assert state.current.macd == pytest.approx(0.5)
    assert state.current.signal is None  # one macd value: not seeded yet
    assert state.current.histogram is None
    reading = state.update(13.0)
    assert reading.signal == pytest.approx(0.5)  # SMA of the first two macd values
    assert reading.histogram == pytest.approx(0.0)


def test_macd_recursion_hand_computed_on_a_walk():
    # fast=2 (α=2/3), slow=3 (α=1/2), closes 10, 12, 11, 13, 12, 14.
    # fast: 11; 11; 37/3; 109/9; 361/27      slow: 11; 12; 12; 13
    # macd: (obs 3) 0; (4) 1/3; (5) 1/9; (6) 10/27
    # signal (α=2/3): seed(0, 1/3) = 1/6; then 2/3·(1/9) + 1/3·(1/6) = 7/54;
    #                 then 2/3·(10/27) + 1/3·(7/54) = 47/162
    # histogram (obs 6): 10/27 − 47/162 = 13/162
    state = MacdState(fast_period=2, slow_period=3, signal_period=2)
    readings = [state.update(close) for close in (10.0, 12.0, 11.0, 13.0, 12.0, 14.0)]
    assert readings[2].macd == pytest.approx(0.0)
    assert readings[3].macd == pytest.approx(1 / 3)
    assert readings[4].macd == pytest.approx(1 / 9)
    assert readings[5].macd == pytest.approx(10 / 27)
    assert readings[3].signal == pytest.approx(1 / 6)
    assert readings[4].signal == pytest.approx(7 / 54)
    assert readings[5].signal == pytest.approx(47 / 162)
    assert readings[5].histogram == pytest.approx(13 / 162)


def test_falling_series_carries_negative_macd_through_the_signal_ema():
    # Mirror of the rising case: macd = −0.5 from the 3rd close. The signal EMA
    # must accept signed input (require_positive=False) — that is its whole point.
    state = MacdState(fast_period=2, slow_period=3, signal_period=2)
    readings = [state.update(close) for close in (15.0, 14.0, 13.0, 12.0, 11.0, 10.0)]
    for reading in readings[2:]:
        assert reading.macd == pytest.approx(-0.5)
    assert readings[3].signal == pytest.approx(-0.5)
    assert readings[3].histogram == pytest.approx(0.0)
    assert readings[5].ready is True


# ── warm-up timeline and flat prices ───────────────────────────────────────


def test_readiness_timeline_for_twelve_twenty_six_nine():
    # Classic (12, 26, 9): macd line on the 26th completed bucket, signal and
    # histogram on the 34th. Constant closes make every valid value exactly 0.0,
    # so the None-structure is the only thing moving, and it must be exact.
    state = MacdState()
    assert (state.fast_period, state.slow_period, state.signal_period) == (
        DEFAULT_FAST_PERIOD,
        DEFAULT_SLOW_PERIOD,
        9,
    )
    assert state.min_observations_for_signal == 34
    for index in range(1, 41):
        reading = state.update(100.0)
        if index <= 25:
            assert reading.macd is None and reading.signal is None
        elif index <= 33:
            assert reading.macd == pytest.approx(0.0, abs=1e-9)
            assert reading.signal is None and reading.histogram is None
        else:
            assert reading.macd == pytest.approx(0.0, abs=1e-9)
            assert reading.signal == pytest.approx(0.0, abs=1e-9)
            assert reading.histogram == pytest.approx(0.0, abs=1e-9)
            assert reading.ready is True


def test_flat_prices_give_zero_not_none_once_ready():
    state = MacdState(fast_period=3, slow_period=5, signal_period=3)
    reading = None
    for _ in range(7):  # 5 + 3 − 1
        reading = state.update(250.0)
    assert reading is not None and reading.ready
    assert reading.macd == pytest.approx(0.0, abs=1e-9)
    assert reading.signal == pytest.approx(0.0, abs=1e-9)
    assert reading.histogram == pytest.approx(0.0, abs=1e-9)


# ── gaps and rebuilds ──────────────────────────────────────────────────────


def test_missing_buckets_leave_no_trace_and_are_not_synthesised():
    # A feed gap is an *absent* feed: four closes fed means four observations,
    # and the state equals a twin that was fed the same four closes — nothing
    # was interpolated for the buckets that never arrived.
    gapped = MacdState(fast_period=2, slow_period=3, signal_period=2)
    gapped.update(10.0)
    gapped.update(11.0)
    gapped.update(12.0)
    # three buckets go missing here
    gapped.update(18.0)
    twin = MacdState(fast_period=2, slow_period=3, signal_period=2)
    for close in (10.0, 11.0, 12.0, 18.0):
        twin.update(close)
    assert gapped.observations == 4 == twin.observations
    assert gapped.current == twin.current
    assert gapped.to_dict() == twin.to_dict()


def test_rebuilding_from_the_same_closes_reproduces_the_same_state():
    # What a recovery replay does: the stored buckets are folded again into a
    # *fresh* engine, and the rebuilt state must equal the one the live tape
    # built — otherwise a mid-session restart changes today's readings.
    closes = [100.0 + ((index * 5) % 17) - 8 for index in range(40)]
    live = MacdState(fast_period=3, slow_period=5, signal_period=3)
    for close in closes:
        live.update(close)
    replayed = MacdState(fast_period=3, slow_period=5, signal_period=3)
    for close in closes:
        replayed.update(close)
    assert replayed.to_dict() == live.to_dict()


# ── checkpoint round-trips ─────────────────────────────────────────────────


def test_checkpoint_roundtrip_reproduces_next_values_exactly():
    closes = [100.0 + ((index * 7) % 23) - 11 for index in range(60)]
    state = MacdState()
    for close in closes:
        state.update(close)
    restored = MacdState.from_dict(state.to_dict())
    assert restored.current == state.current
    assert restored.observations == state.observations
    for close in (101.0, 97.0, 103.5, 99.0, 104.0):
        assert restored.update(close) == state.update(close)
    # And the checkpoints stay equal after diverging updates are impossible to
    # hide: the restored state must serialize back to the same payload.
    assert restored.to_dict() == state.to_dict()


def test_checkpoint_carries_the_configured_periods():
    state = MacdState(fast_period=5, slow_period=9, signal_period=4)
    for close in (10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0):
        state.update(close)
    restored = MacdState.from_dict(state.to_dict())
    assert (restored.fast_period, restored.slow_period, restored.signal_period) == (5, 9, 4)
    assert restored.min_observations_for_signal == 12  # 9 + 4 − 1


# ── exactness against an independent reference implementation ─────────────


def _reference_ema(values: list[float], period: int) -> list[float | None]:
    """SMA-seed + α recursion, written from the definition (not from EmaState)."""
    alpha = 2.0 / (period + 1.0)
    out: list[float | None] = [None] * len(values)
    seed: list[float] = []
    ema: float | None = None
    for index, value in enumerate(values):
        if ema is None:
            seed.append(value)
            if len(seed) == period:
                ema = sum(seed) / period
                out[index] = ema
                seed = []
        else:
            ema = alpha * value + (1.0 - alpha) * ema
            out[index] = ema
    return out


def test_streaming_macd_matches_an_independent_reference_on_a_walk():
    # Two implementations of one definition must agree step by step — the
    # house rule from the RSI tests, applied to the same walk shape.
    closes = [100.0 + ((index * 7) % 23) - 11 + (index % 3) * 0.5 for index in range(80)]
    fast, slow, signal = 12, 26, 9
    fast_line = _reference_ema(closes, fast)
    slow_line = _reference_ema(closes, slow)
    macd_line = [
        f - s if f is not None and s is not None else None
        for f, s in zip(fast_line, slow_line, strict=True)
    ]
    signal_input = [value for value in macd_line if value is not None]
    signal_line = _reference_ema(signal_input, signal)
    signal_by_index: list[float | None] = [None] * len(closes)
    cursor = 0
    for index, value in enumerate(macd_line):
        if value is not None:
            signal_by_index[index] = signal_line[cursor]
            cursor += 1

    state = MacdState(fast_period=fast, slow_period=slow, signal_period=signal)
    for index, close in enumerate(closes):
        reading = state.update(close)
        expected_macd = macd_line[index]
        expected_signal = signal_by_index[index]
        if expected_macd is None:
            assert reading.macd is None
        else:
            assert reading.macd == pytest.approx(expected_macd, rel=1e-9, abs=1e-9)
        if expected_signal is None:
            assert reading.signal is None
        else:
            assert reading.signal == pytest.approx(expected_signal, rel=1e-9, abs=1e-9)
            assert reading.histogram == pytest.approx(
                expected_macd - expected_signal, rel=1e-9, abs=1e-9
            )


# ── input validation ───────────────────────────────────────────────────────


def test_construction_rejects_impossible_periods():
    with pytest.raises(ValueError, match="fast period"):
        MacdState(fast_period=1)
    with pytest.raises(ValueError, match="slow period"):
        MacdState(fast_period=12, slow_period=12)
    with pytest.raises(ValueError, match="signal period"):
        MacdState(signal_period=1)


def test_update_rejects_a_missing_close():
    state = MacdState(fast_period=2, slow_period=3, signal_period=2)
    with pytest.raises(ValueError):
        state.update(None)  # type: ignore[arg-type]
