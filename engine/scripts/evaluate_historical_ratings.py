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


def _auc(labels: np.ndarray, scores: np.ndarray) -> float | None:
    """Compute tie-aware AUC, returning None when either class is absent."""
    labels = np.asarray(labels, dtype=bool)
    scores = np.asarray(scores, dtype=np.float64)
    positive, negative = scores[labels], scores[~labels]
    if not len(positive) or not len(negative):
        return None
    return float(
        ((positive[:, None] > negative).sum()
         + 0.5 * (positive[:, None] == negative).sum())
        / (len(positive) * len(negative))
    )


def _score_deciles(ratings: np.ndarray, scores: np.ndarray) -> list[dict[str, float | int | None]]:
    """Summarize equally sized ascending-score cohorts; ties keep input order."""
    ratings = np.asarray(ratings, dtype=np.float64)
    scores = np.asarray(scores, dtype=np.float64)
    order = np.argsort(scores, kind="stable")
    deciles = []
    for index, cohort in enumerate(np.array_split(order, 10), start=1):
        count = int(len(cohort))
        cohort_ratings = ratings[cohort]
        high_count = int(np.sum(cohort_ratings >= 4))
        low_count = int(np.sum(cohort_ratings <= 2))
        deciles.append({
            "decile": index,
            "count": count,
            "score_min": float(scores[cohort].min()) if count else None,
            "score_max": float(scores[cohort].max()) if count else None,
            "mean_rating": float(cohort_ratings.mean()) if count else None,
            "high_4_or_5_count": high_count,
            "high_4_or_5_fraction": float(high_count / count) if count else None,
            "low_1_or_2_count": low_count,
            "low_1_or_2_fraction": float(low_count / count) if count else None,
        })
    return deciles


def raw_ranking_metrics(ratings, scores):
    """Evaluate ordering directly from raw ranker scores, without calibration."""
    ratings = np.asarray(ratings, dtype=np.float64)
    scores = np.asarray(scores, dtype=np.float64)
    if len(ratings) != len(scores):
        raise ValueError("ratings and scores must have equal lengths")
    if not len(ratings):
        return {
            "count": 0,
            "spearman": None,
            "graded_ndcg_at_20": None,
            "auc_four_or_five_vs_one_to_three": None,
            "auc_one_or_two_vs_three_to_five": None,
            "auc_high_vs_low_excluding_neutral": None,
            "high_4_or_5_count": 0,
            "low_1_or_2_count": 0,
            "high_rated_in_top_20": 0,
            "low_rated_in_bottom_20": 0,
            "score_deciles": _score_deciles(ratings, scores),
        }

    order = np.argsort(-scores, kind="stable")
    top = order[:min(20, len(order))]
    bottom = order[-min(20, len(order)):]
    gains = 2 ** (ratings - 1) - 1
    discounts = 1 / np.log2(np.arange(2, len(top) + 2))
    ideal = np.sort(gains)[::-1][:len(top)] @ discounts
    rho = (
        spearmanr(ratings, scores).statistic
        if len(np.unique(ratings)) > 1 and len(np.unique(scores)) > 1
        else None
    )
    high = ratings >= 4
    low = ratings <= 2
    non_neutral = high | low
    return {
        "count": int(len(ratings)),
        "spearman": float(rho) if rho is not None and np.isfinite(rho) else None,
        "graded_ndcg_at_20": float(gains[top] @ discounts / ideal) if ideal else None,
        "auc_four_or_five_vs_one_to_three": _auc(high, scores),
        # Low preference means a low raw score, so reverse the score direction.
        "auc_one_or_two_vs_three_to_five": _auc(low, -scores),
        # This sensitivity compares only clear high and low ratings, omitting 3★.
        "auc_high_vs_low_excluding_neutral": _auc(high[non_neutral], scores[non_neutral]),
        "high_4_or_5_count": int(high.sum()),
        "low_1_or_2_count": int(low.sum()),
        "high_rated_in_top_20": int(high[top].sum()),
        "low_rated_in_bottom_20": int(low[bottom].sum()),
        "score_deciles": _score_deciles(ratings, scores),
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
    raw_ranking = {
        "validation": {
            name: raw_ranking_metrics(ratings[validation], values[validation])
            for name, values in scores.items()
        },
        "test": {
            name: raw_ranking_metrics(ratings[test], values[test])
            for name, values in scores.items()
        },
        "cohort_definition": (
            "Score deciles are ascending, equally sized rank cohorts; ties preserve input order. "
            "High means 4–5 stars and low means 1–2 stars. The primary high AUC treats 1–3 stars "
            "as negatives, the primary low AUC treats 3–5 stars as negatives, and the sensitivity "
            "high-vs-low AUC omits 3-star books; high and low class counts are reported."
        ),
    }
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
        "raw_ranking": raw_ranking,
        "legacy_metrics_note": (
            "The existing validation/test model metrics are calculated on calibrated 1–5 star predictions. "
            "Use raw_ranking for discrimination and ordering from uncalibrated 0–100 scorer outputs."
        ),
        "limitations": (
            "One historical reader; the replay treats each eventual rating as available on its read date, "
            "although imports/upserts do not retain a reliable historical rating-availability timestamp. "
            "Current embeddings may postdate reads. Held-out books were chosen by the reader; NDCG and "
            "top/bottom cohorts compare synthetic time-block groups, not contemporaneous recommendation slates "
            "or unseen-candidate retrieval."
        ),
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
