"""Candle-engine tests: boundaries, volume deltas, gaps, duplicates, latency."""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.conftest import SESSION_CLOSE, SESSION_OPEN, minute, tick
from worker.marketdata.candle_builder import CandleEngine, DataQuality
from worker.models import Tick


def new_engine(**kwargs) -> CandleEngine:
    return CandleEngine(session_open=SESSION_OPEN, session_close=SESSION_CLOSE, **kwargs)


def finalize(engine: CandleEngine, offset: int, *, extra_seconds: int = 4):
    return engine.finalize_due(minute(offset) + timedelta(minutes=1, seconds=extra_seconds))


# ── OHLC construction ───────────────────────────────────────────────────────


def test_candle_ohlc_uses_first_high_low_last_trade_prices():
    engine = CandleEngine()
    for price, second in ((101, 0), (105, 10), (99, 20), (103, 40), (104, 55)):
        engine.on_tick(tick(price=price, offset_minutes=0, second=second, cumulative_volume=None,
                            last_traded_quantity=100))
    candles = finalize(engine, 0)
    assert len(candles) == 1
    candle = candles[0]
    assert (candle.open, candle.high, candle.low, candle.close) == (101, 105, 99, 104)
    assert candle.candle_start == SESSION_OPEN
    assert candle.is_complete is True
    assert candle.tick_count == 5


def test_incomplete_candle_is_exposed_as_forming_and_not_finalized():
    engine = new_engine(grace_seconds=3)
    engine.on_tick(tick(price=100, offset_minutes=0, second=10))
    forming = engine.forming_candle(1)
    assert forming is not None
    assert forming.is_complete is False
    assert forming.data_quality == DataQuality.INCOMPLETE
    # 09:15:30 is inside the minute; nothing may be finalized yet
    assert engine.finalize_due(minute(0) + timedelta(seconds=45)) == []


def test_grace_period_defers_finalization():
    engine = new_engine(grace_seconds=5)
    engine.on_tick(tick(price=100, offset_minutes=0, second=30))
    assert engine.finalize_due(minute(1)) == []  # boundary reached, grace not
    candles = engine.finalize_due(minute(1) + timedelta(seconds=6))
    assert [candle.candle_start for candle in candles] == [minute(0)]


# ── cumulative volume handling (spec §8.2 critical requirement) ─────────────


def test_session_start_cumulative_volume_becomes_candle_volume():
    engine = new_engine()
    for price, cumulative in ((100, 1000), (101, 2500), (102, 6100)):
        engine.on_tick(tick(price=price, offset_minutes=0, cumulative_volume=cumulative))
    candles = finalize(engine, 0)
    # day-cumulative values must never be summed: 1000 + (2500-1000) + (6100-2500)
    assert candles[0].volume == 6100


def test_mid_session_connect_marks_partial_instead_of_inventing_volume():
    engine = new_engine()
    # 10:55 — the worker connected mid-day, so volume traded before connect is unknown
    engine.on_tick(tick(price=100, offset_minutes=100, second=30, cumulative_volume=540_000))
    candles = finalize(engine, 100)
    assert candles[0].volume == 0
    assert candles[0].data_quality == DataQuality.PARTIAL
    assert [gap.reason for gap in engine.gaps] == ["RECONNECT"]
    # the next minute can be measured properly because a baseline now exists
    engine.on_tick(tick(price=101, offset_minutes=101, second=5, cumulative_volume=540_000))
    engine.on_tick(tick(price=102, offset_minutes=101, second=40, cumulative_volume=552_000))
    second_minute = finalize(engine, 101)
    assert second_minute[0].volume == 12_000
    assert second_minute[0].data_quality == DataQuality.OK


def test_counter_reset_produces_no_negative_volume():
    engine = new_engine()
    engine.on_tick(tick(price=100, offset_minutes=0, cumulative_volume=900_000))
    engine.on_tick(tick(price=101, offset_minutes=0, second=30, cumulative_volume=12_000))
    candles = finalize(engine, 0)
    assert candles[0].volume == 12_000  # re-baselined from zero, not -888000
    assert engine.stats.counter_resets == 1


def test_ltp_only_ticks_flag_partial_volume():
    engine = new_engine()
    engine.on_tick(tick(price=100, offset_minutes=0, cumulative_volume=None, last_traded_quantity=40))
    engine.on_tick(tick(price=101, offset_minutes=0, second=30, cumulative_volume=None, last_traded_quantity=60))
    candles = finalize(engine, 0)
    assert candles[0].volume == 100
    assert candles[0].data_quality == DataQuality.PARTIAL


# ── gaps, duplicates, ordering ──────────────────────────────────────────────


def test_no_trade_minute_creates_a_gap_not_a_zero_candle():
    engine = new_engine()
    engine.on_tick(tick(price=100, offset_minutes=0))
    engine.on_tick(tick(price=105, offset_minutes=3))
    finalize(engine, 3)
    starts = [candle.candle_start for candle in engine.finalized_candles(1)]
    assert minute(1) not in starts and minute(2) not in starts
    assert {gap.reason for gap in engine.gaps} == {"NO_TICKS"}
    assert len(engine.gaps) == 2
    assert starts == [minute(0), minute(3)]


def test_duplicate_ticks_are_ignored():
    engine = new_engine()
    first = tick(price=100, offset_minutes=0, second=5, cumulative_volume=1000)
    engine.on_tick(first)
    engine.on_tick(first)
    engine.on_tick(first)
    assert engine.stats.ticks_duplicate == 2
    candles = finalize(engine, 0)
    assert candles[0].tick_count == 1
    assert candles[0].volume == 1000


def test_tick_after_finalization_is_counted_as_late_correction():
    engine = new_engine()
    engine.on_tick(tick(price=100, offset_minutes=0))
    finalize(engine, 0)
    before = engine.finalized_candles(1)[0].close
    engine.on_tick(tick(price=999, offset_minutes=0, second=59))
    assert engine.stats.late_corrections == 1
    assert engine.finalized_candles(1)[0].close == before  # finalized history intact


def test_out_of_order_ticks_still_set_extremes_and_flag_partial():
    engine = new_engine()
    engine.on_tick(tick(price=100, offset_minutes=0, second=30))
    engine.on_tick(tick(price=120, offset_minutes=0, second=10))  # earlier stamp, later arrival
    engine.on_tick(tick(price=90, offset_minutes=0, second=20))
    candles = finalize(engine, 0)
    candle = candles[0]
    assert candle.open == 100  # first *arrived* valid print
    assert candle.high == 120 and candle.low == 90
    assert engine.stats.ticks_out_of_order == 1
    assert candle.data_quality == DataQuality.PARTIAL


def test_ohlc_sanity_holds_after_construction():
    engine = new_engine()
    for price in (110, 108, 115, 102, 109):
        engine.on_tick(tick(price=price, offset_minutes=0, second=5))
    candle = finalize(engine, 0)[0]
    assert candle.high >= max(candle.open, candle.close)
    assert candle.low <= min(candle.open, candle.close)


# ── session boundaries ──────────────────────────────────────────────────────


def test_pre_open_and_post_close_ticks_are_rejected():
    engine = new_engine()
    early = Tick(
        instrument_id=1,
        instrument_token="2885",
        exchange="NSE",
        exchange_timestamp=SESSION_OPEN - timedelta(minutes=10),
        received_at=SESSION_OPEN,
        last_traded_price=100,
    )
    engine.on_tick(early)
    assert engine.stats.ticks_rejected == 1
    engine.on_tick(tick(price=100, offset_minutes=0))
    late = Tick(
        instrument_id=1,
        instrument_token="2885",
        exchange="NSE",
        exchange_timestamp=SESSION_CLOSE + timedelta(minutes=1),
        received_at=SESSION_CLOSE,
        last_traded_price=101,
    )
    engine.on_tick(late)
    assert engine.stats.ticks_rejected == 2
    assert engine.forming_candle(1) is not None  # the valid minute survived


def test_new_trading_day_resets_instrument_state():
    # No session window here on purpose: the engine is reused across two days so
    # the rollover behaviour itself is what is under test.
    engine = CandleEngine()
    engine.on_tick(tick(price=100, offset_minutes=0, cumulative_volume=500_000))
    finalize(engine, 0)
    assert engine.finalized_candles(1)[0].trading_date == SESSION_OPEN.date()

    tomorrow = SESSION_OPEN + timedelta(days=1)
    fresh = Tick(
        instrument_id=1,
        instrument_token="2885",
        exchange="NSE",
        exchange_timestamp=tomorrow,
        received_at=tomorrow,
        last_traded_price=90,
        cumulative_volume=1_000,
    )
    engine.on_tick(fresh)
    candles = engine.finalize_due(tomorrow + timedelta(minutes=1, seconds=5))
    assert candles[0].trading_date == tomorrow.date()
    assert candles[0].close == 90
    # the day counter reset, but volume traded before the first tick of that
    # bucket is unknown → explicitly PARTIAL, never a guessed 1_000
    assert candles[0].data_quality == DataQuality.PARTIAL

    next_day = Tick(
        instrument_id=1,
        instrument_token="2885",
        exchange="NSE",
        exchange_timestamp=tomorrow + timedelta(minutes=1, seconds=10),
        received_at=tomorrow,
        last_traded_price=93,
        cumulative_volume=3_000,
    )
    engine.on_tick(next_day)
    following = engine.finalize_due(tomorrow + timedelta(minutes=2, seconds=5))
    assert following[0].volume == 2_000  # delta against the new day's baseline


def test_finalize_all_flushes_the_last_partial_minute():
    engine = new_engine()
    engine.on_tick(tick(price=100, offset_minutes=0))
    engine.on_tick(tick(price=101, offset_minutes=374, second=30))
    flushed = engine.finalize_all(SESSION_CLOSE)
    assert [candle.candle_start for candle in flushed] == [minute(374)]
    assert flushed[0].close == 101


def test_grace_seconds_validation():
    with pytest.raises(ValueError):
        CandleEngine(grace_seconds=99)
