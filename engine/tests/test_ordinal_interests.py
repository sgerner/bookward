"""Focused invariants for the aggregate ordinal preference evaluator."""
from datetime import date
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "evaluate_ordinal_interests.py"
spec = importlib.util.spec_from_file_location("evaluate_ordinal_interests", SCRIPT)
evaluator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluator)


def test_same_day_queries_do_not_enter_causal_history():
    days = [date(2020, 1, 1), date(2020, 1, 1), date(2020, 1, 2),
            date(2020, 1, 2), date(2020, 1, 3)]

    assert evaluator.causal_history_end(days, days[2]) == 2
    assert evaluator.causal_history_end(days, days[4]) == 4


def test_prefix_clusters_freeze_centers_and_append_only_after_scoring():
    rng = np.random.default_rng(20260930)
    vectors = evaluator.normalize(rng.normal(size=(101, 8)).astype(np.float32))
    state = evaluator.PrefixInterests(3)
    state.ensure_started(np.arange(100), vectors)
    centers = state.model.cluster_centers_.copy()
    prefix_labels = state.labels_by_index[:100].copy()

    assert state.labels_by_index[100] == -1
    state.append_day(np.array([100]), vectors)

    np.testing.assert_array_equal(state.labels_by_index[:100], prefix_labels)
    np.testing.assert_allclose(state.model.cluster_centers_, centers)
    assert 0 <= state.labels_by_index[100] < 3


def test_ordinal_fit_scales_on_training_prefix_and_orders_toy_preferences():
    rng = np.random.default_rng(41)
    x = np.linspace(-1.5, 1.5, 501)
    latent = x + rng.normal(0.0, 0.3, size=len(x))
    ratings = np.digitize(latent, [-0.85, -0.25, 0.25, 0.85]) + 1
    train_X = np.column_stack([x, np.sin(2.0 * x)])
    held_out_extreme = np.array([[1000.0, -1000.0]])

    model = evaluator.fit_ordinal(train_X, ratings)
    scores = evaluator.ordinal_scores(model, train_X)
    probabilities = evaluator.ordinal_probabilities(model, scores)
    expected = evaluator.ordinal_expected_ratings(model, train_X)

    np.testing.assert_allclose(model["mean"], train_X.mean(axis=0))
    np.testing.assert_allclose(model["scale"], train_X.std(axis=0))
    assert not np.allclose(model["mean"], np.vstack([train_X, held_out_extreme]).mean(axis=0))
    assert np.all(np.diff(model["thresholds"]) > 0)
    assert model["beta"][0] > 0
    assert np.isfinite(probabilities).all()
    np.testing.assert_allclose(probabilities.sum(axis=1), 1.0, atol=1e-12)
    assert np.all(np.diff(expected) >= -1e-8)
    assert expected[-1] > expected[0]


def test_ordinal_probabilities_remain_finite_and_normalized_at_extreme_scores():
    model = {"thresholds": np.array([-1.5, -0.4, 0.5, 1.7])}
    probabilities = evaluator.ordinal_probabilities(model, np.array([-1e6, -5.0, 0.0, 5.0, 1e6]))

    assert probabilities.shape == (5, 5)
    assert np.isfinite(probabilities).all()
    assert np.all(probabilities >= 0.0)
    np.testing.assert_allclose(probabilities.sum(axis=1), 1.0, atol=1e-12)


def test_expected_ratings_align_models_with_different_latent_origins():
    first = {"mean": np.array([0.0]), "scale": np.array([1.0]),
             "beta": np.array([1.0]), "thresholds": np.array([-1.5, -0.4, 0.5, 1.7])}
    shifted = {**first, "mean": np.array([-10.0]),
               "thresholds": first["thresholds"] + 10.0}
    queries = np.array([[-2.0], [2.0]])

    np.testing.assert_allclose(
        evaluator.ordinal_expected_ratings(first, queries),
        evaluator.ordinal_expected_ratings(shifted, queries),
    )
    # Raw latent scores would misorder these two different query days.
    raw = np.array([evaluator.ordinal_scores(first, queries[1:])[0],
                    evaluator.ordinal_scores(shifted, queries[:1])[0]])
    common = np.array([evaluator.ordinal_expected_ratings(first, queries[1:])[0],
                       evaluator.ordinal_expected_ratings(shifted, queries[:1])[0]])
    assert evaluator.metric_set([5, 1], raw)["auc_high_4plus"] == 0.0
    assert evaluator.metric_set([5, 1], common)["auc_high_4plus"] == 1.0


def test_bottom_tail_ties_match_the_raw_ranking_report():
    from evaluate_historical_ratings import raw_ranking_metrics

    ratings = np.array([1] + [3] * 19 + [5], dtype=float)
    scores = np.full(21, 50.0)
    result = evaluator.metric_set(ratings, scores)
    reference = raw_ranking_metrics(ratings, scores)

    assert result["bottom20_low_count"] == reference["low_rated_in_bottom_20"] == 0


def test_ordinal_fit_rejects_fractional_or_out_of_range_ratings():
    X = np.arange(5, dtype=np.float64)[:, None]

    with pytest.raises(ValueError, match="integer stars"):
        evaluator.fit_ordinal(X, np.array([1, 2, 3.5, 4, 5]))
    with pytest.raises(ValueError, match="integer stars"):
        evaluator.fit_ordinal(X, np.array([0, 2, 3, 4, 5]))


def test_ordinal_fit_fails_closed_when_optimizer_does_not_converge(monkeypatch):
    def nonconverged(_objective, initial, **_kwargs):
        return SimpleNamespace(fun=1.0, x=initial, success=False,
                               message="iteration limit reached", nit=2)

    monkeypatch.setattr(evaluator, "minimize", nonconverged)
    X = np.arange(5, dtype=np.float64)[:, None]

    with pytest.raises(RuntimeError, match="did not converge"):
        evaluator.fit_ordinal(X, np.arange(1, 6))
