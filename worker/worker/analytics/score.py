"""The §6 candidate score: six components, 100 points, visible completeness.

The brief's quality score, kept deliberately separate from the live watchlist
rank (:mod:`worker.analytics.ranking`): that 4-component score stays untouched
until a versioned switch is explicitly approved, and this module ships ready to
be that switch's other side. It is used on **candidate records** — a stock the
mandatory rules have already passed — so a high score can never override a
failed mandatory condition; the score is computed downstream of the gates by
construction, not by convention.

The six components, with the brief's proposed default weights (configurable,
versioned, "for backtesting, not established profitable parameters"):

============================  ======  ==============================================
Trend alignment               20      daily class × intraday fast/slow EMA
VWAP position / quality       20      distance above the session VWAP, in ATR units
Relative volume & liquidity   20      RVOL against its threshold × daily liquidity class
Momentum confirmation         15      RSI-5m against the house 40/60 lines × MACD histogram
Breakout/retest structure     15      strongest evidence on record (setup strategies)
Entry quality / anti-chase    10      the extension classification
============================  ======  ==============================================

Rules, each from the brief and each visible in the output:

* **Missing is not bullish.** Every component whose inputs are absent is
  ``None`` — it contributes nothing and is omitted from the normalization.
* **Completeness is visible.** ``completeness = eligible weight / total weight``
  (0–1) travels with the score, so a 90 built from two components cannot pose
  as a 90 built from six.
* **Normalize over eligible components**, documented method: the total is
  ``100 × Σ(wᵢ·sᵢ) / Σ(wᵢ over eligible i)``. Weights totalling 100 need no
  special case; other totals normalize identically, and a weights document that
  sums to neither is a configuration fact recorded here, not an error.
* **No correlated inflation.** Within a component, corroborating inputs are
  *averaged*, never summed — two trend facts are one trend component, by
  construction.
* **The labels never signal.** ``80/65/50`` label the score for manual
  attention only; the brief states only ENTRY READY can produce a fresh entry
  alert, and the readiness machine — not this module — owns that gate.

The scoring *shapes* below (the RSI band mapping, the ATR distance fade, the
per-evidence structure weights, the extension mapping) are draft defaults in
the same sense as the weights: documented, configurable, and waiting for
historical evaluation (§10E) before anyone calls them calibrated. The report
carries the full breakdown and the parameter version, so every stored score can
be reconstructed exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .daily import (
    TREND_BULL,
    TREND_EMERGING,
    TREND_NEUTRAL,
    TREND_STRONG_BULL,
    TREND_WEAK,
)
from .extension import (
    EXTENSION_BLOCKED,
    EXTENSION_EXTENDED,
    EXTENSION_NORMAL,
    EXTENSION_PENDING,
)

#: Component names; strings because they are stored in score breakdowns.
COMPONENT_TREND = "trend_alignment"
COMPONENT_VWAP = "vwap_position"
COMPONENT_VOLUME = "relative_volume_liquidity"
COMPONENT_MOMENTUM = "momentum_confirmation"
COMPONENT_STRUCTURE = "structure"
COMPONENT_ENTRY_QUALITY = "entry_quality"

#: Ranking labels (the brief's four bands; thresholds configurable).
LABEL_HIGH = "HIGH_PRIORITY"
LABEL_QUALIFIED = "QUALIFIED"
LABEL_DEVELOPING = "DEVELOPING"
LABEL_LOW = "LOW_PRIORITY"

#: The daily trend class as a 0–1 contribution; ``UNKNOWN`` is omitted, never 0
#: — absence of history is not an opinion about the trend.
_DAILY_TREND_SCORES: dict[str, float] = {
    TREND_STRONG_BULL: 1.0,
    TREND_BULL: 0.8,
    TREND_EMERGING: 0.6,
    TREND_NEUTRAL: 0.3,
    TREND_WEAK: 0.0,
}

#: The daily liquidity tercile as a 0–1 contribution.
_LIQUIDITY_SCORES: dict[str, float] = {
    "HIGH": 1.0,
    "MEDIUM": 0.6,
    "LOW": 0.3,
}


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


@dataclass(frozen=True)
class ScoreWeights:
    """The six weights. Zero disables a component; the sum needs no magic."""

    trend_alignment: float = 20.0
    vwap_position: float = 20.0
    relative_volume_liquidity: float = 20.0
    momentum_confirmation: float = 15.0
    structure: float = 15.0
    entry_quality: float = 10.0

    def problems(self) -> list[str]:
        found: list[str] = []
        for name in (
            "trend_alignment",
            "vwap_position",
            "relative_volume_liquidity",
            "momentum_confirmation",
            "structure",
            "entry_quality",
        ):
            if getattr(self, name) < 0:
                found.append(f"{name} weight must be >= 0")
        if self.total <= 0:
            found.append("at least one weight must be positive")
        return found

    @property
    def total(self) -> float:
        return (
            self.trend_alignment
            + self.vwap_position
            + self.relative_volume_liquidity
            + self.momentum_confirmation
            + self.structure
            + self.entry_quality
        )

    def as_dict(self) -> dict[str, float]:
        return {
            COMPONENT_TREND: self.trend_alignment,
            COMPONENT_VWAP: self.vwap_position,
            COMPONENT_VOLUME: self.relative_volume_liquidity,
            COMPONENT_MOMENTUM: self.momentum_confirmation,
            COMPONENT_STRUCTURE: self.structure,
            COMPONENT_ENTRY_QUALITY: self.entry_quality,
        }


@dataclass(frozen=True)
class ScoreParams:
    """Weights plus the scoring shapes' knobs — all drafts until §10E says otherwise."""

    weights: ScoreWeights = field(default_factory=ScoreWeights)
    version: str = "score-v1"
    #: RVOL reaching this multiple of its qualification threshold scores full
    #: marks for the relative-volume subscore (threshold itself = half marks).
    rvol_full_multiple: float = 2.0
    #: The house RSI lines: at/below ``rsi_entry`` full marks, at/above
    #: ``rsi_overbought`` zero — the operator's 40/60 rule as a fade.
    rsi_entry: float = 40.0
    rsi_overbought: float = 60.0
    #: Distance above VWAP, in ATR units, at which the VWAP subscore reaches 0.
    vwap_zero_atr: float = 3.0
    #: Structure scores per evidence id (draft defaults, strongest wins).
    structure_scores: dict[str, float] = field(
        default_factory=lambda: {
            "orb_retest": 1.0,
            "orb_breakout": 0.8,
            "vwap_reclaim": 0.7,
            "donchian_fresh": 0.6,
        }
    )
    #: The extension classification as an entry-quality score; PENDING omitted.
    extension_normal_score: float = 1.0
    extension_extended_score: float = 0.2
    extension_blocked_score: float = 0.0
    #: Label thresholds (the brief's 80/65/50).
    label_high: float = 80.0
    label_qualified: float = 65.0
    label_developing: float = 50.0

    def problems(self) -> list[str]:
        found = list(self.weights.problems())
        if self.rvol_full_multiple <= 1.0:
            found.append("rvol_full_multiple must be > 1")
        if not (self.rsi_entry < self.rsi_overbought):
            found.append("rsi_entry must be below rsi_overbought")
        if self.vwap_zero_atr <= 0:
            found.append("vwap_zero_atr must be positive")
        if not (self.label_developing < self.label_qualified < self.label_high):
            found.append("label thresholds must be strictly increasing")
        return found


@dataclass(frozen=True)
class ScoreInputs:
    """Everything a candidate can contribute; every field optional.

    The caller fills what the engines produced on this candidate's completed
    candles and leaves the rest ``None`` — absence lowers completeness and is
    never scored as a value.
    """

    #: Stage 1 daily trend class (``None`` when discovery hasn't run/joined).
    daily_trend_class: str | None = None
    #: Intraday fast EMA above slow EMA (the live baseline's middle condition).
    fast_above_slow: bool | None = None
    #: Latest completed close / session VWAP / ATR-14 — the VWAP component.
    price: float | None = None
    vwap: float | None = None
    atr: float | None = None
    #: Relative volume and the threshold it was judged against.
    rvol: float | None = None
    rvol_threshold: float | None = None
    #: Daily liquidity tercile (``HIGH``/``MEDIUM``/``LOW``).
    liquidity_class: str | None = None
    rsi_5m: float | None = None
    macd_histogram: float | None = None
    #: Setup/strategy evidence ids on this candidate; empty = evaluated, none
    #: present (scores 0); ``None`` = not evaluated (omitted).
    structure_evidence: tuple[str, ...] | None = None
    #: The extension classification for the entry-quality component.
    extension_class: str | None = None


@dataclass(frozen=True)
class ComponentScore:
    """One component's contribution: weight, 0–1 score (or ``None``), detail."""

    name: str
    weight: float
    score: float | None
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "weight": self.weight,
            "score": self.score,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ScoreReport:
    """The full breakdown: total, per-component scores, completeness, label."""

    total: float | None
    label: str | None
    completeness: float
    components: tuple[ComponentScore, ...]
    version: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "total": self.total,
            "label": self.label,
            "completeness": self.completeness,
            "components": [component.to_dict() for component in self.components],
        }


def compute_score(inputs: ScoreInputs, params: ScoreParams | None = None) -> ScoreReport:
    """Score one candidate; the same inputs always produce the same report."""
    p = params or ScoreParams()
    problems = p.problems()
    if problems:
        raise ValueError("invalid score parameters: " + "; ".join(problems))

    components = (
        _trend_component(inputs, p),
        _vwap_component(inputs, p),
        _volume_component(inputs, p),
        _momentum_component(inputs, p),
        _structure_component(inputs, p),
        _entry_quality_component(inputs, p),
    )

    eligible = [component for component in components if component.score is not None]
    eligible_weight = sum(component.weight for component in eligible)
    total_weight = p.weights.total
    if eligible_weight <= 0:
        total = None
    else:
        total = (
            100.0
            * sum(component.weight * float(component.score) for component in eligible)
            / eligible_weight
        )
    completeness = eligible_weight / total_weight
    return ScoreReport(
        total=total,
        label=_label(total, p),
        completeness=completeness,
        components=components,
        version=p.version,
    )


def _label(total: float | None, params: ScoreParams) -> str | None:
    if total is None:
        return None
    if total >= params.label_high:
        return LABEL_HIGH
    if total >= params.label_qualified:
        return LABEL_QUALIFIED
    if total >= params.label_developing:
        return LABEL_DEVELOPING
    return LABEL_LOW


def _trend_component(inputs: ScoreInputs, params: ScoreParams) -> ComponentScore:
    weight = params.weights.trend_alignment
    subscores: list[float] = []
    details: list[str] = []
    if inputs.daily_trend_class is not None:
        daily = _DAILY_TREND_SCORES.get(inputs.daily_trend_class)
        if daily is not None:
            subscores.append(daily)
            details.append(f"daily={inputs.daily_trend_class.lower()}")
        # UNKNOWN (or an unrecognised class) contributes nothing, not a zero.
    if inputs.fast_above_slow is not None:
        subscores.append(1.0 if inputs.fast_above_slow else 0.0)
        details.append(f"intraday={'aligned' if inputs.fast_above_slow else 'crossed'}")
    score = _mean(subscores)
    detail = ", ".join(details) if details else "no trend evidence"
    return ComponentScore(COMPONENT_TREND, weight, score, detail)


def _vwap_component(inputs: ScoreInputs, params: ScoreParams) -> ComponentScore:
    weight = params.weights.vwap_position
    if (
        inputs.price is None
        or inputs.vwap is None
        or inputs.atr is None
        or inputs.vwap <= 0
        or inputs.atr <= 0
    ):
        return ComponentScore(COMPONENT_VWAP, weight, None, "no price/VWAP/ATR")
    distance_atr = (inputs.price - inputs.vwap) / inputs.atr
    if distance_atr < 0:
        return ComponentScore(
            COMPONENT_VWAP, weight, 0.0, f"{distance_atr:.2f} ATR below VWAP"
        )
    score = _clamp(1.0 - distance_atr / params.vwap_zero_atr)
    return ComponentScore(
        COMPONENT_VWAP, weight, score, f"{distance_atr:.2f} ATR above VWAP"
    )


def _volume_component(inputs: ScoreInputs, params: ScoreParams) -> ComponentScore:
    weight = params.weights.relative_volume_liquidity
    subscores: list[float] = []
    details: list[str] = []
    if (
        inputs.rvol is not None
        and inputs.rvol_threshold is not None
        and inputs.rvol >= 0
        and inputs.rvol_threshold > 0
    ):
        subscores.append(
            _clamp(inputs.rvol / (params.rvol_full_multiple * inputs.rvol_threshold))
        )
        details.append(f"rvol={inputs.rvol:.2f}")
    if inputs.liquidity_class is not None:
        liquidity = _LIQUIDITY_SCORES.get(inputs.liquidity_class)
        if liquidity is not None:
            subscores.append(liquidity)
            details.append(f"liquidity={inputs.liquidity_class.lower()}")
    score = _mean(subscores)
    detail = ", ".join(details) if details else "no volume evidence"
    return ComponentScore(COMPONENT_VOLUME, weight, score, detail)


def _momentum_component(inputs: ScoreInputs, params: ScoreParams) -> ComponentScore:
    weight = params.weights.momentum_confirmation
    subscores: list[float] = []
    details: list[str] = []
    if inputs.rsi_5m is not None:
        span = params.rsi_overbought - params.rsi_entry
        subscores.append(_clamp((params.rsi_overbought - inputs.rsi_5m) / span))
        details.append(f"rsi5m={inputs.rsi_5m:.1f}")
    if inputs.macd_histogram is not None:
        if inputs.macd_histogram > 0:
            subscores.append(1.0)
        elif inputs.macd_histogram == 0:
            subscores.append(0.5)
        else:
            subscores.append(0.0)
        details.append(f"macd_hist={inputs.macd_histogram:+.3f}")
    score = _mean(subscores)
    detail = ", ".join(details) if details else "no momentum evidence"
    return ComponentScore(COMPONENT_MOMENTUM, weight, score, detail)


def _structure_component(inputs: ScoreInputs, params: ScoreParams) -> ComponentScore:
    weight = params.weights.structure
    if inputs.structure_evidence is None:
        return ComponentScore(COMPONENT_STRUCTURE, weight, None, "not evaluated")
    known = [
        (evidence, params.structure_scores[evidence])
        for evidence in inputs.structure_evidence
        if evidence in params.structure_scores
    ]
    if not known:
        if not inputs.structure_evidence:
            return ComponentScore(
                COMPONENT_STRUCTURE, weight, 0.0, "no structure evidence"
            )
        unknown = ",".join(sorted(inputs.structure_evidence))
        return ComponentScore(
            COMPONENT_STRUCTURE, weight, 0.0, f"unrecognised evidence: {unknown}"
        )
    best_name, best_score = max(known, key=lambda pair: pair[1])
    all_names = ",".join(sorted(name for name, _ in known))
    return ComponentScore(
        COMPONENT_STRUCTURE, weight, best_score, f"strongest={best_name} of {all_names}"
    )


def _entry_quality_component(inputs: ScoreInputs, params: ScoreParams) -> ComponentScore:
    weight = params.weights.entry_quality
    classification = inputs.extension_class
    if classification == EXTENSION_NORMAL:
        return ComponentScore(COMPONENT_ENTRY_QUALITY, weight, params.extension_normal_score, "normal")
    if classification == EXTENSION_EXTENDED:
        return ComponentScore(
            COMPONENT_ENTRY_QUALITY, weight, params.extension_extended_score, "extended"
        )
    if classification == EXTENSION_BLOCKED:
        return ComponentScore(
            COMPONENT_ENTRY_QUALITY, weight, params.extension_blocked_score, "blocked"
        )
    # PENDING, None (not computed) or unknown: omitted — insufficient evidence
    # is not a quality judgement.
    reason = EXTENSION_PENDING if classification == EXTENSION_PENDING else "not computed"
    return ComponentScore(COMPONENT_ENTRY_QUALITY, weight, None, reason.lower())
