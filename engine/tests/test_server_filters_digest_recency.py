import json
from pathlib import Path

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from auth_helpers import authenticated_headers
from afterword_engine import profiles
from afterword_engine.config import settings
from afterword_engine.database import initialize, normalize_key, row, transaction
from afterword_engine.main import api_recommendations, app, recommendations
from afterword_engine.secrets import seal


@pytest.fixture()
def database(tmp_path: Path):
    settings.db = str(tmp_path / "test.db")
    initialize()
    profiles.initialize_auth()
    return settings.db


def _seed_candidate(
    con,
    *,
    title: str,
    author: str,
    score: float,
    genres: list[str],
    description: str = "A richly drawn speculative novel.",
    status: str = "recommended",
    source_id: int = 1,
) -> int:
    key = normalize_key(title, author)
    cursor = con.execute(
        """
        INSERT INTO candidates (
            source_id, normalized_key, title, author, description, cover_url,
            release_date, date_kind, genres, source_url, score, explanation, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            key,
            title,
            author,
            description,
            "https://covers.openlibrary.org/b/id/8228691-L.jpg",
            "2026-10-01",
            "day",
            json.dumps(genres),
            f"https://openlibrary.org/works/OL{abs(hash(key)) % 900000 + 100000}W",
            score,
            json.dumps(["Strong thematic alignment"]),
            status,
        ),
    )
    candidate_id = int(cursor.lastrowid)
    con.execute(
        """
        INSERT OR REPLACE INTO candidate_quality (
            candidate_id, quality_status, quality_score, metadata_confidence, audited_at
        ) VALUES (?, 'accepted', 0.95, 0.9, CURRENT_TIMESTAMP)
        """,
        (candidate_id,),
    )
    return candidate_id


def test_overview_exposes_full_pool_genres_and_server_filters_beyond_page_one(database):
    with transaction() as con:
        con.execute("DELETE FROM candidates")
        con.execute("DELETE FROM candidate_quality")
        seeded_ids: list[int] = []
        for idx in range(1, 41):
            is_deep_target = idx == 35
            cid = _seed_candidate(
                con,
                title=f"Catalog Entry {idx:02d}" + (" Quasar Cipher" if is_deep_target else ""),
                author=f"Author {idx:02d}",
                score=float(100 - idx),
                genres=(
                    ["Xenolinguistics", "Archive Science"]
                    if is_deep_target
                    else ["Space Opera" if idx % 2 == 0 else "Epic Fantasy"]
                ),
                description=(
                    "Decodes an ancient quasar-cipher broadcast across deep space."
                    if is_deep_target
                    else f"Standard adventure volume {idx}."
                ),
            )
            seeded_ids.append(cid)
        deep_target_id = seeded_ids[34]

    with TestClient(app, headers=authenticated_headers()) as client:
        overview = client.get("/api/overview", params={"recommendation_limit": 24})
        assert overview.status_code == 200
        payload = overview.json()
        assert len(payload["recommendations"]) == 24
        assert payload["recommendations_has_more"] is True
        assert "Xenolinguistics" in payload["recommendation_genres"]
        assert "Archive Science" in payload["recommendation_genres"]
        assert all(item["id"] != deep_target_id for item in payload["recommendations"])

        by_search = client.get(
            "/api/recommendations",
            params={
                "status": "recommended",
                "limit": 8,
                "offset": 0,
                "search": "quasar-cipher",
            },
        )
        assert by_search.status_code == 200
        search_items = by_search.json()
        assert [item["title"] for item in search_items] == [
            "Catalog Entry 35 Quasar Cipher"
        ]

        by_genre = client.get(
            "/api/recommendations",
            params={
                "status": "recommended",
                "limit": 8,
                "offset": 0,
                "genre": "xenolinguistics",
            },
        )
        assert by_genre.status_code == 200
        assert [item["title"] for item in by_genre.json()] == [
            "Catalog Entry 35 Quasar Cipher"
        ]

        # Multi-page filtered pagination across 20 Space Opera books.
        collected_ids: list[int] = []
        offset = 0
        while True:
            page_res = client.get(
                "/api/recommendations",
                params={
                    "status": "recommended",
                    "limit": 8,
                    "offset": offset,
                    "genre": "Space Opera",
                },
            )
            assert page_res.status_code == 200
            batch = page_res.json()
            if not batch:
                break
            collected_ids.extend(item["id"] for item in batch)
            offset += len(batch)
        assert len(collected_ids) == 20
        assert len(set(collected_ids)) == 20


def test_filtered_recommendations_skip_llm_reranker_and_full_slate_evidence(
    database, monkeypatch
):
    import afterword_engine.llm_reranker as llm_reranker

    rerank_calls = 0

    def fake_rerank(reads, candidates, **kwargs):
        nonlocal rerank_calls
        rerank_calls += 1
        return list(candidates), {"applied": True, "reranked_count": len(candidates)}

    monkeypatch.setattr(llm_reranker, "rerank_with_llm_sync", fake_rerank)
    with transaction() as con:
        con.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES ('llm_rerank_enabled', '1')"
        )
        con.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES ('llm_rerank_api_key', ?)",
            (seal("sk-test-secret"),),
        )

    with TestClient(app, headers=authenticated_headers()) as client:
        unfiltered = client.get(
            "/api/recommendations", params={"status": "recommended", "limit": 4}
        )
        assert unfiltered.status_code == 200
        assert rerank_calls == 1

        latest_unfiltered = row(
            "SELECT e.capture_status, e.reason, e.payload_bytes "
            "FROM recommendation_run_evidence e "
            "JOIN recommendation_runs r ON r.id = e.run_id "
            "ORDER BY r.created_at DESC, r.rowid DESC LIMIT 1"
        )
        assert latest_unfiltered["capture_status"] == "complete"
        assert latest_unfiltered["payload_bytes"] > 0

        filtered = client.get(
            "/api/recommendations",
            params={"status": "recommended", "limit": 4, "search": "isako"},
        )
        assert filtered.status_code == 200
        assert len(filtered.json()) == 1
        assert rerank_calls == 1  # External LLM reranker was NOT called for filtered query

        latest_filtered = row(
            "SELECT e.capture_status, e.reason, e.payload_bytes, e.vector_count "
            "FROM recommendation_run_evidence e "
            "JOIN recommendation_runs r ON r.id = e.run_id "
            "ORDER BY r.created_at DESC, r.rowid DESC LIMIT 1"
        )
        assert latest_filtered["capture_status"] == "incomplete"
        assert latest_filtered["reason"] == "filtered_query"
        assert latest_filtered["vector_count"] == 0
        assert latest_filtered["payload_bytes"] < latest_unfiltered["payload_bytes"]

        runs_before_empty = row("SELECT COUNT(*) count FROM recommendation_runs")["count"]
        empty_filtered = client.get(
            "/api/recommendations",
            params={"status": "recommended", "limit": 4, "search": "piranesi"},
        )
        assert empty_filtered.status_code == 200
        assert empty_filtered.json() == []
        assert empty_filtered.headers.get("X-Bookward-Recommendation-Run") is None
        assert row("SELECT COUNT(*) count FROM recommendation_runs")["count"] == runs_before_empty


def test_v1_recommendations_token_and_direct_python_call_compatibility(database):
    # Direct Python call compatibility (Annotated default = None avoids FastAPI Query sentinel).
    direct_all = recommendations(Response())
    assert len(direct_all) == 4
    direct_v1 = api_recommendations(Response())
    assert len(direct_v1) == 4

    with TestClient(app, headers=authenticated_headers()) as client:
        created = client.post(
            "/api/settings/api-tokens", json={"name": "External Reader"}
        )
        assert created.status_code == 200
        token = created.json()["token"]

        unfiltered = client.get(
            "/api/v1/recommendations",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert unfiltered.status_code == 200
        assert len(unfiltered.json()) == 4

        target_title = unfiltered.json()[0]["title"]
        filtered = client.get(
            "/api/v1/recommendations",
            params={"search": target_title[:8]},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert filtered.status_code == 200
        assert any(item["title"] == target_title for item in filtered.json())


def test_digest_review_hydrates_rank_thirty_unread_picks_and_excludes_acted_books(
    database,
):
    with transaction() as con:
        con.execute("DELETE FROM candidates")
        con.execute("DELETE FROM candidate_quality")
        seeded_ids: list[int] = []
        for idx in range(1, 36):
            cid = _seed_candidate(
                con,
                title=f"Digest Pool Book {idx:02d}",
                author=f"Digest Author {idx:02d}",
                score=float(100 - idx),
                genres=["Literary" if idx != 30 else "Deep Catalog"],
            )
            seeded_ids.append(cid)

        rank_one_id = seeded_ids[0]
        shortlisted_id = seeded_ids[5]
        read_id = seeded_ids[10]
        rank_thirty_id = seeded_ids[29]
        digest_ids = [rank_one_id, shortlisted_id, read_id, rank_thirty_id]

        con.execute(
            """
            INSERT INTO notification_deliveries (
                id, period_key, channel, status, recipient, candidate_ids, attempts, sent_at
            ) VALUES ('deliv-w42', '2026-W42', 'discord', 'sent', 'webhook', ?, 1, CURRENT_TIMESTAMP)
            """,
            (json.dumps(digest_ids),),
        )

    with TestClient(app, headers=authenticated_headers()) as client:
        assert (
            client.post(
                f"/api/recommendations/{shortlisted_id}/feedback",
                json={"action": "save"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/recommendations/{read_id}/read", json={"rating": 4}
            ).status_code
            == 200
        )

        overview = client.get("/api/overview", params={"recommendation_limit": 24}).json()
        overview_ids = {item["id"] for item in overview["recommendations"]}
        assert rank_thirty_id not in overview_ids

        review = client.get("/api/digest/review", params={"period": "2026-W42"})
        assert review.status_code == 200
        review_payload = review.json()
        assert review_payload["found"] is True
        assert review_payload["ids"] == digest_ids
        assert [item["id"] for item in review_payload["items"]] == [
            rank_one_id,
            rank_thirty_id,
        ]
        assert review_payload["items"][1]["title"] == "Digest Pool Book 30"


def test_decision_archive_orders_newest_active_decision_first_and_filters_by_status(
    database,
):
    with transaction() as con:
        con.execute("DELETE FROM candidates")
        con.execute("DELETE FROM candidate_quality")
        high_score_id = _seed_candidate(
            con,
            title="High Score Earlier Pass",
            author="Alpha Writer",
            score=96.0,
            genres=["Epic Fantasy"],
        )
        deferred_id = _seed_candidate(
            con,
            title="Mid Score Deferred Book",
            author="Beta Writer",
            score=78.0,
            genres=["Cyberpunk"],
        )
        low_score_id = _seed_candidate(
            con,
            title="Low Score Latest Pass",
            author="Gamma Writer",
            score=41.0,
            genres=["Solarpunk"],
        )
        undone_id = _seed_candidate(
            con,
            title="Temporary Pass Undone",
            author="Delta Writer",
            score=88.0,
            genres=["Noir"],
        )

    with TestClient(app, headers=authenticated_headers()) as client:
        assert (
            client.post(
                f"/api/recommendations/{high_score_id}/feedback",
                json={"action": "reject"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/recommendations/{deferred_id}/feedback",
                json={"action": "maybe_later"},
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/recommendations/{low_score_id}/feedback",
                json={"action": "reject"},
            ).status_code
            == 200
        )
        temp_decision = client.post(
            f"/api/recommendations/{undone_id}/feedback",
            json={"action": "reject"},
        )
        assert temp_decision.status_code == 200
        undo_res = client.post(
            f"/api/recommendations/{undone_id}/undo",
            json={"decision_id": temp_decision.json()["decision_id"]},
        )
        assert undo_res.status_code == 200

        overview = client.get("/api/overview", params={"recommendation_limit": 24}).json()
        assert [item["id"] for item in overview["decisions"]] == [
            low_score_id,
            deferred_id,
            high_score_id,
        ]
        assert overview["decision_genres"] == [
            "Cyberpunk",
            "Epic Fantasy",
            "Solarpunk",
        ]

        all_decisions = client.get(
            "/api/recommendations", params={"status": "decisions", "limit": 10}
        ).json()
        assert [item["id"] for item in all_decisions] == [
            low_score_id,
            deferred_id,
            high_score_id,
        ]

        only_deferred = client.get(
            "/api/recommendations", params={"status": "maybe_later", "limit": 10}
        ).json()
        assert [item["id"] for item in only_deferred] == [deferred_id]

        only_rejected = client.get(
            "/api/recommendations", params={"status": "rejected", "limit": 10}
        ).json()
        assert [item["id"] for item in only_rejected] == [
            low_score_id,
            high_score_id,
        ]

        filtered_rejected = client.get(
            "/api/recommendations",
            params={"status": "rejected", "genre": "Epic Fantasy", "limit": 10},
        ).json()
        assert [item["id"] for item in filtered_rejected] == [high_score_id]

        searched_decisions = client.get(
            "/api/recommendations",
            params={"status": "decisions", "search": "gamma writer", "limit": 10},
        ).json()
        assert [item["id"] for item in searched_decisions] == [low_score_id]
