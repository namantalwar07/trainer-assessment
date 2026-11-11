from dataclasses import replace

import pytest

from app.scoring import classify, compute_scores
from tests.conftest import make_payload


def test_weighted_score_all_fours(rubric, settings):
    r = compute_scores(make_payload(), rubric, settings)
    assert r.overall_score == 80.0  # 4/5 * 100
    assert r.band == "Good"
    assert r.observed_weight == 100
    assert r.coverage_text == "Score based on 100 of 100 rubric points observed"
    assert not r.review_required


def test_all_fives_and_all_ones(rubric, settings):
    assert compute_scores(make_payload({k: 5 for k in "C01 C02 C03 C04 C05 C06 C07 C08 C09 C10".split()}),
                          rubric, settings).overall_score == 100.0
    ones = compute_scores(make_payload({k: 1 for k in "C01 C02 C03 C04 C05 C06 C07 C08 C09 C10".split()}),
                          rubric, settings)
    assert ones.overall_score == 20.0  # documented floor under SCORE_FLOOR_MODE=accept
    assert ones.band == "Unsatisfactory"


def test_na_weight_is_redistributed_proportionally(rubric, settings):
    # C01 (20) is N/A; everything else is 5 -> still 100/100 on the adjusted 80-point denominator.
    r = compute_scores(make_payload({"C01": None, **{f"C{i:02d}": 5 for i in range(2, 11)}}),
                       rubric, settings, has_reference=False)
    assert r.observed_weight == 80
    assert r.overall_score == 100.0
    c02 = next(c for c in r.competencies if c.criterion_id == "C02")
    assert c02.adjusted_weight == pytest.approx(10 * 100 / 80)
    assert r.coverage_text == "Score based on 80 of 100 rubric points observed"


def test_overall_equals_manual_formula_with_na(rubric, settings):
    ratings = {"C01": None, "C02": 3, "C03": 5, "C04": 2, "C05": 4, "C06": 4, "C07": 3, "C08": 5,
               "C09": 4, "C10": 1}
    w = {"C02": 10, "C03": 15, "C04": 10, "C05": 15, "C06": 10, "C07": 5, "C08": 5, "C09": 5, "C10": 5}
    expected = sum(ratings[k] / 5 * w[k] for k in w) / sum(w.values()) * 100
    r = compute_scores(make_payload(ratings), rubric, settings, has_reference=False)
    assert r.overall_score == round(expected, 1)


@pytest.mark.parametrize("score,band", [
    (100, "Excellent"), (90, "Excellent"), (89.9, "Good"), (89.5, "Good"), (75, "Good"),
    (74.9, "Satisfactory"), (60, "Satisfactory"), (59.9, "Needs improvement"), (40, "Needs improvement"),
    (39.9, "Unsatisfactory"), (20, "Unsatisfactory"),
])
def test_band_thresholds_have_no_gaps(rubric, settings, score, band):
    assert classify(score, rubric, settings) == band


def test_band_round_half_up_rule(rubric, settings):
    s = replace(settings, band_rounding_rule="round_half_up")
    assert classify(89.5, rubric, s) == "Excellent"
    assert classify(89.4, rubric, s) == "Good"


def test_review_not_required_on_clean_session(rubric, settings):
    assert compute_scores(make_payload(), rubric, settings).review_reasons == []


def test_review_on_critical_flag_regardless_of_score(rubric, settings):
    p = make_payload()
    p["critical_flags"] = [{"timestamp": "00:01:00", "description": "Serious misinformation"}]
    r = compute_scores(p, rubric, settings)
    assert r.review_required and "critical flag" in r.review_reasons[0]


def test_review_on_low_coverage(rubric, settings):
    ratings = {"C01": None, "C03": None, "C05": None}  # 50 points N/A -> 50% coverage
    r = compute_scores(make_payload(ratings), rubric, settings, has_reference=False)
    assert r.observed_weight == 50
    assert any("below the 70" in x for x in r.review_reasons)


def test_review_when_c01_na_for_missing_reference(rubric, settings):
    r = compute_scores(make_payload({"C01": None}), rubric, settings, has_reference=False)
    assert any("C01 is N/A" in x for x in r.review_reasons)
    assert r.observed_weight == 80  # >= 70, so only the C01 rule triggers


def test_review_on_low_confidence_only_above_weight_threshold(rubric, settings):
    p = make_payload()
    p["competencies"][6]["confidence"] = "Low"  # C07, weight 5 -> ignored
    assert not compute_scores(p, rubric, settings).review_required
    p["competencies"][0]["confidence"] = "Low"  # C01, weight 20 -> routed
    r = compute_scores(p, rubric, settings)
    assert r.review_required and "C01" in r.review_reasons[0]


def test_review_when_validation_needed_retries(rubric, settings):
    r = compute_scores(make_payload(), rubric, settings, validation_retries=2)
    assert r.review_required and "2 validation retries" in r.review_reasons[0]


def test_model_supplied_totals_and_flags_are_ignored(rubric, settings):
    p = make_payload()
    p.update({"overall_score": 99, "performance_band": "Excellent", "human_review_required": False})
    r = compute_scores(p, rubric, settings)
    assert r.overall_score == 80.0 and r.band == "Good"


def test_all_na_is_not_scorable(rubric, settings):
    r = compute_scores(make_payload({f"C{i:02d}": None for i in range(1, 11)}), rubric, settings)
    assert r.overall_score is None and r.band == "Not scorable" and r.review_required


def test_zero_anchor_rating_scores_zero(rubric, settings):
    s = replace(settings, score_floor_mode="zero_anchor")
    r = compute_scores(make_payload({f"C{i:02d}": 0 for i in range(1, 11)}), rubric, s)
    assert r.overall_score == 0.0 and r.band == "Unsatisfactory"
