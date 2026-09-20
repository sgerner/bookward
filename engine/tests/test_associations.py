import asyncio
from pathlib import Path
import socket

import httpx
import pytest
import respx

from afterword_engine.associations import (
    Association,
    AssociationCache,
    RateLimiter,
    persist_associations,
    run_association_provider,
)
from afterword_engine.association_sources.openlibrary import (
    OpenLibraryClient,
    OpenLibraryListProvider,
)
from afterword_engine.config import settings
from afterword_engine.database import initialize, row, transaction


@pytest.fixture()
def database(tmp_path: Path):
    settings.db = str(tmp_path / "test.db")
    initialize()
    return settings.db


@respx.mock
def test_open_library_list_graph_is_cached_and_excludes_read_seed(database, monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    search = respx.get("https://93.184.216.34/search.json").mock(
        return_value=httpx.Response(
            200,
            json={"docs": [{"key": "OL1W", "title": "Loved", "author_name": ["A Author"]}]},
        )
    )
    lists = respx.get("https://93.184.216.34/works/OL1W/lists.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "entries": [
                    {
                        "url": "/people/reader/lists/OL2L",
                        "name": "Favorites",
                        "seed_count": 3,
                    }
                ]
            },
        )
    )
    seeds = respx.get("https://93.184.216.34/people/reader/lists/OL2L/seeds.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "entries": [
                    {
                        "key": "/works/OL1W",
                        "title": "Loved",
                        "authors": [{"name": "A Author"}],
                    },
                    {
                        "key": "/works/OL3W",
                        "title": "New Recommendation",
                        "authors": [{"name": "B Author"}],
                    },
                ]
            },
        )
    )
    client = OpenLibraryClient(
        rate_limiter=RateLimiter(0), cache_ttl_seconds=3600, contact="reader@example.test"
    )
    provider = OpenLibraryListProvider(client=client, max_seeds=5, max_lists_per_seed=1)
    reads = [{"id": 1, "title": "Loved", "author": "A Author", "rating": 5, "isbn": "978-0000000001"}]

    first = asyncio.run(provider.collect(reads))
    second = asyncio.run(provider.collect(reads))

    assert [(item.title, item.author, item.external_id) for item in first] == [
        ("New Recommendation", "B Author", "/works/OL3W")
    ]
    assert [(item.title, item.author) for item in second] == [("New Recommendation", "B Author")]
    assert search.call_count == 1
    assert lists.call_count == 1
    assert seeds.call_count == 1
    assert "contact=reader@example.test" in search.calls[0].request.headers["user-agent"]
    assert seeds.calls[0].request.headers["host"] == "openlibrary.org"


@respx.mock
def test_open_library_missing_author_is_resolved_by_bounded_search(database, monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    search = respx.get("https://93.184.216.34/search.json").mock(
        side_effect=[
            httpx.Response(200, json={"docs": [{"key": "OL1W", "title": "Loved", "author_name": ["A Author"]}]}),
            httpx.Response(200, json={"docs": [{"key": "OL3W", "title": "New Recommendation", "author_name": ["B Author"]}]}),
        ]
    )
    respx.get("https://93.184.216.34/works/OL1W/lists.json").mock(
        return_value=httpx.Response(200, json={"entries": [{"url": "/people/r/lists/OL2L", "seed_count": 2}]})
    )
    respx.get("https://93.184.216.34/people/r/lists/OL2L/seeds.json").mock(
        return_value=httpx.Response(
            200,
            json={"entries": [{"key": "/works/OL3W", "title": "New Recommendation"}]},
        )
    )
    provider = OpenLibraryListProvider(
        client=OpenLibraryClient(rate_limiter=RateLimiter(0)), max_lists_per_seed=1, max_resolutions=1
    )
    result = asyncio.run(
        provider.collect([{"id": 1, "title": "Loved", "author": "A Author", "rating": 5, "isbn": "123"}])
    )
    assert len(result) == 1
    assert result[0].author == "B Author"
    assert search.call_count == 2


def test_persist_associations_is_evidence_only_and_idempotent(database):
    evidence = Association(
        provider="openlibrary_lists",
        seed_read_id=1,
        external_id="/works/OL3W",
        title="New Recommendation",
        author="B Author",
        source_url="https://openlibrary.org/works/OL3W",
        rank=2,
        metadata={"list_name": "Favorites"},
    )
    assert persist_associations([evidence]) == 1
    assert persist_associations([evidence]) == 1
    candidate = row("SELECT * FROM candidates WHERE normalized_key=?", ("new recommendation b author",))
    assert candidate is not None
    assert candidate["status"] == "new"
    assert candidate["score"] == 0
    source = row("SELECT enabled,kind FROM sources WHERE url='association://openlibrary_lists'")
    assert source == {"enabled": 0, "kind": "association"}
    assert row("SELECT COUNT(*) count FROM association_evidence") ["count"] == 1


def test_association_cache_is_bounded(database):
    cache = AssociationCache(max_entries=2)
    cache.put("test", "one", {"n": 1}, ttl_seconds=60)
    cache.put("test", "two", {"n": 2}, ttl_seconds=60)
    cache.put("test", "three", {"n": 3}, ttl_seconds=60)
    assert cache.get("test", "one") is None
    assert cache.get("test", "two") == {"n": 2}
    assert cache.get("test", "three") == {"n": 3}


def test_rate_limiter_serializes_waits_without_real_sleep():
    current = [0.0]
    delays = []

    async def sleeper(delay):
        delays.append(delay)
        current[0] += delay

    limiter = RateLimiter(1.0, clock=lambda: current[0], sleeper=sleeper)
    asyncio.run(limiter.wait())
    asyncio.run(limiter.wait())
    assert delays == [1.0]


def test_run_association_provider_records_failure(database):
    class BrokenProvider:
        provider = "broken"

        async def collect(self, _reads):
            raise RuntimeError("upstream unavailable")

    with pytest.raises(RuntimeError, match="upstream unavailable"):
        asyncio.run(run_association_provider(BrokenProvider(), []))
    failed = row("SELECT status,error FROM association_runs WHERE provider='broken'")
    assert failed["status"] == "failed"
    assert failed["error"] == "upstream unavailable"

