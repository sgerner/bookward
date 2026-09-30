"""Focused protocol tests for the private-corpus ranking experiment runner."""
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
import sys

import numpy as np
import pytest

SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import evaluate_ranking_experiments as experiments


def synthetic_records(target_ratings=(4, 1, 5)):
    records = []
    first_day = datetime(2020, 1, 1)
    target_day = first_day + timedelta(days=1)
    for index in range(100 + len(target_ratings)):
        is_target = index >= 100
        read_day = target_day if is_target else first_day
        rating = target_ratings[index - 100] if is_target else index % 5 + 1
        item = {
            "id": index + 1,
            "title": f"Synthetic book {index + 1}",
            "author": f"Synthetic author {index % 7}",
            "rating": rating,
            "read_at": read_day.isoformat(),
        }
        vector = np.asarray(
            [1.0, (index % 11) / 11, (index % 7) / 7, (index % 3) / 3],
            dtype=np.float32,
        )
        records.append((read_day, item, vector))
    return records


def test_expanding_replay_excludes_every_book_from_its_target_day():
    records = synthetic_records()
    changed_same_day = deepcopy(records)
    for _, item, _ in changed_same_day[100:]:
        item["rating"] = 6 - item["rating"]

    original = experiments.replay(records)
    changed = experiments.replay(changed_same_day)

    assert original[-1]["compared_predictions"] == 3
    np.testing.assert_array_equal(
        original[4]["current"][100:], changed[4]["current"][100:]
    )


def test_expanding_replay_fails_closed_when_served_scores_disagree(monkeypatch):
    def wrong_scorer(reads, read_vectors, candidates, candidate_vectors):
        return [{**candidate, "score": 0.0} for candidate in candidates]

    monkeypatch.setattr(experiments, "rank_candidates", wrong_scorer)
    with pytest.raises(RuntimeError, match="differed from rank_candidates"):
        experiments.replay(synthetic_records())


def test_formula_selection_uses_validation_then_reports_later_test():
    ratings = np.asarray([1, 5, 1, 5, 5, 1, 5, 1], dtype=float)
    current = np.asarray([10, 90, 20, 80, 20, 80, 80, 20], dtype=float)
    validation_winner = np.asarray([10, 90, 20, 80, 80, 20, 20, 80], dtype=float)
    predictions = {name: current.copy() for name in experiments.VARIANTS}
    predictions["negative_top5"] = validation_winner

    result = experiments.fit_evaluation(
        ratings, predictions, (4, 6), bootstrap_iterations=0, bootstrap_seed=17
    )

    assert result["selected_on_validation"] == "negative_top5"
    assert result["validation_balanced_high_low_auc"]["negative_top5"] > result[
        "validation_balanced_high_low_auc"
    ]["current"]
    assert result["test_selected_minus_current"]["balanced_auc"] < 0


def test_validation_without_low_ratings_fails_with_clear_reason():
    ratings = np.asarray([1, 5, 1, 5, 5, 3, 5, 1], dtype=float)
    scores = np.arange(len(ratings), dtype=float) * 10
    predictions = {name: scores.copy() for name in experiments.VARIANTS}

    with pytest.raises(ValueError, match="no 1–2 star reads"):
        experiments.fit_evaluation(
            ratings, predictions, (4, 6), bootstrap_iterations=0, bootstrap_seed=17
        )


def test_single_class_metrics_and_bootstrap_are_undefined_not_zero():
    result = experiments.metrics(np.asarray([5, 5]), np.asarray([1.0, 2.0]))
    assert result["auc_high_4plus"] is None
    assert result["auc_low_2or_less"] is None
    assert experiments.bootstrap_auc_deltas(
        np.asarray([5, 5]), np.asarray([1.0, 2.0]), np.asarray([2.0, 1.0]), 20, 17
    ) == {
        "high_auc_delta_95pct": None,
        "low_auc_delta_95pct": None,
        "bootstrap_resamples": 20,
        "bootstrap_seed": 17,
    }


def test_feedback_replay_fails_closed_when_current_score_parity_breaks(monkeypatch):
    import evaluate_ranking_experiments as experiments

    import afterword_engine.embeddings as embeddings
    from evaluate_ranking import document

    candidate = {
        "id": 10,
        "title": "Synthetic candidate",
        "author": "Unrated author",
        "description": "",
        "genres": "[]",
        "source_weight": 1.0,
    }
    vector = np.asarray([1.0, 0.0], dtype=np.float32)
    entry = {
        "entity_type": "candidate",
        "entity_id": candidate["id"],
        "backend": "synthetic",
        "model": "test",
        "content_hash": embeddings.content_hash(document(candidate)),
        "vector": vector.tobytes(),
        "dimensions": 2,
    }
    records = [
        (
            datetime(2020, 1, 1),
            {
                "id": 1,
                "title": "Earlier read",
                "author": "Another author",
                "rating": 5,
                "read_at": "2020-01-01",
            },
            vector,
        )
    ]
    data = {
        "candidates": [candidate],
        "embeddings": [entry],
        "feedback": [
            {
                "id": 1,
                "candidate_id": candidate["id"],
                "action": "save",
                "created_at": "2020-01-02T00:00:00Z",
            }
        ],
    }
    monkeypatch.setattr(
        experiments,
        "rank_candidates",
        lambda _reads, _read_vectors, candidates, _candidate_vectors: [
            {**item, "score": 0.0} for item in candidates
        ],
    )

    with pytest.raises(RuntimeError, match="differed from rank_candidates"):
        experiments.feedback_replay(data, records, ("synthetic", "test"), "current")
