"""Historical star-rating replay must hide each target day's books."""

import importlib.util
from pathlib import Path
import sys

import numpy as np

SCRIPTS = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from evaluate_ranking import read_time

spec = importlib.util.spec_from_file_location("evaluate_historical_ratings", SCRIPTS / "evaluate_historical_ratings.py")
historical = importlib.util.module_from_spec(spec)
spec.loader.exec_module(historical)


def test_replay_excludes_same_day_targets_from_history(monkeypatch):
    dates = ["2020-01-01", "2020-01-02", "2020-01-02"]
    records = [
        (read_time(date),
         {"id": i, "title": f"Book {i}", "author": "Writer", "rating": rating},
         np.array([1.0, float(i)], dtype=np.float32))
        for i, (date, rating) in enumerate(zip(dates, [2, 5, 1]))
    ]

    def scorer(history, vectors, query, query_vectors):
        assert [item["id"] for item in history] == [0]
        assert [item["id"] for item in query] == [1, 2]
        return [{**item, "score": 50.0 + item["id"]} for item in query]

    def old_scorer(history, vectors, query, query_vectors):
        assert [item["id"] for item in history] == [0]
        return np.array([40.0, 41.0])

    monkeypatch.setattr(historical, "rank_candidates", scorer)
    monkeypatch.setattr(historical, "baseline", old_scorer)
    ratings, current, earlier, prior_mean, prior_median = historical.replay(records, min_history=1)
    np.testing.assert_array_equal(ratings, [2, 5, 1])
    assert np.isnan(current[0])
    np.testing.assert_array_equal(current[1:], [51.0, 52.0])
    np.testing.assert_array_equal(earlier[1:], [40.0, 41.0])
    np.testing.assert_array_equal(prior_mean[1:], [2.0, 2.0])
    np.testing.assert_array_equal(prior_median[1:], [2.0, 2.0])


def test_star_metrics_preserve_five_point_error():
    result = historical.metrics(np.array([1.0, 3.0, 5.0]), np.array([2.0, 3.0, 4.0]))
    assert result["mae_stars"] == 2 / 3
    assert result["rmse_stars"] == np.sqrt(2 / 3)
    assert result["spearman"] == 1.0


def test_raw_ranking_metrics_are_independent_of_negative_star_calibration(monkeypatch):
    ratings = np.array([5, 1, 4, 2, 5, 1, 4, 2, 5, 1], dtype=float)
    raw_scores = np.array([90, 10, 80, 20, 90, 10, 80, 20, 90, 10], dtype=float)
    records = [
        (read_time(f"2020-01-{day:02d}"), {"id": day, "rating": rating}, np.array([1.0]))
        for day, rating in enumerate(ratings, start=1)
    ]

    monkeypatch.setattr(
        historical,
        "replay",
        lambda _records: (
            ratings,
            raw_scores,
            raw_scores,
            np.full(len(ratings), 3.0),
            np.full(len(ratings), 3.0),
        ),
    )
    # A deliberately bad calibration reverses the ordering. It may affect
    # legacy calibrated metrics, but must not rewrite the raw-ranker report.
    monkeypatch.setattr(
        historical,
        "fit_calibrator",
        lambda _scores, _ratings, _method: lambda values: -np.asarray(values),
    )

    result = historical.evaluate(records)
    assert result["validation"]["models"]["current"]["linear"]["auc_four_or_five_stars"] == 0.0
    assert result["raw_ranking"]["validation"]["current"][
        "auc_four_or_five_vs_one_to_three"
    ] == 1.0
    assert result["raw_ranking"]["test"]["current"][
        "auc_four_or_five_vs_one_to_three"
    ] == 1.0


def test_raw_ranking_ties_are_stable_and_single_class_auc_is_undefined():
    tied = historical.raw_ranking_metrics(
        np.array([5, 1, 4, 2], dtype=float),
        np.ones(4, dtype=float),
    )
    assert tied["auc_four_or_five_vs_one_to_three"] == 0.5
    assert tied["auc_one_or_two_vs_three_to_five"] == 0.5
    assert tied["auc_high_vs_low_excluding_neutral"] == 0.5
    assert tied["high_4_or_5_count"] == 2
    assert tied["low_1_or_2_count"] == 2
    assert [cohort["mean_rating"] for cohort in tied["score_deciles"][:4]] == [5, 1, 4, 2]
    assert [(cohort["score_min"], cohort["score_max"]) for cohort in tied["score_deciles"][:4]] == [(1.0, 1.0)] * 4
    assert all(cohort["count"] == 0 for cohort in tied["score_deciles"][4:])
    assert all(cohort["score_min"] is None and cohort["score_max"] is None for cohort in tied["score_deciles"][4:])

    single_class = historical.raw_ranking_metrics(
        np.array([4, 5, 5], dtype=float),
        np.array([1, 2, 3], dtype=float),
    )
    assert single_class["auc_four_or_five_vs_one_to_three"] is None
    assert single_class["auc_one_or_two_vs_three_to_five"] is None
    assert single_class["auc_high_vs_low_excluding_neutral"] is None


def test_neutral_excluded_auc_ignores_three_star_scores_and_is_undefined_for_neutral_only():
    mixed = historical.raw_ranking_metrics(
        np.array([5, 1, 3], dtype=float),
        np.array([2, 1, 100], dtype=float),
    )
    assert mixed["auc_four_or_five_vs_one_to_three"] == 0.5
    assert mixed["auc_high_vs_low_excluding_neutral"] == 1.0
    assert mixed["high_4_or_5_count"] == 1
    assert mixed["low_1_or_2_count"] == 1

    neutral_only = historical.raw_ranking_metrics(
        np.array([3, 3], dtype=float),
        np.array([90, 10], dtype=float),
    )
    assert neutral_only["auc_high_vs_low_excluding_neutral"] is None
    assert neutral_only["high_4_or_5_count"] == 0
    assert neutral_only["low_1_or_2_count"] == 0


def test_whole_day_split_keeps_boundary_day_together():
    dates = ["2020-01-01"] * 6 + ["2020-01-02"] * 7 + ["2020-01-03"] * 4 + ["2020-01-04"] * 3
    records = [(read_time(date), {"id": i}, None) for i, date in enumerate(dates)]
    _, cuts = historical.whole_day_cuts(records)
    assert cuts == [13, 17]
