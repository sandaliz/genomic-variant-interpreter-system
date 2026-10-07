"""
tests/test_evaluation_metrics.py

Unit tests for the analysis functions in evaluation.py.

Calibration and subgroup accuracy are the two places in this project where a
wrong number would be *invisible*: a reliability table always draws a picture,
and a subgroup table always has rows, so both look like findings whether or not
the arithmetic under them is right. These tests pin the arithmetic -- ECE,
Brier, the tri-state directional outcome, the allele-frequency band boundaries,
and the small-sample guard -- against hand-computed values.

The functions are pure (a list of result dicts in, a dict out), so no dataset
and no pipeline is needed except for the one integration test at the bottom.
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from evaluation import (
    _allele_frequency_band,
    _directional_outcome,
    _optimistic_rank,
    _review_status_band,
    compute_calibration,
    compute_fairness,
    stratum_fields,
)

DATASET_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "clinvar_variant_dataset.xlsx")


def _calibration_row(confidence, outcome, key="match"):
    """A result dict carrying only what compute_calibration reads."""
    return {"confidence_numeric": confidence, key: outcome}


class TestDirectionalOutcome:
    def test_agreement_on_direction_is_true(self):
        assert _directional_outcome("Pathogenic", "Likely Pathogenic") is True
        assert _directional_outcome("Benign", "Benign") is True

    def test_disagreement_on_direction_is_false(self):
        assert _directional_outcome("Pathogenic", "Benign") is False

    def test_an_indeterminate_truth_is_not_a_failure(self):
        # "Conflicting classifications" is not a class the system can be wrong
        # about, so it must not be scored as a miss.
        assert _directional_outcome("Conflicting classifications of pathogenicity",
                                   "Benign") is None

    def test_a_vus_prediction_is_a_direction_not_an_unscorable_row(self):
        # "Uncertain" is one of the three directions (pathogenic / benign /
        # uncertain), so calling a pathogenic variant VUS is a directional miss
        # -- False. Returning None here would quietly drop every VUS prediction
        # out of the denominator and flatter the accuracy.
        assert _directional_outcome("Pathogenic", "Uncertain Significance (VUS)") is False

    def test_an_unmappable_label_is_skipped(self):
        # A label outside the taxonomy has no direction on either side.
        assert _directional_outcome("Pathogenic", "not a classification") is None
        assert _directional_outcome("not a classification", "Pathogenic") is None


class TestCalibration:
    def test_perfect_calibration_scores_zero_error(self):
        rows = ([_calibration_row(100.0, True) for _ in range(5)]
                + [_calibration_row(0.0, False) for _ in range(5)])
        report = compute_calibration(rows)
        assert report["total"] == 10
        assert report["ece"] == pytest.approx(0.0)
        assert report["brier"] == pytest.approx(0.0)
        assert report["brier_skill"] == pytest.approx(1.0)

    def test_overconfidence_is_measured_and_signed(self):
        # Four rows claiming 90, half of them right: gap = 0.5 - 0.9 = -0.4
        rows = ([_calibration_row(90.0, True) for _ in range(2)]
                + [_calibration_row(90.0, False) for _ in range(2)])
        report = compute_calibration(rows)
        assert report["total"] == 4
        assert report["bins"][0]["mean_confidence"] == pytest.approx(0.9)
        assert report["bins"][0]["accuracy"] == pytest.approx(0.5)
        assert report["bins"][0]["gap"] == pytest.approx(-0.4)
        assert report["ece"] == pytest.approx(0.4)
        assert report["mce"] == pytest.approx(0.4)
        # Brier = mean((0.9 - y)^2) over two hits and two misses
        assert report["brier"] == pytest.approx(0.41)
        # The reference forecast is the base rate (0.5); this is worse than it.
        assert report["brier_skill"] == pytest.approx(-0.64)

    def test_underconfidence_has_the_opposite_sign(self):
        rows = [_calibration_row(10.0, True) for _ in range(4)]
        report = compute_calibration(rows)
        assert report["bins"][0]["gap"] == pytest.approx(0.9)
        assert report["ece"] == pytest.approx(0.9)

    def test_ece_weights_bins_by_their_size(self):
        # Nine rows with a 0.5 gap and one row with no gap: ECE = 0.9 * 0.5 =
        # 0.45, not the unweighted mean of the two gaps (0.25).
        rows = ([_calibration_row(50.0, True) for _ in range(9)]
                + [_calibration_row(0.0, False)])
        report = compute_calibration(rows)
        assert report["ece"] == pytest.approx(0.45)
        assert report["mce"] == pytest.approx(0.5)

    def test_rows_without_an_outcome_are_excluded(self):
        rows = [_calibration_row(90.0, None), _calibration_row(90.0, True)]
        report = compute_calibration(rows)
        assert report["total"] == 1
        assert report["bins"][0]["count"] == 1

    def test_rows_without_a_confidence_are_excluded(self):
        rows = [{"confidence_numeric": None, "match": True},
                {"confidence_numeric": float("nan"), "match": True},
                _calibration_row(80.0, True)]
        assert compute_calibration(rows)["total"] == 1

    def test_an_empty_input_is_survivable(self):
        report = compute_calibration([])
        assert report["total"] == 0
        assert report["bins"] == []
        assert report["ece"] == 0.0

    def test_small_bins_are_flagged_rather_than_trusted(self):
        rows = [_calibration_row(90.0, True) for _ in range(4)]
        report = compute_calibration(rows)
        assert report["bins"][0]["small"] is True
        assert report["min_bin"] == 10

    def test_confidence_at_the_top_is_not_dropped(self):
        # probability 1.0 must land in the last bin, not fall off the end.
        report = compute_calibration([_calibration_row(100.0, True)])
        assert report["total"] == 1
        assert report["bins"][0]["upper"] == pytest.approx(1.0)

    def test_the_outcome_key_is_reported_back(self):
        assert compute_calibration([], key="directional_match")["outcome"] == \
            "directional_match"


class TestAlleleFrequencyBand:
    def _row(self, **frequencies):
        return frequencies

    def test_the_band_boundaries_are_the_acmg_thresholds(self):
        assert _allele_frequency_band(self._row(AF_ESP=0.0)) == \
            "absent_or_very_rare (<=0.01%)"
        assert _allele_frequency_band(self._row(AF_ESP=0.0001)) == "rare (0.01-0.1%)"
        assert _allele_frequency_band(self._row(AF_ESP=0.001)) == "low_frequency (0.1-1%)"
        assert _allele_frequency_band(self._row(AF_ESP=0.01)) == "common (1-5%)"
        assert _allele_frequency_band(self._row(AF_ESP=0.05)) == "too_common (>=5%)"

    def test_the_highest_of_the_three_frequencies_is_used(self):
        row = self._row(AF_ESP=0.0, AF_EXAC=0.2, AF_TGP=0.0002)
        assert _allele_frequency_band(row) == "too_common (>=5%)"

    def test_missing_frequencies_are_unknown_not_zero(self):
        # Unknown must not be silently folded into "absent", which would claim
        # population evidence the record does not have.
        assert _allele_frequency_band(self._row()) == "unknown"
        assert _allele_frequency_band(self._row(AF_ESP=float("nan"))) == "unknown"

    def test_unparseable_values_are_unknown(self):
        assert _allele_frequency_band(self._row(AF_ESP="not a number")) == "unknown"


class TestReviewStatusBand:
    def test_stars_are_banded(self):
        assert _review_status_band({"ClinVarReviewStars": "0"}) == "0 stars"
        assert _review_status_band({"ClinVarReviewStars": "1"}) == "1 star"
        assert _review_status_band({"ClinVarReviewStars": "2"}) == "2 stars"

    def test_three_and_four_stars_share_a_band(self):
        # Both mean an expert panel looked at it; the split is not readable at
        # this sample size.
        assert _review_status_band({"ClinVarReviewStars": "3"}) == \
            "3+ stars (expert reviewed)"
        assert _review_status_band({"ClinVarReviewStars": "4"}) == \
            "3+ stars (expert reviewed)"

    def test_a_missing_column_is_unknown(self):
        assert _review_status_band({}) == "unknown"
        assert _review_status_band({"ClinVarReviewStars": None}) == "unknown"
        assert _review_status_band({"ClinVarReviewStars": "abc"}) == "unknown"


class TestStratumFields:
    def test_all_three_keys_are_always_present(self):
        fields = stratum_fields({"Variant_type": "missense"})
        assert set(fields) == {"variant_type", "allele_frequency_band", "review_stars"}

    def test_a_blank_variant_type_is_unknown(self):
        assert stratum_fields({"Variant_type": None})["variant_type"] == "unknown"
        assert stratum_fields({"Variant_type": "   "})["variant_type"] == "unknown"


class TestOptimisticRank:
    def test_rank_counts_strictly_higher_scores(self):
        scores = [0.5, 0.9, 0.9, 0.1]
        assert _optimistic_rank(scores, 1) == 1
        assert _optimistic_rank(scores, 0) == 3
        assert _optimistic_rank(scores, 3) == 4

    def test_a_tie_resolves_in_the_documents_favour(self):
        # Both of these score 0.9; neither is behind the other.
        assert _optimistic_rank([0.9, 0.9], 0) == 1
        assert _optimistic_rank([0.9, 0.9], 1) == 1

    def test_an_out_of_range_position_is_none(self):
        assert _optimistic_rank([0.1, 0.2], 5) is None


class TestFairness:
    def _result(self, variant_type, match=True, directional_match=True):
        return {
            "variant_type": variant_type,
            "allele_frequency_band": "rare (0.01-0.1%)",
            "review_stars": "2 stars",
            "match": match,
            "match_reconciled": match,
            "directional_match": directional_match,
            "predicted": "Pathogenic" if match else "Benign",
            "predicted_reconciled": "Pathogenic" if match else "Benign",
            "ground_truth": "Pathogenic",
        }

    def test_accuracy_is_per_stratum_not_pooled(self):
        results = ([self._result("missense", match=True) for _ in range(5)]
                   + [self._result("missense", match=False) for _ in range(5)]
                   + [self._result("frameshift", match=True) for _ in range(10)])
        report = compute_fairness(results, min_group_size=5)["variant_type"]
        assert report["strata"]["missense"]["accuracy"] == pytest.approx(0.5)
        assert report["strata"]["frameshift"]["accuracy"] == pytest.approx(1.0)
        assert report["strata"]["missense"]["total"] == 10

    def test_the_worst_eligible_stratum_is_named(self):
        results = ([self._result("missense", match=True) for _ in range(7)]
                   + [self._result("missense", match=False) for _ in range(3)]
                   + [self._result("frameshift", match=True) for _ in range(10)])
        report = compute_fairness(results, min_group_size=5)["variant_type"]
        assert report["worst"]["stratum"] == "missense"
        assert report["worst"]["accuracy"] == pytest.approx(0.7)

    def test_a_small_stratum_cannot_be_the_worst_finding(self):
        # One wrong row in a group of two is not a finding, even though its
        # accuracy is the lowest in the table.
        results = ([self._result("missense", match=True) for _ in range(20)]
                   + [self._result("deletion", match=False) for _ in range(2)])
        report = compute_fairness(results, min_group_size=5)["variant_type"]
        assert report["strata"]["deletion"]["accuracy"] == 0.0
        assert report["strata"]["deletion"]["small"] is True
        assert report["small_strata"] == ["deletion"]
        assert report["worst"]["stratum"] == "missense"
        assert report["worst"]["accuracy"] == pytest.approx(1.0)

    def test_all_three_dimensions_are_reported(self):
        report = compute_fairness([self._result("missense")], min_group_size=1)
        assert set(report) == {"variant_type", "allele_frequency_band", "review_stars"}

    def test_a_dimension_with_no_eligible_stratum_says_so(self):
        report = compute_fairness([self._result("missense")], min_group_size=5)
        assert report["variant_type"]["worst"] is None
        assert report["variant_type"]["eligible_strata"] == 0

    def test_directional_accuracy_ignores_indeterminate_rows(self):
        results = [self._result("missense", directional_match=True)] * 3
        results.append({**self._result("missense"), "directional_match": None})
        block = compute_fairness(results, min_group_size=1)["variant_type"]["strata"]["missense"]
        assert block["directional_total"] == 3
        assert block["directional_accuracy"] == pytest.approx(1.0)


class TestRankerComparisonIntegration:
    """The comparison itself, against the real corpus (no labels needed)."""

    def test_all_three_rankers_are_compared_over_the_corpus(self):
        from evaluation import compare_lexical_rankers

        comparison = compare_lexical_rankers(DATASET_PATH, sample_size=3, k=5)
        if not comparison.get("available"):
            pytest.skip("scikit-learn is not installed")

        assert comparison["queries"] == 3
        assert comparison["corpus_size"] > 1000
        assert set(comparison["summary"]) == {"tfidf", "bm25", "hybrid"}
        for metrics in comparison["summary"].values():
            assert metrics["queries"] == 3
            assert 1 <= metrics["mean_rank"] <= comparison["corpus_size"]
            assert 0.0 <= metrics["exact_at_k"] <= 1.0
            # One relevant document per query, so these two must agree.
            assert metrics["map"] == pytest.approx(metrics["mrr"])

    def test_every_ranker_puts_the_correct_row_somewhere(self):
        from evaluation import compare_lexical_rankers

        comparison = compare_lexical_rankers(DATASET_PATH, sample_size=2, k=5)
        if not comparison.get("available"):
            pytest.skip("scikit-learn is not installed")

        for ranks in comparison["per_query"].values():
            assert len(ranks) == 2
            assert all(rank is None or rank >= 1 for rank in ranks)
