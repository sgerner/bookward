#!/usr/bin/env python3
"""Evaluate a preregistered, aggregate-only grid of combined ranking signals.

This is a retrospective screen over rated reads, not an evaluation of retrieval
or live recommendation outcomes. The script prints aggregate metrics only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from threadpoolctl import threadpool_limits

ENGINE = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(ENGINE))

import evaluate_ordinal_interests as ordinal
from afterword_engine.identity import book_identity
from evaluate_historical_ratings import whole_day_cuts
from evaluate_ranking import load_corpus, prepare

SIGNALS = ("cluster", "recency", "local", "ordinal")
ARMS = {
    "current": (),
    "cluster": ("cluster",),
    "recency": ("recency",),
    "local": ("local",),
    "ordinal": ("ordinal",),
    "cluster_recency": ("cluster", "recency"),
    "cluster_ordinal": ("cluster", "ordinal"),
    "local_recency": ("local", "recency"),
}
PAIR_ARMS = ("cluster_recency", "cluster_ordinal", "local_recency")
ADDITION_WEIGHT = 0.25
NONINFERIORITY_MARGIN = 0.01


def preregistration_metadata() -> dict:
    """Describe the evaluator's fixed grid without claiming verified chronology."""
    return {
        "version": 1,
        "fixed_grid": True,
        "registration_verification": (
            "Not verified by the evaluator; preregistration timing must be established externally. "
            "A joint manifest hash verifies file identity, not chronology."
        ),
        "fixed_addition_weight": ADDITION_WEIGHT,
        "noninferiority_margin_each_auc": NONINFERIORITY_MARGIN,
        "registered_arms": {name: list(additions) for name, additions in ARMS.items()},
    }


def train_standardize(values: np.ndarray, train_indexes: np.ndarray) -> tuple[np.ndarray, dict[str, float]]:
    """Standardize with training rows only; leave unavailable early rows as NaN."""
    values = np.asarray(values, dtype=np.float64)
    train_indexes = np.asarray(train_indexes, dtype=np.int64)
    if values.ndim != 1 or not len(train_indexes):
        raise ValueError("standardization requires a vector and non-empty training indexes")
    train_values = values[train_indexes]
    if not np.isfinite(train_values).all():
        raise ValueError("training signal values must be finite")
    mean = float(train_values.mean())
    scale = float(train_values.std())
    if scale < 1e-8:
        scale = 1.0
    return (values - mean) / scale, {"mean": mean, "scale": scale}


def build_arm_scores(
    current_scores: np.ndarray,
    signal_values: dict[str, np.ndarray],
    train_indexes: np.ndarray,
    addition_weight: float = ADDITION_WEIGHT,
) -> tuple[dict[str, np.ndarray], dict[str, dict[str, float]]]:
    """Build all fixed blends from train-fitted z scores and no fitted weights."""
    if set(signal_values) != set(SIGNALS):
        raise ValueError(f"signal_values must contain exactly {SIGNALS}")
    if not np.isfinite(addition_weight) or addition_weight < 0:
        raise ValueError("addition_weight must be finite and non-negative")
    current_z, current_scaler = train_standardize(current_scores, train_indexes)
    standardized: dict[str, np.ndarray] = {}
    scalers = {"current": current_scaler}
    for name in SIGNALS:
        standardized[name], scalers[name] = train_standardize(signal_values[name], train_indexes)
    scores = {"current": current_z}
    for arm, additions in ARMS.items():
        if arm == "current":
            continue
        scores[arm] = current_z + addition_weight * sum(
            (standardized[name] for name in additions), start=np.zeros_like(current_z)
        )
    return scores, scalers


def balanced_auc(result: dict) -> float | None:
    high = result.get("auc_high_4plus")
    low = result.get("auc_low_2or_less_reversed")
    if high is None or low is None:
        return None
    return float((high + low) / 2.0)


def select_validation_arm(
    results: dict[str, dict],
    noninferiority_margin: float = NONINFERIORITY_MARGIN,
) -> tuple[str, dict[str, dict]]:
    """Select the best validation blend only if both rating directions are safe."""
    if "current" not in results:
        raise ValueError("validation results must include current")
    if not np.isfinite(noninferiority_margin) or noninferiority_margin < 0:
        raise ValueError("noninferiority_margin must be finite and non-negative")
    baseline = results["current"]
    baseline_balanced = balanced_auc(baseline)
    if baseline_balanced is None:
        raise ValueError("current validation balanced AUC is undefined")
    eligibility: dict[str, dict] = {}
    eligible = []
    arm_order = {name: index for index, name in enumerate(ARMS)}
    for arm in ARMS:
        candidate = results.get(arm)
        if candidate is None:
            continue
        score = balanced_auc(candidate)
        high = candidate.get("auc_high_4plus")
        low = candidate.get("auc_low_2or_less_reversed")
        delta_high = high - baseline["auc_high_4plus"] if high is not None else None
        delta_low = low - baseline["auc_low_2or_less_reversed"] if low is not None else None
        qualifies = (
            arm != "current"
            and score is not None
            and score > baseline_balanced
            and delta_high is not None
            and delta_low is not None
            and delta_high >= -noninferiority_margin
            and delta_low >= -noninferiority_margin
        )
        eligibility[arm] = {
            "balanced_auc": score,
            "delta_high_auc": delta_high,
            "delta_low_auc": delta_low,
            "qualifies": bool(qualifies),
        }
        if qualifies:
            eligible.append(arm)
    if not eligible:
        return "current", eligibility
    winner = max(
        eligible,
        key=lambda name: (
            eligibility[name]["balanced_auc"],
            -len(ARMS[name]),
            -arm_order[name],
        ),
    )
    return winner, eligibility


def interaction_residual(results: dict[str, dict], pair_arm: str) -> float | None:
    """Return a descriptive balanced-AUC residual beyond the pair's singles."""
    if pair_arm not in PAIR_ARMS:
        raise ValueError(f"not a preregistered pair arm: {pair_arm}")
    if not all(name in results for name in ("current", pair_arm)):
        return None
    left, right = ARMS[pair_arm]
    if left not in results or right not in results:
        return None
    baseline = balanced_auc(results["current"])
    pair = balanced_auc(results[pair_arm])
    left_score = balanced_auc(results[left])
    right_score = balanced_auc(results[right])
    if any(value is None for value in (baseline, pair, left_score, right_score)):
        return None
    return float((pair - baseline) - (left_score - baseline) - (right_score - baseline))


def _auc_delta(ratings: np.ndarray, baseline: np.ndarray, candidate: np.ndarray) -> dict[str, float | None]:
    base = ordinal.metric_set(ratings, baseline)
    challenger = ordinal.metric_set(ratings, candidate)
    return {
        "high": (
            challenger["auc_high_4plus"] - base["auc_high_4plus"]
            if challenger["auc_high_4plus"] is not None and base["auc_high_4plus"] is not None
            else None
        ),
        "low": (
            challenger["auc_low_2or_less_reversed"] - base["auc_low_2or_less_reversed"]
            if challenger["auc_low_2or_less_reversed"] is not None
            and base["auc_low_2or_less_reversed"] is not None
            else None
        ),
    }


def paired_cluster_bootstrap(
    ratings: np.ndarray,
    baseline_scores: np.ndarray,
    candidate_scores: np.ndarray,
    groups: list[np.ndarray],
    *,
    iterations: int = 2000,
    seed: int = 20261001,
    unit: str,
    minimum_groups: int = 5,
) -> dict:
    """Paired bootstrap by resampling whole clusters; IDs are never returned."""
    if iterations < 1:
        raise ValueError("iterations must be positive")
    if minimum_groups < 2:
        raise ValueError("minimum_groups must be at least 2")
    if len(groups) < minimum_groups:
        return {
            "status": "insufficient_groups",
            "bootstrap_unit": unit,
            "groups": int(len(groups)),
            "minimum_groups": int(minimum_groups),
            "bootstrap_resamples": int(iterations),
            "valid_high_resamples": 0,
            "valid_low_resamples": 0,
        }
    ratings = np.asarray(ratings, dtype=np.float64)
    baseline_scores = np.asarray(baseline_scores, dtype=np.float64)
    candidate_scores = np.asarray(candidate_scores, dtype=np.float64)
    rng = np.random.default_rng(seed)
    high_deltas: list[float] = []
    low_deltas: list[float] = []
    for _ in range(iterations):
        sampled_groups = rng.integers(0, len(groups), size=len(groups))
        indexes = np.concatenate([groups[index] for index in sampled_groups])
        delta = _auc_delta(ratings[indexes], baseline_scores[indexes], candidate_scores[indexes])
        if delta["high"] is not None:
            high_deltas.append(delta["high"])
        if delta["low"] is not None:
            low_deltas.append(delta["low"])

    def summarize(values: list[float]) -> list[float] | None:
        if len(values) < max(20, int(0.5 * iterations)):
            return None
        return [float(value) for value in np.quantile(values, [0.025, 0.975])]

    return {
        "status": "ok" if summarize(high_deltas) is not None and summarize(low_deltas) is not None else "sparse_class_support",
        "bootstrap_unit": unit,
        "groups": int(len(groups)),
        "bootstrap_resamples": int(iterations),
        "valid_high_resamples": int(len(high_deltas)),
        "valid_low_resamples": int(len(low_deltas)),
        "high_auc_delta_95pct": summarize(high_deltas),
        "low_auc_delta_95pct": summarize(low_deltas),
        "interpretation": "paired dependence-sensitivity estimate; not confirmatory evidence",
    }


def author_clusters(items: list[dict], indexes: np.ndarray) -> list[np.ndarray]:
    groups: dict[str, list[int]] = defaultdict(list)
    for local_index, corpus_index in enumerate(indexes):
        key = book_identity("", items[int(corpus_index)].get("author", ""))
        groups[key].append(local_index)
    return [np.asarray(group, dtype=np.int64) for group in groups.values()]


def calendar_block_clusters(records: list, indexes: np.ndarray, block_days: int) -> list[np.ndarray]:
    if block_days < 1:
        raise ValueError("block_days must be positive")
    days = [records[int(index)][0].date().toordinal() for index in indexes]
    if not days:
        return []
    origin = min(days)
    groups: dict[int, list[int]] = defaultdict(list)
    for local_index, day in enumerate(days):
        groups[(day - origin) // block_days].append(local_index)
    return [np.asarray(groups[key], dtype=np.int64) for key in sorted(groups)]


def evaluate_period(
    records: list,
    items: list[dict],
    indexes: np.ndarray,
    ratings: np.ndarray,
    scores: dict[str, np.ndarray],
    arm_names: tuple[str, ...] | None = None,
    baseline_name: str = "current",
) -> dict:
    if baseline_name not in scores:
        raise ValueError(f"missing baseline score arm: {baseline_name}")
    current = scores[baseline_name][indexes]
    result = {}
    for name in arm_names or tuple(scores):
        candidate = scores[name][indexes]
        result[name] = {
            "metrics": ordinal.metric_set(ratings[indexes], candidate),
            "temporal_halves": safe_temporal_halves(
                records, indexes, ratings[indexes], current, candidate
            ),
            "author_slices": safe_author_slices(
                records, items, indexes, ratings[indexes], current, candidate
            ),
        }
    return result


def validation_metrics(period: dict) -> dict[str, dict]:
    return {name: value["metrics"] for name, value in period.items()}


def _id_token(value) -> str:
    if isinstance(value, (bytes, np.bytes_)):
        return value.decode("utf-8")
    if isinstance(value, (int, np.integer)):
        return str(int(value))
    if isinstance(value, (float, np.floating)) and float(value).is_integer():
        return str(int(value))
    return str(value)


def align_npz_array(path: Path, key: str, target_ids: list, id_key: str = "read_ids") -> np.ndarray:
    """Join one private score array to corpus order by explicit IDs, never row position."""
    with np.load(path, allow_pickle=False) as archive:
        if id_key not in archive or key not in archive:
            raise ValueError(f"score archive is missing a required array: {key}")
        source_ids = [_id_token(value) for value in archive[id_key].reshape(-1)]
        values = np.asarray(archive[key])
    if values.ndim != 1 or len(source_ids) != len(values):
        raise ValueError(f"score archive array has an invalid shape: {key}")
    if len(set(source_ids)) != len(source_ids):
        raise ValueError("score archive has duplicate IDs")
    target_tokens = [_id_token(value) for value in target_ids]
    if len(set(target_tokens)) != len(target_tokens):
        raise ValueError("causal corpus contains duplicate target IDs")
    missing = set(target_tokens) - set(source_ids)
    if missing:
        raise ValueError(f"score archive is missing {len(missing)} causal target IDs")
    index_by_id = {value: index for index, value in enumerate(source_ids)}
    return values[np.asarray([index_by_id[value] for value in target_tokens], dtype=np.int64)]


def align_npz_mask(path: Path, key: str, target_ids: list, id_key: str = "read_ids") -> np.ndarray:
    mask = align_npz_array(path, key, target_ids, id_key=id_key)
    if mask.dtype != np.bool_:
        raise ValueError(f"score archive mask must be boolean: {key}")
    return mask


def shared_score_z(values: np.ndarray, reference_mean: float, reference_scale: float) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64)
    if np.isinf(values).any():
        raise ValueError("joint score inputs must not contain infinities")
    if not np.isfinite(reference_mean) or not np.isfinite(reference_scale) or reference_scale <= 0:
        raise ValueError("common score reference must have finite positive scale")
    return (values - reference_mean) / reference_scale


def build_joint_followup_scores(
    fresh_current: np.ndarray,
    fresh_ess5: np.ndarray,
    metadata_current: np.ndarray,
    metadata_ess5: np.ndarray,
    ordinal_signal: np.ndarray,
    cluster_signal: np.ndarray,
    train_indexes: np.ndarray,
    *,
    addition_weight: float = ADDITION_WEIGHT,
) -> tuple[dict[str, np.ndarray], dict]:
    """Use the fresh-current train scale for every base score and ESS5 arm."""
    arrays = [fresh_current, fresh_ess5, metadata_current, metadata_ess5]
    arrays = [np.asarray(values, dtype=np.float64) for values in arrays]
    if len({values.shape for values in arrays}) != 1 or arrays[0].ndim != 1:
        raise ValueError("joint score arrays must be equal-length vectors")
    fresh_train = arrays[0][np.asarray(train_indexes, dtype=np.int64)]
    if not np.isfinite(fresh_train).all():
        raise ValueError("fresh-current train scores must be finite")
    reference_mean = float(fresh_train.mean())
    reference_scale = float(fresh_train.std())
    if reference_scale < 1e-8:
        reference_scale = 1.0
    ordinal_z, ordinal_scaler = train_standardize(ordinal_signal, train_indexes)
    cluster_z, cluster_scaler = train_standardize(cluster_signal, train_indexes)
    scores = {
        "fresh_ta_current": shared_score_z(arrays[0], reference_mean, reference_scale),
        "fresh_ta_ess5": shared_score_z(arrays[1], reference_mean, reference_scale),
        "selected_metadata_current": shared_score_z(arrays[2], reference_mean, reference_scale),
        "selected_metadata_ess5": shared_score_z(arrays[3], reference_mean, reference_scale),
    }
    scores["fresh_ta_plus_ordinal"] = scores["fresh_ta_current"] + addition_weight * ordinal_z
    scores["fresh_ta_plus_cluster"] = scores["fresh_ta_current"] + addition_weight * cluster_z
    scores["selected_metadata_plus_ordinal"] = (
        scores["selected_metadata_current"] + addition_weight * ordinal_z
    )
    scores["selected_metadata_plus_cluster"] = (
        scores["selected_metadata_current"] + addition_weight * cluster_z
    )
    scores["fresh_ess5_plus_ordinal"] = scores["fresh_ta_ess5"] + addition_weight * ordinal_z
    scalers = {
        "reference": "fresh title-author current train mean and population standard deviation, shared by all base/ESS5 score arrays",
        "reference_train_mean": reference_mean,
        "reference_train_scale": reference_scale,
        "ordinal_train_scaler": ordinal_scaler,
        "cluster_train_scaler": cluster_scaler,
        "addition_weight": float(addition_weight),
    }
    return scores, scalers


def metric_deltas(candidate: dict, baseline: dict) -> dict:
    keys = {
        "high_auc": "auc_high_4plus",
        "low_auc": "auc_low_2or_less_reversed",
        "high_vs_low_auc": "auc_high_vs_low_excluding_neutral",
    }
    result = {}
    for label, key in keys.items():
        left, right = candidate.get(key), baseline.get(key)
        result[label] = left - right if left is not None and right is not None else None
    candidate_balanced, baseline_balanced = balanced_auc(candidate), balanced_auc(baseline)
    result["balanced_auc"] = (
        candidate_balanced - baseline_balanced
        if candidate_balanced is not None and baseline_balanced is not None
        else None
    )
    for label in ("top20_high_count", "bottom20_low_count"):
        result[label] = (
            candidate[label] - baseline[label]
            if candidate.get(label) is not None and baseline.get(label) is not None
            else None
        )
    return result


def safe_compare_slice(ratings: np.ndarray, baseline_scores: np.ndarray, candidate_scores: np.ndarray) -> dict:
    baseline = ordinal.metric_set(ratings, baseline_scores)
    candidate = ordinal.metric_set(ratings, candidate_scores)
    return {
        "current": baseline,
        "candidate": candidate,
        "delta_vs_current": metric_deltas(candidate, baseline),
    }


def safe_temporal_halves(
    records: list,
    indexes: np.ndarray,
    ratings: np.ndarray,
    baseline_scores: np.ndarray,
    candidate_scores: np.ndarray,
) -> dict:
    if not len(indexes):
        return {}
    days = [records[int(index)][0].date() for index in indexes]
    unique_days = sorted(set(days))
    boundary = unique_days[len(unique_days) // 2]
    first = np.asarray([i for i, day in enumerate(days) if day < boundary], dtype=np.int64)
    second = np.asarray([i for i, day in enumerate(days) if day >= boundary], dtype=np.int64)
    return {
        "first_half": safe_compare_slice(ratings[first], baseline_scores[first], candidate_scores[first]),
        "second_half": safe_compare_slice(ratings[second], baseline_scores[second], candidate_scores[second]),
        "boundary_date": str(boundary),
    }


def safe_author_slices(
    records: list,
    items: list[dict],
    indexes: np.ndarray,
    ratings: np.ndarray,
    baseline_scores: np.ndarray,
    candidate_scores: np.ndarray,
) -> dict:
    seen = []
    for index in indexes:
        author = book_identity("", items[int(index)].get("author", ""))
        current_day = records[int(index)][0].date()
        seen.append(any(
            records[j][0].date() < current_day
            and book_identity("", items[j].get("author", "")) == author
            for j in range(int(index))
        ))
    seen = np.asarray(seen, dtype=bool)
    return {
        "author_seen": safe_compare_slice(
            ratings[seen], baseline_scores[seen], candidate_scores[seen]
        ),
        "author_unseen": safe_compare_slice(
            ratings[~seen], baseline_scores[~seen], candidate_scores[~seen]
        ),
        "author_unseen_count": int((~seen).sum()),
    }


def metadata_array_key(arm: str, view: str) -> str:
    allowed = {"fresh_ta", "metadata_combined", "equal_vector_blend", "negative_guard"}
    if arm not in allowed or view not in {"current", "ess5"}:
        raise ValueError("unknown joint metadata score arm")
    return f"{arm}_{view}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_joint_provenance(
    corpus_path: Path,
    metadata_scores_path: Path,
    metadata_report_path: Path,
    history_scores_path: Path,
    metadata_arm: str,
    joint_preregistration_path: Path | None = None,
) -> dict:
    """Fail closed unless the frozen metadata replay matches its declared inputs."""
    report = json.loads(Path(metadata_report_path).read_text())
    corpus_digest = sha256_file(corpus_path)
    metadata_digest = sha256_file(metadata_scores_path)
    history_digest = sha256_file(history_scores_path)
    if report.get("corpus_sha256") != corpus_digest:
        raise ValueError("metadata report corpus hash does not match the joint corpus")
    if report.get("score_array_sha256") != metadata_digest:
        raise ValueError("metadata report score-array hash does not match the NPZ")
    if report.get("selected_current_arm_on_primary_validation") != metadata_arm:
        raise ValueError("joint metadata arm differs from the frozen validation winner")

    joint_preregistration_digest = None
    if joint_preregistration_path is not None:
        joint_preregistration_digest = sha256_file(joint_preregistration_path)
        if report.get("joint_preregistration_sha256") != joint_preregistration_digest:
            raise ValueError("metadata report joint preregistration hash does not match the manifest")

    parity = report.get("direct_rank_candidates_current_parity")
    for arm in ("fresh_ta", metadata_arm):
        arm_parity = parity.get(arm) if isinstance(parity, dict) else None
        if not isinstance(arm_parity, dict) or (
            arm_parity.get("compared") != 1677
            or arm_parity.get("mismatches") != 0
            or arm_parity.get("max_error") != 0.0
        ):
            raise ValueError(f"metadata report lacks exact served-score parity for {arm}")

    frozen_matches = report.get("full_replay_matches_frozen_validation_arrays")
    validation_keys = ["fresh_ta_current", f"{metadata_arm}_current", f"{metadata_arm}_ess5"]
    if not isinstance(frozen_matches, dict):
        raise ValueError("metadata report lacks frozen validation replay checks")
    # Newer full-replay reports include the fresh title-author ESS5 validation
    # check. Keep older aggregate reports usable, but never ignore a failed
    # check when that field is present.
    if "fresh_ta_ess5" in frozen_matches:
        validation_keys.append("fresh_ta_ess5")
    for key in validation_keys:
        check = frozen_matches.get(key)
        if not isinstance(check, dict) or check.get("n") != 355 or (
            check.get("mismatches") != 0 or check.get("max_error") != 0.0
        ):
            raise ValueError(f"metadata full replay does not match frozen validation array: {key}")

    with np.load(history_scores_path, allow_pickle=False) as archive:
        if "snapshot_sha256" not in archive.files:
            raise ValueError("history score archive has no corpus snapshot hash")
        history_corpus_digest = str(np.asarray(archive["snapshot_sha256"]).item())
    if history_corpus_digest != corpus_digest:
        raise ValueError("history score archive corpus hash does not match the joint corpus")

    return {
        "corpus_sha256": corpus_digest,
        "metadata_report_sha256": sha256_file(metadata_report_path),
        "metadata_score_archive_sha256": metadata_digest,
        "history_score_archive_sha256": history_digest,
        "selected_current_arm_on_primary_validation": metadata_arm,
        "joint_preregistration_sha256": joint_preregistration_digest,
        "joint_preregistration_hash_verified": joint_preregistration_digest is not None,
        "upstream_current_ranker_parity": {
            "fresh_ta": parity["fresh_ta"],
            metadata_arm: parity[metadata_arm],
        },
        "selected_view_matches_frozen_validation_arrays": True,
        "temporal_availability_caveat": (
            "The enrichment metadata snapshot was fetched in September 2026 and the text vectors "
            "were re-embedded for this retrospective study. Neither the metadata nor these vector "
            "outputs are documented as available at the historical read dates, so this is not an "
            "as-of feature replay."
        ),
    }


def compare_history_current_archive(
    history_scores_path: Path,
    target_ids: list,
    canonical_current: np.ndarray,
) -> dict:
    """Check the cached current archive against the canonical cached-vector replay."""
    history_current = align_npz_array(history_scores_path, "score_current", target_ids)
    canonical_current = np.asarray(canonical_current, dtype=np.float64)
    if canonical_current.shape != history_current.shape:
        raise ValueError("history audit and canonical current arrays have different lengths")
    error = np.abs(np.asarray(history_current, dtype=np.float64) - canonical_current)
    audit_served = np.round(np.asarray(history_current, dtype=np.float32), 1)
    canonical_served = np.round(np.asarray(canonical_current, dtype=np.float32), 1)
    return {
        "ids_joined_by_explicit_key": True,
        "compared_targets": int(len(target_ids)),
        "raw_max_abs_error_points": float(error.max(initial=0.0)),
        "raw_mean_abs_error_points": float(error.mean()) if len(error) else None,
        "count_abs_error_gt_0_05": int(np.count_nonzero(error > 0.05)),
        "served_precision_one_decimal_mismatches": int(
            np.count_nonzero(audit_served != canonical_served)
        ),
        "float32_exact_mismatches": int(np.count_nonzero(
            np.asarray(history_current, dtype=np.float32)
            != np.asarray(canonical_current, dtype=np.float32)
        )),
        "status": "cached_current_parity_check",
        "reason": (
            "Compare audit score_current to the canonical evaluator current array built from the same "
            "cached embedding execution. The separate metadata replay uses a newer fresh-vector "
            "execution and is not the source-parity reference."
        ),
    }


def run_joint_followup(
    records: list,
    items: list[dict],
    ratings: np.ndarray,
    train: np.ndarray,
    validation: np.ndarray,
    later: np.ndarray,
    ordinal_signal: np.ndarray,
    cluster_signal: np.ndarray,
    metadata_path: Path,
    history_current_check: dict,
    metadata_arm: str,
    *,
    iterations: int,
    seed: int,
    block_days: int,
) -> dict:
    eligible_indexes = np.concatenate((train, validation, later))
    ids = [items[int(index)].get("id") for index in eligible_indexes]

    def align_full_score(key: str, path: Path = metadata_path) -> np.ndarray:
        aligned = align_npz_array(path, key, ids)
        full = np.full(len(items), np.nan, dtype=np.float64)
        full[eligible_indexes] = np.asarray(aligned, dtype=np.float64)
        return full

    def align_full_mask(key: str) -> np.ndarray:
        aligned = align_npz_mask(metadata_path, key, ids)
        full = np.zeros(len(items), dtype=bool)
        full[eligible_indexes] = aligned
        return full

    fresh_current = align_full_score(metadata_array_key("fresh_ta", "current"))
    fresh_ess5 = align_full_score(metadata_array_key("fresh_ta", "ess5"))
    metadata_current = align_full_score(metadata_array_key(metadata_arm, "current"))
    metadata_ess5 = align_full_score(metadata_array_key(metadata_arm, "ess5"))
    metadata_covered = align_full_mask("primary_isbn_identity_mask")
    metadata_secondary = align_full_mask("secondary_title_author_only_mask")

    scores, scalers = build_joint_followup_scores(
        fresh_current, fresh_ess5, metadata_current, metadata_ess5,
        ordinal_signal, cluster_signal, train,
    )
    if any(not np.isfinite(values[validation]).all() for values in scores.values()):
        raise ValueError("a joint validation arm contains non-finite scores")
    if any(not np.isfinite(values[later]).all() for values in scores.values()):
        raise ValueError("a joint later arm contains non-finite scores")

    all_names = tuple(scores)
    validation_report = evaluate_period(
        records, items, validation, ratings, scores, arm_names=all_names,
        baseline_name="fresh_ta_current",
    )
    later_report = evaluate_period(
        records, items, later, ratings, scores, arm_names=all_names,
        baseline_name="fresh_ta_current",
    )
    target_components = {
        "selected_metadata_ess5": ["selected_metadata_current", "fresh_ta_ess5"],
        "selected_metadata_plus_ordinal": ["selected_metadata_current", "fresh_ta_plus_ordinal"],
        "selected_metadata_plus_cluster": ["selected_metadata_current", "fresh_ta_plus_cluster"],
        "fresh_ess5_plus_ordinal": ["fresh_ta_ess5", "fresh_ta_plus_ordinal"],
    }

    def comparisons(period_report: dict) -> dict:
        output = {}
        for arm, refs in target_components.items():
            arm_metrics = period_report[arm]["metrics"]
            output[arm] = {
                reference: metric_deltas(arm_metrics, period_report[reference]["metrics"])
                for reference in refs
            }
        return output

    validation_covered = validation[metadata_covered[validation]]
    later_covered = later[metadata_covered[later]]
    validation_secondary = validation[metadata_secondary[validation]]
    later_secondary = later[metadata_secondary[later]]

    def cohort_metrics(indexes: np.ndarray) -> dict:
        return {
            name: ordinal.metric_set(ratings[indexes], values[indexes])
            for name, values in scores.items()
        }

    author_groups = author_clusters(items, later)
    time_groups = calendar_block_clusters(records, later, block_days)
    uncertainty = {}
    for arm, refs in target_components.items():
        uncertainty[arm] = {}
        for ref_index, reference in enumerate(refs):
            uncertainty[arm][reference] = {
                "author_cluster_bootstrap": paired_cluster_bootstrap(
                    ratings[later], scores[reference][later], scores[arm][later], author_groups,
                    iterations=iterations, seed=seed + ref_index,
                    unit="whole author identity clusters",
                ),
                "calendar_block_bootstrap": paired_cluster_bootstrap(
                    ratings[later], scores[reference][later], scores[arm][later], time_groups,
                    iterations=iterations, seed=seed + 100 + ref_index,
                    unit=f"fixed contiguous {block_days}-day calendar blocks",
                ),
            }
    return {
        "status": "fixed_joint_followup; no joint arm selected on later data",
        "selected_metadata_arm_from_validation_only": metadata_arm,
        "validation": {
            "arms": validation_report,
            "paired_constituent_deltas": comparisons(validation_report),
            "primary_metadata_covered_target_count": int(len(validation_covered)),
            "primary_metadata_covered_metrics": cohort_metrics(validation_covered),
            "secondary_title_author_only_target_count": int(len(validation_secondary)),
            "secondary_title_author_only_metrics": cohort_metrics(validation_secondary),
        },
        "later_exploratory": {
            "arms": later_report,
            "paired_constituent_deltas": comparisons(later_report),
            "primary_metadata_covered_target_count": int(len(later_covered)),
            "primary_metadata_covered_metrics": cohort_metrics(later_covered),
            "secondary_title_author_only_target_count": int(len(later_secondary)),
            "secondary_title_author_only_metrics": cohort_metrics(later_secondary),
        },
        "later_paired_uncertainty": uncertainty,
        "scaling": scalers,
        "score_array_checks": {
            "ids_joined_by_explicit_key": True,
            "joint_target_count": int(len(eligible_indexes)),
            "metadata_coverage_all_targets": int(metadata_covered.sum()),
            "secondary_metadata_coverage_all_targets": int(metadata_secondary.sum()),
            "history_current_archive_comparison": history_current_check,
        },
        "limits": "This joint replay is exploratory on one retrospective reader and an already inspected later period. It is not untouched confirmation, retrieval evidence, or a live causal policy comparison.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path,
                        help="private JSON or SQLite corpus snapshot; output contains aggregates only")
    parser.add_argument("--output", type=Path,
                        help="optional path for the aggregate JSON result (default: stdout)")
    parser.add_argument("--backend")
    parser.add_argument("--model")
    parser.add_argument("--bootstrap-iterations", type=int, default=2000)
    parser.add_argument("--bootstrap-seed", type=int, default=20261001)
    parser.add_argument("--block-days", type=int, default=30)
    parser.add_argument("--joint-metadata-scores", type=Path,
                        help="optional private NPZ containing ID-aligned metadata and fresh-TA arrays")
    parser.add_argument("--joint-metadata-report", type=Path,
                        help="full upstream report whose corpus and score-archive hashes are checked")
    parser.add_argument("--joint-preregistration", type=Path,
                        help="optional frozen joint preregistration manifest; when supplied, its hash must match the upstream report")
    parser.add_argument("--joint-history-scores", type=Path,
                        help="private ID-aligned history audit score NPZ used for control comparison diagnostics")
    parser.add_argument("--joint-metadata-arm", choices=(
        "metadata_combined", "equal_vector_blend", "negative_guard",
    ), help="validation-selected upstream metadata arm; frozen before joint later-period scoring")
    args = parser.parse_args()
    if args.bootstrap_iterations < 1:
        parser.error("--bootstrap-iterations must be positive")
    if args.block_days < 1:
        parser.error("--block-days must be positive")
    if args.output is not None and args.output.resolve() == args.corpus.resolve():
        parser.error("--output must not overwrite --corpus")
    joint_values = (
        args.joint_metadata_scores,
        args.joint_metadata_report,
        args.joint_history_scores,
        args.joint_metadata_arm,
    )
    joint_requested = any(joint_values)
    if args.joint_preregistration is not None and not joint_requested:
        parser.error("--joint-preregistration requires a joint follow-up")
    if joint_requested and not all(joint_values):
        parser.error(
            "joint follow-up requires --joint-metadata-scores, --joint-metadata-report, "
            "--joint-history-scores, and --joint-metadata-arm"
        )

    joint_provenance = None
    if joint_requested:
        joint_provenance = validate_joint_provenance(
            args.corpus,
            args.joint_metadata_scores,
            args.joint_metadata_report,
            args.joint_history_scores,
            args.joint_metadata_arm,
            args.joint_preregistration,
        )

    with threadpool_limits(limits=1):
        records, selected, excluded = prepare(load_corpus(args.corpus), args.backend, args.model)
        items, _vectors, ratings, dates, base_X, current, interest_X, runtime = ordinal.extract_features(
            records, with_interests=(3,)
        )
        parity = runtime["exact_ranker_parity"]
        if parity["mismatches_at_served_precision"]:
            raise RuntimeError("current score parity failed; refusing combined-score evaluation")

        _, cuts = whole_day_cuts(records)
        train_stop, validation_stop = cuts
        all_indexes = np.arange(len(records))
        train = np.flatnonzero(np.isfinite(current) & (all_indexes < train_stop))
        validation = np.arange(train_stop, validation_stop)
        later = np.arange(validation_stop, len(records))
        if not len(train) or not np.isfinite(current[validation]).all() or not np.isfinite(current[later]).all():
            raise ValueError("chronological replay did not provide finite train, validation, and later scores")

        history_current_check = None
        if joint_requested:
            joint_indexes = np.concatenate((train, validation, later))
            joint_ids = [items[int(index)].get("id") for index in joint_indexes]
            history_current_check = compare_history_current_archive(
                args.joint_history_scores, joint_ids, current[joint_indexes]
            )
            if (
                history_current_check["count_abs_error_gt_0_05"]
                or history_current_check["served_precision_one_decimal_mismatches"]
                or history_current_check["float32_exact_mismatches"]
            ):
                raise RuntimeError(
                    "cached history current-score parity failed at served precision; refusing joint evaluation"
                )

        ordinal_train_model = ordinal.fit_ordinal(base_X[train], ratings[train])
        ordinal_train_expected = ordinal.ordinal_expected_ratings(ordinal_train_model, base_X[train])
        ordinal_validation_expected = ordinal.ordinal_expected_ratings(
            ordinal_train_model, base_X[validation]
        )
        train_validation = np.concatenate((train, validation))
        ordinal_later_model = ordinal.fit_ordinal(base_X[train_validation], ratings[train_validation])
        ordinal_later_expected = ordinal.ordinal_expected_ratings(ordinal_later_model, base_X[later])
        ordinal_values = np.full(len(records), np.nan, dtype=np.float64)
        ordinal_values[train] = ordinal_train_expected
        ordinal_values[validation] = ordinal_validation_expected
        ordinal_values[later] = ordinal_later_expected

        signal_values = {
            "cluster": interest_X[3][:, 0],
            "recency": base_X[:, 6],
            "local": base_X[:, 4],
            "ordinal": ordinal_values,
        }
        scores, scalers = build_arm_scores(current, signal_values, train)
        if not all(np.isfinite(scores[name][validation]).all() for name in ARMS):
            raise ValueError("a preregistered validation arm contains non-finite scores")
        if not all(np.isfinite(scores[name][later]).all() for name in ARMS):
            raise ValueError("a preregistered later arm contains non-finite scores")

        validation_report = evaluate_period(records, items, validation, ratings, scores)
        later_report = evaluate_period(records, items, later, ratings, scores)
        validation_flat = validation_metrics(validation_report)
        selected_arm, eligibility = select_validation_arm(validation_flat)
        interactions = {
            name: {
                "validation_balanced_auc_residual": interaction_residual(validation_flat, name),
                "later_balanced_auc_residual": interaction_residual(
                    validation_metrics(later_report), name
                ),
                "interpretation": "descriptive nonlinear ranking residual; not an additive effect or test",
            }
            for name in PAIR_ARMS
        }
        baseline_validation = validation_flat["current"]
        tail_utility_validation = []
        for name, result in validation_flat.items():
            if name == "current":
                continue
            high_gain = result["top20_high_count"] > baseline_validation["top20_high_count"]
            low_gain = result["bottom20_low_count"] > baseline_validation["bottom20_low_count"]
            if high_gain or low_gain:
                tail_utility_validation.append({
                    "arm": name,
                    "higher_top20_high_count": bool(high_gain),
                    "higher_bottom20_low_count": bool(low_gain),
                    "status": "exploratory tail signal; global two-direction gate still governs general selection",
                })

        if selected_arm == "current":
            uncertainty = {"status": "no challenger selected on validation"}
        else:
            author_groups = author_clusters(items, later)
            time_groups = calendar_block_clusters(records, later, args.block_days)
            uncertainty = {
                "selected_validation_arm": selected_arm,
                "author_cluster_bootstrap": paired_cluster_bootstrap(
                    ratings[later], scores["current"][later], scores[selected_arm][later], author_groups,
                    iterations=args.bootstrap_iterations, seed=args.bootstrap_seed,
                    unit="whole author identity clusters",
                ),
                "calendar_block_bootstrap": paired_cluster_bootstrap(
                    ratings[later], scores["current"][later], scores[selected_arm][later], time_groups,
                    iterations=args.bootstrap_iterations, seed=args.bootstrap_seed + 1,
                    unit=f"fixed contiguous {args.block_days}-day calendar blocks",
                ),
            }

        joint_report = None
        if args.joint_metadata_scores is not None:
            joint_report = run_joint_followup(
                records, items, ratings, train, validation, later,
                ordinal_values, interest_X[3][:, 0],
                args.joint_metadata_scores,
                history_current_check,
                args.joint_metadata_arm,
                iterations=args.bootstrap_iterations,
                seed=args.bootstrap_seed + 1000,
                block_days=args.block_days,
            )
            joint_report["provenance"] = joint_provenance

        data_bytes = args.corpus.read_bytes()
        output = {
            "study": "Bookward combined-score synergy screen",
            "preregistration": preregistration_metadata(),
            "data": {
                "snapshot_sha256": hashlib.sha256(data_bytes).hexdigest(),
                "embedding_backend": selected[0],
                "embedding_model": selected[1],
                "usable_records": int(len(records)),
                "excluded": excluded,
                "whole_day_split": {
                    "train": int(len(train)),
                    "validation": int(len(validation)),
                    "later": int(len(later)),
                    "train_end_day": str(dates[train_stop - 1]),
                    "validation_end_day": str(dates[validation_stop - 1]),
                    "later_start_day": str(dates[validation_stop]),
                },
            },
            "protocol": (
                "Whole-day chronological replay with same-day targets hidden. Existing k=3 causal prefix clusters and 10-feature ordinal model are reused. "
                "All signal standardizers use training rows only and are frozen. Ordinal training expected ratings used for its scaler are in-sample predictions from the train-fitted model; validation and later predictions use models fit only on labels available before their period. "
                "The later period was examined in previous studies and is exploratory; no arm or weight was selected or rescued from later outcomes."
            ),
            "component_scalers": scalers,
            "current_exact_ranker_parity": parity,
            "validation": {
                "arms": validation_report,
                "balanced_auc_selection": {
                    "selected_arm": selected_arm,
                    "eligibility": eligibility,
                    "rule": "A challenger must beat current balanced high/low AUC and keep each direction within 0.01 of current; ties prefer fewer additions then declared order.",
                },
                "tail_utility_candidates": tail_utility_validation,
            },
            "later_exploratory": {"all_registered_arms": later_report},
            "pair_interactions": interactions,
            "later_selected_arm_uncertainty": uncertainty,
            "limits": (
                "One retrospective reader-selected history. This measures rating discrimination among eventually read books, not unread-candidate retrieval, live exposure, or causal user benefit. "
                "Later data have been inspected in earlier studies, and block intervals remain imprecise dependence sensitivities rather than independent confirmation."
            ),
        }
        if joint_report is not None:
            output["joint_followup"] = joint_report
        rendered = json.dumps(output, indent=2, allow_nan=False) + "\n"
        if args.output is None:
            print(rendered, end="")
        else:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered)
            os.chmod(args.output, 0o600)
            print(json.dumps({
                "output_written": True,
                "current_exact_ranker_parity": parity,
                "validation_selected_arm": selected_arm,
                "later_arms_reported": list(ARMS),
            }, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
