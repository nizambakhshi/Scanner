"""Unit tests for the ORB breakout-retest setups (worker.analytics.setups).

Every expected number is worked out by hand in the comment above it, from the
definitions stated in the module docstring. The default tolerance of 0.5% on a
102.0 level gives a touch band top of 102.51 and a failure line of 101.49; the
zero-tolerance cases exist where a boundary has to be exact in binary floats.
"""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from tests.conftest import BASE_DAY, minute
from worker.analytics.breakouts import DOWN, UP, BreakoutEvent
from worker.analytics.setups import (
    DEFAULT_MAX_RETEST_BARS,
    DEFAULT_RETEST_TOLERANCE_PCT,
    RetestCandidate,
    SetupParams,
    SetupTracker,
)

NEXT_DAY = BASE_DAY + timedelta(days=1)


def bar(
    offset: int,
    close: float,
    *,
    high: float | None = None,
    low: float | None = None,
    volume: int = 1000,
    instrument_id: int = 1,
    day: date = BASE_DAY,
) -> SimpleNamespace:
    """One completed 1-minute bar, ``offset`` minutes after the 09:15 open."""
    start = minute(offset).replace(year=day.year, month=day.month, day=day.day)
    return SimpleNamespace(
        instrument_id=instrument_id,
        trading_date=day,
        candle_start=start,
        high=close if high is None else high,
        low=close if low is None else low,
        close=close,
        volume=volume,
    )


def orb_event(**overrides: Any) -> BreakoutEvent:
    """An ORB UP breakout event as the tracker would hand it over.

    Defaults: TATAMOTORS broke above a 102.0 opening-range high at 103.02 on
    the 15-minute-offset candle.
    """
    base: dict[str, Any] = {
        "strategy": "orb",
        "direction": UP,
        "instrument_id": 1,
        "symbol": "TATAMOTORS",
        "level": 102.0,
        "price": 103.02,
        "candle_start": minute(15),
        "trading_date": BASE_DAY,
        "volume_ratio": 2.2,
        "cooldown_minutes": 0,
        "telegram": True,
        "reasons": ("strategy:orb", "orb_high_break"),
    }
    base.update(overrides)
    return BreakoutEvent(**base)


def feed(tracker: SetupTracker, candle: Any, **overrides: Any) -> list[RetestCandidate]:
    """Run one completed candle through the tracker under test."""
    kwargs: dict[str, Any] = {"symbol": "TATAMOTORS"}
    kwargs.update(overrides)
    return tracker.on_candle(candle, **kwargs)


def test_default_constants_are_the_documented_ones() -> None:
    assert DEFAULT_RETEST_TOLERANCE_PCT == 0.5
    assert DEFAULT_MAX_RETEST_BARS == 30


def test_on_break_opens_a_leg_with_the_break_stamped_in() -> None:
    tracker = SetupTracker()
    leg = tracker.on_break(orb_event(), atr=0.9)
    assert leg is not None
    assert (leg.level, leg.break_price) == (102.0, 103.02)
    assert leg.break_candle_start == minute(15)
    assert leg.trading_date == BASE_DAY
    assert leg.atr_at_break == 0.9
    # The window opens on the first candle after the break; nothing counted yet.
    assert (leg.bars_since_break, leg.touched_at, leg.min_low) == (0, None, None)
    # The evidence seed is the break price: the break candle's own high is not
    # observed (the break is judged at its close).
    assert leg.extension_max == 103.02
    assert leg.touched is False
    assert tracker.legs_for(1) == (leg,)
    assert tracker.open_leg_count == 1


def test_on_break_ignores_everything_but_orb_up() -> None:
    tracker = SetupTracker()
    for strategy, direction in (
        ("orb", DOWN),
        ("session_break", UP),
        ("vwap", UP),
        ("rsi", UP),
    ):
        assert tracker.on_break(orb_event(strategy=strategy, direction=direction)) is None
    assert tracker.open_leg_count == 0
    assert tracker.legs_for(1) == ()


def test_touch_waits_for_its_reclaim_on_a_later_candle() -> None:
    # The classic shape, walked bar by bar. Band top 102.51, failure 101.49.
    tracker = SetupTracker()
    tracker.on_break(orb_event(), atr=0.9)

    # run-up: high 103.8 extends the evidence, low 103.0 stays clear of the band
    assert feed(tracker, bar(16, 103.5, high=103.8, low=103.0)) == []
    leg = tracker.legs_for(1)[0]
    assert (leg.bars_since_break, leg.extension_max, leg.touched) == (1, 103.8, False)

    # dip into the band: low 101.8 returns to the level, close 101.7 is above
    # the failure line (101.49) so this is a touch, not a failure — and not
    # yet a reclaim (close is not above 102.0)
    assert feed(tracker, bar(17, 101.7, high=102.5, low=101.8)) == []
    leg = tracker.legs_for(1)[0]
    assert leg.touched_at == minute(17)
    assert leg.min_low == 101.8
    assert leg.extension_max == 103.8  # 102.5 did not beat it

    # reclaim: close 102.9 back above the level
    candidates = feed(tracker, bar(18, 102.9, high=102.9, low=101.9), relative_volume=1.8)
    assert len(candidates) == 1
    c = candidates[0]
    assert (c.instrument_id, c.symbol, c.trading_date) == (1, "TATAMOTORS", BASE_DAY)
    assert (c.level, c.break_candle_start, c.break_price) == (102.0, minute(15), 103.02)
    assert (c.touched_at, c.reclaim_at, c.reclaim_price) == (minute(17), minute(18), 102.9)
    assert (c.min_low, c.extension_max) == (101.8, 103.8)
    assert (c.bars_since_break, c.volume_ratio, c.atr_at_break) == (3, 1.8, 0.9)
    # depth (102−101.8)/102 × 100 = 0.19607843%
    assert c.depth_pct == pytest.approx(0.1960784313725518, rel=1e-12)
    # extension (103.8−102)/0.9 = 2.0 ATR exactly
    assert c.extension_atr == pytest.approx(2.0, rel=1e-12)
    # one leg, one candidate: consumed
    assert tracker.legs_for(1) == ()
    assert tracker.open_leg_count == 0


def test_one_candle_may_touch_and_reclaim_at_once() -> None:
    tracker = SetupTracker()
    tracker.on_break(orb_event())
    candidates = feed(tracker, bar(16, 102.5, high=102.6, low=101.9))
    assert len(candidates) == 1
    c = candidates[0]
    assert c.touched_at == c.reclaim_at == minute(16)
    assert c.reclaim_price == 102.5
    assert c.bars_since_break == 1
    assert c.min_low == 101.9
    # depth (102−101.9)/102 × 100 = 0.09803922%
    assert c.depth_pct == pytest.approx(0.09803921568626893, rel=1e-12)
    # the run-up never beat the break price, so the seed holds
    assert c.extension_max == 103.02
    # no ATR was supplied: the run is reported, the ATR multiple is not
    assert c.atr_at_break is None
    assert c.extension_atr is None


def test_a_close_below_the_failure_line_drops_the_leg_even_when_it_dipped_in() -> None:
    # low 101.0 is inside the band, but close 101.2 is below the failure line
    # 101.49: the level did not hold, and the failure is checked first.
    tracker = SetupTracker()
    tracker.on_break(orb_event())
    assert feed(tracker, bar(16, 101.2, high=102.0, low=101.0)) == []
    assert tracker.legs_for(1) == ()
    assert tracker.open_leg_count == 0


def test_zero_tolerance_pins_the_level_as_the_whole_line() -> None:
    # With tolerance 0 the band and the failure line are the level itself, and
    # every comparison lands exactly on it in binary floats.
    tracker = SetupTracker(params=SetupParams(retest_tolerance_pct=0.0))
    tracker.on_break(orb_event(level=100.0, price=100.8))

    # close exactly at the level: not a failure (strictly below), not a
    # reclaim (strictly above) — a touch and nothing more
    assert feed(tracker, bar(16, 100.0, high=100.2, low=100.0)) == []
    leg = tracker.legs_for(1)[0]
    assert leg.touched_at == minute(16)
    assert leg.min_low == 100.0

    # close 100.2 > 100.0 reclaims; depth (100−100)/100 = 0.0%
    candidates = feed(tracker, bar(17, 100.2, high=100.2, low=100.1))
    assert len(candidates) == 1
    assert candidates[0].reclaim_price == 100.2
    assert candidates[0].min_low == 100.0
    assert candidates[0].depth_pct == 0.0


def test_zero_tolerance_close_below_the_level_fails() -> None:
    tracker = SetupTracker(params=SetupParams(retest_tolerance_pct=0.0))
    tracker.on_break(orb_event(level=100.0, price=100.8))
    assert feed(tracker, bar(16, 99.99, high=100.2, low=99.99)) == []
    assert tracker.legs_for(1) == ()


def test_reclaim_at_the_expiry_boundary_is_kept_and_one_bar_later_is_not() -> None:
    params = SetupParams(max_retest_bars=2)

    # kept: the reclaim lands exactly on bar 2 of 2
    kept = SetupTracker(params=params)
    kept.on_break(orb_event())
    feed(kept, bar(16, 103.2, high=103.4, low=103.0))
    candidates = feed(kept, bar(17, 102.5, high=102.6, low=101.9))
    assert len(candidates) == 1
    assert candidates[0].bars_since_break == 2

    # dropped: the same shape one bar past the window
    late = SetupTracker(params=params)
    late.on_break(orb_event())
    feed(late, bar(16, 103.2, high=103.4, low=103.0))
    feed(late, bar(17, 103.1, high=103.3, low=103.0))
    assert feed(late, bar(18, 102.6, high=102.7, low=101.8)) == []
    assert late.legs_for(1) == ()


def test_a_foreign_date_candle_drops_the_leg() -> None:
    tracker = SetupTracker()
    tracker.on_break(orb_event())
    assert feed(tracker, bar(16, 103.5, high=103.8, low=103.0, day=NEXT_DAY)) == []
    assert tracker.legs_for(1) == ()
    assert tracker.open_leg_count == 0


def test_candles_of_other_instruments_leave_the_leg_alone() -> None:
    tracker = SetupTracker()
    tracker.on_break(orb_event())
    # an instrument with no legs is a no-op
    assert feed(tracker, bar(16, 50.0, instrument_id=2)) == []
    assert tracker.open_leg_count == 1
    # and another instrument's candle never advances this instrument's leg
    leg = tracker.legs_for(1)[0]
    assert leg.bars_since_break == 0


def test_a_consumed_leg_never_emits_twice() -> None:
    tracker = SetupTracker()
    tracker.on_break(orb_event())
    assert len(feed(tracker, bar(16, 102.5, high=102.6, low=101.9))) == 1
    # the next candle also closes above the level after the earlier touch —
    # but the leg is gone, so there is nothing left to reclaim
    assert feed(tracker, bar(17, 102.7, high=102.8, low=101.8)) == []


def test_two_legs_advance_and_consume_independently() -> None:
    # Zero tolerance so both lines sit exactly on their levels. The two levels
    # are 100.0 and 100.5, and the first candle closes exactly on 100.5: that
    # reclaims the first leg while leaving the second touched but alive (a
    # close below 100.5 would be a failure for it, a close above would reclaim
    # it too — the boundary is the only shape that separates the two legs on
    # one candle).
    tracker = SetupTracker(params=SetupParams(retest_tolerance_pct=0.0))
    tracker.on_break(orb_event(level=100.0, price=100.8))
    tracker.on_break(orb_event(level=100.5, price=100.8))
    assert tracker.open_leg_count == 2

    # candle low 100.0, close 100.5: touches both, reclaims only the 100.0 leg
    candidates = feed(tracker, bar(16, 100.5, high=100.6, low=100.0))
    assert [c.level for c in candidates] == [100.0]
    assert [leg.level for leg in tracker.legs_for(1)] == [100.5]

    # candle low 100.5, close 100.8: the second leg reclaims
    candidates = feed(tracker, bar(17, 100.8, high=100.9, low=100.5))
    assert [c.level for c in candidates] == [100.5]
    assert tracker.open_leg_count == 0


def test_depth_goes_negative_when_the_band_held_from_above() -> None:
    # low 102.3 never reached the level (102.0) but is inside the band top
    # 102.51, and the same candle closes 102.6 above the level.
    tracker = SetupTracker()
    tracker.on_break(orb_event())
    candidates = feed(tracker, bar(16, 102.6, high=102.6, low=102.3))
    assert len(candidates) == 1
    # depth (102−102.3)/102 × 100 = −0.29411765%
    assert candidates[0].depth_pct == pytest.approx(-0.29411764705882076, rel=1e-12)


def test_a_degenerate_atr_never_produces_an_atr_multiple() -> None:
    tracker = SetupTracker()
    tracker.on_break(orb_event(), atr=0.0)
    candidates = feed(tracker, bar(16, 102.5, high=102.6, low=101.9))
    assert len(candidates) == 1
    assert candidates[0].atr_at_break == 0.0
    assert candidates[0].extension_atr is None


def test_candidate_log_dict_rounds_the_evidence() -> None:
    tracker = SetupTracker()
    tracker.on_break(orb_event(), atr=0.9)
    feed(tracker, bar(16, 103.5, high=103.8, low=103.0))
    feed(tracker, bar(17, 101.7, high=102.5, low=101.8))
    candidate = feed(tracker, bar(18, 102.9, high=102.9, low=101.9), relative_volume=1.8)[0]
    assert candidate.to_log_dict() == {
        "symbol": "TATAMOTORS",
        "level": 102.0,
        "reclaim_price": 102.9,
        "depth_pct": 0.196,  # (102−101.8)/102 = 0.19608%
        "extension_pct": 1.765,  # (103.8−102)/102 = 1.76471%
        "bars_since_break": 3,
        "volume_ratio": 1.8,
        "reclaim_at": minute(18).isoformat(),
    }


def test_a_reclaim_without_volume_reports_none() -> None:
    tracker = SetupTracker()
    tracker.on_break(orb_event())
    candidate = feed(tracker, bar(16, 102.5, high=102.6, low=101.9))[0]
    assert candidate.volume_ratio is None
    assert candidate.to_log_dict()["volume_ratio"] is None


def test_setup_params_defaults_are_valid_and_out_of_range_values_are_named() -> None:
    assert SetupParams().problems() == []
    assert SetupParams(retest_tolerance_pct=-0.1).problems() == [
        "retest_tolerance_pct must be between 0 and 5"
    ]
    assert SetupParams(retest_tolerance_pct=5.1).problems() == [
        "retest_tolerance_pct must be between 0 and 5"
    ]
    assert SetupParams(max_retest_bars=0).problems() == ["max_retest_bars must be between 1 and 240"]
    assert SetupParams(max_retest_bars=241).problems() == [
        "max_retest_bars must be between 1 and 240"
    ]
    assert SetupParams(retest_tolerance_pct=-0.1, max_retest_bars=241).problems() == [
        "retest_tolerance_pct must be between 0 and 5",
        "max_retest_bars must be between 1 and 240",
    ]


def test_tracker_refuses_invalid_parameters_at_construction() -> None:
    with pytest.raises(ValueError, match="invalid setup parameters"):
        SetupTracker(params=SetupParams(max_retest_bars=0))


def test_reset_forgets_every_leg_and_the_window_reopens() -> None:
    tracker = SetupTracker()
    tracker.on_break(orb_event())
    tracker.on_break(orb_event(instrument_id=2, symbol="INFY"))
    assert tracker.open_leg_count == 2

    tracker.reset()

    assert tracker.open_leg_count == 0
    assert tracker.legs_for(1) == ()
    assert feed(tracker, bar(16, 102.5, high=102.6, low=101.9)) == []
    assert tracker.on_break(orb_event()) is not None
    assert tracker.open_leg_count == 1
