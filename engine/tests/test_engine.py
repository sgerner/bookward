import asyncio
import json
import socket
from datetime import datetime, timedelta, timezone
import stat
from pathlib import Path

import pytest
import respx
import httpx
from fastapi.testclient import TestClient

from afterword_engine.config import settings
from afterword_engine.database import MIGRATIONS, initialize, row, rows, transaction
from afterword_engine.covers import (
    GOOGLE_BOOKS_SEARCH,
    OPEN_LIBRARY_SEARCH,
    canonical_book_source_url,
    fallback_cover_url,
    is_weak_cover_url,
    resolve_book_metadata,
    resolve_cover_url,
    safe_cover_url,
)
from afterword_engine.ingestion import (
    _source_client,
    enrich_book_metadata,
    enrich_goodreads_blog_items,
    fetch_bytes,
    import_goodreads_csv,
    parse_book_items,
    scan_source,
)
from afterword_engine.scoring import score_all, cached_vectors, _max_cosine_similarities
from afterword_engine.scoring import rebuild_all_embeddings
from afterword_engine.embeddings import get_embedder
from afterword_engine.secrets import seal
from afterword_engine.security import safe_error_message, validate_public_url, validate_service_url
from afterword_engine.api_tokens import hash_api_token, legacy_hash_api_token
from afterword_engine.main import (
    SOURCE_SYNC_ERROR_RETRY_SECONDS,
    app,
    handle_job,
    recommendation_list,
    score,
    source_sync_is_due,
    sync,
)
from afterword_engine.digest import digest_is_due, digest_preview, send_digest, validate_digest_config

@pytest.fixture()
def database(tmp_path: Path):
    settings.db = str(tmp_path / "test.db")
    initialize()
    return settings.db

def test_fresh_database_seeds_independent_demo(database):
    assert row("SELECT COUNT(*) count FROM candidates")["count"] == 4
    assert row("SELECT COUNT(*) count FROM reads")["count"] == 4
    covers = [item["cover_url"] for item in rows("SELECT cover_url FROM candidates")]
    assert len(covers) == 4 and all(covers) and not any(is_weak_cover_url(cover) for cover in covers)
    assert all("/isbn/" not in item["source_url"] for item in rows("SELECT source_url FROM candidates"))


def test_database_files_are_owner_only(tmp_path):
    settings.db = str(tmp_path / "private.db")
    Path(settings.db).touch(mode=0o644)
    Path(settings.db).chmod(0o644)

    initialize()
    with transaction() as con:
        con.execute("INSERT INTO reads(title,author,source) VALUES(?,?,?)", ("Private", "Reader", "test"))

    for path in (Path(settings.db), Path(f"{settings.db}-wal"), Path(f"{settings.db}-shm")):
        if path.exists():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_database_indexes_cover_recent_history_and_job_queue(database):
    indexes = {
        item["name"]
        for item in rows(
            "SELECT name FROM sqlite_master WHERE type='index' AND name IN (?,?)",
            ("idx_reads_recent", "idx_jobs_queue"),
        )
    }

    assert indexes == {"idx_reads_recent", "idx_jobs_queue"}
    history_plan = rows(
        "EXPLAIN QUERY PLAN SELECT * FROM reads "
        "ORDER BY COALESCE(read_at, created_at) DESC LIMIT 12"
    )
    queue_plan = rows(
        "EXPLAIN QUERY PLAN SELECT * FROM jobs "
        "WHERE status='queued' ORDER BY created_at LIMIT 1"
    )
    assert any("idx_reads_recent" in item["detail"] for item in history_plan)
    assert any("idx_jobs_queue" in item["detail"] for item in queue_plan)


def test_fresh_database_seeds_curated_sources(database):
    sources = rows("SELECT name,url,enabled FROM sources WHERE is_default=0")
    urls = {source["url"] for source in sources}
    assert len(sources) >= 10
    assert "https://itunes.apple.com/us/rss/topaudiobooks/limit=50/xml" in urls
    assert "https://openlibrary.org/subjects/science_fiction.json?limit=50" in urls
    assert "https://www.goodreads.com/genres/science-fiction" in urls
    assert "https://api.nytimes.com/svc/books/v3/lists/overview.json" in urls
    assert sum(source["enabled"] for source in sources) >= 8
    assert row("SELECT value FROM settings WHERE key='source_sync_interval_hours'")["value"] == "24"
    assert {source["lifecycle"] for source in rows("SELECT lifecycle FROM sources")} == {"permanent"}


def test_source_scheduler_detects_due_permanent_feeds(database):
    with transaction() as con:
        con.execute("UPDATE sources SET enabled=0")
        con.execute(
            "INSERT INTO sources(name,url,enabled,lifecycle) VALUES(?,?,1,'permanent')",
            ("Due feed", "https://example.com/due"),
        )
    assert source_sync_is_due() is True
    with transaction() as con:
        con.execute("UPDATE sources SET last_scanned_at=CURRENT_TIMESTAMP WHERE name='Due feed'")
    assert source_sync_is_due() is False


def test_expensive_job_requests_reuse_active_queue_entries(database):
    first_sync = asyncio.run(sync())
    second_sync = asyncio.run(sync())
    first_score = asyncio.run(score())
    second_score = asyncio.run(score())

    assert first_sync == second_sync
    assert first_score == second_score
    assert row("SELECT COUNT(*) count FROM jobs WHERE kind='sync'")["count"] == 1
    assert row("SELECT COUNT(*) count FROM jobs WHERE kind='score'")["count"] == 1
    with transaction() as con:
        con.execute("UPDATE settings SET value='0' WHERE key='source_sync_interval_hours'")
    assert source_sync_is_due() is False

def test_open_library_isbn_links_become_search_links():
    assert canonical_book_source_url("A Book", "A Writer", "https://openlibrary.org/isbn/9780000000000") == "https://openlibrary.org/search?title=A+Book&author=A+Writer"
    assert canonical_book_source_url("A Book", "A Writer", "https://example.com/book") == "https://example.com/book"

def test_source_parsers_handle_apple_open_library_and_goodreads_formats():
    apple = b'''<?xml version="1.0"?><feed xmlns:im="http://itunes.apple.com/rss"><entry>
      <im:name>Glass House</im:name><im:artist>Jane Reader</im:artist>
      <im:image>https://is1-ssl.mzstatic.com/cover.jpg</im:image>
      <im:releaseDate label="January 2, 2027">2027-01-02T00:00:00-07:00</im:releaseDate>
      <link href="https://books.apple.com/us/book/glass-house/id1" />
      <summary>A literary mystery.</summary><category im:id="1" term="Mystery" />
    </entry></feed>'''
    apple_items = parse_book_items(apple, "application/xml", "https://itunes.apple.com/us/rss/toppaidebooks/limit=1/xml")
    assert apple_items[0]["title"] == "Glass House"
    assert apple_items[0]["author"] == "Jane Reader"
    assert apple_items[0]["release_date"] == "2027-01-02"
    marketing_apple_items = parse_book_items(
        apple,
        "application/rss+xml",
        "https://rss.marketingtools.apple.com/api/v2/us/audio-books/top/1/audio-books.rss",
    )
    assert marketing_apple_items[0]["author"] == "Jane Reader"
    open_library = json.dumps({"works": [{"title": "A New World", "authors": [{"name": "A Writer"}], "cover_id": 42, "key": "/works/OL1W", "subject": ["Science fiction"]}]}).encode()
    open_library_items = parse_book_items(open_library, "application/json", "https://openlibrary.org/subjects/science_fiction.json?limit=1")
    assert open_library_items[0]["author"] == "A Writer"
    assert open_library_items[0]["source_url"] == "https://openlibrary.org/works/OL1W"
    goodreads = b'''<div class="coverWrapper" id="bookCover1"><img class="bookImage" src="https://i.gr-assets.com/cover.jpg" /></div>
    <script>new Tip($('bookCover1'), "<h2><a class=\\"readable bookTitle\\" href=\\"https://www.goodreads.com/book/show/1-glass-house?x=1\\">Glass House<\\/a></h2><div>by <a class=\\"authorName\\" href=\\"/author/show/1\\">Jane Reader<\\/a></div><div class=\\"addBookTipDescription\\"><span id=\\"freeTextContainer1\\">A mystery.</span></div>", {});</script>'''
    goodreads_items = parse_book_items(goodreads, "text/html", "https://www.goodreads.com/genres/mystery-thriller")
    assert goodreads_items[0]["title"] == "Glass House"
    assert goodreads_items[0]["source_url"].startswith("https://www.goodreads.com/book/show/1-glass-house")
    nyt = json.dumps({"results": {"lists": [{"books": [{"title": "NYT Pick", "author": "A Critic", "description": "A review.", "published_date": "2027-02-03"}]}]}}).encode()
    nyt_items = parse_book_items(nyt, "application/json", "https://api.nytimes.com/svc/books/v3/lists/overview.json")
    assert nyt_items[0]["title"] == "NYT Pick" and nyt_items[0]["release_date"] == "2027-02-03"


def test_source_parser_provider_dispatch_requires_matching_host_and_path():
    payload = json.dumps({"works": [{"title": "A New World", "authors": [{"name": "A Writer"}]}]}).encode()

    items = parse_book_items(
        payload,
        "application/json",
        "https://example.com/feed?source=openlibrary.org/subjects/science_fiction.json",
    )

    assert items == []


def test_source_parsers_handle_editorial_and_goodreads_blog_formats():
    editorial = b'''<section class="gh-content">
      <h3><em>Stranger Things: The Complete Scripts</em>
        <a href="https://bookshop.org/season-3">Season 3</a> and
        <a href="https://bookshop.org/season-4">Season 4</a>
        by The Duffer Brothers (December 9th)</h3>
      <h3><a href="https://store.gollancz.co.uk/loss-protocol">Loss Protocol</a>
        by Paul McAuley (February 12th)</h3>
    </section>'''
    editorial_items = parse_book_items(
        editorial,
        "text/html",
        "https://www.andrewliptak.com/sci-fi-fantasy-horror-books-february-2026-ashton-okorafor-mcauley/",
    )
    assert [(item["title"], item["author"]) for item in editorial_items] == [
        ("Stranger Things: The Complete Scripts Season 3", "The Duffer Brothers"),
        ("Stranger Things: The Complete Scripts Season 4", "The Duffer Brothers"),
        ("Loss Protocol", "Paul McAuley"),
    ]

    blog = b'''<div class="tooltipTrigger book" data-resource-id="42">
      <a href="/book/show/42-example"><img alt="Example Book (Series, #1)" src="https://i.gr-assets.com/example.jpg"></a>
    </div>
    <div class="tooltipTrigger book" data-resource-id="42">
      <a href="/book/show/42-example"><img alt="Example Book (Series, #1)" src="https://i.gr-assets.com/example.jpg"></a>
    </div>'''
    blog_items = parse_book_items(blog, "text/html", "https://www.goodreads.com/blog/show/3127")
    assert len(blog_items) == 1
    assert blog_items[0]["title"] == "Example Book (Series, #1)"
    assert blog_items[0]["author"] == "Unknown author"


@respx.mock
def test_goodreads_blog_tooltips_fill_author_and_metadata(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    respx.get("https://93.184.216.34/").mock(
        return_value=httpx.Response(200, text="home", headers={"content-type": "text/html"})
    )
    respx.get("https://93.184.216.34/tooltips").mock(
        return_value=httpx.Response(
            200,
            json={
                "tooltips": {
                    "Book.42": """<section class='tooltip'>
                      <h2><a class='readable' href='/book/show/42-example'>Example Book</a></h2>
                      by <a class='authorName'>A Writer</a>
                      <div class='bookRatingAndPublishing'>published 2026</div>
                      <div class='addBookTipDescription'><span id='freeTextContainer42'>A description.</span></div>
                    </section>"""
                }
            },
        )
    )

    async def run():
        async with _source_client() as client:
            return await enrich_goodreads_blog_items(
                [
                    {
                        "title": "Example Book (Series, #1)",
                        "author": "Unknown author",
                        "description": "",
                        "cover_url": "",
                        "source_url": "https://www.goodreads.com/book/show/42-example",
                        "release_date": None,
                        "genres": [],
                    }
                ],
                client,
            )

    item = asyncio.run(run())[0]
    assert item["title"] == "Example Book"
    assert item["author"] == "A Writer"
    assert item["description"] == "A description."
    assert item["release_date"] == "2026-01-01"


@respx.mock
def test_source_fetch_follows_and_repins_public_redirects(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    respx.get("https://93.184.216.34/list").mock(
        return_value=httpx.Response(
            302,
            headers={"location": "https://redirect.example.test/final"},
        )
    )
    final = respx.get("https://93.184.216.34/final").mock(
        return_value=httpx.Response(200, text="books", headers={"content-type": "text/plain"})
    )
    content, content_type = asyncio.run(fetch_bytes("https://books.example.test/list"))
    assert content == b"books" and content_type == "text/plain"
    assert final.calls[0].request.headers["host"] == "redirect.example.test"

@respx.mock
def test_scan_source_persists_provider_metadata(database, monkeypatch):
    url = "https://openlibrary.org/subjects/science_fiction.json?limit=1"
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    with transaction() as con:
        cursor = con.execute("INSERT INTO sources(name,url) VALUES(?,?)", ("Test source", url))
    respx.get("https://93.184.216.34/subjects/science_fiction.json?limit=1").mock(return_value=httpx.Response(200, json={"works": [{"title": "A New World", "authors": [{"name": "A Writer"}], "cover_id": 42, "key": "/works/OL1W", "subject": ["Science fiction"]}]}))
    source = row("SELECT * FROM sources WHERE id=?", (cursor.lastrowid,))
    assert asyncio.run(scan_source(source)) == 1
    candidate = row("SELECT * FROM candidates WHERE title='A New World'")
    assert json.loads(candidate["genres"]) == ["Science fiction"]
    assert candidate["source_url"] == "https://openlibrary.org/works/OL1W"


def test_empty_source_scan_preserves_existing_candidates(database, monkeypatch):
    async def empty_fetch(_url):
        return b"<html><body>temporarily unavailable</body></html>", "text/html"

    monkeypatch.setattr("afterword_engine.ingestion.fetch_bytes", empty_fetch)
    monkeypatch.setattr("afterword_engine.ingestion.parse_book_items", lambda *_args: [])
    with transaction() as con:
        cursor = con.execute(
            "INSERT INTO sources(name,url) VALUES(?,?)",
            ("Empty source", "https://example.com/empty"),
        )
        con.execute(
            "INSERT INTO candidates(title,author,source_id,status,normalized_key) VALUES(?,?,?,?,?)",
            ("Keep this book", "A Writer", cursor.lastrowid, "recommended", "keep this book a writer"),
        )

    source = row("SELECT * FROM sources WHERE id=?", (cursor.lastrowid,))
    assert asyncio.run(scan_source(source)) == 0
    assert row("SELECT status FROM candidates WHERE title='Keep this book'")["status"] == "recommended"
    assert row("SELECT last_status FROM sources WHERE id=?", (cursor.lastrowid,))["last_status"] == "empty:0"


@respx.mock
def test_one_time_source_is_imported_once_and_remains_visible(database, monkeypatch):
    url = "https://openlibrary.org/subjects/science_fiction.json?limit=1"
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    with transaction() as con:
        con.execute("UPDATE sources SET enabled=0 WHERE lifecycle='permanent'")
        cursor = con.execute(
            "INSERT INTO sources(name,url,enabled,lifecycle) VALUES(?,?,1,'one_time')",
            ("One-time shelf", url),
        )
    route = respx.get("https://93.184.216.34/subjects/science_fiction.json?limit=1").mock(
        return_value=httpx.Response(200, json={"works": [{"title": "One Visit", "authors": [{"name": "A Writer"}], "cover_id": 42, "key": "/works/OL2W"}]}),
    )
    first = asyncio.run(handle_job("sync"))
    assert first["collected"] == 1
    source = row("SELECT * FROM sources WHERE id=?", (cursor.lastrowid,))
    assert source["enabled"] == 1 and source["last_scanned_at"]
    assert row("SELECT COUNT(*) count FROM candidates WHERE title='One Visit'")["count"] == 1
    second = asyncio.run(handle_job("sync"))
    assert second["collected"] == 0 and route.call_count == 1


def test_failed_one_time_source_remains_pending_for_retry(database, monkeypatch):
    with transaction() as con:
        con.execute("UPDATE sources SET enabled=0")
        cursor = con.execute(
            "INSERT INTO sources(name,url,enabled,lifecycle) VALUES(?,?,1,'one_time')",
            ("Retry me", "https://example.com/retry"),
        )

    async def fail(_source):
        raise RuntimeError("temporary source outage at https://api.nytimes.com/v3/books?api-key=source-secret")

    monkeypatch.setattr("afterword_engine.main.scan_source", fail)
    result = asyncio.run(handle_job("sync"))
    source = row("SELECT * FROM sources WHERE id=?", (cursor.lastrowid,))
    assert len(result["errors"]) == 1
    assert source["last_status"].startswith("error:") and source["last_scanned_at"] is None
    assert "source-secret" not in source["last_status"]


def test_provider_error_messages_redact_query_and_webhook_credentials():
    message = safe_error_message(
        "request failed for https://source.example/v3/books?api-key=source-secret "
        "and https://hooks.example/api/webhooks/123/webhook-secret"
    )

    assert "source-secret" not in message
    assert "webhook-secret" not in message
    assert "source.example" in message
    assert "hooks.example" in message


@respx.mock
def test_failed_discord_delivery_does_not_persist_webhook_secret(database, monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("162.159.135.42", 443))],
    )
    secret_url = "https://discord.com/api/webhooks/123/webhook-secret"
    route = respx.post(secret_url).mock(return_value=httpx.Response(500))
    config = {
        "digest_enabled": "1",
        "digest_channels": "discord",
        "digest_minimum_score": "0",
        "digest_maximum_books": "1",
        "digest_app_url": "https://afterword.example",
        "digest_discord_webhook_url": secret_url,
    }

    result = asyncio.run(send_digest(config))
    delivery = row("SELECT error FROM notification_deliveries LIMIT 1")

    assert route.called
    assert result["status"] == "failed"
    assert result["deliveries"][0]["error"] == "Discord webhook returned HTTP 500"
    assert delivery["error"] == "Discord webhook returned HTTP 500"
    assert "webhook-secret" not in json.dumps(result)


def test_failed_permanent_source_retries_after_short_backoff(database, monkeypatch):
    with transaction() as con:
        con.execute("UPDATE sources SET enabled=0")
        cursor = con.execute(
            "INSERT INTO sources(name,url,enabled,lifecycle) VALUES(?,?,1,'permanent')",
            ("Retry permanent", "https://example.com/retry-permanent"),
        )

    async def fail(_source):
        raise RuntimeError("temporary source outage")

    monkeypatch.setattr("afterword_engine.main.scan_source", fail)
    result = asyncio.run(handle_job("sync"))
    source = row("SELECT * FROM sources WHERE id=?", (cursor.lastrowid,))
    assert len(result["errors"]) == 1
    assert source["last_status"].startswith("error:")
    assert source_sync_is_due() is False

    retry_at = datetime.now(timezone.utc) - timedelta(
        seconds=SOURCE_SYNC_ERROR_RETRY_SECONDS + 1
    )
    with transaction() as con:
        con.execute(
            "UPDATE sources SET last_scanned_at=? WHERE id=?",
            (retry_at.isoformat(), cursor.lastrowid),
        )
    assert source_sync_is_due() is True


def test_goodreads_csv_import_is_idempotent(database):
    payload = b'Book Id,Title,Author,My Rating,Date Read,ISBN13\n1,"A Book, With Comma",Writer,5,2026/01/02,"=\"9781234567890\""\n'
    assert import_goodreads_csv(payload) == 1
    assert import_goodreads_csv(payload) == 1
    assert row("SELECT COUNT(*) count FROM reads WHERE title='A Book, With Comma'")["count"] == 1

def test_local_cpu_scoring_runs_without_model_download(database):
    assert asyncio.run(score_all("local", "hashing-768")) == 4
    assert row("SELECT score FROM candidates ORDER BY score DESC LIMIT 1")["score"] > 0
    first_count = row("SELECT COUNT(*) count FROM embeddings")["count"]
    assert first_count == 8
    assert asyncio.run(score_all("local", "hashing-768")) == 4
    assert row("SELECT COUNT(*) count FROM embeddings")["count"] == first_count


def test_cached_vectors_batches_cache_reads(database, monkeypatch):
    import afterword_engine.scoring as scoring_module

    candidates = rows("SELECT * FROM candidates")
    embedder = get_embedder("local", "hashing-768")
    cache_queries = []
    original_rows = scoring_module.rows

    def capture_rows(query, params=()):
        cache_queries.append(query)
        return original_rows(query, params)

    monkeypatch.setattr(scoring_module, "rows", capture_rows)
    vectors = asyncio.run(cached_vectors(embedder, "candidate", candidates))

    assert len(vectors) == len(candidates)
    assert len(cache_queries) == 1
    assert "entity_id IN" in cache_queries[0]


def test_scoring_uses_chunked_cosine_maxima():
    result = _max_cosine_similarities(
        [[1, 0], [0, 1], [0, 0]],
        [[1, 0], [1, 1]],
        0.25,
    )

    assert result.tolist() == pytest.approx([1, 1 / 2**0.5, 0])
    assert _max_cosine_similarities([[1, 0]], [], 0.25).tolist() == [0.25]


def test_embedding_rebuild_replaces_every_stored_vector(database):
    assert asyncio.run(score_all("local", "hashing-768")) == 4
    assert row("SELECT COUNT(*) count FROM embeddings")["count"] == 8
    result = asyncio.run(handle_job("rebuild_embeddings"))
    assert result == {"embeddings": 8, "scored": 4}
    assert row("SELECT COUNT(*) count FROM embeddings")["count"] == 8

def test_failed_embedding_rebuild_keeps_previous_provider_cache(database, monkeypatch):
    with transaction() as con:
        con.execute(
            "INSERT INTO embeddings(entity_type,entity_id,backend,model,vector,dimensions,content_hash) VALUES('read',999,'legacy','old-model',?,?,?)",
            (b"\x00\x00\x00\x00", 1, "legacy-hash"),
        )

    async def unavailable(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr("afterword_engine.main.rebuild_all_embeddings", unavailable)
    with pytest.raises(RuntimeError, match="provider unavailable"):
        asyncio.run(handle_job("rebuild_embeddings"))
    assert row("SELECT COUNT(*) count FROM embeddings WHERE backend='legacy'")["count"] == 1


def test_failed_embedding_rebuild_keeps_active_provider_cache(database, monkeypatch):
    assert asyncio.run(score_all("local", "hashing-768")) == 4
    before = rows(
        "SELECT entity_type,entity_id,backend,model,vector,content_hash "
        "FROM embeddings ORDER BY entity_type,entity_id"
    )

    class BrokenEmbedder:
        name = "local"
        model = "hashing-768"

        async def embed(self, _texts):
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr(
        "afterword_engine.scoring.get_embedder",
        lambda *_args, **_kwargs: BrokenEmbedder(),
    )
    with pytest.raises(RuntimeError, match="provider unavailable"):
        asyncio.run(rebuild_all_embeddings("local", "hashing-768"))

    after = rows(
        "SELECT entity_type,entity_id,backend,model,vector,content_hash "
        "FROM embeddings ORDER BY entity_type,entity_id"
    )
    assert after == before


def test_private_source_addresses_are_rejected(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(ValueError, match="Private"):
        validate_public_url("https://books.example.test/list")

def test_api_boots_and_serves_recommendations(database):
    with TestClient(app) as client:
        assert client.get("/api/health").json()["ok"] is True
        overview = client.get("/api/overview")
        assert overview.status_code == 200
        assert len(overview.json()["recommendations"]) == 4
        saved = client.post("/api/recommendations/1/feedback", json={"action":"save"})
        assert saved.json()["status"] == "saved"
        assert client.put("/api/sources/99999/toggle").status_code == 404
        invalid = client.put("/api/settings", json={"embedding_backend":"not-real","embedding_model":"x"})
        assert invalid.status_code == 400
        schedule = client.put("/api/sources/schedule", json={"interval_hours": 168})
        assert schedule.status_code == 200
        assert client.get("/api/overview").json()["settings"]["source_sync_interval_hours"] == 168
        rebuild = client.post("/api/embeddings/rebuild")
        assert rebuild.status_code == 200 and rebuild.json()["job_id"]


def test_recommendations_filter_and_paginate_in_sql(database):
    with transaction() as con:
        con.execute("UPDATE candidates SET status='recommended', score=id")
        con.execute("UPDATE candidates SET status='saved' WHERE id=1")

    with TestClient(app) as client:
        page = client.get("/api/recommendations?status=recommended&limit=2&offset=1")
        assert page.status_code == 200
        assert len(page.json()) == 2
        assert all(item["status"] == "recommended" for item in page.json())

        saved = client.get("/api/recommendations?status=saved&limit=1")
        assert [item["id"] for item in saved.json()] == [1]

        overview = client.get("/api/overview?recommendation_limit=1")
        assert [item["status"] for item in overview.json()["recommendations"]] == [
            "recommended",
            "saved",
        ]


def test_overview_payload_reuses_one_database_connection(database, monkeypatch):
    import afterword_engine.main as main_module

    connections = []
    original_connect = main_module.connect

    def capture_connect():
        connection = original_connect()
        connections.append(connection)
        return connection

    monkeypatch.setattr(main_module, "connect", capture_connect)
    overview = main_module.overview_payload()

    assert len(overview["recommendations"]) == 4
    assert "api_tokens" in overview["settings"]
    assert len(connections) == 1


def test_settings_encrypt_and_preserve_api_keys(database):
    payload = {"embedding_backend":"local","embedding_model":"anything","embedding_url":"","embedding_api_key":"embedding-secret","librarr_url":"http://librarr:5050","librarr_api_key":"librarr-secret","librarr_media_type":"ebook"}
    with TestClient(app) as client:
        assert client.put("/api/settings", json=payload).status_code == 200
        payload["embedding_api_key"] = payload["librarr_api_key"] = ""
        assert client.put("/api/settings", json=payload).status_code == 200
    stored = row("SELECT value,secret FROM settings WHERE key='librarr_api_key'")
    assert stored["secret"] == 1 and stored["value"].startswith("fernet:") and "librarr-secret" not in stored["value"]
    assert row("SELECT value FROM settings WHERE key='librarr_media_type'")["value"] == "ebook"


def test_api_tokens_authenticate_public_api_and_can_be_revoked(database):
    with TestClient(app) as client:
        unauthorized = client.get("/api/v1/recommendations")
        assert unauthorized.status_code == 401
        assert unauthorized.headers["www-authenticate"] == "Bearer"
        schema = client.get("/openapi.json").json()
        assert schema["components"]["securitySchemes"]["HTTPBearer"] == {
            "type": "http",
            "scheme": "bearer",
        }
        assert schema["paths"]["/api/v1/recommendations"]["get"]["security"] == [
            {"HTTPBearer": []}
        ]
        cross_origin = client.options(
            "/api/v1/recommendations",
            headers={
                "Origin": "https://untrusted.example",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        assert cross_origin.headers.get("access-control-allow-origin") != "https://untrusted.example"
        created = client.post("/api/settings/api-tokens", json={"name": "Home Assistant"})
        assert created.status_code == 200
        token = created.json()["token"]
        assert token.startswith("bkw_")

        listed = client.get("/api/settings/api-tokens").json()["tokens"]
        assert listed[0]["name"] == "Home Assistant"
        assert "token" not in listed[0]
        stored = row("SELECT token_hash FROM api_tokens WHERE id=?", (created.json()["id"],))
        assert stored["token_hash"] != token

        authorized = client.get(
            "/api/v1/recommendations",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert authorized.status_code == 200
        assert len(authorized.json()) == 4
        assert row("SELECT last_used_at FROM api_tokens WHERE id=?", (created.json()["id"],))["last_used_at"]
        public_overview = client.get(
            "/api/v1/overview",
            headers={"Authorization": f"Bearer {token}"},
        ).json()
        assert "api_tokens" not in public_overview["settings"]
        assert client.get(
            "/api/v1/recommendations",
            headers={"X-API-Key": token},
        ).status_code == 200

        revoked = client.delete(f"/api/settings/api-tokens/{created.json()['id']}")
        assert revoked.status_code == 200
        assert client.get(
            "/api/v1/recommendations",
            headers={"Authorization": f"Bearer {token}"},
        ).status_code == 401


def test_legacy_api_token_hash_is_upgraded_on_first_use(database):
    with TestClient(app) as client:
        created = client.post("/api/settings/api-tokens", json={"name": "Legacy client"})
        token = created.json()["token"]
        token_id = created.json()["id"]
        with transaction() as con:
            con.execute(
                "UPDATE api_tokens SET token_hash=? WHERE id=?",
                (legacy_hash_api_token(token), token_id),
            )

        authorized = client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert authorized.status_code == 200
        assert row("SELECT token_hash FROM api_tokens WHERE id=?", (token_id,))["token_hash"] == hash_api_token(token)


def test_api_recommendation_status_filter_applies_before_limit(database):
    with transaction() as con:
        source_id = con.execute(
            "SELECT id FROM sources WHERE is_default=1 LIMIT 1"
        ).fetchone()[0]
        con.execute(
            "UPDATE candidates SET status='recommended', source_id=?, score=100",
            (source_id,),
        )
        for index in range(101):
            con.execute(
                "INSERT INTO candidates(title,author,status,score,normalized_key,source_id) "
                "VALUES(?,?,?,?,?,?)",
                (
                    f"Recommended {index}",
                    "Author",
                    "recommended",
                    100 - index,
                    f"recommended {index} author",
                    source_id,
                ),
            )
        con.execute(
            "UPDATE candidates SET status='saved' WHERE title='Recommended 100'"
        )

    with TestClient(app) as client:
        token = client.post(
            "/api/settings/api-tokens", json={"name": "Filter test"}
        ).json()["token"]
        response = client.get(
            "/api/v1/recommendations?status=saved&limit=100",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert [item["title"] for item in response.json()] == ["Recommended 100"]

def test_api_token_rejects_unbounded_and_wrong_prefix_candidates(database):
    with TestClient(app) as client:
        oversized = client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {'bkw_' + 'x' * 200}"},
        )
        wrong_prefix = client.get(
            "/api/v1/health",
            headers={"Authorization": "Bearer not-a-bookward-token"},
        )

    assert oversized.status_code == 401
    assert wrong_prefix.status_code == 401

def test_invalid_goodreads_rating_rolls_back(database):
    payload = b"Title,Author,My Rating\nValid,Writer,5\nBroken,Writer,not-a-number\n"
    with pytest.raises(ValueError): import_goodreads_csv(payload)
    assert row("SELECT COUNT(*) count FROM reads WHERE source='goodreads_csv'")["count"] == 0

def test_goodreads_only_imports_read_shelf(database):
    payload = b"Title,Author,My Rating,Exclusive Shelf\nFinished,Writer,5,read\nFuture,Writer,5,to-read\n"
    assert import_goodreads_csv(payload) == 1
    assert row("SELECT COUNT(*) count FROM reads WHERE title='Future'")["count"] == 0

def test_disabled_source_is_removed_from_discovery_and_scoring(database):
    with transaction() as con: con.execute("UPDATE sources SET enabled=0 WHERE is_default=1")
    assert recommendation_list() == []
    assert asyncio.run(score_all("local", "hashing-768")) == 0

def test_json_ld_graph_and_urls_are_normalized():
    payload = b'''<script type="application/ld+json">{"@graph":[{"@type":["Thing","Book"],"name":"Safe","author":[{"name":"Writer"}],"image":"javascript:alert(1)","datePublished":"2027-03-04T00:00:00Z"}]}</script>'''
    items = parse_book_items(payload, "text/html", "https://books.example/list")
    assert items == [{"title":"Safe","author":"Writer","description":"","cover_url":"","source_url":"https://books.example/list","release_date":"2027-03-04"}]

def test_cover_urls_are_https_and_source_or_provider_scoped():
    assert safe_cover_url("javascript:alert(1)") == ""
    assert safe_cover_url("http://covers.openlibrary.org/b/id/42-L.jpg") == ""
    assert safe_cover_url("https://covers.openlibrary.org/b/id/42-L.jpg")
    assert safe_cover_url("https://books.example/cover.jpg", "https://books.example/list")
    assert safe_cover_url("https://tracking.example/cover.jpg", "https://books.example/list") == ""
    assert fallback_cover_url("A Book", "An Author").startswith("https://placehold.co/")

@respx.mock
def test_cover_lookup_replaces_open_library_isbn_no_cover_urls():
    route = respx.get(OPEN_LIBRARY_SEARCH).mock(
        return_value=httpx.Response(200, json={"docs": [{"title": "A Book", "cover_i": 12345}]})
    )
    cover = asyncio.run(resolve_cover_url("A Book", "An Author", "https://covers.openlibrary.org/b/isbn/9780000000000-L.jpg"))
    assert cover == "https://covers.openlibrary.org/b/id/12345-L.jpg"
    assert route.called

@respx.mock
def test_cover_lookup_falls_back_to_google_books_then_placeholder():
    respx.get(OPEN_LIBRARY_SEARCH).mock(return_value=httpx.Response(200, json={"docs": []}))
    google = respx.get(GOOGLE_BOOKS_SEARCH).mock(
        return_value=httpx.Response(200, json={"items": [{"volumeInfo": {"title": "A Book", "imageLinks": {"thumbnail": "http://books.google.com/books/content?id=x"}}}]})
    )
    cover = asyncio.run(resolve_cover_url("A Book", "An Author"))
    assert cover == "https://books.google.com/books/content?id=x"
    assert google.called
    respx.reset()
    respx.get(OPEN_LIBRARY_SEARCH).mock(return_value=httpx.Response(503))
    respx.get(GOOGLE_BOOKS_SEARCH).mock(return_value=httpx.Response(503))
    assert asyncio.run(resolve_cover_url("No Match", "Nobody")).startswith("https://placehold.co/")

@respx.mock
def test_book_metadata_lookup_supplies_summary_and_year():
    route = respx.get(OPEN_LIBRARY_SEARCH).mock(
        return_value=httpx.Response(
            200,
            json={
                "docs": [
                    {
                        "title": "A Book",
                        "cover_i": 12345,
                        "first_publish_year": 1998,
                        "first_sentence": ["A quiet story about becoming brave."],
                    }
                ]
            },
        )
    )
    metadata = asyncio.run(resolve_book_metadata("A Book", "An Author"))
    assert route.called
    assert metadata["description"] == "A quiet story about becoming brave."
    assert metadata["release_date"] == "1998-01-01"
    assert metadata["date_kind"] == "year"
    assert metadata["cover_url"] == "https://covers.openlibrary.org/b/id/12345-L.jpg"


@respx.mock
def test_metadata_enrichment_reuses_duplicate_provider_lookups():
    route = respx.get(OPEN_LIBRARY_SEARCH).mock(
        return_value=httpx.Response(
            200,
            json={
                "docs": [
                    {
                        "title": "A Book",
                        "cover_i": 12345,
                        "first_publish_year": 1998,
                        "first_sentence": ["A quiet story about becoming brave."],
                    }
                ]
            },
        )
    )
    items = [
        {"title": "A Book", "author": "An Author"},
        {"title": "A Book", "author": "An Author"},
    ]

    enriched = asyncio.run(enrich_book_metadata(items))

    assert route.call_count == 1
    assert len(enriched) == 2
    assert all(item["release_date"] == "1998-01-01" for item in enriched)

def test_existing_seed_isbn_cover_urls_are_migrated(database):
    with transaction() as con:
        con.execute("UPDATE candidates SET cover_url='https://covers.openlibrary.org/b/isbn/9781668056767-L.jpg' WHERE title='The Last Contract of Isako'")
    initialize()
    assert row("SELECT cover_url FROM candidates WHERE title='The Last Contract of Isako'")["cover_url"] == "https://covers.openlibrary.org/b/id/15154431-L.jpg"

def test_service_url_only_allows_explicit_private_host(monkeypatch):
    assert validate_service_url("http://librarr:5050", {"librarr"})
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 5050))])
    with pytest.raises(ValueError, match="Private"):
        validate_service_url("http://other-service:5050", {"librarr"})

@respx.mock
def test_source_fetch_pins_the_validated_address(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    route = respx.get("https://93.184.216.34/list").mock(return_value=httpx.Response(200, text="books", headers={"content-type":"text/plain"}))
    content, _ = asyncio.run(fetch_bytes("https://books.example.test/list"))
    assert content == b"books" and route.called
    assert route.calls[0].request.headers["host"] == "books.example.test"

def test_invalid_embedding_vectors_are_rejected(database):
    class BadEmbedder:
        name, model = "bad", "bad"
        async def embed(self, texts): return [[1.0, float("nan")]] * len(texts)
    with pytest.raises(ValueError, match="invalid"):
        asyncio.run(cached_vectors(BadEmbedder(), "read", [{"id":99,"title":"Bad","author":"Vector"}]))

def test_initialize_is_versioned_and_uses_actual_builtin_source_id(tmp_path):
    settings.db = str(tmp_path / "legacy.db")
    import sqlite3
    with sqlite3.connect(settings.db) as con:
        con.execute("CREATE TABLE sources (id INTEGER PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL UNIQUE, kind TEXT NOT NULL DEFAULT 'web', enabled INTEGER NOT NULL DEFAULT 1, is_default INTEGER NOT NULL DEFAULT 0, weight REAL NOT NULL DEFAULT 1, last_status TEXT, last_scanned_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        con.execute("INSERT INTO sources(id,name,url) VALUES(7,'Existing','https://example.com')")
    initialize()
    assert row("SELECT COUNT(*) count FROM schema_migrations")["count"] == 9
    assert row(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='api_tokens'"
    )["name"] == "api_tokens"
    assert row("SELECT source_id FROM candidates LIMIT 1")["source_id"] != 1


def test_initialize_upgrades_existing_v3_database_to_api_tokens(tmp_path):
    settings.db = str(tmp_path / "v3.db")
    import sqlite3

    with sqlite3.connect(settings.db) as con:
        con.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        for version, script in MIGRATIONS[:3]:
            con.executescript(script)
            con.execute("INSERT INTO schema_migrations(version) VALUES(?)", (version,))
        source_id = con.execute(
            "INSERT INTO sources(name,url) VALUES(?,?) RETURNING id",
            ("Existing source", "https://example.com/existing"),
        ).fetchone()[0]
        con.execute(
            "INSERT INTO candidates(title,author,source_id,normalized_key) VALUES(?,?,?,?)",
            ("Existing book", "Existing author", source_id, "existing book existing author"),
        )

    initialize()
    assert row("SELECT COUNT(*) count FROM schema_migrations")["count"] == 9
    assert row("SELECT name FROM sqlite_master WHERE type='table' AND name='api_tokens'")["name"] == "api_tokens"
    assert row("SELECT title FROM candidates WHERE normalized_key=?", ("existing book existing author",))["title"] == "Existing book"

    initialize()
    assert row("SELECT COUNT(*) count FROM schema_migrations")["count"] == 9
    assert row("SELECT COUNT(*) count FROM candidates WHERE normalized_key=?", ("existing book existing author",))["count"] == 1


@respx.mock
def test_openai_compatible_normalizes_v1_and_orders_vectors():
    route = respx.post("https://embeddings.example/v1/embeddings").mock(return_value=httpx.Response(200, json={"data":[{"index":1,"embedding":[0,1]},{"index":0,"embedding":[1,0]}]}))
    embedder = get_embedder("openai-compatible", "test-model", "https://embeddings.example/v1", "secret")
    assert asyncio.run(embedder.embed(["first","second"])) == [[1,0],[0,1]]
    assert route.called
    assert route.calls[0].request.headers["authorization"] == "Bearer secret"

@respx.mock
def test_librarr_import_contract_and_idempotency(database):
    with transaction() as con:
        con.execute("UPDATE candidates SET status='saved' WHERE id=1")
        con.execute("INSERT INTO settings(key,value,secret) VALUES('librarr_url','http://librarr:5050',0)")
        con.execute("INSERT INTO settings(key,value,secret) VALUES('librarr_api_key',?,1)", (seal("test-key"),))
    route = respx.post("http://librarr:5050/api/wishlist").mock(return_value=httpx.Response(200, json={"id":"remote-1"}))
    with TestClient(app) as client:
        first = client.post("/api/recommendations/1/import")
        second = client.post("/api/recommendations/1/import")
    assert first.json()["remote_id"] == "remote-1"
    assert second.json()["duplicate"] is True
    assert route.call_count == 1
    assert route.calls[0].request.headers["x-api-key"] == "test-key"
    assert json.loads(route.calls[0].request.content)["media_type"] == "audiobook"

@respx.mock
def test_librarr_search_and_direct_download_forward_selected_media(database):
    with transaction() as con:
        con.execute("INSERT INTO settings(key,value,secret) VALUES('librarr_url','http://librarr:5050',0)")
        con.execute("INSERT INTO settings(key,value,secret) VALUES('librarr_api_key',?,1)", (seal("test-key"),))
    search_route = respx.get("http://librarr:5050/api/search").mock(return_value=httpx.Response(200, json={"results":[{"title":"A Book","author":"A Writer","id":"book-1"}]}))
    audio_search_route = respx.get("http://librarr:5050/api/search/audiobooks").mock(return_value=httpx.Response(200, json={"items":[{"title":"An Audio Book","author":"A Writer","id":"audio-1"}]}))
    download_route = respx.post("http://librarr:5050/api/download").mock(return_value=httpx.Response(200, json={"id":"download-1"}))
    audio_download_route = respx.post("http://librarr:5050/api/download/audiobook").mock(return_value=httpx.Response(200, json={"id":"audio-download-1"}))
    with TestClient(app) as client:
        found = client.get("/api/librarr/search", params={"q":"A Book","media_type":"ebook"})
        found_audio = client.get("/api/librarr/search", params={"q":"An Audio Book","media_type":"audiobook"})
        added = client.post("/api/librarr/download", json={"media_type":"ebook","result":{"title":"A Book","author":"A Writer","id":"book-1"}})
        added_audio = client.post("/api/librarr/download", json={"media_type":"audiobook","result":{"title":"An Audio Book","author":"A Writer","id":"audio-1"}})
    assert found.status_code == 200 and found.json()["results"][0]["id"] == "book-1"
    assert search_route.calls[0].request.url.params["q"] == "A Book"
    assert search_route.calls[0].request.headers["x-api-key"] == "test-key"
    assert found_audio.status_code == 200 and found_audio.json()["results"][0]["id"] == "audio-1"
    assert audio_search_route.calls[0].request.url.params["q"] == "An Audio Book"
    assert added.status_code == 200 and added.json()["result"]["id"] == "download-1"
    assert json.loads(download_route.calls[0].request.content)["id"] == "book-1"
    assert download_route.calls[0].request.headers["x-api-key"] == "test-key"
    assert added_audio.status_code == 200 and added_audio.json()["result"]["id"] == "audio-download-1"
    assert json.loads(audio_download_route.calls[0].request.content)["id"] == "audio-1"

def test_librarr_media_type_is_validated(database):
    with TestClient(app) as client:
        response = client.put("/api/settings", json={"embedding_backend":"local","embedding_model":"hashing-768","librarr_media_type":"vinyl"})
    assert response.status_code == 400


def test_digest_settings_are_safe_and_bulk_feedback_is_idempotent(database):
    with TestClient(app) as client:
        saved = client.put(
            "/api/digest/settings",
            json={
                "enabled": True,
                "channels": [],
                "day": 1,
                "time": "09:00",
                "timezone": "UTC",
                "minimum_score": 80,
                "maximum_books": 5,
                "only_new": True,
                "app_url": "https://afterword.example",
            },
        )
        assert saved.status_code == 200
        digest = client.get("/api/digest/settings").json()
        assert digest["enabled"] is True and digest["channels"] == []
        assert "discord_webhook_url" not in digest
        assert client.post("/api/recommendations/bulk-feedback", json={"ids": [1, 2, 99999], "action": "save"}).json()["updated"] == 2
        assert client.post("/api/recommendations/bulk-feedback", json={"ids": [1, 2], "action": "save"}).json()["updated"] == 2


def test_digest_scheduler_window_and_preview(database):
    from datetime import datetime, timezone

    values = {
        "digest_enabled": "1",
        "digest_channels": "discord",
        "digest_day": "1",
        "digest_time": "09:00",
        "digest_timezone": "UTC",
        "digest_minimum_score": "0",
        "digest_maximum_books": "2",
        "digest_only_new": "1",
        "digest_last_period": "",
    }
    monday = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)
    assert digest_is_due(values, monday)
    values["digest_last_period"] = "2026-W39"
    assert not digest_is_due(values, monday)
    preview = digest_preview(values)
    assert preview["count"] == 2
    with transaction() as con:
        con.execute("UPDATE sources SET enabled=0")
    assert digest_preview(values)["count"] == 0


@respx.mock
def test_discord_digest_is_idempotent_and_records_delivery(database, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("162.159.135.42", 443))])
    route = respx.post("https://discord.com/api/webhooks/123/token").mock(return_value=httpx.Response(204))
    with transaction() as con:
        con.execute("UPDATE candidates SET status='recommended',score=99")
        con.execute("UPDATE settings SET value='1' WHERE key='digest_enabled'")
        con.execute("UPDATE settings SET value='discord' WHERE key='digest_channels'")
        con.execute("UPDATE settings SET value='0' WHERE key='digest_minimum_score'")
        con.execute("UPDATE settings SET value=? WHERE key='digest_discord_webhook_url'", ("https://discord.com/api/webhooks/123/token",))
    config = {
        "digest_enabled": "1",
        "digest_channels": "discord",
        "digest_minimum_score": "0",
        "digest_maximum_books": "2",
        "digest_discord_webhook_url": "https://discord.com/api/webhooks/123/token",
        "digest_app_url": "https://afterword.example",
    }
    first = asyncio.run(send_digest(config))
    assert first["status"] == "sent" and route.call_count == 1
    discord_body = json.loads(route.calls[0].request.content)
    assert "digest=1" in discord_body["content"]
    with TestClient(app) as client:
        review = client.get("/api/digest/review", params={"period": first["period"]})
        assert review.status_code == 200 and review.json()["found"] is True and len(review.json()["ids"]) == 2
    assert row("SELECT COUNT(*) count FROM notification_deliveries WHERE status='sent'")["count"] == 1
    assert row("SELECT COUNT(*) count FROM digest_items")["count"] == 2
    second = asyncio.run(send_digest(config))
    assert second["status"] == "skipped" and route.call_count == 1
    third = asyncio.run(send_digest(config))
    assert third["status"] == "skipped" and route.call_count == 1


def test_digest_review_marks_unknown_period_as_stale(database):
    with TestClient(app) as client:
        response = client.get("/api/digest/review", params={"period": "2026-W52"})
    assert response.status_code == 200
    assert response.json() == {"period": "2026-W52", "ids": [], "found": False}


def test_partial_digest_delivery_retries_only_failed_channel(database, monkeypatch):
    import afterword_engine.digest as digest_module

    calls = {"discord": 0, "email": 0}

    async def discord(_config, _payload):
        calls["discord"] += 1
        return "discord webhook"

    async def email(_config, _subject, _body):
        calls["email"] += 1
        if calls["email"] == 1:
            raise RuntimeError("SMTP temporarily unavailable")
        return "reader@example.com"

    monkeypatch.setattr(digest_module, "_send_discord", discord)
    monkeypatch.setattr(digest_module, "_send_email", email)
    config = {
        "digest_enabled": "1",
        "digest_channels": "discord,email",
        "digest_minimum_score": "0",
        "digest_maximum_books": "2",
        "digest_app_url": "https://afterword.example",
        "digest_email_to": "reader@example.com",
        "digest_email_from": "afterword@example.com",
        "digest_smtp_host": "smtp.example.com",
        "digest_smtp_security": "none",
    }
    first = asyncio.run(send_digest(config))
    assert first["status"] == "failed"
    assert calls == {"discord": 1, "email": 1}
    assert row("SELECT value FROM settings WHERE key='digest_last_period'")["value"] == ""
    second = asyncio.run(send_digest(config))
    assert second["status"] == "sent"
    assert calls == {"discord": 1, "email": 2}
    assert row("SELECT COUNT(*) count FROM digest_items")["count"] == 2


def test_discord_webhook_cannot_be_redirected_to_an_arbitrary_host():
    with pytest.raises(ValueError, match="must point to Discord"):
        validate_digest_config(
            {
                "enabled": True,
                "channels": ["discord"],
                "day": 1,
                "time": "09:00",
                "timezone": "UTC",
                "minimum_score": 80,
                "maximum_books": 5,
                "only_new": True,
                "app_url": "https://afterword.example",
                "discord_webhook_url": "https://attacker.example/collect",
            }
        )


def test_digest_can_be_disabled_with_stale_provider_settings():
    from afterword_engine.digest import validate_digest_config

    validate_digest_config(
        {
            "enabled": False,
            "channels": ["discord", "email"],
            "day": 1,
            "time": "09:00",
            "timezone": "UTC",
            "minimum_score": 80,
            "maximum_books": 5,
            "only_new": True,
            "app_url": "https://afterword.example",
            "discord_webhook_url": "https://not-discord.example/webhook",
            "email_to": "not-an-email",
            "email_from": "",
            "smtp_host": "",
            "smtp_security": "starttls",
        }
    )


def test_digest_rejects_unknown_channels():
    from afterword_engine.digest import validate_digest_config

    with pytest.raises(ValueError, match="Unsupported digest channel"):
        validate_digest_config(
            {
                "enabled": False,
                "channels": ["pagerduty"],
                "day": 1,
                "time": "09:00",
                "timezone": "UTC",
                "minimum_score": 80,
                "maximum_books": 5,
                "only_new": True,
                "app_url": "https://afterword.example",
                "smtp_security": "starttls",
            }
        )


def test_failed_digest_delivery_has_scheduler_backoff(database):
    from datetime import datetime, timezone
    from afterword_engine.digest import period_key

    now = datetime.now(timezone.utc)
    values = {
        "digest_enabled": "1",
        "digest_channels": "discord",
        "digest_day": str(now.isoweekday()),
        "digest_time": "00:00",
        "digest_timezone": "UTC",
        "digest_last_period": "",
    }
    with transaction() as con:
        con.execute(
            "INSERT INTO notification_deliveries(id,period_key,channel,status,updated_at) VALUES(?,?,?,?,?)",
            ("failed-delivery", period_key(now), "discord", "failed", now.isoformat()),
        )
    assert digest_is_due(values, now) is False


def test_smtp_digest_uses_starttls_and_never_exposes_credentials(database, monkeypatch):
    import afterword_engine.digest as digest_module

    class FakeSMTP:
        instance = None

        def __init__(self, host, port, timeout):
            self.host, self.port, self.timeout = host, port, timeout
            self.started_tls = False
            self.logged_in = None
            self.message = None
            FakeSMTP.instance = self

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def ehlo(self):
            return None

        def starttls(self, context):
            self.started_tls = context is not None

        def login(self, username, password):
            self.logged_in = (username, password)

        def send_message(self, message):
            self.message = message

    monkeypatch.setattr(digest_module.smtplib, "SMTP", FakeSMTP)
    config = {
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_security": "starttls",
        "smtp_username": "smtp-user",
        "smtp_password": "secret-password",
        "email_from": "afterword@example.com",
        "email_to": "reader@example.com",
    }
    assert asyncio.run(digest_module._send_email(config, "Subject", "Body")) == "reader@example.com"
    assert FakeSMTP.instance.started_tls is True
    assert FakeSMTP.instance.logged_in == ("smtp-user", "secret-password")
    assert FakeSMTP.instance.message["To"] == "reader@example.com"
    assert FakeSMTP.instance.message.get_content().strip() == "Body"


def test_retry_of_corrupt_delivery_is_recorded_as_a_failure(database):
    from afterword_engine.digest import retry_delivery

    with transaction() as con:
        con.execute(
            "INSERT INTO notification_deliveries(id,period_key,channel,status,payload) VALUES(?,?,?,?,?)",
            ("bad-delivery", "2026-W39", "email", "failed", "not-json"),
        )
    result = asyncio.run(retry_delivery("bad-delivery", {"digest_channels": "email"}))
    assert result["status"] == "failed"
    assert row("SELECT error,attempts FROM notification_deliveries WHERE id='bad-delivery'") == {
        "error": "Stored delivery payload is invalid",
        "attempts": 1,
    }
