import hashlib
import json
from pathlib import Path
import sqlite3
from datetime import datetime
import zlib

import numpy as np
import pytest

from afterword_engine.config import settings
from afterword_engine.database import connect, initialize, transaction
from afterword_engine.decision_evidence import (
    EVIDENCE_SCHEMA_VERSION,
    build_decision_evidence,
    load_decision_evidence,
    persist_run_evidence,
    purge_recommendation_evidence,
    replay_decision_evidence,
    verify_decision_evidence_replay,
)
import afterword_engine.decision_evidence as decision_evidence_module
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


def test_run_quota_counts_shared_vector_bytes_once(database, monkeypatch):
    shared_vector = np.asarray([0.25, -0.5, 0.75], dtype=np.float32)
    with transaction() as con:
        first = _candidate(con, "Shared Vector First", "E. Author")
        second = _candidate(con, "Shared Vector Second", "F. Author")
        vectors = {first["id"]: shared_vector, second["id"]: shared_vector}
        _set_embeddings(con, [first, second], vectors)
        evidence = _build(con, [first, second], [], vectors)

        artifacts = evidence["_vector_artifacts"]
        manifest = []
        unique_bytes = {}
        for artifact in artifacts:
            raw = artifact["vector_bytes"]
            spec = decision_evidence_module._canonical_json(
                {
                    "backend": artifact["backend"],
                    "model": artifact["model"],
                    "dimensions": artifact["dimensions"],
                    "encoding": "float32-le",
                }
            )
            artifact_hash = hashlib.sha256(spec + b"\0" + raw).hexdigest()
            unique_bytes[artifact_hash] = len(raw)
            manifest.append(
                {
                    "entity_type": artifact["entity_type"],
                    "entity_id": artifact["entity_id"],
                    "backend": artifact["backend"],
                    "model": artifact["model"],
                    "dimensions": artifact["dimensions"],
                    "encoding": "float32-le",
                    "source_content_hash": artifact["source_content_hash"],
                    "artifact_hash": artifact_hash,
                    "vector_sha256": hashlib.sha256(raw).hexdigest(),
                }
            )
        payload = {
            key: value
            for key, value in evidence.items()
            if not key.startswith("_") and key != "vectors"
        }
        payload["vectors"] = manifest
        packed = zlib.compress(decision_evidence_module._canonical_json(payload), level=1)
        identities = evidence["_candidate_identities"]
        references = 96 * len(artifacts) + sum(
            72 + len(str(item["identity_hash"]).encode("utf-8"))
            for item in identities
        )
        exact_budget = len(packed) + sum(unique_bytes.values()) + references
        monkeypatch.setattr(
            decision_evidence_module, "MAX_STORED_EVIDENCE_BYTES", exact_budget
        )
        run_id = "shared-vector-quota-run"
        con.execute(
            "INSERT INTO recommendation_runs(id,policy,policy_version,candidate_count) "
            "VALUES(?,?,?,?)",
            (run_id, "fixture", "fixture-v1", 2),
        )
        assert persist_run_evidence(con, run_id=run_id, evidence=evidence) == "complete"
        assert con.execute(
            "SELECT COUNT(*) FROM recommendation_evidence_vectors"
        ).fetchone()[0] == 1
        assert con.execute(
            "SELECT used_bytes FROM recommendation_evidence_budget WHERE id=1"
        ).fetchone()[0] == exact_budget


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


def test_era_profile_fields_replay_reproduces_observed_books_and_era_adjustments(database):
    previous_exploration = settings.exploration_enabled
    settings.exploration_enabled = False
    try:
        with transaction() as con:
            con.execute("UPDATE candidates SET status='excluded'")
            source_id = con.execute("SELECT id FROM sources WHERE is_default=1 LIMIT 1").fetchone()[0]
            # Seed 22 historical acted candidates: 11 classic 1960s-1970s saves, 11 2020s rejects.
            for idx in range(11):
                classic_id = con.execute(
                    "INSERT INTO candidates(title,author,description,genres,score,status,source_id,normalized_key,release_date) "
                    "VALUES(?,?,?,?,75.0,'saved',?,?,?)",
                    (
                        f"Classic Era Save {idx}",
                        f"Classic Author {idx}",
                        "Vintage speculative work.",
                        json.dumps(["Space opera"]),
                        source_id,
                        f"classic-era-save-{idx}",
                        f"197{idx % 9}-06-15",
                    ),
                ).lastrowid
                con.execute(
                    "UPDATE candidate_quality SET quality_status='accepted',quality_score=0.85,metadata_confidence=0.8 WHERE candidate_id=?",
                    (classic_id,),
                )
                con.execute(
                    "INSERT INTO recommendation_events(event_key,candidate_id,event_type,source,occurred_at) "
                    "VALUES(?,?,'save','ui',?)",
                    (f"era-save-{idx}", classic_id, f"2026-10-{10 + (idx % 10):02d}T12:00:00+00:00"),
                )

                modern_id = con.execute(
                    "INSERT INTO candidates(title,author,description,genres,score,status,source_id,normalized_key,release_date) "
                    "VALUES(?,?,?,?,75.0,'excluded',?,?,?)",
                    (
                        f"Modern Era Reject {idx}",
                        f"Modern Author {idx}",
                        "Recent release.",
                        json.dumps(["Cyberpunk"]),
                        source_id,
                        f"modern-era-reject-{idx}",
                        f"202{idx % 5}-04-10",
                    ),
                ).lastrowid
                con.execute(
                    "UPDATE candidate_quality SET quality_status='accepted',quality_score=0.85,metadata_confidence=0.8 WHERE candidate_id=?",
                    (modern_id,),
                )
                con.execute(
                    "INSERT INTO recommendation_events(event_key,candidate_id,event_type,source,occurred_at) "
                    "VALUES(?,?,'reject','ui',?)",
                    (f"era-reject-{idx}", modern_id, f"2026-10-{10 + (idx % 10):02d}T13:00:00+00:00"),
                )

            cand_modern = _candidate(con, "Live Modern Candidate", "Z. New", score=78.0)
            cand_classic = _candidate(con, "Live Classic Candidate", "Y. Vintage", score=77.6)
            con.execute(
                "UPDATE candidates SET release_date='2024-09-01' WHERE id=?",
                (cand_modern["id"],),
            )
            con.execute(
                "UPDATE candidates SET release_date='1974-03-01' WHERE id=?",
                (cand_classic["id"],),
            )
            cand_modern["release_date"] = "2024-09-01"
            cand_classic["release_date"] = "1974-03-01"
            vectors = {
                int(cand_modern["id"]): np.asarray([1.0, 0.2, 0.1], dtype=np.float32),
                int(cand_classic["id"]): np.asarray([0.2, 1.0, 0.1], dtype=np.float32),
            }
            _set_embeddings(con, [cand_modern, cand_classic], vectors)

        with connect() as con:
            served, run_id = tracked_recommendations(
                status="recommended", limit=10, offset=0, connection=con
            )
        with connect() as con:
            record = load_decision_evidence(con, run_id)

        assert record["capture_status"] == "complete"
        decision = record["decision"]
        assert decision["schema_version"] == EVIDENCE_SCHEMA_VERSION == 2
        assert decision["policy"]["ranking"]["applied"] is True
        assert decision["policy"]["ranking"]["observed_books"] == 22
        assert any(item.get("release_date") for item in decision["interaction_profile"])

        replayed = replay_decision_evidence(record)
        assert replayed["verification"]["matches_archived"] is True
        assert replayed["verification"]["max_score_drift"] == 0.0
        assert replayed["diagnostics"]["personalization"]["observed_books"] == 22
        assert [item["id"] for item in replayed["pools"]["served"]] == [
            item["id"] for item in served
        ]
        assert [item["score"] for item in replayed["pools"]["served"]] == [
            item["score"] for item in served
        ]

        # Also verify explicit first_publication_year overrides reprint release_date in PROFILE_FIELDS.
        ranking_at_iso = "2026-11-15T12:00:00+00:00"
        fpy_events = [
            {
                "id": 5000 + i,
                "candidate_id": 8000 + i,
                "event_type": "save" if i < 11 else "reject",
                "value": None,
                "occurred_at": f"2026-11-{(i % 9) + 1:02d}T10:00:00+00:00",
                "title": f"Reprint Book {i}",
                "author": f"Unique Author {i}",
                "genres": json.dumps(["History"]),
                "description": "Reprint edition.",
                "release_date": "2024-01-01",
                "first_publication_year": 1968 if i < 11 else 2023,
            }
            for i in range(22)
        ]
        pool_inputs = [
            {**cand_modern, "release_date": "2024-09-01", "first_publication_year": 2024},
            {**cand_classic, "release_date": "2024-09-01", "first_publication_year": 1969},
        ]
        personalized_fpy, fpy_diag = personalize_recommendations(
            pool_inputs,
            fpy_events,
            now=datetime.fromisoformat(ranking_at_iso),
            interaction_vectors={},
            candidate_vectors=vectors,
        )
        assert fpy_diag["applied"] is True
        with connect() as con:
            fpy_evidence = build_decision_evidence(
                con,
                base_pool=pool_inputs,
                personalized_pool=personalized_fpy,
                final_pool=personalized_fpy,
                interaction_events=fpy_events,
                cached_vectors=vectors,
                ranking_metadata=fpy_diag,
                discovery_slate_metadata={"applied": False, "reason": "no_qualifying_swaps"},
                request_context={
                    "status": "recommended",
                    "limit": 10,
                    "offset": 0,
                    "ranking_at": ranking_at_iso,
                    "exploration_enabled": False,
                },
            )
        fpy_run_id = create_recommendation_run(personalized_fpy, decision_evidence=fpy_evidence)
        with connect() as con:
            loaded_fpy = load_decision_evidence(con, fpy_run_id)
        assert loaded_fpy["decision"]["interaction_profile"][0]["first_publication_year"] == 1968
        assert verify_decision_evidence_replay(loaded_fpy)["matches_archived"] is True
    finally:
        settings.exploration_enabled = previous_exploration


@pytest.mark.parametrize("rerank_mode", ["adjustments", "listwise"])
def test_impression_fatigue_and_llm_reranker_replay_without_network_or_secret_leakage(
    database, monkeypatch, rerank_mode
):
    import httpx
    from afterword_engine.secrets import seal

    monkeypatch.setattr(settings, "exploration_enabled", False)
    monkeypatch.setattr(settings, "impression_fatigue_enabled", True)
    monkeypatch.setattr(settings, "impression_fatigue_window_days", 21)
    monkeypatch.setattr(settings, "impression_fatigue_grace_days", 1)
    monkeypatch.setattr(settings, "impression_fatigue_penalty_per_day", 1.5)
    monkeypatch.setattr(settings, "impression_fatigue_max_penalty", 9.0)
    monkeypatch.setattr(settings, "llm_rerank_top_k", 24)
    monkeypatch.setattr(settings, "llm_rerank_max_delta", 2.0)

    with transaction() as con:
        con.execute("UPDATE candidates SET status='excluded'")
        first = _candidate(con, "Fatigued Front-Runner", "A. Writer", score=82.0)
        second = _candidate(con, "Fresh Challenger", "B. Writer", score=79.5)
        third = _candidate(con, "Steady Contender", "C. Writer", score=78.0)
        vectors = {
            int(first["id"]): np.asarray([1.0, 0.0, 0.0], dtype=np.float32),
            int(second["id"]): np.asarray([0.0, 1.0, 0.0], dtype=np.float32),
            int(third["id"]): np.asarray([0.0, 0.0, 1.0], dtype=np.float32),
        }
        _set_embeddings(con, [first, second, third], vectors)

        # Record 4 distinct unacted visible days within the last 10 days on `first` -> penalty 4.5.
        now_utc = datetime.now().astimezone()
        for day_offset in (1, 2, 3, 4):
            day_iso = (
                datetime.fromtimestamp(
                    now_utc.timestamp() - day_offset * 86400, tz=now_utc.tzinfo
                )
                .astimezone()
                .isoformat()
            )
            hist_run = f"fatigue-hist-{rerank_mode}-{day_offset}"
            con.execute(
                "INSERT INTO recommendation_runs(id,policy,policy_version,candidate_count,created_at) "
                "VALUES(?,?,?,1,?)",
                (hist_run, "default", "v1", day_iso),
            )
            con.execute(
                "INSERT INTO recommendation_impressions(run_id,candidate_id,rank,score,propensity,presented_at,visible_at) "
                "VALUES(?,?,1,82.0,1.0,?,?)",
                (hist_run, first["id"], day_iso, day_iso),
            )

        con.executemany(
            "INSERT INTO settings(key,value,secret) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,secret=excluded.secret",
            [
                ("llm_rerank_enabled", "1", 0),
                ("llm_rerank_provider", "openai", 0),
                (
                    "llm_rerank_api_url",
                    "https://user-cred:super-secret-pass@api.example.com/v1/chat/completions?token=query-secret-999#frag",
                    0,
                ),
                ("llm_rerank_api_key", seal("sk-live-super-secret-bearer-777"), 1),
                ("llm_rerank_model", "gpt-4o-mini", 0),
            ],
        )

    llm_calls = 0

    class _FakeLLMResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            if rerank_mode == "adjustments":
                body = {
                    "adjustments": [
                        {"id": third["id"], "delta": 2.5, "reason": "Compelling thematic depth"},
                        {"id": second["id"], "delta": -0.5, "reason": "Slightly narrower hook"},
                        {"id": 999999, "delta": 5.0, "reason": "Hallucinated candidate"},
                    ]
                }
            else:
                body = {
                    "rankings": [
                        {"id": third["id"], "reason": "Best overall fit"},
                        {"id": first["id"], "reason": "Strong premise despite delay"},
                        {"id": second["id"], "reason": "Solid runner-up"},
                    ]
                }
            return {"choices": [{"message": {"content": json.dumps(body)}}]}

    class _ServingClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, url: str, *, headers: dict, json: dict):
            nonlocal llm_calls
            llm_calls += 1
            assert "Bearer sk-live-super-secret-bearer-777" == headers["Authorization"]
            return _FakeLLMResponse()

    monkeypatch.setattr(httpx, "Client", _ServingClient)
    with connect() as con:
        served, run_id = tracked_recommendations(
            status="recommended", limit=10, offset=0, connection=con
        )
    assert llm_calls == 1

    # Block all HTTP clients during archive loading and replay to prove zero network calls.
    class _BlockedClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("Replay must never instantiate an HTTP client")

    monkeypatch.setattr(httpx, "Client", _BlockedClient)
    monkeypatch.setattr(httpx, "AsyncClient", _BlockedClient)

    with connect() as con:
        raw_blob = con.execute(
            "SELECT payload FROM recommendation_run_evidence WHERE run_id=?",
            (run_id,),
        ).fetchone()["payload"]
        raw_decompressed = zlib.decompress(raw_blob).decode("utf-8")
        record = load_decision_evidence(con, run_id)

    assert record["capture_status"] == "complete"
    decision = record["decision"]
    assert decision["schema_version"] == 2

    # Secret non-leakage assertions across raw compressed payload and loaded structure.
    serialized_loaded = json.dumps(
        {
            **decision,
            "vectors": [
                {k: v for k, v in item.items() if k != "vector_bytes"}
                for item in decision["vectors"]
            ],
        }
    )
    for text_blob in (raw_decompressed, serialized_loaded):
        assert "sk-live-super-secret-bearer-777" not in text_blob
        assert "super-secret-pass" not in text_blob
        assert "user-cred" not in text_blob
        assert "query-secret-999" not in text_blob
        assert "frag" not in text_blob
        assert "bearer" not in text_blob.lower()
        assert "api_key" not in text_blob.lower()
        assert "session_id" not in text_blob.lower()

    fatigue_stage = decision["stage_inputs"]["impression_fatigue"]
    assert fatigue_stage["settings"]["enabled"] is True
    assert fatigue_stage["visible_day_counts"] == {str(first["id"]): 4}
    personalized_by_id = {int(item["id"]): item for item in decision["pools"]["personalized"]}
    assert personalized_by_id[int(first["id"])]["fatigue_penalty"] == 4.5

    llm_stage = decision["stage_inputs"]["llm_rerank"]
    assert llm_stage["settings"]["enabled"] is True
    assert llm_stage["settings"]["configured"] is True
    assert llm_stage["settings"]["endpoint_url"] == "https://api.example.com/v1/chat/completions"
    assert llm_stage["settings"]["model"] == "gpt-4o-mini"
    assert llm_stage["mode"] == rerank_mode
    if rerank_mode == "adjustments":
        assert len(llm_stage["adjustments"]) == 2
        assert llm_stage["rankings"] == []
    else:
        assert len(llm_stage["rankings"]) == 3
        assert llm_stage["adjustments"] == []

    assert decision["policy"]["ranking"]["impression_fatigue"] == {
        "applied": True,
        "adjusted_candidates": 1,
        "max_penalty": 4.5,
    }
    assert decision["policy"]["ranking"]["llm_rerank"]["applied"] is True
    assert decision["policy"]["ranking"]["llm_rerank"]["mode"] == rerank_mode

    replayed = replay_decision_evidence(record)
    assert replayed["verification"]["matches_archived"] is True
    assert replayed["verification"]["max_score_drift"] == 0.0
    assert [item["id"] for item in replayed["pools"]["served"]] == [
        item["id"] for item in served
    ]
    assert [item["score"] for item in replayed["pools"]["served"]] == [
        item["score"] for item in served
    ]
    assert [item["explanation"] for item in replayed["pools"]["served"]] == [
        item["explanation"] for item in served
    ]

    # Verify candidate privacy purge clears the extended v2 run and restores zero budget.
    with transaction() as con:
        purge_recommendation_evidence(con, clear_all_evidence=True)
        assert (
            con.execute(
                "SELECT used_bytes FROM recommendation_evidence_budget WHERE id=1"
            ).fetchone()[0]
            == 0
        )


def test_runtime_source_lineage_covers_all_twenty_modules_and_tracks_stage_edits(
    tmp_path: Path, monkeypatch
):
    import shutil

    decision_evidence_module._runtime_source_lineage.cache_clear()
    try:
        lineage = decision_evidence_module._runtime_source_lineage()
        assert len(lineage["source_hashes"]) == 20
        assert {
            "impression_fatigue.py",
            "llm_reranker.py",
            "read_dates.py",
            "decision_evidence.py",
            "scoring_evidence.py",
        }.issubset(lineage["source_hashes"])
        for digest in lineage["source_hashes"].values():
            assert len(digest) == 64

        pkg_copy = tmp_path / "afterword_engine_copy"
        shutil.copytree(Path(decision_evidence_module.__file__).resolve().parent, pkg_copy)
        monkeypatch.setattr(
            decision_evidence_module, "__file__", str(pkg_copy / "decision_evidence.py")
        )
        decision_evidence_module._runtime_source_lineage.cache_clear()
        baseline = decision_evidence_module._runtime_source_lineage()
        assert baseline == lineage

        (pkg_copy / "impression_fatigue.py").write_text(
            (pkg_copy / "impression_fatigue.py").read_text(encoding="utf-8")
            + "\n# fatigue version bump\n",
            encoding="utf-8",
        )
        decision_evidence_module._runtime_source_lineage.cache_clear()
        after_fatigue = decision_evidence_module._runtime_source_lineage()
        assert (
            after_fatigue["source_hashes"]["impression_fatigue.py"]
            != baseline["source_hashes"]["impression_fatigue.py"]
        )
        assert after_fatigue["runtime_code_id"] != baseline["runtime_code_id"]

        (pkg_copy / "llm_reranker.py").write_text(
            (pkg_copy / "llm_reranker.py").read_text(encoding="utf-8")
            + "\n# reranker version bump\n",
            encoding="utf-8",
        )
        decision_evidence_module._runtime_source_lineage.cache_clear()
        after_reranker = decision_evidence_module._runtime_source_lineage()
        assert (
            after_reranker["source_hashes"]["llm_reranker.py"]
            != after_fatigue["source_hashes"]["llm_reranker.py"]
        )
        assert after_reranker["runtime_code_id"] != after_fatigue["runtime_code_id"]
    finally:
        decision_evidence_module._runtime_source_lineage.cache_clear()


def test_legacy_v1_decision_snapshot_loads_and_replays_and_rerank_cap_fails_open(database):
    vector = np.asarray([0.6, 0.8, 0.0], dtype=np.float32)
    with transaction() as con:
        candidate = _candidate(con, "Legacy V1 Candidate", "L. Author", score=80.0)
        _set_embeddings(con, [candidate], {candidate["id"]: vector})
        v2_evidence = _build(con, [candidate], [], {candidate["id"]: vector})

        # Convert to a legacy schema_version=1 payload without stage_inputs or v2 fields.
        v1_evidence = dict(v2_evidence)
        v1_evidence["schema_version"] = 1
        v1_evidence.pop("stage_inputs", None)
        v1_raw_payload = {
            k: v
            for k, v in v1_evidence.items()
            if not k.startswith("_")
        }
        v1_evidence["_raw_json"] = decision_evidence_module._canonical_json(v1_raw_payload)

        run_id = "legacy-v1-decision-run"
        con.execute(
            "INSERT INTO recommendation_runs(id,policy,policy_version,candidate_count) "
            "VALUES(?,?,?,1)",
            (run_id, "fixture", "v1",),
        )
        assert persist_run_evidence(con, run_id=run_id, evidence=v1_evidence) == "complete"

        over_cap = build_decision_evidence(
            con,
            base_pool=[candidate],
            personalized_pool=[candidate],
            final_pool=[candidate],
            interaction_events=[],
            cached_vectors={candidate["id"]: vector},
            ranking_metadata={},
            discovery_slate_metadata={},
            request_context={"ranking_at": "2026-10-02T19:37:00-07:00"},
            llm_rerank_adjustments=[{"id": candidate["id"], "delta": 0.1}] * 5_001,
        )
        assert over_cap["capture_status"] == "incomplete"
        assert over_cap["reason"] == "rerank_limit_exceeded"

    with connect() as con:
        loaded = load_decision_evidence(con, run_id)
    assert loaded["schema_version"] == 1
    assert loaded["capture_status"] == "complete"
    assert "stage_inputs" not in loaded["decision"]
    replayed = replay_decision_evidence(loaded)
    assert replayed["schema_version"] == 1
    assert replayed["verification"]["matches_archived"] is True
    assert replayed["verification"]["max_score_drift"] == 0.0

