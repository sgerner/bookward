import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import uuid

import pytest
import respx
import httpx
from fastapi.testclient import TestClient

from auth_helpers import authenticated_headers
from afterword_engine import profiles
from afterword_engine.config import settings
from afterword_engine.database import MIGRATIONS, initialize, normalize_key, row, rows, transaction
from afterword_engine.decision_evidence import build_decision_evidence, persist_run_evidence
from afterword_engine.digest import digest_preview
from afterword_engine.embeddings import content_hash, vector_blob
from afterword_engine.impression_fatigue import (
    apply_impression_fatigue,
    load_unacted_visible_impression_counts,
)
from afterword_engine.ingestion import scan_source
from afterword_engine.learning import attribute_read_outcomes, create_recommendation_run
from afterword_engine.main import app
from afterword_engine.ranking import rank_candidates
from afterword_engine.scoring import document, score_all
from afterword_engine.tenancy import profile_scope


@pytest.fixture()
def database(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "db", str(tmp_path / "deferred-history.db"))
    monkeypatch.setattr(settings, "backup_dir", "")
    initialize(seed_demo=False)
    profiles.initialize_auth()
    return settings.db


def _insert_run_and_impression(
    con,
    candidate_id: int,
    *,
    visible_at: str,
    score: float = 84.0,
) -> str:
    run_id = str(uuid.uuid4())
    con.execute(
        "INSERT INTO recommendation_runs"
        "(id,policy,policy_version,session_id,candidate_count,metadata,created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            run_id,
            "deterministic",
            "v1",
            "",
            1,
            json.dumps({"status": "recommended", "limit": 24}),
            visible_at,
        ),
    )
    con.execute(
        "INSERT INTO recommendation_impressions"
        "(run_id,candidate_id,rank,score,propensity,presented_at,visible_at) "
        "VALUES(?,?,1,?,1.0,?,?)",
        (run_id, candidate_id, score, visible_at, visible_at),
    )
    return run_id


def _insert_accepted_candidate(
    con,
    *,
    title: str,
    author: str = "Reader Test",
    score: float = 75.0,
    status: str = "recommended",
    source_id: int | None = None,
    quality_status: str = "accepted",
    explanation: list[str] | None = None,
) -> int:
    if source_id is None:
        source_id = int(
            con.execute("SELECT id FROM sources WHERE is_default=1 ORDER BY id LIMIT 1").fetchone()[0]
        )
    key = normalize_key(title, author)
    candidate_id = int(
        con.execute(
            "INSERT INTO candidates(title,author,description,genres,score,explanation,status,source_id,normalized_key) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                title,
                author,
                f"A novel titled {title}.",
                json.dumps(["Literary fiction"]),
                score,
                json.dumps(explanation or ["Initial match"]),
                status,
                source_id,
                key,
            ),
        ).lastrowid
    )
    con.execute(
        "UPDATE candidate_quality SET quality_status=?,quality_score=0.88,metadata_confidence=0.8 "
        "WHERE candidate_id=?",
        (quality_status, candidate_id),
    )
    return candidate_id


def test_deferred_archive_orders_most_recent_decision_first_with_bounded_paging(database):
    headers = authenticated_headers()
    with transaction() as con:
        rejected_ids = [
            _insert_accepted_candidate(
                con,
                title=f"High Score Pass {idx:02d}",
                score=99.0 - idx * 0.5,
            )
            for idx in range(25)
        ]
        deferred_id = _insert_accepted_candidate(
            con,
            title="Quiet Deferred Gem",
            score=21.5,
        )

    with TestClient(app, headers=headers) as client:
        for cid in rejected_ids:
            resp = client.post(f"/api/recommendations/{cid}/feedback", json={"action": "reject"})
            assert resp.status_code == 200
        # Undo and re-defer one book to ensure undone pass rows do not leak into status=rejected.
        flipped_id = rejected_ids[0]
        decision_row = row(
            "SELECT id FROM feedback WHERE candidate_id=? AND undone_at IS NULL ORDER BY id DESC LIMIT 1",
            (flipped_id,),
        )
        assert client.post(
            f"/api/recommendations/{flipped_id}/undo",
            json={"decision_id": decision_row["id"]},
        ).status_code == 200
        assert client.post(
            f"/api/recommendations/{flipped_id}/feedback",
            json={"action": "maybe_later"},
        ).status_code == 200

        defer_resp = client.post(
            f"/api/recommendations/{deferred_id}/feedback",
            json={"action": "maybe_later"},
        )
        assert defer_resp.status_code == 200

        overview = client.get("/api/overview").json()
        assert overview["decisions_has_more"] is True
        assert len(overview["decisions"]) == 24
        assert overview["decisions"][0]["id"] == deferred_id
        assert overview["decisions"][0]["status"] == "maybe_later"
        assert overview["decisions"][1]["id"] == flipped_id
        assert overview["decisions"][1]["status"] == "maybe_later"

        decisions_page_1 = client.get("/api/recommendations?status=decisions&limit=24&offset=0").json()
        decisions_page_2 = client.get("/api/recommendations?status=decisions&limit=24&offset=24").json()
        assert [item["id"] for item in decisions_page_1[:2]] == [deferred_id, flipped_id]
        assert len(decisions_page_1) == 24
        assert len(decisions_page_2) == 2

        maybe_later = client.get("/api/recommendations?status=maybe_later&limit=24&offset=0").json()
        assert [item["id"] for item in maybe_later] == [deferred_id, flipped_id]

        rejected = client.get("/api/recommendations?status=rejected&limit=25&offset=0").json()
        assert len(rejected) == 24
        assert flipped_id not in {item["id"] for item in rejected}
        assert deferred_id not in {item["id"] for item in rejected}


def test_score_all_refreshes_maybe_later_without_promoting_to_discover_or_digest(database):
    headers = authenticated_headers()
    feed_url = "https://93.184.216.34/active.xml"
    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title,author,rating,read_at,source) VALUES(?,?,?,?,?)",
            ("Loved Anchor", "Anchor Writer", 5, "2026-04-01", "manual"),
        )
        enabled_source_id = int(
            con.execute(
                "INSERT INTO sources(name,url,kind,enabled,is_default) VALUES(?,?,?,?,0)",
                ("Active Custom Feed", feed_url, "rss", 1),
            ).lastrowid
        )
        disabled_source_id = int(
            con.execute(
                "INSERT INTO sources(name,url,kind,enabled,is_default) VALUES(?,?,?,?,0)",
                ("Paused Custom Feed", "https://93.184.216.34/paused.xml", "rss", 0),
            ).lastrowid
        )
        active_deferred_id = _insert_accepted_candidate(
            con,
            title="Deferred Active Book",
            author="Anchor Writer",
            score=11.0,
            status="maybe_later",
            source_id=enabled_source_id,
            explanation=["Stale reason"],
        )
        disabled_deferred_id = _insert_accepted_candidate(
            con,
            title="Deferred Disabled Book",
            author="Anchor Writer",
            score=12.0,
            status="maybe_later",
            source_id=disabled_source_id,
            explanation=["Paused reason"],
        )
        live_id = _insert_accepted_candidate(
            con,
            title="Live Discover Book",
            author="Anchor Writer",
            score=15.0,
            status="recommended",
            source_id=enabled_source_id,
        )

    asyncio.run(score_all())

    active_deferred = row("SELECT score,explanation,status FROM candidates WHERE id=?", (active_deferred_id,))
    disabled_deferred = row("SELECT score,explanation,status FROM candidates WHERE id=?", (disabled_deferred_id,))
    live_row = row("SELECT score,status FROM candidates WHERE id=?", (live_id,))

    assert active_deferred["status"] == "maybe_later"
    assert active_deferred["score"] != 11.0
    assert json.loads(active_deferred["explanation"]) != ["Stale reason"]
    assert disabled_deferred["status"] == "maybe_later"
    assert live_row["status"] == "recommended"

    with TestClient(app, headers=headers) as client:
        discover = client.get("/api/recommendations").json()
        assert {item["id"] for item in discover} == {live_id}

    preview = digest_preview()
    digest_ids = {item["id"] for item in preview.get("books", [])}
    assert active_deferred_id not in digest_ids
    assert disabled_deferred_id not in digest_ids

    rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><title>Active Custom Feed</title>
      <item><title>Deferred Active Book — Anchor Writer</title><description>Updated synopsis</description></item>
    </channel></rss>"""
    source_record = row("SELECT * FROM sources WHERE id=?", (enabled_source_id,))
    with respx.mock(assert_all_called=False) as router:
        router.get(feed_url).mock(
            return_value=httpx.Response(200, text=rss_xml, headers={"content-type": "application/rss+xml"})
        )
        router.route().mock(return_value=httpx.Response(404))
        asyncio.run(scan_source(source_record))

    after_rescan = row("SELECT status FROM candidates WHERE id=?", (active_deferred_id,))
    assert after_rescan["status"] == "maybe_later"


def test_restoring_candidate_resets_prior_impression_fatigue_until_post_restore_days_accrue(database):
    headers = authenticated_headers()
    base_now = datetime.now(timezone.utc)
    with transaction() as con:
        cid = _insert_accepted_candidate(con, title="Nine Day Veteran", score=84.0)
        for day_offset in range(10, 1, -1):
            visible_ts = (base_now - timedelta(days=day_offset)).strftime("%Y-%m-%dT12:00:00Z")
            _insert_run_and_impression(con, cid, visible_at=visible_ts)
        prior_counts = load_unacted_visible_impression_counts(con, now=base_now)
        assert prior_counts[cid] == 9
        adjusted_prior, _ = apply_impression_fatigue(
            [{"id": cid, "status": "recommended", "score": 84.0}], prior_counts
        )
        assert adjusted_prior[0]["fatigue_penalty"] == 4.5

    with TestClient(app, headers=headers) as client:
        assert client.post(
            f"/api/recommendations/{cid}/feedback", json={"action": "maybe_later"}
        ).status_code == 200
        assert client.post(
            f"/api/recommendations/{cid}/feedback", json={"action": "restore"}
        ).status_code == 200

    with transaction() as con:
        after_restore_now = datetime.now(timezone.utc) + timedelta(minutes=1)
        reset_counts = load_unacted_visible_impression_counts(con, now=after_restore_now)
        assert reset_counts.get(cid, 0) == 0
        adjusted_reset, meta_reset = apply_impression_fatigue(
            [{"id": cid, "status": "recommended", "score": 84.0}], reset_counts
        )
        assert meta_reset["max_penalty"] == 0.0
        assert adjusted_reset[0].get("fatigue_penalty", 0.0) == 0.0

        for day in range(1, 4):
            visible_ts = (after_restore_now + timedelta(days=day)).strftime("%Y-%m-%dT12:00:00Z")
            _insert_run_and_impression(con, cid, visible_at=visible_ts)
        grace_now = after_restore_now + timedelta(days=3, hours=1)
        grace_counts = load_unacted_visible_impression_counts(con, now=grace_now)
        assert grace_counts[cid] == 3
        adjusted_grace, meta_grace = apply_impression_fatigue(
            [{"id": cid, "status": "recommended", "score": 84.0}], grace_counts
        )
        assert meta_grace["max_penalty"] == 0.0
        assert adjusted_grace[0].get("fatigue_penalty", 0.0) == 0.0

        fourth_ts = (after_restore_now + timedelta(days=4)).strftime("%Y-%m-%dT12:00:00Z")
        _insert_run_and_impression(con, cid, visible_at=fourth_ts)
        decayed_now = after_restore_now + timedelta(days=4, hours=1)
        decayed_counts = load_unacted_visible_impression_counts(con, now=decayed_now)
        assert decayed_counts[cid] == 4
        adjusted_decayed, meta_decayed = apply_impression_fatigue(
            [{"id": cid, "status": "recommended", "score": 84.0}], decayed_counts
        )
        assert meta_decayed["max_penalty"] == 0.75
        assert adjusted_decayed[0]["fatigue_penalty"] == 0.75


def test_disabled_source_or_unaccepted_restore_returns_409_while_preserving_shortlist_and_mark_read(database):
    headers = authenticated_headers()
    with transaction() as con:
        disabled_source_id = int(
            con.execute(
                "INSERT INTO sources(name,url,kind,enabled,is_default) VALUES(?,?,?,?,0)",
                ("Disabled Custom Feed", "https://93.184.216.34/disabled.xml", "rss", 0),
            ).lastrowid
        )
        disabled_cid = _insert_accepted_candidate(
            con,
            title="Paused Source Deferral",
            status="maybe_later",
            source_id=disabled_source_id,
        )
        second_disabled_cid = _insert_accepted_candidate(
            con,
            title="Second Paused Source Deferral",
            status="maybe_later",
            source_id=disabled_source_id,
        )
        quarantined_cid = _insert_accepted_candidate(
            con,
            title="Quarantined Deferral",
            status="maybe_later",
            quality_status="quarantine",
        )
        enabled_cid = _insert_accepted_candidate(
            con,
            title="Enabled Source Deferral",
            status="maybe_later",
        )

    with TestClient(app, headers=headers) as client:
        maybe_later = client.get("/api/recommendations?status=maybe_later&limit=24&offset=0").json()
        by_id = {item["id"]: item for item in maybe_later}
        assert by_id[disabled_cid]["source_enabled"] is False
        assert by_id[enabled_cid]["source_enabled"] is True

        blocked = client.post(
            f"/api/recommendations/{disabled_cid}/feedback",
            json={"action": "restore"},
        )
        assert blocked.status_code == 409
        detail = blocked.json()["detail"].lower()
        assert "is disabled" in detail
        assert "re-enable the source" in detail
        assert row("SELECT status FROM candidates WHERE id=?", (disabled_cid,))["status"] == "maybe_later"

        bulk_blocked = client.post(
            "/api/recommendations/bulk-feedback",
            json={"ids": [disabled_cid], "action": "restore"},
        )
        assert bulk_blocked.status_code == 409

        quarantine_blocked = client.post(
            f"/api/recommendations/{quarantined_cid}/feedback",
            json={"action": "restore"},
        )
        assert quarantine_blocked.status_code == 409
        assert "catalog quality checks" in quarantine_blocked.json()["detail"].lower()

        saved = client.post(
            f"/api/recommendations/{disabled_cid}/feedback",
            json={"action": "save"},
        )
        assert saved.status_code == 200
        assert row("SELECT status FROM candidates WHERE id=?", (disabled_cid,))["status"] == "saved"

        marked_read = client.post(
            f"/api/recommendations/{second_disabled_cid}/read",
            json={"rating": 4, "historical": True},
        )
        assert marked_read.status_code == 200
        assert row("SELECT status FROM candidates WHERE id=?", (second_disabled_cid,))["status"] == "read"


def test_historical_undated_mark_read_skips_outcomes_and_sorts_below_dated_reads(database):
    headers = authenticated_headers()
    with transaction() as con:
        historical_cid = _insert_accepted_candidate(con, title="Read Years Ago", author="Past Writer")
        dated_cid = _insert_accepted_candidate(con, title="Finished Yesterday", author="Recent Writer")
        _insert_run_and_impression(con, historical_cid, visible_at="2026-03-01T10:00:00Z")

    with TestClient(app, headers=headers) as client:
        bad_date = client.post(
            f"/api/recommendations/{historical_cid}/read",
            json={"read_at": "not-a-date"},
        )
        assert bad_date.status_code == 400

        future_date = client.post(
            f"/api/recommendations/{historical_cid}/read",
            json={"read_at": "2099-12-31"},
        )
        assert future_date.status_code == 400

        dated_resp = client.post(
            f"/api/recommendations/{dated_cid}/read",
            json={"rating": 5, "read_at": "2024-02-15"},
        )
        assert dated_resp.status_code == 200
        dated_read_id = dated_resp.json()["read_id"]

        hist_resp = client.post(
            f"/api/recommendations/{historical_cid}/read",
            json={"rating": 4, "historical": True},
        )
        assert hist_resp.status_code == 200
        hist_read_id = hist_resp.json()["read_id"]

        hist_read = row("SELECT * FROM reads WHERE id=?", (hist_read_id,))
        assert hist_read["source"] == "manual_history"
        assert hist_read["read_at"] is None
        assert hist_read["rating"] == 4

        attribute_read_outcomes()
        assert (
            row("SELECT COUNT(*) count FROM recommendation_outcomes WHERE read_id=?", (hist_read_id,))["count"]
            == 0
        )

        overview_titles = [item["title"] for item in client.get("/api/overview").json()["history"]]
        history_titles = [item["title"] for item in client.get("/api/reading-history").json()]
        scored_ids = [item["id"] for item in client.get("/api/reading-history/scores").json()]
        assert overview_titles == ["Finished Yesterday", "Read Years Ago"]
        assert history_titles == ["Finished Yesterday", "Read Years Ago"]
        assert scored_ids == [dated_read_id, hist_read_id]

    # Verify rank_candidates leaves read_at=None neutral on median neighbor age.
    undated_read = {"id": 1, "title": "Read Years Ago", "author": "Past Writer", "rating": 5, "read_at": None}
    no_date_key_read = {"id": 1, "title": "Read Years Ago", "author": "Past Writer", "rating": 5}
    candidate = {"id": 99, "title": "Neighbor Candidate", "author": "Other"}
    ranked_null = rank_candidates([undated_read], [[1.0, 0.0]], [candidate], [[1.0, 0.0]])[0]
    ranked_omitted = rank_candidates([no_date_key_read], [[1.0, 0.0]], [candidate], [[1.0, 0.0]])[0]
    assert ranked_null["score"] == ranked_omitted["score"]


def test_patch_reading_history_updates_and_clears_fields_reconciles_outcomes_and_dedupes_score_job(database):
    headers = authenticated_headers()
    with transaction() as con:
        cid = _insert_accepted_candidate(con, title="Reconciled Book", author="Outcome Writer")
        run_id = _insert_run_and_impression(con, cid, visible_at="2025-05-10T12:00:00Z", score=82.0)

    with TestClient(app, headers=headers) as client:
        token = client.post("/api/settings/api-tokens", json={"name": "History Test"}).json()["token"]
        marked = client.post(
            f"/api/recommendations/{cid}/read",
            json={"rating": 5, "read_at": "2025-05-12", "run_id": run_id},
        )
        assert marked.status_code == 200
        read_id = marked.json()["read_id"]

        outcome = row("SELECT label,label_kind FROM recommendation_outcomes WHERE read_id=?", (read_id,))
        assert outcome["label"] == 1.0
        assert outcome["label_kind"] == "rating_positive"

        with transaction() as con:
            con.execute("DELETE FROM jobs")

        low = client.patch(f"/api/reading-history/{read_id}", json={"rating": 2})
        assert low.status_code == 200
        assert low.json()["rating"] == 2
        outcome = row("SELECT label,label_kind FROM recommendation_outcomes WHERE read_id=?", (read_id,))
        assert outcome["label"] == 0.0
        assert outcome["label_kind"] == "rating_negative"

        cleared_rating = client.patch(
            f"/api/v1/reading-history/{read_id}",
            headers={"Authorization": f"Bearer {token}"},
            json={"rating": None},
        )
        assert cleared_rating.status_code == 200
        assert cleared_rating.json()["rating"] is None
        outcome = row("SELECT label,label_kind FROM recommendation_outcomes WHERE read_id=?", (read_id,))
        assert outcome["label"] == 0.75
        assert outcome["label_kind"] == "completed_unrated"

        queued_score_jobs = row(
            "SELECT COUNT(*) count FROM jobs WHERE kind='score' AND status='queued'"
        )["count"]
        assert queued_score_jobs == 1

        # Moving read_at earlier than the impression removes causal outcome attribution.
        pre_impression = client.patch(
            f"/api/reading-history/{read_id}",
            json={"read_at": "2025-05-01"},
        )
        assert pre_impression.status_code == 200
        assert (
            row("SELECT COUNT(*) count FROM recommendation_outcomes WHERE read_id=?", (read_id,))["count"]
            == 0
        )

        # Moving read_at after the impression restores causal outcome attribution.
        post_impression = client.patch(
            f"/api/reading-history/{read_id}",
            json={"read_at": "2025-05-15", "rating": 4},
        )
        assert post_impression.status_code == 200
        restored_outcome = row(
            "SELECT label,label_kind FROM recommendation_outcomes WHERE read_id=?",
            (read_id,),
        )
        assert restored_outcome["label"] == 1.0
        assert restored_outcome["label_kind"] == "rating_positive"

        # Clearing read_at removes causal attribution and transitions manual -> manual_history.
        cleared_date = client.patch(
            f"/api/reading-history/{read_id}",
            json={"read_at": None},
        )
        assert cleared_date.status_code == 200
        assert cleared_date.json()["read_at"] is None
        assert cleared_date.json()["source"] == "manual_history"
        assert (
            row("SELECT COUNT(*) count FROM recommendation_outcomes WHERE read_id=?", (read_id,))["count"]
            == 0
        )


def test_delete_manual_read_purges_evidence_restores_candidates_guards_imports_and_isolates_profiles(database):
    headers = authenticated_headers()
    with transaction() as con:
        enabled_cid = _insert_accepted_candidate(con, title="Mistaken Tap Book", author="Tap Writer")
        disabled_source_id = int(
            con.execute(
                "INSERT INTO sources(name,url,kind,enabled,is_default) VALUES(?,?,?,?,0)",
                ("Paused Feed", "https://93.184.216.34/paused2.xml", "rss", 0),
            ).lastrowid
        )
        disabled_cid = _insert_accepted_candidate(
            con,
            title="Disabled Mistaken Read",
            author="Paused Writer",
            status="maybe_later",
            source_id=disabled_source_id,
        )
        rss_read_id = int(
            con.execute(
                "INSERT INTO reads(title,author,rating,read_at,source) VALUES(?,?,?,?,?)",
                ("Synced Shelf Book", "Goodreads Author", 5, "2025-08-01", "goodreads_rss"),
            ).lastrowid
        )
        csv_read_id = int(
            con.execute(
                "INSERT INTO reads(title,author,rating,read_at,source) VALUES(?,?,?,?,?)",
                ("Imported Shelf Book", "CSV Author", 4, "2025-07-01", "goodreads_csv"),
            ).lastrowid
        )
        cand_row = dict(con.execute("SELECT * FROM candidates WHERE id=?", (enabled_cid,)).fetchone())
        cand_row["genres"] = ["Literary fiction"]
        cand_row["explanation"] = ["Initial match"]
        cand_row["source_name"] = "Default"
        cand_row["source_weight"] = 0.5
        cand_row["quality_status"] = "accepted"
        cand_row["quality_score"] = 0.88
        cand_row["metadata_confidence"] = 0.8
        vec = [1.0, 0.0, 0.0, 0.0]
        con.execute("UPDATE settings SET value='local' WHERE key='embedding_backend'")
        con.execute("UPDATE settings SET value='hashing-768' WHERE key='embedding_model'")
        con.execute(
            "INSERT OR REPLACE INTO embeddings(entity_type,entity_id,backend,model,vector,dimensions,content_hash) "
            "VALUES('candidate',?,'local','hashing-768',?,?,?)",
            (enabled_cid, vector_blob(vec), len(vec), content_hash(document(cand_row))),
        )
        evidence = build_decision_evidence(
            con,
            base_pool=[cand_row],
            personalized_pool=[cand_row],
            final_pool=[cand_row],
            interaction_events=[],
            cached_vectors={enabled_cid: vec},
            ranking_metadata={"mode": "live"},
            discovery_slate_metadata={"applied": False},
            request_context={"status": "recommended", "limit": 24, "offset": 0},
        )

    run_id = create_recommendation_run([cand_row], decision_evidence=evidence)
    with transaction() as con:
        con.execute(
            "UPDATE recommendation_impressions SET presented_at='2025-06-01T10:00:00Z',"
            "visible_at='2025-06-01T10:00:00Z' WHERE run_id=?",
            (run_id,),
        )

    with TestClient(app, headers=headers) as client:
        token = client.post("/api/settings/api-tokens", json={"name": "Delete Token"}).json()["token"]
        marked_enabled = client.post(
            f"/api/recommendations/{enabled_cid}/read",
            json={"rating": 5, "read_at": "2025-06-05", "run_id": run_id},
        )
        assert marked_enabled.status_code == 200
        enabled_read_id = marked_enabled.json()["read_id"]

        with transaction() as con:
            con.execute(
                "INSERT OR REPLACE INTO read_metadata(read_id,verified_work_id,identity_hash,description) VALUES(?,?,?,?)",
                (enabled_read_id, "/works/OL999W", "hash-enabled", "Metadata description"),
            )

        assert (
            row(
                "SELECT COUNT(*) count FROM recommendation_events WHERE event_key GLOB ?",
                (f"manual-read:*:{enabled_read_id:08d}",),
            )["count"]
            == 1
        )
        assert (
            row(
                "SELECT COUNT(*) count FROM recommendation_evidence_events WHERE event_key GLOB ?",
                (f"manual-read:*:{enabled_read_id:08d}",),
            )["count"]
            == 1
        )
        assert (
            row("SELECT COUNT(*) count FROM recommendation_outcomes WHERE read_id=?", (enabled_read_id,))["count"]
            == 1
        )

        deleted = client.delete(f"/api/reading-history/{enabled_read_id}")
        assert deleted.status_code == 200
        assert row("SELECT id FROM reads WHERE id=?", (enabled_read_id,)) is None
        assert row("SELECT read_id FROM read_metadata WHERE read_id=?", (enabled_read_id,)) is None
        assert (
            row("SELECT COUNT(*) count FROM recommendation_outcomes WHERE read_id=?", (enabled_read_id,))["count"]
            == 0
        )
        assert (
            row(
                "SELECT COUNT(*) count FROM recommendation_events WHERE event_key GLOB ?",
                (f"manual-read:*:{enabled_read_id:08d}",),
            )["count"]
            == 0
        )
        assert (
            row(
                "SELECT COUNT(*) count FROM recommendation_evidence_events WHERE event_key GLOB ?",
                (f"manual-read:*:{enabled_read_id:08d}",),
            )["count"]
            == 0
        )
        assert row("SELECT status FROM candidates WHERE id=?", (enabled_cid,))["status"] == "recommended"

        # Disabled-source candidate marked read and then deleted returns to maybe_later, not Discover.
        marked_disabled = client.post(
            f"/api/recommendations/{disabled_cid}/read",
            json={"historical": True},
        )
        assert marked_disabled.status_code == 200
        disabled_read_id = marked_disabled.json()["read_id"]
        deleted_v1 = client.delete(
            f"/api/v1/reading-history/{disabled_read_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert deleted_v1.status_code == 200
        assert row("SELECT status FROM candidates WHERE id=?", (disabled_cid,))["status"] == "maybe_later"

        # Imported Goodreads rows cannot be deleted.
        for imported_id in (rss_read_id, csv_read_id):
            resp = client.delete(f"/api/reading-history/{imported_id}")
            assert resp.status_code == 409
            assert row("SELECT id FROM reads WHERE id=?", (imported_id,)) is not None

    # Multi-profile isolation: Alice cannot mutate Owner's manual read.
    with transaction() as con:
        owner_read_id = int(
            con.execute(
                "INSERT INTO reads(title,author,rating,read_at,source) VALUES(?,?,?,?,?)",
                ("Owner Manual Book", "Owner Author", 5, "2026-01-10", "manual"),
            ).lastrowid
        )
    alice = profiles.create_account("alice-reader", "alice-password-with-enough-length")
    with profile_scope(alice["profile_id"]):
        initialize(seed_demo=False)
    alice_session, _ = profiles.create_session(alice["id"])
    with TestClient(app, headers={"x-bookward-session": alice_session}) as alice_client:
        assert alice_client.patch(f"/api/reading-history/{owner_read_id}", json={"rating": 1}).status_code == 404
        assert alice_client.delete(f"/api/reading-history/{owner_read_id}").status_code == 404

    assert row("SELECT rating FROM reads WHERE id=?", (owner_read_id,))["rating"] == 5
    assert [item["version"] for item in rows("SELECT version FROM schema_migrations ORDER BY version")] == [
        version for version, _ in MIGRATIONS
    ]
