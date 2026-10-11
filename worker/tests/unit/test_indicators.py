"""Unit tests for the indicator definitions in spec §9.

Expected values are computed by hand in the test, independent of the
implementation, so a formula change cannot quietly rewrite the truth.
"""

from __future__ import annotations

import pytest

from worker.analytics.indicators import (
    EmaState,
    IndicatorRegistry,
    InstrumentIndicators,
    RelativeVolumeState,
    VwapState,
)

# ── VWAP ────────────────────────────────────────────────────────────────────


def test_vwap_uses_typical_price_weighted_by_volume():
    # Three candles: TP = (110+90+100)/3 = 100, (120+100+110)/3 = 110, (104+96+100)/3 = 100
    vwap = VwapState()
    vwap.update(110, 90, 100, volume=1000)
    vwap.update(120, 100, 110, volume=2000)
    vwap.update(104, 96, 100, volume=1000)
    expected = (100 * 1000 + 110 * 2000 + 100 * 1000) / 4000
    assert vwap.value == pytest.approx(expected)
    assert vwap.value == pytest.approx(105.0)


def test_vwap_ignores_zero_volume_candles():
    vwap = VwapState()
    vwap.update(100, 100, 100, volume=0)
    assert vwap.value is None
    vwap.update(120, 100, 110, volume=500)
    assert vwap.value == pytest.approx(110.0)


def test_vwap_resets_between_sessions():
    vwap = VwapState()
    vwap.update(200, 100, 150, volume=1000)
    assert vwap.value is not None
    vwap.reset()
    assert vwap.value is None
    vwap.update(60, 40, 50, volume=1000)
    assert vwap.value == pytest.approx(50.0)  # yesterday's 150 is gone


def test_vwap_survives_serialization_roundtrip():
    vwap = VwapState()
    vwap.update(110, 90, 100, volume=700)
    restored = VwapState.from_dict(vwap.to_dict())
    assert restored.value == pytest.approx(vwap.value)


# ── EMA ─────────────────────────────────────────────────────────────────────


def test_ema_seeds_with_sma_of_first_period_closes():
    ema = EmaState(period=3)
    assert ema.update(10) is None
    assert ema.update(20) is None
    assert ema.update(30) == pytest.approx(20.0)  # SMA(10,20,30)


def test_ema_recursion_matches_alpha_formula():
    ema = EmaState(period=3)  # alpha = 0.5
    for close in (10, 20, 30):
        ema.update(close)
    # EMA4 = 0.5*40 + 0.5*20 = 30
    assert ema.update(40) == pytest.approx(30.0)
    # EMA5 = 0.5*50 + 0.5*30 = 40
    assert ema.update(50) == pytest.approx(40.0)


def test_ema_period_nine_hand_computed_series():
    closes = [101, 102, 103, 104, 105, 106, 107, 108, 109, 110]
    ema = EmaState(period=9)
    value = None
    for close in closes:
        value = ema.update(close)
    alpha = 2 / 10
    seed = sum(closes[:9]) / 9  # 105
    expected = alpha * closes[9] + (1 - alpha) * seed
    assert value == pytest.approx(expected)
    assert value == pytest.approx(106.0)


def test_ema_rejects_nonpositive_close():
    with pytest.raises(ValueError):
        EmaState(period=5).update(0)


def test_ema_warm_flag_only_after_seed():
    ema = EmaState(period=4)
    ema.update(10)
    assert not ema.warm
    for close in (11, 12, 13):
        ema.update(close)
    assert ema.warm


# ── relative volume ─────────────────────────────────────────────────────────


def build_volume_history(lookback: int, volumes: list[int]):
    state = RelativeVolumeState(lookback=lookback)
    for volume in volumes:
        state.push(volume)
    return state


def test_relative_volume_excludes_current_candle_from_baseline():
    # baseline = 20 candles of 1000, current = 1700 → RV = 1.7
    state = build_volume_history(20, [1000] * 20 + [1700])
    assert state.baseline_average == pytest.approx(1000.0)
    assert state.value() == pytest.approx(1.7)


def test_relative_volume_undefined_until_full_baseline_exists():
    state = build_volume_history(20, [1000] * 20)  # 20 values = baseline only
    assert not state.ready
    assert state.value() is None
    state.push(1500)
    assert state.ready
    assert state.value() == pytest.approx(1.5)


def test_relative_volume_baseline_uses_previous_lookback_after_roll():
    volumes = [100] * 10 + [200] * 10 + [900]
    state = build_volume_history(20, volumes)
    # baseline is the 20 candles before the current one: 100×0 + 200×10 + 100×10
    assert state.baseline_average == pytest.approx(150.0)
    assert state.value() == pytest.approx(6.0)


def test_zero_baseline_yields_no_relative_volume_instead_of_infinity():
    state = build_volume_history(3, [0, 0, 0, 500])
    assert state.baseline_average == pytest.approx(0.0)
    assert state.value() is None


def test_relative_volume_rejects_negative_volume():
    with pytest.raises(ValueError):
        build_volume_history(3, [-1])


# ── combined engine: warm-up, session separation, recovery ─────────────────


def test_engine_requires_full_history_before_reporting_warm():
    engine = InstrumentIndicators(fast_period=9, slow_period=20, volume_lookback=20)
    values = None
    for index in range(20):
        values = engine.on_candle(high=100 + index, low=99 + index, close=100 + index, volume=1000)
    assert values is not None
    assert values.warm is False  # 20 candles: EMA20 seeded, RV needs 21
    values = engine.on_candle(high=121, low=120, close=121, volume=1500)
    assert values.warm is True
    assert values.relative_volume == pytest.approx(1.5)


def test_historical_warmup_does_not_pollute_session_vwap():
    engine = InstrumentIndicators(fast_period=3, slow_period=5, volume_lookback=3)
    # yesterday's candles: high prices, must not enter today's VWAP
    for _ in range(6):
        engine.on_candle(high=500, low=500, close=500, volume=1000, in_current_session=False)
    assert engine.vwap.value is None
    engine.on_candle(high=110, low=90, close=100, volume=1000, in_current_session=True)
    assert engine.vwap.value == pytest.approx(100.0)


def test_engine_roundtrip_reproduces_next_value_exactly():
    engine = InstrumentIndicators(fast_period=9, slow_period=20, volume_lookback=20)
    for index in range(25):
        engine.on_candle(high=100 + index, low=99 + index, close=100 + index, volume=1000 + index)
    restored = InstrumentIndicators.from_dict(engine.to_dict())
    for close, volume in ((130.0, 2200), (131.0, 2300)):
        live = engine.on_candle(high=close, low=close - 1, close=close, volume=volume)
        replayed = restored.on_candle(high=close, low=close - 1, close=close, volume=volume)
        assert replayed.ema_fast == pytest.approx(live.ema_fast)
        assert replayed.ema_slow == pytest.approx(live.ema_slow)
        assert replayed.vwap == pytest.approx(live.vwap)
        assert replayed.relative_volume == pytest.approx(live.relative_volume)


def test_registry_isolates_instruments_and_serializes():
    registry = IndicatorRegistry(fast_period=3, slow_period=5, volume_lookback=3)
    for close in (10, 11, 12, 13, 14, 15):
        registry.get(1).on_candle(high=close, low=close, close=close, volume=100)
    for close in (50, 60, 70, 80, 90, 100):
        registry.get(2).on_candle(high=close, low=close, close=close, volume=200)
    assert registry.get(1).vwap.value != registry.get(2).vwap.value

    restored = IndicatorRegistry.from_dict(registry.to_dict())
    assert restored.get(1).vwap.value == pytest.approx(registry.get(1).vwap.value)
    assert restored.get(2).slow.value == pytest.approx(registry.get(2).slow.value)
    assert registry.instrument_ids == {1, 2}


def test_reset_session_vwap_keeps_ema_and_volume_history():
    engine = InstrumentIndicators(fast_period=3, slow_period=5, volume_lookback=3)
    for close in (10, 11, 12, 13, 14):
        engine.on_candle(high=close, low=close, close=close, volume=1000)
    before_fast = engine.fast.value
    engine.reset_session_vwap()
    assert engine.vwap.value is None
    assert engine.fast.value == before_fast
    assert engine.volume.ready
