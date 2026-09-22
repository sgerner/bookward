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
