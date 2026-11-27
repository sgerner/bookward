import json
import math
import time

import pytest

from afterword_engine import evaluation
import random
import subprocess
import sys
from pathlib import Path

from afterword_engine.evaluation import (
    build_report,
    cluster_bootstrap,
    ips_auc,
    ips_grade_concordance,
    ips_ndcg_at_k,
    ips_precision_at_k,
    load_rows,
    promotion_gate,
    signal_diagnostics,
    temporal_split,
)
from afterword_engine.exploration import epsilon_tail_explore
from afterword_engine.config import settings
from afterword_engine.database import initialize
from afterword_engine.main import recommendation_list, tracked_recommendations


def _rows():
    values = []
    for run_number, day in enumerate((1, 2, 3, 4, 5), 1):
        for rank, (candidate_id, label, score, signal) in enumerate(
            ((run_number * 10, 1, 1.0, "rating_positive"), (run_number * 10 + 1, 0, 0.0, "rating_negative")),
            1,
        ):
            values.append({
                "run_id": f"run-{run_number}",
                "run_created_at": f"2026-01-0{day}T00:00:00+00:00",
                "presented_at": f"2026-01-0{day}T00:00:00+00:00",
                "candidate_id": candidate_id,
                "rank": rank,
                "score": score,
                "propensity": 0.5,
                "label": label,
                "confidence": 1.0,
                "label_kind": signal,
            })
    return values


def _pairwise_ips_auc_reference(
    rows,
    *,
    score_key: str = "score",
    positive_threshold: float = 0.5,
) -> float | None:
    usable = evaluation._weighted_rows(rows, score_key=score_key)
    positives = [(score, weight) for _, score, label, weight in usable if label > positive_threshold]
    negatives = [(score, weight) for _, score, label, weight in usable if label < positive_threshold]
    denominator = sum(left_weight * right_weight for _, left_weight in positives for _, right_weight in negatives)
    if not positives or not negatives or denominator <= 0:
        return None
    numerator = 0.0
    for positive, positive_weight in positives:
        for negative, negative_weight in negatives:
            pair_weight = positive_weight * negative_weight
            if positive > negative:
                numerator += pair_weight
            elif positive == negative:
                numerator += pair_weight * 0.5
    return numerator / denominator


def test_ips_metrics_weight_propensity_and_ignore_censored_rows():
    rows = _rows()[:2] + [{"run_id": "censored", "rank": 1, "score": 100, "propensity": 0.01, "label": None}]
    assert ips_auc(rows) == 1
    assert ips_precision_at_k(rows, 20) == 0.5
    assert ips_ndcg_at_k(rows, 20) == 1


def test_ips_auc_matches_pairwise_reference_across_ties_zero_weights_and_neutral_labels():
    # Empty, single-class, neutral-only, and zero-denominator cases return None
    assert ips_auc([]) is None
    assert ips_auc([{"score": 1.0, "label": 1.0}]) is None
    assert ips_auc([{"score": 0.0, "label": 0.0}]) is None
    assert ips_auc([{"score": 1.0, "label": 0.5}, {"score": 0.0, "label": 0.5}]) is None
    assert ips_auc([
        {"score": 1.0, "label": 1.0, "confidence": 0.0},
        {"score": 0.0, "label": 0.0, "confidence": 1.0},
    ]) is None

    # Signed-zero ties (-0.0 and +0.0) and input-order invariance
    signed_zero_rows = [
        {"score": -0.0, "label": 1.0, "propensity": 0.4, "confidence": 0.8},
        {"score": 0.0, "label": 0.0, "propensity": 0.5, "confidence": 1.0},
        {"score": -1.0, "label": 0.0, "propensity": 0.25, "confidence": 0.5},
        {"score": 0.0, "label": 0.5, "propensity": 0.5, "confidence": 1.0},
    ]
    ref_zero = _pairwise_ips_auc_reference(signed_zero_rows)
    assert ref_zero is not None
    assert abs(ips_auc(signed_zero_rows) - ref_zero) <= 1e-12
    assert abs(ips_auc(list(reversed(signed_zero_rows))) - ref_zero) <= 1e-12

    # Mixed ties, zero-confidence rows alongside positive weights, neutral labels, and censored rows
    rng = random.Random(20261121)
    mixed_rows = []
    for index in range(300):
        mixed_rows.append({
            "run_id": f"run-{index // 15}",
            "rank": (index % 15) + 1,
            "score": rng.choice([-0.0, 0.0, 0.25, 0.5, 0.75, 1.0]),
            "alt_score": round(rng.uniform(-2.0, 2.0), 1),
            "propensity": rng.choice([0.1, 0.25, 0.5, 1.0]),
            "confidence": rng.choice([0.0, 0.25, 0.5, 1.0]),
            "label": rng.choice([None, 0.0, 0.2, 0.5, 0.7, 1.0]),
        })
    ref_default = _pairwise_ips_auc_reference(mixed_rows)
    actual_default = ips_auc((row for row in mixed_rows))
    assert ref_default is not None and actual_default is not None
    assert abs(actual_default - ref_default) <= 1e-12

    ref_custom = _pairwise_ips_auc_reference(mixed_rows, score_key="alt_score", positive_threshold=0.7)
    actual_custom = ips_auc(mixed_rows, score_key="alt_score", positive_threshold=0.7)
    assert ref_custom is not None and actual_custom is not None
    assert abs(actual_custom - ref_custom) <= 1e-12

    # Verify cluster_bootstrap matches reference pairwise bootstrap within 1e-12
    boot_actual = cluster_bootstrap(mixed_rows, iterations=40, seed=99)
    orig_ips_auc = evaluation.ips_auc
    try:
        evaluation.ips_auc = _pairwise_ips_auc_reference
        boot_ref = cluster_bootstrap(mixed_rows, iterations=40, seed=99)
    finally:
        evaluation.ips_auc = orig_ips_auc
    assert boot_actual["iterations"] == boot_ref["iterations"]
    assert boot_actual["clusters"] == boot_ref["clusters"]
    assert abs(boot_actual["estimate"] - boot_ref["estimate"]) <= 1e-12
    assert abs(boot_actual["lower"] - boot_ref["lower"]) <= 1e-12
    assert abs(boot_actual["upper"] - boot_ref["upper"]) <= 1e-12


def test_ips_auc_sorted_groups_scales_sublinearly_vs_quadratic_pairs():
    rows_20k = [
        {
            "run_id": f"run-{index // 20}",
            "rank": (index % 20) + 1,
            "score": float((index * 37) % 500),
            "propensity": 0.5,
            "confidence": 1.0,
            "label": 1.0 if index % 2 == 0 else 0.0,
        }
        for index in range(20_000)
    ]
    started = time.perf_counter()
    auc_value = ips_auc(rows_20k)
    elapsed = time.perf_counter() - started
    assert auc_value is not None and 0.0 <= auc_value <= 1.0
    # 20,000 binary rows represent 100M pairs (~15-20s with nested O(P * N) loops, <0.1s with sorted groups)
    assert elapsed < 1.0


def test_cluster_bootstrap_is_run_clustered_and_seeded():
    first = cluster_bootstrap(_rows(), iterations=50, seed=7)
    second = cluster_bootstrap(_rows(), iterations=50, seed=7)
    assert first == second
    assert first["clusters"] == 5
    assert first["estimate"] == 1


def test_temporal_split_keeps_each_run_whole():
    splits = temporal_split(_rows())
    assert {row["run_id"] for row in splits["train"]} == {"run-1", "run-2", "run-3"}
    assert {row["run_id"] for row in splits["validation"]} == {"run-4"}
    assert {row["run_id"] for row in splits["test"]} == {"run-5"}


def test_signal_diagnostics_and_promotion_gate_have_sample_guards():
    diagnostics = signal_diagnostics(_rows())
    assert diagnostics["rating_positive"]["labeled_impressions"] == 5
    gate = promotion_gate(
        {"auc": 0.7, "precision_at_20": 0.6, "ndcg_at_20": 0.8},
        {"auc": 0.6, "precision_at_20": 0.6, "ndcg_at_20": 0.7},
        sample={"runs": 30, "labeled_impressions": 300, "positive_impressions": 30, "negative_impressions": 30},
    )
    assert gate["eligible"] is True
    assert promotion_gate(
        {"auc": 0.7, "precision_at_20": 0.6, "ndcg_at_20": 0.8},
        {"auc": 0.6, "precision_at_20": 0.6, "ndcg_at_20": 0.7},
        sample={"runs": 1, "labeled_impressions": 2, "positive_impressions": 1, "negative_impressions": 1},
    )["eligible"] is False


def test_tail_exploration_keeps_top_four_and_logs_marginal_propensity():
    recommendations = [{"id": number, "score": 100 - number} for number in range(1, 9)]
    disabled = epsilon_tail_explore(recommendations, epsilon=0)
    assert [item["id"] for item in disabled] == list(range(1, 9))
    assert all(item["propensity"] == 1 for item in disabled)

    explored = epsilon_tail_explore(recommendations, epsilon=0.5, rng=random.Random(2))
    assert [item["id"] for item in explored[:4]] == [1, 2, 3, 4]
    assert all(item["propensity"] == 1 for item in explored[:4])
    assert sorted(item["id"] for item in explored[4:]) == [5, 6, 7, 8]
    assert all(0 < item["propensity"] <= 1 for item in explored[4:])


def test_disabled_exploration_preserves_tracked_order(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "db", str(tmp_path / "disabled.db"))
    monkeypatch.setattr(settings, "exploration_enabled", False)
    initialize()
    expected = [item["id"] for item in recommendation_list(limit=8)]
    actual = [item["id"] for item in tracked_recommendations(limit=8)[0]]
    assert actual == expected


def test_report_loads_json_join_and_cli_emits_json(tmp_path: Path):
    corpus = {
        "recommendation_runs": [{"id": "run-1", "created_at": "2026-01-01T00:00:00Z"}],
        "recommendation_impressions": [
            {"id": 1, "run_id": "run-1", "candidate_id": 1, "rank": 1, "score": 2, "propensity": 1, "presented_at": "2026-01-01T00:00:00Z"},
            {"id": 2, "run_id": "run-1", "candidate_id": 2, "rank": 2, "score": 1, "propensity": 1, "presented_at": "2026-01-01T00:00:00Z"},
        ],
        "recommendation_outcomes": [
            {"impression_id": 1, "label": 1, "confidence": 1, "label_kind": "save", "attributed_at": "2026-01-02T00:00:00Z"},
            {"impression_id": 2, "label": 0, "confidence": 1, "label_kind": "reject", "attributed_at": "2026-01-02T00:00:00Z"},
        ],
    }
    path = tmp_path / "export.json"
    path.write_text(json.dumps(corpus))
    rows = load_rows(path)
    report = build_report(rows, bootstrap_iterations=3, minimums={"runs": 0, "labeled_impressions": 0, "positive_impressions": 0, "negative_impressions": 0})
    assert report["protocol"].startswith("temporal run split")
    assert set(report["splits"]["test"]) == {"champion"}
    result = subprocess.run(
        [sys.executable, "engine/scripts/evaluate_recommendations.py", str(path), "--bootstrap-iterations", "1", "--minimum-runs", "0", "--minimum-labeled-impressions", "0", "--minimum-positive-impressions", "0", "--minimum-negative-impressions", "0"],
        check=True,
        capture_output=True,
        text=True,
        cwd=Path(__file__).parents[2],
    )
    assert json.loads(result.stdout)["protocol"].startswith("temporal run split")


def row(run, rank, label, *, candidate_id=None, impression_id=None):
    return {
        "run_id": run,
        "rank": rank,
        "candidate_id": candidate_id if candidate_id is not None else f"{run}-{rank}",
        "impression_id": impression_id if impression_id is not None else f"{run}-{rank}",
        "score": float(100 - rank),
        "propensity": 1.0,
        "confidence": 1.0,
        "label": label,
    }


def test_top_k_metrics_are_macro_by_run_and_keep_logged_slots():
    rows = [row("a", 1, 1), row("b", 1, 0)]
    assert evaluation.ips_precision_at_k(rows, 1) == pytest.approx(0.5)

    rows = [row("one", 1, None), row("one", 2, 1)]
    expected = 1 / math.log2(3)
    assert evaluation.ips_precision_at_k(rows, 2) == 1
    assert evaluation.ips_ndcg_at_k(rows, 2) == pytest.approx(expected)


def test_rank_cutoff_uses_actual_positive_integer_rank():
    assert evaluation.ips_precision_at_k([row("late", 25, 1)], 20) is None
    assert evaluation.ips_ndcg_at_k([row("late", 20, 1)], 20) == pytest.approx(1 / math.log2(21))
    assert evaluation.ips_ndcg_at_k([row("late", 25, 1)], 20) is None
    assert evaluation.ips_precision_at_k([row("bad", 1.5, 1)], 20) is None
    assert evaluation.ips_precision_at_k([row("bad", 0, 1)], 20) is None
    assert evaluation.ips_precision_at_k([row("bad", True, 1)], 20) is None


def test_input_order_does_not_change_metrics_and_duplicates_do_not_add_evidence():
    values = [row("a", 1, 1), row("a", 2, 0), row("b", 1, 0), row("b", 2, 1)]
    assert evaluation.ips_ndcg_at_k(values, 2) == evaluation.ips_ndcg_at_k(list(reversed(values)), 2)
    duplicate = values + [dict(values[0])]
    assert evaluation.ips_precision_at_k(duplicate, 2) == evaluation.ips_precision_at_k(values, 2)


def test_conflicting_duplicate_and_duplicate_rank_exports_are_rejected():
    conflicting = [row("a", 1, 1, candidate_id="c", impression_id="i"), row("a", 1, 0, candidate_id="c", impression_id="i")]
    with pytest.raises(ValueError, match="Conflicting duplicate"):
        evaluation.ips_precision_at_k(conflicting, 1)
    same_impression = [row("a", 1, 1, candidate_id="c1", impression_id="i"), row("a", 1, 1, candidate_id="c2", impression_id="i")]
    assert evaluation.ips_precision_at_k(same_impression, 1) == 1
    duplicate_rank = [row("a", 1, 1, candidate_id="c1", impression_id="i1"), row("a", 1, 1, candidate_id="c2", impression_id="i2")]
    with pytest.raises(ValueError, match="Duplicate logged rank"):
        evaluation.ips_ndcg_at_k(duplicate_rank, 2)


def test_bootstrap_remaps_repeated_run_draws():
    values = [row("positive", 1, 1), row("negative", 1, 0)]
    result = evaluation.cluster_bootstrap(values, metric="precision_at_20", iterations=200, seed=7)
    assert result["estimate"] == pytest.approx(0.5)
    assert result["lower"] == 0
    assert result["upper"] == 1


def test_enjoyment_report_excludes_unrated_completions_and_intent():
    rows = _rows() + [
        {"run_id": "intent", "rank": 1, "score": 100, "label": 1, "label_kind": "save"},
        {"run_id": "unrated", "rank": 1, "score": 100, "label": .75, "label_kind": "completed_unrated"},
    ]
    report = build_report(rows, bootstrap_iterations=0)
    assert report["enjoyment"]["labeled_impressions"] == 10
    assert report["sample"]["labeled_impressions"] == 12


def test_load_rows_preserves_earlier_intent_alongside_rated_outcome():
    outcomes = [
        {"impression_id": 1, "label": 1, "label_kind": "save", "confidence": .5},
        {"impression_id": 1, "label": 0, "label_kind": "rating_negative", "confidence": 1},
    ]
    rows = load_rows({"recommendation_impressions": [{"id": 1, "run_id": "r", "rank": 1, "score": 90}],
                      "recommendation_outcomes": outcomes})
    assert rows[0]["label_kind"] == "rating_negative"
    assert rows[0]["observed_outcomes"] == outcomes
    diagnostics = signal_diagnostics(rows)
    assert diagnostics["save"]["labeled_impressions"] == 1
    assert diagnostics["rating_negative"]["labeled_impressions"] == 1


def test_load_rows_prioritizes_neutral_ratings_and_completions_over_explicit_feedback():
    corpus = {
        "recommendation_runs": [{"id": "run-1", "created_at": "2026-01-01T00:00:00Z"}],
        "recommendation_impressions": [
            {"id": 1, "run_id": "run-1", "candidate_id": 101, "rank": 1, "score": 95.0, "propensity": 1.0},
            {"id": 2, "run_id": "run-1", "candidate_id": 102, "rank": 2, "score": 90.0, "propensity": 1.0},
            {"id": 3, "run_id": "run-1", "candidate_id": 103, "rank": 3, "score": 85.0, "propensity": 1.0},
        ],
        "recommendation_outcomes": [
            {
                "impression_id": 1,
                "label": 1.0,
                "confidence": 0.9,
                "label_kind": "explicit_feedback",
                "attributed_at": "2026-01-02T00:00:00Z",
            },
            {
                "impression_id": 1,
                "label": 0.5,
                "confidence": 0.25,
                "label_kind": "rating_neutral",
                "attributed_at": "2026-01-05T00:00:00Z",
            },
            {
                "impression_id": 2,
                "label": 1.0,
                "confidence": 0.8,
                "label_kind": "explicit_feedback",
                "attributed_at": "2026-01-02T00:00:00Z",
            },
            {
                "impression_id": 2,
                "label": 0.75,
                "confidence": 0.4,
                "label_kind": "completed_unrated",
                "attributed_at": "2026-01-04T00:00:00Z",
            },
        ],
    }
    rows = load_rows(corpus)
    assert rows[0]["label_kind"] == "rating_neutral"
    assert rows[0]["label"] == 0.5
    assert rows[0]["attributed_at"] == "2026-01-05T00:00:00Z"
    assert rows[1]["label_kind"] == "completed_unrated"
    assert rows[1]["label"] == 0.75
    assert rows[1]["attributed_at"] == "2026-01-04T00:00:00Z"
    assert rows[2]["label"] is None
    assert rows[2]["attributed_at"] is None


def test_build_report_retains_shortlisted_neutral_reads_in_enjoyment_at_full_weight():
    corpus = {
        "recommendation_runs": [{"id": "run-1", "created_at": "2026-01-01T00:00:00Z"}],
        "recommendation_impressions": [
            {"id": 1, "run_id": "run-1", "candidate_id": 1, "rank": 1, "score": 99.0, "propensity": 1.0},
            {"id": 2, "run_id": "run-1", "candidate_id": 2, "rank": 2, "score": 90.0, "propensity": 1.0},
            {"id": 3, "run_id": "run-1", "candidate_id": 3, "rank": 3, "score": 80.0, "propensity": 1.0},
            {"id": 4, "run_id": "run-1", "candidate_id": 4, "rank": 4, "score": 70.0, "propensity": 1.0},
        ],
        "recommendation_outcomes": [
            {"impression_id": 1, "label": 1.0, "confidence": 0.8, "label_kind": "explicit_feedback", "attributed_at": "2026-01-01T01:00:00Z"},
            {"impression_id": 1, "label": 1.0, "confidence": 1.0, "label_kind": "rating_positive", "attributed_at": "2026-01-03T00:00:00Z"},
            {"impression_id": 2, "label": 1.0, "confidence": 0.9, "label_kind": "explicit_feedback", "attributed_at": "2026-01-01T02:00:00Z"},
            {"impression_id": 2, "label": 0.5, "confidence": 0.25, "label_kind": "rating_neutral", "attributed_at": "2026-01-04T00:00:00Z"},
            {"impression_id": 3, "label": 1.0, "confidence": 0.8, "label_kind": "explicit_feedback", "attributed_at": "2026-01-01T03:00:00Z"},
            {"impression_id": 4, "label": 0.75, "confidence": 0.4, "label_kind": "completed_unrated", "attributed_at": "2026-01-04T00:00:00Z"},
        ],
    }
    rows = load_rows(corpus)
    # Even if a caller supplies an overwritten top-level label, enjoyment projects from observed_outcomes.
    overwritten = [
        {**rows[0]},
        {**rows[1], "label": 1.0, "confidence": 0.9, "label_kind": "explicit_feedback"},
        {**rows[2]},
        {**rows[3]},
    ]
    report = build_report(overwritten, bootstrap_iterations=0)
    enjoyment = report["enjoyment"]
    assert enjoyment["impressions"] == 2
    assert enjoyment["labeled_impressions"] == 2
    assert enjoyment["positive_impressions"] == 1
    assert enjoyment["neutral_impressions"] == 1
    assert enjoyment["negative_impressions"] == 0
    # Binary AUC excludes neutral 0.5 ties, so with no < 0.5 negatives it is None;
    # multi-grade concordance reports 5★ > 3★ under its separate key.
    assert enjoyment["auc"] is None
    assert enjoyment["grade_concordance"] == pytest.approx(1.0)
    # Full certainty (1.0) on rating_neutral in enjoyment means 1 positive + 1 neutral => precision 0.5, not 1 / 1.25 = 0.8.
    assert enjoyment["precision_at_20"] == pytest.approx(0.5)


def test_ips_auc_honors_positive_threshold_and_raw_confidence_against_hand_computed_values():
    # Independent hand-computed fixture (does not call _weighted_rows):
    # row A: score=10, label=1.0,  conf=1.0,  prop=0.5  -> weight = 1.0 / 0.5  = 2.0
    # row B: score=30, label=0.75, conf=0.5,  prop=1.0  -> weight = 0.5 / 1.0  = 0.5
    # row C: score=20, label=0.5,  conf=0.25, prop=0.5  -> weight = 0.25 / 0.5 = 0.5 (label_kind=None: NOT overridden to 1.0!)
    # row D: score=15, label=0.2,  conf=0.4,  prop=0.05 -> weight = 0.4 / 0.2  = 2.0 (propensity clipped to 0.2)
    rows = [
        {"score": 10.0, "label": 1.0, "confidence": 1.0, "propensity": 0.5},
        {"score": 30.0, "label": 0.75, "confidence": 0.5, "propensity": 1.0},
        {"score": 20.0, "label": 0.5, "confidence": 0.25, "propensity": 0.5},
        {"score": 15.0, "label": 0.2, "confidence": 0.4, "propensity": 0.05},
    ]

    # At positive_threshold=0.5:
    # - row C (label == 0.5) is excluded as a threshold tie.
    # - positives: A (score=10, w=2.0), B (score=30, w=0.5); total_pos = 2.5
    # - negatives: D (score=15, w=2.0); total_neg = 2.0; denominator = 5.0
    # - pairs: (A vs D: 10 < 15 -> 0) + (B vs D: 30 > 15 -> 0.5 * 2.0 = 1.0) => AUC = 1.0 / 5.0 = 0.20
    assert ips_auc(rows, positive_threshold=0.5) == pytest.approx(0.20, abs=1e-12)

    # At positive_threshold=0.75:
    # - row B (label == 0.75) is excluded as a threshold tie.
    # - positives: A (score=10, w=2.0); total_pos = 2.0
    # - negatives: C (score=20, w=0.5), D (score=15, w=2.0); total_neg = 2.5; denominator = 5.0
    #   (if C's confidence had been forced to 1.0, C's weight would be 2.0 and denominator would be 8.0)
    # - both negatives outscore A (20 > 10, 15 > 10) => AUC = 0.0
    assert ips_auc(rows, positive_threshold=0.75) == pytest.approx(0.0, abs=1e-12)

    # Raising A's score to 18.0 at positive_threshold=0.75:
    # - A beats D (18 > 15 -> 2.0 * 2.0 = 4.0) and loses to C (18 < 20 -> 0)
    # - Hand-computed AUC = 4.0 / (2.0 * (0.5 + 2.0)) = 4.0 / 5.0 = 0.80
    #   (would be 4.0 / (2.0 * (2.0 + 2.0)) = 0.50 if label=0.5 forced confidence=1.0)
    raised = [{**rows[0], "score": 18.0}, *rows[1:]]
    assert ips_auc(raised, positive_threshold=0.75) == pytest.approx(0.80, abs=1e-12)


def test_ips_grade_concordance_evaluates_distinct_grades_without_changing_binary_auc():
    five_over_three = [
        {"run_id": "r1", "rank": 1, "score": 95.0, "label": 1.0, "confidence": 1.0, "label_kind": "rating_positive"},
        {"run_id": "r1", "rank": 2, "score": 80.0, "label": 0.5, "confidence": 0.25, "label_kind": "rating_neutral"},
    ]
    assert ips_auc(five_over_three) is None
    assert ips_grade_concordance(five_over_three) == pytest.approx(1.0)

    three_over_one = [
        {"run_id": "r1", "rank": 1, "score": 80.0, "label": 0.5, "confidence": 0.25, "label_kind": "rating_neutral"},
        {"run_id": "r1", "rank": 2, "score": 60.0, "label": 0.0, "confidence": 1.0, "label_kind": "rating_negative"},
    ]
    assert ips_auc(three_over_one) is None
    assert ips_grade_concordance(three_over_one) == pytest.approx(1.0)

    three_grades_partial_inversion = [
        {"run_id": "r1", "rank": 1, "score": 80.0, "label": 1.0, "confidence": 1.0, "label_kind": "rating_positive"},
        {"run_id": "r1", "rank": 2, "score": 90.0, "label": 0.5, "confidence": 0.25, "label_kind": "rating_neutral"},
        {"run_id": "r1", "rank": 3, "score": 60.0, "label": 0.0, "confidence": 1.0, "label_kind": "rating_negative"},
    ]
    # Binary AUC compares only 1.0 (score 80) vs 0.0 (score 60) => 1.0.
    assert ips_auc(three_grades_partial_inversion) == pytest.approx(1.0)
    # Raw weights: w(1.0)=1.0, w(0.5)=0.25, w(0.0)=1.0.
    # Pairs: (1.0 vs 0.5: 80 < 90, w=0.25 -> 0), (1.0 vs 0.0: 80 > 60, w=1.0 -> 1.0), (0.5 vs 0.0: 90 > 60, w=0.25 -> 0.25)
    # Concordance = (1.0 + 0.25) / (0.25 + 1.0 + 0.25) = 1.25 / 1.50 = 5 / 6.
    assert ips_grade_concordance(three_grades_partial_inversion) == pytest.approx(5 / 6)
    # With full_rating_certainty=True (all ratings weight 1.0): 2 / 3.
    assert ips_grade_concordance(three_grades_partial_inversion, full_rating_certainty=True) == pytest.approx(2 / 3)

    single_grade_only = [
        {"run_id": "r1", "rank": 1, "score": 90.0, "label": 0.5, "confidence": 0.25, "label_kind": "rating_neutral"},
        {"run_id": "r1", "rank": 2, "score": 70.0, "label": 0.5, "confidence": 0.25, "label_kind": "rating_neutral"},
    ]
    assert ips_grade_concordance(single_grade_only) is None


def test_scoped_full_rating_certainty_and_zero_neutral_ndcg_gain():
    one_five_above_four_threes = [
        {"run_id": "r1", "rank": 1, "score": 99.0, "propensity": 1.0, "label": 1.0, "confidence": 1.0, "label_kind": "rating_positive"},
        *[
            {
                "run_id": "r1",
                "rank": rank,
                "score": float(100 - rank),
                "propensity": 1.0,
                "label": 0.5,
                "confidence": 0.25,
                "label_kind": "rating_neutral",
            }
            for rank in range(2, 6)
        ],
    ]
    # Default (full_rating_certainty=False) preserves raw confidence=0.25: 1.0 / (1.0 + 4 * 0.25) = 0.50.
    assert ips_precision_at_k(one_five_above_four_threes, 5) == pytest.approx(0.50)
    # Scoped enjoyment certainty (full_rating_certainty=True) weights all explicit ratings at 1.0: 1.0 / 5.0 = 0.20.
    assert ips_precision_at_k(one_five_above_four_threes, 5, full_rating_certainty=True) == pytest.approx(0.20)
    # Neutral rows without a rating label_kind never have confidence overridden even when full_rating_certainty=True.
    untyped_neutrals = [
        {k: v for k, v in item.items() if k != "label_kind"}
        for item in one_five_above_four_threes
    ]
    assert ips_precision_at_k(untyped_neutrals, 5, full_rating_certainty=True) == pytest.approx(0.50)
    assert ips_ndcg_at_k(one_five_above_four_threes, 5) == pytest.approx(1.0)

    neutral_above_five = [
        {"run_id": "r2", "rank": 1, "score": 95.0, "propensity": 1.0, "label": 0.5, "confidence": 0.25, "label_kind": "rating_neutral"},
        {"run_id": "r2", "rank": 2, "score": 90.0, "propensity": 1.0, "label": 1.0, "confidence": 1.0, "label_kind": "rating_positive"},
    ]
    assert ips_ndcg_at_k(neutral_above_five, 5) == pytest.approx(1 / math.log2(3))

    no_positive_outcomes = [
        {"run_id": "r3", "rank": 1, "score": 95.0, "propensity": 1.0, "label": 0.5, "confidence": 0.25, "label_kind": "rating_neutral"},
        {"run_id": "r3", "rank": 2, "score": 85.0, "propensity": 1.0, "label": 0.0, "confidence": 1.0, "label_kind": "rating_negative"},
    ]
    assert ips_ndcg_at_k(no_positive_outcomes, 5) is None


def test_ips_ndcg_at_k_matches_hand_computed_gain_only_ideal_without_clamping():
    # Two relevant items at ranks 2 and 3 (rank 1 censored) with skewed propensities:
    # DCG  = 1.0 / log2(3) + 1.0 / log2(4)
    # IDCG = 1.0 / log2(2) + 1.0 / log2(3)  (from gains alone, independent of propensities)
    two_relevant_ranks_2_and_3 = [
        {"run_id": "r1", "rank": 1, "score": 99.0, "propensity": 1.0, "confidence": 1.0, "label": None},
        {"run_id": "r1", "rank": 2, "score": 95.0, "propensity": 0.2, "confidence": 0.9, "label": 1.0},
        {"run_id": "r1", "rank": 3, "score": 90.0, "propensity": 1.0, "confidence": 0.5, "label": 1.0},
    ]
    expected_ranks_2_3 = (1.0 / math.log2(3) + 1.0 / math.log2(4)) / (1.0 / math.log2(2) + 1.0 / math.log2(3))
    assert ips_ndcg_at_k(two_relevant_ranks_2_and_3, 5) == pytest.approx(expected_ranks_2_3, abs=1e-12)

    # Swapping propensities leaves NDCG unchanged because IDCG and DCG depend symmetrically on gains alone.
    swapped_propensity = [
        two_relevant_ranks_2_and_3[0],
        {**two_relevant_ranks_2_and_3[1], "propensity": 1.0, "confidence": 0.5},
        {**two_relevant_ranks_2_and_3[2], "propensity": 0.2, "confidence": 0.9},
    ]
    assert ips_ndcg_at_k(swapped_propensity, 5) == pytest.approx(expected_ranks_2_3, abs=1e-12)

    # Suboptimal order where rank 1 has lower gain (0.75) and much larger IPS weight than rank 2 (gain 1.0):
    # Previously, attaching item weights to sorted gains made dcg > idcg (~1.40) and relied on clamping to 1.0.
    # Hand-computed: DCG = 0.75 / log2(2) + 1.0 / log2(3); IDCG = 1.0 / log2(2) + 0.75 / log2(3) => ~0.937369 < 1.0.
    suboptimal_weighted = [
        {"run_id": "r2", "rank": 1, "score": 95.0, "propensity": 0.2, "confidence": 1.0, "label": 0.75},
        {"run_id": "r2", "rank": 2, "score": 90.0, "propensity": 1.0, "confidence": 0.2, "label": 1.0},
    ]
    expected_suboptimal = (0.75 / math.log2(2) + 1.0 / math.log2(3)) / (1.0 / math.log2(2) + 0.75 / math.log2(3))
    assert expected_suboptimal < 1.0
    assert ips_ndcg_at_k(suboptimal_weighted, 5) == pytest.approx(expected_suboptimal, abs=1e-12)


def test_signal_diagnostics_reports_multiclass_facets_and_per_kind_counts():
    corpus = {
        "recommendation_runs": [{"id": "run-1", "created_at": "2026-01-01T00:00:00Z"}],
        "recommendation_impressions": [
            {"id": 1, "run_id": "run-1", "candidate_id": 1, "rank": 1, "score": 95.0, "propensity": 1.0},
            {"id": 2, "run_id": "run-1", "candidate_id": 2, "rank": 2, "score": 85.0, "propensity": 1.0},
            {"id": 3, "run_id": "run-1", "candidate_id": 3, "rank": 3, "score": 75.0, "propensity": 1.0},
            {"id": 4, "run_id": "run-1", "candidate_id": 4, "rank": 4, "score": 65.0, "propensity": 1.0},
        ],
        "recommendation_outcomes": [
            {"impression_id": 1, "label": 1.0, "confidence": 0.8, "label_kind": "save", "attributed_at": "2026-01-01T01:00:00Z"},
            {"impression_id": 1, "label": 1.0, "confidence": 1.0, "label_kind": "rating_positive", "attributed_at": "2026-01-03T00:00:00Z"},
            {"impression_id": 2, "label": 1.0, "confidence": 0.8, "label_kind": "explicit_feedback", "attributed_at": "2026-01-01T02:00:00Z"},
            {"impression_id": 2, "label": 0.5, "confidence": 0.25, "label_kind": "rating_neutral", "attributed_at": "2026-01-04T00:00:00Z"},
            {"impression_id": 3, "label": 0.0, "confidence": 0.8, "label_kind": "reject", "attributed_at": "2026-01-01T03:00:00Z"},
            {"impression_id": 3, "label": 0.0, "confidence": 1.0, "label_kind": "rating_negative", "attributed_at": "2026-01-05T00:00:00Z"},
            {"impression_id": 4, "label": 0.75, "confidence": 0.4, "label_kind": "completed_unrated", "attributed_at": "2026-01-06T00:00:00Z"},
        ],
    }
    diagnostics = signal_diagnostics(load_rows(corpus))
    assert diagnostics["shortlist_intent"]["labeled_impressions"] == 3
    assert diagnostics["shortlist_intent"]["auc"] == pytest.approx(1.0)
    assert diagnostics["explicit_ratings"]["labeled_impressions"] == 3
    assert diagnostics["explicit_ratings"]["auc"] == pytest.approx(1.0)
    assert diagnostics["completion"]["labeled_impressions"] == 1
    assert diagnostics["completion"]["auc"] is None
    assert diagnostics["save"]["labeled_impressions"] == 1
    assert diagnostics["explicit_feedback"]["labeled_impressions"] == 1
    assert diagnostics["reject"]["labeled_impressions"] == 1
    assert diagnostics["rating_positive"]["labeled_impressions"] == 1
    assert diagnostics["rating_neutral"]["labeled_impressions"] == 1
    assert diagnostics["rating_negative"]["labeled_impressions"] == 1
    assert diagnostics["completed_unrated"]["labeled_impressions"] == 1


def test_clipped_snips_bounds_explored_slates_and_excludes_out_of_window_idcg():
    # Experiment E3 configuration: 24-item slate, stable_top_k=8, epsilon=0.15 -> moved propensity = 0.15 / 16 = 0.009375.
    base_slate = [{"id": idx, "score": float(100 - idx)} for idx in range(1, 25)]
    explored = epsilon_tail_explore(base_slate, epsilon=0.15, stable_top_k=8, rng=random.Random(1))
    assert any(item["propensity"] == pytest.approx(0.15 / 16) for item in explored[8:])

    all_five_stars = [
        {
            "run_id": "all-five",
            "rank": rank,
            "candidate_id": item["id"],
            "impression_id": rank,
            "score": item["score"],
            "propensity": item["propensity"],
            "confidence": 1.0,
            "label": 1.0,
            "label_kind": "rating_positive",
        }
        for rank, item in enumerate(explored, 1)
    ]
    assert ips_ndcg_at_k(all_five_stars, 20) == pytest.approx(1.0)
    assert ips_precision_at_k(all_five_stars, 20) == pytest.approx(1.0)

    # Out-of-window (rank > k) moved tail positives must not enter idcg and collapse top-k NDCG.
    out_of_window = [
        {
            "run_id": "window-check",
            "rank": 1,
            "candidate_id": 1,
            "impression_id": 1,
            "score": 99.0,
            "propensity": 1.0,
            "confidence": 1.0,
            "label": 1.0,
            "label_kind": "rating_positive",
        },
        *[
            {
                "run_id": "window-check",
                "rank": rank,
                "candidate_id": rank,
                "impression_id": rank,
                "score": float(100 - rank),
                "propensity": 0.15 / 16,
                "confidence": 1.0,
                "label": 1.0,
                "label_kind": "rating_positive",
            }
            for rank in range(21, 25)
        ],
    ]
    assert ips_ndcg_at_k(out_of_window, 20) == pytest.approx(1.0)

    # One shuffled tail slot (propensity 0.009375) must not overpower eight top-slot outcomes.
    eight_top_vs_one_tail = [
        *[
            {
                "run_id": "top-vs-tail",
                "rank": rank,
                "candidate_id": rank,
                "impression_id": rank,
                "score": float(100 - rank),
                "propensity": 1.0,
                "confidence": 1.0,
                "label": 1.0,
                "label_kind": "rating_positive",
            }
            for rank in range(1, 9)
        ],
        {
            "run_id": "top-vs-tail",
            "rank": 9,
            "candidate_id": 24,
            "impression_id": 9,
            "score": 76.0,
            "propensity": 0.15 / 16,
            "confidence": 1.0,
            "label": 0.0,
            "label_kind": "rating_negative",
        },
    ]
    precision = ips_precision_at_k(eight_top_vs_one_tail, 20)
    ndcg = ips_ndcg_at_k(eight_top_vs_one_tail, 20)
    auc = ips_auc(eight_top_vs_one_tail)
    # Clipped weight on tail slot is 1 / 0.2 = 5.0 vs eight 1.0 top slots => 8 / (8 + 5) = 8 / 13.
    assert precision == pytest.approx(8 / 13, abs=1e-12)
    assert ndcg == pytest.approx(1.0, abs=1e-12)
    assert auc == pytest.approx(1.0, abs=1e-12)


def test_temporal_split_censors_post_cutoff_outcomes_without_mutating_inputs():
    def make_corpus(late_test_label: float) -> list[dict]:
        return load_rows({
            "recommendation_runs": [
                {"id": f"run-{day}", "created_at": f"2026-01-0{day}T00:00:00Z"}
                for day in (1, 2, 3, 4, 5)
            ],
            "recommendation_impressions": [
                {"id": 1, "run_id": "run-1", "candidate_id": 11, "rank": 1, "score": 95.0, "propensity": 1.0},
                {"id": 2, "run_id": "run-1", "candidate_id": 12, "rank": 2, "score": 85.0, "propensity": 1.0},
                {"id": 3, "run_id": "run-2", "candidate_id": 21, "rank": 1, "score": 90.0, "propensity": 1.0},
                {"id": 4, "run_id": "run-3", "candidate_id": 31, "rank": 1, "score": 88.0, "propensity": 1.0},
                {"id": 5, "run_id": "run-4", "candidate_id": 41, "rank": 1, "score": 92.0, "propensity": 1.0},
                {"id": 6, "run_id": "run-4", "candidate_id": 42, "rank": 2, "score": 82.0, "propensity": 1.0},
                {"id": 7, "run_id": "run-5", "candidate_id": 51, "rank": 1, "score": 96.0, "propensity": 1.0},
                {"id": 8, "run_id": "run-5", "candidate_id": 52, "rank": 2, "score": 76.0, "propensity": 1.0},
            ],
            "recommendation_outcomes": [
                # Pre-cutoff train outcomes (< 2026-01-04T00:00:00Z)
                {"impression_id": 1, "label": 1.0, "confidence": 0.8, "label_kind": "explicit_feedback", "attributed_at": "2026-01-02T00:00:00Z"},
                {"impression_id": 2, "label": 0.0, "confidence": 0.8, "label_kind": "explicit_feedback", "attributed_at": "2026-01-02T00:00:00Z"},
                # Exact boundary outcome (== validation start 2026-01-04T00:00:00Z) and test-era outcome on train impressions
                {"impression_id": 1, "label": late_test_label, "confidence": 1.0, "label_kind": "rating_positive" if late_test_label > 0.5 else "rating_negative", "attributed_at": "2026-01-05T12:00:00Z"},
                {"impression_id": 3, "label": 1.0, "confidence": 1.0, "label_kind": "rating_positive", "attributed_at": "2026-01-04T00:00:00Z"},
                # Validation outcomes: one before test start (< 2026-01-05T00:00:00Z) and one at/after test start
                {"impression_id": 5, "label": 1.0, "confidence": 1.0, "label_kind": "rating_positive", "attributed_at": "2026-01-04T12:00:00Z"},
                {"impression_id": 6, "label": late_test_label, "confidence": 1.0, "label_kind": "rating_positive" if late_test_label > 0.5 else "rating_negative", "attributed_at": "2026-01-05T00:00:00Z"},
                # Test outcomes
                {"impression_id": 7, "label": 1.0, "confidence": 1.0, "label_kind": "rating_positive", "attributed_at": "2026-01-06T00:00:00Z"},
                {"impression_id": 8, "label": 0.0, "confidence": 1.0, "label_kind": "rating_negative", "attributed_at": "2026-01-06T00:00:00Z"},
            ],
        })

    rows_positive = make_corpus(1.0)
    rows_negative = make_corpus(0.0)
    splits = temporal_split(rows_negative)

    # Impression 1 in train falls back to its pre-cutoff explicit_feedback outcome; impression 3 (at cutoff) is censored to None.
    by_impression_train = {row["impression_id"]: row for row in splits["train"]}
    assert by_impression_train[1]["label_kind"] == "explicit_feedback"
    assert by_impression_train[1]["label"] == 1.0
    assert len(by_impression_train[1]["observed_outcomes"]) == 1
    assert by_impression_train[3]["label"] is None
    assert by_impression_train[3]["label_kind"] is None
    assert by_impression_train[3]["observed_outcomes"] == []

    # Impression 6 in validation (attributed at exact test start) is censored to None, never turned into a negative.
    by_impression_val = {row["impression_id"]: row for row in splits["validation"]}
    assert by_impression_val[5]["label"] == 1.0
    assert by_impression_val[6]["label"] is None
    assert by_impression_val[6]["observed_outcomes"] == []

    # Input rows remain unmutated.
    assert len(rows_negative[0]["observed_outcomes"]) == 2
    assert rows_negative[0]["label_kind"] == "rating_negative"

    # Mutating test-period ratings on train/validation impressions never alters train or validation metrics.
    report_pos = build_report(rows_positive, bootstrap_iterations=5, bootstrap_seed=11)
    report_neg = build_report(rows_negative, bootstrap_iterations=5, bootstrap_seed=11)
    assert report_pos["splits"]["train"] == report_neg["splits"]["train"]
    assert report_pos["splits"]["validation"] == report_neg["splits"]["validation"]


def test_temporal_split_fails_closed_on_missing_or_unparseable_attributed_at():
    rows = load_rows({
        "recommendation_runs": [
            {"id": "run-1", "created_at": "2026-01-01T00:00:00Z"},
            {"id": "run-2", "created_at": "2026-01-02T00:00:00Z"},
            {"id": "run-3", "created_at": "2026-01-03T00:00:00Z"},
            {"id": "run-4", "created_at": "2026-01-04T00:00:00Z"},
            {"id": "run-5", "created_at": "2026-01-05T00:00:00Z"},
        ],
        "recommendation_impressions": [
            {"id": 1, "run_id": "run-1", "candidate_id": 11, "rank": 1, "score": 95.0, "propensity": 1.0},
            {"id": 2, "run_id": "run-1", "candidate_id": 12, "rank": 2, "score": 85.0, "propensity": 1.0},
            {"id": 3, "run_id": "run-2", "candidate_id": 21, "rank": 1, "score": 90.0, "propensity": 1.0},
            {"id": 4, "run_id": "run-4", "candidate_id": 41, "rank": 1, "score": 92.0, "propensity": 1.0},
            {"id": 5, "run_id": "run-5", "candidate_id": 51, "rank": 1, "score": 96.0, "propensity": 1.0},
        ],
        "recommendation_outcomes": [
            # Impression 1: unparseable timestamp on rating_positive, valid pre-cutoff timestamp on explicit_feedback -> falls back to explicit_feedback.
            {"impression_id": 1, "label": 1.0, "confidence": 0.8, "label_kind": "explicit_feedback", "attributed_at": "2026-01-01T12:00:00Z"},
            {"impression_id": 1, "label": 0.0, "confidence": 1.0, "label_kind": "rating_negative", "attributed_at": "not-a-timestamp"},
            # Impression 2: only an unparseable attributed_at -> must be censored to None in train.
            {"impression_id": 2, "label": 1.0, "confidence": 1.0, "label_kind": "rating_positive", "attributed_at": "2026-13-99"},
            # Impression 3: missing attributed_at (None) -> must be censored to None in train.
            {"impression_id": 3, "label": 1.0, "confidence": 1.0, "label_kind": "rating_positive", "attributed_at": None},
            # Impression 4 (validation, cutoff=run-5 start): whitespace attributed_at -> must be censored to None in validation.
            {"impression_id": 4, "label": 1.0, "confidence": 1.0, "label_kind": "rating_positive", "attributed_at": "   "},
            # Impression 5 (test, cutoff=None): unparseable attributed_at in final test split has no future cutoff and stays observed.
            {"impression_id": 5, "label": 1.0, "confidence": 1.0, "label_kind": "rating_positive", "attributed_at": "unknown-date"},
        ],
    })
    # Also include a flat synthetic train row with an unparseable top-level attributed_at.
    flat_unparseable = {
        "run_id": "run-3",
        "run_created_at": "2026-01-03T00:00:00Z",
        "impression_id": 99,
        "candidate_id": 31,
        "rank": 1,
        "score": 88.0,
        "propensity": 1.0,
        "label": 1.0,
        "confidence": 1.0,
        "label_kind": "rating_positive",
        "attributed_at": "corrupt-timestamp",
    }
    splits = temporal_split([*rows, flat_unparseable])
    by_train = {row["impression_id"]: row for row in splits["train"]}
    assert by_train[1]["label_kind"] == "explicit_feedback"
    assert by_train[1]["label"] == 1.0
    assert len(by_train[1]["observed_outcomes"]) == 1
    assert by_train[2]["label"] is None
    assert by_train[2]["observed_outcomes"] == []
    assert by_train[3]["label"] is None
    assert by_train[3]["observed_outcomes"] == []
    assert by_train[99]["label"] is None
    assert by_train[99]["attributed_at"] is None

    by_val = {row["impression_id"]: row for row in splits["validation"]}
    assert by_val[4]["label"] is None
    assert by_val[4]["observed_outcomes"] == []

    by_test = {row["impression_id"]: row for row in splits["test"]}
    assert by_test[5]["label"] == 1.0


def test_sqlite_cli_preserves_report_contract_with_shortlisted_neutral_reads(tmp_path: Path):
    import sqlite3

    db_path = tmp_path / "snapshot.sqlite"
    with sqlite3.connect(db_path) as con:
        con.executescript(
            """
            CREATE TABLE recommendation_runs (id TEXT PRIMARY KEY, created_at TEXT);
            CREATE TABLE recommendation_impressions (
                id INTEGER PRIMARY KEY,
                run_id TEXT,
                candidate_id INTEGER,
                rank INTEGER,
                score REAL,
                propensity REAL,
                presented_at TEXT
            );
            CREATE TABLE recommendation_outcomes (
                id INTEGER PRIMARY KEY,
                impression_id INTEGER,
                label REAL,
                label_kind TEXT,
                confidence REAL,
                attributed_at TEXT
            );
            """
        )
        for day in range(1, 6):
            run_id = f"run-{day}"
            ts = f"2026-02-0{day}T00:00:00Z"
            con.execute("INSERT INTO recommendation_runs(id, created_at) VALUES(?, ?)", (run_id, ts))
            con.execute(
                "INSERT INTO recommendation_impressions VALUES(?,?,?,?,?,?,?)",
                (day * 10 + 1, run_id, day * 100 + 1, 1, 95.0, 1.0, ts),
            )
            con.execute(
                "INSERT INTO recommendation_impressions VALUES(?,?,?,?,?,?,?)",
                (day * 10 + 2, run_id, day * 100 + 2, 2, 85.0, 1.0, ts),
            )
            con.execute(
                "INSERT INTO recommendation_outcomes(impression_id, label, label_kind, confidence, attributed_at) VALUES(?,?,?,?,?)",
                (day * 10 + 1, 1.0, "explicit_feedback", 0.9, ts),
            )
            con.execute(
                "INSERT INTO recommendation_outcomes(impression_id, label, label_kind, confidence, attributed_at) VALUES(?,?,?,?,?)",
                (day * 10 + 1, 1.0, "rating_positive", 1.0, ts),
            )
            con.execute(
                "INSERT INTO recommendation_outcomes(impression_id, label, label_kind, confidence, attributed_at) VALUES(?,?,?,?,?)",
                (day * 10 + 2, 1.0, "explicit_feedback", 0.8, ts),
            )
            con.execute(
                "INSERT INTO recommendation_outcomes(impression_id, label, label_kind, confidence, attributed_at) VALUES(?,?,?,?,?)",
                (day * 10 + 2, 0.5, "rating_neutral", 0.25, ts),
            )
    result = subprocess.run(
        [
            sys.executable,
            "engine/scripts/evaluate_recommendations.py",
            str(db_path),
            "--bootstrap-iterations",
            "5",
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=Path(__file__).parents[2],
    )
    report = json.loads(result.stdout)
    assert set(report) == {
        "enjoyment",
        "metric_scope",
        "minimums",
        "protocol",
        "sample",
        "signal_diagnostics",
        "splits",
    }
    assert report["enjoyment"]["labeled_impressions"] == 10
    assert report["enjoyment"]["neutral_impressions"] == 5
    assert report["enjoyment"]["auc"] is None
    assert report["enjoyment"]["grade_concordance"] == pytest.approx(1.0)
    assert report["enjoyment"]["precision_at_20"] == pytest.approx(0.5)
    assert report["signal_diagnostics"]["explicit_ratings"]["auc"] is None
    assert report["signal_diagnostics"]["explicit_ratings"]["grade_concordance"] == pytest.approx(1.0)
