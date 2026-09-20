import json
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
        "llm_runs": [{"id": "llm-1", "status": "complete", "created_at": "2025-12-01T00:00:00Z"}],
        "llm_scores": [{"run_id": "llm-1", "candidate_id": 1, "score": 0.8}, {"run_id": "llm-1", "candidate_id": 2, "score": 0.1}],
    }
    path = tmp_path / "export.json"
    path.write_text(json.dumps(corpus))
    rows = load_rows(path)
    assert rows[0]["shadow_score"] == 0.8
    report = build_report(rows, bootstrap_iterations=3, minimums={"runs": 0, "labeled_impressions": 0, "positive_impressions": 0, "negative_impressions": 0})
    assert report["protocol"].startswith("temporal run split")
    result = subprocess.run(
        [sys.executable, "engine/scripts/evaluate_recommendations.py", str(path), "--bootstrap-iterations", "1", "--minimum-runs", "0", "--minimum-labeled-impressions", "0", "--minimum-positive-impressions", "0", "--minimum-negative-impressions", "0"],
        check=True,
        capture_output=True,
        text=True,
        cwd=Path(__file__).parents[2],
    )
    assert json.loads(result.stdout)["protocol"].startswith("temporal run split")
