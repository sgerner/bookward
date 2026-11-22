import json
import math

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


def test_ips_metrics_weight_propensity_and_ignore_censored_rows():
    rows = _rows()[:2] + [{"run_id": "censored", "rank": 1, "score": 100, "propensity": 0.01, "label": None}]
    assert ips_auc(rows) == 1
    assert ips_precision_at_k(rows, 20) == 0.5
    assert ips_ndcg_at_k(rows, 20) == 1


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
    assert enjoyment["auc"] == pytest.approx(1.0)
    # Full certainty (1.0) on rating_neutral means 1 positive + 1 neutral => precision 0.5, not 1 / 1.25 = 0.8.
    assert enjoyment["precision_at_20"] == pytest.approx(0.5)


def test_ips_auc_evaluates_concordance_across_all_distinct_grades():
    five_over_three = [
        {"run_id": "r1", "rank": 1, "score": 95.0, "label": 1.0, "confidence": 1.0, "label_kind": "rating_positive"},
        {"run_id": "r1", "rank": 2, "score": 80.0, "label": 0.5, "confidence": 0.25, "label_kind": "rating_neutral"},
    ]
    assert ips_auc(five_over_three) == pytest.approx(1.0)

    three_over_one = [
        {"run_id": "r1", "rank": 1, "score": 80.0, "label": 0.5, "confidence": 0.25, "label_kind": "rating_neutral"},
        {"run_id": "r1", "rank": 2, "score": 60.0, "label": 0.0, "confidence": 1.0, "label_kind": "rating_negative"},
    ]
    assert ips_auc(three_over_one) == pytest.approx(1.0)

    three_grades_partial_inversion = [
        {"run_id": "r1", "rank": 1, "score": 80.0, "label": 1.0, "confidence": 1.0, "label_kind": "rating_positive"},
        {"run_id": "r1", "rank": 2, "score": 90.0, "label": 0.5, "confidence": 0.25, "label_kind": "rating_neutral"},
        {"run_id": "r1", "rank": 3, "score": 60.0, "label": 0.0, "confidence": 1.0, "label_kind": "rating_negative"},
    ]
    assert ips_auc(three_grades_partial_inversion) == pytest.approx(2 / 3)

    single_grade_only = [
        {"run_id": "r1", "rank": 1, "score": 90.0, "label": 0.5, "confidence": 0.25, "label_kind": "rating_neutral"},
        {"run_id": "r1", "rank": 2, "score": 70.0, "label": 0.5, "confidence": 0.25, "label_kind": "rating_neutral"},
    ]
    assert ips_auc(single_grade_only) is None


def test_neutral_reads_have_full_precision_weight_and_zero_ndcg_gain():
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
    assert ips_precision_at_k(one_five_above_four_threes, 5) == pytest.approx(0.20)
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
    assert precision is not None and 0.5 < precision <= 1.0
    assert ndcg is not None and 0.0 <= ndcg <= 1.0
    assert auc == pytest.approx(1.0)


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
    assert report["enjoyment"]["auc"] == pytest.approx(1.0)
    assert report["enjoyment"]["precision_at_20"] == pytest.approx(0.5)
    assert report["signal_diagnostics"]["explicit_ratings"]["auc"] == pytest.approx(1.0)
