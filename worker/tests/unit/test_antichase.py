"""Unit tests for the anti-chase policy (worker.analytics.antichase).

Every expected number is worked out by hand in the comment above it, from the
definition stated in the module docstring — never copied from the
implementation's own output. The shared tape (``context`` and the two candidate
builders below) is chosen so each gate has one distinct, hand-checkable number.

On the default retest candidate — close 100.6, VWAP 100.2, ATR 0.5, opening
range low 99.8, session high 101.0 overhead:

* VWAP distance   (100.6 − 100.2) / 100.2 × 100 = 0.3992015968063787 %
* extension       (101.2 − 100.2) / 0.5 = 2.0 ATR exactly (in binary floats too)
* resistance room (101.0 − 100.6) / 100.6 × 100 = 0.39761431411531384 %
* stop distance   (100.6 − 99.8) / 100.6 × 100 = 0.7952286282306135 %

On the default reclaim candidate — crossed 100.0 at 100.3 over a dip low of
99.4: stop distance (100.3 − 99.4) / 100.3 × 100 = 0.8973080757726735 %, and its
placement a hair above the VWAP gives (100.3 − 100.2) / 100.2 × 100 =
0.09980039920159114 %.

The configuration-matrix test is acceptance #2 of audit D10: the same tape
under different operator documents must yield different verdicts.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.conftest import BASE_DAY, minute
from worker.analytics.antichase import (
    GATE_EXTENSION,
    GATE_RESISTANCE,
    GATE_STOP_DISTANCE,
    GATE_VWAP_DISTANCE,
    SETUP_RECLAIM,
    SETUP_RETEST,
    AntiChaseParams,
    AntiChasePolicy,
    MarketContext,
)
from worker.analytics.levels import SESSION_HIGH, SESSION_LOW, VWAP, Level
from worker.analytics.setups import ReclaimCandidate, RetestCandidate


def retest_candidate(**overrides: Any) -> RetestCandidate:
    """The ORB retest under judgement: TATAMOTORS reclaimed a 100.2 level at 100.6.

    Evidence defaults mirror the shapes ``test_setups`` builds: touch at 100.1,
    run-up to 101.2, six bars waited, break ATR 0.5.
    """
    base: dict[str, Any] = {
        "instrument_id": 7,
        "symbol": "TATAMOTORS",
        "trading_date": BASE_DAY,
        "level": 100.2,
        "break_candle_start": minute(15),
        "break_price": 100.5,
        "touched_at": minute(19),
        "reclaim_at": minute(21),
        "reclaim_price": 100.6,
        "min_low": 100.1,
        "extension_max": 101.2,
        "bars_since_break": 6,
        "volume_ratio": 1.9,
        "atr_at_break": 0.5,
    }
    base.update(overrides)
    return RetestCandidate(**base)


def reclaim_candidate(**overrides: Any) -> ReclaimCandidate:
    """The VWAP reclaim under judgement: crossed 100.0 at 100.3 over a 99.4 dip.

    The stop basis is the dip's low; the context's ``orb_low`` must never leak
    into this candidate's numbers.
    """
    base: dict[str, Any] = {
        "instrument_id": 7,
        "symbol": "TATAMOTORS",
        "trading_date": BASE_DAY,
        "level": 100.0,
        "reclaim_at": minute(24),
        "reclaim_price": 100.3,
        "dip_low": 99.4,
        "bars_below": 5,
        "volume_ratio": 1.5,
        "atr_at_reclaim": 0.5,
    }
    base.update(overrides)
    return ReclaimCandidate(**base)


def context(**overrides: Any) -> MarketContext:
    """A mid-morning tape: VWAP 100.2, ATR 0.5, opening range low 99.8.

    Inventory: the session high 101.0 overhead, the session low 99.2 far below,
    and the VWAP itself at 100.2. For the default retest close of 100.6 the
    nearest level above is the session high — room 0.4.
    """
    base: dict[str, Any] = {
        "vwap": 100.2,
        "atr": 0.5,
        "orb_low": 99.8,
        "levels": (
            Level(SESSION_HIGH, 101.0),
            Level(SESSION_LOW, 99.2),
            Level(VWAP, 100.2),
        ),
    }
    base.update(overrides)
    return MarketContext(**base)


def test_defaults_are_inert_judge_nothing_and_block_nothing() -> None:
    policy = AntiChasePolicy()
    report = policy.evaluate(retest_candidate(), context=context())
    assert report.setup == SETUP_RETEST == "orb_retest"
    assert report.symbol == "TATAMOTORS"
    assert report.cleared is True
    assert [v.gate for v in report.verdicts] == [
        GATE_VWAP_DISTANCE,
        GATE_EXTENSION,
        GATE_RESISTANCE,
        GATE_STOP_DISTANCE,
    ]
    for verdict in report.verdicts:
        assert verdict.skipped is True and verdict.passed is True
    assert report.reasons() == (
        "setup_orb_retest",
        "antichase_vwap_distance_skip",
        "antichase_extension_skip",
        "antichase_resistance_skip",
        "antichase_stop_distance_skip",
    )

    reclaim_report = AntiChasePolicy().evaluate(reclaim_candidate(), context=context())
    assert reclaim_report.cleared is True
    assert reclaim_report.reasons()[0] == "setup_vwap_reclaim"


def test_vwap_distance_measures_the_candidates_own_close() -> None:
    # (100.6 − 100.2) / 100.2 × 100 = 0.3992015968063787 %
    policy = AntiChasePolicy(params=AntiChaseParams(max_vwap_distance_pct=1.0))
    verdict = policy.evaluate(retest_candidate(), context=context()).verdicts[0]
    assert verdict.skipped is False and verdict.passed is True
    check = verdict.checks[0]
    assert check.name == "vwap_distance_pct"
    assert check.measured == pytest.approx(0.3992015968063787, rel=1e-12)
    assert check.limit == 1.0
    assert verdict.detail == "vwap_distance_pct: 0.40 vs 1 (pass)"

    # a 0.3% cap refuses the same candidate: 0.399…% does not fit
    strict = AntiChasePolicy(params=AntiChaseParams(max_vwap_distance_pct=0.3))
    strict_report = strict.evaluate(retest_candidate(), context=context())
    assert strict_report.cleared is False
    assert strict_report.reasons()[1] == "antichase_vwap_distance_fail"


def test_vwap_distance_fails_closed_when_no_vwap_exists() -> None:
    policy = AntiChasePolicy(params=AntiChaseParams(max_vwap_distance_pct=1.0))
    verdict = policy.evaluate(retest_candidate(), context=context(vwap=None)).verdicts[0]
    assert verdict.passed is False and verdict.skipped is False
    check = verdict.checks[0]
    assert check.measured is None
    assert check.note == "no VWAP to measure against"
    assert verdict.detail == "vwap_distance_pct: no VWAP to measure against"


def test_extension_bars_judges_the_wait_and_its_boundary() -> None:
    boundary = AntiChasePolicy(params=AntiChaseParams(max_bars_since_break=6))
    verdict = boundary.evaluate(retest_candidate(bars_since_break=6), context=context()).verdicts[1]
    assert verdict.skipped is False and verdict.passed is True
    assert verdict.checks[0].describe() == "bars_since_break: 6.00 vs 6 (pass)"

    late = AntiChasePolicy(params=AntiChaseParams(max_bars_since_break=6))
    verdict = late.evaluate(retest_candidate(bars_since_break=7), context=context()).verdicts[1]
    assert verdict.passed is False
    assert verdict.checks[0].describe() == "bars_since_break: 7.00 vs 6 (fail)"


def test_extension_atr_reads_the_run_in_atr_units_and_its_boundary() -> None:
    # (101.2 − 100.2) / 0.5 = 2.0 ATR exactly in binary floats: the boundary
    # is inclusive, so a 2.0 cap keeps the candidate.
    boundary = AntiChasePolicy(params=AntiChaseParams(max_extension_atr=2.0))
    verdict = boundary.evaluate(retest_candidate(), context=context()).verdicts[1]
    assert verdict.passed is True
    assert verdict.checks[0].measured == 2.0
    assert verdict.checks[0].limit == 2.0

    # the same run against a 1.5 ATR cap fails
    strict = AntiChasePolicy(params=AntiChaseParams(max_extension_atr=1.5))
    verdict = strict.evaluate(retest_candidate(), context=context()).verdicts[1]
    assert verdict.passed is False


def test_both_extension_measures_appear_in_the_configured_order() -> None:
    policy = AntiChasePolicy(
        params=AntiChaseParams(max_bars_since_break=30, max_extension_atr=3.0)
    )
    verdict = policy.evaluate(retest_candidate(), context=context()).verdicts[1]
    assert [check.name for check in verdict.checks] == ["bars_since_break", "extension_atr"]
    assert verdict.passed is True


def test_extension_atr_fails_closed_without_an_atr_at_the_break() -> None:
    policy = AntiChasePolicy(params=AntiChaseParams(max_extension_atr=3.0))
    report = policy.evaluate(retest_candidate(atr_at_break=None), context=context())
    verdict = report.verdicts[1]
    assert verdict.passed is False
    assert verdict.checks[0].measured is None
    assert verdict.checks[0].note == "no ATR at the break"
    assert report.cleared is False


def test_a_reclaim_has_no_breakout_extension_to_judge() -> None:
    # Limits are configured, but the gate judges retest candidates only — and
    # says so, rather than pretending to measure.
    policy = AntiChasePolicy(
        params=AntiChaseParams(max_bars_since_break=1, max_extension_atr=0.5)
    )
    report = policy.evaluate(reclaim_candidate(), context=context())
    verdict = report.verdicts[1]
    assert verdict.skipped is True and verdict.passed is True
    assert verdict.note == "not applicable to this setup"
    assert verdict.reason() == "antichase_extension_skip"
    assert report.cleared is True


def test_resistance_room_measures_to_the_nearest_level_above() -> None:
    # The next level above the 100.6 close is the session high 101.0:
    # (101.0 − 100.6) / 100.6 × 100 = 0.39761431411531384 %
    tight = AntiChasePolicy(params=AntiChaseParams(min_resistance_room_pct=0.5))
    verdict = tight.evaluate(retest_candidate(), context=context()).verdicts[2]
    assert verdict.passed is False and verdict.skipped is False
    check = verdict.checks[0]
    assert check.name == "resistance_room_pct"
    assert check.measured == pytest.approx(0.39761431411531384, rel=1e-12)
    assert verdict.detail == "resistance_room_pct: 0.40 vs 0.5 (fail)"

    loose = AntiChasePolicy(params=AntiChaseParams(min_resistance_room_pct=0.3))
    assert loose.evaluate(retest_candidate(), context=context()).verdicts[2].passed is True


def test_resistance_room_in_atr_units() -> None:
    # 0.4 / 0.5 = 0.8 ATR of room
    fails = AntiChasePolicy(params=AntiChaseParams(min_resistance_room_atr=1.0))
    verdict = fails.evaluate(retest_candidate(), context=context()).verdicts[2]
    assert verdict.passed is False
    assert verdict.checks[0].measured == pytest.approx(0.8, rel=1e-12)

    passes = AntiChasePolicy(params=AntiChaseParams(min_resistance_room_atr=0.5))
    assert passes.evaluate(retest_candidate(), context=context()).verdicts[2].passed is True


def test_no_level_above_the_price_is_blue_sky_and_passes() -> None:
    policy = AntiChasePolicy(params=AntiChaseParams(min_resistance_room_pct=0.5))
    report = policy.evaluate(
        retest_candidate(), context=context(levels=(Level(SESSION_LOW, 99.2),))
    )
    verdict = report.verdicts[2]
    assert verdict.skipped is False and verdict.passed is True
    check = verdict.checks[0]
    assert check.measured is None and check.passed is True
    assert check.note == "no level above the price"
    assert report.cleared is True


def test_an_absent_inventory_fails_the_resistance_gate() -> None:
    policy = AntiChasePolicy(params=AntiChaseParams(min_resistance_room_pct=0.5))
    # None = the caller kept no inventory; () = kept but empty. Both refuse to
    # read as blue sky — a missing measurement is not permission.
    for absent in (None, ()):
        report = policy.evaluate(retest_candidate(), context=context(levels=absent))
        verdict = report.verdicts[2]
        assert verdict.passed is False and verdict.skipped is False
        assert verdict.checks[0].measured is None
        assert verdict.checks[0].note == "no level inventory to measure against"
        assert report.cleared is False


def test_stop_distance_for_a_retest_uses_the_open_range_low() -> None:
    # (100.6 − 99.8) / 100.6 × 100 = 0.7952286282306135 %
    tight = AntiChasePolicy(params=AntiChaseParams(max_stop_distance_pct=0.5))
    verdict = tight.evaluate(retest_candidate(), context=context()).verdicts[3]
    assert verdict.passed is False
    check = verdict.checks[0]
    assert check.name == "stop_distance_pct"
    assert check.measured == pytest.approx(0.7952286282306135, rel=1e-12)

    loose = AntiChasePolicy(params=AntiChaseParams(max_stop_distance_pct=1.0))
    assert loose.evaluate(retest_candidate(), context=context()).verdicts[3].passed is True


def test_stop_distance_for_a_reclaim_uses_the_dip_low_not_the_open_range() -> None:
    # (100.3 − 99.4) / 100.3 × 100 = 0.8973080757726735 %. The orb_low is
    # deliberately absurd (10.0) and then absent: if it leaked into the
    # reclaim's stop basis, the measured value would move or the gate would
    # fail closed — the dip's low is the only basis this setup has.
    policy = AntiChasePolicy(params=AntiChaseParams(max_stop_distance_pct=1.0))
    report = policy.evaluate(reclaim_candidate(), context=context(orb_low=10.0))
    assert report.setup == SETUP_RECLAIM
    verdict = report.verdicts[3]
    assert verdict.passed is True
    assert verdict.checks[0].measured == pytest.approx(0.8973080757726735, rel=1e-12)

    assert policy.evaluate(reclaim_candidate(), context=context(orb_low=None)).verdicts[
        3
    ].passed is True

    tight = AntiChasePolicy(params=AntiChaseParams(max_stop_distance_pct=0.5))
    assert (
        tight.evaluate(reclaim_candidate(), context=context(orb_low=10.0)).verdicts[3].passed
        is False
    )


def test_a_retest_without_an_open_range_fails_the_stop_gate() -> None:
    policy = AntiChasePolicy(params=AntiChaseParams(max_stop_distance_pct=1.0))
    verdict = policy.evaluate(retest_candidate(), context=context(orb_low=None)).verdicts[3]
    assert verdict.passed is False
    assert verdict.checks[0].measured is None
    assert verdict.checks[0].note == "no stop basis for this setup"


def test_a_stop_basis_at_or_above_the_entry_fails_the_gate() -> None:
    # A dip low of 100.5 is above the 100.3 cross: there is no stop below the
    # entry to measure, and the gate says why rather than reporting a negative.
    policy = AntiChasePolicy(params=AntiChaseParams(max_stop_distance_pct=1.0))
    report = policy.evaluate(reclaim_candidate(dip_low=100.5), context=context())
    verdict = report.verdicts[3]
    assert verdict.passed is False
    assert verdict.checks[0].measured is None
    assert verdict.checks[0].note == "the stop basis is not below the entry"


def test_the_configuration_matrix_one_tape_three_verdicts() -> None:
    """D10 acceptance #2: one candidate, different documents, different verdicts.

    The tape runs 2.4 ATR past its level — (101.4 − 100.2) / 0.5 — with a VWAP
    distance of 0.399 %, room of 0.398 % and a stop 0.795 % away. The generous
    document blocks nothing; the balanced one still clears; the strict
    resistance and strict extension documents each refuse it, on different gates.
    """
    tape = retest_candidate(extension_max=101.4, bars_since_break=9)
    shared: dict[str, Any] = {
        "max_vwap_distance_pct": 1.0,
        "max_bars_since_break": 30,
        "max_extension_atr": 3.0,
        "min_resistance_room_pct": 0.3,
        "max_stop_distance_pct": 1.0,
    }
    generous = AntiChasePolicy()
    balanced = AntiChasePolicy(params=AntiChaseParams(**shared))
    strict_resistance = AntiChasePolicy(
        params=AntiChaseParams(**{**shared, "min_resistance_room_pct": 0.5})
    )
    strict_extension = AntiChasePolicy(
        params=AntiChaseParams(**{**shared, "max_extension_atr": 2.0})
    )

    ctx = context()
    reports = [
        policy.evaluate(tape, context=ctx)
        for policy in (generous, balanced, strict_resistance, strict_extension)
    ]

    def failing(report: Any) -> list[str]:
        return [v.gate for v in report.verdicts if not v.passed]

    assert [r.cleared for r in reports] == [True, True, False, False]
    assert failing(reports[0]) == failing(reports[1]) == []
    assert failing(reports[2]) == [GATE_RESISTANCE]
    assert failing(reports[3]) == [GATE_EXTENSION]

    # Same tape, same measurements: only the verdicts differ. (The generous
    # document has no checks at all — every gate is skipped.)
    balanced_measured = [
        (v.gate, [c.measured for c in v.checks]) for v in reports[1].verdicts
    ]
    for report in reports[2:]:
        assert [
            (v.gate, [c.measured for c in v.checks]) for v in report.verdicts
        ] == balanced_measured


def test_a_fresh_reclaim_sits_near_its_vwap_by_construction() -> None:
    # (100.3 − 100.2) / 100.2 × 100 = 0.09980039920159114 % — the cross that
    # raises the event is a hair above the VWAP by definition, so this gate
    # rarely blocks a reclaim; the stop and the dip are what a chase shows up in.
    policy = AntiChasePolicy(params=AntiChaseParams(max_vwap_distance_pct=0.5))
    report = policy.evaluate(reclaim_candidate(), context=context())
    verdict = report.verdicts[0]
    assert verdict.passed is True
    assert verdict.checks[0].measured == pytest.approx(0.09980039920159114, rel=1e-12)


def test_params_validation_names_every_bad_limit() -> None:
    assert AntiChaseParams().problems() == []
    assert AntiChaseParams(max_vwap_distance_pct=0.0).problems() == [
        "max_vwap_distance_pct must be between 0 and 100"
    ]
    assert AntiChaseParams(min_resistance_room_pct=100.5).problems() == [
        "min_resistance_room_pct must be between 0 and 100"
    ]
    assert AntiChaseParams(max_stop_distance_pct=-1.0).problems() == [
        "max_stop_distance_pct must be between 0 and 100"
    ]
    assert AntiChaseParams(max_extension_atr=51.0).problems() == [
        "max_extension_atr must be between 0 and 50"
    ]
    assert AntiChaseParams(min_resistance_room_atr=0.0).problems() == [
        "min_resistance_room_atr must be between 0 and 50"
    ]
    assert AntiChaseParams(max_stop_atr=50.5).problems() == [
        "max_stop_atr must be between 0 and 50"
    ]
    assert AntiChaseParams(max_bars_since_break=-1).problems() == [
        "max_bars_since_break must be between 0 and 480"
    ]
    assert AntiChaseParams(max_bars_since_break=481).problems() == [
        "max_bars_since_break must be between 0 and 480"
    ]
    combined = AntiChaseParams(max_vwap_distance_pct=0.0, max_extension_atr=51.0)
    assert combined.problems() == [
        "max_vwap_distance_pct must be between 0 and 100",
        "max_extension_atr must be between 0 and 50",
    ]


def test_params_boundary_values_are_valid() -> None:
    assert (
        AntiChaseParams(
            max_vwap_distance_pct=100.0,
            max_bars_since_break=0,
            max_extension_atr=50.0,
            min_resistance_room_pct=0.001,
            min_resistance_room_atr=0.001,
            max_stop_distance_pct=100.0,
            max_stop_atr=0.001,
        ).problems()
        == []
    )


def test_the_policy_refuses_invalid_parameters_at_construction() -> None:
    with pytest.raises(ValueError, match="invalid anti-chase parameters"):
        AntiChasePolicy(params=AntiChaseParams(max_bars_since_break=481))


def test_evaluate_is_pure_the_same_inputs_always_agree() -> None:
    policy = AntiChasePolicy(params=AntiChaseParams(max_vwap_distance_pct=1.0))
    candidate = retest_candidate()
    ctx = context()
    first = policy.evaluate(candidate, context=ctx)
    second = policy.evaluate(candidate, context=ctx)
    assert first == second  # frozen dataclasses, no hidden state
    assert first is not second


def test_report_log_dict_carries_each_gate_story() -> None:
    policy = AntiChasePolicy(
        params=AntiChaseParams(min_resistance_room_pct=0.5, max_vwap_distance_pct=1.0)
    )
    report = policy.evaluate(retest_candidate(), context=context())
    assert report.cleared is False
    assert report.to_log_dict() == {
        "symbol": "TATAMOTORS",
        "setup": "orb_retest",
        "cleared": False,
        "gates": [
            {
                "gate": "vwap_distance",
                "passed": True,
                "skipped": False,
                "detail": "vwap_distance_pct: 0.40 vs 1 (pass)",
            },
            {
                "gate": "extension",
                "passed": True,
                "skipped": True,
                "detail": "not configured",
            },
            {
                "gate": "resistance",
                "passed": False,
                "skipped": False,
                "detail": "resistance_room_pct: 0.40 vs 0.5 (fail)",
            },
            {
                "gate": "stop_distance",
                "passed": True,
                "skipped": True,
                "detail": "not configured",
            },
        ],
    }
