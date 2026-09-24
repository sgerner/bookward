"""Pure, bounded recommendation ranking from read and candidate embeddings."""

from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Sequence

import numpy as np

from .identity import book_identity
from .subjects import normalize_subjects


_BATCH_SIZE = 64
_KERNEL_NEIGHBORS = 40
_KERNEL_POWER = 8
_KERNEL_SCORE_PER_STAR = 10
_KERNEL_FULL_HISTORY = 100
_RECENCY_TIMESCALE_YEARS = 8
_RECENCY_SCORE_PER_STAR = 20


def _read_day(value: object) -> date | None:
    """Parse supported read dates as UTC days; leave unknown dates neutral."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        return value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.strptime(value, "%Y/%m/%d")
            except ValueError:
                try:
                    parsed = parsedate_to_datetime(value)
                except (TypeError, ValueError, IndexError):
                    return None
    else:
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc).date()


def _matrix(vectors: Sequence[Sequence[float]], dimensions: int | None = None) -> np.ndarray:
    """Validate vectors and return row-normalized float32 vectors."""
    if len(vectors) == 0:
        return np.empty((0, dimensions or 0), dtype=np.float32)
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.ndim != 2 or not matrix.shape[1]:
        raise ValueError("embedding vectors must be non-empty, equally sized rows")
    if dimensions is not None and matrix.shape[1] != dimensions:
        raise ValueError("embedding dimensions do not match")
    if not np.isfinite(matrix).all():
        raise ValueError("embedding vectors must contain only finite values")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return np.divide(matrix, norms, out=np.zeros_like(matrix), where=norms != 0)


def _top_weighted(values: np.ndarray, default: float = 0.0) -> np.ndarray:
    if values.shape[1] == 0:
        return np.full(values.shape[0], default, dtype=np.float32)
    count = min(5, values.shape[1])
    top = np.partition(values, -count, axis=1)[:, -count:]
    weights = np.maximum(top, 0) + 1e-4
    return (top * weights).sum(axis=1) / weights.sum(axis=1)


def _metadata_confidence(candidate: dict[str, Any]) -> float:
    """Estimate how much catalog evidence supports the candidate text."""

    identity = candidate.get("catalog_confidence", candidate.get("quality_score"))
    if identity is None:
        # Keep the pure ranker useful for callers without the catalog ledger.
        return 1.0
    try:
        identity_confidence = min(1.0, max(0.0, float(identity)))
    except (TypeError, ValueError):
        identity_confidence = 0.0
    description = " ".join(str(candidate.get("description") or "").split())
    description_confidence = min(1.0, len(description) / 400.0)
    subject_confidence = min(
        1.0, len(normalize_subjects(candidate.get("genres"), limit=8)) / 4.0
    )
    return round(
        0.45 * identity_confidence
        + 0.40 * description_confidence
        + 0.15 * subject_confidence,
        4,
    )


def rank_candidates(
    reads: Sequence[dict[str, Any]],
    read_vectors: Sequence[Sequence[float]],
    candidates: Sequence[dict[str, Any]],
    candidate_vectors: Sequence[Sequence[float]],
) -> list[dict[str, Any]]:
    """Rank candidates using positive/negative neighborhoods and author affinity.

    The returned rows contain the original candidate fields plus ``score`` and
    ``explanation``. Duplicate read identities are deterministically reduced to
    the lowest read id, which keeps repeated imports from overweighting history.
    """
    if len(reads) != len(read_vectors) or len(candidates) != len(candidate_vectors):
        raise ValueError("items and embedding vectors must have equal lengths")
    if not candidates:
        return []
    read_matrix = _matrix(read_vectors)
    dimensions = read_matrix.shape[1]
    candidate_matrix = _matrix(candidate_vectors, dimensions or None)
    unique: dict[str, tuple[dict[str, Any], np.ndarray]] = {}
    for read, vector in zip(reads, read_matrix):
        if not 1 <= float(read.get("rating") or 0) <= 5:
            continue
        identity = book_identity(read.get("title", ""), read.get("author", ""))
        read_id = read.get("id", 0)
        previous = unique.get(identity)
        if previous is None or read_id < previous[0].get("id", 0):
            unique[identity] = (read, vector)
    # Ignore unrated/out-of-range values and resolve similarity ties by read id.
    history = sorted(unique.values(), key=lambda pair: pair[0].get("id", 0))
    positives = [(item, vector) for item, vector in history if float(item.get("rating") or 0) >= 4]
    negatives = [(item, vector) for item, vector in history if 0 < float(item.get("rating") or 0) <= 2]
    all_ratings = history
    rating_values = np.asarray([float(item.get("rating") or 0) for item, _ in all_ratings], dtype=np.float64)
    read_ordinals = np.asarray([day.toordinal() if (day := _read_day(item.get("read_at"))) else np.nan
                                for item, _ in all_ratings], dtype=np.float64)
    pos_matrix = np.stack([v for _, v in positives]) if positives else np.empty((0, dimensions), dtype=np.float32)
    neg_matrix = np.stack([v for _, v in negatives]) if negatives else np.empty((0, dimensions), dtype=np.float32)
    all_matrix = np.stack([v for _, v in all_ratings]) if all_ratings else np.empty((0, dimensions), dtype=np.float32)
    positive_authors = {book_identity("", item.get("author", "")) for item, _ in positives}
    global_mean = float(np.mean([float(item.get("rating") or 0) for item, _ in history])) if history else 3.0
    author_stats: dict[str, list[float]] = {}
    for item, _ in history:
        author_stats.setdefault(book_identity("", item.get("author", "")), []).append(float(item.get("rating") or 0))
    results: list[dict[str, Any]] = []
    for start in range(0, len(candidates), _BATCH_SIZE):
        batch = candidates[start : start + _BATCH_SIZE]
        vectors = candidate_matrix[start : start + len(batch)]
        positive_sim = vectors @ pos_matrix.T if len(pos_matrix) else np.empty((len(batch), 0), dtype=np.float32)
        negative_sim = vectors @ neg_matrix.T if len(neg_matrix) else np.empty((len(batch), 0), dtype=np.float32)
        all_sim = vectors @ all_matrix.T if len(all_matrix) else np.empty((len(batch), 0), dtype=np.float32)
        positive_max = positive_sim.max(axis=1) if positive_sim.shape[1] else np.full(len(batch), .25)
        negative_max = negative_sim.max(axis=1) if negative_sim.shape[1] else np.zeros(len(batch))
        positive_top = _top_weighted(positive_sim, default=.25)
        for offset, candidate in enumerate(batch):
            author = book_identity("", candidate.get("author", ""))
            author_ratings = author_stats.get(author, [])
            if author_ratings:
                author_mean = sum(author_ratings) / len(author_ratings)
                author_delta = (len(author_ratings) * author_mean + 5 * global_mean) / (len(author_ratings) + 5) - global_mean
            else:
                author_delta = 0.0
            if all_sim.shape[1] and np.any(all_sim[offset] > 0):
                neighbors = np.argsort(-all_sim[offset], kind="stable")[:min(_KERNEL_NEIGHBORS, all_sim.shape[1])]
                similarities = np.maximum(all_sim[offset, neighbors].astype(np.float64), 0)
                kernel_weights = (similarities + 1e-8) ** _KERNEL_POWER
                kernel_rating = float(np.dot(kernel_weights, rating_values[neighbors]) / kernel_weights.sum())
                neighbor_days = read_ordinals[neighbors]
                known = np.isfinite(neighbor_days)
                if known.any():
                    # The query-date factor cancels after normalization; only
                    # relative ages among these neighbors affect the rating.
                    ages = np.maximum(0, (neighbor_days[known].max() - neighbor_days) / 365.25)
                    ages[~known] = np.median(ages[known])
                    recent_weights = kernel_weights * np.exp(-ages / _RECENCY_TIMESCALE_YEARS)
                    recent_rating = float(np.dot(recent_weights, rating_values[neighbors]) / recent_weights.sum())
                else:
                    recent_rating = kernel_rating
                local = neighbors[:min(5, len(neighbors))]
                weights = np.maximum(all_sim[offset, local], 0) + 1e-4
                local_rating = float(np.dot(weights, rating_values[local]) / weights.sum())
            else:
                local_rating = 3.0
                kernel_rating = global_mean
                recent_rating = global_mean
            score = 42 + 48 * float(positive_top[offset]) - 24 * float(negative_max[offset]) + 3.5 * (local_rating - 3) + 3.5 * author_delta
            # The historical comparison begins at 100 reads. Ramp in the new
            # term for smaller libraries, where one neighbor is weak evidence.
            kernel_strength = _KERNEL_SCORE_PER_STAR * min(1.0, len(all_ratings) / _KERNEL_FULL_HISTORY)
            score += kernel_strength * (kernel_rating - global_mean)
            recency_strength = _RECENCY_SCORE_PER_STAR * min(1.0, len(all_ratings) / _KERNEL_FULL_HISTORY)
            score += recency_strength * (recent_rating - kernel_rating)
            score += (float(candidate.get("source_weight") or 1) - 1) * 5
            metadata_confidence = _metadata_confidence(candidate)
            # Sparse catalog records produce weak text vectors. Shrink their
            # ranking signal toward a neutral score instead of letting a
            # title-only match dominate the top of discovery.
            score = 50 + metadata_confidence * (score - 50)
            score = max(0.0, min(100.0, score))
            explanation: list[str] = []
            if author in positive_authors:
                if any(rating <= 2 for rating in author_ratings):
                    explanation.append("This author has mixed ratings in your reading history")
                else:
                    explanation.append("You rated a book by this author highly")
            if positive_sim.shape[1] and positive_max[offset] > .25:
                nearest = int(np.argmax(positive_sim[offset]))
                explanation.append(f"Closest highly rated title: {positives[nearest][0].get('title', 'a highly rated book')}")
            if negative_sim.shape[1] and negative_max[offset] > 0:
                nearest = int(np.argmax(negative_sim[offset]))
                explanation.append(f"Reduced for similarity to {negatives[nearest][0].get('title', 'a low-rated book')}")
            if candidate.get("source_name"):
                explanation.append(f"From {candidate['source_name']}")
            if metadata_confidence < 0.65:
                explanation.append(
                    "Ranking confidence reduced because catalog details are sparse"
                )
            results.append(
                {
                    **candidate,
                    "score": round(score, 1),
                    "metadata_confidence": metadata_confidence,
                    "explanation": explanation,
                }
            )
    return results
