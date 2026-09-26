"""Bounded anti-redundancy swaps for the first discovery recommendations.

This layer leaves the ranker's scores intact. It only swaps a redundant item
with a close-scoring candidate a few positions later, and every moved item
stays within the configured displacement bound. Cached candidate vectors are
used when available; the request path never generates embeddings.
"""

from __future__ import annotations

import math
import unicodedata
from collections.abc import Iterable, Mapping, MutableMapping
from typing import Any

import numpy as np


POLICY_VERSION = "discovery-slate-v1"
DEFAULT_SLATE_SIZE = 8
MAX_DISPLACEMENT = 3
MAX_SCORE_SACRIFICE = 5.0
MAX_BOOKS_PER_AUTHOR = 2
NEAR_DUPLICATE_COSINE = 0.90


def empty_diagnostics(reason: str) -> dict[str, Any]:
    """Return aggregate run metadata without reader or candidate details."""

    return {
        "policy_version": POLICY_VERSION,
        "scope": "recommended_only",
        "applied": False,
        "reason": reason,
        "slate_size": 0,
        "max_displacement": MAX_DISPLACEMENT,
        "max_score_sacrifice": MAX_SCORE_SACRIFICE,
        "max_books_per_author": MAX_BOOKS_PER_AUTHOR,
        "near_duplicate_cosine": NEAR_DUPLICATE_COSINE,
        "author_repeat_swaps": 0,
        "similarity_swaps": 0,
        "reordered_items": 0,
    }


def _author_key(item: Mapping[str, Any]) -> str:
    author = unicodedata.normalize("NFKC", str(item.get("author") or ""))
    return " ".join(author.casefold().split())


def _score(item: Mapping[str, Any]) -> float:
    try:
        value = float(item.get("score") or 0)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def _normalized_vector(vectors: Mapping[int, Any], item: Mapping[str, Any]):
    try:
        raw = vectors.get(int(item["id"]))
        vector = np.asarray(raw, dtype=np.float32)
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if vector.ndim != 1 or vector.size == 0 or not np.isfinite(vector).all():
        return None
    norm = float(np.linalg.norm(vector))
    if not math.isfinite(norm) or norm <= 0:
        return None
    return vector / norm


def _similarity(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    vectors: Mapping[int, Any],
) -> float | None:
    left_vector = _normalized_vector(vectors, left)
    right_vector = _normalized_vector(vectors, right)
    if (
        left_vector is None
        or right_vector is None
        or left_vector.shape != right_vector.shape
    ):
        return None
    value = float(np.dot(left_vector, right_vector))
    return value if math.isfinite(value) else None


def _redundancy(
    candidate: Mapping[str, Any],
    selected: list[Mapping[str, Any]],
    vectors: Mapping[int, Any],
) -> tuple[int, int]:
    author = _author_key(candidate)
    author_count = sum(bool(author) and _author_key(item) == author for item in selected)
    author_overflow = int(author_count >= MAX_BOOKS_PER_AUTHOR)
    similar_pairs = sum(
        (similarity := _similarity(candidate, item, vectors)) is not None
        and similarity >= NEAR_DUPLICATE_COSINE
        for item in selected
    )
    return author_overflow, int(similar_pairs)


def diversify_discovery_slate(
    recommendations: Iterable[MutableMapping[str, Any]],
    candidate_vectors: Mapping[int, Any] | None = None,
    *,
    slate_size: int = DEFAULT_SLATE_SIZE,
    max_displacement: int = MAX_DISPLACEMENT,
    max_score_sacrifice: float = MAX_SCORE_SACRIFICE,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Reduce third-and-later author repeats and near-duplicate pairs locally.

    Only the first ``slate_size`` output positions are considered. Candidates
    from later positions can swap into that prefix, but no book moves more than
    ``max_displacement`` from its original position. A swap must lower
    redundancy against the already selected prefix and lose no more than
    ``max_score_sacrifice`` at the current slot. If there is no qualifying
    alternative, the original recommendation remains.
    """

    if isinstance(slate_size, bool) or int(slate_size) < 0:
        raise ValueError("slate_size must be non-negative")
    if isinstance(max_displacement, bool) or int(max_displacement) < 0:
        raise ValueError("max_displacement must be non-negative")
    try:
        max_score_sacrifice = float(max_score_sacrifice)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_score_sacrifice must be non-negative") from exc
    if not math.isfinite(max_score_sacrifice) or max_score_sacrifice < 0:
        raise ValueError("max_score_sacrifice must be non-negative")

    items = [dict(item) for item in recommendations]
    original_positions = list(range(len(items)))
    slate_size = min(int(slate_size), len(items))
    max_displacement = int(max_displacement)
    vectors = candidate_vectors or {}
    diagnostics = empty_diagnostics("no_qualifying_swaps")
    diagnostics.update(
        {
            "slate_size": slate_size,
            "max_displacement": max_displacement,
            "max_score_sacrifice": max_score_sacrifice,
        }
    )

    author_swaps = 0
    similarity_swaps = 0
    for position in range(slate_size):
        prefix = items[:position]
        current_redundancy = _redundancy(items[position], prefix, vectors)
        current_cost = sum(current_redundancy)
        if current_cost == 0:
            continue

        current_score = _score(items[position])
        best: tuple[tuple[int, float, int, int], int, tuple[int, int]] | None = None
        last_position = min(len(items) - 1, position + max_displacement)
        for candidate_position in range(position + 1, last_position + 1):
            moved_forward = original_positions[candidate_position]
            moved_back = original_positions[position]
            if (
                abs(moved_forward - position) > max_displacement
                or abs(moved_back - candidate_position) > max_displacement
            ):
                continue
            candidate = items[candidate_position]
            if _score(candidate) < current_score - max_score_sacrifice:
                continue
            candidate_redundancy = _redundancy(candidate, prefix, vectors)
            candidate_cost = sum(candidate_redundancy)
            if candidate_cost >= current_cost:
                continue
            # Prefer fewer conflicts, then higher score, then the nearest and
            # earliest original candidate for stable deterministic ties.
            key = (
                candidate_cost,
                -_score(candidate),
                candidate_position - position,
                moved_forward,
            )
            if best is None or key < best[0]:
                best = (key, candidate_position, candidate_redundancy)

        if best is None:
            continue
        _, candidate_position, candidate_redundancy = best
        if current_redundancy[0] > candidate_redundancy[0]:
            author_swaps += 1
        if current_redundancy[1] > candidate_redundancy[1]:
            similarity_swaps += 1
        items[position], items[candidate_position] = (
            items[candidate_position],
            items[position],
        )
        original_positions[position], original_positions[candidate_position] = (
            original_positions[candidate_position],
            original_positions[position],
        )

    reordered = sum(
        original_positions[position] != position for position in range(slate_size)
    )
    diagnostics.update(
        {
            "applied": reordered > 0,
            "reason": "bounded_swaps" if reordered else "no_qualifying_swaps",
            "author_repeat_swaps": author_swaps,
            "similarity_swaps": similarity_swaps,
            "reordered_items": reordered,
        }
    )
    return items, diagnostics
