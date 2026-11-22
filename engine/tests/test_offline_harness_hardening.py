"""End-to-end regression tests for the hardened offline corpus and replay harness."""
from __future__ import annotations

import base64
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import sys

import numpy as np
import pytest

ENGINE = Path(__file__).parents[1]
SCRIPTS = ENGINE / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ENGINE))

import evaluate_embedding_representations as embedding_study
import evaluate_historical_ratings as historical
import evaluate_metric_facets as metric_facets
import evaluate_next_five_combinations as next_five_combo
import evaluate_ordinal_interests as ordinal
import evaluate_ranking as ranking_eval
import evaluate_ranking_experiments as ranking_experiments
import evaluate_ranking_synergy as synergy
from afterword_engine.embeddings import content_hash
from afterword_engine.ingestion import _read_metadata_identity_hash
from afterword_engine.ranking import rank_candidates
from afterword_engine.scoring import document


def _vec(x: float, y: float) -> np.ndarray:
    arr = np.array([x, y], dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    return arr / norm if norm else arr


def _embedding_row(entity_type: str, item: dict, vector: np.ndarray, *, backend="test", model="v1") -> dict:
    return {
        "entity_type": entity_type,
        "entity_id": int(item["id"]),
        "backend": backend,
        "model": model,
        "vector": vector.astype(np.float32).tobytes(),
        "dimensions": int(vector.size),
        "content_hash": content_hash(document(item)),
        "updated_at": "2025-01-01T00:00:00Z",
    }


def test_sqlite_loader_matches_enriched_json_export_and_preserves_verified_metadata(tmp_path):
    """Requirement 1 & AC 1: SQLite snapshot matches enriched JSON with zero sparse score drift."""
    sqlite_path = tmp_path / "snapshot.sqlite"
    json_path = tmp_path / "enriched.json"

    reads = [
        {
            "id": 1,
            "title": "Loved Foundation",
            "author": "Author Alpha",
            "rating": 5.0,
            "read_at": "2024-01-10",
            "isbn": "9780141036144",
            "source": "csv",
            "created_at": "2024-01-11T00:00:00Z",
            "openlibrary_work_id": "/works/OL100W",
        },
        {
            "id": 2,
            "title": "Disliked Detour",
            "author": "Author Beta",
            "rating": 1.0,
            "read_at": "2024-01-12",
            "isbn": "",
            "source": "csv",
            "created_at": "2024-01-13T00:00:00Z",
            "openlibrary_work_id": "",
        },
    ]
    read_metadata = [
        {
            "read_id": 1,
            "verified_work_id": "/works/OL100W",
            "identity_provider": "openlibrary",
            "identity_provider_id": "/works/OL100W",
            "identity_evidence": "{}",
            "metadata_provenance": "{}",
            "identity_hash": _read_metadata_identity_hash(reads[0]),
            "description": "An expansive galactic chronicle of psychohistory and empire.",
            "description_kind": "synopsis",
            "genres": '["Science Fiction", "Space Opera"]',
            "cover_url": "",
            "release_date": "1951",
            "date_kind": "year",
            "metadata_checked_at": "2024-02-01T00:00:00Z",
        }
    ]
    field_provenance = [
        {
            "id": 1,
            "entity_type": "read",
            "entity_id": 1,
            "field": "description",
            "provider": "openlibrary",
            "provider_id": "/works/OL100W",
            "confidence": 1.0,
            "source_payload": "{}",
            "verified_at": "2024-02-01T00:00:00Z",
        }
    ]
    sources = [
        {"id": 10, "name": "Trusted Editorial", "url": "https://example.com/a", "weight": 0.85},
        {"id": 11, "name": "Noisy Feed", "url": "https://example.com/b", "weight": 0.35},
    ]
    # Sparse candidate (id=102) has pending quality_score=0 -> catalog_confidence=0.0,
    # which must shrink its score toward 50 identically in SQLite and JSON.
    candidates_base = [
        {
            "id": 101,
            "title": "Verified Candidate",
            "author": "Author Gamma",
            "description": "A richly documented speculative novel with verified catalog metadata.",
            "genres": '["Science Fiction", "Adventure"]',
            "source_id": 10,
            "status": "recommended",
            "normalized_key": "verified candidate|author gamma",
            "isbn13": "9780316769488",
            "isbn10": "0316769487",
            "created_at": "2025-01-01T00:00:00Z",
        },
        {
            "id": 102,
            "title": "Sparse Unverified Candidate",
            "author": "Author Delta",
            "description": "",
            "genres": "[]",
            "source_id": 11,
            "status": "recommended",
            "normalized_key": "sparse unverified candidate|author delta",
            "isbn13": "",
            "isbn10": "",
            "created_at": "2025-01-01T00:00:00Z",
        },
        {
            "id": 103,
            "title": "Accepted Without Explicit Score",
            "author": "Author Epsilon",
            "description": "Accepted catalog entry without numeric quality_score.",
            "genres": '["Fantasy"]',
            "source_id": 10,
            "status": "recommended",
            "normalized_key": "accepted without explicit score|author epsilon",
            "isbn13": "",
            "isbn10": "",
            "created_at": "2025-01-01T00:00:00Z",
        },
    ]
    quality_rows = [
        {
            "candidate_id": 101,
            "quality_status": "accepted",
            "quality_score": 0.92,
            "metadata_confidence": 0.88,
            "work_id": "/works/OL901W",
            "provider": "openlibrary",
            "isbn13": "9780316769488",
            "isbn10": "0316769487",
        },
        {
            "candidate_id": 102,
            "quality_status": "pending",
            "quality_score": 0.0,
            "metadata_confidence": 0.35,
            "work_id": "",
            "provider": "",
            "isbn13": "",
            "isbn10": "",
        },
        {
            "candidate_id": 103,
            "quality_status": "accepted",
            "quality_score": 0.0,
            "metadata_confidence": 0.65,
            "work_id": "/works/OL903W",
            "provider": "openlibrary",
            "isbn13": "9780140449136",
            "isbn10": "0140449132",
        },
    ]
    feedback_rows = [
        {"id": 1, "candidate_id": 101, "action": "save", "created_at": "2025-02-01T12:00:00Z", "undone_at": None},
        {"id": 2, "candidate_id": 102, "action": "reject", "created_at": "2025-02-01T12:01:00Z", "undone_at": None},
    ]
    runs = [{"id": "run-abc12345", "policy": "default", "policy_version": "v1", "candidate_count": 2, "created_at": "2025-02-01T11:59:00Z"}]
    impressions = [
        {"id": 501, "run_id": "run-abc12345", "candidate_id": 101, "rank": 1, "score": 78.0, "propensity": 1.0, "presented_at": "2025-02-01T11:59:05Z", "visible_at": "2025-02-01T11:59:10Z"},
        {"id": 502, "run_id": "run-abc12345", "candidate_id": 102, "rank": 2, "score": 52.0, "propensity": 1.0, "presented_at": "2025-02-01T11:59:05Z", "visible_at": None},
    ]
    events = [
        {"id": 701, "event_key": "feedback:1", "run_id": "run-abc12345", "candidate_id": 101, "event_type": "save", "source": "ui", "occurred_at": "2025-02-01T12:00:00Z"},
        {"id": 702, "event_key": "feedback:2", "run_id": "run-abc12345", "candidate_id": 102, "event_type": "reject", "source": "ui", "occurred_at": "2025-02-01T12:01:00Z"},
    ]

    read_vecs = [_vec(1.0, 0.0), _vec(0.0, 1.0)]
    cand_vecs = [_vec(0.95, 0.05), _vec(1.0, 0.0), _vec(0.8, 0.2)]
    embeddings = [
        _embedding_row("read", reads[0], read_vecs[0]),
        _embedding_row("read", reads[1], read_vecs[1]),
        _embedding_row("candidate", candidates_base[0], cand_vecs[0]),
        _embedding_row("candidate", candidates_base[1], cand_vecs[1]),
        _embedding_row("candidate", candidates_base[2], cand_vecs[2]),
    ]

    with sqlite3.connect(sqlite_path) as con:
        con.executescript(
            """
            CREATE TABLE sources (id INTEGER PRIMARY KEY, name TEXT, url TEXT, weight REAL);
            CREATE TABLE candidates (
                id INTEGER PRIMARY KEY, title TEXT, author TEXT, description TEXT, genres TEXT,
                source_id INTEGER, status TEXT, normalized_key TEXT, isbn13 TEXT, isbn10 TEXT, created_at TEXT
            );
            CREATE TABLE candidate_quality (
                candidate_id INTEGER PRIMARY KEY, quality_status TEXT, quality_score REAL,
                metadata_confidence REAL, work_id TEXT, provider TEXT, isbn13 TEXT, isbn10 TEXT
            );
            CREATE TABLE reads (
                id INTEGER PRIMARY KEY, title TEXT, author TEXT, rating REAL, read_at TEXT,
                isbn TEXT, source TEXT, created_at TEXT, openlibrary_work_id TEXT
            );
            CREATE TABLE read_metadata (
                read_id INTEGER PRIMARY KEY, verified_work_id TEXT, identity_provider TEXT,
                identity_provider_id TEXT, identity_evidence TEXT, metadata_provenance TEXT,
                identity_hash TEXT, description TEXT, description_kind TEXT, genres TEXT,
                cover_url TEXT, release_date TEXT, date_kind TEXT, metadata_checked_at TEXT
            );
            CREATE TABLE metadata_field_provenance (
                id INTEGER PRIMARY KEY, entity_type TEXT, entity_id INTEGER, field TEXT,
                provider TEXT, provider_id TEXT, confidence REAL, source_payload TEXT, verified_at TEXT
            );
            CREATE TABLE feedback (
                id INTEGER PRIMARY KEY, candidate_id INTEGER, action TEXT, created_at TEXT, undone_at TEXT
            );
            CREATE TABLE recommendation_runs (
                id TEXT PRIMARY KEY, policy TEXT, policy_version TEXT, candidate_count INTEGER, created_at TEXT
            );
            CREATE TABLE recommendation_impressions (
                id INTEGER PRIMARY KEY, run_id TEXT, candidate_id INTEGER, rank INTEGER,
                score REAL, propensity REAL, presented_at TEXT, visible_at TEXT, UNIQUE(run_id, candidate_id)
            );
            CREATE TABLE recommendation_events (
                id INTEGER PRIMARY KEY, event_key TEXT UNIQUE, run_id TEXT, candidate_id INTEGER,
                event_type TEXT, source TEXT, occurred_at TEXT
            );
            CREATE TABLE embeddings (
                entity_type TEXT, entity_id INTEGER, backend TEXT, model TEXT,
                vector BLOB, dimensions INTEGER, content_hash TEXT, updated_at TEXT
            );
            """
        )
        for s in sources:
            con.execute("INSERT INTO sources VALUES (:id,:name,:url,:weight)", s)
        for c in candidates_base:
            con.execute(
                "INSERT INTO candidates VALUES (:id,:title,:author,:description,:genres,"
                ":source_id,:status,:normalized_key,:isbn13,:isbn10,:created_at)",
                c,
            )
        for q in quality_rows:
            con.execute(
                "INSERT INTO candidate_quality VALUES (:candidate_id,:quality_status,:quality_score,"
                ":metadata_confidence,:work_id,:provider,:isbn13,:isbn10)",
                q,
            )
        for r in reads:
            con.execute(
                "INSERT INTO reads VALUES (:id,:title,:author,:rating,:read_at,:isbn,:source,:created_at,:openlibrary_work_id)",
                r,
            )
        for rm in read_metadata:
            con.execute(
                "INSERT INTO read_metadata VALUES (:read_id,:verified_work_id,:identity_provider,"
                ":identity_provider_id,:identity_evidence,:metadata_provenance,:identity_hash,"
                ":description,:description_kind,:genres,:cover_url,:release_date,:date_kind,:metadata_checked_at)",
                rm,
            )
        for fp in field_provenance:
            con.execute(
                "INSERT INTO metadata_field_provenance VALUES (:id,:entity_type,:entity_id,:field,"
                ":provider,:provider_id,:confidence,:source_payload,:verified_at)",
                fp,
            )
        for fb in feedback_rows:
            con.execute("INSERT INTO feedback VALUES (:id,:candidate_id,:action,:created_at,:undone_at)", fb)
        for run in runs:
            con.execute("INSERT INTO recommendation_runs VALUES (:id,:policy,:policy_version,:candidate_count,:created_at)", run)
        for imp in impressions:
            con.execute(
                "INSERT INTO recommendation_impressions VALUES (:id,:run_id,:candidate_id,:rank,:score,:propensity,:presented_at,:visible_at)",
                imp,
            )
        for ev in events:
            con.execute(
                "INSERT INTO recommendation_events VALUES (:id,:event_key,:run_id,:candidate_id,:event_type,:source,:occurred_at)",
                ev,
            )
        for emb in embeddings:
            con.execute(
                "INSERT INTO embeddings VALUES (:entity_type,:entity_id,:backend,:model,:vector,:dimensions,:content_hash,:updated_at)",
                emb,
            )

    # Make SQLite file read-only on disk to verify ?mode=ro opens cleanly without write access.
    os.chmod(sqlite_path, 0o444)

    enriched_candidates = []
    src_map = {s["id"]: s for s in sources}
    qual_map = {q["candidate_id"]: q for q in quality_rows}
    for c in candidates_base:
        s = src_map[c["source_id"]]
        q = qual_map[c["id"]]
        cat_conf = q["quality_score"] if q["quality_score"] > 0 else (0.85 if q["quality_status"] == "accepted" else 0.0)
        enriched_candidates.append({
            **c,
            "source_name": s["name"],
            "source_weight": s["weight"],
            "quality_work_id": q["work_id"],
            "quality_provider": q["provider"],
            "quality_isbn13": q["isbn13"],
            "quality_isbn10": q["isbn10"],
            "quality_score": q["quality_score"],
            "quality_status": q["quality_status"],
            "metadata_confidence": q["metadata_confidence"],
            "catalog_confidence": cat_conf,
        })
    json_payload = {
        "reads": reads,
        "candidates": enriched_candidates,
        "read_metadata": read_metadata,
        "metadata_field_provenance": field_provenance,
        "feedback": feedback_rows,
        "recommendation_runs": runs,
        "recommendation_impressions": impressions,
        "recommendation_events": events,
        "embeddings": [
            {**emb, "vector": base64.b64encode(emb["vector"]).decode("ascii")}
            for emb in embeddings
        ],
    }
    json_path.write_text(json.dumps(json_payload))

    sqlite_data = ranking_eval.load_corpus(sqlite_path)
    json_data = ranking_eval.load_corpus(json_path)

    for key in (
        "source_weight",
        "source_name",
        "catalog_confidence",
        "quality_work_id",
        "quality_provider",
        "quality_isbn13",
        "quality_isbn10",
        "quality_score",
        "quality_status",
        "metadata_confidence",
        "isbn13",
        "isbn10",
    ):
        assert [c[key] for c in sqlite_data["candidates"]] == [
            c[key] for c in json_data["candidates"]
        ], f"Mismatch on candidate key {key}"

    assert sqlite_data["read_metadata"] == json_data["read_metadata"]
    assert sqlite_data["metadata_field_provenance"] == json_data["metadata_field_provenance"]
    assert [f["run_id"] for f in sqlite_data["feedback"]] == ["run-abc12345", "run-abc12345"]
    assert [f["visible_at"] for f in sqlite_data["feedback"]] == ["2025-02-01T11:59:10Z", None]
    assert sqlite_data["feedback"] == json_data["feedback"]

    # Zero sparse-candidate score drift between SQLite and enriched JSON:
    sqlite_scores = [
        row["score"]
        for row in rank_candidates(sqlite_data["reads"], read_vecs, sqlite_data["candidates"], cand_vecs)
    ]
    json_scores = [
        row["score"]
        for row in rank_candidates(json_data["reads"], read_vecs, json_data["candidates"], cand_vecs)
    ]
    assert sqlite_scores == json_scores
    # Sparse candidate 102 must NOT get unshrunk metadata_confidence=1.0 score:
    unshrunk_sparse = rank_candidates(
        sqlite_data["reads"],
        read_vecs,
        [{k: v for k, v in candidates_base[1].items()}],
        [cand_vecs[1]],
    )[0]["score"]
    assert sqlite_scores[1] < unshrunk_sparse

    # 100% verified read_metadata retention in evaluate_embedding_representations.build_text_views:
    row_records = [
        (date(2024, 1, 10), sqlite_data["reads"][0], read_vecs[0]),
        (date(2024, 1, 12), sqlite_data["reads"][1], read_vecs[1]),
    ]
    sqlite_views = embedding_study.build_text_views(sqlite_data, row_records)
    json_views = embedding_study.build_text_views(json_data, row_records)
    assert sqlite_views["metadata_mask"].tolist() == [True, False]
    assert sqlite_views["verified_metadata_text"] == json_views["verified_metadata_text"]
    assert "psychohistory" in sqlite_views["verified_metadata_text"][0]


def test_prepare_excludes_subtitle_alias_suffix_isbn_and_work_id_duplicates_and_replay_blocks_cross_split():
    """Requirement 2 & AC 2: Exclude all five duplicate edition classes before splitting."""
    reads = []
    read_metadata = []
    embeddings = []
    start_day = date(2020, 1, 1)

    for idx in range(1, 96):
        item = {
            "id": idx,
            "title": f"Unique Work Number {idx}",
            "author": f"Author {idx % 19}",
            "rating": float((idx % 5) + 1),
            "read_at": (start_day + timedelta(days=idx)).isoformat(),
            "isbn": "",
        }
        reads.append(item)
        embeddings.append(_embedding_row("read", item, _vec(1.0, float(idx % 7) + 0.1)))

    # Five canonical early works (IDs 201..205):
    early_specs = [
        (201, "The Left Hand of Darkness", "Ursula K. Le Guin", "", "2020-05-01"),
        (202, "1984", "George Orwell", "", "2020-05-02"),
        (203, "Mother Night", "Kurt Vonnegut", "", "2020-05-03"),
        (204, "Original Pressing", "Elena Ferrante", "0141036141", "2020-05-04"),
        (205, "First English Translation", "Stanislaw Lem", "", "2020-05-05"),
    ]
    # Five late duplicate variants (IDs 301..305):
    # 301: Subtitle variant matched by ReadIdentityIndex
    # 302: Numeric alias variant
    # 303: Generational suffix variant
    # 304: ISBN-10 <-> ISBN-13 variant (0141036141 == 9780141036144)
    # 305: Verified Open Library work-ID variant via read_metadata
    late_specs = [
        (301, "The Left Hand of Darkness: A Novel", "Ursula K. Le Guin", "", "2022-08-01"),
        (302, "Nineteen Eighty-Four", "George Orwell", "", "2022-08-02"),
        (303, "Mother Night", "Kurt Vonnegut Jr.", "", "2022-08-03"),
        (304, "Completely Retitled Reissue", "Elena Ferrante", "9780141036144", "2022-08-04"),
        (305, "Alternate Retitled Translation", "Stanislaw Lem", "", "2022-08-05"),
    ]
    for rid, title, author, isbn, read_at in early_specs + late_specs:
        item = {
            "id": rid,
            "title": title,
            "author": author,
            "rating": 5.0,
            "read_at": read_at,
            "isbn": isbn,
        }
        reads.append(item)
        embeddings.append(_embedding_row("read", item, _vec(0.9, 0.4)))
        if rid in {205, 305}:
            read_metadata.append({
                "read_id": rid,
                "verified_work_id": "/works/OL424242W",
                "identity_provider": "openlibrary",
                "identity_provider_id": "/works/OL424242W",
                "identity_hash": _read_metadata_identity_hash(item),
                "description": "",
                "genres": "[]",
            })

    records, selected, excluded = ranking_eval.prepare(
        {"reads": reads, "read_metadata": read_metadata, "embeddings": embeddings},
        "test",
        "v1",
    )
    kept_ids = {item["id"] for _, item, _ in records}
    assert selected == ("test", "v1")
    assert len(records) == 100
    assert excluded["duplicate_work"] == 5
    assert {201, 202, 203, 204, 205}.issubset(kept_ids)
    assert kept_ids.isdisjoint({301, 302, 303, 304, 305})

    # Direct replay guard: even if raw records contain a cross-split edition pair,
    # historical.replay excludes the matching earlier edition from the query's history.
    raw_records = [
        (ranking_eval.read_time("2020-01-01"), {"id": 1, "title": "Unrelated Baseline", "author": "Other", "rating": 2.0}, _vec(0.0, 1.0)),
        (ranking_eval.read_time("2020-01-02"), {"id": 2, "title": "1984", "author": "George Orwell", "rating": 5.0}, _vec(1.0, 0.0)),
        (ranking_eval.read_time("2020-01-03"), {"id": 3, "title": "Nineteen Eighty-Four: A Novel", "author": "George Orwell", "rating": 5.0}, _vec(1.0, 0.0)),
    ]
    observed_histories = []
    orig_rank = historical.rank_candidates

    def spy_rank(history_rows, history_vecs, query_rows, query_vecs):
        observed_histories.append(([r["id"] for r in query_rows], [h["id"] for h in history_rows]))
        return orig_rank(history_rows, history_vecs, query_rows, query_vecs)

    historical.rank_candidates = spy_rank
    try:
        historical.replay(raw_records, min_history=1)
    finally:
        historical.rank_candidates = orig_rank
    assert ([3], [1]) in observed_histories


def test_feedback_streaks_preserve_initial_timestamps_exclude_post_imports_and_stratify_regimes():
    """Requirements 4, 5, 6, 7 & AC 4 across evaluate_ranking and evaluate_ranking_experiments."""
    pos_vec = _vec(1.0, 0.0)
    neg_vec = _vec(0.0, 1.0)

    # Backlog imported at 2025-06-10T00:00:00Z (read_at in 2021):
    # High ratings align with pos_vec; low ratings align with neg_vec.
    records = []
    for idx in range(1, 21):
        is_liked = idx <= 10
        vec = pos_vec if is_liked else neg_vec
        read_item = {
            "id": idx,
            "title": "1984" if idx == 1 else f"Backlog Read {idx}",
            "author": "George Orwell" if idx == 1 else f"Backlog Author {idx}",
            "rating": 5.0 if is_liked else 1.0,
            "read_at": f"2021-01-{idx:02d}",
            "created_at": "2025-06-10T00:00:00Z",
        }
        records.append((ranking_eval.read_time(read_item["read_at"]), read_item, vec))

    # Candidates:
    # 101: Cold-start save at 2025-06-01 (aligns with neg_vec!), then repeat Librarr save at 2025-06-20.
    #      Without streak timestamp retention, 2025-06-20 would see the 2025-06-10 backlog and score low!
    # 102: Cold-start reject at 2025-06-01.
    # 103: Post-import save at 2025-06-15 (aligns with pos_vec -> high score) in run-warm (visible).
    # 104: Post-import reject at 2025-06-15 (aligns with neg_vec -> low score) in run-warm (visible).
    # 105: Post-import save at 2025-06-16 on an edition alias of read #1 ("Nineteen Eighty-Four: A Novel")
    #      so ReadIdentityIndex must exclude read #1 from its history.
    candidates = [
        {
            "id": 101,
            "title": "Cold Start Saved Book",
            "author": "New Writer A",
            "description": "Full catalog synopsis " * 20,
            "genres": '["Fiction", "Drama", "History", "Mystery"]',
            "source_weight": 1.0,
            "catalog_confidence": 0.9,
        },
        {
            "id": 102,
            "title": "Cold Start Rejected Book",
            "author": "New Writer B",
            "description": "Full catalog synopsis " * 20,
            "genres": '["Fiction", "Drama", "History", "Mystery"]',
            "source_weight": 1.0,
            "catalog_confidence": 0.9,
        },
        {
            "id": 103,
            "title": "Warm Saved Book",
            "author": "New Writer C",
            "description": "Full catalog synopsis " * 20,
            "genres": '["Fiction", "Drama", "History", "Mystery"]',
            "source_weight": 1.0,
            "catalog_confidence": 0.9,
        },
        {
            "id": 104,
            "title": "Warm Rejected Book",
            "author": "New Writer D",
            "description": "Full catalog synopsis " * 20,
            "genres": '["Fiction", "Drama", "History", "Mystery"]',
            "source_weight": 1.0,
            "catalog_confidence": 0.9,
        },
        {
            "id": 105,
            "title": "Nineteen Eighty-Four: A Novel",
            "author": "George Orwell",
            "description": "Full catalog synopsis " * 20,
            "genres": '["Fiction", "Drama", "History", "Mystery"]',
            "source_weight": 1.0,
            "catalog_confidence": 0.9,
        },
    ]
    cand_vectors = {
        101: neg_vec,
        102: pos_vec,
        103: pos_vec,
        104: neg_vec,
        105: pos_vec,
    }
    embeddings = [
        _embedding_row("candidate", c, cand_vectors[c["id"]])
        for c in candidates
    ]
    feedback = [
        # Initial save before CSV import, then Librarr download repeat save after CSV import:
        {"id": 1, "candidate_id": 101, "action": "save", "created_at": "2025-06-01T10:00:00Z", "run_id": "run-cold"},
        {"id": 2, "candidate_id": 101, "action": "save", "created_at": "2025-06-20T18:00:00Z"},
        # Cold-start reject:
        {"id": 3, "candidate_id": 102, "action": "reject", "created_at": "2025-06-01T10:01:00Z", "run_id": "run-cold"},
        # Action switch before warm period (reject -> save starts a fresh save streak at 2025-06-15):
        {"id": 4, "candidate_id": 103, "action": "reject", "created_at": "2025-06-01T10:02:00Z"},
        {"id": 5, "candidate_id": 103, "action": "save", "created_at": "2025-06-15T12:00:00Z", "run_id": "run-warm", "visible_at": "2025-06-15T11:59:00Z"},
        {"id": 6, "candidate_id": 104, "action": "reject", "created_at": "2025-06-15T12:01:00Z", "run_id": "run-warm", "visible_at": "2025-06-15T11:59:00Z"},
        {"id": 7, "candidate_id": 105, "action": "save", "created_at": "2025-06-16T12:00:00Z"},
    ]
    data = {"candidates": candidates, "embeddings": embeddings, "feedback": feedback}

    # Check edition exclusion directly on candidate 105:
    prior_105, counts_105 = ranking_eval.feedback_history_available_at(
        records, ranking_eval.read_time("2025-06-16T12:00:00Z"), candidates[4]
    )
    assert counts_105["same_identity"] == 1
    assert 1 not in {r["id"] for r, _ in prior_105}

    diag = ranking_eval.feedback_check(data, records, ("test", "v1"), strict_provenance=True)
    avail_diag = ranking_eval.feedback_check_engine_available_history(
        data, records, ("test", "v1"), strict_provenance=True
    )
    exp_diag = ranking_experiments.feedback_replay(
        data, records, ("test", "v1"), "current", strict_provenance=True
    )

    # Candidate 101 kept its 2025-06-01 timestamp across the Librarr repeat save,
    # so cold_start_zero_history has 2 items (101 and 102) and history_backed has 3 (103, 104, 105):
    assert diag["pooled_across_regimes"] is False
    assert diag["streak_preparation"]["repeat_events_collapsed"] == 1
    assert diag["cohorts"]["cold_start_zero_history"]["usable_candidates"] == 2
    assert diag["cohorts"]["history_backed"]["usable_candidates"] == 3
    assert diag["cohorts"]["history_backed"]["neighborhood"]["auc"] == 1.0
    assert diag["cohorts"]["same_run_slates"]["visible_same_run"]["usable"] == 2
    assert (
        diag["cohorts"]["same_run_slates"]["visible_same_run"]["metrics"]["neighborhood"][
            "within_run_macro_auc"
        ]
        == 1.0
    )

    assert avail_diag["cohorts"]["cold_start_zero_history"]["usable_candidates"] == 2
    assert avail_diag["cohorts"]["history_backed"]["usable_candidates"] == 3
    assert avail_diag["cohorts"]["history_backed"]["neighborhood"]["auc"] == 1.0

    assert exp_diag["pooled_across_regimes"] is False
    assert exp_diag["streak_preparation"]["repeat_events_collapsed"] == 1
    assert exp_diag["cohorts"]["cold_start_zero_history"]["usable"] == 2
    assert exp_diag["cohorts"]["history_backed"]["usable"] == 3
    assert exp_diag["cohorts"]["history_backed"]["metrics"]["current"]["save_auc"] == 1.0
    assert (
        exp_diag["cohorts"]["same_run_slates"]["visible_same_run"]["metrics"]["current"][
            "within_run_macro_auc"
        ]
        == 1.0
    )

    # Missing catalog provenance warns by default and fails closed when strict_provenance=True:
    stripped_candidates = [
        {k: v for k, v in c.items() if k not in {"catalog_confidence", "source_weight"}}
        for c in candidates
    ]
    stripped_data = {**data, "candidates": stripped_candidates}
    with pytest.warns(UserWarning, match="lack catalog_confidence"):
        warned = ranking_eval.feedback_check(stripped_data, records, ("test", "v1"))
    assert warned["provenance"]["status"] == "missing_catalog_provenance_warning"

    with pytest.raises(ValueError, match="Missing required candidate catalog provenance"):
        ranking_eval.feedback_check(
            stripped_data, records, ("test", "v1"), strict_provenance=True
        )
    with pytest.raises(ValueError, match="Missing required candidate catalog provenance"):
        ranking_experiments.feedback_replay(
            stripped_data, records, ("test", "v1"), "current", strict_provenance=True
        )


def test_all_benchmark_tail_helpers_are_order_invariant_under_boundary_ties():
    """Requirement 8 & AC 5: All offline tail metrics agree and ignore input row order."""
    ratings = np.array([1, 2, 5, 1, 4] + [3] * 20 + [5, 5, 4, 1, 2], dtype=float)
    # 25 tied floor scores at 10.0 and 5 tied ceiling scores at 90.0 (total n=30):
    scores = np.array([10.0] * 25 + [90.0] * 5, dtype=float)
    rng = np.random.default_rng(20261121)
    perm = rng.permutation(len(ratings))

    for candidate_ratings, candidate_scores in (
        (ratings[::-1], scores[::-1]),
        (ratings[perm], scores[perm]),
    ):
        assert historical.raw_ranking_metrics(ratings, scores)["low_rated_in_bottom_20"] == pytest.approx(
            historical.raw_ranking_metrics(candidate_ratings, candidate_scores)["low_rated_in_bottom_20"]
        )
        assert historical.raw_ranking_metrics(ratings, scores)["high_rated_in_top_20"] == pytest.approx(
            historical.raw_ranking_metrics(candidate_ratings, candidate_scores)["high_rated_in_top_20"]
        )
        assert historical.raw_ranking_metrics(ratings, scores)["graded_ndcg_at_20"] == pytest.approx(
            historical.raw_ranking_metrics(candidate_ratings, candidate_scores)["graded_ndcg_at_20"]
        )
        assert next_five_combo.metrics(ratings, scores) == pytest.approx(
            next_five_combo.metrics(candidate_ratings, candidate_scores)
        )
        assert ordinal.metric_set(ratings, scores) == pytest.approx(
            ordinal.metric_set(candidate_ratings, candidate_scores)
        )
        assert metric_facets._metrics(ratings, scores) == pytest.approx(
            metric_facets._metrics(candidate_ratings, candidate_scores)
        )
        assert embedding_study.metric_set(ratings, scores) == pytest.approx(
            embedding_study.metric_set(candidate_ratings, candidate_scores)
        )
        assert ranking_eval.metrics(ratings >= 4, scores) == pytest.approx(
            ranking_eval.metrics(candidate_ratings >= 4, candidate_scores)
        )


def test_expanding_history_is_default_and_frozen_prefix_is_labeled_explicitly():
    """Requirement 9: Standalone and historical evaluators default to expanding history."""
    records = []
    origin = datetime(2020, 1, 1, tzinfo=timezone.utc)
    for idx in range(200):
        liked = idx % 2 == 0
        vec = _vec(1.0, 0.1) if liked else _vec(0.1, 1.0)
        item = {
            "id": idx + 1,
            "title": f"Chronological Book {idx + 1}",
            "author": f"Chronological Author {idx % 11}",
            "rating": 5.0 if liked else 1.0,
            "read_at": (origin + timedelta(days=idx)).date().isoformat(),
        }
        records.append((origin + timedelta(days=idx), item, vec))

    default_protocol, default_splits = ranking_eval.evaluate_splits(records)
    frozen_protocol, frozen_splits = ranking_eval.evaluate_splits(
        records, history_mode="frozen-prefix"
    )
    assert "strictly earlier-day expanding history" in default_protocol
    assert default_splits["validation"]["history_mode"] == "expanding"
    assert default_splits["validation"]["max_history"] > default_splits["validation"]["training"]
    assert "optional frozen-prefix" in frozen_protocol
    assert frozen_splits["validation"]["history_mode"] == "frozen-prefix"
    assert frozen_splits["validation"]["max_history"] == frozen_splits["validation"]["training"]

    hist_default = historical.evaluate(records)
    hist_frozen = historical.evaluate(records, history_mode="frozen-prefix")
    assert hist_default["history_mode"] == "expanding"
    assert "per-book strictly earlier-day history" in hist_default["protocol"]
    assert hist_frozen["history_mode"] == "frozen-prefix"
    assert "optional frozen-prefix split history" in hist_frozen["protocol"]
