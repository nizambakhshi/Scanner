"""The anti-chase policy: the line between a setup and a chase of it.

The brief has one hard rule above all the others — a bullish stock must never
become an entry signal *because* it is moving. Momentum is evidence about the
tape; it is not a licence to buy whatever the tape has already bought. This
module is where a candidate meets that rule: given the evidence the setup
tracker observed (:mod:`worker.analytics.setups`), it measures the dimensions
along which a real entry turns into a chase and returns one verdict per
dimension.

The dimensions, one gate each — deliberately **not** one fixed percent:

* **vwap_distance** — how far above the session VWAP the candidate's close
  already stands (``max_vwap_distance_pct``).
* **extension** — how far the move has run past its level before returning.
  Two independent measures, both configurable: the bars since the break
  (``max_bars_since_break``) and the run in ATR multiples
  (``max_extension_atr``), read off the retest candidate's own evidence. A
  fresh VWAP reclaim has no breakout behind it to be extended from — the gate
  judges retest candidates only, and says so rather than pretending to measure.
* **resistance** — the room between the candidate's price and the next level
  above it, from the level inventory
  (:func:`worker.analytics.levels.next_resistance`), in percent
  (``min_resistance_room_pct``) and in ATR multiples (``min_resistance_room_atr``).
  Little room overhead is the classic chase: the entry that buys into a wall.
  An inventory with *no level above the price* is blue sky and passes — an
  answer, not a silence.
* **stop_distance** — how far the candidate's close sits from the stop its
  setup defines, in percent (``max_stop_distance_pct``) and in ATR multiples
  (``max_stop_atr``). The stop basis is chosen per setup: an ORB retest stops
  at the opening range's opposite end, a VWAP reclaim at the dip's low.

Every limit defaults to ``None`` — **not configured**: it judges nothing and
blocks nothing — because the numbers belong to the operator's document
(``ops_settings['setup_engine']``, audit D7) and not to a builder's opinion.
The engine ships the machinery inert; until the document exists every verdict
here is observation, logged and nothing more.

Two rules bind every gate and are worth stating once:

* a gate that cannot measure its input **fails**, naming the missing input:
  no VWAP, no ATR, no inventory — the policy refuses to clear a candidate on
  evidence it does not have.
* a verdict is the full story: every enabled check appears with its measured
  value and limit, passed or not, so a log can tell "blocked at 1.42% against
  a 1.00% cap" apart from "blocked because no VWAP existed".

The policy is pure: candidates in, verdicts out. It holds no state, reads no
clock, and the same inputs always produce the same report — the
configuration-matrix tests lean on exactly that property.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from .levels import Level, next_resistance
from .setups import ReclaimCandidate, RetestCandidate

#: The setup labels a report carries — the two names the queue speaks.
SETUP_RETEST = "orb_retest"
SETUP_RECLAIM = "vwap_reclaim"

#: Gate names, in the fixed order every report lists them.
GATE_VWAP_DISTANCE = "vwap_distance"
GATE_EXTENSION = "extension"
GATE_RESISTANCE = "resistance"
GATE_STOP_DISTANCE = "stop_distance"

#: The one sentence both skip kinds share; which kind it is lives in the detail.
_NOT_CONFIGURED = "not configured"
_NOT_APPLICABLE = "not applicable to this setup"


@dataclass(frozen=True)
class AntiChaseParams:
    """The policy's limits — every ``None`` means the check is not configured.

    Field names carry the direction (``max_`` versus ``min_``) so a verdict's
    numbers read unambiguously; the operator document (audit D7) stores this
    same shape, and this dataclass is what validation will be run against.
    """

    max_vwap_distance_pct: float | None = None
    max_bars_since_break: int | None = None
    max_extension_atr: float | None = None
    min_resistance_room_pct: float | None = None
    min_resistance_room_atr: float | None = None
    max_stop_distance_pct: float | None = None
    max_stop_atr: float | None = None

    def problems(self) -> list[str]:
        found: list[str] = []
        for name, value in (
            ("max_vwap_distance_pct", self.max_vwap_distance_pct),
            ("min_resistance_room_pct", self.min_resistance_room_pct),
            ("max_stop_distance_pct", self.max_stop_distance_pct),
        ):
            if value is not None and not 0.0 < value <= 100.0:
                found.append(f"{name} must be between 0 and 100")
        for name, value in (
            ("max_extension_atr", self.max_extension_atr),
            ("min_resistance_room_atr", self.min_resistance_room_atr),
            ("max_stop_atr", self.max_stop_atr),
        ):
            if value is not None and not 0.0 < value <= 50.0:
                found.append(f"{name} must be between 0 and 50")
        if self.max_bars_since_break is not None and not 0 <= self.max_bars_since_break <= 480:
            found.append("max_bars_since_break must be between 0 and 480")
        return found


@dataclass(frozen=True)
class MarketContext:
    """What the policy needs to measure beyond the candidate itself.

    All optional because the tape decides what exists: a young session has a
    VWAP before it has an ATR, and the opening range has ends before a retest
    leg exists. ``levels`` is ``None`` when the caller kept no inventory at all
    — an honest "nothing to measure against", distinct from an inventory that
    is present but happens to be empty, which the resistance gate also refuses
    to read as blue sky.
    """

    vwap: float | None = None
    atr: float | None = None
    #: The opening range's low — the stop basis for an ORB retest candidate.
    orb_low: float | None = None
    #: Today's levels (``LevelBook.inventory``), or ``None`` when not kept.
    levels: Sequence[Level] | None = None


@dataclass(frozen=True)
class GateCheck:
    """One measured comparison inside a gate."""

    name: str
    measured: float | None
    limit: float
    passed: bool
    #: The missing input, in words, when the comparison could not be made.
    note: str = ""

    def describe(self) -> str:
        if self.measured is None:
            return f"{self.name}: {self.note or 'not measurable'}"
        state = "pass" if self.passed else "fail"
        return f"{self.name}: {self.measured:.2f} vs {self.limit:g} ({state})"


@dataclass(frozen=True)
class GateVerdict:
    """The policy's judgement of one gate, with every measurement behind it."""

    gate: str
    passed: bool
    checks: tuple[GateCheck, ...] = ()
    #: The gate judged nothing: not configured, or not applicable to this setup
    #: (``note`` says which). Distinct from passing — the log shows the
    #: difference, and a later readiness phase reads the same distinction.
    skipped: bool = False
    note: str = ""

    @property
    def detail(self) -> str:
        if self.skipped:
            return self.note or _NOT_CONFIGURED
        return "; ".join(check.describe() for check in self.checks)

    def reason(self) -> str:
        """One token for the snapshot's reasons column (the D6 vocabulary)."""
        if self.skipped:
            return f"antichase_{self.gate}_skip"
        state = "pass" if self.passed else "fail"
        return f"antichase_{self.gate}_{state}"


@dataclass(frozen=True)
class AntiChaseReport:
    """Every gate's verdict for one candidate, in the fixed gate order."""

    setup: str
    symbol: str
    verdicts: tuple[GateVerdict, ...]

    @property
    def cleared(self) -> bool:
        """True when every gate passed or was skipped.

        A *necessary* condition for entry readiness — the readiness phase adds
        the score floor on top — and never sufficient by itself.
        """
        return all(verdict.passed for verdict in self.verdicts)

    def reasons(self) -> tuple[str, ...]:
        return (f"setup_{self.setup}", *(verdict.reason() for verdict in self.verdicts))

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "setup": self.setup,
            "cleared": self.cleared,
            "gates": [
                {
                    "gate": verdict.gate,
                    "passed": verdict.passed,
                    "skipped": verdict.skipped,
                    "detail": verdict.detail,
                }
                for verdict in self.verdicts
            ],
        }


class AntiChasePolicy:
    """Judges one candidate against the configured gates. Pure and stateless."""

    def __init__(self, *, params: AntiChaseParams | None = None) -> None:
        self.params = params or AntiChaseParams()
        problems = self.params.problems()
        if problems:
            raise ValueError("invalid anti-chase parameters: " + "; ".join(problems))

    def evaluate(
        self,
        candidate: RetestCandidate | ReclaimCandidate,
        *,
        context: MarketContext,
    ) -> AntiChaseReport:
        """Judge one candidate; the same inputs always produce the same report."""
        if isinstance(candidate, RetestCandidate):
            setup, stop_basis = SETUP_RETEST, context.orb_low
        else:
            setup, stop_basis = SETUP_RECLAIM, candidate.dip_low
        price = candidate.reclaim_price
        return AntiChaseReport(
            setup=setup,
            symbol=candidate.symbol,
            verdicts=(
                self._vwap_distance(price, context),
                self._extension(candidate),
                self._resistance(price, context),
                self._stop_distance(price, stop_basis, context),
            ),
        )

    # ── the gates ──────────────────────────────────────────────────────────
    def _vwap_distance(self, price: float, context: MarketContext) -> GateVerdict:
        limit = self.params.max_vwap_distance_pct
        if limit is None:
            return GateVerdict(GATE_VWAP_DISTANCE, passed=True, skipped=True)
        if context.vwap is None or context.vwap <= 0:
            check = GateCheck(
                "vwap_distance_pct", None, limit, False, note="no VWAP to measure against"
            )
        else:
            measured = (price - context.vwap) / context.vwap * 100.0
            check = GateCheck("vwap_distance_pct", measured, limit, measured <= limit)
        return GateVerdict(GATE_VWAP_DISTANCE, passed=check.passed, checks=(check,))

    def _extension(self, candidate: RetestCandidate | ReclaimCandidate) -> GateVerdict:
        if isinstance(candidate, ReclaimCandidate):
            return GateVerdict(
                GATE_EXTENSION, passed=True, skipped=True, note=_NOT_APPLICABLE
            )
        checks: list[GateCheck] = []
        bars_limit = self.params.max_bars_since_break
        if bars_limit is not None:
            measured = float(candidate.bars_since_break)
            checks.append(
                GateCheck(
                    "bars_since_break", measured, float(bars_limit), measured <= bars_limit
                )
            )
        atr_limit = self.params.max_extension_atr
        if atr_limit is not None:
            measured = candidate.extension_atr
            if measured is None:
                checks.append(
                    GateCheck(
                        "extension_atr", None, atr_limit, False, note="no ATR at the break"
                    )
                )
            else:
                checks.append(
                    GateCheck("extension_atr", measured, atr_limit, measured <= atr_limit)
                )
        if not checks:
            return GateVerdict(GATE_EXTENSION, passed=True, skipped=True)
        return GateVerdict(GATE_EXTENSION, passed=all(c.passed for c in checks), checks=tuple(checks))

    def _resistance(self, price: float, context: MarketContext) -> GateVerdict:
        pct_limit = self.params.min_resistance_room_pct
        atr_limit = self.params.min_resistance_room_atr
        if pct_limit is None and atr_limit is None:
            return GateVerdict(GATE_RESISTANCE, passed=True, skipped=True)
        if context.levels is None or len(context.levels) == 0:
            note = "no level inventory to measure against"
            return GateVerdict(
                GATE_RESISTANCE,
                passed=False,
                checks=tuple(
                    GateCheck(name, None, limit, False, note=note)
                    for name, limit in (
                        ("resistance_room_pct", pct_limit),
                        ("resistance_room_atr", atr_limit),
                    )
                    if limit is not None
                ),
            )
        level = next_resistance(context.levels, price)
        if level is None:
            # Blue sky: nothing overhead is an answer, not a missing measurement.
            note = "no level above the price"
            return GateVerdict(
                GATE_RESISTANCE,
                passed=True,
                checks=tuple(
                    GateCheck(name, None, limit, True, note=note)
                    for name, limit in (
                        ("resistance_room_pct", pct_limit),
                        ("resistance_room_atr", atr_limit),
                    )
                    if limit is not None
                ),
            )
        room = level.price - price
        checks: list[GateCheck] = []
        if pct_limit is not None:
            measured = room / price * 100.0
            checks.append(
                GateCheck("resistance_room_pct", measured, pct_limit, measured >= pct_limit)
            )
        if atr_limit is not None:
            if context.atr is None or context.atr <= 0:
                checks.append(
                    GateCheck(
                        "resistance_room_atr", None, atr_limit, False, note="no ATR to measure against"
                    )
                )
            else:
                measured = room / context.atr
                checks.append(
                    GateCheck("resistance_room_atr", measured, atr_limit, measured >= atr_limit)
                )
        return GateVerdict(GATE_RESISTANCE, passed=all(c.passed for c in checks), checks=tuple(checks))

    def _stop_distance(
        self, price: float, stop_basis: float | None, context: MarketContext
    ) -> GateVerdict:
        pct_limit = self.params.max_stop_distance_pct
        atr_limit = self.params.max_stop_atr
        if pct_limit is None and atr_limit is None:
            return GateVerdict(GATE_STOP_DISTANCE, passed=True, skipped=True)
        if stop_basis is None:
            note = "no stop basis for this setup"
            return GateVerdict(
                GATE_STOP_DISTANCE,
                passed=False,
                checks=tuple(
                    GateCheck(name, None, limit, False, note=note)
                    for name, limit in (
                        ("stop_distance_pct", pct_limit),
                        ("stop_distance_atr", atr_limit),
                    )
                    if limit is not None
                ),
            )
        if stop_basis >= price:
            note = "the stop basis is not below the entry"
            return GateVerdict(
                GATE_STOP_DISTANCE,
                passed=False,
                checks=tuple(
                    GateCheck(name, None, limit, False, note=note)
                    for name, limit in (
                        ("stop_distance_pct", pct_limit),
                        ("stop_distance_atr", atr_limit),
                    )
                    if limit is not None
                ),
            )
        distance = price - stop_basis
        checks: list[GateCheck] = []
        if pct_limit is not None:
            measured = distance / price * 100.0
            checks.append(
                GateCheck("stop_distance_pct", measured, pct_limit, measured <= pct_limit)
            )
        if atr_limit is not None:
            if context.atr is None or context.atr <= 0:
                checks.append(
                    GateCheck(
                        "stop_distance_atr", None, atr_limit, False, note="no ATR to measure against"
                    )
                )
            else:
                measured = distance / context.atr
                checks.append(
                    GateCheck("stop_distance_atr", measured, atr_limit, measured <= atr_limit)
                )
        return GateVerdict(GATE_STOP_DISTANCE, passed=all(c.passed for c in checks), checks=tuple(checks))
