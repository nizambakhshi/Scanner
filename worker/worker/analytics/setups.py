"""ORB breakout-retest detection: the first setup the engine watches for.

The definitions, written once here so tests and the later scoring phases agree:

* a **leg** begins when the opening-range breakout fires — the ``orb`` UP event
  the existing tracker raises (:class:`worker.analytics.breakouts.BreakoutTracker`).
  UP only: the scanner is a bullish one and a bearish retest belongs to no queue
  that exists.
* a **touch** is a completed candle whose low returns to the level's band,
  ``low <= level·(1 + tolerance)``. The dip may undershoot the level — a retest
  that pokes slightly through and recovers is the classic shape — so the band is
  one-sided: at or below, never above.
* a **reclaim** — the candidate this module emits — is a completed candle that
  closes back **above** the level after a touch. The touch and the reclaim may
  be the same candle; then it is recorded once, with one timestamp.
* a **failure** is a close below ``level·(1 − tolerance)``: the level did not
  hold, the leg is dropped without a candidate. Checked before the touch, so a
  candle that both touched and closed far below is a failure, not a retest.
* **expiry**: a leg that has waited more than ``max_retest_bars`` completed
  candles since the break is dropped — a retest that takes longer than that is
  a different move, not this one.

A leg sees only candles after the one that broke the level: the break candle
itself is judged at its close and may have printed its low on the way out.

The candidate carries the **evidence** the next phases read — never a verdict:
the extension made before returning (highest high since the break, seeded with
the break price, expressed in ATR units when the break candle knew an ATR), the
bars waited, how deep the retest dipped (``depth_pct``), the volume ratio on the
reclaim candle, and the ATR at the break. Scoring and anti-chase state the
policy later; this module's only job is to observe exactly and to drop a leg
the moment it is no longer the same idea.

Scopes, stated because this engine keeps getting them wrong in review: one leg
per ORB event, one candidate per leg, legs live only inside their own trading
day (a foreign-date candle drops the leg — its session is over), and there is
**no restart recovery in this phase**: a mid-session restart begins with no
legs, the same treatment the RSI baseline gets. Nothing consumes the candidates
yet — no alert, no row, no snapshot — the pipeline feeds the tracker so the
behaviour is exercised end to end, and the phases that act on candidates land
next.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from .breakouts import UP, Bar, BreakoutEvent

#: How close to the level counts as "returned to it", in percent of the level.
#: Half a percent is the width of a real retest on the F&O names this engine
#: watches — tight enough that a drift 2% above the level is not a retest.
DEFAULT_RETEST_TOLERANCE_PCT = 0.5

#: Completed candles a leg may wait for its retest. Thirty minutes: beyond
#: that the move that broke the level has gone, and a "reclaim" would be a
#: fresh setup wearing an old level's name.
DEFAULT_MAX_RETEST_BARS = 30


@dataclass(frozen=True)
class SetupParams:
    """The setup engine's tunables (env/config hookup lands with its consumers)."""

    retest_tolerance_pct: float = DEFAULT_RETEST_TOLERANCE_PCT
    max_retest_bars: int = DEFAULT_MAX_RETEST_BARS

    def problems(self) -> list[str]:
        found: list[str] = []
        if not 0.0 <= self.retest_tolerance_pct <= 5.0:
            found.append("retest_tolerance_pct must be between 0 and 5")
        if not 1 <= self.max_retest_bars <= 240:
            found.append("max_retest_bars must be between 1 and 240")
        return found


@dataclass
class Leg:
    """One open breakout-retest watch (mutable: it advances candle by candle)."""

    level: float
    break_candle_start: datetime
    break_price: float
    trading_date: date
    atr_at_break: float | None = None
    bars_since_break: int = 0
    #: Highest high seen since the break, seeded with the break price — the
    #: break candle's own high is not observed here (the break is judged at
    #: its close), and the seed keeps the evidence monotonic from bar one.
    extension_max: float = 0.0
    touched_at: datetime | None = None
    min_low: float | None = None

    @property
    def touched(self) -> bool:
        return self.touched_at is not None


@dataclass(frozen=True)
class RetestCandidate:
    """A reclaimed level, with the evidence that surrounded the reclaim."""

    instrument_id: int
    symbol: str
    trading_date: date
    level: float
    break_candle_start: datetime
    break_price: float
    touched_at: datetime
    reclaim_at: datetime
    reclaim_price: float
    #: Lowest low of the touch band during the retest — the dip's depth.
    min_low: float
    extension_max: float
    bars_since_break: int
    volume_ratio: float | None
    atr_at_break: float | None

    @property
    def depth_pct(self) -> float:
        """How far below the level the retest dipped, in percent of the level.

        Positive means the dip crossed the level; zero or negative means the
        band held from above (the low never reached the price itself).
        """
        return (self.level - self.min_low) / self.level * 100.0

    @property
    def extension_atr(self) -> float | None:
        """The run beyond the level, in ATR units — ``None`` when no ATR was known."""
        if self.atr_at_break is None or self.atr_at_break <= 0:
            return None
        return (self.extension_max - self.level) / self.atr_at_break

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "level": round(self.level, 4),
            "reclaim_price": round(self.reclaim_price, 4),
            "depth_pct": round(self.depth_pct, 3),
            "extension_pct": round(
                (self.extension_max - self.level) / self.level * 100.0, 3
            ),
            "bars_since_break": self.bars_since_break,
            "volume_ratio": None if self.volume_ratio is None else round(self.volume_ratio, 2),
            "reclaim_at": self.reclaim_at.isoformat(),
        }


class SetupTracker:
    """Per-instrument legs, fed by the ORB events and then by the candle stream."""

    def __init__(self, *, params: SetupParams | None = None) -> None:
        self.params = params or SetupParams()
        problems = self.params.problems()
        if problems:
            raise ValueError("invalid setup parameters: " + "; ".join(problems))
        self._legs: dict[int, list[Leg]] = {}

    # ── feeds ──────────────────────────────────────────────────────────────
    def on_break(self, event: BreakoutEvent, *, atr: float | None = None) -> Leg | None:
        """Open a leg for an ORB UP break; anything else is not this module's event.

        Called with every breakout event — the filter lives here, once, rather
        than in every caller. The returned leg is the live state: assert on it,
        do not write to it.
        """
        if event.strategy != "orb" or event.direction != UP:
            return None
        leg = Leg(
            level=event.level,
            break_candle_start=event.candle_start,
            break_price=event.price,
            trading_date=event.trading_date,
            atr_at_break=atr,
            extension_max=event.price,
        )
        self._legs.setdefault(event.instrument_id, []).append(leg)
        return leg

    def on_candle(
        self,
        candle: Bar,
        *,
        symbol: str,
        relative_volume: float | None = None,
    ) -> list[RetestCandidate]:
        """Advance every open leg of this instrument with one completed candle.

        The break candle must have been fed to :meth:`on_break` *after* this
        candle was advanced (the pipeline's order): the retest window opens on
        the first candle after the break, never on the break itself.
        """
        legs = self._legs.get(candle.instrument_id)
        if not legs:
            return []
        tolerance = self.params.retest_tolerance_pct / 100.0
        candidates: list[RetestCandidate] = []
        survivors: list[Leg] = []
        for leg in legs:
            if candle.trading_date != leg.trading_date:
                continue  # its session is over; the leg dies with it
            leg.bars_since_break += 1
            if candle.high > leg.extension_max:
                leg.extension_max = candle.high
            if leg.bars_since_break > self.params.max_retest_bars:
                continue  # waited too long — a different move by now
            if candle.close < leg.level * (1.0 - tolerance):
                continue  # the level did not hold
            if candle.low <= leg.level * (1.0 + tolerance):
                if leg.touched_at is None:
                    leg.touched_at = candle.candle_start
                    leg.min_low = candle.low
                elif leg.min_low is None or candle.low < leg.min_low:
                    leg.min_low = candle.low
            if leg.touched and candle.close > leg.level:
                candidates.append(
                    self._candidate(
                        leg, candle, symbol=symbol, relative_volume=relative_volume
                    )
                )
                continue  # consumed: one leg, one candidate
            survivors.append(leg)
        self._legs[candle.instrument_id] = survivors
        return candidates

    def _candidate(
        self,
        leg: Leg,
        candle: Bar,
        *,
        symbol: str,
        relative_volume: float | None,
    ) -> RetestCandidate:
        assert leg.touched_at is not None and leg.min_low is not None  # set together
        return RetestCandidate(
            instrument_id=candle.instrument_id,
            symbol=symbol,
            trading_date=leg.trading_date,
            level=leg.level,
            break_candle_start=leg.break_candle_start,
            break_price=leg.break_price,
            touched_at=leg.touched_at,
            reclaim_at=candle.candle_start,
            reclaim_price=candle.close,
            min_low=leg.min_low,
            extension_max=leg.extension_max,
            bars_since_break=leg.bars_since_break,
            volume_ratio=relative_volume,
            atr_at_break=leg.atr_at_break,
        )

    # ── introspection ──────────────────────────────────────────────────────
    def legs_for(self, instrument_id: int) -> tuple[Leg, ...]:
        return tuple(self._legs.get(instrument_id, ()))

    @property
    def open_leg_count(self) -> int:
        return sum(len(legs) for legs in self._legs.values())

    def reset(self) -> None:
        """Session boundary: no leg outlives its trading day."""
        self._legs.clear()
