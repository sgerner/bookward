import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from afterword_engine.config import settings
from afterword_engine.database import initialize, row, transaction
from afterword_engine.main import app
from afterword_engine.secrets import seal


@pytest.fixture()
def database(tmp_path):
    settings.db = str(tmp_path / "librarr.db")
    initialize()


@respx.mock
def test_search_download_shortlists_the_source_candidate(database):
    with transaction() as con:
        con.execute("UPDATE candidates SET status='recommended' WHERE id=1")
        con.execute("INSERT INTO settings(key,value,secret) VALUES('librarr_url','http://librarr:5050',0)")
        con.execute("INSERT INTO settings(key,value,secret) VALUES('librarr_api_key',?,1)", (seal("test-key"),))
    route = respx.post("http://librarr:5050/api/download").mock(
        return_value=httpx.Response(200, json={"id": "download-1"})
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/librarr/download",
            json={
                "media_type": "ebook",
                "candidate_id": 1,
                "result": {"title": "A Book", "author": "A Writer", "score": 97},
            },
        )

    assert response.status_code == 200
    assert response.json()["candidate"] == {"id": 1, "status": "saved"}
    assert row("SELECT status FROM candidates WHERE id=1")["status"] == "saved"
    assert row("SELECT COUNT(*) count FROM feedback WHERE candidate_id=1 AND action='save'")["count"] == 1
    assert row("SELECT source FROM recommendation_events WHERE candidate_id=1 AND event_type='save'")["source"] == "librarr"
    assert route.calls[0].request.headers["idempotency-key"]

