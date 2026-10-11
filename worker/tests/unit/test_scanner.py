"""Scanner qualification tests (spec §10) — conditions, gates, reasons."""

from __future__ import annotations

import pytest

from tests.conftest import minute
from worker.analytics.indicators import IndicatorValues
from worker.analytics.scanner import (
    F_CANDLE_INCOMPLETE,
    F_CLOSE_BELOW_VWAP,
    F_DATA_QUALITY,
    F_EMA_NOT_ALIGNED,
    F_FEED_STALE,
    F_INSUFFICIENT_HISTORY,
    F_OUTSIDE_UNIVERSE,
    F_RELATIVE_VOLUME_LOW,
    F_VOLUME_BASELINE_INVALID,
    F_VWAP_UNAVAILABLE,
    R_CLOSE_ABOVE_VWAP,
    R_EMA_FAST_ABOVE_SLOW,
    R_RELATIVE_VOLUME_OK,
    ScanContext,
    Scanner,
)
from worker.config import ScannerParams
from worker.models import Candle, DataQuality


def values(**overrides) -> IndicatorValues:
    base = {
        "close": 105.0,
        "vwap": 100.0,
        "ema_fast": 103.0,
        "ema_slow": 101.0,
        "average_volume": 1000.0,
        "relative_volume": 1.8,
        "candles_seen": 40,
        "warm": True,
    }
    base.update(overrides)
    return IndicatorValues(**base)


def context(**overrides) -> ScanContext:
    base = {
        "instrument_id": 1,
        "symbol": "DEMO",
        "in_universe": True,
        "previous_close": 100.0,
        "feed_age_seconds": 5.0,
        "session_candle_count": 40,
        "evaluation_time": minute(40),
    }
    base.update(overrides)
    return ScanContext(**base)


def completed_candle(**overrides) -> Candle:
    base = {
        "instrument_id": 1,
        "trading_date": minute(0).date(),
        "candle_start": minute(40),
        "open": 104.0,
        "high": 106.0,
        "low": 104.0,
        "close": 105.0,
        "volume": 1800,
        "is_complete": True,
        "data_quality": DataQuality.OK,
        "tick_count": 12,
    }
    base.update(overrides)
    return Candle(**base)


@pytest.fixture()
def scanner() -> Scanner:
    return Scanner(ScannerParams())


def test_all_three_conditions_qualify(scanner):
    result = scanner.evaluate(completed_candle(), values(), context())
    assert result.bullish is True
    assert result.evaluable is True
    assert set(result.reasons) == {R_CLOSE_ABOVE_VWAP, R_EMA_FAST_ABOVE_SLOW, R_RELATIVE_VOLUME_OK}
    assert result.failed_reasons == ()


def test_close_below_vwap_rejects(scanner):
    result = scanner.evaluate(completed_candle(close=99.0, low=98.0), values(vwap=100.0), context())
    assert result.bullish is False
    assert F_CLOSE_BELOW_VWAP in result.failed_reasons


def test_close_exactly_at_vwap_is_not_above(scanner):
    result = scanner.evaluate(completed_candle(close=100.0, open=101.0, high=101.0, low=99.0), values(vwap=100.0), context())
    assert result.bullish is False
    assert F_CLOSE_BELOW_VWAP in result.failed_reasons


def test_ema_cross_and_equal_ema_both_reject(scanner):
    below = scanner.evaluate(completed_candle(), values(ema_fast=100.0, ema_slow=102.0), context())
    assert F_EMA_NOT_ALIGNED in below.failed_reasons
    equal = scanner.evaluate(completed_candle(), values(ema_fast=101.0, ema_slow=101.0), context())
    assert F_EMA_NOT_ALIGNED in equal.failed_reasons


def test_relative_volume_threshold_is_inclusive(scanner):
    just_under = scanner.evaluate(completed_candle(), values(relative_volume=1.4999), context())
    assert F_RELATIVE_VOLUME_LOW in just_under.failed_reasons
    exactly = scanner.evaluate(completed_candle(), values(relative_volume=1.5), context())
    assert exactly.bullish is True
    assert R_RELATIVE_VOLUME_OK in exactly.reasons


def test_threshold_is_configurable(scanner):
    stricter = Scanner(ScannerParams(relative_volume_threshold=2.5))
    result = stricter.evaluate(completed_candle(), values(relative_volume=1.8), context())
    assert F_RELATIVE_VOLUME_LOW in result.failed_reasons


def test_incomplete_candle_can_never_alert(scanner):
    result = scanner.evaluate(
        completed_candle(is_complete=False, data_quality=DataQuality.INCOMPLETE), values(), context()
    )
    assert result.bullish is False
    assert result.evaluable is False
    assert F_CANDLE_INCOMPLETE in result.failed_reasons


def test_zero_volume_no_trade_candle_is_rejected_by_data_quality(scanner):
    result = scanner.evaluate(
        completed_candle(volume=0, data_quality=DataQuality.NO_TRADE),
        values(relative_volume=None, average_volume=0.0),
        context(),
    )
    assert result.bullish is False
    assert F_VOLUME_BASELINE_INVALID in result.failed_reasons


@pytest.mark.parametrize("quality", [DataQuality.STALE, DataQuality.SUSPECT])
def test_suspect_or_stale_data_quality_blocks_signal(scanner, quality):
    result = scanner.evaluate(completed_candle(data_quality=quality), values(), context())
    assert result.bullish is False
    assert F_DATA_QUALITY in result.failed_reasons


def test_stale_feed_blocks_signal(scanner):
    result = scanner.evaluate(completed_candle(), values(), context(feed_age_seconds=600.0))
    assert result.bullish is False
    assert F_FEED_STALE in result.failed_reasons


def test_warming_up_history_blocks_signal(scanner):
    result = scanner.evaluate(
        completed_candle(),
        values(candles_seen=12, warm=False),
        context(session_candle_count=12),
    )
    assert result.bullish is False
    assert result.warming_up is True
    assert F_INSUFFICIENT_HISTORY in result.failed_reasons


def test_missing_session_vwap_blocks_signal(scanner):
    result = scanner.evaluate(completed_candle(), values(vwap=None), context())
    assert result.bullish is False
    assert F_VWAP_UNAVAILABLE in result.failed_reasons


def test_outside_universe_blocks_signal(scanner):
    result = scanner.evaluate(completed_candle(), values(), context(in_universe=False))
    assert result.bullish is False
    assert F_OUTSIDE_UNIVERSE in result.failed_reasons


def test_no_positive_return_requirement_is_applied(scanner):
    """Spec §10.2: a falling stock that meets VWAP+EMA+RV still qualifies."""
    result = scanner.evaluate(
        completed_candle(close=95.0),
        values(close=95.0, vwap=90.0, ema_fast=94.0, ema_slow=92.0, relative_volume=2.0),
        context(previous_close=120.0),
    )
    assert result.bullish is True
    assert result.intraday_change_pct == pytest.approx(-20.8333, abs=1e-3)


def test_context_metrics_are_reported(scanner):
    result = scanner.evaluate(
        completed_candle(), values(), context(previous_close=100.0), session_high=106.0, session_low=98.0
    )
    assert result.intraday_change_pct == pytest.approx(5.0)
    assert result.distance_from_vwap_pct == pytest.approx(5.0)
    assert result.ema_spread_pct == pytest.approx((103.0 - 101.0) / 101.0 * 100.0)
    assert result.distance_from_high_pct == pytest.approx(-0.9434, abs=1e-3)
    assert result.session_high == 106.0 and result.session_low == 98.0


def test_every_rejection_carries_a_reason(scanner):
    result = scanner.evaluate(
        completed_candle(is_complete=False, data_quality=DataQuality.STALE),
        values(vwap=None, ema_fast=None, ema_slow=None, relative_volume=None, warm=False, candles_seen=2),
        context(in_universe=False, feed_age_seconds=999.0),
    )
    assert result.bullish is False
    assert len(result.failed_reasons) >= 5
    assert result.reasons == ()


def test_snapshot_conversion_is_lossless(scanner):
    result = scanner.evaluate(completed_candle(), values(), context())
    snapshot = result.to_snapshot(trading_date=minute(0).date(), config_version=3, is_simulated=True)
    assert snapshot.close == result.close
    assert snapshot.vwap == result.vwap
    assert snapshot.ema_fast == result.ema_fast
    assert snapshot.relative_volume == result.relative_volume
    assert snapshot.reasons == result.reasons
    assert snapshot.bullish_qualified is True
    assert snapshot.config_version == 3
    assert snapshot.is_simulated is True


# ── watchlist predicates ────────────────────────────────────────────────────


def snapshot_for(close, vwap, ema_fast, ema_slow):
    from worker.models import IndicatorSnapshot

    return IndicatorSnapshot(
        instrument_id=1,
        trading_date=minute(0).date(),
        candle_start=minute(1),
        close=close,
        previous_close=None,
        vwap=vwap,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        average_volume=1000.0,
        relative_volume=1.0,
        intraday_change_pct=None,
        distance_from_vwap_pct=None,
        ema_spread_pct=None,
        session_high=None,
        session_low=None,
        distance_from_high_pct=None,
        bullish_qualified=False,
        reasons=(),
        failed_reasons=(),
        data_quality=DataQuality.OK,
        warming_up=False,
    )


def test_weakening_triggers_on_either_condition():
    assert Scanner.is_weakening(snapshot_for(99, 100, 101, 100)) is True   # below VWAP only
    assert Scanner.is_weakening(snapshot_for(101, 100, 99, 100)) is True   # EMA cross only
    assert Scanner.is_weakening(snapshot_for(101, 100, 101, 100)) is False  # still strong


def test_removal_requires_both_conditions_on_the_same_candle():
    assert Scanner.is_removal_condition(snapshot_for(99, 100, 101, 100)) is False
    assert Scanner.is_removal_condition(snapshot_for(101, 100, 99, 100)) is False
    assert Scanner.is_removal_condition(snapshot_for(99, 100, 99, 100)) is True


def test_removal_predicate_ignores_unavailable_indicators():
    assert Scanner.is_removal_condition(snapshot_for(99, None, None, None)) is False
    assert Scanner.is_weakening(snapshot_for(99, None, None, None)) is False
