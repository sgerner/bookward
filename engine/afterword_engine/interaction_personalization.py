"""Confidence-gated, on-policy personalization from existing user actions.

The learner only uses observed saves, rejects, imports, and attributed ratings.
It never turns an unclicked or unseen recommendation into a negative label.
Preference estimates are rebuilt cheaply from the local event log at request
time, so a newly confident signal affects the next recommendation response.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from datetime import date, datetime, timezone
from typing import Any

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
MAX_ERA_ADJUSTMENT = 3.5
MAX_CONTEMPORARY_ERA_ADJUSTMENT = 1.0
MAX_SUBJECT_ADJUSTMENT = 2.0
MAX_TOTAL_ADJUSTMENT = 8.0
_MAX_SUBJECTS_PER_CANDIDATE = 4
_SUBJECT_DIMINISHING_WEIGHTS = (1.0, 0.5, 0.25, 0.125)
_FEATURE_RATE_SCALE = 0.5
_CONFIDENCE_GAP_SCALE = 0.15
_MATURE_OBSERVATION_THRESHOLD = 20
_EB_MIN_FEATURE_WEIGHT = 1.5
_EB_AUTHOR_PRIOR_STRENGTH = 3.0
_EB_ERA_PRIOR_STRENGTH = 3.0
_EB_CONTEMPORARY_ERA_PRIOR_STRENGTH = 4.0
_EB_SUBJECT_PRIOR_STRENGTH = 3.5
_SEMANTIC_SIMILARITY_FLOOR = 0.45
_SEMANTIC_POSITIVE_FLOOR = 0.52
_SEMANTIC_PRIOR_WEIGHT = 1.5
_SEMANTIC_MATURE_PRIOR_WEIGHT = 1.0
_SEMANTIC_MIN_OBSERVATIONS = 10
_MAX_SEMANTIC_ADJUSTMENT = 4.0
_MAX_MATURE_SEMANTIC_ADJUSTMENT = 5.0
_NONINFORMATIVE_SUBJECTS = {
    "audio books",
    "audiobook",
    "audiobooks",
    "book",
    "books",
    "ebook",
    "ebooks",
    "fiction",
    "fiction general",
    "fiction literature",
    "general",
    "general fiction",
    "large type books",
    "literary",
    "literature",
    "nonfiction",
    "nonfiction general",
    "novel",
    "novels",
}


def load_interaction_events(
    connection=None,
    *,
    limit: int = MAX_INTERACTION_EVENTS,
):
    """Load recent actions and feedback across the whole installation.

    Recommendation runs retain their historical session identifiers for
    telemetry, but a self-hosted Bookward installation has one shared learner
    profile across browsers and devices.
    """
    values: tuple[Any, ...] = (max(1, min(MAX_INTERACTION_EVENTS, int(limit))),)
    query = """
        SELECT id,candidate_id,event_type,value,occurred_at,title,author,genres,description,
               release_date,date_kind,quality_work_id,metadata
        FROM (
            SELECT e.id,e.candidate_id,e.event_type,e.value,e.occurred_at,
                   c.title,c.author,c.genres,c.description,c.release_date,c.date_kind,
                   q.work_id AS quality_work_id,e.metadata
            FROM recommendation_events e
            JOIN candidates c ON c.id=e.candidate_id
            LEFT JOIN candidate_quality q ON q.candidate_id=c.id
            WHERE e.event_type IN ('save','reject','maybe_later','restore','read','librarr_import')
            UNION ALL
            SELECT 1000000000+f.id AS id,f.candidate_id,f.action AS event_type,
                   NULL AS value,f.created_at AS occurred_at,
                   c.title,c.author,c.genres,c.description,c.release_date,c.date_kind,
                   q.work_id AS quality_work_id,NULL AS metadata
            FROM feedback f
            JOIN candidates c ON c.id=f.candidate_id
            LEFT JOIN candidate_quality q ON q.candidate_id=c.id
            WHERE f.action IN ('save','reject','maybe_later','restore')
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


_ELIGIBLE_EVENT_TYPES = frozenset(
    {"save", "reject", "maybe_later", "restore", "read", "librarr_import"}
)
_UNSET = object()


def _event_order(
    event: dict[str, Any],
    input_index: int = 0,
    *,
    occurred: datetime | None | object = _UNSET,
) -> tuple[float, int, int]:
    if not isinstance(event, dict):
        event = dict(event)
    if occurred is _UNSET:
        occurred = _parse_datetime(event.get("occurred_at"))
    # Stable event ids and input order break timestamp ties. Missing timestamps
    # sort before dated events, allowing a later known action to supersede them.
    timestamp = occurred.timestamp() if isinstance(occurred, datetime) else float("-inf")
    try:
        event_id = int(event.get("id") or 0)
    except (TypeError, ValueError):
        event_id = 0
    return timestamp, event_id, input_index


def _event_label(event: dict[str, Any]) -> tuple[float | None, float]:
    event_type = str(event.get("event_type") or "").casefold()
    if event_type == "save":
        return 1.0, 1.0
    if event_type == "reject":
        return 0.0, 1.0
    if event_type == "maybe_later":
        # This neutral event still replaces an earlier save or rejection for
        # the same book so a deferral cannot keep training the learner.
        return None, 0.0
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

    raw_year = item.get("first_publication_year") or item.get("release_date")
    if raw_year is not None:
        match = re.match(r"^(\d{4})", str(raw_year).strip())
        if match:
            year = int(match.group(1))
            if 1000 <= year <= 2100:
                if year <= 1950:
                    keys.add("era:pre_1950")
                elif year < 2000:
                    keys.add("era:1950_1999")
                elif year >= 2020:
                    keys.add("era:2020_plus")
    return keys


def _latest_observations(
    events: Iterable[dict[str, Any]],
    *,
    now: datetime,
    interaction_vectors: dict[int, np.ndarray] | None = None,
) -> list[dict[str, Any]]:
    deduped: dict[str, list[Any]] = {}
    for input_index, raw_event in enumerate(events):
        event = dict(raw_event)
        event_type = str(event.get("event_type") or "").casefold()
        if event_type not in _ELIGIBLE_EVENT_TYPES:
            continue
        identity = book_identity(event.get("title", ""), event.get("author", ""))
        if identity == "\x1f":
            continue
        occurred = _parse_datetime(event.get("occurred_at"))
        order_key = _event_order(event, input_index, occurred=occurred)
        existing = deduped.get(identity)
        if existing is None:
            deduped[identity] = [order_key, order_key, identity, event, occurred]
        else:
            if order_key < existing[0]:
                existing[0] = order_key
            if order_key > existing[1]:
                existing[1] = order_key
                existing[3] = event
                existing[4] = occurred

    vectors = interaction_vectors or {}
    observations: list[dict[str, Any]] = []
    for _first_key, _winner_key, identity, event, occurred in sorted(
        deduped.values(), key=lambda entry: entry[0]
    ):
        label, reliability = _event_label(event)
        if label is None:
            continue
        age_days = max(0.0, (now - occurred).total_seconds() / 86_400) if occurred else 0.0
        weight = reliability * (0.5 ** (age_days / DECAY_HALF_LIFE_DAYS))
        if weight < 0.05:
            continue
        features = _book_features(event)
        if not features:
            continue
        try:
            candidate_id = int(event.get("candidate_id"))
        except (TypeError, ValueError):
            candidate_id = 0
        observations.append(
            {
                "identity": identity,
                "candidate_id": candidate_id,
                "label": label,
                "reliability": reliability,
                "occurred_at": occurred,
                "features": features,
                "vector": vectors.get(candidate_id),
                "weight": weight,
            }
        )
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


def _feature_cap_and_prior(feature: str) -> tuple[float, float]:
    if feature.startswith("author:"):
        return MAX_AUTHOR_ADJUSTMENT, _EB_AUTHOR_PRIOR_STRENGTH
    if feature == "era:2020_plus":
        return MAX_CONTEMPORARY_ERA_ADJUSTMENT, _EB_CONTEMPORARY_ERA_PRIOR_STRENGTH
    if feature.startswith("era:"):
        return MAX_ERA_ADJUSTMENT, _EB_ERA_PRIOR_STRENGTH
    return MAX_SUBJECT_ADJUSTMENT, _EB_SUBJECT_PRIOR_STRENGTH


def _learn_feature_effects(observations: list[dict[str, Any]]):
    """Return preference differences supported by Wilson intervals or mature EB shrinkage."""

    global_stats = _group_stats(observations)
    if global_stats["weight"] < MIN_WEIGHTED_EVIDENCE:
        return {}
    global_rate = global_stats["rate"]
    mature_history = len(observations) >= _MATURE_OBSERVATION_THRESHOLD
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
        cap, prior_strength = _feature_cap_and_prior(feature)
        if (
            feature_stats["effective_n"] + _EFFECTIVE_COUNT_EPSILON >= MIN_EFFECTIVE_BOOKS
            and other_stats["effective_n"] + _EFFECTIVE_COUNT_EPSILON >= MIN_EFFECTIVE_BOOKS
            and feature_stats["weight"] >= MIN_WEIGHTED_EVIDENCE
            and other_stats["weight"] >= MIN_WEIGHTED_EVIDENCE
        ):
            feature_low, feature_high = feature_stats["interval"]
            other_low, other_high = other_stats["interval"]
            if feature_low > other_high:
                sign = 1.0
                interval_gap = feature_low - other_high
            elif feature_high < other_low:
                sign = -1.0
                interval_gap = other_low - feature_high
            else:
                sign = 0.0
                interval_gap = 0.0
            if sign != 0.0:
                difference = abs(feature_stats["rate"] - other_stats["rate"])
                confidence = min(1.0, interval_gap / _CONFIDENCE_GAP_SCALE)
                raw_strength = min(1.0, difference / _FEATURE_RATE_SCALE)
                effects[feature] = sign * cap * raw_strength * confidence
                continue

        if mature_history and sums[0] >= _EB_MIN_FEATURE_WEIGHT:
            shrunk_rate = (sums[1] + prior_strength * global_rate) / (sums[0] + prior_strength)
            delta = shrunk_rate - global_rate
            scaled = max(-cap, min(cap, cap * (delta / _FEATURE_RATE_SCALE)))
            if abs(scaled) >= 0.05:
                effects[feature] = scaled
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
    if len(vector_observations) < _SEMANTIC_MIN_OBSERVATIONS:
        return {}, len(vector_observations)

    labels = np.asarray([item["label"] for item in vector_observations], dtype=np.float64)
    base_weights = np.asarray([item["weight"] for item in vector_observations], dtype=np.float64)
    total_weight = float(base_weights.sum())
    if total_weight < MIN_WEIGHTED_EVIDENCE:
        return {}, len(vector_observations)
    global_rate = float((base_weights @ labels + 1.0) / (total_weight + 2.0))
    mature_semantics = len(vector_observations) >= _MATURE_OBSERVATION_THRESHOLD
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
        if mature_semantics:
            pos_local = (labels == 1.0) & (similarities >= _SEMANTIC_POSITIVE_FLOOR)
            neg_local = (labels == 0.0) & (similarities >= _SEMANTIC_SIMILARITY_FLOOR)
            local = pos_local | neg_local
            if not np.any(local):
                continue
            local_weights = np.zeros_like(similarities)
            if np.any(pos_local):
                pos_rel = (similarities[pos_local] - _SEMANTIC_POSITIVE_FLOOR) / (
                    1.0 - _SEMANTIC_POSITIVE_FLOOR
                )
                local_weights[pos_local] = base_weights[pos_local] * (pos_rel ** 2)
            if np.any(neg_local):
                neg_rel = (similarities[neg_local] - _SEMANTIC_SIMILARITY_FLOOR) / (
                    1.0 - _SEMANTIC_SIMILARITY_FLOOR
                )
                local_weights[neg_local] = base_weights[neg_local] * neg_rel
            w_tot = float(local_weights[local].sum())
            w_pos = float(local_weights[local] @ labels[local])
            prior_weight = _SEMANTIC_MATURE_PRIOR_WEIGHT
            max_adj = _MAX_MATURE_SEMANTIC_ADJUSTMENT
        else:
            local = similarities >= _SEMANTIC_SIMILARITY_FLOOR
            if not np.any(local):
                continue
            rel_sim = (similarities[local] - _SEMANTIC_SIMILARITY_FLOOR) / (
                1.0 - _SEMANTIC_SIMILARITY_FLOOR
            )
            local_weights = base_weights[local] * rel_sim
            w_tot = float(local_weights.sum())
            w_pos = float(local_weights @ labels[local])
            prior_weight = _SEMANTIC_PRIOR_WEIGHT
            max_adj = _MAX_SEMANTIC_ADJUSTMENT
        if w_tot <= 0.0:
            continue
        posterior_rate = (w_pos + prior_weight * global_rate) / (
            w_tot + prior_weight
        )
        delta = posterior_rate - global_rate
        scaled = max(
            -max_adj,
            min(max_adj, max_adj * (delta / _FEATURE_RATE_SCALE)),
        )
        if abs(scaled) >= 0.05:
            adjustments[candidate_id] = scaled
    return adjustments, len(vector_observations)


def _aggregate_feature_delta(feature_keys: set[str], effects: dict[str, float]) -> float:
    delta = 0.0
    subject_deltas: list[float] = []
    for feature in feature_keys:
        effect = effects.get(feature, 0.0)
        if not effect:
            continue
        if feature.startswith("subject:"):
            subject_deltas.append(effect)
        else:
            delta += effect
    if subject_deltas:
        subject_deltas.sort(key=abs, reverse=True)
        delta += sum(
            weight * value
            for weight, value in zip(_SUBJECT_DIMINISHING_WEIGHTS, subject_deltas)
        )
    return delta


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
        delta = _aggregate_feature_delta(feature_keys, effects)
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
            # Name a contributing feature only when its learned effect agrees
            # with the actual movement; semantic-only changes retain the
            # general explanation rather than inventing a seed-book match.
            contributing = sorted(
                (feature for feature in feature_keys if effects.get(feature, 0.0) * delta > 0),
                key=lambda feature: (-abs(effects[feature]), feature),
            )
            if contributing:
                feature = contributing[0]
                if feature.startswith("author:"):
                    detail = f"author {item.get('author')}"
                elif feature.startswith("era:"):
                    detail = "publication era"
                else:
                    detail = f"subject {feature.removeprefix('subject:')}"
                message += f" ({detail})"
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
