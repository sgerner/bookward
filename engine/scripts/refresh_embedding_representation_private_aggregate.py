#!/usr/bin/env python3
"""Rebuild partition summaries from private aligned scores after audit fixes."""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import sys
from pathlib import Path

import numpy as np

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE / "scripts"))
import evaluate_embedding_representations as study


def refresh(
    aggregate_path: Path,
    score_path: Path,
    feature_path: Path,
    corpus_path: Path,
    bge_cache_dir: Path,
    strict_amendment_path: Path,
    method_amendment_path: Path,
    provenance_supplement_path: Path,
) -> dict:
    aggregate = json.loads(aggregate_path.read_text())
    feature_manifest_path = feature_path.with_name("features-manifest-private.json")
    if not aggregate_path.is_file() or not score_path.is_file() or not feature_path.is_file():
        raise FileNotFoundError("private aggregate, scores, and features must all exist")
    for path in (strict_amendment_path, method_amendment_path, provenance_supplement_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    strict_amendment = json.loads(strict_amendment_path.read_text())
    method_amendment = json.loads(method_amendment_path.read_text())
    if (aggregate.get("experiment") != "embedding-representation-track2-strict-v2"
            or aggregate.get("strict_v2_amendment_sha256") != study.sha256_file(strict_amendment_path)
            or aggregate.get("metadata_method_amendment_sha256") != study.sha256_file(method_amendment_path)
            or aggregate.get("provenance_supplement_sha256") != study.sha256_file(provenance_supplement_path)
            or strict_amendment.get("metadata_method_amendment", {}).get("sha256")
            != study.sha256_file(method_amendment_path)):
        raise ValueError("strict-v2 aggregate does not match its frozen amendments and supplement")
    import evaluate_metric_facets as grounded
    verifier_source_sha256 = hashlib.sha256(
        inspect.getsource(grounded._verified_field_provenance).encode("utf-8")
    ).hexdigest()
    if (grounded._method_amendment_sha256(method_amendment_path) != study.sha256_file(method_amendment_path)
            or verifier_source_sha256 != strict_amendment.get("metadata_method_amendment", {}).get("verifier_function_sha256")):
        raise ValueError("metadata method amendment failed its frozen verifier gate")
    manifest = json.loads(feature_manifest_path.read_text())
    with np.load(feature_path, allow_pickle=False) as archive:
        ids = np.asarray(archive["read_ids"], dtype=np.int64)
        ratings = np.asarray(archive["ratings"], dtype=np.float64)
        days = np.asarray(archive["utc_day"], dtype=np.int64)
        authors = np.asarray(archive["author_group"], dtype=np.int64)
        current = np.asarray(archive["current"], dtype=np.float64)
        train = np.asarray(archive["train_mask"], dtype=bool)
        validation = np.asarray(archive["validation_mask"], dtype=bool)
        later = np.asarray(archive["later_mask"], dtype=bool)
    with np.load(score_path, allow_pickle=False) as archive:
        scores = {key: np.asarray(archive[key]) for key in archive.files}
    expected_masks = {
        "train_mask": train,
        "validation_mask": validation,
        "later_mask": later,
        "oof_selection_mask": study.development_oof_mask(
            train, first_oof_start=int(manifest["development_folds"][0]["evaluation_start"])),
    }
    for key, expected in expected_masks.items():
        if key not in scores or not np.array_equal(scores[key], expected):
            raise ValueError(f"strict-v2 aligned scores have a mismatched mask: {key}")
    if not np.array_equal(scores["read_ids"], ids) or not np.array_equal(scores["ratings"], ratings):
        raise ValueError("private score rows do not align with frozen feature rows")
    if not np.array_equal(scores["utc_day"], days) or not np.array_equal(scores["author_group"], authors):
        raise ValueError("private score temporal/author groups do not align with frozen features")
    if not np.array_equal(scores["current"], current):
        raise ValueError("private current score differs from the frozen serving feature")
    if len(ids) != 1677 or int(train.sum()) != 966 or int(validation.sum()) != 355 or int(later.sum()) != 356:
        raise ValueError("frozen cohort partitions have unexpected sizes")

    folds = manifest["development_folds"]
    dev = study.development_oof_mask(train, first_oof_start=int(folds[0]["evaluation_start"]))
    if int(dev.sum()) != 666 or np.any(dev & (validation | later)):
        raise ValueError("development OOF mask includes held-out partitions or has wrong coverage")
    metadata = np.asarray(scores["metadata_mask"], dtype=bool)
    if metadata.shape != ratings.shape:
        raise ValueError("metadata mask differs from the common target rows")
    masks = {
        "development_oof": dev,
        "validation_exploratory": validation,
        "later_exploratory": later,
        "metadata_present_development_oof": metadata & dev,
        "metadata_fallback_development_oof": ~metadata & dev,
    }
    arms = [arm for arm in study.ARM_ORDER if f"direct__{arm}" in scores]
    if len(arms) != len(study.ARM_ORDER):
        raise ValueError("score archive lacks one or more predeclared representation arms")
    aggregate["direct_and_learner_results"] = {}
    for arm in arms:
        direct = np.asarray(scores[f"direct__{arm}"], dtype=np.float64)
        ridge = np.asarray(scores[f"ridge__{arm}"], dtype=np.float64)
        complement = np.asarray(scores[f"complement__{arm}"], dtype=np.float64)
        aggregate["direct_and_learner_results"][arm] = {
            "direct_ranker_development_oof": study.metric_set(ratings[dev], direct[dev]),
            "representation_ridge_development_oof": study.metric_set(ratings[dev], ridge[dev]),
            "current_plus_representation_ridge_development_oof": study.metric_set(ratings[dev], complement[dev]),
        }
        for part in ("validation_exploratory", "later_exploratory"):
            mask = masks[part]
            aggregate["direct_and_learner_results"][arm][part] = {
                "direct_ranker": study.metric_set(ratings[mask], direct[mask]),
                "representation_ridge": study.metric_set(ratings[mask], ridge[mask]),
                "current_plus_representation_ridge": study.metric_set(ratings[mask], complement[mask]),
            }

    fresh_control = np.asarray(scores["direct__qwen_current_title_author"], dtype=np.float64)
    fold_direct_comparisons = {}
    fold_means: dict[str, list[float]] = {arm: [] for arm in arms if arm != "qwen_current_title_author"}
    for index, fold in enumerate(manifest["development_folds"]):
        begin, stop = int(fold["evaluation_start"]), int(fold["evaluation_stop"])
        row = {
            "fold": index + 1,
            "rows": stop - begin,
            "fresh_qwen_title_author_control": study.metric_set(ratings[begin:stop], fresh_control[begin:stop]),
            "alternatives": {},
        }
        control_auc = row["fresh_qwen_title_author_control"]["balanced_auc"]
        for arm in arms:
            if arm == "qwen_current_title_author":
                continue
            direct = np.asarray(scores[f"direct__{arm}"], dtype=np.float64)
            metrics = study.metric_set(ratings[begin:stop], direct[begin:stop])
            delta = metrics["balanced_auc"] - control_auc if metrics["balanced_auc"] is not None and control_auc is not None else None
            row["alternatives"][arm] = {"direct": metrics, "balanced_auc_delta_vs_fresh_qwen_control": delta}
            if delta is not None:
                fold_means[arm].append(float(delta))
        fold_direct_comparisons[f"fold_{index + 1}"] = row
    direct_alternatives = [arm for arm in arms if arm != "qwen_current_title_author"]
    best_direct_alternative = max(
        direct_alternatives,
        key=lambda arm: (
            float(np.mean(fold_means[arm])) if fold_means[arm] else -float("inf"),
            -study.ARM_ORDER.index(arm),
        ),
    )
    aggregate["direct_arm_fold_comparisons_vs_fresh_qwen_control"] = fold_direct_comparisons
    aggregate["best_direct_alternative_diagnostic"] = {
        "arm": best_direct_alternative,
        "selection_scope": "highest mean three-fold development OOF direct balanced-AUC delta versus the re-embedded Qwen title-author execution-matched control; diagnostic only, excluded from main selection and adoption claims",
        "mean_development_fold_balanced_auc_delta": float(np.mean(fold_means[best_direct_alternative])) if fold_means[best_direct_alternative] else None,
    }

    selected_arm = aggregate["selected_arm"]
    selected_ridge = np.asarray(scores[f"ridge__{selected_arm}"], dtype=np.float64)
    selected_complement = np.asarray(scores[f"complement__{selected_arm}"], dtype=np.float64)
    selected_direct = np.asarray(scores[f"direct__{selected_arm}"], dtype=np.float64)
    capped = np.asarray(scores["selected_capped_residual_score"], dtype=np.float64)
    aggregate["partition_metrics"] = {}
    for part, mask in masks.items():
        if "metadata_" in part:
            aggregate["partition_metrics"][part] = {
                "rows": int(mask.sum()),
                "current_score_only_ridge": study.metric_set(ratings[mask], scores["current_ridge_score"][mask]) if mask.any() else None,
                "selected_current_plus_representation_ridge": study.metric_set(ratings[mask], selected_complement[mask]) if mask.any() else None,
            }
        else:
            aggregate["partition_metrics"][part] = {
                "served_current_direct": study.metric_set(ratings[mask], current[mask]),
                "current_score_only_ridge": study.metric_set(ratings[mask], scores["current_ridge_score"][mask]),
                "selected_representation_only_ridge": study.metric_set(ratings[mask], selected_ridge[mask]),
                "selected_current_plus_representation_ridge": study.metric_set(ratings[mask], selected_complement[mask]),
                "selected_direct_replacement": study.metric_set(ratings[mask], selected_direct[mask]),
                "selected_capped_residual": study.metric_set(ratings[mask], capped[mask]),
            }

    day_groups = study._days_to_groups(days[dev])
    block_groups = study._thirty_day_groups(days[dev])
    author_groups = study._groups_to_indices(authors[dev])
    aggregate["uncertainty"] = {
        "selected_complement_vs_current_ridge_by_day": study.grouped_bootstrap_delta(
            ratings[dev], scores["current_ridge_score"][dev], selected_complement[dev], day_groups),
        "selected_complement_vs_current_ridge_by_30_day_block": study.grouped_bootstrap_delta(
            ratings[dev], scores["current_ridge_score"][dev], selected_complement[dev], block_groups),
        "selected_complement_vs_current_ridge_by_author": study.grouped_bootstrap_delta(
            ratings[dev], scores["current_ridge_score"][dev], selected_complement[dev], author_groups),
        "selected_direct_replacement_vs_served_current_by_day": study.grouped_bootstrap_delta(
            ratings[dev], current[dev], selected_direct[dev], day_groups),
        "selected_capped_residual_vs_served_current_by_day": study.grouped_bootstrap_delta(
            ratings[dev], current[dev], capped[dev], day_groups),
        "best_direct_alternative_vs_fresh_qwen_control_by_day": study.grouped_bootstrap_delta(
            ratings[dev], fresh_control[dev], scores[f"direct__{best_direct_alternative}"][dev], day_groups),
        "best_direct_alternative_vs_fresh_qwen_control_by_author": study.grouped_bootstrap_delta(
            ratings[dev], fresh_control[dev], scores[f"direct__{best_direct_alternative}"][dev], author_groups),
    }
    aggregate["sample_units"] = {
        "development_oof_rows": int(dev.sum()),
        "development_oof_unique_utc_days": int(len(set(map(int, days[dev])))),
        "development_oof_distinct_authors": int(len(set(map(int, authors[dev])))),
        "development_oof_30_day_blocks": int(len(block_groups)),
        "high_count": int((ratings[dev] >= 4).sum()),
        "low_count": int((ratings[dev] <= 2).sum()),
        "neutral_count": int(((ratings[dev] > 2) & (ratings[dev] < 4)).sum()),
    }
    unfamiliar_rows = []
    for index, fold in enumerate(folds):
        start, stop = int(fold["evaluation_start"]), int(fold["evaluation_stop"])
        unfamiliar = study._unfamiliar_mask(authors, int(fold["train_stop"]), stop)
        unfamiliar_idx = np.arange(start, stop)[unfamiliar]
        familiar_idx = np.arange(start, stop)[~unfamiliar]
        row = {"fold": index + 1, "unfamiliar_rows": int(len(unfamiliar_idx)), "familiar_rows": int(len(familiar_idx))}
        for label, indexes in (("unfamiliar", unfamiliar_idx), ("familiar", familiar_idx)):
            row[f"current_score_only_ridge_{label}"] = study.metric_set(
                ratings[indexes], scores["current_ridge_score"][indexes]) if len(indexes) else None
            row[f"served_current_direct_{label}"] = study.metric_set(
                ratings[indexes], current[indexes]) if len(indexes) else None
            row[f"selected_complement_{label}"] = study.metric_set(
                ratings[indexes], selected_complement[indexes]) if len(indexes) else None
            row[f"selected_direct_replacement_{label}"] = study.metric_set(
                ratings[indexes], selected_direct[indexes]) if len(indexes) else None
            row[f"selected_capped_residual_{label}"] = study.metric_set(
                ratings[indexes], capped[indexes]) if len(indexes) else None
        unfamiliar_rows.append(row)
    aggregate["unfamiliar_author_development_folds"] = unfamiliar_rows
    aggregate["prediction_coverage"] = {
        "selected_direct_replacement": int(np.isfinite(selected_direct).sum()),
        "selected_representation_ridge_oof": int(np.isfinite(selected_ridge).sum()),
        "selected_current_plus_representation_oof": int(np.isfinite(selected_complement).sum()),
        "selected_capped_residual_with_fit_only_current_fallback": int(np.isfinite(capped).sum()),
        "fit_only_development_targets_using_current_fallback_for_capped_residual": int(folds[0]["evaluation_start"]),
        "development_oof_rows_used_for_inference": int(dev.sum()),
        "validation_targets_excluded_from_selection": int(validation.sum()),
        "later_targets_excluded_from_selection": int(later.sum()),
    }
    corpus = study.load_corpus(corpus_path)
    # The exact prepared item order is the target suffix after the 100-read warm-up.
    records, _, _ = study.prepare(corpus, "ollama", study.QWEN_MODEL)
    target_items = [row[1] for row in records[100:]]
    confidences = np.asarray([study._metadata_confidence(item) for item in target_items], dtype=np.float64)
    source_weights = np.asarray([float(item.get("source_weight") or 1.0) for item in target_items], dtype=np.float64)
    aggregate["candidate_scoring_context"] = {
        "representations_share_same_candidate_rows_and_confidence_terms": True,
        "metadata_confidence_unique_values": int(len(np.unique(confidences))),
        "metadata_confidence_min": float(confidences.min()) if len(confidences) else None,
        "metadata_confidence_mean": float(confidences.mean()) if len(confidences) else None,
        "metadata_confidence_max": float(confidences.max()) if len(confidences) else None,
        "source_weight_unique_values": int(len(np.unique(source_weights))),
        "source_weight_min": float(source_weights.min()) if len(source_weights) else None,
        "source_weight_mean": float(source_weights.mean()) if len(source_weights) else None,
        "source_weight_max": float(source_weights.max()) if len(source_weights) else None,
        "metadata_confidence_and_source_weight_are_representation_invariant": True,
    }
    aggregate["summary_mask_audit"] = {
        "development_oof_rule": "train_mask AND target_index >= first fixed development evaluation start",
        "development_oof_rows": int(dev.sum()),
        "validation_rows_excluded_from_development_metrics": int(validation.sum()),
        "later_rows_excluded_from_development_metrics": int(later.sum()),
        "refreshed_from_private_aligned_score_archive": True,
    }
    provenance_supplement = json.loads(provenance_supplement_path.read_text())
    text_views = study.build_text_views(corpus, records, provenance_supplement=provenance_supplement)
    if text_views["metadata_verification_mode"] != "field_provenance":
        raise ValueError("strict-v2 refresh did not activate field provenance verification")
    target_description = text_views["description_mask"][100:]
    target_subjects = text_views["subjects_mask"][100:]
    target_any = text_views["metadata_mask"][100:]
    if (not np.array_equal(target_any, target_description | target_subjects)
            or int(target_any.sum()) < int(target_description.sum())
            or int(target_any.sum()) < int(target_subjects.sum())):
        raise ValueError("eligible metadata coverage must be the description/subject union")
    metadata_counts = {
        "prepared_reads": int(len(records)),
        "prepared_reads_with_any_verified_metadata": int(text_views["metadata_mask"].sum()),
        "eligible_targets": int(len(records) - 100),
        "eligible_targets_with_any_verified_metadata": int(target_any.sum()),
        "eligible_targets_with_description": int(target_description.sum()),
        "eligible_targets_with_subjects": int(target_subjects.sum()),
        "eligible_targets_with_both": int(np.count_nonzero(target_description & target_subjects)),
        "eligible_targets_fallback_to_title_author": int(np.count_nonzero(~target_any)),
        "prepared_read_field_counts": text_views["metadata_sources"],
    }
    aggregate["metadata_coverage"] = metadata_counts
    from tokenizers import Tokenizer
    tokenizer_paths = sorted(bge_cache_dir.rglob("tokenizer.json"))
    if len(tokenizer_paths) > 1:
        raise ValueError("expected at most one cached BGE tokenizer for token-truncation coverage")

    def token_coverage(texts: list[str]) -> dict:
        counts = np.fromiter((len(tokenizer.encode(value).ids) for value in texts),
                             dtype=np.int32, count=len(texts))
        return {
            "rows": int(len(counts)),
            "over_512_token_input_limit": int(np.count_nonzero(counts > 512)),
            "maximum_input_tokens": int(counts.max()) if len(counts) else 0,
            "p95_input_tokens": float(np.quantile(counts, .95)) if len(counts) else 0.0,
            "input_limit_tokens": 512,
        }

    bge_runtime = aggregate["runtime"]["bge_small_both_text_views"]
    if tokenizer_paths:
        tokenizer = Tokenizer.from_file(str(tokenizer_paths[0]))
        bge_runtime["tokenizer_file_sha256"] = study.sha256_file(tokenizer_paths[0])
        bge_runtime["title_author_tokenization"] = token_coverage(text_views["title_author_text"])
        bge_runtime["verified_metadata_tokenization"] = token_coverage(text_views["verified_metadata_text"])
    elif "tokenizer_file_sha256" not in bge_runtime:
        raise ValueError("the frozen BGE tokenizer metrics are unavailable for refresh")
    else:
        bge_runtime["tokenization_refresh"] = (
            "reused previously recorded tokenizer coverage; private tokenizer cache was not retained"
        )
    for key, value in aggregate["runtime"].items():
        if key.startswith("qwen_"):
            value.setdefault("model_max_context_tokens", 32768)
            if value.get("effective_runner_context_tokens") is not None:
                value["effective_context_capture"] = "read-only Ollama /api/ps during strict-v2 scoring"
    temp = aggregate_path.with_suffix(".tmp.json")
    temp.write_text(json.dumps(aggregate, indent=2, sort_keys=True, allow_nan=False) + "\n")
    os.chmod(temp, 0o600)
    os.replace(temp, aggregate_path)
    return aggregate


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aggregate", type=Path, required=True)
    parser.add_argument("--scores", type=Path, required=True)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--bge-cache-dir", type=Path, required=True)
    parser.add_argument("--strict-amendment", type=Path, required=True)
    parser.add_argument("--method-amendment", type=Path, required=True)
    parser.add_argument("--provenance-supplement", type=Path, required=True)
    args = parser.parse_args()
    result = refresh(args.aggregate, args.scores, args.features, args.corpus, args.bge_cache_dir,
                     args.strict_amendment, args.method_amendment, args.provenance_supplement)
    print(json.dumps({
        "selected_arm": result["selected_arm"],
        "development_oof_rows": result["summary_mask_audit"]["development_oof_rows"],
        "validation_rows": result["prediction_coverage"]["validation_targets_excluded_from_selection"],
        "later_rows": result["prediction_coverage"]["later_targets_excluded_from_selection"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
