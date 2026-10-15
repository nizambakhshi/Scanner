"""Breakout / breakdown detectors: day break, opening range, VWAP reclaim-loss, RSI entry.

Operator features (2026-10-14, and the fourth rule on 2026-10-15): strategies the
bullish scanner did not have. The bullish scanner answers "is this stock setting
up?"; these answer "did this stock just *do* something?", evaluated on every
completed 1-minute candle of the whole configured universe — not only the
watchlist, because a day high on a stock that never qualified is still a day high.

The four definitions, stated so this file and the dashboard can be compared:

* ``session_break`` — a **close** above the session high recorded *before* this
  candle (or below the session low), by more than ``threshold_pct``. Breaking the
  running high with the high itself is not a signal — every new high does that —
  so the rule is about where the minute *ended*.
* ``orb`` — the opening range is the first ``orb_minutes`` one-minute bars
  (``max(high)``, ``min(low)``), and a break is a 5-minute **bucket close** beyond
  that range. This is the same definition the /strategy board computes in SQL
  (``ORB_STRATEGY_SQL`` in ``app/lib/source-postgres.ts``): the bucket index is
  ``floor((candle_start - session_open) / 300)``, a bucket's close is the last
  1-minute close inside it, and the volume ratio is that bucket's volume over the
  mean volume of **every earlier bucket including the range candle**. A worker
  that disagreed with the board would be a bug the operator can see, so the board
  is the specification here.
* ``vwap`` — a reclaim (close above session VWAP) or a loss (close below it), with
  ``threshold_pct`` acting as a hysteresis band so a tape that hugs the VWAP
  cannot flapping-alert, and the first determination of the day treated as the
  baseline rather than as a cross — the same rollout rule the two-timeframe RSI
  alerts use.

* ``rsi`` — the operator's entry rule, restated 2026-10-15 as "RSI below 40 is
  green, an entry; above 60 is red", and *at the time of* a trend. Wilder RSI-14 on
  the session's 5-minute closes, judged against the trend the scanner already
  measures:

  ``ENTRY`` (green)      RSI below ``oversold`` while the trend is **up**.
  ``OVERBOUGHT`` (red)   RSI above ``overbought`` while the trend is **down**.
  ``NEUTRAL``            the band, or an extreme the trend does not confirm.

  An oversold print inside a downtrend is a falling tape, not a dip to buy, and the
  same print with the EMAs crossed down pages all day — which is why the trend is
  part of the rule and not a footnote. The state is judged on **every** bucket close
  whether or not this strategy is armed: it is a measurement recorded on the candle
  and shown live on the board, and the ``enabled`` switch governs the *alert*, the
  same way disarming ORB never deletes the opening range.

  ``confirm_candles`` here counts **buckets**, not minutes: one 5-minute print below
  40 is a spike, two in a row is a reading. ``min_volume_ratio`` defaults to 0
  (gate off) because the whole point of the rule is the quiet pullback, and a
  volume surge at the bottom is news about the crowd, not about the level.

Two dedupe mechanisms, deliberately different:

* **Per level, in the idempotency key** (:attr:`BreakoutEvent.episode_id`): a
  breakout is identified by the level it broke, so reprocessing the same candle
  after a restart produces the same key and the database's UNIQUE constraint makes
  the second attempt a no-op. The OR level is fixed for the day, so an ORB break
  alerts once per direction per session by construction — which is exactly the
  board's "first breakout" column.
* **Per leg, by re-arming** (``session_break``): after a day-high break the
  strategy disarms and will not alert again until a close falls back below the
  level it broke, at which point it re-arms against the *new* session high.
  Without that, a stock that grinds higher all day pages once per new high.

Volume evidence is a gate, not a report: ``min_volume_ratio`` of 0 turns it off,
and a missing ratio *fails* it rather than passing a signal with no evidence
(spec §9.4 — an undefined relative volume is a failure, never infinity).

Everything here is pure: no database, no wall clock, no network. The tracker is
fed completed candles in time order and returns events;
:class:`~worker.pipeline.ScannerPipeline` owns the wiring and
:class:`~worker.alerts.AlertEngine` owns delivery.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol

from ..timeutils import SessionWindow, to_ist
from .rsi import ENTRY, NEUTRAL, OVERBOUGHT, classify_intraday, closes_bucket

#: The strategy keys, in display order. Also the JSON keys of
#: ``ops_settings['alert_strategies']`` and the names the dashboard renders.
STRATEGIES = ("session_break", "orb", "vwap", "rsi")

STRATEGY_LABELS = {
    "session_break": "Session high / low break",
    "orb": "Opening range break",
    "vwap": "VWAP reclaim / loss",
    "rsi": "RSI entry / overbought",
}

UP = "UP"
DOWN = "DOWN"

#: Five minutes: the opening range the /strategy board uses, and the one an
#: operator can read off any chart without being told what it is.
DEFAULT_ORB_MINUTES = 5

#: The day break needs a day to break. The first five minutes are price discovery
#: — the open print, the first auction, and a "high" made of two candles is not a
#: level worth a phone call. Same wait as the scanner's own initial scan
#: (``INITIAL_SCAN_CANDLES``), for the same reason.
DAY_BREAK_MIN_CANDLES = 5


class Bar(Protocol):
    """Anything shaped like a completed one-minute candle."""

    instrument_id: int
    trading_date: date
    candle_start: datetime
    high: float
    low: float
    close: float
    volume: int


# ── configuration ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StrategyParams:
    """One strategy's tunables: the stored shape and the edited shape.

    ``oversold``/``overbought`` belong to ``rsi`` — the two RSI levels the operator
    named (40 and 60). They are carried on the shared shape rather than a subclass
    so one document, one parser and one editor page cover every strategy; a
    strategy that ignores them still stores and returns them at the defaults.
    """

    enabled: bool = True
    threshold_pct: float = 0.10
    confirm_candles: int = 1
    min_volume_ratio: float = 1.0
    cooldown_minutes: int = 15
    telegram: bool = True
    oversold: float = 40.0
    overbought: float = 60.0

    def problems(self, name: str) -> list[str]:
        found: list[str] = []
        if not 0.0 <= self.threshold_pct <= 5.0:
            found.append(f"{name}: threshold_pct must be between 0 and 5")
        if not 1 <= self.confirm_candles <= 5:
            found.append(f"{name}: confirm_candles must be between 1 and 5")
        if not 0.0 <= self.min_volume_ratio <= 10.0:
            found.append(f"{name}: min_volume_ratio must be between 0 and 10")
        if not 0 <= self.cooldown_minutes <= 240:
            found.append(f"{name}: cooldown_minutes must be between 0 and 240")
        if not 0.0 < self.oversold < self.overbought < 100.0:
            found.append(
                f"{name}: oversold/overbought must be 0 < oversold < overbought < 100"
            )
        return found

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "threshold_pct": self.threshold_pct,
            "confirm_candles": self.confirm_candles,
            "min_volume_ratio": self.min_volume_ratio,
            "cooldown_minutes": self.cooldown_minutes,
            "telegram": self.telegram,
            "oversold": self.oversold,
            "overbought": self.overbought,
        }


#: Defaults reproduce the /strategy board for ORB (a pure bucket-close cross, one
#: bucket, with the board's volume evidence kept as a gate) and are conservative
#: for the two strategies whose level moves every minute. ORB's cooldown is 0
#: because its level-keyed idempotency already allows exactly one alert per
#: direction per session — a window on top of that would only hide a row.
DEFAULTS: dict[str, StrategyParams] = {
    "session_break": StrategyParams(
        enabled=True,
        threshold_pct=0.10,
        confirm_candles=1,
        min_volume_ratio=1.0,
        cooldown_minutes=15,
        telegram=True,
    ),
    "orb": StrategyParams(
        enabled=True,
        threshold_pct=0.0,
        confirm_candles=1,
        min_volume_ratio=1.5,
        cooldown_minutes=0,
        telegram=True,
    ),
    "vwap": StrategyParams(
        enabled=True,
        threshold_pct=0.10,
        confirm_candles=2,
        min_volume_ratio=1.0,
        cooldown_minutes=10,
        telegram=True,
    ),
    # The operator's own two numbers, and the only reason this strategy exists.
    # Two buckets (10 minutes) rather than one so a single printed low cannot page;
    # 30 minutes because an RSI that hugs 40 in a slow drift is one piece of news,
    # not six; no volume gate, for the reason stated in the module docstring.
    "rsi": StrategyParams(
        enabled=True,
        threshold_pct=0.0,
        confirm_candles=2,
        min_volume_ratio=0.0,
        cooldown_minutes=30,
        telegram=True,
        oversold=40.0,
        overbought=60.0,
    ),
}


@dataclass(frozen=True)
class StrategiesConfig:
    """The parsed, validated document: one :class:`StrategyParams` per strategy."""

    session_break: StrategyParams = field(default_factory=lambda: DEFAULTS["session_break"])
    orb: StrategyParams = field(default_factory=lambda: DEFAULTS["orb"])
    vwap: StrategyParams = field(default_factory=lambda: DEFAULTS["vwap"])
    rsi: StrategyParams = field(default_factory=lambda: DEFAULTS["rsi"])

    def get(self, strategy: str) -> StrategyParams:
        if strategy not in STRATEGIES:
            raise KeyError(f"unknown strategy {strategy!r}")
        return getattr(self, strategy)

    @property
    def any_enabled(self) -> bool:
        return any(self.get(key).enabled for key in STRATEGIES)

    def enabled_for(self, strategy: str) -> bool:
        return self.get(strategy).enabled

    def to_dict(self) -> dict[str, Any]:
        """The stored shape — also what the dashboard reads back."""
        return {"version": 1, "strategies": {key: self.get(key).to_dict() for key in STRATEGIES}}

    def summary(self) -> dict[str, Any]:
        """Log-safe view: what is armed, with no credentials anywhere near it."""
        return {key: self.get(key).to_dict() for key in STRATEGIES}


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"expected a boolean, got {value!r}")


def _as_number(value: Any, default: float) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"expected a number, got {value!r}") from exc


def _as_int(value: Any, default: int) -> int:
    if value is None or value == "":
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"expected an integer, got {value!r}") from exc


def parse_strategies(payload: Any) -> StrategiesConfig:
    """Build a :class:`StrategiesConfig` from the stored JSON document.

    Accepts ``{"strategies": {...}}``, a bare ``{strategy: {...}}`` map, or
    nothing at all (``None`` / ``{}`` → the defaults). A *malformed* value raises
    :class:`ValueError` naming the strategy and the field: a config the operator
    can read on the dashboard but the worker silently ignores is worse than one
    that refuses to boot, because the page would claim a strategy is armed while
    the detector never ran.
    """
    if payload is None:
        return StrategiesConfig()
    if not isinstance(payload, Mapping):
        raise ValueError("alert_strategies must be a JSON object")
    body: Mapping[str, Any] = payload
    inner = payload.get("strategies")
    if isinstance(inner, Mapping):
        body = inner
    values: dict[str, StrategyParams] = {}
    problems: list[str] = []
    for key in STRATEGIES:
        defaults = DEFAULTS[key]
        raw = body.get(key)
        if raw is None:
            values[key] = defaults
            continue
        if not isinstance(raw, Mapping):
            problems.append(f"{key}: expected an object")
            continue
        try:
            params = StrategyParams(
                enabled=_as_bool(raw.get("enabled"), defaults.enabled),
                threshold_pct=_as_number(raw.get("threshold_pct"), defaults.threshold_pct),
                confirm_candles=_as_int(raw.get("confirm_candles"), defaults.confirm_candles),
                min_volume_ratio=_as_number(
                    raw.get("min_volume_ratio"), defaults.min_volume_ratio
                ),
                cooldown_minutes=_as_int(raw.get("cooldown_minutes"), defaults.cooldown_minutes),
                telegram=_as_bool(raw.get("telegram"), defaults.telegram),
                oversold=_as_number(raw.get("oversold"), defaults.oversold),
                overbought=_as_number(raw.get("overbought"), defaults.overbought),
            )
        except ValueError as exc:
            problems.append(f"{key}: {exc}")
            continue
        problems.extend(params.problems(key))
        values[key] = params
    unknown = sorted(str(key) for key in body if str(key) not in STRATEGIES)
    if unknown:
        # Not fatal: a newer dashboard can offer a strategy this worker build
        # does not know, and refusing to boot would take the tape down. Warn-only,
        # because the alternative is a page switch that silently does nothing —
        # the caller logs these.
        problems.append("ignored unknown strategies: " + ", ".join(unknown))
    fatal = [problem for problem in problems if not problem.startswith("ignored ")]
    if fatal:
        raise ValueError("; ".join(fatal))
    return StrategiesConfig(**values)


def unknown_strategies_in(payload: Any) -> list[str]:
    """Strategy names present in a document but unknown to this build.

    Split out of :func:`parse_strategies` so a caller can log the warning without
    the parse having to raise for it.
    """
    if not isinstance(payload, Mapping):
        return []
    body = payload.get("strategies") if isinstance(payload.get("strategies"), Mapping) else payload
    if not isinstance(body, Mapping):
        return []
    return sorted(str(key) for key in body if str(key) not in STRATEGIES)


def strategy_reason(strategy: str) -> str:
    """The reason code that records which strategy fired.

    Stored first in a breakout signal's ``reasons`` array and used as the cooldown
    discriminator: ``BREAKOUT_UP`` rows from ORB and from VWAP are different
    strategies with different cooldowns, and the signal row is the only place that
    distinction survives.
    """
    return f"strategy:{strategy}"


# ── events ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BreakoutEvent:
    """One level break the detector decided is worth telling someone about."""

    strategy: str
    direction: str
    instrument_id: int
    symbol: str
    level: float
    price: float
    candle_start: datetime
    trading_date: date
    volume_ratio: float | None
    cooldown_minutes: int
    telegram: bool
    reasons: tuple[str, ...] = ()
    #: The measured RSI, for the ``rsi`` strategy only — its ``level`` is the
    #: threshold that was crossed (40 / 60), and a person reading the alert wants
    #: the number, not the band. ``None`` for every other strategy.
    reading: float | None = None

    @property
    def is_up(self) -> bool:
        return self.direction == UP

    @property
    def exceed_pct(self) -> float:
        """How far past the level the break finished.

        A percentage of price for the three level strategies — how much the tape
        cleared the day high by. For ``rsi`` a price percentage of an RSI threshold
        would be meaningless, so it is **RSI points** past the level instead
        (37.2 below 40 is 2.8). The two uses never mix in one number, and the
        message says which one it is printing.
        """
        if self.strategy == "rsi":
            if self.reading is None:
                return 0.0
            return abs(self.reading - self.level)
        if self.level <= 0:
            return 0.0
        if self.is_up:
            return (self.price / self.level - 1.0) * 100.0
        return (1.0 - self.price / self.level) * 100.0

    @property
    def episode_id(self) -> str:
        """Level-keyed identity: one alert per level per direction per session.

        ``rsi`` keys on the *reading* rather than the threshold, because its level
        is a fixed 40 or 60 for the whole day: keyed on the threshold, a stock that
        dips to 38, recovers, and sinks to 31 in the afternoon would produce one row
        and one page, and the second, deeper dip — the one worth acting on — would be
        discarded as a duplicate. The strategy's own cooldown is what keeps the
        tape from paging on every point of that drift.
        """
        mark = self.level if self.reading is None else self.reading
        return (
            f"{self.trading_date.isoformat()}-{self.instrument_id}"
            f"-{self.strategy}-{self.direction}-{mark:.4f}"
        )

    def describe(self) -> str:
        """The line a person reads, without needing the dashboard open."""
        if self.strategy == "rsi":
            reading = 0.0 if self.reading is None else self.reading
            if self.direction == DOWN:
                return (
                    f"{self.symbol} RSI-14 {reading:.1f} below {self.level:g} "
                    f"in an up-trend — entry at ₹{self.price:,.2f}"
                )
            return (
                f"{self.symbol} RSI-14 {reading:.1f} above {self.level:g} "
                f"in a down-trend — overbought at ₹{self.price:,.2f}"
            )
        what = {
            ("session_break", UP): "broke the day high",
            ("session_break", DOWN): "broke the day low",
            ("orb", UP): "broke above the opening range",
            ("orb", DOWN): "broke below the opening range",
            ("vwap", UP): "reclaimed the VWAP",
            ("vwap", DOWN): "lost the VWAP",
        }[(self.strategy, self.direction)]
        volume = "" if self.volume_ratio is None else f" on {self.volume_ratio:.2f}x volume"
        arrow = "+" if self.is_up else "-"
        return (
            f"{self.symbol} {what} ₹{self.level:,.2f} at ₹{self.price:,.2f} "
            f"({arrow}{self.exceed_pct:.2f}%){volume}"
        )

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "direction": self.direction,
            "symbol": self.symbol,
            "level": round(self.level, 4),
            "price": round(self.price, 4),
            "exceed_pct": round(self.exceed_pct, 3),
            "volume_ratio": None if self.volume_ratio is None else round(self.volume_ratio, 2),
            "candle_start": self.candle_start.isoformat(),
        }


def _gate(ratio: float | None, minimum: float) -> bool:
    """Is there enough volume behind this break?

    ``minimum <= 0`` means the operator turned the gate off. Otherwise a missing
    ratio fails: no evidence, no alert.
    """
    if minimum <= 0:
        return True
    return ratio is not None and ratio >= minimum


def _reasons(
    strategy: str, code: str, params: StrategyParams, ratio: float | None
) -> tuple[str, ...]:
    parts = [strategy_reason(strategy), code]
    if params.threshold_pct > 0:
        parts.append(f"threshold_{params.threshold_pct:g}pct")
    if params.confirm_candles > 1:
        parts.append(f"confirmed_{params.confirm_candles}_candles")
    if ratio is not None:
        parts.append(f"volume_{ratio:.2f}x")
    return tuple(parts)


# ── per-instrument state ────────────────────────────────────────────────────


@dataclass
class _SessionBreak:
    """Arming state for the day high / low rule."""

    high: float | None = None  # session high through the *previous* candle
    low: float | None = None
    candles: int = 0
    armed_high: float | None = None
    armed_low: float | None = None
    rearm_high_below: float | None = None
    rearm_low_above: float | None = None
    closes_above: int = 0
    closes_below: int = 0


@dataclass
class _Orb:
    bucket: int | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    close_at: datetime | None = None
    volume: int = 0
    last_closed: int | None = None
    or_high: float | None = None
    or_low: float | None = None
    prior_buckets: int = 0
    prior_volume: int = 0
    confirm_above: int = 0
    confirm_below: int = 0
    fired_up: bool = False
    fired_down: bool = False


@dataclass
class _Vwap:
    side: str | None = None
    above: int = 0
    below: int = 0


@dataclass
class _Rsi:
    """The RSI rule's memory: the standing state, plus the buckets behind a new one.

    ``state`` is what the dashboard reads (via :meth:`BreakoutTracker.rsi_state`)
    and what the snapshot records, so the board, the row and the alert cannot
    disagree. ``pending``/``streak`` count *consecutive five-minute buckets* that
    agree on a state the tracker does not yet hold — the confirmation is a property
    of the candidate, so an ENTRY building a streak while an earlier OVERBOUGHT
    streak is abandoned starts at one, not at two.
    """

    state: str | None = None
    pending: str | None = None
    streak: int = 0
    reading: float | None = None
    trend: str | None = None
    last_close_at: datetime | None = None


@dataclass
class _InstrumentState:
    instrument_id: int
    trading_date: date
    symbol: str = ""
    session: _SessionBreak = field(default_factory=_SessionBreak)
    orb: _Orb = field(default_factory=_Orb)
    vwap: _Vwap = field(default_factory=_Vwap)
    rsi: _Rsi = field(default_factory=_Rsi)


class BreakoutTracker:
    """Runs the four detectors over a stream of completed 1-minute candles."""

    def __init__(
        self,
        *,
        strategies: StrategiesConfig,
        window: SessionWindow,
        orb_minutes: int = DEFAULT_ORB_MINUTES,
    ) -> None:
        if orb_minutes < 1:
            raise ValueError("orb_minutes must be >= 1")
        self.strategies = strategies
        self.window = window
        self.orb_minutes = orb_minutes
        self._state: dict[int, _InstrumentState] = {}
        self.events_raised = 0
        self.by_strategy: dict[str, int] = dict.fromkeys(STRATEGIES, 0)
        # Set only by :meth:`recover`: today's stored tape is replayed to rebuild
        # levels, and a replay must not page the phone for minutes already judged.
        self._muted = False

    # ── configuration ─────────────────────────────────────────────────────
    def reconfigure(self, strategies: StrategiesConfig) -> None:
        """Swap the parameters without dropping any level state.

        Called when the operator edits the page mid-session: an opening range does
        not change because a threshold did, and re-deriving it would re-alert a
        break the session already reported. Disabling a strategy takes effect on
        the next candle; whatever is armed stays armed.
        """
        self.strategies = strategies

    def _state_for(self, candle: Bar, symbol: str) -> _InstrumentState:
        stored = self._state.get(candle.instrument_id)
        if stored is None:
            stored = _InstrumentState(
                instrument_id=candle.instrument_id,
                trading_date=self.window.trading_date,
                symbol=symbol,
            )
            self._state[candle.instrument_id] = stored
        elif symbol:
            stored.symbol = symbol
        return stored

    # ── the per-candle entry point ────────────────────────────────────────
    def on_candle(
        self,
        candle: Bar,
        *,
        symbol: str = "",
        vwap: float | None = None,
        relative_volume: float | None = None,
        rsi_5m: float | None = None,
        trend: str | None = None,
    ) -> list[BreakoutEvent]:
        """Evaluate one completed candle; returns 0..n events, at most one per strategy.

        Call with candles of ``self.window``'s session, in time order. A candle from
        another trading day is ignored — warm-up history and a replay after a
        restart must never arm today's levels, and today's state must survive a
        late foreign bar — and a candle outside 09:15–15:30 still counts toward the
        day break but never enters an ORB bucket, the same cut the board's SQL makes.

        ``rsi_5m`` and ``trend`` feed the fourth detector and are ignored by the
        other three. Both are measured elsewhere — the reading by the indicator
        engine, the trend by the scanner — because the tracker must not keep a second
        implementation of either; see :meth:`_rsi`.
        """
        if candle.trading_date != self.window.trading_date:
            return []
        state = self._state_for(candle, symbol)
        events: list[BreakoutEvent] = []
        # Levels are read *before* this candle is folded in: the day-break rule is
        # "closed beyond the high that existed coming into this minute".
        session = state.session
        prior_high, prior_low, prior_candles = session.high, session.low, session.candles
        events += self._session_break(
            state, candle, prior_high, prior_low, prior_candles, relative_volume
        )
        events += self._orb(state, candle)
        events += self._vwap(state, candle, vwap, relative_volume)
        # This one judges the state whether or not it is armed: the reading belongs
        # on the candle and on the board, and `enabled` governs only the page.
        events += self._rsi(state, candle, rsi_5m, trend, relative_volume)
        # Only now does this candle become history for the next one.
        session.high = candle.high if prior_high is None else max(prior_high, candle.high)
        session.low = candle.low if prior_low is None else min(prior_low, candle.low)
        session.candles = prior_candles + 1
        if self._muted:
            # Folding history into the levels is the whole point; the events it
            # would have produced were delivered (or suppressed) when they happened.
            return []
        for event in events:
            self.events_raised += 1
            self.by_strategy[event.strategy] += 1
        return events

    def recover(self, rows: Iterable[tuple[Bar, float | None]], *, symbol: str = "") -> int:
        """Fold this session's already-stored candles in silently, return how many.

        A worker that starts at 11:40 has never seen the opening range, so without
        this the ORB detector stays mute until tomorrow and the first new high after
        the restart is reported as a *day* break against a level that ignores the
        morning. The minutes are in the database; replaying them through the same
        code path rebuilds exactly what the live tape would have built.

        ``rows`` must be in time order per instrument and carry the session VWAP as
        of that candle (``None`` where it cannot be reproduced). The relative-volume
        gate is deliberately not replayed — a stored minute has no honest ratio
        attached, and the live path recomputes it from the next candle onward.

        The RSI detector is the one rule that cannot be rebuilt this way, because a
        stored minute carries no five-minute reading: replaying without one leaves
        ``rsi_state`` empty until the next live bucket close, where the verdict is
        adopted as a baseline rather than paged. That is the intended behaviour — a
        restart should not phone the operator about a condition that was already
        standing before it — but it does mean the board's RSI column starts at the
        restart, not at 09:15, on a day the worker came back up mid-session.
        """
        self._muted = True
        folded = 0
        try:
            for candle, vwap in rows:
                if candle.trading_date != self.window.trading_date:
                    continue
                self.on_candle(candle, symbol=symbol, vwap=vwap)
                folded += 1
        finally:
            self._muted = False
        return folded

    def flush(self) -> list[BreakoutEvent]:
        """Finalize the still-open ORB bucket of every instrument (session end).

        The day's last bucket has no following candle to close it, so without this
        a break made at 15:26 would never be evaluated. The board's SQL sees that
        bucket because it queries a finished session; this is what makes live
        match retroactive.
        """
        events: list[BreakoutEvent] = []
        for state in self._state.values():
            bucket = state.orb.bucket
            if bucket is None:
                continue
            events += self._close_bucket(state, bucket)
        for event in events:
            self.events_raised += 1
            self.by_strategy[event.strategy] += 1
        return events

    def armed_summary(self) -> dict[str, Any]:
        """Small observability view for the heartbeat log."""
        states = [s.rsi.state for s in self._state.values()]
        return {
            "tracked_instruments": len(self._state),
            "events_raised": self.events_raised,
            "by_strategy": dict(self.by_strategy),
            "ranges_known": sum(1 for s in self._state.values() if s.orb.or_high is not None),
            # How much of the universe the fourth detector can actually read: a low
            # ``rsi_judged`` an hour into the session means the tape, not the rule, is
            # the reason nothing is on the board.
            "rsi_judged": sum(1 for state in states if state is not None),
            "rsi_entry": states.count(ENTRY),
            "rsi_overbought": states.count(OVERBOUGHT),
        }

    # ── session high / low ────────────────────────────────────────────────
    def _session_break(
        self,
        state: _InstrumentState,
        candle: Bar,
        prior_high: float | None,
        prior_low: float | None,
        prior_candles: int,
        relative_volume: float | None,
    ) -> list[BreakoutEvent]:
        params = self.strategies.session_break
        if not params.enabled or prior_candles < DAY_BREAK_MIN_CANDLES:
            return []
        sb = state.session
        margin = params.threshold_pct / 100.0
        out: list[BreakoutEvent] = []
        # Arming / re-arming. Disarmed after a break, the strategy waits for a
        # close back under the level it broke: that level is resistance again, so
        # the next break of the (higher) day high is a new leg, not a repeat.
        if sb.armed_high is None and prior_high is not None and (
            sb.rearm_high_below is None or candle.close < sb.rearm_high_below
        ):
            sb.armed_high = prior_high
            sb.rearm_high_below = None
            sb.closes_above = 0
        if sb.armed_low is None and prior_low is not None and (
            sb.rearm_low_above is None or candle.close > sb.rearm_low_above
        ):
            sb.armed_low = prior_low
            sb.rearm_low_above = None
            sb.closes_below = 0

        if sb.armed_high is not None:
            mark = sb.armed_high * (1.0 + margin)
            sb.closes_above = sb.closes_above + 1 if candle.close > mark else 0
            if sb.closes_above >= params.confirm_candles and _gate(
                relative_volume, params.min_volume_ratio
            ):
                out.append(
                    self._event(
                        params,
                        state=state,
                        strategy="session_break",
                        direction=UP,
                        candle=candle,
                        level=sb.armed_high,
                        ratio=relative_volume,
                        code="day_high_break",
                    )
                )
                sb.rearm_high_below = sb.armed_high
                sb.armed_high = None
                sb.closes_above = 0
        if sb.armed_low is not None:
            mark = sb.armed_low * (1.0 - margin)
            sb.closes_below = sb.closes_below + 1 if candle.close < mark else 0
            if sb.closes_below >= params.confirm_candles and _gate(
                relative_volume, params.min_volume_ratio
            ):
                out.append(
                    self._event(
                        params,
                        state=state,
                        strategy="session_break",
                        direction=DOWN,
                        candle=candle,
                        level=sb.armed_low,
                        ratio=relative_volume,
                        code="day_low_break",
                    )
                )
                sb.rearm_low_above = sb.armed_low
                sb.armed_low = None
                sb.closes_below = 0
        return out

    # ── opening range ─────────────────────────────────────────────────────
    def _orb(self, state: _InstrumentState, candle: Bar) -> list[BreakoutEvent]:
        if not self.strategies.orb.enabled:
            return []
        bucket = self.bucket_of(candle.candle_start)
        if bucket is None:
            return []
        orb = state.orb
        if orb.last_closed is not None and bucket <= orb.last_closed:
            return []  # a late candle for a bucket already judged
        if orb.bucket is None:
            self._open_bucket(orb, bucket, candle)
            return []
        if bucket == orb.bucket:
            self._extend_bucket(orb, candle)
            return []
        # A later bucket started, so the previous one is final: judge it now.
        events = self._close_bucket(state, orb.bucket)
        self._open_bucket(orb, bucket, candle)
        return events

    def _open_bucket(self, orb: _Orb, bucket: int, candle: Bar) -> None:
        orb.bucket = bucket
        orb.high = candle.high
        orb.low = candle.low
        orb.close = candle.close
        orb.close_at = candle.candle_start
        orb.volume = max(int(candle.volume), 0)

    def _extend_bucket(self, orb: _Orb, candle: Bar) -> None:
        assert orb.high is not None and orb.low is not None  # set by _open_bucket
        orb.high = max(orb.high, candle.high)
        orb.low = min(orb.low, candle.low)
        # Candles arrive in time order, so the newest close is the bucket's close.
        orb.close = candle.close
        orb.close_at = candle.candle_start
        orb.volume += max(int(candle.volume), 0)

    def _close_bucket(self, state: _InstrumentState, bucket: int) -> list[BreakoutEvent]:
        """Judge one finalized 5-minute bucket against the opening range."""
        orb = state.orb
        params = self.strategies.orb
        if (
            orb.bucket != bucket
            or orb.close is None
            or orb.high is None
            or orb.low is None
            or orb.close_at is None
        ):
            return []
        events: list[BreakoutEvent] = []
        if bucket == 0:
            # The range itself: max(high) / min(low) over the opening minutes —
            # the board's `bucket = 0` row, and every later bucket's baseline.
            orb.or_high, orb.or_low = orb.high, orb.low
        elif orb.or_high is not None and orb.or_low is not None:
            mean_prior = orb.prior_volume / orb.prior_buckets if orb.prior_buckets else None
            ratio = orb.volume / mean_prior if mean_prior and mean_prior > 0 else None
            up_mark = orb.or_high * (1.0 + params.threshold_pct / 100.0)
            down_mark = orb.or_low * (1.0 - params.threshold_pct / 100.0)
            orb.confirm_above = orb.confirm_above + 1 if orb.close > up_mark else 0
            orb.confirm_below = orb.confirm_below + 1 if orb.close < down_mark else 0
            gate_ok = _gate(ratio, params.min_volume_ratio)
            for direction, level, code, fired in (
                (UP, orb.or_high, "orb_high_break", orb.fired_up),
                (DOWN, orb.or_low, "orb_low_break", orb.fired_down),
            ):
                confirm = orb.confirm_above if direction == UP else orb.confirm_below
                if fired or not gate_ok or confirm < params.confirm_candles:
                    continue
                if direction == UP:
                    orb.fired_up = True
                else:
                    orb.fired_down = True
                events.append(
                    self._bucket_event(
                        params,
                        state=state,
                        strategy="orb",
                        direction=direction,
                        candle_start=orb.close_at,
                        level=level,
                        price=orb.close,
                        ratio=ratio,
                        code=code,
                    )
                )
        orb.prior_buckets += 1
        orb.prior_volume += orb.volume
        orb.last_closed = bucket
        orb.bucket = None
        return events

    def bucket_of(self, moment: datetime) -> int | None:
        """5-minute bucket index from the session open, ``None`` outside it.

        ``floor((t - open_at) / 300)`` on the exchange clock — the same expression
        the board's SQL uses, so bucket 0 is 09:15–09:20 and the last bucket ends
        at 15:30.
        """
        local = to_ist(moment)
        delta = (local - self.window.open_at).total_seconds()
        if delta < 0 or local >= self.window.close_at:
            return None
        return int(delta // (self.orb_minutes * 60.0))

    # ── VWAP ──────────────────────────────────────────────────────────────
    def _vwap(
        self,
        state: _InstrumentState,
        candle: Bar,
        vwap: float | None,
        relative_volume: float | None,
    ) -> list[BreakoutEvent]:
        params = self.strategies.vwap
        if not params.enabled or vwap is None or vwap <= 0:
            return []
        vs = state.vwap
        band = params.threshold_pct / 100.0
        if candle.close > vwap * (1.0 + band):
            vs.above += 1
            vs.below = 0
        elif candle.close < vwap * (1.0 - band):
            vs.below += 1
            vs.above = 0
        # Inside the band neither side gains a confirmation and neither loses the
        # count it already has — the band exists to stop a hug-the-VWAP tape
        # firing a reclaim on every tick.
        won = UP if vs.above >= params.confirm_candles else None
        if won is None and vs.below >= params.confirm_candles:
            won = DOWN
        if won is None or won == vs.side:
            return []
        if vs.side is None:
            # First determination of the day, not a cross: the open prints on one
            # side of its own VWAP by construction, and paging for that is noise.
            vs.side = won
            vs.above = 0
            vs.below = 0
            return []
        if not _gate(relative_volume, params.min_volume_ratio):
            # A cross without volume evidence is not reported — and the standing
            # side is deliberately *kept*, so the same move on better volume later
            # in the session is still a fresh cross rather than a silent no-op.
            vs.above = 0
            vs.below = 0
            return []
        vs.side = won
        vs.above = 0
        vs.below = 0
        code = "vwap_reclaim" if won == UP else "vwap_loss"
        return [
            self._event(
                params,
                state=state,
                strategy="vwap",
                direction=won,
                candle=candle,
                level=vwap,
                ratio=relative_volume,
                code=code,
            )
        ]

    # ── RSI entry / overbought ────────────────────────────────────────────
    def _rsi(
        self,
        state: _InstrumentState,
        candle: Bar,
        rsi_5m: float | None,
        trend: str | None,
        relative_volume: float | None,
    ) -> list[BreakoutEvent]:
        """Judge the standing RSI state at every five-minute bucket close.

        The reading arrives already computed and the trend already decided, and that
        is deliberate: the indicator engine owns Wilder's recursion, the scanner owns
        "close above VWAP with the EMAs crossed up", and this method owns only what
        the operator's two numbers mean *together*. One implementation of the rule is
        what keeps the badge on the row, the board, and the Telegram line honest with
        each other.
        """
        params = self.strategies.rsi
        rs = state.rsi
        # Only the candle that *closes* a bucket carries a new reading. The four
        # inside it hold the previous one, and re-judging a held value as if it were
        # fresh would count one dip five times and call it a confirmation.
        if not closes_bucket(candle.candle_start):
            return []
        if rs.last_close_at is not None and candle.candle_start <= rs.last_close_at:
            return []  # a late or replayed candle for a bucket already judged
        if rsi_5m is None or trend is None:
            return []  # fewer than 15 buckets yet, or the indicators cannot judge
        rs.last_close_at = candle.candle_start
        rs.reading = rsi_5m
        rs.trend = trend
        verdict = classify_intraday(
            rsi_5m,
            trend,
            entry_threshold=params.oversold,
            overbought_threshold=params.overbought,
        )
        if verdict is None:  # only for a missing reading, already handled above
            return []
        if rs.state is None or verdict == rs.state or verdict == NEUTRAL:
            # Two silences, both inherited from the VWAP detector. The first
            # determination of the tape is a baseline, not a transition — a stock
            # whose opening buckets print oversold in an uptrend has not *done*
            # anything yet. And leaving a named state for the band is not news: it is
            # the dip being over.
            rs.state = verdict
            rs.pending = None
            rs.streak = 0
            return []
        if verdict == rs.pending:
            rs.streak += 1
        else:
            # A different side starts its own count; the abandoned one's buckets are
            # evidence about a state nobody is claiming.
            rs.pending = verdict
            rs.streak = 1
        if rs.streak < params.confirm_candles:
            return []
        if not _gate(relative_volume, params.min_volume_ratio):
            # A confirmed reading with no volume behind it is dropped and the count
            # restarts: "consecutive buckets" cannot survive one the gate rejected.
            rs.pending = None
            rs.streak = 0
            return []
        # The state is adopted whether or not the strategy is armed — the switch
        # governs the page, never the measurement the board shows.
        rs.state = verdict
        rs.pending = None
        rs.streak = 0
        if not params.enabled:
            return []
        if verdict == ENTRY:
            direction, level, code = DOWN, params.oversold, "rsi_oversold"
        else:
            direction, level, code = UP, params.overbought, "rsi_overbought"
        return [
            self._event(
                params,
                state=state,
                strategy="rsi",
                direction=direction,
                candle=candle,
                level=level,
                ratio=relative_volume,
                code=code,
                reading=rsi_5m,
            )
        ]

    # ── event construction ────────────────────────────────────────────────
    def _event(
        self,
        params: StrategyParams,
        *,
        state: _InstrumentState,
        strategy: str,
        direction: str,
        candle: Bar,
        level: float,
        ratio: float | None,
        code: str,
        reading: float | None = None,
    ) -> BreakoutEvent:
        return self._bucket_event(
            params,
            state=state,
            strategy=strategy,
            direction=direction,
            candle_start=candle.candle_start,
            level=level,
            price=candle.close,
            ratio=ratio,
            code=code,
            reading=reading,
        )

    def _bucket_event(
        self,
        params: StrategyParams,
        *,
        state: _InstrumentState,
        strategy: str,
        direction: str,
        candle_start: datetime,
        level: float,
        price: float,
        ratio: float | None,
        code: str,
        reading: float | None = None,
    ) -> BreakoutEvent:
        """Assemble one event.

        Separate from :meth:`_event` because an ORB break is attributed to the
        *bucket's* final candle, which is no longer in hand when the next bucket
        proves it was the last.
        """
        return BreakoutEvent(
            strategy=strategy,
            direction=direction,
            instrument_id=state.instrument_id,
            symbol=state.symbol,
            level=round(float(level), 4),
            price=round(float(price), 4),
            candle_start=candle_start,
            trading_date=state.trading_date,
            volume_ratio=None if ratio is None else round(float(ratio), 4),
            cooldown_minutes=params.cooldown_minutes,
            telegram=params.telegram,
            reasons=_reasons(strategy, code, params, ratio),
            reading=None if reading is None else round(float(reading), 4),
        )

    def rsi_state(self, instrument_id: int) -> str | None:
        """The RSI rule's standing verdict for one instrument.

        Read by the pipeline to stamp ``indicator_snapshots.rsi_state`` *after* the
        candle has been judged: the snapshot row and the alert therefore carry the
        same word, decided once. ``None`` means "not evaluable yet" — the young
        session has fewer than fifteen buckets, and an em dash on the dashboard is
        truer than a NEUTRAL nobody measured.
        """
        state = self._state.get(instrument_id)
        return None if state is None else state.rsi.state

