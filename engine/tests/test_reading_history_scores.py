import asyncio
from pathlib import Path
import sqlite3

import pytest
from fastapi.testclient import TestClient

from auth_helpers import authenticated_headers
from afterword_engine import database as engine_database, main, profiles
from afterword_engine.config import settings
from afterword_engine.database import initialize, transaction
from afterword_engine.embeddings import get_embedder
from afterword_engine.ranking import rank_candidates
from afterword_engine.scoring import cached_vectors
from afterword_engine.tenancy import profile_scope


@pytest.fixture()
def database(tmp_path: Path):
    main._RATED_READ_VECTOR_CACHE.clear()
    settings.db = str(tmp_path / "reading-scores.db")
    initialize()
    profiles.initialize_auth()
    yield settings.db
    main._RATED_READ_VECTOR_CACHE.clear()


def _seed_reads(count: int, *, rating: int = 5, prefix: str = "History Book"):
    with transaction() as con:
        con.execute("DELETE FROM reads")
        con.execute("DELETE FROM embeddings")
        con.executemany(
            "INSERT INTO reads(title,author,rating,read_at,source) VALUES(?,?,?,?,?)",
            [
                (
                    f"{prefix} {index:03d}",
                    f"Author {index % 17:02d}",
                    rating if index % 9 != 0 else max(1, (index % 5) + 1),
                    f"2026-01-{(index % 28) + 1:02d}",
                    "goodreads_csv",
                )
                for index in range(1, count + 1)
            ],
        )


def test_reading_history_scores_endpoint_accepts_200_limit_and_rejects_above_cap(database):
    _seed_reads(10)
    with TestClient(main.app) as client:
        headers = authenticated_headers()
        ok_response = client.get(
            "/api/reading-history/scores?offset=0&limit=200",
            headers=headers,
        )
        assert ok_response.status_code == 200
        assert len(ok_response.json()) == 10

        too_large = client.get(
            "/api/reading-history/scores?offset=0&limit=201",
            headers=headers,
        )
        assert too_large.status_code == 422


def test_100_read_profile_scores_in_single_pass_with_single_rated_read_scan_and_unchanged_math(
    database, monkeypatch
):
    _seed_reads(100)
    rated_read_queries: list[str] = []
    real_connect = main.connect

    class AuditingConnection:
        def __init__(self, inner: sqlite3.Connection):
            self._inner = inner

        def execute(self, sql: str, parameters=()):
            if "FROM reads WHERE rating BETWEEN 1 AND 5" in sql:
                rated_read_queries.append(sql)
            return self._inner.execute(sql, parameters)

        def __enter__(self):
            self._inner.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            return self._inner.__exit__(exc_type, exc, tb)

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

    monkeypatch.setattr(main, "connect", lambda: AuditingConnection(real_connect()))

    scores = asyncio.run(main.reading_history_scores(offset=0, limit=200))
    assert len(scores) == 100
    assert len(rated_read_queries) == 1

    # Verify leave-one-out ranking math matches rank_candidates directly.
    with real_connect() as con:
        items = [
            dict(row)
            for row in con.execute(
                "SELECT id,title,author,rating,read_at,source,created_at FROM reads "
                "ORDER BY COALESCE(read_at,created_at) DESC,id DESC LIMIT 200 OFFSET 0"
            ).fetchall()
        ]
        rated_reads = [
            dict(row)
            for row in con.execute(
                "SELECT * FROM reads WHERE rating BETWEEN 1 AND 5 ORDER BY id"
            ).fetchall()
        ]
    embedder = get_embedder("local", "local-hash-v1", "", "")
    read_vectors = asyncio.run(cached_vectors(embedder, "read", rated_reads))
    virtual_candidates = [
        {
            "id": -int(item["id"]),
            "title": item["title"],
            "author": item["author"],
            "description": "",
            "genres": "[]",
            "source_weight": 1,
        }
        for item in items
    ]
    candidate_vectors = asyncio.run(
        cached_vectors(embedder, "read_candidate", virtual_candidates)
    )
    expected = {
        -int(entry["id"]): entry["score"]
        for entry in rank_candidates(
            rated_reads,
            read_vectors,
            virtual_candidates,
            candidate_vectors,
            exclude_candidate_identity=True,
        )
    }
    assert {entry["id"]: entry["algorithm_score"] for entry in scores} == expected


def test_rated_read_vector_cache_reuses_vectors_across_batches_and_scopes_invalidation(
    database, tmp_path: Path, monkeypatch
):
    _seed_reads(230)
    read_vector_computations = 0
    real_cached_vectors = main.cached_vectors

    async def counting_cached_vectors(embedder, entity_type, items, expected_dimensions=None):
        nonlocal read_vector_computations
        if entity_type == "read":
            read_vector_computations += 1
        return await real_cached_vectors(
            embedder,
            entity_type,
            items,
            expected_dimensions=expected_dimensions,
        )

    monkeypatch.setattr(main, "cached_vectors", counting_cached_vectors)

    first_batch = asyncio.run(main.reading_history_scores(offset=0, limit=200))
    second_batch = asyncio.run(main.reading_history_scores(offset=200, limit=200))
    assert len(first_batch) == 200
    assert len(second_batch) == 30
    assert read_vector_computations == 1

    # Mutating `reads` invalidates the cache key.
    with transaction() as con:
        con.execute("UPDATE reads SET rating=2 WHERE id=(SELECT MIN(id) FROM reads)")
    asyncio.run(main.reading_history_scores(offset=0, limit=50))
    assert read_vector_computations == 2

    # Repeated call with unchanged `reads` hits the cache.
    asyncio.run(main.reading_history_scores(offset=50, limit=50))
    assert read_vector_computations == 2

    # Changing embedding model configuration invalidates the cache key.
    with transaction() as con:
        con.execute(
            "INSERT INTO settings(key,value) VALUES('embedding_model','local-hash-v2') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
    asyncio.run(main.reading_history_scores(offset=0, limit=50))
    assert read_vector_computations == 3

    # Switching tenant profile scope isolates cached vectors per tenant.
    second_account = profiles.create_account(
        "reader-two", "second-password-with-sufficient-length"
    )
    second_profile_id = second_account["profile_id"]
    with profile_scope(second_profile_id):
        initialize()
        _seed_reads(15, prefix="Second Tenant Book")
        tenant_scores = asyncio.run(main.reading_history_scores(offset=0, limit=200))
        assert len(tenant_scores) == 15
    assert read_vector_computations == 4

    # Returning to the original profile still reuses its cached entry without recomputing.
    asyncio.run(main.reading_history_scores(offset=0, limit=50))
    assert read_vector_computations == 4
