#!/usr/bin/env python3
"""Replay Bookward ranking from an allowlisted snapshot; emit aggregates only."""
from __future__ import annotations

import argparse
import base64
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import hashlib
import math
import os
from pathlib import Path
import random
import re
import sqlite3
import statistics
import sys
from typing import Any, Iterable, Mapping

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from afterword_engine.main import recommendation_list
from afterword_engine.discovery_slate import diversify_discovery_slate, empty_diagnostics
from afterword_engine.embeddings import content_hash
from afterword_engine.exploration import epsilon_tail_explore
from afterword_engine.identity import book_identity, book_identity_match_index, book_row_identity_match_keys
from afterword_engine.interaction_personalization import (
    load_cached_candidate_vectors,
    load_interaction_events,
    personalize_recommendations,
)
from afterword_engine.scoring import document, rank_candidates


RECOMPUTED_ARMS = (
    "current",
    "source_neutral",
    "metadata_neutral",
    "interaction_off",
    "slate_off",
)
SCOPES = ("recommended", "all")

# Frozen input contract for audit v1. New or removed runtime files require a
# separately reviewed audit protocol; never read arbitrary paths from a corpus.
RUNTIME_SOURCE_FILES_V1 = frozenset({
    "config.py",
    "discovery_slate.py",
    "exploration.py",
    "identity.py",
    "interaction_personalization.py",
    "learning.py",
    "main.py",
    "ranking.py",
    "scoring.py",
    "subjects.py",
})


def _json_text(value: Any, fallback: str) -> str:
    if value is None:
        return fallback
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _fixture_connection(snapshot: Mapping[str, Any]) -> sqlite3.Connection:
    """Create a read-only helper fixture in RAM using the production query shape."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE sources (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL DEFAULT 'web', enabled INTEGER NOT NULL DEFAULT 1,
            is_default INTEGER NOT NULL DEFAULT 0, weight REAL NOT NULL DEFAULT 1,
            lifecycle TEXT NOT NULL DEFAULT 'permanent'
        );
        CREATE TABLE candidates (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, author TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '', cover_url TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '', source_id INTEGER, release_date TEXT,
            date_kind TEXT NOT NULL DEFAULT 'unknown', genres TEXT NOT NULL DEFAULT '[]',
            score REAL NOT NULL DEFAULT 0, explanation TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'new', normalized_key TEXT NOT NULL DEFAULT '',
            created_at TEXT, updated_at TEXT, isbn13 TEXT NOT NULL DEFAULT '',
            isbn10 TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX idx_candidates_status_score ON candidates(status, score DESC);
        CREATE TABLE candidate_quality (
            candidate_id INTEGER PRIMARY KEY, quality_status TEXT NOT NULL DEFAULT 'pending',
            quality_score REAL NOT NULL DEFAULT 0, metadata_confidence REAL NOT NULL DEFAULT 0.5,
            work_id TEXT NOT NULL DEFAULT '', provider TEXT NOT NULL DEFAULT '',
            isbn13 TEXT NOT NULL DEFAULT '', isbn10 TEXT NOT NULL DEFAULT '',
            title_match REAL NOT NULL DEFAULT 0, author_match REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE reads (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, author TEXT NOT NULL,
            rating REAL, read_at TEXT, isbn TEXT, source TEXT NOT NULL DEFAULT '',
            created_at TEXT, openlibrary_work_id TEXT
        );
        CREATE TABLE feedback (
            id INTEGER PRIMARY KEY, candidate_id INTEGER NOT NULL, action TEXT NOT NULL,
            created_at TEXT, previous_status TEXT, undone_at TEXT
        );
        CREATE TABLE recommendation_events (
            id INTEGER PRIMARY KEY, event_key TEXT, candidate_id INTEGER NOT NULL,
            event_type TEXT NOT NULL, value REAL, occurred_at TEXT, title TEXT,
            author TEXT, genres TEXT, description TEXT, metadata TEXT
        );
        CREATE TABLE embeddings (
            entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL, backend TEXT NOT NULL,
            model TEXT NOT NULL, vector BLOB NOT NULL, dimensions INTEGER NOT NULL,
            content_hash TEXT NOT NULL, updated_at TEXT
        );
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """
    )
    con.executemany(
        "INSERT INTO sources(id,name,url,kind,enabled,is_default,weight,lifecycle) VALUES(?,?,?,?,?,?,?,?)",
        [
            (
                int(item["id"]), str(item.get("name") or ""),
                f"snapshot://source/{int(item['id'])}",
                str(item.get("kind") or "web"), int(item.get("enabled") or 0),
                int(item.get("is_default") or 0), float(item.get("weight") or 0),
                str(item.get("lifecycle") or "permanent"),
            )
            for item in snapshot.get("sources", [])
        ],
    )
    con.executemany(
        """INSERT INTO candidates(
            id,title,author,description,source_url,source_id,release_date,date_kind,
            genres,score,explanation,status,normalized_key,created_at,updated_at,isbn13,isbn10
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                int(item["id"]), str(item.get("title") or ""),
                str(item.get("author") or ""), str(item.get("description") or ""),
                str(item.get("source_url") or ""), item.get("source_id"),
                item.get("release_date"), str(item.get("date_kind") or "unknown"),
                _json_text(item.get("genres"), "[]"), float(item.get("score") or 0),
                _json_text(item.get("explanation"), "[]"), str(item.get("status") or "new"),
                str(item.get("normalized_key") or f"fixture {item['id']}"),
                item.get("created_at"), item.get("updated_at"),
                str(item.get("isbn13") or ""), str(item.get("isbn10") or ""),
            )
            for item in snapshot.get("candidates", [])
        ],
    )
    con.executemany(
        """INSERT INTO candidate_quality(
            candidate_id,quality_status,quality_score,metadata_confidence,work_id,
            provider,isbn13,isbn10,title_match,author_match
        ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                int(item["candidate_id"]), str(item.get("quality_status") or "pending"),
                float(item.get("quality_score") or 0),
                float(item.get("metadata_confidence") or 0), str(item.get("work_id") or ""),
                str(item.get("provider") or ""), str(item.get("isbn13") or ""),
                str(item.get("isbn10") or ""), float(item.get("title_match") or 0),
                float(item.get("author_match") or 0),
            )
            for item in snapshot.get("candidate_quality", [])
        ],
    )
    con.executemany(
        """INSERT INTO reads(id,title,author,rating,read_at,isbn,source,created_at,openlibrary_work_id)
        VALUES(?,?,?,?,?,?,?,?,?)""",
        [
            (
                int(item["id"]), str(item.get("title") or ""),
                str(item.get("author") or ""), item.get("rating"), item.get("read_at"),
                item.get("isbn"), str(item.get("source") or ""), item.get("created_at"),
                item.get("openlibrary_work_id"),
            )
            for item in snapshot.get("reads", [])
        ],
    )
    con.executemany(
        """INSERT INTO feedback(id,candidate_id,action,created_at,previous_status,undone_at)
        VALUES(?,?,?,?,?,?)""",
        [
            (
                int(item["id"]), int(item["candidate_id"]), str(item.get("action") or ""),
                item.get("created_at"), item.get("previous_status"), item.get("undone_at"),
            )
            for item in snapshot.get("feedback", [])
        ],
    )
    candidate_by_id = {int(item["id"]): item for item in snapshot.get("candidates", [])}
    con.executemany(
        """INSERT INTO recommendation_events(
            id,event_key,candidate_id,event_type,value,occurred_at,title,author,
            genres,description,metadata
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (
                int(item["id"]), item.get("event_key"), int(item["candidate_id"]),
                str(item.get("event_type") or ""), item.get("value"),
                item.get("occurred_at"),
                str(candidate_by_id.get(int(item["candidate_id"]), {}).get("title") or ""),
                str(candidate_by_id.get(int(item["candidate_id"]), {}).get("author") or ""),
                _json_text(candidate_by_id.get(int(item["candidate_id"]), {}).get("genres"), "[]"),
                str(candidate_by_id.get(int(item["candidate_id"]), {}).get("description") or ""),
                _json_text(item.get("metadata"), "{}"),
            )
            for item in snapshot.get("recommendation_events", [])
        ],
    )
    con.executemany(
        """INSERT INTO embeddings(
            entity_type,entity_id,backend,model,vector,dimensions,content_hash,updated_at
        ) VALUES(?,?,?,?,?,?,?,?)""",
        [
            (
                str(item["entity_type"]), int(item["entity_id"]), str(item["backend"]),
                str(item["model"]), base64.b64decode(item["vector"], validate=True),
                int(item["dimensions"]), str(item["content_hash"]), item.get("updated_at"),
            )
            for item in snapshot.get("embeddings", [])
        ],
    )
    con.executemany(
        "INSERT INTO settings(key,value) VALUES(?,?)",
        [
            (str(item["key"]), str(item.get("value") or ""))
            for item in snapshot.get("settings", [])
        ],
    )
    return con


def _as_datetime(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)


def _cache_items(
    base_rows: list[dict[str, Any]],
    events: list[dict[str, Any]],
    connection: sqlite3.Connection,
    *,
    corrected_event_key: bool = False,
) -> tuple[dict[int, np.ndarray], dict[int, np.ndarray]]:
    # Parity path intentionally passes event rows unchanged. Production cache
    # lookup keys every item by item.id, so event vectors use event-row IDs.
    cache_items = [*base_rows, *events]
    if corrected_event_key:
        # Diagnostic only: normalize lookup IDs, but keep the original events
        # intact so event.id continues to define ordering and supersession.
        cache_items = [
            *base_rows,
            *[{**event, "id": int(event["candidate_id"])} for event in events],
        ]
    cached = load_cached_candidate_vectors(cache_items, connection)
    interaction_vectors = {
        int(event["candidate_id"]): cached[int(event["candidate_id"])]
        for event in events
        if int(event["candidate_id"]) in cached
    }
    return cached, interaction_vectors


def _apply_serving_stages(
    base_rows: list[dict[str, Any]],
    events: list[dict[str, Any]],
    candidate_vectors: Mapping[int, Any],
    *,
    now: datetime,
    runtime: Mapping[str, Any],
    interaction_enabled: bool = True,
    slate_enabled: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    if interaction_enabled:
        interaction_vectors = {
            int(event["candidate_id"]): candidate_vectors[int(event["candidate_id"])]
            for event in events
            if int(event["candidate_id"]) in candidate_vectors
        }
        personalized, learning = personalize_recommendations(
            base_rows, events, now=now,
            interaction_vectors=interaction_vectors,
            candidate_vectors=candidate_vectors,
        )
    else:
        personalized = [dict(item) for item in base_rows]
        learning = {
            "applied": False, "observed_books": 0, "qualified_features": 0,
            "semantic_evidence_books": 0, "semantic_adjusted_candidates": 0,
            "adjusted_candidates": 0, "max_score_adjustment": 0.0,
        }
    if slate_enabled:
        recommended = [item for item in personalized if item.get("status") == "recommended"]
        if recommended:
            diversified, slate = diversify_discovery_slate(recommended, candidate_vectors)
            result = diversified + [
                item for item in personalized if item.get("status") != "recommended"
            ]
        else:
            slate = empty_diagnostics("no_recommended_candidates")
            result = personalized
    else:
        slate = empty_diagnostics("ablation_disabled")
        result = personalized
    epsilon = (
        float(runtime.get("exploration_epsilon") or 0)
        if runtime.get("exploration_enabled") else 0.0
    )
    result = epsilon_tail_explore(
        result, epsilon=epsilon,
        stable_top_k=int(runtime.get("exploration_stable_top_k") or 4),
        rng=random.Random(20261002),
    )
    return result, learning, slate


def _signature(rows: Iterable[Mapping[str, Any]]) -> list[tuple[Any, ...]]:
    return [
        (
            int(item["id"]), round(float(item.get("score") or 0), 4),
            str(item.get("status") or ""),
            None if item.get("metadata_confidence") is None
            else round(float(item["metadata_confidence"]), 4),
        )
        for item in rows
    ]


def _reference_ids(values: Iterable[Any]) -> set[int]:
    result: set[int] = set()
    for item in values:
        value = item.get("id", item.get("candidate_id")) if isinstance(item, Mapping) else item
        if value is not None:
            result.add(int(value))
    return result


def _compare_reference(generated: Mapping[str, Any], reference: Mapping[str, Any]) -> dict[str, Any]:
    stages: dict[str, Any] = {}
    for stage in ("base", "personalized", "final"):
        expected = _signature(reference.get(stage, []))
        actual = _signature(generated.get(stage, []))
        stages[stage] = {
            "exact": actual == expected,
            "expected_rows": len(expected),
            "actual_rows": len(actual),
            "mismatched_rows": sum(a != b for a, b in zip(expected, actual))
            + abs(len(expected) - len(actual)),
        }
    for stage, key in (("interaction_diagnostics", "learning"), ("slate_diagnostics", "slate")):
        stages[stage] = {"exact": generated.get(key, {}) == reference.get(stage, {})}
    for stage in ("vectors", "interaction_vectors"):
        expected = _reference_ids(reference.get(stage, []))
        actual = _reference_ids(generated.get(stage, []))
        stages[stage] = {
            "exact": actual == expected,
            "expected_count": len(expected),
            "actual_count": len(actual),
            "symmetric_difference_count": len(actual ^ expected),
        }
    return {"exact": all(value.get("exact") for value in stages.values()), "stages": stages}


def _score_eligible_candidates(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    query = """
        SELECT c.*, s.name source_name, s.weight source_weight,
               q.work_id quality_work_id, q.provider quality_provider,
               q.isbn13 quality_isbn13, q.isbn10 quality_isbn10,
               CASE WHEN q.quality_score>0 THEN q.quality_score
                    WHEN q.quality_status='accepted' THEN 0.85 ELSE 0 END
                    AS catalog_confidence
        FROM candidates c
        JOIN sources s ON s.id=c.source_id
        JOIN candidate_quality q ON q.candidate_id=c.id
        WHERE c.status IN ('new','recommended')
          AND q.quality_status='accepted'
          AND s.enabled=1
    """
    read_keys = book_identity_match_index(
        [dict(item) for item in connection.execute("SELECT * FROM reads").fetchall()]
    )
    return [
        dict(item) for item in connection.execute(query).fetchall()
        if not book_row_identity_match_keys(dict(item)) & read_keys
    ]


def _vector_record(
    snapshot: Mapping[str, Any],
    entity_type: str,
    item: Mapping[str, Any],
    *,
    backend: str,
    model: str,
) -> tuple[np.ndarray | None, str, str | None]:
    match = next(
        (
            vector for vector in snapshot.get("embeddings", [])
            if vector.get("entity_type") == entity_type
            and int(vector.get("entity_id", -1)) == int(item.get("id", -2))
            and vector.get("backend") == backend
            and vector.get("model") == model
        ),
        None,
    )
    if match is None:
        return None, "missing", None
    if match.get("content_hash") != content_hash(document(dict(item))):
        return None, "stale", match.get("updated_at")
    raw = base64.b64decode(match["vector"], validate=True)
    if len(raw) % np.dtype(np.float32).itemsize:
        return None, "invalid", match.get("updated_at")
    vector = np.frombuffer(raw, dtype=np.float32)
    if vector.size == 0 or not np.isfinite(vector).all():
        return None, "invalid", match.get("updated_at")
    return vector, ("zero" if float(np.linalg.norm(vector)) == 0 else "fresh"), match.get("updated_at")


def _vector_summary(
    snapshot: Mapping[str, Any],
    *,
    rated_reads: list[dict[str, Any]],
    active_candidates: list[dict[str, Any]],
    serving_candidates: list[dict[str, Any]],
    events: list[dict[str, Any]],
    backend: str,
    model: str,
    now: datetime,
) -> dict[str, Any]:
    def states(kind: str, items: list[dict[str, Any]]) -> tuple[Counter[str], list[float]]:
        counts: Counter[str] = Counter()
        ages: list[float] = []
        for item in items:
            _vector, state, updated = _vector_record(
                snapshot, kind, item, backend=backend, model=model
            )
            counts[state] += 1
            if updated:
                try:
                    ages.append(max(0.0, (now - _as_datetime(updated)).total_seconds() / 86400))
                except (TypeError, ValueError):
                    pass
        return counts, ages

    read_states, read_ages = states("read", rated_reads)
    active_states, active_ages = states("candidate", active_candidates)
    serving_states, serving_ages = states("candidate", serving_candidates)
    event_ids = {int(event["candidate_id"]) for event in events}
    event_candidates = [
        item for item in snapshot.get("candidates", []) if int(item["id"]) in event_ids
    ]
    event_states, event_ages = states("candidate", event_candidates)
    ages = read_ages + active_ages + serving_ages + event_ages
    return {
        "configured_backend": backend,
        "configured_model": model,
        "rated_reads": dict(read_states),
        "active_score_population": dict(active_states),
        "currently_recommended_population": dict(serving_states),
        "unique_event_candidate_cache": dict(event_states),
        "cache_age_days": {
            "count": len(ages),
            "under_30_days": sum(age < 30 for age in ages),
            "30_to_180_days": sum(30 <= age < 180 for age in ages),
            "180_days_or_more": sum(age >= 180 for age in ages),
            "median": round(statistics.median(ages), 2) if ages else None,
            "maximum": round(max(ages), 2) if ages else None,
        },
    }


def _tie_density(items: list[Mapping[str, Any]]) -> dict[str, int]:
    counts = Counter(round(float(item.get("score") or 0), 1) for item in items)
    return {
        "items": len(items),
        "unique_rounded_scores": len(counts),
        "tied_score_groups": sum(count > 1 for count in counts.values()),
        "items_in_tied_groups": sum(count for count in counts.values() if count > 1),
    }


def _score_change_summary(baseline: Iterable[Mapping[str, Any]], candidate: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    left = {int(item["id"]): float(item.get("score") or 0) for item in baseline}
    right = {int(item["id"]): float(item.get("score") or 0) for item in candidate}
    ids = left.keys() & right.keys()
    deltas = [right[item_id] - left[item_id] for item_id in ids]
    absolute = [abs(value) for value in deltas]
    return {
        "shared_candidates": len(deltas),
        "changed_candidates": sum(value != 0 for value in deltas),
        "mean_delta": round(statistics.fmean(deltas), 4) if deltas else None,
        "mean_absolute_delta": round(statistics.fmean(absolute), 4) if absolute else None,
        "median_absolute_delta": round(statistics.median(absolute), 4) if absolute else None,
        "maximum_absolute_delta": round(max(absolute), 4) if absolute else None,
    }


def _rank_movement(baseline: Iterable[Mapping[str, Any]], candidate: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    left = [int(item["id"]) for item in baseline]
    right = [int(item["id"]) for item in candidate]
    lp = {item_id: rank for rank, item_id in enumerate(left, 1)}
    rp = {item_id: rank for rank, item_id in enumerate(right, 1)}
    shared = lp.keys() & rp.keys()
    moves = [abs(lp[item_id] - rp[item_id]) for item_id in shared]
    return {
        "shared_candidates": len(shared),
        "top8_overlap": len(set(left[:8]) & set(right[:8])),
        "top20_overlap": len(set(left[:20]) & set(right[:20])),
        "moved_candidates": sum(value != 0 for value in moves),
        "mean_absolute_displacement": round(statistics.fmean(moves), 4) if moves else None,
        "maximum_displacement": max(moves) if moves else None,
    }


def _metadata_slices(baseline: list[Mapping[str, Any]], candidate: list[Mapping[str, Any]]) -> dict[str, Any]:
    old = {int(item["id"]): float(item.get("score") or 0) for item in baseline}
    original_confidence = {int(item["id"]): item.get("metadata_confidence") for item in baseline}
    bins: dict[str, list[float]] = defaultdict(list)
    for item in candidate:
        candidate_id = int(item["id"])
        if candidate_id not in old:
            continue
        try:
            confidence = float(original_confidence[candidate_id] or 0)
        except (TypeError, ValueError):
            confidence = 0
        label = (
            "<0.25" if confidence < 0.25 else
            "0.25-0.50" if confidence < 0.50 else
            "0.50-0.75" if confidence < 0.75 else "0.75+"
        )
        bins[label].append(float(item.get("score") or 0) - old[candidate_id])
    return {
        key: {
            "candidates": len(values),
            "mean_score_delta": round(statistics.fmean(values), 4) if values else None,
            "changed_candidates": sum(value != 0 for value in values),
        }
        for key, values in sorted(bins.items())
    }


def _author_repeat_slices(items: list[Mapping[str, Any]]) -> dict[str, int]:
    keys = {
        int(item["id"]): book_identity("", item.get("author", "")).split(chr(31), 1)[-1]
        for item in items if item.get("status") == "recommended"
    }
    counts = Counter(value for value in keys.values() if value)
    groups: Counter[str] = Counter()
    for author in keys.values():
        count = counts.get(author, 0)
        groups["one_candidate" if count == 1 else "two_candidates" if count == 2 else "three_or_more"] += 1
    return dict(groups)


def _known_author_slices(baseline, candidate, reads):
    known = {book_identity("", read.get("author", "")) for read in reads}
    result = {}
    for label, is_known in (("known_author", True), ("unfamiliar_author", False)):
        left = [item for item in baseline if item.get("status") == "recommended" and (book_identity("", item.get("author", "")) in known) == is_known]
        ids = {int(item["id"]) for item in left}
        right = [item for item in candidate if int(item["id"]) in ids]
        result[label] = {"candidates": len(left), "score_changes": _score_change_summary(left, right)}
    return result


def _rank_recomputed(
    arm: str,
    *,
    reads: list[dict[str, Any]],
    read_vectors: list[np.ndarray],
    candidate_rows: list[dict[str, Any]],
    candidate_vectors: list[np.ndarray],
) -> list[dict[str, Any]]:
    candidates = [dict(item) for item in candidate_rows]
    if arm == "source_neutral":
        for item in candidates:
            item["source_weight"] = 1.0
    if arm == "metadata_neutral":
        # The ranker's documented ledger-free path returns confidence=1.
        # Only these private scorer inputs change; quality/identity eligibility
        # remains exactly the same in the fixture database.
        for item in candidates:
            item["catalog_confidence"] = None
    return rank_candidates(reads, read_vectors, candidates, candidate_vectors)


def _recomputed_arms(
    snapshot: Mapping[str, Any],
    connection: sqlite3.Connection,
    *,
    scope: str,
    persisted_base: list[dict[str, Any]],
    events: list[dict[str, Any]],
    now: datetime,
    backend: str,
    model: str,
    runtime: Mapping[str, Any],
    corrected_event_key: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    rated_reads = [
        dict(item) for item in connection.execute(
            "SELECT * FROM reads WHERE rating BETWEEN 1 AND 5 ORDER BY id"
        ).fetchall()
    ]
    active = _score_eligible_candidates(connection)
    active_by_id = {int(item["id"]): item for item in active}
    current_recommended = [
        dict(item) for item in persisted_base if item.get("status") == "recommended"
    ]
    read_vectors: list[np.ndarray] = []
    read_states: Counter[str] = Counter()
    for item in rated_reads:
        vector, state, _updated = _vector_record(
            snapshot, "read", item, backend=backend, model=model
        )
        read_states[state] += 1
        if vector is not None:
            read_vectors.append(vector)
    reads_complete = (
        len(read_vectors) == len(rated_reads)
        and bool(read_vectors)
        and all(vector.size == read_vectors[0].size for vector in read_vectors)
    )
    dimensions = read_vectors[0].size if reads_complete else None
    candidate_states: Counter[str] = Counter()
    vectors_by_id: dict[int, np.ndarray] = {}
    for item in current_recommended:
        candidate_id = int(item["id"])
        scorer_item = active_by_id.get(candidate_id)
        if scorer_item is None:
            candidate_states["not_in_score_population"] += 1
            continue
        vector, state, _updated = _vector_record(
            snapshot, "candidate", scorer_item, backend=backend, model=model
        )
        if vector is not None and dimensions is not None and vector.size != dimensions:
            state = "incompatible_dimensions"
        candidate_states[state] += 1
        if vector is not None and dimensions is not None and vector.size == dimensions:
            vectors_by_id[candidate_id] = vector
    common_order = [
        int(item["id"]) for item in current_recommended
        if int(item["id"]) in vectors_by_id
    ] if reads_complete else []
    common_candidates = [active_by_id[item_id] for item_id in common_order]
    common_vectors = [vectors_by_id[item_id] for item_id in common_order]

    coverage = {
        "scope": scope,
        "rated_reads": len(rated_reads),
        "rated_read_vector_states": dict(read_states),
        "rated_read_vectors_complete": reads_complete,
        "active_score_eligible_candidates": len(active),
        "currently_recommended_candidates": len(current_recommended),
        "recommended_in_score_population": sum(int(item["id"]) in active_by_id for item in current_recommended),
        "common_complete_recompute_candidates": len(common_order),
        "dropped_recommended_candidates": len(current_recommended) - len(common_order),
        "recommended_candidate_vector_states": dict(candidate_states),
        "new_candidates_not_promoted": sum(item.get("status") == "new" for item in active),
        "common_cohort": "same currently-recommended IDs with fresh current-hash read and candidate vectors",
        "score_rounding": "rank_candidates rounds base score to one decimal before interaction personalization",
    }
    if not reads_complete or not common_candidates:
        return {"status": "unidentifiable_from_cached_vectors", "arms": {}}, coverage

    base_by_arm: dict[str, list[dict[str, Any]]] = {}
    arm_stages: dict[str, dict[str, Any]] = {}
    # Score each arm on exactly the same currently-recommended population.
    for arm in RECOMPUTED_ARMS:
        rescored = _rank_recomputed(
            arm, reads=rated_reads, read_vectors=read_vectors, candidate_rows=common_candidates,
            candidate_vectors=common_vectors,
        )
        # Rebuild the production SQL order, including status/progress priority
        # and ties. Sorting old rows by their new scores can preserve stale
        # tie order and makes interaction_off an unintended second ablation.
        with _fixture_connection(snapshot) as arm_connection:
            for ranked in rescored:
                arm_connection.execute(
                    "UPDATE candidates SET score=?,explanation=? WHERE id=?",
                    (ranked["score"], json.dumps(ranked["explanation"]), ranked["id"]),
                )
                arm_connection.execute(
                    "UPDATE candidate_quality SET metadata_confidence=? WHERE candidate_id=?",
                    (ranked["metadata_confidence"], ranked["id"]),
                )
            base_rows = recommendation_list(
                arm_connection, status="recommended" if scope == "recommended" else None,
                limit=None,
            )
            common_ids = set(common_order)
            base_rows = [row for row in base_rows if row.get("status") != "recommended" or int(row["id"]) in common_ids]
        base_by_arm[arm] = base_rows
        cached, _event_vectors = _cache_items(base_rows, events, connection, corrected_event_key=corrected_event_key)
        enabled_interaction = arm != "interaction_off"
        enabled_slate = arm != "slate_off"
        output, learning, slate = _apply_serving_stages(
            base_rows, events, cached, now=now, runtime=runtime,
            interaction_enabled=enabled_interaction, slate_enabled=enabled_slate,
        )
        arm_stages[arm] = {
            "rows": output,
            "learning": learning,
            "slate": slate,
        }

    current = arm_stages["current"]["rows"]
    report_arms = {}
    for arm in RECOMPUTED_ARMS:
        stage = arm_stages[arm]
        output = stage["rows"]
        report_arms[arm] = {
            "rows": len(output),
            "recommended_rows": sum(item.get("status") == "recommended" for item in output),
            "learning": stage["learning"],
            "slate": stage["slate"],
            "tie_density": _tie_density(
                [item for item in output if item.get("status") == "recommended"]
            ),
            "score_changes_vs_recomputed_current": _score_change_summary(current, output),
            "rank_movement_vs_recomputed_current": _rank_movement(current, output),
            "metadata_slices": _metadata_slices(current, output),
            "author_repeat_slices": _author_repeat_slices(output),
            "known_author_slices": _known_author_slices(current, output, rated_reads),
        }

    # Separate current-only fallback diagnostic: fresh rows get recomputed
    # scores; stale/missing rows retain their exact persisted score/confidence.
    current_fresh = {
        int(item["id"]): item for item in base_by_arm["current"]
        if item.get("status") == "recommended"
    }
    full_recommended = []
    fallback_kept = 0
    for item in current_recommended:
        candidate_id = int(item["id"])
        replacement = current_fresh.get(candidate_id)
        if replacement is None:
            fallback_kept += 1
            full_recommended.append(dict(item))
        else:
            full_recommended.append(dict(replacement))
    full_recommended.sort(key=lambda item: -float(item.get("score") or 0))
    if scope == "all":
        full_base = full_recommended + [
            dict(item) for item in persisted_base
            if item.get("status") != "recommended"
        ]
    else:
        full_base = full_recommended
    fallback_cache, _ = _cache_items(full_base, events, connection, corrected_event_key=corrected_event_key)
    fallback_rows, fallback_learning, fallback_slate = _apply_serving_stages(
        full_base, events, fallback_cache, now=now, runtime=runtime
    )
    report = {
        "status": "complete",
        "corrected_event_vector_key": corrected_event_key,
        "arms": report_arms,
        "common_cohort_candidates": len(common_order),
        "full_pool_fallback_diagnostic": {
            "rows": len(fallback_rows),
            "persisted_score_and_confidence_rows_retained": fallback_kept,
            "learning": fallback_learning,
            "slate": fallback_slate,
            "score_changes_vs_persisted": _score_change_summary(persisted_base, fallback_rows),
            "rank_movement_vs_persisted": _rank_movement(persisted_base, fallback_rows),
        },
        "persisted_to_recomputed_drift": _score_change_summary(
            current_recommended,
            base_by_arm["current"],
        ),
    }
    return report, coverage


def _persisted_scope(
    snapshot: Mapping[str, Any],
    *,
    scope: str,
    reference: Mapping[str, Any],
    runtime: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    with _fixture_connection(snapshot) as connection:
        base = recommendation_list(
            connection=connection,
            status="recommended" if scope == "recommended" else None,
            limit=None,
            offset=0,
        )
        events = load_interaction_events(connection)
        cache, interaction_vectors = _cache_items(base, events, connection)
        now = _as_datetime(reference.get("clock_utc") or snapshot["exported_at_utc"])
        personalized, learning = personalize_recommendations(
            base, events, now=now,
            interaction_vectors=interaction_vectors, candidate_vectors=cache,
        )
        recs = [item for item in personalized if item.get("status") == "recommended"]
        if recs:
            diversified, slate = diversify_discovery_slate(recs, cache)
            final = diversified + [
                item for item in personalized if item.get("status") != "recommended"
            ]
        else:
            slate = empty_diagnostics("no_recommended_candidates")
            final = personalized
        final = epsilon_tail_explore(
            final,
            epsilon=(
                float(runtime.get("exploration_epsilon") or 0)
                if runtime.get("exploration_enabled") else 0.0
            ),
            stable_top_k=int(runtime.get("exploration_stable_top_k") or 4),
            rng=random.Random(20261002),
        )
        generated = {
            "base": base,
            "personalized": personalized,
            "final": final,
            "learning": learning,
            "slate": slate,
            "vectors": sorted(cache),
            "interaction_vectors": sorted(interaction_vectors),
        }
        parity = _compare_reference(generated, reference)

        without_interaction, no_interaction_learning, no_interaction_slate = _apply_serving_stages(
            base, events, cache, now=now, runtime=runtime, interaction_enabled=False
        )
        without_slate, no_slate_learning, no_slate_diagnostics = _apply_serving_stages(
            base, events, cache, now=now, runtime=runtime, slate_enabled=False
        )

        # Proposed loader correction diagnostic: event rows stay untouched for
        # latest-event semantics; only their cache lookup ID becomes candidate_id.
        corrected_cache, corrected_event_vectors = _cache_items(
            base, events, connection, corrected_event_key=True
        )
        corrected_personalized, corrected_learning = personalize_recommendations(
            base, events, now=now,
            interaction_vectors=corrected_event_vectors,
            candidate_vectors=corrected_cache,
        )
        corrected_recs = [
            item for item in corrected_personalized if item.get("status") == "recommended"
        ]
        if corrected_recs:
            corrected_diverse, corrected_slate = diversify_discovery_slate(
                corrected_recs, corrected_cache
            )
            corrected_final = corrected_diverse + [
                item for item in corrected_personalized
                if item.get("status") != "recommended"
            ]
        else:
            corrected_slate = empty_diagnostics("no_recommended_candidates")
            corrected_final = corrected_personalized
        corrected_final = epsilon_tail_explore(
            corrected_final, epsilon=0,
            stable_top_k=int(runtime.get("exploration_stable_top_k") or 4),
            rng=random.Random(20261002),
        )
        return {
            "parity": parity,
            "base_rows": len(base),
            "current_learning": learning,
            "current_slate": slate,
            "current_cache_rows": len(cache),
            "current_event_vector_rows": len(interaction_vectors),
            "interaction_off": {
                "learning": no_interaction_learning,
                "slate": no_interaction_slate,
                "score_changes": _score_change_summary(final, without_interaction),
                "rank_movement": _rank_movement(final, without_interaction),
            },
            "slate_off": {
                "learning": no_slate_learning,
                "slate": no_slate_diagnostics,
                "score_changes": _score_change_summary(final, without_slate),
                "rank_movement": _rank_movement(final, without_slate),
            },
            "corrected_loader_diagnostic": {
                "cache_rows": len(corrected_cache),
                "event_candidate_vector_rows": len(corrected_event_vectors),
                "learning": corrected_learning,
                "slate": corrected_slate,
                "score_changes_vs_current": _score_change_summary(final, corrected_final),
                "rank_movement_vs_current": _rank_movement(final, corrected_final),
            },
        }, base


def _telemetry_summary(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    runs = snapshot.get("recommendation_runs", [])
    impressions = snapshot.get("recommendation_impressions", [])
    outcomes = snapshot.get("recommendation_outcomes", [])
    by_kind: Counter[str] = Counter()
    by_label: Counter[str] = Counter()
    for outcome in outcomes:
        by_kind[str(outcome.get("label_kind") or "unknown")] += 1
        by_label[str(outcome.get("label"))] += 1
    propensity = []
    for item in impressions:
        try:
            number = float(item.get("propensity", 1))
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            propensity.append(number)
    return {
        "runs": len(runs),
        "impressions": len(impressions),
        "visible_impressions": sum(bool(item.get("visible_at")) for item in impressions),
        "outcomes": len(outcomes),
        "outcome_label_kind_counts": dict(by_kind),
        "outcome_label_counts": dict(by_label),
        "impression_propensities": {
            "count": len(propensity),
            "all_one": bool(propensity) and all(value == 1 for value in propensity),
            "below_one": sum(value < 1 for value in propensity),
            "minimum": min(propensity) if propensity else None,
        },
    }


def _source_hash_status(source_hashes: Any) -> dict[str, Any]:
    """Require the complete, fixed v1 source set before hashing any path."""

    if not isinstance(source_hashes, Mapping):
        provided_count = 0
        keys_exact = False
    else:
        provided_count = len(source_hashes)
        keys_exact = (
            all(isinstance(name, str) for name in source_hashes)
            and set(source_hashes) == RUNTIME_SOURCE_FILES_V1
        )
    if not keys_exact:
        return {
            "exact": False,
            "provided_count": provided_count,
            "expected_count": len(RUNTIME_SOURCE_FILES_V1),
            "key_set_exact": False,
        }

    package = Path(__file__).resolve().parents[1] / "afterword_engine"
    matches = {}
    for name in sorted(RUNTIME_SOURCE_FILES_V1):
        try:
            actual = hashlib.sha256((package / name).read_bytes()).hexdigest()
        except OSError:
            actual = ""
        matches[name] = actual == source_hashes[name]
    return {
        "exact": all(matches.values()),
        "provided_count": provided_count,
        "expected_count": len(RUNTIME_SOURCE_FILES_V1),
        "key_set_exact": True,
    }


def _snapshot_production_commit(snapshot: Mapping[str, Any]) -> str:
    """Return only an explicit full commit ID; frozen exports may omit it."""

    runtime = snapshot.get("runtime")
    runtime = runtime if isinstance(runtime, Mapping) else {}
    for value in (snapshot.get("production_commit"), runtime.get("git_commit")):
        if isinstance(value, str):
            candidate = value.strip()
            if re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", candidate):
                return candidate.lower()
    return "unknown"


def _run_snapshot(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    runtime = snapshot.get("runtime") or {}
    references = snapshot.get("serving_reference") or {}
    settings_by_key = {
        str(item["key"]): str(item.get("value") or "")
        for item in snapshot.get("settings", [])
    }
    backend = settings_by_key.get("embedding_backend", runtime.get("embedding_backend_default", ""))
    model = settings_by_key.get("embedding_model", runtime.get("embedding_model_default", ""))
    persisted: dict[str, Any] = {}
    recomputed: dict[str, Any] = {}
    coverage: dict[str, Any] = {}
    vectors: dict[str, Any] = {}
    corrected_scopes: dict[str, Any] = {}
    for scope in SCOPES:
        reference = references.get(scope, {})
        current, base = _persisted_scope(
            snapshot, scope=scope, reference=reference, runtime=runtime
        )
        persisted[scope] = current
        with _fixture_connection(snapshot) as connection:
            events = load_interaction_events(connection)
            rated_reads = [
                dict(item) for item in connection.execute(
                    "SELECT * FROM reads WHERE rating BETWEEN 1 AND 5 ORDER BY id"
                ).fetchall()
            ]
            active = _score_eligible_candidates(connection)
            vectors[scope] = _vector_summary(
                snapshot,
                rated_reads=rated_reads,
                active_candidates=active,
                serving_candidates=[
                    item for item in base if item.get("status") == "recommended"
                ],
                events=events,
                backend=backend,
                model=model,
                now=_as_datetime(reference.get("clock_utc") or snapshot["exported_at_utc"]),
            )
            recomputed[scope], coverage[scope] = _recomputed_arms(
                snapshot,
                connection,
                scope=scope,
                persisted_base=base,
                events=events,
                now=_as_datetime(reference.get("clock_utc") or snapshot["exported_at_utc"]),
                backend=backend,
                model=model,
                runtime=runtime,
            )
            corrected_scopes[scope], _ = _recomputed_arms(
                snapshot, connection, scope=scope, persisted_base=base,
                events=events, now=_as_datetime(reference.get("clock_utc") or snapshot["exported_at_utc"]),
                backend=backend, model=model, runtime=runtime, corrected_event_key=True,
            )
    source_status = _source_hash_status(snapshot.get("source_hashes"))
    return {
        "protocol": "complete-production-pipeline-audit-v1",
        "production_commit": _snapshot_production_commit(snapshot),
        "snapshot": {
            "exported_at_utc": snapshot.get("exported_at_utc"),
            "schema_version": snapshot.get("schema_version"),
            "integrity_ok": snapshot.get("integrity_ok"),
            "foreign_key_violations": snapshot.get("foreign_key_violations"),
            "source_hashes_exact": source_status["exact"],
            "source_hash_file_count": source_status["provided_count"],
            "source_hash_expected_file_count": source_status["expected_count"],
            "source_hash_key_set_exact": source_status["key_set_exact"],
            "runtime": runtime,
            "population_counts": {
                key: len(snapshot.get(key, []))
                for key in (
                    "reads", "candidates", "sources", "candidate_quality",
                    "feedback", "recommendation_events",
                    "recommendation_runs", "recommendation_impressions",
                    "recommendation_outcomes", "embeddings",
                )
            },
        },
        "persisted_score_serving": {
            "meaning": "captured Candidate.score plus exact current serving helpers",
            "scopes": persisted,
            "neutralization_limit": "stored rounded scores do not contain enough information to remove source or metadata effects exactly",
        },
        "recomputed_score_view": {
            "meaning": "five fixed arms on a shared complete-vector cohort of currently-recommended rows",
            "scopes": recomputed,
            "coverage": coverage,
            "vector_cache": vectors,
            "fallback_rule": "separate current-only diagnostic retains persisted score/confidence for stale or missing vectors; those rows are omitted from every fixed arm",
        },
        "historical_telemetry": _telemetry_summary(snapshot),
        "corrected_loader_recomputed_diagnostic": {
            "meaning": "same five fixed arms after normalizing only event vector lookup IDs; separate correctness diagnostic, no ranker fitting or utility claim",
            "scopes": corrected_scopes,
        },
        "interpretation_rule": "Rank movement and score changes describe ranking mechanics only. Missing outcomes remain unknown; no preference or utility improvement is inferred.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path, help="private allowlisted pipeline JSON")
    parser.add_argument("--output", type=Path, help="write aggregate JSON here; default stdout")
    args = parser.parse_args()
    snapshot = json.loads(args.snapshot.read_text())
    report = _run_snapshot(snapshot)
    if not report["snapshot"]["source_hashes_exact"]:
        raise ValueError("Local production source hashes do not match the capture")
    if not all(scope["parity"]["exact"] for scope in report["persisted_score_serving"]["scopes"].values()):
        raise ValueError("Current serving helper replay did not match the production reference")
    result = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output:
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(result)
    else:
        sys.stdout.write(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
