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


def test_whole_day_split_keeps_boundary_day_together():
    dates = ["2020-01-01"] * 6 + ["2020-01-02"] * 7 + ["2020-01-03"] * 4 + ["2020-01-04"] * 3
    records = [(read_time(date), {"id": i}, None) for i, date in enumerate(dates)]
    _, cuts = historical.whole_day_cuts(records)
    assert cuts == [13, 17]
