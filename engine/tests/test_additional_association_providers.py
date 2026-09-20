import asyncio
from pathlib import Path
import socket

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from afterword_engine.association_sources.google_books import (
    GoogleBooksAssociatedProvider,
    GoogleBooksClient,
)
from afterword_engine.association_sources.librarything import (
    LIBRARYTHING_PROVIDER,
    LibraryThingClient,
    LibraryThingProvider,
    librarything_requests_today,
)
from afterword_engine.association_sources.openlibrary import OpenLibraryClient
from afterword_engine.associations import RateLimiter, run_association_provider
from afterword_engine.config import settings
from afterword_engine.database import initialize, row, rows, transaction
from afterword_engine.main import app


@pytest.fixture()
def database(tmp_path: Path):
    settings.db = str(tmp_path / "test.db")
    initialize()
    return settings.db


def public_dns(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )


@respx.mock
def test_librarything_batches_isbns_resolves_titles_and_reuses_cache(database, monkeypatch):
    public_dns(monkeypatch)
    librarything = respx.get(
        "https://93.184.216.34/api/multirecommendations.php"
    ).mock(
        return_value=httpx.Response(
            200,
            json={
                "request": [{"isbn": "9781111111111", "work": "seed-work"}],
                "recommendations": [
                    {
                        "rank": 1,
                        "work": "recommended-work",
                        "fromworks": ["seed-work"],
                        "isbns": ["9782222222222"],
                    }
                ],
            },
        )
    )
    metadata = respx.get("https://93.184.216.34/search.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "docs": [
                    {
                        "key": "/works/OL22W",
                        "title": "Recommended Book",
                        "author_name": ["A New Author"],
                    }
                ]
            },
        )
    )
    client = LibraryThingClient("lt-secret", rate_limiter=RateLimiter(0))
    metadata_client = OpenLibraryClient(rate_limiter=RateLimiter(0))
    provider = LibraryThingProvider(
        "lt-secret", client=client, metadata_client=metadata_client, max_seeds=5
    )
    reads = [{
        "id": 1,
        "title": "Loved Book",
        "author": "Loved Author",
        "rating": 5,
        "isbn": "978-111-111-1111",
    }]

    first = asyncio.run(provider.collect(reads))
    second = asyncio.run(provider.collect(reads))

    assert [(item.title, item.author, item.external_id) for item in first] == [
        ("Recommended Book", "A New Author", "recommended-work")
    ]
    assert len(second) == 1
    assert librarything.call_count == 1
    assert metadata.call_count == 1
    request = librarything.calls[0].request
    assert request.url.params["apiKey"] == "lt-secret"
    assert request.url.params["isbns"] == "9781111111111"
    assert "showtitles" not in request.url.params
    assert librarything_requests_today() == 1


@respx.mock
def test_librarything_persisted_daily_quota_blocks_network(database, monkeypatch):
    public_dns(monkeypatch)
    with transaction() as con:
        for index in range(25):
            con.execute(
                "INSERT INTO association_requests(provider,request_key,status) VALUES(?,?,?)",
                (LIBRARYTHING_PROVIDER, f"request-{index}", "error"),
            )
    route = respx.get("https://93.184.216.34/api/multirecommendations.php").mock(
        return_value=httpx.Response(200, json={"request": [], "recommendations": []})
    )
    client = LibraryThingClient("lt-secret", rate_limiter=RateLimiter(0))
    with pytest.raises(RuntimeError, match="daily request limit"):
        asyncio.run(client.recommendations(["9781111111111"]))
    assert route.call_count == 0


@respx.mock
def test_google_preview_honors_cache_control_without_sqlite_persistence(database, monkeypatch):
    public_dns(monkeypatch)
    volumes = respx.get("https://93.184.216.34/books/v1/volumes").mock(
        return_value=httpx.Response(
            200,
            headers={"Cache-Control": "public, max-age=120"},
            json={
                "items": [
                    {
                        "id": "seed-volume",
                        "volumeInfo": {"title": "Loved Book", "authors": ["Loved Author"]},
                    }
                ]
            },
        )
    )
    associated = respx.get(
        "https://93.184.216.34/books/v1/volumes/seed-volume/associated"
    ).mock(
        return_value=httpx.Response(
            200,
            headers={"Cache-Control": "public, max-age=120"},
            json={
                "items": [
                    {
                        "id": "recommendation-volume",
                        "volumeInfo": {
                            "title": "Recommended Book",
                            "authors": ["A New Author"],
                            "canonicalVolumeLink": "https://books.google.com/books?id=recommendation-volume",
                        },
                    }
                ]
            },
        )
    )
    client = GoogleBooksClient(rate_limiter=RateLimiter(0))
    provider = GoogleBooksAssociatedProvider(client=client, max_seeds=1)
    reads = [{"id": 1, "title": "Loved Book", "author": "Loved Author", "rating": 5, "isbn": "9781111111111"}]

    first = asyncio.run(provider.collect(reads))
    second = asyncio.run(provider.collect(reads))

    assert len(first) == len(second) == 1
    assert first[0].title == "Recommended Book"
    assert volumes.call_count == 1
    assert associated.call_count == 1
    assert row("SELECT COUNT(*) count FROM association_cache")["count"] == 0
    assert row("SELECT COUNT(*) count FROM association_evidence")["count"] == 0


@respx.mock
def test_google_no_cache_header_fetches_again(database, monkeypatch):
    public_dns(monkeypatch)
    volumes = respx.get("https://93.184.216.34/books/v1/volumes").mock(
        return_value=httpx.Response(
            200,
            json={"items": [{"id": "seed-volume", "volumeInfo": {"title": "Loved", "authors": ["Author"]}}]},
        )
    )
    associated = respx.get("https://93.184.216.34/books/v1/volumes/seed-volume/associated").mock(
        return_value=httpx.Response(
            200,
            json={"items": [{"id": "candidate", "volumeInfo": {"title": "Candidate", "authors": ["Other"]}}]},
        )
    )
    provider = GoogleBooksAssociatedProvider(
        client=GoogleBooksClient(rate_limiter=RateLimiter(0)), max_seeds=1
    )
    reads = [{"id": 1, "title": "Loved", "author": "Author", "rating": 5}]
    asyncio.run(provider.collect(reads))
    asyncio.run(provider.collect(reads))
    assert volumes.call_count == 2
    assert associated.call_count == 2


def test_google_shadow_run_records_count_but_does_not_persist_candidates(database, monkeypatch):
    class Provider:
        provider = "google_books_associated"

        async def collect(self, _reads):
            from afterword_engine.associations import Association

            return [
                Association(
                    provider=self.provider,
                    seed_read_id=1,
                    external_id="volume",
                    title="Ephemeral",
                    author="Provider",
                )
            ]

    result = asyncio.run(run_association_provider(Provider(), [], persist=False))
    assert result.persisted == 0 and result.edges == 1
    assert row("SELECT COUNT(*) count FROM candidates")["count"] == 4
    assert row("SELECT status,edge_count FROM association_runs")["status"] == "complete"
    assert row("SELECT edge_count FROM association_runs")["edge_count"] == 1


def test_association_settings_and_run_api_are_shadow_only(database):
    with TestClient(app) as client:
        saved = client.put(
            "/api/associations/settings",
            json={
                "openlibrary_enabled": True,
                "openlibrary_contact": "reader@example.test",
                "librarything_enabled": False,
                "librarything_api_key": "",
                "google_books_api_key": "",
            },
        )
        assert saved.status_code == 200
        assert saved.json()["associations"]["openlibrary_enabled"] is True
        queued = client.post("/api/associations/openlibrary/run")
        assert queued.status_code == 200 and queued.json()["job_id"]
        preview = client.get("/api/associations/settings")
        assert preview.json()["librarything_api_key_set"] is False
