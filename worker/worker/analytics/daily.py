"""Stage 1: the daily discovery layer — trend, liquidity, volatility, groups.

This module computes, from completed daily bars only (``candles_1d``), the
readings the brief's Stage 1 asks for, and assigns each instrument to one of the
four discovery groups. It **never generates a signal**: the brief is explicit
that Stage 1 writes classifications and snapshots, and entry decisions belong to
Stages 2 and 3.

Two layers, deliberately separable:

* :class:`DailyFeatures` — the per-instrument **fold**. Feed it completed daily
  bars in order; each :meth:`update` returns a :class:`DailyReading` with the
  20/50/200 EMAs (the same :class:`worker.analytics.ema.EmaState` the intraday
  scanner uses — one EMA definition, two timeframes), SMA 50/200 for the
  Golden-Cross preset, ATR-14 (daily) for the volatility measure, 12-session
  ROC, the 20-session average traded value, the previous-20 Donchian channel and
  freshness of a close above it, the EMA-20 rising read, OBV + trend, ADX-14,
  and the trend class. The fold is a pure replay: restart recovery rebuilds it
  by replaying stored bars, so it carries no checkpoint (the LevelBook rule).
* :func:`classify_universe` — the **cross-sectional** step. Terciles for
  liquidity and volatility, relative-strength percentiles, and then one
  mutually exclusive group per instrument (``EXCLUDED`` checked first). Only
  this step sees the whole universe, because only it can rank.

The classification rules, fixed here so every surface agrees (audit §3.1):

* ``STRONG_BULL`` — ``close > ema20 > ema50 > ema200``, all present.
* ``BULL`` — ``close > ema20`` and ``close > ema50`` (full stack unavailable or
  the 200 below). Early-stage trend, full group weight as a candidate.
* ``EMERGING`` — ``close > ema20`` with early evidence: a fresh previous-20
  Donchian high within the last few sessions, or EMA-20 rising over the last
  few sessions while ``close <= ema50``. Early-stage breakouts stay visible.
* ``NEUTRAL`` — above or at the fast EMA but none of the above: no breakout in
  the window, EMA-20 not rising, still below the slow EMA. A watch-only read,
  added during implementation so every close/EMA tie has an honest name instead
  of being forced into a neighbouring class (it maps to NEUTRAL_WATCH like
  ``WEAK``).
* ``WEAK`` — ``close < ema20``.
* ``UNKNOWN`` — not enough history for the reading to exist at all.

**EMA 50 > EMA 200 is a feature, not a gate.** The brief reserves that decision:
the golden-cross state is *recorded* (``sma50_above_sma200``, sessions since the
cross) and contributes evidence, but it never excludes a stock by itself.

Groups, ordered and mutually exclusive (the audit's rule): ``EXCLUDED`` when a
mandatory tradability check fails (missing/stale last bar, insufficient history,
or the price floor when it is armed) → ``STRONG_BULLISH`` → ``EMERGING_CANDIDATE``
(``BULL`` and ``EMERGING`` trends) → ``NEUTRAL_WATCH`` (everything else).
``EXCLUDED`` is the only group that withholds a stock from the opening
evaluation, and the brief's own rule holds: this layer does not filter the live
watchlist — ``stage2_gate`` stays off until the operator arms it.

The liquidity floor is **not armed**: ``min_price`` and any turnover floor ship
as ``None`` ("compute only"), because the brief forbids inventing a fixed
threshold and deploying it without justification. The liquidity and volatility
*classes* are relative — cross-sectional terciles on the day's own distribution —
which needs no invented absolute number. Absolute floors can be set in
configuration once measured against the real 212-name distribution and approved.

Everything is pure: bars in, readings out, no clock read (the caller passes
``as_of`` where staleness matters), and the same inputs always produce the same
snapshot list — the deterministic-replay tests lean on exactly that property.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections import deque
from dataclasses import dataclass
from datetime import date
from typing import Any

from .atr import AtrState
from .ema import EmaState
from .structure import (
    AdxState,
    AdxValues,
    DonchianState,
    ObvState,
    SmaState,
)

#: The trend stack, quoted from the brief (§4.2) and the strategy library's
#: Golden Cross preset (SMA 50/200).
DEFAULT_EMA_FAST = 20
DEFAULT_EMA_MID = 50
DEFAULT_EMA_TREND = 200

#: Trend classes, coarse to strong. Strings: they are stored in snapshots.
TREND_STRONG_BULL = "STRONG_BULL"
TREND_BULL = "BULL"
TREND_EMERGING = "EMERGING"
TREND_NEUTRAL = "NEUTRAL"
TREND_WEAK = "WEAK"
TREND_UNKNOWN = "UNKNOWN"

#: Discovery groups, the brief's four (§4.4).
GROUP_STRONG_BULLISH = "STRONG_BULLISH"
GROUP_EMERGING_CANDIDATE = "EMERGING_CANDIDATE"
GROUP_NEUTRAL_WATCH = "NEUTRAL_WATCH"
GROUP_EXCLUDED = "EXCLUDED"

#: Cross-sectional classes.
CLASS_HIGH = "HIGH"
CLASS_MEDIUM = "MEDIUM"
CLASS_LOW = "LOW"
CLASS_INSUFFICIENT = "INSUFFICIENT"


def _positive_price(name: str, value: float) -> float:
    """Coerce and validate one bar price; a bad price is a data problem."""
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"daily bar {name} must be a number, got {value!r}") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"daily bar {name} must be a positive finite number, got {value!r}")
    return number


def _finite_volume(value: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"daily bar volume must be a number, got {value!r}") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"daily bar volume must be a finite non-negative number, got {value!r}")
    return number


@dataclass(frozen=True)
class DailyBar:
    """One completed daily bar; the shape ``candles_1d`` rows reduce to."""

    trading_date: date
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class DiscoveryParams:
    """The discovery layer's knobs — all ship as documented defaults.

    ``min_price=None`` is the brief's default discipline: the floor exists in
    the schema of this configuration but is **not armed**, because an absolute
    threshold is deployed only after it is measured against the real universe
    and approved. ``max_staleness_days`` counts calendar days (about five
    sessions allowing for a weekend) so the module needs no trading calendar.
    """

    ema_fast: int = DEFAULT_EMA_FAST
    ema_mid: int = DEFAULT_EMA_MID
    ema_trend: int = DEFAULT_EMA_TREND
    atr_period: int = 14
    roc_period: int = 12
    turnover_window: int = 20
    donchian_period: int = 20
    ema_rising_lookback: int = 5
    fresh_high_window: int = 5
    min_history_bars: int = 50
    min_price: float | None = None
    max_staleness_days: int = 7

    def problems(self) -> list[str]:
        found: list[str] = []
        for name in (
            "ema_fast",
            "ema_mid",
            "ema_trend",
            "atr_period",
            "roc_period",
            "turnover_window",
            "donchian_period",
            "ema_rising_lookback",
            "fresh_high_window",
            "min_history_bars",
            "max_staleness_days",
        ):
            if getattr(self, name) < 1:
                found.append(f"{name} must be >= 1")
        if not (self.ema_fast < self.ema_mid < self.ema_trend):
            found.append("EMA periods must be strictly increasing")
        if self.min_history_bars < self.ema_mid:
            found.append("min_history_bars must allow the mid EMA to warm")
        if self.min_price is not None and self.min_price <= 0:
            found.append("min_price must be positive when armed")
        return found


@dataclass(frozen=True)
class DailyReading:
    """One instrument's daily readings at its latest completed bar.

    Every indicator field is ``None`` until its own history exists — never a
    fabricated zero (the RSI-5m contract, kept on the daily timeframe).
    """

    symbol: str
    trading_date: date
    close: float
    bars: int
    ema20: float | None
    ema50: float | None
    ema200: float | None
    sma50: float | None
    sma200: float | None
    sma50_above_sma200: bool | None
    golden_cross_sessions: int | None
    atr14: float | None
    atr14_pct: float | None
    roc12: float | None
    turnover20: float | None
    turnover_ratio5_20: float | None
    donchian_upper: float | None
    donchian_lower: float | None
    fresh_high: bool
    sessions_since_fresh: int | None
    fresh_high_recent: bool
    ema20_rising: bool | None
    obv: float
    obv_rising: bool | None
    adx: AdxValues | None
    trend_class: str

    @property
    def fresh_high_session(self) -> bool:
        """The breakout happened on the latest completed session."""
        return self.sessions_since_fresh == 0


class DailyFeatures:
    """Per-instrument daily fold (definition and rules in the module docstring).

    Feed completed bars in order, oldest first. The fold keeps only the trailing
    windows the features need; nothing is forward-filled and a missing session
    is simply absent — the next real bar continues every recursion.
    """

    def __init__(self, symbol: str, *, params: DiscoveryParams | None = None) -> None:
        self.symbol = symbol
        self.params = params or DiscoveryParams()
        problems = self.params.problems()
        if problems:
            raise ValueError("invalid discovery parameters: " + "; ".join(problems))
        p = self.params
        self._ema20 = EmaState(period=p.ema_fast)
        self._ema50 = EmaState(period=p.ema_mid)
        self._ema200 = EmaState(period=p.ema_trend)
        self._sma50 = SmaState(period=p.ema_mid)
        self._sma200 = SmaState(period=p.ema_trend)
        self._atr = AtrState(period=p.atr_period)
        self._donchian = DonchianState(period=p.donchian_period)
        self._obv = ObvState()
        self._adx = AdxState(period=p.atr_period)
        self._closes: deque[float] = deque(maxlen=p.roc_period)
        self._turnovers: deque[float] = deque(maxlen=p.turnover_window)
        self._turnovers_short: deque[float] = deque(maxlen=5)
        self._ema20_history: deque[float] = deque(maxlen=p.ema_rising_lookback)
        self._sessions_since_fresh: int | None = None
        self._sma50_above: bool | None = None
        self._golden_cross_sessions: int | None = None
        self._bars = 0
        self.current: DailyReading | None = None

    def update(self, bar: DailyBar) -> DailyReading:
        """Feed one completed daily bar; returns the current reading."""
        high = _positive_price("high", bar.high)
        low = _positive_price("low", bar.low)
        close = _positive_price("close", bar.close)
        volume = _finite_volume(bar.volume)
        if high < low:
            raise ValueError(f"daily bar has high {high} below low {low}")

        self._bars += 1
        ema20 = self._ema20.update(close)
        ema50 = self._ema50.update(close)
        ema200 = self._ema200.update(close)
        sma50 = self._sma50.update(close)
        sma200 = self._sma200.update(close)
        atr14 = self._atr.update(high=high, low=low, close=close)
        adx = self._adx.update(high=high, low=low, close=close)
        obv = self._obv.update(close=close, volume=volume)

        roc12: float | None = None
        if len(self._closes) == self.params.roc_period:
            base = self._closes[0]
            roc12 = (close / base - 1.0) * 100.0
        self._closes.append(close)

        turnover = close * volume
        self._turnovers.append(turnover)
        self._turnovers_short.append(turnover)
        turnover20 = (
            sum(self._turnovers) / self.params.turnover_window
            if len(self._turnovers) == self.params.turnover_window
            else None
        )
        turnover_ratio5_20: float | None = None
        if turnover20 not in (None, 0.0) and len(self._turnovers_short) == 5:
            turnover_ratio5_20 = (sum(self._turnovers_short) / 5.0) / turnover20

        channel = self._donchian.update(high=high, low=low)
        if channel is not None:
            if close > channel.upper:
                self._sessions_since_fresh = 0
            elif self._sessions_since_fresh is not None:
                self._sessions_since_fresh += 1

        ema20_rising: bool | None = None
        if ema20 is not None:
            ema20_rising = (
                ema20 > self._ema20_history[0]
                if len(self._ema20_history) == self.params.ema_rising_lookback
                else None
            )
            self._ema20_history.append(ema20)

        sma50_above: bool | None = None
        if sma50 is not None and sma200 is not None:
            sma50_above = sma50 > sma200
            if self._sma50_above is not None and self._sma50_above and sma50_above:
                if self._golden_cross_sessions is not None:
                    self._golden_cross_sessions += 1
            elif self._sma50_above is False and sma50_above:
                self._golden_cross_sessions = 0
            elif not sma50_above:
                self._golden_cross_sessions = None
            # First measurable bar already above: the cross predates the
            # window, so the age is not knowable — it stays None, never guessed.
            self._sma50_above = sma50_above

        sessions_since_fresh = self._sessions_since_fresh
        fresh_high = sessions_since_fresh == 0
        fresh_high_recent = (
            sessions_since_fresh is not None
            and sessions_since_fresh <= self.params.fresh_high_window
        )

        trend_class = classify_trend(
            close=close,
            ema20=ema20,
            ema50=ema50,
            ema200=ema200,
            fresh_high_recent=fresh_high_recent,
            ema20_rising=ema20_rising,
        )

        atr14_pct = (
            atr14 / close * 100.0 if atr14 is not None else None
        )
        self.current = DailyReading(
            symbol=self.symbol,
            trading_date=bar.trading_date,
            close=close,
            bars=self._bars,
            ema20=ema20,
            ema50=ema50,
            ema200=ema200,
            sma50=sma50,
            sma200=sma200,
            sma50_above_sma200=self._sma50_above,
            golden_cross_sessions=self._golden_cross_sessions,
            atr14=atr14,
            atr14_pct=atr14_pct,
            roc12=roc12,
            turnover20=turnover20,
            turnover_ratio5_20=turnover_ratio5_20,
            donchian_upper=channel.upper if channel is not None else None,
            donchian_lower=channel.lower if channel is not None else None,
            fresh_high=fresh_high,
            sessions_since_fresh=sessions_since_fresh,
            fresh_high_recent=fresh_high_recent,
            ema20_rising=ema20_rising,
            obv=obv,
            obv_rising=self._obv.rising,
            adx=adx if adx.plus_di is not None else None,
            trend_class=trend_class,
        )
        return self.current

    def replay(self, bars: list[DailyBar]) -> DailyReading | None:
        """Fold a whole series (oldest first); ``None`` for an empty series."""
        reading: DailyReading | None = None
        for bar in bars:
            reading = self.update(bar)
        return reading


def classify_trend(
    *,
    close: float,
    ema20: float | None,
    ema50: float | None,
    ema200: float | None,
    fresh_high_recent: bool,
    ema20_rising: bool | None,
) -> str:
    """The Stage 1 trend class (rules in the module docstring); ordered checks."""
    if ema20 is None:
        return TREND_UNKNOWN
    if ema50 is not None and ema200 is not None and close > ema20 > ema50 > ema200:
        return TREND_STRONG_BULL
    if ema50 is not None and close > ema20 and close > ema50:
        return TREND_BULL
    if close > ema20:
        evidence = fresh_high_recent or (
            ema20_rising is True and (ema50 is None or close <= ema50)
        )
        if evidence:
            return TREND_EMERGING
        return TREND_NEUTRAL
    if close < ema20:
        return TREND_WEAK
    # close == ema20: no strict side exists; the honest read is the neutral one.
    return TREND_NEUTRAL


@dataclass(frozen=True)
class DiscoverySnapshot:
    """One ``(as_of, symbol)`` row of the persisted discovery snapshot (§4.5)."""

    as_of: date
    symbol: str
    group: str
    trend_class: str
    liquidity_class: str
    volatility_class: str
    relative_strength: float | None
    eligibility: bool
    reason: str
    indicators: dict[str, Any]
    completeness: float


def _fraction_below(value: float, sorted_values: list[float]) -> float | None:
    """The fraction of a sorted series strictly below ``value`` (0..1).

    Used for both the RS percentile and the tercile bins, so the two cuts share
    one definition of "where does this value sit". Fewer than two values means
    there is no distribution to sit in: ``None``.
    """
    if len(sorted_values) < 2:
        return None
    return bisect_left(sorted_values, value) / (len(sorted_values) - 1)


def _tercile(value: float | None, sorted_values: list[float]) -> str:
    """LOW / MEDIUM / HIGH by the day's own distribution; INSUFFICIENT if thin."""
    if value is None:
        return CLASS_INSUFFICIENT
    fraction = _fraction_below(value, sorted_values)
    if fraction is None:
        return CLASS_INSUFFICIENT
    if fraction < 1.0 / 3.0:
        return CLASS_LOW
    if fraction < 2.0 / 3.0:
        return CLASS_MEDIUM
    return CLASS_HIGH


def classify_universe(
    readings: list[DailyReading],
    *,
    as_of: date,
    params: DiscoveryParams | None = None,
) -> list[DiscoverySnapshot]:
    """Assign every reading its groups and cross-sectional classes.

    Deterministic and pure: the same readings and ``as_of`` always produce the
    same snapshot list (output sorted by symbol). ``EXCLUDED`` is checked first
    and is the only group that claims a stock should not proceed to opening
    evaluation — and only when ``stage2_gate`` is armed does the live scanner
    even read that verdict.
    """
    p = params or DiscoveryParams()
    liquidity_values = sorted(
        r.turnover20 for r in readings if r.turnover20 is not None
    )
    volatility_values = sorted(
        r.atr14_pct for r in readings if r.atr14_pct is not None
    )
    rs_values = sorted(r.roc12 for r in readings if r.roc12 is not None)

    snapshots: list[DiscoverySnapshot] = []
    for reading in readings:
        exclusion = _exclusion_reason(reading, as_of=as_of, params=p)
        liquidity_class = _tercile(reading.turnover20, liquidity_values)
        volatility_class = _tercile(reading.atr14_pct, volatility_values)
        rs_fraction = (
            _fraction_below(reading.roc12, rs_values)
            if reading.roc12 is not None
            else None
        )
        relative_strength = rs_fraction * 100.0 if rs_fraction is not None else None

        if exclusion is not None:
            group = GROUP_EXCLUDED
            eligibility = False
            reason = exclusion
        else:
            group = _group_for(reading.trend_class)
            eligibility = True
            reason = _basis(reading)

        indicators: dict[str, Any] = {
            "ema20": reading.ema20,
            "ema50": reading.ema50,
            "ema200": reading.ema200,
            "sma50": reading.sma50,
            "sma200": reading.sma200,
            "sma50_above_sma200": reading.sma50_above_sma200,
            "golden_cross_sessions": reading.golden_cross_sessions,
            "atr14_pct": reading.atr14_pct,
            "roc12": reading.roc12,
            "turnover20": reading.turnover20,
            "turnover_ratio5_20": reading.turnover_ratio5_20,
            "donchian_upper": reading.donchian_upper,
            "donchian_lower": reading.donchian_lower,
            "fresh_high": reading.fresh_high,
            "sessions_since_fresh": reading.sessions_since_fresh,
            "fresh_high_recent": reading.fresh_high_recent,
            "ema20_rising": reading.ema20_rising,
            "obv": reading.obv,
            "obv_rising": reading.obv_rising,
            "adx14": reading.adx.adx if reading.adx is not None else None,
            "plus_di": reading.adx.plus_di if reading.adx is not None else None,
            "minus_di": reading.adx.minus_di if reading.adx is not None else None,
        }
        present = sum(1 for value in indicators.values() if value is not None)
        completeness = present / len(indicators)

        snapshots.append(
            DiscoverySnapshot(
                as_of=as_of,
                symbol=reading.symbol,
                group=group,
                trend_class=reading.trend_class,
                liquidity_class=liquidity_class,
                volatility_class=volatility_class,
                relative_strength=relative_strength,
                eligibility=eligibility,
                reason=reason,
                indicators=indicators,
                completeness=completeness,
            )
        )
    snapshots.sort(key=lambda snapshot: snapshot.symbol)
    return snapshots


def _exclusion_reason(
    reading: DailyReading, *, as_of: date, params: DiscoveryParams
) -> str | None:
    """The first mandatory tradability failure, or ``None`` when eligible."""
    age_days = (as_of - reading.trading_date).days
    if age_days > params.max_staleness_days:
        return f"stale_last_bar:{reading.trading_date.isoformat()}"
    if reading.bars < params.min_history_bars:
        return f"insufficient_history:{reading.bars}"
    if params.min_price is not None and reading.close < params.min_price:
        return f"price_below_floor:{reading.close:g}"
    return None


def _group_for(trend_class: str) -> str:
    if trend_class == TREND_STRONG_BULL:
        return GROUP_STRONG_BULLISH
    if trend_class in (TREND_BULL, TREND_EMERGING):
        return GROUP_EMERGING_CANDIDATE
    return GROUP_NEUTRAL_WATCH


def _basis(reading: DailyReading) -> str:
    """One short human line of why this class holds (audit §5.5 reason field)."""
    if reading.trend_class == TREND_STRONG_BULL:
        return "close>ema20>ema50>ema200"
    if reading.trend_class == TREND_BULL:
        return "close>ema20 and close>ema50"
    if reading.trend_class == TREND_EMERGING:
        if reading.fresh_high_recent:
            age = reading.sessions_since_fresh
            return f"fresh_20bar_high:{age} sessions ago" if age else "fresh_20bar_high:today"
        return "ema20 rising, close below ema50"
    if reading.trend_class == TREND_NEUTRAL:
        return "above ema20, no fresh evidence"
    if reading.trend_class == TREND_WEAK:
        return "close<ema20"
    return "insufficient history for the trend stack"
