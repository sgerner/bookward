#!/usr/bin/env python3
"""Compare bounded ranking alternatives using a private corpus export.

The script prints aggregate metrics only. It never emits book titles or raw rows.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from bisect import bisect_left
from collections import defaultdict
from pathlib import Path

import numpy as np
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import Ridge


ENGINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE_ROOT / 'scripts'))
sys.path.insert(0, str(ENGINE_ROOT))

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
from evaluate_historical_ratings import raw_ranking_metrics, whole_day_cuts
from evaluate_ranking import baseline, content_hash, document, load_corpus, prepare, read_time

VARIANTS = ('current', 'kernel_unshrunk', 'author_stronger', 'kernel_ess2', 'kernel_ess5', 'cosine_centered', 'negative_top5')
HYPOTHESES = {
    'current': 'Deployed formula: kernel_unshrunk with only its kernel rating adjustment multiplied by effective_sample_size / (effective_sample_size + 5).',
    'kernel_unshrunk': 'Former deployed formula; the kernel rating adjustment has no effective-sample-size shrinkage.',
    'author_stronger': 'Starting from kernel_unshrunk, increase the author contribution and shrink author means with 3 prior books instead of 5.',
    'kernel_ess2': 'Starting from kernel_unshrunk, shrink the kernel rating adjustment by effective_sample_size / (effective_sample_size + 2).',
    'kernel_ess5': 'Starting from kernel_unshrunk, shrink the kernel rating adjustment by effective_sample_size / (effective_sample_size + 5); this equals current.',
    'cosine_centered': 'Starting from kernel_unshrunk, center each query cosine around its mean history similarity, keeping 0.25 as the neutral reference.',
    'negative_top5': 'Starting from kernel_unshrunk, replace the maximum low-rated neighbor penalty with a similarity-weighted mean of the top five.',
}
MIN_HISTORY = 100


def normalized(vectors):
    values = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(values, axis=1, keepdims=True)
    return np.divide(values, norms, out=np.zeros_like(values), where=norms != 0)


def scores_for_group(history, history_vectors, queries, query_vectors):
    """Return unrounded scores for predeclared formulas on one query day."""
    hvec = normalized(history_vectors)
    qvec = normalized(query_vectors)
    if hvec.size and qvec.shape[1] != hvec.shape[1]:
        raise ValueError('embedding dimensions do not match')
    sims = qvec @ hvec.T if len(history) else np.empty((len(queries), 0), dtype=np.float32)
    ratings = np.asarray([float(x.get('rating') or 0) for x in history], dtype=np.float64)
    read_days = np.asarray([_read_day(x.get('read_at')).toordinal() if _read_day(x.get('read_at')) else np.nan for x in history], dtype=np.float64)
    positive_mask = ratings >= 4
    negative_mask = (ratings > 0) & (ratings <= 2)
    global_mean = float(ratings.mean()) if len(ratings) else 3.0
    author_ratings = defaultdict(list)
    for item, rating in zip(history, ratings):
        author_ratings[book_identity('', item.get('author', ''))].append(float(rating))
    base_authors = {key: values for key, values in author_ratings.items()}
    outputs = {key: np.empty(len(queries), dtype=np.float64) for key in VARIANTS}

    for row, candidate in enumerate(queries):
        sim = sims[row].astype(np.float64)
        centered_sim = np.clip(sim - (float(sim.mean()) if len(sim) else 0.0) + .25, 0.0, 1.0) if len(sim) else sim
        author = book_identity('', candidate.get('author', ''))
        values = base_authors.get(author, [])
        author_mean = float(np.mean(values)) if values else global_mean
        author_delta = ((len(values) * author_mean + 5 * global_mean) / (len(values) + 5) - global_mean) if values else 0.0
        stronger_author_delta = ((len(values) * author_mean + 3 * global_mean) / (len(values) + 3) - global_mean) if values else 0.0
        if len(sim) and np.any(sim > 0):
            neighbors = np.argsort(-sim, kind='stable')[:min(_KERNEL_NEIGHBORS, len(sim))]
            similarities = np.maximum(sim[neighbors], 0)
            kernel_weights = (similarities + 1e-8) ** _KERNEL_POWER
            kernel_rating = float(np.dot(kernel_weights, ratings[neighbors]) / kernel_weights.sum())
            days = read_days[neighbors]
            known = np.isfinite(days)
            if known.any():
                ages = np.maximum(0, (days[known].max() - days) / 365.25)
                ages[~known] = np.median(ages[known])
                recent_weights = kernel_weights * np.exp(-ages / _RECENCY_TIMESCALE_YEARS)
                recent_rating = float(np.dot(recent_weights, ratings[neighbors]) / recent_weights.sum())
            else:
                recent_rating = kernel_rating
            local = neighbors[:min(5, len(neighbors))]
            local_weights = np.maximum(sim[local], 0) + 1e-4
            local_rating = float(np.dot(local_weights, ratings[local]) / local_weights.sum())
            ess = float(kernel_weights.sum() ** 2 / np.dot(kernel_weights, kernel_weights))
        else:
            neighbors = np.empty(0, dtype=np.int64)
            kernel_weights = np.empty(0, dtype=np.float64)
            kernel_rating = recent_rating = global_mean
            local_rating = 3.0
            ess = 0.0
        if len(sim):
            positive = sim[positive_mask]
            negative = sim[negative_mask]
            center_positive = centered_sim[positive_mask]
            center_negative = centered_sim[negative_mask]
            positive_top = float(_top_weighted(positive[None, :], default=.25)[0]) if len(positive) else .25
            negative_max = float(np.max(negative)) if len(negative) else 0.0
            centered_positive_top = float(_top_weighted(center_positive[None, :], default=.25)[0]) if len(center_positive) else .25
            centered_negative_max = float(np.max(center_negative)) if len(center_negative) else 0.0
            robust_negative = float(_top_weighted(negative[None, :], default=0.0)[0]) if len(negative) else 0.0
            center_neighbors = np.argsort(-centered_sim, kind='stable')[:min(_KERNEL_NEIGHBORS, len(centered_sim))]
            center_weights = (np.maximum(centered_sim[center_neighbors], 0) + 1e-8) ** _KERNEL_POWER
            center_kernel_rating = float(np.dot(center_weights, ratings[center_neighbors]) / center_weights.sum()) if center_weights.sum() else global_mean
            center_days = read_days[center_neighbors]
            center_known = np.isfinite(center_days)
            if center_known.any() and center_weights.sum():
                center_ages = np.maximum(0, (center_days[center_known].max() - center_days) / 365.25)
                center_ages[~center_known] = np.median(center_ages[center_known])
                center_recent_weights = center_weights * np.exp(-center_ages / _RECENCY_TIMESCALE_YEARS)
                center_recent_rating = float(np.dot(center_recent_weights, ratings[center_neighbors]) / center_recent_weights.sum())
            else:
                center_recent_rating = center_kernel_rating
            center_local = center_neighbors[:min(5, len(center_neighbors))]
            center_local_weights = np.maximum(centered_sim[center_local], 0) + 1e-4
            center_local_rating = float(np.dot(center_local_weights, ratings[center_local]) / center_local_weights.sum()) if len(center_local) else 3.0
        else:
            positive_top, negative_max, robust_negative = .25, 0.0, 0.0
            centered_positive_top, centered_negative_max = .25, 0.0
            center_kernel_rating = center_recent_rating = global_mean
            center_local_rating = 3.0

        n = len(history)
        kernel_strength = _KERNEL_SCORE_PER_STAR * min(1.0, n / _KERNEL_FULL_HISTORY)
        recency_strength = _RECENCY_SCORE_PER_STAR * min(1.0, n / _KERNEL_FULL_HISTORY)
        metadata_confidence = _metadata_confidence(candidate)
        source_term = (float(candidate.get('source_weight') or 1) - 1) * 5
        base = 42 + 48 * positive_top - 24 * negative_max + 3.5 * (local_rating - 3) + 3.5 * author_delta
        base += kernel_strength * (kernel_rating - global_mean)
        base += recency_strength * (recent_rating - kernel_rating) + source_term
        centered = 42 + 48 * centered_positive_top - 24 * centered_negative_max + 3.5 * (center_local_rating - 3) + 3.5 * author_delta
        centered += kernel_strength * (center_kernel_rating - global_mean)
        centered += recency_strength * (center_recent_rating - center_kernel_rating) + source_term
        robust = 42 + 48 * positive_top - 24 * robust_negative + 3.5 * (local_rating - 3) + 3.5 * author_delta
        robust += kernel_strength * (kernel_rating - global_mean)
        robust += recency_strength * (recent_rating - kernel_rating) + source_term
        kernel_term = kernel_strength * (kernel_rating - global_mean)
        ess2 = base - kernel_term + kernel_term * ess / (ess + 2)
        ess5 = base - kernel_term + kernel_term * ess / (ess + _KERNEL_ESS_PRIOR)
        stronger_author = base - 3.5 * author_delta + 5.0 * stronger_author_delta
        # ``current`` follows the deployed ranker. Keep the old unshrunk
        # formula and the original ESS2/ESS5 values available as comparisons.
        outputs['current'][row] = np.clip(50 + metadata_confidence * (ess5 - 50), 0, 100)
        outputs['kernel_unshrunk'][row] = np.clip(50 + metadata_confidence * (base - 50), 0, 100)
        outputs['author_stronger'][row] = np.clip(50 + metadata_confidence * (stronger_author - 50), 0, 100)
        outputs['kernel_ess2'][row] = np.clip(50 + metadata_confidence * (ess2 - 50), 0, 100)
        outputs['kernel_ess5'][row] = np.clip(50 + metadata_confidence * (ess5 - 50), 0, 100)
        outputs['cosine_centered'][row] = np.clip(50 + metadata_confidence * (centered - 50), 0, 100)
        outputs['negative_top5'][row] = np.clip(50 + metadata_confidence * (robust - 50), 0, 100)
    return outputs


def replay(records, min_history=MIN_HISTORY):
    dates = [row[0].date() for row in records]
    items = [row[1] for row in records]
    raw_vectors = np.stack([row[2] for row in records])
    vectors = normalized(raw_vectors)
    ratings = np.asarray([float(item['rating']) for item in items], dtype=np.float64)
    predictions = {name: np.full(len(records), np.nan, dtype=np.float64) for name in VARIANTS}
    parity_count = 0
    parity_mismatches = 0
    parity_max_error = 0.0
    start = 0
    while start < len(records):
        history_end = bisect_left(dates, dates[start])
        stop = start + 1
        while stop < len(records) and dates[stop] == dates[start]:
            stop += 1
        if history_end >= min_history:
            # rank_candidates internally restores read-id order before resolving similarity ties.
            history_indexes = sorted(range(history_end), key=lambda i: items[i].get('id', 0))
            group = scores_for_group([items[i] for i in history_indexes], vectors[history_indexes],
                                     items[start:stop], vectors[start:stop])
            exact_rows = rank_candidates(items[:history_end], raw_vectors[:history_end],
                                          items[start:stop], raw_vectors[start:stop])
            exact_scores = np.asarray([row['score'] for row in exact_rows], dtype=np.float64)
            errors = np.abs(np.round(group['current'], 1) - exact_scores)
            parity_count += len(errors)
            parity_mismatches += int(np.sum(errors > 1e-8))
            parity_max_error = max(parity_max_error, float(errors.max(initial=0.0)))
            for name in VARIANTS:
                predictions[name][start:stop] = np.round(group[name], 1)
        start = stop
    parity = {
        'compared_predictions': parity_count, 'mismatches_at_served_precision': parity_mismatches,
        'max_score_error_points': parity_max_error,
    }
    require_exact_ranker_parity(parity)
    return items, vectors, ratings, dates, predictions, parity


def require_exact_ranker_parity(parity):
    if parity['mismatches_at_served_precision']:
        raise RuntimeError(
            'Optimized replay differed from rank_candidates for '
            f"{parity['mismatches_at_served_precision']} of {parity['compared_predictions']} "
            f"served scores (maximum error {parity['max_score_error_points']:.1f} points)."
        )


def calibration(scores, ratings, method):
    scores = np.asarray(scores, dtype=np.float64)
    ratings = np.asarray(ratings, dtype=np.float64)
    if method == 'linear':
        model = Ridge(alpha=10.0).fit(scores[:, None], ratings)
        return lambda values: np.clip(model.predict(np.asarray(values)[:, None]), 1, 5)
    model = IsotonicRegression(y_min=1, y_max=5, out_of_bounds='clip').fit(scores, ratings)
    return lambda values: model.predict(np.asarray(values))


def metrics(ratings, scores, star_predictions=None):
    ratings = np.asarray(ratings, dtype=np.float64)
    raw = raw_ranking_metrics(ratings, np.asarray(scores, dtype=np.float64))
    k = min(20, len(ratings))
    result = {
        'n': raw['count'],
        'high_count': raw['high_4_or_5_count'],
        'low_count': raw['low_1_or_2_count'],
        'auc_high_4plus': raw['auc_four_or_five_vs_one_to_three'],
        'auc_low_2or_less': raw['auc_one_or_two_vs_three_to_five'],
        'top20_high_rate': raw['high_rated_in_top_20'] / k if k else None,
        'bottom20_low_rate': raw['low_rated_in_bottom_20'] / k if k else None,
    }
    if star_predictions is not None:
        result['calibration_mae_stars'] = float(np.mean(np.abs(np.asarray(star_predictions) - ratings)))
    return result


def feedback_metrics(labels, scores):
    """Return binary save/reject metrics using the historical evaluator's tie rules."""
    labels = np.asarray(labels, dtype=bool)
    # The shared raw metric helper accepts star ratings. This binary mapping is
    # used only to read its AUC and top/bottom counts; no mean-star metric is emitted.
    encoded = np.where(labels, 5.0, 1.0)
    raw = raw_ranking_metrics(encoded, np.asarray(scores, dtype=np.float64))
    k = min(20, len(labels))
    return {
        'save_auc': raw['auc_four_or_five_vs_one_to_three'],
        'top20_save_count': raw['high_rated_in_top_20'],
        'top20_save_rate': raw['high_rated_in_top_20'] / k if k else None,
        'bottom20_reject_count': raw['low_rated_in_bottom_20'],
        'bottom20_reject_rate': raw['low_rated_in_bottom_20'] / k if k else None,
    }


def bootstrap_auc_deltas(ratings, baseline_scores, candidate_scores, iterations, seed):
    rng = np.random.default_rng(seed)
    n = len(ratings)
    highs, lows = [], []
    ratings = np.asarray(ratings, dtype=np.float64)
    baseline_scores = np.asarray(baseline_scores, dtype=np.float64)
    candidate_scores = np.asarray(candidate_scores, dtype=np.float64)
    for _ in range(iterations):
        idx = rng.integers(0, n, n)
        candidate_result = raw_ranking_metrics(ratings[idx], candidate_scores[idx])
        baseline_result = raw_ranking_metrics(ratings[idx], baseline_scores[idx])
        hi = candidate_result['auc_four_or_five_vs_one_to_three']
        base_hi = baseline_result['auc_four_or_five_vs_one_to_three']
        lo = candidate_result['auc_one_or_two_vs_three_to_five']
        base_lo = baseline_result['auc_one_or_two_vs_three_to_five']
        if hi is not None and base_hi is not None:
            highs.append(hi - base_hi)
        if lo is not None and base_lo is not None:
            lows.append(lo - base_lo)
    q = lambda values: np.quantile(values, [.025, .975]).tolist() if values else None
    return {
        'high_auc_delta_95pct': q(highs),
        'low_auc_delta_95pct': q(lows),
        'bootstrap_resamples': iterations,
        'bootstrap_seed': seed,
    }


def fit_evaluation(ratings, predictions, cuts, bootstrap_iterations, bootstrap_seed):
    train_stop, validation_stop = cuts
    usable = np.isfinite(predictions['current'])
    train = np.flatnonzero(usable & (np.arange(len(ratings)) < train_stop))
    validation = np.arange(train_stop, validation_stop)
    test = np.arange(validation_stop, len(ratings))
    if not len(train) or not np.isfinite(predictions['current'][validation]).all() or not np.isfinite(predictions['current'][test]).all():
        raise ValueError('Chronological replay could not provide train, validation, and test predictions')
    val_results = {}
    calibrated = {}
    for name, scores in predictions.items():
        choices = []
        for method in ('linear', 'isotonic'):
            model = calibration(scores[train], ratings[train], method)
            val_mae = float(np.mean(np.abs(model(scores[validation]) - ratings[validation])))
            choices.append((val_mae, method, model))
        val_mae, method, model = min(choices, key=lambda item: item[0])
        calibrated[name] = {'method': method, 'validation_mae': val_mae}
        val_results[name] = metrics(ratings[validation], scores[validation], model(scores[validation]))
    validation_ratings = ratings[validation]
    if not np.any(validation_ratings >= 4):
        raise ValueError('Validation split contains no 4–5 star reads; balanced selection is undefined.')
    if not np.any(validation_ratings <= 2):
        raise ValueError('Validation split contains no 1–2 star reads; balanced selection is undefined.')
    if val_results['current']['auc_high_4plus'] is None:
        raise ValueError('Validation split cannot compute high AUC without both high and 1–3 star reads.')
    if val_results['current']['auc_low_2or_less'] is None:
        raise ValueError('Validation split cannot compute low AUC without both low and 3–5 star reads.')
    balanced = {
        name: (
            float(np.mean([result['auc_high_4plus'], result['auc_low_2or_less']]))
            if result['auc_high_4plus'] is not None and result['auc_low_2or_less'] is not None
            else None
        )
        for name, result in val_results.items()
    }
    # Selection is validation-only: maximize balanced discrimination for high and low ratings.
    winner = max(
        (name for name in VARIANTS if name not in {'current', 'kernel_ess5'}),
        key=lambda name: balanced[name] if balanced[name] is not None else float('-inf'),
    )
    if balanced[winner] is None or balanced[winner] <= balanced['current']:
        winner = 'current'
    train_validation = np.concatenate((train, validation))
    test_results = {}
    for name in ('current', winner):
        model = calibration(predictions[name][train_validation], ratings[train_validation], calibrated[name]['method'])
        test_results[name] = metrics(ratings[test], predictions[name][test], model(predictions[name][test]))
    def nullable_delta(candidate, current):
        return candidate - current if candidate is not None and current is not None else None

    high_delta = nullable_delta(test_results[winner]['auc_high_4plus'], test_results['current']['auc_high_4plus'])
    low_delta = nullable_delta(test_results[winner]['auc_low_2or_less'], test_results['current']['auc_low_2or_less'])
    balanced_delta = (float(np.mean([high_delta, low_delta]))
                      if high_delta is not None and low_delta is not None else None)
    return {
        'split': {'train': int(len(train)), 'validation': int(len(validation)), 'test': int(len(test)),
                  'minimum_history': MIN_HISTORY,
                  'protocol': 'whole-day 60/20/20 chronological split; expanding history with only strictly earlier read days; validation selects formula and calibrator; current and the validation-selected formula are evaluated on the latest 20%'},
        'validation_selection_objective': 'Maximize the unweighted mean of high 4–5 vs 1–3 AUC and low 1–2 vs 3–5 AUC; ties keep the first declared alternative; choose current if no alternative beats it.',
        'validation': val_results,
        'validation_balanced_high_low_auc': balanced,
        'validation_calibration': calibrated,
        'selected_on_validation': winner,
        'test': test_results,
        'test_selected_minus_current': {
            'high_auc': high_delta,
            'low_auc': low_delta,
            'balanced_auc': balanced_delta,
            'calibration_mae_stars': nullable_delta(
                test_results[winner]['calibration_mae_stars'],
                test_results['current']['calibration_mae_stars'],
            ),
            'bootstrap_uncertainty': bootstrap_auc_deltas(
                ratings[test], predictions['current'][test], predictions[winner][test],
                bootstrap_iterations, bootstrap_seed,
            ),
        } if winner != 'current' else {'status': 'current formula won validation; no alternative selected for test'},
        'score_changes_on_test': score_changes(predictions['current'][test], predictions[winner][test]) if winner != 'current' else {'status': 'no alternative selected'},
        'latest_20pct_is_test': True,
        'limitations': 'One production reader. Every historical target was eventually read and rated, so this replay conditions on the reader’s choice to read and does not model outcomes for unseen candidates. Embeddings may postdate read dates. The later 20% may have been inspected in prior work and is not independent confirmation; only prospectively collected data frozen before analysis can provide confirmatory evidence. Iid book bootstrap does not capture reader or time dependence.'
    }


def score_changes(current, candidate):
    delta = np.asarray(candidate) - np.asarray(current)
    return {'mean_delta_points': float(delta.mean()), 'median_delta_points': float(np.median(delta)),
            'mean_absolute_delta_points': float(np.abs(delta).mean()), 'p90_absolute_delta_points': float(np.quantile(np.abs(delta), .90)),
            'changed_by_at_least_5_points': int(np.sum(np.abs(delta) >= 5)), 'n': int(len(delta))}


def feedback_replay(data, records, selected, selected_formula):
    """Summarize logged saves/rejects as a separate, untuned intent diagnostic."""
    candidates = {item['id']: item for item in data.get('candidates', [])}
    candidate_cache = {
        entry['entity_id']: entry
        for entry in data['embeddings']
        if entry['entity_type'] == 'candidate'
        and (entry['backend'], entry['model']) == selected
    }
    dated_events = [
        (read_time(event.get('created_at')), event)
        for event in data.get('feedback', [])
        if not event.get('undone_at')
    ]
    latest = {}
    for when, event in sorted(
        ((when, event) for when, event in dated_events if when is not None),
        key=lambda pair: (pair[0], pair[1]['id']),
    ):
        latest[event['candidate_id']] = (when, event)

    labels = []
    feedback_variants = tuple(dict.fromkeys(('current', selected_formula)))
    variant_scores = {name: [] for name in feedback_variants}
    original_scores = []
    parity_count = 0
    parity_mismatches = 0
    parity_max_error = 0.0
    source_weight_present = 0
    catalog_confidence_present = 0
    excluded = 0
    for candidate_id, (when, event) in latest.items():
        item, entry = candidates.get(candidate_id), candidate_cache.get(candidate_id)
        if event.get('action') not in {'save', 'reject'} or not item or not entry:
            excluded += 1
            continue
        if entry.get('content_hash') != content_hash(document(item)):
            excluded += 1
            continue
        vector = np.frombuffer(entry['vector'], dtype=np.float32)
        if len(vector) != entry['dimensions'] or not np.isfinite(vector).all():
            excluded += 1
            continue
        identity = book_identity(item.get('title', ''), item.get('author', ''))
        prior = [
            (row_date, row_item, row_vector)
            for row_date, row_item, row_vector in records
            if row_date.date() < when.date()
            and book_identity(row_item.get('title', ''), row_item.get('author', '')) != identity
        ]
        if not prior:
            excluded += 1
            continue
        prior.sort(key=lambda row: row[1].get('id', 0))
        histories = [row[1] for row in prior]
        history_vectors = np.stack([row[2] for row in prior])
        scored = scores_for_group(histories, history_vectors, [item], vector[None, :])
        exact_score = float(
            rank_candidates(histories, history_vectors, [item], vector[None, :])[0]['score']
        )
        score_error = abs(float(np.round(scored['current'][0], 1)) - exact_score)
        parity_count += 1
        parity_mismatches += int(score_error > 1e-8)
        parity_max_error = max(parity_max_error, score_error)
        for name in feedback_variants:
            variant_scores[name].append(float(np.round(scored[name][0], 1)))
        original_scores.append(float(baseline(histories, history_vectors, [item], vector[None, :])[0]))
        source_weight_present += int(item.get('source_weight') is not None)
        catalog_confidence_present += int(
            item.get('catalog_confidence') is not None or item.get('quality_score') is not None
        )
        labels.append(event['action'] == 'save')

    if not labels:
        return {
            'usable': 0,
            'excluded': int(excluded),
            'exact_ranker_parity_check': {
                'compared_predictions': parity_count,
                'mismatches_at_served_precision': parity_mismatches,
                'max_score_error_points': parity_max_error,
            },
        }
    parity = {
        'compared_predictions': parity_count,
        'mismatches_at_served_precision': parity_mismatches,
        'max_score_error_points': parity_max_error,
    }
    require_exact_ranker_parity(parity)
    labels = np.asarray(labels, dtype=bool)
    result = {
        'usable': int(len(labels)),
        'saves': int(labels.sum()),
        'rejects': int((~labels).sum()),
        'excluded': int(excluded),
        'exact_ranker_parity_check': parity,
        'candidate_feature_coverage': {
            'usable_candidates': int(len(labels)),
            'with_source_weight': source_weight_present,
            'with_catalog_confidence_or_quality_score': catalog_confidence_present,
            'missing_fields_use_ranker_defaults': True,
        },
        'protocol': (
            'Latest non-undone save/reject per candidate; read history is restricted to earlier '
            'calendar days. This is a separate, untuned intent diagnostic without exposure correction.'
        ),
        'metrics': {
            name: feedback_metrics(labels, np.asarray(scores))
            for name, scores in variant_scores.items()
        },
        'original_formula': feedback_metrics(labels, np.asarray(original_scores)),
        'limitations': (
            'Sparse logged actions do not estimate the causal effect of ranking or exposure. '
            'The SQLite loader does not join source weight or catalog confidence; use the enriched '
            'JSON export to reproduce those production inputs. When either field is absent, the '
            'ranker uses its documented default, so parity applies to the fields supplied.'
        ),
    }
    return result

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('corpus', type=Path, help='private JSON or SQLite export accepted by evaluate_ranking.py')
    parser.add_argument('--backend', help='embedding backend to evaluate')
    parser.add_argument('--model', help='embedding model to evaluate')
    parser.add_argument('--bootstrap-iterations', type=int, default=2000)
    parser.add_argument('--bootstrap-seed', type=int, default=20260930)
    args = parser.parse_args()
    if args.bootstrap_iterations < 0:
        parser.error('--bootstrap-iterations must be non-negative')

    data = load_corpus(args.corpus)
    records, selected, excluded = prepare(data, args.backend, args.model)
    items, vectors, ratings, dates, predictions, parity = replay(records)
    cuts = whole_day_cuts(records)[1]
    fitted = fit_evaluation(
        ratings,
        predictions,
        cuts,
        bootstrap_iterations=args.bootstrap_iterations,
        bootstrap_seed=args.bootstrap_seed,
    )
    fitted['algorithm_hypotheses'] = HYPOTHESES
    fitted['comparison_references'] = {
        'current': 'Deployed ESS5 formula, identical to kernel_ess5.',
        'kernel_unshrunk': 'Former deployed formula before kernel ESS shrinkage.',
        'author_stronger_cosine_centered_negative_top5': 'Frozen historical alternatives computed from kernel_unshrunk; their metrics do not isolate a one-factor change against the current ESS5 formula.',
    }
    fitted['exact_ranker_parity_check'] = parity
    fitted['uncertainty_settings'] = {
        'bootstrap_iterations': args.bootstrap_iterations,
        'bootstrap_seed': args.bootstrap_seed,
    }
    fitted['data'] = {
        'snapshot_sha256': hashlib.sha256(args.corpus.read_bytes()).hexdigest(),
        'selected_embedding': list(selected),
        'usable_reads': len(records),
        'excluded': excluded,
        'whole_day_split_boundaries': [
            str(dates[cuts[0] - 1]), str(dates[cuts[0]]),
            str(dates[cuts[1] - 1]), str(dates[cuts[1]]),
        ],
    }
    fitted['feedback_diagnostic'] = feedback_replay(
        data, records, selected, fitted['selected_on_validation']
    )
    print(json.dumps(fitted, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
