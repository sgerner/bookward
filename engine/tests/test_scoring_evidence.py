import asyncio
import json
import zlib
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
from afterword_engine.identity import (
    book_identity_match_index,
    book_row_identity_match_keys,
    read_metadata_identity_hash,
)
from afterword_engine.ranking import rank_candidates
from afterword_engine.scoring import SCORING_BATCH_SIZE, document, score_all
from afterword_engine.scoring_evidence import (
    SCORING_BATCH_SCHEMA_VERSION,
    build_scoring_batch_evidence,
    load_scoring_batch,
    replay_scoring_batch,
    verify_scoring_batch_replay,
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
                else read_metadata_identity_hash(read)
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
        assert fresh_identity["read_metadata_identity_hash"] == read_metadata_identity_hash(
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


def test_scoring_batch_archives_goodreads_shelves_and_replays_zero_drift(database):
    rated_id, _unrated_id, _excluded_id, eligible_id = _create_production_shape(database)
    with transaction() as con:
        boosted_id = _candidate(con, "Shelf Boosted Target", "S. Shelf")
        reading_id = _candidate(con, "Currently Reading Hidden", "C. Reader")
        con.execute(
            "UPDATE candidate_quality SET isbn13='9780593135204' WHERE candidate_id=?",
            (boosted_id,),
        )
        boosted = _served_candidate(con, boosted_id)
        reading = _served_candidate(con, reading_id)
        _store_vector(con, "candidate", boosted_id, boosted, [0.75, 0.35, 0.15])
        _store_vector(con, "candidate", reading_id, reading, [0.65, 0.45, 0.25])
        con.execute(
            "INSERT INTO goodreads_shelf_items(shelf,title,author,isbn,added_at) "
            "VALUES('to-read','Different Edition Title','S. Shelf','9780593135204','2026-09-01')"
        )
        con.execute(
            "INSERT INTO goodreads_shelf_items(shelf,title,author,added_at) "
            "VALUES('currently-reading','Currently Reading Hidden','C. Reader','2026-09-02')"
        )
        con.execute(
            "INSERT INTO goodreads_shelf_items(shelf,title,author,isbn,added_at) "
            "VALUES('to-read','Never Scored Secret Title','Private Author','9780000000099','2026-09-03')"
        )
        con.execute(
            "INSERT INTO goodreads_shelf_items(shelf,title,author,added_at) "
            "VALUES('currently-reading','Private Nightstand Book','Unscored Writer','2026-09-04')"
        )

    assert asyncio.run(score_all(embedder=_CachedEmbedder())) == 3

    with connect() as con:
        hidden_row = con.execute(
            "SELECT status,score_batch_id FROM candidates WHERE id=?", (reading_id,)
        ).fetchone()
        assert hidden_row["status"] == "hidden"
        assert hidden_row["score_batch_id"] is None

        boosted_row = con.execute(
            "SELECT status,score,explanation,score_batch_id FROM candidates WHERE id=?",
            (boosted_id,),
        ).fetchone()
        ordinary_row = con.execute(
            "SELECT status,score,explanation,score_batch_id FROM candidates WHERE id=?",
            (eligible_id,),
        ).fetchone()
        assert boosted_row["status"] == "recommended"
        assert boosted_row["score_batch_id"] == ordinary_row["score_batch_id"]
        batch_id = boosted_row["score_batch_id"]

        archived = load_scoring_batch(con, batch_id)
        assert archived["schema_version"] == SCORING_BATCH_SCHEMA_VERSION == 2
        assert archived["capture_status"] == "complete"
        decision = archived["decision"]
        assert "goodreads_shelf_items" not in decision
        assert "goodreads_shelf_count" not in decision["input_selection"]
        assert decision["input_selection"]["to_read_candidate_ids"] == [boosted_id]
        raw_batch_blob = con.execute(
            "SELECT payload FROM scoring_batches WHERE id=?", (batch_id,)
        ).fetchone()["payload"]
        raw_batch_json = zlib.decompress(raw_batch_blob).decode("utf-8")
        for forbidden_shelf_text in (
            "Never Scored Secret Title",
            "Private Author",
            "9780000000099",
            "Private Nightstand Book",
            "Unscored Writer",
            "Currently Reading Hidden",
            "Different Edition Title",
        ):
            assert forbidden_shelf_text not in raw_batch_json
        assert {
            "impression_fatigue.py",
            "llm_reranker.py",
            "read_dates.py",
        }.issubset(decision["policy"]["source_hashes"])
        assert len(decision["policy"]["source_hashes"]) == 20

        outputs_by_id = {
            int(item["candidate_id"]): item for item in decision["candidate_outputs"]
        }
        assert reading_id not in outputs_by_id
        boosted_out = outputs_by_id[boosted_id]
        ordinary_out = outputs_by_id[eligible_id]
        assert boosted_out["on_goodreads_to_read"] is True
        assert ordinary_out["on_goodreads_to_read"] is False
        assert boosted_out["score"] == boosted_row["score"]
        assert boosted_out["score"] == round(min(100.0, boosted_out["base_score"] + 10.0), 1)
        assert boosted_out["shelf_boost"] == round(
            boosted_out["score"] - boosted_out["base_score"], 1
        )
        assert boosted_out["shelf_boost"] > 0
        assert boosted_out["explanation"][0] == "On your Goodreads to-read shelf"
        assert boosted_out["explanation"] == json.loads(boosted_row["explanation"])
        assert ordinary_out["base_score"] == ordinary_out["score"] == ordinary_row["score"]
        assert ordinary_out["shelf_boost"] == 0.0

        replayed = replay_scoring_batch(archived)
        assert replayed["schema_version"] == 2
        assert replayed["verification"]["matches_archived"] is True
        assert replayed["verification"]["max_score_drift"] == 0.0
        assert replayed["verification"]["max_base_score_drift"] == 0.0
        assert verify_scoring_batch_replay(archived)["matches_archived"] is True

        # Serving provenance confirms the shelf-boosted materialized score matches its batch link.
        served_boosted = _served_candidate(con, boosted_id)
        vector_map = {
            (item["entity_type"], int(item["entity_id"])): np.frombuffer(
                item["vector_bytes"], dtype="<f4"
            )
            for item in decision["vectors"]
        }
        provenance = build_decision_evidence(
            con,
            base_pool=[served_boosted],
            personalized_pool=[served_boosted],
            final_pool=[served_boosted],
            interaction_events=[],
            cached_vectors={boosted_id: vector_map[("candidate", boosted_id)]},
            ranking_metadata={},
            discovery_slate_metadata={},
            request_context={"ranking_at": datetime.now(timezone.utc).isoformat()},
        )["base_score_provenance"]
        assert provenance["derivation_status"] == "complete"
        assert provenance["candidates"][0]["status"] == "replayable"

    with transaction() as con:
        removed = purge_recommendation_evidence(con, candidate_ids=[boosted_id])
        assert removed["scoring_batches_removed"] == 1
        assert (
            con.execute(
                "SELECT used_bytes FROM recommendation_evidence_budget WHERE id=1"
            ).fetchone()[0]
            == 0
        )


def test_legacy_v1_scoring_batch_loads_and_replays_and_rejects_raw_shelf_payloads(
    database,
):
    from afterword_engine import scoring_evidence as scoring_evidence_module

    rated_id, _unrated_id, _excluded_id, eligible_id = _create_production_shape(database)
    with transaction() as con:
        boosted_id = _candidate(con, "Legacy V1 Boosted Book", "V. Shelf")
    with connect() as con:
        all_reads = [dict(r) for r in con.execute("SELECT * FROM reads ORDER BY id").fetchall()]
        rated_reads = [r for r in all_reads if r["id"] == rated_id]
        unboosted_cand = _served_candidate(con, eligible_id)
        boosted_cand = _served_candidate(con, boosted_id)
        read_vec = np.asarray([0.8, 0.2, 0.1], dtype=np.float32)
        cand_vec = np.asarray([0.7, 0.4, 0.2], dtype=np.float32)
        boosted_vec = np.asarray([0.65, 0.45, 0.25], dtype=np.float32)
        ranked = rank_candidates(
            rated_reads,
            [read_vec],
            [unboosted_cand, boosted_cand],
            [cand_vec, boosted_vec],
        )
        for item in ranked:
            base_score = float(item["score"])
            on_to_read = int(item["id"]) == boosted_id
            if on_to_read:
                item["score"] = round(min(100.0, base_score + 10.0), 1)
                item["explanation"] = [
                    "On your Goodreads to-read shelf",
                    *list(item.get("explanation") or []),
                ]
            item["base_score"] = base_score
            item["shelf_boost"] = round(float(item["score"]) - base_score, 1)
            item["on_goodreads_to_read"] = on_to_read
        v2_ev = build_scoring_batch_evidence(
            con,
            captured_at="2026-10-02T00:00:00+00:00",
            read_history=all_reads,
            rated_reads=rated_reads,
            candidates=[unboosted_cand, boosted_cand],
            ranked=ranked,
            read_vectors=[read_vec],
            candidate_vectors={eligible_id: cand_vec, boosted_id: boosted_vec},
            backend="local",
            model="test-model",
            scoring_batch_size=SCORING_BATCH_SIZE,
            to_read_candidate_ids=[boosted_id],
        )

    # Strip v2 fields to construct an authentic main-produced schema_version=1 batch
    # containing both an unboosted candidate and a +10.0 shelf-boosted candidate.
    v1_ev = dict(v2_ev)
    v1_ev["schema_version"] = 1
    v1_ev["input_selection"] = {
        k: v
        for k, v in v2_ev["input_selection"].items()
        if k != "to_read_candidate_ids"
    }
    v1_ev["candidate_outputs"] = [
        {
            k: v
            for k, v in item.items()
            if k not in {"base_score", "shelf_boost", "on_goodreads_to_read"}
        }
        for item in v2_ev["candidate_outputs"]
    ]
    v1_raw_payload = {k: v for k, v in v1_ev.items() if not k.startswith("_")}
    v1_ev["_raw_json"] = scoring_evidence_module._canonical_json(v1_raw_payload)

    raw_shelf_ev = dict(v2_ev)
    raw_shelf_ev["goodreads_shelf_items"] = [
        {"shelf": "to-read", "title": "Leaked Shelf Title", "author": "Author"}
    ]
    raw_shelf_payload = {k: v for k, v in raw_shelf_ev.items() if not k.startswith("_")}
    raw_shelf_ev["_raw_json"] = scoring_evidence_module._canonical_json(raw_shelf_payload)

    bad_manifest_ev = dict(v2_ev)
    bad_manifest_ev["input_selection"] = {
        **v2_ev["input_selection"],
        "to_read_candidate_ids": ["not-an-int"],
    }
    bad_manifest_payload = {
        k: v for k, v in bad_manifest_ev.items() if not k.startswith("_")
    }
    bad_manifest_ev["_raw_json"] = scoring_evidence_module._canonical_json(
        bad_manifest_payload
    )

    with transaction() as con:
        assert (
            scoring_evidence_module.persist_scoring_batch(
                con, batch_id="legacy-v1-batch", evidence=v1_ev
            )
            == "complete"
        )
        assert (
            scoring_evidence_module.persist_scoring_batch(
                con, batch_id="raw-shelf-batch", evidence=raw_shelf_ev
            )
            == "complete"
        )
        assert (
            scoring_evidence_module.persist_scoring_batch(
                con, batch_id="bad-manifest-batch", evidence=bad_manifest_ev
            )
            == "complete"
        )

    with connect() as con:
        loaded_v1 = load_scoring_batch(con, "legacy-v1-batch")
        with pytest.raises(ValueError, match="must not persist raw Goodreads shelf items"):
            load_scoring_batch(con, "raw-shelf-batch")
        with pytest.raises(ValueError, match="shelf match manifest is invalid"):
            load_scoring_batch(con, "bad-manifest-batch")
        invalid_build = build_scoring_batch_evidence(
            con,
            captured_at="2026-10-02T00:00:00+00:00",
            read_history=all_reads,
            rated_reads=rated_reads,
            candidates=[unboosted_cand, boosted_cand],
            ranked=ranked,
            read_vectors=[read_vec],
            candidate_vectors={eligible_id: cand_vec, boosted_id: boosted_vec},
            backend="local",
            model="test-model",
            scoring_batch_size=SCORING_BATCH_SIZE,
            to_read_candidate_ids=["not-an-int"],
        )
    assert loaded_v1["schema_version"] == 1
    assert loaded_v1["capture_status"] == "complete"
    assert "goodreads_shelf_items" not in loaded_v1["decision"]
    replayed_v1 = replay_scoring_batch(loaded_v1)
    assert replayed_v1["schema_version"] == 1
    assert replayed_v1["verification"]["matches_archived"] is True
    assert replayed_v1["verification"]["max_score_drift"] == 0.0
    v1_replayed_by_id = {
        int(item["candidate_id"]): item for item in replayed_v1["candidate_outputs"]
    }
    assert v1_replayed_by_id[boosted_id]["on_goodreads_to_read"] is True
    assert v1_replayed_by_id[boosted_id]["shelf_boost"] > 0.0
    assert v1_replayed_by_id[eligible_id]["on_goodreads_to_read"] is False
    assert v1_replayed_by_id[eligible_id]["shelf_boost"] == 0.0

    # Tampering with the archived v1 boosted score is caught as drift.
    drifted_v1 = {
        **loaded_v1,
        "decision": {
            **loaded_v1["decision"],
            "candidate_outputs": [
                (
                    {**item, "score": round(float(item["score"]) - 5.0, 1)}
                    if int(item["candidate_id"]) == boosted_id
                    else dict(item)
                )
                for item in loaded_v1["decision"]["candidate_outputs"]
            ],
        },
    }
    drifted_replay = replay_scoring_batch(drifted_v1)
    assert drifted_replay["verification"]["matches_archived"] is False
    assert drifted_replay["verification"]["max_score_drift"] == 5.0

    assert invalid_build["capture_status"] == "incomplete"
    assert invalid_build["reason"] == "invalid_shelf_match_manifest"


def test_goodreads_shelf_delete_and_reset_purge_evidence_while_preserving_feedback_and_promoted_reads(
    database, monkeypatch
):
    from afterword_engine import ingestion
    from afterword_engine.ingestion import import_goodreads_rss, import_goodreads_shelves
    from afterword_engine.main import tracked_recommendations

    _rated_id, _unrated_id, _excluded_id, eligible_id = _create_production_shape(database)

    # 1. Score `eligible_id` before any Goodreads shelves exist so we have a clean non-shelf batch & run.
    assert asyncio.run(score_all(embedder=_CachedEmbedder())) == 1
    with connect() as con:
        clean_served, clean_run_id = tracked_recommendations(
            status="recommended", limit=10, offset=0, connection=con
        )
        clean_batch_id = con.execute(
            "SELECT score_batch_id FROM candidates WHERE id=?", (eligible_id,)
        ).fetchone()[0]
    assert [item["id"] for item in clean_served] == [eligible_id]
    assert clean_run_id is not None
    assert clean_batch_id is not None

    # 2. Score and serve `Promoted Soon` in its own shelf-boosted batch & run, then promote it to `reads`.
    with transaction() as con:
        promoted_id = _candidate(con, "Promoted Soon", "P. Writer")
        _store_vector(
            con,
            "candidate",
            promoted_id,
            _served_candidate(con, promoted_id),
            [0.72, 0.32, 0.18],
        )
        con.execute(
            "INSERT INTO goodreads_shelf_items(shelf,title,author,added_at) "
            "VALUES('to-read','Promoted Soon','P. Writer','2026-09-02')"
        )
    assert asyncio.run(score_all(embedder=_CachedEmbedder())) == 2
    with connect() as con:
        promoted_served, promoted_run_id = tracked_recommendations(
            status="recommended", limit=10, offset=0, connection=con
        )
        promoted_batch_id = con.execute(
            "SELECT score_batch_id FROM candidates WHERE id=?", (promoted_id,)
        ).fetchone()[0]
    with transaction() as con:
        con.execute(
            "UPDATE recommendation_impressions SET visible_at=presented_at "
            "WHERE run_id=? AND candidate_id=?",
            (promoted_run_id, promoted_id),
        )
    assert {item["id"] for item in promoted_served} >= {promoted_id}

    promoted_rss = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>Promoted Soon</title>
        <author_name>P. Writer</author_name>
        <user_rating>5</user_rating>
        <user_read_at>2026/12/01</user_read_at>
      </item>
    </channel></rss>"""

    async def fake_read_rss(url: str, **kwargs):
        return promoted_rss, "application/rss+xml"

    monkeypatch.setattr(ingestion, "fetch_bytes", fake_read_rss)
    assert (
        asyncio.run(
            import_goodreads_rss(
                "https://www.goodreads.com/review/list_rss/12345?key=test&shelf=read"
            )
        )
        == 1
    )
    with transaction() as con:
        promoted_read = con.execute(
            "SELECT * FROM reads WHERE title='Promoted Soon'"
        ).fetchone()
        _store_vector(
            con, "read", promoted_read["id"], dict(promoted_read), [0.72, 0.32, 0.18]
        )

    # 3. Add a separate ISBN-matched shelf item ("Different Edition Title") + unscored item, score & serve.
    with transaction() as con:
        boosted_id = _candidate(con, "Shelf Boosted Target", "S. Shelf")
        con.execute(
            "UPDATE candidate_quality SET isbn13='9780593135204' WHERE candidate_id=?",
            (boosted_id,),
        )
        _store_vector(
            con,
            "candidate",
            boosted_id,
            _served_candidate(con, boosted_id),
            [0.75, 0.35, 0.15],
        )
        con.executemany(
            "INSERT INTO goodreads_shelf_items(shelf,source,title,author,isbn,added_at) "
            "VALUES(?,'goodreads_rss',?,?,?,?)",
            [
                (
                    "to-read",
                    "Different Edition Title",
                    "S. Shelf",
                    "9780593135204",
                    "2026-09-01",
                ),
                (
                    "to-read",
                    "Unscored Shelf Entry",
                    "U. Author",
                    "9780000000111",
                    "2026-09-03",
                ),
            ],
        )

    assert asyncio.run(score_all(embedder=_CachedEmbedder())) == 2
    with connect() as con:
        shelf_served, shelf_run_id = tracked_recommendations(
            status="recommended", limit=10, offset=0, connection=con
        )
        shelf_batch_id = con.execute(
            "SELECT score_batch_id FROM candidates WHERE id=?", (boosted_id,)
        ).fetchone()[0]
    with transaction() as con:
        con.execute(
            "INSERT INTO feedback(candidate_id,action) VALUES(?,?)",
            (boosted_id, "save"),
        )
    assert len({clean_batch_id, promoted_batch_id, shelf_batch_id}) == 3
    assert {item["id"] for item in shelf_served} >= {boosted_id}

    # 4. Reset both Goodreads shelves to empty feeds (user deleted remaining shelf items).
    empty_rss = b"""<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel></channel></rss>"""

    async def fake_empty_shelves(url: str, **kwargs):
        return empty_rss, "application/xml"

    monkeypatch.setattr(ingestion, "fetch_bytes", fake_empty_shelves)
    counts = asyncio.run(
        import_goodreads_shelves(
            "https://www.goodreads.com/review/list_rss/12345?key=test&shelf=read"
        )
    )
    assert counts == {"to-read": 0, "currently-reading": 0}

    with connect() as con:
        assert (
            con.execute("SELECT COUNT(*) FROM goodreads_shelf_items").fetchone()[0] == 0
        )
        # Deleted shelf item's batch & run were purged on shelf reset.
        assert (
            con.execute(
                "SELECT 1 FROM scoring_batches WHERE id=?", (shelf_batch_id,)
            ).fetchone()
            is None
        )
        assert (
            con.execute(
                "SELECT 1 FROM recommendation_runs WHERE id=?", (shelf_run_id,)
            ).fetchone()
            is None
        )
        assert (
            con.execute(
                "SELECT score_batch_id FROM candidates WHERE id=?", (boosted_id,)
            ).fetchone()[0]
            is None
        )
        # Promoted read's pre-promotion batch, attributed run, and outcome intentionally remain intact.
        assert (
            con.execute(
                "SELECT 1 FROM scoring_batches WHERE id=?", (promoted_batch_id,)
            ).fetchone()
            is not None
        )
        assert (
            con.execute(
                "SELECT 1 FROM recommendation_runs WHERE id=?", (promoted_run_id,)
            ).fetchone()
            is not None
        )
        assert (
            con.execute(
                "SELECT run_id FROM recommendation_evidence_outcomes WHERE candidate_id=?",
                (promoted_id,),
            ).fetchone()["run_id"]
            == promoted_run_id
        )
        # Unrelated non-shelf batch & run remain intact, and user `save` feedback is preserved.
        assert (
            con.execute(
                "SELECT 1 FROM scoring_batches WHERE id=?", (clean_batch_id,)
            ).fetchone()
            is not None
        )
        assert (
            con.execute(
                "SELECT 1 FROM recommendation_runs WHERE id=?", (clean_run_id,)
            ).fetchone()
            is not None
        )
        assert (
            con.execute(
                "SELECT COUNT(*) FROM feedback WHERE candidate_id=? AND action='save'",
                (boosted_id,),
            ).fetchone()[0]
            == 1
        )

    # 5. Erasing the promoted `read` row via `read_ids` also purges its pre-promotion shelf batch & run.
    with transaction() as con:
        read_purged = purge_recommendation_evidence(con, read_ids=[int(promoted_read["id"])])
        assert read_purged["scoring_batches_removed"] >= 1
        assert read_purged["runs_removed"] >= 1
        assert (
            con.execute(
                "SELECT 1 FROM scoring_batches WHERE id=?", (promoted_batch_id,)
            ).fetchone()
            is None
        )
        assert (
            con.execute(
                "SELECT 1 FROM recommendation_runs WHERE id=?", (promoted_run_id,)
            ).fetchone()
            is None
        )
        clean_only_budget = con.execute(
            "SELECT used_bytes FROM recommendation_evidence_budget WHERE id=1"
        ).fetchone()[0]
        assert clean_only_budget > 0

    # 6. Verify `shelf_items` also purges shelf-boosted evidence even after the candidate row was deleted.
    with transaction() as con:
        con.execute("UPDATE candidates SET status='new', score=0.0 WHERE id=?", (boosted_id,))
        con.execute(
            "INSERT INTO goodreads_shelf_items(shelf,source,title,author,isbn) "
            "VALUES('to-read','goodreads_rss','Different Edition Title','S. Shelf','9780593135204')"
        )
    assert asyncio.run(score_all(embedder=_CachedEmbedder())) == 2
    with connect() as con:
        _, second_shelf_run = tracked_recommendations(
            status="recommended", limit=10, offset=0, connection=con
        )
    assert second_shelf_run is not None

    with transaction() as con:
        con.execute("DELETE FROM feedback WHERE candidate_id=?", (boosted_id,))
        con.execute("DELETE FROM candidates WHERE id=?", (boosted_id,))
    counts_after_cand_delete = asyncio.run(
        import_goodreads_shelves(
            "https://www.goodreads.com/review/list_rss/12345?key=test&shelf=read"
        )
    )
    assert counts_after_cand_delete == {"to-read": 0, "currently-reading": 0}
    with connect() as con:
        assert (
            con.execute(
                "SELECT 1 FROM recommendation_runs WHERE id=?", (second_shelf_run,)
            ).fetchone()
            is None
        )
        assert (
            con.execute(
                "SELECT used_bytes FROM recommendation_evidence_budget WHERE id=1"
            ).fetchone()[0]
            == clean_only_budget
        )


def test_scoring_batch_payload_and_budget_have_zero_growth_with_large_goodreads_shelf(
    database,
):
    _rated_id, _unrated_id, _excluded_id, eligible_id = _create_production_shape(database)

    assert asyncio.run(score_all(embedder=_CachedEmbedder())) == 1
    with connect() as con:
        baseline_batch = con.execute(
            "SELECT id, payload_bytes, length(payload) AS compressed_bytes "
            "FROM scoring_batches"
        ).fetchone()
        baseline_budget = con.execute(
            "SELECT used_bytes FROM recommendation_evidence_budget WHERE id=1"
        ).fetchone()[0]

    with transaction() as con:
        con.executemany(
            "INSERT INTO goodreads_shelf_items(shelf,title,author,isbn,openlibrary_work_id,added_at) "
            "VALUES(?,?,?,?,?,?)",
            [
                (
                    "to-read" if idx % 3 else "currently-reading",
                    f"Unscored Shelf Volume {idx:04d}: Extended Edition Subtitle",
                    f"Shelf Author {idx:04d}",
                    f"979123456{idx:04d}",
                    f"OL{100000 + idx}W",
                    "2026-09-15",
                )
                for idx in range(2_500)
            ],
        )

    batch_metrics: list[tuple[int, int]] = []
    for idx in range(3):
        with transaction() as con:
            # Re-score the exact same `eligible_id` candidate against the 2,500-item shelf
            # so payload sizes are directly comparable byte-for-byte.
            con.execute(
                "UPDATE candidates SET status='new', score=0.0, score_batch_id=NULL WHERE id=?",
                (eligible_id,),
            )
            if idx == 2:
                cand_row = con.execute(
                    "SELECT title, author FROM candidates WHERE id=?", (eligible_id,)
                ).fetchone()
                con.execute(
                    "INSERT INTO goodreads_shelf_items(shelf,title,author) VALUES('to-read',?,?)",
                    (cand_row["title"], cand_row["author"]),
                )
        assert asyncio.run(score_all(embedder=_CachedEmbedder())) == 1
        with connect() as con:
            batch_id = con.execute(
                "SELECT score_batch_id FROM candidates WHERE id=?", (eligible_id,)
            ).fetchone()[0]
            row = con.execute(
                "SELECT payload_bytes, length(payload) AS compressed_bytes "
                "FROM scoring_batches WHERE id=?",
                (batch_id,),
            ).fetchone()
            batch_metrics.append((int(row["payload_bytes"]), int(row["compressed_bytes"])))

    # Unmatched batches with 2,500 shelf items have literally zero shelf-induced payload growth
    # (only timestamps differ, which are fixed-width ISO strings), and a matched batch adds
    # only the integer candidate ID + boost explanation (~40 bytes uncompressed).
    assert batch_metrics[0][0] == int(baseline_batch["payload_bytes"])
    assert batch_metrics[1][0] == int(baseline_batch["payload_bytes"])
    assert abs(batch_metrics[2][0] - int(baseline_batch["payload_bytes"])) <= 64
    with connect() as con:
        final_budget = con.execute(
            "SELECT used_bytes FROM recommendation_evidence_budget WHERE id=1"
        ).fetchone()[0]
    # Total budget across all 4 batches stays under a few KiB rather than ~4x 2,500 shelf dumps.
    assert final_budget < baseline_budget * 5
    assert final_budget < 20_000


def test_clear_all_evidence_preserves_user_feedback_rows(database):
    _rated_id, _unrated_id, _excluded_id, eligible_id = _create_production_shape(database)
    assert asyncio.run(score_all(embedder=_CachedEmbedder())) == 1
    from afterword_engine.learning import create_recommendation_run, record_event_in_connection

    with connect() as con:
        dismissed_id = int(
            con.execute("SELECT id FROM candidates WHERE id!=? LIMIT 1", (eligible_id,)).fetchone()[0]
        )
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
        record_event_in_connection(
            con,
            event_key="evt-clear-all-save-1",
            event_type="save",
            candidate_id=eligible_id,
            run_id=run_id,
        )
        con.executemany(
            "INSERT INTO feedback(candidate_id,action) VALUES(?,?)",
            [(eligible_id, "save"), (dismissed_id, "reject")],
        )
        result = purge_recommendation_evidence(con, clear_all_evidence=True)
        assert result["runs_removed"] == 1
        assert result["scoring_batches_removed"] == 1
        assert con.execute("SELECT COUNT(*) FROM recommendation_runs").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM scoring_batches").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM recommendation_events").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM recommendation_evidence_events").fetchone()[0] == 0
        assert (
            con.execute(
                "SELECT candidate_id, action FROM feedback ORDER BY candidate_id, action"
            ).fetchall()
            != []
        )
        saved_feedback = [
            (int(row["candidate_id"]), str(row["action"]))
            for row in con.execute(
                "SELECT candidate_id, action FROM feedback ORDER BY candidate_id, action"
            ).fetchall()
        ]
        assert saved_feedback == sorted([(eligible_id, "save"), (dismissed_id, "reject")])

