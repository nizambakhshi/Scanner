"""Unit tests for the Stage 1 discovery layer (worker.analytics.daily).

Two styles of fixture, deliberately:

* The **long deterministic series** (linear uptrends/declines) exercise the fold
  end-to-end at the default periods — no number here is hand-computable, so the
  assertions are structural (classes, flags, ordering) and the hand-computed
  arithmetic lives in the tiny-parameter fixtures, where every value is written
  out in the comment above it.
* The **tiny-parameter fixtures** (EMA 2/4/5, Donchian 2, windows of 2) make
  each classification rule exactly hand-checkable. The rules do not depend on
  the periods' size; the arithmetic does.

The module is a pure fold over completed daily bars, so the tests read like a
replay: feed bars, read the state.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from worker.analytics.daily import (
    CLASS_HIGH,
    CLASS_LOW,
    CLASS_MEDIUM,
    GROUP_EMERGING_CANDIDATE,
    GROUP_EXCLUDED,
    GROUP_NEUTRAL_WATCH,
    GROUP_STRONG_BULLISH,
    TREND_BULL,
    TREND_EMERGING,
    TREND_NEUTRAL,
    TREND_STRONG_BULL,
    TREND_UNKNOWN,
    TREND_WEAK,
    DailyBar,
    DailyFeatures,
    DailyReading,
    DiscoveryParams,
    classify_trend,
    classify_universe,
)
from worker.analytics.structure import AdxValues

DAY_ZERO = date(2026, 1, 1)


def bar(i: int, close: float, *, volume: float = 1000.0, spread: float = 0.05) -> DailyBar:
    """Daily bar i (0-based) at ``close`` with a symmetric spread around it."""
    return DailyBar(
        trading_date=DAY_ZERO + timedelta(days=i),
        high=close + spread,
        low=close - spread,
        close=close,
        volume=volume,
    )


def uptrend(bars: int, *, start: float = 100.0, step: float = 0.1) -> list[DailyBar]:
    return [bar(i, start + step * i) for i in range(bars)]


#: Everything hand-checkable: EMA 2/4/5, Donchian 2, ROC 2, turnover window 2,
#: freshness window 2 sessions, history floor 4 bars (>= the mid EMA).
TINY = DiscoveryParams(
    ema_fast=2,
    ema_mid=4,
    ema_trend=5,
    atr_period=2,
    roc_period=2,
    turnover_window=2,
    donchian_period=2,
    ema_rising_lookback=2,
    fresh_high_window=2,
    min_history_bars=4,
)


class TestTrendClassification:
    """The pure matrix: every class, the ties, and the precedence order."""

    def test_unknown_before_the_fast_ema_exists(self) -> None:
        assert (
            classify_trend(
                close=100.0,
                ema20=None,
                ema50=None,
                ema200=None,
                fresh_high_recent=False,
                ema20_rising=None,
            )
            == TREND_UNKNOWN
        )

    def test_strong_stack(self) -> None:
        assert (
            classify_trend(
                close=11.0,
                ema20=10.0,
                ema50=9.0,
                ema200=8.0,
                fresh_high_recent=False,
                ema20_rising=True,
            )
            == TREND_STRONG_BULL
        )

    def test_stack_broken_at_the_200_is_bull_not_strong(self) -> None:
        # close > e20 > e50 but e50 < e200: the stack is not complete.
        assert (
            classify_trend(
                close=11.0,
                ema20=10.0,
                ema50=9.5,
                ema200=9.6,
                fresh_high_recent=False,
                ema20_rising=None,
            )
            == TREND_BULL
        )

    def test_bull_without_the_200(self) -> None:
        assert (
            classify_trend(
                close=11.0,
                ema20=10.0,
                ema50=9.0,
                ema200=None,
                fresh_high_recent=False,
                ema20_rising=None,
            )
            == TREND_BULL
        )

    def test_emerging_by_fresh_high(self) -> None:
        # Above the fast EMA, below the slow one, with a fresh prior-20 high.
        assert (
            classify_trend(
                close=11.0,
                ema20=10.0,
                ema50=12.0,
                ema200=13.0,
                fresh_high_recent=True,
                ema20_rising=None,
            )
            == TREND_EMERGING
        )

    def test_emerging_by_rising_fast_ema(self) -> None:
        assert (
            classify_trend(
                close=12.5,
                ema20=10.0,
                ema50=13.0,
                ema200=13.0,
                fresh_high_recent=False,
                ema20_rising=True,
            )
            == TREND_EMERGING
        )

    def test_rising_branch_needs_close_at_or_below_the_slow_ema(self) -> None:
        # Rising fast EMA but close above the slow EMA is already BULL.
        assert (
            classify_trend(
                close=13.5,
                ema20=10.0,
                ema50=13.0,
                ema200=14.0,
                fresh_high_recent=False,
                ema20_rising=True,
            )
            == TREND_BULL
        )

    def test_neutral_when_above_fast_without_evidence(self) -> None:
        assert (
            classify_trend(
                close=11.0,
                ema20=10.0,
                ema50=11.0,
                ema200=12.0,
                fresh_high_recent=False,
                ema20_rising=False,
            )
            == TREND_NEUTRAL
        )

    def test_neutral_on_the_fast_ema_tie(self) -> None:
        assert (
            classify_trend(
                close=10.0,
                ema20=10.0,
                ema50=8.0,
                ema200=None,
                fresh_high_recent=False,
                ema20_rising=None,
            )
            == TREND_NEUTRAL
        )

    def test_weak_below_fast(self) -> None:
        assert (
            classify_trend(
                close=9.0,
                ema20=10.0,
                ema50=10.5,
                ema200=11.0,
                fresh_high_recent=True,  # even fresh evidence cannot lift it
                ema20_rising=None,
            )
            == TREND_WEAK
        )


class TestFoldEndToEnd:
    def test_long_uptrend_classifies_strong_bull(self) -> None:
        features = DailyFeatures("UPTREND")
        reading = features.replay(uptrend(260))
        assert reading is not None
        assert reading.trend_class == TREND_STRONG_BULL
        assert reading.ema200 is not None
        assert reading.ema200 < reading.ema50 < reading.ema20 < reading.close
        assert reading.bars == 260

    def test_short_uptrend_classifies_bull_without_the_200(self) -> None:
        features = DailyFeatures("YOUNG")
        reading = features.replay(uptrend(60))
        assert reading is not None
        assert reading.trend_class == TREND_BULL
        assert reading.ema200 is None

    def test_under_twenty_bars_is_unknown(self) -> None:
        features = DailyFeatures("NEW")
        reading = features.replay(uptrend(10))
        assert reading is not None
        assert reading.trend_class == TREND_UNKNOWN
        assert reading.ema20 is None

    def test_roc_and_turnover_hand_computed(self) -> None:
        """Closes 10, 11, 12 with volumes 100, 200, 300 on the tiny params.

        bar 3 ROC(2) = (12/10 − 1) × 100 = 20%.
        turnover(bar 2) = 11 × 200 = 2200; turnover(bar 3) = 12 × 300 = 3600;
        mean over the 2-bar window = (2200 + 3600)/2 = 2900.
        """
        features = DailyFeatures("T", params=TINY)
        features.update(bar(0, 10.0, volume=100.0))
        features.update(bar(1, 11.0, volume=200.0))
        reading = features.update(bar(2, 12.0, volume=300.0))
        assert reading.roc12 == pytest.approx(20.0)
        assert reading.turnover20 == pytest.approx(2900.0)

    def test_emerging_fold_with_fresh_high(self) -> None:
        """Closes 12, 12, 11, 11, 11.35 on EMA 2/4/5 — hand-checked.

        ema2: bar2 = 12; bar3 = (2×11 + 12)/3 = 11.333…;
              bar4 = (2×11 + 11.333…)/3 = 11.111…;
              bar5 = (2×11.35 + 11.111…)/3 = 11.270…  → close 11.35 > 11.270
        ema4: seeded bar4 = (12+12+11+11)/4 = 11.5;
              bar5 = 0.4×11.35 + 0.6×11.5 = 11.44            → close ≤ 11.44
        Donchian(2): bar5's prior window is bars 3–4, upper = 11.05;
              close 11.35 > 11.05 → fresh high today (age 0).
        Above fast EMA, below slow, fresh high → EMERGING.
        """
        features = DailyFeatures("E", params=TINY)
        for i, close in enumerate((12.0, 12.0, 11.0, 11.0)):
            features.update(bar(i, close))
        reading = features.update(bar(4, 11.35))
        assert reading.ema20 == pytest.approx(11.27037037037037)
        assert reading.ema50 == pytest.approx(11.44)
        assert reading.fresh_high is True
        assert reading.sessions_since_fresh == 0
        assert reading.fresh_high_recent is True
        assert reading.donchian_upper == pytest.approx(11.05)
        assert reading.trend_class == TREND_EMERGING

    def test_fresh_counter_expires(self) -> None:
        """A breakout on bar 3 stays fresh for the window's sessions, then ages out.

        Closes 10, 10, 10.5, 10, 10, 10 (tiny params, window 2): the fresh high
        fires on bar 3 (10.5 > prior-2 upper 10.05); the flat closes after leave
        the counter counting 1, 2, 3 — recent through age 2, expired at 3.
        """
        features = DailyFeatures("F", params=TINY)
        features.update(bar(0, 10.0))
        features.update(bar(1, 10.0))
        fresh = features.update(bar(2, 10.5))
        assert fresh.fresh_high is True
        assert features.update(bar(3, 10.0)).fresh_high_recent is True  # age 1
        two = features.update(bar(4, 10.0))
        assert two.sessions_since_fresh == 2
        assert two.fresh_high_recent is True
        three = features.update(bar(5, 10.0))
        assert three.sessions_since_fresh == 3
        assert three.fresh_high_recent is False

    def test_atr_percent_hand_computed(self) -> None:
        """Bars (11, 10, 10.5) then (11.5, 10.5, 11) on ATR period 2.

        TR1 = 11 − 10 = 1; TR2 = max(1, |11.5 − 10.5|, |10.5 − 10.5|) = 1;
        ATR = (1 + 1)/2 = 1 → ATR% at close 11 = 1/11 × 100 = 9.0909…%.
        """
        features = DailyFeatures("A", params=TINY)
        features.update(DailyBar(DAY_ZERO, 11.0, 10.0, 10.5, 1000.0))
        reading = features.update(
            DailyBar(DAY_ZERO + timedelta(days=1), 11.5, 10.5, 11.0, 1000.0)
        )
        assert reading.atr14 == pytest.approx(1.0)
        assert reading.atr14_pct == pytest.approx(100.0 / 11.0)

    def test_adx_appears_on_its_own_schedule(self) -> None:
        """ADX period 2: DI at bar 2, the ADX value itself at bar 2·2 − 1 = 3."""
        features = DailyFeatures("X", params=TINY)
        first = features.update(bar(0, 10.0))
        assert first.adx is None  # no comparison bar yet, so no ±DM
        second = features.update(bar(1, 10.1))
        assert second.adx is not None
        assert second.adx.adx is None  # DI exists; ADX still seeding its DX average
        assert second.adx.plus_di is not None
        third = features.update(bar(2, 10.2))
        assert third.adx is not None
        assert third.adx.adx is not None

    def test_golden_cross_age_seen_through_the_window(self) -> None:
        """A long decline then a strong rise: SMA50 crosses above SMA200.

        210 bars of 200 − 0.2·i, then 70 bars rising 159 + 0.5·j. At bar 210 the
        fast average sits below the slow one; by bar 280 it has crossed and the
        age is a known session count (not None — the window saw the cross).
        """
        features = DailyFeatures("GC")
        readings = []
        for i in range(210):
            readings.append(features.update(bar(i, 200.0 - 0.2 * i)))
        assert readings[-1].sma50_above_sma200 is False
        for j in range(70):
            readings.append(features.update(bar(210 + j, 159.0 + 0.5 * j)))
        final = readings[-1]
        assert final.sma50_above_sma200 is True
        assert final.golden_cross_sessions is not None
        assert final.golden_cross_sessions >= 0

    def test_rejects_bad_bars_and_params(self) -> None:
        features = DailyFeatures("V")
        with pytest.raises(ValueError):
            features.update(DailyBar(DAY_ZERO, 9.0, 10.0, 9.5, 1000.0))  # H < L
        with pytest.raises(ValueError):
            features.update(DailyBar(DAY_ZERO, 10.0, 9.0, 0.0, 1000.0))  # bad close
        with pytest.raises(ValueError):
            DailyFeatures("V", params=DiscoveryParams(ema_fast=30, ema_mid=20))
        with pytest.raises(ValueError):
            DailyFeatures("V", params=DiscoveryParams(min_history_bars=10))


def reading(
    symbol: str,
    *,
    trend_class: str = TREND_NEUTRAL,
    roc12: float | None = None,
    turnover20: float | None = None,
    atr14_pct: float | None = None,
    bars: int = 60,
    trading_date: date = DAY_ZERO,
    close: float = 100.0,
    adx: AdxValues | None = None,
) -> DailyReading:
    """A hand-built reading: only the fields the cross-sectional tests read."""
    return DailyReading(
        symbol=symbol,
        trading_date=trading_date,
        close=close,
        bars=bars,
        ema20=close,
        ema50=close,
        ema200=close,
        sma50=close,
        sma200=close,
        sma50_above_sma200=True,
        golden_cross_sessions=3,
        atr14=1.0,
        atr14_pct=atr14_pct,
        roc12=roc12,
        turnover20=turnover20,
        turnover_ratio5_20=None,
        donchian_upper=close,
        donchian_lower=close,
        fresh_high=False,
        sessions_since_fresh=4,
        fresh_high_recent=True,
        ema20_rising=True,
        obv=1000.0,
        obv_rising=True,
        adx=adx,
        trend_class=trend_class,
    )


class TestClassifyUniverse:
    def test_groups_and_percentiles_hand_computed(self) -> None:
        """Six names, evenly spaced ROC/turnover/ATR%.

        ROC 10…60 → percentile ranks 0, 20, 40, 60, 80, 100 (n−1 = 5 gaps).
        Turnover 1e7…6e7 → fractions 0, .2, .4, .6, .8, 1.0 → terciles
        LOW, LOW, MEDIUM, MEDIUM, HIGH, HIGH at cuts 1/3 and 2/3.
        """
        readings = [
            reading(
                symbol,
                trend_class=trend_class,
                roc12=roc,
                turnover20=turnover,
                atr14_pct=atr,
            )
            for symbol, trend_class, roc, turnover, atr in (
                ("A", TREND_STRONG_BULL, 10.0, 1e7, 1.0),
                ("B", TREND_BULL, 20.0, 2e7, 2.0),
                ("C", TREND_EMERGING, 30.0, 3e7, 3.0),
                ("D", TREND_NEUTRAL, 40.0, 4e7, 4.0),
                ("E", TREND_WEAK, 50.0, 5e7, 5.0),
                ("F", TREND_UNKNOWN, 60.0, 6e7, 6.0),
            )
        ]
        snapshots = classify_universe(readings, as_of=DAY_ZERO)
        by_symbol = {snapshot.symbol: snapshot for snapshot in snapshots}

        assert [s.symbol for s in snapshots] == ["A", "B", "C", "D", "E", "F"]
        assert by_symbol["A"].group == GROUP_STRONG_BULLISH
        assert by_symbol["B"].group == GROUP_EMERGING_CANDIDATE
        assert by_symbol["C"].group == GROUP_EMERGING_CANDIDATE
        assert by_symbol["D"].group == GROUP_NEUTRAL_WATCH
        assert by_symbol["E"].group == GROUP_NEUTRAL_WATCH
        assert by_symbol["F"].group == GROUP_NEUTRAL_WATCH
        assert by_symbol["A"].relative_strength == pytest.approx(0.0)
        assert by_symbol["F"].relative_strength == pytest.approx(100.0)
        assert by_symbol["B"].relative_strength == pytest.approx(20.0)
        assert by_symbol["A"].liquidity_class == CLASS_LOW
        assert by_symbol["B"].liquidity_class == CLASS_LOW
        assert by_symbol["C"].liquidity_class == CLASS_MEDIUM
        assert by_symbol["D"].liquidity_class == CLASS_MEDIUM
        assert by_symbol["E"].liquidity_class == CLASS_HIGH
        assert by_symbol["F"].liquidity_class == CLASS_HIGH
        assert by_symbol["C"].volatility_class == CLASS_MEDIUM

    def test_stale_last_bar_is_excluded_first(self) -> None:
        old = reading("OLD", trend_class=TREND_STRONG_BULL, trading_date=DAY_ZERO - timedelta(days=30))
        snapshots = classify_universe([old], as_of=DAY_ZERO)
        assert snapshots[0].group == GROUP_EXCLUDED
        assert snapshots[0].eligibility is False
        assert snapshots[0].reason.startswith("stale_last_bar:")

    def test_short_history_is_excluded(self) -> None:
        young = reading("YOUNG", bars=10)
        snapshots = classify_universe([young], as_of=DAY_ZERO)
        assert snapshots[0].group == GROUP_EXCLUDED
        assert snapshots[0].reason == "insufficient_history:10"

    def test_price_floor_only_applies_when_armed(self) -> None:
        cheap = reading("CHEAP", close=15.0)
        unarmed = classify_universe([cheap], as_of=DAY_ZERO)
        assert unarmed[0].group != GROUP_EXCLUDED
        armed = classify_universe(
            [cheap], as_of=DAY_ZERO, params=DiscoveryParams(min_price=20.0)
        )
        assert armed[0].group == GROUP_EXCLUDED
        assert armed[0].reason == "price_below_floor:15"

    def test_completeness_counts_the_feature_vector(self) -> None:
        full = reading(
            "FULL",
            atr14_pct=2.0,
            roc12=5.0,
            turnover20=1e7,
            adx=AdxValues(30.0, 25.0, 10.0),
        )
        frozen = reading("THIN")
        snapshots = classify_universe([full, frozen], as_of=DAY_ZERO)
        by_symbol = {s.symbol: s for s in snapshots}
        for snapshot in snapshots:
            present = sum(1 for value in snapshot.indicators.values() if value is not None)
            assert snapshot.completeness == pytest.approx(
                present / len(snapshot.indicators)
            )
        assert by_symbol["FULL"].completeness > by_symbol["THIN"].completeness

    def test_terciles_need_a_distribution(self) -> None:
        single = reading("ONE", turnover20=1e7, atr14_pct=1.0, roc12=1.0)
        snapshots = classify_universe([single], as_of=DAY_ZERO)
        assert snapshots[0].liquidity_class == "INSUFFICIENT"
        assert snapshots[0].volatility_class == "INSUFFICIENT"
        assert snapshots[0].relative_strength is None
