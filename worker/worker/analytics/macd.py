"""MACD (12, 26, 9): the second momentum reading, on the same five-minute closes.

Definition — written down once here so every surface computes the same numbers:

* the **MACD line** is ``EMA(fast) − EMA(slow)`` over completed 5-minute
  closes, using the house EMA convention (SMA seed, then
  ``EMA_t = α·C_t + (1−α)·EMA_{t−1}``, ``α = 2/(n+1)``) from
  :mod:`worker.analytics.ema`;
* the **signal line** is that same recursion applied to the MACD line values,
  seeded with the SMA of the first ``signal_period`` of them — a *signed*
  series, which is why the signal EMA is built with ``require_positive=False``;
* the **histogram** is ``MACD − signal``.

Availability: with ``(12, 26, 9)`` the MACD line first exists on the **26th**
completed bucket and the signal/histogram on the **34th**. On a session that
opens at 09:15 that is roughly 11:25 and 12:05 IST. Everything before then is
``None`` — an indicator that has not seen enough tape reads as absent, never as
a fabricated zero (the same contract the RSI-5m keeps).

Missing buckets are simply absent: the series continues from the last *real*
close and nothing is interpolated or forward-filled — the same rule the candle
engine follows. That is why ``observations`` counts fed closes rather than
wall-clock buckets.

Session scope: **this session only**, exactly like the RSI-5m and for the same
reason — an intraday reading describes the move that is happening now. The
state resets with the session boundary, alongside the VWAP and the RSI, and
prior-day warm-up candles never enter it.

Feeding contract: one close per completed 5-minute bucket, from the *same*
candle that closes the RSI's bucket (``closes_bucket`` in
:mod:`worker.analytics.rsi`) — the partition is one definition, not two.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .ema import EmaState

DEFAULT_FAST_PERIOD = 12
DEFAULT_SLOW_PERIOD = 26
DEFAULT_SIGNAL_PERIOD = 9


@dataclass(frozen=True)
class MacdValues:
    """One bucket's reading; each member is ``None`` until its stage has history."""

    macd: float | None
    signal: float | None
    histogram: float | None

    @property
    def ready(self) -> bool:
        """Both lines defined — the histogram exists exactly when this is true."""
        return self.macd is not None and self.signal is not None


def _reading(
    fast_value: float | None,
    slow_value: float | None,
    signal_value: float | None,
) -> MacdValues:
    """Assemble the triple from the three EMA states (one place, two callers)."""
    macd_line = (
        fast_value - slow_value
        if fast_value is not None and slow_value is not None
        else None
    )
    histogram = (
        macd_line - signal_value
        if macd_line is not None and signal_value is not None
        else None
    )
    return MacdValues(macd=macd_line, signal=signal_value, histogram=histogram)


@dataclass
class MacdState:
    """Streaming MACD over a series of 5-minute closes (definition above)."""

    fast_period: int = DEFAULT_FAST_PERIOD
    slow_period: int = DEFAULT_SLOW_PERIOD
    signal_period: int = DEFAULT_SIGNAL_PERIOD
    observations: int = 0
    current: MacdValues = field(default_factory=lambda: MacdValues(None, None, None))
    fast: EmaState = field(init=False)
    slow: EmaState = field(init=False)
    signal: EmaState = field(init=False)

    def __post_init__(self) -> None:
        if self.fast_period < 2:
            raise ValueError("MACD fast period must be >= 2")
        if self.slow_period <= self.fast_period:
            raise ValueError("MACD slow period must be greater than the fast period")
        if self.signal_period < 2:
            raise ValueError("MACD signal period must be >= 2")
        self.fast = EmaState(period=self.fast_period)
        self.slow = EmaState(period=self.slow_period)
        # The signal line smooths MACD values, which are signed: any finite
        # number is valid input (see EmaState.require_positive).
        self.signal = EmaState(period=self.signal_period, require_positive=False)

    @property
    def min_observations_for_signal(self) -> int:
        """Buckets needed before the signal (and histogram) exist: slow+signal−1."""
        return self.slow_period + self.signal_period - 1

    def update(self, close: float) -> MacdValues:
        """Feed one completed 5-minute close; returns the current triple.

        The two price EMAs always advance. The signal EMA advances only while a
        MACD line exists (i.e. once both price EMAs are warm), so skipping a
        bucket — a feed gap, a halted name — leaves the series as the next real
        close's history, never a synthesised one.
        """
        fast_value = self.fast.update(close)
        slow_value = self.slow.update(close)
        self.observations += 1
        if fast_value is not None and slow_value is not None:
            self.signal.update(fast_value - slow_value)
        self.current = _reading(fast_value, slow_value, self.signal.value)
        return self.current

    def to_dict(self) -> dict[str, Any]:
        """Checkpoint view. ``current`` is not stored: it is a function of the
        three EMA states and recomputed on restore, so there is exactly one
        source of truth for it."""
        return {
            "fast_period": self.fast_period,
            "slow_period": self.slow_period,
            "signal_period": self.signal_period,
            "observations": self.observations,
            "fast": self.fast.to_dict(),
            "slow": self.slow.to_dict(),
            "signal": self.signal.to_dict(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> MacdState:
        """Restore a checkpoint; the EMA states carry the whole history they need."""
        state = cls(
            fast_period=int(payload["fast_period"]),
            slow_period=int(payload["slow_period"]),
            signal_period=int(payload["signal_period"]),
        )
        state.fast = EmaState.from_dict(payload["fast"])
        state.slow = EmaState.from_dict(payload["slow"])
        state.signal = EmaState.from_dict(payload["signal"])
        state.observations = int(payload.get("observations", 0))
        state.current = _reading(state.fast.value, state.slow.value, state.signal.value)
        return state
