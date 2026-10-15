"""The level inventory: every price that matters, listed in one place.

The setup engine keeps asking one question — *where is price relative to the
levels that matter?* — and that question must be answered from a single
inventory rather than from whichever engine happens to look. This module owns
exactly two jobs:

* the levels a **candle stream produces by itself**:

  - the **session high / low** — the running max high / min low of today's
    completed candles; they exist from the first candle of the session;
  - the **trailing swing extremes** — the max high / min low over the last
    ``k`` completed candles (``k`` configurable, default
    :data:`DEFAULT_SWING_WINDOW`). An extreme falls out of the window with the
    candle that made it: a swing level describes the recent tape, not the day.

* the merge that folds in the levels **other engines derive** — as *inputs*,
  never re-derived here:

  - the session **VWAP** (:mod:`worker.analytics.indicators`),
  - the **opening range ends** (:class:`worker.analytics.breakouts.BreakoutTracker`),
  - the **prior day's high / low** (the ``candles_1d`` series the nightly sync
    writes).

Why the merge takes the ORB ends as arguments instead of computing them: the
opening range is derived once, in the tracker that already judges bucket closes
with ``bucket_of`` semantics, and a second derivation is how two surfaces start
disagreeing about where the range ended. One derivation, consumed everywhere.

``None`` means "not known" — an input that has not happened yet (no ORB range
before the first bucket closes, no prior day on a cold instrument) is omitted
from the inventory rather than guessed. The inventory is returned sorted by
``(price, kind)`` so downstream scoring sees a stable order; when two kinds
share a price (a swing high that *is* the day high), the higher-scope kind
keeps the level and the redundant one is dropped — the price is one fact.

No checkpoint: like the ATR, the book is a pure fold of today's completed
candles, so recovery replays those candles through :meth:`LevelBook.fold` and
rebuilds exactly what the live tape would have built. A stored copy would just
be a second source of truth.

Phase 2C ships this module as tested foundation; the pipeline gains the first
consumer (retest and anti-chase evidence) in the phases that follow, and the
pipeline-level knob for ``k`` lands with it.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field

#: The trailing window the swing extremes are measured over, in completed
#: one-minute candles. Fifteen minutes is the intraday swing scale: long enough
#: that a single spike does not define a level, short enough that the level is
#: about the move happening now.
DEFAULT_SWING_WINDOW = 15

#: Level kinds. Strings, because they are logged and later stored — the same
#: rule the breakout codes follow.
PRIOR_DAY_HIGH = "prior_day_high"
PRIOR_DAY_LOW = "prior_day_low"
SESSION_HIGH = "session_high"
SESSION_LOW = "session_low"
ORB_HIGH = "orb_high"
ORB_LOW = "orb_low"
VWAP = "vwap"
SWING_HIGH = "swing_high"
SWING_LOW = "swing_low"


def _positive_price(name: str, value: float) -> float:
    """Coerce and validate one bar price; a bad price is a data problem."""
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"level bar {name} must be a number, got {value!r}") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"level bar {name} must be a positive finite number, got {value!r}")
    return number


@dataclass(frozen=True)
class Level:
    """One price with its provenance. ``kind`` disambiguates equal prices."""

    kind: str
    price: float


@dataclass
class LevelBook:
    """Per-instrument level state for one session (fold + merge, contract above)."""

    swing_window: int = DEFAULT_SWING_WINDOW
    session_high: float | None = None
    session_low: float | None = None
    _highs: deque[float] = field(default_factory=deque, repr=False)
    _lows: deque[float] = field(default_factory=deque, repr=False)

    def __post_init__(self) -> None:
        if self.swing_window < 1:
            raise ValueError("swing window must be >= 1")
        self._highs = deque(self._highs, maxlen=self.swing_window)
        self._lows = deque(self._lows, maxlen=self.swing_window)

    # ── the candle-driven half ─────────────────────────────────────────────
    def fold(self, *, high: float, low: float) -> None:
        """Fold one completed candle in: session extremes + the swing window."""
        high = _positive_price("high", high)
        low = _positive_price("low", low)
        if high < low:
            raise ValueError(f"level bar has high {high} below low {low}")
        if self.session_high is None or high > self.session_high:
            self.session_high = high
        if self.session_low is None or low < self.session_low:
            self.session_low = low
        self._highs.append(high)
        self._lows.append(low)

    @property
    def swing_high(self) -> float | None:
        """Max high over the trailing window; ``None`` before the first candle."""
        return max(self._highs) if self._highs else None

    @property
    def swing_low(self) -> float | None:
        return min(self._lows) if self._lows else None

    def reset(self) -> None:
        """Session boundary: today's levels are not tomorrow's."""
        self.session_high = None
        self.session_low = None
        self._highs.clear()
        self._lows.clear()

    # ── the merged inventory ───────────────────────────────────────────────
    def inventory(
        self,
        *,
        vwap: float | None = None,
        orb_high: float | None = None,
        orb_low: float | None = None,
        prior_day_high: float | None = None,
        prior_day_low: float | None = None,
    ) -> list[Level]:
        """Today's levels, sorted by ``(price, kind)``.

        The swing extremes are dropped when they *equal* the session extreme
        they duplicate (the window simply still contains the day's high) — same
        fold, same day, one fact. External inputs are kept as their own entries
        even at equal prices: a session high that equals the prior day's high is
        two facts about two sessions, and a consumer that wants unique prices
        collapses them itself. They are validated only for presence: they come
        from engines that have already validated their own numbers.
        """
        levels: list[Level] = []
        if self.session_high is not None:
            levels.append(Level(SESSION_HIGH, self.session_high))
        if self.session_low is not None:
            levels.append(Level(SESSION_LOW, self.session_low))
        swing_high = self.swing_high
        if swing_high is not None and (
            self.session_high is None or swing_high < self.session_high
        ):
            levels.append(Level(SWING_HIGH, swing_high))
        swing_low = self.swing_low
        if swing_low is not None and (
            self.session_low is None or swing_low > self.session_low
        ):
            levels.append(Level(SWING_LOW, swing_low))
        for kind, price in (
            (VWAP, vwap),
            (ORB_HIGH, orb_high),
            (ORB_LOW, orb_low),
            (PRIOR_DAY_HIGH, prior_day_high),
            (PRIOR_DAY_LOW, prior_day_low),
        ):
            if price is not None:
                levels.append(Level(kind, float(price)))
        levels.sort(key=lambda level: (level.price, level.kind))
        return levels


def next_resistance(levels: Sequence[Level], price: float) -> Level | None:
    """The nearest level **strictly above** ``price`` (ties: kind, alphabetical).

    A level exactly at the price is neither resistance nor support for it: the
    question "what is overhead?" has a different answer at the level than one
    tick above it, and callers that want "at or above" filter the inventory
    themselves.
    """
    candidates = [level for level in levels if level.price > price]
    if not candidates:
        return None
    return min(candidates, key=lambda level: (level.price, level.kind))


def next_support(levels: Sequence[Level], price: float) -> Level | None:
    """The nearest level **strictly below** ``price`` (ties: kind, alphabetical)."""
    candidates = [level for level in levels if level.price < price]
    if not candidates:
        return None
    highest = max(level.price for level in candidates)
    return min(
        (level for level in candidates if level.price == highest),
        key=lambda level: level.kind,
    )
