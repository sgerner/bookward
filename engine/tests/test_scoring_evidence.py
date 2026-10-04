import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from afterword_engine.config import settings
from afterword_engine.database import connect, initialize, transaction
from afterword_engine.decision_evidence import (
    build_decision_evidence,
    load_decision_evidence,
    purge_recommendation_evidence,
)
from afterword_engine.embeddings import content_hash, vector_blob
from afterword_engine.ingestion import _read_metadata_identity_hash
from afterword_engine.identity import book_identity_match_index, book_row_identity_match_keys
from afterword_engine.ranking import rank_candidates
from afterword_engine.scoring import SCORING_BATCH_SIZE, document, score_all
from afterword_engine.scoring_evidence import (
    build_scoring_batch_evidence,
    load_scoring_batch,
)


@pytest.fixture()
def database(tmp_path: Path):
    settings.db = str(tmp_path / "scoring-evidence.db")
    initialize()
    return settings.db


class _CachedEmbedder:
    name = "local"
    model = "test-model"

    async def embed(self, _texts):
        raise AssertionError("the fixture should use the stored exact vectors")


def _candidate(con, title, author):
    source_id = con.execute(
        "SELECT id FROM sources WHERE url='builtin://upcoming'"
    ).fetchone()[0]
    candidate_id = con.execute(
        "INSERT INTO candidates(title,author,description,genres,status,normalized_key,source_id) "
        "VALUES(?,?,?,?,'new',?,?)",
        (
            title,
            author,
            f"A grounded description for {title}.",
            json.dumps(["Science fiction"]),
            f"{title} {author}",
            source_id,
        ),
    ).lastrowid
    con.execute(
        "UPDATE candidate_quality SET quality_status='accepted',quality_score=0.85 "
        "WHERE candidate_id=?",
        (candidate_id,),
    )
    return int(candidate_id)


def _store_vector(con, entity_type, entity_id, item, vector):
    array = np.asarray(vector, dtype=np.float32)
    con.execute(
        "INSERT INTO embeddings(entity_type,entity_id,backend,model,vector,dimensions,content_hash) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            entity_type,
            entity_id,
            "local",
            "test-model",
            vector_blob(array),
            int(array.size),
            content_hash(document(item)),
        ),
    )


def _create_production_shape(database):
    with transaction() as con:
        con.execute("DELETE FROM recommendation_runs")
        con.execute("DELETE FROM candidates")
        con.execute("DELETE FROM reads")
        con.execute(
            "INSERT INTO settings(key,value,secret) VALUES('embedding_backend','local',0) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
        con.execute(
            "INSERT INTO settings(key,value,secret) VALUES('embedding_model','test-model',0) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
        rated_id = con.execute(
            "INSERT INTO reads(title,author,rating,read_at,source) "
            "VALUES('A Favorite','R. Reader',5,'2024-03-10','fixture')"
        ).lastrowid
        unrated_id = con.execute(
            "INSERT INTO reads(title,author,rating,read_at,source) "
            "VALUES('Already Read','U. Reader',NULL,'2022-05-01','fixture')"
        ).lastrowid
        excluded_id = _candidate(con, "Already Read", "U. Reader")
        eligible_id = _candidate(con, "A New Book", "N. Writer")
        rated = dict(con.execute("SELECT * FROM reads WHERE id=?", (rated_id,)).fetchone())
        eligible = dict(
            con.execute(
                "SELECT c.*,s.name source_name,s.weight source_weight,"
                "q.work_id quality_work_id,q.provider quality_provider,"
                "q.isbn13 quality_isbn13,q.isbn10 quality_isbn10,"
                "CASE WHEN q.quality_score>0 THEN q.quality_score "
                "WHEN q.quality_status='accepted' THEN 0.85 ELSE 0 END AS catalog_confidence "
                "FROM candidates c JOIN sources s ON s.id=c.source_id "
                "JOIN candidate_quality q ON q.candidate_id=c.id WHERE c.id=?",
                (eligible_id,),
            ).fetchone()
        )
        _store_vector(con, "read", rated_id, rated, [0.8, 0.2, 0.1])
        _store_vector(con, "candidate", eligible_id, eligible, [0.7, 0.4, 0.2])
    return int(rated_id), int(unrated_id), int(excluded_id), int(eligible_id)


def _served_candidate(con, candidate_id):
    return dict(
        con.execute(
            "SELECT c.*,s.name source_name,s.weight source_weight,"
            "q.work_id quality_work_id,q.provider quality_provider,"
            "q.isbn13 quality_isbn13,q.isbn10 quality_isbn10,"
            "CASE WHEN q.quality_score>0 THEN q.quality_score "
            "WHEN q.quality_status='accepted' THEN 0.85 ELSE 0 END AS catalog_confidence "
            "FROM candidates c JOIN sources s ON s.id=c.source_id "
            "JOIN candidate_quality q ON q.candidate_id=c.id WHERE c.id=?",
            (candidate_id,),
        ).fetchone()
    )


def test_scoring_job_archives_and_replays_actual_scored_inputs(database):
    rated_id, unrated_id, excluded_id, eligible_id = _create_production_shape(database)
    assert asyncio.run(score_all(embedder=_CachedEmbedder())) == 1

    with connect() as con:
        row = con.execute(
            "SELECT score,explanation,score_batch_id FROM candidates WHERE id=?",
            (eligible_id,),
        ).fetchone()
        assert row["score_batch_id"]
        batch_id = row["score_batch_id"]
        assert con.execute(
            "SELECT 1 FROM candidates WHERE id=? AND score_batch_id IS NULL", (excluded_id,)
        ).fetchone() is not None
        archived = load_scoring_batch(con, batch_id)

        assert archived["capture_status"] == "complete"
        decision = archived["decision"]
        assert decision["input_selection"]["rated_read_ids"] == [rated_id]
        assert [item["id"] for item in decision["read_history"]] == [rated_id, unrated_id]
        assert [item["id"] for item in decision["rated_read_inputs"]] == [rated_id]
        assert [item["id"] for item in decision["candidate_inputs"]] == [eligible_id]
        assert len(decision["read_identity_projection"]) == 2
        assert len(decision["vectors"]) == 2
        assert decision["policy"]["scoring_batch_size"] == SCORING_BATCH_SIZE
        assert "scoring_evidence.py" in decision["policy"]["source_hashes"]
        assert archived["candidate_links"][0]["score"] == row["score"]
        safe_decision = {
            **decision,
            "vectors": [
                {key: value for key, value in item.items() if key != "vector_bytes"}
                for item in decision["vectors"]
            ],
        }
        serialized_decision = json.dumps(safe_decision)
        assert "BOOKWARD_BUILD_ID" not in serialized_decision
        assert "api_key" not in serialized_decision.lower()
        assert "session_id" not in serialized_decision.lower()

        vector_map = {
            (item["entity_type"], int(item["entity_id"])): np.frombuffer(
                item["vector_bytes"], dtype="<f4"
            )
            for item in decision["vectors"]
        }
        replayed = rank_candidates(
            decision["rated_read_inputs"],
            [vector_map[("read", rated_id)]],
            decision["candidate_inputs"],
            [vector_map[("candidate", eligible_id)]],
        )
        expected = decision["candidate_outputs"][0]
        assert replayed[0]["score"] == expected["score"] == row["score"]
        assert replayed[0]["metadata_confidence"] == expected["metadata_confidence"]
        assert replayed[0]["explanation"] == expected["explanation"] == json.loads(row["explanation"])

        served = _served_candidate(con, eligible_id)
        provenance = build_decision_evidence(
            con,
            base_pool=[served],
            personalized_pool=[served],
            final_pool=[served],
            interaction_events=[],
            cached_vectors={eligible_id: vector_map[("candidate", eligible_id)]},
            ranking_metadata={},
            discovery_slate_metadata={},
            request_context={"ranking_at": datetime.now(timezone.utc).isoformat()},
        )
        base = provenance["base_score_provenance"]
        assert base["derivation_status"] == "complete"
        assert base["candidates"][0]["status"] == "replayable"
        assert base["candidates"][0]["input_freshness"] == "matches_batch_input"
        payload_length = con.execute(
            "SELECT length(payload) FROM scoring_batches WHERE id=?", (batch_id,)
        ).fetchone()[0]
        header = con.execute(
            "SELECT reason,captured_at FROM scoring_batches WHERE id=?", (batch_id,)
        ).fetchone()
        budget_expected = (
            192 + len(header["reason"].encode()) + len(header["captured_at"].encode()) + payload_length
            + con.execute("SELECT COALESCE(SUM(length(vector)),0) FROM recommendation_evidence_vectors").fetchone()[0]
            + 96 * con.execute("SELECT COUNT(*) FROM scoring_batch_vectors").fetchone()[0]
            + con.execute(
                "SELECT COALESCE(SUM(96+length(identity_hash)+length(input_content_hash)),0) "
                "FROM scoring_batch_candidates"
            ).fetchone()[0]
            + 64 * con.execute("SELECT COUNT(*) FROM scoring_batch_reads").fetchone()[0]
        )
        assert con.execute(
            "SELECT used_bytes FROM recommendation_evidence_budget WHERE id=1"
        ).fetchone()[0] == budget_expected

        # Description edits leave the archived job replayable but explicitly
        # mark the serving-time candidate document as stale.
        con.execute("UPDATE candidates SET description='Updated after scoring' WHERE id=?", (eligible_id,))
        changed = _served_candidate(con, eligible_id)
        stale = build_decision_evidence(
            con,
            base_pool=[changed],
            personalized_pool=[changed],
            final_pool=[changed],
            interaction_events=[],
            cached_vectors={eligible_id: vector_map[("candidate", eligible_id)]},
            ranking_metadata={},
            discovery_slate_metadata={},
            request_context={},
        )["base_score_provenance"]
        assert stale["derivation_status"] == "complete"
        assert stale["stale_input_candidate_count"] == 1
        assert stale["candidates"][0]["status"] == "replayable"
        assert stale["candidates"][0]["input_freshness"] == "stale_batch_input"


def test_score_all_filters_only_current_verified_metadata_work_aliases_and_archives_them(database):
    rated_id, unrated_id, raw_excluded_id, eligible_id = _create_production_shape(database)
    work_ids = {
        "fresh": "/works/OL111W",
        "stale": "/works/OL222W",
        "provider_mismatch": "/works/OL333W",
    }
    read_ids = {}
    with transaction() as con:
        read_ids["fresh"] = con.execute(
            "INSERT INTO reads(title,author,rating,isbn,source) VALUES(?,?,NULL,?,'fixture')",
            ("Northern Lights", "Philip Pullman", "9781407130221"),
        ).lastrowid
        read_ids["stale"] = con.execute(
            "INSERT INTO reads(title,author,rating,isbn,source) VALUES(?,?,NULL,?,'fixture')",
            ("Stale Source Title", "S. Writer", "9780307474278"),
        ).lastrowid
        read_ids["provider_mismatch"] = con.execute(
            "INSERT INTO reads(title,author,rating,isbn,source) VALUES(?,?,NULL,?,'fixture')",
            ("Provider Source Title", "P. Writer", "9780439023528"),
        ).lastrowid
        for key, read_id in read_ids.items():
            read = dict(con.execute("SELECT * FROM reads WHERE id=?", (read_id,)).fetchone())
            provider = "google_books" if key == "provider_mismatch" else "openlibrary"
            provider_id = "volume:wrong-provider" if key == "provider_mismatch" else work_ids[key]
            identity_hash = (
                "stale-identity-hash"
                if key == "stale"
                else _read_metadata_identity_hash(read)
            )
            con.execute(
                "INSERT INTO read_metadata(read_id,verified_work_id,identity_provider,"
                "identity_provider_id,identity_hash) VALUES(?,?,?,?,?)",
                (read_id, work_ids[key], provider, provider_id, identity_hash),
            )

    candidate_ids = {}
    candidate_names = {
        "fresh": ("The Golden Compass", "Philip Pullman"),
        "stale": ("Stale Catalog Alias", "S. Writer"),
        "provider_mismatch": ("Provider Catalog Alias", "P. Writer"),
    }
    with transaction() as con:
        for position, (key, (title, author)) in enumerate(candidate_names.items(), start=1):
            candidate_id = _candidate(con, title, author)
            con.execute(
                "UPDATE candidate_quality SET provider='openlibrary',work_id=? "
                "WHERE candidate_id=?",
                (work_ids[key], candidate_id),
            )
            candidate = dict(
                con.execute(
                    "SELECT c.*,s.name source_name,s.weight source_weight,"
                    "q.work_id quality_work_id,q.provider quality_provider,"
                    "q.isbn13 quality_isbn13,q.isbn10 quality_isbn10,"
                    "CASE WHEN q.quality_score>0 THEN q.quality_score "
                    "WHEN q.quality_status='accepted' THEN 0.85 ELSE 0 END AS catalog_confidence "
                    "FROM candidates c JOIN sources s ON s.id=c.source_id "
                    "JOIN candidate_quality q ON q.candidate_id=c.id WHERE c.id=?",
                    (candidate_id,),
                ).fetchone()
            )
            _store_vector(
                con, "candidate", candidate_id, candidate,
                [0.4 + position * 0.1, 0.3, 0.2],
            )
            candidate_ids[key] = int(candidate_id)

    # The verified alias read is intentionally unrated: it affects the
    # all-read exclusion index but must not enter the ranking vectors.
    assert asyncio.run(score_all(embedder=_CachedEmbedder())) == 3

    with connect() as con:
        assert con.execute(
            "SELECT score_batch_id FROM candidates WHERE id=?", (candidate_ids["fresh"],)
        ).fetchone()[0] is None
        assert con.execute(
            "SELECT score_batch_id FROM candidates WHERE id=?", (raw_excluded_id,)
        ).fetchone()[0] is None
        for key in ("stale", "provider_mismatch"):
            assert con.execute(
                "SELECT score_batch_id FROM candidates WHERE id=?", (candidate_ids[key],)
            ).fetchone()[0]

        batch_id = con.execute(
            "SELECT score_batch_id FROM candidates WHERE id=?", (candidate_ids["stale"],)
        ).fetchone()[0]
        decision = load_scoring_batch(con, batch_id)["decision"]
        assert decision["input_selection"]["rated_read_ids"] == [rated_id]
        assert [item["id"] for item in decision["rated_read_inputs"]] == [rated_id]
        assert next(
            item for item in decision["read_history"] if item["id"] == read_ids["fresh"]
        )["rating"] is None
        identity_by_id = {
            item["id"]: item for item in decision["read_identity_projection"]
        }
        fresh_identity = identity_by_id[read_ids["fresh"]]
        assert fresh_identity["read_metadata_work_id"] == work_ids["fresh"]
        assert fresh_identity["read_metadata_identity_provider"] == "openlibrary"
        assert fresh_identity["quality_work_id"] == work_ids["fresh"]
        assert fresh_identity["quality_provider"] == "openlibrary"
        assert fresh_identity["read_metadata_identity_hash"] == _read_metadata_identity_hash(
            dict(con.execute("SELECT * FROM reads WHERE id=?", (read_ids["fresh"],)).fetchone())
        )
        assert book_row_identity_match_keys(
            {
                "title": "The Golden Compass",
                "author": "Philip Pullman",
                "quality_work_id": work_ids["fresh"],
                "quality_provider": "openlibrary",
            }
        ) & book_identity_match_index(decision["read_identity_projection"])
        assert "read_metadata_work_id" not in identity_by_id[read_ids["stale"]]
        assert "read_metadata_work_id" not in identity_by_id[read_ids["provider_mismatch"]]
        assert "read_metadata_work_id" not in next(
            item for item in decision["read_history"] if item["id"] == read_ids["fresh"]
        )
        assert "ingestion.py" in decision["policy"]["source_hashes"]
        assert len(decision["vectors"]) == 4


def test_exact_rated_read_rows_are_separate_from_same_id_history_projection(database):
    rated = {
        "id": 45,
        "title": "Same Read",
        "author": "R. Reader",
        "rating": 5.0,
        "read_at": "2024-04-01",
        "genres": "[]",
        "description": "",
    }
    history_version = {**rated, "rating": 1.0, "read_at": "2023-02-01"}
    candidate = {
        "id": 81,
        "title": "New Story",
        "author": "A. Writer",
        "description": "A new story.",
        "genres": "[]",
        "source_weight": 1.0,
        "catalog_confidence": 0.8,
    }
    read_vector = np.asarray([1.0, 0.0], dtype=np.float32)
    candidate_vector = np.asarray([0.4, 0.9], dtype=np.float32)
    ranked = rank_candidates([rated], [read_vector], [candidate], [candidate_vector])
    with connect() as con:
        evidence = build_scoring_batch_evidence(
            con,
            captured_at="2026-10-02T00:00:00+00:00",
            read_history=[history_version],
            rated_reads=[rated],
            candidates=[candidate],
            ranked=ranked,
            read_vectors=[read_vector],
            candidate_vectors={81: candidate_vector},
            backend="local",
            model="test-model",
            scoring_batch_size=SCORING_BATCH_SIZE,
        )
    assert evidence["capture_status"] == "complete"
    assert evidence["read_history"][0]["rating"] == 1.0
    assert evidence["read_identity_projection"][0]["id"] == 45
    assert evidence["rated_read_inputs"][0]["rating"] == 5.0
    assert evidence["rated_read_inputs"][0]["read_at"] == "2024-04-01"


def test_incomplete_scoring_capture_keeps_score_and_marks_provenance_incomplete(database, monkeypatch):
    _rated_id, _unrated_id, _excluded_id, eligible_id = _create_production_shape(database)
    from afterword_engine import scoring_evidence

    monkeypatch.setattr(scoring_evidence, "MAX_SCORING_READS", 1)
    assert asyncio.run(score_all(embedder=_CachedEmbedder())) == 1
    with connect() as con:
        row = con.execute(
            "SELECT score,score_batch_id FROM candidates WHERE id=?", (eligible_id,)
        ).fetchone()
        assert row["score"] > 0
        assert row["score_batch_id"]
        record = load_scoring_batch(con, row["score_batch_id"])
        assert record["capture_status"] == "incomplete"
        assert record["reason"] == "read_limit_exceeded"
        served = _served_candidate(con, eligible_id)
        evidence = build_decision_evidence(
            con,
            base_pool=[served],
            personalized_pool=[served],
            final_pool=[served],
            interaction_events=[],
            cached_vectors={},
            ranking_metadata={},
            discovery_slate_metadata={},
            request_context={},
        )
        assert evidence["capture_status"] == "complete", evidence
        assert evidence["base_score_provenance"]["derivation_status"] == "incomplete"
        assert evidence["base_score_provenance"]["candidates"][0]["status"] == "batch_incomplete"


def test_read_history_purge_removes_linked_batches_and_vector_artifacts(database):
    rated_id, _unrated_id, _excluded_id, eligible_id = _create_production_shape(database)
    assert asyncio.run(score_all(embedder=_CachedEmbedder())) == 1
    with connect() as con:
        served = _served_candidate(con, eligible_id)
    from afterword_engine.learning import create_recommendation_run, record_event_in_connection

    run_id = create_recommendation_run([served])
    with transaction() as con:
        record_event_in_connection(
            con,
            event_key=f"read:{rated_id:08d}",
            candidate_id=eligible_id,
            event_type="read",
            run_id=run_id,
            value=5.0,
            source="read_import",
            label=1.0,
            label_kind="rating_positive",
            confidence=1.0,
            read_id=rated_id,
        )
    with transaction() as con:
        batch_id = con.execute(
            "SELECT score_batch_id FROM candidates WHERE id=?", (eligible_id,)
        ).fetchone()[0]
        assert con.execute(
            "SELECT COUNT(*) FROM scoring_batch_reads WHERE batch_id=? AND read_id=?",
            (batch_id, rated_id),
        ).fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM recommendation_evidence_vectors").fetchone()[0] == 2
        removed = purge_recommendation_evidence(con, read_ids=[rated_id])
        assert removed["scoring_batches_removed"] == 1
        assert removed["runs_removed"] == 1
        assert con.execute("SELECT 1 FROM scoring_batches WHERE id=?", (batch_id,)).fetchone() is None
        assert con.execute("SELECT 1 FROM recommendation_runs WHERE id=?", (run_id,)).fetchone() is None
        assert con.execute(
            "SELECT COUNT(*) FROM recommendation_evidence_events WHERE event_key=?",
            (f"read:{rated_id:08d}",),
        ).fetchone()[0] == 0
        assert con.execute(
            "SELECT score_batch_id FROM candidates WHERE id=?", (eligible_id,)
        ).fetchone()[0] is None
        assert con.execute("SELECT COUNT(*) FROM recommendation_evidence_vectors").fetchone()[0] == 0
        assert con.execute(
            "SELECT used_bytes FROM recommendation_evidence_budget WHERE id=1"
        ).fetchone()[0] == 0


def test_run_purge_keeps_vectors_still_used_by_scoring_batch(database):
    _rated_id, _unrated_id, _excluded_id, eligible_id = _create_production_shape(database)
    assert asyncio.run(score_all(embedder=_CachedEmbedder())) == 1
    shared_vector = np.asarray([0.7, 0.4, 0.2], dtype=np.float32)
    with transaction() as con:
        other_id = _candidate(con, "Independent Served Book", "I. Author")
        other = _served_candidate(con, other_id)
        _store_vector(con, "candidate", other_id, other, shared_vector)
        evidence = build_decision_evidence(
            con,
            base_pool=[other],
            personalized_pool=[other],
            final_pool=[other],
            interaction_events=[],
            cached_vectors={other_id: shared_vector},
            ranking_metadata={},
            discovery_slate_metadata={},
            request_context={},
        )
        assert evidence["capture_status"] == "complete", evidence
    from afterword_engine.learning import create_recommendation_run

    run_id = create_recommendation_run([other], decision_evidence=evidence)
    with transaction() as con:
        run_evidence = con.execute(
            "SELECT capture_status,reason FROM recommendation_run_evidence WHERE run_id=?",
            (run_id,),
        ).fetchone()
        assert run_evidence["capture_status"] == "complete", dict(run_evidence)
        shared_hash = con.execute(
            "SELECT artifact_hash FROM recommendation_evidence_vectors "
            "WHERE backend='local' AND model='test-model' AND vector=?",
            (vector_blob(shared_vector),),
        ).fetchone()[0]
        assert con.execute(
            "SELECT 1 FROM recommendation_run_evidence_vectors "
            "WHERE run_id=? AND artifact_hash=?",
            (run_id, shared_hash),
        ).fetchone() is not None
        assert con.execute(
            "SELECT 1 FROM scoring_batch_vectors WHERE artifact_hash=?", (shared_hash,)
        ).fetchone() is not None
        removed = purge_recommendation_evidence(con, run_ids=[run_id])
        assert removed["runs_removed"] == 1
        assert con.execute(
            "SELECT 1 FROM recommendation_evidence_vectors WHERE artifact_hash=?", (shared_hash,)
        ).fetchone() is not None
        assert con.execute(
            "SELECT COUNT(*) FROM scoring_batches WHERE capture_status='complete'"
        ).fetchone()[0] == 1


def test_read_purge_removes_serving_run_that_references_score_batch(database):
    rated_id, _unrated_id, _excluded_id, eligible_id = _create_production_shape(database)
    assert asyncio.run(score_all(embedder=_CachedEmbedder())) == 1
    with connect() as con:
        served = _served_candidate(con, eligible_id)
        vector = np.frombuffer(
            con.execute(
                "SELECT vector FROM embeddings WHERE entity_type='candidate' AND entity_id=? "
                "AND backend='local' AND model='test-model'",
                (eligible_id,),
            ).fetchone()[0],
            dtype="<f4",
        )
        evidence = build_decision_evidence(
            con,
            base_pool=[served],
            personalized_pool=[served],
            final_pool=[served],
            interaction_events=[],
            cached_vectors={eligible_id: vector},
            ranking_metadata={},
            discovery_slate_metadata={},
            request_context={},
        )
    from afterword_engine.learning import create_recommendation_run

    run_id = create_recommendation_run([served], decision_evidence=evidence)
    with transaction() as con:
        batch_id = con.execute(
            "SELECT score_batch_id FROM candidates WHERE id=?", (eligible_id,)
        ).fetchone()[0]
        assert con.execute(
            "SELECT 1 FROM recommendation_run_evidence_scoring_batches "
            "WHERE run_id=? AND score_batch_id=?",
            (run_id, batch_id),
        ).fetchone() is not None
        removed = purge_recommendation_evidence(con, read_ids=[rated_id])
        assert removed["scoring_batches_removed"] == 1
        assert removed["runs_removed"] == 1
        assert con.execute("SELECT 1 FROM scoring_batches WHERE id=?", (batch_id,)).fetchone() is None
        assert con.execute(
            "SELECT 1 FROM recommendation_run_evidence WHERE run_id=?", (run_id,)
        ).fetchone() is None
        assert con.execute(
            "SELECT 1 FROM recommendation_run_evidence_scoring_batches WHERE run_id=?",
            (run_id,),
        ).fetchone() is None
        assert load_decision_evidence(con, run_id) is None


def test_clear_all_evidence_removes_scoring_batches_and_run_history(database):
    rated_id, _unrated_id, _excluded_id, eligible_id = _create_production_shape(database)
    assert asyncio.run(score_all(embedder=_CachedEmbedder())) == 1
    from afterword_engine.learning import create_recommendation_run

    with connect() as con:
        served = _served_candidate(con, eligible_id)
        evidence = build_decision_evidence(
            con,
            base_pool=[served],
            personalized_pool=[served],
            final_pool=[served],
            interaction_events=[],
            cached_vectors={},
            ranking_metadata={},
            discovery_slate_metadata={},
            request_context={},
        )
    run_id = create_recommendation_run([served], decision_evidence=evidence)
    with transaction() as con:
        result = purge_recommendation_evidence(con, clear_all_evidence=True)
        assert result["runs_removed"] == 1
        assert result["scoring_batches_removed"] == 1
        assert con.execute("SELECT COUNT(*) FROM recommendation_runs").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM scoring_batches").fetchone()[0] == 0
        assert con.execute("SELECT score_batch_id FROM candidates WHERE id=?", (eligible_id,)).fetchone()[0] is None
        assert con.execute("SELECT COUNT(*) FROM recommendation_evidence_vectors").fetchone()[0] == 0
        assert con.execute("SELECT used_bytes FROM recommendation_evidence_budget WHERE id=1").fetchone()[0] == 0
