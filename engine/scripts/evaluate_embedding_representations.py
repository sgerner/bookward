#!/usr/bin/env python3
"""Causal, aggregate-only comparison of book embedding representations.

The script reads a frozen private corpus and aligned causal feature artifact,
embeds only with the locally installed Qwen model and the bounded private
FastEmbed cache, then writes identity-aligned vectors/scores only to the
private evidence directory. The repository receives aggregate metrics only.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import os
import resource
import sys
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
import numpy as np
from scipy.sparse import csr_matrix, hstack
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import Ridge
from threadpoolctl import threadpool_limits

ENGINE = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
sys.path[:0] = [str(SCRIPTS), str(ENGINE)]

from afterword_engine.embeddings import content_hash
from afterword_engine.ingestion import _clean_text, _read_metadata_identity_hash
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
from afterword_engine.scoring import document
from afterword_engine.subjects import normalize_subjects
from evaluate_next_five import development_folds as frozen_development_folds
from evaluate_ordinal_interests import BASE_NAMES, history_components
from evaluate_ranking import load_corpus, prepare, read_time

QWEN_MODEL = "qwen3-embedding:4b"
QWEN_DIGEST = "df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907"
QWEN_INSTRUCTION = (
    "Given a book a reader enjoyed, retrieve other books with similar likely reader appeal."
)
BGE_MODEL = "BAAI/bge-small-en-v1.5"
BGE_EXPECTED_SOURCE = "Qdrant/bge-small-en-v1.5-onnx-Q"
BGE_MAX_CACHE_BYTES = 100 * 1024 * 1024
BGE_INPUT_TOKEN_LIMIT = 512
BATCH_SIZE = 32
RATING_RIDGE_ALPHA = 10.0
BOOTSTRAP_REPLICATES = 2000
BOOTSTRAP_SEED = 20261002
REPRESENTATIONS = (
    "qwen_current",
    "qwen_instructed",
    "bge_small",
    "tfidf",
)
TEXT_VIEWS = ("title_author", "verified_metadata")
ARM_ORDER = tuple(f"{representation}_{view}" for representation in REPRESENTATIONS for view in TEXT_VIEWS)


def unique_file_bytes(root: Path) -> int:
    """Count bytes once per underlying file; HF snapshots contain symlinks."""
    seen: set[tuple[int, int]] = set()
    total = 0
    for path in root.rglob("*"):
        try:
            info = path.stat()
        except FileNotFoundError:
            continue
        if not path.is_file():
            continue
        identity = (int(info.st_dev), int(info.st_ino))
        if identity not in seen:
            seen.add(identity)
            total += int(info.st_size)
    return total


def instructed_query(text: str, instruction: str = QWEN_INSTRUCTION) -> str:
    return f"Instruct: {instruction}\nQuery: {text}"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def unit_rows(values: np.ndarray) -> np.ndarray:
    matrix = np.asarray(values, dtype=np.float32)
    if matrix.ndim != 2 or not matrix.shape[1] or not np.isfinite(matrix).all():
        raise ValueError("vectors must be a finite non-empty matrix")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return np.divide(matrix, norms, out=np.zeros_like(matrix), where=norms > 0)


def build_text_views(
    data: dict[str, Any],
    records: list[tuple[date, dict, np.ndarray]],
    *,
    provenance_supplement: dict[str, Any] | None = None,
):
    """Build read documents; v2 consumes only fields passing the frozen source audit."""
    reads = {int(item["id"]): item for item in data["reads"]}
    metadata_rows: dict[int, dict] = {}
    original_metadata_rows: dict[int, dict] = {}
    invalid_metadata = 0
    duplicate_metadata = 0
    for row in data.get("read_metadata", []):
        read_id = int(row.get("read_id") or 0)
        if read_id:
            original_metadata_rows.setdefault(read_id, row)
        item = reads.get(read_id)
        if item is None or not row.get("verified_work_id"):
            invalid_metadata += 1
            continue
        if row.get("identity_hash") != _read_metadata_identity_hash(item):
            invalid_metadata += 1
            continue
        if read_id in metadata_rows:
            duplicate_metadata += 1
            continue
        metadata_rows[read_id] = row

    supplements: dict[int, dict] = {}
    provenance_by_read: dict[int, list[dict]] = {}
    verified_field = None
    if provenance_supplement is not None:
        import evaluate_metric_facets as grounded

        supplements = {
            int(row["read_id"]): row
            for row in provenance_supplement.get("read_metadata", [])
        }
        if set(supplements) != set(original_metadata_rows):
            raise ValueError("frozen provenance supplement does not align with read metadata")
        for read_id, original in original_metadata_rows.items():
            if any(supplements[read_id].get(key) != value for key, value in original.items()):
                raise ValueError("frozen metadata differs from provenance supplement")
        for row in provenance_supplement.get("metadata_field_provenance", []):
            if row.get("entity_type") == "read":
                provenance_by_read.setdefault(int(row["entity_id"]), []).append(row)
        verified_field = grounded._verified_field_provenance

    items = [row[1] for row in records]
    read_ids = np.asarray([int(item["id"]) for item in items], dtype=np.int64)
    title_author: list[str] = []
    verified_metadata: list[str] = []
    metadata_mask = np.zeros(len(items), dtype=bool)
    description_mask = np.zeros(len(items), dtype=bool)
    subjects_mask = np.zeros(len(items), dtype=bool)
    source_counts: dict[str, int] = defaultdict(int)
    for index, item in enumerate(items):
        # Calling the production formatter keeps the control text byte-for-byte
        # aligned with the current read embedding document.
        ta_item = dict(item)
        ta_item["rating"] = item.get("rating", 1)
        ta_item["genres"] = "[]"
        ta_item["description"] = ""
        ta = document(ta_item)
        title_author.append(ta)

        if provenance_supplement is None:
            meta = metadata_rows.get(int(item["id"]))
            description = _clean_text(str(meta.get("description") or ""), limit=4000) if meta else ""
            subjects = normalize_subjects(meta.get("genres"), limit=8) if meta else []
        else:
            meta = original_metadata_rows.get(int(item["id"]))
            supplemental = supplements.get(int(item["id"]))
            field_rows = provenance_by_read.get(int(item["id"]), [])
            description_provenance = verified_field(
                item, meta, supplemental, field_rows, "description"
            )
            subjects_provenance = verified_field(
                item, meta, supplemental, field_rows, "genres"
            )
            raw_description = _clean_text(str(meta.get("description") or ""), limit=4000) if meta else ""
            raw_subjects = normalize_subjects(meta.get("genres"), limit=8) if meta else []
            source_counts["unverified_description_fields_rejected"] += bool(raw_description and not description_provenance)
            source_counts["unverified_subject_fields_rejected"] += bool(raw_subjects and not subjects_provenance)
            source_counts["opening_sentence_fields_rejected"] += bool(
                raw_description and meta and meta.get("description_kind") == "opening_sentence"
                and not description_provenance
            )
            description = raw_description if description_provenance else ""
            subjects = raw_subjects if subjects_provenance else []
        description_mask[index] = bool(description)
        subjects_mask[index] = bool(subjects)
        if meta and (description or subjects):
            metadata_mask[index] = True
            source_counts["description"] += bool(description)
            source_counts["subjects"] += bool(subjects)
            source_counts["both"] += bool(description and subjects)
            source_counts[f"description_kind:{str(meta.get('description_kind') or 'unknown')}"] += bool(description)
            m_item = dict(item)
            m_item["rating"] = item.get("rating", 1)
            m_item["genres"] = json.dumps(subjects, ensure_ascii=False, separators=(",", ":"))
            m_item["description"] = description
            rich = document(m_item)
        else:
            # Missing evidence contributes no implicit negative signal and
            # retains the exact title-author fallback.
            rich = ta
        verified_metadata.append(rich)

    if duplicate_metadata:
        raise ValueError("verified metadata contains duplicate read identities")
    return {
        "read_ids": read_ids,
        "items": items,
        "title_author_text": title_author,
        "verified_metadata_text": verified_metadata,
        "metadata_mask": metadata_mask,
        "description_mask": description_mask,
        "subjects_mask": subjects_mask,
        "metadata_verification_mode": "field_provenance" if provenance_supplement is not None else "identity_only",
        "verified_metadata_rows": len(metadata_rows),
        "invalid_metadata_rows": int(invalid_metadata),
        "metadata_sources": dict(source_counts),
    }


def _row_text_hashes(texts: list[str]) -> np.ndarray:
    return np.asarray([
        hashlib.sha256(text.encode("utf-8")).hexdigest() for text in texts
    ], dtype="U64")


def _reuse_cached_vectors(
    texts: list[str],
    old_texts: list[str],
    cached_vectors: np.ndarray,
    *,
    dimensions: int,
    encoder,
) -> tuple[np.ndarray, dict[str, int]]:
    """Reuse a row only when its exact text hash is unchanged."""
    cached = np.asarray(cached_vectors, dtype=np.float32)
    if cached.shape != (len(old_texts), dimensions) or not np.isfinite(cached).all():
        raise ValueError("cached vectors do not match their prior text rows and dimensions")
    if len(texts) != len(old_texts):
        raise ValueError("new and cached text rows differ in length")
    old_hashes = _row_text_hashes(old_texts)
    new_hashes = _row_text_hashes(texts)
    reusable = old_hashes == new_hashes
    output = cached.copy()
    changed_indexes = np.flatnonzero(~reusable)
    if len(changed_indexes):
        changed_texts = [texts[int(index)] for index in changed_indexes]
        encoded = np.asarray(encoder(changed_texts), dtype=np.float32)
        if encoded.shape != (len(changed_indexes), dimensions) or not np.isfinite(encoded).all():
            raise ValueError("fresh vectors do not match changed text rows and dimensions")
        output[changed_indexes] = encoded
    return output, {
        "rows": len(texts),
        "reused_rows": int(reusable.sum()),
        "freshly_encoded_rows": int(len(changed_indexes)),
    }


def _validate_prior_vector_archive(
    *,
    prior_vectors_path: Path,
    prior_aggregate_path: Path,
    base_protocol_path: Path,
    prepared_read_ids: np.ndarray,
    target_read_ids: np.ndarray,
    bge_cache_dir: Path,
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    """Check v1 artifact lineage before using it as a per-row vector cache."""
    aggregate = json.loads(prior_aggregate_path.read_text())
    recorded = aggregate.get("private_artifacts", {})
    if recorded.get("vector_sha256") != sha256_file(prior_vectors_path):
        raise ValueError("prior vector archive checksum differs from its private manifest")
    if aggregate.get("frozen_protocol_sha256") != sha256_file(base_protocol_path):
        raise ValueError("prior vectors were not produced from the supplied frozen protocol")
    for key in ("qwen_current_title_author", "qwen_current_verified_metadata",
                "qwen_instructed_title_author_queries", "qwen_instructed_verified_metadata_queries"):
        runtime = aggregate.get("runtime", {}).get(key, {})
        if runtime.get("model") != QWEN_MODEL or runtime.get("digest") != QWEN_DIGEST:
            raise ValueError("prior Qwen vector lineage differs from the frozen model")
        expected_rows = 1777 if "queries" not in key else 1677
        if runtime.get("documents") != expected_rows or runtime.get("dimensions") != 2560:
            raise ValueError("prior Qwen vector coverage differs from the frozen cohort")
    bge_runtime = aggregate.get("runtime", {}).get("bge_small_both_text_views", {})
    if (bge_runtime.get("model") != BGE_MODEL
            or bge_runtime.get("fastembed_version") != "0.8.1"
            or bge_runtime.get("artifact_sha256") != "51f1bd0addd6e859e42c2c8021a5e5461385bb676a649f4b269aa445449f2431"):
        raise ValueError("prior BGE vector lineage differs from the frozen model")
    onnx_files = sorted(bge_cache_dir.rglob("model_optimized.onnx"))
    if len(onnx_files) != 1 or sha256_file(onnx_files[0]) != bge_runtime.get("artifact_sha256"):
        raise ValueError("cached BGE artifact differs from the prior vector manifest")
    with np.load(prior_vectors_path, allow_pickle=False) as archive:
        required = {
            "prepared_read_ids", "target_read_ids", "qwen_document_title_author",
            "qwen_document_verified_metadata", "qwen_query_instructed_title_author",
            "qwen_query_instructed_verified_metadata", "bge_document_title_author",
            "bge_document_verified_metadata",
        }
        if not required.issubset(archive.files):
            raise ValueError("prior vector archive is missing a frozen representation family")
        prior = {name: np.asarray(archive[name]).copy() for name in required}
    if (not np.array_equal(prior["prepared_read_ids"], prepared_read_ids)
            or not np.array_equal(prior["target_read_ids"], target_read_ids)):
        raise ValueError("prior vectors do not align to the exact frozen prepared and target IDs")
    return prior, aggregate


def validate_fresh_qwen_vectors(
    current_corpus: dict,
    fresh_corpus_path: Path,
    fresh_vectors_path: Path,
    fresh_manifest_path: Path,
    model_digest: str,
) -> dict[int, np.ndarray]:
    """Validate fresh Qwen artifact against exact current read documents."""
    old = load_corpus(fresh_corpus_path)
    old_reads = {int(item["id"]): item for item in old["reads"]}
    current_reads = {int(item["id"]): item for item in current_corpus["reads"]}
    manifest = json.loads(fresh_manifest_path.read_text())
    if manifest.get("model") != QWEN_MODEL or manifest.get("model_digest") != model_digest:
        raise ValueError("fresh Qwen model lineage does not match the frozen local model")
    if manifest.get("exact_document_cache_hash_matches") != len(old_reads):
        raise ValueError("fresh Qwen artifact was not made from exact production read documents")
    if set(old_reads) != set(current_reads):
        raise ValueError("fresh Qwen and current corpus read IDs differ")
    for read_id in old_reads:
        if document(old_reads[read_id]) != document(current_reads[read_id]):
            raise ValueError("fresh Qwen input text differs from the current frozen corpus")
    with np.load(fresh_vectors_path, allow_pickle=False) as archive:
        ids = np.asarray(archive["read_ids"], dtype=np.int64)
        vectors = np.asarray(archive["read_vectors"], dtype=np.float32)
    if len(set(map(int, ids))) != len(ids) or set(map(int, ids)) != set(current_reads):
        raise ValueError("fresh Qwen vector IDs do not match current reads")
    lookup = {int(read_id): i for i, read_id in enumerate(ids)}
    if vectors.shape != (len(ids), 2560) or not np.isfinite(vectors).all():
        raise ValueError("fresh Qwen vector dimensions or values are invalid")
    return {int(read_id): vectors[lookup[int(read_id)]] for read_id in ids}


def tfidf_vectors(
    train_texts: list[str],
    all_texts: list[str],
) -> tuple[csr_matrix, dict[str, int]]:
    """Fit word/character vocabulary and IDF on training text only."""
    if not train_texts:
        raise ValueError("TF-IDF training prefix is empty")
    word = TfidfVectorizer(
        analyzer="word", ngram_range=(1, 2), stop_words="english",
        lowercase=True, strip_accents="unicode", norm="l2", min_df=1,
        max_features=50000,
    )
    char = TfidfVectorizer(
        analyzer="char_wb", ngram_range=(3, 5), lowercase=True,
        strip_accents="unicode", norm="l2", min_df=1, max_features=100000,
    )
    word_train = word.fit_transform(train_texts)
    char_train = char.fit_transform(train_texts)
    word_all = word.transform(all_texts)
    char_all = char.transform(all_texts)
    features = hstack((word_all * 0.5, char_all * 0.5), format="csr", dtype=np.float32)
    row_norms = np.sqrt(np.asarray(features.multiply(features).sum(axis=1)).ravel())
    inverse = np.divide(1.0, row_norms, out=np.zeros_like(row_norms), where=row_norms > 0)
    features = features.multiply(inverse[:, None]).tocsr()
    return features, {
        "word_vocabulary": int(len(word.vocabulary_)),
        "character_vocabulary": int(len(char.vocabulary_)),
        "train_documents": int(len(train_texts)),
        "nonzero_rows": int(np.count_nonzero(row_norms)),
        "zero_rows": int(np.count_nonzero(row_norms == 0)),
    }


def _safe_standardize(train: np.ndarray, values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    train = np.asarray(train, dtype=np.float64)
    values = np.asarray(values, dtype=np.float64)
    mean = np.mean(train, axis=0)
    scale = np.std(train, axis=0)
    scale = np.where(scale < 1e-8, 1.0, scale)
    return (train - mean) / scale, (values - mean) / scale


def fit_ridge_predictions(
    train_features: np.ndarray,
    train_ratings: np.ndarray,
    eval_features: np.ndarray,
    *,
    alpha: float = RATING_RIDGE_ALPHA,
) -> np.ndarray:
    """Fit the same small rating learner with train-only feature scaling."""
    train_features = np.asarray(train_features, dtype=np.float64)
    eval_features = np.asarray(eval_features, dtype=np.float64)
    if train_features.ndim != 2 or eval_features.ndim != 2 or train_features.shape[1] != eval_features.shape[1]:
        raise ValueError("training and evaluation feature widths do not match")
    if not len(train_features) or not np.isfinite(train_features).all() or not np.isfinite(eval_features).all():
        raise ValueError("ridge features must be finite and training must be non-empty")
    train_scaled, eval_scaled = _safe_standardize(train_features, eval_features)
    model = Ridge(alpha=alpha, fit_intercept=True)
    model.fit(train_scaled, np.asarray(train_ratings, dtype=np.float64))
    prediction = model.predict(eval_scaled)
    if not np.isfinite(prediction).all():
        raise ValueError("ridge learner returned non-finite predictions")
    return prediction


def history_features(
    records: list[tuple[date, dict, np.ndarray]],
    history_vectors: np.ndarray,
    query_vectors: np.ndarray,
    *,
    min_history: int = 100,
) -> tuple[np.ndarray, np.ndarray]:
    """Replay feature and direct-score paths with strict earlier-day history."""
    if history_vectors.shape[0] != len(records):
        raise ValueError("history vectors must align with all prepared records")
    target_start = min_history
    target_count = len(records) - target_start
    if query_vectors.shape[0] != target_count:
        raise ValueError("query vectors must align with eligible target rows")
    feature_values = np.full((target_count, len(BASE_NAMES)), np.nan, dtype=np.float64)
    direct_scores = np.full(target_count, np.nan, dtype=np.float64)
    days = [row[0].date() for row in records]
    items = [row[1] for row in records]
    cursor = target_start
    parity_checked = 0
    with threadpool_limits(limits=1):
        while cursor < len(records):
            stop = cursor + 1
            while stop < len(records) and days[stop] == days[cursor]:
                stop += 1
            history_end = cursor
            while history_end > 0 and days[history_end - 1] >= days[cursor]:
                history_end -= 1
            if history_end >= min_history:
                history_indexes = np.asarray(
                    sorted(range(history_end), key=lambda index: int(items[index]["id"])), dtype=np.int64
                )
                target_indexes = np.arange(cursor, stop, dtype=np.int64)
                history = [items[index] for index in history_indexes]
                queries = [items[index] for index in target_indexes]
                X, scores = history_components(
                    history,
                    history_vectors[history_indexes],
                    queries,
                    query_vectors[target_indexes - target_start],
                )
                feature_values[target_indexes - target_start] = X
                direct_scores[target_indexes - target_start] = np.round(scores, 1)
                exact = np.asarray([
                    row["score"] for row in rank_candidates(
                        history,
                        history_vectors[history_indexes],
                        queries,
                        query_vectors[target_indexes - target_start],
                    )
                ], dtype=np.float64)
                if not np.array_equal(exact, direct_scores[target_indexes - target_start]):
                    raise ValueError("direct representation replay differs from the serving ranker")
                parity_checked += len(target_indexes)
            cursor = stop
    if parity_checked != target_count or not np.isfinite(feature_values).all() or not np.isfinite(direct_scores).all():
        raise ValueError("representation replay lacks complete common-target coverage")
    return feature_values, direct_scores


def auc(labels: np.ndarray, scores: np.ndarray) -> float | None:
    labels = np.asarray(labels, dtype=bool)
    scores = np.asarray(scores, dtype=np.float64)
    positive, negative = scores[labels], scores[~labels]
    if not len(positive) or not len(negative):
        return None
    return float(((positive[:, None] > negative).sum() + 0.5 * (positive[:, None] == negative).sum())
                 / (len(positive) * len(negative)))


def metric_set(ratings: np.ndarray, scores: np.ndarray) -> dict[str, Any]:
    ratings = np.asarray(ratings, dtype=np.float64)
    scores = np.asarray(scores, dtype=np.float64)
    high = ratings >= 4
    low = ratings <= 2
    high_auc = auc(high, scores)
    low_auc = auc(low, -scores)
    output: dict[str, Any] = {
        "rows": int(len(ratings)),
        "high_count": int(high.sum()),
        "low_count": int(low.sum()),
        "neutral_count": int(np.sum((ratings > 2) & (ratings < 4))),
        "high_auc": high_auc,
        "low_reversed_auc": low_auc,
        "balanced_auc": (high_auc + low_auc) / 2 if high_auc is not None and low_auc is not None else None,
    }
    for k in (8, 20):
        indexes = np.argsort(-scores, kind="stable")[:min(k, len(scores))]
        discounts = 1 / np.log2(np.arange(2, len(indexes) + 2))
        high_gain = high[indexes].astype(np.float64)
        ideal = np.sort(high.astype(np.float64))[::-1][:len(indexes)]
        ideal_dcg = float(ideal @ discounts)
        output[f"favorite_precision_at_{k}"] = float(high_gain.mean()) if len(indexes) else None
        output[f"disliked_fraction_at_{k}"] = float(low[indexes].mean()) if len(indexes) else None
        output[f"ndcg_at_{k}"] = float(high_gain @ discounts / ideal_dcg) if ideal_dcg else None
    return output


def grouped_bootstrap_delta(
    ratings: np.ndarray,
    baseline: np.ndarray,
    challenger: np.ndarray,
    groups: list[np.ndarray],
    *,
    iterations: int = BOOTSTRAP_REPLICATES,
    seed: int = BOOTSTRAP_SEED,
    minimum_groups: int = 5,
) -> dict[str, Any]:
    if len(groups) < minimum_groups:
        return {"status": "insufficient_groups", "groups": len(groups), "iterations": iterations}
    rng = np.random.default_rng(seed)
    deltas: list[float] = []
    high_deltas: list[float] = []
    low_deltas: list[float] = []
    for _ in range(iterations):
        chosen = rng.integers(0, len(groups), size=len(groups))
        indexes = np.concatenate([groups[int(index)] for index in chosen])
        base_metrics = metric_set(ratings[indexes], baseline[indexes])
        new_metrics = metric_set(ratings[indexes], challenger[indexes])
        if base_metrics["balanced_auc"] is not None and new_metrics["balanced_auc"] is not None:
            deltas.append(new_metrics["balanced_auc"] - base_metrics["balanced_auc"])
        if base_metrics["high_auc"] is not None and new_metrics["high_auc"] is not None:
            high_deltas.append(new_metrics["high_auc"] - base_metrics["high_auc"])
        if base_metrics["low_reversed_auc"] is not None and new_metrics["low_reversed_auc"] is not None:
            low_deltas.append(new_metrics["low_reversed_auc"] - base_metrics["low_reversed_auc"])
    if not deltas:
        return {"status": "no_valid_resamples", "groups": len(groups), "iterations": iterations}
    return {
        "status": "ok", "groups": len(groups), "iterations": iterations,
        "valid_balanced_resamples": len(deltas),
        "balanced_auc_delta_ci95": np.quantile(deltas, [0.025, 0.975]).tolist(),
        "high_auc_delta_ci95": np.quantile(high_deltas, [0.025, 0.975]).tolist() if high_deltas else None,
        "low_auc_delta_ci95": np.quantile(low_deltas, [0.025, 0.975]).tolist() if low_deltas else None,
    }


async def ollama_vectors(texts: list[str], *, model: str, expected_digest: str) -> tuple[np.ndarray, dict[str, Any]]:
    started = time.perf_counter()
    result: list[list[float]] = []
    memory_before: dict[str, int] = {}
    async with httpx.AsyncClient(timeout=240) as client:
        tags = (await client.get("http://127.0.0.1:11434/api/tags")).raise_for_status()
        matches = [row for row in tags.json().get("models", []) if row.get("name") == model]
        if len(matches) != 1 or not str(matches[0].get("digest", "")).startswith(expected_digest[:12]):
            raise ValueError("local Qwen model digest does not match the frozen model")
        version_response = await client.get("http://127.0.0.1:11434/api/version")
        version_response.raise_for_status()
        try:
            ps = await client.get("http://127.0.0.1:11434/api/ps")
            ps.raise_for_status()
            for row in ps.json().get("models", []):
                if row.get("name") == model:
                    memory_before = {key: int(row[key]) for key in ("size", "size_vram", "context_length") if row.get(key) is not None}
        except (httpx.HTTPError, ValueError, TypeError):
            memory_before = {}
        for start in range(0, len(texts), BATCH_SIZE):
            chunk = texts[start:start + BATCH_SIZE]
            response = await client.post(
                "http://127.0.0.1:11434/api/embed",
                json={"model": model, "input": chunk},
            )
            response.raise_for_status()
            values = response.json().get("embeddings")
            if not isinstance(values, list) or len(values) != len(chunk):
                raise ValueError("local Qwen embedding response count differs")
            matrix = np.asarray(values, dtype=np.float32)
            if matrix.ndim != 2 or matrix.shape[1] != 2560 or not np.isfinite(matrix).all():
                raise ValueError("local Qwen embedding dimensions or values are invalid")
            result.extend(matrix.tolist())
            if start + BATCH_SIZE < len(texts):
                await asyncio.sleep(0.1)
        memory_after: dict[str, int] = {}
        try:
            ps = await client.get("http://127.0.0.1:11434/api/ps")
            ps.raise_for_status()
            for row in ps.json().get("models", []):
                if row.get("name") == model:
                    memory_after = {key: int(row[key]) for key in ("size", "size_vram", "context_length") if row.get(key) is not None}
        except (httpx.HTTPError, ValueError, TypeError):
            memory_after = {}
        version = str(version_response.json().get("version") or "unknown")
    return np.asarray(result, dtype=np.float32), {
        "model": model,
        "digest": matches[0]["digest"],
        "ollama_version": version,
        "documents": int(len(texts)),
        "dimensions": 2560,
        "batch_size": BATCH_SIZE,
        "seconds": round(time.perf_counter() - started, 3),
        "vectors_per_second": round(len(texts) / max(time.perf_counter() - started, 1e-6), 3),
        "memory_before_bytes": memory_before,
        "memory_after_bytes": memory_after,
        "max_input_characters": max((len(text) for text in texts), default=0),
        "description_char_cap": 4000,
        "model_max_context_tokens": 32768,
        "effective_runner_context_tokens": (
            memory_after.get("context_length") or memory_before.get("context_length")
        ),
    }


async def fastembed_views(
    title_author_texts: list[str], verified_metadata_texts: list[str], *, cache_dir: Path,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    os.environ["AFTERWORD_MODEL_CACHE"] = str(cache_dir)
    from afterword_engine.embeddings import FastEmbedder
    from fastembed import TextEmbedding
    from importlib.metadata import version
    from tokenizers import Tokenizer
    registry_rows = [row for row in TextEmbedding.list_supported_models() if row.get("model") == BGE_MODEL]
    if version("fastembed") != "0.8.1" or len(registry_rows) != 1:
        raise ValueError("FastEmbed installation or BGE registry differs from the frozen adapter")
    registry = registry_rows[0]
    if (registry.get("sources", {}).get("hf") != BGE_EXPECTED_SOURCE
            or registry.get("model_file") != "model_optimized.onnx"
            or int(registry.get("dim", 0)) != 384
            or float(registry.get("size_in_GB", 0)) > 0.07):
        raise ValueError("FastEmbed BGE artifact does not match the frozen bounded model")
    started = time.perf_counter()
    embedder = FastEmbedder(BGE_MODEL)
    ta = np.asarray(await embedder.embed(title_author_texts), dtype=np.float32)
    metadata = np.asarray(await embedder.embed(verified_metadata_texts), dtype=np.float32)
    if (ta.shape != (len(title_author_texts), 384)
            or metadata.shape != (len(verified_metadata_texts), 384)
            or not np.isfinite(ta).all() or not np.isfinite(metadata).all()):
        raise ValueError("BGE vectors must have 384 finite dimensions")
    cache_bytes = unique_file_bytes(cache_dir)
    if cache_bytes > BGE_MAX_CACHE_BYTES:
        raise ValueError("private FastEmbed cache exceeded the frozen 100 MiB unique-file limit")
    onnx_files = sorted(cache_dir.rglob("model_optimized.onnx"))
    if len(onnx_files) != 1:
        raise ValueError("expected exactly one cached BGE ONNX artifact")
    tokenizer_files = sorted(cache_dir.rglob("tokenizer.json"))
    if len(tokenizer_files) != 1:
        raise ValueError("expected exactly one cached BGE tokenizer")
    tokenizer = Tokenizer.from_file(str(tokenizer_files[0]))

    def token_summary(texts: list[str]) -> dict[str, Any]:
        token_counts = np.fromiter(
            (len(tokenizer.encode(text).ids) for text in texts),
            dtype=np.int32, count=len(texts),
        )
        return {
            "rows": int(len(token_counts)),
            "input_token_limit": BGE_INPUT_TOKEN_LIMIT,
            "rows_over_input_token_limit": int(np.count_nonzero(token_counts > BGE_INPUT_TOKEN_LIMIT)),
            "maximum_input_tokens": int(token_counts.max()) if len(token_counts) else 0,
            "p95_input_tokens": float(np.quantile(token_counts, 0.95)) if len(token_counts) else 0.0,
            "maximum_tokens_are_truncated_by_encoder": bool(np.any(token_counts > BGE_INPUT_TOKEN_LIMIT)),
        }

    artifact = onnx_files[0]
    return ta, metadata, {
        "model": BGE_MODEL,
        "fastembed_version": version("fastembed"),
        "registry": registry,
        "model_file": artifact.name,
        "artifact_sha256": sha256_file(artifact),
        "artifact_bytes": artifact.stat().st_size,
        "cache_bytes": int(cache_bytes),
        "title_author_documents": int(len(title_author_texts)),
        "verified_metadata_documents": int(len(verified_metadata_texts)),
        "dimensions": 384,
        "seconds": round(time.perf_counter() - started, 3),
        "vectors_per_second": round((len(title_author_texts) + len(verified_metadata_texts)) / max(time.perf_counter() - started, 1e-6), 3),
        "title_author_max_input_characters": max((len(text) for text in title_author_texts), default=0),
        "verified_metadata_max_input_characters": max((len(text) for text in verified_metadata_texts), default=0),
        "model_context_limit_tokens": 512,
        "tokenizer_file_sha256": sha256_file(tokenizer_files[0]),
        "title_author_tokenization": token_summary(title_author_texts),
        "verified_metadata_tokenization": token_summary(verified_metadata_texts),
    }


def fastembed_reuse_views(
    title_author_texts: list[str],
    verified_metadata_texts: list[str],
    *,
    old_title_author_texts: list[str],
    old_verified_metadata_texts: list[str],
    cached_title_author: np.ndarray,
    cached_verified_metadata: np.ndarray,
    cache_dir: Path,
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """Reuse exact v1 BGE rows and encode only changed strict-v2 metadata rows."""
    os.environ["AFTERWORD_MODEL_CACHE"] = str(cache_dir)
    from afterword_engine.embeddings import FastEmbedder
    from fastembed import TextEmbedding
    from importlib.metadata import version
    from tokenizers import Tokenizer
    registry_rows = [row for row in TextEmbedding.list_supported_models() if row.get("model") == BGE_MODEL]
    if version("fastembed") != "0.8.1" or len(registry_rows) != 1:
        raise ValueError("FastEmbed installation or BGE registry differs from the frozen adapter")
    registry = registry_rows[0]
    if (registry.get("sources", {}).get("hf") != BGE_EXPECTED_SOURCE
            or registry.get("model_file") != "model_optimized.onnx"
            or int(registry.get("dim", 0)) != 384
            or float(registry.get("size_in_GB", 0)) > 0.07):
        raise ValueError("FastEmbed BGE artifact does not match the frozen bounded model")
    started = time.perf_counter()
    embedder = FastEmbedder(BGE_MODEL)
    encoded_counts: dict[str, int] = {}

    def encode(texts: list[str]) -> np.ndarray:
        return np.asarray(asyncio.run(embedder.embed(texts)), dtype=np.float32)

    title_author, ta_reuse = _reuse_cached_vectors(
        title_author_texts, old_title_author_texts, cached_title_author,
        dimensions=384, encoder=encode,
    )
    verified_metadata, metadata_reuse = _reuse_cached_vectors(
        verified_metadata_texts, old_verified_metadata_texts, cached_verified_metadata,
        dimensions=384, encoder=encode,
    )
    cache_bytes = unique_file_bytes(cache_dir)
    if cache_bytes > BGE_MAX_CACHE_BYTES:
        raise ValueError("private FastEmbed cache exceeded the frozen 100 MiB unique-file limit")
    onnx_files = sorted(cache_dir.rglob("model_optimized.onnx"))
    if len(onnx_files) != 1:
        raise ValueError("expected exactly one cached BGE ONNX artifact")
    tokenizer_files = sorted(cache_dir.rglob("tokenizer.json"))
    if len(tokenizer_files) != 1:
        raise ValueError("expected exactly one cached BGE tokenizer")
    tokenizer = Tokenizer.from_file(str(tokenizer_files[0]))

    def token_summary(texts: list[str]) -> dict[str, Any]:
        token_counts = np.fromiter(
            (len(tokenizer.encode(text).ids) for text in texts), dtype=np.int32, count=len(texts)
        )
        return {
            "rows": int(len(token_counts)),
            "input_token_limit": BGE_INPUT_TOKEN_LIMIT,
            "rows_over_input_token_limit": int(np.count_nonzero(token_counts > BGE_INPUT_TOKEN_LIMIT)),
            "maximum_input_tokens": int(token_counts.max()) if len(token_counts) else 0,
            "p95_input_tokens": float(np.quantile(token_counts, 0.95)) if len(token_counts) else 0.0,
            "maximum_tokens_are_truncated_by_encoder": bool(np.any(token_counts > BGE_INPUT_TOKEN_LIMIT)),
        }

    artifact = onnx_files[0]
    total_documents = len(title_author_texts) + len(verified_metadata_texts)
    encoded_documents = ta_reuse["freshly_encoded_rows"] + metadata_reuse["freshly_encoded_rows"]
    elapsed = max(time.perf_counter() - started, 1e-6)
    return title_author, verified_metadata, {
        "model": BGE_MODEL,
        "fastembed_version": version("fastembed"),
        "registry": registry,
        "model_file": artifact.name,
        "artifact_sha256": sha256_file(artifact),
        "artifact_bytes": artifact.stat().st_size,
        "cache_bytes": int(cache_bytes),
        "title_author_documents": int(len(title_author_texts)),
        "verified_metadata_documents": int(len(verified_metadata_texts)),
        "dimensions": 384,
        "seconds": round(elapsed, 3),
        "vectors_per_second": round(encoded_documents / elapsed, 3),
        "title_author_max_input_characters": max((len(text) for text in title_author_texts), default=0),
        "verified_metadata_max_input_characters": max((len(text) for text in verified_metadata_texts), default=0),
        "model_context_limit_tokens": BGE_INPUT_TOKEN_LIMIT,
        "tokenizer_file_sha256": sha256_file(tokenizer_files[0]),
        "title_author_tokenization": token_summary(title_author_texts),
        "verified_metadata_tokenization": token_summary(verified_metadata_texts),
        "vector_reuse": {
            "title_author": ta_reuse,
            "verified_metadata": metadata_reuse,
            "total_documents": total_documents,
            "total_freshly_encoded_rows": int(encoded_documents),
        },
    }


def _fit_fold_predictions(
    current_scores: np.ndarray,
    arm_feature_folds: dict[str, list[np.ndarray]],
    ratings: np.ndarray,
    folds: list[dict[str, int]],
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray]]:
    current_scores = np.asarray(current_scores, dtype=np.float64)
    ratings = np.asarray(ratings, dtype=np.float64)
    n = len(ratings)
    if current_scores.shape != (n,) or not np.isfinite(current_scores).all():
        raise ValueError("current scores are not a complete aligned vector")
    base_predictions = np.full(n, np.nan, dtype=np.float64)
    rep_predictions = {arm: np.full(n, np.nan, dtype=np.float64) for arm in arm_feature_folds}
    complement_predictions = {arm: np.full(n, np.nan, dtype=np.float64) for arm in arm_feature_folds}
    for fold_index, fold in enumerate(folds):
        train_stop = int(fold["train_stop"])
        begin = int(fold["evaluation_start"])
        stop = int(fold["evaluation_stop"])
        if not (0 < train_stop == begin < stop <= n):
            raise ValueError("causal fold bounds are invalid")
        base_predictions[begin:stop] = fit_ridge_predictions(
            current_scores[:train_stop, None], ratings[:train_stop], current_scores[begin:stop, None]
        )
        for arm, feature_views in arm_feature_folds.items():
            if len(feature_views) != len(folds):
                raise ValueError(f"representation fold count differs: {arm}")
            features = np.asarray(feature_views[fold_index], dtype=np.float64)
            if (features.shape != (n, len(BASE_NAMES))
                    or not np.isfinite(features[:stop]).all()):
                raise ValueError(f"representation features are not aligned within fold: {arm}")
            rep_predictions[arm][begin:stop] = fit_ridge_predictions(
                features[:train_stop], ratings[:train_stop], features[begin:stop]
            )
            joined_train = np.column_stack((current_scores[:train_stop], features[:train_stop]))
            joined_eval = np.column_stack((current_scores[begin:stop], features[begin:stop]))
            complement_predictions[arm][begin:stop] = fit_ridge_predictions(
                joined_train, ratings[:train_stop], joined_eval
            )
    return {"current": base_predictions, **rep_predictions}, complement_predictions


def _fold_metrics(ratings: np.ndarray, score: np.ndarray, folds: list[dict[str, int]], *, train_stop: int) -> list[dict]:
    result = []
    for index, fold in enumerate(folds):
        if int(fold["evaluation_start"]) >= train_stop:
            continue
        start, stop = int(fold["evaluation_start"]), int(fold["evaluation_stop"])
        result.append({"fold": index + 1, **metric_set(ratings[start:stop], score[start:stop])})
    return result


def _days_to_groups(day_values: np.ndarray) -> list[np.ndarray]:
    mapping: dict[int, list[int]] = defaultdict(list)
    for index, value in enumerate(day_values):
        mapping[int(value)].append(index)
    return [np.asarray(mapping[key], dtype=np.int64) for key in sorted(mapping)]


def _groups_to_indices(values: np.ndarray) -> list[np.ndarray]:
    mapping: dict[int, list[int]] = defaultdict(list)
    for index, value in enumerate(np.asarray(values, dtype=np.int64)):
        mapping[int(value)].append(index)
    return [np.asarray(mapping[key], dtype=np.int64) for key in sorted(mapping)]


def _thirty_day_groups(day_values: np.ndarray) -> list[np.ndarray]:
    days = np.asarray(day_values, dtype=np.int64)
    if not len(days):
        return []
    origin = int(days.min())
    buckets: dict[int, list[int]] = defaultdict(list)
    for index, value in enumerate(days):
        buckets[(int(value) - origin) // 30].append(index)
    return [np.asarray(buckets[key], dtype=np.int64) for key in sorted(buckets)]


def development_oof_mask(train_mask: np.ndarray, *, first_oof_start: int) -> np.ndarray:
    """Keep development OOF rows inside the development partition only."""
    train_mask = np.asarray(train_mask, dtype=bool)
    if train_mask.ndim != 1 or not 0 <= first_oof_start <= len(train_mask):
        raise ValueError("development mask or first OOF boundary is invalid")
    indexes = np.arange(len(train_mask))
    return train_mask & (indexes >= first_oof_start)


def capped_residual_predictions(
    current: np.ndarray,
    ratings: np.ndarray,
    features_by_fold: list[np.ndarray],
    folds: list[dict[str, int]],
) -> np.ndarray:
    """Causal current-score residual with the existing 5-point/25%-SD cap."""
    current = np.asarray(current, dtype=np.float64)
    ratings = np.asarray(ratings, dtype=np.float64)
    if (current.ndim != 1 or ratings.shape != current.shape or len(features_by_fold) != len(folds)
            or not np.isfinite(current).all()
            or not np.isfinite(ratings).all()):
        raise ValueError("capped residual inputs must be row-aligned and finite")
    output = current.copy()  # The first development fit prefix has no OOF residual.
    for fold, features in zip(folds, features_by_fold):
        features = np.asarray(features, dtype=np.float64)
        if features.ndim != 2 or features.shape[0] != len(current):
            raise ValueError("selected residual features must align with common targets")
        train_stop = int(fold["train_stop"])
        begin = int(fold["evaluation_start"])
        stop = int(fold["evaluation_stop"])
        train_x = features[:train_stop]
        eval_x = features[begin:stop]
        if not np.isfinite(train_x).all() or not np.isfinite(eval_x).all():
            raise ValueError("selected residual features contain non-finite fold values")
        x_mean = train_x.mean(axis=0)
        x_scale = train_x.std(axis=0)
        x_scale = np.where(x_scale < 1e-8, 1.0, x_scale)
        z_train = (train_x - x_mean) / x_scale
        z_eval = (eval_x - x_mean) / x_scale

        score_mean = float(current[:train_stop].mean())
        score_scale = float(current[:train_stop].std())
        if score_scale < 1e-8:
            score_scale = 1.0
        score_train = ((current[:train_stop] - score_mean) / score_scale)[:, None]
        calibration = Ridge(alpha=RATING_RIDGE_ALPHA).fit(score_train, ratings[:train_stop])
        residual = ratings[:train_stop] - calibration.predict(score_train)
        residual_model = Ridge(alpha=RATING_RIDGE_ALPHA).fit(z_train, residual)
        correction = 25.0 * residual_model.predict(z_eval)
        cap = min(5.0, 0.25 * float(current[:train_stop].std()))
        correction = np.clip(correction, -cap, cap)
        output[begin:stop] = np.round(np.clip(current[begin:stop] + correction, 0.0, 100.0), 1)
    if not np.isfinite(output).all():
        raise ValueError("capped residual output is incomplete")
    return output


def history_components_from_similarities(
    history: list[dict],
    query: list[dict],
    similarities: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Apply the production history components to a precomputed cosine matrix."""
    sims = np.asarray(similarities, dtype=np.float32)
    if sims.shape != (len(query), len(history)) or not np.isfinite(sims).all():
        raise ValueError("cosine matrix does not align with history and query rows")
    ratings = np.asarray([float(item.get("rating") or 0) for item in history], dtype=np.float64)
    days = np.asarray([
        _read_day(item.get("read_at")).toordinal() if _read_day(item.get("read_at")) else np.nan
        for item in history
    ], dtype=np.float64)
    positive = ratings >= 4
    negative = (ratings > 0) & (ratings <= 2)
    global_mean = float(ratings.mean()) if len(ratings) else 3.0
    author_ratings: dict[str, list[float]] = defaultdict(list)
    from afterword_engine.identity import book_identity
    for item, rating in zip(history, ratings):
        author_ratings[book_identity("", item.get("author", ""))].append(float(rating))
    X = np.zeros((len(query), len(BASE_NAMES)), dtype=np.float64)
    scores = np.zeros(len(query), dtype=np.float64)
    for row, candidate in enumerate(query):
        sim = sims[row].astype(np.float64)
        pos, neg = sim[positive], sim[negative]
        pos_top = float(_top_weighted(pos[None, :], default=.25)[0]) if len(pos) else .25
        pos_max = float(np.max(pos)) if len(pos) else .25
        neg_max = float(np.max(neg)) if len(neg) else 0.0
        neg_top = float(_top_weighted(neg[None, :], default=0.0)[0]) if len(neg) else 0.0
        if len(sim) and np.any(sim > 0):
            neighbors = np.argsort(-sim, kind="stable")[:min(_KERNEL_NEIGHBORS, len(sim))]
            weights = (np.maximum(sim[neighbors], 0.0) + 1e-8) ** _KERNEL_POWER
            kernel = float(np.dot(weights, ratings[neighbors]) / weights.sum())
            ess = float(weights.sum() ** 2 / np.dot(weights, weights))
            neighbor_days = days[neighbors]
            known = np.isfinite(neighbor_days)
            if known.any():
                ages = np.maximum(0.0, (neighbor_days[known].max() - neighbor_days) / 365.25)
                ages[~known] = np.median(ages[known])
                recent_weights = weights * np.exp(-ages / _RECENCY_TIMESCALE_YEARS)
                recent = float(np.dot(recent_weights, ratings[neighbors]) / recent_weights.sum())
            else:
                recent = kernel
            local = neighbors[:min(5, len(neighbors))]
            local_weights = np.maximum(sim[local], 0.0) + 1e-4
            local_rating = float(np.dot(local_weights, ratings[local]) / local_weights.sum())
        else:
            kernel = recent = global_mean
            local_rating = 3.0
            ess = 0.0
        from afterword_engine.identity import book_identity
        values = author_ratings.get(book_identity("", candidate.get("author", "")), [])
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
                     + kernel_strength * (kernel - global_mean) * ess / (ess + _KERNEL_ESS_PRIOR)
                     + recency_strength * (recent - kernel)
                     + (float(candidate.get("source_weight") or 1.0) - 1.0) * 5.0)
        scores[row] = np.clip(50.0 + _metadata_confidence(candidate) * (raw_score - 50.0), 0.0, 100.0)
    return X, scores


def _unfamiliar_mask(author_groups: np.ndarray, train_stop: int, stop: int) -> np.ndarray:
    values = np.asarray(author_groups, dtype=np.int64)
    known = set(map(int, values[:train_stop]))
    return np.asarray([int(author) not in known for author in values[train_stop:stop]], dtype=bool)


def build_experiment(
    *,
    corpus_path: Path,
    feature_path: Path,
    feature_manifest_path: Path,
    protocol_path: Path,
    strict_amendment_path: Path,
    method_amendment_path: Path,
    provenance_supplement_path: Path,
    prior_vectors_path: Path,
    prior_aggregate_path: Path,
    fresh_corpus_path: Path,
    fresh_vectors_path: Path,
    fresh_manifest_path: Path,
    private_output: Path,
    bge_cache_dir: Path,
) -> dict[str, Any]:
    """Run strict-v2 only; the identity-only view is reserved for cache matching.

    The public CLI requires the strict protocol/method amendments and provenance
    supplement. The internal identity-only text view below is used solely to
    identify unchanged v1 cache rows and never supplies scored metadata.
    """
    for path in (corpus_path, feature_path, feature_manifest_path, protocol_path,
                 strict_amendment_path, method_amendment_path, provenance_supplement_path, prior_vectors_path,
                 prior_aggregate_path,
                 fresh_corpus_path, fresh_vectors_path, fresh_manifest_path):
        if not path.is_file():
            raise FileNotFoundError(path)
    protocol = json.loads(protocol_path.read_text())
    if protocol.get("experiment") != "embedding-representation-track2-v1":
        raise ValueError("unexpected frozen representation protocol")
    if tuple(protocol.get("selection_order", ())) != ARM_ORDER:
        raise ValueError("frozen v1 arm order differs from the evaluator")
    strict_amendment = json.loads(strict_amendment_path.read_text())
    if (strict_amendment.get("experiment") != "embedding-representation-track2-strict-v2"
            or strict_amendment.get("supersedes_protocol_sha256") != sha256_file(protocol_path)
            or strict_amendment.get("frozen_inputs", {}).get("provenance_supplement_sha256")
            != sha256_file(provenance_supplement_path)
            or strict_amendment.get("metadata_method_amendment", {}).get("sha256")
            != sha256_file(method_amendment_path)):
        raise ValueError("strict-v2 amendment does not match the frozen base protocol and provenance supplement")
    import evaluate_metric_facets as grounded
    if grounded._method_amendment_sha256(method_amendment_path) != sha256_file(method_amendment_path):
        raise ValueError("metadata method amendment failed its frozen verifier gate")
    method_amendment = json.loads(method_amendment_path.read_text())
    method_inputs = method_amendment.get("input_artifacts", {})
    verifier_source_sha256 = hashlib.sha256(
        inspect.getsource(grounded._verified_field_provenance).encode("utf-8")
    ).hexdigest()
    if (grounded.METADATA_GENRE_LIMIT != 12 or grounded.FACET_SUBJECT_FEATURE_LIMIT != 8
            or verifier_source_sha256 != strict_amendment["metadata_method_amendment"].get("verifier_function_sha256")
            or method_inputs.get("corpus-private.json") != sha256_file(corpus_path)
            or method_inputs.get("features-private.npz") != sha256_file(feature_path)
            or method_inputs.get("provenance-supplement-private.json") != sha256_file(provenance_supplement_path)):
        raise ValueError("metadata method amendment does not bind the frozen inputs and 12/8 genre limits")
    if sha256_file(corpus_path) != protocol["input_artifacts"]["corpus_sha256"]:
        raise ValueError("corpus hash differs from the frozen protocol")
    if sha256_file(feature_path) != protocol["input_artifacts"]["causal_features_sha256"]:
        raise ValueError("causal feature artifact differs from the frozen protocol")
    if sha256_file(feature_manifest_path) != protocol["input_artifacts"]["causal_manifest_sha256"]:
        raise ValueError("causal manifest differs from the frozen protocol")

    private_output.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(private_output, 0o700)
    bge_cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(bge_cache_dir, 0o700)
    data = load_corpus(corpus_path)
    records, selected, excluded = prepare(data, "ollama", QWEN_MODEL)
    features_manifest = json.loads(feature_manifest_path.read_text())
    folds = features_manifest.get("development_folds", [])
    expected_folds = frozen_development_folds(
        np.asarray([row[0].date().toordinal() for row in records[100:]], dtype=np.int64),
        int(features_manifest["split_counts"]["development"]),
    )
    if folds != expected_folds:
        raise ValueError("common causal folds differ from the frozen manifest")
    if strict_amendment.get("unchanged_terms", {}).get("development_folds") != folds:
        raise ValueError("strict-v2 amendment does not preserve the frozen development folds")
    if strict_amendment.get("unchanged_terms", {}).get("arms") != list(ARM_ORDER):
        raise ValueError("strict-v2 amendment does not preserve the original eight arms")

    with provenance_supplement_path.open() as stream:
        provenance_supplement = json.load(stream)
    prior_text_data = build_text_views(data, records)
    text_data = build_text_views(data, records, provenance_supplement=provenance_supplement)
    if text_data["metadata_verification_mode"] != "field_provenance":
        raise ValueError("strict-v2 run did not activate field provenance verification")
    items = text_data["items"]
    all_ids = text_data["read_ids"]
    target_ids = all_ids[100:]
    with np.load(feature_path, allow_pickle=False) as archived:
        required = {"read_ids", "record_indexes", "ratings", "utc_day", "author_group", "current",
                    "train_mask", "validation_mask", "later_mask", "oof_selection_mask"}
        if not required.issubset(archived.files):
            raise ValueError("causal feature archive is missing required arrays")
        feature_ids = np.asarray(archived["read_ids"], dtype=np.int64)
        record_indexes = np.asarray(archived["record_indexes"], dtype=np.int64)
        ratings = np.asarray(archived["ratings"], dtype=np.float64)
        day_values = np.asarray(archived["utc_day"], dtype=np.int64)
        author_groups = np.asarray(archived["author_group"], dtype=np.int64)
        current = np.asarray(archived["current"], dtype=np.float64)
        train_mask = np.asarray(archived["train_mask"], dtype=bool)
        validation_mask = np.asarray(archived["validation_mask"], dtype=bool)
        later_mask = np.asarray(archived["later_mask"], dtype=bool)
        oof_selection_mask = np.asarray(archived["oof_selection_mask"], dtype=bool)
    if not np.array_equal(feature_ids, target_ids):
        raise ValueError("representation target IDs do not align with frozen common cohort")
    if len(target_ids) != 1677 or int(train_mask.sum()) != 966 or int(validation_mask.sum()) != 355 or int(later_mask.sum()) != 356:
        raise ValueError("common cohort counts differ from frozen contract")
    if not np.array_equal(train_mask, np.arange(len(target_ids)) < 966):
        raise ValueError("development target mask differs from frozen folds")
    if not np.array_equal(oof_selection_mask, development_oof_mask(
            train_mask, first_oof_start=int(folds[0]["evaluation_start"]))):
        raise ValueError("frozen OOF-selection mask differs from the representation fold contract")
    if not np.array_equal(feature_ids, np.asarray([int(records[index][1]["id"]) for index in record_indexes])):
        raise ValueError("frozen record indexes do not resolve to the common target IDs")

    if (not np.array_equal(day_values, np.asarray([row[0].date().toordinal() for row in records[100:]], dtype=np.int64))
            or not np.array_equal(ratings, np.asarray([float(item.get("rating")) for item in items[100:]], dtype=np.float64))):
        raise ValueError("causal feature dates or ratings do not align with the common cohort")

    # Reproduce the deployed current scorer from its cached Qwen vectors before
    # generating any alternative representation. The pre-existing feature
    # artifact must match exactly at the served one-decimal precision.
    cached_vectors = np.stack([row[2] for row in records]).astype(np.float32, copy=False)
    _, cached_score = history_features(records, cached_vectors, cached_vectors[100:])
    current_parity = {
        "targets": int(len(current)),
        "mismatches_at_served_precision": int(np.count_nonzero(cached_score != current)),
        "max_absolute_score_delta": float(np.max(np.abs(cached_score - current))),
        "mean_absolute_score_delta": float(np.mean(np.abs(cached_score - current))),
    }
    if current_parity["mismatches_at_served_precision"]:
        raise ValueError("frozen current Qwen scorer does not match the serving replay")
    if features_manifest.get("exact_ranker_parity", {}).get("mismatches_at_served_precision") != 0:
        raise ValueError("authoritative feature manifest lacks exact production scorer parity")

    prior_vectors, prior_aggregate = _validate_prior_vector_archive(
        prior_vectors_path=prior_vectors_path,
        prior_aggregate_path=prior_aggregate_path,
        base_protocol_path=protocol_path,
        prepared_read_ids=all_ids,
        target_read_ids=target_ids,
        bge_cache_dir=bge_cache_dir,
    )

    # Check the prior fresh Qwen control's exact text/model lineage against the
    # current corpus. The frozen v1 study vectors are reusable only for rows
    # whose exact v1 and strict-v2 input text hashes still match.
    dummy, qwen_provenance = asyncio.run(ollama_vectors(["Bookward model identity check."], model=QWEN_MODEL,
                                                        expected_digest=QWEN_DIGEST))
    if dummy.shape != (1, 2560):
        raise ValueError("Qwen model identity probe returned invalid dimensions")
    if qwen_provenance.get("effective_runner_context_tokens") != strict_amendment["unchanged_terms"]["qwen_runner_context_tokens"]:
        raise ValueError("local Qwen context differs from the frozen v1 execution context")
    del dummy
    qwen_reference_by_id = validate_fresh_qwen_vectors(
        data, fresh_corpus_path, fresh_vectors_path, fresh_manifest_path, qwen_provenance["digest"]
    )
    qwen_reference = np.stack([qwen_reference_by_id[int(read_id)] for read_id in all_ids])

    def qwen_reuse(new_texts: list[str], old_texts: list[str], cached: np.ndarray):
        fresh_cost: dict[str, Any] = {}

        def encode(changed_texts: list[str]) -> np.ndarray:
            vectors, cost = asyncio.run(ollama_vectors(
                changed_texts, model=QWEN_MODEL, expected_digest=QWEN_DIGEST))
            fresh_cost.update(cost)
            return vectors

        vectors, reuse = _reuse_cached_vectors(
            new_texts, old_texts, cached, dimensions=2560, encoder=encode,
        )
        fresh_cost.setdefault("model", QWEN_MODEL)
        fresh_cost.setdefault("digest", QWEN_DIGEST)
        fresh_cost.setdefault("dimensions", 2560)
        fresh_cost.setdefault("documents", 0)
        fresh_cost["vector_reuse"] = {**reuse, "model": QWEN_MODEL, "digest": QWEN_DIGEST}
        return vectors, fresh_cost

    ta_texts = text_data["title_author_text"]
    strict_meta_texts = text_data["verified_metadata_text"]
    v1_meta_texts = prior_text_data["verified_metadata_text"]
    ta_queries = [instructed_query(text) for text in ta_texts[100:]]
    strict_meta_queries = [instructed_query(text) for text in strict_meta_texts[100:]]
    v1_meta_queries = [instructed_query(text) for text in v1_meta_texts[100:]]
    qwen_doc_ta, qwen_ta_cost = qwen_reuse(
        ta_texts, prior_text_data["title_author_text"], prior_vectors["qwen_document_title_author"])
    qwen_doc_meta, qwen_meta_cost = qwen_reuse(
        strict_meta_texts, v1_meta_texts, prior_vectors["qwen_document_verified_metadata"])
    qwen_query_instructed_ta, qwen_query_ta_cost = qwen_reuse(
        ta_queries, [instructed_query(text) for text in prior_text_data["title_author_text"][100:]],
        prior_vectors["qwen_query_instructed_title_author"])
    qwen_query_instructed_meta, qwen_query_meta_cost = qwen_reuse(
        strict_meta_queries, v1_meta_queries, prior_vectors["qwen_query_instructed_verified_metadata"])
    fresh_ta_cosine = np.sum(unit_rows(qwen_doc_ta) * unit_rows(qwen_reference), axis=1)
    fresh_qwen_execution_drift = {
        "reference_rows": int(len(fresh_ta_cosine)),
        "cosine_mean": float(np.mean(fresh_ta_cosine)),
        "cosine_p01": float(np.quantile(fresh_ta_cosine, 0.01)),
        "cosine_p05": float(np.quantile(fresh_ta_cosine, 0.05)),
        "cosine_min": float(np.min(fresh_ta_cosine)),
        "cosine_max": float(np.max(fresh_ta_cosine)),
    }
    _, qwen_fresh_direct_ta = history_features(records, qwen_doc_ta, qwen_doc_ta[100:])
    fresh_qwen_execution_drift["served_score_mismatches_vs_cached_current"] = int(
        np.count_nonzero(qwen_fresh_direct_ta != current)
    )
    fresh_qwen_execution_drift["served_score_max_absolute_delta"] = float(
        np.max(np.abs(qwen_fresh_direct_ta - current))
    )

    # Reuse the exact in-scope production adapter with a private, bounded cache.
    bge_model_before = unique_file_bytes(bge_cache_dir)
    if bge_model_before > BGE_MAX_CACHE_BYTES:
        raise ValueError("private BGE model cache was already above its registered size limit")
    bge_doc_ta, bge_doc_meta, bge_cost = fastembed_reuse_views(
        ta_texts,
        strict_meta_texts,
        old_title_author_texts=prior_text_data["title_author_text"],
        old_verified_metadata_texts=v1_meta_texts,
        cached_title_author=prior_vectors["bge_document_title_author"],
        cached_verified_metadata=prior_vectors["bge_document_verified_metadata"],
        cache_dir=bge_cache_dir,
    )

    complete_folds = folds + [
        {"train_stop": 966, "evaluation_start": 966, "evaluation_stop": 1321},
        {"train_stop": 1321, "evaluation_start": 1321, "evaluation_stop": 1677},
    ]
    # TF-IDF vocabulary and IDF are refit inside each causal fold. Its sparse
    # vectors are never serialized into the repository or fitted on later text.
    direct_scores: dict[str, np.ndarray] = {}
    arm_feature_folds: dict[str, list[np.ndarray]] = {}
    fold_tfidf: dict[str, list[dict[str, Any]]] = {view: [] for view in TEXT_VIEWS}

    # Fixed dense-vector representations use the same complete common cohort.
    dense_arm_vectors = {
        "qwen_current_title_author": (qwen_doc_ta, qwen_doc_ta[100:]),
        "qwen_current_verified_metadata": (qwen_doc_meta, qwen_doc_meta[100:]),
        "qwen_instructed_title_author": (qwen_doc_ta, qwen_query_instructed_ta),
        "qwen_instructed_verified_metadata": (qwen_doc_meta, qwen_query_instructed_meta),
        "bge_small_title_author": (bge_doc_ta, bge_doc_ta[100:]),
        "bge_small_verified_metadata": (bge_doc_meta, bge_doc_meta[100:]),
    }
    for arm, (history_vectors, query_vectors) in dense_arm_vectors.items():
        arm_features, arm_direct = history_features(records, history_vectors, query_vectors)
        arm_feature_folds[arm] = [arm_features.copy() for _ in complete_folds]
        direct_scores[arm] = arm_direct

    target_texts = {
        "title_author": text_data["title_author_text"],
        "verified_metadata": text_data["verified_metadata_text"],
    }
    target_offset = 100
    for view in TEXT_VIEWS:
        all_text = target_texts[view]
        arm = f"tfidf_{view}"
        arm_feature_folds[arm] = []
        direct_scores[arm] = np.full(len(target_ids), np.nan, dtype=np.float64)
        # Score the first development fit prefix with a vectorizer fit only on
        # the 100 warm-up reads, so this complete direct export remains causal.
        warmup_matrix, warmup_info = tfidf_vectors(all_text[:target_offset], all_text)
        warmup_direct_stop = int(complete_folds[0]["evaluation_start"])
        warmup_records = records[:target_offset + warmup_direct_stop]
        _, warmup_scores = history_features_sparse(
            warmup_records,
            warmup_matrix[:len(warmup_records)],
            warmup_matrix[target_offset:target_offset + warmup_direct_stop],
            target_start=target_offset,
        )
        direct_scores[arm][:warmup_direct_stop] = warmup_scores
        fold_tfidf[view].append({"fold": "warmup_direct_diagnostic", **warmup_info})
        for fold_index, fold in enumerate(complete_folds):
            train_stop = int(fold["train_stop"])
            eval_start = int(fold["evaluation_start"])
            eval_stop = int(fold["evaluation_stop"])
            fit_stop_in_records = target_offset + train_stop
            matrix, tfidf_info = tfidf_vectors(
                all_text[:fit_stop_in_records], all_text
            )
            # Every target in this fold uses a vocabulary/IDF fitted on the
            # earlier prefix only. Feature construction still uses strictly
            # prior-day history under the common 100-read warm-up.
            fold_records = records[:eval_stop + target_offset]
            history_vectors = matrix[:len(fold_records)]
            query_vectors = matrix[target_offset:eval_stop + target_offset]
            fold_features, fold_direct = history_features_sparse(
                fold_records, history_vectors, query_vectors, target_start=target_offset
            )
            fold_feature_values = np.full((len(target_ids), len(BASE_NAMES)), np.nan, dtype=np.float64)
            fold_feature_values[:eval_stop] = fold_features[:eval_stop]
            arm_feature_folds[arm].append(fold_feature_values)
            direct_scores[arm][eval_start:eval_stop] = fold_direct[eval_start:eval_stop]
            fold_tfidf[view].append({"fold": fold_index + 1, **tfidf_info})

    # All feature arrays from the fixed encoders are now target aligned. Build
    # direct-score diagnostics and the identical Ridge learners for each arm.
    if any(not np.isfinite(values[300:]).all() for values in direct_scores.values()):
        raise ValueError("one or more direct representation scores are incomplete")
    all_folds = complete_folds
    ridge_results, complement_results = _fit_fold_predictions(
        current, arm_feature_folds, ratings, all_folds
    )

    # Select exactly one fixed challenger from development OOF results. The
    # held-out historical regions remain exploratory and cannot affect choice.
    train_stop = int(train_mask.sum())
    fold_summary: dict[str, dict[str, Any]] = {}
    for arm in ARM_ORDER:
        if arm not in complement_results:
            continue
        arm_folds = _fold_metrics(ratings, complement_results[arm], folds, train_stop=train_stop)
        base_folds = _fold_metrics(ratings, ridge_results["current"], folds, train_stop=train_stop)
        delta_values = [row["balanced_auc"] - base["balanced_auc"]
                        for row, base in zip(arm_folds, base_folds)
                        if row.get("balanced_auc") is not None and base.get("balanced_auc") is not None]
        fold_summary[arm] = {
            "complement_folds": arm_folds,
            "current_score_only_folds": base_folds,
            "mean_development_balanced_auc_delta": float(np.mean(delta_values)) if delta_values else None,
            "eligible_folds": int(len(delta_values)),
        }
    available_order = [arm for arm in ARM_ORDER if arm in fold_summary]
    if not available_order:
        raise ValueError("no representation challenger was available for fixed selection")
    selected_arm = max(
        available_order,
        key=lambda arm: (
            fold_summary[arm]["mean_development_balanced_auc_delta"]
            if fold_summary[arm]["mean_development_balanced_auc_delta"] is not None else -float("inf"),
            -ARM_ORDER.index(arm),
        ),
    )
    selected_view = selected_arm.split("_", 2)[2]
    selected_rep = selected_arm.rsplit("_", 2)[0]
    selected_metadata_mask = text_data["metadata_mask"][100:]
    selected_capped_residual = capped_residual_predictions(
        current, ratings, arm_feature_folds[selected_arm], complete_folds
    )

    # Compute diagnostic slices only on development OOF, plus descriptive
    # validation and later regions. No labels outside development choose an arm.
    dev_oof_mask = development_oof_mask(
        train_mask, first_oof_start=int(folds[0]["evaluation_start"])
    )
    partition_masks = {
        "development_oof": dev_oof_mask,
        "validation_exploratory": validation_mask,
        "later_exploratory": later_mask,
        "metadata_present_development_oof": selected_metadata_mask & dev_oof_mask,
        "metadata_fallback_development_oof": ~selected_metadata_mask & dev_oof_mask,
    }
    aggregate: dict[str, Any] = {
        "version": 2,
        "experiment": "embedding-representation-track2-strict-v2",
        "base_frozen_protocol_sha256": sha256_file(protocol_path),
        "strict_v2_amendment_sha256": sha256_file(strict_amendment_path),
        "metadata_method_amendment_sha256": sha256_file(method_amendment_path),
        "metadata_method_module_sha256": sha256_file(SCRIPTS / "evaluate_metric_facets.py"),
        "metadata_verifier_function_sha256": hashlib.sha256(
            inspect.getsource(grounded._verified_field_provenance).encode("utf-8")
        ).hexdigest(),
        "provenance_supplement_sha256": sha256_file(provenance_supplement_path),
        "protocol_amendments": {
            "bge_private_download": sha256_file(protocol_path.parent / "track2-protocol-amendment-bge-private.json"),
            "integration_score_exports": sha256_file(protocol_path.parent / "track2-amendment-integration-private.json"),
            "strict_metadata_v2": sha256_file(strict_amendment_path),
            "metadata_method_correction": sha256_file(method_amendment_path),
        },
        "corpus_sha256": sha256_file(corpus_path),
        "feature_artifact_sha256": sha256_file(feature_path),
        "prepared_reads": len(records),
        "common_targets": len(target_ids),
        "excluded": excluded,
        "current_qwen_parity": current_parity,
        "metadata_coverage": {
            "verification_mode": text_data["metadata_verification_mode"],
            "prepared_read_rows_with_verified_metadata": int(text_data["metadata_mask"].sum()),
            "eligible_targets_with_verified_metadata": int(selected_metadata_mask.sum()),
            "eligible_targets_fallback_to_title_author": int((~selected_metadata_mask).sum()),
            "invalid_metadata_rows_rejected": int(text_data["invalid_metadata_rows"]),
            "verified_metadata_rows": int(text_data["verified_metadata_rows"]),
            "fields_and_description_kind_counts": text_data["metadata_sources"],
        },
        "runtime": {
            "qwen_model_identity_probe": qwen_provenance,
            "qwen_current_title_author": qwen_ta_cost,
            "qwen_current_verified_metadata": qwen_meta_cost,
            "qwen_instructed_title_author_queries": qwen_query_ta_cost,
            "qwen_instructed_verified_metadata_queries": qwen_query_meta_cost,
            "bge_small_both_text_views": bge_cost,
            "peak_process_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 2),
        },
        "tfidf_fold_fits": fold_tfidf,
        "selection_rule": "Largest mean balanced-AUC delta across the three fixed development folds for Ridge(current score + representation features) versus Ridge(current score only); original fixed arm order breaks ties. Validation/later labels never selected or tuned.",
        "selected_arm": selected_arm,
        "selected_representation": selected_rep,
        "selected_text_view": selected_view,
        "selection_delta_mean_balanced_auc": fold_summary[selected_arm]["mean_development_balanced_auc_delta"],
        "historical_labels_status": "development/exploratory only; no confirmatory claim; the 44 fresh blinded judgments were excluded",
        "fresh_qwen_execution_drift": fresh_qwen_execution_drift,
        "direct_and_learner_results": {},
        "fold_selection_summary": fold_summary,
        "partition_metrics": {},
        "uncertainty": {},
        "sample_units": {},
        "prediction_coverage": {
            "selected_direct_replacement": int(np.isfinite(direct_scores[selected_arm]).sum()),
            "selected_representation_ridge_oof": int(np.isfinite(ridge_results[selected_arm]).sum()),
            "selected_current_plus_representation_oof": int(np.isfinite(complement_results[selected_arm]).sum()),
            "selected_capped_residual_with_fit_only_current_fallback": int(np.isfinite(selected_capped_residual).sum()),
            "fit_only_development_targets_using_current_fallback_for_capped_residual": int(folds[0]["evaluation_start"]),
        },
    }

    baseline_score = ridge_results["current"]
    challenger_score = ridge_results[selected_arm]
    selected_complement = complement_results[selected_arm]
    for arm in ARM_ORDER:
        if arm not in direct_scores:
            continue
        aggregate["direct_and_learner_results"][arm] = {
            "direct_ranker_development_oof": metric_set(
                ratings[partition_masks["development_oof"]], direct_scores[arm][partition_masks["development_oof"]]),
            "representation_ridge_development_oof": metric_set(
                ratings[partition_masks["development_oof"]], ridge_results[arm][partition_masks["development_oof"]]),
            "current_plus_representation_ridge_development_oof": metric_set(
                ratings[partition_masks["development_oof"]], complement_results[arm][partition_masks["development_oof"]]),
        }
        for part in ("validation_exploratory", "later_exploratory"):
            mask = partition_masks[part]
            aggregate["direct_and_learner_results"][arm][part] = {
                "direct_ranker": metric_set(ratings[mask], direct_scores[arm][mask]),
                "representation_ridge": metric_set(ratings[mask], ridge_results[arm][mask]),
                "current_plus_representation_ridge": metric_set(ratings[mask], complement_results[arm][mask]),
            }
    for part, mask in partition_masks.items():
        if "metadata_" in part:
            base = ridge_results["current"][mask]
            selected = complement_results[selected_arm][mask]
            aggregate["partition_metrics"][part] = {
                "rows": int(mask.sum()),
                "current_score_only_ridge": metric_set(ratings[mask], base) if mask.any() else None,
                "selected_current_plus_representation_ridge": metric_set(ratings[mask], selected) if mask.any() else None,
            }
        else:
            aggregate["partition_metrics"][part] = {
                "current_score_only_ridge": metric_set(ratings[mask], ridge_results["current"][mask]),
                "selected_representation_only_ridge": metric_set(ratings[mask], challenger_score[mask]),
                "selected_current_plus_representation_ridge": metric_set(ratings[mask], selected_complement[mask]),
            }

    dev_mask = partition_masks["development_oof"]
    dev_indexes = np.flatnonzero(dev_mask)
    paired_day_groups = _days_to_groups(day_values[dev_mask])
    aggregate["uncertainty"]["selected_complement_vs_current_ridge_by_day"] = grouped_bootstrap_delta(
        ratings[dev_mask], baseline_score[dev_mask], selected_complement[dev_mask], paired_day_groups)
    paired_30_groups = _thirty_day_groups(day_values[dev_mask])
    aggregate["uncertainty"]["selected_complement_vs_current_ridge_by_30_day_block"] = grouped_bootstrap_delta(
        ratings[dev_mask], baseline_score[dev_mask], selected_complement[dev_mask], paired_30_groups)
    aggregate["uncertainty"]["selected_complement_vs_current_ridge_by_author"] = grouped_bootstrap_delta(
        ratings[dev_mask], baseline_score[dev_mask], selected_complement[dev_mask],
        _groups_to_indices(author_groups[dev_mask]))
    aggregate["uncertainty"]["selected_direct_replacement_vs_served_current_by_day"] = grouped_bootstrap_delta(
        ratings[dev_mask], current[dev_mask], direct_scores[selected_arm][dev_mask], paired_day_groups)
    aggregate["uncertainty"]["selected_capped_residual_vs_served_current_by_day"] = grouped_bootstrap_delta(
        ratings[dev_mask], current[dev_mask], selected_capped_residual[dev_mask], paired_day_groups)
    aggregate["sample_units"] = {
        "development_oof_rows": int(dev_mask.sum()),
        "development_oof_unique_utc_days": int(len(set(map(int, day_values[dev_mask])))),
        "development_oof_distinct_authors": int(len(set(map(int, author_groups[dev_mask])))),
        "development_oof_30_day_blocks": int(len(paired_30_groups)),
        "high_count": int((ratings[dev_mask] >= 4).sum()),
        "low_count": int((ratings[dev_mask] <= 2).sum()),
        "neutral_count": int(((ratings[dev_mask] > 2) & (ratings[dev_mask] < 4)).sum()),
    }
    unfamiliar_by_fold = []
    for fold in folds:
        start, stop = int(fold["evaluation_start"]), int(fold["evaluation_stop"])
        unfamiliar = _unfamiliar_mask(author_groups, int(fold["train_stop"]), stop)
        # The mask aligns to the fold's evaluation interval, including known
        # blank-author histories as one explicit author group.
        indexes = np.arange(start, stop)[unfamiliar]
        familiar_indexes = np.arange(start, stop)[~unfamiliar]
        unfamiliar_by_fold.append({
            "fold": len(unfamiliar_by_fold) + 1,
            "unfamiliar_rows": int(len(indexes)),
            "familiar_rows": int(len(familiar_indexes)),
            "current_score_only_ridge": metric_set(ratings[indexes], baseline_score[indexes]) if len(indexes) else None,
            "selected_complement": metric_set(ratings[indexes], selected_complement[indexes]) if len(indexes) else None,
            "current_score_only_ridge_familiar": metric_set(ratings[familiar_indexes], baseline_score[familiar_indexes]) if len(familiar_indexes) else None,
            "selected_complement_familiar": metric_set(ratings[familiar_indexes], selected_complement[familiar_indexes]) if len(familiar_indexes) else None,
            "selected_direct_replacement_unfamiliar": metric_set(ratings[indexes], direct_scores[selected_arm][indexes]) if len(indexes) else None,
            "selected_direct_replacement_familiar": metric_set(ratings[familiar_indexes], direct_scores[selected_arm][familiar_indexes]) if len(familiar_indexes) else None,
        })
    aggregate["unfamiliar_author_development_folds"] = unfamiliar_by_fold

    # Save only private row-aligned evidence here. Public report generation
    # consumes aggregate JSON and never receives private IDs or vectors.
    vector_path = private_output / "embedding-vectors-private.npz"
    vectors_payload = {
        "prepared_read_ids": all_ids,
        "target_read_ids": target_ids,
        "qwen_document_title_author": qwen_doc_ta,
        "qwen_document_verified_metadata": qwen_doc_meta,
        "qwen_query_instructed_title_author": qwen_query_instructed_ta,
        "qwen_query_instructed_verified_metadata": qwen_query_instructed_meta,
        "bge_document_title_author": bge_doc_ta,
        "bge_document_verified_metadata": bge_doc_meta,
        "metadata_mask_prepared": text_data["metadata_mask"],
    }
    tmp_vectors = vector_path.with_suffix(".tmp.npz")
    np.savez_compressed(tmp_vectors, **vectors_payload)
    os.chmod(tmp_vectors, 0o600)
    os.replace(tmp_vectors, vector_path)

    score_path = private_output / "embedding-scores-private.npz"
    arrays: dict[str, np.ndarray] = {
        "read_ids": feature_ids,
        "record_indexes": record_indexes,
        "ratings": ratings,
        "utc_day": day_values,
        "author_group": author_groups,
        "train_mask": train_mask,
        "validation_mask": validation_mask,
        "later_mask": later_mask,
        "oof_selection_mask": oof_selection_mask,
        "metadata_mask": selected_metadata_mask,
        "current": current,
        "selected_representation_score": challenger_score,
        "selected_complement_score": selected_complement,
        "selected_direct_replacement_score": direct_scores[selected_arm],
        "selected_representation_ridge_oof_score": ridge_results[selected_arm],
        "selected_current_plus_representation_oof_score": selected_complement,
        "selected_capped_residual_score": selected_capped_residual,
        "current_ridge_score": baseline_score,
    }
    for arm in ARM_ORDER:
        if arm in direct_scores:
            arrays[f"direct__{arm}"] = direct_scores[arm]
            arrays[f"ridge__{arm}"] = ridge_results[arm]
            arrays[f"complement__{arm}"] = complement_results[arm]
    tmp_scores = score_path.with_suffix(".tmp.npz")
    np.savez_compressed(tmp_scores, **arrays)
    os.chmod(tmp_scores, 0o600)
    os.replace(tmp_scores, score_path)

    aligned_score_path = private_output / "embedding-scores-aligned-private.npz"
    tmp_aligned_scores = aligned_score_path.with_suffix(".tmp.npz")
    np.savez_compressed(tmp_aligned_scores, **arrays)
    os.chmod(tmp_aligned_scores, 0o600)
    os.replace(tmp_aligned_scores, aligned_score_path)

    aggregate["private_artifacts"] = {
        "vector_path": str(vector_path),
        "vector_sha256": sha256_file(vector_path),
        "vector_bytes": vector_path.stat().st_size,
        "score_path": str(score_path),
        "score_sha256": sha256_file(score_path),
        "score_bytes": score_path.stat().st_size,
        "aligned_score_path": str(aligned_score_path),
        "aligned_score_sha256": sha256_file(aligned_score_path),
        "aligned_score_bytes": aligned_score_path.stat().st_size,
        "aligned_score_masks": ["train_mask", "validation_mask", "later_mask", "oof_selection_mask"],
        "aligned_score_ids_key": "read_ids",
    }
    aggregate_path = private_output / "embedding-representation-aggregate-private.json"
    aggregate_path.write_text(json.dumps(aggregate, indent=2, sort_keys=True, allow_nan=False) + "\n")
    os.chmod(aggregate_path, 0o600)
    return aggregate


def history_features_sparse(
    records: list[tuple[date, dict, np.ndarray]],
    history_vectors: csr_matrix,
    query_vectors: csr_matrix,
    *,
    target_start: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Sparse equivalent of history_features for fold-fitted TF-IDF views."""
    target_count = query_vectors.shape[0]
    feature_values = np.full((target_count, len(BASE_NAMES)), np.nan, dtype=np.float64)
    direct_scores = np.full(target_count, np.nan, dtype=np.float64)
    days = [row[0].date() for row in records]
    items = [row[1] for row in records]
    cursor = target_start
    with threadpool_limits(limits=1):
        while cursor < len(records):
            stop = cursor + 1
            while stop < len(records) and days[stop] == days[cursor]:
                stop += 1
            history_end = cursor
            while history_end > 0 and days[history_end - 1] >= days[cursor]:
                history_end -= 1
            if history_end >= target_start:
                history_indexes = np.asarray(
                    sorted(range(history_end), key=lambda index: int(items[index]["id"])), dtype=np.int64
                )
                target_indexes = np.arange(cursor, stop, dtype=np.int64)
                history = [items[index] for index in history_indexes]
                queries = [items[index] for index in target_indexes]
                hist_sparse = history_vectors[history_indexes]
                query_sparse = query_vectors[target_indexes - target_start]
                similarities = (query_sparse @ hist_sparse.T).toarray().astype(np.float32)
                X, scores = history_components_from_similarities(history, queries, similarities)
                feature_values[target_indexes - target_start] = X
                direct_scores[target_indexes - target_start] = np.round(scores, 1)
            cursor = stop
    valid_start = max(0, target_start - target_start)
    if (not np.isfinite(feature_values[valid_start:]).all()
            or not np.isfinite(direct_scores[valid_start:]).all()):
        raise ValueError("sparse TF-IDF replay lacks complete target coverage")
    return feature_values, direct_scores


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run the strict field-provenance v2 embedding screen. All three "
            "frozen amendments/supplements are required; identity-only cache "
            "matching is internal and is not a scoring mode.\n\n" + (__doc__ or "")
        ),
        allow_abbrev=False,
    )
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--feature-manifest", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument(
        "--strict-amendment", type=Path, required=True,
        help="frozen strict-v2 protocol amendment (required; no identity-only CLI mode)",
    )
    parser.add_argument(
        "--method-amendment", type=Path, required=True,
        help="frozen field-verification method amendment (required)",
    )
    parser.add_argument(
        "--provenance-supplement", type=Path, required=True,
        help="frozen per-field provenance supplement (required)",
    )
    parser.add_argument("--prior-vectors", type=Path, required=True)
    parser.add_argument("--prior-aggregate", type=Path, required=True)
    parser.add_argument("--fresh-corpus", type=Path, required=True)
    parser.add_argument("--fresh-vectors", type=Path, required=True)
    parser.add_argument("--fresh-manifest", type=Path, required=True)
    parser.add_argument("--private-output", type=Path, required=True)
    parser.add_argument("--bge-cache-dir", type=Path, required=True)
    args = parser.parse_args()
    result = build_experiment(
        corpus_path=args.corpus,
        feature_path=args.features,
        feature_manifest_path=args.feature_manifest,
        protocol_path=args.protocol,
        strict_amendment_path=args.strict_amendment,
        method_amendment_path=args.method_amendment,
        provenance_supplement_path=args.provenance_supplement,
        prior_vectors_path=args.prior_vectors,
        prior_aggregate_path=args.prior_aggregate,
        fresh_corpus_path=args.fresh_corpus,
        fresh_vectors_path=args.fresh_vectors,
        fresh_manifest_path=args.fresh_manifest,
        private_output=args.private_output,
        bge_cache_dir=args.bge_cache_dir,
    )
    # Only an aggregate status line is printed; no private IDs, text, or vectors.
    print(json.dumps({
        "experiment": result["experiment"],
        "common_targets": result["common_targets"],
        "selected_arm": result["selected_arm"],
        "selected_delta": result["selection_delta_mean_balanced_auc"],
        "private_score_sha256": result["private_artifacts"]["score_sha256"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
