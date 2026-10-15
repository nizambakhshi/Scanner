"""The setups the engine watches for: ORB breakout-retest and the VWAP reclaim.

The definitions, written once here so tests and the later scoring phases agree.

**ORB breakout-retest:**

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

**VWAP reclaim:**

* the **trigger** is the ``vwap`` UP event the same detector raises — its
  hysteresis cross with the band and the confirm candles already applied. The
  event is the primitive and this module never re-derives it: one
  implementation of "the tape reclaimed its VWAP", consumed everywhere.
* the **dip** is what makes a reclaim a setup rather than noise. The tracker
  counts, per instrument, the completed candles that closed strictly below the
  session VWAP and remembers the lowest low among them. A run ends when a
  candle closes at or above the VWAP; the run behind the standing tape is the
  dip a reclaim event must find, and a cross with no dip behind it — the day
  opened above its VWAP and never lost it, or a tape this tracker never
  watched below it — produces no candidate. The determination of the day's
  first side is the *detector's* silence to keep (it raises no event for it);
  the tracker's guard is the structural twin, and together they mean a
  "reclaim" of a VWAP the tape never lost cannot exist. The run is measured
  against the VWAP itself, not the detector's band: the dip is evidence about
  the day's tape, measured once, while the band belongs to the trigger's
  hysteresis.
* a **candidate** is emitted for the crossing candle (the event's own candle —
  a reclaim's close *is* the event's price) and consumes the dip: one dip, one
  candidate. The detector's own side grammar guarantees a later cross starts
  from a fresh dip, because the side it flips back from below can only be
  re-entered through a loss event, and a loss is a new below-run by
  construction.

A leg sees only candles after the one that broke the level: the break candle
itself is judged at its close and may have printed its low on the way out. A
dip is symmetric: only candles the tracker was fed count, and a foreign-date
candle ends whatever run the previous session left standing.

The candidates carry the **evidence** the next phases read — never a verdict:
for a retest, the extension made before returning (highest high since the
break, seeded with the break price, expressed in ATR units when the break
candle knew an ATR), the bars waited, how deep the retest dipped
(``depth_pct``), the volume ratio on the reclaim candle, and the ATR at the
break; for a reclaim, the dip's depth and length, the level it crossed, and
the ATR at the crossing. Scoring and anti-chase state the policy later; this
module's only job is to observe exactly and to drop a leg or a dip the moment
it is no longer the same idea.

Scopes, stated because this engine keeps getting them wrong in review: one leg
per ORB event, one candidate per leg, one candidate per dip, legs and dips live
only inside their own trading day (a foreign-date candle drops them — their
session is over), and there is **no restart recovery in this phase**: a
mid-session restart begins with no legs and no dip, the same treatment the RSI
baseline gets. Nothing consumes the candidates yet — no alert, no row, no
snapshot — the pipeline feeds the tracker and hands its candidates to the
anti-chase policy so the behaviour is exercised end to end, and the phases
that act on candidates land next.
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


@dataclass
class DipRun:
    """One stretch of closes below the session VWAP (mutable: grows candle by candle).

    ``ended`` marks the first close at or above the VWAP — the reclaim may still
    be building its confirm candles, so the run stays readable until the
    detector's event consumes it, and a close below the VWAP afterwards is a
    *new* run, not a continuation.
    """

    trading_date: date
    bars: int = 0
    low: float | None = None
    ended: bool = False


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


@dataclass(frozen=True)
class ReclaimCandidate:
    """A reclaimed VWAP, with the dip that stood behind the cross."""

    instrument_id: int
    symbol: str
    trading_date: date
    level: float
    reclaim_at: datetime
    reclaim_price: float
    #: Lowest low of the dip — closes strictly below the VWAP and the lows
    #: among them. The natural stop basis for this setup.
    dip_low: float
    bars_below: int
    volume_ratio: float | None
    atr_at_reclaim: float | None

    @property
    def depth_pct(self) -> float:
        """How far below the VWAP the dip dipped, in percent of the level."""
        return (self.level - self.dip_low) / self.level * 100.0

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "level": round(self.level, 4),
            "reclaim_price": round(self.reclaim_price, 4),
            "dip_low": round(self.dip_low, 4),
            "depth_pct": round(self.depth_pct, 3),
            "bars_below": self.bars_below,
            "volume_ratio": None if self.volume_ratio is None else round(self.volume_ratio, 2),
            "reclaim_at": self.reclaim_at.isoformat(),
        }


class SetupTracker:
    """Per-instrument legs and dips, fed by the detector events and the candle stream."""

    def __init__(self, *, params: SetupParams | None = None) -> None:
        self.params = params or SetupParams()
        problems = self.params.problems()
        if problems:
            raise ValueError("invalid setup parameters: " + "; ".join(problems))
        self._legs: dict[int, list[Leg]] = {}
        self._dips: dict[int, DipRun] = {}
        #: The tracker's session: the date of the first candle it is fed. A
        #: candle from another day can only end a run, never seed one.
        self._day: date | None = None

    # ── feeds ──────────────────────────────────────────────────────────────
    def on_event(
        self, event: BreakoutEvent, *, atr: float | None = None
    ) -> Leg | ReclaimCandidate | None:
        """Consume one detector event; the filter for what belongs here lives in one place.

        An ORB UP break opens a retest leg and returns it (the live state: assert
        on it, do not write to it). A VWAP UP cross consults the standing dip and
        returns the reclaim candidate it proves — or ``None`` when no dip stands
        behind the cross. Every other event is not this module's news.
        """
        if event.strategy == "vwap" and event.direction == UP:
            return self._reclaim(event, atr=atr)
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

    def _reclaim(self, event: BreakoutEvent, *, atr: float | None) -> ReclaimCandidate | None:
        run = self._dips.get(event.instrument_id)
        if run is None or run.bars == 0 or run.trading_date != event.trading_date:
            # No dip behind the cross: the day opened above its VWAP and never
            # lost it, or a tape this tracker never watched below it. The event
            # is real; the setup is not ours to claim.
            return None
        assert run.low is not None  # set with the first closing bar of the run
        del self._dips[event.instrument_id]  # one dip, one candidate
        return ReclaimCandidate(
            instrument_id=event.instrument_id,
            symbol=event.symbol,
            trading_date=event.trading_date,
            level=event.level,
            reclaim_at=event.candle_start,
            reclaim_price=event.price,
            dip_low=run.low,
            bars_below=run.bars,
            volume_ratio=event.volume_ratio,
            atr_at_reclaim=atr,
        )

    def on_candle(
        self,
        candle: Bar,
        *,
        symbol: str,
        relative_volume: float | None = None,
        vwap: float | None = None,
    ) -> list[RetestCandidate]:
        """Advance the dip watch and every open leg of this instrument with one candle.

        The break candle must have been fed to :meth:`on_event` *after* this
        candle was advanced (the pipeline's order): the retest window opens on
        the first candle after the break, never on the break itself.
        """
        self._advance_dip(candle, vwap=vwap)
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

    def _advance_dip(self, candle: Bar, *, vwap: float | None) -> None:
        """Track the running below-VWAP dip for one instrument (definitions above).

        The tracker's session is the date of the first candle it is fed; a
        candle from another day can only *end* a run — warm-up history and a
        replay must never seed today's dips. A missing VWAP pauses the watch
        instead: the tape neither extends the dip nor ends it when nothing was
        *said*.
        """
        if self._day is None:
            self._day = candle.trading_date
        if candle.trading_date != self._day:
            self._dips.pop(candle.instrument_id, None)
            return
        run = self._dips.get(candle.instrument_id)
        if vwap is None or vwap <= 0:
            return
        if candle.close < vwap:
            if run is None or run.ended:
                run = DipRun(trading_date=self._day)
                self._dips[candle.instrument_id] = run
            run.bars += 1
            run.low = candle.low if run.low is None else min(run.low, candle.low)
        elif run is not None:
            run.ended = True

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

    def dip_for(self, instrument_id: int) -> DipRun | None:
        """The standing dip run — assert on it, do not write to it."""
        return self._dips.get(instrument_id)

    @property
    def open_leg_count(self) -> int:
        return sum(len(legs) for legs in self._legs.values())

    @property
    def open_dip_count(self) -> int:
        """Dips still being extended — a run whose reclaim has not crossed yet."""
        return sum(1 for run in self._dips.values() if not run.ended)

    def reset(self) -> None:
        """Session boundary: no leg or dip outlives its trading day."""
        self._legs.clear()
        self._dips.clear()
        self._day = None
