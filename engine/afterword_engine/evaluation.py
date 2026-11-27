"""Offline evaluation for recommendation impressions and ranking quality.

All metrics in this module are read-only calculations over exported rows.  A
missing outcome is censored and is excluded from the metric; it is never
silently converted to a negative label.  The legacy ``ips_*`` names are kept
for API compatibility, but the outputs are descriptive logged-policy metrics:
``confidence / propensity`` is a diagnostic weight, not a universal causal
correction.  Ranking metrics are computed within each recommendation run.
"""

from __future__ import annotations

import json
import math
import random
import sqlite3
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from operator import itemgetter
from pathlib import Path
from typing import Any


DEFAULT_K = 20
DEFAULT_BOOTSTRAP_ITERATIONS = 1000
DEFAULT_MIN_PROPENSITY = 0.2
DEFAULT_MINIMUMS = {
    "runs": 20,
    "labeled_impressions": 200,
    "positive_impressions": 20,
    "negative_impressions": 20,
}

RATING_KINDS = frozenset({"rating_positive", "rating_neutral", "rating_negative"})
COMPLETION_KINDS = frozenset({"completed_unrated", "read"})
SHORTLIST_INTENT_KINDS = frozenset({
    "explicit_feedback",
    "save",
    "reject",
    "maybe_later",
    "restore",
    "librarr_import",
    "librarr_search",
})
FACET_SIGNAL_KINDS: dict[str, frozenset[str]] = {
    "shortlist_intent": SHORTLIST_INTENT_KINDS,
    "explicit_ratings": RATING_KINDS,
    "completion": COMPLETION_KINDS,
}


def _as_float(value: Any, *, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    else:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            result = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            try:
                result = parsedate_to_datetime(raw)
            except (TypeError, ValueError, OverflowError):
                try:
                    result = datetime.strptime(raw, "%Y/%m/%d")
                except (TypeError, ValueError):
                    return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _time_sort_key(value: Any) -> tuple[datetime, str]:
    parsed = _time(value)
    return (
        parsed if parsed is not None else datetime.min.replace(tzinfo=timezone.utc),
        str(value or ""),
    )


def _is_at_or_after(value: Any, cutoff: datetime | None) -> bool:
    """Return True when ``cutoff`` is set and ``value`` is missing, unparseable, or ``>= cutoff``."""

    if cutoff is None:
        return False
    parsed = _time(value)
    return parsed is None or parsed >= cutoff


def _row_label(row: Mapping[str, Any]) -> float | None:
    label = _as_float(row.get("label"))
    return label if label is not None and 0 <= label <= 1 else None


def _effective_confidence(
    row: Mapping[str, Any],
    *,
    full_rating_certainty: bool = False,
) -> float | None:
    confidence = _as_float(row.get("confidence"), default=1.0)
    if confidence is None or confidence < 0:
        return None
    if full_rating_certainty and confidence > 0 and row.get("label_kind") in RATING_KINDS:
        return 1.0
    return confidence


def _outcome_priority(item: Mapping[str, Any]) -> int:
    kind = str(item.get("label_kind") or "")
    if kind in RATING_KINDS:
        return 3
    if kind in COMPLETION_KINDS:
        return 2
    if kind in SHORTLIST_INTENT_KINDS:
        return 1
    return 0


def _select_primary_outcome(
    outcomes: Iterable[Mapping[str, Any]],
    *,
    full_rating_certainty: bool = False,
) -> Mapping[str, Any] | None:
    candidates = list(outcomes)
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda item: (
            1 if _row_label(item) is not None else 0,
            _outcome_priority(item),
            _effective_confidence(item, full_rating_certainty=full_rating_certainty) or 0.0,
            _time_sort_key(item.get("attributed_at")),
        ),
    )


def _project_outcome_row(
    row: Mapping[str, Any],
    outcome: Mapping[str, Any],
    *,
    full_rating_certainty: bool = False,
) -> dict[str, Any]:
    raw_confidence = outcome.get("confidence")
    confidence: Any = 1.0 if raw_confidence is None else raw_confidence
    if full_rating_certainty:
        effective = _effective_confidence(outcome, full_rating_certainty=True)
        if effective is not None:
            confidence = effective
    return {
        **row,
        "label": outcome.get("label"),
        "confidence": confidence,
        "label_kind": outcome.get("label_kind"),
        "attributed_at": outcome.get("attributed_at"),
    }


def _facet_rows(
    rows: Iterable[Mapping[str, Any]],
    allowed_kinds: frozenset[str],
    *,
    full_rating_certainty: bool = False,
) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for row in rows:
        observed = row.get("observed_outcomes")
        if observed:
            matching = [
                outcome
                for outcome in observed
                if str(outcome.get("label_kind") or "") in allowed_kinds
            ]
            selected = _select_primary_outcome(matching, full_rating_certainty=full_rating_certainty)
            if selected is not None:
                projected.append(_project_outcome_row(row, selected, full_rating_certainty=full_rating_certainty))
        elif str(row.get("label_kind") or "") in allowed_kinds:
            projected.append(_project_outcome_row(row, row, full_rating_certainty=full_rating_certainty))
    return projected


def _enjoyment_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return _facet_rows(rows, RATING_KINDS, full_rating_certainty=True)


def _positive_rank(value: Any) -> int | None:
    """Return a strictly positive integer logged rank, or ``None``."""

    if isinstance(value, bool):
        return None
    number = _as_float(value)
    if number is None or number <= 0 or not number.is_integer():
        return None
    return int(number)


def _run_key(row: Mapping[str, Any]) -> str:
    value = row.get("run_id")
    return str(value) if value is not None else "__missing_run__"


def _row_tie_key(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    """Make equal logged ranks deterministic without relying on input order."""

    return tuple(
        str(row.get(field, ""))
        for field in ("candidate_id", "impression_id", "event_id", "score")
    )


def _ranked_runs(rows: Iterable[Mapping[str, Any]]) -> dict[str, list[tuple[Mapping[str, Any], int]]]:
    """Group rows by run and sort valid logged slots deterministically."""

    groups: dict[str, list[tuple[Mapping[str, Any], int]]] = defaultdict(list)
    seen: dict[str, dict[tuple[str, str], Mapping[str, Any]]] = defaultdict(dict)
    seen_ranks: dict[str, dict[int, Mapping[str, Any]]] = defaultdict(dict)
    for row in rows:
        rank = _positive_rank(row.get("rank"))
        if rank is not None:
            run_key = _run_key(row)
            candidate_id = row.get("candidate_id")
            impression_id = row.get("impression_id")
            duplicate_keys = []
            if impression_id is not None:
                duplicate_keys.append(("impression", str(impression_id)))
            if candidate_id is not None:
                duplicate_keys.append(("candidate", str(candidate_id)))
            if not duplicate_keys:
                duplicate_keys.append(("row", "|".join(_row_tie_key(row))))
            previous = next(
                (seen[run_key].get(key) for key in duplicate_keys if seen[run_key].get(key) is not None),
                None,
            )
            if previous is not None:
                fingerprint = tuple(
                    str(previous.get(field, ""))
                    for field in ("rank", "score", "propensity", "confidence", "label", "label_kind")
                )
                current_fingerprint = tuple(
                    str(row.get(field, ""))
                    for field in ("rank", "score", "propensity", "confidence", "label", "label_kind")
                )
                if fingerprint != current_fingerprint:
                    raise ValueError("Conflicting duplicate impression in recommendation run")
                continue
            previous_rank = seen_ranks[run_key].get(rank)
            if previous_rank is not None:
                raise ValueError("Duplicate logged rank in recommendation run")
            seen_ranks[run_key][rank] = row
            for key in duplicate_keys:
                seen[run_key][key] = row
            groups[run_key].append((row, rank))
    for values in groups.values():
        values.sort(key=lambda item: (item[1], _row_tie_key(item[0])))
    return groups


def _weighted_item(
    row: Mapping[str, Any],
    *,
    score_key: str,
    min_propensity: float = DEFAULT_MIN_PROPENSITY,
    full_rating_certainty: bool = False,
) -> tuple[float, float, float] | None:
    label = _row_label(row)
    score = _as_float(row.get(score_key))
    propensity = _as_float(row.get("propensity"), default=1.0)
    confidence = _effective_confidence(row, full_rating_certainty=full_rating_certainty)
    if (
        label is None
        or score is None
        or propensity is None
        or confidence is None
        or propensity <= 0
        or propensity > 1
        or min_propensity <= 0
        or min_propensity > 1
    ):
        return None
    clipped_propensity = max(propensity, min_propensity)
    return score, label, confidence / clipped_propensity


def _weighted_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    score_key: str = "score",
    min_propensity: float = DEFAULT_MIN_PROPENSITY,
    full_rating_certainty: bool = False,
) -> list[tuple[Mapping[str, Any], float, float, float]]:
    """Return ``(row, score, label, weight)`` for usable labeled rows."""

    result = []
    for row in rows:
        item = _weighted_item(
            row,
            score_key=score_key,
            min_propensity=min_propensity,
            full_rating_certainty=full_rating_certainty,
        )
        if item is not None:
            score, label, weight = item
            result.append((row, score, label, weight))
    return result


def ips_auc(
    rows: Iterable[Mapping[str, Any]],
    *,
    score_key: str = "score",
    positive_threshold: float = 0.5,
    min_propensity: float = DEFAULT_MIN_PROPENSITY,
    full_rating_certainty: bool = False,
) -> float | None:
    """Compute pairwise binary IPS AUC with confidence-adjusted propensity weights.

    Labels strictly above ``positive_threshold`` are positives, labels strictly
    below ``positive_threshold`` are negatives, and labels equal to
    ``positive_threshold`` are excluded.
    """

    usable = _weighted_rows(
        rows,
        score_key=score_key,
        min_propensity=min_propensity,
        full_rating_certainty=full_rating_certainty,
    )
    scored: list[tuple[float, bool, float]] = []
    total_pos_weight = 0.0
    total_neg_weight = 0.0
    has_positive = False
    has_negative = False
    for _, score, label, weight in usable:
        if label > positive_threshold:
            has_positive = True
            total_pos_weight += weight
            scored.append((0.0 if score == 0.0 else score, True, weight))
        elif label < positive_threshold:
            has_negative = True
            total_neg_weight += weight
            scored.append((0.0 if score == 0.0 else score, False, weight))
    denominator = total_pos_weight * total_neg_weight
    if not has_positive or not has_negative or denominator <= 0:
        return None
    scored.sort(key=itemgetter(0))
    numerator = 0.0
    cumulative_lower_neg_weight = 0.0
    current_score = scored[0][0]
    pos_group_weight = 0.0
    neg_group_weight = 0.0
    for score, is_positive, weight in scored:
        if score != current_score:
            numerator += (
                pos_group_weight * cumulative_lower_neg_weight
                + 0.5 * pos_group_weight * neg_group_weight
            )
            cumulative_lower_neg_weight += neg_group_weight
            current_score = score
            pos_group_weight = 0.0
            neg_group_weight = 0.0
        if is_positive:
            pos_group_weight += weight
        else:
            neg_group_weight += weight
    numerator += (
        pos_group_weight * cumulative_lower_neg_weight
        + 0.5 * pos_group_weight * neg_group_weight
    )
    return numerator / denominator


def ips_grade_concordance(
    rows: Iterable[Mapping[str, Any]],
    *,
    score_key: str = "score",
    min_propensity: float = DEFAULT_MIN_PROPENSITY,
    full_rating_certainty: bool = False,
) -> float | None:
    """Compute weighted pairwise concordance across all distinct outcome grades."""

    usable = _weighted_rows(
        rows,
        score_key=score_key,
        min_propensity=min_propensity,
        full_rating_certainty=full_rating_certainty,
    )
    if len(usable) < 2:
        return None
    by_score: dict[float, dict[float, float]] = defaultdict(lambda: defaultdict(float))
    for _row, score, label, weight in usable:
        if weight > 0:
            normalized_score = 0.0 if score == 0.0 else score
            by_score[normalized_score][label] += weight
    if not by_score:
        return None
    numerator = 0.0
    denominator = 0.0
    lower_by_label: dict[float, float] = defaultdict(float)
    for score in sorted(by_score):
        items = list(by_score[score].items())
        for index, (label_left, weight_left) in enumerate(items):
            for label_right, weight_right in items[index + 1:]:
                if label_left != label_right:
                    pair_weight = weight_left * weight_right
                    numerator += 0.5 * pair_weight
                    denominator += pair_weight
        for label_current, weight_current in items:
            for label_lower, weight_lower in lower_by_label.items():
                if label_current > label_lower:
                    pair_weight = weight_current * weight_lower
                    numerator += pair_weight
                    denominator += pair_weight
                elif label_lower > label_current:
                    pair_weight = weight_lower * weight_current
                    denominator += pair_weight
        for label_current, weight_current in items:
            lower_by_label[label_current] += weight_current
    return numerator / denominator if denominator > 0 else None


def ips_precision_at_k(
    rows: Iterable[Mapping[str, Any]],
    k: int = DEFAULT_K,
    *,
    score_key: str = "score",
    positive_threshold: float = 0.5,
    min_propensity: float = DEFAULT_MIN_PROPENSITY,
    full_rating_certainty: bool = False,
) -> float | None:
    """Estimate logged top-k precision as a clipped SNIPS macro-average over runs.

    The logged rank is used for the slate position.  Top-k selection happens
    before censoring unlabeled rows, and only runs with an observed label in
    their top-k contribute to the descriptive average.  Neutral reads (``0.5``)
    are non-positive; pass ``full_rating_certainty=True`` (used by the
    ``enjoyment`` projection) to treat all explicit star ratings as certainty
    ``1.0``.
    """

    if k <= 0:
        raise ValueError("k must be positive")
    per_run: list[float] = []
    for values in _ranked_runs(rows).values():
        topk = [(row, rank) for row, rank in values if rank <= k]
        usable = []
        for row, _rank in topk:
            item = _weighted_item(
                row,
                score_key=score_key,
                min_propensity=min_propensity,
                full_rating_certainty=full_rating_certainty,
            )
            if item is not None:
                usable.append(item)
        total_weight = sum(weight for _, _, weight in usable)
        if total_weight > 0:
            normalized = [(label, weight / total_weight) for _, label, weight in usable]
            numerator = sum(norm_weight for label, norm_weight in normalized if label > positive_threshold)
            per_run.append(numerator)
    return sum(per_run) / len(per_run) if per_run else None


def ips_ndcg_at_k(
    rows: Iterable[Mapping[str, Any]],
    k: int = DEFAULT_K,
    *,
    score_key: str = "score",
    positive_threshold: float = 0.5,
    min_propensity: float = DEFAULT_MIN_PROPENSITY,
    full_rating_certainty: bool = False,
) -> float | None:
    """Estimate logged NDCG@k as a macro-average over runs.

    Logged integer ranks determine both the cutoff (``rank <= k``) and the
    logarithmic discount ``1 / log2(rank + 1)``.  Missing outcomes leave their
    slots in place instead of moving a later observed label upward, and
    neutral/negative outcomes (``<= positive_threshold``) receive zero gain.

    Weighting rule: ``_weighted_item`` filters usable positive-weight
    observations (valid label, finite score, ``0 < propensity <= 1``, and
    ``confidence > 0``), and both ``DCG@k`` and ``IDCG@k`` apply unweighted
    intrinsic relevance gains symmetrically so ``IDCG@k`` is computed from the
    observed top-k gains alone and each run's ``DCG@k / IDCG@k`` is guaranteed
    within ``[0, 1]`` without clamping.
    """

    if k <= 0:
        raise ValueError("k must be positive")
    per_run: list[float] = []
    for values in _ranked_runs(rows).values():
        topk = [(row, rank) for row, rank in values if rank <= k]
        observed_topk: list[tuple[int, float]] = []
        for row, rank in topk:
            item = _weighted_item(
                row,
                score_key=score_key,
                min_propensity=min_propensity,
                full_rating_certainty=full_rating_certainty,
            )
            if item is not None:
                _score, label, weight = item
                if weight > 0:
                    gain = label if label > positive_threshold else 0.0
                    observed_topk.append((rank, gain))
        if not observed_topk:
            continue
        dcg = sum(gain / math.log2(rank + 1) for rank, gain in observed_topk)
        ideal_gains = sorted((gain for _rank, gain in observed_topk), reverse=True)
        idcg = sum(
            gain / math.log2(position + 2)
            for position, gain in enumerate(ideal_gains)
        )
        if idcg > 0:
            ratio = dcg / idcg
            if not 0.0 <= ratio <= 1.0:
                raise AssertionError(f"NDCG@k out of [0, 1] bounds: {ratio}")
            per_run.append(ratio)
    return sum(per_run) / len(per_run) if per_run else None


def ips_rank_concordance(
    rows: Iterable[Mapping[str, Any]],
    *,
    score_key: str = "score",
    positive_threshold: float = 0.5,
    min_propensity: float = DEFAULT_MIN_PROPENSITY,
    full_rating_certainty: bool = False,
) -> float | None:
    """Compute within-run pairwise rank concordance as a macro-average over runs.

    Logged slate ``rank`` orders candidates within each discovery run (smaller
    rank means presented earlier), while unlabeled slots remain censored.  This
    measures concordance of the served slate order even when local diversity
    swaps reorder close-scoring candidates without mutating raw scores.
    """

    per_run: list[float] = []
    for values in _ranked_runs(rows).values():
        usable: list[tuple[int, float, float]] = []
        for row, rank in values:
            item = _weighted_item(
                row,
                score_key=score_key,
                min_propensity=min_propensity,
                full_rating_certainty=full_rating_certainty,
            )
            if item is not None:
                _score, label, weight = item
                usable.append((rank, label, weight))
        positives = [
            (rank, weight) for rank, label, weight in usable if label > positive_threshold
        ]
        negatives = [
            (rank, weight) for rank, label, weight in usable if label < positive_threshold
        ]
        denominator = sum(
            left_weight * right_weight
            for _, left_weight in positives
            for _, right_weight in negatives
        )
        if not positives or not negatives or denominator <= 0:
            continue
        numerator = 0.0
        for positive_rank, positive_weight in positives:
            for negative_rank, negative_weight in negatives:
                pair_weight = positive_weight * negative_weight
                if positive_rank < negative_rank:
                    numerator += pair_weight
                elif positive_rank == negative_rank:
                    numerator += pair_weight * 0.5
        per_run.append(numerator / denominator)
    return sum(per_run) / len(per_run) if per_run else None


def _metric_bundle(
    rows: Sequence[Mapping[str, Any]],
    *,
    score_key: str,
    k: int,
    full_rating_certainty: bool = False,
) -> dict[str, Any]:
    usable = _weighted_rows(
        rows,
        score_key=score_key,
        full_rating_certainty=full_rating_certainty,
    )
    labels = [label for _, _, label, _ in usable]
    return {
        "auc": ips_auc(
            rows,
            score_key=score_key,
            full_rating_certainty=full_rating_certainty,
        ),
        "rank_concordance": ips_rank_concordance(
            rows,
            score_key=score_key,
            full_rating_certainty=full_rating_certainty,
        ),
        "grade_concordance": ips_grade_concordance(
            rows,
            score_key=score_key,
            full_rating_certainty=full_rating_certainty,
        ),
        "precision_at_20": ips_precision_at_k(
            rows,
            k,
            score_key=score_key,
            full_rating_certainty=full_rating_certainty,
        ),
        "ndcg_at_20": ips_ndcg_at_k(
            rows,
            k,
            score_key=score_key,
            full_rating_certainty=full_rating_certainty,
        ),
        "impressions": len(rows),
        "labeled_impressions": len(usable),
        "positive_impressions": sum(label > 0.5 for label in labels),
        "neutral_impressions": sum(label == 0.5 for label in labels),
        "negative_impressions": sum(label < 0.5 for label in labels),
        "runs": len({_run_key(row) for row in rows}),
    }


def evaluate_metrics(
    rows: Iterable[Mapping[str, Any]],
    *,
    score_key: str = "score",
    k: int = DEFAULT_K,
    full_rating_certainty: bool = False,
) -> dict[str, Any]:
    """Return IPS metrics and usable sample counts for one score column."""

    values = list(rows)
    return _metric_bundle(
        values,
        score_key=score_key,
        k=k,
        full_rating_certainty=full_rating_certainty,
    )


def cluster_bootstrap(
    rows: Iterable[Mapping[str, Any]],
    *,
    score_key: str = "score",
    metric: str = "auc",
    k: int = DEFAULT_K,
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    seed: int = 20260920,
    full_rating_certainty: bool = False,
) -> dict[str, Any]:
    """Bootstrap a metric by resampling complete recommendation runs."""

    values = list(rows)
    clusters: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in values:
        clusters[_run_key(row)].append(row)
    extra = {"full_rating_certainty": True} if full_rating_certainty else {}
    metric_fn = {
        "auc": lambda sample: ips_auc(sample, score_key=score_key, **extra),
        "rank_concordance": lambda sample: ips_rank_concordance(sample, score_key=score_key, **extra),
        "grade_concordance": lambda sample: ips_grade_concordance(sample, score_key=score_key, **extra),
        "precision_at_20": lambda sample: ips_precision_at_k(sample, k, score_key=score_key, **extra),
        "ndcg_at_20": lambda sample: ips_ndcg_at_k(sample, k, score_key=score_key, **extra),
    }.get(metric)
    if metric_fn is None:
        raise ValueError(f"Unsupported bootstrap metric: {metric}")
    estimate = metric_fn(values)
    if not clusters or estimate is None or iterations <= 0:
        return {"estimate": estimate, "lower": None, "upper": None, "iterations": 0, "clusters": len(clusters)}
    generator = random.Random(seed)
    cluster_values = list(clusters.values())
    samples: list[float] = []
    for _ in range(int(iterations)):
        sampled = [cluster_values[generator.randrange(len(cluster_values))] for _ in cluster_values]
        # Each draw is a separate run.  Without remapping IDs, repeated draws
        # of one run collapse back into one group inside per-run metrics.
        sampled_rows: list[Mapping[str, Any]] = []
        for replica_index, cluster in enumerate(sampled):
            replica_id = f"__bootstrap_run_{replica_index}"
            for row in cluster:
                copy = dict(row)
                copy["run_id"] = replica_id
                sampled_rows.append(copy)
        metric_value = metric_fn(sampled_rows)
        if metric_value is not None and math.isfinite(metric_value):
            samples.append(metric_value)
    if not samples:
        return {"estimate": estimate, "lower": None, "upper": None, "iterations": 0, "clusters": len(clusters)}
    samples.sort()
    lower_index = max(0, min(len(samples) - 1, int(0.025 * (len(samples) - 1))))
    upper_index = max(0, min(len(samples) - 1, int(0.975 * (len(samples) - 1))))
    return {
        "estimate": estimate,
        "lower": samples[lower_index],
        "upper": samples[upper_index],
        "iterations": len(samples),
        "clusters": len(clusters),
    }


def _censor_row(row: Mapping[str, Any], cutoff: datetime | None) -> dict[str, Any]:
    copy = dict(row)
    has_observed_key = "observed_outcomes" in copy and copy["observed_outcomes"] is not None
    if has_observed_key:
        raw_observed = list(copy["observed_outcomes"])
        kept = [
            dict(item)
            for item in raw_observed
            if not _is_at_or_after(item.get("attributed_at"), cutoff)
        ]
        copy["observed_outcomes"] = kept
        if cutoff is not None and raw_observed:
            primary = _select_primary_outcome(kept)
            raw_conf = primary.get("confidence") if primary is not None else None
            copy["label"] = primary.get("label") if primary is not None else None
            copy["confidence"] = (1.0 if raw_conf is None else raw_conf) if primary is not None else None
            copy["label_kind"] = primary.get("label_kind") if primary is not None else None
            copy["attributed_at"] = primary.get("attributed_at") if primary is not None else None
            return copy
    if cutoff is not None and _is_at_or_after(copy.get("attributed_at"), cutoff):
        copy["label"] = None
        copy["confidence"] = None
        copy["label_kind"] = None
        copy["attributed_at"] = None
    return copy


def temporal_split(
    rows: Iterable[Mapping[str, Any]],
    *,
    train_fraction: float = 0.6,
    validation_fraction: float = 0.2,
) -> dict[str, list[Mapping[str, Any]]]:
    """Split rows chronologically by whole recommendation runs and censor future outcomes.

    A run is the atomic unit, preventing impressions from the same request
    leaking across train, validation, and test.  Each split also censors any
    observed outcome attributed at or after the next split's start timestamp.
    Missing timestamps sort before dated runs and remain deterministic by run id.
    """

    if train_fraction <= 0 or validation_fraction < 0 or train_fraction + validation_fraction >= 1:
        raise ValueError("fractions must satisfy 0 < train and train + validation < 1")
    values = list(rows)
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in values:
        grouped[_run_key(row)].append(row)

    def group_timestamp(group: Sequence[Mapping[str, Any]]) -> datetime | None:
        dates = [_time(row.get("run_created_at") or row.get("created_at") or row.get("presented_at")) for row in group]
        dated = [date for date in dates if date is not None]
        return min(dated) if dated else None

    def run_time(group: list[Mapping[str, Any]]) -> tuple[datetime, str]:
        timestamp = group_timestamp(group)
        return (
            timestamp if timestamp is not None else datetime.min.replace(tzinfo=timezone.utc),
            str(group[0].get("run_id", "")),
        )

    def split_cutoff(later_groups: Sequence[Sequence[Mapping[str, Any]]]) -> datetime | None:
        dated = [ts for ts in (group_timestamp(group) for group in later_groups) if ts is not None]
        return min(dated) if dated else None

    ordered = sorted(grouped.values(), key=run_time)
    count = len(ordered)
    train_end = min(count, max(1 if count else 0, int(count * train_fraction)))
    validation_end = min(count, max(train_end, int(count * (train_fraction + validation_fraction))))
    train_cutoff = split_cutoff(ordered[train_end:])
    validation_cutoff = split_cutoff(ordered[validation_end:])
    return {
        "train": [_censor_row(row, train_cutoff) for group in ordered[:train_end] for row in group],
        "validation": [_censor_row(row, validation_cutoff) for group in ordered[train_end:validation_end] for row in group],
        "test": [_censor_row(row, None) for group in ordered[validation_end:] for row in group],
    }


def signal_diagnostics(
    rows: Iterable[Mapping[str, Any]],
    *,
    score_key: str = "score",
    k: int = DEFAULT_K,
    full_rating_certainty: bool = False,
) -> dict[str, dict[str, Any]]:
    """Report multi-class behavioral facets alongside per-signal sample and IPS metrics."""

    values = list(rows)
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in values:
        observed = row.get("observed_outcomes")
        if not observed:
            signal = str(row.get("label_kind") or row.get("signal_type") or "unknown")
            groups[signal].append(
                _project_outcome_row(row, row, full_rating_certainty=full_rating_certainty)
                if row.get("label_kind")
                else dict(row)
            )
            continue
        # Preserve each kind of observed signal without counting repeated
        # events for one impression more than once within that signal.
        by_signal: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for outcome in observed:
            by_signal[str(outcome.get("label_kind") or "unknown")].append(outcome)
        for signal, outcomes in by_signal.items():
            selected = _select_primary_outcome(outcomes, full_rating_certainty=full_rating_certainty)
            if selected is not None:
                groups[signal].append(
                    _project_outcome_row(row, selected, full_rating_certainty=full_rating_certainty)
                )
    diagnostics: dict[str, dict[str, Any]] = {
        signal: _metric_bundle(
            signal_rows,
            score_key=score_key,
            k=k,
            full_rating_certainty=full_rating_certainty,
        )
        for signal, signal_rows in sorted(groups.items())
    }
    for facet_name, allowed_kinds in FACET_SIGNAL_KINDS.items():
        facet_rows = _facet_rows(
            values,
            allowed_kinds,
            full_rating_certainty=full_rating_certainty,
        )
        diagnostics[facet_name] = _metric_bundle(
            facet_rows,
            score_key=score_key,
            k=k,
            full_rating_certainty=full_rating_certainty,
        )
    return dict(sorted(diagnostics.items()))


def promotion_gate(
    candidate: Mapping[str, Any],
    champion: Mapping[str, Any],
    *,
    sample: Mapping[str, Any] | None = None,
    minimums: Mapping[str, int] | None = None,
    min_auc_delta: float = 0.01,
    min_precision_delta: float = 0.0,
    min_ndcg_delta: float = 0.01,
) -> dict[str, Any]:
    """Screen metric improvements; this is not authorization to promote a ranker.

    Callers must independently verify held-out provenance, paired uncertainty,
    and cohort regressions before changing serving behavior.
    """

    counts = dict(sample or candidate)
    limits = {**DEFAULT_MINIMUMS, **(minimums or {})}
    reasons: list[str] = []
    for name, minimum in limits.items():
        if int(counts.get(name, 0) or 0) < int(minimum):
            reasons.append(f"{name}<{minimum}")
    deltas: dict[str, float | None] = {}
    for metric in ("auc", "precision_at_20", "ndcg_at_20"):
        candidate_value = _as_float(candidate.get(metric))
        champion_value = _as_float(champion.get(metric))
        deltas[metric] = candidate_value - champion_value if candidate_value is not None and champion_value is not None else None
    thresholds = {"auc": min_auc_delta, "precision_at_20": min_precision_delta, "ndcg_at_20": min_ndcg_delta}
    for metric, minimum in thresholds.items():
        if deltas[metric] is None or not math.isfinite(deltas[metric]) or deltas[metric] < minimum:
            reasons.append(f"{metric}_delta<{minimum}")
    return {"eligible": not reasons, "reasons": reasons, "deltas": deltas, "minimums": limits}


def _read_json_or_sqlite(path: Path) -> dict[str, Any]:
    if path.suffix.lower() == ".json":
        return json.loads(path.read_text())
    uri = path.resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as con:
        con.row_factory = sqlite3.Row
        tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        def fetch(table: str) -> list[dict[str, Any]]:
            return [dict(row) for row in con.execute(f"SELECT * FROM {table}")] if table in tables else []
        return {table: fetch(table) for table in (
            "recommendation_runs", "recommendation_impressions", "recommendation_outcomes",
        )}


_DISCOVERY_RUN_STATUSES = frozenset({"", "recommended", "all"})


def _is_discovery_run(run: Mapping[str, Any]) -> bool:
    direct_status = run.get("status")
    if direct_status is not None and (
        not isinstance(direct_status, str)
        or direct_status.strip() not in _DISCOVERY_RUN_STATUSES
    ):
        return False
    raw_metadata = run.get("metadata")
    if raw_metadata is None or raw_metadata == "":
        return True
    if isinstance(raw_metadata, str):
        try:
            metadata = json.loads(raw_metadata)
        except (TypeError, ValueError):
            return False
    elif isinstance(raw_metadata, Mapping):
        metadata = raw_metadata
    else:
        return False
    if not isinstance(metadata, Mapping):
        return False
    status = metadata.get("status")
    if status is None:
        return True
    return isinstance(status, str) and status.strip() in _DISCOVERY_RUN_STATUSES


def load_rows(source: str | Path | Mapping[str, Any]) -> list[dict[str, Any]]:
    """Load joined discovery recommendation impressions and observed outcomes."""

    data = dict(source) if isinstance(source, Mapping) else _read_json_or_sqlite(Path(source))
    runs = {str(row["id"]): row for row in data.get("recommendation_runs", [])}
    excluded_run_ids = {
        run_id for run_id, row in runs.items() if not _is_discovery_run(row)
    }
    impressions = data.get("recommendation_impressions", [])
    outcomes_by_impression: dict[Any, list[Mapping[str, Any]]] = defaultdict(list)
    for outcome in data.get("recommendation_outcomes", []):
        outcomes_by_impression[outcome.get("impression_id")].append(outcome)
    values: list[dict[str, Any]] = []
    for impression in impressions:
        run_key = str(impression.get("run_id"))
        if run_key in excluded_run_ids:
            continue
        run = runs.get(run_key, {})
        candidates = outcomes_by_impression.get(impression.get("id"), [])
        outcome = _select_primary_outcome(candidates)
        raw_confidence = outcome.get("confidence") if outcome is not None else None
        values.append({
            "run_id": impression.get("run_id"), "run_created_at": run.get("created_at"),
            "created_at": run.get("created_at"), "presented_at": impression.get("presented_at"),
            "impression_id": impression.get("id"), "candidate_id": impression.get("candidate_id"),
            "rank": impression.get("rank"), "score": impression.get("score"),
            "propensity": impression.get("propensity", 1.0),
            "label": outcome.get("label") if outcome else None,
            "confidence": (1.0 if raw_confidence is None else raw_confidence) if outcome else None,
            "label_kind": outcome.get("label_kind") if outcome else None,
            "attributed_at": outcome.get("attributed_at") if outcome else None,
            "observed_outcomes": [dict(item) for item in candidates],
        })
    return values

def build_report(
    rows: Iterable[Mapping[str, Any]],
    *,
    k: int = DEFAULT_K,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    bootstrap_seed: int = 20260920,
    minimums: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Build a JSON-serializable ranking-quality report."""

    values = list(rows)
    splits = temporal_split(values)
    report: dict[str, Any] = {
        "protocol": "temporal run split 60/20/20 with fail-closed cutoff outcome censoring; clipped propensity weighting for AUC and precision@k; unweighted gain-only NDCG@k; run-macro top-k metrics; cluster bootstrap by run",
        "sample": _metric_bundle(values, score_key="score", k=k),
        "splits": {},
        "signal_diagnostics": signal_diagnostics(values, score_key="score", k=k),
        "enjoyment": _metric_bundle(
            _enjoyment_rows(values),
            score_key="score",
            k=k,
            full_rating_certainty=True,
        ),
        "metric_scope": "Logged-policy metrics mix observed intent and outcomes; enjoyment uses explicit ratings only. Neither authorizes serving promotion.",
        "minimums": {**DEFAULT_MINIMUMS, **(minimums or {})},
    }
    for split_name, split_rows in splits.items():
        champion = evaluate_metrics(split_rows, score_key="score", k=k)
        champion["auc_bootstrap"] = cluster_bootstrap(split_rows, score_key="score", metric="auc", k=k, iterations=bootstrap_iterations, seed=bootstrap_seed)
        champion["rank_concordance_bootstrap"] = cluster_bootstrap(split_rows, score_key="score", metric="rank_concordance", k=k, iterations=bootstrap_iterations, seed=bootstrap_seed)
        report["splits"][split_name] = {"champion": champion}
    return report
