"""The four-way anti-chase classification: Normal, Extended, Blocked, Pending.

The brief's Stage 2 asks one question of every candidate — *is this entry a
chase of a move that has already happened?* — and insists the answer is a
persisted classification, not a log line. This module is that classifier. It is
the **measure-and-classify** half of the anti-chase story;
:class:`worker.analytics.antichase.AntiChasePolicy` remains the **gate**
implementation (per-candidate pass/fail verdicts against the operator's
document), and :mod:`worker.analytics.readiness` consumes both.

The four classes, exactly as the brief names them:

* ``PENDING`` — insufficient reliable data: the feed is stale, the session is
  younger than the confirmation window, or a measure with a configured limit
  cannot be computed because its input (VWAP, ATR, the opening range) does not
  exist yet. An armed rule that *cannot be checked* is not a pass and not a
  violation — it is an unanswerable question, and the class says so.
* ``BLOCKED`` — a configured anti-chase mandatory rule was violated or a
  data-quality hard failure occurred; the caller merges those reasons in
  (``hard_block_reasons``), because only the layer holding the policy verdicts
  and the feed status can decide them.
* ``EXTENDED`` — the measures exist and at least one sits at or beyond its
  configured limit: bullish, possibly late, watch for a pullback. **Extended is
  not bearish** — it blocks a fresh chase entry until a new setup qualifies
  (the brief's own words).
* ``NORMAL`` — measures exist, everything configured is inside its limit.

Precedence is fixed: ``PENDING ▸ BLOCKED ▸ EXTENDED ▸ NORMAL``.

The limits all default to ``None`` — **not configured**: nothing is armed, and
with every limit unset the classifier returns ``NORMAL``/``PENDING`` from data
sufficiency alone. That is deliberate: the brief forbids "an arbitrary universal
percentage", the numbers belong to the operator's document, and the draft
multiples written in the audit (e.g. distance-from-VWAP ≥ 3× ATR) stay *drafts*
until historical evaluation arms them. A verdict is the full story: every
measure that could be computed appears with its value, every breach with its
measured number and limit, so a record can tell "extended at 3.2× ATR against a
3.0 cap" apart from "blocked because no ATR existed".

Qualification is not this module's business: a stock that fails the mandatory
baseline is not a candidate at all, and the readiness machine — not this
classifier — owns that transition. This classifier answers only the chase
question, from completed-candle measures the caller supplies; a lower-timeframe
measure is recorded with the timeframe it was computed on by whoever computed
it. The classifier is pure: same measures, same params, same verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: The four classes; strings because they are persisted and displayed.
EXTENSION_NORMAL = "NORMAL"
EXTENSION_EXTENDED = "EXTENDED"
EXTENSION_BLOCKED = "BLOCKED"
EXTENSION_PENDING = "PENDING"

#: Each limit field and the measure it caps (the ``max_`` prefix is the
#: direction, not the name — this map is the one place the two spellings meet).
MEASURE_FOR_LIMIT: dict[str, str] = {
    "max_gap_pct": "gap_pct",
    "max_change_from_open_pct": "change_from_open_pct",
    "max_change_from_first_candle_pct": "change_from_first_candle_pct",
    "max_vwap_distance_pct": "vwap_distance_pct",
    "max_vwap_distance_atr": "vwap_distance_atr",
    "max_ema_distance_pct": "ema_distance_pct",
    "max_ema_distance_atr": "ema_distance_atr",
    "max_open_change_atr": "open_change_atr",
    "max_orb_distance_atr": "orb_distance_atr",
}


def _limit_problems(name: str, value: float | None, high: float) -> str | None:
    if value is not None and not 0.0 < value <= high:
        return f"{name} must be between 0 and {high:g}"
    return None


@dataclass(frozen=True)
class ExtensionParams:
    """The classifier's limits — every ``None`` means the measure is not armed.

    The percentage caps and ATR multiples are the operator document's numbers
    (audit D7 discipline, same as :class:`AntiChaseParams`): this module ships
    the machinery, the numbers arrive with approval and validation.
    """

    #: The completed-candle floor before any judgement is possible: the brief's
    #: initial-qualification window.
    min_completed_candles: int = 5
    max_gap_pct: float | None = None
    max_change_from_open_pct: float | None = None
    max_change_from_first_candle_pct: float | None = None
    max_vwap_distance_pct: float | None = None
    max_vwap_distance_atr: float | None = None
    max_ema_distance_pct: float | None = None
    max_ema_distance_atr: float | None = None
    max_open_change_atr: float | None = None
    max_orb_distance_atr: float | None = None

    def problems(self) -> list[str]:
        found: list[str] = []
        if self.min_completed_candles < 1:
            found.append("min_completed_candles must be >= 1")
        for name in (
            "max_gap_pct",
            "max_change_from_open_pct",
            "max_change_from_first_candle_pct",
            "max_vwap_distance_pct",
            "max_ema_distance_pct",
        ):
            problem = _limit_problems(name, getattr(self, name), 100.0)
            if problem:
                found.append(problem)
        for name in (
            "max_vwap_distance_atr",
            "max_ema_distance_atr",
            "max_open_change_atr",
            "max_orb_distance_atr",
        ):
            problem = _limit_problems(name, getattr(self, name), 50.0)
            if problem:
                found.append(problem)
        return found

    def armed(self) -> list[str]:
        """The names of every configured limit, in the canonical order."""
        return [
            name
            for name, value in (
                ("max_gap_pct", self.max_gap_pct),
                ("max_change_from_open_pct", self.max_change_from_open_pct),
                ("max_change_from_first_candle_pct", self.max_change_from_first_candle_pct),
                ("max_vwap_distance_pct", self.max_vwap_distance_pct),
                ("max_vwap_distance_atr", self.max_vwap_distance_atr),
                ("max_ema_distance_pct", self.max_ema_distance_pct),
                ("max_ema_distance_atr", self.max_ema_distance_atr),
                ("max_open_change_atr", self.max_open_change_atr),
                ("max_orb_distance_atr", self.max_orb_distance_atr),
            )
            if value is not None
        ]


@dataclass(frozen=True)
class ExtensionMeasures:
    """The raw tape facts the classifier measures from — all optional.

    Raw inputs, not precomputed percentages, so the arithmetic lives in one
    module (the level-book rule): the caller supplies what the tape knows and
    this module derives every configured measure itself.
    """

    completed_candles: int
    price: float | None = None
    previous_close: float | None = None
    session_open: float | None = None
    first_candle_close: float | None = None
    vwap: float | None = None
    fast_ema: float | None = None
    atr: float | None = None
    orb_high: float | None = None
    feed_stale: bool = False
    #: Merged by the caller: policy-gate violations and data-quality hard fails.
    hard_block_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExtensionBreach:
    """One measured value at or beyond its configured limit."""

    name: str
    measured: float
    limit: float

    def describe(self) -> str:
        return f"{self.name}: {self.measured:.2f} vs {self.limit:g}"


@dataclass(frozen=True)
class ExtensionVerdict:
    """The classification plus every number behind it."""

    classification: str
    measurements: tuple[tuple[str, float], ...] = ()
    breaches: tuple[ExtensionBreach, ...] = ()
    pending: tuple[str, ...] = ()
    blocked: tuple[str, ...] = ()

    @property
    def extended(self) -> bool:
        return self.classification == EXTENSION_EXTENDED

    def reasons(self) -> tuple[str, ...]:
        if self.classification == EXTENSION_PENDING:
            return tuple(f"pending:{reason}" for reason in self.pending)
        if self.classification == EXTENSION_BLOCKED:
            return tuple(f"blocked:{reason}" for reason in self.blocked)
        if self.classification == EXTENSION_EXTENDED:
            return tuple(f"extended:{breach.name}" for breach in self.breaches)
        return ("normal",)

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "measurements": dict(self.measurements),
            "breaches": [breach.describe() for breach in self.breaches],
            "pending": list(self.pending),
            "blocked": list(self.blocked),
        }


class ExtensionClassifier:
    """Classifies one candidate's chase risk. Pure and stateless."""

    def __init__(self, *, params: ExtensionParams | None = None) -> None:
        self.params = params or ExtensionParams()
        problems = self.params.problems()
        if problems:
            raise ValueError("invalid extension parameters: " + "; ".join(problems))

    def classify(self, measures: ExtensionMeasures) -> ExtensionVerdict:
        """Judge one candidate; the same measures always yield the same verdict."""
        pending: list[str] = []
        if measures.feed_stale:
            pending.append("feed stale")
        if measures.completed_candles < self.params.min_completed_candles:
            pending.append(
                f"only {measures.completed_candles} completed candles "
                f"(need {self.params.min_completed_candles})"
            )

        values, missing = self._measure(measures)
        for name in self.params.armed():
            measure_name = MEASURE_FOR_LIMIT[name]
            if measure_name not in values:
                pending.append(f"cannot measure {measure_name}: {missing[measure_name]}")

        if pending:
            return ExtensionVerdict(
                classification=EXTENSION_PENDING,
                measurements=self._sorted_pairs(values),
                pending=tuple(pending),
            )

        if measures.hard_block_reasons:
            return ExtensionVerdict(
                classification=EXTENSION_BLOCKED,
                measurements=self._sorted_pairs(values),
                blocked=measures.hard_block_reasons,
            )

        breaches = self._breaches(values)
        classification = EXTENSION_EXTENDED if breaches else EXTENSION_NORMAL
        return ExtensionVerdict(
            classification=classification,
            measurements=self._sorted_pairs(values),
            breaches=tuple(breaches),
        )

    # ── the arithmetic ─────────────────────────────────────────────────────
    def _measure(
        self, measures: ExtensionMeasures
    ) -> tuple[dict[str, float], dict[str, str]]:
        """All computable measures, and why each missing one is missing."""
        values: dict[str, float] = {}
        missing: dict[str, str] = {}
        price = measures.price

        def gap() -> tuple[float | None, str]:
            if measures.session_open is None or measures.previous_close is None:
                return None, "no session open or previous close"
            if measures.previous_close <= 0:
                return None, "previous close is not positive"
            return (
                (measures.session_open - measures.previous_close)
                / measures.previous_close
                * 100.0,
                "",
            )

        def from_open() -> tuple[float | None, str]:
            if price is None or measures.session_open is None:
                return None, "no price or session open"
            if measures.session_open <= 0:
                return None, "session open is not positive"
            return (price - measures.session_open) / measures.session_open * 100.0, ""

        def from_first() -> tuple[float | None, str]:
            if price is None or measures.first_candle_close is None:
                return None, "no price or first-candle close"
            if measures.first_candle_close <= 0:
                return None, "first-candle close is not positive"
            return (
                (price - measures.first_candle_close) / measures.first_candle_close * 100.0,
                "",
            )

        def vwap_distance() -> tuple[float | None, str]:
            if price is None or measures.vwap is None:
                return None, "no price or VWAP"
            if measures.vwap <= 0:
                return None, "VWAP is not positive"
            return price - measures.vwap, ""

        def ema_distance() -> tuple[float | None, str]:
            if price is None or measures.fast_ema is None:
                return None, "no price or fast EMA"
            if measures.fast_ema <= 0:
                return None, "fast EMA is not positive"
            return price - measures.fast_ema, ""

        def atr_guard() -> tuple[float | None, str]:
            if measures.atr is None:
                return None, "no ATR"
            if measures.atr <= 0:
                return None, "ATR is not positive"
            return measures.atr, ""

        candidates: list[tuple[str, tuple[float | None, str]]] = [
            ("gap_pct", gap()),
            ("change_from_open_pct", from_open()),
            ("change_from_first_candle_pct", from_first()),
        ]
        for name, (value, reason) in candidates:
            if value is not None:
                values[name] = value
            else:
                missing[name] = reason

        vwap_delta, vwap_reason = vwap_distance()
        if vwap_delta is not None:
            values["vwap_distance_pct"] = vwap_delta / measures.vwap * 100.0
            atr, atr_reason = atr_guard()
            if atr is not None:
                values["vwap_distance_atr"] = vwap_delta / atr
            else:
                missing["vwap_distance_atr"] = atr_reason
        else:
            missing["vwap_distance_pct"] = vwap_reason
            missing["vwap_distance_atr"] = vwap_reason

        ema_delta, ema_reason = ema_distance()
        if ema_delta is not None:
            values["ema_distance_pct"] = ema_delta / measures.fast_ema * 100.0
            atr, atr_reason = atr_guard()
            if atr is not None:
                values["ema_distance_atr"] = ema_delta / atr
            else:
                missing["ema_distance_atr"] = atr_reason
        else:
            missing["ema_distance_pct"] = ema_reason
            missing["ema_distance_atr"] = ema_reason

        if price is not None and measures.session_open is not None:
            atr, atr_reason = atr_guard()
            if atr is None:
                missing["open_change_atr"] = atr_reason
            elif measures.session_open <= 0:
                missing["open_change_atr"] = "session open is not positive"
            else:
                values["open_change_atr"] = (price - measures.session_open) / atr
        else:
            missing["open_change_atr"] = "no price or session open"

        if price is not None and measures.orb_high is not None:
            atr, atr_reason = atr_guard()
            if atr is not None:
                values["orb_distance_atr"] = (price - measures.orb_high) / atr
            else:
                missing["orb_distance_atr"] = atr_reason
        else:
            missing["orb_distance_atr"] = "no price or opening-range high"

        return values, missing

    def _breaches(self, values: dict[str, float]) -> list[ExtensionBreach]:
        """Every armed limit whose measured value sits at or beyond it."""
        breaches: list[ExtensionBreach] = []
        for name in self.params.armed():
            limit = getattr(self.params, name)
            measured = values.get(MEASURE_FOR_LIMIT[name])
            if measured is not None and limit is not None and measured >= limit:
                breaches.append(ExtensionBreach(name=name, measured=measured, limit=limit))
        return breaches

    @staticmethod
    def _sorted_pairs(values: dict[str, float]) -> tuple[tuple[str, float], ...]:
        return tuple(sorted(values.items()))
