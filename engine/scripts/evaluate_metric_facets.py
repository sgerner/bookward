#!/usr/bin/env python3
"""Causal, aggregate-only experiments for constrained similarity and grounded facets.

The private evaluator is intentionally wired only after the study manifest and
corpus adapter are supplied. The pure helpers below support deterministic
synthetic tests without reading or scoring private corpus rows.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import sys
import unicodedata
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
from scipy.sparse import csr_matrix
from scipy.stats import rankdata
from sklearn.decomposition import PCA
from sklearn.linear_model import Ridge
from threadpoolctl import threadpool_limits

ENGINE = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(ENGINE))

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
)
from afterword_engine.covers import METADATA_GENRE_LIMIT
from afterword_engine.identity import book_identity
from afterword_engine.ingestion import _read_metadata_identity_hash
from afterword_engine.subjects import normalize_subjects
from evaluate_next_five import development_folds as frozen_development_folds
from evaluate_ranking import load_corpus, prepare
from evaluate_next_five import development_folds as frozen_development_folds
from evaluate_ranking import load_corpus, prepare


METRIC_COMPONENTS = (4, 8)
METRIC_RIDGE_ALPHA = 10.0
METRIC_LAMBDAS = (0.025, 0.05)
FACET_BLEND_WEIGHTS = (0.10, 0.25)
FACET_VERSION = "grounded-synopsis-facets-v2-explicit-setting"
FACET_SUBJECT_FEATURE_LIMIT = 8
METRIC_VERSION = "rank1-pca-ridge-metric-v1"
FROZEN_PROTOCOL_SHA256 = "8c58739a896acb2d0284f3c51eb5752121ed9262a415ecd313b8f124d7dd935d"
LOCKED_MASTER_SELECTION_SHA256 = "e8431705b95cbbc1d9dea0fce1f3cfa3f9b014f815869f11788a19898a5d2268"
TOPIC_CUES: dict[str, tuple[str, ...]] = {
    "war_conflict": ("war", "battle", "soldier", "military", "invasion"),
    "family_relationship": (
        "family", "mother", "father", "sister", "brother", "daughter", "son",
    ),
    "romance": ("romance", "love", "lover", "marriage"),
    "crime_investigation": ("murder", "detective", "investigation", "crime", "mystery"),
    "political_power": ("politics", "government", "political", "president", "empire"),
    "survival": ("survive", "survival", "escape"),
    "technology": ("technology", "artificial intelligence", "robot", "computer"),
}
TIME_CUES: dict[str, tuple[str, ...]] = {
    "past": (
        "during world war i", "during world war ii", "set during world war i",
        "set during world war ii", "set in world war i", "set in world war ii",
        "takes place during world war i", "takes place during world war ii",
        "takes place in world war i", "takes place in world war ii",
        "in the 18th century", "in the 19th century", "in the 20th century",
        "during the 18th century", "during the 19th century", "during the 20th century",
        "set in the 18th century", "set in the 19th century", "set in the 20th century",
        "set during the 18th century", "set during the 19th century", "set during the 20th century",
        "takes place in the 18th century", "takes place in the 19th century",
        "takes place in the 20th century",
    ),
    "contemporary": (
        "set in the present day", "set in present-day", "set in the present",
        "takes place in the present day", "takes place in present-day",
        "takes place in the present", "unfolds in the present day",
        "unfolds in present-day", "unfolds in the present",
    ),
    "future": (
        "set in the future", "set in a future", "takes place in the future",
        "takes place in a future", "unfolds in the future", "unfolds in a future",
        "set in a dystopian future", "set in a post-apocalyptic future",
        "in a dystopian future", "in a post-apocalyptic future",
    ),
}
_ALL_FIXED_FACETS = {
    "synopsis_topics": tuple(TOPIC_CUES),
    "setting_time": tuple(TIME_CUES),
}


def preregistration_metadata() -> dict[str, Any]:
    """Return the fixed family grid without asserting protocol chronology."""
    lexicon = json.dumps(
        {"topics": TOPIC_CUES, "times": TIME_CUES},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return {
        "version": 1,
        "metric": {
            "family": METRIC_VERSION,
            "rank": 1,
            "pca_components": list(METRIC_COMPONENTS),
            "ridge_alpha": METRIC_RIDGE_ALPHA,
            "lambdas": [0.0, *METRIC_LAMBDAS],
            "training_only": True,
        },
        "facets": {
            "family": FACET_VERSION,
            "blend_weights": list(FACET_BLEND_WEIGHTS),
            "fields": ["verified subjects", "explicit synopsis time cues", "explicit synopsis topic cues"],
            "unknown_policy": "unknown values carry no feature and are never treated as a match",
            "evidence_policy": "each populated facet retains the private source field, source reference, and exact matched span",
            "training_only_subject_vocabulary": True,
            "subject_provenance_normalization_limit": METADATA_GENRE_LIMIT,
            "subject_feature_limit": FACET_SUBJECT_FEATURE_LIMIT,
            "time_setting_rule": "time values require explicit setting phrases or named era references in temporal context; bare historical/future/genre terms are excluded",
            "cue_lexicon_sha256": hashlib.sha256(lexicon).hexdigest(),
        },
        "registration_verification": "Grid metadata only; external frozen-protocol manifest establishes timing.",
    }


def _unit_rows(vectors: Sequence[Sequence[float]]) -> np.ndarray:
    values = np.asarray(vectors, dtype=np.float64)
    if values.ndim != 2 or values.shape[1] == 0 or not np.isfinite(values).all():
        raise ValueError("vectors must be a finite non-empty two-dimensional matrix")
    norms = np.linalg.norm(values, axis=1, keepdims=True)
    return np.divide(values, norms, out=np.zeros_like(values), where=norms > 1e-12)


def _servable_unit_rows(vectors: Sequence[Sequence[float]]) -> np.ndarray:
    """Use the serving ranker's float32 normalization path."""
    values = np.asarray(vectors, dtype=np.float32)
    if values.ndim != 2 or values.shape[1] == 0 or not np.isfinite(values).all():
        raise ValueError("vectors must be a finite non-empty two-dimensional matrix")
    norms = np.linalg.norm(values, axis=1, keepdims=True)
    return np.divide(values, norms, out=np.zeros_like(values), where=norms > 0)


def fit_supervised_metric(
    vectors: Sequence[Sequence[float]],
    ratings: Sequence[float],
    train_indexes: Sequence[int],
    *,
    components: int,
    ridge_alpha: float = METRIC_RIDGE_ALPHA,
) -> dict[str, Any]:
    """Fit one supervised direction inside a training-only PCA subspace.

    The only learned metric direction is the ridge prediction direction in the
    first four or eight training-prefix principal components. Query rows,
    labels, means, scales, and PCA components outside ``train_indexes`` are
    never consulted.
    """
    matrix = _servable_unit_rows(vectors).astype(np.float64)
    stars = np.asarray(ratings, dtype=np.float64)
    indexes = np.asarray(train_indexes, dtype=np.int64)
    if stars.shape != (len(matrix),):
        raise ValueError("ratings must be one value per vector")
    if indexes.ndim != 1 or len(indexes) <= 1 or len(np.unique(indexes)) != len(indexes):
        raise ValueError("training indexes must be a unique non-empty vector")
    if np.any(indexes < 0) or np.any(indexes >= len(matrix)):
        raise ValueError("training indexes are out of range")
    train_stars = stars[indexes]
    if not np.isfinite(train_stars).all():
        raise ValueError("training ratings must be finite")
    if not np.equal(train_stars, np.floor(train_stars)).all() or np.any((train_stars < 1) | (train_stars > 5)):
        raise ValueError("training ratings must be integer stars from 1 through 5")
    if components not in METRIC_COMPONENTS or components > min(matrix.shape[1], len(indexes) - 1):
        raise ValueError("components must be a supported PCA size available in training")
    if not np.isfinite(ridge_alpha) or ridge_alpha <= 0:
        raise ValueError("ridge_alpha must be finite and positive")

    train = matrix[indexes]
    pca = PCA(n_components=components, svd_solver="randomized", random_state=20261002,
              iterated_power=4)
    train_projection = pca.fit_transform(train)
    scale = train_projection.std(axis=0)
    scale[scale < 1e-10] = 1.0
    standardized = train_projection / scale
    model = Ridge(alpha=ridge_alpha, fit_intercept=True)
    model.fit(standardized, train_stars - 3.0)
    direction = pca.components_.T @ (model.coef_ / scale)
    norm = float(np.linalg.norm(direction))
    if not np.isfinite(norm) or norm < 1e-12:
        direction = np.zeros(matrix.shape[1], dtype=np.float64)
    else:
        direction = direction / norm
    return {
        "direction": direction,
        "components": int(components),
        "ridge_alpha": float(ridge_alpha),
        "train_count": int(len(indexes)),
        "fit_seed": 20261002,
        "version": METRIC_VERSION,
    }


def metric_vectors(
    vectors: Sequence[Sequence[float]],
    direction: Sequence[float],
    strength: float,
) -> np.ndarray:
    """Apply a rank-one PSD metric and return unit vectors for cosine scoring."""
    unit = _servable_unit_rows(vectors).astype(np.float64)
    direction = np.asarray(direction, dtype=np.float64)
    if direction.shape != (unit.shape[1],) or not np.isfinite(direction).all():
        raise ValueError("direction must be a finite vector matching embedding width")
    if not np.isfinite(strength) or strength < 0:
        raise ValueError("strength must be finite and non-negative")
    norm = float(np.linalg.norm(direction))
    if norm < 1e-12 or strength == 0:
        return unit.astype(np.float32)
    v = direction / norm
    # BᵀB = I + strength vvᵀ. This is the symmetric square-root transform.
    factor = np.sqrt(1.0 + strength) - 1.0
    transformed = unit + factor * np.outer(unit @ v, v)
    return _unit_rows(transformed).astype(np.float32)


def _normalize_subject(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value)).casefold()
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return " ".join(value.split())


def _raw_subjects(subjects: Iterable[str] | str | None) -> list[str]:
    if subjects is None:
        return []
    if isinstance(subjects, str):
        raw = subjects.strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            parsed = [raw]
        subjects = parsed if isinstance(parsed, list) else [str(parsed)]
    result = []
    seen = set()
    for subject in subjects:
        raw_value = str(subject).strip()
        normalized = _normalize_subject(raw_value)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(raw_value)
    return result


def _matched_cues(text: str, cues: dict[str, tuple[str, ...]], provenance: dict[str, Any], field: str):
    matches: dict[str, list[dict[str, Any]]] = {}
    for label, phrases in cues.items():
        evidence = []
        for phrase in phrases:
            pattern = re.compile(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", re.IGNORECASE)
            for found in pattern.finditer(text):
                evidence.append({
                    "source_field": provenance.get("source_field", field),
                    "source": provenance.get("source"),
                    "source_reference": provenance.get("source_reference"),
                    "document_sha256": provenance.get("document_sha256"),
                    "span": [int(found.start()), int(found.end())],
                    "evidence_text": text[found.start():found.end()],
                })
        if evidence:
            # Repeated words do not increase a facet's weight.
            matches[label] = evidence[:3]
    return matches


def _has_source_reference(provenance: dict[str, Any] | None) -> bool:
    return bool(
        isinstance(provenance, dict)
        and str(provenance.get("source") or "").strip()
        and str(provenance.get("source_reference") or "").strip()
    )


def extract_grounded_facets(
    *,
    description: str | None,
    description_provenance: dict[str, Any] | None,
    subjects: Iterable[str] | str | None,
    subjects_provenance: dict[str, Any] | None,
) -> dict[str, Any]:
    """Extract literal catalog and synopsis cues with private evidence spans.

    The caller must pass only independently verified catalog fields. Ratings,
    reviews, user notes, and generated prose are deliberately not parameters.
    A facet without an explicit cue remains ``unknown``.
    """
    description = str(description or "")
    description_provenance = description_provenance or {}
    subjects_provenance = subjects_provenance or {}
    if not _has_source_reference(description_provenance) or description_provenance.get("kind") != "synopsis":
        description = ""
        description_provenance = {}
    if not _has_source_reference(subjects_provenance):
        subjects = None
        subjects_provenance = {}
    raw_subjects = _raw_subjects(subjects)
    subject_document_hash = str(subjects_provenance.get("document_sha256") or hashlib.sha256(
        json.dumps(raw_subjects, ensure_ascii=False).encode("utf-8")
    ).hexdigest())
    description_document_hash = str(description_provenance.get("document_sha256") or hashlib.sha256(
        description.encode("utf-8")
    ).hexdigest())
    subject_evidence = []
    subject_values = []
    for raw_subject in raw_subjects:
        normalized = _normalize_subject(raw_subject)
        subject_values.append(normalized)
        subject_evidence.append({
            "value": normalized,
            "source_field": subjects_provenance.get("source_field", "subjects"),
            "source": subjects_provenance.get("source"),
            "source_reference": subjects_provenance.get("source_reference"),
            "document_sha256": subject_document_hash,
            "evidence_text": raw_subject,
        })
    description_provenance = {**description_provenance, "document_sha256": description_document_hash}
    topics = _matched_cues(description, TOPIC_CUES, description_provenance, "description")
    times = _matched_cues(description, TIME_CUES, description_provenance, "description")
    values: dict[str, Any] = {
        "catalog_subjects": subject_values or "unknown",
        "synopsis_topics": sorted(topics) if topics else "unknown",
        "setting_time": sorted(times) if times else "unknown",
    }
    return {
        "version": FACET_VERSION,
        "values": values,
        "evidence": {
            "catalog_subjects": subject_evidence,
            "synopsis_topics": topics,
            "setting_time": times,
        },
    }


def _label_sets(facets: Sequence[dict[str, Any]], group: str) -> list[set[str]]:
    result = []
    for facet in facets:
        values = facet.get("values", {})
        if group == "catalog_subjects":
            value = values.get(group, "unknown")
            result.append(set(value) if isinstance(value, list) else set())
        elif group == "synopsis_topics":
            value = values.get(group, "unknown")
            result.append(set(value) if isinstance(value, list) else set())
        elif group == "setting_time":
            value = values.get(group, "unknown")
            result.append(set(value) if isinstance(value, list) else set())
        else:
            raise ValueError(f"unknown facet group: {group}")
    return result


def _binary_matrix(label_sets: list[set[str]], vocabulary: Sequence[str]) -> csr_matrix:
    if not vocabulary:
        return csr_matrix((len(label_sets), 0), dtype=np.float32)
    vocab = set(vocabulary)
    rows, cols = [], []
    positions = {name: i for i, name in enumerate(vocabulary)}
    for row, labels in enumerate(label_sets):
        for label in labels & vocab:
            rows.append(row)
            cols.append(positions[label])
    return csr_matrix((np.ones(len(rows), dtype=np.float32), (rows, cols)),
                      shape=(len(label_sets), len(vocabulary)), dtype=np.float32)


def facet_similarity_matrix(
    facets: Sequence[dict[str, Any]],
    train_indexes: Sequence[int],
) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    """Build mean Jaccard agreement over shared, known facet groups.

    Direct subject vocabulary is learned from the training prefix only. Fixed
    synopsis categories come from the frozen lexicon. Unknown facets are omitted
    from both numerator and denominator; no unsupported value is imputed.
    """
    n = len(facets)
    indexes = np.asarray(train_indexes, dtype=np.int64)
    if indexes.ndim != 1 or not len(indexes) or np.any(indexes < 0) or np.any(indexes >= n):
        raise ValueError("training indexes must be non-empty and in range")
    subject_sets = _label_sets(facets, "catalog_subjects")
    topic_sets = _label_sets(facets, "synopsis_topics")
    time_sets = _label_sets(facets, "setting_time")
    subject_vocab = sorted(set().union(*(subject_sets[i] for i in indexes)))
    vocabularies = {
        "catalog_subjects": subject_vocab,
        "synopsis_topics": sorted(TOPIC_CUES),
        "setting_time": sorted(TIME_CUES),
    }
    groups = {
        "catalog_subjects": subject_sets,
        "synopsis_topics": topic_sets,
        "setting_time": time_sets,
    }
    total = np.zeros((n, n), dtype=np.float32)
    known_count = np.zeros((n, n), dtype=np.uint8)
    coverage = {}
    for name, sets in groups.items():
        vocabulary = vocabularies[name]
        matrix = _binary_matrix(sets, vocabulary)
        lengths = np.asarray(matrix.sum(axis=1)).ravel().astype(np.float32)
        shared = (matrix @ matrix.T).toarray().astype(np.float32)
        union = lengths[:, None] + lengths[None, :] - shared
        known = (lengths[:, None] > 0) & (lengths[None, :] > 0)
        jaccard = np.divide(shared, union, out=np.zeros_like(shared), where=union > 0)
        total += jaccard * known
        known_count += known.astype(np.uint8)
        coverage[name] = int(np.count_nonzero(lengths))
    observed = known_count > 0
    result = np.divide(total, known_count, out=np.zeros_like(total), where=observed)
    return result, observed, {
        "training_subject_vocabulary_size": int(len(subject_vocab)),
        "documents_with_known_group": coverage,
        "unknown_policy": "unknown groups omitted; pairs with no shared known groups fall back to the reference similarity",
    }


def blend_facet_similarity(
    reference_similarity: np.ndarray,
    facet_similarity: np.ndarray,
    facet_observed: np.ndarray,
    weight: float,
) -> np.ndarray:
    """Add known facet agreement while preserving base cosine for unknown pairs."""
    reference = np.asarray(reference_similarity, dtype=np.float32)
    facet = np.asarray(facet_similarity, dtype=np.float32)
    observed = np.asarray(facet_observed, dtype=bool)
    if reference.ndim != 2 or facet.shape != reference.shape or observed.shape != reference.shape:
        raise ValueError("similarity and observed matrices must have identical two-dimensional shapes")
    if not np.isfinite(reference).all() or not np.isfinite(facet).all():
        raise ValueError("similarity matrices must be finite")
    if not np.isfinite(weight) or weight < 0 or weight > 1:
        raise ValueError("weight must be in [0, 1]")
    result = reference.copy()
    result[observed] = np.clip(
        reference[observed] + weight * facet[observed], -1.0, 1.0)
    return result


def make_metric_similarity(
    reference_similarity: np.ndarray,
    unit_vectors: Sequence[Sequence[float]],
    direction: Sequence[float],
    strength: float,
) -> np.ndarray:
    """Return normalized rank-one Mahalanobis cosine from a cached cosine matrix."""
    reference = np.asarray(reference_similarity, dtype=np.float32)
    unit = _servable_unit_rows(unit_vectors).astype(np.float64)
    direction = np.asarray(direction, dtype=np.float64)
    if reference.shape != (len(unit), len(unit)):
        raise ValueError("reference similarity must be square and align to vectors")
    if direction.shape != (unit.shape[1],) or not np.isfinite(direction).all():
        raise ValueError("direction must be finite and match embedding width")
    if not np.isfinite(strength) or strength < 0:
        raise ValueError("strength must be finite and non-negative")
    norm = float(np.linalg.norm(direction))
    if norm < 1e-12 or strength == 0:
        return reference.copy()
    projection = unit @ (direction / norm)
    # For M = I + λvvᵀ, the cosine numerator uses λ.  The square-root
    # transform uses γ = sqrt(1 + λ) - 1, whose induced dot-product term is
    # 2γ + γ² = λ.
    numerator = reference + strength * np.outer(projection, projection)
    denominator = np.sqrt(1.0 + strength * projection * projection)
    result = numerator / np.outer(denominator, denominator)
    return np.clip(result, -1.0, 1.0).astype(np.float32)


def score_from_similarities(
    history: Sequence[dict[str, Any]],
    candidate: dict[str, Any],
    similarities: Sequence[float],
    *,
    history_day_ordinals: Sequence[float] | None = None,
) -> float:
    """Apply the current serving formula to caller-supplied history cosines.

    History must already be strictly earlier than the candidate day and deduped
    by work identity. The formula is kept in sync with ``rank_candidates`` and
    the lambda-zero parity invariant is tested against that implementation.
    """
    sims = np.asarray(similarities, dtype=np.float32)
    if sims.shape != (len(history),) or not np.isfinite(sims).all():
        raise ValueError("one finite similarity is required for each history row")
    ratings = np.asarray([float(row.get("rating") or 0) for row in history], dtype=np.float64)
    if np.any((ratings < 1) | (ratings > 5)):
        raise ValueError("history must contain rated works only")
    positive = ratings >= 4
    negative = ratings <= 2
    positive_values = sims[positive]
    negative_values = sims[negative]
    positive_max = float(positive_values.max()) if len(positive_values) else 0.25
    negative_max = float(negative_values.max()) if len(negative_values) else 0.0
    positive_top = float(_top_weighted(positive_values.reshape(1, -1), default=0.25)[0])

    author = book_identity("", candidate.get("author", ""))
    author_ratings = [
        float(row.get("rating") or 0)
        for row in history
        if book_identity("", row.get("author", "")) == author
    ]
    global_mean = float(ratings.mean()) if len(ratings) else 3.0
    author_mean = float(np.mean(author_ratings)) if author_ratings else 0.0
    author_delta = (
        (len(author_ratings) * author_mean + 5.0 * global_mean) / (len(author_ratings) + 5.0)
        - global_mean
        if author_ratings else 0.0
    )

    local_rating = 3.0
    kernel_rating = recent_rating = global_mean
    effective_sample_size = 0.0
    if len(sims) and np.any(sims > 0):
        neighbors = np.argsort(-sims, kind="stable")[:min(_KERNEL_NEIGHBORS, len(sims))]
        weights = (np.maximum(sims[neighbors].astype(np.float64), 0.0) + 1e-8) ** _KERNEL_POWER
        kernel_rating = float(np.dot(weights, ratings[neighbors]) / weights.sum())
        effective_sample_size = float(weights.sum() ** 2 / np.dot(weights, weights))
        if history_day_ordinals is None:
            days = np.asarray([
                _read_day(row.get("read_at")).toordinal() if _read_day(row.get("read_at")) else np.nan
                for row in history
            ], dtype=np.float64)
        else:
            days = np.asarray(history_day_ordinals, dtype=np.float64)
            if days.shape != (len(history),):
                raise ValueError("history day ordinals must align to history rows")
        days = days[neighbors]
        known = np.isfinite(days)
        if known.any():
            ages = np.maximum(0.0, (days[known].max() - days) / 365.25)
            ages[~known] = np.median(ages[known])
            recent_weights = weights * np.exp(-ages / _RECENCY_TIMESCALE_YEARS)
            recent_rating = float(np.dot(recent_weights, ratings[neighbors]) / recent_weights.sum())
        else:
            recent_rating = kernel_rating
        local = neighbors[:min(5, len(neighbors))]
        local_weights = np.maximum(sims[local], 0.0) + 1e-4
        local_rating = float(np.dot(local_weights, ratings[local]) / local_weights.sum())

    score = (
        42.0 + 48.0 * positive_top - 24.0 * negative_max
        + 3.5 * (local_rating - 3.0) + 3.5 * author_delta
    )
    kernel_strength = _KERNEL_SCORE_PER_STAR * min(1.0, len(history) / _KERNEL_FULL_HISTORY)
    score += (
        kernel_strength * (kernel_rating - global_mean)
        * effective_sample_size / (effective_sample_size + _KERNEL_ESS_PRIOR)
    )
    recency_strength = _RECENCY_SCORE_PER_STAR * min(1.0, len(history) / _KERNEL_FULL_HISTORY)
    score += recency_strength * (recent_rating - kernel_rating)
    score += (float(candidate.get("source_weight") or 1.0) - 1.0) * 5.0
    confidence = _metadata_confidence(candidate)
    score = 50.0 + confidence * (score - 50.0)
    return round(max(0.0, min(100.0, score)), 1)


def score_records_from_similarity(
    records: Sequence[tuple[Any, dict[str, Any], Sequence[float]]],
    target_indexes: Sequence[int],
    similarity_matrix: np.ndarray,
    *,
    context: dict[str, Any] | None = None,
) -> np.ndarray:
    """Score each target from strictly earlier whole-day history rows."""
    similarities = np.asarray(similarity_matrix, dtype=np.float64)
    if similarities.shape != (len(records), len(records)) or not np.isfinite(similarities).all():
        raise ValueError("similarity matrix must be finite, square, and aligned to records")
    if context is None:
        context = make_scoring_context(records)
    dates = context["date_ordinals"]
    history_by_day = context["history_by_day"]
    if len(dates) != len(records):
        raise ValueError("scoring context must align to records")
    target_indexes = np.asarray(target_indexes, dtype=np.int64)
    if np.any(target_indexes < 0) or np.any(target_indexes >= len(records)):
        raise ValueError("target index out of range")
    output = np.full(len(target_indexes), np.nan, dtype=np.float64)
    for out_pos, target_index in enumerate(target_indexes):
        query_day = int(dates[target_index])
        history_indexes = history_by_day[query_day]
        history = [records[i][1] for i in history_indexes]
        output[out_pos] = score_from_similarities(
            history,
            records[target_index][1],
            similarities[target_index, history_indexes],
            history_day_ordinals=dates[history_indexes],
        )
    return output


def make_scoring_context(records):
    """Cache strict-earlier, serving-ID-ordered histories for every target day."""
    dates = np.asarray([
        (row[0].date() if hasattr(row[0], "date") else row[0]).toordinal()
        for row in records
    ], dtype=np.int64)
    ids = np.asarray([int(row[1].get("id", 0)) for row in records], dtype=np.int64)
    histories = {}
    for day in np.unique(dates):
        indexes = np.flatnonzero(dates < day)
        indexes = indexes[np.argsort(ids[indexes], kind="stable")]
        histories[int(day)] = indexes
    return {"date_ordinals": dates, "history_by_day": histories}


def cosine_similarity_matrix(vectors: Sequence[Sequence[float]]) -> np.ndarray:
    """Match serving-ranker float32 normalization and matrix multiplication."""
    unit = _servable_unit_rows(vectors)
    return unit @ unit.T


def cosine_cross_similarity_matrix(
    query_vectors: Sequence[Sequence[float]],
    history_vectors: Sequence[Sequence[float]],
) -> np.ndarray:
    """Match serving normalization for distinct query and history documents."""
    query = _servable_unit_rows(query_vectors)
    history = _servable_unit_rows(history_vectors)
    if query.shape[1] != history.shape[1]:
        raise ValueError("query and history embedding widths must match")
    return query @ history.T


def causal_cross_similarity_matrix(
    records,
    query_vectors: Sequence[Sequence[float]],
    history_vectors: Sequence[Sequence[float]],
    context: dict[str, Any] | None = None,
) -> np.ndarray:
    """Use the serving ranker's same-day query batches and strict history slices."""
    query = _servable_unit_rows(query_vectors)
    history = _servable_unit_rows(history_vectors)
    if query.shape != history.shape or len(query) != len(records):
        raise ValueError("causal query/history vectors must align to prepared records")
    if context is None:
        context = make_scoring_context(records)
    days = context["date_ordinals"]
    if len(days) != len(records):
        raise ValueError("scoring context must align to prepared records")
    result = np.zeros((len(records), len(records)), dtype=np.float32)
    for day in np.unique(days):
        targets = np.flatnonzero(days == day)
        prior = context["history_by_day"][int(day)]
        if len(prior):
            result[np.ix_(targets, prior)] = query[targets] @ history[prior].T
    return result


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _auc(labels: Sequence[bool], scores: Sequence[float]) -> float | None:
    labels = np.asarray(labels, dtype=bool)
    scores = np.asarray(scores, dtype=np.float64)
    positives = int(labels.sum())
    if positives == 0 or positives == len(labels):
        return None
    ranks = rankdata(scores, method="average")
    return float((ranks[labels].sum() - positives * (positives + 1) / 2)
                 / (positives * (len(labels) - positives)))


def _metrics(ratings: Sequence[float], scores: Sequence[float]) -> dict[str, Any]:
    ratings = np.asarray(ratings, dtype=np.float64)
    scores = np.asarray(scores, dtype=np.float64)
    if not len(ratings) or not np.isfinite(scores).all():
        raise ValueError("Metrics require a nonempty finite score slice")
    high, low = ratings >= 4, ratings <= 2
    high_auc = _auc(high, scores)
    low_auc = _auc(low, -scores)
    top = np.argsort(-scores, kind="stable")[:min(20, len(scores))]
    bottom = np.argsort(scores, kind="stable")[:min(20, len(scores))]
    tails = high | low
    high_low_auc = _auc(high[tails], scores[tails]) if tails.any() else None
    return {
        "n": int(len(scores)),
        "high_n": int(high.sum()),
        "low_n": int(low.sum()),
        "high_auc": high_auc,
        "low_reversed_auc": low_auc,
        "balanced_auc": None if high_auc is None or low_auc is None else (high_auc + low_auc) / 2,
        "high_vs_low_auc": high_low_auc,
        "top20_high_precision": float(high[top].mean()),
        "bottom20_low_precision": float(low[bottom].mean()),
    }


def _fold_metrics(ratings: np.ndarray, scores: np.ndarray, folds: Sequence[dict[str, int]]):
    return [
        _metrics(ratings[int(fold["evaluation_start"]):int(fold["evaluation_stop"])],
                 scores[int(fold["evaluation_start"]):int(fold["evaluation_stop"])])
        for fold in folds
    ]


def _mean_balanced(values: Sequence[dict[str, Any]]) -> float:
    aucs = [row["balanced_auc"] for row in values]
    if any(value is None for value in aucs):
        raise ValueError("Every frozen fold must contain both endpoint classes")
    return float(np.mean(aucs))


def _round_served(values: Sequence[float]) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64)
    return np.asarray([round(float(value), 1) if np.isfinite(value) else np.nan
                       for value in np.clip(values, 0.0, 100.0)], dtype=np.float64)


def _fit_alignment(source: Sequence[float], reference: Sequence[float]) -> dict[str, float]:
    source = np.asarray(source, dtype=np.float64)
    reference = np.asarray(reference, dtype=np.float64)
    valid = np.isfinite(source) & np.isfinite(reference)
    if not valid.any():
        raise ValueError("No finite training scores for score-scale alignment")
    source_mean, reference_mean = float(source[valid].mean()), float(reference[valid].mean())
    source_sd, reference_sd = float(source[valid].std()), float(reference[valid].std())
    return {
        "source_mean": source_mean,
        "reference_mean": reference_mean,
        "source_sd": source_sd,
        "reference_sd": reference_sd,
        "scale": reference_sd / source_sd if source_sd > 1e-12 else 0.0,
    }


def _apply_alignment(values: Sequence[float], fit: dict[str, float]) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64)
    return _round_served(fit["reference_mean"] + (values - fit["source_mean"]) * fit["scale"])


def _save_private_json(path: Path, value: Any):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _method_amendment_sha256(path: Path | None) -> str | None:
    if path is None:
        return None
    amendment = json.loads(path.read_text(encoding="utf-8"))
    if amendment.get("status") != "frozen_before_corrected_metric_facets_outputs":
        raise ValueError("Method amendment is not frozen before corrected outputs")
    correction = amendment.get("correction") or {}
    if (
        correction.get("verification_limit") != METADATA_GENRE_LIMIT
        or correction.get("feature_limit") != FACET_SUBJECT_FEATURE_LIMIT
    ):
        raise ValueError("Method amendment genre limits do not match this evaluator")
    return sha256_file(path)


def _rich_transport_amendment_sha256(
    path: Path | None,
    *,
    views_path: Path,
    rich_features_sha256: str,
    primary_amendment_sha256: str | None,
) -> str | None:
    if path is None:
        return None
    amendment = json.loads(path.read_text(encoding="utf-8"))
    if amendment.get("status") != "frozen_before_corrected_rich_transport_outputs":
        raise ValueError("Rich transport amendment is not frozen before corrected outputs")
    if amendment.get("rich_views_sha256") != sha256_file(views_path):
        raise ValueError("Rich transport amendment references different frozen views")
    if amendment.get("rich_features_sha256") != rich_features_sha256:
        raise ValueError("Rich transport amendment references different rich features")
    if amendment.get("primary_method_amendment_sha256") != primary_amendment_sha256:
        raise ValueError("Rich transport amendment references a different primary amendment")
    return sha256_file(path)


def _verified_field_provenance(
    read: dict[str, Any],
    original: dict[str, Any] | None,
    supplemented: dict[str, Any] | None,
    field_rows: Sequence[dict[str, Any]],
    field: str,
) -> dict[str, Any] | None:
    """Return source data only when identity, value hash, and provider agree."""
    if original is None or supplemented is None:
        return None
    read_id = int(read["id"])
    if int(original.get("read_id", -1)) != read_id or int(supplemented.get("read_id", -1)) != read_id:
        return None
    try:
        if str(original.get("identity_hash") or "") != _read_metadata_identity_hash(read):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    raw_value = original.get(field)
    if not raw_value:
        return None
    field_info = ((supplemented.get("metadata_provenance") or {}).get("fields") or {}).get(field)
    if not isinstance(field_info, dict):
        return None
    if field == "description":
        value = str(raw_value)
        value_digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        if original.get("description_kind") != "synopsis" or field_info.get("kind") != "synopsis":
            return None
    elif field == "genres":
        value = normalize_subjects(raw_value, limit=METADATA_GENRE_LIMIT)
        value_digest = hashlib.sha256(json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
    else:
        raise ValueError("Unsupported grounded field")
    if field_info.get("value_sha256") != value_digest:
        return None

    verified_rows = [
        row for row in field_rows
        if row.get("entity_type") == "read"
        and int(row.get("entity_id", -1)) == read_id
        and row.get("field") == field
        and float(row.get("confidence", 0.0) or 0.0) >= 1.0
    ]
    if field == "genres":
        possible_sources = field_info.get("sources")
        possible_sources = possible_sources if isinstance(possible_sources, list) else [field_info]
    else:
        possible_sources = [field_info]
    matches = [
        source for source in possible_sources
        if isinstance(source, dict)
        and source.get("provider")
        and source.get("provider_id")
        and any(
            row.get("provider") == source.get("provider")
            and str(row.get("provider_id") or "") == str(source.get("provider_id") or "")
            for row in verified_rows
        )
    ]
    if not matches:
        return None
    source = matches[0]
    return {
        "source": str(source["provider"]),
        "source_reference": str(source["provider"]) + ":" + str(source["provider_id"]),
        "source_field": str(source.get("source_field") or field),
        "document_sha256": value_digest,
        "kind": "synopsis" if field == "description" else "verified_catalog_subjects",
    }


def _grounded_facets_for_records(corpus: dict[str, Any], supplement_path: Path, records):
    originals = {int(row["read_id"]): row for row in corpus.get("read_metadata", [])}
    supplement = json.loads(supplement_path.read_text(encoding="utf-8"))
    supplemented = {int(row["read_id"]): row for row in supplement.get("read_metadata", [])}
    provenance_rows = supplement.get("metadata_field_provenance", [])
    by_read: dict[int, list[dict[str, Any]]] = {}
    for row in provenance_rows:
        if row.get("entity_type") == "read":
            by_read.setdefault(int(row["entity_id"]), []).append(row)
    if set(originals) != set(supplemented):
        raise ValueError("Frozen metadata and provenance supplement do not align")
    for read_id, original in originals.items():
        supplemented_row = supplemented[read_id]
        if any(supplemented_row.get(key) != value for key, value in original.items()):
            raise ValueError("Frozen metadata values differ from provenance supplement")

    facets, evidence = [], []
    description_count = subject_count = 0
    for _, read, _ in records:
        read_id = int(read["id"])
        original = originals.get(read_id)
        supplemented_row = supplemented.get(read_id)
        rows = by_read.get(read_id, [])
        description_provenance = _verified_field_provenance(
            read, original, supplemented_row, rows, "description")
        subjects_provenance = _verified_field_provenance(
            read, original, supplemented_row, rows, "genres")
        description_count += bool(description_provenance)
        subject_count += bool(subjects_provenance)
        facet = extract_grounded_facets(
            description=original.get("description") if original else None,
            description_provenance=description_provenance,
            subjects=(normalize_subjects(original.get("genres"), limit=FACET_SUBJECT_FEATURE_LIMIT)
                      if original and subjects_provenance else None),
            subjects_provenance=subjects_provenance,
        )
        facets.append(facet)
        # This full evidence archive is private by construction and preserves
        # exact snippets, offsets, and source references for audit.
        evidence.append({"read_id": read_id, **facet})
    counts = {
        "metadata_record_count": len(originals),
        "field_provenance_record_count": len(provenance_rows),
        "eligible_verified_synopsis_count": int(description_count),
        "eligible_verified_subject_count": int(subject_count),
        "identity_and_value_hash_checks": "passed for every consumed field",
    }
    return facets, evidence, counts


def _candidate_grids():
    metric = [
        {"family": "metric", "name": _metric_name(k, strength),
         "components": k, "strength": strength, "weight": None}
        for k in METRIC_COMPONENTS for strength in METRIC_LAMBDAS
    ]
    facets = [
        {"family": "facets", "name": _facet_name(weight),
         "components": None, "strength": 0.0, "weight": weight}
        for weight in FACET_BLEND_WEIGHTS
    ]
    joint = [
        {"family": "joint", "name": _joint_name(k, strength, weight),
         "components": k, "strength": strength, "weight": weight}
        for k in METRIC_COMPONENTS for strength in METRIC_LAMBDAS
        for weight in FACET_BLEND_WEIGHTS
    ]
    return metric + facets + joint


def _metric_name(k: int, strength: float) -> str:
    return f"metric_k{k}_lambda{strength:.3f}"


def _facet_name(weight: float) -> str:
    return f"facets_w{weight:.3f}"


def _joint_name(k: int, strength: float, weight: float) -> str:
    return f"joint_k{k}_lambda{strength:.3f}_w{weight:.3f}"


def _metric_similarity_for(vectors, reference, direction, strength):
    if strength == 0.0:
        return reference
    transformed = metric_vectors(vectors, direction, strength)
    return cosine_similarity_matrix(transformed)


def _score_targets(records, target_indexes, similarity, context=None):
    return score_records_from_similarity(
        records, target_indexes, similarity, context=context)


def _candidate_similarity(config, vectors, reference, direction, facet, observed):
    if config["components"] is None:
        similarity = reference
    else:
        similarity = _metric_similarity_for(
            vectors, reference, direction, float(config["strength"]))
    if config["weight"] is not None:
        similarity = blend_facet_similarity(
            similarity, facet, observed, float(config["weight"]))
    return similarity


def _fit_directions(eligible_vectors, ratings, train_stop):
    train = np.arange(int(train_stop), dtype=np.int64)
    return {
        k: fit_supervised_metric(
            eligible_vectors, ratings, train, components=k, ridge_alpha=METRIC_RIDGE_ALPHA)
        for k in METRIC_COMPONENTS
    }


def _fold_facet_matrix(facets, eligible_record_indexes, train_stop):
    train_records = eligible_record_indexes[:int(train_stop)]
    return facet_similarity_matrix(facets, train_records)


def _score_grid_for_fit(records, full_vectors, reference, facets, ratings,
                        eligible_vectors, eligible_record_indexes, train_stop,
                        target_positions, configs, scoring_context=None):
    directions = _fit_directions(eligible_vectors, ratings, train_stop)
    facet, observed, facet_summary = _fold_facet_matrix(
        facets, eligible_record_indexes, train_stop)
    targets = eligible_record_indexes[np.asarray(target_positions, dtype=np.int64)]
    scores = {}
    similarity_cache = {}
    for config in configs:
        k = config["components"]
        direction = None if k is None else directions[k]["direction"]
        key = (k, float(config["strength"]))
        base_similarity = similarity_cache.get(key)
        if base_similarity is None:
            base_similarity = _metric_similarity_for(
                full_vectors, reference, direction, float(config["strength"]))
            similarity_cache[key] = base_similarity
        similarity = (
            base_similarity if config["weight"] is None else blend_facet_similarity(
                base_similarity, facet, observed, float(config["weight"]))
        )
        scores[config["name"]] = _score_targets(
            records, targets, similarity, scoring_context)
    return scores, directions, facet, observed, facet_summary


def _select_by_oof(configs, score_arrays, ratings, folds):
    summaries = {}
    chosen = {}
    for family in ("metric", "facets", "joint"):
        candidates = []
        for config in configs:
            if config["family"] != family:
                continue
            fold_rows = _fold_metrics(ratings, score_arrays[config["name"]], folds)
            mean_auc = _mean_balanced(fold_rows)
            summaries[config["name"]] = {
                "family": family,
                "folds": fold_rows,
                "mean_fold_balanced_auc": mean_auc,
                "development_only": True,
            }
            complexity = (
                float(config["strength"]),
                float(config["weight"] or 0.0),
                int(config["components"] or 0),
            )
            candidates.append(((-mean_auc, *complexity, config["name"]), config))
        if not candidates:
            raise ValueError("No candidates found for a registered family")
        candidates.sort(key=lambda item: item[0])
        chosen[family] = candidates[0][1]
    return chosen, summaries


def _prediction_arrays(configs, count):
    return {config["name"]: np.full(count, np.nan, dtype=np.float64) for config in configs}


def _assign_slice(arrays, values_by_name, begin, end):
    for name, values in values_by_name.items():
        if len(values) != end - begin:
            raise ValueError("Model returned a score vector with the wrong target count")
        arrays[name][begin:end] = values


def _signal_zscores(features, name, train_stop, evaluation_indexes, *, first_fold=False):
    values = np.asarray(features[name], dtype=np.float64)
    training = values[:int(train_stop)]
    finite = training[np.isfinite(training)]
    if name == "ordinal_expected" and not len(finite) and first_fold:
        # The frozen ordinal archive intentionally has no predictions for its
        # first fit prefix. Use a prefix-only fitted model to get normalization
        # statistics; never backfill or score those rows.
        import evaluate_ordinal_interests as ordinal
        fitted = ordinal.fit_ordinal(
            features["base_features"][:int(train_stop)],
            features["ratings"][:int(train_stop)],
        )
        finite = np.asarray(ordinal.ordinal_expected_ratings(
            fitted, features["base_features"][:int(train_stop)]), dtype=np.float64)
        finite = finite[np.isfinite(finite)]
    if not len(finite):
        raise ValueError("No finite training values for a predeclared signal")
    mean, sd = float(finite.mean()), float(finite.std())
    if not np.isfinite(values[np.asarray(evaluation_indexes, dtype=np.int64)]).all():
        raise ValueError("A predeclared signal has missing evaluation values")
    evaluated = values[np.asarray(evaluation_indexes, dtype=np.int64)]
    z = np.zeros_like(evaluated) if sd <= 1e-12 else (evaluated - mean) / sd
    return z, {"mean": mean, "sd": sd, "training_n": int(len(finite))}


def _prior_signal_scores(features, selected_aligned, alignment_records, folds):
    count = len(features["ratings"])
    signals = ("recency", "cluster3", "ordinal_expected")
    result = {
        f"{family}_plus_{signal}": np.full(count, np.nan, dtype=np.float64)
        for family in selected_aligned for signal in signals
    }
    result.update({
        f"{family}_plus_recency_cluster3": np.full(count, np.nan, dtype=np.float64)
        for family in selected_aligned
    })
    normalization = {}

    for fold_number, fold in enumerate(folds):
        begin, end = int(fold["evaluation_start"]), int(fold["evaluation_stop"])
        fit_stop = int(fold["train_stop"])
        z_by_signal, stats_by_signal = {}, {}
        evaluation_indexes = np.arange(begin, end)
        for signal in signals:
            z_by_signal[signal], stats_by_signal[signal] = _signal_zscores(
                features, signal, fit_stop, evaluation_indexes,
                first_fold=(fold_number == 0),
            )
        pair_z = z_by_signal["recency"] + z_by_signal["cluster3"]
        for family in selected_aligned:
            fold_key = f"oof_fold_{begin}_{end}"
            scale = alignment_records[family][fold_key]["reference_sd"]
            base = selected_aligned[family][begin:end]
            for signal in signals:
                result[f"{family}_plus_{signal}"][begin:end] = _round_served(
                    base + .25 * scale * z_by_signal[signal])
            result[f"{family}_plus_recency_cluster3"][begin:end] = _round_served(
                base + .25 * scale * pair_z)
            normalization[f"{family}:fold:{fold_number}"] = {
                "training_signals": stats_by_signal,
                "current_reference_sd": scale,
                "recency_cluster3_rule": "sum of separately training-standardized signals",
            }

    for split_name, begin, end, fit_stop in (
        ("validation", int(features["train_mask"].sum()),
         int(features["train_mask"].sum() + features["validation_mask"].sum()),
         int(features["train_mask"].sum())),
        ("later_exploratory", int(features["train_mask"].sum() + features["validation_mask"].sum()),
         count, int(features["train_mask"].sum() + features["validation_mask"].sum())),
    ):
        evaluation_indexes = np.arange(begin, end)
        z_by_signal, stats_by_signal = {}, {}
        for signal in signals:
            z_by_signal[signal], stats_by_signal[signal] = _signal_zscores(
                features, signal, fit_stop, evaluation_indexes)
        pair_z = z_by_signal["recency"] + z_by_signal["cluster3"]
        for family in selected_aligned:
            fit = alignment_records[family]["validation_later_from_oof"]
            scale = fit["reference_sd"]
            base = selected_aligned[family][begin:end]
            for signal in signals:
                result[f"{family}_plus_{signal}"][begin:end] = _round_served(
                    base + .25 * scale * z_by_signal[signal])
            result[f"{family}_plus_recency_cluster3"][begin:end] = _round_served(
                base + .25 * scale * pair_z)
            normalization[f"{family}:{split_name}"] = {
                "training_signals": stats_by_signal,
                "current_reference_sd": scale,
                "scorer_alignment": "development OOF rows",
                "recency_cluster3_rule": "sum of separately training-standardized signals",
            }
    return result, normalization


def evaluate_private_artifacts(
    *,
    corpus_path: Path,
    features_path: Path,
    feature_manifest_path: Path,
    protocol_path: Path,
    provenance_path: Path,
    output_path: Path,
    selection_path: Path,
    method_amendment_path: Path | None = None,
):
    """Run the frozen OOF/validation/later score grid; write private artifacts only."""
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    manifest = json.loads(feature_manifest_path.read_text(encoding="utf-8"))
    if sha256_file(features_path) != manifest.get("feature_artifact_sha256"):
        raise ValueError("Feature archive hash differs from the frozen manifest")
    if sha256_file(corpus_path) != protocol.get("corpus_sha256"):
        raise ValueError("Private corpus hash differs from the frozen protocol")
    if int(protocol.get("version", -1)) != 1:
        raise ValueError("Unsupported frozen protocol version")
    registration = preregistration_metadata()
    method_amendment_sha256 = _method_amendment_sha256(method_amendment_path)
    registration.update({
        "frozen_at_utc": datetime.now(timezone.utc).isoformat(),
        "frozen_protocol_sha256": sha256_file(protocol_path),
        "corpus_sha256": protocol["corpus_sha256"],
        "registered_before_fitting": True,
        "method_amendment_sha256": method_amendment_sha256,
        "time_cue_correction": (
            "Version 2 removes bare historical/future/contemporary terms; explicit "
            "setting and named-era phrases are required. Frozen before this run's fits."
        ),
    })
    registration_path = output_path.with_name("metric-facets-registration-private.json")
    if registration_path.exists():
        prior_registration = json.loads(registration_path.read_text(encoding="utf-8"))
        if (
            prior_registration.get("frozen_protocol_sha256") != registration["frozen_protocol_sha256"]
            or prior_registration.get("facets") != registration["facets"]
            or prior_registration.get("method_amendment_sha256") != method_amendment_sha256
            or not prior_registration.get("registered_before_fitting")
        ):
            raise ValueError("Existing pre-score registration differs from this fixed evaluator")
        registration = prior_registration
    else:
        _save_private_json(registration_path, registration)

    corpus = load_corpus(corpus_path)
    backend_model = manifest.get("backend_model")
    if not isinstance(backend_model, list) or len(backend_model) != 2:
        raise ValueError("Frozen feature manifest has no selected encoder")
    records, selected_encoder, _ = prepare(corpus, *backend_model)
    if list(selected_encoder) != backend_model:
        raise ValueError("Prepared corpus selected a different encoder")
    with np.load(features_path, allow_pickle=False) as archive:
        features = {name: archive[name].copy() for name in archive.files}
    eligible_record_indexes = np.asarray(features["record_indexes"], dtype=np.int64)
    read_ids = np.asarray(features["read_ids"], dtype=np.int64)
    if len(records) <= int(eligible_record_indexes.max(initial=-1)):
        raise ValueError("Eligible record indexes exceed the prepared corpus")
    if not np.array_equal(read_ids, [int(records[index][1]["id"]) for index in eligible_record_indexes]):
        raise ValueError("Feature archive is not aligned to the prepared corpus")
    if not np.array_equal(features["utc_day"], [
        records[index][0].date().toordinal() for index in eligible_record_indexes
    ]):
        raise ValueError("Feature days differ from the prepared corpus")

    folds = manifest.get("development_folds", [])
    train_stop = int(features["train_mask"].sum())
    val_stop = train_stop + int(features["validation_mask"].sum())
    if len(folds) != 3:
        raise ValueError("Frozen feature manifest must define three development folds")
    recomputed = frozen_development_folds(features["utc_day"][:train_stop], train_stop)
    if folds != recomputed:
        raise ValueError("Frozen development folds are not whole-day causal cuts")
    if int(features["validation_mask"].sum()) == 0 or int(features["later_mask"].sum()) == 0:
        raise ValueError("Frozen validation and later partitions must be nonempty")

    full_vectors = np.stack([record[2] for record in records]).astype(np.float32, copy=False)
    scoring_context = make_scoring_context(records)
    eligible_vectors = full_vectors[eligible_record_indexes]
    ratings = np.asarray(features["ratings"], dtype=np.float64)
    current = np.asarray(features["current"], dtype=np.float64)
    if len(ratings) != len(read_ids) or not np.isfinite(ratings).all():
        raise ValueError("Frozen eligible labels are malformed")
    if not np.isfinite(current).all():
        raise ValueError("Frozen current serving scores must be finite")

    facets, evidence, provenance_summary = _grounded_facets_for_records(
        corpus, provenance_path, records)
    reference_similarity = cosine_similarity_matrix(full_vectors)
    all_target_positions = np.arange(len(eligible_record_indexes), dtype=np.int64)
    baseline_replay = score_records_from_similarity(
        records, eligible_record_indexes, reference_similarity, context=scoring_context)
    baseline_mismatches = int(np.count_nonzero(baseline_replay != current))
    if baseline_mismatches:
        raise ValueError(f"Cosine serving parity failed for {baseline_mismatches} eligible targets")

    configs = _candidate_grids()
    raw_scores = _prediction_arrays(configs, len(read_ids))
    raw_scores["cosine_control"] = baseline_replay
    facet_fit_summaries = []
    with threadpool_limits(limits=1):
        # The three OOF blocks use an expanding fit prefix and are the only
        # labels read by the registered hyperparameter selection.
        for fold in folds:
            begin, end, fit_stop = (
                int(fold["evaluation_start"]),
                int(fold["evaluation_stop"]),
                int(fold["train_stop"]),
            )
            grid, _, _, _, facet_summary = _score_grid_for_fit(
                records, full_vectors, reference_similarity, facets, ratings,
                eligible_vectors, eligible_record_indexes, fit_stop,
                np.arange(begin, end), configs, scoring_context,
            )
            _assign_slice(raw_scores, grid, begin, end)
            facet_fit_summaries.append({
                "train_count": fit_stop,
                "vocabulary_size": facet_summary["training_subject_vocabulary_size"],
                "known_group_documents": facet_summary["documents_with_known_group"],
            })

        chosen, oof_summaries = _select_by_oof(configs, raw_scores, ratings, folds)
        # Keep grid predictions for all frozen candidate configurations in the
        # private archive. Validation/later labels are never used to select.
        for begin, end, fit_stop, split_name in (
            (train_stop, val_stop, train_stop, "validation"),
            (val_stop, len(read_ids), val_stop, "later"),
        ):
            grid, _, _, _, _ = _score_grid_for_fit(
                records, full_vectors, reference_similarity, facets, ratings,
                eligible_vectors, eligible_record_indexes, fit_stop,
                np.arange(begin, end), configs, scoring_context,
            )
            _assign_slice(raw_scores, grid, begin, end)

        selected_raw = {}
        selected_aligned = {}
        alignment_records = {}
        for family in ("metric", "facets", "joint"):
            config = chosen[family]
            selected_raw[family] = raw_scores[config["name"]].copy()
            selected_aligned[family] = np.full(len(read_ids), np.nan, dtype=np.float64)
            alignment_records[family] = {}

        # Fold-specific OOF alignment is estimated on that model's own
        # training-prefix predictions, then applied only to that fold's targets.
        for fold in folds:
            begin, end, fit_stop = (
                int(fold["evaluation_start"]),
                int(fold["evaluation_stop"]),
                int(fold["train_stop"]),
            )
            chosen_configs = [chosen[name] for name in ("metric", "facets", "joint")]
            train_grid, _, _, _, _ = _score_grid_for_fit(
                records, full_vectors, reference_similarity, facets, ratings,
                eligible_vectors, eligible_record_indexes, fit_stop,
                np.arange(0, fit_stop), chosen_configs, scoring_context,
            )
            reference_train = current[:fit_stop]
            for family in ("metric", "facets", "joint"):
                config = chosen[family]
                fit = _fit_alignment(train_grid[config["name"]], reference_train)
                selected_aligned[family][begin:end] = _apply_alignment(
                    selected_raw[family][begin:end], fit)
                alignment_records[family][f"oof_fold_{begin}_{end}"] = fit

        # Validation and later are aligned using only the selected model's OOF
        # rows. The already-inspected later split is descriptive output only.
        oof_mask = np.asarray(features["oof_selection_mask"], dtype=bool)
        if not oof_mask.any() or not np.isfinite(current[oof_mask]).all():
            raise ValueError("Frozen OOF selection rows are missing")
        for family in ("metric", "facets", "joint"):
            fit = _fit_alignment(selected_raw[family][oof_mask], current[oof_mask])
            alignment_records[family]["validation_later_from_oof"] = fit
            for split_mask in (features["validation_mask"], features["later_mask"]):
                selected_aligned[family][split_mask] = _apply_alignment(
                    selected_raw[family][split_mask], fit)

        prior_scores, prior_normalization = _prior_signal_scores(
            features, selected_aligned, alignment_records, folds)

    # Candidate score arrays are private identity-aligned artifacts. Only
    # aggregate fold metrics enter the separate selection manifest.
    score_archive = {
        f"score__{name}": values for name, values in raw_scores.items()
        if name != "cosine_control"
    }
    score_archive["cosine_control"] = raw_scores["cosine_control"]
    score_archive.update({f"score__{name}": values for name, values in prior_scores.items()})
    score_archive.update({
        "read_ids": read_ids,
        "utc_day": np.asarray(features["utc_day"], dtype=np.int64),
        "train_mask": np.asarray(features["train_mask"], dtype=bool),
        "validation_mask": np.asarray(features["validation_mask"], dtype=bool),
        "later_mask": np.asarray(features["later_mask"], dtype=bool),
        "oof_selection_mask": np.asarray(features["oof_selection_mask"], dtype=bool),
        "protocol_sha256": np.asarray(sha256_file(protocol_path)),
        "corpus_sha256": np.asarray(protocol["corpus_sha256"]),
        "features_sha256": np.asarray(sha256_file(features_path)),
        "feature_manifest_sha256": np.asarray(sha256_file(feature_manifest_path)),
        "selected_metric": selected_raw["metric"],
        "selected_facets": selected_raw["facets"],
        "selected_joint": selected_raw["joint"],
        "aligned_metric": selected_aligned["metric"],
        "aligned_facets": selected_aligned["facets"],
        "aligned_joint": selected_aligned["joint"],
    })
    output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output_path.parent, 0o700)
    np.savez_compressed(output_path, **score_archive)
    os.chmod(output_path, 0o600)
    evidence_path = output_path.with_name("metric-facets-evidence-private.json")
    _save_private_json(evidence_path, {
        "version": FACET_VERSION,
        "corpus_sha256": protocol["corpus_sha256"],
        "features_sha256": manifest["feature_artifact_sha256"],
        "records": evidence,
    })

    selection = {
        "protocol_sha256": sha256_file(protocol_path),
        "method_amendment_sha256": method_amendment_sha256,
        "features_sha256": sha256_file(features_path),
        "score_archive_sha256": sha256_file(output_path),
        "selection_metric": "mean balanced high/low AUC across three expanding whole-day OOF folds",
        "development_only": True,
        "candidate_folds": oof_summaries,
        "selected": {
            family: {
                "name": chosen[family]["name"],
                "components": chosen[family]["components"],
                "strength": chosen[family]["strength"],
                "facet_weight": chosen[family]["weight"],
                "mean_fold_balanced_auc": oof_summaries[chosen[family]["name"]]["mean_fold_balanced_auc"],
            }
            for family in ("metric", "facets", "joint")
        },
        "facet_fit_coverage": facet_fit_summaries,
        "provenance_coverage": provenance_summary,
        "facet_registration_sha256": sha256_file(registration_path),
        "facet_registration_path": str(registration_path),
        "serving_parity": {"eligible_targets": len(read_ids), "mismatches": baseline_mismatches},
        "alignment": alignment_records,
        "prior_signal_normalization": prior_normalization,
        "score_archive": str(output_path),
        "evidence_archive": str(evidence_path),
    }
    _save_private_json(selection_path, selection)
    return {
        "eligible_targets": len(read_ids),
        "candidate_arms": len(configs) + 1,
        "score_archive_sha256": sha256_file(output_path),
        "selection_sha256": sha256_file(selection_path),
        "evidence_archive_sha256": sha256_file(evidence_path),
        "serving_parity_mismatches": baseline_mismatches,
        "selected_metric": chosen["metric"]["name"],
        "selected_facets": chosen["facets"]["name"],
        "selected_joint": chosen["joint"]["name"],
        "development_only_selection": True,
    }


def evaluate_rich_private_artifacts(
    *,
    corpus_path: Path,
    features_path: Path,
    feature_manifest_path: Path,
    protocol_path: Path,
    provenance_path: Path,
    views_path: Path,
    output_path: Path,
    selection_path: Path,
    method_amendment_path: Path | None = None,
    rich_amendment_path: Path | None = None,
):
    """Transport primary OOF-selected models to verified rich query/history vectors."""
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    manifest = json.loads(feature_manifest_path.read_text(encoding="utf-8"))
    rich_manifest_path = features_path.with_name("rich-features-manifest-private.json")
    rich_features_path = features_path.with_name("rich-features-private.npz")
    rich_manifest = json.loads(rich_manifest_path.read_text(encoding="utf-8"))
    if sha256_file(features_path) != manifest.get("feature_artifact_sha256"):
        raise ValueError("Primary feature archive hash differs from the frozen manifest")
    if sha256_file(rich_features_path) != rich_manifest.get("feature_artifact_sha256"):
        raise ValueError("Rich feature archive hash differs from its manifest")
    if sha256_file(views_path) != rich_manifest.get("frozen_views_sha256"):
        raise ValueError("Rich vector archive hash differs from its manifest")
    if sha256_file(corpus_path) != protocol.get("corpus_sha256"):
        raise ValueError("Private corpus hash differs from the frozen protocol")
    if not rich_manifest.get("original_vector_byte_equality"):
        raise ValueError("Rich bundle did not verify original history vectors")
    if rich_manifest.get("query_control") != "same rich query with original title-author history":
        raise ValueError("Rich bundle does not define the matched-query control")

    corpus = load_corpus(corpus_path)
    records, _, _ = prepare(corpus, *manifest["backend_model"])
    with np.load(features_path, allow_pickle=False) as archive:
        base_features = {name: archive[name].copy() for name in archive.files}
    with np.load(rich_features_path, allow_pickle=False) as archive:
        rich_features = {name: archive[name].copy() for name in archive.files}
    with np.load(views_path, allow_pickle=False) as archive:
        view_ids = np.asarray(archive["read_ids"], dtype=np.int64)
        if len(np.unique(view_ids)) != len(view_ids):
            raise ValueError("Rich view IDs are not unique")
        view_lookup = {int(value): index for index, value in enumerate(view_ids)}
        try:
            view_indexes = np.asarray([
                view_lookup[int(record[1]["id"])] for record in records
            ], dtype=np.int64)
        except KeyError as exc:
            raise ValueError("Rich views do not cover the prepared corpus") from exc
        history_rich = np.asarray(archive["read_full"][view_indexes], dtype=np.float32)
        query_rich = np.asarray(archive["query_new_full"][view_indexes], dtype=np.float32)
        history_actual = np.asarray(archive["read_actual_ta"][view_indexes], dtype=np.float32)
        full_coverage = np.asarray(archive["full_mask"][view_indexes], dtype=bool)
    original_actual = np.stack([record[2] for record in records]).astype(np.float32, copy=False)
    if not np.array_equal(history_actual, original_actual):
        raise ValueError("Rich bundle changed an original history vector")
    if not np.isfinite(history_rich).all() or not np.isfinite(query_rich).all():
        raise ValueError("Rich vector views contain nonfinite values")
    eligible_record_indexes = np.asarray(base_features["record_indexes"], dtype=np.int64)
    read_ids = np.asarray(base_features["read_ids"], dtype=np.int64)
    if not np.array_equal(read_ids, rich_features["read_ids"]):
        raise ValueError("Rich and primary feature archives are not identity-aligned")
    if not np.array_equal(read_ids, [int(records[index][1]["id"]) for index in eligible_record_indexes]):
        raise ValueError("Rich feature target indexes do not match the prepared corpus")
    if not np.array_equal(base_features["train_mask"], rich_features["train_mask"]) or not np.array_equal(
        base_features["validation_mask"], rich_features["validation_mask"]
    ) or not np.array_equal(base_features["later_mask"], rich_features["later_mask"]):
        raise ValueError("Rich and primary split masks differ")

    chosen_path = output_path.with_name("metric-facets-selection-private.json")
    primary_selection = json.loads(chosen_path.read_text(encoding="utf-8"))
    method_amendment_sha256 = _method_amendment_sha256(method_amendment_path)
    if primary_selection.get("method_amendment_sha256") != method_amendment_sha256:
        raise ValueError("Rich transport amendment differs from primary selection amendment")
    rich_transport_amendment_sha256 = _rich_transport_amendment_sha256(
        rich_amendment_path,
        views_path=views_path,
        rich_features_sha256=rich_manifest["feature_artifact_sha256"],
        primary_amendment_sha256=method_amendment_sha256,
    )
    master_selection_path = output_path.with_name("master-selection-private.json")
    if not master_selection_path.exists() or (
        sha256_file(master_selection_path) != LOCKED_MASTER_SELECTION_SHA256
    ):
        raise ValueError("Rich reporting requires the frozen master OOF selection lock")
    configs = {}
    for family, item in primary_selection["selected"].items():
        configs[family] = {
            "family": family,
            "name": item["name"],
            "components": item["components"],
            "strength": float(item["strength"]),
            "weight": item["facet_weight"],
        }
    if set(configs) != {"metric", "facets", "joint"}:
        raise ValueError("Primary OOF selection lacks one of the metric/facet families")

    facets, evidence, provenance_summary = _grounded_facets_for_records(
        corpus, provenance_path, records)
    context = make_scoring_context(records)
    reference_rich = causal_cross_similarity_matrix(
        records, query_rich, history_rich, context)
    reference_matched = causal_cross_similarity_matrix(
        records, query_rich, original_actual, context)
    baseline_rich = score_records_from_similarity(
        records, eligible_record_indexes, reference_rich, context=context)
    baseline_matched = score_records_from_similarity(
        records, eligible_record_indexes, reference_matched, context=context)
    if not np.array_equal(baseline_rich, rich_features["current"]):
        mismatches = int(np.count_nonzero(baseline_rich != rich_features["current"]))
        raise ValueError(f"Rich current parity failed for {mismatches} targets")
    if not np.array_equal(baseline_matched, rich_features["matched_query_control"]):
        mismatches = int(np.count_nonzero(
            baseline_matched != rich_features["matched_query_control"]))
        raise ValueError(f"Matched-query control parity failed for {mismatches} targets")

    ratings = np.asarray(base_features["ratings"], dtype=np.float64)
    rich_query_eligible = query_rich[eligible_record_indexes]
    scores = {name: np.full(len(read_ids), np.nan, dtype=np.float64)
              for name in configs}
    folds = manifest["development_folds"]

    def score_config(config, directions, facet, observed, target_positions):
        target_indexes = eligible_record_indexes[np.asarray(target_positions, dtype=np.int64)]
        if config["components"] is None:
            similarity = reference_rich
        else:
            direction = directions[int(config["components"])]["direction"]
            q_metric = metric_vectors(query_rich, direction, config["strength"])
            h_metric = metric_vectors(history_rich, direction, config["strength"])
            similarity = causal_cross_similarity_matrix(
                records, q_metric, h_metric, context)
        if config["weight"] is not None:
            similarity = blend_facet_similarity(
                similarity, facet, observed, float(config["weight"]))
        return _score_targets(records, target_indexes, similarity, context)

    with threadpool_limits(limits=1):
        for fold in folds:
            begin, end, fit_stop = (
                int(fold["evaluation_start"]),
                int(fold["evaluation_stop"]),
                int(fold["train_stop"]),
            )
            directions = _fit_directions(rich_query_eligible, ratings, fit_stop)
            facet, observed, _ = _fold_facet_matrix(
                facets, eligible_record_indexes, fit_stop)
            for family, config in configs.items():
                scores[family][begin:end] = score_config(
                    config, directions, facet, observed, np.arange(begin, end))
        train_stop = int(base_features["train_mask"].sum())
        validation_stop = train_stop + int(base_features["validation_mask"].sum())
        for begin, end, fit_stop in (
            (train_stop, validation_stop, train_stop),
            (validation_stop, len(read_ids), validation_stop),
        ):
            directions = _fit_directions(rich_query_eligible, ratings, fit_stop)
            facet, observed, _ = _fold_facet_matrix(
                facets, eligible_record_indexes, fit_stop)
            for family, config in configs.items():
                scores[family][begin:end] = score_config(
                    config, directions, facet, observed, np.arange(begin, end))

    archive = {
        "read_ids": read_ids,
        "utc_day": np.asarray(base_features["utc_day"], dtype=np.int64),
        "train_mask": np.asarray(base_features["train_mask"], dtype=bool),
        "validation_mask": np.asarray(base_features["validation_mask"], dtype=bool),
        "later_mask": np.asarray(base_features["later_mask"], dtype=bool),
        "oof_selection_mask": np.asarray(base_features["oof_selection_mask"], dtype=bool),
        "protocol_sha256": np.asarray(sha256_file(protocol_path)),
        "features_sha256": np.asarray(sha256_file(features_path)),
        "rich_features_sha256": np.asarray(sha256_file(rich_features_path)),
        "rich_views_sha256": np.asarray(sha256_file(views_path)),
        "rich_current": np.asarray(rich_features["current"], dtype=np.float64),
        "matched_query_control": np.asarray(
            rich_features["matched_query_control"], dtype=np.float64),
        "full_vector_coverage": full_coverage[eligible_record_indexes],
    }
    archive.update({f"score__rich_{family}": values for family, values in scores.items()})
    output_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output_path.parent, 0o700)
    np.savez_compressed(output_path, **archive)
    os.chmod(output_path, 0o600)
    evidence_path = output_path.with_name("metric-facets-rich-evidence-private.json")
    _save_private_json(evidence_path, {
        "version": FACET_VERSION,
        "corpus_sha256": protocol["corpus_sha256"],
        "records": evidence,
    })

    aggregate = {"controls": {}, "selected_primary_models": {}, "fold_oof_balanced_auc": {},
                 "coverage": int(full_coverage[eligible_record_indexes].sum()),
                 "eligible_targets": len(read_ids),
                 "rich_query_history_parity_mismatches": 0,
                 "matched_query_control_parity_mismatches": 0,
                 "provenance_coverage": provenance_summary,
                 "selection_note": (
                     "Primary OOF-selected hyperparameters transported without rich-view tuning; "
                     "validation and previously inspected later period are descriptive only."
                 )}
    y = ratings
    for split, mask in (
        ("validation", np.asarray(base_features["validation_mask"], dtype=bool)),
        ("later_exploratory", np.asarray(base_features["later_mask"], dtype=bool)),
    ):
        aggregate["controls"][split] = {
            "rich_current": _metrics(y[mask], baseline_rich[mask]),
            "matched_query_control": _metrics(y[mask], baseline_matched[mask]),
        }
        aggregate["selected_primary_models"][split] = {}
        for family, values in scores.items():
            measure = _metrics(y[mask], values[mask])
            aggregate["selected_primary_models"][split][family] = {
                "metrics": measure,
                "delta_vs_rich_current": measure["balanced_auc"]
                    - aggregate["controls"][split]["rich_current"]["balanced_auc"],
                "delta_vs_matched_query_control": measure["balanced_auc"]
                    - aggregate["controls"][split]["matched_query_control"]["balanced_auc"],
            }
    for family, values in scores.items():
        aggregate["fold_oof_balanced_auc"][family] = [
            _metrics(y[int(f["evaluation_start"]):int(f["evaluation_stop"])],
                     values[int(f["evaluation_start"]):int(f["evaluation_stop"])])["balanced_auc"]
            for f in folds
        ]
    selection = {
        "protocol_sha256": sha256_file(protocol_path),
        "method_amendment_sha256": method_amendment_sha256,
        "rich_transport_amendment_sha256": rich_transport_amendment_sha256,
        "primary_score_archive_sha256": primary_selection["score_archive_sha256"],
        "rich_features_sha256": rich_manifest["feature_artifact_sha256"],
        "rich_views_sha256": rich_manifest["frozen_views_sha256"],
        "selected_primary_parameters": configs,
        "selection_locked_on_primary_oof": True,
        "aggregate": aggregate,
        "score_archive": str(output_path),
        "score_archive_sha256": sha256_file(output_path),
        "evidence_archive": str(evidence_path),
        "evidence_archive_sha256": sha256_file(evidence_path),
    }
    _save_private_json(selection_path, selection)
    return {
        "eligible_targets": len(read_ids),
        "full_vector_coverage": int(full_coverage[eligible_record_indexes].sum()),
        "rich_score_archive_sha256": sha256_file(output_path),
        "rich_report_sha256": sha256_file(selection_path),
        "rich_parity_mismatches": 0,
        "selection_locked_on_primary_oof": True,
    }


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--features", type=Path, required=True)
    parser.add_argument("--feature-manifest", type=Path, required=True)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--rich-views", type=Path)
    parser.add_argument("--method-amendment", type=Path)
    parser.add_argument("--rich-amendment", type=Path)
    args = parser.parse_args()
    protocol_digest = sha256_file(args.protocol)
    if protocol_digest != FROZEN_PROTOCOL_SHA256:
        raise ValueError("Protocol file does not match the frozen preregistration")
    if args.rich_views:
        result = evaluate_rich_private_artifacts(
            corpus_path=args.corpus,
            features_path=args.features,
            feature_manifest_path=args.feature_manifest,
            protocol_path=args.protocol,
            provenance_path=args.provenance,
            views_path=args.rich_views,
            output_path=args.output,
            selection_path=args.selection,
            method_amendment_path=args.method_amendment,
            rich_amendment_path=args.rich_amendment,
        )
    else:
        result = evaluate_private_artifacts(
            corpus_path=args.corpus,
            features_path=args.features,
            feature_manifest_path=args.feature_manifest,
            protocol_path=args.protocol,
            provenance_path=args.provenance,
            output_path=args.output,
            selection_path=args.selection,
            method_amendment_path=args.method_amendment,
        )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
