"""Offline evaluation for recommendation impressions and shadow rankings.

All metrics in this module are read-only calculations over exported rows.  A
missing outcome is censored and is excluded from the metric; it is never
silently converted to a negative label.  Inverse propensity weights are
``confidence / propensity`` and every bootstrap resample is made at the
recommendation-run level to preserve within-run dependence.
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


def _weighted_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    score_key: str = "score",
) -> list[tuple[Mapping[str, Any], float, float, float]]:
    """Return ``(row, score, label, weight)`` for usable labeled rows."""

    result = []
    for row in rows:
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
            continue
        result.append((row, score, label, confidence / propensity))
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
    """Estimate top-k precision using IPS weights.

    The logged rank is used for the slate position.  Rows without outcomes are
    censored, so the denominator is the total IPS weight of observed labels in
    the top-k rather than an invented full-slate negative count.
    """

    if k <= 0:
        raise ValueError("k must be positive")
    usable = _weighted_rows(rows, score_key=score_key)
    ranked = sorted(usable, key=lambda item: (_as_float(item[0].get("rank"), default=10**9), -item[1]))[:k]
    denominator = sum(weight for _, _, _, weight in ranked)
    if denominator <= 0:
        return None
    numerator = sum(weight for _, _, label, weight in ranked if label > positive_threshold)
    return numerator / denominator


def ips_ndcg_at_k(
    rows: Iterable[Mapping[str, Any]],
    k: int = DEFAULT_K,
    *,
    score_key: str = "score",
) -> float | None:
    """Estimate normalized DCG@k using inverse-propensity gains."""

    if k <= 0:
        raise ValueError("k must be positive")
    usable = _weighted_rows(rows, score_key=score_key)
    ranked = sorted(usable, key=lambda item: (_as_float(item[0].get("rank"), default=10**9), -item[1]))[:k]
    if not ranked:
        return None
    discounts = [1.0 / math.log2(position + 2) for position in range(k)]
    dcg = sum(label * weight * discounts[position] for position, (_, _, label, weight) in enumerate(ranked))
    # The ideal list uses the observed gains and weights, sorted independently
    # of rank.  This keeps the normalization bounded while preserving IPS.
    ideal = sorted((label * weight for _, _, label, weight in usable), reverse=True)[:k]
    idcg = sum(gain * discounts[position] for position, gain in enumerate(ideal))
    return dcg / idcg if idcg > 0 else None


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
        "runs": len({str(row.get("run_id")) for row in rows if row.get("run_id") is not None}),
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
        clusters[str(row.get("run_id", "__missing_run__"))].append(row)
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
        metric_value = metric_fn([row for cluster in sampled for row in cluster])
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
        grouped[str(row.get("run_id", "__missing_run__"))].append(row)

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
    """Apply minimum sample and metric-improvement gates to a shadow model."""

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
            "llm_runs", "llm_scores",
        )}


def load_rows(source: str | Path | Mapping[str, Any], *, llm_run_id: str | None = None) -> list[dict[str, Any]]:
    """Load joined impression/outcome rows and optional as-of shadow scores."""

    data = dict(source) if isinstance(source, Mapping) else _read_json_or_sqlite(Path(source))
    runs = {str(row["id"]): row for row in data.get("recommendation_runs", [])}
    impressions = data.get("recommendation_impressions", [])
    outcomes_by_impression: dict[Any, list[Mapping[str, Any]]] = defaultdict(list)
    for outcome in data.get("recommendation_outcomes", []):
        outcomes_by_impression[outcome.get("impression_id")].append(outcome)
    llm_runs = [row for row in data.get("llm_runs", []) if row.get("status") in (None, "complete")]
    llm_scores = data.get("llm_scores", [])
    explicit_scores: dict[Any, dict[Any, Mapping[str, Any]]] = defaultdict(dict)
    for score in llm_scores:
        explicit_scores[score.get("run_id")][score.get("candidate_id")] = score
    rows: list[dict[str, Any]] = []
    for impression in impressions:
        run = runs.get(str(impression.get("run_id")), {})
        candidates = outcomes_by_impression.get(impression.get("id"), [])
        # Multiple signals can be attached to an impression.  Choose the most
        # confident/latest signal for the primary ranking metric; diagnostics
        # still expose signal-specific metrics from those same records below.
        outcome = None
        if candidates:
            outcome = sorted(candidates, key=lambda item: (_as_float(item.get("confidence"), default=0), str(item.get("attributed_at") or "")), reverse=True)[0]
        value = {
            "run_id": impression.get("run_id"),
            "run_created_at": run.get("created_at"),
            "created_at": run.get("created_at"),
            "presented_at": impression.get("presented_at"),
            "impression_id": impression.get("id"),
            "candidate_id": impression.get("candidate_id"),
            "rank": impression.get("rank"),
            "score": impression.get("score"),
            "propensity": impression.get("propensity", 1.0),
            "label": outcome.get("label") if outcome else None,
            "confidence": outcome.get("confidence", 1.0) if outcome else None,
            "label_kind": outcome.get("label_kind") if outcome else None,
        }
        if llm_run_id:
            score = explicit_scores.get(llm_run_id, {}).get(impression.get("candidate_id"))
            value["shadow_score"] = score.get("score") if score else None
            value["shadow_run_id"] = llm_run_id if score else None
        else:
            # As-of selection prevents a later shadow run from leaking into an
            # earlier impression.  If timestamps are absent, leave it missing.
            exposure_time = _time(impression.get("presented_at"))
            eligible = [run for run in llm_runs if _time(run.get("created_at")) and exposure_time and _time(run.get("created_at")) <= exposure_time]
            eligible.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("id"))), reverse=True)
            score = None
            selected = None
            for selected in eligible:
                score = explicit_scores.get(selected.get("id"), {}).get(impression.get("candidate_id"))
                if score:
                    break
            value["shadow_score"] = score.get("score") if score else None
            value["shadow_run_id"] = selected.get("id") if score and selected else None
        rows.append(value)
    return rows


def build_report(
    rows: Iterable[Mapping[str, Any]],
    *,
    llm_score_key: str = "shadow_score",
    k: int = DEFAULT_K,
    bootstrap_iterations: int = DEFAULT_BOOTSTRAP_ITERATIONS,
    bootstrap_seed: int = 20260920,
    minimums: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Build a JSON-serializable champion/shadow report."""

    values = list(rows)
    splits = temporal_split(values)
    report: dict[str, Any] = {
        "protocol": "temporal run split 60/20/20; IPS confidence/propensity weighting; cluster bootstrap by run",
        "sample": _metric_bundle(values, score_key="score", k=k),
        "splits": {},
        "signal_diagnostics": signal_diagnostics(values, score_key="score", k=k),
        "shadow_signal_diagnostics": signal_diagnostics(
            [row for row in values if _as_float(row.get(llm_score_key)) is not None],
            score_key=llm_score_key,
            k=k,
        ),
    }
    for split_name, split_rows in splits.items():
        champion = evaluate_metrics(split_rows, score_key="score", k=k)
        champion["auc_bootstrap"] = cluster_bootstrap(split_rows, score_key="score", metric="auc", k=k, iterations=bootstrap_iterations, seed=bootstrap_seed)
        shadow_rows = [row for row in split_rows if _as_float(row.get(llm_score_key)) is not None]
        shadow = evaluate_metrics(shadow_rows, score_key=llm_score_key, k=k)
        shadow["auc_bootstrap"] = cluster_bootstrap(shadow_rows, score_key=llm_score_key, metric="auc", k=k, iterations=bootstrap_iterations, seed=bootstrap_seed)
        shadow["available_impressions"] = len(shadow_rows)
        report["splits"][split_name] = {"champion": champion, "shadow_llm": shadow}
    test = report["splits"]["test"]
    report["promotion_gate"] = promotion_gate(
        test["shadow_llm"],
        test["champion"],
        sample=test["shadow_llm"],
        minimums=minimums,
    )
    return report
