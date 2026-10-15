"""Wilder ATR(14): the move's own scale, measured on today's one-minute candles.

Definition — written down once here so every surface computes the same numbers:

* the **true range** of a candle is the largest of three spans,
  ``TR_t = max(H_t − L_t, |H_t − C_{t−1}|, |L_t − C_{t−1}|)``, where ``C_{t−1}``
  is the previous completed candle's close **within this session**;
* the session's **first** candle has no previous close in scope, so its true
  range is ``H − L`` — the standard convention, and the reason a gap from
  yesterday's close never leaks into today's measure;
* the first ``period`` true ranges form a **simple average**; that SMA becomes
  ``ATR_period``, and every later candle uses Wilder's recursion
  ``ATR_t = ((period − 1)·ATR_{t−1} + TR_t) / period`` — the same smoothing
  family the RSI engine uses, for the same reason (every charting package the
  operator compares against smooths this way).

Availability: with ``period = 14`` the first reading exists on the **14th**
completed candle — roughly 09:29 IST on a session that opens at 09:15.
Everything before that is ``None``: an indicator that has not seen enough tape
reads as absent, never as a fabricated zero (the contract the RSI-5m and the
MACD keep).

Missing minutes are simply absent: the recursion continues from the last *real*
close and nothing is interpolated or forward-filled — the same rule the candle
engine follows. ``observations`` counts fed candles rather than wall-clock
minutes, which is why a session with a feed gap still converges, just later.

Session scope: **this session only**. The state resets with the VWAP, the
RSI-5m and the MACD at the session boundary, and restart recovery rebuilds it
by replaying today's stored minutes (``session_candles=True`` in
:mod:`worker.analytics.indicators`) — the same rebuild-by-replay property every
session-scoped measure here has.

Purpose (audit D5, Step 2): the ATR is an **input to stop distance and
extension measurement, never a hard-coded threshold**. Nothing consumes it yet
(Phase 2C: foundation only — the setup engine's anti-chase policy reads it
next), and no alert gate may treat "price moved more than k·ATR" as a rule
until the operator's configuration says so.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

DEFAULT_ATR_PERIOD = 14


def _positive_price(name: str, value: float) -> float:
    """Coerce and validate one bar price; a bad price is a data problem."""
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"ATR {name} must be a number, got {value!r}") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"ATR {name} must be a positive finite number, got {value!r}")
    return number


@dataclass
class AtrState:
    """Streaming Wilder ATR over a session's completed candles (definition above)."""

    period: int = DEFAULT_ATR_PERIOD
    value: float | None = None
    initialized: bool = False
    observations: int = 0
    _seed: list[float] = field(default_factory=list, repr=False)
    _prev_close: float | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.period < 2:
            raise ValueError("ATR period must be >= 2")

    def update(self, *, high: float, low: float, close: float) -> float | None:
        """Feed one completed candle; returns the current ATR (``None`` until warm).

        A bar whose high is below its low is corrupt and raises: a negative
        span would quietly poison every later average, which is exactly the
        kind of number that looks plausible and is not.
        """
        high = _positive_price("high", high)
        low = _positive_price("low", low)
        close = _positive_price("close", close)
        if high < low:
            raise ValueError(f"ATR bar has high {high} below low {low}")
        if self._prev_close is None:
            true_range = high - low
        else:
            true_range = max(
                high - low,
                abs(high - self._prev_close),
                abs(low - self._prev_close),
            )
        self._prev_close = close
        self.observations += 1
        if not self.initialized:
            self._seed.append(true_range)
            if len(self._seed) < self.period:
                return None
            self.value = sum(self._seed) / self.period
            self.initialized = True
            self._seed = []
            return self.value
        # ``self.value`` is always set once initialized; the fallback mirrors
        # EmaState's belt-and-braces for a hand-built checkpoint.
        prior = self.value if self.value is not None else true_range
        self.value = ((self.period - 1.0) * prior + true_range) / self.period
        return self.value

    def to_dict(self) -> dict[str, Any]:
        """Checkpoint view; ``_seed`` and ``_prev_close`` carry everything the
        next true range needs, so a restored state continues the exact series."""
        return {
            "period": self.period,
            "value": self.value,
            "initialized": self.initialized,
            "observations": self.observations,
            "seed": list(self._seed),
            "prev_close": self._prev_close,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> AtrState:
        """Restore a checkpoint — mid-seed or initialized, both resume exactly."""
        state = cls(period=int(payload["period"]))
        state.value = None if payload.get("value") is None else float(payload["value"])
        state.initialized = bool(payload.get("initialized"))
        state.observations = int(payload.get("observations", 0))
        state._seed = [float(t) for t in payload.get("seed", [])]
        state._prev_close = (
            None if payload.get("prev_close") is None else float(payload["prev_close"])
        )
        return state
