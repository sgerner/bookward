#!/usr/bin/env python3
"""Combine private, causal study scores and select on development folds only.

The selection pass never reads validation labels or computes validation metrics.
The reporting pass checks the saved selection and treats all runner-up results
as exploratory. Inputs and identity-aligned outputs stay outside the checkout.
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import os
from pathlib import Path

import numpy as np
from scipy.stats import rankdata


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def served(values):
    return np.asarray([round(float(x), 1) if np.isfinite(x) else np.nan
                       for x in np.clip(values, 0, 100)], dtype=np.float64)


def auc(labels, scores):
    labels = np.asarray(labels, dtype=bool)
    n = len(labels)
    positive = int(labels.sum())
    if not 0 < positive < n:
        return None
    ranks = rankdata(scores, method="average")
    return float((ranks[labels].sum() - positive * (positive + 1) / 2)
                 / (positive * (n - positive)))


def metrics(y, scores):
    y, scores = np.asarray(y), np.asarray(scores)
    if not len(y) or not np.isfinite(scores).all():
        raise ValueError("Metrics require a nonempty aligned finite score slice")
    high, low = y >= 4, y <= 2
    ha, la = auc(high, scores), auc(low, -scores)
    k = min(20, len(y))
    top = np.argsort(-scores, kind="stable")[:k]
    bottom = np.argsort(scores, kind="stable")[:k]
    tails = high | low
    return {"n": len(y), "high_n": int(high.sum()), "low_n": int(low.sum()),
            "high_auc": ha, "low_auc": la,
            "balanced_auc": None if ha is None or la is None else (ha + la) / 2,
            "high_vs_low_auc": auc(high[tails], scores[tails]),
            "top20_high_precision": float(high[top].mean()),
            "bottom20_low_precision": float(low[bottom].mean())}


def grouped_bootstrap(y, base, candidate, groups, repetitions=2000, seed=20261002):
    """Paired group bootstrap; each draw keeps every book in a sampled group."""
    labels = np.unique(groups)
    indexes = [np.flatnonzero(groups == key) for key in labels]
    if len(indexes) < 2:
        return {"valid_replicates": 0, "reason": "too_few_groups"}
    rng = np.random.default_rng(seed)
    deltas = []
    for _ in range(repetitions):
        draw = np.concatenate([indexes[i] for i in rng.integers(0, len(indexes), len(indexes))])
        ys, a, b = y[draw], base[draw], candidate[draw]
        high_a, high_b = auc(ys >= 4, a), auc(ys >= 4, b)
        low_a, low_b = auc(ys <= 2, -a), auc(ys <= 2, -b)
        if None in (high_a, high_b, low_a, low_b):
            continue
        dh, dl = high_b - high_a, low_b - low_a
        deltas.append(((dh + dl) / 2, dh, dl))
    if not deltas:
        return {"valid_replicates": 0, "reason": "no_replicates_with_both_endpoints"}
    values = np.asarray(deltas)
    return {"requested_replicates": repetitions, "valid_replicates": len(values),
            "group_count": len(indexes), "seed": seed,
            **{name: np.quantile(values[:, col], [.025, .975]).tolist()
               for col, name in enumerate(("balanced_delta_ci95", "high_delta_ci95", "low_delta_ci95"))}}


def load_scores(features_path, score_paths):
    with np.load(features_path, allow_pickle=False) as archive:
        features = {name: archive[name].copy() for name in archive.files}
    scores = {"current": features["current"]}
    aligned = {}
    for path in score_paths:
        with np.load(path, allow_pickle=False) as archive:
            if not np.array_equal(features["read_ids"], archive["read_ids"]):
                raise ValueError("Score archive is not identity-aligned to frozen features")
            for key in ("train_mask", "validation_mask", "later_mask", "oof_selection_mask"):
                if key not in archive or not np.array_equal(features[key], archive[key]):
                    raise ValueError(f"Score archive split mask differs from frozen features: {key}")
            for key in archive.files:
                if key.startswith("score__"):
                    name = key.removeprefix("score__")
                    if name in scores:
                        if np.array_equal(scores[name], archive[key], equal_nan=True):
                            continue
                        raise ValueError("Duplicate score arm with different predictions")
                    scores[name] = archive[key].copy()
                if key.startswith("aligned_"):
                    name = key.removeprefix("aligned_")
                    if name not in {"heads", "pairwise", "metric", "facets"}:
                        continue
                    if name in aligned:
                        raise ValueError("Duplicate aligned family")
                    aligned[name] = archive[key].copy()
                if key.startswith("selected_"):
                    name = key.removeprefix("selected_")
                    if name not in scores:
                        scores[name] = archive[key].copy()
    for name, values in aligned.items():
        scores[f"current75_{name}25"] = served(.75 * scores["current"] + .25 * values)
    for (name_a, a), (name_b, b) in itertools.combinations(sorted(aligned.items()), 2):
        scores[f"pair_{name_a}_{name_b}"] = served(.5 * a + .5 * b)
    for name, values in scores.items():
        if values.shape != features["current"].shape or np.isinf(values).any():
            raise ValueError(f"Malformed score arm: {name}")
    return features, scores


def validate_folds(features, manifest):
    days = features["utc_day"]
    if np.any(np.diff(days) < 0):
        raise ValueError("UTC days must be chronological")
    masks = [features[name] for name in ("train_mask", "validation_mask", "later_mask")]
    if any(mask.dtype != np.bool_ or mask.shape != days.shape for mask in masks):
        raise ValueError("Split masks must be aligned boolean vectors")
    if np.any(sum(mask.astype(int) for mask in masks) != 1):
        raise ValueError("Split masks must partition every eligible target")
    train_stop = int(masks[0].sum())
    val_stop = train_stop + int(masks[1].sum())
    if not np.array_equal(masks[0], np.arange(len(days)) < train_stop):
        raise ValueError("Development split must be an earlier prefix")
    if not np.array_equal(masks[1], (np.arange(len(days)) >= train_stop) & (np.arange(len(days)) < val_stop)):
        raise ValueError("Validation split must follow development")
    covered = np.zeros(len(days), dtype=bool)
    boundaries = [train_stop, val_stop]
    for fold in manifest["development_folds"]:
        start, stop, fit_stop = (fold[key] for key in ("evaluation_start", "evaluation_stop", "train_stop"))
        if not 0 < fit_stop == start < stop <= train_stop or covered[start:stop].any():
            raise ValueError("Fold must score disjoint development rows after its training prefix")
        covered[start:stop] = True
        boundaries.extend([start, stop])
    if not np.array_equal(covered, features["oof_selection_mask"]):
        raise ValueError("Manifest folds differ from frozen OOF selection mask")
    for index in boundaries:
        if 0 < index < len(days) and days[index] == days[index - 1]:
            raise ValueError("Fold or split boundary divides a UTC day")


def selection(features, scores, manifest):
    y = features["ratings"]
    results = {}
    for name, values in sorted(scores.items()):
        folds = []
        for fold in manifest["development_folds"]:
            begin, end = fold["evaluation_start"], fold["evaluation_stop"]
            if not np.isfinite(values[begin:end]).all():
                raise ValueError(f"Missing OOF predictions: {name}")
            folds.append(metrics(y[begin:end], values[begin:end]))
        results[name] = {"folds": folds,
                         "mean_fold_balanced_auc": float(np.mean([f["balanced_auc"] for f in folds]))}
    candidates = [name for name in results if name != "current"]
    if not candidates:
        raise ValueError("No candidate score arms supplied")
    # Prefer the simplest family on exact ties; this ordering is deterministic
    # and does not consult validation or the already-inspected later period.
    chosen = sorted(candidates, key=lambda name: (-results[name]["mean_fold_balanced_auc"],
                                                 name.count("_") + len(name) / 1000, name))[0]
    return {"selected_arm": chosen, "development_only": True,
            "selection_metric": "unweighted mean of three within-fold balanced high/low AUCs",
            "arms": results}


def report(features, scores, chosen, unseen_author=None, repetitions=2000):
    y, current = features["ratings"], features["current"]
    result = {"selected_arm": chosen, "runner_up_status": "exploratory; never promoted from validation wins",
              "splits": {}}
    for split, mask_name in (("validation", "validation_mask"), ("later_exploratory", "later_mask")):
        mask = features[mask_name]
        base = metrics(y[mask], current[mask])
        arms = {}
        for name, values in sorted(scores.items()):
            measured = metrics(y[mask], values[mask])
            arms[name] = {"metrics": measured,
                          "balanced_delta": measured["balanced_auc"] - base["balanced_auc"],
                          "high_delta": measured["high_auc"] - base["high_auc"],
                          "low_delta": measured["low_auc"] - base["low_auc"]}
        selected = scores[chosen]
        group_types = {"UTC_day": features["utc_day"], "author": features["author_group"],
                       "30_day_block": features["utc_day"] // 30}
        uncertainty = {name: grouped_bootstrap(y[mask], current[mask], selected[mask], groups[mask], repetitions)
                       for name, groups in group_types.items()}
        indexes = np.flatnonzero(mask)
        days = np.unique(features["utc_day"][indexes])
        mid = days[len(days) // 2]
        slice_masks = {"first_temporal_half": mask & (features["utc_day"] < mid),
                       "second_temporal_half": mask & (features["utc_day"] >= mid)}
        if unseen_author is not None:
            slice_masks["unseen_author"] = mask & unseen_author
        slices = {name: {"current": metrics(y[use], current[use]),
                         "selected": metrics(y[use], selected[use])}
                  for name, use in slice_masks.items() if use.any()}
        result["splits"][split] = {"arms": arms, "selected_uncertainty": uncertainty,
                                    "selected_slices": slices}
    return result


def private_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.chmod(path, 0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--scores", type=Path, action="append", required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--repetitions", type=int, default=2000)
    args = parser.parse_args()
    features, scores = load_scores(args.features, args.scores)
    manifest = json.loads(args.manifest.read_text())
    protocol = json.loads(args.protocol.read_text())
    if sha(args.features) != manifest["feature_artifact_sha256"]:
        raise ValueError("Feature artifact hash differs from frozen manifest")
    if protocol["corpus_sha256"] != manifest["corpus_sha256"]:
        raise ValueError("Protocol and feature manifest use different corpora")
    validate_folds(features, manifest)
    result = selection(features, scores, manifest)
    hashes = {"features": sha(args.features), "manifest": sha(args.manifest),
              "protocol": sha(args.protocol), "score_archives": [sha(p) for p in args.scores]}
    result["input_hashes"] = hashes
    if not args.report:
        private_json(args.selection, result)
        print(json.dumps({"selected_arm": result["selected_arm"], "arms": len(scores),
                          "selection_sha256": sha(args.selection), "development_only": True}))
        return
    locked = json.loads(args.selection.read_text())
    if result != locked:
        raise ValueError("Reporting inputs differ from locked development selection")
    unseen = None
    if args.corpus:
        from evaluate_ranking import load_corpus, prepare
        from afterword_engine.identity import book_identity
        corpus = load_corpus(args.corpus)
        records, _, _ = prepare(corpus, *manifest["backend_model"])
        first = {}
        earliest_by_id = {}
        for timestamp, item, _ in records:
            key = book_identity("", item.get("author", ""))
            day = timestamp.date().toordinal()
            first.setdefault(key, day)
            earliest_by_id[int(item["id"])] = first[key] >= day
        unseen = np.asarray([earliest_by_id[int(rid)] for rid in features["read_ids"]])
    final = report(features, scores, result["selected_arm"], unseen, args.repetitions)
    final["selection_sha256"] = sha(args.selection)
    final["input_hashes"] = hashes
    private_json(args.report, final)
    print(json.dumps(final, sort_keys=True))


if __name__ == "__main__":
    main()
