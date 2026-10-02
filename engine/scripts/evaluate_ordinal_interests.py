#!/usr/bin/env python3
"""Aggregate-only causal evaluation of small ordinal and interest-neighborhood rankers."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
import warnings
from bisect import bisect_left
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.optimize import minimize
from scipy.special import expit, logit
from scipy.stats import rankdata
from sklearn.cluster import KMeans
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import Ridge
from threadpoolctl import threadpool_limits

ENGINE = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(ENGINE))
from afterword_engine.identity import book_identity
from afterword_engine.ranking import (
    _KERNEL_ESS_PRIOR,
    _KERNEL_FULL_HISTORY,
    _KERNEL_NEIGHBORS,
    _KERNEL_POWER,
    _KERNEL_SCORE_PER_STAR,
    _RECENCY_SCORE_PER_STAR,
    _RECENCY_TIMESCALE_YEARS,
    _metadata_confidence,
    _read_day,
    _top_weighted,
    rank_candidates,
)
from evaluate_ranking import load_corpus, prepare
from evaluate_historical_ratings import whole_day_cuts

BASE_NAMES = (
    'positive_top5_cosine', 'positive_max_cosine', 'negative_max_cosine',
    'negative_top5_cosine', 'local_neighbor_rating_delta', 'kernel_rating_delta',
    'recency_rating_delta', 'same_author_rating_delta', 'reader_mean_delta',
    'log_history_count',
)
ORDINAL_NAMES = BASE_NAMES
INTEREST_NAMES = BASE_NAMES + ('multi_interest_rating_delta', 'matched_interest_negative_cosine')
MIN_HISTORY = 100
PRIOR_BOOKS = 5.0


def normalize(vectors):
    arr = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    return np.divide(arr, norms, out=np.zeros_like(arr), where=norms != 0)


def causal_history_end(dates, query_day):
    """Index of the first read not strictly earlier than ``query_day``."""
    return bisect_left(dates, query_day)


def history_components(history, history_vectors, query, query_vectors):
    """Compute interpretable per-target components and exact serving scores."""
    hvec = normalize(history_vectors)
    qvec = normalize(query_vectors)
    if hvec.size and qvec.shape[1] != hvec.shape[1]:
        raise ValueError('embedding dimensions do not match')
    sims = qvec @ hvec.T if len(history) else np.empty((len(query), 0), dtype=np.float32)
    ratings = np.asarray([float(item.get('rating') or 0) for item in history], dtype=np.float64)
    days = np.asarray([_read_day(item.get('read_at')).toordinal() if _read_day(item.get('read_at')) else np.nan
                       for item in history], dtype=np.float64)
    positive = ratings >= 4
    negative = (ratings > 0) & (ratings <= 2)
    global_mean = float(ratings.mean()) if len(ratings) else 3.0
    author_ratings = defaultdict(list)
    for item, rating in zip(history, ratings):
        author_ratings[book_identity('', item.get('author', ''))].append(float(rating))
    X = np.zeros((len(query), len(BASE_NAMES)), dtype=np.float64)
    baseline_scores = np.zeros(len(query), dtype=np.float64)
    for row, candidate in enumerate(query):
        sim = sims[row].astype(np.float64)
        pos, neg = sim[positive], sim[negative]
        pos_top = float(_top_weighted(pos[None, :], default=.25)[0]) if len(pos) else .25
        pos_max = float(np.max(pos)) if len(pos) else .25
        neg_max = float(np.max(neg)) if len(neg) else 0.0
        neg_top = float(_top_weighted(neg[None, :], default=0.0)[0]) if len(neg) else 0.0
        if len(sim) and np.any(sim > 0):
            neighbors = np.argsort(-sim, kind='stable')[:min(_KERNEL_NEIGHBORS, len(sim))]
            weights = (np.maximum(sim[neighbors], 0.0) + 1e-8) ** _KERNEL_POWER
            kernel = float(np.dot(weights, ratings[neighbors]) / weights.sum())
            effective_sample_size = float(weights.sum() ** 2 / np.dot(weights, weights))
            neighbor_days = days[neighbors]
            known = np.isfinite(neighbor_days)
            if known.any():
                ages = np.maximum(0.0, (neighbor_days[known].max() - neighbor_days) / 365.25)
                ages[~known] = np.median(ages[known])
                recent_weights = weights * np.exp(-ages / 8.0)
                recent = float(np.dot(recent_weights, ratings[neighbors]) / recent_weights.sum())
            else:
                recent = kernel
            local = neighbors[:min(5, len(neighbors))]
            local_weights = np.maximum(sim[local], 0.0) + 1e-4
            local_rating = float(np.dot(local_weights, ratings[local]) / local_weights.sum())
        else:
            kernel = recent = global_mean
            local_rating = 3.0
            effective_sample_size = 0.0
        author = book_identity('', candidate.get('author', ''))
        values = author_ratings.get(author, [])
        author_mean = float(np.mean(values)) if values else global_mean
        author_delta = ((len(values) * author_mean + 5 * global_mean) / (len(values) + 5) - global_mean) if values else 0.0
        count = len(history)
        kernel_strength = _KERNEL_SCORE_PER_STAR * min(1.0, count / _KERNEL_FULL_HISTORY)
        recency_strength = _RECENCY_SCORE_PER_STAR * min(1.0, count / _KERNEL_FULL_HISTORY)
        X[row] = (pos_top, pos_max, neg_max, neg_top, local_rating - 3.0,
                  kernel - global_mean, recent - kernel, author_delta,
                  global_mean - 3.0, np.log1p(count))
        raw_score = (42.0 + 48.0 * pos_top - 24.0 * neg_max
                     + 3.5 * (local_rating - 3.0) + 3.5 * author_delta
                     + kernel_strength * (kernel - global_mean)
                     * effective_sample_size / (effective_sample_size + _KERNEL_ESS_PRIOR)
                     + recency_strength * (recent - kernel)
                     + (float(candidate.get('source_weight') or 1.0) - 1.0) * 5.0)
        confidence = _metadata_confidence(candidate)
        baseline_scores[row] = np.clip(50.0 + confidence * (raw_score - 50.0), 0.0, 100.0)
    return X, baseline_scores


class PrefixInterests:
    """K-means neighborhoods fit on the first eligible prefix, then causally extended."""
    def __init__(self, k):
        self.k = int(k)
        self.model = None
        self.centers = None
        self.labels_by_index = None
        self.started_at = None
        self.fit_seconds = 0.0
        self.assign_seconds = 0.0
        self.score_seconds = []

    def _predict(self, vectors):
        # KMeans uses Euclidean distance over unit vectors.
        v = np.asarray(vectors, dtype=np.float32)
        center = self.model.cluster_centers_.astype(np.float32, copy=False)
        dist = (np.sum(v * v, axis=1, keepdims=True)
                - 2.0 * (v @ center.T)
                + np.sum(center * center, axis=1)[None, :])
        return np.argmin(dist, axis=1)

    def ensure_started(self, history_indexes, vectors):
        if self.model is not None:
            return
        if len(history_indexes) < max(MIN_HISTORY, self.k):
            return
        start = time.perf_counter()
        seed = np.asarray(vectors[history_indexes], dtype=np.float32)
        with threadpool_limits(limits=1):
            model = KMeans(n_clusters=self.k, n_init=3, max_iter=50,
                           random_state=20260930, algorithm='lloyd')
            with warnings.catch_warnings():
                warnings.simplefilter('ignore')
                labels = model.fit_predict(seed)
        self.model = model
        self.labels_by_index = np.full(len(vectors), -1, dtype=np.int16)
        self.labels_by_index[np.asarray(history_indexes, dtype=np.int64)] = labels.astype(np.int16)
        self.fit_seconds += time.perf_counter() - start
        self.started_at = int(len(history_indexes))

    def features(self, history_indexes, ratings, query_vectors, query_history_sims, history_items):
        """Return query-conditioned, shrunk evidence across the learned neighborhoods."""
        n_query = len(query_vectors)
        result = np.zeros((n_query, 2), dtype=np.float64)
        if self.model is None or not n_query:
            return result
        start = time.perf_counter()
        idx = np.asarray(history_indexes, dtype=np.int64)
        labels = self.labels_by_index[idx]
        if np.any(labels < 0):
            raise RuntimeError('interest history omitted a prior day')
        center = normalize(self.model.cluster_centers_)
        q = normalize(query_vectors)
        affinity = np.maximum(q @ center.T, 0.0)
        d = (np.sum(q * q, axis=1, keepdims=True)
             - 2.0 * (q @ self.model.cluster_centers_.astype(np.float32).T)
             + np.sum(self.model.cluster_centers_.astype(np.float32) ** 2, axis=1)[None, :])
        nearest = np.argmin(d, axis=1)
        hist_ratings = np.asarray([float(history_items[i].get('rating') or 0) for i in idx], dtype=np.float64)
        global_mean = float(hist_ratings.mean()) if len(hist_ratings) else 3.0
        cluster_deltas = np.zeros(self.k, dtype=np.float64)
        cluster_counts = np.zeros(self.k, dtype=np.int64)
        for c in range(self.k):
            members = labels == c
            n = int(members.sum())
            cluster_counts[c] = n
            if n:
                mean = float(hist_ratings[members].mean())
                cluster_deltas[c] = n / (n + PRIOR_BOOKS) * (mean - global_mean)
        neg = hist_ratings <= 2
        for i in range(n_query):
            weights = affinity[i] ** 8
            if not np.any(weights):
                weights[nearest[i]] = 1.0
            weights = weights / weights.sum()
            interest_rating = float(np.dot(weights, cluster_deltas))
            c = int(nearest[i])
            in_cluster = (labels == c) & neg
            negative_sim = float(np.max(query_history_sims[i, in_cluster])) if in_cluster.any() else 0.0
            result[i] = (interest_rating, negative_sim)
        self.score_seconds.append((time.perf_counter() - start) / max(n_query, 1))
        return result

    def append_day(self, indexes, vectors):
        if self.model is None or not len(indexes):
            return
        start = time.perf_counter()
        labels = self._predict(vectors[indexes])
        self.labels_by_index[np.asarray(indexes, dtype=np.int64)] = labels.astype(np.int16)
        self.assign_seconds += time.perf_counter() - start


def extract_features(records, with_interests=(3, 5)):
    dates = [row[0].date() for row in records]
    items = [row[1] for row in records]
    raw_vectors = np.stack([row[2] for row in records]).astype(np.float32)
    vectors = normalize(raw_vectors)
    ratings = np.asarray([float(item['rating']) for item in items], dtype=np.float64)
    base = np.full((len(items), len(BASE_NAMES)), np.nan, dtype=np.float64)
    baseline = np.full(len(items), np.nan, dtype=np.float64)
    interest = {k: np.full((len(items), 2), np.nan, dtype=np.float64) for k in with_interests}
    parity_count = parity_mismatches = 0
    parity_max = 0.0
    clusterers = {k: PrefixInterests(k) for k in with_interests}
    start = 0
    replay_start = time.perf_counter()
    while start < len(records):
        history_end = causal_history_end(dates, dates[start])
        stop = start + 1
        while stop < len(records) and dates[stop] == dates[start]:
            stop += 1
        if history_end >= MIN_HISTORY:
            history_indexes = np.asarray(sorted(range(history_end), key=lambda i: items[i].get('id', 0)), dtype=np.int64)
            query_indexes = np.arange(start, stop, dtype=np.int64)
            history = [items[i] for i in history_indexes]
            queries = [items[i] for i in query_indexes]
            X, scores = history_components(history, raw_vectors[history_indexes], queries, raw_vectors[query_indexes])
            base[query_indexes] = X
            baseline[query_indexes] = np.round(scores, 1)
            exact = rank_candidates(history, raw_vectors[history_indexes], queries, raw_vectors[query_indexes])
            exact_scores = np.asarray([row['score'] for row in exact], dtype=np.float64)
            err = np.abs(baseline[query_indexes] - exact_scores)
            parity_count += len(err)
            parity_mismatches += int(np.sum(err > 1e-8))
            parity_max = max(parity_max, float(err.max(initial=0.0)))
            sims = vectors[query_indexes] @ vectors[history_indexes].T
            for k, state in clusterers.items():
                state.ensure_started(history_indexes, vectors)
                if state.model is not None:
                    interest[k][query_indexes] = state.features(
                        history_indexes, ratings, vectors[query_indexes], sims, items)
            for state in clusterers.values():
                state.append_day(query_indexes, vectors)
        start = stop
    elapsed = time.perf_counter() - replay_start
    details = {
        'replay_wall_seconds': float(elapsed),
        'exact_ranker_parity': {'compared_predictions': int(parity_count),
                                'mismatches_at_served_precision': int(parity_mismatches),
                                'max_score_error_points': float(parity_max)},
        'interest_runtime': {},
    }
    for k, state in clusterers.items():
        details['interest_runtime'][str(k)] = {
            'kmeans_fit_seconds': float(state.fit_seconds),
            'causal_assignment_seconds': float(state.assign_seconds),
            'median_query_feature_seconds': float(np.median(state.score_seconds)) if state.score_seconds else None,
            'p95_query_feature_seconds': float(np.quantile(state.score_seconds, .95)) if state.score_seconds else None,
            'fit_prefix_size': state.started_at,
        }
    return items, vectors, ratings, dates, base, baseline, interest, details


def fit_ordinal(X, y, ridge=0.1):
    """Cumulative-logit proportional-odds model with shared ordered cutpoints."""
    X = np.asarray(X, dtype=np.float64)
    y_values = np.asarray(y, dtype=np.float64)
    if X.ndim != 2 or y_values.ndim != 1 or len(X) != len(y_values) or len(y_values) == 0:
        raise ValueError('ordinal model needs a non-empty feature matrix')
    if (not np.isfinite(y_values).all() or np.any(y_values < 1) or np.any(y_values > 5)
            or np.any(y_values != np.floor(y_values))):
        raise ValueError('ratings must be finite integer stars from 1 through 5')
    y = y_values.astype(np.int64)
    mean = X.mean(axis=0)
    scale = X.std(axis=0)
    scale[scale < 1e-8] = 1.0
    Z = (X - mean) / scale
    counts = np.bincount(y, minlength=6)[1:6].astype(np.float64)
    if np.any(counts == 0):
        raise ValueError('training prefix must contain all five rating levels')
    cumulative = np.cumsum(counts)[:-1] / len(y)
    cuts = logit(np.clip(cumulative, 1e-4, 1 - 1e-4))
    diffs = np.maximum(np.diff(cuts) - 1e-3, 1e-3)
    gaps = np.where(diffs > 30, diffs, np.log(np.expm1(diffs)))
    initial = np.concatenate([np.zeros(X.shape[1]), [cuts[0]], gaps])

    def unpack(params):
        beta = params[:X.shape[1]]
        first = params[X.shape[1]]
        increments = np.logaddexp(0.0, params[X.shape[1] + 1:]) + 1e-3
        thresholds = np.concatenate([[first], first + np.cumsum(increments)])
        return beta, thresholds

    target = y - 1
    def objective(params):
        beta, thresholds = unpack(params)
        eta = Z @ beta
        cdf = expit(thresholds[None, :] - eta[:, None])
        probs = np.column_stack((cdf[:, :1], np.diff(cdf, axis=1), 1.0 - cdf[:, -1:]))
        chosen = np.clip(probs[np.arange(len(y)), target], 1e-12, 1.0)
        return float(-np.log(chosen).sum() + 0.5 * ridge * np.dot(beta, beta))

    result = minimize(objective, initial, method='L-BFGS-B', options={'maxiter': 1200, 'ftol': 1e-10, 'gtol': 1e-6})
    if not np.isfinite(result.fun) or not np.isfinite(result.x).all():
        raise RuntimeError('ordinal fit did not produce finite parameters')
    if not result.success:
        raise RuntimeError(f'ordinal fit did not converge: {result.message}')
    beta, thresholds = unpack(result.x)
    return {'mean': mean, 'scale': scale, 'beta': beta, 'thresholds': thresholds,
            'optimizer_success': bool(result.success), 'optimizer_iterations': int(result.nit),
            'objective': float(result.fun), 'train_rows': int(len(y)), 'ridge': float(ridge)}


def ordinal_scores(model, X):
    Z = (np.asarray(X, dtype=np.float64) - model['mean']) / model['scale']
    return Z @ model['beta']


def ordinal_expected_ratings(model, X):
    latent = ordinal_scores(model, X)
    probs = ordinal_probabilities(model, latent)
    return probs @ np.arange(1, 6, dtype=np.float64)


def ordinal_probabilities(model, latent):
    """Five ordered-logit probabilities for the supplied latent scores."""
    latent = np.asarray(latent, dtype=np.float64).reshape(-1)
    cdf = expit(model['thresholds'][None, :] - latent[:, None])
    probs = np.column_stack((cdf[:, :1], np.diff(cdf, axis=1), 1.0 - cdf[:, -1:]))
    probs = np.maximum(probs, 0.0)
    probs /= probs.sum(axis=1, keepdims=True)
    return probs


def auc(labels, scores):
    labels = np.asarray(labels, dtype=bool)
    scores = np.asarray(scores, dtype=np.float64)
    positives, negatives = int(labels.sum()), int((~labels).sum())
    if not positives or not negatives:
        return None
    ranks = rankdata(scores, method='average')
    return float((ranks[labels].sum() - positives * (positives + 1) / 2) / (positives * negatives))


def metric_set(y, scores, calibrated=None):
    y = np.asarray(y, dtype=np.float64)
    scores = np.asarray(scores, dtype=np.float64)
    n = len(y)
    if not n:
        return {'n': 0}
    high, low = y >= 4, y <= 2
    clear = high | low
    # Match raw_ranking_metrics: ties share one descending stable order.
    order = np.argsort(-scores, kind='stable')
    top = order[:min(20, n)]
    bottom = order[-min(20, n):]
    out = {
        'n': int(n), 'high_count': int(high.sum()), 'low_count': int(low.sum()),
        'auc_high_4plus': auc(high, scores),
        'auc_low_2or_less_reversed': auc(low, -scores),
        'auc_high_vs_low_excluding_neutral': auc(high[clear], scores[clear]),
        'top20_high_count': int(high[top].sum()),
        'top20_high_rate': float(high[top].mean()),
        'bottom20_low_count': int(low[bottom].sum()),
        'bottom20_low_rate': float(low[bottom].mean()),
        'top20_mean_rating': float(y[top].mean()),
        'bottom20_mean_rating': float(y[bottom].mean()),
    }
    if calibrated is not None:
        out['calibrated_star_mae'] = float(np.mean(np.abs(np.asarray(calibrated) - y)))
    return out


def fit_calibrator(train_scores, train_y, validation_scores, validation_y):
    choices = []
    ridge = Ridge(alpha=10.0).fit(np.asarray(train_scores)[:, None], train_y)
    predict_linear = lambda values: np.clip(ridge.predict(np.asarray(values)[:, None]), 1, 5)
    choices.append((float(np.mean(np.abs(predict_linear(validation_scores) - validation_y))), 'linear'))
    iso = IsotonicRegression(y_min=1.0, y_max=5.0, out_of_bounds='clip').fit(train_scores, train_y)
    predict_iso = lambda values: iso.predict(np.asarray(values))
    choices.append((float(np.mean(np.abs(predict_iso(validation_scores) - validation_y))), 'isotonic'))
    method = min(choices, key=lambda x: (x[0], x[1]))[1]
    return method, {name: mae for mae, name in choices}


def calibrate(train_scores, train_y, scores, method):
    if method == 'linear':
        model = Ridge(alpha=10.0).fit(np.asarray(train_scores)[:, None], train_y)
        return np.clip(model.predict(np.asarray(scores)[:, None]), 1, 5)
    model = IsotonicRegression(y_min=1.0, y_max=5.0, out_of_bounds='clip').fit(train_scores, train_y)
    return model.predict(np.asarray(scores))


def expanding_ordinal_replay(records, dates, X, ratings, usable, train_stop, val_stop):
    """Fit once per query day using every strictly earlier causal example."""
    n = len(records)
    all_indexes = np.arange(n)
    latent = np.full(n, np.nan, dtype=np.float64)
    expected = np.full(n, np.nan, dtype=np.float64)
    reports = {'validation': [], 'test': []}
    start = train_stop
    while start < n:
        stop = start + 1
        while stop < n and dates[stop] == dates[start]:
            stop += 1
        history_end = causal_history_end(dates, dates[start])
        prior = np.flatnonzero(usable & (all_indexes < history_end))
        model_started = time.perf_counter()
        model = fit_ordinal(X[prior], ratings[prior])
        fit_seconds = time.perf_counter() - model_started
        query = np.arange(start, stop)
        latent[query] = ordinal_scores(model, X[query])
        expected[query] = ordinal_expected_ratings(model, X[query])
        period = 'validation' if start < val_stop else 'test'
        reports[period].append({'day': str(dates[start]), 'queries': int(len(query)),
                                'prior_examples': int(len(prior)), 'fit_seconds': float(fit_seconds),
                                'optimizer_iterations': int(model['optimizer_iterations']),
                                'optimizer_success': bool(model['optimizer_success'])})
        start = stop
    summaries = {}
    for period, rows in reports.items():
        train_counts = np.asarray([row['prior_examples'] for row in rows], dtype=np.int64)
        fit_times = np.asarray([row['fit_seconds'] for row in rows], dtype=np.float64)
        summaries[period] = {
            'calendar_day_fits': int(len(rows)),
            'queries_scored': int(sum(row['queries'] for row in rows)),
            'prior_examples_min': int(train_counts.min()) if len(rows) else None,
            'prior_examples_max': int(train_counts.max()) if len(rows) else None,
            'total_fit_seconds': float(fit_times.sum()) if len(rows) else 0.0,
            'median_fit_seconds_per_day': float(np.median(fit_times)) if len(rows) else None,
            'p95_fit_seconds_per_day': float(np.quantile(fit_times, .95)) if len(rows) else None,
            'all_optimizer_runs_succeeded': bool(all(row['optimizer_success'] for row in rows)),
            'optimizer_iterations_median': float(np.median([row['optimizer_iterations'] for row in rows])) if len(rows) else None,
        }
    return latent, expected, summaries


def bootstrap_deltas(y, base, candidate, iterations=2000, seed=20260930):
    rng = np.random.default_rng(seed)
    n = len(y)
    deltas = {'high': [], 'low': [], 'clear': []}
    y = np.asarray(y)
    base, candidate = np.asarray(base), np.asarray(candidate)
    for _ in range(iterations):
        ix = rng.integers(0, n, n)
        hi = auc(y[ix] >= 4, candidate[ix])
        hi0 = auc(y[ix] >= 4, base[ix])
        lo = auc(y[ix] <= 2, -candidate[ix])
        lo0 = auc(y[ix] <= 2, -base[ix])
        clear = (y[ix] >= 4) | (y[ix] <= 2)
        cl = auc(y[ix][clear] >= 4, candidate[ix][clear])
        cl0 = auc(y[ix][clear] >= 4, base[ix][clear])
        if hi is not None and hi0 is not None: deltas['high'].append(hi - hi0)
        if lo is not None and lo0 is not None: deltas['low'].append(lo - lo0)
        if cl is not None and cl0 is not None: deltas['clear'].append(cl - cl0)
    result = {}
    for name, values in deltas.items():
        result[f'{name}_auc_delta_95pct'] = np.quantile(values, [.025, .975]).tolist() if values else None
    result['bootstrap_resamples'] = int(iterations)
    result['bootstrap_unit'] = 'individual held-out books; temporal dependence is not captured'
    return result


def compare_slice(y, current_scores, candidate_scores):
    current = metric_set(y, current_scores)
    candidate = metric_set(y, candidate_scores)
    return {
        'current': current,
        'candidate': candidate,
        'delta_vs_current': {
            'high_auc': candidate['auc_high_4plus'] - current['auc_high_4plus'],
            'low_auc': candidate['auc_low_2or_less_reversed'] - current['auc_low_2or_less_reversed'],
            'clear_high_vs_low_auc': candidate['auc_high_vs_low_excluding_neutral'] - current['auc_high_vs_low_excluding_neutral'],
            'top20_high_count': candidate['top20_high_count'] - current['top20_high_count'],
            'bottom20_low_count': candidate['bottom20_low_count'] - current['bottom20_low_count'],
        },
    }


def temporal_halves(records, indexes, y, current_scores, candidate_scores):
    if not len(indexes):
        return {}
    dates = [records[i][0].date() for i in indexes]
    unique_dates = sorted(set(dates))
    mid = len(unique_dates) // 2
    boundary = unique_dates[mid]
    first = np.asarray([j for j, d in enumerate(dates) if d < boundary], dtype=np.int64)
    second = np.asarray([j for j, d in enumerate(dates) if d >= boundary], dtype=np.int64)
    return {'first_half': compare_slice(y[first], current_scores[first], candidate_scores[first]),
            'second_half': compare_slice(y[second], current_scores[second], candidate_scores[second]),
            'boundary_date': str(boundary)}


def author_slices(records, items, indexes, y, current_scores, candidate_scores):
    seen = []
    for index in indexes:
        author = book_identity('', items[index].get('author', ''))
        current_day = index
        known = any(
            records[j][0].date() < records[current_day][0].date()
            and book_identity('', items[j].get('author', '')) == author
            for j in range(index)
        )
        seen.append(known)
    seen = np.asarray(seen, dtype=bool)
    return {'author_seen': compare_slice(y[seen], current_scores[seen], candidate_scores[seen]),
            'author_unseen': compare_slice(y[~seen], current_scores[~seen], candidate_scores[~seen]),
            'author_unseen_count': int((~seen).sum())}


def paired_metrics(y, baseline_scores, candidate_scores, iterations):
    baseline_result = metric_set(y, baseline_scores)
    candidate_result = metric_set(y, candidate_scores)
    return {
        'candidate': candidate_result,
        'delta_vs_current': {
            'high_auc': candidate_result['auc_high_4plus'] - baseline_result['auc_high_4plus'],
            'low_auc': candidate_result['auc_low_2or_less_reversed'] - baseline_result['auc_low_2or_less_reversed'],
            'clear_high_vs_low_auc': candidate_result['auc_high_vs_low_excluding_neutral'] - baseline_result['auc_high_vs_low_excluding_neutral'],
            'top20_high_count': candidate_result['top20_high_count'] - baseline_result['top20_high_count'],
            'bottom20_low_count': candidate_result['bottom20_low_count'] - baseline_result['bottom20_low_count'],
        },
        'paired_book_bootstrap': bootstrap_deltas(y, baseline_scores, candidate_scores, iterations),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--corpus', required=True, type=Path,
                        help='private JSON or SQLite corpus snapshot; output contains aggregates only')
    parser.add_argument('--output', type=Path,
                        help='optional path for the aggregate JSON result (default: stdout)')
    parser.add_argument('--backend', default='ollama')
    parser.add_argument('--model', default='qwen3-embedding:4b')
    parser.add_argument('--bootstrap-iterations', type=int, default=2000)
    args = parser.parse_args()
    if args.bootstrap_iterations < 1:
        parser.error('--bootstrap-iterations must be positive')
    if args.output is not None and args.output.resolve() == args.corpus.resolve():
        parser.error('--output must not overwrite --corpus')
    with threadpool_limits(limits=1):
        records, selected, excluded = prepare(load_corpus(args.corpus), args.backend, args.model)
        items, vectors, ratings, dates, base_X, current, interest_X, runtime = extract_features(records)
        if runtime['exact_ranker_parity']['mismatches_at_served_precision']:
            raise RuntimeError('current score parity failed; refusing candidate evaluation')
        _, (train_stop, val_stop) = whole_day_cuts(records)
        all_idx = np.arange(len(records))
        train = np.flatnonzero(np.isfinite(current) & (all_idx < train_stop))
        validation = np.arange(train_stop, val_stop)
        test = np.arange(val_stop, len(records))
        if not np.isfinite(current[validation]).all() or not np.isfinite(current[test]).all():
            raise ValueError('chronological replay did not cover validation and test')
        candidates = {'ordinal_10': base_X}
        for k, X in interest_X.items():
            candidates[f'interests_k{k}'] = np.column_stack([base_X, X])
        fitted = {}
        validation_results = {'current': metric_set(ratings[validation], current[validation])}
        validation_balanced = {'current': float(np.mean([validation_results['current']['auc_high_4plus'], validation_results['current']['auc_low_2or_less_reversed']]))}
        for name, X in candidates.items():
            fit_started = time.perf_counter()
            model = fit_ordinal(X[train], ratings[train])
            validation_fit_seconds = time.perf_counter() - fit_started
            latent = ordinal_scores(model, X)
            method, calibration_mae = fit_calibrator(latent[train], ratings[train], latent[validation], ratings[validation])
            val_calibrated = calibrate(latent[train], ratings[train], latent[validation], method)
            ordinal_expected = ordinal_expected_ratings(model, X[validation])
            validation_results[name] = metric_set(ratings[validation], latent[validation], val_calibrated)
            validation_results[name]['ordinal_expected_star_mae_uncalibrated'] = float(np.mean(np.abs(ordinal_expected - ratings[validation])))
            validation_balanced[name] = float(np.mean([validation_results[name]['auc_high_4plus'], validation_results[name]['auc_low_2or_less_reversed']]))
            fitted[name] = {'train_model': model, 'latent': latent, 'calibration_method': method,
                            'calibration_validation_mae_by_method': calibration_mae,
                            'validation_fit_seconds': float(validation_fit_seconds)}
        expanding_latent, expanding_expected, expanding_runtime = expanding_ordinal_replay(
            records, dates, base_X, ratings, np.isfinite(current), train_stop, val_stop)
        if not np.isfinite(expanding_expected[validation]).all() or not np.isfinite(expanding_expected[test]).all():
            raise ValueError('expanding ordinal replay did not cover validation and test')
        # Independently refitted latent scales are not comparable across days.
        # Expected ratings map each day's posterior onto the same 1–5 scale.
        expanding_validation = metric_set(ratings[validation], expanding_expected[validation])
        expanding_validation['ordinal_expected_star_mae'] = float(
            np.mean(np.abs(expanding_expected[validation] - ratings[validation])))
        expanding_validation['balanced_high_low_auc'] = float(np.mean([
            expanding_validation['auc_high_4plus'], expanding_validation['auc_low_2or_less_reversed']]))
        # Family selection is locked to validation: option 1 has one fixed model; option 4 chooses k=3 or k=5.
        ordinal_choice = 'ordinal_10'
        interest_choice = max(('interests_k3', 'interests_k5'), key=lambda name: validation_balanced[name])
        chosen = {'option_1_ordinal': ordinal_choice, 'option_4_interest': interest_choice}
        # Keep a family winner only when it beats the reference balanced AUC on validation.
        selected_for_test = {family: (name if validation_balanced[name] > validation_balanced['current'] else 'current')
                             for family, name in chosen.items()}
        fit = np.concatenate([train, validation])
        test_results = {'current': metric_set(ratings[test], current[test])}
        test_details = {}
        for family, name in selected_for_test.items():
            if name == 'current':
                test_details[family] = {'selected_on_validation': name,
                                        'validation_balanced_auc': validation_balanced[chosen[family]],
                                        'status': 'current scored as validation winner; no candidate evaluated on test'}
                continue
            X = candidates[name]
            refit_started = time.perf_counter()
            test_model = fit_ordinal(X[fit], ratings[fit])
            refit_seconds = time.perf_counter() - refit_started
            scores = ordinal_scores(test_model, X[test])
            # Calibration family (linear/isotonic) is selected on validation and frozen; refit on prior-to-test labels.
            cal = calibrate(ordinal_scores(test_model, X[fit]), ratings[fit], scores,
                            fitted[name]['calibration_method'])
            result = paired_metrics(ratings[test], current[test], scores, args.bootstrap_iterations)
            result['candidate']['calibrated_star_mae'] = float(np.mean(np.abs(cal - ratings[test])))
            result['candidate']['ordinal_expected_star_mae_uncalibrated'] = float(np.mean(np.abs(ordinal_expected_ratings(test_model, X[test]) - ratings[test])))
            result['temporal_halves'] = temporal_halves(records, test, ratings[test], current[test], scores)
            result['author_slices'] = author_slices(records, items, test, ratings[test], current[test], scores)
            result['test_model'] = {'train_rows': test_model['train_rows'], 'optimizer_success': test_model['optimizer_success'],
                                    'optimizer_iterations': test_model['optimizer_iterations'], 'ridge': test_model['ridge'],
                                    'refit_seconds': float(refit_seconds)}
            test_results[name] = result['candidate']
            test_details[family] = {'selected_on_validation': name,
                                    'validation_balanced_auc': validation_balanced[name],
                                    'current_validation_balanced_auc': validation_balanced['current'],
                                    'paired_test_comparison': result}
        expanding_test = paired_metrics(ratings[test], current[test], expanding_expected[test], args.bootstrap_iterations)
        expanding_test['candidate']['ordinal_expected_star_mae'] = float(
            np.mean(np.abs(expanding_expected[test] - ratings[test])))
        expanding_test['temporal_halves'] = temporal_halves(
            records, test, ratings[test], current[test], expanding_expected[test])
        expanding_test['author_slices'] = author_slices(
            records, items, test, ratings[test], current[test], expanding_expected[test])
        current_cal_method, current_mae = fit_calibrator(current[train], ratings[train], current[validation], ratings[validation])
        current_test_cal = calibrate(current[fit], ratings[fit], current[test], current_cal_method)
        test_results['current']['calibrated_star_mae'] = float(np.mean(np.abs(current_test_cal - ratings[test])))
        output = {
            'study': 'Bookward ordinal preference and multiple-interest neighborhoods',
            'data': {'snapshot_sha256': hashlib.sha256(args.corpus.read_bytes()).hexdigest(),
                     'embedding': list(selected), 'usable_records': int(len(records)),
                     'excluded': excluded, 'feature_coverage': int(np.isfinite(current).sum()),
                     'whole_day_split': {'train': int(len(train)), 'validation': int(len(validation)), 'test': int(len(test)),
                                         'train_end_day': str(dates[train_stop - 1]),
                                         'validation_end_day': str(dates[val_stop - 1]),
                                         'test_start_day': str(dates[val_stop])}},
            'protocol': 'Chronological 60/20/20 whole-day boundaries; each query uses strictly earlier calendar days; same-day targets are hidden together; exact current serving score parity required; learned models train on causal examples from the train window with train-only standardization; model families and calibration are selected on validation; a selected model is refit on train+validation before one later-period evaluation.',
            'current_score_formula': 'Current serving ranker, with the kernel rating adjustment multiplied by effective_sample_size / (effective_sample_size + 5); recency is unchanged.',
            'feature_note': 'The kernel_rating_delta model input is the raw component. The current score reference uses the ESS5-shrunk adjustment and exact serving-ranker parity is checked separately.',
            'labels': {'high': '4-5 stars versus 1-3', 'low': '1-2 stars versus 3-5', 'neutral_sensitivity': 'high versus low, excluding 3-star ratings'},
            'features': {'ordinal_10': list(ORDINAL_NAMES), 'interests_k3_or_k5': list(INTEREST_NAMES),
                         'interest_definition': 'K-means is fit once on the first strictly earlier 100-read prefix using unit title/author embeddings; each later read is assigned to the frozen nearest center only after its day is scored. Candidate evidence combines shrunk cluster rating deltas across all neighborhoods using query-center cosine weights and the nearest neighborhood low-rating cosine.'},
            'ordinal_model': {'family': 'proportional-odds cumulative logistic regression with shared ordered thresholds',
                              'regularization': 'L2 beta penalty 0.1', 'optimizer': 'scipy L-BFGS-B',
                              'ordinal_calibration': 'train-fitted ordinal expected-star MAE is reported; separate linear-vs-isotonic score calibration is selected on validation for calibrated-star MAE'},
            'current_exact_serving_parity': runtime['exact_ranker_parity'],
            'runtime_seconds': runtime,
            'expanding_refit_runtime_seconds': expanding_runtime,
            'runtime_scope': 'Includes chronological feature replay and exact serving-ranker parity calls. Interest timing covers causal k-means initialization, assigning each newly available read, and interest-specific per-query features; per-query figures exclude the shared history-cosine matrix, database access, and embedding generation. Ordinal fit timings are reported by model below.',
            'validation': {'metrics': validation_results, 'balanced_high_low_auc': validation_balanced,
                           'post_initial_diagnostic_expanding_refit': {
                               'model': 'same 10 features and ridge 0.1; refit on all strictly earlier causal examples for each calendar day; pooled ranking uses posterior expected ratings on a common 1–5 scale, not daily latent scores; not included in primary variant selection',
                               'metrics': expanding_validation,
                               'model_fit_runtime_seconds': expanding_runtime['validation']},
                           'current_calibration': {'method': current_cal_method, 'validation_mae_by_method': current_mae},
                           'ordinal_calibration': {name: {'method': fitted[name]['calibration_method'],
                                                          'validation_mae_by_method': fitted[name]['calibration_validation_mae_by_method'],
                                                          'fit_seconds': fitted[name]['validation_fit_seconds']}
                                                   for name in fitted},
                           'family_choices': chosen, 'selected_for_test': selected_for_test},
            'test': {'current': test_results['current'], 'family_comparisons': test_details,
                     'post_initial_diagnostic_expanding_refit': {
                         'model': 'same 10 features and ridge 0.1; refit on all strictly earlier causal examples for each calendar day; pooled ranking uses posterior expected ratings on a common 1–5 scale, not daily latent scores; held-out outcomes become training data only after their day',
                         'paired_comparison': expanding_test,
                         'model_fit_runtime_seconds': expanding_runtime['test']}},
            'limits': 'One retrospective reader-selected history; book-bootstrap intervals ignore temporal and reader dependence; embeddings and eventual ratings may postdate read dates; no evidence here covers unseen-candidate retrieval or live policy effects. The later period has been inspected in previous studies and is not independent confirmation. Ordinal calibration uses in-sample model scores for fitting its mapping; later MAE is out of sample but calibration training can be optimistic.'
        }
        rendered = json.dumps(output, indent=2, allow_nan=False) + '\n'
        if args.output is None:
            print(rendered, end='')
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered)
            print(json.dumps({
                'output_written': True,
                'exact_ranker_parity': runtime['exact_ranker_parity'],
                'validation_balanced_high_low_auc': validation_balanced,
                'test_candidate_families': sorted(test_details),
            }, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
