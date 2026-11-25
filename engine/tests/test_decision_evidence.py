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
    build_decision_evidence,
    load_decision_evidence,
    persist_run_evidence,
    purge_recommendation_evidence,
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


def test_purge_read_ids_expands_two_part_three_part_and_manual_read_keys(database):
    vec = np.asarray([0.5, 0.25, -0.5], dtype=np.float32)
    with transaction() as con:
        cand_a = _candidate(con, "Purge Read Alpha", "A. Writer")
        cand_b = _candidate(con, "Purge Read Beta", "B. Writer")
        cand_c = _candidate(con, "Purge Read Gamma", "C. Writer")
        cand_keep = _candidate(con, "Purge Read Keep", "D. Writer")
        all_cands = [cand_a, cand_b, cand_c, cand_keep]
        vec_map = {item["id"]: vec for item in all_cands}
        _set_embeddings(con, all_cands, vec_map)
        ev_a = _build(con, [cand_a], [], {cand_a["id"]: vec})
        ev_b = _build(con, [cand_b], [], {cand_b["id"]: vec})
        ev_c = _build(con, [cand_c], [], {cand_c["id"]: vec})
        ev_keep = _build(con, [cand_keep], [], {cand_keep["id"]: vec})
        read_id = int(
            con.execute(
                "INSERT INTO reads(title,author,rating,read_at,source) "
                "VALUES('Purge Read Alpha','A. Writer',5,'2025-06-01','goodreads_csv')"
            ).lastrowid
        )
        other_read_id = int(
            con.execute(
                "INSERT INTO reads(title,author,rating,read_at,source) "
                "VALUES('Purge Read Keep','D. Writer',4,'2025-06-01','goodreads_csv')"
            ).lastrowid
        )

    run_a = create_recommendation_run([cand_a], decision_evidence=ev_a)
    run_b = create_recommendation_run([cand_b], decision_evidence=ev_b)
    run_c = create_recommendation_run([cand_c], decision_evidence=ev_c)
    run_keep = create_recommendation_run([cand_keep], decision_evidence=ev_keep)

    two_part_key = f"read:{read_id:08d}"
    three_part_key = f"read:{read_id:08d}:{cand_b['id']}"
    manual_key = f"manual-read:{cand_c['id']}:{read_id:08d}"
    keep_key = f"read:{other_read_id:08d}"

    with transaction() as con:
        for key, cand, run, rid in (
            (two_part_key, cand_a, run_a, read_id),
            (three_part_key, cand_b, run_b, read_id),
            (manual_key, cand_c, run_c, read_id),
            (keep_key, cand_keep, run_keep, other_read_id),
        ):
            record_event_in_connection(
                con,
                event_key=key,
                candidate_id=cand["id"],
                event_type="read",
                run_id=run,
                value=5.0,
                source="read_import",
                label=1.0,
                label_kind="rating_positive",
                confidence=1.0,
                read_id=rid,
            )

        removed = purge_recommendation_evidence(con, read_ids=[read_id])
        assert removed["runs_removed"] == 3
        for purged_run in (run_a, run_b, run_c):
            assert (
                con.execute(
                    "SELECT 1 FROM recommendation_runs WHERE id=?", (purged_run,)
                ).fetchone()
                is None
            )
        assert (
            con.execute(
                "SELECT 1 FROM recommendation_runs WHERE id=?", (run_keep,)
            ).fetchone()
            is not None
        )
        for table in (
            "recommendation_events",
            "recommendation_evidence_events",
            "recommendation_evidence_outcomes",
        ):
            assert (
                con.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE event_key IN (?,?,?)",
                    (two_part_key, three_part_key, manual_key),
                ).fetchone()[0]
                == 0
            )
            assert (
                con.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE event_key=?",
                    (keep_key,),
                ).fetchone()[0]
                == 1
            )


def test_purge_orphan_sweep_is_scoped_to_purged_runs(database):
    vec = np.asarray([0.3, -0.4, 0.8], dtype=np.float32)
    with transaction() as con:
        first = _candidate(con, "Scoped Purge First", "E. Writer")
        second = _candidate(con, "Scoped Purge Second", "F. Writer")
        unrelated = _candidate(con, "Scoped Purge Unrelated", "G. Writer")
        _set_embeddings(
            con,
            [first, second, unrelated],
            {first["id"]: vec, second["id"]: vec, unrelated["id"]: vec},
        )
        ev_first = _build(con, [first, second], [], {first["id"]: vec, second["id"]: vec})
        ev_second = _build(con, [second], [], {second["id"]: vec})

    run_first = create_recommendation_run([first, second], decision_evidence=ev_first)
    run_second = create_recommendation_run([second], decision_evidence=ev_second)

    key_purged = "scoped-read:00000001"
    key_shared = "scoped-read:00000002"
    key_unrelated_orphan = "unrelated-orphan:00000099"

    with transaction() as con:
        # key_purged belongs only to run_first, then gets an unlinked retraction revision.
        record_event_in_connection(
            con,
            event_key=key_purged,
            candidate_id=first["id"],
            event_type="read",
            run_id=run_first,
            value=5.0,
            source="read_import",
            label=1.0,
            label_kind="rating_positive",
            confidence=1.0,
        )
        record_event_in_connection(
            con,
            event_key=key_purged,
            candidate_id=first["id"],
            event_type="read",
            run_id=None,
            value=5.0,
            source="read_import",
            clear_read_outcome=True,
        )

        # key_shared spans run_first and run_second, then gets an unlinked retraction revision.
        record_event_in_connection(
            con,
            event_key=key_shared,
            candidate_id=second["id"],
            event_type="read",
            run_id=run_first,
            value=4.0,
            source="read_import",
            label=1.0,
            label_kind="rating_positive",
            confidence=1.0,
        )
        record_event_in_connection(
            con,
            event_key=key_shared,
            candidate_id=second["id"],
            event_type="read",
            run_id=run_second,
            value=5.0,
            source="read_import",
            label=1.0,
            label_kind="rating_positive",
            confidence=1.0,
        )
        record_event_in_connection(
            con,
            event_key=key_shared,
            candidate_id=second["id"],
            event_type="read",
            run_id=None,
            value=5.0,
            source="read_import",
            clear_read_outcome=True,
        )

        # Unrelated run-less evidence event with no run_id IS NOT NULL sibling anywhere.
        con.execute(
            "INSERT INTO recommendation_evidence_events("
            "event_key,revision,run_id,candidate_id,event_type,value,source,occurred_at) "
            "VALUES(?,1,NULL,?,'restore',NULL,'ui_undo','2025-06-02T00:00:00+00:00')",
            (key_unrelated_orphan, unrelated["id"]),
        )

        removed = purge_recommendation_evidence(con, run_ids=[run_first])
        assert removed["runs_removed"] == 1

        # key_purged's unlinked revision is cleaned up with run_first.
        assert (
            con.execute(
                "SELECT COUNT(*) FROM recommendation_evidence_events WHERE event_key=?",
                (key_purged,),
            ).fetchone()[0]
            == 0
        )
        # key_shared still has run_second, so both its run_second revision and unlinked retraction remain.
        assert (
            con.execute(
                "SELECT COUNT(*) FROM recommendation_evidence_events WHERE event_key=?",
                (key_shared,),
            ).fetchone()[0]
            == 2
        )
        assert load_decision_evidence(con, run_second)["outcomes"] == []
        # Unrelated run-less evidence row is untouched because the sweep is scoped to run_first's keys.
        assert (
            con.execute(
                "SELECT COUNT(*) FROM recommendation_evidence_events WHERE event_key=?",
                (key_unrelated_orphan,),
            ).fetchone()[0]
            == 1
        )

        # Purging run_second now cleans up key_shared's unlinked revision while still preserving key_unrelated_orphan.
        removed_second = purge_recommendation_evidence(con, run_ids=[run_second])
        assert removed_second["runs_removed"] == 1
        assert (
            con.execute(
                "SELECT COUNT(*) FROM recommendation_evidence_events WHERE event_key=?",
                (key_shared,),
            ).fetchone()[0]
            == 0
        )
        assert (
            con.execute(
                "SELECT COUNT(*) FROM recommendation_evidence_events WHERE event_key=?",
                (key_unrelated_orphan,),
            ).fetchone()[0]
            == 1
        )


def test_purge_read_ids_matches_all_four_key_patterns_batching_and_preserve_runs(
    database,
):
    vec = np.asarray([0.4, 0.3, -0.5], dtype=np.float32)
    with transaction() as con:
        cand_two = _candidate(con, "Four Key Two-Part", "A. Author")
        cand_three = _candidate(con, "Four Key Three-Part", "B. Author")
        cand_manual = _candidate(con, "Four Key Manual", "C. Author")
        cand_attr = _candidate(con, "Four Key Attribution", "D. Author")
        cand_keep = _candidate(con, "Four Key Keep Read", "E. Author")
        cand_fb = _candidate(con, "Four Key Keep Feedback", "F. Author")
        cand_full_two = _candidate(con, "Full Purge Two-Part", "G. Author")
        cand_full_three = _candidate(con, "Full Purge Three-Part", "H. Author")
        cand_full_manual = _candidate(con, "Full Purge Manual", "I. Author")
        cand_full_attr = _candidate(con, "Full Purge Attribution", "J. Author")

        shared_cands = [cand_two, cand_three, cand_manual, cand_attr, cand_keep, cand_fb]
        full_cands = [cand_full_two, cand_full_three, cand_full_manual, cand_full_attr]
        all_cands = [*shared_cands, *full_cands]
        vec_map = {item["id"]: vec for item in all_cands}
        _set_embeddings(con, all_cands, vec_map)

        ev_shared = _build(con, shared_cands, [], {item["id"]: vec for item in shared_cands})
        ev_full_two = _build(con, [cand_full_two], [], {cand_full_two["id"]: vec})
        ev_full_three = _build(con, [cand_full_three], [], {cand_full_three["id"]: vec})
        ev_full_manual = _build(con, [cand_full_manual], [], {cand_full_manual["id"]: vec})
        ev_full_attr = _build(con, [cand_full_attr], [], {cand_full_attr["id"]: vec})

        target_read_id = int(
            con.execute(
                "INSERT INTO reads(title,author,rating,read_at,source) "
                "VALUES('Target Purge Book','A. Author',5,'2025-06-01','demo')"
            ).lastrowid
        )
        full_read_id = int(
            con.execute(
                "INSERT INTO reads(title,author,rating,read_at,source) "
                "VALUES('Full Purge Book','G. Author',5,'2025-06-01','goodreads_csv')"
            ).lastrowid
        )
        keep_read_id = int(
            con.execute(
                "INSERT INTO reads(title,author,rating,read_at,source) "
                "VALUES('Keep Real Book','E. Author',4,'2025-06-01','goodreads_csv')"
            ).lastrowid
        )
        padding_read_ids = [
            int(
                con.execute(
                    "INSERT INTO reads(title,author,rating,read_at,source) "
                    "VALUES(?,?,4,'2025-06-01','demo')",
                    (f"Batch Padding {idx}", "P. Author"),
                ).lastrowid
            )
            for idx in range(120)
        ]

    run_shared = create_recommendation_run(shared_cands, decision_evidence=ev_shared)
    run_full_two = create_recommendation_run([cand_full_two], decision_evidence=ev_full_two)
    run_full_three = create_recommendation_run([cand_full_three], decision_evidence=ev_full_three)
    run_full_manual = create_recommendation_run([cand_full_manual], decision_evidence=ev_full_manual)
    run_full_attr = create_recommendation_run([cand_full_attr], decision_evidence=ev_full_attr)

    shared_keys = (
        f"read:{target_read_id:08d}",
        f"read:{target_read_id:08d}:{cand_three['id']}",
        f"manual-read:{cand_manual['id']}:{target_read_id:08d}",
        f"attribution:read:{target_read_id:08d}:{cand_attr['id']}",
    )
    keep_read_key = f"read:{keep_read_id:08d}"
    keep_feedback_key = f"feedback:keep-{cand_fb['id']}"

    full_keys = (
        f"read:{full_read_id:08d}",
        f"read:{full_read_id:08d}:{cand_full_three['id']}",
        f"manual-read:{cand_full_manual['id']}:{full_read_id:08d}",
        f"attribution:read:{full_read_id:08d}:{cand_full_attr['id']}",
    )

    with transaction() as con:
        for key, cand in zip(
            shared_keys,
            (cand_two, cand_three, cand_manual, cand_attr),
            strict=True,
        ):
            record_event_in_connection(
                con,
                event_key=key,
                candidate_id=cand["id"],
                event_type="read",
                run_id=run_shared,
                value=5.0,
                source="read_import",
                label=1.0,
                label_kind="rating_positive",
                confidence=1.0,
                read_id=target_read_id,
            )
        # Also append an unlinked retraction revision on one of the target read keys.
        record_event_in_connection(
            con,
            event_key=shared_keys[0],
            candidate_id=cand_two["id"],
            event_type="read",
            run_id=None,
            value=5.0,
            source="read_import",
            clear_read_outcome=True,
        )
        record_event_in_connection(
            con,
            event_key=keep_read_key,
            candidate_id=cand_keep["id"],
            event_type="read",
            run_id=run_shared,
            value=4.0,
            source="read_import",
            label=1.0,
            label_kind="rating_positive",
            confidence=1.0,
            read_id=keep_read_id,
        )
        record_event_in_connection(
            con,
            event_key=keep_feedback_key,
            candidate_id=cand_fb["id"],
            event_type="save",
            run_id=run_shared,
            value=1.0,
            source="ui_feedback",
            label=0.8,
            label_kind="explicit_feedback",
            confidence=0.9,
        )

        for key, cand, run in zip(
            full_keys,
            (cand_full_two, cand_full_three, cand_full_manual, cand_full_attr),
            (run_full_two, run_full_three, run_full_manual, run_full_attr),
            strict=True,
        ):
            record_event_in_connection(
                con,
                event_key=key,
                candidate_id=cand["id"],
                event_type="read",
                run_id=run,
                value=5.0,
                source="read_import",
                label=1.0,
                label_kind="rating_positive",
                confidence=1.0,
                read_id=full_read_id,
            )
        record_event_in_connection(
            con,
            event_key=full_keys[3],
            candidate_id=cand_full_attr["id"],
            event_type="read",
            run_id=None,
            value=5.0,
            source="read_import",
            clear_read_outcome=True,
        )

        # 1. preserve_runs=True across >100 batched read IDs removes all 4 read key forms
        # (including unlinked revisions) and replay snapshot while keeping run_shared + kept rows.
        preserved_result = purge_recommendation_evidence(
            con,
            read_ids=[target_read_id, *padding_read_ids],
            preserve_runs=True,
        )
        assert preserved_result["runs_removed"] == 1
        assert (
            con.execute(
                "SELECT 1 FROM recommendation_runs WHERE id=?", (run_shared,)
            ).fetchone()
            is not None
        )
        assert (
            con.execute(
                "SELECT 1 FROM recommendation_run_evidence WHERE run_id=?",
                (run_shared,),
            ).fetchone()
            is None
        )
        assert (
            con.execute(
                "SELECT COUNT(*) FROM recommendation_impressions WHERE run_id=?",
                (run_shared,),
            ).fetchone()[0]
            == len(shared_cands)
        )
        assert (
            con.execute(
                "SELECT COUNT(*) FROM recommendation_evidence_exposures WHERE run_id=?",
                (run_shared,),
            ).fetchone()[0]
            == len(shared_cands)
        )
        for key in shared_keys:
            for table in (
                "recommendation_events",
                "recommendation_evidence_events",
                "recommendation_evidence_outcomes",
            ):
                assert (
                    con.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE event_key=?",
                        (key,),
                    ).fetchone()[0]
                    == 0
                )
        assert (
            con.execute(
                "SELECT COUNT(*) FROM recommendation_outcomes WHERE read_id=?",
                (target_read_id,),
            ).fetchone()[0]
            == 0
        )
        for kept_key in (keep_read_key, keep_feedback_key):
            for table in (
                "recommendation_events",
                "recommendation_evidence_events",
                "recommendation_evidence_outcomes",
            ):
                assert (
                    con.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE event_key=?",
                        (kept_key,),
                    ).fetchone()[0]
                    == 1
                )

        # 2. preserve_runs=False removes all 4 runs, all 4 key forms, and unlinked revisions.
        full_result = purge_recommendation_evidence(con, read_ids=[full_read_id])
        assert full_result["runs_removed"] == 4
        for purged_run in (run_full_two, run_full_three, run_full_manual, run_full_attr):
            assert (
                con.execute(
                    "SELECT 1 FROM recommendation_runs WHERE id=?", (purged_run,)
                ).fetchone()
                is None
            )
        for key in full_keys:
            for table in (
                "recommendation_events",
                "recommendation_evidence_events",
                "recommendation_evidence_outcomes",
            ):
                assert (
                    con.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE event_key=?",
                        (key,),
                    ).fetchone()[0]
                    == 0
                )

