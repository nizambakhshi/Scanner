"""Unit tests for the four-way anti-chase classification (worker.analytics.extension).

Every expected number is worked out by hand in the comment above it, from the
definitions in the module docstring — never copied from the implementation's
own output. The shared tape is chosen so every asserted boundary is
binary-exact:

    price 102.0 · session open 100.0 · previous close 100.0 · first-candle
    close 101.0 · VWAP 100.5 · fast EMA 100.0 · ATR 0.5 · opening-range high 101.0

    gap              (100.0 − 100.0) / 100.0 × 100 = 0.0 %
    change open      (102.0 − 100.0) / 100.0 × 100 = 2.0 %
    change first     (102.0 − 101.0) / 101.0 × 100 = 0.9900990099009901… %
    VWAP distance    (102.0 − 100.5) / 0.5 = 3.0 ATR exactly
    VWAP distance %  (102.0 − 100.5) / 100.5 × 100 = 1.4925373134328357… %
    EMA distance     (102.0 − 100.0) / 0.5 = 4.0 ATR exactly
    EMA distance %   (102.0 − 100.0) / 100.0 × 100 = 2.0 %
    open change      (102.0 − 100.0) / 0.5 = 4.0 ATR exactly
    ORB distance     (102.0 − 101.0) / 0.5 = 2.0 ATR exactly

The configuration-matrix test is acceptance #2 of audit D10: the same tape
under different operator documents must yield different verdicts. The
precedence tests pin the module's fixed order: PENDING ▸ BLOCKED ▸ EXTENDED ▸
NORMAL.
"""

from __future__ import annotations

from typing import Any

import pytest

from worker.analytics.extension import (
    EXTENSION_BLOCKED,
    EXTENSION_EXTENDED,
    EXTENSION_NORMAL,
    EXTENSION_PENDING,
    ExtensionClassifier,
    ExtensionMeasures,
    ExtensionParams,
)

BASE: dict[str, Any] = {
    "completed_candles": 5,
    "price": 102.0,
    "previous_close": 100.0,
    "session_open": 100.0,
    "first_candle_close": 101.0,
    "vwap": 100.5,
    "fast_ema": 100.0,
    "atr": 0.5,
    "orb_high": 101.0,
}


def tape(**overrides: Any) -> ExtensionMeasures:
    """The shared tape; overrides may set fields to ``None`` deliberately."""
    return ExtensionMeasures(**{**BASE, **overrides})


def classify(*, measures: ExtensionMeasures | None = None, **param_kwargs: Any):
    classifier = ExtensionClassifier(params=ExtensionParams(**param_kwargs))
    return classifier.classify(measures if measures is not None else tape())


class TestUnarmedBaseline:
    def test_default_params_are_inert(self):
        # Every limit None: nothing armed, so from a sufficient tape the
        # verdict is NORMAL no matter how far the price has run.
        verdict = classify()
        assert verdict.classification == EXTENSION_NORMAL
        assert verdict.reasons() == ("normal",)
        assert verdict.breaches == ()

    def test_every_measure_is_recorded(self):
        verdict = classify()
        values = dict(verdict.measurements)
        assert set(values) == {
            "gap_pct",
            "change_from_open_pct",
            "change_from_first_candle_pct",
            "vwap_distance_pct",
            "vwap_distance_atr",
            "ema_distance_pct",
            "ema_distance_atr",
            "open_change_atr",
            "orb_distance_atr",
        }
        assert values["gap_pct"] == 0.0
        assert values["change_from_open_pct"] == 2.0
        assert values["change_from_first_candle_pct"] == pytest.approx(
            0.9900990099009901, rel=1e-12
        )
        assert values["vwap_distance_atr"] == 3.0
        assert values["vwap_distance_pct"] == pytest.approx(1.4925373134328357, rel=1e-12)
        assert values["ema_distance_atr"] == 4.0
        assert values["ema_distance_pct"] == 2.0
        assert values["open_change_atr"] == 4.0
        assert values["orb_distance_atr"] == 2.0

    def test_unarmed_missing_measure_is_not_pending(self):
        # No ATR and no ATR-based limit: the ATR measures simply do not exist
        # in the record; the verdict is still NORMAL. Only an *armed* rule
        # that cannot be checked raises PENDING.
        verdict = classify(measures=tape(atr=None))
        assert verdict.classification == EXTENSION_NORMAL
        values = dict(verdict.measurements)
        assert "vwap_distance_atr" not in values
        assert "open_change_atr" not in values
        assert "orb_distance_atr" not in values
        # Non-ATR measures survive untouched.
        assert values["vwap_distance_pct"] == pytest.approx(1.4925373134328357, rel=1e-12)
        assert values["ema_distance_pct"] == 2.0


class TestArmedLimits:
    @pytest.mark.parametrize(
        ("kwargs", "expected_class", "expected_breach"),
        [
            # Each limit against the tape above; at-or-beyond is a breach.
            ({"max_gap_pct": 1.0}, EXTENSION_NORMAL, None),  # 0.0 < 1.0
            ({"max_change_from_open_pct": 2.0}, EXTENSION_EXTENDED, "max_change_from_open_pct"),
            ({"max_change_from_open_pct": 2.5}, EXTENSION_NORMAL, None),  # 2.0 < 2.5
            (
                {"max_change_from_first_candle_pct": 0.5},
                EXTENSION_EXTENDED,
                "max_change_from_first_candle_pct",  # 0.990099… >= 0.5
            ),
            ({"max_vwap_distance_pct": 1.0}, EXTENSION_EXTENDED, "max_vwap_distance_pct"),
            ({"max_vwap_distance_atr": 3.0}, EXTENSION_EXTENDED, "max_vwap_distance_atr"),
            ({"max_vwap_distance_atr": 3.5}, EXTENSION_NORMAL, None),  # 3.0 < 3.5
            ({"max_ema_distance_pct": 1.0}, EXTENSION_EXTENDED, "max_ema_distance_pct"),
            ({"max_ema_distance_atr": 3.0}, EXTENSION_EXTENDED, "max_ema_distance_atr"),
            ({"max_open_change_atr": 4.0}, EXTENSION_EXTENDED, "max_open_change_atr"),
            ({"max_orb_distance_atr": 2.0}, EXTENSION_EXTENDED, "max_orb_distance_atr"),
            ({"max_orb_distance_atr": 2.5}, EXTENSION_NORMAL, None),  # 2.0 < 2.5
        ],
    )
    def test_single_armed_limit_matrix(self, kwargs, expected_class, expected_breach):
        verdict = classify(**kwargs)
        assert verdict.classification == expected_class
        if expected_breach is None:
            assert verdict.breaches == ()
        else:
            assert [breach.name for breach in verdict.breaches] == [expected_breach]
            assert verdict.reasons() == (f"extended:{expected_breach}",)

    def test_limit_is_inclusive(self):
        # VWAP distance is exactly 3.0 ATR; a 3.0 cap is met, not beaten —
        # the module's contract is "at or beyond the limit".
        verdict = classify(max_vwap_distance_atr=3.0)
        assert verdict.classification == EXTENSION_EXTENDED
        breach = verdict.breaches[0]
        assert breach.measured == 3.0
        assert breach.limit == 3.0
        assert breach.describe() == "max_vwap_distance_atr: 3.00 vs 3"

    def test_multiple_breaches_recorded(self):
        # VWAP distance 1.4925 % >= 1.0 and EMA distance 2.0 % >= 1.5, in the
        # canonical armed() order.
        verdict = classify(max_ema_distance_pct=1.5, max_vwap_distance_pct=1.0)
        assert verdict.classification == EXTENSION_EXTENDED
        assert [breach.name for breach in verdict.breaches] == [
            "max_vwap_distance_pct",
            "max_ema_distance_pct",
        ]

    def test_limits_cap_upside_only(self):
        # price 99.5: VWAP distance −1.0/0.5 = −2.0 ATR, change from open
        # −0.5 % — both below any positive cap and therefore never breaches.
        verdict = classify(
            measures=tape(price=99.5),
            max_vwap_distance_atr=0.5,
            max_change_from_open_pct=0.5,
        )
        assert verdict.classification == EXTENSION_NORMAL
        assert verdict.breaches == ()

    def test_same_tape_different_documents(self):
        # Acceptance #2 of audit D10: the verdict follows the operator's
        # document, not the tape alone. VWAP distance is 3.0 ATR here.
        mild = classify(max_vwap_distance_atr=3.5)
        strict = classify(max_vwap_distance_atr=2.0)
        assert mild.classification == EXTENSION_NORMAL
        assert strict.classification == EXTENSION_EXTENDED
        assert strict.breaches[0].measured == 3.0
        assert strict.breaches[0].limit == 2.0

    def test_armed_order_is_canonical(self):
        params = ExtensionParams(
            max_orb_distance_atr=1.0, max_gap_pct=1.0, max_vwap_distance_atr=2.0
        )
        assert params.armed() == [
            "max_gap_pct",
            "max_vwap_distance_atr",
            "max_orb_distance_atr",
        ]


class TestPending:
    def test_feed_stale_is_pending(self):
        verdict = classify(measures=tape(feed_stale=True))
        assert verdict.classification == EXTENSION_PENDING
        assert verdict.reasons() == ("pending:feed stale",)

    def test_insufficient_candles_is_pending(self):
        verdict = classify(measures=tape(completed_candles=3))
        assert verdict.classification == EXTENSION_PENDING
        assert verdict.reasons() == ("pending:only 3 completed candles (need 5)",)

    def test_pending_carries_measurements(self):
        # The fix pinned after review: a PENDING verdict still records every
        # number that could be computed.
        verdict = classify(measures=tape(completed_candles=3))
        assert dict(verdict.measurements)["vwap_distance_atr"] == 3.0

    @pytest.mark.parametrize(
        ("param_kwargs", "overrides", "expected_reason"),
        [
            (
                {"max_vwap_distance_atr": 3.0},
                {"atr": None},
                "cannot measure vwap_distance_atr: no ATR",
            ),
            (
                {"max_vwap_distance_pct": 1.0},
                {"vwap": None},
                "cannot measure vwap_distance_pct: no price or VWAP",
            ),
            (
                {"max_orb_distance_atr": 2.0},
                {"orb_high": None},
                "cannot measure orb_distance_atr: no price or opening-range high",
            ),
            (
                {"max_open_change_atr": 3.0},
                {"session_open": None},
                "cannot measure open_change_atr: no price or session open",
            ),
        ],
    )
    def test_unmeasurable_armed_limit_is_pending(self, param_kwargs, overrides, expected_reason):
        # An armed rule whose input does not exist is an unanswerable
        # question, not a pass — and the reason names the missing input.
        verdict = classify(measures=tape(**overrides), **param_kwargs)
        assert verdict.classification == EXTENSION_PENDING
        assert verdict.reasons() == (f"pending:{expected_reason}",)

    def test_pending_reason_order(self):
        # Feed staleness and candle count precede the armed-limit checks.
        verdict = classify(measures=tape(feed_stale=True, completed_candles=3))
        assert verdict.reasons() == (
            "pending:feed stale",
            "pending:only 3 completed candles (need 5)",
        )

    def test_pending_beats_blocked(self):
        # Precedence: an unanswerable question outranks a known failure.
        verdict = classify(measures=tape(feed_stale=True, hard_block_reasons=("gate:miss",)))
        assert verdict.classification == EXTENSION_PENDING
        assert verdict.blocked == ()


class TestBlocked:
    def test_hard_block_reasons_blocked(self):
        verdict = classify(
            measures=tape(hard_block_reasons=("policy:never_chase", "data_quality:gap"))
        )
        assert verdict.classification == EXTENSION_BLOCKED
        assert verdict.blocked == ("policy:never_chase", "data_quality:gap")
        assert verdict.reasons() == (
            "blocked:policy:never_chase",
            "blocked:data_quality:gap",
        )

    def test_blocked_beats_extended(self):
        # An armed limit is also breached (3.0 ATR vs a 2.0 cap) but the hard
        # block wins the precedence.
        verdict = classify(
            measures=tape(hard_block_reasons=("policy:miss",)),
            max_vwap_distance_atr=2.0,
        )
        assert verdict.classification == EXTENSION_BLOCKED
        assert verdict.breaches == ()


class TestRendering:
    def test_to_log_dict_shape(self):
        verdict = classify(
            measures=tape(completed_candles=3),
            max_vwap_distance_atr=3.0,
        )
        payload = verdict.to_log_dict()
        assert payload["classification"] == EXTENSION_PENDING
        assert isinstance(payload["measurements"], dict)
        assert payload["breaches"] == []
        assert payload["pending"] == ["only 3 completed candles (need 5)"]
        assert payload["blocked"] == []

    def test_extended_flag(self):
        assert classify(max_vwap_distance_atr=3.0).extended is True
        assert classify().extended is False


class TestParameterValidation:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"min_completed_candles": 0},
            {"max_gap_pct": 0.0},  # limits must be strictly positive
            {"max_gap_pct": 100.5},
            {"max_change_from_open_pct": -1.0},
            {"max_vwap_distance_atr": 50.5},
            {"max_ema_distance_atr": 0.0},
        ],
    )
    def test_invalid_params_raise(self, kwargs):
        with pytest.raises(ValueError, match="invalid extension parameters"):
            ExtensionClassifier(params=ExtensionParams(**kwargs))
