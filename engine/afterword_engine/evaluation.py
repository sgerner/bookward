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
from pathlib import Path
from typing import Any


DEFAULT_K = 20
DEFAULT_BOOTSTRAP_ITERATIONS = 1000
DEFAULT_MINIMUMS = {
    "runs": 20,
    "labeled_impressions": 200,
    "positive_impressions": 20,
    "negative_impressions": 20,
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
                return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _row_label(row: Mapping[str, Any]) -> float | None:
    label = _as_float(row.get("label"))
    return label if label is not None and 0 <= label <= 1 else None


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
) -> tuple[float, float, float] | None:
    label = _row_label(row)
    score = _as_float(row.get(score_key))
    propensity = _as_float(row.get("propensity"), default=1.0)
    confidence = _as_float(row.get("confidence"), default=1.0)
    if (
        label is None
        or score is None
        or propensity is None
        or confidence is None
        or propensity <= 0
        or propensity > 1
        or confidence < 0
    ):
        return None
    return score, label, confidence / propensity


def _weighted_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    score_key: str = "score",
) -> list[tuple[Mapping[str, Any], float, float, float]]:
    """Return ``(row, score, label, weight)`` for usable labeled rows."""

    result = []
    for row in rows:
        item = _weighted_item(row, score_key=score_key)
        if item is not None:
            score, label, weight = item
            result.append((row, score, label, weight))
    return result


def ips_auc(
    rows: Iterable[Mapping[str, Any]],
    *,
    score_key: str = "score",
    positive_threshold: float = 0.5,
) -> float | None:
    """Compute pairwise IPS AUC with confidence-adjusted propensity weights."""

    usable = _weighted_rows(rows, score_key=score_key)
    positives = [(score, weight) for _, score, label, weight in usable if label > positive_threshold]
    negatives = [(score, weight) for _, score, label, weight in usable if label < positive_threshold]
    denominator = sum(left_weight * right_weight for _, left_weight in positives for _, right_weight in negatives)
    if not positives or not negatives or denominator <= 0:
        return None
    numerator = 0.0
    for positive, positive_weight in positives:
        for negative, negative_weight in negatives:
            pair_weight = positive_weight * negative_weight
            if positive > negative:
                numerator += pair_weight
            elif positive == negative:
                numerator += pair_weight * 0.5
    return numerator / denominator


def ips_precision_at_k(
    rows: Iterable[Mapping[str, Any]],
    k: int = DEFAULT_K,
    *,
    score_key: str = "score",
    positive_threshold: float = 0.5,
) -> float | None:
    """Estimate logged top-k precision as a macro-average over runs.

    The logged rank is used for the slate position.  Top-k selection happens
    before censoring unlabeled rows, and only runs with an observed label in
    their top-k contribute to the descriptive average.  The legacy ``ips``
    name does not make this a counterfactual policy estimate.
    """

    if k <= 0:
        raise ValueError("k must be positive")
    per_run: list[float] = []
    for values in _ranked_runs(rows).values():
        topk = [(row, rank) for row, rank in values if rank <= k]
        usable = []
        for row, _rank in topk:
            item = _weighted_item(row, score_key=score_key)
            if item is not None:
                usable.append(item)
        denominator = sum(weight for _, _, weight in usable)
        if denominator > 0:
            numerator = sum(weight for _, label, weight in usable if label > positive_threshold)
            per_run.append(numerator / denominator)
    return sum(per_run) / len(per_run) if per_run else None


def ips_ndcg_at_k(
    rows: Iterable[Mapping[str, Any]],
    k: int = DEFAULT_K,
    *,
    score_key: str = "score",
) -> float | None:
    """Estimate logged NDCG@k as a macro-average over runs.

    Logged integer ranks determine both the cutoff and the discount.  Missing
    outcomes therefore leave their slots in place instead of moving a later
    observed label upward.  The legacy ``ips`` name denotes diagnostic
    confidence/propensity weighting, not a universal causal correction.
    """

    if k <= 0:
        raise ValueError("k must be positive")
    per_run: list[float] = []
    for values in _ranked_runs(rows).values():
        topk = [(row, rank) for row, rank in values if rank <= k]
        gains: list[tuple[int, float, float]] = []
        for row, rank in topk:
            item = _weighted_item(row, score_key=score_key)
            if item is not None:
                _score, label, weight = item
                gains.append((rank, label, weight))
        if not gains:
            continue
        dcg = sum(
            label * weight / math.log2(rank + 1)
            for rank, label, weight in gains
        )
        all_gains = []
        for row, _rank in values:
            item = _weighted_item(row, score_key=score_key)
            if item is not None:
                _score, label, weight = item
                all_gains.append(label * weight)
        all_gains.sort(reverse=True)
        idcg = sum(gain / math.log2(position + 2) for position, gain in enumerate(all_gains[:k]))
        if idcg > 0:
            per_run.append(dcg / idcg)
    return sum(per_run) / len(per_run) if per_run else None


# Short aliases make the metric layer convenient for callers and scripts.
weighted_auc = ips_auc
precision_at_k = ips_precision_at_k
ndcg_at_k = ips_ndcg_at_k


def _metric_bundle(rows: Sequence[Mapping[str, Any]], *, score_key: str, k: int) -> dict[str, Any]:
    usable = _weighted_rows(rows, score_key=score_key)
    labels = [label for _, _, label, _ in usable]
    return {
        "auc": ips_auc(rows, score_key=score_key),
        "precision_at_20": ips_precision_at_k(rows, k, score_key=score_key),
        "ndcg_at_20": ips_ndcg_at_k(rows, k, score_key=score_key),
        "impressions": len(rows),
        "labeled_impressions": len(usable),
        "positive_impressions": sum(label > 0.5 for label in labels),
        "negative_impressions": sum(label < 0.5 for label in labels),
        "runs": len({_run_key(row) for row in rows}),
    }


def evaluate_metrics(
    rows: Iterable[Mapping[str, Any]],
    *,
    score_key: str = "score",
    k: int = DEFAULT_K,
) -> dict[str, Any]:
    """Return IPS metrics and usable sample counts for one score column."""

    values = list(rows)
    return _metric_bundle(values, score_key=score_key, k=k)


def cluster_bootstrap(
    rows: Iterable[Mapping[str, Any]],
    *,
    score_key: str = "score",
    metric: str = "auc",
    k: int = DEFAULT_K,
    iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    seed: int = 20260920,
) -> dict[str, Any]:
    """Bootstrap a metric by resampling complete recommendation runs."""

    values = list(rows)
    clusters: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in values:
        clusters[_run_key(row)].append(row)
    metric_fn = {
        "auc": lambda sample: ips_auc(sample, score_key=score_key),
        "precision_at_20": lambda sample: ips_precision_at_k(sample, k, score_key=score_key),
        "ndcg_at_20": lambda sample: ips_ndcg_at_k(sample, k, score_key=score_key),
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


def cluster_bootstrap_ci(*args: Any, **kwargs: Any) -> tuple[float | None, float | None]:
    """Return just the lower/upper interval for integrations needing a tuple."""

    result = cluster_bootstrap(*args, **kwargs)
    return result["lower"], result["upper"]


def temporal_split(
    rows: Iterable[Mapping[str, Any]],
    *,
    train_fraction: float = 0.6,
    validation_fraction: float = 0.2,
) -> dict[str, list[Mapping[str, Any]]]:
    """Split rows chronologically by whole recommendation runs.

    A run is the atomic unit, preventing impressions from the same request
    leaking across train, validation, and test.  Missing timestamps sort
    before dated runs and remain deterministic by run id.
    """

    if train_fraction <= 0 or validation_fraction < 0 or train_fraction + validation_fraction >= 1:
        raise ValueError("fractions must satisfy 0 < train and train + validation < 1")
    values = list(rows)
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in values:
        grouped[_run_key(row)].append(row)

    def run_time(group: list[Mapping[str, Any]]) -> tuple[datetime, str]:
        dates = [_time(row.get("run_created_at") or row.get("created_at") or row.get("presented_at")) for row in group]
        dated = [date for date in dates if date is not None]
        return (min(dated) if dated else datetime.min.replace(tzinfo=timezone.utc), str(group[0].get("run_id", "")))

    ordered = sorted(grouped.values(), key=run_time)
    count = len(ordered)
    train_end = min(count, max(1 if count else 0, int(count * train_fraction)))
    validation_end = min(count, max(train_end, int(count * (train_fraction + validation_fraction))))
    return {
        "train": [row for group in ordered[:train_end] for row in group],
        "validation": [row for group in ordered[train_end:validation_end] for row in group],
        "test": [row for group in ordered[validation_end:] for row in group],
    }


def signal_diagnostics(
    rows: Iterable[Mapping[str, Any]],
    *,
    score_key: str = "score",
    k: int = DEFAULT_K,
) -> dict[str, dict[str, Any]]:
    """Report sample and IPS metrics separately for each outcome signal."""

    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get("label_kind") or row.get("signal_type") or "unknown")].append(row)
    return {signal: _metric_bundle(values, score_key=score_key, k=k) for signal, values in sorted(groups.items())}


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
    """Apply minimum sample and metric-improvement gates to a candidate ranker."""

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


def load_rows(source: str | Path | Mapping[str, Any]) -> list[dict[str, Any]]:
    """Load joined recommendation impressions and observed outcomes."""

    data = dict(source) if isinstance(source, Mapping) else _read_json_or_sqlite(Path(source))
    runs = {str(row["id"]): row for row in data.get("recommendation_runs", [])}
    impressions = data.get("recommendation_impressions", [])
    outcomes_by_impression: dict[Any, list[Mapping[str, Any]]] = defaultdict(list)
    for outcome in data.get("recommendation_outcomes", []):
        outcomes_by_impression[outcome.get("impression_id")].append(outcome)
    values: list[dict[str, Any]] = []
    for impression in impressions:
        run = runs.get(str(impression.get("run_id")), {})
        candidates = outcomes_by_impression.get(impression.get("id"), [])
        outcome = None
        if candidates:
            outcome = sorted(candidates, key=lambda item: (_as_float(item.get("confidence"), default=0), str(item.get("attributed_at") or "")), reverse=True)[0]
        values.append({
            "run_id": impression.get("run_id"), "run_created_at": run.get("created_at"),
            "created_at": run.get("created_at"), "presented_at": impression.get("presented_at"),
            "impression_id": impression.get("id"), "candidate_id": impression.get("candidate_id"),
            "rank": impression.get("rank"), "score": impression.get("score"),
            "propensity": impression.get("propensity", 1.0),
            "label": outcome.get("label") if outcome else None,
            "confidence": outcome.get("confidence", 1.0) if outcome else None,
            "label_kind": outcome.get("label_kind") if outcome else None,
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
        "protocol": "temporal run split 60/20/20; descriptive confidence/propensity weighting; run-macro top-k metrics; cluster bootstrap by run",
        "sample": _metric_bundle(values, score_key="score", k=k),
        "splits": {},
        "signal_diagnostics": signal_diagnostics(values, score_key="score", k=k),
        "minimums": {**DEFAULT_MINIMUMS, **(minimums or {})},
    }
    for split_name, split_rows in splits.items():
        champion = evaluate_metrics(split_rows, score_key="score", k=k)
        champion["auc_bootstrap"] = cluster_bootstrap(split_rows, score_key="score", metric="auc", k=k, iterations=bootstrap_iterations, seed=bootstrap_seed)
        report["splits"][split_name] = {"champion": champion}
    return report
