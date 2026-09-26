import json
from pathlib import Path

from fastapi.testclient import TestClient

from afterword_engine.config import settings
from afterword_engine.database import initialize, row, transaction
from afterword_engine.discovery_slate import diversify_discovery_slate
from afterword_engine.main import app, tracked_recommendations


def book(candidate_id, author, score):
    return {"id": candidate_id, "title": f"Book {candidate_id}", "author": author, "score": score}


def test_third_author_repeat_uses_close_candidate_with_bounded_displacement():
    recommendations = [
        book(1, "Niche author", 100),
        book(2, "Niche author", 99),
        book(3, "Niche author", 98),
        book(4, "Different author", 97),
        book(5, "Tail author", 40),
    ]

    ranked, diagnostics = diversify_discovery_slate(recommendations, slate_size=4)

    assert [item["id"] for item in ranked] == [1, 2, 4, 3, 5]
    assert [item["score"] for item in ranked] == [100, 99, 97, 98, 40]
    assert diagnostics["policy_version"] == "discovery-slate-v1"
    assert diagnostics["applied"] is True
    assert diagnostics["author_repeat_swaps"] == 1
    assert diagnostics["similarity_swaps"] == 0
    assert diagnostics["max_displacement"] == 3
    # The input and its tail are never mutated or reordered.
    assert [item["id"] for item in recommendations] == [1, 2, 3, 4, 5]


def test_low_scoring_alternative_does_not_suppress_niche_author_depth():
    recommendations = [
        book(1, "Niche author", 100),
        book(2, "Niche author", 99),
        book(3, "Niche author", 98),
        book(4, "Different author", 50),
    ]

    ranked, diagnostics = diversify_discovery_slate(recommendations)

    assert [item["id"] for item in ranked] == [1, 2, 3, 4]
    assert diagnostics["applied"] is False


def test_near_duplicate_swaps_only_when_score_floor_and_local_bound_allow_it():
    recommendations = [
        book(1, "One", 100),
        book(2, "Two", 99),
        book(3, "Three", 97),
        book(4, "Four", 96),
        book(5, "Five", 94),
    ]
    vectors = {
        1: [1.0, 0.0],
        2: [0.995, 0.1],
        3: [0.0, 1.0],
        4: [0.0, -1.0],
        5: [-1.0, 0.0],
    }

    ranked, diagnostics = diversify_discovery_slate(
        recommendations, vectors, slate_size=2
    )
    assert [item["id"] for item in ranked] == [1, 3, 2, 4, 5]
    assert diagnostics["similarity_swaps"] == 1

    bounded, bounded_diagnostics = diversify_discovery_slate(
        recommendations, vectors, slate_size=4, max_displacement=1
    )
    assert [item["id"] for item in bounded] == [1, 3, 2, 4, 5]
    assert bounded_diagnostics["similarity_swaps"] == 1
    original_position = {item["id"]: position for position, item in enumerate(recommendations)}
    assert all(
        abs(position - original_position[item["id"]]) <= 1
        for position, item in enumerate(bounded[:4])
    )

    unchanged, unchanged_diagnostics = diversify_discovery_slate(
        recommendations,
        vectors,
        slate_size=4,
        max_displacement=1,
        max_score_sacrifice=1,
    )
    assert [item["id"] for item in unchanged] == [1, 2, 3, 4, 5]
    assert unchanged_diagnostics["applied"] is False


def test_close_candidate_from_later_rank_can_enter_visible_slate():
    recommendations = [
        book(1, "One", 100),
        book(2, "Two", 99),
        book(3, "Three", 98),
        book(4, "Four", 97),
        book(5, "Five", 96),
    ]
    vectors = {
        1: [1.0, 0.0],
        2: [0.995, 0.1],
        3: [0.999, 0.01],
        4: [0.0, 1.0],
        5: [0.0, -1.0],
    }

    ranked, diagnostics = diversify_discovery_slate(
        recommendations, vectors, slate_size=2, max_displacement=2
    )
    assert [item["id"] for item in ranked] == [1, 4, 3, 2, 5]
    assert diagnostics["similarity_swaps"] == 1
    assert diagnostics["reordered_items"] == 1


def test_diversity_layer_is_deterministic_before_paginated_responses(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "db", str(tmp_path / "discovery-slate.db"))
    monkeypatch.setattr(settings, "exploration_enabled", False)
    initialize()
    with transaction() as con:
        source_id = con.execute(
            "SELECT id FROM sources WHERE is_default=1 LIMIT 1"
        ).fetchone()[0]
        con.execute("UPDATE candidates SET status='rejected'")
        ids = []
        records = [
            ("Series book one", "Series Author", 100),
            ("Series book two", "Series Author", 99),
            ("Series book three", "Series Author", 98),
            ("Other book one", "Other Author One", 97),
            ("Other book two", "Other Author Two", 96),
            ("Other book three", "Other Author Three", 95),
            ("Other book four", "Other Author Four", 94),
            ("Other book five", "Other Author Five", 93),
            ("Other book six", "Other Author Six", 92),
        ]
        for title, author, score in records:
            candidate_id = con.execute(
                "INSERT INTO candidates(title,author,score,status,source_id,normalized_key) "
                "VALUES(?,?,?,'recommended',?,?)",
                (title, author, score, source_id, f"slate test {title.casefold()}"),
            ).lastrowid
            con.execute(
                "UPDATE candidate_quality SET quality_status='accepted',metadata_confidence=1 "
                "WHERE candidate_id=?",
                (candidate_id,),
            )
            ids.append(candidate_id)

    vectors = {candidate_id: [0.0] * 9 for candidate_id in ids}
    for position, candidate_id in enumerate(ids):
        vectors[candidate_id][position] = 1.0
    monkeypatch.setattr(
        "afterword_engine.main.load_cached_candidate_vectors",
        lambda items, connection=None: vectors,
    )

    full, full_run_id = tracked_recommendations(status="recommended", limit=20)
    repeated, _ = tracked_recommendations(status="recommended", limit=20)
    first_page, _ = tracked_recommendations(status="recommended", limit=4, offset=0)
    second_page, _ = tracked_recommendations(status="recommended", limit=4, offset=4)

    full_ids = [item["id"] for item in full]
    assert full_ids == [item["id"] for item in repeated]
    assert [item["id"] for item in first_page] == full_ids[:4]
    assert [item["id"] for item in second_page] == full_ids[4:8]
    assert full_ids[2] == ids[3]
    assert full_ids[3] == ids[4]

    with TestClient(app) as client:
        api_first = client.get("/api/recommendations?status=recommended&limit=4&offset=0")
        api_second = client.get("/api/recommendations?status=recommended&limit=4&offset=4")
    assert api_first.status_code == api_second.status_code == 200
    assert [item["id"] for item in api_first.json()] == full_ids[:4]
    assert [item["id"] for item in api_second.json()] == full_ids[4:8]
    assert api_first.headers["x-bookward-recommendation-run"]
    assert api_second.headers["x-bookward-recommendation-run"]

    run = row(
        "SELECT policy_version,metadata FROM recommendation_runs WHERE id=?",
        (full_run_id,),
    )
    metadata = json.loads(run["metadata"])
    assert run["policy_version"] == "rating-kernel-recency-interaction-installation-slate-v1"
    assert metadata["discovery_slate"]["policy_version"] == "discovery-slate-v1"
    assert metadata["discovery_slate"]["author_repeat_swaps"] > 0
    assert "Series Author" not in json.dumps(metadata["discovery_slate"])
