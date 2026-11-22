"""Pure, bounded recommendation ranking from read and candidate embeddings."""

import re
from collections.abc import Sequence
from typing import Any

import numpy as np

from .identity import book_identity
from .read_dates import read_day as _read_day
from .subjects import _subject_key, normalize_subjects, subject_display_map


_BATCH_SIZE = 64
_KERNEL_NEIGHBORS = 40
_KERNEL_POWER = 8
_KERNEL_SCORE_PER_STAR = 10
_KERNEL_FULL_HISTORY = 100
_KERNEL_ESS_PRIOR = 5.0
_RECENCY_TIMESCALE_YEARS = 8
_RECENCY_SCORE_PER_STAR = 20
_POSITIVE_EXPLANATION_MIN_SIMILARITY = 0.25
_NEGATIVE_EXPLANATION_MIN_SIMILARITY = 0.25
_NEGATIVE_EXPLANATION_MEDIAN_MARGIN = 0.12
_LEGACY_PERSONALIZATION_PREFIXES = (
    "Raised using your saved, imported, or positively rated books",
    "Raised using books you saved, imported, or positively rated",
)
_PERSONALIZATION_POSITIVE_PREFIX = "Raised using books you shortlisted, added to Librarr, or rated highly"
_PAREN_DETAILS_RE = re.compile(r"^((?:Raised|Lowered) using [^(]+\()([^()]+)(\))$")


def _normalize_feature_token(
    token: str,
    *,
    subject_labels: dict[str, str],
    candidate_author: str,
) -> str:
    stripped = token.strip()
    if stripped.startswith("subject "):
        raw_subject = stripped[len("subject "):].strip()
        key = _subject_key(raw_subject)
        display_subject = subject_labels.get(key)
        if not display_subject:
            if raw_subject and raw_subject == raw_subject.lower() and any(ch.isalpha() for ch in raw_subject):
                display_subject = raw_subject[:1].upper() + raw_subject[1:]
            else:
                display_subject = raw_subject
        return f"subject {display_subject}" if display_subject else stripped
    if stripped.startswith("author "):
        raw_author = stripped[len("author "):].strip()
        if (
            candidate_author
            and " ".join(raw_author.casefold().split()) == " ".join(candidate_author.casefold().split())
        ):
            return f"author {candidate_author}"
        if raw_author and raw_author == raw_author.lower() and any(ch.isalpha() for ch in raw_author):
            return f"author {raw_author.title()}"
        return stripped
    return stripped


def _parse_era_decade(value: object) -> int | None:
    if isinstance(value, (bool, np.bool_)):
        return None
    if isinstance(value, (int, np.integer)):
        year = int(value)
    elif isinstance(value, str) and value.strip():
        match = re.match(r"^(\d{4})", value.strip())
        if not match:
            return None
        year = int(match.group(1))
    else:
        return None
    return (year // 10) * 10 if 1000 <= year <= 2100 else None


def format_era_explanation(year_value: object, *, positive: bool) -> str:
    """Format a plain-language publication-era reason without validation jargon."""
    decade = _parse_era_decade(year_value)
    if positive:
        if decade is not None:
            return f"Matches your high ratings for books from the {decade}s"
        return "Matches a publication era you tend to rate highly"
    if decade is not None:
        return f"Reduced: books from the {decade}s tend to score lower in your reading history"
    return "Reduced: books from this publication era tend to score lower in your reading history"


def is_caveat_explanation(text: str) -> bool:
    """Return True when an explanation string is a negative caveat or confidence warning."""
    lowered = str(text or "").strip().casefold()
    return (
        lowered.startswith("reduced:")
        or lowered.startswith("lowered ")
        or lowered.startswith("ranking confidence reduced")
        or "mixed ratings in your reading history" in lowered
    )


def normalize_explanation_items(
    explanation: object,
    *,
    genres: object = None,
    author: object = None,
    release_date: object = None,
    first_publication_year: object = None,
) -> list[str]:
    """Rewrite stale stored jargon and order positive reasons ahead of source and caveats."""
    if not isinstance(explanation, list):
        return []
    subject_labels = subject_display_map(genres, limit=8) if genres is not None else {}
    candidate_author = str(author or "").strip()
    year_hint = first_publication_year if first_publication_year is not None else release_date

    positives: list[str] = []
    sources: list[str] = []
    serving_notes: list[str] = []
    caveats: list[str] = []
    seen: set[str] = set()

    for raw in explanation:
        item = str(raw or "").strip()
        if not item:
            continue
        lowered_raw = item.casefold()
        if "held-out ratings" in lowered_raw:
            is_negative_era = lowered_raw.startswith("reduced") or bool(
                re.search(r"(?:adjusted\s*)?-\d", lowered_raw)
            )
            item = format_era_explanation(year_hint, positive=not is_negative_era)
        else:
            for legacy_prefix in _LEGACY_PERSONALIZATION_PREFIXES:
                if item.startswith(legacy_prefix):
                    item = _PERSONALIZATION_POSITIVE_PREFIX + item[len(legacy_prefix):]
                    break

        paren_match = _PAREN_DETAILS_RE.match(item)
        if paren_match:
            prefix, raw_details, suffix = paren_match.groups()
            normalized_tokens = [
                _normalize_feature_token(
                    part,
                    subject_labels=subject_labels,
                    candidate_author=candidate_author,
                )
                for part in raw_details.split(",")
            ]
            item = f"{prefix}{', '.join(normalized_tokens)}{suffix}"

        if item in seen:
            continue
        seen.add(item)

        lowered = item.casefold()
        if is_caveat_explanation(item):
            caveats.append(item)
        elif lowered.startswith("from "):
            sources.append(item)
        elif lowered.startswith("moved up "):
            serving_notes.append(item)
        else:
            positives.append(item)

    return [*positives, *sources, *serving_notes, *caveats]


def normalized_vector_matrix(vectors: Sequence[Sequence[float]], dimensions: int | None = None) -> np.ndarray:
    """Validate vectors and return row-normalized float32 vectors."""
    if len(vectors) == 0:
        return np.empty((0, dimensions or 0), dtype=np.float32)
    try:
        with np.errstate(over="ignore", invalid="ignore"):
            matrix = np.asarray(vectors, dtype=np.float64)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("embedding vectors must contain numeric rows") from exc
    if matrix.ndim != 2 or not matrix.shape[1]:
        raise ValueError("embedding vectors must be non-empty, equally sized rows")
    if dimensions is not None and matrix.shape[1] != dimensions:
        raise ValueError("embedding dimensions do not match")
    if not np.isfinite(matrix).all():
        raise ValueError("embedding vectors must contain only finite values")
    # Scale before taking the norm: direct float32 or float64 squaring can
    # overflow for large but finite provider vectors and silently turn them
    # into zero vectors.
    scales = np.max(np.abs(matrix), axis=1, keepdims=True)
    scaled = np.divide(matrix, scales, out=np.zeros_like(matrix), where=scales != 0)
    norms = np.linalg.norm(scaled, axis=1, keepdims=True)
    normalized = np.divide(scaled, norms, out=np.zeros_like(scaled), where=norms != 0)
    return normalized.astype(np.float32)


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
    *,
    exclude_candidate_identity: bool = False,
    publication_era: bool = True,
) -> list[dict[str, Any]]:
    """Rank candidates using positive/negative neighborhoods and author affinity.

    The returned rows contain the original candidate fields plus ``score`` and
    ``explanation``. Duplicate read identities are deterministically reduced to
    the lowest read id, which keeps repeated imports from overweighting history.
    ``exclude_candidate_identity`` supports leave-one-out scoring for books that
    are already in the read history.
    """
    if len(reads) != len(read_vectors) or len(candidates) != len(candidate_vectors):
        raise ValueError("items and embedding vectors must have equal lengths")
    if not candidates:
        return []
    read_matrix = normalized_vector_matrix(read_vectors)
    dimensions = read_matrix.shape[1]
    candidate_matrix = normalized_vector_matrix(candidate_vectors, dimensions or None)
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
    history_identities = [book_identity(item.get("title", ""), item.get("author", "")) for item, _ in all_ratings]
    history_index_by_identity = {identity: index for index, identity in enumerate(history_identities)}
    all_history_indices = np.arange(len(all_ratings), dtype=np.int64)
    positive_history_indices = np.asarray([
        index for index, (item, _) in enumerate(all_ratings)
        if float(item.get("rating") or 0) >= 4
    ], dtype=np.int64)
    negative_history_indices = np.asarray([
        index for index, (item, _) in enumerate(all_ratings)
        if 0 < float(item.get("rating") or 0) <= 2
    ], dtype=np.int64)
    all_positive_columns = np.arange(len(positives), dtype=np.int64)
    all_negative_columns = np.arange(len(negatives), dtype=np.int64)
    positive_column_by_history_index = {
        int(history_index): column
        for column, history_index in enumerate(positive_history_indices)
    }
    negative_column_by_history_index = {
        int(history_index): column
        for column, history_index in enumerate(negative_history_indices)
    }
    author_history_indices: dict[str, list[int]] = {}
    for index, (item, _) in enumerate(all_ratings):
        author_history_indices.setdefault(
            book_identity("", item.get("author", "")), []
        ).append(index)
    era_fit = None
    # History diagnostics deliberately omit this fitted feature: fitting its
    # gates with the target's own rating would leak the leave-one-out label.
    # Prediction validation inside the era model also uses the baseline only.
    if publication_era and not exclude_candidate_identity and any(
        item.get("first_publication_year") is not None for item in candidates
    ):
        from .publication_era_signal import fit_publication_era_signal

        def baseline_score(history_rows, history_vectors, query_rows, query_vectors):
            return [item["score"] for item in rank_candidates(
                history_rows, history_vectors, query_rows, query_vectors,
                exclude_candidate_identity=True, publication_era=False,
            )]

        era_fit = fit_publication_era_signal(
            [item for item, _ in history],
            [vector for _, vector in history],
            baseline_score,
        )
    results: list[dict[str, Any]] = []
    for start in range(0, len(candidates), _BATCH_SIZE):
        batch = candidates[start : start + _BATCH_SIZE]
        vectors = candidate_matrix[start : start + len(batch)]
        positive_sim = vectors @ pos_matrix.T if len(pos_matrix) else np.empty((len(batch), 0), dtype=np.float32)
        negative_sim = vectors @ neg_matrix.T if len(neg_matrix) else np.empty((len(batch), 0), dtype=np.float32)
        all_sim = vectors @ all_matrix.T if len(all_matrix) else np.empty((len(batch), 0), dtype=np.float32)
        for offset, candidate in enumerate(batch):
            author = book_identity("", candidate.get("author", ""))
            excluded_index = (
                history_index_by_identity.get(book_identity(candidate.get("title", ""), candidate.get("author", "")))
                if exclude_candidate_identity
                else None
            )
            profile_indices = (
                np.delete(all_history_indices, excluded_index)
                if excluded_index is not None
                else all_history_indices
            )
            excluded_positive_column = positive_column_by_history_index.get(excluded_index)
            positive_columns = (
                np.delete(all_positive_columns, excluded_positive_column)
                if excluded_positive_column is not None
                else all_positive_columns
            )
            excluded_negative_column = negative_column_by_history_index.get(excluded_index)
            negative_columns = (
                np.delete(all_negative_columns, excluded_negative_column)
                if excluded_negative_column is not None
                else all_negative_columns
            )
            positive_values = positive_sim[offset, positive_columns]
            negative_values = negative_sim[offset, negative_columns]
            positive_max = float(positive_values.max()) if len(positive_values) else .25
            negative_max = float(negative_values.max()) if len(negative_values) else 0.0
            positive_top = float(_top_weighted(positive_values.reshape(1, -1), default=.25)[0])

            author_ratings = [
                float(all_ratings[index][0].get("rating") or 0)
                for index in author_history_indices.get(author, [])
                if index != excluded_index
            ]
            if author_ratings:
                author_mean = sum(author_ratings) / len(author_ratings)
            else:
                author_mean = 0.0
            effective_ratings = rating_values[profile_indices]
            global_mean = float(effective_ratings.mean()) if len(effective_ratings) else 3.0
            author_delta = (
                (len(author_ratings) * author_mean + 5 * global_mean)
                / (len(author_ratings) + 5)
                - global_mean
                if author_ratings
                else 0.0
            )
            profile_similarities = all_sim[offset, profile_indices]
            kernel_effective_sample_size = 0.0
            if len(profile_similarities) and np.any(profile_similarities > 0):
                neighbor_positions = np.argsort(-profile_similarities, kind="stable")[:min(_KERNEL_NEIGHBORS, len(profile_indices))]
                neighbors = np.asarray([profile_indices[position] for position in neighbor_positions], dtype=np.int64)
                similarities = np.maximum(all_sim[offset, neighbors].astype(np.float64), 0)
                kernel_weights = (similarities + 1e-8) ** _KERNEL_POWER
                kernel_rating = float(np.dot(kernel_weights, rating_values[neighbors]) / kernel_weights.sum())
                kernel_effective_sample_size = float(
                    kernel_weights.sum() ** 2 / np.dot(kernel_weights, kernel_weights)
                )
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
            score = 42 + 48 * positive_top - 24 * negative_max + 3.5 * (local_rating - 3) + 3.5 * author_delta
            # The historical comparison begins at 100 reads. Ramp in the new
            # term for smaller libraries, where one neighbor is weak evidence.
            kernel_strength = _KERNEL_SCORE_PER_STAR * min(1.0, len(profile_indices) / _KERNEL_FULL_HISTORY)
            kernel_confidence = kernel_effective_sample_size / (
                kernel_effective_sample_size + _KERNEL_ESS_PRIOR
            )
            score += kernel_strength * (kernel_rating - global_mean) * kernel_confidence
            recency_strength = _RECENCY_SCORE_PER_STAR * min(1.0, len(profile_indices) / _KERNEL_FULL_HISTORY)
            score += recency_strength * (recent_rating - kernel_rating)
            score += (float(candidate.get("source_weight") or 1) - 1) * 5
            metadata_confidence = _metadata_confidence(candidate)
            # Sparse catalog records produce weak text vectors. Shrink their
            # ranking signal toward a neutral score instead of letting a
            # title-only match dominate the top of discovery.
            score = 50 + metadata_confidence * (score - 50)
            era_adjustment = (
                era_fit.adjustment(candidate.get("first_publication_year"))
                if era_fit is not None else 0.0
            )
            if era_adjustment:
                # Validate and deploy the same one-decimal baseline scale.
                score = round(max(0.0, min(100.0, score)), 1) + era_adjustment
            score = max(0.0, min(100.0, score))
            positive_reasons: list[str] = []
            source_reasons: list[str] = []
            caveat_reasons: list[str] = []

            if any(rating >= 4 for rating in author_ratings):
                if any(rating <= 2 for rating in author_ratings):
                    caveat_reasons.append("This author has mixed ratings in your reading history")
                else:
                    positive_reasons.append("You rated a book by this author highly")
            elif (
                author_ratings
                and all(rating <= 2 for rating in author_ratings)
                and author_delta < 0
            ):
                caveat_reasons.append(
                    "Reduced: you rated a book by this author poorly"
                    if len(author_ratings) == 1
                    else "Reduced: you rated books by this author poorly"
                )

            actual_positive_max = float(positive_values.max()) if len(positive_values) else 0.0
            if len(positive_values) and actual_positive_max > _POSITIVE_EXPLANATION_MIN_SIMILARITY:
                nearest = positive_columns[int(np.argmax(positive_values))]
                positive_reasons.append(
                    f"Closest highly rated title: {positives[nearest][0].get('title', 'a highly rated book')}"
                )

            non_negative_similarities = np.asarray(
                [
                    all_sim[offset, int(index)]
                    for index in profile_indices
                    if float(all_ratings[int(index)][0].get("rating") or 0) > 2
                ],
                dtype=np.float32,
            )
            if len(profile_similarities) > 1 and len(non_negative_similarities):
                baseline_similarity = max(
                    0.0,
                    min(
                        float(np.median(profile_similarities)),
                        float(np.median(non_negative_similarities)),
                    ),
                )
            else:
                baseline_similarity = 0.0

            if (
                len(negative_values)
                and negative_max > _NEGATIVE_EXPLANATION_MIN_SIMILARITY
                and negative_max > baseline_similarity + _NEGATIVE_EXPLANATION_MEDIAN_MARGIN
                and (negative_max >= actual_positive_max or score < 50.0)
            ):
                nearest = negative_columns[int(np.argmax(negative_values))]
                caveat_reasons.append(
                    f"Reduced: similar to a 1–2★ book — {negatives[nearest][0].get('title', 'a low-rated book')}"
                )

            if era_adjustment:
                era_reason = format_era_explanation(
                    candidate.get("first_publication_year"),
                    positive=era_adjustment > 0,
                )
                if era_adjustment > 0:
                    positive_reasons.append(era_reason)
                else:
                    caveat_reasons.append(era_reason)

            if candidate.get("source_name"):
                source_reasons.append(f"From {candidate['source_name']}")
            if metadata_confidence < 0.65:
                caveat_reasons.append(
                    "Ranking confidence reduced because catalog details are sparse"
                )
            explanation = [*positive_reasons, *source_reasons, *caveat_reasons]
            results.append(
                {
                    **candidate,
                    "score": round(score, 1),
                    "metadata_confidence": metadata_confidence,
                    **({"publication_era": {
                        **era_fit.diagnostics,
                        "score_adjustment": round(era_adjustment, 4),
                    }} if era_fit is not None else {}),
                    "explanation": explanation,
                }
            )
    return results
