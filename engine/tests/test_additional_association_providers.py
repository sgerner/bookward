import asyncio
from pathlib import Path
import socket

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from auth_helpers import authenticated_headers

from afterword_engine.association_sources.common import (
    DEFAULT_USER_AGENT,
    fetch_pinned_association_json,
)
from afterword_engine.association_sources.google_books import (
    GOOGLE_BOOKS_PROVIDER,
    GOOGLE_BOOKS_USER_AGENT,
    GoogleBooksAssociatedProvider,
    GoogleBooksClient,
)
from afterword_engine.association_sources.librarything import (
    LIBRARYTHING_PROVIDER,
    LIBRARYTHING_USER_AGENT,
    LibraryThingClient,
    LibraryThingProvider,
    librarything_requests_today,
)
from afterword_engine.association_sources.openlibrary import (
    OPEN_LIBRARY_USER_AGENT,
    OpenLibraryClient,
)
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


def _google_provider_with_candidates(candidates):
    class FixtureClient:
        async def get_json(self, path, _params=None):
            if path == "/books/v1/volumes":
                return {
                    "items": [
                        {
                            "id": "seed-volume",
                            "volumeInfo": {
                                "title": "Seed Book",
                                "authors": ["Seed Author"],
                            },
                        }
                    ]
                }
            if path == "/books/v1/volumes/seed-volume/associated":
                return {"items": candidates}
            raise AssertionError(f"unexpected Google Books path: {path}")

    provider = GoogleBooksAssociatedProvider(client=FixtureClient(), max_seeds=1)
    reads = [{"id": 1, "title": "Seed Book", "author": "Seed Author", "rating": 5}]
    return provider, reads


def test_librarything_run_excludes_all_known_reads(database, monkeypatch):
    class FixtureClient:
        async def recommendations(self, _isbns, **_kwargs):
            return {"recommendations": [
                {"work": str(i), "isbns": [isbn]} for i, isbn in enumerate(
                    ("9782222222222", "9780306406157", "9784444444444", "9785555555555"), start=1)
            ]}

    provider = LibraryThingProvider("fixture-key", client=FixtureClient())
    metadata = {
        "9782222222222": ("Disliked Book", "B Author", "/works/OL2W"),
        "9780306406157": ("ISBN Alias", "Different Author", "/works/OL3W"),
        "9784444444444": ("Work Alias", "Different Author", "/works/OL4W"),
        "9785555555555": ("Unread Book", "D Author", "/works/OL5W"),
    }

    async def resolve(isbn):
        return metadata[isbn]

    monkeypatch.setattr(provider, "_metadata_for_isbn", resolve)
    reads = [
        {"id": 1, "title": "Loved Book", "author": "A Author", "rating": 5,
         "isbn": "9781111111111"},
        {"id": 2, "title": "Disliked Book", "author": "B Author", "rating": 1},
        {"id": 3, "title": "Another Read", "author": "C Author", "rating": None,
         "isbn": "9780306406157"},
        {"id": 4, "title": "A Different Read", "author": "E Author", "rating": None,
         "openlibrary_work_id": "/works/OL4W"},
    ]
    result = asyncio.run(run_association_provider(provider, reads))
    assert result.seeds == 1
    assert result.edges == result.persisted == 1
    assert row("SELECT c.title FROM candidates c JOIN association_evidence e "
               "ON e.candidate_id=c.id WHERE e.provider=?", (LIBRARYTHING_PROVIDER,)) == {
                   "title": "Unread Book"}


@pytest.mark.parametrize("bad_rank", ["not-a-rank", "²", "9" * 5000])
def test_librarything_ignores_malformed_recommendation_rank(database, monkeypatch, bad_rank):
    class FixtureClient:
        async def recommendations(self, _isbns, **_kwargs):
            return {
                "recommendations": [
                    {"rank": bad_rank, "work": "safe-work", "isbns": ["9782222222222"]}
                ]
            }

    provider = LibraryThingProvider("fixture-key", client=FixtureClient())

    async def resolve(_isbn):
        return "Recommended book", "Author", "/works/OL2W"

    monkeypatch.setattr(provider, "_metadata_for_isbn", resolve)
    result = asyncio.run(provider.collect([{
        "id": 1,
        "title": "Read book",
        "author": "Reader",
        "rating": 5,
        "isbn": "9781111111111",
    }]))

    assert len(result) == 1
    assert result[0].rank == 1


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


def test_google_association_keeps_same_title_with_different_known_author():
    provider, reads = _google_provider_with_candidates(
        [
            {
                "id": "different-work-volume",
                "volumeInfo": {
                    "title": "The Shared Title",
                    "authors": ["Different Author"],
                },
            }
        ]
    )
    reads.append(
        {"id": 2, "title": "The Shared Title", "author": "Read Author", "rating": 1}
    )

    result = asyncio.run(provider.collect(reads))

    assert [(item.title, item.author) for item in result] == [
        ("The Shared Title", "Different Author")
    ]


def test_google_association_excludes_same_title_and_author():
    provider, reads = _google_provider_with_candidates(
        [
            {
                "id": "same-work-new-volume",
                "volumeInfo": {
                    "title": "The Shared Title",
                    "authors": ["Read Author"],
                },
            }
        ]
    )
    reads.append(
        {"id": 2, "title": "The Shared Title", "author": "Read Author", "rating": 1}
    )

    assert asyncio.run(provider.collect(reads)) == []


def test_google_association_excludes_isbn_match_across_different_metadata():
    provider, reads = _google_provider_with_candidates(
        [
            {
                "id": "same-edition-volume",
                "volumeInfo": {
                    "title": "A Different Catalog Title",
                    "authors": ["Different Catalog Author"],
                    "industryIdentifiers": [
                        {"type": "ISBN_13", "identifier": "9780140328721"}
                    ],
                },
            }
        ]
    )
    reads.append(
        {
            "id": 2,
            "title": "Read Edition",
            "author": "Read Author",
            "isbn": "9780140328721",
            "rating": 1,
        }
    )

    assert asyncio.run(provider.collect(reads)) == []


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
    with TestClient(app, headers=authenticated_headers()) as client:
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


def test_association_user_agents_alias_default_user_agent():
    assert DEFAULT_USER_AGENT == "Bookward/0.1 (+self-hosted book recommender)"
    assert GOOGLE_BOOKS_USER_AGENT == DEFAULT_USER_AGENT
    assert OPEN_LIBRARY_USER_AGENT == DEFAULT_USER_AGENT
    assert LIBRARYTHING_USER_AGENT == DEFAULT_USER_AGENT


@respx.mock
def test_fetch_pinned_association_json_pins_ip_sets_sni_and_records_ok(database, monkeypatch):
    public_dns(monkeypatch)
    route = respx.get("https://93.184.216.34:8443/v1/items").mock(
        return_value=httpx.Response(
            200,
            headers={"Cache-Control": "public, max-age=60"},
            json={"items": [1, 2]},
        )
    )

    payload, headers = asyncio.run(
        fetch_pinned_association_json(
            "https://api.example.test:8443/v1/items",
            provider="example_provider",
            request_key="items?q=book",
            source_label="Example",
            params={"q": "book"},
            timeout_seconds=5.0,
            max_bytes=1024,
        )
    )

    assert payload == {"items": [1, 2]}
    assert headers["cache-control"] == "public, max-age=60"
    assert route.call_count == 1
    request = route.calls[0].request
    assert request.headers["host"] == "api.example.test:8443"
    assert request.headers["user-agent"] == DEFAULT_USER_AGENT
    assert request.extensions["sni_hostname"] == "api.example.test"
    assert dict(request.url.params) == {"q": "book"}
    assert rows(
        "SELECT provider, request_key, status FROM association_requests"
    ) == [
        {
            "provider": "example_provider",
            "request_key": "items?q=book",
            "status": "ok",
        }
    ]


@respx.mock
def test_fetch_pinned_association_json_dns_failure_skips_error_telemetry(database, monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))
        ],
    )
    route = respx.get("https://127.0.0.1/v1/items").mock(
        return_value=httpx.Response(200, json={})
    )

    with pytest.raises(ValueError, match="Private, loopback, and link-local"):
        asyncio.run(
            fetch_pinned_association_json(
                "https://api.example.test/v1/items",
                provider="example_provider",
                request_key="items",
                source_label="Example",
                timeout_seconds=5.0,
                max_bytes=1024,
            )
        )

    assert route.call_count == 0
    assert rows("SELECT * FROM association_requests") == []


@respx.mock
def test_fetch_pinned_association_json_enforces_guards_and_on_response_boundary(
    database, monkeypatch
):
    public_dns(monkeypatch)
    route = respx.get("https://93.184.216.34/v1/items")

    route.mock(return_value=httpx.Response(503, json={"error": "unavailable"}))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(
            fetch_pinned_association_json(
                "https://api.example.test/v1/items",
                provider="example_provider",
                request_key="status-error",
                source_label="Example",
                timeout_seconds=5.0,
                max_bytes=1024,
            )
        )

    route.mock(return_value=httpx.Response(200, content=b'{"oversized": true}'))
    with pytest.raises(ValueError, match="Example response is too large"):
        asyncio.run(
            fetch_pinned_association_json(
                "https://api.example.test/v1/items",
                provider="example_provider",
                request_key="too-large",
                source_label="Example",
                timeout_seconds=5.0,
                max_bytes=5,
            )
        )

    route.mock(return_value=httpx.Response(200, json=["not", "an", "object"]))
    with pytest.raises(ValueError, match="Example response must be an object"):
        asyncio.run(
            fetch_pinned_association_json(
                "https://api.example.test/v1/items",
                provider="example_provider",
                request_key="mapping-required",
                source_label="Example",
                timeout_seconds=5.0,
                max_bytes=1024,
                require_mapping=True,
            )
        )

    raw_payload, _ = asyncio.run(
        fetch_pinned_association_json(
            "https://api.example.test/v1/items",
            provider="example_provider",
            request_key="mapping-optional",
            source_label="Example",
            timeout_seconds=5.0,
            max_bytes=1024,
            require_mapping=False,
        )
    )
    assert raw_payload == ["not", "an", "object"]

    route.mock(return_value=httpx.Response(200, json={"ok": True}))

    def fail_on_response(_response: httpx.Response) -> None:
        raise RuntimeError("response callback failure")

    with pytest.raises(RuntimeError, match="response callback failure"):
        asyncio.run(
            fetch_pinned_association_json(
                "https://api.example.test/v1/items",
                provider="example_provider",
                request_key="callback-error",
                source_label="Example",
                timeout_seconds=5.0,
                max_bytes=1024,
                on_response=fail_on_response,
            )
        )

    recorded = {
        (item["request_key"], item["status"])
        for item in rows("SELECT request_key, status FROM association_requests")
    }
    assert recorded == {
        ("status-error", "error"),
        ("too-large", "error"),
        ("mapping-required", "error"),
        ("mapping-optional", "ok"),
        ("callback-error", "error"),
    }


@respx.mock
def test_clients_preserve_byte_caps_mapping_guards_and_google_ttl_boundary(
    database, monkeypatch
):
    import afterword_engine.association_sources.google_books as google_books_module

    public_dns(monkeypatch)
    monkeypatch.setattr(settings, "source_max_bytes", 24)

    respx.get("https://93.184.216.34/books/v1/volumes").mock(
        return_value=httpx.Response(200, json={"items": ["exceeds-twenty-four-bytes"]})
    )
    respx.get("https://93.184.216.34/search.json").mock(
        return_value=httpx.Response(200, json=["raw-list"])
    )
    respx.get("https://93.184.216.34/api/multirecommendations.php").mock(
        return_value=httpx.Response(
            200,
            json={"request": [], "recommendations": ["exceeds-twenty-four-bytes"]},
        )
    )

    google_client = GoogleBooksClient(rate_limiter=RateLimiter(0))
    with pytest.raises(ValueError, match="Google Books response is too large"):
        asyncio.run(google_client.get_json("/books/v1/volumes", {"q": "test"}))

    openlibrary_client = OpenLibraryClient(rate_limiter=RateLimiter(0))
    assert asyncio.run(openlibrary_client.get_json("/search.json")) == ["raw-list"]

    librarything_client = LibraryThingClient("lt-secret", rate_limiter=RateLimiter(0))
    lt_payload = asyncio.run(librarything_client.recommendations(["9781111111111"]))
    assert lt_payload["recommendations"] == ["exceeds-twenty-four-bytes"]

    monkeypatch.setattr(settings, "source_max_bytes", 2_000_000)
    respx.get("https://93.184.216.34/books/v1/volumes").mock(
        return_value=httpx.Response(200, json={"items": []})
    )

    def broken_ttl(_headers):
        raise ValueError("invalid cache header")

    monkeypatch.setattr(google_books_module, "_cache_ttl", broken_ttl)
    with pytest.raises(ValueError, match="invalid cache header"):
        asyncio.run(google_client.get_json("/books/v1/volumes", {"q": "ttl-check"}))

    google_statuses = [
        item["status"]
        for item in rows(
            "SELECT status FROM association_requests WHERE provider=? ORDER BY id",
            (GOOGLE_BOOKS_PROVIDER,),
        )
    ]
    assert google_statuses == ["error", "error"]
