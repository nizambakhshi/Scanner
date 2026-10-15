"""Unit tests for the six-component score (worker.analytics.score).

Every expected total is worked out by hand in the comment above it, from the
normalization rule stated in the module docstring:

    total = 100 × Σ(wᵢ·sᵢ) / Σ(wᵢ over eligible components)

With the brief's default weights (20/20/20/15/15/10) the full-evidence fixture
scores, component by component:

    trend       mean(strong-bull 1.0, intraday aligned 1.0) = 1.0   → 20 · 1.0
    VWAP        (101.25 − 100.5)/1.0 = 0.75 ATR above; 1 − 0.75/3 = 0.75
                                                                    → 20 · 0.75
    volume      rvol 1.5/(2 × 0.75) = 1.0, liquidity HIGH 1.0;
                mean(1.0, 1.0) = 1.0                                → 20 · 1.0
    momentum    RSI fade (60 − 45)/20 = 0.75, MACD hist + → 1.0;
                mean(0.75, 1.0) = 0.875                             → 15 · 0.875
    structure   strongest of {vwap_reclaim 0.7, orb_retest 1.0} = 1.0
                                                                    → 15 · 1.0
    entry       NORMAL → 1.0                                        → 10 · 1.0

    total = (20 + 15 + 20 + 13.125 + 15 + 10) / 100 × 100 = 93.125
    completeness = 100/100 = 1.0 ; label HIGH_PRIORITY (≥ 80)

All of these are binary-exact in float, so the totals are asserted exactly;
only genuinely repeating decimals (none in the fixture) would need approx.
"""

from __future__ import annotations

import pytest

from worker.analytics.daily import (
    TREND_BULL,
    TREND_EMERGING,
    TREND_NEUTRAL,
    TREND_STRONG_BULL,
    TREND_UNKNOWN,
    TREND_WEAK,
)
from worker.analytics.extension import (
    EXTENSION_BLOCKED,
    EXTENSION_EXTENDED,
    EXTENSION_NORMAL,
    EXTENSION_PENDING,
)
from worker.analytics.score import (
    COMPONENT_ENTRY_QUALITY,
    COMPONENT_MOMENTUM,
    COMPONENT_STRUCTURE,
    COMPONENT_TREND,
    COMPONENT_VOLUME,
    COMPONENT_VWAP,
    LABEL_DEVELOPING,
    LABEL_HIGH,
    LABEL_LOW,
    LABEL_QUALIFIED,
    ScoreInputs,
    ScoreParams,
    ScoreWeights,
    compute_score,
)


def component_scores(report) -> dict[str, float | None]:
    return {component.name: component.score for component in report.components}


FULL = ScoreInputs(
    daily_trend_class=TREND_STRONG_BULL,
    fast_above_slow=True,
    price=101.25,
    vwap=100.5,
    atr=1.0,
    rvol=1.5,
    rvol_threshold=0.75,
    liquidity_class="HIGH",
    rsi_5m=45.0,
    macd_histogram=0.05,
    structure_evidence=("vwap_reclaim", "orb_retest"),
    extension_class=EXTENSION_NORMAL,
)


class TestFullFixture:
    def test_total_and_label(self):
        report = compute_score(FULL)
        assert report.total == 93.125
        assert report.label == LABEL_HIGH
        assert report.completeness == 1.0
        assert report.version == "score-v1"

    def test_component_scores(self):
        scores = component_scores(compute_score(FULL))
        assert scores == {
            COMPONENT_TREND: 1.0,
            COMPONENT_VWAP: 0.75,
            COMPONENT_VOLUME: 1.0,
            COMPONENT_MOMENTUM: 0.875,
            COMPONENT_STRUCTURE: 1.0,
            COMPONENT_ENTRY_QUALITY: 1.0,
        }

    def test_to_dict_carries_the_breakdown(self):
        payload = compute_score(FULL).to_dict()
        assert set(payload) == {"version", "total", "label", "completeness", "components"}
        assert payload["total"] == 93.125
        assert len(payload["components"]) == 6
        for component in payload["components"]:
            assert set(component) == {"name", "weight", "score", "detail"}


class TestNormalization:
    def test_single_eligible_component_normalizes_to_100(self):
        # Only the trend component has inputs; 100 × (20·1.0)/20 = 100.
        report = compute_score(
            ScoreInputs(daily_trend_class=TREND_STRONG_BULL, fast_above_slow=True)
        )
        assert report.total == 100.0
        assert report.completeness == 0.2  # 20/100
        assert report.label == LABEL_HIGH

    def test_all_missing_is_no_score_not_a_zero(self):
        report = compute_score(ScoreInputs())
        assert report.total is None
        assert report.label is None
        assert report.completeness == 0.0
        assert component_scores(report) == {
            COMPONENT_TREND: None,
            COMPONENT_VWAP: None,
            COMPONENT_VOLUME: None,
            COMPONENT_MOMENTUM: None,
            COMPONENT_STRUCTURE: None,
            COMPONENT_ENTRY_QUALITY: None,
        }

    def test_weights_need_not_total_100(self):
        # weights trend 10 + vwap 10 (others 0): eligible weight 20.
        # trend 1.0 and VWAP 0.75 →
        # total = 100 × (10·1.0 + 10·0.75)/20 = 100 × 17.5/20 = 87.5.
        params = ScoreParams(
            weights=ScoreWeights(
                trend_alignment=10.0,
                vwap_position=10.0,
                relative_volume_liquidity=0.0,
                momentum_confirmation=0.0,
                structure=0.0,
                entry_quality=0.0,
            )
        )
        inputs = ScoreInputs(
            daily_trend_class=TREND_STRONG_BULL,
            fast_above_slow=True,
            price=101.25,
            vwap=100.5,
            atr=1.0,
        )
        report = compute_score(inputs, params)
        assert report.total == 87.5
        assert report.completeness == 1.0  # 20/20
        assert report.label == LABEL_HIGH

    def test_partial_completeness_is_visible(self):
        # trend (20) + volume (20) eligible out of 100.
        report = compute_score(
            ScoreInputs(
                daily_trend_class=TREND_STRONG_BULL,
                fast_above_slow=True,
                rvol=1.5,
                rvol_threshold=0.75,
                liquidity_class="HIGH",
            )
        )
        assert report.completeness == 0.4
        assert report.total == 100.0

    @pytest.mark.parametrize(
        ("evidence_score", "expected_total", "expected_label"),
        [
            # The brief's 80/65/50 bands, inclusive floors.
            (0.80, 80.0, LABEL_HIGH),
            (0.65, 65.0, LABEL_QUALIFIED),
            (0.50, 50.0, LABEL_DEVELOPING),
            (0.499, 49.9, LABEL_LOW),
        ],
    )
    def test_label_bands(self, evidence_score, expected_total, expected_label):
        # Structure-only weights (100) and one custom evidence score make the
        # total exactly the component score × 100.
        params = ScoreParams(
            weights=ScoreWeights(
                trend_alignment=0.0,
                vwap_position=0.0,
                relative_volume_liquidity=0.0,
                momentum_confirmation=0.0,
                structure=100.0,
                entry_quality=0.0,
            ),
            structure_scores={"custom": evidence_score},
        )
        report = compute_score(ScoreInputs(structure_evidence=("custom",)), params)
        assert report.total == pytest.approx(expected_total)
        assert report.label == expected_label


class TestTrendComponent:
    def test_unknown_daily_class_contributes_nothing(self):
        # Absence of history is not an opinion: UNKNOWN alone → omitted.
        report = compute_score(ScoreInputs(daily_trend_class=TREND_UNKNOWN))
        assert component_scores(report)[COMPONENT_TREND] is None
        assert report.total is None

    def test_unknown_does_not_zero_a_known_intraday_read(self):
        report = compute_score(
            ScoreInputs(daily_trend_class=TREND_UNKNOWN, fast_above_slow=True)
        )
        assert component_scores(report)[COMPONENT_TREND] == 1.0

    def test_daily_class_scale(self):
        for trend_class, expected in (
            (TREND_STRONG_BULL, 1.0),
            (TREND_BULL, 0.8),
            (TREND_EMERGING, 0.6),
            (TREND_NEUTRAL, 0.3),
            (TREND_WEAK, 0.0),
        ):
            report = compute_score(ScoreInputs(daily_trend_class=trend_class))
            assert component_scores(report)[COMPONENT_TREND] == expected

    def test_weak_trend_scores_zero_but_remains_eligible(self):
        report = compute_score(ScoreInputs(daily_trend_class=TREND_WEAK))
        assert report.total == 0.0
        assert report.label == LABEL_LOW
        assert report.completeness == 0.2

    def test_corroborating_reads_average_never_sum(self):
        # daily 0.0 (weak) + intraday aligned 1.0 → mean 0.5, not 1.0.
        report = compute_score(
            ScoreInputs(daily_trend_class=TREND_WEAK, fast_above_slow=True)
        )
        assert component_scores(report)[COMPONENT_TREND] == 0.5


class TestVwapComponent:
    def test_below_vwap_scores_zero(self):
        report = compute_score(
            ScoreInputs(price=99.5, vwap=100.5, atr=1.0)
        )
        component = next(c for c in report.components if c.name == COMPONENT_VWAP)
        assert component.score == 0.0
        assert component.detail == "-1.00 ATR below VWAP"

    def test_at_vwap_scores_full(self):
        report = compute_score(ScoreInputs(price=100.5, vwap=100.5, atr=1.0))
        assert component_scores(report)[COMPONENT_VWAP] == 1.0

    def test_fade_reaches_zero_at_the_configured_distance(self):
        # 3 ATR above VWAP with the default 3.0: 1 − 3/3 = 0.0 (still
        # eligible — the price is above VWAP, the geometry is just late).
        report = compute_score(ScoreInputs(price=103.5, vwap=100.5, atr=1.0))
        component = next(c for c in report.components if c.name == COMPONENT_VWAP)
        assert component.score == 0.0
        assert component.detail == "3.00 ATR above VWAP"

    def test_beyond_zero_distance_clamps_at_zero(self):
        report = compute_score(ScoreInputs(price=104.5, vwap=100.5, atr=1.0))
        assert component_scores(report)[COMPONENT_VWAP] == 0.0

    @pytest.mark.parametrize(
        "overrides",
        [{"price": None}, {"vwap": None}, {"atr": None}, {"atr": 0.0}, {"vwap": 0.0}],
    )
    def test_missing_or_invalid_inputs_omit_the_component(self, overrides):
        base = {"price": 101.25, "vwap": 100.5, "atr": 1.0, **overrides}
        report = compute_score(ScoreInputs(**base))
        assert component_scores(report)[COMPONENT_VWAP] is None


class TestVolumeComponent:
    def test_rvol_at_full_multiple_scores_full(self):
        # rvol 1.5 = 2.0 × threshold 0.75 → 1.5/1.5 = 1.0.
        report = compute_score(ScoreInputs(rvol=1.5, rvol_threshold=0.75))
        assert component_scores(report)[COMPONENT_VOLUME] == 1.0

    def test_rvol_at_threshold_scores_half(self):
        # rvol 0.75 = 1.0 × threshold → 0.75/1.5 = 0.5.
        report = compute_score(ScoreInputs(rvol=0.75, rvol_threshold=0.75))
        assert component_scores(report)[COMPONENT_VOLUME] == 0.5
        assert report.total == 50.0
        assert report.label == LABEL_DEVELOPING

    def test_rvol_beyond_full_multiple_clamps(self):
        report = compute_score(ScoreInputs(rvol=3.0, rvol_threshold=0.75))
        assert component_scores(report)[COMPONENT_VOLUME] == 1.0

    def test_nonpositive_threshold_omits_evidence(self):
        report = compute_score(ScoreInputs(rvol=1.0, rvol_threshold=0.0))
        assert component_scores(report)[COMPONENT_VOLUME] is None

    def test_liquidity_scale_and_averaging(self):
        # rvol 1.5/1.5 = 1.0 and liquidity MEDIUM 0.6 → mean 0.8, not 1.6.
        report = compute_score(
            ScoreInputs(rvol=1.5, rvol_threshold=0.75, liquidity_class="MEDIUM")
        )
        assert component_scores(report)[COMPONENT_VOLUME] == 0.8

    def test_liquidity_alone_is_eligible(self):
        report = compute_score(ScoreInputs(liquidity_class="LOW"))
        assert component_scores(report)[COMPONENT_VOLUME] == 0.3


class TestMomentumComponent:
    @pytest.mark.parametrize(
        ("rsi", "expected"),
        [
            (30.0, 1.0),  # (60 − 30)/20 = 1.5 → clamped 1.0
            (40.0, 1.0),  # (60 − 40)/20 = 1.0
            (45.0, 0.75),  # (60 − 45)/20 = 0.75
            (50.0, 0.5),  # (60 − 50)/20 = 0.5
            (60.0, 0.0),
            (75.0, 0.0),  # negative fade clamps to 0.0
        ],
    )
    def test_rsi_fade(self, rsi, expected):
        report = compute_score(ScoreInputs(rsi_5m=rsi))
        assert component_scores(report)[COMPONENT_MOMENTUM] == expected

    @pytest.mark.parametrize(
        ("histogram", "expected"),
        [(0.1, 1.0), (0.0, 0.5), (-0.1, 0.0)],
    )
    def test_macd_histogram(self, histogram, expected):
        report = compute_score(ScoreInputs(macd_histogram=histogram))
        assert component_scores(report)[COMPONENT_MOMENTUM] == expected

    def test_rsi_and_macd_average(self):
        # fade 0.75 and histogram + → mean 0.875, not a sum.
        report = compute_score(ScoreInputs(rsi_5m=45.0, macd_histogram=0.05))
        assert component_scores(report)[COMPONENT_MOMENTUM] == 0.875


class TestStructureComponent:
    def test_not_evaluated_is_omitted(self):
        report = compute_score(ScoreInputs(structure_evidence=None))
        component = next(c for c in report.components if c.name == COMPONENT_STRUCTURE)
        assert component.score is None
        assert component.detail == "not evaluated"

    def test_evaluated_with_nothing_present_scores_zero(self):
        report = compute_score(ScoreInputs(structure_evidence=()))
        component = next(c for c in report.components if c.name == COMPONENT_STRUCTURE)
        assert component.score == 0.0
        assert component.detail == "no structure evidence"

    def test_strongest_evidence_wins(self):
        report = compute_score(ScoreInputs(structure_evidence=("vwap_reclaim", "orb_retest")))
        component = next(c for c in report.components if c.name == COMPONENT_STRUCTURE)
        assert component.score == 1.0
        assert component.detail == "strongest=orb_retest of orb_retest,vwap_reclaim"

    def test_single_evidence(self):
        report = compute_score(ScoreInputs(structure_evidence=("donchian_fresh",)))
        assert component_scores(report)[COMPONENT_STRUCTURE] == 0.6

    def test_unrecognised_evidence_scores_zero_visibly(self):
        report = compute_score(ScoreInputs(structure_evidence=("mystery",)))
        component = next(c for c in report.components if c.name == COMPONENT_STRUCTURE)
        assert component.score == 0.0
        assert component.detail == "unrecognised evidence: mystery"

    def test_unknown_evidence_is_ignored_beside_known(self):
        report = compute_score(
            ScoreInputs(structure_evidence=("mystery", "orb_retest"))
        )
        component = next(c for c in report.components if c.name == COMPONENT_STRUCTURE)
        assert component.score == 1.0
        assert component.detail == "strongest=orb_retest of orb_retest"


class TestEntryQualityComponent:
    def test_classification_mapping(self):
        for classification, expected in (
            (EXTENSION_NORMAL, 1.0),
            (EXTENSION_EXTENDED, 0.2),
            (EXTENSION_BLOCKED, 0.0),
        ):
            report = compute_score(ScoreInputs(extension_class=classification))
            assert component_scores(report)[COMPONENT_ENTRY_QUALITY] == expected, classification

    def test_pending_and_uncomputed_are_omitted(self):
        # Insufficient evidence is not a quality judgement.
        for classification in (EXTENSION_PENDING, None):
            report = compute_score(ScoreInputs(extension_class=classification))
            assert component_scores(report)[COMPONENT_ENTRY_QUALITY] is None


class TestValidationAndVersioning:
    def test_negative_weight_is_a_problem(self):
        weights = ScoreWeights(trend_alignment=-1.0)
        assert "trend_alignment weight must be >= 0" in weights.problems()

    def test_all_zero_weights_are_a_problem(self):
        weights = ScoreWeights(
            trend_alignment=0.0,
            vwap_position=0.0,
            relative_volume_liquidity=0.0,
            momentum_confirmation=0.0,
            structure=0.0,
            entry_quality=0.0,
        )
        assert "at least one weight must be positive" in weights.problems()

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"rvol_full_multiple": 1.0},
            {"rsi_entry": 60.0, "rsi_overbought": 40.0},
            {"vwap_zero_atr": 0.0},
            {"label_developing": 90.0},  # threshold order broken
        ],
    )
    def test_invalid_params_raise(self, kwargs):
        with pytest.raises(ValueError, match="invalid score parameters"):
            compute_score(ScoreInputs(), ScoreParams(**kwargs))

    def test_version_travels_with_the_report(self):
        report = compute_score(FULL, ScoreParams(version="score-v9"))
        assert report.version == "score-v9"
        assert report.to_dict()["version"] == "score-v9"
