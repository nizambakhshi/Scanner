"""Transparent candidate ranking (spec §11.2).

This is deliberately *not* a prediction model. Each component is a published
value with a published clipping range, normalized linearly to 0–100 and
combined with configurable weights that must sum to 1.0. A user can read the
score and reproduce it by hand from the numbers on the row.

Clipping (rather than cross-sectional min-max) keeps two properties that matter
for an intraday tool: the score of a stock does not change because some other
stock printed an outlier, and the ranking is reproducible after a worker
restart with a different number of live candidates.

The score only orders candidates. It never overrides the mandatory bullish
conditions: a stock with a great score and a failed condition is not added.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import ScannerParams
from ..models import IndicatorSnapshot

# Clipping ranges: wide enough for real intraday moves, tight enough that one
# 40% gapper cannot dominate the table.
PRICE_STRENGTH_RANGE = (-10.0, 15.0)
RELATIVE_VOLUME_RANGE = (0.0, 10.0)
VWAP_DISTANCE_RANGE = (0.0, 5.0)
EMA_SPREAD_RANGE = (0.0, 5.0)


@dataclass(frozen=True)
class RankComponents:
    """Normalized (0–100) components plus the weighted total for one candidate."""

    price_strength: float
    relative_volume: float
    vwap_strength: float
    ema_spread: float
    score: float

    def as_dict(self) -> dict[str, float]:
        return {
            "price_strength": round(self.price_strength, 2),
            "relative_volume": round(self.relative_volume, 2),
            "vwap_strength": round(self.vwap_strength, 2),
            "ema_spread": round(self.ema_spread, 2),
            "score": round(self.score, 2),
        }


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _normalize(value: float | None, low: float, high: float, *, floor_at_zero: bool = False) -> float:
    """Linear map of ``value`` from [low, high] onto 0–100."""
    if value is None:
        return 0.0
    clipped = _clip(value, low, high)
    if floor_at_zero and clipped < 0:
        clipped = 0.0
    if high == low:
        return 0.0
    return (clipped - low) / (high - low) * 100.0


def score_snapshot(snapshot: IndicatorSnapshot, params: ScannerParams) -> RankComponents:
    """Compute the score for one evaluated candle.

    Missing inputs score 0 for that component — a stock cannot gain rank from
    data the feed did not provide.
    """
    price_norm = _normalize(
        snapshot.intraday_change_pct, *PRICE_STRENGTH_RANGE
    )
    rv_norm = _normalize(snapshot.relative_volume, *RELATIVE_VOLUME_RANGE)
    vwap_norm = _normalize(
        snapshot.distance_from_vwap_pct, *VWAP_DISTANCE_RANGE, floor_at_zero=True
    )
    ema_norm = _normalize(snapshot.ema_spread_pct, *EMA_SPREAD_RANGE, floor_at_zero=True)

    score = (
        params.rank_weight_price_strength * price_norm
        + params.rank_weight_relative_volume * rv_norm
        + params.rank_weight_vwap_strength * vwap_norm
        + params.rank_weight_ema_spread * ema_norm
    )
    return RankComponents(
        price_strength=price_norm,
        relative_volume=rv_norm,
        vwap_strength=vwap_norm,
        ema_spread=ema_norm,
        score=round(score, 2),
    )


def rank_candidates(
    snapshots: list[IndicatorSnapshot], params: ScannerParams
) -> list[tuple[IndicatorSnapshot, RankComponents]]:
    """Score every candidate and order them best-first.

    Ties break on relative volume then symbol, so ranking is deterministic and
    survives a restart (no dictionary-ordering artefacts).
    """
    scored = [(snapshot, score_snapshot(snapshot, params)) for snapshot in snapshots]
    scored.sort(
        key=lambda pair: (
            -pair[1].score,
            -(pair[0].relative_volume or 0.0),
            pair[0].instrument_id,
        )
    )
    return scored
