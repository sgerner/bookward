import json
import math
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auth_helpers import authenticated_headers
from afterword_engine.config import settings
from afterword_engine.database import initialize, row, rows, transaction
from afterword_engine.evaluation import ips_ndcg_at_k, ips_precision_at_k, load_rows
from afterword_engine.main import app


@pytest.fixture()
def seeded_db(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "discover_session_runs.db"
    monkeypatch.setattr(settings, "db", str(db_path))
    monkeypatch.setattr(settings, "exploration_enabled", False)
    initialize()

    with transaction() as con:
        source_id = con.execute(
            "SELECT id FROM sources WHERE is_default=1 LIMIT 1"
        ).fetchone()[0]
        con.execute("DELETE FROM recommendation_outcomes")
        con.execute("DELETE FROM recommendation_impressions")
        con.execute("DELETE FROM recommendation_runs")
        con.execute("DELETE FROM recommendation_events")
        con.execute("DELETE FROM candidate_quality")
        con.execute("DELETE FROM candidates")

        candidate_ids: list[int] = []
        for idx in range(1, 37):
            score = round(98.0 - idx * 0.75, 2)
            cid = con.execute(
                "INSERT INTO candidates("
                "title,author,description,cover_url,source_url,genres,score,explanation,status,source_id,normalized_key"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"Session Book {idx:02d}",
                    f"Author {idx:02d}",
                    f"Description for book {idx:02d}",
                    f"https://covers.example/book-{idx:02d}.jpg",
                    f"https://books.example/book-{idx:02d}",
                    json.dumps(["Literary Fiction"]),
                    score,
                    json.dumps([f"Ranked slot {idx}"]),
                    "recommended",
                    source_id,
                    f"session-book-{idx:02d}",
                ),
            ).lastrowid
            con.execute(
                "INSERT OR REPLACE INTO candidate_quality("
                "candidate_id,quality_status,metadata_confidence"
                ") VALUES(?,?,?)",
                (cid, "accepted", 1.0),
            )
            candidate_ids.append(cid)

    return db_path, candidate_ids


def test_five_click_discover_triage_creates_one_run_and_preserves_ndcg(seeded_db):
    db_path, _ = seeded_db
    client = TestClient(app, headers=authenticated_headers())
    overview = client.get(
        "/api/overview?recommendation_limit=24&decision_limit=24"
    )
    assert overview.status_code == 200
    payload = overview.json()
    run_id = payload["recommendation_run_id"]
    assert run_id
    recommended = [
        item
        for item in payload["recommendations"]
        if item["status"] == "recommended"
    ]
    assert len(recommended) == 24

    for item in recommended[:4]:
        resp = client.post(
            f"/api/recommendations/{item['id']}/feedback",
            json={"action": "reject", "run_id": run_id},
        )
        assert resp.status_code == 200

    save_resp = client.post(
        f"/api/recommendations/{recommended[4]['id']}/feedback",
        json={"action": "save", "run_id": run_id},
    )
    assert save_resp.status_code == 200

    assert row("SELECT COUNT(*) count FROM recommendation_runs")["count"] == 1
    attributed = rows(
        "SELECT ri.rank, ro.label "
        "FROM recommendation_outcomes ro "
        "JOIN recommendation_impressions ri ON ri.id = ro.impression_id "
        "ORDER BY ri.rank ASC"
    )
    assert [(r["rank"], r["label"]) for r in attributed] == [
        (1, 0),
        (2, 0),
        (3, 0),
        (4, 0),
        (5, 1),
    ]

    eval_rows = load_rows(db_path)
    assert len(eval_rows) == 24
    assert sum(1 for r in eval_rows if r["label"] is None) == 19
    assert ips_ndcg_at_k(eval_rows, 20) == pytest.approx(1.0 / math.log2(6))


def test_twenty_passes_then_rank_twenty_one_save_preserves_session_ranks(seeded_db):
    db_path, _ = seeded_db
    client = TestClient(app, headers=authenticated_headers())
    overview = client.get(
        "/api/overview?recommendation_limit=24&decision_limit=24"
    ).json()
    run_id = overview["recommendation_run_id"]
    recommended = [
        item
        for item in overview["recommendations"]
        if item["status"] == "recommended"
    ]

    for item in recommended[:20]:
        resp = client.post(
            f"/api/recommendations/{item['id']}/feedback",
            json={"action": "reject", "run_id": run_id},
        )
        assert resp.status_code == 200

    save_resp = client.post(
        f"/api/recommendations/{recommended[20]['id']}/feedback",
        json={"action": "save", "run_id": run_id},
    )
    assert save_resp.status_code == 200

    assert row("SELECT COUNT(*) count FROM recommendation_runs")["count"] == 1
    eval_rows = load_rows(db_path)
    assert ips_precision_at_k(eval_rows, 20) == 0.0
    assert ips_ndcg_at_k(eval_rows, 20) is None
    assert ips_ndcg_at_k(eval_rows, 24) == pytest.approx(1.0 / math.log2(22))


def test_non_discover_requests_create_zero_runs_and_impressions(seeded_db):
    client = TestClient(app, headers=authenticated_headers())
    r_overview = client.get(
        "/api/overview?recommendation_limit=24&decision_limit=24&track_run=false"
    )
    assert r_overview.status_code == 200
    assert r_overview.json()["recommendation_run_id"] == ""

    for query in (
        "status=saved",
        "status=decisions&limit=25&offset=0",
        "status=rejected",
        "status=maybe_later",
        "status=recommended&limit=8&offset=0&track_run=false",
    ):
        resp = client.get(f"/api/recommendations?{query}")
        assert resp.status_code == 200
        assert "x-bookward-recommendation-run" not in resp.headers

    assert row("SELECT COUNT(*) count FROM recommendation_runs")["count"] == 0
    assert row("SELECT COUNT(*) count FROM recommendation_impressions")["count"] == 0


def test_paginated_discover_attaches_true_sequential_ranks_without_triage_drift(
    seeded_db,
):
    _, candidate_ids = seeded_db
    client = TestClient(app, headers=authenticated_headers())
    overview = client.get(
        "/api/overview?recommendation_limit=24&decision_limit=24"
    ).json()
    run_id = overview["recommendation_run_id"]
    initial_ids = [
        item["id"]
        for item in overview["recommendations"]
        if item["status"] == "recommended"
    ]
    assert initial_ids == candidate_ids[:24]

    # Triage 3 books from the first 24 before paginating past slot 24.
    assert (
        client.post(
            f"/api/recommendations/{candidate_ids[0]}/feedback",
            json={"action": "reject", "run_id": run_id},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/recommendations/{candidate_ids[1]}/feedback",
            json={"action": "save", "run_id": run_id},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/recommendations/{candidate_ids[2]}/feedback",
            json={"action": "maybe_later", "run_id": run_id},
        ).status_code
        == 200
    )

    # Fetch page 4 (offset=24, limit=9 for 8 items + 1 lookahead, impression_limit=8).
    page4 = client.get(
        f"/api/recommendations?status=recommended&limit=9&offset=24&run_id={run_id}&impression_limit=8"
    )
    assert page4.status_code == 200
    assert page4.headers["x-bookward-recommendation-run"] == run_id
    items = page4.json()
    assert [item["id"] for item in items] == candidate_ids[24:33]
    assert all(item["recommendation_run_id"] == run_id for item in items[:8])
    assert items[8]["recommendation_run_id"] == ""

    # Save rank 27 (3rd book on page 4 -> candidate_ids[26]).
    save_page4 = client.post(
        f"/api/recommendations/{candidate_ids[26]}/feedback",
        json={"action": "save", "run_id": run_id},
    )
    assert save_page4.status_code == 200

    assert row("SELECT COUNT(*) count FROM recommendation_runs")["count"] == 1
    assert (
        row(
            "SELECT candidate_count FROM recommendation_runs WHERE id=?",
            (run_id,),
        )["candidate_count"]
        == 32
    )
    logged_ranks = rows(
        "SELECT candidate_id, rank FROM recommendation_impressions "
        "WHERE run_id=? AND rank >= 25 ORDER BY rank ASC",
        (run_id,),
    )
    assert [(r["candidate_id"], r["rank"]) for r in logged_ranks] == [
        (candidate_ids[24 + idx], 25 + idx) for idx in range(8)
    ]
    rank_27_outcome = row(
        "SELECT ri.rank, ro.label FROM recommendation_outcomes ro "
        "JOIN recommendation_impressions ri ON ri.id = ro.impression_id "
        "WHERE ri.candidate_id = ?",
        (candidate_ids[26],),
    )
    assert rank_27_outcome["rank"] == 27
    assert rank_27_outcome["label"] == 1


def test_visible_telemetry_records_across_triage_and_refresh_runs(seeded_db):
    _, candidate_ids = seeded_db
    client = TestClient(app, headers=authenticated_headers())
    run_1 = client.get(
        "/api/overview?recommendation_limit=24&decision_limit=24"
    ).json()["recommendation_run_id"]

    telemetry_1 = client.post(
        "/api/telemetry/events",
        json={
            "events": [
                {
                    "event_key": f"vis-r1-{idx:04d}",
                    "candidate_id": candidate_ids[idx],
                    "event_type": "visible",
                    "run_id": run_1,
                }
                for idx in range(4)
            ]
        },
    )
    assert telemetry_1.status_code == 200

    # Triage card 0, then record visibility on card 4 within run_1.
    assert (
        client.post(
            f"/api/recommendations/{candidate_ids[0]}/feedback",
            json={"action": "reject", "run_id": run_1},
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/telemetry/events",
            json={
                "events": [
                    {
                        "event_key": "vis-r1-0004",
                        "candidate_id": candidate_ids[4],
                        "event_type": "visible",
                        "run_id": run_1,
                    }
                ]
            },
        ).status_code
        == 200
    )

    # Explicit refresh mints run_2; stationary cards 1..4 emit visible for run_2.
    run_2 = client.get(
        "/api/overview?recommendation_limit=24&decision_limit=24"
    ).json()["recommendation_run_id"]
    assert run_2 != run_1

    telemetry_2 = client.post(
        "/api/telemetry/events",
        json={
            "events": [
                {
                    "event_key": f"vis-r2-{idx:04d}",
                    "candidate_id": candidate_ids[idx],
                    "event_type": "visible",
                    "run_id": run_2,
                }
                for idx in range(1, 5)
            ]
        },
    )
    assert telemetry_2.status_code == 200

    r1_visible = row(
        "SELECT COUNT(*) count FROM recommendation_impressions "
        "WHERE run_id=? AND visible_at IS NOT NULL",
        (run_1,),
    )["count"]
    r2_visible = row(
        "SELECT COUNT(*) count FROM recommendation_impressions "
        "WHERE run_id=? AND visible_at IS NOT NULL",
        (run_2,),
    )["count"]
    assert r1_visible == 5
    assert r2_visible == 4


def test_digest_period_overview_includes_fatigued_active_picks_below_top_24(seeded_db):
    _, candidate_ids = seeded_db
    fatigued_id = candidate_ids[29]  # Rank 30, below recommendation_limit=24

    with transaction() as con:
        # Add prior multi-day impressions on fatigued_id so impression fatigue also applies.
        for day in ("2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"):
            prior_run = f"prior-run-{day}"
            con.execute(
                "INSERT INTO recommendation_runs(id,policy,policy_version,candidate_count,created_at) "
                "VALUES(?,?,?,?,?)",
                (prior_run, "rating-neighborhood", "v1", 1, f"{day}T10:00:00+00:00"),
            )
            con.execute(
                "INSERT INTO recommendation_impressions("
                "run_id,candidate_id,rank,score,propensity,presented_at,visible_at"
                ") VALUES(?,?,?,?,?,?,?)",
                (
                    prior_run,
                    fatigued_id,
                    5,
                    75.0,
                    1.0,
                    f"{day}T10:00:00+00:00",
                    f"{day}T10:00:05+00:00",
                ),
            )
        con.execute(
            "INSERT INTO notification_deliveries("
            "id,period_key,channel,status,candidate_ids,attempts,sent_at"
            ") VALUES(?,?,?,?,?,?,?)",
            (
                "digest-2026-w42",
                "2026-W42",
                "email",
                "sent",
                json.dumps([candidate_ids[0], fatigued_id]),
                1,
                "2026-10-15T09:00:00+00:00",
            ),
        )

    client = TestClient(app, headers=authenticated_headers())
    plain_overview = client.get(
        "/api/overview?recommendation_limit=24&decision_limit=24&track_run=false"
    ).json()
    assert fatigued_id not in {
        item["id"] for item in plain_overview["recommendations"]
    }

    digest_overview = client.get(
        "/api/overview?recommendation_limit=24&decision_limit=24&digest_period=2026-W42"
    ).json()
    run_id = digest_overview["recommendation_run_id"]
    returned_ids = {item["id"] for item in digest_overview["recommendations"]}
    assert candidate_ids[0] in returned_ids
    assert fatigued_id in returned_ids

    fatigued_card = next(
        item
        for item in digest_overview["recommendations"]
        if item["id"] == fatigued_id
    )
    assert fatigued_card["recommendation_run_id"] == run_id

    bulk_resp = client.post(
        "/api/recommendations/bulk-feedback",
        json={
            "ids": [candidate_ids[0], fatigued_id],
            "action": "save",
            "run_id": run_id,
        },
    )
    assert bulk_resp.status_code == 200
    assert bulk_resp.json()["updated"] == 2

    fatigued_impression = row(
        "SELECT ri.rank, ro.label FROM recommendation_impressions ri "
        "JOIN recommendation_outcomes ro ON ro.impression_id = ri.id "
        "WHERE ri.run_id = ? AND ri.candidate_id = ?",
        (run_id, fatigued_id),
    )
    assert fatigued_impression is not None
    assert fatigued_impression["rank"] > 24
    assert fatigued_impression["label"] == 1


def test_public_v1_endpoints_keep_backward_compatible_contract(seeded_db):
    client = TestClient(app, headers=authenticated_headers())
    token_resp = client.post(
        "/api/settings/api-tokens", json={"name": "External integration"}
    )
    assert token_resp.status_code == 200
    token = token_resp.json()["token"]

    public_client = TestClient(app, headers={"Authorization": f"Bearer {token}"})
    v1_overview = public_client.get("/api/v1/overview")
    assert v1_overview.status_code == 200
    body = v1_overview.json()
    assert body["recommendation_run_id"]
    assert "recommendations" in body
    assert len(body["recommendations"]) == 36
    assert "api_tokens" not in body["settings"]

    v1_recs = public_client.get("/api/v1/recommendations?status=recommended&limit=5")
    assert v1_recs.status_code == 200
    recs = v1_recs.json()
    assert len(recs) == 5
    assert "x-bookward-recommendation-run" in v1_recs.headers


def test_paginated_run_recommendations_rejects_non_discovery_or_cross_profile_run_id(
    seeded_db,
):
    from afterword_engine import profiles

    client = TestClient(app, headers=authenticated_headers())
    overview = client.get(
        "/api/overview?recommendation_limit=24&decision_limit=24"
    ).json()
    valid_run_id = overview["recommendation_run_id"]
    assert valid_run_id

    # Unknown run_id is rejected without minting a new run or appending impressions.
    unknown = client.get(
        "/api/recommendations?status=recommended&limit=8&offset=24&run_id=missing-run-1234"
    )
    assert unknown.status_code == 400
    assert row("SELECT COUNT(*) count FROM recommendation_runs")["count"] == 1

    # Passing a valid discovery run_id to a non-discovery status query is rejected.
    wrong_query_status = client.get(
        f"/api/recommendations?status=saved&limit=8&offset=0&run_id={valid_run_id}"
    )
    assert wrong_query_status.status_code == 400

    # Historical/legacy non-discovery runs in recommendation_runs cannot receive impressions.
    with transaction() as con:
        for bad_run_id, bad_meta in (
            ("legacy-saved-run-01", json.dumps({"status": "saved"})),
            ("legacy-decisions-01", json.dumps({"status": "decisions"})),
            ("other-profile-meta", json.dumps({"status": "recommended", "profile_id": "deadbeef" * 4})),
            ("malformed-meta-run", "{not-json"),
        ):
            con.execute(
                "INSERT INTO recommendation_runs(id,policy,policy_version,candidate_count,metadata) "
                "VALUES(?,?,?,?,?)",
                (bad_run_id, "rating-neighborhood", "v1", 0, bad_meta),
            )

    for bad_run_id in (
        "legacy-saved-run-01",
        "legacy-decisions-01",
        "other-profile-meta",
        "malformed-meta-run",
    ):
        resp = client.get(
            f"/api/recommendations?status=recommended&limit=8&offset=24&run_id={bad_run_id}"
        )
        assert resp.status_code == 400
        assert (
            row(
                "SELECT COUNT(*) count FROM recommendation_impressions WHERE run_id=?",
                (bad_run_id,),
            )["count"]
            == 0
        )

    # A second profile cannot append or read pagination against the first profile's run_id.
    from afterword_engine.tenancy import profile_scope

    second_account = profiles.create_account(
        "second-reader", "test-password-with-sufficient-length"
    )
    with profile_scope(second_account["profile_id"]):
        initialize(seed_demo=False)
    second_session, _ = profiles.create_session(second_account["id"])
    other_client = TestClient(app, headers={"x-bookward-session": second_session})
    cross_profile = other_client.get(
        f"/api/recommendations?status=recommended&limit=8&offset=0&run_id={valid_run_id}"
    )
    assert cross_profile.status_code == 400
    assert (
        row(
            "SELECT COUNT(*) count FROM recommendation_impressions WHERE run_id=?",
            (valid_run_id,),
        )["count"]
        == 24
    )


def test_untracked_recommendations_skip_exploration_reordering_and_log_no_propensity(
    seeded_db, monkeypatch
):
    from afterword_engine.database import connect
    from afterword_engine.main import tracked_recommendations

    _, candidate_ids = seeded_db
    monkeypatch.setattr(settings, "exploration_enabled", True)
    monkeypatch.setattr(settings, "exploration_epsilon", 1.0)
    monkeypatch.setattr(settings, "exploration_stable_top_k", 4)

    with connect() as con:
        # Mark one candidate saved and one user-rejected (with active feedback row)
        # so non-discovery status queries return items while exercising `should_track == False`.
        con.execute("UPDATE candidates SET status='saved' WHERE id=?", (candidate_ids[28],))
        con.execute("UPDATE candidates SET status='rejected' WHERE id=?", (candidate_ids[29],))
        con.execute(
            "INSERT INTO feedback(candidate_id, action) VALUES(?, 'reject')",
            (candidate_ids[29],),
        )
        con.commit()

        direct_items, direct_run_id = tracked_recommendations(
            connection=con,
            status="recommended",
            limit=24,
            offset=0,
            track_run=False,
        )
        assert direct_run_id == ""
        assert [item["id"] for item in direct_items] == candidate_ids[:24]
        assert all("propensity" not in item for item in direct_items)
        assert all(item["recommendation_run_id"] == "" for item in direct_items)

        for non_discovery_status in ("saved", "rejected", "decisions"):
            non_disc_items, non_disc_run_id = tracked_recommendations(
                connection=con,
                status=non_discovery_status,
                limit=24,
                offset=0,
                track_run=True,
            )
            assert non_disc_run_id == ""
            assert len(non_disc_items) >= 1
            assert all("propensity" not in item for item in non_disc_items)
            assert all(item["recommendation_run_id"] == "" for item in non_disc_items)

    client = TestClient(app, headers=authenticated_headers())
    for _ in range(5):
        overview = client.get(
            "/api/overview?recommendation_limit=24&decision_limit=24&track_run=false"
        )
        assert overview.status_code == 200
        payload = overview.json()
        assert payload["recommendation_run_id"] == ""
        returned_ids = [
            item["id"]
            for item in payload["recommendations"]
            if item["status"] == "recommended"
        ]
        assert returned_ids == candidate_ids[:24]
        assert all("propensity" not in item for item in payload["recommendations"])

        recs = client.get(
            "/api/recommendations?status=recommended&limit=24&offset=0&track_run=false"
        )
        assert recs.status_code == 200
        assert "x-bookward-recommendation-run" not in recs.headers
        rec_items = recs.json()
        assert [item["id"] for item in rec_items] == candidate_ids[:24]
        assert all("propensity" not in item for item in rec_items)
        assert all(item["recommendation_run_id"] == "" for item in rec_items)

    assert row("SELECT COUNT(*) count FROM recommendation_runs")["count"] == 0
    assert row("SELECT COUNT(*) count FROM recommendation_impressions")["count"] == 0
    assert (
        row("SELECT COUNT(*) count FROM recommendation_evidence_exposures")["count"] == 0
    )


def test_run_metadata_helpers_are_pure_and_exclude_non_json_legacy_rows():
    from afterword_engine.run_metadata import (
        is_discovery_run_metadata,
        is_matching_run_profile,
        parse_run_metadata,
    )

    for legacy_discovery in (
        None,
        "",
        "{}",
        {},
        json.dumps({"status": "recommended"}),
        json.dumps({"status": "ALL"}),
        json.dumps({"status": ""}),
        json.dumps({"offset": 0}),
    ):
        assert is_discovery_run_metadata(legacy_discovery) is True

    for excluded in (
        json.dumps({"status": "saved"}),
        json.dumps({"status": "decisions"}),
        json.dumps({"status": "rejected"}),
        json.dumps({"status": "maybe_later"}),
        json.dumps({"status": 123}),
        "legacy-non-json-metadata",
        "{truncated",
        "[1, 2, 3]",
        '"recommended"',
        42,
    ):
        assert is_discovery_run_metadata(excluded) is False

    assert parse_run_metadata("legacy-non-json-metadata") is None
    assert is_matching_run_profile(None, "legacy") is True
    assert is_matching_run_profile("{}", "profile-abc") is True
    assert (
        is_matching_run_profile(
            json.dumps({"profile_id": "profile-abc"}), "profile-abc"
        )
        is True
    )
    assert (
        is_matching_run_profile(
            json.dumps({"profile_id": "profile-other"}), "profile-abc"
        )
        is False
    )
    assert is_matching_run_profile("legacy-non-json-metadata", "legacy") is False


def test_explicit_non_discovery_or_missing_run_id_never_falls_back_to_other_run(
    seeded_db,
):
    from afterword_engine.learning import _find_latest_impression

    _, candidate_ids = seeded_db
    target_id = candidate_ids[0]
    client = TestClient(app, headers=authenticated_headers())
    overview = client.get(
        "/api/overview?recommendation_limit=24&decision_limit=24"
    ).json()
    discovery_run_id = overview["recommendation_run_id"]
    assert discovery_run_id

    with transaction() as con:
        for run_id, raw_meta, ts in (
            (
                "run-a-non-discovery",
                json.dumps({"status": "saved"}),
                "2026-12-01T12:00:00+00:00",
            ),
            (
                "run-a-non-json-meta",
                "legacy-plain-text-metadata",
                "2026-12-01T12:05:00+00:00",
            ),
        ):
            con.execute(
                "INSERT INTO recommendation_runs(id,policy,policy_version,candidate_count,metadata,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (run_id, "rating-neighborhood", "v1", 1, raw_meta, ts),
            )
            con.execute(
                "INSERT INTO recommendation_impressions("
                "run_id,candidate_id,rank,score,propensity,presented_at"
                ") VALUES(?,?,?,?,?,?)",
                (run_id, target_id, 1, 95.0, 1.0, ts),
            )

        # Explicit non-discovery, non-JSON, or missing run_id must return None rather than
        # falling back to discovery_run_id's impression.
        assert _find_latest_impression(con, target_id, "run-a-non-discovery") is None
        assert _find_latest_impression(con, target_id, "run-a-non-json-meta") is None
        assert _find_latest_impression(con, target_id, "missing-run-9999") is None
        fallback = _find_latest_impression(con, target_id, None)
        assert fallback is not None
        assert fallback["run_id"] == discovery_run_id

    # Feedback explicitly naming run A (non-discovery) must NOT attribute an outcome to run B.
    save_non_disc = client.post(
        f"/api/recommendations/{target_id}/feedback",
        json={"action": "save", "run_id": "run-a-non-discovery"},
    )
    assert save_non_disc.status_code == 200
    assert row("SELECT COUNT(*) count FROM recommendation_outcomes")["count"] == 0

    reject_non_json = client.post(
        f"/api/recommendations/{target_id}/feedback",
        json={"action": "reject", "run_id": "run-a-non-json-meta"},
    )
    assert reject_non_json.status_code == 200
    assert row("SELECT COUNT(*) count FROM recommendation_outcomes")["count"] == 0

    # Without an explicit run_id, attribution still matches discovery_run_id.
    save_unscoped = client.post(
        f"/api/recommendations/{target_id}/feedback",
        json={"action": "save"},
    )
    assert save_unscoped.status_code == 200
    outcomes = rows(
        "SELECT ri.run_id, ro.label FROM recommendation_outcomes ro "
        "JOIN recommendation_impressions ri ON ri.id = ro.impression_id"
    )
    assert [(r["run_id"], r["label"]) for r in outcomes] == [(discovery_run_id, 1.0)]


def test_concurrent_pagination_assigns_unique_contiguous_ranks_inside_write_transaction(
    seeded_db, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from afterword_engine import main as engine_main

    _, candidate_ids = seeded_db
    auth_headers = authenticated_headers()
    client = TestClient(app, headers=auth_headers)

    # Case 1: Complete frozen-evidence run under concurrent disjoint + duplicate page loads.
    run_frozen = client.get(
        "/api/overview?recommendation_limit=24&decision_limit=24"
    ).json()["recommendation_run_id"]
    assert run_frozen

    orig_read_state = engine_main._read_paginated_run_state
    barrier_frozen = threading.Barrier(2, timeout=10)

    def synced_read_state_frozen(*args, **kwargs):
        res = orig_read_state(*args, **kwargs)
        barrier_frozen.wait()
        return res

    monkeypatch.setattr(engine_main, "_read_paginated_run_state", synced_read_state_frozen)

    def fetch_page(run_id: str, offset: int, limit: int):
        page_client = TestClient(app, headers=auth_headers)
        resp = page_client.get(
            f"/api/recommendations?status=recommended&limit={limit}&offset={offset}&run_id={run_id}"
        )
        assert resp.status_code == 200
        return [item["id"] for item in resp.json()]

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(fetch_page, run_frozen, 24, 4)
        f2 = pool.submit(fetch_page, run_frozen, 28, 4)
        ids_24, ids_28 = f1.result(), f2.result()

    assert ids_24 == candidate_ids[24:28]
    assert ids_28 == candidate_ids[28:32]

    barrier_dup = threading.Barrier(2, timeout=10)

    def synced_read_state_dup(*args, **kwargs):
        res = orig_read_state(*args, **kwargs)
        barrier_dup.wait()
        return res

    monkeypatch.setattr(engine_main, "_read_paginated_run_state", synced_read_state_dup)
    with ThreadPoolExecutor(max_workers=2) as pool:
        d1 = pool.submit(fetch_page, run_frozen, 32, 4)
        d2 = pool.submit(fetch_page, run_frozen, 32, 4)
        assert d1.result() == candidate_ids[32:36]
        assert d2.result() == candidate_ids[32:36]

    monkeypatch.setattr(engine_main, "_read_paginated_run_state", orig_read_state)

    frozen_imp = rows(
        "SELECT candidate_id, rank FROM recommendation_impressions WHERE run_id=? ORDER BY rank ASC",
        (run_frozen,),
    )
    assert [r["rank"] for r in frozen_imp] == list(range(1, 37))
    assert len({r["candidate_id"] for r in frozen_imp}) == 36
    frozen_exp = rows(
        "SELECT candidate_id, rank FROM recommendation_evidence_exposures WHERE run_id=? ORDER BY rank ASC",
        (run_frozen,),
    )
    assert [r["rank"] for r in frozen_exp] == list(range(1, 37))
    assert [r["candidate_id"] for r in frozen_exp] == [r["candidate_id"] for r in frozen_imp]

    # Case 2: Incomplete-evidence fallback (frozen_recommended is None) under concurrent page loads.
    run_fallback = client.get(
        "/api/overview?recommendation_limit=24&decision_limit=24"
    ).json()["recommendation_run_id"]
    with transaction() as con:
        con.execute(
            "DELETE FROM recommendation_run_evidence WHERE run_id=?",
            (run_fallback,),
        )

    barrier_fallback = threading.Barrier(2, timeout=10)

    def synced_read_state_fallback(*args, **kwargs):
        res = orig_read_state(*args, **kwargs)
        assert res[3] is None
        barrier_fallback.wait()
        return res

    monkeypatch.setattr(engine_main, "_read_paginated_run_state", synced_read_state_fallback)
    with ThreadPoolExecutor(max_workers=2) as pool:
        fb1 = pool.submit(fetch_page, run_fallback, 24, 4)
        fb2 = pool.submit(fetch_page, run_fallback, 28, 4)
        fb_ids_24, fb_ids_28 = fb1.result(), fb2.result()

    monkeypatch.setattr(engine_main, "_read_paginated_run_state", orig_read_state)

    assert fb_ids_24 == candidate_ids[24:28]
    assert fb_ids_28 == candidate_ids[28:32]
    fallback_imp = rows(
        "SELECT candidate_id, rank FROM recommendation_impressions WHERE run_id=? ORDER BY rank ASC",
        (run_fallback,),
    )
    assert [r["rank"] for r in fallback_imp] == list(range(1, 33))
    assert set(r["candidate_id"] for r in fallback_imp) == set(candidate_ids[:32])
    fallback_exp = rows(
        "SELECT candidate_id, rank FROM recommendation_evidence_exposures WHERE run_id=? ORDER BY rank ASC",
        (run_fallback,),
    )
    assert [r["rank"] for r in fallback_exp] == list(range(1, 33))
    assert [r["candidate_id"] for r in fallback_exp] == [
        r["candidate_id"] for r in fallback_imp
    ]

