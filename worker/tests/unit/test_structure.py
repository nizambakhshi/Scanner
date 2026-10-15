"""Unit tests for the structural indicator set (worker.analytics.structure).

Every expected number is worked out by hand in the comment above it, from the
module docstring's definitions — never copied from the implementation's output.
ADX uses ``period=2`` so the whole Wilder chain (seed, smoothing, DI, DX, ADX)
is hand-computable in three bars; the property under test (the recursion) does
not depend on the period's size.
"""

from __future__ import annotations

import pytest

from worker.analytics.structure import (
    AdxState,
    AdxValues,
    DonchianChannel,
    DonchianState,
    EmaStack,
    ObvState,
    SmaState,
)


class TestSma:
    def test_first_value_on_the_nth_close(self) -> None:
        sma = SmaState(period=3)
        assert sma.update(10.0) is None
        assert sma.update(20.0) is None
        # mean(10, 20, 30) = 20
        assert sma.update(30.0) == pytest.approx(20.0)
        # mean(20, 30, 40) = 30
        assert sma.update(40.0) == pytest.approx(30.0)

    def test_rejects_bad_prices_and_periods(self) -> None:
        with pytest.raises(ValueError):
            SmaState(period=0)
        with pytest.raises(ValueError):
            SmaState(period=3).update(0.0)

    def test_round_trip_continues_exactly(self) -> None:
        direct = SmaState(period=3)
        for close in (10.0, 20.0, 30.0):
            direct.update(close)
        restored = SmaState.from_dict(direct.to_dict())
        assert restored.update(40.0) == pytest.approx(direct.update(40.0))


class TestDonchian:
    def test_channel_is_the_prior_window(self) -> None:
        donchian = DonchianState(period=3)
        # Bars 1–3 fill the window; the channel they define is published when
        # bar 4 arrives — the bar 4 close is judged against bars 1–3.
        assert donchian.update(high=10.0, low=9.0) is None
        assert donchian.update(high=11.0, low=9.5) is None
        assert donchian.update(high=12.0, low=10.0) is None
        channel = donchian.update(high=13.0, low=11.0)
        assert channel == DonchianChannel(upper=12.0, lower=9.0)

    def test_breakout_is_strictly_above_the_prior_upper(self) -> None:
        donchian = DonchianState(period=2)
        donchian.update(high=10.0, low=9.0)
        donchian.update(high=11.0, low=10.0)
        channel = donchian.update(high=11.5, low=10.5)
        assert channel == DonchianChannel(upper=11.0, lower=9.0)
        # A close at the channel top is not a breakout; a hair above is.
        assert not (channel.upper < 11.0)
        assert channel.upper < 11.01

    def test_rejects_high_below_low(self) -> None:
        donchian = DonchianState(period=2)
        with pytest.raises(ValueError):
            donchian.update(high=9.0, low=10.0)

    def test_round_trip_rebuilds_the_channel(self) -> None:
        direct = DonchianState(period=2)
        direct.update(high=10.0, low=9.0)  # one bar in the window
        restored = DonchianState.from_dict(direct.to_dict())
        # The second bar fills the window without publishing anything...
        assert restored.update(high=11.0, low=10.0) is None
        direct.update(high=11.0, low=10.0)
        # ...and the third judges against bars 1–2: upper max(10, 11) = 11,
        # lower min(9, 10) = 9.
        got = restored.update(high=12.0, low=11.0)
        want = direct.update(high=12.0, low=11.0)
        assert got == want == DonchianChannel(upper=11.0, lower=9.0)
        assert restored.current == direct.current


class TestObv:
    def test_ledger_arithmetic(self) -> None:
        obv = ObvState(lookback=2)
        # First bar: no previous close to sign against — contributes nothing.
        assert obv.update(close=10.0, volume=100.0) == pytest.approx(0.0)
        # Up 10 → 11 on 50 shares: +50.
        assert obv.update(close=11.0, volume=50.0) == pytest.approx(50.0)
        # Flat 11 → 11 on 30 shares: unchanged.
        assert obv.update(close=11.0, volume=30.0) == pytest.approx(50.0)
        # Down 11 → 10.5 on 20 shares: −20.
        assert obv.update(close=10.5, volume=20.0) == pytest.approx(30.0)

    def test_rising_compares_against_the_lookback(self) -> None:
        obv = ObvState(lookback=2)
        obv.update(close=10.0, volume=100.0)  # 0
        obv.update(close=11.0, volume=50.0)  # 50
        assert obv.rising is None  # only two observations, needs three
        obv.update(close=11.0, volume=30.0)  # 50
        # 50 > 0 (the value two bars back): rising.
        assert obv.rising is True
        obv.update(close=10.5, volume=20.0)  # 30
        # 30 < 50 (two bars back): no longer rising.
        assert obv.rising is False

    def test_rejects_negative_volume(self) -> None:
        obv = ObvState()
        with pytest.raises(ValueError):
            obv.update(close=10.0, volume=-1.0)

    def test_round_trip_continues_exactly(self) -> None:
        direct = ObvState(lookback=2)
        for close, volume in ((10.0, 100.0), (11.0, 50.0), (11.0, 30.0)):
            direct.update(close=close, volume=volume)
        restored = ObvState.from_dict(direct.to_dict())
        assert restored.update(close=10.5, volume=20.0) == pytest.approx(
            direct.update(close=10.5, volume=20.0)
        )
        assert restored.rising == direct.rising


class TestAdx:
    """A hand-computed three-bar series with ``period=2``.

    Bars: (H, L, C) = (10, 9, 9.5), (11, 9.5, 10.5), (12, 10.5, 11.5) — a
    steadily rising three-bar tape with every move up.

    Bar 1: TR = H − L = 1; +DM = −DM = 0 (no previous bar in scope).
    Bar 2: up = 11 − 10 = 1, down = 9 − 9.5 = −0.5 → +DM = 1, −DM = 0;
           TR = max(11 − 9.5, |11 − 9.5|, |9.5 − 9.5|) = 1.5.
    Seeds (period 2): TR_s = (1 + 1.5)/2 = 1.25; +DM_s = (0 + 1)/2 = 0.5;
           −DM_s = 0.
           +DI = 100 × 0.5 / 1.25 = 40; −DI = 0; DX = 100·40/40 = 100.
    Bar 3: up = 1, down = −1 → +DM = 1, −DM = 0; TR = 1.5.
           TR_s = (1×1.25 + 1.5)/2 = 1.375; +DM_s = (0.5 + 1)/2 = 0.75.
           +DI = 100 × 0.75 / 1.375 = 54.545…; DX = 100.
           ADX seed (period 2 DX values: 100, 100) = 100.
    """

    def test_three_bar_chain(self) -> None:
        adx = AdxState(period=2)
        first = adx.update(high=10.0, low=9.0, close=9.5)
        assert first == AdxValues(None, None, None)
        second = adx.update(high=11.0, low=9.5, close=10.5)
        assert second.adx is None
        assert second.plus_di == pytest.approx(40.0)
        assert second.minus_di == pytest.approx(0.0)
        third = adx.update(high=12.0, low=10.5, close=11.5)
        assert third.adx == pytest.approx(100.0)
        assert third.plus_di == pytest.approx(54.54545454545455)
        assert third.minus_di == pytest.approx(0.0)
        assert third.ready

    def test_min_observations_property(self) -> None:
        assert AdxState(period=14).min_observations_for_adx == 27
        assert AdxState(period=2).min_observations_for_adx == 3

    def test_down_moves_feed_minus_dm(self) -> None:
        """Bars: (10, 9, 9.5) then (10.2, 8, 8.2).

        up = 10.2 − 10 = 0.2; down = 9 − 8 = 1 → down is larger and positive:
        +DM = 0, −DM = 1. TR = max(2.2, 0.7, 1.5) = 2.2.
        Period-2 seeds: TR_s = (1 + 2.2)/2 = 1.6; −DM_s = 1/2 = 0.5.
        −DI = 100 × 0.5 / 1.6 = 31.25, +DI = 0, DX = 100.
        """
        adx = AdxState(period=2)
        adx.update(high=10.0, low=9.0, close=9.5)
        second = adx.update(high=10.2, low=8.0, close=8.2)
        assert second.plus_di == pytest.approx(0.0)
        assert second.minus_di == pytest.approx(31.25)

    def test_round_trip_continues_exactly(self) -> None:
        direct = AdxState(period=2)
        for bar in ((10.0, 9.0, 9.5), (11.0, 9.5, 10.5)):
            direct.update(high=bar[0], low=bar[1], close=bar[2])
        restored = AdxState.from_dict(direct.to_dict())
        got = restored.update(high=12.0, low=10.5, close=11.5)
        want = direct.update(high=12.0, low=10.5, close=11.5)
        assert got.adx == pytest.approx(want.adx)
        assert got.plus_di == pytest.approx(want.plus_di)


class TestEmaStack:
    def test_alignment_none_until_warm_then_true(self) -> None:
        stack = EmaStack(periods=(2, 3, 4))
        assert stack.aligned is None
        stack.update(10.0)
        stack.update(11.0)
        # e2 = (10+11)/2 = 10.5; longer lines still seeding.
        assert stack.aligned is None
        stack.update(12.0)
        stack.update(13.0)
        # Rising closes stack the shorter averages above the longer ones.
        assert stack.aligned is True

    def test_alignment_false_when_order_breaks(self) -> None:
        stack = EmaStack(periods=(2, 3, 4))
        for close in (13.0, 12.0, 11.0, 10.0):
            stack.update(close)
        assert stack.aligned is False

    def test_round_trip_continues_exactly(self) -> None:
        direct = EmaStack(periods=(2, 3, 4))
        for close in (10.0, 11.0):
            direct.update(close)
        restored = EmaStack.from_dict(direct.to_dict())
        assert restored.update(12.0) == pytest.approx(direct.update(12.0))
