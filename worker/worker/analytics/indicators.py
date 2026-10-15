"""Indicator engine: session VWAP, EMA(fast/slow), relative volume, 5-minute RSI (spec §9).

Every calculation here is deterministic and pure: the same candle sequence
produces the same numbers regardless of process restarts, worker count, or
wall-clock time. Each definition matches the formula written in the master spec,
and the unit tests in ``tests/unit/test_indicators.py`` assert against
independently hand-computed values rather than against this code's own output.

EMA initialization convention (used identically by warm-up, live processing,
restart recovery and the tests): the first ``n`` valid closes form a simple
moving average, that SMA becomes ``EMA_n``, and every later close uses the
recursive form ``EMA_t = α·C_t + (1−α)·EMA_{t−1}`` with ``α = 2/(n+1)``.

The RSI-14 is measured on **today's five-minute closes only** — the last 1-minute
close of each bucket, the same partition the /strategy board builds in SQL — and
is deliberately not seeded from yesterday's tape. An intraday entry rule has to
describe the move that is happening now.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .rsi import RSI_PERIOD, WilderRsi, closes_bucket


@dataclass
class EmaState:
    """Streaming EMA with SMA seed (documented convention above)."""

    period: int
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
        if close is None or close <= 0:
            raise ValueError(f"EMA close price must be positive, got {close}")
        if not self.initialized:
            self._seed.append(close)
            if len(self._seed) < self.period:
                return None
            self.value = sum(self._seed) / self.period
            self.initialized = True
            self._seed = []
            return self.value
        self.value = self.alpha * close + (1.0 - self.alpha) * (self.value or close)
        return self.value

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "value": self.value,
            "initialized": self.initialized,
            "seed": list(self._seed),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> EmaState:
        state = cls(period=int(payload["period"]))
        state.value = None if payload.get("value") is None else float(payload["value"])
        state.initialized = bool(payload.get("initialized"))
        state._seed = [float(v) for v in payload.get("seed", [])]
        return state


@dataclass
class VwapState:
    """Session VWAP from completed 1-minute candles (candle-based approximation).

    ``TP = (H + L + C) / 3``; ``VWAP = Σ(TP·V) / Σ(V)``. Cumulative sums are
    reset at every trading-session boundary so yesterday's VWAP can never bleed
    into today's (spec §9.1).
    """

    price_volume_sum: float = 0.0
    volume_sum: int = 0

    @staticmethod
    def typical_price(high: float, low: float, close: float) -> float:
        return (high + low + close) / 3.0

    def update(self, high: float, low: float, close: float, volume: int) -> None:
        if volume < 0:
            raise ValueError("VWAP volume must be nonnegative")
        if volume == 0:
            return  # a zero-volume candle contributes nothing
        tp = self.typical_price(high, low, close)
        self.price_volume_sum += tp * volume
        self.volume_sum += int(volume)

    @property
    def value(self) -> float | None:
        if self.volume_sum <= 0:
            return None  # safe: no division by zero, and never a fake 0.0 VWAP
        return self.price_volume_sum / self.volume_sum

    def reset(self) -> None:
        self.price_volume_sum = 0.0
        self.volume_sum = 0

    def to_dict(self) -> dict[str, Any]:
        return {"price_volume_sum": self.price_volume_sum, "volume_sum": self.volume_sum}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> VwapState:
        return cls(
            price_volume_sum=float(payload.get("price_volume_sum", 0.0)),
            volume_sum=int(payload.get("volume_sum", 0)),
        )


@dataclass
class RelativeVolumeState:
    """``RV = V_latest / mean(V[t-lookback .. t-1])`` — current candle excluded."""

    lookback: int
    history: deque[int] = field(init=False)

    def __post_init__(self) -> None:
        if self.lookback < 2:
            raise ValueError("volume lookback must be >= 2")
        self.history = deque(maxlen=self.lookback + 1)

    def push(self, volume: int) -> None:
        if volume < 0:
            raise ValueError("candle volume must be nonnegative")
        self.history.append(int(volume))

    @property
    def ready(self) -> bool:
        return len(self.history) >= self.lookback + 1

    @property
    def baseline_average(self) -> float | None:
        """Mean of the previous ``lookback`` completed candles (no current)."""
        if not self.ready:
            return None
        baseline = list(self.history)[:-1]
        return sum(baseline) / len(baseline)

    @property
    def current_volume(self) -> int | None:
        if not self.history:
            return None
        return self.history[-1]

    def value(self) -> float | None:
        """Relative volume, or ``None`` when undefined.

        A zero/invalid baseline returns ``None`` — the caller treats that as a
        *failed* volume condition instead of inventing an infinite RV (spec §9.4).
        """
        average = self.baseline_average
        if average is None or average <= 0:
            return None
        return self.current_volume / average

    def to_dict(self) -> dict[str, Any]:
        return {"lookback": self.lookback, "history": list(self.history)}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> RelativeVolumeState:
        state = cls(lookback=int(payload["lookback"]))
        for volume in payload.get("history", []):
            state.push(int(volume))
        return state


@dataclass(frozen=True)
class IndicatorValues:
    """The indicator block the scanner evaluates for one completed candle."""

    close: float
    vwap: float | None
    ema_fast: float | None
    ema_slow: float | None
    average_volume: float | None
    relative_volume: float | None
    candles_seen: int
    warm: bool
    #: Wilder RSI-14 on today's five-minute closes. ``None`` until the fifteenth
    #: bucket exists (~10:30 IST), and it holds its value through the four minutes
    #: that do not close a bucket — the reading is a bucket measurement, not a
    #: per-minute one.
    rsi_5m: float | None = None


class InstrumentIndicators:
    """Per-instrument indicator state, fed one completed candle at a time."""

    def __init__(
        self,
        *,
        fast_period: int,
        slow_period: int,
        volume_lookback: int,
        rsi_period: int = RSI_PERIOD,
    ) -> None:
        if slow_period <= fast_period:
            raise ValueError("slow EMA period must exceed fast EMA period")
        self.fast = EmaState(period=fast_period)
        self.slow = EmaState(period=slow_period)
        self.vwap = VwapState()
        self.volume = RelativeVolumeState(lookback=volume_lookback)
        self.rsi = WilderRsi(period=rsi_period)
        self.candles_seen = 0
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.volume_lookback = volume_lookback

    # ── live path ───────────────────────────────────────────────────────────
    def on_candle(
        self,
        *,
        high: float,
        low: float,
        close: float,
        volume: int,
        in_current_session: bool = True,
        candle_start: datetime | None = None,
    ) -> IndicatorValues:
        """Update state with one **completed** candle and return the values.

        ``in_current_session=False`` is used for historical warm-up candles:
        they seed EMA and volume history but must not touch session VWAP, and
        they must not enter today's RSI series either — the entry rule is about
        the move happening now.

        ``candle_start`` is what tells a five-minute bucket close from the four
        minutes inside it. Omit it and the RSI simply stops advancing (EMA, VWAP
        and volume still update), which keeps an older caller honest rather than
        silently bucketing on the wrong minute.
        """
        if close <= 0:
            raise ValueError(f"candle close must be positive, got {close}")
        if volume < 0:
            raise ValueError("candle volume must be nonnegative")
        if in_current_session:
            self.vwap.update(high, low, close, volume)
            if candle_start is not None and closes_bucket(candle_start):
                self.rsi.update(close)
        self.fast.update(close)
        self.slow.update(close)
        self.volume.push(volume)
        self.candles_seen += 1
        return self.values(close=close)

    def values(self, *, close: float) -> IndicatorValues:
        warm = (
            self.fast.initialized
            and self.slow.initialized
            and self.volume.ready
            and self.vwap.volume_sum > 0
        )
        return IndicatorValues(
            close=close,
            vwap=self.vwap.value,
            ema_fast=self.fast.value,
            ema_slow=self.slow.value,
            average_volume=self.volume.baseline_average,
            relative_volume=self.volume.value(),
            candles_seen=self.candles_seen,
            warm=warm,
            rsi_5m=self.rsi.value,
        )

    def reset_session_vwap(self) -> None:
        """Called at each session boundary; EMA/volume history is preserved.

        The five-minute RSI resets with the VWAP rather than with the EMA: both
        measure *this* session, and yesterday's tape is not today's dip.
        """
        self.vwap.reset()
        self.rsi = WilderRsi(period=self.rsi.period)

    # ── warm-up + recovery ──────────────────────────────────────────────────
    def seed(self, candles: Iterable[Any], *, session_candles: bool = False) -> int:
        """Feed validated prior candles to warm up EMA/volume.

        ``session_candles=True`` only when the candles belong to the session
        currently being evaluated (restart recovery); historical days seed EMA
        and the volume baseline but never today's VWAP or today's RSI.
        """
        count = 0
        for candle in candles:
            self.on_candle(
                high=float(candle.high),
                low=float(candle.low),
                close=float(candle.close),
                volume=int(candle.volume),
                in_current_session=session_candles,
                candle_start=getattr(candle, "candle_start", None),
            )
            count += 1
        return count

    def to_dict(self) -> dict[str, Any]:
        return {
            "fast_period": self.fast_period,
            "slow_period": self.slow_period,
            "volume_lookback": self.volume_lookback,
            "fast": self.fast.to_dict(),
            "slow": self.slow.to_dict(),
            "vwap": self.vwap.to_dict(),
            "volume": self.volume.to_dict(),
            "rsi": self.rsi.to_dict(),
            "candles_seen": self.candles_seen,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> InstrumentIndicators:
        engine = cls(
            fast_period=int(payload["fast_period"]),
            slow_period=int(payload["slow_period"]),
            volume_lookback=int(payload["volume_lookback"]),
        )
        engine.fast = EmaState.from_dict(payload["fast"])
        engine.slow = EmaState.from_dict(payload["slow"])
        engine.vwap = VwapState.from_dict(payload["vwap"])
        if payload.get("rsi"):
            engine.rsi = WilderRsi.from_dict(payload["rsi"])
        for volume in payload["volume"].get("history", []):
            engine.volume.push(int(volume))
        engine.candles_seen = int(payload.get("candles_seen", 0))
        return engine


class IndicatorRegistry:
    """Owns one :class:`InstrumentIndicators` per instrument."""

    def __init__(self, *, fast_period: int, slow_period: int, volume_lookback: int) -> None:
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.volume_lookback = volume_lookback
        self._engines: dict[int, InstrumentIndicators] = {}

    def get(self, instrument_id: int) -> InstrumentIndicators:
        engine = self._engines.get(instrument_id)
        if engine is None:
            engine = InstrumentIndicators(
                fast_period=self.fast_period,
                slow_period=self.slow_period,
                volume_lookback=self.volume_lookback,
            )
            self._engines[instrument_id] = engine
        return engine

    def remove(self, instrument_id: int) -> None:
        self._engines.pop(instrument_id, None)

    @property
    def instrument_ids(self) -> set[int]:
        return set(self._engines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fast_period": self.fast_period,
            "slow_period": self.slow_period,
            "volume_lookback": self.volume_lookback,
            "instruments": {str(key): value.to_dict() for key, value in self._engines.items()},
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> IndicatorRegistry:
        registry = cls(
            fast_period=int(payload["fast_period"]),
            slow_period=int(payload["slow_period"]),
            volume_lookback=int(payload["volume_lookback"]),
        )
        for key, value in payload.get("instruments", {}).items():
            registry._engines[int(key)] = InstrumentIndicators.from_dict(value)
        return registry
