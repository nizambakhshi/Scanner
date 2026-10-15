"""Unit tests for the indicator definitions in spec §9.

Expected values are computed by hand in the test, independent of the
implementation, so a formula change cannot quietly rewrite the truth.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from worker.analytics.indicators import (
    EmaState,
    IndicatorRegistry,
    IndicatorValues,
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


# ── EMA: the signed mode the MACD signal line needs ─────────────────────────


def test_signed_ema_accepts_zero_and_negative_values():
    ema = EmaState(period=2, require_positive=False)
    assert ema.update(0.0) is None
    assert ema.update(-4.0) == pytest.approx(-2.0)  # SMA(0, −4)
    # α=2/3: 2/3·(−6) + 1/3·(−2) = −14/3
    assert ema.update(-6.0) == pytest.approx(-14 / 3)
    with pytest.raises(ValueError):
        ema.update(float("nan"))
    with pytest.raises(ValueError):
        ema.update(float("inf"))


def test_signed_ema_at_exact_zero_uses_the_real_zero():
    # The truthiness trap: a signed EMA sitting at exactly 0.0 must recurse
    # from 0.0, not from the incoming value — `value or close` would pick the
    # close and corrupt the recursion (price EMAs can never reach 0.0, so this
    # branch only exists for the MACD signal line).
    ema = EmaState(period=2, require_positive=False)
    ema.update(0.0)
    ema.update(0.0)  # value is now exactly 0.0
    # 2/3·10 + 1/3·0 = 20/3 (with the trap this would read 10.0)
    assert ema.update(10.0) == pytest.approx(20 / 3)


def test_ema_require_positive_flag_round_trips_and_defaults_to_price_mode():
    signed = EmaState(period=2, require_positive=False)
    signed.update(0.0)
    restored = EmaState.from_dict(signed.to_dict())
    assert restored.require_positive is False
    restored.update(-1.0)  # signed mode survived the checkpoint
    legacy = dict(signed.to_dict())
    del legacy["require_positive"]
    assert EmaState.from_dict(legacy).require_positive is True  # pre-flag checkpoints


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


# ── MACD inside the instrument engine (Phase 2B) ────────────────────────────

SESSION_OPEN = datetime(2026, 10, 15, 9, 15)


def macd_engine() -> InstrumentIndicators:
    """Small periods so the MACD is warm within a handful of buckets."""
    return InstrumentIndicators(
        fast_period=3,
        slow_period=5,
        volume_lookback=3,
        rsi_period=3,
        macd_fast_period=2,
        macd_slow_period=3,
        macd_signal_period=2,
    )


def feed_session(engine: InstrumentIndicators, minutes: range, *, start: float = 100.0) -> None:
    for minute in minutes:
        close = start + minute
        engine.on_candle(
            high=close + 1,
            low=close - 1,
            close=close,
            volume=1000 + minute,
            candle_start=SESSION_OPEN + timedelta(minutes=minute),
        )


def test_macd_and_rsi_advance_on_the_same_bucket_closes():
    # One partition, not two: both readings move on exactly the candles whose
    # start is the fifth minute of a bucket (09:19, 09:24, …) and on no others.
    engine = macd_engine()
    feed_session(engine, range(20))
    assert engine.rsi.observations == 4
    assert engine.macd.observations == 4
    assert engine.macd.current.ready  # slows 3 + signal 2 − 1 = 4 buckets


def test_macd_never_advances_on_a_minute_inside_the_bucket():
    engine = macd_engine()
    feed_session(engine, range(4))  # 09:15–09:18: four minutes, no bucket close
    assert engine.macd.observations == 0
    assert engine.rsi.observations == 0
    assert engine.values(close=104.0).macd is None
    engine.on_candle(
        high=105,
        low=104,
        close=104.0,
        volume=1100,
        candle_start=SESSION_OPEN + timedelta(minutes=4),  # 09:19 closes the bucket
    )
    assert engine.macd.observations == 1


def test_a_bucket_whose_closing_minute_never_arrived_is_absent_not_filled():
    # The feed lost 09:24 exactly: the 09:15–09:20 and 09:25–09:30 buckets close
    # normally, the middle one simply never happens — no interpolation, and the
    # observation count follows the tape rather than the wall clock.
    gapped = macd_engine()
    for minute in range(15):
        if minute == 9:  # the candle that would have closed the 09:20–09:25 bucket
            continue
        close = 100.0 + minute
        gapped.on_candle(
            high=close + 1,
            low=close - 1,
            close=close,
            volume=1000 + minute,
            candle_start=SESSION_OPEN + timedelta(minutes=minute),
        )
    assert gapped.macd.observations == 2  # 09:19 and 09:29 only
    assert gapped.rsi.observations == 2


def test_session_reset_clears_rsi_and_macd_but_keeps_ema_and_volume():
    engine = macd_engine()
    feed_session(engine, range(20))
    assert engine.rsi.value is not None
    assert engine.macd.current.ready
    before_fast = engine.fast.value
    before_slow = engine.slow.value

    engine.reset_session_vwap()

    assert engine.vwap.value is None
    assert engine.rsi.value is None and engine.rsi.observations == 0
    assert engine.macd.observations == 0
    assert engine.macd.current.macd is None and engine.macd.current.signal is None
    assert engine.macd.current.ready is False
    assert engine.fast.value == before_fast
    assert engine.slow.value == before_slow
    assert engine.volume.ready
    # …and it re-warms from the next session's own buckets.
    feed_session(engine, range(20), start=200.0)
    assert engine.macd.observations == 4
    assert engine.macd.current.ready


def test_engine_checkpoint_roundtrip_includes_macd():
    engine = macd_engine()
    feed_session(engine, range(20))
    restored = InstrumentIndicators.from_dict(engine.to_dict())
    for minute in range(20, 30):  # 09:35 onwards: two more bucket closes (…:39, …:44)
        close = 100.0 + minute
        live = engine.on_candle(
            high=close + 1,
            low=close - 1,
            close=close,
            volume=1000 + minute,
            candle_start=SESSION_OPEN + timedelta(minutes=minute),
        )
        replayed = restored.on_candle(
            high=close + 1,
            low=close - 1,
            close=close,
            volume=1000 + minute,
            candle_start=SESSION_OPEN + timedelta(minutes=minute),
        )
    assert replayed.macd == pytest.approx(live.macd)
    assert replayed.macd_signal == pytest.approx(live.macd_signal)
    assert replayed.macd_histogram == pytest.approx(live.macd_histogram)


def test_legacy_checkpoint_without_macd_restores_fresh():
    engine = macd_engine()
    feed_session(engine, range(20))
    payload = engine.to_dict()
    del payload["macd"]  # as written by any pre-Phase-2B checkpoint
    restored = InstrumentIndicators.from_dict(payload)
    assert restored.macd.observations == 0
    assert restored.macd.current.macd is None
    assert restored.macd.fast_period == 12  # module defaults
    # The state that existed before the MACD came back untouched.
    assert restored.rsi.value == pytest.approx(engine.rsi.value)
    assert restored.vwap.value == pytest.approx(engine.vwap.value)
    assert restored.fast.value == pytest.approx(engine.fast.value)


def test_registry_carries_macd_periods_and_serializes():
    registry = IndicatorRegistry(
        fast_period=3,
        slow_period=5,
        volume_lookback=3,
        macd_fast_period=5,
        macd_slow_period=9,
        macd_signal_period=4,
    )
    engine = registry.get(7)
    assert (engine.macd.fast_period, engine.macd.slow_period, engine.macd.signal_period) == (5, 9, 4)
    engine.on_candle(high=101, low=99, close=100, volume=10)

    restored = IndicatorRegistry.from_dict(registry.to_dict())
    assert restored.get(7).macd.fast_period == 5  # existing state kept its periods
    newly = restored.get(8)  # engines built after the restore use registry periods
    assert (newly.macd.fast_period, newly.macd.slow_period, newly.macd.signal_period) == (5, 9, 4)


def test_legacy_registry_checkpoint_without_macd_keys():
    registry = IndicatorRegistry(fast_period=3, slow_period=5, volume_lookback=3)
    registry.get(1).on_candle(high=101, low=99, close=100, volume=10)
    payload = registry.to_dict()
    for key in ("macd_fast_period", "macd_slow_period", "macd_signal_period"):
        del payload[key]
    payload["instruments"]["1"].pop("macd")
    restored = IndicatorRegistry.from_dict(payload)
    engine = restored.get(1)
    assert engine.macd.fast_period == 12  # defaults, not a crash
    assert engine.macd.observations == 0
    assert engine.candles_seen == 1  # the rest of the checkpoint came back


def test_indicator_values_keep_the_pre_macd_constructor_shape():
    # Callers that build IndicatorValues by hand (tests, tools) must keep
    # working unchanged: every MACD field is optional and reads as absent.
    values = IndicatorValues(
        close=100.0,
        vwap=None,
        ema_fast=None,
        ema_slow=None,
        average_volume=None,
        relative_volume=None,
        candles_seen=0,
        warm=False,
    )
    assert (values.macd, values.macd_signal, values.macd_histogram) == (None, None, None)


def test_indicator_values_regression_pin_from_before_the_macd_phase():
    """The exact values the engine produced *before* the MACD foundation landed.

    Captured from the pre-change implementation (2026-10-15) over a deterministic
    80-candle session. This pin exists to prove the MACD work changed none of the
    numbers the scanner, watchlist, alerts and dashboard already rely on — and
    that the new fields correctly read as absent while their warm-up is short.
    """
    engine = InstrumentIndicators(fast_period=9, slow_period=20, volume_lookback=20)
    mid = end = None
    for index in range(80):
        close = 100.0 + ((index * 7) % 19) - 9
        values = engine.on_candle(
            high=close + 0.5,
            low=close - 0.5,
            close=close,
            volume=1000 + (index * 13) % 97,
            candle_start=SESSION_OPEN + timedelta(minutes=index),
        )
        if index == 39:
            mid = values
        if index == 79:
            end = values
    assert mid is not None and end is not None
    assert mid.vwap == pytest.approx(99.73745155270588, rel=1e-12)
    assert mid.ema_fast == pytest.approx(98.69540366717939, rel=1e-12)
    assert mid.ema_slow == pytest.approx(99.42567493049877, rel=1e-12)
    assert mid.average_volume == pytest.approx(1050.4, rel=1e-12)
    assert mid.relative_volume == pytest.approx(0.9729626808834729, rel=1e-12)
    assert mid.rsi_5m is None and mid.warm is True
    assert end.vwap == pytest.approx(99.8458589598137, rel=1e-12)
    assert end.ema_fast == pytest.approx(98.56615270869261, rel=1e-12)
    assert end.ema_slow == pytest.approx(99.29730594429992, rel=1e-12)
    assert end.average_volume == pytest.approx(1046.6, rel=1e-12)
    assert end.relative_volume == pytest.approx(1.0099369386585133, rel=1e-12)
    assert end.rsi_5m == pytest.approx(44.92440604751619, rel=1e-12)
    # 16 buckets have completed; the default MACD needs 26 — absent, not zero.
    assert mid.macd is None and end.macd is None
    assert end.macd_signal is None and end.macd_histogram is None
