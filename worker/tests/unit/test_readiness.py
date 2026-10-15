"""Unit tests for the seven-state readiness machine (worker.analytics.readiness).

No arithmetic here — the machine only *arbitrates* facts other modules own, so
these tests are a precedence matrix. Each test names the pair of facts being
weighed and which one the brief (and the module docstring) says must win:

    STALE ▸ INVALIDATED ▸ EXTENDED_WAIT ▸ ENTRY_READY ▸ SETUP DEVELOPING ▸
    WEAKENING ▸ WATCH

``fresh_entry_allowed`` is asserted for all seven states — only ENTRY_READY
may raise a fresh entry alert, which is the gate the alert layer will read.
"""

from __future__ import annotations

from typing import Any

import pytest

from worker.analytics.extension import (
    EXTENSION_BLOCKED,
    EXTENSION_EXTENDED,
    EXTENSION_NORMAL,
    EXTENSION_PENDING,
)
from worker.analytics.readiness import (
    READINESS_ENTRY_READY,
    READINESS_EXTENDED_WAIT,
    READINESS_INVALIDATED,
    READINESS_LABELS,
    READINESS_SETUP_DEVELOPING,
    READINESS_STALE,
    READINESS_WATCH,
    READINESS_WEAKENING,
    ReadinessInputs,
    classify_readiness,
)

INVALIDATION_LEVEL = 99.5


def ready_inputs(**overrides: Any) -> ReadinessInputs:
    """A candidate that satisfies every ENTRY_READY condition."""
    base: dict[str, Any] = {
        "setup_trigger_confirmed": True,
        "mandatory_cleared": True,
        "extension_class": EXTENSION_NORMAL,
        "invalidation_level": INVALIDATION_LEVEL,
    }
    base.update(overrides)
    return ReadinessInputs(**base)


#: One input set per state, used by the cross-state assertions below.
ONE_PER_STATE: dict[str, ReadinessInputs] = {
    READINESS_STALE: ReadinessInputs(data_stale=True),
    READINESS_INVALIDATED: ready_inputs(invalidation_met=True),
    READINESS_EXTENDED_WAIT: ready_inputs(extension_class=EXTENSION_EXTENDED),
    READINESS_ENTRY_READY: ready_inputs(),
    READINESS_SETUP_DEVELOPING: ReadinessInputs(developing=True),
    READINESS_WEAKENING: ReadinessInputs(weakening=True),
    READINESS_WATCH: ReadinessInputs(),
}


class TestPrecedence:
    def test_stale_beats_everything(self):
        # Feed truth outranks every judgement about the stock.
        report = classify_readiness(
            ready_inputs(
                data_stale=True,
                invalidation_met=True,
                removal_streak_complete=True,
            )
        )
        assert report.state == READINESS_STALE
        assert report.reasons == ("data_stale",)

    def test_invalidated_beats_extended(self):
        report = classify_readiness(
            ready_inputs(invalidation_met=True, extension_class=EXTENSION_EXTENDED)
        )
        assert report.state == READINESS_INVALIDATED
        assert report.reasons == ("setup_invalidated",)

    def test_invalidated_beats_entry_ready_inputs(self):
        report = classify_readiness(ready_inputs(removal_streak_complete=True))
        assert report.state == READINESS_INVALIDATED
        assert report.reasons == ("removal_streak_complete",)

    def test_extended_beats_entry_ready(self):
        # Trigger, gates and risk block all present — only the chase verdict
        # stands between this and ENTRY READY.
        report = classify_readiness(ready_inputs(extension_class=EXTENSION_EXTENDED))
        assert report.state == READINESS_EXTENDED_WAIT

    def test_no_stale_candidate_is_ever_entry_ready(self):
        for inputs in ONE_PER_STATE.values():
            report = classify_readiness(
                ReadinessInputs(**{**inputs.__dict__, "data_stale": True})
            )
            assert report.state == READINESS_STALE


class TestEntryReady:
    def test_full_ready(self):
        report = classify_readiness(ready_inputs())
        assert report.state == READINESS_ENTRY_READY
        assert report.reasons == ("trigger_confirmed",)
        assert report.fresh_entry_allowed is True

    def test_missing_invalidation_level_is_developing(self):
        # The brief: no reliable invalidation level → the setup is incomplete,
        # not ready; the machine says exactly why.
        report = classify_readiness(ready_inputs(invalidation_level=None))
        assert report.state == READINESS_SETUP_DEVELOPING
        assert report.reasons == ("risk_block_incomplete:no_invalidation_level",)

    def test_uncomputed_extension_is_developing(self):
        report = classify_readiness(ready_inputs(extension_class=None))
        assert report.state == READINESS_SETUP_DEVELOPING
        assert report.reasons == ("extension_not_evaluable:not_computed",)

    def test_pending_extension_is_developing(self):
        report = classify_readiness(ready_inputs(extension_class=EXTENSION_PENDING))
        assert report.state == READINESS_SETUP_DEVELOPING
        assert report.reasons == ("extension_not_evaluable:PENDING",)

    def test_trigger_without_mandatory_falls_to_floor(self):
        # Not fresh (mandatory failed upstream) and not a developing pattern:
        # it lands on the watch floor with the reason recorded.
        report = classify_readiness(ready_inputs(mandatory_cleared=False))
        assert report.state == READINESS_WATCH
        assert report.reasons == ("trigger_confirmed_but_mandatory_failed",)

    def test_trigger_without_mandatory_and_weakening(self):
        report = classify_readiness(ready_inputs(mandatory_cleared=False, weakening=True))
        assert report.state == READINESS_WEAKENING
        assert report.reasons == (
            "trigger_confirmed_but_mandatory_failed",
            "weakening",
        )


class TestInvalidated:
    def test_reasons_are_composed_in_order(self):
        report = classify_readiness(
            ready_inputs(
                invalidation_met=True,
                removal_streak_complete=True,
                invalidation_reasons=("stop_level_broken", "structure_lost"),
            )
        )
        assert report.state == READINESS_INVALIDATED
        assert report.reasons == (
            "setup_invalidated",
            "removal_streak_complete",
            "stop_level_broken",
            "structure_lost",
        )


class TestExtendedWait:
    def test_extended_reason(self):
        report = classify_readiness(ready_inputs(extension_class=EXTENSION_EXTENDED))
        assert report.state == READINESS_EXTENDED_WAIT
        assert report.reasons == ("extension_extended",)

    def test_blocked_reason(self):
        report = classify_readiness(ready_inputs(extension_class=EXTENSION_BLOCKED))
        assert report.state == READINESS_EXTENDED_WAIT
        assert report.reasons == ("extension_blocked",)

    def test_label_spelling(self):
        assert READINESS_LABELS[READINESS_EXTENDED_WAIT] == "EXTENDED — WAIT"


class TestDevelopingAndFloors:
    def test_pattern_forming(self):
        report = classify_readiness(ReadinessInputs(developing=True))
        assert report.state == READINESS_SETUP_DEVELOPING
        assert report.reasons == ("pattern_forming",)

    def test_developing_beats_weakening(self):
        report = classify_readiness(ReadinessInputs(developing=True, weakening=True))
        assert report.state == READINESS_SETUP_DEVELOPING

    def test_watch_floor(self):
        report = classify_readiness(ReadinessInputs())
        assert report.state == READINESS_WATCH
        assert report.reasons == ("no_setup_candidate",)

    def test_weakening_floor(self):
        report = classify_readiness(ReadinessInputs(weakening=True))
        assert report.state == READINESS_WEAKENING
        assert report.reasons == ("no_setup_candidate", "weakening")

    def test_weakening_never_overrides_a_candidate(self):
        # A weakening read on a setup that also has a confirmed trigger and
        # failed mandatory: WEAKENING only lives below every candidate state.
        report = classify_readiness(
            ReadinessInputs(setup_trigger_confirmed=False, developing=False, weakening=True)
        )
        assert report.state == READINESS_WEAKENING


class TestFreshEntryGate:
    def test_only_entry_ready_allows_a_fresh_entry(self):
        for expected_state, inputs in ONE_PER_STATE.items():
            report = classify_readiness(inputs)
            assert report.state == expected_state
            assert report.fresh_entry_allowed is (expected_state == READINESS_ENTRY_READY)

    def test_labels_exist_for_all_seven_states(self):
        for state in ONE_PER_STATE:
            assert state in READINESS_LABELS


class TestIdempotence:
    def test_same_inputs_same_report(self):
        # The machine is a pure function of current facts — restart safety by
        # construction; history is rebuilt by replay, never stored here.
        inputs = ready_inputs(extension_class=EXTENSION_PENDING)
        first = classify_readiness(inputs)
        second = classify_readiness(inputs)
        assert first == second

    def test_to_log_dict(self):
        report = classify_readiness(ReadinessInputs(data_stale=True))
        payload = report.to_log_dict()
        assert payload == {
            "state": READINESS_STALE,
            "label": "STALE / DATA UNAVAILABLE",
            "reasons": ["data_stale"],
        }


@pytest.mark.parametrize(
    "state",
    [
        READINESS_STALE,
        READINESS_INVALIDATED,
        READINESS_EXTENDED_WAIT,
        READINESS_SETUP_DEVELOPING,
        READINESS_WEAKENING,
        READINESS_WATCH,
    ],
)
def test_no_state_other_than_entry_ready_allows_fresh(state):
    inputs = ONE_PER_STATE[state]
    assert classify_readiness(inputs).fresh_entry_allowed is False
