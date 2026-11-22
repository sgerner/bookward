from __future__ import annotations

import asyncio
import json
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
from afterword_engine.config import settings
from afterword_engine.database import connect, initialize, transaction
from afterword_engine.decision_evidence import (
    build_decision_evidence,
    load_decision_evidence,
)
from afterword_engine.embeddings import content_hash, vector_blob
from afterword_engine.evaluation import build_report, load_rows
from afterword_engine.impression_fatigue import load_unacted_visible_impression_counts
from afterword_engine.learning import create_recommendation_run
from afterword_engine.main import app
from afterword_engine.maintenance import run_storage_maintenance
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


def test_non_ranking_recommendation_statuses_skip_run_creation(database):
    with transaction() as con:
        _source_id, candidate_ids = _seed_source_and_candidates(con)
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
        run_id = ranked_resp.headers.get("X-Bookward-Recommendation-Run")
        assert run_id
        assert all(item["recommendation_run_id"] == run_id for item in ranked_resp.json())
        with connect() as con:
            assert con.execute("SELECT COUNT(*) FROM recommendation_runs").fetchone()[0] == 1
            assert con.execute("SELECT COUNT(*) FROM recommendation_impressions").fetchone()[0] == 1


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


def test_evidence_watermark_rotation_evicts_to_65_percent_and_preserves_lineage(
    database, monkeypatch
):
    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title, author, rating, read_at, source) VALUES(?, ?, 5, '2026-01-10', 'test')",
            ("Anchor Read", "Anchor Author"),
        )
        _source_id, candidate_ids = _seed_source_and_candidates(con)
        rec_id = candidate_ids[0]
        saved_id = candidate_ids[1]

    # First scoring job creates batch 1, then a second scoring job creates batch 2,
    # leaving batch 1 unreferenced once candidates point to batch 2.
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

    # Record interaction events and feedback that MUST survive evidence rotation.
    with transaction() as con:
        con.execute(
            "INSERT INTO feedback(candidate_id, action) VALUES(?, 'save')",
            (saved_id,),
        )
        con.execute(
            """
            INSERT INTO recommendation_events(
                event_key, candidate_id, event_type, source, occurred_at
            ) VALUES ('manual:save:1', ?, 'save', 'web', '2026-11-01T00:00:00+00:00')
            """,
            (saved_id,),
        )

    def _make_complete_run(idx: int) -> str:
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
        return create_recommendation_run([cand], decision_evidence=ev, status="recommended", limit=20)

    early_runs = [_make_complete_run(i) for i in range(1, 7)]
    with connect() as con:
        used_with_six = int(
            con.execute("SELECT used_bytes FROM recommendation_evidence_budget WHERE id=1").fetchone()[0]
        )
    # Set quota so current usage is right around 85% of quota, forcing rotation on next capture.
    tight_quota = int(used_with_six / 0.85)
    monkeypatch.setattr(decision_evidence_module, "MAX_STORED_EVIDENCE_BYTES", tight_quota)

    newest_run = _make_complete_run(7)

    with connect() as con:
        used_after = int(
            con.execute("SELECT used_bytes FROM recommendation_evidence_budget WHERE id=1").fetchone()[0]
        )
        assert used_after <= tight_quota
        # Newest run is complete and passes full integrity checks including score_batch_id links!
        loaded_newest = load_decision_evidence(con, newest_run)
        assert loaded_newest is not None
        assert loaded_newest["capture_status"] == "complete"

        # Oldest unreferenced batch and oldest run evidence were rotated out.
        assert load_scoring_batch(con, first_batch_id) is None
        assert load_decision_evidence(con, early_runs[0]) is None

        # Active candidates.score_batch_id batch and all retained complete runs pass integrity checks.
        loaded_active_batch = load_scoring_batch(con, active_batch_id)
        assert loaded_active_batch is not None
        assert loaded_active_batch["capture_status"] == "complete"

        retained_run_ids = [
            str(r["run_id"])
            for r in con.execute(
                "SELECT run_id FROM recommendation_run_evidence WHERE capture_status='complete'"
            ).fetchall()
        ]
        assert newest_run in retained_run_ids
        for retained_id in retained_run_ids:
            verified = load_decision_evidence(con, retained_id)
            assert verified is not None and verified["capture_status"] == "complete"

        # Personalization feedback & events were NEVER deleted.
        assert con.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM recommendation_events").fetchone()[0] == 1


def test_telemetry_pruning_removes_aged_unattributed_rows_and_preserves_outcomes(database):
    now = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
    old_ts = (now - timedelta(days=120)).isoformat()
    recent_ts = (now - timedelta(days=15)).isoformat()

    with transaction() as con:
        _source_id, candidate_ids = _seed_source_and_candidates(con)
        c1, c2 = candidate_ids[0], candidate_ids[1]

        # 1. Old unattributed run (>90d) -> should be pruned, while its event survives with run_id=NULL
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

        # 2. Old attributed run (>90d) -> MUST survive with its impression and outcome for evaluation.py
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
        assert remaining_runs == {"old-attributed", "recent-unattributed"}
        assert con.execute("SELECT COUNT(*) FROM recommendation_events").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM feedback").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM recommendation_outcomes").fetchone()[0] == 1

    eval_rows = load_rows(settings.db)
    attributed = [r for r in eval_rows if r["run_id"] == "old-attributed"]
    assert len(attributed) == 1
    assert attributed[0]["label"] == 1.0
    assert attributed[0]["run_created_at"] == old_ts
    report = build_report(eval_rows, bootstrap_iterations=10)
    assert report["sample"]["runs"] == 2


def test_embedding_triggers_scan_source_rotation_and_virtual_history_scores(
    database, monkeypatch
):
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

    # Rotate feed items via scan_source so rec_id ('recommended') is deleted by scan_source.
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

    # Delete a read row as well and verify AFTER DELETE triggers removed both vectors immediately.
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

    # Calling /api/reading-history/scores must NOT persist read_candidate or negative-ID vectors.
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

    # Insert a stale-model vector; maintenance should prune both the stale-model vector
    # and the dismissed ('rejected') candidate vector (which has no active learner event).
    with transaction() as con:
        con.execute(
            "INSERT INTO embeddings(entity_type, entity_id, backend, model, vector, dimensions, content_hash) "
            "VALUES('candidate', ?, 'legacy-backend', 'legacy-model', ?, 4, 'legacy')",
            (saved_id, vec),
        )

    maint = run_storage_maintenance()
    assert maint["embeddings"]["removed_stale_model"] == 1
    assert maint["embeddings"]["removed_dismissed"] == 1
    with connect() as con:
        remaining = con.execute(
            "SELECT entity_type, entity_id, backend, model FROM embeddings"
        ).fetchall()
        keys = {(r["entity_type"], int(r["entity_id"])) for r in remaining}
        assert ("candidate", rejected_id) not in keys
        assert ("candidate", saved_id) in keys
        assert ("read", read_id_1) in keys


def test_operational_maintenance_trims_terminal_logs_and_keeps_active_leases(database):
    now = datetime(2026, 11, 21, 15, 0, tzinfo=timezone.utc)
    today_sql = now.strftime("%Y-%m-%d %H:%M:%S")
    old_sql = (now - timedelta(days=45)).strftime("%Y-%m-%d %H:%M:%S")
    old_iso = (now - timedelta(days=120)).isoformat()
    recent_iso = (now - timedelta(days=5)).isoformat()

    with transaction() as con:
        # Jobs: active queued + leased running + today's goodreads_rss + old terminal
        con.execute(
            "INSERT INTO jobs(id, kind, status, created_at) VALUES('job-queued', 'refresh', 'queued', ?)",
            (old_sql,),
        )
        con.execute(
            "INSERT INTO jobs(id, kind, status, created_at, started_at, heartbeat_at, lease_token) "
            "VALUES('job-running', 'refresh', 'running', ?, ?, ?, 'lease-123')",
            (old_sql, today_sql, today_sql),
        )
        con.execute(
            "INSERT INTO jobs(id, kind, status, created_at, finished_at) "
            "VALUES('job-rss-today', 'goodreads_rss', 'failed', ?, ?)",
            (today_sql, today_sql),
        )
        con.execute(
            "INSERT INTO jobs(id, kind, status, created_at, finished_at) "
            "VALUES('job-old-complete', 'refresh', 'complete', ?, ?)",
            (old_sql, old_sql),
        )

        # Association runs, requests, and cache
        con.execute(
            "INSERT INTO association_runs(id, provider, status, started_at) "
            "VALUES('assoc-running', 'librarything', 'running', ?)",
            (old_iso,),
        )
        con.execute(
            "INSERT INTO association_runs(id, provider, status, started_at, finished_at) "
            "VALUES('assoc-old', 'librarything', 'complete', ?, ?)",
            (old_iso, old_iso),
        )
        con.execute(
            "INSERT INTO association_requests(provider, request_key, status, requested_at) "
            "VALUES('librarything', 'today-key', 'ok', ?), ('librarything', 'old-key', 'ok', ?)",
            (now.isoformat(), old_iso),
        )
        con.execute(
            "INSERT INTO association_cache(provider, cache_key, payload, fetched_at, expires_at) "
            "VALUES('librarything', 'expired', '{}', ?, ?), ('librarything', 'valid', '{}', ?, ?)",
            (old_iso, (now - timedelta(days=1)).isoformat(), now.isoformat(), (now + timedelta(days=7)).isoformat()),
        )

        # Notification deliveries: pending + protected last_period + recent + aged expired
        con.execute(
            "INSERT INTO settings(key, value, secret) VALUES('digest_last_period', '2026-W46', 0) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        )
        con.execute(
            """
            INSERT INTO notification_deliveries(id, period_key, channel, status, candidate_ids, created_at, updated_at, sent_at)
            VALUES
                ('deliv-pending', '2026-W20', 'email', 'pending', '[1]', ?, ?, NULL),
                ('deliv-protected', '2026-W46', 'email', 'sent', '[1]', ?, ?, ?),
                ('deliv-recent', '2026-W47', 'discord', 'sent', '[1]', ?, ?, ?),
                ('deliv-aged', '2026-W05', 'email', 'sent', '[1]', ?, ?, ?)
            """,
            (
                old_iso, old_iso,
                old_iso, old_iso, old_iso,
                recent_iso, recent_iso, recent_iso,
                old_iso, old_iso, old_iso,
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
        assert jobs == {"job-queued", "job-running", "job-rss-today"}
        assoc_runs = {r["id"] for r in con.execute("SELECT id FROM association_runs").fetchall()}
        assert assoc_runs == {"assoc-running"}
        req_keys = {r["request_key"] for r in con.execute("SELECT request_key FROM association_requests").fetchall()}
        assert req_keys == {"today-key"}
        cache_keys = {r["cache_key"] for r in con.execute("SELECT cache_key FROM association_cache").fetchall()}
        assert cache_keys == {"valid"}
        deliveries = {r["id"] for r in con.execute("SELECT id FROM notification_deliveries").fetchall()}
        assert deliveries == {"deliv-pending", "deliv-protected", "deliv-recent"}

    # Expired/pruned digest periods cleanly fall back to found=False.
    with TestClient(app, headers=authenticated_headers()) as client:
        review = client.get("/api/digest/review?period=2026-W05")
        assert review.status_code == 200
        assert review.json() == {"period": "2026-W05", "ids": [], "found": False}


def test_incremental_vacuum_pre_backup_reclamation_and_guarded_online_compaction(
    database, monkeypatch
):
    db_path = Path(settings.db)
    with connect() as con:
        assert con.execute("PRAGMA auto_vacuum").fetchone()[0] == 2

    # Inflate DB with ~1.5 MiB of terminal job payloads, checkpoint into main DB, then delete them.
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

    # Pre-backup maintenance + incremental_vacuum shrinks BOTH live DB and backup snapshot!
    backup = backups.create_backup()
    shrunk_live_size = db_path.stat().st_size
    snapshot_path = Path(settings.backup_dir) / f"{backup['id']}.sqlite3"
    assert shrunk_live_size < bloated_size * 0.6
    assert snapshot_path.stat().st_size <= shrunk_live_size + 16_384

    # Simulate an existing legacy database with auto_vacuum=NONE (0) and free pages.
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

    DiskUsage = namedtuple("DiskUsage", ["total", "used", "free"])
    real_disk_usage = shutil.disk_usage
    with TestClient(app, headers=authenticated_headers()) as client:
        # Low disk space blocks full VACUUM upgrade cleanly without corrupting DB.
        monkeypatch.setattr(
            shutil,
            "disk_usage",
            lambda _p: DiskUsage(total=100_000_000, used=99_000_000, free=1_000_000),
        )
        blocked = client.post("/api/backups/compact", json={})
        assert blocked.status_code == 400
        assert "free disk space" in blocked.json()["detail"]

        # With sufficient disk space, online compaction migrates to INCREMENTAL and shrinks DB.
        monkeypatch.setattr(shutil, "disk_usage", real_disk_usage)
        compacted = client.post("/api/backups/compact", json={})
        assert compacted.status_code == 200
        payload = compacted.json()
        assert payload["migrated_auto_vacuum"] is True
        assert payload["auto_vacuum_mode"] == "incremental"
        assert payload["reclaimed_bytes"] > 500_000
        assert payload["after_bytes"] < legacy_bloated_size * 0.6

        status = client.get("/api/backups").json()
        assert status["auto_vacuum_mode"] == "incremental"
        assert status["live_size_bytes"] == payload["after_bytes"]
        assert status["evidence_quota_bytes"] == 512 * 1024 * 1024
        assert status["evidence_status"] == "healthy"
