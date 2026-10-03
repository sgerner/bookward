#!/usr/bin/env python3
"""Helpers for preregistered tail-head and pairwise ranking evaluations.

This evaluator is designed to read an ID-aligned private feature artifact and
write aggregate metrics only. Corpus loading and the frozen artifact schema are
kept outside the fitting helpers so callers can audit row alignment explicitly.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sys

import numpy as np
from scipy.optimize import minimize
from scipy.special import expit
from scipy.stats import rankdata
from sklearn.linear_model import LogisticRegression

ENGINE = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(ENGINE))
import evaluate_ordinal_interests as ordinal


TAIL_HEAD_C_GRID = (0.1, 1.0, 10.0)
PAIRWISE_C_GRID = (0.1, 1.0, 10.0)
PAIRWISE_CORRECTION_CAP_SD = 0.25
PAIRWISE_CORRECTION_CAP_POINTS = 5.0


@dataclass(frozen=True)
class FeatureScaler:
    mean: np.ndarray
    scale: np.ndarray


@dataclass(frozen=True)
class TailHeads:
    scaler: FeatureScaler
    high_model: LogisticRegression
    low_model: LogisticRegression
    c: float
    train_rows: int


@dataclass(frozen=True)
class PairwiseRanker:
    feature_scaler: FeatureScaler
    current_mean: float
    current_scale: float
    coefficients: np.ndarray
    c: float
    correction_cap_points: float
    train_rows: int
    comparison_pairs: int
    objective: float
    optimizer_iterations: int


@dataclass(frozen=True)
class OOFSelection:
    selected_c: float
    candidate_metrics: dict[float, dict]
    baseline_metrics: dict


@dataclass(frozen=True)
class FeatureArtifact:
    read_ids: np.ndarray
    ratings: np.ndarray
    utc_day: np.ndarray
    author_group: np.ndarray
    current: np.ndarray
    base_features: np.ndarray
    signals: dict[str, np.ndarray]
    train_mask: np.ndarray
    validation_mask: np.ndarray
    later_mask: np.ndarray
    oof_selection_mask: np.ndarray
    optional_scores: dict[str, np.ndarray]
    optional_masks: dict[str, np.ndarray]
    extra_arrays: dict[str, np.ndarray]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def development_folds_from_manifest(
    artifact: FeatureArtifact,
    manifest: dict,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Read and verify the frozen chronological fold cuts."""
    n = len(artifact.ratings)
    folds = []
    for row in manifest.get("development_folds", []):
        start = int(row["evaluation_start"])
        stop = int(row["evaluation_stop"])
        train_stop = int(row["train_stop"])
        if not (0 < train_stop == start < stop <= n):
            raise ValueError("development fold bounds are invalid")
        train = np.arange(train_stop, dtype=np.int64)
        evaluate = np.arange(start, stop, dtype=np.int64)
        if not artifact.train_mask[train].all() or not artifact.train_mask[evaluate].all():
            raise ValueError("development folds must remain inside the authoritative train mask")
        if artifact.utc_day[train[-1]] >= artifact.utc_day[evaluate[0]]:
            raise ValueError("development fold boundary splits a UTC calendar day")
        folds.append((train, evaluate))
    validated = _validated_folds(folds, n)
    if len(validated) != 3:
        raise ValueError("frozen protocol requires exactly three development folds")
    covered = np.zeros(n, dtype=bool)
    for _, evaluate in validated:
        covered[evaluate] = True
    if not np.array_equal(covered, artifact.oof_selection_mask):
        raise ValueError("development folds do not match the artifact OOF selection mask")
    return validated


def validate_feature_manifest(
    feature_path: Path,
    manifest_path: Path,
    protocol: dict,
    artifact: FeatureArtifact,
) -> dict:
    manifest = json.loads(Path(manifest_path).read_text())
    if manifest.get("feature_artifact_sha256") != file_sha256(feature_path):
        raise ValueError("feature manifest hash does not match its NPZ archive")
    if manifest.get("corpus_sha256") != protocol.get("corpus_sha256"):
        raise ValueError("feature manifest and frozen protocol corpus hashes differ")
    if manifest.get("eligible_targets") != len(artifact.ratings):
        raise ValueError("feature manifest target count differs from its arrays")
    if manifest.get("exact_ranker_parity", {}).get("mismatches_at_served_precision") != 0:
        raise ValueError("feature artifact does not confirm exact current ranker parity")
    if tuple(manifest.get("feature_names", ())) != tuple(ordinal.BASE_NAMES):
        raise ValueError("feature manifest names or order differ from the causal ranker inputs")
    expected_counts = manifest.get("split_counts", {})
    actual_counts = {
        "development": int(artifact.train_mask.sum()),
        "validation": int(artifact.validation_mask.sum()),
        "later_exploratory": int(artifact.later_mask.sum()),
    }
    if expected_counts != actual_counts:
        raise ValueError("feature manifest counts differ from authoritative split masks")
    development_folds_from_manifest(artifact, manifest)
    return manifest


def validate_protocol(protocol: dict) -> None:
    families = protocol.get("families", {})
    heads = families.get("tail_heads", {})
    pairwise = families.get("pairwise", {})
    if tuple(float(value) for value in heads.get("C", ())) != TAIL_HEAD_C_GRID:
        raise ValueError("tail-head C grid differs from the frozen protocol")
    if tuple(float(value) for value in pairwise.get("C", ())) != PAIRWISE_C_GRID:
        raise ValueError("pairwise C grid differs from the frozen protocol")
    if heads.get("class_weight") is not None:
        raise ValueError("tail-head class weighting differs from the frozen protocol")
    pair_definition = re.sub(r"\s+", "", pairwise.get("pairs", "")).lower()
    if pair_definition != "allunequal-ratingunorderedtrainingpairs,meanloss,nopair-iidclaims":
        raise ValueError("pairwise comparison definition differs from the frozen protocol")
    if "0.5||beta||²/C" not in pairwise.get("loss", ""):
        raise ValueError("pairwise regularization differs from the frozen protocol")
    correction = re.sub(r"\s+", "", pairwise.get("serving_correction", "")).lower()
    if "min(5points,.25trainingcurrentsd)" not in correction:
        raise ValueError("pairwise serving correction cap differs from the frozen protocol")
    selection = re.sub(r"\s+", "", protocol.get("grid_selection", "")).lower()
    if "meanbalancedauc" not in selection or "3developmentoof" not in selection:
        raise ValueError("grid selection must use frozen development-fold mean balanced AUC")


def _round_served(values: np.ndarray) -> np.ndarray:
    values = np.clip(np.asarray(values, dtype=np.float64), 0.0, 100.0)
    return np.fromiter((round(float(value), 1) for value in values), dtype=np.float64,
                       count=len(values))


def _fit_alignment(candidate_train: np.ndarray, current_train: np.ndarray) -> dict[str, float]:
    candidate_train = np.asarray(candidate_train, dtype=np.float64)
    current_train = np.asarray(current_train, dtype=np.float64)
    if (candidate_train.ndim != 1 or current_train.shape != candidate_train.shape
            or not np.isfinite(candidate_train).all() or not np.isfinite(current_train).all()):
        raise ValueError("alignment fit scores must be equal-length finite vectors")
    if not len(candidate_train):
        raise ValueError("alignment requires training predictions")
    candidate_scale = float(candidate_train.std())
    current_scale = float(current_train.std())
    return {
        "candidate_mean": float(candidate_train.mean()),
        "candidate_scale": candidate_scale if candidate_scale >= 1e-8 else 1.0,
        "current_mean": float(current_train.mean()),
        "current_scale": current_scale if current_scale >= 1e-8 else 1.0,
    }


def _apply_alignment(values: np.ndarray, alignment: dict[str, float]) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64)
    if not np.isfinite(values).all():
        raise ValueError("aligned scores must be finite")
    return _round_served(
        alignment["current_mean"]
        + (values - alignment["candidate_mean"])
        * alignment["current_scale"] / alignment["candidate_scale"]
    )


def _fixed_oof_alignment(
    raw_scores: np.ndarray,
    current_scores: np.ndarray,
    selection_mask: np.ndarray,
) -> dict[str, float]:
    mask = np.asarray(selection_mask, dtype=bool)
    raw = np.asarray(raw_scores, dtype=np.float64)
    current = np.asarray(current_scores, dtype=np.float64)
    if (raw.shape != mask.shape or current.shape != mask.shape
            or not mask.any() or not np.isfinite(raw[mask]).all()
            or not np.isfinite(current[mask]).all()):
        raise ValueError("OOF alignment rows must be finite and aligned")
    return _fit_alignment(raw[mask], current[mask])


def _current_scale(scores: np.ndarray) -> float:
    scale = float(np.asarray(scores, dtype=np.float64).std())
    return scale if scale >= 1e-8 else 1.0


def _signal_z_for_segment(
    artifact: FeatureArtifact,
    name: str,
    fit_indexes: np.ndarray,
    score_indexes: np.ndarray,
) -> tuple[np.ndarray, dict, bool]:
    values = artifact.signals[name]
    fit_indexes = np.asarray(fit_indexes, dtype=np.int64)
    score_indexes = np.asarray(score_indexes, dtype=np.int64)
    fit_override = None
    used_in_sample_ordinal_for_scaler = False
    if name == "ordinal" and not np.isfinite(values[fit_indexes]).any():
        # The frozen artifact has no OOF ordinal predictions before row 300.
        # For the first fold only, use same-prefix fitted values solely to
        # estimate this signal's train scaler; eval predictions remain artifact OOF.
        model = ordinal.fit_ordinal(
            artifact.base_features[fit_indexes], artifact.ratings[fit_indexes]
        )
        fit_override = ordinal.ordinal_expected_ratings(
            model, artifact.base_features[fit_indexes]
        )
        used_in_sample_ordinal_for_scaler = True
    standardized, scaler = train_standardize(
        values, fit_indexes, fit_values=fit_override
    )
    result = standardized[score_indexes]
    if not np.isfinite(result).all():
        raise ValueError(f"signal lacks finite evaluation values: {name}")
    return result, scaler, used_in_sample_ordinal_for_scaler


def _registered_arm_names() -> tuple[str, ...]:
    return (
        "current",
        "heads",
        "pairwise",
        "heads_pairwise",
        "heads_with_current25",
        "pairwise_with_current25",
        "current_plus_recency",
        "current_plus_cluster3",
        "current_plus_ordinal",
        "current_plus_recency_cluster3",
        "heads_plus_recency",
        "heads_plus_cluster3",
        "heads_plus_ordinal",
        "heads_plus_recency_cluster3",
        "pairwise_plus_recency",
        "pairwise_plus_cluster3",
        "pairwise_plus_ordinal",
        "pairwise_plus_recency_cluster3",
    )


def _segment_arms(
    current: np.ndarray,
    heads: np.ndarray,
    pairwise: np.ndarray,
    signal_z: dict[str, np.ndarray],
    current_scale: float,
    *,
    raw_heads: np.ndarray | None = None,
    raw_pairwise: np.ndarray | None = None,
) -> dict[str, np.ndarray]:
    current = np.asarray(current, dtype=np.float64)
    heads = np.asarray(heads, dtype=np.float64)
    pairwise = np.asarray(pairwise, dtype=np.float64)
    if current.shape != heads.shape or current.shape != pairwise.shape:
        raise ValueError("primary score vectors must share a segment shape")
    standalone_heads = heads if raw_heads is None else np.asarray(raw_heads, dtype=np.float64)
    standalone_pairwise = pairwise if raw_pairwise is None else np.asarray(raw_pairwise, dtype=np.float64)
    if standalone_heads.shape != current.shape or standalone_pairwise.shape != current.shape:
        raise ValueError("raw standalone scores must share the aligned segment shape")
    names = _registered_arm_names()
    result = {
        "current": current.copy(),
        "heads": standalone_heads.copy(),
        "pairwise": standalone_pairwise.copy(),
        "heads_pairwise": _round_served(0.5 * heads + 0.5 * pairwise),
        "heads_with_current25": _round_served(0.25 * heads + 0.75 * current),
        "pairwise_with_current25": _round_served(0.25 * pairwise + 0.75 * current),
    }
    expected_signal_names = {"recency", "cluster", "ordinal"}
    if set(signal_z) != expected_signal_names:
        raise ValueError("signal blend requires recency, cluster, and ordinal")
    for signal_name, output_name in (
        ("recency", "recency"), ("cluster", "cluster3"), ("ordinal", "ordinal")
    ):
        delta = 0.25 * current_scale * np.asarray(signal_z[signal_name], dtype=np.float64)
        if delta.shape != current.shape or not np.isfinite(delta).all():
            raise ValueError(f"standardized signal does not align: {signal_name}")
        result[f"current_plus_{output_name}"] = _round_served(current + delta)
        result[f"heads_plus_{output_name}"] = _round_served(heads + delta)
        result[f"pairwise_plus_{output_name}"] = _round_served(pairwise + delta)
    recency_cluster = signal_z["recency"] + signal_z["cluster"]
    result["current_plus_recency_cluster3"] = _round_served(
        current + 0.25 * current_scale * recency_cluster
    )
    result["heads_plus_recency_cluster3"] = _round_served(
        heads + 0.25 * current_scale * recency_cluster
    )
    result["pairwise_plus_recency_cluster3"] = _round_served(
        pairwise + 0.25 * current_scale * recency_cluster
    )
    if set(result) != set(names):
        raise RuntimeError("registered arm construction is incomplete")
    return result


def _metric_report(ratings: np.ndarray, scores: np.ndarray) -> dict:
    report = ordinal.metric_set(ratings, scores)
    high, low = report["auc_high_4plus"], report["auc_low_2or_less_reversed"]
    report["balanced_high_low_auc"] = (
        float((high + low) / 2.0) if high is not None and low is not None else None
    )
    return report


def _metric_delta(reference: dict, candidate: dict) -> dict:
    result = {}
    for name, key in (
        ("high_auc", "auc_high_4plus"),
        ("low_auc", "auc_low_2or_less_reversed"),
        ("balanced_auc", "balanced_high_low_auc"),
        ("high_vs_low_auc", "auc_high_vs_low_excluding_neutral"),
        ("top20_high_count", "top20_high_count"),
        ("bottom20_low_count", "bottom20_low_count"),
    ):
        left, right = reference.get(key), candidate.get(key)
        result[name] = float(right - left) if isinstance(left, (int, float)) and isinstance(right, (int, float)) else None
    return result


def _temporal_halves(artifact: FeatureArtifact, indexes: np.ndarray, scores: np.ndarray) -> dict:
    indexes = np.asarray(indexes, dtype=np.int64)
    dates = artifact.utc_day[indexes]
    unique_days = np.unique(dates)
    if len(unique_days) < 2:
        return {"status": "insufficient_days"}
    boundary = int(unique_days[len(unique_days) // 2])
    first = dates < boundary
    second = ~first
    values = np.asarray(scores, dtype=np.float64)
    ratings = artifact.ratings[indexes]
    return {
        "split_boundary_utc_day": boundary,
        "first_half": _metric_report(ratings[first], values[first]),
        "second_half": _metric_report(ratings[second], values[second]),
    }


def _oof_report(
    artifact: FeatureArtifact,
    folds: list[tuple[np.ndarray, np.ndarray]],
    scores: dict[str, np.ndarray],
) -> dict:
    arms = {}
    per_fold = []
    for fold_number, (_, indexes) in enumerate(folds, start=1):
        fold_results = {}
        for name, values in scores.items():
            candidate = np.asarray(values)[indexes]
            if not np.isfinite(candidate).all():
                raise ValueError(f"OOF scores are missing for {name} in fold {fold_number}")
            fold_results[name] = _metric_report(artifact.ratings[indexes], candidate)
        per_fold.append({"fold": fold_number, "targets": int(len(indexes)), "arms": fold_results})
    for name in scores:
        metrics = [fold["arms"][name] for fold in per_fold]
        arms[name] = {
            "mean_balanced_high_low_auc": float(np.mean([row["balanced_high_low_auc"] for row in metrics])),
            "mean_high_auc": float(np.mean([row["auc_high_4plus"] for row in metrics])),
            "mean_low_reversed_auc": float(np.mean([row["auc_low_2or_less_reversed"] for row in metrics])),
            "fold_metrics": metrics,
        }
    return {
        "targets": int(sum(len(indexes) for _, indexes in folds)),
        "aggregation": "unweighted mean of the three within-fold balanced AUCs",
        "folds": per_fold,
        "arms": arms,
    }


def _period_report(
    artifact: FeatureArtifact,
    indexes: np.ndarray,
    scores: dict[str, np.ndarray],
    *,
    references: dict[str, np.ndarray],
) -> dict:
    indexes = np.asarray(indexes, dtype=np.int64)
    ratings = artifact.ratings[indexes]
    reference_metrics = {
        name: _metric_report(ratings, np.asarray(values)[indexes])
        for name, values in references.items()
    }
    arms = {}
    for name, values in scores.items():
        candidate = np.asarray(values)[indexes]
        if not np.isfinite(candidate).all():
            raise ValueError(f"period scores are missing for {name}")
        metrics = _metric_report(ratings, candidate)
        arms[name] = {
            "metrics": metrics,
            "deltas_vs_references": {
                reference_name: _metric_delta(reference_metrics[reference_name], metrics)
                for reference_name in references
            },
            "temporal_halves_vs_current": _temporal_halves(artifact, indexes, candidate),
        }
    return {
        "targets": int(len(indexes)),
        "references": reference_metrics,
        "arms": arms,
    }


def _classifier_fit_summary(model: TailHeads) -> dict:
    return {
        "C": float(model.c),
        "train_rows": int(model.train_rows),
        "high_optimizer_iterations": int(model.high_model.n_iter_[0]),
        "low_optimizer_iterations": int(model.low_model.n_iter_[0]),
        "feature_mean": model.scaler.mean.tolist(),
        "feature_scale": model.scaler.scale.tolist(),
    }


def _pairwise_fit_summary(model: PairwiseRanker) -> dict:
    return {
        "C": float(model.c),
        "train_rows": int(model.train_rows),
        "unequal_rating_pairs": int(model.comparison_pairs),
        "mean_logistic_plus_l2_objective": float(model.objective),
        "optimizer_iterations": int(model.optimizer_iterations),
        "current_train_mean": float(model.current_mean),
        "current_train_scale": float(model.current_scale),
        "serving_correction_cap_points": float(model.correction_cap_points),
    }


def evaluate_feature_view(
    artifact: FeatureArtifact,
    manifest: dict,
    *,
    view_name: str,
) -> tuple[dict, dict[str, np.ndarray]]:
    """Fit frozen tail families and export aligned OOF/validation/later arrays."""
    folds = development_folds_from_manifest(artifact, manifest)
    train = np.flatnonzero(artifact.train_mask)
    validation = np.flatnonzero(artifact.validation_mask)
    later = np.flatnonzero(artifact.later_mask)
    fit_later = np.concatenate((train, validation))
    x, y, current = artifact.base_features, artifact.ratings, artifact.current

    # C is selected only on the three frozen OOF folds. Validation confirms the
    # selected model; it never chooses a runner-up.
    heads_selection = select_tail_heads_c_oof(x, y, folds)
    pairwise_selection = select_pairwise_c_oof(x, y, current, folds)
    selected_heads = np.full(len(y), np.nan, dtype=np.float64)
    selected_pairwise = np.full(len(y), np.nan, dtype=np.float64)
    aligned_heads = np.full(len(y), np.nan, dtype=np.float64)
    aligned_pairwise = np.full(len(y), np.nan, dtype=np.float64)
    fold_alignment = []
    fold_fit_summary = []

    for fold_number, (fold_train, fold_eval) in enumerate(folds, start=1):
        head_model = fit_tail_heads(x[fold_train], y[fold_train], heads_selection.selected_c)
        head_train_raw = score_tail_heads(head_model, x[fold_train])
        head_eval_raw = score_tail_heads(head_model, x[fold_eval])
        head_fit_alignment = _fit_alignment(head_train_raw, current[fold_train])
        head_eval_aligned = _apply_alignment(head_eval_raw, head_fit_alignment)

        pairwise_model = fit_pairwise_ranker(
            x[fold_train], y[fold_train], current[fold_train], pairwise_selection.selected_c
        )
        pair_train_raw = score_pairwise_ranker(pairwise_model, x[fold_train], current[fold_train])
        pair_eval_raw = score_pairwise_ranker(pairwise_model, x[fold_eval], current[fold_eval])
        pair_fit_alignment = _fit_alignment(pair_train_raw, current[fold_train])
        pair_eval_aligned = _apply_alignment(pair_eval_raw, pair_fit_alignment)

        selected_heads[fold_eval] = head_eval_raw
        selected_pairwise[fold_eval] = pair_eval_raw
        aligned_heads[fold_eval] = head_eval_aligned
        aligned_pairwise[fold_eval] = pair_eval_aligned
        fold_alignment.append({
            "fold": int(fold_number),
            "train_rows": int(len(fold_train)),
            "evaluation_rows": int(len(fold_eval)),
            "heads": head_fit_alignment,
            "pairwise": pair_fit_alignment,
        })
        fold_fit_summary.append({
            "fold": int(fold_number),
            "heads": _classifier_fit_summary(head_model),
            "pairwise": _pairwise_fit_summary(pairwise_model),
        })

    heads_oof_alignment = _fixed_oof_alignment(selected_heads, current, artifact.oof_selection_mask)
    pairwise_oof_alignment = _fixed_oof_alignment(selected_pairwise, current, artifact.oof_selection_mask)

    heads_validation_model = fit_tail_heads(x[train], y[train], heads_selection.selected_c)
    selected_heads[validation] = score_tail_heads(heads_validation_model, x[validation])
    aligned_heads[validation] = _apply_alignment(
        selected_heads[validation], heads_oof_alignment
    )
    pairwise_validation_model = fit_pairwise_ranker(
        x[train], y[train], current[train], pairwise_selection.selected_c
    )
    selected_pairwise[validation] = score_pairwise_ranker(
        pairwise_validation_model, x[validation], current[validation]
    )
    aligned_pairwise[validation] = _apply_alignment(
        selected_pairwise[validation], pairwise_oof_alignment
    )

    heads_later_model = fit_tail_heads(x[fit_later], y[fit_later], heads_selection.selected_c)
    selected_heads[later] = score_tail_heads(heads_later_model, x[later])
    aligned_heads[later] = _apply_alignment(selected_heads[later], heads_oof_alignment)
    pairwise_later_model = fit_pairwise_ranker(
        x[fit_later], y[fit_later], current[fit_later], pairwise_selection.selected_c
    )
    selected_pairwise[later] = score_pairwise_ranker(
        pairwise_later_model, x[later], current[later]
    )
    aligned_pairwise[later] = _apply_alignment(selected_pairwise[later], pairwise_oof_alignment)

    scores = {name: np.full(len(y), np.nan, dtype=np.float64)
              for name in _registered_arm_names()}
    signal_scalers = []
    ordinal_scaler_in_sample_rows = []

    def fill_segment(indexes: np.ndarray, fit_indexes: np.ndarray, *, segment_name: str) -> None:
        nonlocal signal_scalers, ordinal_scaler_in_sample_rows
        signal_z = {}
        segment_scalers = {}
        fallback = False
        for signal_name in ("recency", "cluster", "ordinal"):
            z, scaler, used_fallback = _signal_z_for_segment(
                artifact, signal_name, fit_indexes, indexes
            )
            signal_z[signal_name] = z
            segment_scalers[signal_name] = scaler
            if used_fallback:
                fallback = True
        current_scale = _current_scale(current[fit_indexes])
        segment = _segment_arms(
            current[indexes], aligned_heads[indexes], aligned_pairwise[indexes],
            signal_z, current_scale,
            raw_heads=selected_heads[indexes],
            raw_pairwise=selected_pairwise[indexes],
        )
        for name, values in segment.items():
            scores[name][indexes] = values
        signal_scalers.append({
            "segment": segment_name,
            "fit_rows": int(len(fit_indexes)),
            "score_rows": int(len(indexes)),
            "current_train_scale": current_scale,
            "signals": segment_scalers,
            "ordinal_fit_only_scaler_fallback": bool(fallback),
        })
        if fallback:
            ordinal_scaler_in_sample_rows.append(int(len(fit_indexes)))

    for fold_number, (fold_train, fold_eval) in enumerate(folds, start=1):
        fill_segment(fold_eval, fold_train, segment_name=f"oof_fold_{fold_number}")
    fill_segment(validation, train, segment_name="validation_fit_development")
    fill_segment(later, fit_later, segment_name="later_fit_development_plus_validation")

    if any(not np.isfinite(values[artifact.oof_selection_mask]).all() for values in scores.values()):
        raise RuntimeError("a registered arm lacks an OOF score")
    if any(not np.isfinite(values[validation]).all() for values in scores.values()):
        raise RuntimeError("a registered arm lacks a validation score")
    if any(not np.isfinite(values[later]).all() for values in scores.values()):
        raise RuntimeError("a registered arm lacks a later score")

    references = {"current": current}
    for name in ("matched_query_control", "primary_current"):
        if name in artifact.optional_scores:
            references[name] = artifact.optional_scores[name]
    oof_report = _oof_report(artifact, folds, scores)
    validation_report = _period_report(artifact, validation, scores, references=references)
    later_report = _period_report(artifact, later, scores, references=references)
    reference_labels = {
        "current": "scorer current for this feature view",
        "matched_query_control": "same rich query with original title-author history",
        "primary_current": "original primary title-author history and query scorer",
    }
    report = {
        "view": view_name,
        "features": "ten causal BASE_NAMES; scaling fitted separately inside each training prefix",
        "selected_configurations": {
            "tail_heads_C": float(heads_selection.selected_c),
            "pairwise_C": float(pairwise_selection.selected_c),
            "configuration_selection": "unweighted mean balanced AUC across three OOF folds; no validation runner-up switching",
        },
        "oof_c_grid": {
            "tail_heads": {str(key): value for key, value in heads_selection.candidate_metrics.items()},
            "pairwise": {str(key): value for key, value in pairwise_selection.candidate_metrics.items()},
            "pairwise_current_control": pairwise_selection.baseline_metrics,
        },
        "fit_details": {
            "development_oof_folds": fold_fit_summary,
            "tail_heads_validation_fit": _classifier_fit_summary(heads_validation_model),
            "tail_heads_later_fit": _classifier_fit_summary(heads_later_model),
            "pairwise_validation_fit": _pairwise_fit_summary(pairwise_validation_model),
            "pairwise_later_fit": _pairwise_fit_summary(pairwise_later_model),
        },
        "score_alignment": {
            "oof_fold_fit_only": fold_alignment,
            "fixed_development_oof_rows_for_validation_and_later": {
                "heads": heads_oof_alignment,
                "pairwise": pairwise_oof_alignment,
            },
            "method": "OOF scores align to current using only each fold's preceding fit prefix; validation/later use one fixed affine map fit on all OOF selection rows",
        },
        "signal_scaling": {
            "rule": "0.25 times training-only standardized recency, cluster3, or ordinal signal on the current-score training SD",
            "predeclared_fixed_signal_arms": [
                "current_plus_recency_cluster3",
                "heads_plus_recency_cluster3",
                "pairwise_plus_recency_cluster3",
            ],
            "segments": signal_scalers,
            "ordinal_first_fold_scaler_rows": ordinal_scaler_in_sample_rows,
            "ordinal_caveat": "For the first fold only, an ordinal model fit on that fold's training prefix predicts the same prefix solely to estimate scaler mean/SD because the frozen ordinal signal is missing there; this does not replace evaluation predictions or backfill the artifact.",
        },
        "reference_meanings": reference_labels,
        "oof_results": oof_report,
        "validation_results": validation_report,
        "later_exploratory_results": later_report,
        "limits": [
            "Single-reader retrospective ratings are not live recommendation outcomes.",
            "The later period was inspected previously and is descriptive, not independent confirmation.",
            "Enriched rich-view vectors were collected later; rich-view history is not an as-of historical replay.",
            "Head probabilities are model outputs and are not calibrated enjoyment likelihoods.",
            "Pairwise comparisons are optimization terms; uncertainty is measured on books/days, never pairs.",
            "Unseen-author familiarity requires the full prepared corpus including the warmup history and is reported by the root aggregate.",
        ],
    }

    score_archive = {
        "read_ids": artifact.read_ids,
        "train_mask": artifact.train_mask,
        "validation_mask": artifact.validation_mask,
        "later_mask": artifact.later_mask,
        "oof_selection_mask": artifact.oof_selection_mask,
        "current": current,
        "selected_heads": selected_heads,
        "selected_pairwise": selected_pairwise,
        "aligned_heads": aligned_heads,
        "aligned_pairwise": aligned_pairwise,
    }
    for name, values in scores.items():
        # The loader already seeds its score table with the complete frozen
        # current vector, including fit-only rows that have no model OOF score.
        score_archive[f"score__{name}"] = current if name == "current" else values
    for name in ("matched_query_control", "primary_current"):
        if name in artifact.optional_scores:
            score_archive[f"reference__{name}"] = artifact.optional_scores[name]
    return report, score_archive


def _array_alias(archive, names: tuple[str, ...], *, required: bool = True) -> np.ndarray | None:
    present = [name for name in names if name in archive]
    if not present:
        if required:
            raise ValueError(f"feature archive is missing required array: {names[0]}")
        return None
    first = np.asarray(archive[present[0]])
    for name in present[1:]:
        if not np.array_equal(first, np.asarray(archive[name]), equal_nan=True):
            raise ValueError(f"feature archive has conflicting aliases for {names[0]}")
    return first


def load_feature_artifact(path: Path) -> FeatureArtifact:
    """Load and validate a private ID-aligned feature archive without exposing IDs."""
    with np.load(Path(path), allow_pickle=False) as archive:
        required_1d = {
            "read_ids": _array_alias(archive, ("read_ids",)),
            "ratings": _array_alias(archive, ("ratings",)),
            "utc_day": _array_alias(archive, ("utc_day",)),
            "author_group": _array_alias(archive, ("author_group",)),
            "current": _array_alias(archive, ("current", "score_current")),
            "train_mask": _array_alias(archive, ("train_mask",)),
            "validation_mask": _array_alias(archive, ("validation_mask", "val_mask")),
            "later_mask": _array_alias(archive, ("later_mask", "test_mask")),
            "oof_selection_mask": _array_alias(archive, ("oof_selection_mask",)),
        }
        base_features = _array_alias(archive, ("base_features", "ordinal_features"))
        raw_signals = {
            "cluster": _array_alias(archive, ("cluster3", "old_cluster3", "cluster")),
            "recency": _array_alias(archive, ("recency",)),
            "local": _array_alias(archive, ("local",)),
            "ordinal": _array_alias(archive, ("ordinal_expected", "ordinal")),
        }
        expected_names = _array_alias(
            archive, ("base_feature_names", "feature_names"), required=False
        )
        optional_scores = {}
        for name, aliases in {
            "metadata_current": ("metadata_current", "metadata_score_current"),
            "metadata_ess5": ("metadata_ess5", "metadata_score_ess5"),
            "primary_current": ("primary_current",),
            "matched_query_control": ("matched_query_control",),
        }.items():
            value = _array_alias(archive, aliases, required=False)
            if value is not None:
                optional_scores[name] = value
        optional_masks = {}
        coverage = _array_alias(
            archive, ("metadata_coverage", "primary_metadata_coverage"), required=False
        )
        if coverage is not None:
            optional_masks["metadata_coverage"] = coverage
        extra_arrays = {
            name: np.asarray(archive[name])
            for name in archive.files
            if name.startswith("oof_") or name.startswith("fold_")
        }

    ids = np.asarray(required_1d["read_ids"])
    tokenized = [str(value.decode("utf-8") if isinstance(value, (bytes, np.bytes_)) else value)
                 for value in ids.reshape(-1)]
    if ids.ndim != 1 or not len(ids) or len(set(tokenized)) != len(tokenized):
        raise ValueError("feature archive read IDs must be a non-empty unique vector")
    n = len(ids)
    arrays = {name: np.asarray(value) for name, value in required_1d.items() if name != "read_ids"}
    for name, value in arrays.items():
        if value.ndim != 1 or len(value) != n:
            raise ValueError(f"feature archive vector is not aligned: {name}")
    y = _validate_ratings(arrays["ratings"], expected_rows=n)
    days = np.asarray(arrays["utc_day"])
    if not np.issubdtype(days.dtype, np.integer):
        raise ValueError("UTC-day values must be integer UTC day ordinals")
    days = days.astype(np.int64, copy=False)
    if np.any(np.diff(days) < 0):
        raise ValueError("feature archive rows must be ordered by UTC day")
    groups = arrays["author_group"]
    if not np.issubdtype(groups.dtype, np.integer):
        raise ValueError("author-group values must be integer codes")
    groups = groups.astype(np.int64, copy=False)
    current = np.asarray(arrays["current"], dtype=np.float64)
    if not np.isfinite(current).all():
        raise ValueError("current served scores must be finite for every eligible target")
    x = np.asarray(base_features, dtype=np.float64)
    if x.ndim != 2 or x.shape != (n, len(ordinal.BASE_NAMES)) or not np.isfinite(x).all():
        raise ValueError("base feature matrix must align to the ten causal feature names")
    if expected_names is not None:
        names = [str(value.decode("utf-8") if isinstance(value, (bytes, np.bytes_)) else value)
                 for value in np.asarray(expected_names).reshape(-1)]
        if tuple(names) != tuple(ordinal.BASE_NAMES):
            raise ValueError("base feature names or order differ from the ordinal evaluator")
    signals = {}
    for name, value in raw_signals.items():
        values = np.asarray(value, dtype=np.float64)
        if values.ndim != 1 or len(values) != n or np.isinf(values).any():
            raise ValueError(f"causal signal is not an aligned valid vector: {name}")
        if name != "ordinal" and not np.isfinite(values).all():
            raise ValueError(f"causal signal is not an aligned finite vector: {name}")
        signals[name] = values
    masks = {}
    for name in ("train_mask", "validation_mask", "later_mask"):
        value = arrays[name]
        if value.dtype != np.bool_:
            raise ValueError(f"split mask must be boolean: {name}")
        masks[name] = value.astype(bool, copy=False)
    total = masks["train_mask"].astype(np.int8) + masks["validation_mask"].astype(np.int8) + masks["later_mask"].astype(np.int8)
    if np.any(total != 1):
        raise ValueError("authoritative split masks must partition every eligible target once")
    oof_mask = arrays["oof_selection_mask"]
    if oof_mask.dtype != np.bool_ or np.any(oof_mask & ~masks["train_mask"]):
        raise ValueError("OOF selection mask must be boolean and contained in development")
    if not np.isfinite(signals["ordinal"][masks["validation_mask"]]).all():
        raise ValueError("ordinal signal must be available for every validation target")
    if not np.isfinite(signals["ordinal"][masks["later_mask"]]).all():
        raise ValueError("ordinal signal must be available for every later target")
    for name, value in optional_scores.items():
        scores = np.asarray(value, dtype=np.float64)
        if scores.ndim != 1 or len(scores) != n or not np.isfinite(scores).all():
            raise ValueError(f"optional metadata score is not aligned and finite: {name}")
        optional_scores[name] = scores
    for name, value in optional_masks.items():
        mask = np.asarray(value)
        if mask.dtype != np.bool_ or mask.ndim != 1 or len(mask) != n:
            raise ValueError(f"optional metadata mask is not an aligned boolean vector: {name}")
        optional_masks[name] = mask.astype(bool, copy=False)
    for name in ("primary_current", "matched_query_control"):
        if name in optional_scores and not np.isfinite(optional_scores[name]).all():
            raise ValueError(f"comparison reference scores must be finite: {name}")
    for name, value in extra_arrays.items():
        if value.dtype != np.bool_ or value.ndim != 1 or len(value) != n:
            raise ValueError(f"fold mask must be an aligned boolean vector: {name}")
    return FeatureArtifact(
        read_ids=ids,
        ratings=y,
        utc_day=days,
        author_group=groups,
        current=current,
        base_features=x,
        signals=signals,
        train_mask=masks["train_mask"],
        validation_mask=masks["validation_mask"],
        later_mask=masks["later_mask"],
        oof_selection_mask=oof_mask.astype(bool, copy=False),
        optional_scores=optional_scores,
        optional_masks=optional_masks,
        extra_arrays=extra_arrays,
    )


def fit_feature_scaler(features: np.ndarray) -> FeatureScaler:
    """Fit population mean and scale on training features only."""
    x = np.asarray(features, dtype=np.float64)
    if x.ndim != 2 or not len(x) or not x.shape[1] or not np.isfinite(x).all():
        raise ValueError("training features must be a non-empty finite matrix")
    mean = x.mean(axis=0)
    scale = x.std(axis=0)
    scale[scale < 1e-8] = 1.0
    return FeatureScaler(mean=mean, scale=scale)


def transform_features(features: np.ndarray, scaler: FeatureScaler) -> np.ndarray:
    x = np.asarray(features, dtype=np.float64)
    if x.ndim != 2 or x.shape[1] != len(scaler.mean):
        raise ValueError("feature matrix does not match its fitted scaler")
    if not np.isfinite(x).all():
        raise ValueError("scored features must be finite")
    return (x - scaler.mean) / scaler.scale


def train_standardize(
    values: np.ndarray,
    train_indexes: np.ndarray,
    *,
    fit_values: np.ndarray | None = None,
) -> tuple[np.ndarray, dict[str, float]]:
    """Standardize one score/signal with fit rows only."""
    values = np.asarray(values, dtype=np.float64)
    indexes = np.asarray(train_indexes, dtype=np.int64)
    if values.ndim != 1 or not len(indexes) or np.any(indexes < 0) or np.any(indexes >= len(values)):
        raise ValueError("standardization requires a vector and valid non-empty training indexes")
    if np.isinf(values).any():
        raise ValueError("score values must not contain infinities")
    train_values = values[indexes]
    train_values = train_values[np.isfinite(train_values)]
    if not len(train_values) and fit_values is not None:
        train_values = np.asarray(fit_values, dtype=np.float64).reshape(-1)
        train_values = train_values[np.isfinite(train_values)]
    if not len(train_values):
        raise ValueError("training score values must include finite rows")
    mean = float(train_values.mean())
    scale = float(train_values.std())
    if scale < 1e-8:
        scale = 1.0
    standardized = np.full_like(values, np.nan, dtype=np.float64)
    finite = np.isfinite(values)
    standardized[finite] = (values[finite] - mean) / scale
    return standardized, {"mean": mean, "scale": scale}


def build_additive_blend(
    primary: np.ndarray,
    signals: dict[str, np.ndarray],
    train_indexes: np.ndarray,
    *,
    addition_weight: float = 0.25,
) -> tuple[np.ndarray, dict]:
    """Compose a primary ranker with fixed, train-standardized signals."""
    primary_z, primary_scaler = train_standardize(primary, train_indexes)
    if not np.isfinite(addition_weight) or addition_weight < 0:
        raise ValueError("addition weight must be finite and non-negative")
    scores = primary_z.copy()
    signal_scalers = {}
    for name, values in signals.items():
        signal_z, signal_scaler = train_standardize(values, train_indexes)
        if signal_z.shape != primary_z.shape:
            raise ValueError("blend signals must align with the primary score")
        scores += float(addition_weight) * signal_z
        signal_scalers[name] = signal_scaler
    return scores, {
        "primary": primary_scaler,
        "signals": signal_scalers,
        "addition_weight": float(addition_weight),
    }


def _validate_ratings(ratings: np.ndarray, expected_rows: int | None = None) -> np.ndarray:
    y = np.asarray(ratings, dtype=np.float64)
    if (y.ndim != 1 or (expected_rows is not None and len(y) != expected_rows)
            or not len(y) or not np.isfinite(y).all()
            or np.any(y < 1) or np.any(y > 5) or np.any(y != np.floor(y))):
        raise ValueError("ratings must be aligned integer stars from 1 through 5")
    return y.astype(np.int64)


def _auc(labels: np.ndarray, scores: np.ndarray) -> float | None:
    labels = np.asarray(labels, dtype=bool)
    scores = np.asarray(scores, dtype=np.float64)
    positives = int(labels.sum())
    negatives = len(labels) - positives
    if not positives or not negatives:
        return None
    ranks = rankdata(scores, method="average")
    return float((ranks[labels].sum() - positives * (positives + 1) / 2)
                 / (positives * negatives))


def _tail_metrics(ratings: np.ndarray, scores: np.ndarray) -> dict:
    y = _validate_ratings(ratings, expected_rows=len(scores))
    score = np.asarray(scores, dtype=np.float64)
    if score.ndim != 1 or not np.isfinite(score).all():
        raise ValueError("tail evaluation scores must be a finite vector")
    high_auc = _auc(y >= 4, score)
    low_auc = _auc(y <= 2, -score)
    if high_auc is None or low_auc is None:
        raise ValueError("OOF fold must contain both classes for high and low AUC")
    return {
        "n": int(len(y)),
        "auc_high_4plus": high_auc,
        "auc_low_2or_less_reversed": low_auc,
        "balanced_high_low_auc": float((high_auc + low_auc) / 2.0),
    }


def _validated_folds(folds: list[tuple[np.ndarray, np.ndarray]], row_count: int) -> list[tuple[np.ndarray, np.ndarray]]:
    if not folds:
        raise ValueError("OOF configuration selection requires at least one fold")
    validated = []
    seen_eval: set[int] = set()
    for train_indexes, eval_indexes in folds:
        train = np.asarray(train_indexes, dtype=np.int64)
        evaluate = np.asarray(eval_indexes, dtype=np.int64)
        if (train.ndim != 1 or evaluate.ndim != 1 or not len(train) or not len(evaluate)
                or np.any(train < 0) or np.any(evaluate < 0)
                or np.any(train >= row_count) or np.any(evaluate >= row_count)
                or len(np.unique(train)) != len(train) or len(np.unique(evaluate)) != len(evaluate)):
            raise ValueError("OOF fold indexes must be non-empty, unique, and in range")
        if np.intersect1d(train, evaluate).size:
            raise ValueError("OOF training and evaluation rows must be disjoint")
        if int(train.max()) >= int(evaluate.min()):
            raise ValueError("OOF folds must train strictly before their evaluation rows")
        if seen_eval.intersection(int(value) for value in evaluate):
            raise ValueError("OOF evaluation rows must not be reused across folds")
        seen_eval.update(int(value) for value in evaluate)
        validated.append((train, evaluate))
    return validated


def _weighted_fold_metrics(fold_metrics: list[dict]) -> dict:
    total = sum(row["n"] for row in fold_metrics)
    return {
        "oof_targets": int(total),
        "folds": int(len(fold_metrics)),
        "auc_high_4plus": float(np.mean([row["auc_high_4plus"] for row in fold_metrics])),
        "auc_low_2or_less_reversed": float(np.mean([row["auc_low_2or_less_reversed"] for row in fold_metrics])),
        "balanced_high_low_auc": float(np.mean([row["balanced_high_low_auc"] for row in fold_metrics])),
        "fold_metrics": fold_metrics,
        "aggregation": "unweighted mean of within-fold AUCs; scores are never pooled across fitted folds",
    }


def select_tail_heads_c_oof(
    features: np.ndarray,
    ratings: np.ndarray,
    folds: list[tuple[np.ndarray, np.ndarray]],
    c_grid: tuple[float, ...] = TAIL_HEAD_C_GRID,
) -> OOFSelection:
    """Choose the shared head C from chronological out-of-fold predictions."""
    x = np.asarray(features, dtype=np.float64)
    y = _validate_ratings(ratings, expected_rows=len(x) if x.ndim == 2 else None)
    if x.ndim != 2 or not np.isfinite(x).all():
        raise ValueError("OOF features must be a finite matrix")
    validated = _validated_folds(folds, len(y))
    candidates: dict[float, dict] = {}
    for c in c_grid:
        if not np.isfinite(c) or c <= 0:
            raise ValueError("tail-head C grid must contain finite positive values")
        fold_metrics = []
        for train, evaluate in validated:
            model = fit_tail_heads(x[train], y[train], float(c))
            scores = score_tail_heads(model, x[evaluate])
            fold_metrics.append(_tail_metrics(y[evaluate], scores))
        candidates[float(c)] = _weighted_fold_metrics(fold_metrics)
    winner = max(candidates, key=lambda c: (candidates[c]["balanced_high_low_auc"], -c))
    return OOFSelection(selected_c=winner, candidate_metrics=candidates, baseline_metrics={})


def select_pairwise_c_oof(
    features: np.ndarray,
    ratings: np.ndarray,
    current_scores: np.ndarray,
    folds: list[tuple[np.ndarray, np.ndarray]],
    c_grid: tuple[float, ...] = PAIRWISE_C_GRID,
) -> OOFSelection:
    """Choose pairwise regularization from chronological OOF folds only."""
    x = np.asarray(features, dtype=np.float64)
    y = _validate_ratings(ratings, expected_rows=len(x) if x.ndim == 2 else None)
    current = np.asarray(current_scores, dtype=np.float64)
    if (x.ndim != 2 or not np.isfinite(x).all() or current.ndim != 1
            or len(current) != len(y) or not np.isfinite(current).all()):
        raise ValueError("OOF feature and current-score arrays must be aligned and finite")
    validated = _validated_folds(folds, len(y))
    candidates: dict[float, dict] = {}
    baseline_folds = []
    for _, evaluate in validated:
        baseline_folds.append(_tail_metrics(y[evaluate], current[evaluate]))
    baseline_metrics = _weighted_fold_metrics(baseline_folds)
    for c in c_grid:
        if not np.isfinite(c) or c <= 0:
            raise ValueError("pairwise C grid must contain finite positive values")
        fold_metrics = []
        for train, evaluate in validated:
            model = fit_pairwise_ranker(x[train], y[train], current[train], float(c))
            scores = score_pairwise_ranker(model, x[evaluate], current[evaluate])
            fold_metrics.append(_tail_metrics(y[evaluate], scores))
        candidates[float(c)] = _weighted_fold_metrics(fold_metrics)
    winner = max(candidates, key=lambda c: (candidates[c]["balanced_high_low_auc"], -c))
    return OOFSelection(selected_c=winner, candidate_metrics=candidates,
                        baseline_metrics=baseline_metrics)


def fit_tail_heads(features: np.ndarray, ratings: np.ndarray, c: float) -> TailHeads:
    """Fit separate high and low heads; three-star rows are negative for both."""
    x = np.asarray(features, dtype=np.float64)
    y = _validate_ratings(ratings, expected_rows=len(x) if x.ndim == 2 else None)
    if not np.isfinite(c) or c <= 0:
        raise ValueError("C must be finite and positive")
    scaler = fit_feature_scaler(x)
    z = transform_features(x, scaler)
    high_target = (y >= 4).astype(np.int8)
    low_target = (y <= 2).astype(np.int8)
    if len(np.unique(high_target)) != 2 or len(np.unique(low_target)) != 2:
        raise ValueError("training prefix must contain both classes for each tail head")
    options = {
        "C": float(c),
        "class_weight": None,
        "max_iter": 2000,
        "random_state": 0,
        "solver": "lbfgs",
        "tol": 1e-10,
    }
    high_model = LogisticRegression(**options).fit(z, high_target)
    low_model = LogisticRegression(**options).fit(z, low_target)
    return TailHeads(
        scaler=scaler, high_model=high_model, low_model=low_model,
        c=float(c), train_rows=int(len(y)),
    )


def tail_head_probabilities(model: TailHeads, features: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return uncalibrated high and low endpoint probabilities."""
    z = transform_features(features, model.scaler)
    high_class = int(np.flatnonzero(model.high_model.classes_ == 1)[0])
    low_class = int(np.flatnonzero(model.low_model.classes_ == 1)[0])
    return (model.high_model.predict_proba(z)[:, high_class],
            model.low_model.predict_proba(z)[:, low_class])


def tail_head_utility(high_probability: np.ndarray, low_probability: np.ndarray) -> np.ndarray:
    """Map separate endpoint probabilities onto the served 0–100 score scale."""
    high = np.asarray(high_probability, dtype=np.float64)
    low = np.asarray(low_probability, dtype=np.float64)
    if (high.ndim != 1 or low.shape != high.shape or not np.isfinite(high).all()
            or not np.isfinite(low).all() or np.any(high < 0) or np.any(high > 1)
            or np.any(low < 0) or np.any(low > 1)):
        raise ValueError("tail probabilities must be aligned finite values from 0 through 1")
    raw = np.clip(50.0 + 50.0 * (high - low), 0.0, 100.0)
    # Match the serving Python round-to-tenth convention exactly.
    return np.fromiter((round(float(value), 1) for value in raw), dtype=np.float64, count=len(raw))


def score_tail_heads(model: TailHeads, features: np.ndarray) -> np.ndarray:
    high, low = tail_head_probabilities(model, features)
    return tail_head_utility(high, low)


def fit_pairwise_ranker(
    features: np.ndarray,
    ratings: np.ndarray,
    current_scores: np.ndarray,
    c: float,
) -> PairwiseRanker:
    """Fit a regularized pairwise residual while retaining current-score order.

    Each unequal-rating unordered pair contributes once to a mean logistic
    ranking loss. The current served score is the fixed offset. The fit is
    uncapped; serving limits corrections to 25% of the training current-score
    standard deviation and at most five score points.
    """
    x = np.asarray(features, dtype=np.float64)
    y = _validate_ratings(ratings, expected_rows=len(x) if x.ndim == 2 else None)
    current = np.asarray(current_scores, dtype=np.float64)
    if current.ndim != 1 or len(current) != len(y) or not np.isfinite(current).all():
        raise ValueError("current scores must be an aligned finite vector")
    if not np.isfinite(c) or c < 0:
        raise ValueError("C must be finite and non-negative")
    feature_scaler = fit_feature_scaler(x)
    z = transform_features(x, feature_scaler)
    current_mean = float(current.mean())
    current_scale = float(current.std())
    if current_scale < 1e-8:
        current_scale = 1.0
    base = (current - current_mean) / current_scale

    left, right = np.triu_indices(len(y), k=1)
    keep = y[left] != y[right]
    left, right = left[keep], right[keep]
    if not len(left):
        raise ValueError("pairwise training requires unequal ratings")
    direction = np.sign(y[left] - y[right]).astype(np.float64)
    delta_features = z[left] - z[right]
    delta_current = base[left] - base[right]
    pair_count = len(left)

    def objective(beta: np.ndarray) -> tuple[float, np.ndarray]:
        margins = direction * (delta_current + delta_features @ beta)
        loss = np.logaddexp(0.0, -margins).mean()
        penalty = 0.5 * float(np.dot(beta, beta)) / float(c)
        derivative = -direction * expit(-margins)
        gradient = (delta_features.T @ derivative) / pair_count + beta / float(c)
        return float(loss + penalty), gradient

    if c == 0:
        coefficients = np.zeros(x.shape[1], dtype=np.float64)
        objective_value = float(np.logaddexp(0.0, -direction * delta_current).mean())
        optimizer_iterations = 0
    else:
        result = minimize(
            objective,
            np.zeros(x.shape[1], dtype=np.float64),
            method="L-BFGS-B",
            jac=True,
            options={"maxiter": 2000, "ftol": 1e-12, "gtol": 1e-8, "maxls": 50},
        )
        if not np.isfinite(result.fun) or not np.isfinite(result.x).all():
            raise RuntimeError("pairwise fit did not produce finite parameters")
        if not result.success:
            raise RuntimeError(f"pairwise fit did not converge: {result.message}")
        coefficients = np.asarray(result.x, dtype=np.float64)
        objective_value = float(result.fun)
        optimizer_iterations = int(result.nit)
    return PairwiseRanker(
        feature_scaler=feature_scaler,
        current_mean=current_mean,
        current_scale=current_scale,
        coefficients=coefficients,
        c=float(c),
        correction_cap_points=float(min(
            PAIRWISE_CORRECTION_CAP_SD * current_scale,
            PAIRWISE_CORRECTION_CAP_POINTS,
        )),
        train_rows=int(len(y)),
        comparison_pairs=int(pair_count),
        objective=objective_value,
        optimizer_iterations=optimizer_iterations,
    )


def score_pairwise_ranker(
    model: PairwiseRanker,
    features: np.ndarray,
    current_scores: np.ndarray,
) -> np.ndarray:
    x = np.asarray(features, dtype=np.float64)
    current = np.asarray(current_scores, dtype=np.float64)
    if current.ndim != 1 or len(current) != len(x) or not np.isfinite(current).all():
        raise ValueError("current scores must be an aligned finite vector")
    z = transform_features(x, model.feature_scaler)
    correction_points = model.current_scale * (z @ model.coefficients)
    correction_points = np.clip(
        correction_points, -model.correction_cap_points, model.correction_cap_points
    )
    raw = np.clip(current + correction_points, 0.0, 100.0)
    # Current is already rounded; this preserves the exact zero-residual arm.
    return np.fromiter((round(float(value), 1) for value in raw), dtype=np.float64, count=len(raw))


def _ensure_private_directory(path: Path) -> Path:
    path = Path(path).expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def _write_private_npz(path: Path, arrays: dict[str, np.ndarray]) -> None:
    path = Path(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            np.savez_compressed(handle, **arrays)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)


def _write_private_json(path: Path, payload: dict) -> None:
    path = Path(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)


def _same_split_identity(left: FeatureArtifact, right: FeatureArtifact) -> bool:
    return all(
        np.array_equal(getattr(left, name), getattr(right, name), equal_nan=True)
        for name in (
            "read_ids", "ratings", "utc_day", "train_mask", "validation_mask",
            "later_mask", "oof_selection_mask",
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate frozen tail-head and pairwise ranking arms from private, "
            "ID-aligned feature NPZ archives."
        )
    )
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--feature-manifest", type=Path, required=True)
    parser.add_argument("--rich-features", type=Path)
    parser.add_argument("--rich-manifest", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="Private directory for aggregate JSON and ID-aligned score NPZs")
    args = parser.parse_args(argv)

    if bool(args.rich_features) != bool(args.rich_manifest):
        parser.error("--rich-features and --rich-manifest must be supplied together")
    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    validate_protocol(protocol)
    primary = load_feature_artifact(args.features)
    primary_manifest = validate_feature_manifest(
        args.features, args.feature_manifest, protocol, primary
    )
    views = [("primary", primary, primary_manifest)]
    if args.rich_features is not None:
        rich = load_feature_artifact(args.rich_features)
        rich_manifest = validate_feature_manifest(
            args.rich_features, args.rich_manifest, protocol, rich
        )
        if not _same_split_identity(primary, rich):
            raise ValueError("primary and rich feature views do not share identical target/split rows")
        views.append(("rich", rich, rich_manifest))

    output_dir = _ensure_private_directory(args.output_dir)
    input_paths = {
        Path(path).expanduser().resolve()
        for path in (
            args.protocol, args.features, args.feature_manifest,
            args.rich_features, args.rich_manifest,
        ) if path is not None
    }
    reports = {}
    output_files = {}
    for view_name, artifact, manifest in views:
        report, score_archive = evaluate_feature_view(
            artifact, manifest, view_name=view_name
        )
        score_path = output_dir / f"tail-pairwise-{view_name}-scores.npz"
        if score_path.resolve() in input_paths:
            raise ValueError("score output path would overwrite a frozen input artifact")
        _write_private_npz(score_path, score_archive)
        reports[view_name] = report
        output_files[view_name] = {
            "name": score_path.name,
            "sha256": file_sha256(score_path),
            "bytes": int(score_path.stat().st_size),
            "mode": "0600",
            "arrays": sorted(score_archive),
        }

    aggregate = {
        "schema_version": 1,
        "evaluator_script_sha256": file_sha256(Path(__file__)),
        "protocol_sha256": file_sha256(args.protocol),
        "feature_artifacts": {
            view_name: {
                "sha256": manifest["feature_artifact_sha256"],
                "eligible_targets": int(manifest["eligible_targets"]),
                "exact_ranker_parity_mismatches": int(
                    manifest["exact_ranker_parity"]["mismatches_at_served_precision"]
                ),
            }
            for view_name, _, manifest in views
        },
        "outputs": output_files,
        "evaluations": reports,
    }
    report_path = output_dir / "tail-pairwise-aggregate.json"
    if report_path.resolve() in input_paths:
        raise ValueError("aggregate output path would overwrite a frozen input artifact")
    _write_private_json(report_path, aggregate)
    print(json.dumps({
        "aggregate": str(report_path),
        "aggregate_sha256": file_sha256(report_path),
        "scores": {
            name: str(output_dir / details["name"])
            for name, details in output_files.items()
        },
        "selected_C": {
            name: report["selected_configurations"]
            for name, report in reports.items()
        },
        "oof_balanced_auc": {
            name: {
                arm: details["mean_balanced_high_low_auc"]
                for arm, details in report["oof_results"]["arms"].items()
            }
            for name, report in reports.items()
        },
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
