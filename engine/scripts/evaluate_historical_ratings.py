#!/usr/bin/env python3
"""Replay rated reads against strictly earlier reading history.

This is a retrospective preference test among books the reader chose to read.
It does not measure retrieval of unread books. Only aggregate metrics are printed.
"""

import argparse
from bisect import bisect_left
import json
from pathlib import Path

import numpy as np
from scipy.stats import spearmanr
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import Ridge

from evaluate_ranking import baseline, load_corpus, prepare
from afterword_engine.ranking import rank_candidates


def whole_day_cuts(records):
    dates = [row[0].date() for row in records]
    cuts = []
    for fraction in (0.6, 0.8):
        cut = int(len(records) * fraction)
        while cut < len(records) and dates[cut] == dates[cut - 1]:
            cut += 1
        cuts.append(cut)
    if cuts[0] >= cuts[1] or cuts[1] >= len(records):
        raise ValueError("Not enough distinct read dates for chronological validation and test")
    return dates, cuts


def replay(records, min_history=100):
    """A target day's reads never appear in any score for that day."""
    dates = [row[0].date() for row in records]
    items = [row[1] for row in records]
    vectors = np.stack([row[2] for row in records])
    n = len(records)
    current = np.full(n, np.nan)
    earlier = np.full(n, np.nan)
    prior_mean = np.full(n, np.nan)
    prior_median = np.full(n, np.nan)
    ratings = np.array([float(item["rating"]) for item in items])
    start = 0
    while start < n:
        history_end = bisect_left(dates, dates[start])
        stop = start + 1
        while stop < n and dates[stop] == dates[start]:
            stop += 1
        if history_end >= min_history:
            history, query = items[:history_end], items[start:stop]
            history_vectors, query_vectors = vectors[:history_end], vectors[start:stop]
            ranked = rank_candidates(history, history_vectors, query, query_vectors)
            by_id = {row["id"]: float(row["score"]) for row in ranked}
            current[start:stop] = [by_id[item["id"]] for item in query]
            earlier[start:stop] = baseline(history, history_vectors, query, query_vectors)
            prior_mean[start:stop] = ratings[:history_end].mean()
            prior_median[start:stop] = np.median(ratings[:history_end])
        start = stop
    return ratings, current, earlier, prior_mean, prior_median


def fit_calibrator(scores, ratings, method):
    if method == "linear":
        model = Ridge(alpha=10.0).fit(scores[:, None], ratings)
        return lambda values: np.clip(model.predict(values[:, None]), 1, 5)
    if method == "isotonic":
        model = IsotonicRegression(y_min=1, y_max=5, out_of_bounds="clip").fit(scores, ratings)
        return lambda values: model.predict(values)
    raise ValueError(f"Unknown calibration method: {method}")


def metrics(ratings, predictions):
    residual = predictions - ratings
    rho = spearmanr(ratings, predictions).statistic if len(np.unique(ratings)) > 1 and len(np.unique(predictions)) > 1 else None
    top = np.argsort(-predictions, kind="stable")[:min(20, len(ratings))]
    gain = 2 ** (ratings - 1) - 1
    discount = 1 / np.log2(np.arange(2, len(top) + 2))
    ideal = np.sort(gain)[::-1][:len(top)] @ discount
    positive, negative = predictions[ratings >= 4], predictions[ratings < 4]
    auc = ((positive[:, None] > negative).sum() + .5 * (positive[:, None] == negative).sum()) / (len(positive) * len(negative)) if len(positive) and len(negative) else None
    return {
        "count": int(len(ratings)),
        "mae_stars": float(np.abs(residual).mean()),
        "rmse_stars": float(np.sqrt(np.mean(residual ** 2))),
        "spearman": float(rho) if rho is not None and np.isfinite(rho) else None,
        "graded_ndcg_at_20": float(gain[top] @ discount / ideal) if ideal else None,
        "auc_four_or_five_stars": float(auc) if auc is not None else None,
    }


def evaluate(records):
    _, (train_stop, validation_stop) = whole_day_cuts(records)
    ratings, current, earlier, prior_mean, prior_median = replay(records)
    usable = np.isfinite(current)
    train = np.flatnonzero(usable & (np.arange(len(records)) < train_stop))
    validation = np.arange(train_stop, validation_stop)
    test = np.arange(validation_stop, len(records))
    if not len(train) or not np.isfinite(current[validation]).all() or not np.isfinite(current[test]).all():
        raise ValueError("The historical replay needs more dated training history")
    scores = {"current": current, "earlier": earlier}
    choices = []
    for name, values in scores.items():
        for method in ("linear", "isotonic"):
            predictor = fit_calibrator(values[train], ratings[train], method)
            choices.append((metrics(ratings[validation], predictor(values[validation]))["mae_stars"], name, method))
    # Validation chooses both the formula and its map from 0–100 to 1–5 stars.
    _, chosen_name, chosen_method = min(choices)
    fit = np.concatenate((train, validation))
    chosen = fit_calibrator(scores[chosen_name][fit], ratings[fit], chosen_method)
    return {
        "protocol": "distinct rated works; chronological 60/20/20 whole-day split; per-book strictly earlier-day history",
        "counts": {"train": int(len(train)), "validation": int(len(validation)), "test": int(len(test))},
        "validation": {
            "prior_mean": metrics(ratings[validation], prior_mean[validation]),
            "prior_median": metrics(ratings[validation], prior_median[validation]),
            "models": {name: {method: metrics(ratings[validation], fit_calibrator(values[train], ratings[train], method)(values[validation]))
                              for method in ("linear", "isotonic")}
                       for name, values in scores.items()},
        },
        "selected": {"formula": chosen_name, "calibration": chosen_method},
        "test": {
            "prior_mean": metrics(ratings[test], prior_mean[test]),
            "prior_median": metrics(ratings[test], prior_median[test]),
            "selected": metrics(ratings[test], chosen(scores[chosen_name][test])),
            "current_linear": metrics(ratings[test], fit_calibrator(current[fit], ratings[fit], "linear")(current[test])),
            "earlier_linear": metrics(ratings[test], fit_calibrator(earlier[fit], ratings[fit], "linear")(earlier[test])),
        },
        "limitations": "One historical reader; current embeddings may postdate reads; test among chosen books, not unseen candidates.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--backend")
    parser.add_argument("--model")
    args = parser.parse_args()
    records, pair, excluded = prepare(load_corpus(args.corpus), args.backend, args.model)
    result = evaluate(records)
    result.update({"backend": pair[0], "model": pair[1], "works": len(records), "excluded": excluded})
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
