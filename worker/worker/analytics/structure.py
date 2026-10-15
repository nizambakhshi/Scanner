"""The structural indicator set: SMA, Donchian channel, OBV, ADX-14, EMA stack.

These are the readings the discovery layer and the candidate-feature layer share
but the live scanner never did: the four "optional confirmations" of the brief
(ADX 14, OBV trend, EMA 9/21/50 alignment, a previous-bar Donchian channel),
plus the plain SMA the Golden-Cross preset compares.

Definitions — written down once here so every surface computes the same numbers:

* **SMA(n)** — the mean of the last ``n`` completed closes. The house seed-free
  average: nothing exists until ``n`` closes have been seen.
* **Donchian channel (period n)** — ``(max high, min low)`` over the ``n``
  completed bars **before** the current one. The brief names this the
  ``compare_offset=1`` rule, and the strategy library repeats the reason in a
  comment: comparing a bar's close against a channel that already contains that
  bar is impossible when the bar *is* the new high — the close can never exceed
  a band it helped define. So :meth:`DonchianState.update` returns the channel
  of the now-*prior* window at exactly the call that feeds the new bar, and the
  caller compares its close against it. The first channel exists on the
  ``period + 1``-th completed bar.
* **OBV** — the signed volume ledger: ``OBV_t = OBV_{t−1} + V_t`` when the close
  rose, ``− V_t`` when it fell, unchanged when flat. The first bar has no
  previous close to sign against and contributes nothing (the same "no previous
  close in scope" discipline the ATR's first bar states). The **trend** reading
  is ``OBV_t > OBV_{t−lookback}`` — a comparison, not a slope fit, so one
  session's ledger noise cannot flip it and nothing is smoothed that the brief
  did not ask to smooth.
* **ADX-14 (Wilder)** — the trend-strength reading from +DM/−DM: up-move
  ``H_t − H_{t−1}`` and down-move ``L_{t−1} − L_t``, each kept only when it is
  the larger and positive; TR exactly as :mod:`worker.analytics.atr` defines it
  (first bar ``H − L``). The three series are smoothed with the house
  SMA-seed-then-Wilder-recursion convention (``s = ((n−1)·s + x) / n`` — the
  same smoothing family the RSI and ATR use, so the reading agrees with the
  charting packages the operator compares against). ``+DI = 100·+DM_s / TR_s``,
  ``−DI`` likewise, ``DX = 100·|+DI − −DI| / (+DI + −DI)`` (both DIs zero reads
  as ``DX = 0``, an answer rather than a division error), and the ADX is the
  Wilder average of DX. Availability: ``+DI``/``−DI`` on the ``period``-th bar,
  the ADX on the ``2·period − 1``-th — with 14, bars 14 and 27.
* **EMA stack (9/21/50)** — three :class:`worker.analytics.ema.EmaState`
  instances, aligned when ``e9 > e21 > e50``. ``None`` until every line is warm:
  an alignment that cannot be judged reads as absent, never as ``False``.

Everything here is streaming, per series, and holds no clock and no session
boundary: daily replay and intraday processing drive the same objects, and a
missing bar is simply absent — the next real bar continues the recursion, the
same rule every other indicator in this package follows. Checkpoints exist for
the states the pipeline persists; the daily fold in :mod:`worker.analytics.daily`
rebuilds by replay instead and stores none.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .ema import EmaState

#: The brief's channel: twenty bars, previous-window comparison.
DEFAULT_DONCHIAN_PERIOD = 20

#: Wilder's own period for the trend-strength reading.
DEFAULT_ADX_PERIOD = 14

#: OBV "rising" compares the ledger against its value this many bars back. Half
#: a month of sessions on daily bars: long enough that one print does not flip
#: the trend, short enough that the trend is about participation *now*.
DEFAULT_OBV_LOOKBACK = 5

#: The confirmations preset's stack, quoted from the uploaded strategy library
#: (``LIB-TRIPLE-EMA``): EMA 9 above EMA 21 above EMA 50.
DEFAULT_STACK_PERIODS = (9, 21, 50)


def _positive_price(name: str, value: float) -> float:
    """Coerce and validate one bar price; a bad price is a data problem."""
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"structure {name} must be a number, got {value!r}") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"structure {name} must be a positive finite number, got {value!r}")
    return number


def _finite_volume(value: float) -> float:
    """Volume may be zero (a halted name) but never negative or NaN."""
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"structure volume must be a number, got {value!r}") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"structure volume must be a finite non-negative number, got {value!r}")
    return number


@dataclass
class SmaState:
    """Streaming SMA over completed closes (definition above). ``None`` until warm."""

    period: int
    _window: deque[float] = field(default_factory=deque, repr=False)

    def __post_init__(self) -> None:
        if self.period < 1:
            raise ValueError("SMA period must be >= 1")
        self._window = deque(self._window, maxlen=self.period)

    @property
    def warm(self) -> bool:
        return len(self._window) == self.period

    @property
    def value(self) -> float | None:
        return sum(self._window) / self.period if self.warm else None

    def update(self, close: float) -> float | None:
        close = _positive_price("close", close)
        self._window.append(close)
        return self.value

    def to_dict(self) -> dict[str, Any]:
        return {"period": self.period, "window": list(self._window)}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SmaState:
        state = cls(period=int(payload["period"]))
        state._window = deque(
            (float(v) for v in payload.get("window", [])), maxlen=state.period
        )
        return state


@dataclass(frozen=True)
class DonchianChannel:
    """The prior-window band a new bar's close is compared against."""

    upper: float
    lower: float


@dataclass
class DonchianState:
    """Previous-``period`` Donchian channel (``compare_offset=1``, definition above).

    :meth:`update` returns the channel of the bars **before** the one being fed —
    the caller judges the fed bar's close against that band. The returned
    ``None`` before ``period + 1`` bars have been seen is the honest answer: the
    band does not exist yet, and a breakout cannot be claimed against a window
    that was still filling.
    """

    period: int = DEFAULT_DONCHIAN_PERIOD
    _highs: deque[float] = field(default_factory=deque, repr=False)
    _lows: deque[float] = field(default_factory=deque, repr=False)
    current: DonchianChannel | None = None

    def __post_init__(self) -> None:
        if self.period < 2:
            raise ValueError("Donchian period must be >= 2")
        self._highs = deque(self._highs, maxlen=self.period)
        self._lows = deque(self._lows, maxlen=self.period)

    @property
    def warm(self) -> bool:
        return self.current is not None

    def update(self, *, high: float, low: float) -> DonchianChannel | None:
        """Feed one completed bar; returns the **prior** window's channel."""
        high = _positive_price("high", high)
        low = _positive_price("low", low)
        if high < low:
            raise ValueError(f"structure bar has high {high} below low {low}")
        if len(self._highs) == self.period:
            self.current = DonchianChannel(upper=max(self._highs), lower=min(self._lows))
        self._highs.append(high)
        self._lows.append(low)
        return self.current

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "highs": list(self._highs),
            "lows": list(self._lows),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> DonchianState:
        state = cls(period=int(payload["period"]))
        state._highs = deque((float(v) for v in payload.get("highs", [])), maxlen=state.period)
        state._lows = deque((float(v) for v in payload.get("lows", [])), maxlen=state.period)
        if len(state._highs) == state.period:
            state.current = DonchianChannel(
                upper=max(state._highs), lower=min(state._lows)
            )
        return state


@dataclass
class ObvState:
    """Streaming OBV ledger with a comparison-based trend reading (definition above)."""

    lookback: int = DEFAULT_OBV_LOOKBACK
    obv: float = 0.0
    observations: int = 0
    _prev_close: float | None = field(default=None, repr=False)
    _history: deque[float] = field(default_factory=deque, repr=False)

    def __post_init__(self) -> None:
        if self.lookback < 1:
            raise ValueError("OBV lookback must be >= 1")
        self._history = deque(self._history, maxlen=self.lookback + 1)

    @property
    def rising(self) -> bool | None:
        """OBV above its value ``lookback`` bars back; ``None`` until measurable."""
        if len(self._history) < self.lookback + 1:
            return None
        return self._history[-1] > self._history[0]

    def update(self, *, close: float, volume: float) -> float:
        close = _positive_price("close", close)
        volume = _finite_volume(volume)
        if self._prev_close is not None:
            if close > self._prev_close:
                self.obv += volume
            elif close < self._prev_close:
                self.obv -= volume
            # A flat close moves the ledger by nothing — the third case is
            # real and stays explicit rather than folded into either sign.
        self._prev_close = close
        self.observations += 1
        self._history.append(self.obv)
        return self.obv

    def to_dict(self) -> dict[str, Any]:
        return {
            "lookback": self.lookback,
            "obv": self.obv,
            "observations": self.observations,
            "prev_close": self._prev_close,
            "history": list(self._history),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ObvState:
        state = cls(lookback=int(payload["lookback"]))
        state.obv = float(payload.get("obv", 0.0))
        state.observations = int(payload.get("observations", 0))
        state._prev_close = (
            None if payload.get("prev_close") is None else float(payload["prev_close"])
        )
        state._history = deque(
            (float(v) for v in payload.get("history", [])), maxlen=state.lookback + 1
        )
        return state


@dataclass(frozen=True)
class AdxValues:
    """One bar's directional read; each field ``None`` until its stage is warm."""

    adx: float | None
    plus_di: float | None
    minus_di: float | None

    @property
    def ready(self) -> bool:
        return self.adx is not None


@dataclass
class AdxState:
    """Streaming Wilder ADX with the house smoothing convention (definition above)."""

    period: int = DEFAULT_ADX_PERIOD
    observations: int = 0
    current: AdxValues = field(
        default_factory=lambda: AdxValues(None, None, None)
    )
    _prev_high: float | None = field(default=None, repr=False)
    _prev_low: float | None = field(default=None, repr=False)
    _prev_close: float | None = field(default=None, repr=False)
    _tr_seed: list[float] = field(default_factory=list, repr=False)
    _pdm_seed: list[float] = field(default_factory=list, repr=False)
    _mdm_seed: list[float] = field(default_factory=list, repr=False)
    _tr_smooth: float | None = field(default=None, repr=False)
    _pdm_smooth: float | None = field(default=None, repr=False)
    _mdm_smooth: float | None = field(default=None, repr=False)
    _dx_seed: list[float] = field(default_factory=list, repr=False)
    _adx: float | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.period < 2:
            raise ValueError("ADX period must be >= 2")

    @property
    def min_observations_for_adx(self) -> int:
        """Bars needed before the ADX exists: one seed pass, then one DX pass."""
        return 2 * self.period - 1

    def update(self, *, high: float, low: float, close: float) -> AdxValues:
        """Feed one completed bar; the next bar's recursion starts from it."""
        high = _positive_price("high", high)
        low = _positive_price("low", low)
        close = _positive_price("close", close)
        if high < low:
            raise ValueError(f"structure bar has high {high} below low {low}")

        if self._prev_high is None:
            # First bar: no previous high/low/close to sign against — the same
            # "nothing in scope yet" answer the ATR's first bar gives.
            true_range = high - low
            plus_dm = 0.0
            minus_dm = 0.0
        else:
            up_move = high - self._prev_high
            down_move = self._prev_low - low
            plus_dm = up_move if up_move > down_move and up_move > 0 else 0.0
            minus_dm = down_move if down_move > up_move and down_move > 0 else 0.0
            true_range = max(
                high - low,
                abs(high - self._prev_close),
                abs(low - self._prev_close),
            )
        self._prev_high = high
        self._prev_low = low
        self._prev_close = close
        self.observations += 1

        if self._tr_smooth is None:
            self._tr_seed.append(true_range)
            self._pdm_seed.append(plus_dm)
            self._mdm_seed.append(minus_dm)
            if len(self._tr_seed) < self.period:
                self.current = AdxValues(None, None, None)
                return self.current
            self._tr_smooth = sum(self._tr_seed) / self.period
            self._pdm_smooth = sum(self._pdm_seed) / self.period
            self._mdm_smooth = sum(self._mdm_seed) / self.period
            self._tr_seed = []
            self._pdm_seed = []
            self._mdm_seed = []
        else:
            p = float(self.period)
            self._tr_smooth = ((p - 1.0) * self._tr_smooth + true_range) / p
            self._pdm_smooth = ((p - 1.0) * self._pdm_smooth + plus_dm) / p
            self._mdm_smooth = ((p - 1.0) * self._mdm_smooth + minus_dm) / p

        plus_di, minus_di, dx = self._directional()
        if self._adx is None:
            self._dx_seed.append(dx)
            if len(self._dx_seed) == self.period:
                self._adx = sum(self._dx_seed) / self.period
                self._dx_seed = []
        else:
            p = float(self.period)
            self._adx = ((p - 1.0) * self._adx + dx) / p
        self.current = AdxValues(self._adx, plus_di, minus_di)
        return self.current

    def _directional(self) -> tuple[float, float, float]:
        """( +DI, −DI, DX ) from the smoothed series; both-zero reads as DX 0."""
        tr = self._tr_smooth if self._tr_smooth is not None else 0.0
        pdm = self._pdm_smooth if self._pdm_smooth is not None else 0.0
        mdm = self._mdm_smooth if self._mdm_smooth is not None else 0.0
        if tr <= 0:
            return 0.0, 0.0, 0.0
        plus_di = 100.0 * pdm / tr
        minus_di = 100.0 * mdm / tr
        total = plus_di + minus_di
        dx = 100.0 * abs(plus_di - minus_di) / total if total > 0 else 0.0
        return plus_di, minus_di, dx

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "observations": self.observations,
            "prev_high": self._prev_high,
            "prev_low": self._prev_low,
            "prev_close": self._prev_close,
            "tr_seed": list(self._tr_seed),
            "pdm_seed": list(self._pdm_seed),
            "mdm_seed": list(self._mdm_seed),
            "tr_smooth": self._tr_smooth,
            "pdm_smooth": self._pdm_smooth,
            "mdm_smooth": self._mdm_smooth,
            "dx_seed": list(self._dx_seed),
            "adx": self._adx,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> AdxState:
        state = cls(period=int(payload["period"]))
        state.observations = int(payload.get("observations", 0))
        state._prev_high = _opt_float(payload.get("prev_high"))
        state._prev_low = _opt_float(payload.get("prev_low"))
        state._prev_close = _opt_float(payload.get("prev_close"))
        state._tr_seed = [float(v) for v in payload.get("tr_seed", [])]
        state._pdm_seed = [float(v) for v in payload.get("pdm_seed", [])]
        state._mdm_seed = [float(v) for v in payload.get("mdm_seed", [])]
        state._tr_smooth = _opt_float(payload.get("tr_smooth"))
        state._pdm_smooth = _opt_float(payload.get("pdm_smooth"))
        state._mdm_smooth = _opt_float(payload.get("mdm_smooth"))
        state._dx_seed = [float(v) for v in payload.get("dx_seed", [])]
        state._adx = _opt_float(payload.get("adx"))
        if state._tr_smooth is not None:
            plus_di, minus_di, _ = state._directional()
            state.current = AdxValues(state._adx, plus_di, minus_di)
        return state


def _opt_float(value: Any) -> float | None:
    return None if value is None else float(value)


@dataclass
class EmaStack:
    """The 9/21/50 alignment preset as three house EMAs (definition above)."""

    periods: tuple[int, ...] = DEFAULT_STACK_PERIODS
    lines: tuple[EmaState, ...] = field(init=False)

    def __post_init__(self) -> None:
        if len(self.periods) < 3:
            raise ValueError("EMA stack needs at least three lines")
        if any(p < 2 for p in self.periods):
            raise ValueError("EMA stack periods must be >= 2")
        self.lines = tuple(EmaState(period=p) for p in self.periods)

    @property
    def values(self) -> tuple[float | None, ...]:
        return tuple(line.value for line in self.lines)

    @property
    def aligned(self) -> bool | None:
        """``e9 > e21 > e50``; ``None`` until every line is warm."""
        values = self.values
        if any(v is None for v in values):
            return None
        ordered = list(values)
        return all(a > b for a, b in zip(ordered, ordered[1:], strict=False))

    def update(self, close: float) -> tuple[float | None, ...]:
        for line in self.lines:
            line.update(close)
        return self.values

    def to_dict(self) -> dict[str, Any]:
        return {"periods": list(self.periods), "lines": [line.to_dict() for line in self.lines]}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> EmaStack:
        state = cls(periods=tuple(int(p) for p in payload["periods"]))
        state.lines = tuple(EmaState.from_dict(line) for line in payload["lines"])
        return state
