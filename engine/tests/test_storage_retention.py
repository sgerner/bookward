from __future__ import annotations

import asyncio
import inspect
import logging
import re
import shutil
import sqlite3
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from auth_helpers import authenticated_headers
import afterword_engine.backups as backups
import afterword_engine.decision_evidence as decision_evidence_module
import afterword_engine.ingestion as ingestion_module
import afterword_engine.main as main_module
from afterword_engine.config import Settings, settings
from afterword_engine.database import MIGRATIONS, connect, initialize, transaction
from afterword_engine.decision_evidence import (
    build_decision_evidence,
    is_evidence_rotation_enabled,
    is_storage_retention_enabled,
    load_decision_evidence,
    rotate_recommendation_evidence_if_needed,
)
from afterword_engine.embeddings import (
    content_hash,
    resolve_embedding_target,
    vector_blob,
)
from afterword_engine.evaluation import build_report, load_rows
from afterword_engine.impression_fatigue import load_unacted_visible_impression_counts
from afterword_engine.learning import (
    create_recommendation_run,
    record_events,
)
from afterword_engine.main import app
from afterword_engine.maintenance import (
    ASSOCIATION_CACHE_EXPIRED_GRACE_DAYS,
    ASSOCIATION_REQUESTS_RETENTION_DAYS,
    ASSOCIATION_RUNS_RETENTION_DAYS,
    DELIVERIES_KEEP_PER_CHANNEL,
    DELIVERIES_RETENTION_DAYS,
    TELEMETRY_RETENTION_DAYS,
    TERMINAL_JOBS_RETENTION_DAYS,
    run_storage_maintenance,
)
from afterword_engine.scoring import document, score_all
from afterword_engine.scoring_evidence import load_scoring_batch


class _DeterministicEmbedder:
    name = "local"
    model = settings.embedding_model

    async def embed(self, texts: list[str]) -> np.ndarray:
        vectors = []
        for index, text in enumerate(texts, start=1):
            seed = sum(ord(ch) for ch in text) % 97 + index
            vec = np.asarray(
                [float(seed), float((seed % 13) + 1), float((seed % 7) + 2), 1.0],
                dtype=np.float32,
            )
            vec /= np.linalg.norm(vec)
            vectors.append(vec)
        return np.vstack(vectors) if vectors else np.zeros((0, 4), dtype=np.float32)


@pytest.fixture()
def database(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "retention.db"
    monkeypatch.setattr(settings, "db", str(db_path))
    monkeypatch.setattr(settings, "backup_dir", str(tmp_path / "afterword-backups"))
    monkeypatch.setattr(settings, "backup_interval_hours", 24)
    monkeypatch.setattr(settings, "backup_retention_count", 7)
    initialize(seed_demo=False)
    return tmp_path


def _seed_source_and_candidates(con) -> tuple[int, list[int]]:
    cur = con.execute(
        "INSERT INTO sources(name, url, kind, enabled, weight) VALUES(?, ?, 'rss', 1, 1.0)",
        ("Retention Feed", "https://example.test/feed.xml"),
    )
    source_id = int(cur.lastrowid)
    statuses = ["recommended", "saved", "rejected", "maybe_later"]
    candidate_ids: list[int] = []
    for idx, status in enumerate(statuses, start=1):
        c_cur = con.execute(
            """
            INSERT INTO candidates(
                source_id, normalized_key, title, author, description, genres,
                score, explanation, status
            ) VALUES (?, ?, ?, ?, ?, '["Fiction"]', ?, '["Strong match"]', ?)
            """,
            (
                source_id,
                f"book-{idx}::author-{idx}",
                f"Retention Book {idx}",
                f"Author {idx}",
                f"Detailed synopsis for book {idx}. " * 8,
                90.0 - idx,
                status,
            ),
        )
        cid = int(c_cur.lastrowid)
        con.execute(
            """
            INSERT INTO candidate_quality(
                candidate_id, quality_status, quality_score, audit_version
            ) VALUES (?, 'accepted', 0.92, 'test-v1')
            ON CONFLICT(candidate_id) DO UPDATE SET
                quality_status='accepted',
                quality_score=0.92,
                audit_version='test-v1'
            """,
            (cid,),
        )
        candidate_ids.append(cid)
    return source_id, candidate_ids


def _build_complete_run(rec_id: int, idx: int) -> str:
    with connect() as con:
        cand = dict(
            con.execute(
                """
                SELECT c.*, s.name AS source_name, s.weight AS source_weight,
                       q.quality_status, q.quality_score,
                       q.work_id AS quality_work_id,
                       q.provider AS quality_provider,
                       q.isbn13 AS quality_isbn13, q.isbn10 AS quality_isbn10,
                       0.92 AS catalog_confidence
                FROM candidates c
                JOIN sources s ON s.id = c.source_id
                JOIN candidate_quality q ON q.candidate_id = c.id
                WHERE c.id = ?
                """,
                (rec_id,),
            ).fetchone()
        )
        cand["description"] = f"Run {idx} unique snapshot padding " + ("x" * 1200)
        vec = np.asarray([0.2 * idx, 0.3, 0.4, 0.5], dtype=np.float32)
        ev = build_decision_evidence(
            con,
            base_pool=[cand],
            personalized_pool=[cand],
            final_pool=[cand],
            interaction_events=[],
            cached_vectors={int(cand["id"]): vec},
            ranking_metadata={"mode": "test", "applied": False},
            discovery_slate_metadata={"applied": False},
            request_context={"status": "recommended", "limit": 20, "offset": 0},
        )
        ev["captured_at"] = (
            datetime(2026, 10, 1, tzinfo=timezone.utc) + timedelta(minutes=idx)
        ).isoformat()
    return create_recommendation_run(
        [cand], decision_evidence=ev, status="recommended", limit=20
    )


def test_non_ranking_recommendation_statuses_skip_run_creation_and_preserve_attribution(
    database, monkeypatch
):
    assert (
        '"base_ranker_fallback" if can_personalize else "non_ranking_request"'
        in inspect.getsource(main_module.tracked_recommendations)
    )
    monkeypatch.setattr(main_module, "enqueue_job", lambda *_a, **_kw: "job-noop")
    with transaction() as con:
        _source_id, candidate_ids = _seed_source_and_candidates(con)
        rec_id = candidate_ids[0]
        con.execute(
            "INSERT INTO feedback(candidate_id, action) VALUES(?, 'reject')",
            (candidate_ids[2],),
        )

    with TestClient(app, headers=authenticated_headers()) as client:
        for non_ranking_status in ("decisions", "rejected", "maybe_later", "saved"):
            resp = client.get(f"/api/recommendations?status={non_ranking_status}")
            assert resp.status_code == 200
            body = resp.json()
            assert len(body) >= 1
            assert all(item["recommendation_run_id"] == "" for item in body)
            assert "X-Bookward-Recommendation-Run" not in resp.headers

        with connect() as con:
            assert con.execute("SELECT COUNT(*) FROM recommendation_runs").fetchone()[0] == 0
            assert con.execute("SELECT COUNT(*) FROM recommendation_impressions").fetchone()[0] == 0
            assert con.execute("SELECT COUNT(*) FROM recommendation_run_evidence").fetchone()[0] == 0

        ranked_resp = client.get("/api/recommendations?status=recommended")
        assert ranked_resp.status_code == 200
        discover_run_id = ranked_resp.headers.get("X-Bookward-Recommendation-Run")
        assert discover_run_id
        assert all(item["recommendation_run_id"] == discover_run_id for item in ranked_resp.json())

        # Save from Discover with run_id, then browse non-ranking Shortlist/Decisions,
        # restore without run_id, and mark read without run_id: attribution must stay
        # anchored to the ranked Discover run without synthetic non-ranking impressions.
        save_resp = client.post(
            f"/api/recommendations/{rec_id}/feedback",
            json={"action": "save", "run_id": discover_run_id},
        )
        assert save_resp.status_code == 200

        for non_ranking_status in ("saved", "decisions"):
            browse = client.get(f"/api/recommendations?status={non_ranking_status}")
            assert browse.status_code == 200
            assert all(item["recommendation_run_id"] == "" for item in browse.json())

        restore_resp = client.post(
            f"/api/recommendations/{rec_id}/feedback",
            json={"action": "restore"},
        )
        assert restore_resp.status_code == 200

        read_resp = client.post(
            f"/api/recommendations/{rec_id}/read",
            json={"rating": 5, "read_at": "2026-11-22"},
        )
        assert read_resp.status_code == 200

        with connect() as con:
            assert con.execute("SELECT COUNT(*) FROM recommendation_runs").fetchone()[0] == 1
            outcomes = con.execute(
                """
                SELECT i.run_id, o.label, o.label_kind
                FROM recommendation_outcomes o
                JOIN recommendation_impressions i ON i.id = o.impression_id
                WHERE i.candidate_id = ?
                ORDER BY o.id ASC
                """,
                (rec_id,),
            ).fetchall()
            assert [(r["run_id"], float(r["label"]), r["label_kind"]) for r in outcomes] == [
                (discover_run_id, 1.0, "explicit_feedback"),
                (discover_run_id, 1.0, "rating_positive"),
            ]


def test_impression_fatigue_query_uses_partial_visible_index(database):
    now = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
    with transaction() as con:
        _source_id, candidate_ids = _seed_source_and_candidates(con)
        rec_id = candidate_ids[0]
        con.execute(
            "INSERT INTO recommendation_runs(id, policy, policy_version, candidate_count) "
            "VALUES('fatigue-run-1', 'test', 'v1', 1)"
        )
        con.execute(
            """
            INSERT INTO recommendation_impressions(
                run_id, candidate_id, rank, score, propensity, presented_at, visible_at
            ) VALUES ('fatigue-run-1', ?, 1, 88.0, 1.0, ?, ?)
            """,
            (rec_id, (now - timedelta(days=2)).isoformat(), (now - timedelta(days=2)).isoformat()),
        )

    with connect() as con:
        plan_rows = con.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT i.candidate_id, COUNT(DISTINCT substr(i.visible_at, 1, 10)) AS visible_days
            FROM recommendation_impressions i
            JOIN candidates c ON c.id = i.candidate_id
            WHERE c.status = 'recommended'
              AND i.visible_at IS NOT NULL
              AND i.visible_at >= ?
            GROUP BY i.candidate_id
            """,
            ((now - timedelta(days=21)).isoformat(),),
        ).fetchall()
        plan_detail = " ".join(str(row["detail"]) for row in plan_rows)
        assert "idx_recommendation_impressions_visible" in plan_detail
        counts = load_unacted_visible_impression_counts(con, window_days=21, now=now)
        assert counts == {rec_id: 1}


def test_evidence_watermark_rotation_evicts_unattributed_and_protects_outcomes(
    database, monkeypatch
):
    monkeypatch.setattr(settings, "evidence_rotation_enabled", True)
    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title, author, rating, read_at, source) VALUES(?, ?, 5, '2026-01-10', 'test')",
            ("Anchor Read", "Anchor Author"),
        )
        source_id, candidate_ids = _seed_source_and_candidates(con)
        rec_id = candidate_ids[0]
        saved_id = candidate_ids[1]
        churn_cur = con.execute(
            """
            INSERT INTO candidates(
                source_id, normalized_key, title, author, description, genres,
                score, explanation, status
            ) VALUES (?, 'churn-book::author', 'Churn Book', 'Churn Author', ?, '["Fiction"]', 91.0, '["Great"]', 'recommended')
            """,
            (source_id, "Transient candidate description. " * 8),
        )
        churn_cid = int(churn_cur.lastrowid)
        con.execute(
            "INSERT INTO candidate_quality(candidate_id, quality_status, quality_score, audit_version) "
            "VALUES (?, 'accepted', 0.95, 'test-v1') "
            "ON CONFLICT(candidate_id) DO UPDATE SET quality_status='accepted', quality_score=0.95, audit_version='test-v1'",
            (churn_cid,),
        )

    embedder = _DeterministicEmbedder()
    assert asyncio.run(score_all(embedder=embedder)) >= 1
    with connect() as con:
        first_batch_id = str(
            con.execute("SELECT score_batch_id FROM candidates WHERE id=?", (rec_id,)).fetchone()[0]
        )
        assert first_batch_id
    assert asyncio.run(score_all(embedder=embedder)) >= 1
    with connect() as con:
        active_batch_id = str(
            con.execute("SELECT score_batch_id FROM candidates WHERE id=?", (rec_id,)).fetchone()[0]
        )
        assert active_batch_id and active_batch_id != first_batch_id

    with transaction() as con:
        con.execute("INSERT INTO feedback(candidate_id, action) VALUES(?, 'save')", (saved_id,))

    # Oldest run 1: attributed outcome on a candidate later deleted by source refresh
    # (leaving ONLY recommendation_evidence_outcomes).
    churned_attributed_run = _build_complete_run(churn_cid, 1)
    record_events(
        [
            {
                "event_key": "test-churn-save-1",
                "event_type": "save",
                "candidate_id": churn_cid,
                "run_id": churned_attributed_run,
                "label": 1.0,
                "label_kind": "save",
                "confidence": 0.8,
                "source": "test",
            }
        ]
    )
    with transaction() as con:
        con.execute("DELETE FROM candidates WHERE id = ?", (churn_cid,))
        assert (
            con.execute(
                "SELECT COUNT(*) FROM recommendation_outcomes o "
                "JOIN recommendation_impressions i ON i.id = o.impression_id "
                "WHERE i.run_id = ?",
                (churned_attributed_run,),
            ).fetchone()[0]
            == 0
        )
        assert (
            con.execute(
                "SELECT COUNT(*) FROM recommendation_evidence_outcomes WHERE run_id = ?",
                (churned_attributed_run,),
            ).fetchone()[0]
            == 1
        )

    # Oldest run 2: live attributed outcome in both recommendation_outcomes and recommendation_evidence_outcomes.
    live_attributed_run = _build_complete_run(rec_id, 2)
    record_events(
        [
            {
                "event_key": "test-live-save-2",
                "event_type": "save",
                "candidate_id": rec_id,
                "run_id": live_attributed_run,
                "label": 1.0,
                "label_kind": "save",
                "confidence": 0.8,
                "source": "test",
            }
        ]
    )

    # Runs 3..8: unattributed complete runs eligible for watermark eviction.
    unattributed_runs = [_build_complete_run(rec_id, idx) for idx in range(3, 9)]
    with connect() as con:
        used_before = int(
            con.execute("SELECT used_bytes FROM recommendation_evidence_budget WHERE id=1").fetchone()[0]
        )

    tight_quota = int(used_before / 0.85)
    monkeypatch.setattr(decision_evidence_module, "MAX_STORED_EVIDENCE_BYTES", tight_quota)

    newest_run = _build_complete_run(rec_id, 9)

    with connect() as con:
        used_after = int(
            con.execute("SELECT used_bytes FROM recommendation_evidence_budget WHERE id=1").fetchone()[0]
        )
        assert used_after <= tight_quota

        # Unreferenced batch 1 and oldest unattributed run(s) rotated out.
        assert load_scoring_batch(con, first_batch_id) is None
        assert load_decision_evidence(con, unattributed_runs[0]) is None

        # BOTH attributed runs (including the catalog-churned one!) and their
        # recommendation_evidence_outcomes rows survived watermark rotation intact.
        for protected_run in (churned_attributed_run, live_attributed_run, newest_run):
            verified = load_decision_evidence(con, protected_run)
            assert verified is not None
            assert verified["capture_status"] == "complete"
        assert (
            con.execute(
                "SELECT COUNT(*) FROM recommendation_evidence_outcomes WHERE run_id IN (?, ?)",
                (churned_attributed_run, live_attributed_run),
            ).fetchone()[0]
            == 2
        )
        assert load_scoring_batch(con, active_batch_id) is not None

        # When quota is lowered below the non-evictable attributed footprint,
        # new captures fail open to 'incomplete' without evicting attributed runs.
        attributed_floor = int(
            con.execute("SELECT used_bytes FROM recommendation_evidence_budget WHERE id=1").fetchone()[0]
        )
    monkeypatch.setattr(decision_evidence_module, "MAX_STORED_EVIDENCE_BYTES", max(1024, attributed_floor // 4))
    degraded_run = _build_complete_run(rec_id, 10)
    with connect() as con:
        degraded_row = con.execute(
            "SELECT capture_status, reason FROM recommendation_run_evidence WHERE run_id = ?",
            (degraded_run,),
        ).fetchone()
        assert degraded_row is not None
        assert degraded_row["capture_status"] == "incomplete"
        assert degraded_row["reason"] == "storage_quota_exceeded"
        assert load_decision_evidence(con, churned_attributed_run)["capture_status"] == "complete"
        assert load_decision_evidence(con, live_attributed_run)["capture_status"] == "complete"


def test_telemetry_pruning_removes_aged_unattributed_rows_and_preserves_outcomes(
    database, monkeypatch
):
    monkeypatch.setattr(settings, "retention_enabled", True)
    now = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
    old_ts = (now - timedelta(days=TELEMETRY_RETENTION_DAYS + 30)).isoformat()
    recent_ts = (now - timedelta(days=TELEMETRY_RETENTION_DAYS - 15)).isoformat()

    with transaction() as con:
        source_id, candidate_ids = _seed_source_and_candidates(con)
        c1, c2 = candidate_ids[0], candidate_ids[1]
        churn_cur = con.execute(
            "INSERT INTO candidates(source_id, normalized_key, title, author, status) "
            "VALUES(?, 'churn-telemetry::a', 'Churned Telemetry', 'Author', 'recommended')",
            (source_id,),
        )
        churn_cid = int(churn_cur.lastrowid)
        con.execute(
            "INSERT INTO candidate_quality(candidate_id, quality_status, quality_score, audit_version) "
            "VALUES(?, 'accepted', 0.9, 'test-v1') "
            "ON CONFLICT(candidate_id) DO UPDATE SET quality_status='accepted', quality_score=0.9, audit_version='test-v1'",
            (churn_cid,),
        )

    # Aged run whose candidate is later deleted, leaving only recommendation_evidence_outcomes.
    churned_evidence_run = _build_complete_run(churn_cid, 1)
    record_events(
        [
            {
                "event_key": "test-telemetry-churn-save-1",
                "event_type": "save",
                "candidate_id": churn_cid,
                "run_id": churned_evidence_run,
                "label": 1.0,
                "label_kind": "save",
                "confidence": 0.8,
                "source": "test",
            }
        ]
    )
    with transaction() as con:
        con.execute(
            "UPDATE recommendation_runs SET created_at = ? WHERE id = ?",
            (old_ts, churned_evidence_run),
        )
        con.execute("DELETE FROM candidates WHERE id = ?", (churn_cid,))

        # 1. Old unattributed run (>90d) -> pruned; event survives with run_id=NULL
        con.execute(
            "INSERT INTO recommendation_runs(id, policy, policy_version, candidate_count, created_at) "
            "VALUES('old-unattributed', 'p', 'v1', 1, ?)",
            (old_ts,),
        )
        con.execute(
            "INSERT INTO recommendation_impressions(run_id, candidate_id, rank, score, propensity, presented_at) "
            "VALUES('old-unattributed', ?, 1, 80.0, 1.0, ?)",
            (c1, old_ts),
        )
        con.execute(
            "INSERT INTO recommendation_events(event_key, run_id, candidate_id, event_type, source, occurred_at) "
            "VALUES('evt-old-save', 'old-unattributed', ?, 'save', 'web', ?)",
            (c1, old_ts),
        )

        # 2. Old attributed run (>90d) -> MUST survive with its impression and outcome
        con.execute(
            "INSERT INTO recommendation_runs(id, policy, policy_version, candidate_count, created_at) "
            "VALUES('old-attributed', 'p', 'v1', 1, ?)",
            (old_ts,),
        )
        imp_cur = con.execute(
            "INSERT INTO recommendation_impressions(run_id, candidate_id, rank, score, propensity, presented_at, visible_at) "
            "VALUES('old-attributed', ?, 1, 89.0, 1.0, ?, ?)",
            (c2, old_ts, old_ts),
        )
        imp_id = int(imp_cur.lastrowid)
        evt_cur = con.execute(
            "INSERT INTO recommendation_events(event_key, run_id, candidate_id, event_type, value, source, occurred_at) "
            "VALUES('evt-old-outcome', 'old-attributed', ?, 'read', 5.0, 'web', ?)",
            (c2, old_ts),
        )
        evt_id = int(evt_cur.lastrowid)
        con.execute(
            "INSERT INTO recommendation_outcomes(impression_id, event_id, label, label_kind, confidence, attributed_at) "
            "VALUES(?, ?, 1.0, 'explicit_rating', 1.0, ?)",
            (imp_id, evt_id, old_ts),
        )

        # 3. Recent unattributed run (<90d) -> MUST survive
        con.execute(
            "INSERT INTO recommendation_runs(id, policy, policy_version, candidate_count, created_at) "
            "VALUES('recent-unattributed', 'p', 'v1', 1, ?)",
            (recent_ts,),
        )
        con.execute(
            "INSERT INTO recommendation_impressions(run_id, candidate_id, rank, score, propensity, presented_at) "
            "VALUES('recent-unattributed', ?, 1, 85.0, 1.0, ?)",
            (c1, recent_ts),
        )
        con.execute("INSERT INTO feedback(candidate_id, action) VALUES(?, 'save')", (c1,))

    stats = run_storage_maintenance(now=now)
    assert stats["telemetry"]["pruned_runs"] == 1

    with connect() as con:
        remaining_runs = {
            str(r["id"]) for r in con.execute("SELECT id FROM recommendation_runs").fetchall()
        }
        assert remaining_runs == {
            "old-attributed",
            "recent-unattributed",
            churned_evidence_run,
        }
        assert load_decision_evidence(con, churned_evidence_run)["capture_status"] == "complete"
        assert (
            con.execute(
                "SELECT COUNT(*) FROM recommendation_evidence_outcomes WHERE run_id = ?",
                (churned_evidence_run,),
            ).fetchone()[0]
            == 1
        )
        assert con.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM recommendation_outcomes").fetchone()[0] == 1

    eval_rows = load_rows(settings.db)
    attributed = [r for r in eval_rows if r["run_id"] == "old-attributed"]
    assert len(attributed) == 1
    assert attributed[0]["label"] == 1.0
    assert attributed[0]["run_created_at"] == old_ts
    report = build_report(eval_rows, bootstrap_iterations=10)
    assert report["sample"]["runs"] == 2


def test_embedding_triggers_scan_source_rotation_virtual_history_and_thrashing_guard(
    database, monkeypatch
):
    monkeypatch.setattr(settings, "retention_enabled", True)
    assert resolve_embedding_target("fastembed", "hashing-768") == (
        "fastembed",
        "BAAI/bge-small-en-v1.5",
    )
    assert resolve_embedding_target("local", "BAAI/bge-small-en-v1.5") == (
        "local",
        "hashing-768",
    )

    vec = vector_blob(np.asarray([0.5, 0.5, 0.5, 0.5], dtype=np.float32))
    with transaction() as con:
        source_id, candidate_ids = _seed_source_and_candidates(con)
        rec_id, saved_id, rejected_id, _later_id = candidate_ids
        read_cur = con.execute(
            "INSERT INTO reads(title, author, rating, read_at, source) VALUES('Read One', 'Writer A', 5, '2026-02-01', 'test')"
        )
        read_id_1 = int(read_cur.lastrowid)
        read_cur_2 = con.execute(
            "INSERT INTO reads(title, author, rating, read_at, source) VALUES('Read Two', 'Writer B', 4, '2026-03-01', 'test')"
        )
        read_id_2 = int(read_cur_2.lastrowid)

        for cid in candidate_ids:
            item = dict(con.execute("SELECT * FROM candidates WHERE id=?", (cid,)).fetchone())
            con.execute(
                "INSERT INTO embeddings(entity_type, entity_id, backend, model, vector, dimensions, content_hash) "
                "VALUES('candidate', ?, ?, ?, ?, 4, ?)",
                (cid, settings.embedding_backend, settings.embedding_model, vec, content_hash(document(item))),
            )
        for rid in (read_id_1, read_id_2):
            item = dict(con.execute("SELECT * FROM reads WHERE id=?", (rid,)).fetchone())
            con.execute(
                "INSERT INTO embeddings(entity_type, entity_id, backend, model, vector, dimensions, content_hash) "
                "VALUES('read', ?, ?, ?, ?, 4, ?)",
                (rid, settings.embedding_backend, settings.embedding_model, vec, content_hash(document(item))),
            )

    async def _fake_fetch(_url):
        return (
            "rss",
            [
                {
                    "title": "Brand New Feed Book",
                    "author": "Fresh Author",
                    "description": "A brand new book.",
                    "genres": ["Fiction"],
                    "source_url": "https://example.test/fresh",
                }
            ],
        )

    async def _passthrough_items(items, *_args, **_kwargs):
        return list(items)

    monkeypatch.setattr(ingestion_module, "_fetch_and_parse_source", _fake_fetch)
    monkeypatch.setattr(ingestion_module, "enrich_penguin_random_house_items", _passthrough_items)
    monkeypatch.setattr(ingestion_module, "enrich_book_metadata", _passthrough_items)
    with connect() as con:
        source_row = dict(con.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone())
    asyncio.run(ingestion_module.scan_source(source_row))

    with transaction() as con:
        con.execute("DELETE FROM reads WHERE id=?", (read_id_2,))

    with connect() as con:
        assert (
            con.execute(
                "SELECT COUNT(*) FROM embeddings WHERE entity_type='candidate' AND entity_id=?",
                (rec_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            con.execute(
                "SELECT COUNT(*) FROM embeddings WHERE entity_type='read' AND entity_id=?",
                (read_id_2,),
            ).fetchone()[0]
            == 0
        )

    monkeypatch.setattr(main_module, "get_embedder", lambda *_a, **_kw: _DeterministicEmbedder())
    with TestClient(app, headers=authenticated_headers()) as client:
        scores_resp = client.get("/api/reading-history/scores")
        assert scores_resp.status_code == 200
        assert len(scores_resp.json()) == 1

    with connect() as con:
        assert (
            con.execute(
                "SELECT COUNT(*) FROM embeddings WHERE entity_type='read_candidate' OR entity_id <= 0"
            ).fetchone()[0]
            == 0
        )

    # Guard 1: If settings switch to a target with ZERO active embeddings yet,
    # stale-model pruning MUST NOT wipe existing embeddings before rebuild finishes.
    with transaction() as con:
        con.execute(
            "INSERT INTO settings(key, value, secret) VALUES('embedding_backend', 'ollama', 0) "
            "ON CONFLICT(key) DO UPDATE SET value='ollama'"
        )
        con.execute(
            "INSERT INTO settings(key, value, secret) VALUES('embedding_model', 'nomic-embed-text', 0) "
            "ON CONFLICT(key) DO UPDATE SET value='nomic-embed-text'"
        )
    pre_rebuild = run_storage_maintenance()
    assert pre_rebuild["embeddings"]["removed_stale_model"] == 0

    # Guard 2: Backend=fastembed with leftover model='hashing-768' resolves to
    # ('fastembed', 'BAAI/bge-small-en-v1.5') and preserves fastembed vectors while
    # pruning old local vectors once fastembed vectors exist.
    with transaction() as con:
        con.execute("UPDATE settings SET value='fastembed' WHERE key='embedding_backend'")
        con.execute("UPDATE settings SET value='hashing-768' WHERE key='embedding_model'")
        con.execute(
            "INSERT INTO embeddings(entity_type, entity_id, backend, model, vector, dimensions, content_hash) "
            "VALUES('candidate', ?, 'fastembed', 'BAAI/bge-small-en-v1.5', ?, 4, 'fe-hash')",
            (saved_id, vec),
        )
        con.execute(
            "INSERT INTO embeddings(entity_type, entity_id, backend, model, vector, dimensions, content_hash) "
            "VALUES('read', ?, 'fastembed', 'BAAI/bge-small-en-v1.5', ?, 4, 'fe-read-hash')",
            (read_id_1, vec),
        )

    maint = run_storage_maintenance()
    assert maint["embeddings"]["removed_stale_model"] >= 1
    with connect() as con:
        remaining = con.execute(
            "SELECT entity_type, entity_id, backend, model FROM embeddings"
        ).fetchall()
        triples = {
            (r["entity_type"], int(r["entity_id"]), r["backend"], r["model"])
            for r in remaining
        }
        assert ("candidate", saved_id, "fastembed", "BAAI/bge-small-en-v1.5") in triples
        assert ("read", read_id_1, "fastembed", "BAAI/bge-small-en-v1.5") in triples
        assert all(r["backend"] == "fastembed" for r in remaining)
        assert all(int(r["entity_id"]) != rejected_id for r in remaining)


def test_operational_maintenance_honors_exact_retention_windows_and_protects_leases(
    database, monkeypatch
):
    monkeypatch.setattr(settings, "retention_enabled", True)
    assert TERMINAL_JOBS_RETENTION_DAYS == 30
    assert ASSOCIATION_RUNS_RETENTION_DAYS == 30
    assert ASSOCIATION_REQUESTS_RETENTION_DAYS == 30
    assert ASSOCIATION_CACHE_EXPIRED_GRACE_DAYS == 7
    assert DELIVERIES_RETENTION_DAYS == 180
    assert DELIVERIES_KEEP_PER_CHANNEL == 2

    now = datetime(2026, 11, 21, 15, 0, tzinfo=timezone.utc)
    today_sql = now.strftime("%Y-%m-%d %H:%M:%S")
    within_30d_sql = (now - timedelta(days=20)).strftime("%Y-%m-%d %H:%M:%S")
    beyond_30d_sql = (now - timedelta(days=45)).strftime("%Y-%m-%d %H:%M:%S")
    within_30d_iso = (now - timedelta(days=14)).isoformat()
    beyond_30d_iso = (now - timedelta(days=45)).isoformat()
    within_180d_iso = (now - timedelta(days=90)).isoformat()
    beyond_180d_oldest = (now - timedelta(days=240)).isoformat()
    beyond_180d_mid = (now - timedelta(days=210)).isoformat()
    beyond_180d_newer = (now - timedelta(days=195)).isoformat()

    with transaction() as con:
        # Jobs: >30d terminal pruned; 20d terminal preserved; queued/running/today rss preserved.
        con.execute(
            "INSERT INTO jobs(id, kind, status, created_at) VALUES('job-queued', 'refresh', 'queued', ?)",
            (beyond_30d_sql,),
        )
        con.execute(
            "INSERT INTO jobs(id, kind, status, created_at, started_at, heartbeat_at, lease_token) "
            "VALUES('job-running', 'refresh', 'running', ?, ?, ?, 'lease-123')",
            (beyond_30d_sql, today_sql, today_sql),
        )
        con.execute(
            "INSERT INTO jobs(id, kind, status, created_at, finished_at) "
            "VALUES('job-rss-today', 'goodreads_rss', 'failed', ?, ?)",
            (today_sql, today_sql),
        )
        con.execute(
            "INSERT INTO jobs(id, kind, status, created_at, finished_at) "
            "VALUES('job-within-30d', 'refresh', 'complete', ?, ?)",
            (within_30d_sql, within_30d_sql),
        )
        con.execute(
            "INSERT INTO jobs(id, kind, status, created_at, finished_at) "
            "VALUES('job-beyond-30d', 'refresh', 'complete', ?, ?)",
            (beyond_30d_sql, beyond_30d_sql),
        )

        # Association runs (30d), requests (30d), and cache (>7d grace past expires_at)
        con.execute(
            "INSERT INTO association_runs(id, provider, status, started_at) "
            "VALUES('assoc-running', 'librarything', 'running', ?)",
            (beyond_30d_iso,),
        )
        con.execute(
            "INSERT INTO association_runs(id, provider, status, started_at, finished_at) "
            "VALUES('assoc-within-30d', 'librarything', 'complete', ?, ?), "
            "      ('assoc-beyond-30d', 'librarything', 'complete', ?, ?)",
            (within_30d_iso, within_30d_iso, beyond_30d_iso, beyond_30d_iso),
        )
        con.execute(
            "INSERT INTO association_requests(provider, request_key, status, requested_at) "
            "VALUES('librarything', 'today-key', 'ok', ?), "
            "      ('librarything', 'within-30d-key', 'ok', ?), "
            "      ('librarything', 'beyond-30d-key', 'ok', ?)",
            (now.isoformat(), within_30d_iso, beyond_30d_iso),
        )
        con.execute(
            "INSERT INTO association_cache(provider, cache_key, payload, fetched_at, expires_at) "
            "VALUES('librarything', 'expired-beyond-grace', '{}', ?, ?), "
            "      ('librarything', 'expired-within-grace', '{}', ?, ?), "
            "      ('librarything', 'valid', '{}', ?, ?)",
            (
                beyond_30d_iso,
                (now - timedelta(days=10)).isoformat(),
                within_30d_iso,
                (now - timedelta(days=2)).isoformat(),
                now.isoformat(),
                (now + timedelta(days=7)).isoformat(),
            ),
        )

        # Notification deliveries: >180d pruned ONLY beyond the 2 most recent terminal rows per channel
        con.execute(
            "INSERT INTO settings(key, value, secret) VALUES('digest_last_period', '2026-W01', 0) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
        con.execute(
            """
            INSERT INTO notification_deliveries(id, period_key, channel, status, candidate_ids, created_at, updated_at, sent_at)
            VALUES
                ('deliv-pending', '2026-W02', 'email', 'pending', '[1]', ?, ?, NULL),
                ('deliv-protected-period', '2026-W01', 'discord', 'sent', '[1]', ?, ?, ?),
                ('deliv-within-180d', '2026-W34', 'discord', 'sent', '[1]', ?, ?, ?),
                ('deliv-email-keep-1', '2026-W18', 'email', 'sent', '[1]', ?, ?, ?),
                ('deliv-email-keep-2', '2026-W16', 'email', 'failed', '[1]', ?, ?, NULL),
                ('deliv-email-prune-oldest', '2026-W12', 'email', 'sent', '[1]', ?, ?, ?)
            """,
            (
                beyond_180d_oldest, beyond_180d_oldest,
                beyond_180d_oldest, beyond_180d_oldest, beyond_180d_oldest,
                within_180d_iso, within_180d_iso, within_180d_iso,
                beyond_180d_newer, beyond_180d_newer, beyond_180d_newer,
                beyond_180d_mid, beyond_180d_mid,
                beyond_180d_oldest, beyond_180d_oldest, beyond_180d_oldest,
            ),
        )

    result = run_storage_maintenance(now=now)
    op = result["operational"]
    assert op["pruned_jobs"] == 1
    assert op["pruned_association_runs"] == 1
    assert op["pruned_association_requests"] == 1
    assert op["pruned_association_cache"] == 1
    assert op["pruned_notification_deliveries"] == 1

    with connect() as con:
        jobs = {r["id"] for r in con.execute("SELECT id FROM jobs").fetchall()}
        assert jobs == {"job-queued", "job-running", "job-rss-today", "job-within-30d"}
        assoc_runs = {r["id"] for r in con.execute("SELECT id FROM association_runs").fetchall()}
        assert assoc_runs == {"assoc-running", "assoc-within-30d"}
        req_keys = {r["request_key"] for r in con.execute("SELECT request_key FROM association_requests").fetchall()}
        assert req_keys == {"today-key", "within-30d-key"}
        cache_keys = {r["cache_key"] for r in con.execute("SELECT cache_key FROM association_cache").fetchall()}
        assert cache_keys == {"expired-within-grace", "valid"}
        deliveries = {r["id"] for r in con.execute("SELECT id FROM notification_deliveries").fetchall()}
        assert deliveries == {
            "deliv-pending",
            "deliv-protected-period",
            "deliv-within-180d",
            "deliv-email-keep-1",
            "deliv-email-keep-2",
        }

    with TestClient(app, headers=authenticated_headers()) as client:
        review = client.get("/api/digest/review?period=2026-W12")
        assert review.status_code == 200
        assert review.json() == {"period": "2026-W12", "ids": [], "found": False}


def test_bounded_multi_transaction_maintenance_resumes_across_passes(
    database, monkeypatch
):
    monkeypatch.setattr(settings, "retention_enabled", True)
    now = datetime(2026, 11, 21, 18, 0, tzinfo=timezone.utc)
    old_iso = (now - timedelta(days=120)).isoformat()
    old_sql = (now - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S")

    with transaction() as con:
        _source_id, candidate_ids = _seed_source_and_candidates(con)
        c1 = candidate_ids[0]
        con.executemany(
            "INSERT INTO recommendation_runs(id, policy, policy_version, candidate_count, created_at) "
            "VALUES(?, 'p', 'v1', 1, ?)",
            [(f"bulk-run-{idx}", old_iso) for idx in range(130)],
        )
        con.executemany(
            "INSERT INTO recommendation_impressions(run_id, candidate_id, rank, score, propensity, presented_at) "
            "VALUES(?, ?, 1, 80.0, 1.0, ?)",
            [(f"bulk-run-{idx}", c1, old_iso) for idx in range(130)],
        )
        con.executemany(
            "INSERT INTO jobs(id, kind, status, created_at, finished_at) "
            "VALUES(?, 'refresh', 'complete', ?, ?)",
            [(f"bulk-job-{idx}", old_sql, old_sql) for idx in range(120)],
        )

    assert backups.maintenance_is_due(now) is True

    # Deterministic clock that hits the 2.0s budget after 3 committed batches
    # (2 batches of impressions + 1 batch of runs with batch_size=80).
    ticks = {"t": 100.0}
    call_counter = {"reads": 0}

    def _clock_advancing_mid_run() -> float:
        call_counter["reads"] += 1
        if call_counter["reads"] >= 5:
            ticks["t"] = 102.5
        return ticks["t"]

    first_pass = run_storage_maintenance(
        now=now,
        batch_size=80,
        max_duration_seconds=2.0,
        clock=_clock_advancing_mid_run,
    )
    assert first_pass["truncated"] is True
    assert first_pass["batches_committed"] >= 2
    assert 0 < first_pass["telemetry"]["pruned_runs"] < 130
    # Because the pass stopped at the wall-clock budget, maintenance remains due!
    assert backups.maintenance_is_due(now) is True

    with connect() as con:
        remaining_after_first = con.execute(
            "SELECT COUNT(*) FROM recommendation_runs"
        ).fetchone()[0]
        assert 0 < remaining_after_first < 130

    # Scheduled maintenance runs outside backups.database_lock and drains the remainder.
    monkeypatch.setattr(
        backups,
        "database_lock",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError("database_lock must not be acquired by run_scheduled_maintenance")
        ),
    )
    second_pass = backups.run_scheduled_maintenance(now=now)
    assert second_pass is not None
    assert second_pass["truncated"] is False
    assert second_pass["batches_committed"] >= 2
    assert backups.maintenance_is_due(now) is False

    with connect() as con:
        assert con.execute("SELECT COUNT(*) FROM recommendation_runs").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_migration_28_has_no_startup_deletes_and_maintenance_batches_orphan_cleanup(
    database, monkeypatch
):
    assert MIGRATIONS[-1][0] == 28
    migration_28_sql = dict(MIGRATIONS)[28]
    sql_without_triggers = re.sub(
        r"CREATE\s+TRIGGER\b.*?END\s*;",
        "",
        migration_28_sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert "DELETE" not in sql_without_triggers.upper()

    with connect() as con:
        applied = {
            int(row[0])
            for row in con.execute("SELECT version FROM schema_migrations").fetchall()
        }
        assert {27, 28} <= applied

    vec = vector_blob(np.asarray([0.25, 0.25, 0.25, 0.25], dtype=np.float32))
    with transaction() as con:
        con.execute(
            "INSERT INTO embeddings(entity_type, entity_id, backend, model, vector, dimensions, content_hash) "
            "VALUES('read_candidate', -7, 'local', 'hashing-768', ?, 4, 'virt-h'), "
            "      ('candidate', 999999, 'local', 'hashing-768', ?, 4, 'orphan-c'), "
            "      ('read', 999999, 'local', 'hashing-768', ?, 4, 'orphan-r')",
            (vec, vec, vec),
        )

    monkeypatch.setattr(settings, "retention_enabled", True)
    maint = run_storage_maintenance(batch_size=2)
    assert maint["embeddings"]["removed_orphan_or_virtual"] == 3
    with connect() as con:
        assert con.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 0


def test_link_table_cleanup_preserves_attributed_runs_without_complete_capture(
    database, monkeypatch
):
    now = datetime(2026, 11, 21, 16, 0, tzinfo=timezone.utc)
    old_ts = (now - timedelta(days=TELEMETRY_RETENTION_DAYS + 20)).isoformat()

    with transaction() as con:
        source_id, candidate_ids = _seed_source_and_candidates(con)
        c1, c2 = candidate_ids[0], candidate_ids[1]
        churn_cur = con.execute(
            "INSERT INTO candidates(source_id, normalized_key, title, author, status) "
            "VALUES(?, 'link-churn::a', 'Link Churn', 'Author', 'recommended')",
            (source_id,),
        )
        churn_cid = int(churn_cur.lastrowid)

        for batch_id in (
            "batch-attr-live",
            "batch-attr-churn",
            "batch-attr-event",
            "batch-unattr-1",
            "batch-unattr-2",
        ):
            con.execute(
                "INSERT INTO scoring_batches(id, schema_version, capture_status, reason, captured_at) "
                "VALUES(?, 1, 'incomplete', 'test_fixture', ?)",
                (batch_id, old_ts),
            )

        for run_id in (
            "inc-attr-live",
            "inc-attr-churn",
            "inc-attr-event",
            "inc-unattr-1",
            "inc-unattr-2",
        ):
            con.execute(
                "INSERT INTO recommendation_runs(id, policy, policy_version, candidate_count, created_at) "
                "VALUES(?, 'p', 'v1', 1, ?)",
                (run_id, old_ts),
            )
            if run_id != "inc-attr-event":
                con.execute(
                    "INSERT INTO recommendation_run_evidence("
                    "run_id, schema_version, capture_status, reason, captured_at"
                    ") VALUES(?, 1, 'incomplete', 'storage_quota_exceeded', ?)",
                    (run_id, old_ts),
                )

        # 1. Incomplete capture with recommendation_outcomes row
        imp_cur = con.execute(
            "INSERT INTO recommendation_impressions(run_id, candidate_id, rank, score, propensity, presented_at) "
            "VALUES('inc-attr-live', ?, 1, 88.0, 1.0, ?)",
            (c1, old_ts),
        )
        imp_id = int(imp_cur.lastrowid)
        evt_cur = con.execute(
            "INSERT INTO recommendation_events(event_key, run_id, candidate_id, event_type, value, source, occurred_at) "
            "VALUES('evt-inc-live', 'inc-attr-live', ?, 'read', 5.0, 'web', ?)",
            (c1, old_ts),
        )
        con.execute(
            "INSERT INTO recommendation_outcomes(impression_id, event_id, label, label_kind, confidence, attributed_at) "
            "VALUES(?, ?, 1.0, 'explicit_rating', 1.0, ?)",
            (imp_id, int(evt_cur.lastrowid), old_ts),
        )
        con.execute(
            "INSERT INTO recommendation_run_evidence_scoring_batches(run_id, score_batch_id) "
            "VALUES('inc-attr-live', 'batch-attr-live')"
        )

        # 2. Incomplete capture whose candidate was deleted, leaving only recommendation_evidence_outcomes
        con.execute(
            "INSERT INTO recommendation_evidence_outcomes("
            "run_id, event_key, event_revision, candidate_id, rank, score, propensity, presented_at, label, label_kind, confidence, attributed_at"
            ") VALUES('inc-attr-churn', 'evt-inc-churn', 1, ?, 1, 88.0, 1.0, ?, 1.0, 'save', 0.8, ?)",
            (churn_cid, old_ts, old_ts),
        )
        con.execute("DELETE FROM candidates WHERE id = ?", (churn_cid,))
        con.execute(
            "INSERT INTO recommendation_run_evidence_scoring_batches(run_id, score_batch_id) "
            "VALUES('inc-attr-churn', 'batch-attr-churn')"
        )

        # 3. Missing recommendation_run_evidence row with non-'visible' recommendation_evidence_events
        con.execute(
            "INSERT INTO recommendation_evidence_events("
            "event_key, revision, run_id, candidate_id, event_type, source, occurred_at"
            ") VALUES('evt-inc-action', 1, 'inc-attr-event', ?, 'save', 'web', ?)",
            (c2, old_ts),
        )
        con.execute(
            "INSERT INTO recommendation_run_evidence_scoring_batches(run_id, score_batch_id) "
            "VALUES('inc-attr-event', 'batch-attr-event')"
        )

        # 4. Unattributed incomplete capture -> eligible for link cleanup in rotation
        con.execute(
            "INSERT INTO recommendation_run_evidence_scoring_batches(run_id, score_batch_id) "
            "VALUES('inc-unattr-1', 'batch-unattr-1')"
        )

    monkeypatch.setattr(settings, "evidence_rotation_enabled", True)
    with transaction() as con:
        rotate_recommendation_evidence_if_needed(
            con, incoming_bytes=10_000_000, max_bytes=512
        )
        links_after_rotation = {
            (str(r["run_id"]), str(r["score_batch_id"]))
            for r in con.execute(
                "SELECT run_id, score_batch_id FROM recommendation_run_evidence_scoring_batches"
            ).fetchall()
        }
        assert links_after_rotation == {
            ("inc-attr-live", "batch-attr-live"),
            ("inc-attr-churn", "batch-attr-churn"),
            ("inc-attr-event", "batch-attr-event"),
        }
        batches_after_rotation = {
            str(r["id"])
            for r in con.execute("SELECT id FROM scoring_batches").fetchall()
        }
        assert {
            "batch-attr-live",
            "batch-attr-churn",
            "batch-attr-event",
        } <= batches_after_rotation
        assert "batch-unattr-1" not in batches_after_rotation

        # Add a second unattributed incomplete link and verify telemetry pruning also
        # deletes only the unattributed link while preserving all attributed links.
        con.execute(
            "INSERT INTO recommendation_run_evidence_scoring_batches(run_id, score_batch_id) "
            "VALUES('inc-unattr-2', 'batch-unattr-2')"
        )

    monkeypatch.setattr(settings, "retention_enabled", True)
    run_storage_maintenance(now=now)

    with connect() as con:
        links_after_maint = {
            (str(r["run_id"]), str(r["score_batch_id"]))
            for r in con.execute(
                "SELECT run_id, score_batch_id FROM recommendation_run_evidence_scoring_batches"
            ).fetchall()
        }
        assert links_after_maint == {
            ("inc-attr-live", "batch-attr-live"),
            ("inc-attr-churn", "batch-attr-churn"),
            ("inc-attr-event", "batch-attr-event"),
        }
        batches_after_maint = {
            str(r["id"])
            for r in con.execute("SELECT id FROM scoring_batches").fetchall()
        }
        assert batches_after_maint == {
            "batch-attr-live",
            "batch-attr-churn",
            "batch-attr-event",
        }


def test_is_storage_retention_enabled_and_rotation_fail_closed_and_log(
    database, monkeypatch, caplog
):
    monkeypatch.setattr(settings, "retention_enabled", True)
    monkeypatch.setattr(settings, "evidence_rotation_enabled", True)

    class _BrokenConnection:
        def execute(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("simulated sqlite read failure")

    caplog.set_level(logging.WARNING)
    caplog.clear()
    assert is_storage_retention_enabled(_BrokenConnection()) is False
    assert any(
        "failing closed (storage retention disabled)" in rec.message
        for rec in caplog.records
    )

    caplog.clear()
    assert is_evidence_rotation_enabled(_BrokenConnection()) is False
    assert any(
        "failing closed (evidence rotation disabled)" in rec.message
        for rec in caplog.records
    )

    def _broken_connect():
        raise sqlite3.OperationalError("simulated connect failure")

    monkeypatch.setattr(decision_evidence_module, "connect", _broken_connect)
    monkeypatch.setattr(backups, "connect", _broken_connect)

    caplog.clear()
    assert is_storage_retention_enabled() is False
    assert any(
        "failing closed (storage retention disabled)" in rec.message
        for rec in caplog.records
    )

    caplog.clear()
    assert is_evidence_rotation_enabled() is False
    assert any(
        "failing closed (evidence rotation disabled)" in rec.message
        for rec in caplog.records
    )

    caplog.clear()
    assert backups.maintenance_is_due() is False
    assert any("failing closed" in rec.message for rec in caplog.records)


def test_conservative_defaults_and_decoupled_rotation_and_retention_controls(
    database, monkeypatch
):
    # Fresh Settings defaults are conservative (False) unless opted in.
    fresh_defaults = Settings(_env_file=None)
    assert fresh_defaults.retention_enabled is False
    assert fresh_defaults.evidence_rotation_enabled is False
    assert is_storage_retention_enabled() is False
    assert is_evidence_rotation_enabled() is False

    now = datetime(2026, 11, 21, 19, 0, tzinfo=timezone.utc)
    old_sql = (now - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S")
    with transaction() as con:
        _source_id, candidate_ids = _seed_source_and_candidates(con)
        rec_id = candidate_ids[0]
        con.execute(
            "INSERT INTO jobs(id, kind, status, created_at, finished_at) "
            "VALUES('optout-job', 'refresh', 'complete', ?, ?)",
            (old_sql, old_sql),
        )
    run_1 = _build_complete_run(rec_id, 1)

    # 1. Conservative default: neither scheduled retention nor evidence rotation deletes anything.
    assert backups.maintenance_is_due(now) is False
    default_maint = run_storage_maintenance(now=now)
    assert default_maint["skipped"] is True
    assert default_maint["reason"] == "retention_disabled"
    with transaction() as con:
        rot = rotate_recommendation_evidence_if_needed(
            con, incoming_bytes=10_000_000, max_bytes=1024
        )
        assert rot["rotated_runs"] == 0
        assert load_decision_evidence(con, run_1)["capture_status"] == "complete"
        assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1

    with TestClient(app, headers=authenticated_headers()) as client:
        initial_status = client.get("/api/backups").json()
        assert initial_status["retention_enabled"] is False
        assert initial_status["evidence_rotation_enabled"] is False

        # 2. Enabling evidence rotation does NOT enable scheduled table retention.
        rot_on = client.post("/api/backups/evidence-rotation", json={"enabled": True})
        assert rot_on.status_code == 200
        assert rot_on.json()["evidence_rotation_enabled"] is True
        assert rot_on.json()["status"]["retention_enabled"] is False
        assert backups.maintenance_is_due(now) is False
        assert run_storage_maintenance(now=now)["skipped"] is True
        with transaction() as con:
            rot_active = rotate_recommendation_evidence_if_needed(
                con, incoming_bytes=10_000_000, max_bytes=1024
            )
            assert rot_active["rotated_runs"] == 1
            assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1

        rot_off = client.post("/api/backups/evidence-rotation", json={"enabled": False})
        assert rot_off.status_code == 200
        assert rot_off.json()["evidence_rotation_enabled"] is False

        # 3. Enabling scheduled storage retention does NOT enable evidence rotation.
        ret_on = client.post("/api/backups/retention", json={"enabled": True})
        assert ret_on.status_code == 200
        assert ret_on.json()["retention_enabled"] is True
        assert ret_on.json()["status"]["evidence_rotation_enabled"] is False
        assert backups.maintenance_is_due(now) is True

        active_maint = run_storage_maintenance(now=now)
        assert active_maint["skipped"] is False
        assert active_maint["operational"]["pruned_jobs"] == 1
        with connect() as con:
            assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0

        ret_off = client.post("/api/backups/retention", json={"enabled": False})
        assert ret_off.status_code == 200
        assert ret_off.json()["retention_enabled"] is False
        assert backups.maintenance_is_due(now) is False


def test_side_effect_free_backups_and_compaction_only_recovery(
    database, monkeypatch
):
    monkeypatch.setattr(settings, "retention_enabled", True)
    db_path = Path(settings.db)
    with connect() as con:
        assert con.execute("PRAGMA auto_vacuum").fetchone()[0] == 2

    # Inflate DB with ~1.5 MiB of aged terminal job payloads and checkpoint into main DB.
    large_blob = "z" * 16_384
    with transaction() as con:
        for idx in range(90):
            con.execute(
                "INSERT INTO jobs(id, kind, status, result, created_at, finished_at) "
                "VALUES(?, 'refresh', 'complete', ?, '2025-01-01 00:00:00', '2025-01-01 00:01:00')",
                (f"bloat-job-{idx}", large_blob),
            )
    raw = sqlite3.connect(db_path, isolation_level=None)
    raw.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    raw.close()
    bloated_size = db_path.stat().st_size
    assert bloated_size > 1_200_000

    # 1. create_backup() MUST be completely side-effect-free (deletes zero rows).
    backup = backups.create_backup()
    assert Path(settings.backup_dir, f"{backup['id']}.sqlite3").is_file()
    with connect() as con:
        assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 90

    with TestClient(app, headers=authenticated_headers()) as client:
        # 2. Compact storage MUST NOT run retention or delete any rows!
        noop_compact = client.post("/api/backups/compact", json={})
        assert noop_compact.status_code == 200
        with connect() as con:
            assert con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 90

        # 3. Once scheduled retention prunes the aged jobs, Compact storage reclaims
        #    the resulting freelist pages online via incremental_vacuum.
        maint = run_storage_maintenance()
        assert maint["operational"]["pruned_jobs"] == 90
        shrunk_compact = client.post("/api/backups/compact", json={})
        assert shrunk_compact.status_code == 200
        shrunk_payload = shrunk_compact.json()
        assert shrunk_payload["migrated_auto_vacuum"] is False
        assert shrunk_payload["reclaimed_bytes"] > 500_000
        assert shrunk_payload["after_bytes"] < bloated_size * 0.6

    # 4. Simulate a legacy database with auto_vacuum=NONE (0) and freelist pages
    #    created by prior deletes, then test guarded one-time VACUUM upgrade.
    raw = sqlite3.connect(db_path, isolation_level=None)
    raw.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    raw.execute("PRAGMA auto_vacuum=NONE")
    raw.execute("VACUUM")
    assert raw.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
    raw.close()

    with transaction() as con:
        for idx in range(60):
            con.execute(
                "INSERT INTO jobs(id, kind, status, result, created_at, finished_at) "
                "VALUES(?, 'refresh', 'complete', ?, '2025-01-01 00:00:00', '2025-01-01 00:01:00')",
                (f"legacy-bloat-{idx}", large_blob),
            )
    raw = sqlite3.connect(db_path, isolation_level=None)
    raw.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    raw.close()
    legacy_bloated_size = db_path.stat().st_size

    # Delete the rows first so freelist pages exist; compact_database itself deletes zero rows.
    with transaction() as con:
        con.execute("DELETE FROM jobs")
    raw = sqlite3.connect(db_path, isolation_level=None)
    raw.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    raw.close()

    DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])
    real_disk_usage = shutil.disk_usage
    with TestClient(app, headers=authenticated_headers()) as client:
        monkeypatch.setattr(
            shutil,
            "disk_usage",
            lambda _p: DiskUsage(total=100_000_000, used=99_000_000, free=1_000_000),
        )
        blocked = client.post("/api/backups/compact", json={})
        assert blocked.status_code == 400
        assert "free disk space" in blocked.json()["detail"]

        monkeypatch.setattr(shutil, "disk_usage", real_disk_usage)
        compacted = client.post("/api/backups/compact", json={})
        assert compacted.status_code == 200
        payload = compacted.json()
        assert payload["migrated_auto_vacuum"] is True
        assert payload["auto_vacuum_mode"] == "incremental"
        assert payload["reclaimed_bytes"] > 500_000
        assert payload["after_bytes"] < legacy_bloated_size * 0.6

        status = client.get("/api/backups").json()
        assert status["retention_enabled"] is True
        assert status["auto_vacuum_mode"] == "incremental"
        assert status["live_size_bytes"] == payload["after_bytes"]
        assert status["evidence_quota_bytes"] == 512 * 1024 * 1024
        assert status["evidence_status"] == "healthy"
