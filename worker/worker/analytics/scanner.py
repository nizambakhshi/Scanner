"""Bullish qualification (spec §10.1).

The primary signal is exactly three conditions — close above session VWAP,
EMA(fast) above EMA(slow), relative volume at or above the threshold — plus
four data-integrity gates. A condition can only be *evaluated* when every
value it needs is valid; anything else is a rejection with a recorded reason,
never a silent pass.

Reason codes are stable strings stored on the signal and shown in the UI, so a
user can read *why* something was added or refused.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..config import ScannerParams
from ..models import Candle, DataQuality, IndicatorSnapshot
from .indicators import IndicatorValues

# ── reason codes ────────────────────────────────────────────────────────────
R_CLOSE_ABOVE_VWAP = "CLOSE_ABOVE_VWAP"
R_EMA_FAST_ABOVE_SLOW = "EMA9_ABOVE_EMA20"
R_RELATIVE_VOLUME_OK = "RELATIVE_VOLUME_ABOVE_THRESHOLD"

F_CANDLE_INCOMPLETE = "CANDLE_INCOMPLETE"
F_DATA_QUALITY = "DATA_QUALITY_FAILED"
F_FEED_STALE = "FEED_STALE"
F_INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY_WARMING_UP"
F_VWAP_UNAVAILABLE = "SESSION_VWAP_UNAVAILABLE"
F_CLOSE_BELOW_VWAP = "CLOSE_AT_OR_BELOW_VWAP"
F_EMA_NOT_ALIGNED = "EMA_NOT_ALIGNED"
F_RELATIVE_VOLUME_LOW = "RELATIVE_VOLUME_BELOW_THRESHOLD"
F_VOLUME_BASELINE_INVALID = "VOLUME_BASELINE_INVALID"
F_INVALID_PRICE = "INVALID_PRICE"
F_OUTSIDE_UNIVERSE = "OUTSIDE_CONFIGURED_UNIVERSE"


@dataclass(frozen=True)
class ScanContext:
    """Everything needed to evaluate one instrument at one moment."""

    instrument_id: int
    symbol: str
    in_universe: bool
    previous_close: float | None
    feed_age_seconds: float | None
    session_candle_count: int
    evaluation_time: datetime
    is_simulated: bool = False


@dataclass
class EvaluationResult:
    """Outcome for one candle evaluation, with the numbers the UI displays."""

    instrument_id: int
    symbol: str
    candle_start: datetime
    close: float
    vwap: float | None
    ema_fast: float | None
    ema_slow: float | None
    relative_volume: float | None
    average_volume: float | None
    intraday_change_pct: float | None
    distance_from_vwap_pct: float | None
    ema_spread_pct: float | None
    session_high: float | None
    session_low: float | None
    distance_from_high_pct: float | None
    bullish: bool
    reasons: tuple[str, ...]
    failed_reasons: tuple[str, ...]
    data_quality: DataQuality
    warming_up: bool
    evaluable: bool
    detail: dict[str, object] = field(default_factory=dict)

    def to_snapshot(self, *, trading_date, config_version: int, is_simulated: bool) -> IndicatorSnapshot:
        return IndicatorSnapshot(
            instrument_id=self.instrument_id,
            trading_date=trading_date,
            candle_start=self.candle_start,
            close=self.close,
            previous_close=_num(self.detail.get("previous_close")),
            vwap=self.vwap,
            ema_fast=self.ema_fast,
            ema_slow=self.ema_slow,
            average_volume=self.average_volume,
            relative_volume=self.relative_volume,
            intraday_change_pct=self.intraday_change_pct,
            distance_from_vwap_pct=self.distance_from_vwap_pct,
            ema_spread_pct=self.ema_spread_pct,
            session_high=self.session_high,
            session_low=self.session_low,
            distance_from_high_pct=self.distance_from_high_pct,
            bullish_qualified=self.bullish,
            reasons=self.reasons,
            failed_reasons=self.failed_reasons,
            data_quality=self.data_quality,
            warming_up=self.warming_up,
            score=self.detail.get("score") if isinstance(self.detail.get("score"), (int, float)) else None,
            is_simulated=is_simulated,
            config_version=config_version,
        )


def _num(value: object) -> float | None:
    return None if value is None else float(value)


class Scanner:
    """Applies the configured strategy to completed candles."""

    def __init__(self, params: ScannerParams) -> None:
        params.validate()
        self.params = params

    # ── helpers ─────────────────────────────────────────────────────────────
    @staticmethod
    def pct_change(value: float, reference: float) -> float | None:
        if reference is None or reference <= 0:
            return None
        return (value - reference) / reference * 100.0

    def evaluate(
        self,
        candle: Candle,
        indicators: IndicatorValues,
        context: ScanContext,
        *,
        session_high: float | None = None,
        session_low: float | None = None,
    ) -> EvaluationResult:
        params = self.params
        reasons: list[str] = []
        failed: list[str] = []

        close = candle.close
        price_valid = close is not None and close > 0
        if not price_valid:
            failed.append(F_INVALID_PRICE)

        # ── data-integrity gates ────────────────────────────────────────────
        if not candle.is_complete:
            failed.append(F_CANDLE_INCOMPLETE)
        if candle.data_quality in (DataQuality.STALE, DataQuality.SUSPECT, DataQuality.INCOMPLETE):
            failed.append(F_DATA_QUALITY)
        if not context.in_universe:
            failed.append(F_OUTSIDE_UNIVERSE)

        feed_age = context.feed_age_seconds
        feed_fresh = feed_age is None or feed_age <= params.feed_staleness_seconds
        if feed_age is not None and not feed_fresh:
            failed.append(F_FEED_STALE)

        insufficient_history = (
            indicators.candles_seen < params.warmup_candles_required
            or not indicators.warm
            or indicators.vwap is None
        )
        if insufficient_history:
            failed.append(F_INSUFFICIENT_HISTORY)
            if indicators.vwap is None:
                failed.append(F_VWAP_UNAVAILABLE)

        evaluable = not failed
        warming_up = indicators.candles_seen < params.warmup_candles_required or not indicators.warm

        # ── the three primary conditions ────────────────────────────────────
        vwap = indicators.vwap
        ema_fast = indicators.ema_fast
        ema_slow = indicators.ema_slow
        relative_volume = indicators.relative_volume

        if evaluable:
            if vwap is not None and close > vwap:
                reasons.append(R_CLOSE_ABOVE_VWAP)
            else:
                failed.append(F_CLOSE_BELOW_VWAP)

            if ema_fast is not None and ema_slow is not None and ema_fast > ema_slow:
                reasons.append(R_EMA_FAST_ABOVE_SLOW)
            else:
                failed.append(F_EMA_NOT_ALIGNED)

            if relative_volume is None:
                failed.append(F_VOLUME_BASELINE_INVALID)
            elif relative_volume + 1e-12 >= params.relative_volume_threshold:
                reasons.append(R_RELATIVE_VOLUME_OK)
            else:
                failed.append(F_RELATIVE_VOLUME_LOW)

        bullish = evaluable and len(failed) == 0 and len(reasons) == 3

        intraday_change = (
            self.pct_change(close, context.previous_close) if context.previous_close else None
        )
        distance_vwap = self.pct_change(close, vwap) if vwap else None
        ema_spread = (
            self.pct_change(ema_fast, ema_slow) if ema_fast and ema_slow else None
        )
        distance_high = (
            self.pct_change(close, session_high) if session_high else None
        )

        detail: dict[str, object] = {
            "previous_close": context.previous_close,
            "feed_age_seconds": feed_age,
            "session_candle_count": context.session_candle_count,
            "tick_count": candle.tick_count,
            "volume": candle.volume,
            "relative_volume_threshold": params.relative_volume_threshold,
            "warmup_required": params.warmup_candles_required,
            "evaluable": evaluable,
        }

        return EvaluationResult(
            instrument_id=candle.instrument_id,
            symbol=context.symbol,
            candle_start=candle.candle_start,
            close=close,
            vwap=vwap,
            ema_fast=ema_fast,
            ema_slow=ema_slow,
            relative_volume=relative_volume,
            average_volume=indicators.average_volume,
            intraday_change_pct=intraday_change,
            distance_from_vwap_pct=distance_vwap,
            ema_spread_pct=ema_spread,
            session_high=session_high,
            session_low=session_low,
            distance_from_high_pct=distance_high,
            bullish=bullish,
            reasons=tuple(reasons),
            failed_reasons=tuple(dict.fromkeys(failed)),
            data_quality=candle.data_quality,
            warming_up=warming_up,
            evaluable=evaluable,
            detail=detail,
        )

    # ── watchlist predicates (spec §11.5 / §11.6) ───────────────────────────
    @staticmethod
    def is_weakening(snapshot: IndicatorSnapshot) -> bool:
        """Close below VWAP *or* EMA(fast) at-or-below EMA(slow)."""
        if snapshot.vwap is None or snapshot.ema_fast is None or snapshot.ema_slow is None:
            return False
        return snapshot.close < snapshot.vwap or snapshot.ema_fast <= snapshot.ema_slow

    @staticmethod
    def is_removal_condition(snapshot: IndicatorSnapshot) -> bool:
        """Both trend conditions failing on the *same* valid candle."""
        if snapshot.vwap is None or snapshot.ema_fast is None or snapshot.ema_slow is None:
            return False
        return snapshot.close < snapshot.vwap and snapshot.ema_fast <= snapshot.ema_slow
