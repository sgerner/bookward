"""Confidence-gated, on-policy personalization from existing user actions.

The learner only uses observed saves, rejects, imports, and attributed ratings.
It never turns an unclicked or unseen recommendation into a negative label.
Preference estimates are rebuilt cheaply from the local event log at request
time, so a newly confident signal affects the next recommendation response.
"""

from __future__ import annotations

import math
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any, Iterable

import numpy as np

from .config import settings
from .database import rows
from .embeddings import content_hash
from .identity import book_identity
from .scoring import document
from .subjects import normalize_subjects


MAX_INTERACTION_EVENTS = 5_000
DECAY_HALF_LIFE_DAYS = 730.0
MIN_EFFECTIVE_BOOKS = 6.0
MIN_WEIGHTED_EVIDENCE = 4.5
WILSON_Z = 1.96
_EFFECTIVE_COUNT_EPSILON = 1e-9
MAX_AUTHOR_ADJUSTMENT = 4.0
MAX_SUBJECT_ADJUSTMENT = 2.0
MAX_TOTAL_ADJUSTMENT = 8.0
_MAX_SUBJECTS_PER_CANDIDATE = 4
_FEATURE_RATE_SCALE = 0.5
_CONFIDENCE_GAP_SCALE = 0.15
_SEMANTIC_SIMILARITY_FLOOR = 0.45
_MIN_SEMANTIC_EFFECTIVE_BOOKS = 4.0
_MIN_SEMANTIC_WEIGHT = 1.25
_MAX_SEMANTIC_ADJUSTMENT = 4.0
_NONINFORMATIVE_SUBJECTS = {
    "book",
    "books",
    "fiction",
    "nonfiction",
    "literature",
    "literary",
    "novel",
    "novels",
    "general",
}


def load_interaction_events(
    connection=None,
    *,
    session_id: str | None = None,
    limit: int = MAX_INTERACTION_EVENTS,
):
    """Load recent actions, scoped to the browser session when one is known.

    Older feedback rows without a recommendation run cannot be assigned to a
    particular reader. They remain usable for legacy, installation-wide API
    requests, but are excluded from a session-scoped profile.
    """

    event_scope = ""
    legacy_feedback_scope = ""
    values: tuple[Any, ...] = (max(1, min(MAX_INTERACTION_EVENTS, int(limit))),)
    if session_id:
        event_scope = """
            AND (
                EXISTS (
                    SELECT 1 FROM recommendation_runs r
                    WHERE r.id=e.run_id AND r.session_id=?
                )
                OR CASE
                    WHEN json_valid(e.metadata)
                    THEN json_extract(e.metadata,'$.session_id')=?
                    ELSE 0
                END
            )
        """
        legacy_feedback_scope = "AND 0"
        values = (session_id, session_id, *values)
    query = f"""
        SELECT id,candidate_id,event_type,value,occurred_at,title,author,genres,description,metadata
        FROM (
            SELECT e.id,e.candidate_id,e.event_type,e.value,e.occurred_at,
                   c.title,c.author,c.genres,c.description,e.metadata
            FROM recommendation_events e
            JOIN candidates c ON c.id=e.candidate_id
            WHERE e.event_type IN ('save','reject','restore','read','librarr_import')
              {event_scope}
            UNION ALL
            SELECT 1000000000+f.id AS id,f.candidate_id,f.action AS event_type,
                   NULL AS value,f.created_at AS occurred_at,
                   c.title,c.author,c.genres,c.description,NULL AS metadata
            FROM feedback f
            JOIN candidates c ON c.id=f.candidate_id
            WHERE f.action IN ('save','reject','restore')
              {legacy_feedback_scope}
              AND NOT EXISTS (
                  SELECT 1 FROM recommendation_events e
                  WHERE e.event_key='feedback:' || f.id
              )
        )
        ORDER BY datetime(occurred_at) DESC,id DESC
        LIMIT ?
    """
    if connection is not None:
        return [dict(item) for item in connection.execute(query, values).fetchall()]
    return rows(query, values)


def load_cached_candidate_vectors(items, connection=None) -> dict[int, np.ndarray]:
    """Load current-model vectors only when their content hashes still match."""

    item_by_id = {int(item["id"]): item for item in items if item.get("id") is not None}
    if not item_by_id:
        return {}
    if connection is not None:
        configured = {
            item["key"]: item["value"]
            for item in connection.execute(
                "SELECT key,value FROM settings WHERE key IN ('embedding_backend','embedding_model')"
            ).fetchall()
        }
    else:
        configured = {
            item["key"]: item["value"]
            for item in rows(
                "SELECT key,value FROM settings WHERE key IN ('embedding_backend','embedding_model')"
            )
        }
    backend = configured.get("embedding_backend", settings.embedding_backend)
    model = configured.get("embedding_model", settings.embedding_model)
    hashes = {item_id: content_hash(document(item)) for item_id, item in item_by_id.items()}
    result: dict[int, np.ndarray] = {}
    item_ids = list(item_by_id)
    for start in range(0, len(item_ids), 500):
        batch = item_ids[start : start + 500]
        placeholders = ",".join("?" for _ in batch)
        query = (
            "SELECT entity_id,vector,content_hash FROM embeddings "
            "WHERE entity_type='candidate' AND backend=? AND model=? "
            f"AND entity_id IN ({placeholders})"
        )
        values = (backend, model, *batch)
        cached = (
            connection.execute(query, values).fetchall()
            if connection is not None
            else rows(query, values)
        )
        for item in cached:
            item_id = int(item["entity_id"])
            if item["content_hash"] != hashes.get(item_id):
                continue
            vector = np.frombuffer(item["vector"], dtype=np.float32)
            if vector.size and np.isfinite(vector).all() and np.linalg.norm(vector) > 0:
                result[item_id] = vector
    return result


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.strptime(value.strip(), "%Y/%m/%d")
            except ValueError:
                try:
                    parsed = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    return None
    else:
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)


def _event_order(event: dict[str, Any]):
    occurred = _parse_datetime(event.get("occurred_at"))
    # Stable event ids break timestamp ties. Missing timestamps sort before
    # dated events, allowing a later known action to supersede them.
    timestamp = occurred.timestamp() if occurred else float("-inf")
    try:
        event_id = int(event.get("id") or 0)
    except (TypeError, ValueError):
        event_id = 0
    return timestamp, event_id


def _event_label(event: dict[str, Any]) -> tuple[float | None, float]:
    event_type = str(event.get("event_type") or "").casefold()
    if event_type == "save":
        return 1.0, 1.0
    if event_type == "reject":
        return 0.0, 1.0
    if event_type == "restore":
        # Restoring a rejected item is an explicit correction of that signal.
        return None, 0.0
    if event_type == "librarr_import":
        # Acquisition is meaningful intent, but weaker evidence of enjoyment.
        return 1.0, 0.7
    if event_type == "read":
        try:
            rating = float(event.get("value"))
        except (TypeError, ValueError):
            return None, 0.0
        if not math.isfinite(rating):
            return None, 0.0
        if rating >= 4:
            return 1.0, 1.0 if rating >= 5 else 0.85
        if rating <= 2:
            return 0.0, 1.0 if rating <= 1 else 0.85
    # Three-star and unrated reads are neutral. Opens, searches, and missing
    # actions are not satisfaction labels.
    return None, 0.0


def _author_key(value: object) -> str:
    identity = book_identity("", value)
    return identity.split("\x1f", 1)[-1].strip()


def _feature_key(value: object) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "")).casefold()
    chars = [char if char.isalnum() else " " for char in normalized]
    return " ".join("".join(chars).split())


def _book_features(item: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    author = str(item.get("author") or "").strip()
    normalized_author = _author_key(author)
    if normalized_author:
        key = f"author:{normalized_author}"
        keys.add(key)

    for subject in normalize_subjects(item.get("genres"), limit=8):
        normalized_subject = _feature_key(subject)
        if (
            not normalized_subject
            or normalized_subject in _NONINFORMATIVE_SUBJECTS
            or len(normalized_subject) < 3
        ):
            continue
        key = f"subject:{normalized_subject}"
        keys.add(key)
        if len([value for value in keys if value.startswith("subject:")]) >= _MAX_SUBJECTS_PER_CANDIDATE:
            break
    return keys


def _latest_observations(
    events: Iterable[dict[str, Any]],
    *,
    now: datetime,
    interaction_vectors: dict[int, np.ndarray] | None = None,
) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    ordered = sorted((dict(item) for item in events), key=_event_order)
    for event in ordered:
        event_type = str(event.get("event_type") or "").casefold()
        if event_type not in {"save", "reject", "restore", "read", "librarr_import"}:
            continue
        identity = book_identity(event.get("title", ""), event.get("author", ""))
        if identity == "\x1f":
            continue
        label, reliability = _event_label(event)
        occurred = _parse_datetime(event.get("occurred_at"))
        try:
            candidate_id = int(event.get("candidate_id"))
        except (TypeError, ValueError):
            candidate_id = 0
        latest[identity] = {
            "identity": identity,
            "candidate_id": candidate_id,
            "label": label,
            "reliability": reliability,
            "occurred_at": occurred,
            "features": _book_features(event),
            "vector": (interaction_vectors or {}).get(candidate_id),
        }

    observations = []
    for item in latest.values():
        if item["label"] is None or not item["features"]:
            continue
        occurred = item["occurred_at"]
        age_days = max(0.0, (now - occurred).total_seconds() / 86_400) if occurred else 0.0
        weight = item["reliability"] * (0.5 ** (age_days / DECAY_HALF_LIFE_DAYS))
        if weight < 0.05:
            continue
        item["weight"] = weight
        observations.append(item)
    return observations


def _wilson_interval(successes: float, total: float, effective_n: float):
    if total <= 0 or effective_n <= 0:
        return 0.0, 1.0
    proportion = min(1.0, max(0.0, successes / total))
    n = max(1.0, effective_n)
    z2 = WILSON_Z * WILSON_Z
    denominator = 1 + z2 / n
    center = (proportion + z2 / (2 * n)) / denominator
    margin = WILSON_Z * math.sqrt(
        proportion * (1 - proportion) / n + z2 / (4 * n * n)
    ) / denominator
    return max(0.0, center - margin), min(1.0, center + margin)


def _group_stats(observations: list[dict[str, Any]]):
    weight = sum(item["weight"] for item in observations)
    positive = sum(item["weight"] for item in observations if item["label"] == 1.0)
    squared_weight = sum(item["weight"] ** 2 for item in observations)
    return _stats_from_sums(weight, positive, squared_weight)


def _stats_from_sums(weight: float, positive: float, squared_weight: float):
    effective_n = weight * weight / squared_weight if squared_weight else 0.0
    interval = _wilson_interval(positive, weight, effective_n)
    rate = positive / weight if weight else 0.5
    return {
        "weight": weight,
        "positive": positive,
        "negative": max(0.0, weight - positive),
        "squared_weight": squared_weight,
        "effective_n": effective_n,
        "rate": rate,
        "interval": interval,
    }


def _learn_feature_effects(observations: list[dict[str, Any]]):
    """Return only preference differences supported by separated 95% intervals."""

    global_stats = _group_stats(observations)
    grouped: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0, 0.0])
    for observation in observations:
        weight = observation["weight"]
        for feature in observation["features"]:
            stats = grouped[feature]
            stats[0] += weight
            stats[1] += weight if observation["label"] == 1.0 else 0.0
            stats[2] += weight * weight

    effects: dict[str, float] = {}
    for feature, sums in grouped.items():
        feature_stats = _stats_from_sums(*sums)
        other_stats = _stats_from_sums(
            max(0.0, global_stats["weight"] - sums[0]),
            max(0.0, global_stats["positive"] - sums[1]),
            max(0.0, global_stats["squared_weight"] - sums[2]),
        )
        if (
            feature_stats["effective_n"] + _EFFECTIVE_COUNT_EPSILON < MIN_EFFECTIVE_BOOKS
            or other_stats["effective_n"] + _EFFECTIVE_COUNT_EPSILON < MIN_EFFECTIVE_BOOKS
            or feature_stats["weight"] < MIN_WEIGHTED_EVIDENCE
            or other_stats["weight"] < MIN_WEIGHTED_EVIDENCE
        ):
            continue

        feature_low, feature_high = feature_stats["interval"]
        other_low, other_high = other_stats["interval"]
        if feature_low > other_high:
            sign = 1.0
            interval_gap = feature_low - other_high
        elif feature_high < other_low:
            sign = -1.0
            interval_gap = other_low - feature_high
        else:
            continue

        difference = abs(feature_stats["rate"] - other_stats["rate"])
        confidence = min(1.0, interval_gap / _CONFIDENCE_GAP_SCALE)
        raw_strength = min(1.0, difference / _FEATURE_RATE_SCALE)
        cap = MAX_AUTHOR_ADJUSTMENT if feature.startswith("author:") else MAX_SUBJECT_ADJUSTMENT
        effects[feature] = sign * cap * raw_strength * confidence
    return effects


def _learn_semantic_adjustments(
    observations: list[dict[str, Any]],
    recommendations: list[dict[str, Any]],
    candidate_vectors: dict[int, np.ndarray] | None,
):
    """Find confident save/reject differences among semantically close books."""

    if not candidate_vectors:
        return {}, 0
    vector_observations = [
        item for item in observations
        if item.get("vector") is not None
    ]
    if not vector_observations:
        return {}, 0
    dimensions = int(np.asarray(vector_observations[0]["vector"]).size)
    vector_observations = [
        item for item in vector_observations
        if np.asarray(item["vector"]).size == dimensions
    ]
    if len(vector_observations) < 2:
        return {}, len(vector_observations)
    reference = np.stack(
        [np.asarray(item["vector"], dtype=np.float32) for item in vector_observations]
    )
    norms = np.linalg.norm(reference, axis=1, keepdims=True)
    reference = np.divide(reference, norms, out=np.zeros_like(reference), where=norms != 0)
    valid = np.linalg.norm(reference, axis=1) > 0
    reference = reference[valid]
    vector_observations = [item for item, keep in zip(vector_observations, valid) if keep]
    if len(vector_observations) < 2:
        return {}, len(vector_observations)

    labels = np.asarray([item["label"] for item in vector_observations], dtype=np.float64)
    base_weights = np.asarray([item["weight"] for item in vector_observations], dtype=np.float64)
    adjustments: dict[int, float] = {}
    for candidate in recommendations:
        if candidate.get("status") != "recommended":
            continue
        try:
            candidate_id = int(candidate["id"])
        except (KeyError, TypeError, ValueError):
            continue
        raw_vector = candidate_vectors.get(candidate_id)
        if raw_vector is None:
            continue
        vector = np.asarray(raw_vector, dtype=np.float32)
        if vector.size != dimensions or not np.isfinite(vector).all():
            continue
        norm = float(np.linalg.norm(vector))
        if not norm:
            continue
        similarities = (reference @ (vector / norm)).astype(np.float64)
        local = similarities >= _SEMANTIC_SIMILARITY_FLOOR
        if int(local.sum()) < _MIN_SEMANTIC_EFFECTIVE_BOOKS:
            continue
        local_weights = base_weights[local] * np.maximum(similarities[local], 0.0) ** 2
        other_weights = base_weights[~local]
        local_sum = float(local_weights.sum())
        local_square_sum = float(local_weights @ local_weights)
        other_sum = float(other_weights.sum())
        other_square_sum = float(other_weights @ other_weights)
        local_stats = _stats_from_sums(
            local_sum,
            float(local_weights @ labels[local]),
            local_square_sum,
        )
        other_stats = _stats_from_sums(
            other_sum,
            float(other_weights @ labels[~local]),
            other_square_sum,
        )
        if (
            local_stats["effective_n"] + _EFFECTIVE_COUNT_EPSILON < _MIN_SEMANTIC_EFFECTIVE_BOOKS
            or other_stats["effective_n"] + _EFFECTIVE_COUNT_EPSILON < MIN_EFFECTIVE_BOOKS
            or local_sum < _MIN_SEMANTIC_WEIGHT
            or other_sum < MIN_WEIGHTED_EVIDENCE
        ):
            continue
        local_low, local_high = local_stats["interval"]
        other_low, other_high = other_stats["interval"]
        if local_low > other_high:
            sign = 1.0
            interval_gap = local_low - other_high
        elif local_high < other_low:
            sign = -1.0
            interval_gap = other_low - local_high
        else:
            continue
        difference = abs(local_stats["rate"] - other_stats["rate"])
        confidence = min(1.0, interval_gap / _CONFIDENCE_GAP_SCALE)
        strength = min(1.0, difference / _FEATURE_RATE_SCALE)
        adjustments[candidate_id] = (
            sign * _MAX_SEMANTIC_ADJUSTMENT * strength * confidence
        )
    return adjustments, len(vector_observations)


def personalize_recommendations(
    recommendations: list[dict[str, Any]],
    events: Iterable[dict[str, Any]],
    *,
    now: datetime | None = None,
    interaction_vectors: dict[int, np.ndarray] | None = None,
    candidate_vectors: dict[int, np.ndarray] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply bounded live score adjustments and return aggregate diagnostics.

    Only recommended items are adjusted. Saved/imported state is preserved,
    and stable sorting keeps the champion's order when scores tie.
    """

    current_time = now or datetime.now(timezone.utc)
    current_time = current_time.replace(tzinfo=current_time.tzinfo or timezone.utc).astimezone(timezone.utc)
    observations = _latest_observations(
        events,
        now=current_time,
        interaction_vectors=interaction_vectors,
    )
    effects = _learn_feature_effects(observations)
    semantic_adjustments, semantic_evidence_books = _learn_semantic_adjustments(
        observations, recommendations, candidate_vectors
    )
    adjusted = []
    adjusted_count = 0
    semantic_adjusted_count = 0
    max_movement = 0.0
    for original in recommendations:
        item = dict(original)
        if item.get("status") != "recommended":
            adjusted.append(item)
            continue
        feature_keys = _book_features(item)
        delta = 0.0
        for feature in feature_keys:
            delta += effects.get(feature, 0.0)
        try:
            candidate_id = int(item["id"])
        except (KeyError, TypeError, ValueError):
            candidate_id = 0
        semantic_delta = semantic_adjustments.get(candidate_id, 0.0)
        delta += semantic_delta
        delta = max(-MAX_TOTAL_ADJUSTMENT, min(MAX_TOTAL_ADJUSTMENT, delta))
        try:
            metadata_confidence = float(item.get("metadata_confidence"))
        except (TypeError, ValueError):
            metadata_confidence = 0.0
        if not math.isfinite(metadata_confidence):
            metadata_confidence = 0.0
        confidence_scale = max(0.0, min(1.0, metadata_confidence))
        if abs(semantic_delta * confidence_scale) >= 0.05:
            semantic_adjusted_count += 1
        delta *= confidence_scale
        if abs(delta) >= 0.05:
            try:
                base_score = float(item.get("score") or 0.0)
            except (TypeError, ValueError):
                base_score = 0.0
            item["score"] = round(max(0.0, min(100.0, base_score + delta)), 1)
            explanation = item.get("explanation")
            if not isinstance(explanation, list):
                explanation = []
            message = (
                "Raised using your saved, imported, or positively rated books"
                if delta > 0
                else "Lowered using books you rejected or rated poorly"
            )
            if message not in explanation:
                item["explanation"] = [*explanation, message]
            adjusted_count += 1
            max_movement = max(max_movement, abs(delta))
        adjusted.append(item)

    recommended = [item for item in adjusted if item.get("status") == "recommended"]
    other_statuses = [item for item in adjusted if item.get("status") != "recommended"]
    recommended.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
    diagnostics = {
        "applied": adjusted_count > 0,
        "observed_books": len(observations),
        "qualified_features": len(effects),
        "semantic_evidence_books": semantic_evidence_books,
        "semantic_adjusted_candidates": semantic_adjusted_count,
        "adjusted_candidates": adjusted_count,
        "max_score_adjustment": round(max_movement, 1),
    }
    return recommended + other_statuses, diagnostics
