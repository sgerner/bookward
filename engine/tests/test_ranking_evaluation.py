"""Tests for created-at gated history in the ranking evaluation diagnostic."""
import importlib.util
import sqlite3
from pathlib import Path

import numpy as np

spec = importlib.util.spec_from_file_location(
    "evaluate_ranking_availability",
    Path(__file__).parents[1] / "scripts" / "evaluate_ranking.py",
)
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)


def test_engine_available_history_requires_prior_day_and_known_row_creation():
    vector = np.array([1.0, 0.0], dtype=np.float32)
    candidate = {"id": 9, "title": "Target", "author": "Writer"}
    records = [
        (evaluation.read_time("2025-05-01"),
         {"id": 1, "title": "Available", "author": "Other", "created_at": "2025-06-10T12:00:00Z"}, vector),
        (evaluation.read_time("2025-05-02"),
         {"id": 2, "title": "Created later", "author": "Other", "created_at": "2025-06-10T12:00:01Z"}, vector),
        (evaluation.read_time("2025-05-03"),
         {"id": 3, "title": "Unknown creation", "author": "Other"}, vector),
        (evaluation.read_time("2025-06-10"),
         {"id": 4, "title": "Same day", "author": "Other", "created_at": "2025-06-01T00:00:00Z"}, vector),
        (evaluation.read_time("2025-05-04"),
         {"id": 5, "title": "Target", "author": "Writer", "created_at": "2025-06-01T00:00:00Z"}, vector),
        (evaluation.read_time("2025-06-11"),
         {"id": 6, "title": "Future read", "author": "Other", "created_at": "2025-06-01T00:00:00Z"}, vector),
    ]

    history, counts = evaluation.feedback_history_available_at(
        records, evaluation.read_time("2025-06-10T12:00:00Z"), candidate
    )

    assert [read["id"] for read, _ in history] == [1]
    assert counts == {
        "available": 1,
        "unknown_created_at": 1,
        "created_after_action": 1,
        "same_day_read_date": 1,
        "same_identity": 1,
    }


def test_availability_replay_keeps_empty_history_candidates_and_counts_unknown(monkeypatch):
    vector = np.array([1.0, 0.0], dtype=np.float32)
    candidates = [
        {"id": 9, "title": "First target", "author": "Writer"},
        {"id": 10, "title": "Second target", "author": "Writer"},
    ]
    events = [
        {"id": 1, "candidate_id": 9, "action": "save", "created_at": "2025-06-10T12:00:00Z"},
        {"id": 2, "candidate_id": 10, "action": "reject", "created_at": "2025-06-11T12:00:00Z"},
    ]
    embeddings = [
        {"entity_type": "candidate", "entity_id": item["id"], "backend": "test", "model": "fixed",
         "content_hash": evaluation.content_hash(evaluation.document(item)),
         "vector": vector.tobytes()}
        for item in candidates
    ]
    data = {"candidates": candidates, "feedback": events, "embeddings": embeddings}
    records = [
        (evaluation.read_time("2025-05-01"),
         {"id": 1, "title": "Before action, imported later", "author": "Other",
          "created_at": "2025-06-10T12:00:01Z"}, vector),
        (evaluation.read_time("2025-05-02"),
         {"id": 2, "title": "Unknown import time", "author": "Other"}, vector),
    ]
    calls = []

    def ranker(history, read_vectors, queries, query_vectors):
        calls.append((len(history), read_vectors.shape, query_vectors.shape))
        return [{**queries[0], "score": 50.0 + len(history)}]

    monkeypatch.setattr("afterword_engine.ranking.rank_candidates", ranker)
    result = evaluation.feedback_check_engine_available_history(data, records, ("test", "fixed"))

    assert result["usable_candidates"] == 2
    assert result["saved"] == 1
    assert result["neighborhood"]["auc"] == 0.0
    assert result["history_availability"]["cases_with_no_available_history"] == 1
    assert result["history_availability"]["cases_with_unknown_prior_row_availability"] == 2
    assert result["history_availability"]["cases_with_rows_created_after_action"] == 1
    assert calls == [(0, (0, 2), (1, 2)), (1, (1, 2), (1, 2))]


def test_sqlite_corpus_loader_preserves_read_created_at(tmp_path):
    path = tmp_path / "study.sqlite"
    with sqlite3.connect(path) as con:
        con.executescript("""
            CREATE TABLE reads (
                id INTEGER, title TEXT, author TEXT, rating REAL, read_at TEXT,
                isbn TEXT, source TEXT, created_at TEXT
            );
            CREATE TABLE candidates (id INTEGER);
            CREATE TABLE feedback (id INTEGER);
            CREATE TABLE embeddings (entity_type TEXT, entity_id INTEGER);
            INSERT INTO reads VALUES (1, 'Read', 'Writer', 4, '2025-01-01', '', 'import', '2025-02-03T04:05:06Z');
        """)

    data = evaluation.load_corpus(path)

    assert data["reads"][0]["created_at"] == "2025-02-03T04:05:06Z"
