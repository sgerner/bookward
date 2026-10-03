"""Focused invariants for the frozen tail and pairwise fit helpers."""
import importlib.util
from pathlib import Path
import sys

import numpy as np
import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "evaluate_tail_pairwise.py"
spec = importlib.util.spec_from_file_location("evaluate_tail_pairwise", SCRIPT)
evaluator = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = evaluator
spec.loader.exec_module(evaluator)


def test_tail_heads_use_all_five_levels_and_keep_three_stars_out_of_both_tails(monkeypatch):
    captured = []

    class RecordingClassifier:
        def __init__(self, **options):
            self.options = options

        def fit(self, features, target):
            self.features = features.copy()
            self.target = target.copy()
            self.classes_ = np.array([0, 1])
            captured.append(self)
            return self

    monkeypatch.setattr(evaluator, "LogisticRegression", RecordingClassifier)
    ratings = np.array([1, 2, 3, 4, 5])
    features = np.column_stack([ratings, ratings ** 2]).astype(float)

    model = evaluator.fit_tail_heads(features, ratings, c=1.0)

    assert len(captured) == 2
    np.testing.assert_array_equal(captured[0].target, [0, 0, 0, 1, 1])
    np.testing.assert_array_equal(captured[1].target, [1, 1, 0, 0, 0])
    assert model.c == 1.0
    assert captured[0].options["class_weight"] is None
    np.testing.assert_allclose(model.scaler.mean, features.mean(axis=0))


def test_tail_utility_clips_and_uses_one_decimal_python_rounding():
    score = evaluator.tail_head_utility(
        np.array([1.0, 0.501, 0.0]),
        np.array([0.0, 0.499, 1.0]),
    )

    np.testing.assert_array_equal(score, [100.0, 50.1, 0.0])


def test_feature_scaler_uses_training_rows_only():
    train = np.array([[1.0, 4.0], [3.0, 8.0]])
    scaler = evaluator.fit_feature_scaler(train)
    heldout = np.array([[1e9, -1e9]])

    np.testing.assert_allclose(scaler.mean, [2.0, 6.0])
    np.testing.assert_allclose(scaler.scale, [1.0, 2.0])
    assert not np.allclose(scaler.mean, np.vstack([train, heldout]).mean(axis=0))


def test_signal_blends_fit_every_scale_on_training_rows_only():
    primary = np.array([10.0, 20.0, 30.0, 40.0])
    signal = np.array([1.0, 3.0, 5.0, 1000.0])

    score, scalers = evaluator.build_additive_blend(
        primary, {"cluster": signal}, np.array([0, 1])
    )

    np.testing.assert_allclose(score, [-1.25, 1.25, 3.75, 254.5])
    assert scalers["primary"] == {"mean": 15.0, "scale": 5.0}
    assert scalers["signals"]["cluster"] == {"mean": 2.0, "scale": 1.0}


def test_selected_scorers_include_the_fixed_recency_cluster3_pair():
    current = np.array([50.0, 60.0])
    heads = np.array([55.0, 65.0])
    pairwise = np.array([54.0, 66.0])
    signals = {
        "recency": np.array([1.0, -1.0]),
        "cluster": np.array([0.5, 0.25]),
        "ordinal": np.array([0.0, 0.0]),
    }

    arms = evaluator._segment_arms(current, heads, pairwise, signals, current_scale=10.0)

    np.testing.assert_array_equal(arms["heads_plus_recency_cluster3"], [58.8, 63.1])
    np.testing.assert_array_equal(arms["pairwise_plus_recency_cluster3"], [57.8, 64.1])


def test_standalone_exports_keep_raw_scores_while_blends_use_aligned_scores():
    signals = {
        "recency": np.array([0.0]),
        "cluster": np.array([0.0]),
        "ordinal": np.array([0.0]),
    }
    arms = evaluator._segment_arms(
        np.array([50.0]), np.array([60.0]), np.array([70.0]), signals, 10.0,
        raw_heads=np.array([40.0]), raw_pairwise=np.array([45.0]),
    )

    np.testing.assert_array_equal(arms["heads"], [40.0])
    np.testing.assert_array_equal(arms["pairwise"], [45.0])
    np.testing.assert_array_equal(arms["heads_plus_ordinal"], [60.0])
    np.testing.assert_array_equal(arms["pairwise_plus_ordinal"], [70.0])


def test_pairwise_fit_counts_unequal_rating_pairs_and_learns_predictive_residual():
    feature = np.arange(10, dtype=float)
    ratings = np.array([1, 1, 2, 2, 3, 3, 4, 4, 5, 5])
    features = feature[:, None]
    # A current score that already ranks the examples correctly provides a
    # stable offset while exercising the five-point serving cap.
    current = 20.0 + feature * 8.0

    model = evaluator.fit_pairwise_ranker(features, ratings, current, c=1.0)
    scored = evaluator.score_pairwise_ranker(model, features, current)

    expected_pairs = sum(ratings[left] != ratings[right]
                         for left in range(len(ratings))
                         for right in range(left + 1, len(ratings)))
    assert model.comparison_pairs == expected_pairs
    assert model.coefficients[0] > 0
    assert np.max(np.abs(scored - current)) <= min(0.25 * current.std(), 5.0) + 0.05
    assert np.all(np.diff(scored) >= -1e-10)

    zero_residual = evaluator.fit_pairwise_ranker(features, ratings, current, c=0.0)
    np.testing.assert_array_equal(
        evaluator.score_pairwise_ranker(zero_residual, features, current), current
    )


def test_pairwise_score_rejects_misaligned_current_values():
    with pytest.raises(ValueError, match="aligned finite vector"):
        evaluator.score_pairwise_ranker(
            object(), np.zeros((2, 1)), np.zeros(1)
        )


def test_c_configuration_uses_only_chronological_oof_folds():
    ratings = np.tile(np.arange(1, 6), 12)
    features = np.column_stack([ratings, np.sin(np.arange(len(ratings)))]).astype(float)
    current = 50.0 + (ratings - 3.0) * 3.0
    folds = [
        (np.arange(20), np.arange(20, 40)),
        (np.arange(40), np.arange(40, 60)),
    ]

    heads = evaluator.select_tail_heads_c_oof(
        features, ratings, folds, c_grid=(0.1, 1.0)
    )
    pairwise = evaluator.select_pairwise_c_oof(
        features, ratings, current, folds, c_grid=(0.1, 1.0)
    )

    assert heads.selected_c in (0.1, 1.0)
    assert pairwise.selected_c in (0.1, 1.0)
    assert heads.candidate_metrics[heads.selected_c]["oof_targets"] == 40
    assert pairwise.baseline_metrics["oof_targets"] == 40
    assert pairwise.candidate_metrics[pairwise.selected_c]["folds"] == 2


def test_oof_folds_reject_reused_or_nonchronological_evaluation_rows():
    with pytest.raises(ValueError, match="must be disjoint"):
        evaluator._validated_folds(
            [(np.array([0, 1]), np.array([1, 2]))], row_count=3
        )
    with pytest.raises(ValueError, match="strictly before"):
        evaluator._validated_folds(
            [(np.array([1, 2]), np.array([0]))], row_count=3
        )
