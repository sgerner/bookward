#!/usr/bin/env python3
"""Build private, parity-checked causal inputs for five recommendation tests.

Only aggregate counts are printed. Identity-aligned feature arrays belong in a
private output directory, never in the repository. This is a retrospective
read-as-candidate screen, not a reconstruction of historical recommendation
availability or proof of live recommendation lift.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import numpy as np
from threadpoolctl import threadpool_limits

import evaluate_ordinal_interests as ordinal
from evaluate_historical_ratings import whole_day_cuts
from evaluate_ranking import load_corpus, prepare
from afterword_engine.identity import book_identity
from afterword_engine.ranking import rank_candidates


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def day_boundary(days, index):
    index = int(index)
    while 0 < index < len(days) and days[index] == days[index - 1]:
        index += 1
    return index


def development_folds(days, train_stop):
    """Three expanding folds; the early development prefix only fits models."""
    boundaries = [day_boundary(days, value) for value in (300, 500, 700)]
    if not 0 < boundaries[0] < boundaries[1] < boundaries[2] < train_stop:
        raise ValueError("Too few whole-day development targets for fixed folds")
    return [
        {"train_stop": start, "evaluation_start": start, "evaluation_stop": stop}
        for start, stop in zip(boundaries, boundaries[1:] + [int(train_stop)])
    ]


def build_features(corpus_path, output_dir, backend, model):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output_dir, 0o700)
    data = load_corpus(Path(corpus_path))
    records, selected, excluded = prepare(data, backend, model)
    dates, cuts = whole_day_cuts(records)
    with threadpool_limits(limits=1):
        items, vectors, ratings, dates, features, current, interests, audit = ordinal.extract_features(
            records, with_interests=(3,)
        )
    parity = audit["exact_ranker_parity"]
    if parity["mismatches_at_served_precision"]:
        raise RuntimeError("Feature replay does not match the current serving ranker")
    usable = np.flatnonzero(np.isfinite(current))
    if not np.isfinite(features[usable]).all() or not np.isfinite(interests[3][usable]).all():
        raise RuntimeError("Causal feature replay has missing eligible features")
    eligible_days = np.asarray([dates[i].toordinal() for i in usable], dtype=np.int64)
    train_mask = usable < cuts[0]
    validation_mask = (usable >= cuts[0]) & (usable < cuts[1])
    later_mask = usable >= cuts[1]
    train_stop = int(train_mask.sum())
    folds = development_folds(eligible_days, train_stop)
    author_keys = [book_identity("", items[i].get("author", "")) for i in usable]
    author_map = {key: index for index, key in enumerate(sorted(set(author_keys)))}
    arrays = {
        "read_ids": np.asarray([items[i]["id"] for i in usable], dtype=np.int64),
        "record_indexes": usable,
        "ratings": ratings[usable],
        "utc_day": eligible_days,
        "author_group": np.asarray([author_map[key] for key in author_keys], dtype=np.int64),
        "current": current[usable],
        "base_features": features[usable],
        "cluster3": interests[3][usable, 0],
        "recency": features[usable, 6],
        "local": features[usable, 4],
        "train_mask": train_mask,
        "validation_mask": validation_mask,
        "later_mask": later_mask,
        "oof_selection_mask": (np.arange(len(usable)) >= folds[0]["evaluation_start"]) & train_mask,
    }
    # The old ordinal arm is reconstructed without future-label fitting for
    # the development selection rows. Earlier fit-only rows remain unknown.
    ordinal_expected = np.full(len(usable), np.nan, dtype=np.float64)
    for fold in folds + [
        {"train_stop": train_stop, "evaluation_start": train_stop,
         "evaluation_stop": train_stop + int(validation_mask.sum())},
        {"train_stop": train_stop + int(validation_mask.sum()),
         "evaluation_start": train_stop + int(validation_mask.sum()),
         "evaluation_stop": len(usable)},
    ]:
        stop, start, end = (fold[key] for key in ("train_stop", "evaluation_start", "evaluation_stop"))
        with threadpool_limits(limits=1):
            fitted = ordinal.fit_ordinal(arrays["base_features"][:stop], arrays["ratings"][:stop])
            ordinal_expected[start:end] = ordinal.ordinal_expected_ratings(
                fitted, arrays["base_features"][start:end]
            )
    arrays["ordinal_expected"] = ordinal_expected
    output = output_dir / "features-private.npz"
    np.savez_compressed(output, **arrays)
    os.chmod(output, 0o600)
    metadata = {
        "version": 1,
        "corpus_sha256": sha256_file(corpus_path),
        "feature_artifact_sha256": sha256_file(output),
        "backend_model": list(selected),
        "feature_names": list(ordinal.BASE_NAMES),
        "prepared_records": len(records),
        "eligible_targets": len(usable),
        "excluded": excluded,
        "split_counts": {"development": int(train_mask.sum()), "validation": int(validation_mask.sum()),
                         "later_exploratory": int(later_mask.sum())},
        "record_cuts": list(cuts),
        "development_folds": folds,
        "exact_ranker_parity": parity,
        "historical_query_representation": "cached title-author read documents; shared by all primary arms",
        "history_rule": "strictly earlier UTC calendar days; current target and same-day ratings hidden",
        "later_period": "previously inspected, descriptive only; refit uses only earlier rows",
    }
    meta_path = output_dir / "features-manifest-private.json"
    meta_path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    os.chmod(meta_path, 0o600)
    return metadata


def build_rich_features(corpus_path, output_dir, views_path, backend, model):
    """Replay frozen rich vectors with an identical query in both history arms.

    This is a separate representation experiment. Its current control uses the
    rich query and title-author history; the rich arm changes only history.
    Both use neutral query confidence, as does the primary replay.
    """
    output_dir = Path(output_dir)
    primary_path = output_dir / "features-private.npz"
    manifest = json.loads((output_dir / "features-manifest-private.json").read_text())
    with np.load(primary_path, allow_pickle=False) as archive:
        arrays = {key: archive[key].copy() for key in archive.files}
    records, _, _ = prepare(load_corpus(Path(corpus_path)), backend, model)
    items = [record[1] for record in records]
    dates = [record[0].date() for record in records]
    actual = np.stack([record[2] for record in records]).astype(np.float32)
    with np.load(views_path, allow_pickle=False) as archive:
        ids = archive["read_ids"]
        if len(set(map(int, ids))) != len(ids):
            raise ValueError("Frozen rich vector IDs are not unique")
        lookup = {int(value): i for i, value in enumerate(ids)}
        indexes = np.asarray([lookup[int(item["id"])] for item in items])
        archived_actual = archive["read_actual_ta"][indexes]
        if not np.array_equal(actual, archived_actual):
            raise ValueError("Frozen rich study does not share the current historical vectors")
        rich = archive["read_full"][indexes].astype(np.float32)
        query = archive["query_new_full"][indexes].astype(np.float32)
        coverage = archive["full_mask"][indexes].astype(bool)
    if not np.isfinite(rich).all() or not np.isfinite(query).all():
        raise ValueError("Nonfinite frozen rich vectors")
    x = np.full((len(records), len(ordinal.BASE_NAMES)), np.nan)
    current = np.full(len(records), np.nan)
    query_control = np.full(len(records), np.nan)
    start = 0
    parity_count = 0
    with threadpool_limits(limits=1):
        while start < len(records):
            stop = start + 1
            while stop < len(records) and dates[stop] == dates[start]:
                stop += 1
            if start >= 100:
                h = np.asarray(sorted(range(start), key=lambda i: items[i]["id"]))
                targets = np.arange(start, stop)
                history = [items[i] for i in h]
                queries = [{"id": items[i]["id"], "title": items[i]["title"],
                            "author": items[i].get("author", ""), "source_weight": 1.0}
                           for i in targets]
                features, scores = ordinal.history_components(history, rich[h], queries, query[targets])
                direct = rank_candidates(history, rich[h], queries, query[targets])
                served = np.asarray([row["score"] for row in direct])
                replay = np.asarray([round(float(value), 1) for value in scores])
                if not np.array_equal(served, replay):
                    raise ValueError("Rich replay differs from the authoritative serving scorer")
                x[targets] = features
                current[targets] = served
                control = rank_candidates(history, actual[h], queries, query[targets])
                query_control[targets] = [row["score"] for row in control]
                parity_count += len(targets)
            start = stop
    eligible = arrays["record_indexes"]
    if not np.array_equal(arrays["read_ids"], [items[i]["id"] for i in eligible]):
        raise ValueError("Rich replay target alignment differs from primary replay")
    arrays["primary_current"] = arrays["current"].copy()
    arrays["matched_query_control"] = query_control[eligible]
    arrays["current"] = current[eligible]
    arrays["base_features"] = x[eligible]
    arrays["recency"] = arrays["base_features"][:, 6]
    arrays["local"] = arrays["base_features"][:, 4]
    arrays["metadata_coverage"] = coverage[eligible]
    # Cluster3 remains an explicitly labelled original-view causal signal.
    arrays["ordinal_expected"] = np.full(len(eligible), np.nan)
    train_stop = int(arrays["train_mask"].sum())
    val_stop = train_stop + int(arrays["validation_mask"].sum())
    folds = manifest["development_folds"] + [
        {"train_stop": train_stop, "evaluation_start": train_stop, "evaluation_stop": val_stop},
        {"train_stop": val_stop, "evaluation_start": val_stop, "evaluation_stop": len(eligible)},
    ]
    with threadpool_limits(limits=1):
        for fold in folds:
            end = fold["train_stop"]
            begin, stop = fold["evaluation_start"], fold["evaluation_stop"]
            model_fit = ordinal.fit_ordinal(arrays["base_features"][:end], arrays["ratings"][:end])
            arrays["ordinal_expected"][begin:stop] = ordinal.ordinal_expected_ratings(
                model_fit, arrays["base_features"][begin:stop])
    output = output_dir / "rich-features-private.npz"
    np.savez_compressed(output, **arrays)
    os.chmod(output, 0o600)
    metadata = dict(manifest, feature_artifact_sha256=sha256_file(output),
                    frozen_views_sha256=sha256_file(views_path),
                    representation="frozen verified rich history and rich query",
                    query_control="same rich query with original title-author history",
                    confidence="identical neutral query confidence in both arms",
                    original_vector_byte_equality=True,
                    exact_ranker_parity={"compared_predictions": parity_count,
                                        "mismatches_at_served_precision": 0},
                    cluster3="original title-author history signal, explicitly mixed-view")
    path = output_dir / "rich-features-manifest-private.json"
    path.write_text(json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    os.chmod(path, 0o600)
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--backend", default="ollama")
    parser.add_argument("--model", default="qwen3-embedding:4b")
    parser.add_argument("--rich-views", type=Path)
    args = parser.parse_args()
    if args.rich_views:
        result = build_rich_features(args.corpus, args.output_dir, args.rich_views, args.backend, args.model)
    else:
        result = build_features(args.corpus, args.output_dir, args.backend, args.model)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
