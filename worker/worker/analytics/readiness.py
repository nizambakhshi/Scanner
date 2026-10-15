"""The entry-readiness machine: seven states, one candidate at a time.

The brief's Stage 3 statuses must never be conflated — *worth watching*, a
*developing setup*, a *confirmed trigger*, and *no longer eligible* are four
different operational facts — so the machine names seven states and fixes the
precedence by which one wins when several apply. This module owns the
state *classification*; the durable episode history (when a candidate entered a
state, how long it stayed) lives with the watchlist episode machinery, and the
alert policy reads :attr:`ReadinessReport.fresh_entry_allowed` as its gate.

The seven states, in precedence order (top wins):

1. ``STALE`` — the feed is stale or no usable candle exists: reliable
   evaluation is impossible, fresh alerts are suspended, the prior state is
   parked (the DATA_STALE discipline on the candidate axis).
2. ``INVALIDATED`` — the setup's own invalidation was met, or the watchlist's
   removal streak (three valid consecutive failing candles) completed.
3. ``EXTENDED_WAIT`` — chasing is blocked: extension class ``EXTENDED``
   (bullish structure remains, entry waits for a pullback) or ``BLOCKED`` (a
   configured rule or data-quality failure — same consequence for a fresh
   entry). The two carry different reasons, never different treatment.
4. ``ENTRY_READY`` — the trigger is confirmed, every mandatory filter passed,
   the extension class is ``NORMAL``, and a real invalidation level exists
   (the brief: if no reliable invalidation level can be identified, the setup
   is incomplete — it cannot be ENTRY READY).
5. ``SETUP DEVELOPING`` — a candidate pattern is forming, or a confirmed
   trigger that cannot yet be called ready for the two honest reasons: no
   invalidation level (incomplete risk block) or an extension state not yet
   evaluable (``PENDING``/not computed). The reason is recorded — "developing"
   without one is a shrug, not an answer.
6. ``WEAKENING`` — ``close < VWAP`` or the fast EMA fell to/below the slow one,
   with the removal streak still below its threshold. Deteriorated but not yet
   removed.
7. ``WATCH`` — the floor: an eligible baseline with no setup candidate.
   (Callers should only ask about watchlist members; for anything else this
   floor would overstate membership, so the machine documents that contract
   rather than guessing at membership it cannot see.)

The machine is a **pure function of the inputs' current facts** — feed status,
invalidation verdicts, trigger confirmation, extension class, level presence —
so it is restart-idempotent by construction: the same completed-candle world
always yields the same state, and history is rebuilt by replay, not stored in
the machine. Only ``ENTRY_READY`` may raise a fresh entry alert
(``fresh_entry_allowed``); an ``EXTENDED_WAIT`` or ``INVALIDATED`` candidate is
never re-alerted while it stays there (the alert layer's episode + cooldown
machinery owns that, on top of this gate).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .extension import (
    EXTENSION_BLOCKED,
    EXTENSION_EXTENDED,
    EXTENSION_NORMAL,
)

#: The seven states; strings because they are persisted, logged and displayed.
READINESS_WATCH = "WATCH"
READINESS_SETUP_DEVELOPING = "SETUP_DEVELOPING"
READINESS_ENTRY_READY = "ENTRY_READY"
READINESS_EXTENDED_WAIT = "EXTENDED_WAIT"
READINESS_WEAKENING = "WEAKENING"
READINESS_INVALIDATED = "INVALIDATED"
READINESS_STALE = "STALE"

#: Display labels, exactly as the brief spells them.
READINESS_LABELS: dict[str, str] = {
    READINESS_WATCH: "WATCH",
    READINESS_SETUP_DEVELOPING: "SETUP DEVELOPING",
    READINESS_ENTRY_READY: "ENTRY READY",
    READINESS_EXTENDED_WAIT: "EXTENDED — WAIT",
    READINESS_WEAKENING: "WEAKENING",
    READINESS_INVALIDATED: "INVALIDATED",
    READINESS_STALE: "STALE / DATA UNAVAILABLE",
}


@dataclass(frozen=True)
class ReadinessInputs:
    """One candidate's current facts, gathered by the caller from the engines.

    Every field is a verdict some other module already owns: feed status from
    the pipeline, invalidation from the setup tracker, the trigger from the
    candidate detector, the extension class from
    :class:`worker.analytics.extension.ExtensionClassifier`, the level from the
    level book. This machine re-derives none of them — it arbitrates.
    """

    #: Feed outage/staleness, or no usable completed candle for this instrument.
    data_stale: bool = False
    #: The setup's own invalidation conditions were met.
    invalidation_met: bool = False
    #: The watchlist removal streak completed (three valid failing candles).
    removal_streak_complete: bool = False
    invalidation_reasons: tuple[str, ...] = ()
    #: A setup trigger was confirmed on a completed candle.
    setup_trigger_confirmed: bool = False
    #: All mandatory filters passed (baseline conditions, policy gates).
    mandatory_cleared: bool = False
    #: The extension classification, or ``None`` when not yet computed.
    extension_class: str | None = None
    #: A real invalidation level exists (setup low, swing low, OR end…).
    invalidation_level: float | None = None
    #: A candidate pattern is forming (base, awaiting retest hold, …).
    developing: bool = False
    #: ``close < VWAP`` or fast EMA at/below slow — the weakening read.
    weakening: bool = False


@dataclass(frozen=True)
class ReadinessReport:
    """The state and the reasons that decided it, in precedence order."""

    state: str
    reasons: tuple[str, ...]

    @property
    def label(self) -> str:
        return READINESS_LABELS.get(self.state, self.state)

    @property
    def fresh_entry_allowed(self) -> bool:
        """Only ENTRY READY may raise a fresh entry alert (the brief's rule)."""
        return self.state == READINESS_ENTRY_READY

    def to_log_dict(self) -> dict[str, Any]:
        return {"state": self.state, "label": self.label, "reasons": list(self.reasons)}


def classify_readiness(inputs: ReadinessInputs) -> ReadinessReport:
    """Apply the precedence above; the same inputs always yield the same state."""
    if inputs.data_stale:
        return ReadinessReport(READINESS_STALE, ("data_stale",))

    if inputs.invalidation_met or inputs.removal_streak_complete:
        reasons: list[str] = []
        if inputs.invalidation_met:
            reasons.append("setup_invalidated")
        if inputs.removal_streak_complete:
            reasons.append("removal_streak_complete")
        reasons.extend(inputs.invalidation_reasons)
        return ReadinessReport(READINESS_INVALIDATED, tuple(reasons))

    if inputs.extension_class in (EXTENSION_EXTENDED, EXTENSION_BLOCKED):
        return ReadinessReport(
            READINESS_EXTENDED_WAIT, (f"extension_{inputs.extension_class.lower()}",)
        )

    if (
        inputs.setup_trigger_confirmed
        and inputs.mandatory_cleared
        and inputs.extension_class == EXTENSION_NORMAL
        and inputs.invalidation_level is not None
    ):
        return ReadinessReport(READINESS_ENTRY_READY, ("trigger_confirmed",))

    if inputs.setup_trigger_confirmed:
        if not inputs.mandatory_cleared:
            # Not a fresh entry and not a pattern either; it falls through to
            # the weakening/watch floor with the reason recorded.
            floor_reason = "trigger_confirmed_but_mandatory_failed"
        elif inputs.extension_class == EXTENSION_NORMAL:
            # Trigger and gates are there; the risk block is not. The brief:
            # classify the setup incomplete rather than invent a stop.
            return ReadinessReport(
                READINESS_SETUP_DEVELOPING, ("risk_block_incomplete:no_invalidation_level",)
            )
        else:
            # EXTENDED/BLOCKED returned above; what remains is PENDING or a
            # classification that has not been computed yet.
            return ReadinessReport(
                READINESS_SETUP_DEVELOPING,
                (f"extension_not_evaluable:{inputs.extension_class or 'not_computed'}",),
            )
    elif inputs.developing:
        return ReadinessReport(READINESS_SETUP_DEVELOPING, ("pattern_forming",))
    else:
        floor_reason = "no_setup_candidate"

    if inputs.weakening:
        return ReadinessReport(READINESS_WEAKENING, (floor_reason, "weakening"))
    return ReadinessReport(READINESS_WATCH, (floor_reason,))
