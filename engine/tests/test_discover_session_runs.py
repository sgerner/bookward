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


def test_twenty_passes_then_rank_twenty_one_save_scores_zero_top_twenty(seeded_db):
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
    assert ips_ndcg_at_k(eval_rows, 20) == 0.0


def test_ips_ndcg_at_k_scores_all_negative_runs_as_zero_and_skips_censored_runs():
    def make_row(run_id: str, rank: int, label: int | None):
        return {
            "run_id": run_id,
            "rank": rank,
            "candidate_id": f"{run_id}-{rank}",
            "impression_id": f"{run_id}-{rank}",
            "score": float(100 - rank),
            "propensity": 1.0,
            "confidence": 1.0,
            "label": label,
        }

    dataset = [
        make_row("neg-only", 1, 0),
        make_row("neg-only", 2, 0),
        *[make_row("twenty-passes", rank, 0) for rank in range(1, 21)],
        make_row("twenty-passes", 21, 1),
        make_row("pos-top", 1, 1),
        make_row("pos-top", 2, 0),
        make_row("censored-only", 1, None),
        make_row("censored-only", 2, None),
    ]
    assert ips_ndcg_at_k(dataset, 20) == pytest.approx(1.0 / 3.0)


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


def test_untracked_recommendations_skip_exploration_reordering(seeded_db, monkeypatch):
    _, candidate_ids = seeded_db
    monkeypatch.setattr(settings, "exploration_enabled", True)
    monkeypatch.setattr(settings, "exploration_epsilon", 1.0)
    monkeypatch.setattr(settings, "exploration_stable_top_k", 4)

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

        recs = client.get(
            "/api/recommendations?status=recommended&limit=24&offset=0&track_run=false"
        )
        assert recs.status_code == 200
        assert [item["id"] for item in recs.json()] == candidate_ids[:24]

    assert row("SELECT COUNT(*) count FROM recommendation_runs")["count"] == 0
    assert row("SELECT COUNT(*) count FROM recommendation_impressions")["count"] == 0


def test_load_rows_excludes_historical_non_discovery_runs_from_ndcg():
    dataset = {
        "recommendation_runs": [
            {
                "id": "discover-pos",
                "created_at": "2026-10-01T00:00:00Z",
                "metadata": json.dumps({"status": "recommended"}),
            },
            {
                "id": "discover-all-neg",
                "created_at": "2026-10-02T00:00:00Z",
                "metadata": json.dumps({"status": "recommended"}),
            },
            {
                "id": "historical-decisions",
                "created_at": "2026-10-03T00:00:00Z",
                "metadata": json.dumps({"status": "decisions"}),
            },
            {
                "id": "historical-saved",
                "created_at": "2026-10-04T00:00:00Z",
                "metadata": json.dumps({"status": "saved"}),
            },
        ],
        "recommendation_impressions": [
            {"id": 1, "run_id": "discover-pos", "candidate_id": 1, "rank": 1, "score": 95.0, "propensity": 1.0},
            {"id": 2, "run_id": "discover-all-neg", "candidate_id": 2, "rank": 1, "score": 94.0, "propensity": 1.0},
            {"id": 3, "run_id": "historical-decisions", "candidate_id": 3, "rank": 1, "score": 60.0, "propensity": 1.0},
            {"id": 4, "run_id": "historical-saved", "candidate_id": 4, "rank": 1, "score": 88.0, "propensity": 1.0},
        ],
        "recommendation_outcomes": [
            {"impression_id": 1, "label": 1.0, "label_kind": "explicit_feedback", "confidence": 1.0},
            {"impression_id": 2, "label": 0.0, "label_kind": "explicit_feedback", "confidence": 1.0},
            {"impression_id": 3, "label": 0.0, "label_kind": "explicit_feedback", "confidence": 1.0},
            {"impression_id": 4, "label": 0.0, "label_kind": "explicit_feedback", "confidence": 1.0},
        ],
    }
    loaded = load_rows(dataset)
    assert {item["run_id"] for item in loaded} == {"discover-pos", "discover-all-neg"}
    assert ips_ndcg_at_k(loaded, 20) == pytest.approx(0.5)

