import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from auth_helpers import authenticated_headers

from afterword_engine.config import settings
from afterword_engine.database import initialize, row, rows, transaction
from afterword_engine.interaction_personalization import load_interaction_events
from afterword_engine.main import app, tracked_recommendations
from afterword_engine.secrets import seal


@pytest.fixture()
def database(tmp_path):
    settings.db = str(tmp_path / "librarr.db")
    initialize()


def _configure_librarr():
    with transaction() as con:
        con.execute("INSERT INTO settings(key,value,secret) VALUES('librarr_url','http://librarr:5050',0)")
        con.execute("INSERT INTO settings(key,value,secret) VALUES('librarr_api_key',?,1)", (seal("test-key"),))


@pytest.mark.parametrize("initial_status", ["recommended", "saved"])
@respx.mock
def test_search_download_marks_unread_candidates_imported_and_logs_one_learning_event(database, initial_status):
    _configure_librarr()
    recommendations, run_id = tracked_recommendations(include_imported=True)
    assert 1 in {item["id"] for item in recommendations}
    if initial_status != "recommended":
        with transaction() as con:
            con.execute("UPDATE candidates SET status=? WHERE id=1", (initial_status,))
    route = respx.post("http://librarr:5050/api/download").mock(
        return_value=httpx.Response(200, json={"id": "download-1"})
    )

    with TestClient(app, headers=authenticated_headers()) as client:
        response = client.post(
            "/api/librarr/download",
            json={
                "media_type": "ebook",
                "candidate_id": 1,
                "run_id": run_id,
                "result": {"title": "A Book", "author": "A Writer", "score": 97},
            },
        )
        # Simulate a browser telemetry event or format re-download on the same imported book:
        telemetry = client.post(
            "/api/telemetry/events",
            json={
                "events": [
                    {
                        "event_key": "ui-duplicate-import-1",
                        "candidate_id": 1,
                        "run_id": run_id,
                        "event_type": "librarr_import",
                        "metadata": {"result_index": 0},
                    }
                ]
            },
        )
        second_download = client.post(
            "/api/librarr/download",
            json={
                "media_type": "ebook",
                "candidate_id": 1,
                "run_id": run_id,
                "result": {"title": "A Book (EPUB)", "author": "A Writer", "score": 95},
            },
        )

    assert response.status_code == 200
    assert response.json()["candidate"] == {"id": 1, "status": "imported"}
    assert second_download.status_code == 200
    assert second_download.json()["candidate"] == {"id": 1, "status": "imported"}
    assert telemetry.status_code == 200
    assert telemetry.json()["duplicates"] == 1
    assert row("SELECT status FROM candidates WHERE id=1")["status"] == "imported"
    assert row("SELECT COUNT(*) count FROM feedback WHERE candidate_id=1 AND action='save'")["count"] == 0
    events = rows("SELECT * FROM recommendation_events WHERE candidate_id=1 AND event_type='librarr_import'")
    assert len(events) == 1
    assert events[0]["source"] == "librarr"
    assert events[0]["run_id"] == run_id
    outcome = row("SELECT * FROM recommendation_outcomes WHERE event_id=?", (events[0]["id"],))
    assert outcome["label"] == 1.0
    assert outcome["label_kind"] == "librarr_import"
    assert outcome["confidence"] == 0.7
    assert route.calls[0].request.headers["idempotency-key"]


@respx.mock
def test_search_download_does_not_overwrite_read_status(database):
    _configure_librarr()
    respx.post("http://librarr:5050/api/download").mock(
        return_value=httpx.Response(200, json={"id": "download-read"})
    )

    with TestClient(app, headers=authenticated_headers()) as client:
        marked = client.post("/api/recommendations/1/read", json={"rating": 5})
        assert marked.status_code == 200
        response = client.post(
            "/api/librarr/download",
            json={
                "media_type": "ebook",
                "candidate_id": 1,
                "result": {"title": "A Book", "author": "A Writer"},
            },
        )

    assert response.status_code == 200
    assert response.json()["candidate"] == {"id": 1, "status": "read"}
    assert row("SELECT status FROM candidates WHERE id=1")["status"] == "read"
    assert row("SELECT COUNT(*) count FROM recommendation_events WHERE candidate_id=1 AND event_type='librarr_import'")["count"] == 0


@respx.mock
def test_waitlist_import_records_idempotent_librarr_import_event_with_run_attribution(database):
    _configure_librarr()
    _, run_id = tracked_recommendations(include_imported=True)
    with transaction() as con:
        con.execute("UPDATE candidates SET status='saved' WHERE id=1")
        con.execute("UPDATE candidates SET status='saved' WHERE id=2")
    route = respx.post("http://librarr:5050/api/wishlist").mock(
        return_value=httpx.Response(200, json={"id": "wishlist-1"})
    )

    with TestClient(app, headers=authenticated_headers()) as client:
        invalid_run = client.post("/api/recommendations/1/import", json={"run_id": "missing-run-1"})
        assert invalid_run.status_code == 400
        assert route.call_count == 0

        first = client.post("/api/recommendations/1/import", json={"run_id": run_id})
        second = client.post("/api/recommendations/1/import", json={"run_id": run_id})
        empty_run = client.post("/api/recommendations/2/import", json={"run_id": ""})

    assert first.status_code == 200
    assert first.json() == {"id": 1, "status": "imported", "remote_id": "wishlist-1"}
    assert second.status_code == 200
    assert second.json() == {"id": 1, "status": "imported", "remote_id": "wishlist-1", "duplicate": True}
    assert empty_run.status_code == 200
    assert empty_run.json() == {"id": 2, "status": "imported", "remote_id": "wishlist-1"}
    assert route.call_count == 2
    assert row("SELECT status FROM candidates WHERE id=1")["status"] == "imported"
    events = rows("SELECT * FROM recommendation_events WHERE candidate_id=1 AND event_type='librarr_import'")
    assert len(events) == 1
    assert events[0]["source"] == "librarr"
    assert events[0]["run_id"] == run_id
    outcome = row("SELECT * FROM recommendation_outcomes WHERE event_id=?", (events[0]["id"],))
    assert outcome["label"] == 1.0
    assert outcome["label_kind"] == "librarr_import"
    assert outcome["confidence"] == 0.7
    fallback_event = row(
        "SELECT id,run_id FROM recommendation_events WHERE candidate_id=2 AND event_type='librarr_import'"
    )
    assert fallback_event["run_id"] is None
    fallback_outcome = row(
        "SELECT i.run_id,o.confidence FROM recommendation_outcomes o "
        "JOIN recommendation_impressions i ON i.id=o.impression_id WHERE o.event_id=?",
        (fallback_event["id"],),
    )
    assert fallback_outcome == {"run_id": run_id, "confidence": 0.7}


def test_remove_on_imported_shortlist_card_restores_to_discover_and_undo_restores_imported(database):
    _, run_id = tracked_recommendations(include_imported=True)
    with transaction() as con:
        con.execute("UPDATE candidates SET status='imported' WHERE id=1")
    reads_before = row("SELECT COUNT(*) count FROM reads")["count"]

    with TestClient(app, headers=authenticated_headers()) as client:
        removed = client.post(
            "/api/recommendations/1/feedback",
            json={"action": "restore", "run_id": run_id},
        )
        assert removed.status_code == 200
        decision_id = removed.json()["decision_id"]
        assert removed.json() == {"id": 1, "status": "recommended", "decision_id": decision_id}
        assert row("SELECT status FROM candidates WHERE id=1")["status"] == "recommended"
        assert row("SELECT COUNT(*) count FROM reads")["count"] == reads_before
        assert row("SELECT action,previous_status FROM feedback WHERE id=?", (decision_id,)) == {
            "action": "restore",
            "previous_status": "imported",
        }

        undone = client.post(
            "/api/recommendations/1/undo",
            json={"decision_id": decision_id},
        )
        assert undone.status_code == 200
        assert undone.json() == {"id": 1, "status": "imported"}
        assert row("SELECT status FROM candidates WHERE id=1")["status"] == "imported"
        assert row("SELECT COUNT(*) count FROM reads")["count"] == reads_before
        events = [item for item in load_interaction_events() if item["candidate_id"] == 1]
        assert events[0]["event_type"] == "librarr_import"


def test_recommendations_endpoints_include_imported_in_saved_and_all_and_filter_imported(database):
    with transaction() as con:
        con.execute("UPDATE candidates SET status='recommended', score=id")
        con.execute("UPDATE candidates SET status='saved', score=90 WHERE id=1")
        con.execute("UPDATE candidates SET status='imported', score=85 WHERE id=2")

    with TestClient(app, headers=authenticated_headers()) as client:
        token = client.post("/api/settings/api-tokens", json={"name": "Shortlist CLI"}).json()["token"]
        v1_headers = {"Authorization": f"Bearer {token}"}

        internal_saved = client.get("/api/recommendations?status=saved")
        v1_saved = client.get("/api/v1/recommendations?status=saved", headers=v1_headers)
        overview = client.get("/api/overview")
        internal_imported = client.get("/api/recommendations?status=imported")
        v1_imported = client.get("/api/v1/recommendations?status=imported", headers=v1_headers)
        internal_all = client.get("/api/recommendations?status=all")
        v1_all = client.get("/api/v1/recommendations?status=all", headers=v1_headers)

    overview_shortlist = [
        (item["id"], item["status"])
        for item in overview.json()["recommendations"]
        if item["status"] in {"saved", "imported"}
    ]
    assert internal_saved.status_code == 200
    assert v1_saved.status_code == 200
    assert [(item["id"], item["status"]) for item in internal_saved.json()] == [(1, "saved"), (2, "imported")]
    assert [(item["id"], item["status"]) for item in v1_saved.json()] == overview_shortlist

    assert internal_imported.status_code == 200
    assert v1_imported.status_code == 200
    assert [(item["id"], item["status"]) for item in internal_imported.json()] == [(2, "imported")]
    assert [(item["id"], item["status"]) for item in v1_imported.json()] == [(2, "imported")]

    assert internal_all.status_code == 200
    assert v1_all.status_code == 200
    assert {1, 2} <= {item["id"] for item in internal_all.json()}
    assert {1, 2} <= {item["id"] for item in v1_all.json()}
