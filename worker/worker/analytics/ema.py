"""The EMA engine: one definition, shared by every indicator that smooths.

EMA initialization convention (used identically by warm-up, live processing,
restart recovery and the tests): the first ``n`` valid closes form a simple
moving average, that SMA becomes ``EMA_n``, and every later close uses the
recursive form ``EMA_t = α·C_t + (1−α)·EMA_{t−1}`` with ``α = 2/(n+1)``.

This lives in its own module because two different series are smoothed with it:
price closes (the scanner's fast/slow EMAs) must reject a non-positive value as
a data problem, while the MACD signal line smooths the MACD line — a derived
series that legitimately crosses zero. Same recursion, one implementation,
``require_positive`` is the only difference; a second copy of the recursion is
how two surfaces start disagreeing with each other.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass
class EmaState:
    """Streaming EMA with SMA seed (documented convention above).

    ``require_positive`` (the default) is the price-series contract: a close at
    or below zero raises, because a bad price must surface as a data problem
    rather than as a plausible-looking average. ``False`` is for derived series
    that legitimately cross zero (the MACD signal line), where the input must
    only be a finite number.
    """

    period: int
    require_positive: bool = True
    alpha: float = field(init=False)
    value: float | None = None
    initialized: bool = False
    _seed: list[float] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        if self.period < 2:
            raise ValueError("EMA period must be >= 2")
        self.alpha = 2.0 / (self.period + 1.0)

    @property
    def warm(self) -> bool:
        return self.initialized

    def update(self, close: float) -> float | None:
        if self.require_positive:
            if close is None or close <= 0:
                raise ValueError(f"EMA close price must be positive, got {close}")
        elif close is None or not math.isfinite(close):
            raise ValueError(f"EMA input must be a finite number, got {close!r}")
        if not self.initialized:
            self._seed.append(close)
            if len(self._seed) < self.period:
                return None
            self.value = sum(self._seed) / self.period
            self.initialized = True
            self._seed = []
            return self.value
        # Explicit ``is not None`` rather than ``or``: a signed series can sit
        # at exactly 0.0, and ``value or close`` would silently feed the new
        # close in place of the real 0.0 — a wrong recursion branch that price
        # series can never reach (their values are always positive).
        prior = self.value if self.value is not None else close
        self.value = self.alpha * close + (1.0 - self.alpha) * prior
        return self.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "require_positive": self.require_positive,
            "value": self.value,
            "initialized": self.initialized,
            "seed": list(self._seed),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> EmaState:
        state = cls(
            period=int(payload["period"]),
            # Absent on checkpoints written before the flag existed: those were
            # all price EMAs, so ``True`` is the faithful default.
            require_positive=bool(payload.get("require_positive", True)),
        )
        state.value = None if payload.get("value") is None else float(payload["value"])
        state.initialized = bool(payload.get("initialized"))
        state._seed = [float(v) for v in payload.get("seed", [])]
        return state
