import hashlib
import json
from pathlib import Path
import sqlite3
from datetime import datetime

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
from afterword_engine.learning import create_recommendation_run, record_event_in_connection
from afterword_engine.scoring import document
from afterword_engine.interaction_personalization import personalize_recommendations
from afterword_engine.discovery_slate import diversify_discovery_slate
from afterword_engine.main import tracked_recommendations


@pytest.fixture()
def database(tmp_path: Path):
    settings.db = str(tmp_path / "decision-evidence.db")
    initialize()
    return settings.db


def _candidate(con, title, author, *, score=71.2):
    source_id = con.execute("SELECT id FROM sources WHERE is_default=1 LIMIT 1").fetchone()[0]
    candidate_id = con.execute(
        "INSERT INTO candidates(title,author,description,genres,score,status,source_id,normalized_key) "
        "VALUES(?,?,?,?,?,'recommended',?,?)",
        (title, author, f"A captured description for {title}.", json.dumps(["Science fiction"]), score, source_id, f"{title}-{author}"),
    ).lastrowid
    con.execute(
        "UPDATE candidate_quality SET quality_status='accepted',quality_score=0.82,"
        "metadata_confidence=0.68 WHERE candidate_id=?",
        (candidate_id,),
    )
    return {
        "id": candidate_id,
        "title": title,
        "author": author,
        "description": f"A captured description for {title}.",
        "genres": ["Science fiction"],
        "score": score,
        "explanation": ["From the default source"],
        "status": "recommended",
        "source_id": source_id,
        "source_name": "Default feed",
        "source_weight": 0.5,
        "catalog_confidence": 0.82,
        "quality_score": 0.82,
        "quality_status": "accepted",
        "metadata_confidence": 0.68,
        "release_date": "2026-11-01",
        "date_kind": "day",
    }


def _build(con, pool, events, vectors, **context):
    return build_decision_evidence(
        con,
        base_pool=pool,
        personalized_pool=pool,
        final_pool=pool,
        interaction_events=events,
        cached_vectors=vectors,
        ranking_metadata={
            "mode": "confidence_gated_live",
            "scope": "installation",
            "applied": True,
            "adjusted_candidates": 1,
            "secret": "must-not-be-captured",
        },
        discovery_slate_metadata={
            "policy_version": "discovery-slate-v1",
            "applied": False,
            "reason": "no_qualifying_swaps",
        },
        request_context={
            "status": "recommended",
            "limit": 24,
            "offset": 0,
            "ranking_at": "2026-10-02T19:37:00-07:00",
            "exploration_enabled": False,
            "exploration_epsilon": 0.0,
            "exploration_stable_top_k": 8,
            "build_id": "test-build-127",
            "session_id": "must-not-be-captured",
            "embedding_api_key": "must-not-be-captured",
            "general_setting": "must-not-be-captured",
            **context,
        },
    )


def _set_embeddings(con, candidates, vectors):
    con.execute("UPDATE settings SET value='local' WHERE key='embedding_backend'")
    con.execute("UPDATE settings SET value='hashing-768' WHERE key='embedding_model'")
    for candidate in candidates:
        candidate_id = int(candidate["id"])
        vector = np.asarray(vectors[candidate_id], dtype=np.float32)
        con.execute(
            "INSERT INTO embeddings(entity_type,entity_id,backend,model,vector,dimensions,content_hash) "
            "VALUES('candidate',?,'local','hashing-768',?,?,?)",
            (
                candidate_id,
                vector_blob(vector),
                int(vector.size),
                content_hash(document(candidate)),
            ),
        )


def test_decision_snapshot_replays_online_inputs_and_survives_catalog_cleanup(database):
    vector = np.asarray([0.25, -0.5, 0.75], dtype=np.float32)
    with transaction() as con:
        candidate = _candidate(con, "After the Blue Moon", "R. Example")
        _set_embeddings(con, [candidate], {candidate["id"]: vector})
        evidence = _build(
            con,
            [candidate],
            [
                {
                    "id": 901,
                    "candidate_id": candidate["id"],
                    "event_type": "save",
                    "value": None,
                    "occurred_at": "2026-10-01T12:00:00+00:00",
                    "title": candidate["title"],
                    "author": candidate["author"],
                    "genres": json.dumps(candidate["genres"]),
                    "description": candidate["description"],
                    "metadata": {"session_id": "private-event-metadata"},
                }
            ],
            {candidate["id"]: vector},
        )

    assert evidence["capture_status"] == "complete"
    assert evidence["base_score_provenance"]["derivation_status"] == "unknown"
    assert evidence["policy"]["build_id"] == "test-build-127"
    assert evidence["request_context"]["ranking_at"] == "2026-10-02T19:37:00-07:00"
    assert evidence["request_context"]["exploration_enabled"] is False
    assert "session_id" not in evidence["request_context"]
    assert "embedding_api_key" not in evidence["request_context"]
    assert evidence["policy"]["ranking"] == {
        "mode": "confidence_gated_live",
        "scope": "installation",
        "applied": True,
        "adjusted_candidates": 1,
    }
    artifact = evidence["_vector_artifacts"][0]
    assert artifact["source_content_hash"] == content_hash(document(candidate))
    assert artifact["source_hash_status"] == "cache_match"
    assert artifact["vector_bytes"] == vector.astype("<f4").tobytes()
    assert b"must-not-be-captured" not in json.dumps(
        {key: value for key, value in evidence.items() if not key.startswith("_")}
    ).encode()

    run_id = create_recommendation_run([candidate], decision_evidence=evidence)
    with transaction() as con:
        record_event_in_connection(
            con,
            event_key="visible-evidence-001",
            candidate_id=candidate["id"],
            event_type="visible",
            run_id=run_id,
            occurred_at="2026-10-02T19:38:00-07:00",
        )
        record_event_in_connection(
            con,
            event_key="save-evidence-001",
            candidate_id=candidate["id"],
            event_type="save",
            run_id=run_id,
            label=1.0,
            label_kind="explicit_feedback",
            confidence=0.8,
        )
        logical_budget = (
            con.execute(
                "SELECT COALESCE(SUM(length(payload)),0) total "
                "FROM recommendation_run_evidence WHERE payload IS NOT NULL"
            ).fetchone()["total"]
            + con.execute(
                "SELECT COALESCE(SUM(length(vector)),0) total "
                "FROM recommendation_evidence_vectors"
            ).fetchone()["total"]
            + 96 * con.execute(
                "SELECT COUNT(*) count FROM recommendation_run_evidence_vectors"
            ).fetchone()["count"]
            + con.execute(
                "SELECT COALESCE(SUM(72+length(identity_hash)),0) total "
                "FROM recommendation_run_evidence_candidates"
            ).fetchone()["total"]
            + con.execute(
                "SELECT COALESCE(SUM(160+length(CAST(candidate_snapshot_json AS BLOB))),0) total "
                "FROM recommendation_evidence_exposures"
            ).fetchone()["total"]
            + con.execute(
                "SELECT COALESCE(SUM(128+length(CAST(event_key AS BLOB)) "
                "+length(CAST(event_type AS BLOB))+length(CAST(source AS BLOB)) "
                "+length(CAST(occurred_at AS BLOB))),0) total "
                "FROM recommendation_evidence_events"
            ).fetchone()["total"]
            + con.execute(
                "SELECT COALESCE(SUM(192+length(CAST(event_key AS BLOB)) "
                "+length(CAST(label_kind AS BLOB))+length(CAST(presented_at AS BLOB)) "
                "+length(CAST(attributed_at AS BLOB)) "
                "+COALESCE(length(CAST(visible_at AS BLOB)),0)),0) total "
                "FROM recommendation_evidence_outcomes"
            ).fetchone()["total"]
        )
        assert con.execute(
            "SELECT used_bytes FROM recommendation_evidence_budget WHERE id=1"
        ).fetchone()["used_bytes"] == logical_budget
        before = con.execute(
            "SELECT used_bytes FROM recommendation_evidence_budget WHERE id=1"
        ).fetchone()["used_bytes"]
        con.execute("DELETE FROM candidates WHERE id=?", (candidate["id"],))
        after = con.execute(
            "SELECT used_bytes FROM recommendation_evidence_budget WHERE id=1"
        ).fetchone()["used_bytes"]

    assert after == before
    with connect() as con:
        loaded = load_decision_evidence(con, run_id)
        assert loaded["capture_status"] == "complete"
        assert loaded["decision"]["pools"]["base"][0]["title"] == "After the Blue Moon"
        assert loaded["decision"]["base_score_provenance"]["derivation_status"] == "unknown"
        assert len(loaded["served_exposures"]) == 1
        assert loaded["served_exposures"][0]["candidate_id"] == candidate["id"]
        assert loaded["served_exposures"][0]["snapshot_status"] == "complete"
        assert [event["event_type"] for event in loaded["events"]] == ["visible", "save"]
        assert loaded["outcomes"][0]["rank"] == 1
        assert loaded["outcomes"][0]["label"] == 1.0
        stored_vector = loaded["decision"]["vectors"][0]
        assert stored_vector["dimensions"] == 3
        assert np.frombuffer(stored_vector["vector_bytes"], dtype="<f4").tolist() == vector.tolist()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            con.execute(
                "UPDATE recommendation_run_evidence SET reason='rewritten' WHERE run_id=?",
                (run_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            con.execute(
                "UPDATE recommendation_evidence_exposures SET score=0 WHERE run_id=?",
                (run_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            con.execute(
                "UPDATE recommendation_evidence_events SET event_type='reject' WHERE run_id=?",
                (run_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            con.execute(
                "UPDATE recommendation_evidence_outcomes SET label=0 WHERE run_id=?",
                (run_id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            con.execute(
                "UPDATE recommendation_evidence_vectors SET model='changed'"
            )


def test_privacy_purge_removes_entire_run_and_unreferenced_artifacts(database):
    shared_vector = np.asarray([1.0, 0.0], dtype=np.float32)
    with transaction() as con:
        first = _candidate(con, "First Book", "A. Author")
        second = _candidate(con, "Second Book", "B. Author")
        _set_embeddings(con, [first, second], {first["id"]: shared_vector, second["id"]: shared_vector})
        evidence_first = _build(con, [first, second], [], {first["id"]: shared_vector, second["id"]: shared_vector})
        evidence_second = _build(con, [second], [], {second["id"]: shared_vector})
    run_first = create_recommendation_run([first, second], decision_evidence=evidence_first)
    run_second = create_recommendation_run([second], decision_evidence=evidence_second)

    with transaction() as con:
        record_event_in_connection(
            con,
            event_key="purge-save-event-001",
            candidate_id=first["id"],
            event_type="save",
            run_id=run_first,
            label=1.0,
            label_kind="explicit_feedback",
            confidence=1.0,
        )
        shared_artifact = con.execute(
            "SELECT artifact_hash FROM recommendation_evidence_vectors LIMIT 1"
        ).fetchone()["artifact_hash"]
        removed = purge_recommendation_evidence(con, candidate_ids=[first["id"]])
        assert removed["runs_removed"] == 1
        assert con.execute(
            "SELECT 1 FROM recommendation_run_evidence WHERE run_id=?", (run_first,)
        ).fetchone() is None
        assert con.execute(
            "SELECT 1 FROM recommendation_run_evidence WHERE run_id=?", (run_second,)
        ).fetchone() is not None
        assert con.execute(
            "SELECT COUNT(*) FROM recommendation_evidence_events WHERE run_id=?", (run_first,)
        ).fetchone()[0] == 0
        assert con.execute(
            "SELECT COUNT(*) FROM recommendation_evidence_outcomes WHERE run_id=?", (run_first,)
        ).fetchone()[0] == 0
        assert con.execute(
            "SELECT COUNT(*) FROM recommendation_evidence_exposures WHERE run_id=?", (run_first,)
        ).fetchone()[0] == 0
        assert con.execute(
            "SELECT 1 FROM recommendation_evidence_vectors WHERE artifact_hash=?", (shared_artifact,)
        ).fetchone() is not None

    with transaction() as con:
        removed = purge_recommendation_evidence(con, run_ids=[run_second])
        assert removed["runs_removed"] == 1
        assert con.execute("SELECT COUNT(*) FROM recommendation_evidence_vectors").fetchone()[0] == 0
        assert con.execute("SELECT used_bytes FROM recommendation_evidence_budget WHERE id=1").fetchone()[0] == 0


def test_actual_vector_and_text_hash_are_retained_when_cache_row_is_stale(database):
    vector = np.asarray([0.125, -0.25, 0.5], dtype=np.float32)
    with transaction() as con:
        candidate = _candidate(con, "Stale Cache Book", "D. Author")
        con.execute("UPDATE settings SET value='local' WHERE key='embedding_backend'")
        con.execute("UPDATE settings SET value='hashing-768' WHERE key='embedding_model'")
        con.execute(
            "INSERT INTO embeddings(entity_type,entity_id,backend,model,vector,dimensions,content_hash) "
            "VALUES('candidate',?,'local','hashing-768',?,?,?)",
            (candidate["id"], vector_blob(vector), int(vector.size), "stale-cache-hash"),
        )
        evidence = _build(con, [candidate], [], {candidate["id"]: vector})

    artifact = evidence["_vector_artifacts"][0]
    assert artifact["source_content_hash"] == content_hash(document(candidate))
    assert artifact["stored_cache_content_hash"] == "stale-cache-hash"
    assert artifact["source_hash_status"] == "cache_mismatch"
    assert artifact["vector_bytes"] == vector.astype("<f4").tobytes()

    run_id = create_recommendation_run([candidate], decision_evidence=evidence)
    with connect() as con:
        stored = load_decision_evidence(con, run_id)["decision"]["vectors"][0]
    assert stored["source_content_hash"] == content_hash(document(candidate))
    assert np.frombuffer(stored["vector_bytes"], dtype="<f4").tolist() == vector.tolist()


def test_capture_caps_and_storage_quota_fail_open_with_explicit_incomplete_marker(database):
    with transaction() as con:
        candidate = _candidate(con, "Bounded Book", "C. Author")
        too_many = _build(con, [{}] * 5_001, [], {})
        complete = _build(con, [candidate], [], {})
    assert too_many["capture_status"] == "incomplete"
    assert too_many["reason"] == "pool_limit_exceeded"

    run_id = create_recommendation_run([candidate], decision_evidence=too_many)
    with connect() as con:
        loaded = load_decision_evidence(con, run_id)
        assert loaded["capture_status"] == "incomplete"
        assert loaded["reason"] == "pool_limit_exceeded"

    with transaction() as con:
        con.execute(
            "UPDATE recommendation_evidence_budget SET used_bytes=? WHERE id=1",
            (512 * 1024 * 1024,),
        )
        candidate = dict(candidate)
        candidate["id"] = int(candidate["id"])
    quota_run = create_recommendation_run([candidate], decision_evidence=complete)
    with connect() as con:
        loaded = load_decision_evidence(con, quota_run)
        assert loaded["capture_status"] == "incomplete"
        assert loaded["reason"] == "storage_quota_exceeded"


@pytest.mark.parametrize(
    "status,limit,offset",
    [(None, 2, 1), ("recommended", 2, 1)],
    ids=["all-visible-page", "recommended-page"],
)
def test_full_serving_replay_matches_two_request_contexts(database, status, limit, offset):
    previous_exploration = settings.exploration_enabled
    settings.exploration_enabled = False
    try:
        with transaction() as con:
            con.execute("UPDATE candidates SET status='excluded'")
            visible = [
                _candidate(con, "Replay One", "A. Author", score=81.2),
                _candidate(con, "Replay Two", "B. Author", score=79.8),
                _candidate(con, "Replay Three", "C. Author", score=79.8),
                _candidate(con, "Replay Saved", "D. Author", score=20.0),
            ]
            con.execute(
                "UPDATE candidates SET status='saved' WHERE id=?", (visible[-1]["id"],)
            )
            vectors = {
                int(item["id"]): np.asarray([index + 1, 2, 1], dtype=np.float32)
                for index, item in enumerate(visible)
            }
            _set_embeddings(con, visible, vectors)

        with connect() as con:
            served, run_id = tracked_recommendations(
                status=status, limit=limit, offset=offset, connection=con
            )
        with connect() as con:
            record = load_decision_evidence(con, run_id)

        assert record["capture_status"] == "complete"
        decision = record["decision"]
        assert decision["base_score_provenance"]["derivation_status"] == "unknown"
        assert decision["request_context"]["ranking_at"]
        assert decision["request_context"]["exploration_enabled"] is False
        assert {
            "main.py",
            "scoring.py",
            "ranking.py",
            "interaction_personalization.py",
            "discovery_slate.py",
            "exploration.py",
        }.issubset(decision["policy"]["source_hashes"])
        vector_map = {
            int(item["entity_id"]): np.frombuffer(item["vector_bytes"], dtype="<f4")
            for item in decision["vectors"]
        }
        ranking_at = datetime.fromisoformat(decision["request_context"]["ranking_at"])
        replayed_personalized, _ = personalize_recommendations(
            decision["pools"]["base"],
            decision["interaction_profile"],
            now=ranking_at,
            interaction_vectors=vector_map,
            candidate_vectors=vector_map,
        )
        assert [item["id"] for item in replayed_personalized] == [
            item["id"] for item in decision["pools"]["personalized"]
        ]
        assert [item["score"] for item in replayed_personalized] == [
            item["score"] for item in decision["pools"]["personalized"]
        ]
        replayed_recommended = [
            item for item in replayed_personalized if item.get("status") == "recommended"
        ]
        replayed_diversified, _ = diversify_discovery_slate(
            replayed_recommended, vector_map
        )
        replayed_final = replayed_diversified + [
            item for item in replayed_personalized if item.get("status") != "recommended"
        ]
        assert [item["id"] for item in replayed_final] == [
            item["id"] for item in decision["pools"]["final"]
        ]
        assert [item["score"] for item in replayed_final] == [
            item["score"] for item in decision["pools"]["final"]
        ]

        replayed_served = replayed_final[offset : offset + limit]
        assert [item["id"] for item in replayed_served] == [
            item["id"] for item in served
        ]
        assert [item["score"] for item in replayed_served] == [
            item["score"] for item in served
        ]
        assert [item["candidate_id"] for item in record["served_exposures"]] == [
            item["id"] for item in served
        ]
    finally:
        settings.exploration_enabled = previous_exploration
