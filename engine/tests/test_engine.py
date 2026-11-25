import asyncio
import json
import socket
from datetime import datetime, timedelta, timezone
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
import respx
import httpx
import numpy as np
from fastapi.testclient import TestClient

from auth_helpers import authenticated_headers

from afterword_engine.config import settings
from afterword_engine import database as engine_database, main
from afterword_engine.database import MIGRATIONS, initialize, normalize_key, row, rows, transaction
from afterword_engine.covers import (
    GOOGLE_BOOKS_SEARCH,
    OPEN_LIBRARY_SEARCH,
    canonical_book_source_url,
    fallback_cover_url,
    is_weak_cover_url,
    resolve_book_metadata,
    safe_cover_url,
)
from afterword_engine.ingestion import (
    _nyt_request_url,
    _source_client,
    discover_feed_urls,
    enrich_book_metadata,
    enrich_goodreads_blog_items,
    fetch_bytes,
    filter_source_items,
    import_csv,
    import_goodreads_csv,
    import_goodreads_rss,
    normalize_source_filters,
    normalize_stored_candidate_subjects,
    parse_book_items,
    preview_source,
    refresh_missing_candidate_metadata,
    scan_source,
)
from afterword_engine import ingestion
from afterword_engine import profiles
from afterword_engine.scoring import score_all, cached_vectors, _max_cosine_similarities
from afterword_engine.scoring import rebuild_all_embeddings
from afterword_engine.embeddings import get_embedder
from afterword_engine.secrets import seal
from afterword_engine.security import safe_error_message, validate_public_url, validate_service_url
from afterword_engine.api_tokens import hash_api_token, legacy_hash_api_token
from afterword_engine.quality import has_candidate_quality_audit_candidates
from afterword_engine.main import (
    GOODREADS_RSS_MAX_ATTEMPTS,
    SOURCE_SYNC_ERROR_RETRY_SECONDS,
    app,
    handle_job,
    queue_goodreads_rss_if_due,
    recommendation_list,
    safe_settings,
    score,
    source_sync_due_sources,
    source_sync_is_due,
    sync,
    tracked_recommendations,
)
from afterword_engine.digest import digest_is_due, digest_preview, send_digest, validate_digest_config

@pytest.fixture()
def database(tmp_path: Path):
    settings.db = str(tmp_path / "test.db")
    initialize()
    profiles.initialize_auth()
    return settings.db

def test_fresh_database_seeds_independent_demo(database):
    assert row("SELECT COUNT(*) count FROM candidates")["count"] == 4
    assert row("SELECT COUNT(*) count FROM reads")["count"] == 4
    covers = [item["cover_url"] for item in rows("SELECT cover_url FROM candidates")]
    assert len(covers) == 4 and all(covers) and not any(is_weak_cover_url(cover) for cover in covers)
    assert all("/isbn/" not in item["source_url"] for item in rows("SELECT source_url FROM candidates"))
    _assert_retired_llm_removed()
    assert [item["version"] for item in rows("SELECT version FROM schema_migrations ORDER BY version")] == [
        version for version, _ in MIGRATIONS
    ]


def _assert_retired_llm_removed():
    tables = {
        item["name"]
        for item in rows(
            "SELECT name FROM sqlite_master WHERE type='table' AND name GLOB 'llm_*'"
        )
    }
    assert not tables
    assert row(
        "SELECT key FROM settings WHERE key GLOB 'llm_*' OR key GLOB 'models_catalog*' LIMIT 1"
    ) is None


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
        "ORDER BY COALESCE(read_at, created_at) DESC,id DESC LIMIT 12"
    )
    queue_plan = rows(
        "EXPLAIN QUERY PLAN SELECT * FROM jobs "
        "WHERE status='queued' ORDER BY created_at LIMIT 1"
    )
    assert any("idx_reads_recent" in item["detail"] for item in history_plan)
    assert any("idx_jobs_queue" in item["detail"] for item in queue_plan)


def test_database_indexes_cover_goodreads_shelf_deletes(database):
    indexes = {
        item["name"]
        for item in rows(
            "SELECT name FROM sqlite_master WHERE type='index' AND name IN (?,?)",
            ("idx_goodreads_shelf_items_shelf", "idx_goodreads_shelf_items_title_author"),
        )
    }

    assert indexes == {
        "idx_goodreads_shelf_items_shelf",
        "idx_goodreads_shelf_items_title_author",
    }
    delete_by_book_plan = rows(
        "EXPLAIN QUERY PLAN DELETE FROM goodreads_shelf_items WHERE title=? AND author=?",
        ("Project Hail Mary", "Andy Weir"),
    )
    delete_by_shelf_plan = rows(
        "EXPLAIN QUERY PLAN DELETE FROM goodreads_shelf_items WHERE shelf=?",
        ("to-read",),
    )
    assert any(
        "SEARCH goodreads_shelf_items USING INDEX idx_goodreads_shelf_items_title_author"
        in item["detail"]
        for item in delete_by_book_plan
    )
    assert any(
        "SEARCH goodreads_shelf_items USING INDEX idx_goodreads_shelf_items_shelf"
        in item["detail"]
        for item in delete_by_shelf_plan
    )


def test_startup_quality_audit_ignores_rejected_legacy_candidates(database):
    candidate_id = row("SELECT id FROM candidates ORDER BY id LIMIT 1")["id"]
    with transaction() as con:
        con.execute("UPDATE candidates SET status='rejected' WHERE id=?", (candidate_id,))
        con.execute(
            "UPDATE candidate_quality SET audit_version='legacy-pending-audit-v1' "
            "WHERE candidate_id=?",
            (candidate_id,),
        )

    assert has_candidate_quality_audit_candidates() is False

    with transaction() as con:
        con.execute("UPDATE candidates SET status='new' WHERE id=?", (candidate_id,))

    assert has_candidate_quality_audit_candidates() is True


def test_fresh_database_seeds_curated_sources(database):
    sources = rows("SELECT name,url,enabled FROM sources WHERE is_default=0")
    urls = {source["url"] for source in sources}
    assert len(sources) >= 10
    assert "https://itunes.apple.com/us/rss/topaudiobooks/limit=50/xml" in urls
    assert "https://openlibrary.org/subjects/science_fiction.json?limit=50" in urls
    assert "https://www.goodreads.com/genres/science-fiction" in urls
    assert "https://www.publishersweekly.com/pw/reviews/starred.html" in urls
    assert "https://www.penguinrandomhouse.com/books/new-releases/" in urls
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


def test_source_scheduler_ignores_association_provider_records(database):
    stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    with transaction() as con:
        con.execute("UPDATE sources SET enabled=0")
        cursor = con.execute(
            "INSERT INTO sources(name,url,kind,enabled,lifecycle,last_status,last_scanned_at) "
            "VALUES(?,?,'association',1,'permanent','error:Sources must use HTTPS',?)",
            ("Association · Open Library", "association://openlibrary_lists", stale),
        )

    assert source_sync_is_due() is False
    assert asyncio.run(handle_job(f"source:{cursor.lastrowid}")) == {
        "skipped": "managed_by_association_provider",
        "source_id": cursor.lastrowid,
    }


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
    trending = json.dumps(
        {
            "query": "/trending/weekly",
            "works": [
                {
                    "key": "/works/OL17930368W",
                    "title": "Atomic Habits",
                    "author_name": ["James Clear"],
                    "cover_i": 12539702,
                    "first_publish_year": 2018,
                    "subject": ["Self-Help"],
                }
            ],
        }
    ).encode()
    trending_items = parse_book_items(
        trending, "application/json", "https://openlibrary.org/trending/weekly"
    )
    assert trending_items[0]["author"] == "James Clear"
    assert trending_items[0]["release_date"] == "2018-01-01"
    assert trending_items[0]["date_kind"] == "year"
    assert trending_items[0]["source_url"] == "https://openlibrary.org/works/OL17930368W"
    assert trending_items[0]["genres"] == ["Self-Help"]
    goodreads = b'''<div class="coverWrapper" id="bookCover1"><img class="bookImage" src="https://i.gr-assets.com/cover.jpg" /></div>
    <script>new Tip($('bookCover1'), "<h2><a class=\\"readable bookTitle\\" href=\\"https://www.goodreads.com/book/show/1-glass-house?x=1\\">Glass House<\\/a></h2><div>by <a class=\\"authorName\\" href=\\"/author/show/1\\">Jane Reader<\\/a></div><div class=\\"addBookTipDescription\\"><span id=\\"freeTextContainer1\\">A mystery.</span></div>", {});</script>'''
    goodreads_items = parse_book_items(goodreads, "text/html", "https://www.goodreads.com/genres/mystery-thriller")
    assert goodreads_items[0]["title"] == "Glass House"
    assert goodreads_items[0]["source_url"].startswith("https://www.goodreads.com/book/show/1-glass-house")
    nyt = json.dumps({"results": {"lists": [{"books": [{"title": "NYT Pick", "author": "A Critic", "description": "A review.", "published_date": "2027-02-03"}]}]}}).encode()
    nyt_items = parse_book_items(nyt, "application/json", "https://api.nytimes.com/svc/books/v3/lists/overview.json")
    assert nyt_items[0]["title"] == "NYT Pick" and nyt_items[0]["release_date"] == "2027-02-03"


def test_nyt_parser_preserves_valid_payload_and_amazon_isbns():
    nyt = json.dumps(
        {
            "results": {
                "books": [
                    {
                        "title": "Payload ISBN",
                        "author": "A Writer",
                        "primary_isbn13": "9780307474278",
                        "primary_isbn10": "0307474275",
                        "amazon_product_url": "https://www.amazon.com/dp/0307474275",
                    },
                    {
                        "title": "URL ISBN",
                        "author": "Another Writer",
                        "primary_isbn13": "not-an-isbn",
                        "amazon_product_url": "https://www.amazon.com/URL-ISBN/dp/0061120081?tag=bookward",
                    },
                ]
            }
        }
    ).encode()

    items = parse_book_items(
        nyt,
        "application/json",
        "https://api.nytimes.com/svc/books/v3/lists/overview.json",
    )

    assert items[0]["isbn13"] == "9780307474278"
    assert items[0]["isbn10"] == "0307474275"
    assert items[1]["isbn13"] == "9780061120084"
    assert items[1]["isbn10"] == "0061120081"


def test_source_parser_provider_dispatch_requires_matching_host_and_path():
    payload = json.dumps({"works": [{"title": "A New World", "authors": [{"name": "A Writer"}]}]}).encode()

    items = parse_book_items(
        payload,
        "application/json",
        "https://example.com/feed?source=openlibrary.org/subjects/science_fiction.json",
    )

    assert items == []


def test_generic_source_parser_walks_nested_jsonld_and_normalizes_dates():
    payload = {
        "@context": "https://schema.org",
        "@type": "WebPage",
        "mainEntity": {
            "@type": "Book",
            "name": "Nested Book",
            "author": [{"@type": "Person", "name": "A Writer"}],
            "description": "<p>A nested description.</p>",
            "image": {"@type": "ImageObject", "url": "/covers/nested.jpg"},
            "url": "/books/nested-book",
            "datePublished": "2025-03",
            "genre": ["Science fiction", "Adventure"],
        },
    }
    items = parse_book_items(
        json.dumps(payload).encode(),
        "application/ld+json",
        "https://publisher.example/books/nested-book",
    )

    assert items == [
        {
            "title": "Nested Book",
            "author": "A Writer",
            "description": "A nested description.",
            "cover_url": "https://publisher.example/covers/nested.jpg",
            "source_url": "https://publisher.example/books/nested-book",
            "release_date": "2025-03-01",
            "date_kind": "month",
            "genres": ["Science fiction", "Adventure"],
        }
    ]


def test_generic_source_parser_handles_semantic_cards_and_publisher_heading_pairs():
    cards = b'''<article class="book-blurb"><div class="blurb-details">
      <a href="/books/guardian"><h2 class="book-title">A Guardian and a Thief</h2></a>
      <span class="author-name">by Megha Majumdar</span>
      <div class="genres"><span class="genre-tag">Fiction</span></div>
      <div class="blurb-content"><p>A story about loyalty and theft.</p><span class="byline">- A Reviewer</span></div>
      <img data-lazy-src="/covers/guardian.jpg" src="data:image/svg+xml;base64,placeholder">
    </div></article>'''
    card_items = parse_book_items(cards, "text/html", "https://bookriot.example/best-books-of-2025/")
    assert card_items == [
        {
            "title": "A Guardian and a Thief",
            "author": "Megha Majumdar",
            "description": "A story about loyalty and theft.",
            "cover_url": "https://bookriot.example/covers/guardian.jpg",
            "source_url": "https://bookriot.example/books/guardian",
            "release_date": None,
            "date_kind": "",
            "genres": ["Fiction"],
        }
    ]

    missing_author = b'''<article class="book-blurb"><h2 class="book-title">Missing Author</h2></article>''' + cards
    bounded_items = parse_book_items(missing_author, "text/html", "https://bookriot.example/best-books-of-2025/")
    assert len(bounded_items) == 1 and bounded_items[0]["title"] == "A Guardian and a Thief"

    microdata = b'''<div itemscope itemtype="https://schema.org/Book">
      <h2 itemprop="name">A Microdata Book</h2>
      <span itemprop="author">A Microdata Writer</span>
      <a itemprop="url" href="/books/microdata"></a>
      <meta itemprop="image" content="/covers/microdata.jpg">
      <time itemprop="datePublished" datetime="2027">2027</time>
    </div>'''
    microdata_items = parse_book_items(microdata, "text/html", "https://publisher.example/list")
    assert microdata_items == [
        {
            "title": "A Microdata Book",
            "author": "A Microdata Writer",
            "description": "",
            "cover_url": "https://publisher.example/covers/microdata.jpg",
            "source_url": "https://publisher.example/books/microdata",
            "release_date": "2027-01-01",
            "date_kind": "year",
            "genres": [],
        }
    ]

    publisher = b'''<section>
      <h3><a href="/books/call-and-response">Call and Response: Stories of the Fantastic</a></h3>
      <h4>Christopher Caldwell. Neon Hemlock, $28 (188p) ISBN 978-1-966503-14-9</h4>
      <p>A collection of fantastic stories.</p>
    </section>'''
    publisher_items = parse_book_items(publisher, "text/html", "https://publisher.example/books")
    assert publisher_items == [
        {
            "title": "Call and Response: Stories of the Fantastic",
            "author": "Christopher Caldwell",
            "description": "A collection of fantastic stories.",
            "cover_url": "",
            "source_url": "https://publisher.example/books/call-and-response",
            "isbn13": "9781966503149",
            "release_date": None,
            "date_kind": "",
            "genres": [],
        }
    ]

    contributor_markup = b'''<div class="contrib-wrap">
      <h2 class="contributors isbn-related 9780000000000 show">By <a href="/authors/writer">A Writer</a></h2>
      <h2 class="contributors isbn-related 9780000000001">By <a href="/authors/writer">A Writer</a></h2>
    </div>'''
    assert parse_book_items(contributor_markup, "text/html", "https://publisher.example/books/example") == []
    assert parse_book_items(b"<h2>About the author</h2><p>By A Writer</p>", "text/html", "https://publisher.example/about") == []


def test_generic_source_parser_keeps_distinct_unknown_author_books_and_prefers_precise_dates():
    payload = json.dumps([
        {
            "@type": "Book",
            "name": "Same Title",
            "url": "/books/one",
            "datePublished": "2027",
        },
        {
            "@type": "Book",
            "name": "Same Title",
            "url": "/books/two",
            "datePublished": "2027-03-04",
        },
    ]).encode()
    items = parse_book_items(payload, "application/json", "https://publisher.example/list")
    assert len(items) == 2
    assert [item["source_url"] for item in items] == [
        "https://publisher.example/books/one",
        "https://publisher.example/books/two",
    ]
    assert [item["release_date"] for item in items] == ["2027-01-01", "2027-03-04"]

    duplicate_payload = json.dumps([
        {"@type": "Book", "name": "One Book", "author": "A Writer", "url": "/books/one", "datePublished": "2027"},
        {"@type": "Book", "name": "One Book", "author": "A Writer", "url": "/books/one", "datePublished": "2027-03-04"},
    ]).encode()
    duplicate_items = parse_book_items(duplicate_payload, "application/json", "https://publisher.example/list")
    assert len(duplicate_items) == 1 and duplicate_items[0]["release_date"] == "2027-03-04"

    invalid_date = json.dumps(
        {"@type": "Book", "name": "Invalid Date", "author": "A Writer", "datePublished": "2027-02-30"}
    ).encode()
    invalid_items = parse_book_items(invalid_date, "application/json", "https://publisher.example/list")
    assert invalid_items[0]["release_date"] is None and invalid_items[0]["date_kind"] == ""


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


def test_discover_feed_urls_rejects_non_default_ports():
    html_doc = b"""
    <html><head>
      <link rel="alternate" type="application/rss+xml" href="https://newsletter.example.test:8443/feed.xml" />
      <link rel="alternate" type="application/rss+xml" href="https://newsletter.example.test:443/default.xml" />
      <link rel="alternate" type="application/rss+xml" href="https://newsletter.example.test/plain.xml" />
    </head></html>
    """
    assert discover_feed_urls(html_doc, "https://newsletter.example.test/archive/") == [
        "https://newsletter.example.test/default.xml",
        "https://newsletter.example.test/plain.xml",
    ]


def test_discover_feed_urls_resolves_relative_links_deduplicates_and_deprioritizes_comments():
    html_doc = b"""
    <html>
      <head>
        <link rel="stylesheet" href="/site.css" />
        <link rel="alternate" type="text/xml+oembed" href="/oembed.xml" />
        <link rel="alternate" type="application/rss+xml" title="Comments Feed" href="/comments/feed.xml#latest" />
        <link rel="alternate" type="application/rss+xml" href="http://newsletter.example.test/insecure.xml" />
        <link rel="alternate" type="application/rss+xml" href="https://localhost/local.xml" />
        <link rel="alternate" type="application/rss+xml" href="https://127.0.0.1/loopback.xml" />
        <link rel="alternate" type="application/rss+xml" href="/archive/index.html#self" />
        <link rel="alternate" type="application/rss+xml" href="https://newsletter.example.test/#root" />
        <link rel="alternate" type="application/rss+xml; charset=utf-8" title="Main RSS" href="feed.xml#top" />
        <link rel="alternate" type="application/rss+xml" href="https://newsletter.example.test/archive/feed.xml" />
        <link rel="ALTERNATE" type="APPLICATION/ATOM+XML" title="Atom Feed" href="../feeds/books.atom#entries" />
      </head>
    </html>
    """
    discovered = discover_feed_urls(
        html_doc,
        "https://newsletter.example.test/archive/index.html",
        original_url="https://newsletter.example.test",
    )
    assert discovered == [
        "https://newsletter.example.test/archive/feed.xml",
        "https://newsletter.example.test/feeds/books.atom",
        "https://newsletter.example.test/comments/feed.xml",
    ]


@respx.mock
def test_preview_source_autodiscovers_atom_after_redirect_and_prioritizes_populated_feed(
    monkeypatch,
):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    respx.get("https://93.184.216.34/").mock(
        return_value=httpx.Response(
            302,
            headers={"location": "https://www.newsletter.example.test/blog/"},
        )
    )
    respx.get("https://93.184.216.34/blog/").mock(
        return_value=httpx.Response(
            200,
            text="""
            <html>
              <head>
                <link rel="alternate" type="application/rss+xml" title="Post Comments" href="/blog/comments/feed.xml" />
                <link rel="alternate" type="application/rss+xml" title="Empty RSS" href="rss.xml" />
                <link rel="alternate" type="application/atom+xml" title="Books Atom" href="../atom.xml#feed" />
                <link rel="alternate" type="application/rss+xml" title="Backup RSS" href="/backup.xml" />
              </head>
              <body><h1>Newsletter Home</h1><p>Weekly dispatches for readers.</p></body>
            </html>
            """,
            headers={"content-type": "text/html; charset=utf-8"},
        )
    )
    empty_rss = respx.get("https://93.184.216.34/blog/rss.xml").mock(
        return_value=httpx.Response(
            200,
            text='<?xml version="1.0"?><rss version="2.0"><channel><title>Empty</title></channel></rss>',
            headers={"content-type": "application/rss+xml"},
        )
    )
    atom_feed = respx.get("https://93.184.216.34/atom.xml").mock(
        return_value=httpx.Response(
            200,
            text="""
            <feed xmlns="http://www.w3.org/2005/Atom">
              <title>Book Dispatches</title>
              <entry>
                <title>The Left Hand of Darkness</title>
                <author><name>Ursula K. Le Guin</name></author>
                <link rel="alternate" href="/posts/left-hand" />
                <summary>A classic science fiction novel.</summary>
              </entry>
            </feed>
            """,
            headers={"content-type": "application/atom"},
        )
    )
    backup_rss = respx.get("https://93.184.216.34/backup.xml").mock(
        return_value=httpx.Response(200, text="<rss/>", headers={"content-type": "application/rss+xml"})
    )
    comments_rss = respx.get("https://93.184.216.34/blog/comments/feed.xml").mock(
        return_value=httpx.Response(200, text="<rss/>", headers={"content-type": "application/rss+xml"})
    )

    _, _, redirected_url = asyncio.run(
        fetch_bytes("https://newsletter.example.test/", return_url=True)
    )
    assert redirected_url == "https://www.newsletter.example.test/blog/"

    preview = asyncio.run(preview_source("https://newsletter.example.test/"))
    assert preview["url"] == "https://www.newsletter.example.test/atom.xml"
    assert preview["content_type"] == "application/atom"
    assert preview["count"] == 1
    assert preview["sample"][0]["title"] == "The Left Hand of Darkness"
    assert preview["sample"][0]["author"] == "Ursula K. Le Guin"
    assert (
        preview["sample"][0]["source_url"]
        == "https://www.newsletter.example.test/posts/left-hand"
    )
    assert empty_rss.called and atom_feed.called
    assert not backup_rss.called
    assert not comments_rss.called


@respx.mock
def test_autodiscovery_enforces_ssrf_caps_fetches_and_keeps_direct_html_single_fetch(
    monkeypatch,
):
    def fake_getaddrinfo(host, port, *args, **kwargs):
        ip = "127.0.0.1" if host == "private.example.test" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    private_route = respx.get("https://127.0.0.1/internal.xml").mock(
        return_value=httpx.Response(200, text="<rss/>", headers={"content-type": "application/rss+xml"})
    )
    respx.get("https://93.184.216.34/empty-home").mock(
        return_value=httpx.Response(
            200,
            text="""
            <html><head>
              <link rel="alternate" type="application/rss+xml" href="http://books.example.test/http-only.xml" />
              <link rel="alternate" type="application/rss+xml" href="https://private.example.test/internal.xml" />
              <link rel="alternate" type="application/rss+xml" href="/nested-html" />
              <link rel="alternate" type="application/rss+xml" href="/redirect-private" />
              <link rel="alternate" type="application/rss+xml" href="/empty-3.xml" />
              <link rel="alternate" type="application/rss+xml" href="/fourth.xml" />
            </head><body><p>No books directly on homepage.</p></body></html>
            """,
            headers={"content-type": "text/html"},
        )
    )
    nested_html = respx.get("https://93.184.216.34/nested-html").mock(
        return_value=httpx.Response(
            200,
            text='<html><head><link rel="alternate" type="application/rss+xml" href="/nested.xml" /></head></html>',
            headers={"content-type": "text/html"},
        )
    )
    nested_feed = respx.get("https://93.184.216.34/nested.xml").mock(
        return_value=httpx.Response(200, text="<rss/>", headers={"content-type": "application/rss+xml"})
    )
    redirect_private = respx.get("https://93.184.216.34/redirect-private").mock(
        return_value=httpx.Response(
            302, headers={"location": "https://private.example.test/internal.xml"}
        )
    )
    empty_three = respx.get("https://93.184.216.34/empty-3.xml").mock(
        return_value=httpx.Response(
            200,
            text='<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>',
            headers={"content-type": "application/rss+xml"},
        )
    )
    fourth_feed = respx.get("https://93.184.216.34/fourth.xml").mock(
        return_value=httpx.Response(
            200,
            text='<?xml version="1.0"?><rss version="2.0"><channel><item><title>Unreached Book</title></item></channel></rss>',
            headers={"content-type": "application/rss+xml"},
        )
    )

    empty_preview = asyncio.run(preview_source("https://books.example.test/empty-home"))
    assert empty_preview["count"] == 0
    assert empty_preview["url"] == "https://books.example.test/empty-home"
    assert not private_route.called
    assert nested_html.called and redirect_private.called and empty_three.called
    assert not nested_feed.called
    assert not fourth_feed.called

    respx.get("https://93.184.216.34/direct-books").mock(
        return_value=httpx.Response(
            200,
            text="""
            <html>
              <head>
                <link rel="alternate" type="application/rss+xml" href="/unused-feed.xml" />
                <script type="application/ld+json">
                  {"@type": "Book", "name": "Direct JSON-LD Book", "author": "A Writer"}
                </script>
              </head>
              <body></body>
            </html>
            """,
            headers={"content-type": "text/html"},
        )
    )
    unused_feed = respx.get("https://93.184.216.34/unused-feed.xml").mock(
        return_value=httpx.Response(200, text="<rss/>", headers={"content-type": "application/rss+xml"})
    )
    direct_preview = asyncio.run(preview_source("https://books.example.test/direct-books"))
    assert direct_preview["count"] == 1
    assert direct_preview["url"] == "https://books.example.test/direct-books"
    assert not unused_feed.called


@respx.mock
def test_source_endpoints_persist_resolved_feed_url_and_reject_duplicate_canonical_feed(
    database, monkeypatch
):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    for path in ("/", "/about"):
        respx.get(f"https://93.184.216.34{path}").mock(
            return_value=httpx.Response(
                200,
                text='<html><head><link rel="alternate" type="application/rss+xml" href="/feed.xml" /></head></html>',
                headers={"content-type": "text/html"},
            )
        )
    respx.get("https://93.184.216.34/feed.xml").mock(
        return_value=httpx.Response(
            200,
            text="""<?xml version="1.0"?>
            <rss version="2.0"><channel>
              <item><title>Orbital</title><author>Samantha Harvey</author></item>
            </channel></rss>""",
            headers={"content-type": "application/rss+xml"},
        )
    )

    with TestClient(app, headers=authenticated_headers()) as client:
        first_preview = client.post(
            "/api/sources/preview", json={"url": "https://newsletter.example.test/"}
        )
        assert first_preview.status_code == 200
        assert first_preview.json()["url"] == "https://newsletter.example.test/feed.xml"
        assert first_preview.json()["count"] == 1

        created = client.post(
            "/api/sources",
            json={
                "name": "newsletter.example.test",
                "url": first_preview.json()["url"],
                "enabled": False,
            },
        )
        assert created.status_code == 200
        stored = row("SELECT name, url FROM sources WHERE id=?", (created.json()["id"],))
        assert stored["name"] == "newsletter.example.test"
        assert stored["url"] == "https://newsletter.example.test/feed.xml"

        second_preview = client.post(
            "/api/sources/preview", json={"url": "https://newsletter.example.test/about"}
        )
        assert second_preview.status_code == 200
        assert second_preview.json()["url"] == "https://newsletter.example.test/feed.xml"

        duplicate = client.post(
            "/api/sources",
            json={
                "name": "Duplicate feed",
                "url": second_preview.json()["url"],
                "enabled": False,
            },
        )
        assert duplicate.status_code == 409
        assert "already exists" in duplicate.json()["detail"]

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


def test_open_library_trending_source_imports_author_names_and_sets_ok_status(database, monkeypatch):
    url = "https://openlibrary.org/trending/weekly"
    payload = json.dumps(
        {
            "works": [
                {
                    "key": "/works/OL17930368W",
                    "title": "Atomic Habits",
                    "author_name": ["James Clear"],
                    "cover_i": 12539702,
                    "first_publish_year": 2018,
                }
            ]
        }
    ).encode()

    async def fake_fetch(_url):
        return payload, "application/json"

    async def fake_enrich(items):
        return items

    monkeypatch.setattr(ingestion, "fetch_bytes", fake_fetch)
    monkeypatch.setattr(ingestion, "enrich_book_metadata", fake_enrich)
    with transaction() as con:
        cursor = con.execute("INSERT INTO sources(name,url) VALUES(?,?)", ("Open Library trending", url))

    source = row("SELECT * FROM sources WHERE id=?", (cursor.lastrowid,))
    assert asyncio.run(scan_source(source)) == 1
    candidate = row("SELECT * FROM candidates WHERE normalized_key=?", (normalize_key("Atomic Habits", "James Clear"),))
    assert candidate["source_url"] == "https://openlibrary.org/works/OL17930368W"
    assert row("SELECT last_status FROM sources WHERE id=?", (cursor.lastrowid,))["last_status"] == "ok:1"


def test_scan_source_preserves_existing_description_and_merges_genres(database, monkeypatch):
    async def fake_fetch(_url):
        return "application/json", [
            {
                "title": "Curated Work",
                "author": "A Writer",
                "description": "A longer catalog description that must not replace the curated copy.",
                "genres": ["Literary fiction", "Family life"],
            }
        ]

    async def no_enrichment(items):
        return items

    monkeypatch.setattr("afterword_engine.ingestion._fetch_and_parse_source", fake_fetch)
    monkeypatch.setattr("afterword_engine.ingestion.enrich_book_metadata", no_enrichment)
    with transaction() as con:
        builtin_id = con.execute("SELECT id FROM sources WHERE url='builtin://upcoming'").fetchone()[0]
        source_id = con.execute(
            "INSERT INTO sources(name,url) VALUES(?,?)",
            ("Metadata merge source", "https://example.com/metadata-merge"),
        ).lastrowid
        candidate_id = con.execute(
            """INSERT INTO candidates(title,author,description,source_url,source_id,genres,normalized_key)
            VALUES(?,?,?,?,?,?,?)""",
            (
                "Curated Work",
                "A Writer",
                "Curated publisher description.",
                "https://example.com/curated",
                builtin_id,
                '["Curated genre"]',
                normalize_key("Curated Work", "A Writer"),
            ),
        ).lastrowid

    source = row("SELECT * FROM sources WHERE id=?", (source_id,))
    assert asyncio.run(scan_source(source)) == 1
    candidate = row("SELECT * FROM candidates WHERE id=?", (candidate_id,))
    assert candidate["description"] == "Curated publisher description."
    assert json.loads(candidate["genres"]) == ["Curated genre", "Literary fiction", "Family life"]


def test_source_filters_match_genres_and_keep_untagged_books():
    items = [
        {"title": "Fantasy book", "genres": ["Fantasy"]},
        {"title": "Romance book", "genres": ["Romance"]},
        {"title": "Untagged book", "genres": []},
    ]
    filters = normalize_source_filters(
        {"include_genres": ["fantasy"], "exclude_genres": ["romance"]}
    )
    assert filters == {"include_genres": ["fantasy"], "exclude_genres": ["romance"]}
    assert [item["title"] for item in filter_source_items(items, filters)] == ["Fantasy book"]
    assert [item["title"] for item in filter_source_items(items, {"exclude_genres": ["romance"]})] == [
        "Fantasy book",
        "Untagged book",
    ]
    assert filter_source_items(
        [{"title": "Cartography", "genres": ["Cartography"]}],
        {"include_genres": ["art"]},
    ) == []
    assert filter_source_items(
        [{"title": "Generic fiction", "genres": ["Fiction"]}],
        {"include_genres": ["Science fiction"]},
    ) == []


def test_scan_source_applies_filters_and_removes_stale_unmatched_candidates(database, monkeypatch):
    async def fake_fetch(_url):
        return "application/json", [
            {"title": "Keep fantasy", "author": "A Writer", "genres": ["Fantasy"]},
            {"title": "Drop romance", "author": "A Writer", "genres": ["Romance"]},
            {"title": "Keep untagged", "author": "A Writer", "genres": []},
        ]

    async def no_enrichment(items):
        return items

    monkeypatch.setattr("afterword_engine.ingestion._fetch_and_parse_source", fake_fetch)
    monkeypatch.setattr("afterword_engine.ingestion.enrich_book_metadata", no_enrichment)
    with transaction() as con:
        cursor = con.execute(
            "INSERT INTO sources(name,url,filters) VALUES(?,?,?)",
            (
                "Filtered source",
                "https://example.com/filtered",
                json.dumps({"include_genres": ["fantasy"]}),
            ),
        )
        con.execute(
            "INSERT INTO candidates(title,author,source_id,status,normalized_key) VALUES(?,?,?,?,?)",
            ("Stale book", "A Writer", cursor.lastrowid, "new", "stale book a writer"),
        )

    source = row("SELECT * FROM sources WHERE id=?", (cursor.lastrowid,))
    assert asyncio.run(scan_source(source)) == 1
    assert row("SELECT id FROM candidates WHERE title='Keep fantasy'")
    assert row("SELECT id FROM candidates WHERE title='Keep untagged'") is None
    assert row("SELECT id FROM candidates WHERE title='Drop romance'") is None
    assert row("SELECT id FROM candidates WHERE title='Stale book'") is None


def test_scan_source_persists_source_isbn_and_quality_evidence(database, monkeypatch):
    async def fake_fetch(_url):
        return "application/json", [
            {
                "title": "Recovered Book",
                "author": "A Writer",
                "source_url": "https://www.amazon.com/dp/0061120081",
            }
        ]

    async def no_enrichment(items):
        return items

    monkeypatch.setattr("afterword_engine.ingestion._fetch_and_parse_source", fake_fetch)
    monkeypatch.setattr("afterword_engine.ingestion.enrich_book_metadata", no_enrichment)
    with transaction() as con:
        cursor = con.execute(
            "INSERT INTO sources(name,url) VALUES(?,?)",
            ("Amazon-backed source", "https://example.com/upcoming"),
        )

    source = row("SELECT * FROM sources WHERE id=?", (cursor.lastrowid,))
    assert asyncio.run(scan_source(source)) == 1
    candidate = row("SELECT * FROM candidates WHERE title='Recovered Book'")
    quality = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (candidate["id"],))
    assert candidate["isbn13"] == "9780061120084"
    assert candidate["isbn10"] == "0061120081"
    assert quality["isbn13"] == "9780061120084"
    assert quality["isbn10"] == "0061120081"


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


def test_failed_permanent_source_retries_after_error_retry_floor(database, monkeypatch):
    good_scan_at = "2026-04-10T12:00:00+00:00"
    with transaction() as con:
        con.execute("UPDATE sources SET enabled=0")
        cursor = con.execute(
            "INSERT INTO sources(name,url,enabled,lifecycle,last_scanned_at,last_attempted_at) "
            "VALUES(?,?,1,'permanent',?,?)",
            ("Retry permanent", "https://example.com/retry-permanent", good_scan_at, good_scan_at),
        )

    async def fail(_source):
        raise RuntimeError("temporary source outage")

    monkeypatch.setattr("afterword_engine.main.scan_source", fail)
    result = asyncio.run(handle_job("sync"))
    source = row("SELECT * FROM sources WHERE id=?", (cursor.lastrowid,))
    assert len(result["errors"]) == 1
    assert source["last_status"].startswith("error:")
    assert source["last_scanned_at"] == good_scan_at
    assert source["last_attempted_at"] != good_scan_at
    assert source_sync_is_due() is False

    retry_at = datetime.now(timezone.utc) - timedelta(
        seconds=SOURCE_SYNC_ERROR_RETRY_SECONDS + 1
    )
    with transaction() as con:
        con.execute(
            "UPDATE sources SET last_attempted_at=? WHERE id=?",
            (retry_at.isoformat(), cursor.lastrowid),
        )
    assert source_sync_is_due() is True


def test_scheduled_sync_retries_only_failed_sources_and_stops_after_all_fail(database, monkeypatch):
    now = datetime.now(timezone.utc)
    retry_at = now - timedelta(seconds=SOURCE_SYNC_ERROR_RETRY_SECONDS + 1)
    with transaction() as con:
        con.execute("UPDATE sources SET enabled=0")
        successful_id = con.execute(
            "INSERT INTO sources(name,url,enabled,lifecycle,last_status,last_scanned_at) "
            "VALUES('Recent source','https://example.com/recent',1,'permanent','ok',?)",
            (now.isoformat(),),
        ).lastrowid
        failed_id = con.execute(
            "INSERT INTO sources(name,url,enabled,lifecycle,last_status,last_scanned_at) "
            "VALUES('Failed source','https://example.com/failed',1,'permanent','error:timeout',?)",
            (retry_at.isoformat(),),
        ).lastrowid
    assert [source["id"] for source in source_sync_due_sources(now)] == [failed_id]

    scanned = []

    async def fail_source(source):
        scanned.append(source["id"])
        raise RuntimeError("feed unavailable")

    monkeypatch.setattr("afterword_engine.main.scan_source", fail_source)
    monkeypatch.setattr(
        "afterword_engine.main.refresh_missing_candidate_metadata",
        lambda: (_ for _ in ()).throw(AssertionError("failed scheduled sync must stop early")),
    )
    result = asyncio.run(handle_job("sync_due"))

    assert scanned == [failed_id]
    assert successful_id not in scanned
    assert result["skipped"] == "all_sources_failed"
    assert result["errors"][0]["id"] == failed_id


def test_goodreads_rss_scheduler_retries_failed_daily_job_with_a_cap(database):
    with transaction() as con:
        con.execute(
            "INSERT INTO settings(key,value,secret) VALUES('goodreads_rss_url',?,1) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,secret=1",
            ("https://www.goodreads.com/review/list_rss/test",),
        )
    now = datetime.now(timezone.utc).replace(hour=3, minute=0, second=0, microsecond=0)
    job_id = queue_goodreads_rss_if_due(now)
    assert job_id
    with transaction() as con:
        con.execute("UPDATE jobs SET status='failed',attempts=1 WHERE id=?", (job_id,))

    assert queue_goodreads_rss_if_due(now) == job_id
    assert row("SELECT status,attempts FROM jobs WHERE id=?", (job_id,)) == {
        "status": "queued",
        "attempts": 1,
    }
    with transaction() as con:
        con.execute("UPDATE jobs SET status='failed',attempts=? WHERE id=?", (GOODREADS_RSS_MAX_ATTEMPTS, job_id))

    assert queue_goodreads_rss_if_due(now) is None
    assert row("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "failed"


def test_goodreads_rss_failure_is_persisted_for_settings(database, monkeypatch):
    async def fail(_url, client=None):
        raise RuntimeError("feed unavailable")

    monkeypatch.setattr("afterword_engine.main.import_goodreads_rss", fail)
    with pytest.raises(RuntimeError, match="feed unavailable"):
        asyncio.run(main._sync_goodreads_rss("https://www.goodreads.com/feed"))

    configured = safe_settings(include_api_tokens=False)
    assert configured["goodreads_rss_last_status"] == "failed"
    assert configured["goodreads_rss_last_attempt_at"]
    assert configured["goodreads_rss_last_error"] == "feed unavailable"
    assert configured["goodreads_rss_last_sync_at"] == ""


def test_sync_goodreads_rss_reuses_single_client_with_3_way_concurrency(database, monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    with transaction() as con:
        con.execute(
            "INSERT INTO goodreads_shelf_items(shelf, title, author) "
            "VALUES('currently-reading', 'Finished From Private Shelf', 'Author A')"
        )
        con.execute(
            "INSERT INTO goodreads_shelf_items(shelf, title, author) "
            "VALUES('currently-reading', 'Still Reading Private', 'Author B')"
        )

    read_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>Finished From Private Shelf</title>
        <author_name>Author A</author_name>
        <user_rating>5</user_rating>
        <isbn13>9780593135204</isbn13>
      </item>
    </channel></rss>"""
    to_read_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>Queued Book</title>
        <author_name>Author C</author_name>
      </item>
    </channel></rss>"""

    in_flight = 0
    max_in_flight = 0
    seen_hosts: list[str] = []
    created_clients: list[httpx.AsyncClient] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, max_in_flight
        seen_hosts.append(request.headers.get("Host", ""))
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            await asyncio.sleep(0.15)
            shelf = request.url.params.get("shelf")
            if shelf == "read":
                return httpx.Response(200, content=read_xml, headers={"content-type": "application/rss+xml"})
            if shelf == "to-read":
                return httpx.Response(200, content=to_read_xml, headers={"content-type": "application/rss+xml"})
            if shelf == "currently-reading":
                return httpx.Response(404, content=b"Not Found", headers={"content-type": "text/plain"})
            raise AssertionError(f"Unexpected shelf: {shelf}")
        finally:
            in_flight -= 1

    def fake_source_client() -> httpx.AsyncClient:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            timeout=settings.source_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        )
        created_clients.append(client)
        return client

    monkeypatch.setattr(ingestion, "_source_client", fake_source_client)

    result = asyncio.run(
        main._sync_goodreads_rss("https://www.goodreads.com/review/list_rss/12345?key=secret&shelf=read")
    )

    assert len(created_clients) == 1
    assert max_in_flight == 3
    assert seen_hosts == ["www.goodreads.com", "www.goodreads.com", "www.goodreads.com"]
    assert result["imported"] == 1
    assert result["shelves"] == {"to-read": 1, "currently-reading": 0}
    assert safe_settings(include_api_tokens=False)["goodreads_rss_last_status"] == "succeeded"
    assert row(
        "SELECT COUNT(*) count FROM reads WHERE title='Finished From Private Shelf'"
    )["count"] == 1
    shelf_items = [
        (item["shelf"], item["title"], item["author"])
        for item in rows("SELECT shelf, title, author FROM goodreads_shelf_items ORDER BY shelf, title")
    ]
    assert shelf_items == [
        ("currently-reading", "Still Reading Private", "Author B"),
        ("to-read", "Queued Book", "Author C"),
    ]


def test_sync_goodreads_rss_read_failure_rolls_back_and_skips_shelf_writes(database, monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    with transaction() as con:
        con.execute(
            "INSERT INTO goodreads_shelf_items(shelf, title, author) "
            "VALUES('to-read', 'Existing Shelf Book', 'Existing Author')"
        )

    shelf_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>Replacement Book</title>
        <author_name>New Author</author_name>
      </item>
    </channel></rss>"""

    completed_shelves: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        shelf = request.url.params.get("shelf")
        if shelf == "read":
            await asyncio.sleep(0.02)
            return httpx.Response(500, content=b"Server Error", headers={"content-type": "text/plain"})
        await asyncio.sleep(0.06)
        completed_shelves.append(str(shelf))
        return httpx.Response(200, content=shelf_xml, headers={"content-type": "application/rss+xml"})

    monkeypatch.setattr(
        ingestion,
        "_source_client",
        lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            timeout=settings.source_timeout_seconds,
            follow_redirects=False,
            trust_env=False,
        ),
    )

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(
            main._sync_goodreads_rss("https://www.goodreads.com/review/list_rss/12345?key=secret&shelf=read")
        )

    assert sorted(completed_shelves) == ["currently-reading", "to-read"]
    assert safe_settings(include_api_tokens=False)["goodreads_rss_last_status"] == "failed"
    shelf_items = [
        (item["shelf"], item["title"], item["author"])
        for item in rows("SELECT shelf, title, author FROM goodreads_shelf_items")
    ]
    assert shelf_items == [("to-read", "Existing Shelf Book", "Existing Author")]


def test_goodreads_csv_import_is_idempotent(database):
    payload = b'Book Id,Title,Author,My Rating,Date Read,ISBN13\n1,"A Book, With Comma",Writer,5,2026/01/02,"=\"9781234567890\""\n'
    assert import_goodreads_csv(payload) == 1
    assert import_goodreads_csv(payload) == 1
    assert row("SELECT COUNT(*) count FROM reads WHERE title='A Book, With Comma'")["count"] == 1


def test_goodreads_reimports_preserve_known_read_date_when_feed_date_is_blank(database, monkeypatch):
    dated_csv = (
        b'Book Id,Title,Author,My Rating,Date Read,ISBN13\n'
        b'1,CSV Read,Writer,5,2024/03/04,\n'
    )
    undated_csv = (
        b'Book Id,Title,Author,My Rating,Date Read,ISBN13\n'
        b'1,CSV Read,Writer,5,,\n'
    )
    import_goodreads_csv(dated_csv)
    import_goodreads_csv(undated_csv)
    assert row("SELECT read_at FROM reads WHERE title='CSV Read'")["read_at"] == "2024/03/04"
    import_goodreads_csv(
        b'Book Id,Title,Author,My Rating,Date Read,ISBN13\n'
        b'1,CSV Read,Writer,5,not-a-date,\n'
    )
    assert row("SELECT read_at FROM reads WHERE title='CSV Read'")["read_at"] == "2024/03/04"
    # DictReader yields None for an omitted field in a short row. Ignore it
    # safely instead of turning a malformed upload into an AttributeError.
    assert import_goodreads_csv(
        b'Book Id,Title,Author,My Rating,Date Read,ISBN13\n'
        b'2,Truncated row\n'
    ) == 0

    import_goodreads_csv(
        b'Book Id,Title,Author,My Rating,Date Read,ISBN13\n'
        b'2,RSS Read,Writer,4,2023/02/01,\n'
    )
    with transaction() as con:
        con.execute(
            "INSERT INTO goodreads_shelf_items(shelf,title,author) "
            "VALUES('to-read','RSS Read','Writer')"
        )

    async def fetch_bytes(*_args, **_kwargs):
        return b"", "application/rss+xml"

    class Feed:
        entries = [{
            "title": "RSS Read",
            "author_name": "Writer",
            "user_rating": "4",
            "user_read_at": "not-a-date",
            "isbn13": "9780306406157",
        }]

    monkeypatch.setattr(ingestion, "fetch_bytes", fetch_bytes)
    monkeypatch.setattr(ingestion.feedparser, "parse", lambda _content: Feed())
    asyncio.run(import_goodreads_rss("https://www.goodreads.com/review/list_rss/reader?shelf=read"))
    assert row("SELECT read_at FROM reads WHERE title='RSS Read'")["read_at"] == "2023/02/01"
    assert row("SELECT isbn FROM reads WHERE title='RSS Read'")["isbn"] == "9780306406157"
    assert row(
        "SELECT COUNT(*) count FROM goodreads_shelf_items WHERE title='RSS Read'"
    )["count"] == 0

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

def test_nyt_source_endpoints_reject_inline_credentials():
    url = "https://api.nytimes.com/svc/books/v3/lists/overview.json?api-key=never-store-this"
    with TestClient(app, headers=authenticated_headers()) as client:
        preview = client.post("/api/sources/preview", json={"url": url})
        assert preview.status_code == 400
        assert "encrypted NYT API settings" in preview.json()["detail"]
        created = client.post("/api/sources", json={"name": "NYT", "url": url})
        assert created.status_code == 400
        assert "never-store-this" not in created.text

def test_api_boots_and_serves_recommendations(database):
    with TestClient(app, headers=authenticated_headers()) as client:
        assert client.get("/api/health").json()["ok"] is True
        overview = client.get("/api/overview")
        assert overview.status_code == 200
        assert len(overview.json()["recommendations"]) == 4
        saved = client.post("/api/recommendations/1/feedback", json={"action":"save"})
        assert saved.json()["status"] == "saved"
        assert client.put("/api/sources/99999/toggle").status_code == 404
        with transaction() as con:
            cursor = con.execute(
                "INSERT INTO sources(name,url,enabled) VALUES(?,?,0)",
                ("Filter API", "https://example.com/filter-api"),
            )
            source_id = cursor.lastrowid
        updated = client.put(
            f"/api/sources/{source_id}",
            json={
                "filters": {
                    "include_genres": ["Fantasy", "fantasy"],
                    "exclude_genres": ["Romance"],
                }
            },
        )
        assert updated.status_code == 200
        assert updated.json()["filters"] == {
            "include_genres": ["Fantasy"],
            "exclude_genres": ["Romance"],
        }
        source = next(
            item for item in client.get("/api/overview").json()["sources"]
            if item["id"] == source_id
        )
        assert source["filters"] == updated.json()["filters"]
        token = client.post(
            "/api/settings/api-tokens", json={"name": "Source filter test"}
        ).json()["token"]
        assert next(
            item
            for item in client.get(
                "/api/v1/sources", headers={"Authorization": f"Bearer {token}"}
            ).json()
            if item["id"] == source_id
        )["filters"] == updated.json()["filters"]
        invalid = client.put("/api/settings", json={"embedding_backend":"not-real","embedding_model":"x"})
        assert invalid.status_code == 400
        schedule = client.put("/api/sources/schedule", json={"interval_hours": 168})
        assert schedule.status_code == 200
        assert client.get("/api/overview").json()["settings"]["source_sync_interval_hours"] == 168
        rebuild = client.post("/api/embeddings/rebuild")
        assert rebuild.status_code == 200 and rebuild.json()["job_id"]


def test_mark_read_removes_shortlist_book_and_adds_it_to_read_history(database):
    with transaction() as con:
        con.execute("UPDATE candidates SET status='saved' WHERE id IN (1,2)")
        book = con.execute("SELECT title,author FROM candidates WHERE id=1").fetchone()

    with TestClient(app, headers=authenticated_headers()) as client:
        response = client.post("/api/recommendations/1/read", json={"rating": 4})
        shortlist = client.get("/api/recommendations?status=saved")
        history = client.get("/api/reading-history")

    assert response.status_code == 200
    assert response.json()["status"] == "read"
    assert all(item["id"] != 1 for item in shortlist.json())
    read = next(
        item
        for item in history.json()
        if item["title"] == book["title"] and item["author"] == book["author"]
    )
    assert read["rating"] == 4


def test_recommendations_filter_and_paginate_in_sql(database):
    with transaction() as con:
        con.execute("UPDATE candidates SET status='recommended', score=id")
        con.execute("UPDATE candidates SET status='saved' WHERE id=1")

    with TestClient(app, headers=authenticated_headers()) as client:
        page = client.get("/api/recommendations?status=recommended&limit=2&offset=1")
        assert page.status_code == 200
        assert len(page.json()) == 2
        assert all(item["status"] == "recommended" for item in page.json())

        saved = client.get("/api/recommendations?status=saved")
        assert [item["id"] for item in saved.json()] == [1]
        discover = client.get("/api/recommendations?status=recommended")
        assert 2 in {item["id"] for item in discover.json()}

        with transaction() as con:
            con.execute("UPDATE candidates SET status='imported' WHERE id=2")

        overview = client.get("/api/overview?recommendation_limit=1")
        assert [item["status"] for item in overview.json()["recommendations"]] == [
            "recommended",
            "imported",
            "saved",
        ]


def test_overview_payload_reuses_one_database_connection(database, monkeypatch):
    import afterword_engine.main as main_module

    connections = []
    original_connect = main_module.connect
    unsealed_values = []
    original_unseal = main_module.unseal
    statements = []

    def capture_connect():
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        connections.append(connection)
        return connection

    def count_unseal(value):
        unsealed_values.append(value)
        return original_unseal(value)

    monkeypatch.setattr(main_module, "connect", capture_connect)
    monkeypatch.setattr(main_module, "unseal", count_unseal)
    overview = main_module.overview_payload()

    assert len(overview["recommendations"]) == 4
    assert "api_tokens" in overview["settings"]
    assert len(connections) == 1
    assert sum(query.strip() == "SELECT * FROM reads" for query in statements) == 1
    assert sum("WHERE c.status IN ('saved','imported')" in query for query in statements) == 1
    assert len(unsealed_values) == row(
        "SELECT COUNT(*) count FROM settings WHERE secret=1"
    )["count"]


def test_tracked_recommendations_reuses_a_supplied_database_connection(database, monkeypatch):
    import afterword_engine.main as main_module

    connections = []
    original_connect = main_module.connect

    def capture_connect():
        connection = original_connect()
        connections.append(connection)
        return connection

    monkeypatch.setattr(main_module, "connect", capture_connect)
    with main_module.connect() as connection:
        values, run_id = main_module.tracked_recommendations(
            connection=connection,
            status="recommended",
            limit=100,
        )

    assert run_id
    assert isinstance(values, list)
    assert len(connections) == 1


def test_librarr_only_setting_update_skips_embedding_health(database, monkeypatch):
    import afterword_engine.main as main_module

    def reject_embedding_check(*_args, **_kwargs):
        raise AssertionError("unchanged embedding settings should not be checked")

    monkeypatch.setattr(main_module, "get_embedder", reject_embedding_check)
    with TestClient(app, headers=authenticated_headers()) as client:
        response = client.put("/api/settings", json={"librarr_media_type": "ebook"})

    assert response.status_code == 200
    assert response.json()["embedding"]["checked"] is False
    assert row("SELECT value FROM settings WHERE key='librarr_media_type'")["value"] == "ebook"


def test_overview_bounds_decisions_and_reports_more(database):
    with transaction() as con:
        con.execute("UPDATE candidates SET status='rejected'")
        candidate_ids = [
            item["id"]
            for item in con.execute("SELECT id FROM candidates ORDER BY id LIMIT 2")
        ]
        con.executemany(
            "INSERT INTO feedback(candidate_id,action) VALUES(?,'reject')",
            [(candidate_id,) for candidate_id in candidate_ids],
        )

    with TestClient(app, headers=authenticated_headers()) as client:
        response = client.get("/api/overview?decision_limit=1")

    assert response.status_code == 200
    assert len(response.json()["decisions"]) == 1
    assert response.json()["decisions_has_more"] is True


def test_settings_encrypt_and_preserve_api_keys(database):
    payload = {"embedding_backend":"local","embedding_model":"anything","embedding_url":"","embedding_api_key":"embedding-secret","librarr_url":"http://librarr:5050","librarr_api_key":"librarr-secret","librarr_media_type":"ebook"}
    with TestClient(app, headers=authenticated_headers()) as client:
        assert client.put("/api/settings", json=payload).status_code == 200
        payload["embedding_api_key"] = payload["librarr_api_key"] = ""
        assert client.put("/api/settings", json=payload).status_code == 200
    stored = row("SELECT value,secret FROM settings WHERE key='librarr_api_key'")
    assert stored["secret"] == 1 and stored["value"].startswith("fernet:") and "librarr-secret" not in stored["value"]
    assert row("SELECT value FROM settings WHERE key='librarr_media_type'")["value"] == "ebook"


def test_partial_settings_updates_preserve_sibling_fields_and_require_initial_librarr_key(
    database, monkeypatch
):
    import afterword_engine.main as main_module

    with TestClient(app, headers=authenticated_headers()) as client:
        missing_key = client.put(
            "/api/settings",
            json={
                "librarr_url": "http://librarr:5050",
                "librarr_api_key": "",
                "librarr_media_type": "ebook",
            },
        )
        assert missing_key.status_code == 400
        assert missing_key.json()["detail"] == "Enter a Librarr API key for the first connection."

        omitted_key = client.put(
            "/api/settings",
            json={"librarr_url": "http://librarr:5050"},
        )
        assert omitted_key.status_code == 400
        assert omitted_key.json()["detail"] == "Enter a Librarr API key for the first connection."

        saved_librarr = client.put(
            "/api/settings",
            json={
                "librarr_url": "http://librarr:5050",
                "librarr_api_key": "librarr-initial-secret",
                "librarr_media_type": "ebook",
            },
        )
        assert saved_librarr.status_code == 200

        saved_embeddings = client.put(
            "/api/settings",
            json={
                "embedding_backend": "local",
                "embedding_model": "hashing-768",
                "embedding_url": "",
                "embedding_api_key": "embedding-initial-secret",
            },
        )
        assert saved_embeddings.status_code == 200

        def reject_embedding_check(*_args, **_kwargs):
            raise AssertionError("unchanged embedding settings should not be checked")

        monkeypatch.setattr(main_module, "get_embedder", reject_embedding_check)
        updated_format = client.put(
            "/api/settings",
            json={
                "librarr_url": "http://librarr:5050",
                "librarr_api_key": "",
                "librarr_media_type": "audiobook",
            },
        )
        assert updated_format.status_code == 200
        assert updated_format.json()["embedding"]["checked"] is False

    private = main_module.private_settings()
    assert private["librarr_url"] == "http://librarr:5050"
    assert private["librarr_api_key"] == "librarr-initial-secret"
    assert private["librarr_media_type"] == "audiobook"
    assert private["embedding_api_key"] == "embedding-initial-secret"
    assert row("SELECT COUNT(*) count FROM recommendation_runs")["count"] == 0


def test_settings_reject_private_librarr_host_even_with_a_key(database):
    import afterword_engine.main as main_module

    with TestClient(app, headers=authenticated_headers()) as client:
        rejected = client.put(
            "/api/settings",
            json={
                "librarr_url": "http://10.0.0.1:5050",
                "librarr_api_key": "librarr-secret",
                "librarr_media_type": "ebook",
            },
        )
        assert rejected.status_code == 400
    private = main_module.private_settings()
    assert private.get("librarr_url", "") == ""
    assert private.get("librarr_api_key", "") == ""


def test_toggle_default_source_is_atomic_and_returns_404_when_absent(database):
    default_source = row("SELECT id, enabled FROM sources WHERE is_default=1")
    assert default_source["enabled"] == 1

    with TestClient(app, headers=authenticated_headers()) as client:
        disabled = client.put("/api/sources/default/toggle")
        assert disabled.status_code == 200
        disabled_payload = disabled.json()
        assert disabled_payload["id"] == default_source["id"]
        assert disabled_payload["enabled"] == 0
        assert disabled_payload["job_id"] is not None
        assert row("SELECT kind FROM jobs WHERE id=?", (disabled_payload["job_id"],)) == {
            "kind": "score",
        }
        assert row("SELECT COUNT(*) count FROM reads WHERE source='demo'")["count"] == 0
        assert row("SELECT enabled FROM sources WHERE id=?", (default_source["id"],))["enabled"] == 0
        assert (
            row(
                "SELECT COUNT(*) count FROM candidates WHERE source_id=? AND status='recommended'",
                (default_source["id"],),
            )["count"]
            == 0
        )

        reenabled = client.put("/api/sources/default/toggle")
        assert reenabled.status_code == 200
        reenabled_payload = reenabled.json()
        assert reenabled_payload["id"] == default_source["id"]
        assert reenabled_payload["enabled"] == 1
        assert reenabled_payload["job_id"] is not None
        assert row("SELECT enabled FROM sources WHERE id=?", (default_source["id"],))["enabled"] == 1

        with transaction() as con:
            con.execute("UPDATE sources SET is_default=0 WHERE is_default=1")

        missing = client.put("/api/sources/default/toggle")
        assert missing.status_code == 404
        assert missing.json()["detail"] == "Default source not found."

    assert row("SELECT COUNT(*) count FROM recommendation_runs")["count"] == 0


def test_nyt_books_key_is_encrypted_and_only_added_to_outgoing_requests(database):
    with TestClient(app, headers=authenticated_headers()) as client:
        assert client.get("/api/settings/nyt").json() == {"api_key_set": False}
        saved = client.put("/api/settings/nyt", json={"api_key": "nyt-test-key"})
        assert saved.json() == {"saved": True, "api_key_set": True}
        assert client.get("/api/settings/nyt").json() == {"api_key_set": True}

    stored = row("SELECT value,secret FROM settings WHERE key='nyt_api_key'")
    assert stored["secret"] == 1
    assert stored["value"].startswith("fernet:")
    assert "nyt-test-key" not in stored["value"]
    outbound = _nyt_request_url(
        "https://api.nytimes.com/svc/books/v3/lists/overview.json?offset=0&api-key=stale"
    )
    assert parse_qs(urlparse(outbound).query) == {
        "offset": ["0"],
        "api-key": ["nyt-test-key"],
    }


def test_nyt_import_uses_key_but_keeps_source_metadata_credential_free(database, monkeypatch):
    with transaction() as con:
        con.execute(
            "INSERT INTO settings(key,value,secret) VALUES('nyt_api_key',?,1) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,secret=1",
            (seal("nyt-import-test-key"),),
        )
    requested = []

    async def fake_fetch(url):
        requested.append(url)
        payload = {
            "results": {
                "lists": [
                    {
                        "books": [
                            {
                                "title": "A Book",
                                "author": "A Writer",
                                "description": "A publisher description.",
                                "list_name": "Hardcover Fiction",
                                "amazon_product_url": "https://books.example/a-book",
                            }
                        ]
                    }
                ]
            }
        }
        return json.dumps(payload).encode(), "application/json"

    monkeypatch.setattr("afterword_engine.ingestion.fetch_bytes", fake_fetch)
    url = "https://api.nytimes.com/svc/books/v3/lists/overview.json"
    _, items = asyncio.run(ingestion._fetch_and_parse_source(url))

    assert parse_qs(urlparse(requested[0]).query) == {
        "api-key": ["nyt-import-test-key"]
    }
    assert items[0]["source_url"] == "https://books.example/a-book"
    assert "nyt-import-test-key" not in json.dumps(items)


def test_existing_subject_cleanup_updates_catalog_artifacts(database):
    with transaction() as con:
        cursor = con.execute(
            "INSERT INTO candidates(title,author,genres,normalized_key) VALUES(?,?,?,?)",
            (
                "Tagged Book",
                "A Writer",
                '["Fiction","nyt:paperback_advice=2007-07-28","89.70 Politics"]',
                "tagged book a writer",
            ),
        )

    assert normalize_stored_candidate_subjects() >= 1
    assert json.loads(row("SELECT genres FROM candidates WHERE id=?", (cursor.lastrowid,))["genres"]) == [
        "Fiction",
        "Politics",
    ]


def test_api_tokens_authenticate_public_api_and_can_be_revoked(database):
    with TestClient(app, headers=authenticated_headers()) as client:
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
        with profiles.auth_connect() as con:
            stored = con.execute("SELECT token_hash FROM api_tokens WHERE id=?", (created.json()["id"],)).fetchone()
        assert stored["token_hash"] != token

        authorized = client.get(
            "/api/v1/recommendations",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert authorized.status_code == 200
        assert len(authorized.json()) == 4
        with profiles.auth_connect() as con:
            last_used = con.execute("SELECT last_used_at FROM api_tokens WHERE id=?", (created.json()["id"],)).fetchone()
        assert last_used["last_used_at"]
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
    with TestClient(app, headers=authenticated_headers()) as client:
        created = client.post("/api/settings/api-tokens", json={"name": "Legacy client"})
        token = created.json()["token"]
        token_id = created.json()["id"]
        with profiles.auth_connect() as con:
            con.execute(
                "UPDATE api_tokens SET token_hash=? WHERE id=?",
                (legacy_hash_api_token(token), token_id),
            )

        authorized = client.get(
            "/api/v1/health",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert authorized.status_code == 200
        with profiles.auth_connect() as con:
            stored = con.execute("SELECT token_hash FROM api_tokens WHERE id=?", (token_id,)).fetchone()
        assert stored["token_hash"] == hash_api_token(token)


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

    with TestClient(app, headers=authenticated_headers()) as client:
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
    with TestClient(app, headers=authenticated_headers()) as client:
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
    with pytest.raises(ValueError, match="CSV row 3.*Broken.*No books were imported"):
        import_goodreads_csv(payload)
    assert row("SELECT COUNT(*) count FROM reads WHERE source='goodreads_csv'")["count"] == 0

def test_goodreads_only_imports_read_shelf(database):
    payload = b"Title,Author,My Rating,Exclusive Shelf\nFinished,Writer,5,read\nFuture,Writer,5,to-read\n"
    assert import_goodreads_csv(payload) == 1
    assert row("SELECT COUNT(*) count FROM reads WHERE title='Future'")["count"] == 0

def test_disabled_source_is_removed_from_discovery_and_scoring(database):
    with transaction() as con: con.execute("UPDATE sources SET enabled=0 WHERE is_default=1")
    assert recommendation_list() == []
    assert asyncio.run(score_all("local", "hashing-768")) == 0
    assert row("SELECT COUNT(*) count FROM candidates WHERE status='recommended'")["count"] == 0

def test_json_ld_graph_and_urls_are_normalized():
    payload = b'''<script type="application/ld+json">{"@graph":[{"@type":["Thing","Book"],"name":"Safe","author":[{"name":"Writer"}],"image":"javascript:alert(1)","url":"https://user:pass@books.example/safe","datePublished":"2027-03-04T00:00:00Z"}]}</script>'''
    items = parse_book_items(payload, "text/html", "https://books.example/list")
    assert items == [{"title":"Safe","author":"Writer","description":"","cover_url":"","source_url":"https://books.example/list","release_date":"2027-03-04","date_kind":"day","genres":[]}]

def test_cover_urls_are_https_and_source_or_provider_scoped():
    assert safe_cover_url("javascript:alert(1)") == ""
    assert safe_cover_url("http://covers.openlibrary.org/b/id/42-L.jpg") == ""
    assert safe_cover_url("https://covers.openlibrary.org/b/id/42-L.jpg")
    assert safe_cover_url("https://books.example/cover.jpg", "https://books.example/list")
    assert safe_cover_url("https://tracking.example/cover.jpg", "https://books.example/list") == ""
    assert fallback_cover_url("A Book", "An Author").startswith("https://placehold.co/")


def test_cover_and_source_url_helpers_deduplicate_parses_and_preserve_edge_cases(monkeypatch):
    import afterword_engine.covers as covers_module

    # Credentials, uppercase/trailing-dot hosts, subdomains, and fragment stripping
    assert safe_cover_url("https://user:pass@covers.openlibrary.org/b/id/42-L.jpg") == ""
    assert (
        safe_cover_url("https://COVERS.OPENLIBRARY.ORG./b/id/42-L.jpg#preview")
        == "https://covers.openlibrary.org./b/id/42-L.jpg"
    )
    assert (
        safe_cover_url("https://is1-ssl.MZSTATIC.com/image/thumb/cover.jpg#")
        == "https://is1-ssl.mzstatic.com/image/thumb/cover.jpg"
    )
    assert (
        safe_cover_url(
            "https://BOOKS.EXAMPLE./art/cover.jpg#top",
            "https://books.example/list",
        )
        == "https://books.example./art/cover.jpg"
    )

    # Weak covers fall back to placeholder_cover_url
    expected_placeholder = covers_module.placeholder_cover_url("A Book", "An Author")
    assert (
        fallback_cover_url(
            "A Book",
            "An Author",
            "https://covers.openlibrary.org/b/isbn/9780000000000-L.jpg",
            "https://openlibrary.org/works/OL1W",
        )
        == expected_placeholder
    )
    assert (
        fallback_cover_url(
            "A Book",
            "An Author",
            "https://placehold.co/640x960?text=Old",
            "https://books.example/list",
        )
        == expected_placeholder
    )

    # Known CDN cover without '#' parses supplied once and skips source_url + copy_with
    httpx_calls = []
    real_httpx_url = covers_module.httpx.URL

    def counting_httpx_url(value, *args, **kwargs):
        httpx_calls.append(value)
        return real_httpx_url(value, *args, **kwargs)

    monkeypatch.setattr(covers_module.httpx, "URL", counting_httpx_url)
    assert (
        fallback_cover_url(
            "A Book",
            "An Author",
            "https://covers.openlibrary.org/b/id/42-L.jpg",
            "https://books.example/list",
        )
        == "https://covers.openlibrary.org/b/id/42-L.jpg"
    )
    assert httpx_calls == ["https://covers.openlibrary.org/b/id/42-L.jpg"]

    # canonical_book_source_url parses source_url once and promotes Open Library list links when work ID exists
    urlparse_calls = []
    real_urlparse = covers_module.urlparse

    def counting_urlparse(value, *args, **kwargs):
        urlparse_calls.append(value)
        return real_urlparse(value, *args, **kwargs)

    monkeypatch.setattr(covers_module, "urlparse", counting_urlparse)
    assert (
        canonical_book_source_url(
            "A Book",
            "A Writer",
            "https://www.openlibrary.org./people/reader/lists/OL2L",
            item={"quality_work_id": "/works/OL99W", "quality_provider": "openlibrary"},
        )
        == "https://openlibrary.org/works/OL99W"
    )
    assert (
        canonical_book_source_url(
            "A Book",
            "A Writer",
            "https://openlibrary.org/people/reader/lists/OL2L",
            item={"quality_work_id": "", "quality_provider": ""},
        )
        == "https://openlibrary.org/search?title=A+Book&author=A+Writer"
    )
    assert len(urlparse_calls) == 2


def test_tracked_recommendations_defers_url_formatting_until_after_pagination(database, monkeypatch):
    import afterword_engine.covers as covers_module
    import afterword_engine.main as main_module
    from afterword_engine.decision_evidence import load_decision_evidence

    with transaction() as con:
        con.execute("DELETE FROM candidates")
        con.execute("DELETE FROM candidate_quality")
        source_id = con.execute("SELECT id FROM sources WHERE enabled=1 LIMIT 1").fetchone()["id"]
        special_cases = [
            (
                "Top Promoted List Book",
                "Writer One",
                "https://covers.openlibrary.org/b/id/501-L.jpg",
                "https://openlibrary.org/people/reader/lists/OL10L",
                "recommended",
                99.0,
                "/works/OL501W",
            ),
            (
                "Top Unpromoted List Book",
                "Writer Two",
                "https://covers.openlibrary.org/b/isbn/9780000000002-L.jpg",
                "https://openlibrary.org/people/reader/lists/OL11L",
                "recommended",
                98.0,
                "",
            ),
            (
                "Top Fragment Publisher Book",
                "Writer Three",
                "https://BOOKS.EXAMPLE./covers/three.jpg#hero",
                "https://books.example/list/three",
                "recommended",
                97.0,
                "",
            ),
            (
                "Top ISBN Search Book",
                "Writer Four",
                "https://placehold.co/600x900",
                "https://openlibrary.org/isbn/9780000000004",
                "recommended",
                96.0,
                "",
            ),
        ]
        for idx in range(196):
            special_cases.append(
                (
                    f"Pool Tail Book {idx}",
                    f"Tail Author {idx}",
                    f"https://covers.openlibrary.org/b/id/{1000 + idx}-L.jpg",
                    f"https://openlibrary.org/people/reader/lists/OL{1000 + idx}L",
                    "recommended",
                    round(90.0 - idx * 0.1, 3),
                    "",
                )
            )
        special_cases.extend(
            [
                (
                    "Saved Weak Cover Book",
                    "Saved Author",
                    "https://covers.openlibrary.org/b/isbn/9780000000099-L.jpg",
                    "https://openlibrary.org/isbn/9780000000099",
                    "saved",
                    85.0,
                    "",
                ),
                (
                    "Rejected List Book",
                    "Rejected Author",
                    "https://covers.openlibrary.org/b/id/900-L.jpg#frag",
                    "https://openlibrary.org/people/reader/lists/OL900L",
                    "rejected",
                    40.0,
                    "/works/OL900W",
                ),
            ]
        )
        for title, author, cover_url, source_url, status, score_val, work_id in special_cases:
            cur = con.execute(
                """INSERT INTO candidates(
                    title,author,description,cover_url,source_url,source_id,
                    release_date,date_kind,genres,score,explanation,status,normalized_key
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    title,
                    author,
                    f"Summary for {title}",
                    cover_url,
                    source_url,
                    source_id,
                    "2026-02-01",
                    "day",
                    json.dumps(["Fiction"]),
                    score_val,
                    json.dumps(["Matched"]),
                    status,
                    normalize_key(title, author),
                ),
            )
            con.execute(
                """INSERT OR REPLACE INTO candidate_quality(
                    candidate_id,quality_status,quality_score,audit_version,metadata_version,work_id,provider
                ) VALUES(?,?,?,?,?,?,?)""",
                (
                    cur.lastrowid,
                    "accepted",
                    1.0,
                    "builtin-curated-v1",
                    "catalog-metadata-v1",
                    work_id,
                    "openlibrary" if work_id else "",
                ),
            )
            if status == "rejected":
                con.execute(
                    "INSERT INTO feedback(candidate_id,action) VALUES(?,?)",
                    (cur.lastrowid, "reject"),
                )

    unformatted = recommendation_list(status="recommended", limit=4, format_urls=False)
    assert unformatted[0]["genres"] == ["Fiction"]
    assert unformatted[0]["explanation"] == ["Matched"]
    assert unformatted[0]["source_url"] == "https://openlibrary.org/people/reader/lists/OL10L"

    httpx_calls = 0
    urlparse_calls = 0
    real_httpx_url = covers_module.httpx.URL
    real_covers_urlparse = covers_module.urlparse
    real_main_urlparse = main_module.urlparse

    def count_httpx_url(*args, **kwargs):
        nonlocal httpx_calls
        httpx_calls += 1
        return real_httpx_url(*args, **kwargs)

    def count_covers_urlparse(*args, **kwargs):
        nonlocal urlparse_calls
        urlparse_calls += 1
        return real_covers_urlparse(*args, **kwargs)

    def count_main_urlparse(*args, **kwargs):
        nonlocal urlparse_calls
        urlparse_calls += 1
        return real_main_urlparse(*args, **kwargs)

    monkeypatch.setattr(covers_module.httpx, "URL", count_httpx_url)
    monkeypatch.setattr(covers_module, "urlparse", count_covers_urlparse)
    monkeypatch.setattr(main_module, "urlparse", count_main_urlparse)

    served, run_id = main_module.tracked_recommendations(status="recommended", limit=8, offset=0)
    assert len(served) == 8
    # Only the 8 served recommendations are URL-formatted (8 source URLs; 8 covers + 1 source-host check)
    assert urlparse_calls == 8
    assert httpx_calls == 9

    assert served[0]["cover_url"] == "https://covers.openlibrary.org/b/id/501-L.jpg"
    assert served[0]["source_url"] == "https://openlibrary.org/works/OL501W"
    assert served[1]["cover_url"].startswith("https://placehold.co/")
    assert (
        served[1]["source_url"]
        == "https://openlibrary.org/search?title=Top+Unpromoted+List+Book&author=Writer+Two"
    )
    assert served[2]["cover_url"] == "https://books.example./covers/three.jpg"
    assert served[2]["source_url"] == "https://books.example/list/three"
    assert served[3]["cover_url"].startswith("https://placehold.co/")
    assert (
        served[3]["source_url"]
        == "https://openlibrary.org/search?title=Top+ISBN+Search+Book&author=Writer+Four"
    )

    with main_module.connect() as con:
        evidence = load_decision_evidence(con, run_id)
    assert evidence is not None and evidence["capture_status"] == "complete"
    assert len(evidence["decision"]["pools"]["base"]) == 200
    assert len(evidence["decision"]["pools"]["final"]) == 200
    assert "cover_url" not in evidence["decision"]["pools"]["final"][0]
    assert "source_url" not in evidence["decision"]["pools"]["final"][0]
    assert len(evidence["served_exposures"]) == 8
    exposure_snapshot = json.loads(evidence["served_exposures"][0]["candidate_snapshot_json"])
    assert "cover_url" not in exposure_snapshot
    assert "source_url" not in exposure_snapshot

    # Non-personalized path (can_personalize is False) still formats URLs for the returned slice
    saved_items, _ = main_module.tracked_recommendations(status="saved", limit=8, offset=0)
    assert len(saved_items) == 1
    assert saved_items[0]["cover_url"].startswith("https://placehold.co/")
    assert (
        saved_items[0]["source_url"]
        == "https://openlibrary.org/search?title=Saved+Weak+Cover+Book&author=Saved+Author"
    )

    decisions = recommendation_list(status="decisions", limit=8)
    assert len(decisions) == 1
    assert decisions[0]["cover_url"] == "https://covers.openlibrary.org/b/id/900-L.jpg"
    assert decisions[0]["source_url"] == "https://openlibrary.org/works/OL900W"

    # Overview path (status=None, recommended_limit=4) formats only 4 recommended + 1 saved
    httpx_calls = 0
    urlparse_calls = 0
    overview_slice, _ = main_module.tracked_recommendations(limit=24, recommended_limit=4)
    assert len(overview_slice) == 5
    assert urlparse_calls == 5

    # Personalization failure fallback still formats URLs for the returned slice
    def broken_personalize(*_args, **_kwargs):
        raise RuntimeError("simulated personalization failure")

    monkeypatch.setattr(main_module, "personalize_recommendations", broken_personalize)
    httpx_calls = 0
    urlparse_calls = 0
    fallback_served, _ = main_module.tracked_recommendations(status="recommended", limit=4, offset=0)
    assert len(fallback_served) == 4
    assert urlparse_calls == 4
    assert fallback_served[0]["source_url"] == "https://openlibrary.org/works/OL501W"
    assert fallback_served[2]["cover_url"] == "https://books.example./covers/three.jpg"

@respx.mock
def test_book_metadata_lookup_supplies_summary_and_year():
    route = respx.get(OPEN_LIBRARY_SEARCH).mock(
        return_value=httpx.Response(
            200,
            json={
                "docs": [
                    {
                        "title": "A Book",
                        "author_name": ["An Author"],
                        "cover_i": 12345,
                        "first_publish_year": 1998,
                        "first_sentence": ["A quiet story about becoming brave."],
                        "subject": ["Literary fiction", "Coming of age"],
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
                        "author_name": ["An Author"],
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


@respx.mock
def test_metadata_rejects_wrong_author_and_falls_back_to_verified_google_record():
    open_library = respx.get(OPEN_LIBRARY_SEARCH).mock(
        return_value=httpx.Response(
            200,
            json={"docs": [
                {"key": "/works/OLWRONG", "title": "A Book", "author_name": ["B Writer"], "first_sentence": ["Wrong book."]},
                {"key": "/works/OLNOAUTHOR", "title": "A Book", "first_sentence": ["No author evidence."]},
            ]},
        )
    )
    google = respx.get(GOOGLE_BOOKS_SEARCH).mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "volume-1",
                        "volumeInfo": {
                            "title": "A Book",
                            "authors": ["An Author"],
                            "description": "A verified description.",
                            "categories": ["Literary fiction", "Mystery"],
                        },
                    }
                ]
            },
        )
    )

    metadata = asyncio.run(resolve_book_metadata("A Book", "An Author"))

    assert open_library.called and google.called
    assert metadata["description"] == "A verified description."
    assert metadata["genres"] == ["Literary fiction", "Mystery"]
    assert metadata["provider"] == "google_books"
    assert metadata["provider_id"] == "volume-1"


@respx.mock
def test_metadata_does_not_use_an_exact_provider_id_with_wrong_author():
    respx.get(OPEN_LIBRARY_SEARCH).mock(
        return_value=httpx.Response(
            200,
            json={"docs": [{"key": "/works/OL1W", "title": "A Book", "author_name": ["B Writer"], "first_sentence": ["Wrong book."], "subject": ["Wrong genre"]}]},
        )
    )
    respx.get(GOOGLE_BOOKS_SEARCH).mock(
        return_value=httpx.Response(
            200,
            json={"items": [{"id": "volume-1", "volumeInfo": {"title": "A Book", "authors": ["An Author"], "description": "Also not the recorded provider."}}]},
        )
    )

    metadata = asyncio.run(
        resolve_book_metadata(
            "A Book",
            "An Author",
            description="Curated description.",
            genres=["Curated genre"],
            expected_provider="openlibrary",
            expected_provider_id="/works/OL1W",
        )
    )

    assert metadata["description"] == "Curated description."
    assert metadata["genres"] == []
    assert metadata["provider"] == ""


@respx.mock
def test_refresh_metadata_preserves_curated_description_and_serializes_genres(database):
    with transaction() as con:
        source_id = con.execute("SELECT id FROM sources WHERE url='builtin://upcoming'").fetchone()[0]
        candidate_id = con.execute(
            """INSERT INTO candidates(title,author,description,cover_url,source_url,source_id,genres,normalized_key)
            VALUES(?,?,?,?,?,?,?,?)""",
            (
                "A Curated Book",
                "An Author",
                "Carefully edited publisher copy.",
                "https://placehold.co/640x960",
                "https://example.com/book",
                source_id,
                '[]',
                "curated-metadata-backfill",
            ),
        ).lastrowid
    respx.get(OPEN_LIBRARY_SEARCH).mock(
        return_value=httpx.Response(
            200,
            json={
                "docs": [
                    {
                        "key": "/works/OLCURATED",
                        "title": "A Curated Book",
                        "author_name": ["An Author"],
                        "cover_i": 42,
                        "first_publish_year": 2001,
                    }
                ]
            },
        )
    )
    respx.get(GOOGLE_BOOKS_SEARCH).mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "volume-fallback",
                        "volumeInfo": {
                            "title": "A Curated Book",
                            "authors": ["An Author"],
                            "description": "A provider summary.",
                            "categories": ["Literary fiction", "Family life"],
                        },
                    }
                ]
            },
        )
    )
    with transaction() as con:
        con.execute(
            "UPDATE candidate_quality SET provider='openlibrary',provider_id='/works/OLCURATED',work_id='/works/OLCURATED' WHERE candidate_id=?",
            (candidate_id,),
        )

    changed = asyncio.run(refresh_missing_candidate_metadata())

    candidate = row("SELECT * FROM candidates WHERE id=?", (candidate_id,))
    quality = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (candidate_id,))
    assert changed == 1
    assert candidate["description"] == "Carefully edited publisher copy."
    assert json.loads(candidate["genres"]) == ["Literary fiction", "Family life"]
    assert candidate["cover_url"] == "https://covers.openlibrary.org/b/id/42-L.jpg"
    assert quality["provider"] == "openlibrary"
    assert quality["provider_id"] == "/works/OLCURATED"
    assert quality["work_id"] == "/works/OLCURATED"
    # The legacy metadata provider is the only prior description attribution
    # available to older rows, so adding genres/covers must not replace it.
    assert quality["metadata_provider"] == ""
    assert quality["metadata_provider_id"] == ""
    assert quality["metadata_checked_at"]


def test_metadata_refresh_rotates_through_sparse_candidates(database, monkeypatch):
    with transaction() as con:
        source_id = con.execute("SELECT id FROM sources WHERE url='builtin://upcoming'").fetchone()[0]
        candidate_ids = []
        for score in range(51):
            candidate_ids.append(
                con.execute(
                    """INSERT INTO candidates(title,author,source_url,source_id,score,normalized_key)
                    VALUES(?,?,?,?,?,?)""",
                    (f"Sparse book {score}", "A Writer", "https://example.com/book", source_id, score, f"sparse-{score}"),
                ).lastrowid
            )

    batches = []

    async def no_match(items):
        batches.append([item["id"] for item in items])
        return items

    monkeypatch.setattr(ingestion, "enrich_book_metadata", no_match)
    assert asyncio.run(refresh_missing_candidate_metadata()) == 0
    assert len(batches[0]) == 50
    remaining = set(candidate_ids) - set(batches[0])
    assert len(remaining) == 1

    assert asyncio.run(refresh_missing_candidate_metadata()) == 0
    assert remaining.issubset(set(batches[1]))

    metadata_checked = rows(
        "SELECT metadata_checked_at FROM candidate_quality WHERE candidate_id IN (%s)" % ",".join("?" for _ in candidate_ids),
        tuple(candidate_ids),
    )
    assert len(metadata_checked) == 51
    assert all(item["metadata_checked_at"] for item in metadata_checked)

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


@pytest.mark.parametrize(
    ("blob", "dimensions"),
    [
        (np.asarray([float("nan"), 0.0, 1.0], dtype=np.float32).tobytes(), 3),
        (b"", 3),
        (b"\x01\x02", 3),
        (np.asarray([0.0, 1.0, 2.0], dtype=np.float32).tobytes(), 2),
    ],
)
def test_corrupt_matching_hash_cache_is_reembedded(database, blob, dimensions):
    import numpy as np

    from afterword_engine.embeddings import content_hash
    from afterword_engine.scoring import document

    class FixedEmbedder:
        name, model = "cache-repair", "fixture-v1"

        def __init__(self):
            self.calls = 0

        async def embed(self, texts):
            self.calls += 1
            return [[0.25, 0.5, 0.75] for _ in texts]

    item = {"id": 987, "title": "Cache repair", "author": "Fixture", "description": "A stable description."}
    digest = content_hash(document(item))
    with transaction() as con:
        con.execute(
            "INSERT INTO embeddings(entity_type,entity_id,backend,model,vector,dimensions,content_hash) VALUES(?,?,?,?,?,?,?)",
            ("candidate", item["id"], "cache-repair", "fixture-v1", blob, dimensions, digest),
        )

    embedder = FixedEmbedder()
    result = asyncio.run(cached_vectors(embedder, "candidate", [item]))
    stored = row(
        "SELECT vector,dimensions,content_hash FROM embeddings WHERE entity_type='candidate' AND entity_id=? AND backend=? AND model=?",
        (item["id"], embedder.name, embedder.model),
    )
    vector = np.frombuffer(stored["vector"], dtype=np.float32)
    assert embedder.calls == 1
    assert result == [[0.25, 0.5, 0.75]]
    assert stored["dimensions"] == 3
    assert stored["content_hash"] == digest
    assert np.isfinite(vector).all()


def test_zero_vector_is_a_valid_cached_embedding(database):
    import numpy as np

    from afterword_engine.embeddings import content_hash
    from afterword_engine.scoring import document

    class UnexpectedEmbedder:
        name, model = "zero-cache", "fixture-v1"

        async def embed(self, texts):
            raise AssertionError("A valid cached zero vector should be reused")

    item = {"id": 988, "title": "Empty text", "author": "", "description": ""}
    digest = content_hash(document(item))
    with transaction() as con:
        con.execute(
            "INSERT INTO embeddings(entity_type,entity_id,backend,model,vector,dimensions,content_hash) VALUES(?,?,?,?,?,?,?)",
            ("candidate", item["id"], "zero-cache", "fixture-v1", np.zeros(3, dtype=np.float32).tobytes(), 3, digest),
        )
    assert asyncio.run(cached_vectors(UnexpectedEmbedder(), "candidate", [item])) == [[0.0, 0.0, 0.0]]


def test_cached_and_new_embedding_dimension_mismatch_fails_before_persist(database):
    import numpy as np

    from afterword_engine.embeddings import content_hash
    from afterword_engine.scoring import document

    class WrongDimensionEmbedder:
        name, model = "dimension-check", "fixture-v1"

        async def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    cached_item = {"id": 989, "title": "Valid cached", "author": "Fixture"}
    missing_item = {"id": 990, "title": "New vector", "author": "Fixture"}
    with transaction() as con:
        con.execute(
            "INSERT INTO embeddings(entity_type,entity_id,backend,model,vector,dimensions,content_hash) VALUES(?,?,?,?,?,?,?)",
            ("candidate", cached_item["id"], "dimension-check", "fixture-v1", np.asarray([1.0, 0.0, 0.0], dtype=np.float32).tobytes(), 3, content_hash(document(cached_item))),
        )
    with pytest.raises(ValueError, match="differ from valid cached vectors"):
        asyncio.run(cached_vectors(WrongDimensionEmbedder(), "candidate", [cached_item, missing_item]))
    assert row(
        "SELECT 1 present FROM embeddings WHERE entity_type='candidate' AND entity_id=? AND backend='dimension-check'",
        (missing_item["id"],),
    ) is None


def test_expected_embedding_dimension_rejects_provider_output_before_persist(database):
    class TwoDimEmbedder:
        name, model = "expected-dimension", "fixture-v1"

        async def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    item = {"id": 992, "title": "Expected width", "author": "Fixture"}
    with pytest.raises(ValueError, match="provider dimensions differ from expected"):
        asyncio.run(
            cached_vectors(
                TwoDimEmbedder(),
                "candidate",
                [item],
                expected_dimensions=3,
            )
        )
    assert row(
        "SELECT 1 present FROM embeddings WHERE entity_type='candidate' AND entity_id=? AND backend='expected-dimension'",
        (item["id"],),
    ) is None


def test_expected_embedding_dimension_rejects_valid_cached_vector(database):
    import numpy as np

    from afterword_engine.embeddings import content_hash
    from afterword_engine.scoring import document

    class UnexpectedEmbedder:
        name, model = "expected-cached-dimension", "fixture-v1"

        async def embed(self, _texts):
            raise AssertionError("A valid but incompatible cached vector must abstain")

    item = {"id": 993, "title": "Cached width", "author": "Fixture"}
    with transaction() as con:
        con.execute(
            "INSERT INTO embeddings(entity_type,entity_id,backend,model,vector,dimensions,content_hash) VALUES(?,?,?,?,?,?,?)",
            (
                "candidate",
                item["id"],
                "expected-cached-dimension",
                "fixture-v1",
                np.asarray([1.0, 0.0], dtype=np.float32).tobytes(),
                2,
                content_hash(document(item)),
            ),
        )
    with pytest.raises(ValueError, match="Cached embedding dimensions differ from expected"):
        asyncio.run(
            cached_vectors(
                UnexpectedEmbedder(),
                "candidate",
                [item],
                expected_dimensions=3,
            )
        )


def test_float32_overflow_from_finite_provider_values_is_not_persisted(database):
    class OverflowEmbedder:
        name, model = "overflow-check", "fixture-v1"

        async def embed(self, texts):
            return [[1e100, 0.0, 1.0] for _ in texts]

    item = {"id": 991, "title": "Overflow", "author": "Fixture"}
    with pytest.raises(ValueError, match="invalid or inconsistent"):
        asyncio.run(cached_vectors(OverflowEmbedder(), "candidate", [item]))
    assert row(
        "SELECT 1 present FROM embeddings WHERE entity_type='candidate' AND entity_id=? AND backend='overflow-check'",
        (item["id"],),
    ) is None


def test_score_all_recovers_from_corrupt_matching_hash_cache(database):
    import numpy as np

    from afterword_engine.embeddings import content_hash, get_embedder
    from afterword_engine.scoring import document

    embedder = get_embedder("local", "hashing-768")
    assert asyncio.run(score_all(embedder=embedder)) == 4
    baseline_scores = [float(candidate["score"]) for candidate in rows("SELECT score FROM candidates ORDER BY id")]
    assert len(baseline_scores) == 4
    assert all(np.isfinite(score) for score in baseline_scores)
    item = rows("SELECT id,title,author,description,genres FROM candidates ORDER BY id LIMIT 1")[0]
    digest = content_hash(document(item))
    with transaction() as con:
        con.execute(
            "UPDATE embeddings SET vector=?,dimensions=768,content_hash=? WHERE entity_type='candidate' AND entity_id=? AND backend=? AND model=?",
            (np.asarray([float("nan")] + [0.0] * 767, dtype=np.float32).tobytes(), digest, item["id"], embedder.name, embedder.model),
        )
    assert asyncio.run(score_all(embedder=embedder)) == 4
    repaired = row(
        "SELECT vector,dimensions,content_hash FROM embeddings WHERE entity_type='candidate' AND entity_id=? AND backend=? AND model=?",
        (item["id"], embedder.name, embedder.model),
    )
    scores = [float(candidate["score"]) for candidate in rows("SELECT score FROM candidates ORDER BY id")]
    assert repaired["dimensions"] == 768
    assert repaired["content_hash"] == digest
    assert np.isfinite(np.frombuffer(repaired["vector"], dtype=np.float32)).all()
    assert all(np.isfinite(score) for score in scores)
    assert scores == baseline_scores


def test_score_all_rejects_candidate_provider_dimension_change_before_persist(database):
    import numpy as np

    from afterword_engine.embeddings import content_hash
    from afterword_engine.scoring import document

    class TwoDimEmbedder:
        name, model = "cross-kind-provider", "fixture-v1"

        async def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    reads = rows("SELECT * FROM reads WHERE rating BETWEEN 1 AND 5 ORDER BY id")
    assert reads
    with transaction() as con:
        for item in reads:
            con.execute(
                "INSERT INTO embeddings(entity_type,entity_id,backend,model,vector,dimensions,content_hash) VALUES(?,?,?,?,?,?,?)",
                (
                    "read",
                    item["id"],
                    "cross-kind-provider",
                    "fixture-v1",
                    np.asarray([1.0, 0.0, 0.0], dtype=np.float32).tobytes(),
                    3,
                    content_hash(document(item)),
                ),
            )

    with pytest.raises(ValueError, match="provider dimensions differ from expected"):
        asyncio.run(score_all(embedder=TwoDimEmbedder()))
    assert row(
        "SELECT COUNT(*) count FROM embeddings WHERE entity_type='candidate' AND backend='cross-kind-provider'"
    )["count"] == 0


def test_score_all_rejects_cross_kind_valid_cache_dimension_mismatch(database):
    import numpy as np

    from afterword_engine.embeddings import content_hash
    from afterword_engine.scoring import document

    class UnexpectedEmbedder:
        name, model = "cross-kind-cache", "fixture-v1"

        def __init__(self):
            self.calls = 0

        async def embed(self, texts):
            self.calls += 1
            return [[1.0, 0.0, 0.0] for _ in texts]

    reads = rows("SELECT * FROM reads WHERE rating BETWEEN 1 AND 5 ORDER BY id")
    candidates = rows("SELECT * FROM candidates ORDER BY id")
    assert reads and candidates
    with transaction() as con:
        for item in reads:
            con.execute(
                "INSERT INTO embeddings(entity_type,entity_id,backend,model,vector,dimensions,content_hash) VALUES(?,?,?,?,?,?,?)",
                (
                    "read",
                    item["id"],
                    "cross-kind-cache",
                    "fixture-v1",
                    np.asarray([1.0, 0.0, 0.0], dtype=np.float32).tobytes(),
                    3,
                    content_hash(document(item)),
                ),
            )
        for item in candidates:
            con.execute(
                "INSERT INTO embeddings(entity_type,entity_id,backend,model,vector,dimensions,content_hash) VALUES(?,?,?,?,?,?,?)",
                (
                    "candidate",
                    item["id"],
                    "cross-kind-cache",
                    "fixture-v1",
                    np.asarray([1.0, 0.0], dtype=np.float32).tobytes(),
                    2,
                    content_hash(document(item)),
                ),
            )

    embedder = UnexpectedEmbedder()
    with pytest.raises(ValueError, match="Cached embedding dimensions differ from expected"):
        asyncio.run(score_all(embedder=embedder))
    assert embedder.calls == 0


def test_forced_nonpersistent_rebuild_rejects_cross_kind_dimension_change(database, monkeypatch):
    class ChangingDimensionEmbedder:
        name, model = "forced-cross-kind", "fixture-v1"

        def __init__(self):
            self.calls = 0

        async def embed(self, texts):
            self.calls += 1
            width = 3 if self.calls == 1 else 2
            return [[1.0] + [0.0] * (width - 1) for _ in texts]

    embedder = ChangingDimensionEmbedder()
    monkeypatch.setattr(
        "afterword_engine.scoring.get_embedder",
        lambda *_args, **_kwargs: embedder,
    )
    with transaction() as con:
        con.execute(
            "INSERT INTO embeddings(entity_type,entity_id,backend,model,vector,dimensions,content_hash) VALUES(?,?,?,?,?,?,?)",
            (
                "read",
                9999,
                "forced-cross-kind",
                "fixture-v1",
                np.asarray([0.5], dtype=np.float32).tobytes(),
                1,
                "old-cache-entry",
            ),
        )
    with pytest.raises(ValueError, match="provider dimensions differ from expected"):
        asyncio.run(rebuild_all_embeddings("forced-cross-kind", "fixture-v1"))
    assert embedder.calls == 2
    retained = row(
        "SELECT vector,dimensions,content_hash FROM embeddings "
        "WHERE backend='forced-cross-kind' AND entity_id=9999"
    )
    assert retained["vector"] == np.asarray([0.5], dtype=np.float32).tobytes()
    assert retained["dimensions"] == 1
    assert retained["content_hash"] == "old-cache-entry"
    assert row(
        "SELECT COUNT(*) count FROM embeddings WHERE backend='forced-cross-kind'"
    )["count"] == 1


def test_reading_history_score_path_rejects_cross_kind_dimension_change(database, monkeypatch):
    import numpy as np

    from afterword_engine.embeddings import content_hash
    from afterword_engine.main import reading_history_scores
    from afterword_engine.scoring import document

    class TwoDimEmbedder:
        name, model = "history-cross-kind", "fixture-v1"

        async def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    reads = rows("SELECT * FROM reads WHERE rating BETWEEN 1 AND 5 ORDER BY id")
    assert reads
    with transaction() as con:
        for item in reads:
            con.execute(
                "INSERT INTO embeddings(entity_type,entity_id,backend,model,vector,dimensions,content_hash) VALUES(?,?,?,?,?,?,?)",
                (
                    "read",
                    item["id"],
                    "history-cross-kind",
                    "fixture-v1",
                    np.asarray([1.0, 0.0, 0.0], dtype=np.float32).tobytes(),
                    3,
                    content_hash(document(item)),
                ),
            )
    monkeypatch.setattr(
        "afterword_engine.main.get_embedder",
        lambda *_args, **_kwargs: TwoDimEmbedder(),
    )
    with pytest.raises(ValueError, match="provider dimensions differ from expected"):
        asyncio.run(reading_history_scores(offset=0, limit=20))
    assert row(
        "SELECT COUNT(*) count FROM embeddings WHERE entity_type='read_candidate' AND backend='history-cross-kind'"
    )["count"] == 0

def test_initialize_is_versioned_and_uses_actual_builtin_source_id(tmp_path):
    settings.db = str(tmp_path / "legacy.db")
    import sqlite3
    with sqlite3.connect(settings.db) as con:
        con.execute("CREATE TABLE sources (id INTEGER PRIMARY KEY, name TEXT NOT NULL, url TEXT NOT NULL UNIQUE, kind TEXT NOT NULL DEFAULT 'web', enabled INTEGER NOT NULL DEFAULT 1, is_default INTEGER NOT NULL DEFAULT 0, weight REAL NOT NULL DEFAULT 1, last_status TEXT, last_scanned_at TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
        con.execute("INSERT INTO sources(id,name,url) VALUES(7,'Existing','https://example.com')")
    initialize()
    assert row("SELECT COUNT(*) count FROM schema_migrations")["count"] == len(MIGRATIONS)
    assert row(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='api_tokens'"
    )["name"] == "api_tokens"
    assert row("SELECT source_id FROM candidates LIMIT 1")["source_id"] != 1


def test_failed_migration_rolls_back_ddl_and_can_be_retried(tmp_path, monkeypatch):
    settings.db = str(tmp_path / "rollback.db")
    initialize(seed_demo=False)
    version = max(item[0] for item in MIGRATIONS) + 1
    monkeypatch.setattr(
        engine_database,
        "MIGRATIONS",
        [*MIGRATIONS, (version, "CREATE TABLE migration_probe(value TEXT); SELECT * FROM missing_migration_table;")],
    )

    with pytest.raises(engine_database.sqlite3.DatabaseError, match=f"Schema migration {version} failed"):
        initialize(seed_demo=False)
    assert row(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migration_probe'"
    ) is None
    assert row("SELECT 1 FROM schema_migrations WHERE version=?", (version,)) is None

    monkeypatch.setattr(
        engine_database,
        "MIGRATIONS",
        [*MIGRATIONS, (version, "CREATE TABLE migration_probe(value TEXT);")],
    )
    initialize(seed_demo=False)
    assert row(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='migration_probe'"
    ) is not None
    assert row("SELECT 1 FROM schema_migrations WHERE version=?", (version,)) is not None


def test_concurrent_initializers_recheck_migration_versions_under_lock(tmp_path):
    settings.db = str(tmp_path / "concurrent-migrations.db")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(initialize, seed_demo=False) for _ in range(2)]
        for future in futures:
            future.result(timeout=10)
    assert row("SELECT COUNT(*) count FROM schema_migrations")["count"] == len(MIGRATIONS)


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
    assert row("SELECT COUNT(*) count FROM schema_migrations")["count"] == len(MIGRATIONS)
    _assert_retired_llm_removed()
    assert {item["version"] for item in rows("SELECT version FROM schema_migrations")} >= {6, 9, 12}
    assert row("SELECT name FROM sqlite_master WHERE type='table' AND name='api_tokens'")["name"] == "api_tokens"
    assert row("SELECT title FROM candidates WHERE normalized_key=?", ("existing book existing author",))["title"] == "Existing book"
    existing_quality = row(
        "SELECT metadata_confidence FROM candidate_quality "
        "WHERE candidate_id=(SELECT id FROM candidates WHERE normalized_key=?)",
        ("existing book existing author",),
    )
    assert existing_quality["metadata_confidence"] == 0.5

    with transaction() as con:
        con.execute(
            "INSERT INTO candidates(title,author,source_id,normalized_key) VALUES(?,?,?,?)",
            ("New book", "New author", source_id, "new book new author"),
        )
    new_quality = row(
        "SELECT quality_status,metadata_confidence FROM candidate_quality "
        "WHERE candidate_id=(SELECT id FROM candidates WHERE normalized_key=?)",
        ("new book new author",),
    )
    assert new_quality == {"quality_status": "pending", "metadata_confidence": 0.5}

    initialize()
    assert row("SELECT COUNT(*) count FROM schema_migrations")["count"] == len(MIGRATIONS)
    assert row("SELECT COUNT(*) count FROM candidates WHERE normalized_key=?", ("existing book existing author",))["count"] == 1


def test_initialize_upgrades_v25_database_to_index_goodreads_shelf_title_author(tmp_path, monkeypatch):
    settings.db = str(tmp_path / "v25.db")
    import sqlite3

    with sqlite3.connect(settings.db) as con:
        con.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        for version, script in MIGRATIONS:
            if version > 25:
                break
            con.executescript(script)
            con.execute("INSERT INTO schema_migrations(version) VALUES(?)", (version,))
        assert {item[0] for item in con.execute("SELECT version FROM schema_migrations")} == set(range(1, 26))
        assert (
            con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_goodreads_shelf_items_title_author'"
            ).fetchone()
            is None
        )
        pre_upgrade_plan = [
            item[3]
            for item in con.execute(
                "EXPLAIN QUERY PLAN DELETE FROM goodreads_shelf_items WHERE title=? AND author=?",
                ("CSV Promoted", "Author One"),
            )
        ]
        assert any("SCAN goodreads_shelf_items" in detail for detail in pre_upgrade_plan)
        con.executemany(
            "INSERT INTO goodreads_shelf_items(shelf, title, author) VALUES(?, ?, ?)",
            [
                ("to-read", "CSV Promoted", "Author One"),
                ("currently-reading", "CSV Promoted", "Author One"),
                ("to-read", "RSS Promoted", "Author Two"),
                ("currently-reading", "RSS Promoted", "Author Two"),
                ("to-read", "Still Queued", "Author Three"),
            ],
        )

    initialize(seed_demo=False)
    assert row("SELECT COUNT(*) count FROM schema_migrations")["count"] == len(MIGRATIONS)
    assert row("SELECT 1 FROM schema_migrations WHERE version=26") is not None
    post_upgrade_plan = rows(
        "EXPLAIN QUERY PLAN DELETE FROM goodreads_shelf_items WHERE title=? AND author=?",
        ("CSV Promoted", "Author One"),
    )
    assert any(
        "SEARCH goodreads_shelf_items USING INDEX idx_goodreads_shelf_items_title_author"
        in item["detail"]
        for item in post_upgrade_plan
    )

    # Rerunning initialize() remains idempotent with IF NOT EXISTS.
    initialize(seed_demo=False)
    assert row("SELECT COUNT(*) count FROM schema_migrations")["count"] == len(MIGRATIONS)

    csv_count = import_goodreads_csv(
        b"Title,Author,My Rating,Date Read,Date Added,Exclusive Shelf,ISBN13\n"
        b"CSV Promoted,Author One,5,2026/03/01,2026/02/01,read,=\"9780593135204\"\n"
    )
    assert csv_count == 1
    assert (
        row(
            "SELECT COUNT(*) count FROM goodreads_shelf_items WHERE title=? AND author=?",
            ("CSV Promoted", "Author One"),
        )["count"]
        == 0
    )

    async def fetch_bytes(*_args, **_kwargs):
        return b"", "application/rss+xml"

    class Feed:
        entries = [
            {
                "title": "RSS Promoted",
                "author_name": "Author Two",
                "user_rating": "4",
                "user_read_at": "2026/03/02",
                "isbn13": "9781635575637",
            }
        ]

    monkeypatch.setattr(ingestion, "fetch_bytes", fetch_bytes)
    monkeypatch.setattr(ingestion.feedparser, "parse", lambda _content: Feed())
    rss_count = asyncio.run(
        import_goodreads_rss("https://www.goodreads.com/review/list_rss/reader?shelf=read")
    )
    assert rss_count == 1
    remaining = rows("SELECT shelf, title, author FROM goodreads_shelf_items ORDER BY shelf, title")
    assert remaining == [{"shelf": "to-read", "title": "Still Queued", "author": "Author Three"}]


def test_initialize_upgrades_v29_database_to_add_additional_authors(tmp_path):
    from afterword_engine.identity import ReadIdentityIndex, read_metadata_identity_hash

    settings.db = str(tmp_path / "v29.db")
    import sqlite3

    with sqlite3.connect(settings.db) as con:
        con.row_factory = sqlite3.Row
        con.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        for version, script in MIGRATIONS:
            if version > 29:
                break
            con.executescript(script)
            con.execute("INSERT INTO schema_migrations(version) VALUES(?)", (version,))
        assert {item[0] for item in con.execute("SELECT version FROM schema_migrations")} == set(range(1, 30))
        assert "additional_authors" not in {
            item["name"] for item in con.execute("PRAGMA table_info(reads)")
        }
        assert "additional_authors" not in {
            item["name"] for item in con.execute("PRAGMA table_info(goodreads_shelf_items)")
        }
        read_id = con.execute(
            "INSERT INTO reads(title,author,rating,read_at,isbn,source,openlibrary_work_id) "
            "VALUES(?,?,?,?,?,?,?) RETURNING id",
            ("Good Omens", "Neil Gaiman", 4.0, "2025-05-01", "9780060853983", "goodreads_csv", "/works/OL262458W"),
        ).fetchone()[0]
        pre_read = dict(con.execute("SELECT * FROM reads WHERE id=?", (read_id,)).fetchone())
        con.execute(
            "INSERT INTO read_metadata(read_id,verified_work_id,identity_provider,"
            "identity_provider_id,identity_hash,description) VALUES(?,?,?,?,?,?)",
            (
                read_id,
                "/works/OL262458W",
                "openlibrary",
                "/works/OL262458W",
                read_metadata_identity_hash(pre_read),
                "Existing cached read metadata.",
            ),
        )
        con.executemany(
            "INSERT INTO goodreads_shelf_items(shelf,title,author,isbn,added_at) VALUES(?,?,?,?,?)",
            [
                ("to-read", "This Is How You Lose the Time War", "Amal El-Mohtar", "9781534431003", "2026-01-10"),
                ("currently-reading", "The Talisman", "Stephen King", "9781982115524", "2026-02-01"),
            ],
        )

    initialize(seed_demo=False)
    assert row("SELECT COUNT(*) count FROM schema_migrations")["count"] == len(MIGRATIONS)
    assert row("SELECT 1 FROM schema_migrations WHERE version=30") is not None

    for table_name in ("reads", "goodreads_shelf_items"):
        col_info = {item["name"]: item for item in rows(f"PRAGMA table_info({table_name})")}
        assert "additional_authors" in col_info
        assert col_info["additional_authors"]["notnull"] == 1
        assert col_info["additional_authors"]["dflt_value"] == "''"

    upgraded_read = row("SELECT * FROM reads WHERE id=?", (read_id,))
    assert upgraded_read["title"] == "Good Omens"
    assert upgraded_read["author"] == "Neil Gaiman"
    assert upgraded_read["additional_authors"] == ""
    assert upgraded_read["openlibrary_work_id"] == "/works/OL262458W"
    assert row("SELECT description FROM read_metadata WHERE read_id=?", (read_id,)) == {
        "description": "Existing cached read metadata."
    }
    assert rows(
        "SELECT shelf,title,author,additional_authors FROM goodreads_shelf_items ORDER BY shelf"
    ) == [
        {
            "shelf": "currently-reading",
            "title": "The Talisman",
            "author": "Stephen King",
            "additional_authors": "",
        },
        {
            "shelf": "to-read",
            "title": "This Is How You Lose the Time War",
            "author": "Amal El-Mohtar",
            "additional_authors": "",
        },
    ]

    # Without a CSV re-import there is no backfill, so title/author matching
    # does not yet match secondary-author-only candidates.
    pre_reimport_index = ReadIdentityIndex.from_reads([
        {"title": upgraded_read["title"], "author": upgraded_read["author"], "additional_authors": upgraded_read["additional_authors"]}
    ])
    assert not pre_reimport_index.matches({"title": "Good Omens", "author": "Terry Pratchett"})

    reimported = import_goodreads_csv(
        b"Title,Author,Additional Authors,My Rating,Date Read,Date Added,Exclusive Shelf,ISBN13\n"
        b"Good Omens,Neil Gaiman,Terry Pratchett,5,2025/05/01,2025/04/01,read,=\"9780060853983\"\n"
    )
    assert reimported == 1
    post_reimport_read = row("SELECT * FROM reads WHERE id=?", (read_id,))
    assert post_reimport_read["additional_authors"] == "Terry Pratchett"
    assert row("SELECT description FROM read_metadata WHERE read_id=?", (read_id,)) == {
        "description": "Existing cached read metadata."
    }
    post_reimport_index = ReadIdentityIndex.from_reads([
        {
            "title": post_reimport_read["title"],
            "author": post_reimport_read["author"],
            "additional_authors": post_reimport_read["additional_authors"],
        }
    ])
    assert post_reimport_index.matches({"title": "Good Omens", "author": "Terry Pratchett"})


def test_initialize_removes_retired_llm_data_from_v11_database(tmp_path):
    settings.db = str(tmp_path / "retired-model.db")
    import sqlite3

    with sqlite3.connect(settings.db) as con:
        con.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        for version, script in MIGRATIONS:
            if version > 11:
                break
            con.executescript(script)
            con.execute("INSERT INTO schema_migrations(version) VALUES(?)", (version,))

        assert {item[0] for item in con.execute("SELECT version FROM schema_migrations")} == set(range(1, 12))
        assert con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='llm_connections'"
        ).fetchone()
        connection_id = con.execute(
            "INSERT INTO llm_connections(name,provider_id,model_id,secret) VALUES(?,?,?,?) RETURNING id",
            ("Retired provider", "openai", "legacy-model", "sealed legacy credential"),
        ).fetchone()[0]
        policy_id = con.execute(
            "INSERT INTO llm_policies(name,connection_id) VALUES(?,?) RETURNING id",
            ("Retired shadow policy", connection_id),
        ).fetchone()[0]
        con.execute(
            "INSERT INTO llm_runs(id,policy_id,connection_id,request_hash,candidate_hash,status) VALUES(?,?,?,?,?,?)",
            ("legacy-run", policy_id, connection_id, "request", "candidates", "complete"),
        )
        candidate_id = con.execute(
            "INSERT INTO candidates(title,author,normalized_key) VALUES(?,?,?) RETURNING id",
            ("Legacy candidate", "Legacy author", "legacy candidate legacy author"),
        ).fetchone()[0]
        con.execute(
            "INSERT INTO llm_scores(run_id,candidate_id,rank,score) VALUES(?,?,?,?)",
            ("legacy-run", candidate_id, 1, 0.9),
        )
        con.executemany(
            "INSERT INTO settings(key,value,secret) VALUES(?,?,?)",
            [
                ("llm_shadow_enabled", "1", 0),
                ("llm_provider_key", "sealed legacy credential", 1),
                ("models_catalog_etag", "legacy-catalog", 0),
                ("librarr_url", "http://librarr:5050", 0),
            ],
        )

    initialize()
    _assert_retired_llm_removed()
    assert {item["version"] for item in rows("SELECT version FROM schema_migrations")} == {
        version for version, _ in MIGRATIONS
    }
    assert row("SELECT value FROM settings WHERE key='librarr_url'")["value"] == "http://librarr:5050"


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
    with TestClient(app, headers=authenticated_headers()) as client:
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
    with TestClient(app, headers=authenticated_headers()) as client:
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


@respx.mock
def test_librarr_stream_search_forwards_sse_and_preserves_event_frames(database):
    with transaction() as con:
        con.execute("INSERT INTO settings(key,value,secret) VALUES('librarr_url','http://librarr:5050',0)")
        con.execute("INSERT INTO settings(key,value,secret) VALUES('librarr_api_key',?,1)", (seal("test-key"),))
    body = (
        'event: started\ndata: {"search_id":"search-1"}\n\n'
        'event: results\ndata: {"results":[{"id":"book-1","title":"A Book"}]}\n\n'
        'event: complete\ndata: {"status":"complete"}\n\n'
    )
    ebook_route = respx.get("http://librarr:5050/api/search/stream").mock(
        return_value=httpx.Response(
            200,
            text=body,
            headers={"content-type": "text/event-stream; charset=utf-8"},
        )
    )
    audiobook_route = respx.get("http://librarr:5050/api/search/audiobooks/stream").mock(
        return_value=httpx.Response(
            200,
            text=body,
            headers={"content-type": "text/event-stream"},
        )
    )

    with TestClient(app, headers=authenticated_headers()) as client:
        ebook = client.get("/api/librarr/search/stream", params={"q": "A Book", "media_type": "ebook"})
        audiobook = client.get("/api/librarr/search/stream", params={"q": "Audio Book", "media_type": "audiobook"})

    assert ebook.status_code == 200
    assert ebook.headers["content-type"].startswith("text/event-stream")
    assert "no-cache" in ebook.headers["cache-control"]
    assert "no-transform" in ebook.headers["cache-control"]
    assert "private" in ebook.headers["cache-control"]
    assert "no-store" in ebook.headers["cache-control"]
    assert ebook.headers["x-accel-buffering"] == "no"
    assert ebook.text == body
    assert ebook_route.calls[0].request.url.params["q"] == "A Book"
    assert ebook_route.calls[0].request.headers["x-api-key"] == "test-key"
    assert ebook_route.calls[0].request.headers["accept"] == "text/event-stream"
    assert audiobook.status_code == 200 and audiobook.text == body
    assert audiobook_route.calls[0].request.url.params["q"] == "Audio Book"


@respx.mock
def test_librarr_stream_search_signals_unsupported_legacy_service(database):
    with transaction() as con:
        con.execute("INSERT INTO settings(key,value,secret) VALUES('librarr_url','http://librarr:5050',0)")
        con.execute("INSERT INTO settings(key,value,secret) VALUES('librarr_api_key',?,1)", (seal("test-key"),))
    respx.get("http://librarr:5050/api/search/stream").mock(
        return_value=httpx.Response(404, json={"detail": "Not found"})
    )
    with TestClient(app, headers=authenticated_headers()) as client:
        response = client.get("/api/librarr/search/stream", params={"q": "A Book", "media_type": "ebook"})
    assert response.status_code == 501
    assert "does not support streaming" in response.json()["detail"]

def test_librarr_media_type_is_validated(database):
    with TestClient(app, headers=authenticated_headers()) as client:
        response = client.put("/api/settings", json={"embedding_backend":"local","embedding_model":"hashing-768","librarr_media_type":"vinyl"})
    assert response.status_code == 400


def test_digest_settings_are_safe_and_bulk_feedback_is_idempotent(database):
    with TestClient(app, headers=authenticated_headers()) as client:
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
    with TestClient(app, headers=authenticated_headers()) as client:
        review = client.get("/api/digest/review", params={"period": first["period"]})
        assert review.status_code == 200 and review.json()["found"] is True and len(review.json()["ids"]) == 2
    assert row("SELECT COUNT(*) count FROM notification_deliveries WHERE status='sent'")["count"] == 1
    assert row("SELECT COUNT(*) count FROM digest_items")["count"] == 2
    second = asyncio.run(send_digest(config))
    assert second["status"] == "skipped" and route.call_count == 1
    third = asyncio.run(send_digest(config))
    assert third["status"] == "skipped" and route.call_count == 1


def test_digest_review_marks_unknown_period_as_stale(database):
    with TestClient(app, headers=authenticated_headers()) as client:
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

    base = {
        "enabled": False,
        "channels": ["discord", "email"],
        "day": 1,
        "time": "09:00",
        "timezone": "UTC",
        "minimum_score": 80,
        "maximum_books": 5,
        "only_new": True,
        "app_url": "https://afterword.example",
        "discord_webhook_url": "",
        "email_to": "",
        "email_from": "",
        "smtp_host": "",
        "smtp_security": "starttls",
    }
    validate_digest_config(base)

    with pytest.raises(ValueError, match="Discord webhook URL must point to Discord"):
        validate_digest_config({**base, "discord_webhook_url": "https://not-discord.example/webhook"})

    with pytest.raises(ValueError, match="Valid email to is required"):
        validate_digest_config({**base, "email_to": "not-an-email"})


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


def _seed_demo_derived_artifacts_for_test():
    """Populate scoring batches, serving snapshots, outcomes, and stale read status from demo reads."""
    from datetime import datetime, timedelta, timezone
    from afterword_engine.learning import attribute_read_outcomes

    past_ts = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    with transaction() as con:
        web_source_id = con.execute(
            "SELECT id FROM sources WHERE is_default=0 AND enabled=1 LIMIT 1"
        ).fetchone()[0]
        piranesi_candidate_id = con.execute(
            "INSERT INTO candidates(title,author,description,genres,source_id,score,status,normalized_key) "
            "VALUES(?,?,?,?,?,80,'recommended',?)",
            (
                "Piranesi",
                "Susanna Clarke",
                "A house of infinite halls and statues flooded by tides.",
                json.dumps(["fantasy"]),
                web_source_id,
                normalize_key("Piranesi", "Susanna Clarke"),
            ),
        ).lastrowid
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',metadata_confidence=0.9 "
            "WHERE candidate_id=?",
            (piranesi_candidate_id,),
        )

    assert asyncio.run(score_all("local", "hashing-768")) >= 4
    served, run_id = tracked_recommendations()
    assert served and run_id

    # Create an exposure for Piranesi preceding the demo read timestamp so
    # attribute_read_outcomes() records a synthetic demo outcome + read event.
    with transaction() as con:
        con.execute(
            "INSERT OR IGNORE INTO recommendation_impressions"
            "(run_id,candidate_id,rank,score,propensity,presented_at,visible_at) "
            "VALUES(?,?,99,80.0,1.0,?,?)",
            (run_id, piranesi_candidate_id, past_ts, past_ts),
        )
        # Simulate the pre-fix mark-read state where a candidate's status became
        # 'read' while its matching reads row remained source='demo'.
        con.execute(
            "UPDATE candidates SET status='read' WHERE id=?",
            (piranesi_candidate_id,),
        )

    attributed = attribute_read_outcomes()
    assert attributed["reads_attributed"] >= 1
    assert row("SELECT COUNT(*) count FROM recommendation_outcomes")["count"] >= 1
    assert row("SELECT COUNT(*) count FROM scoring_batch_reads")["count"] >= 4
    assert row("SELECT COUNT(*) count FROM recommendation_run_evidence")["count"] >= 1
    return piranesi_candidate_id, run_id


def test_disabling_builtin_demo_source_purges_demo_reads_derived_evidence_and_preserves_toggle_off(
    database,
):
    from afterword_engine.database import initialize as init_db

    piranesi_candidate_id, _run_id = _seed_demo_derived_artifacts_for_test()
    with transaction() as con:
        builtin_candidate_id = con.execute(
            "SELECT c.id FROM candidates c JOIN sources s ON s.id=c.source_id "
            "WHERE s.url='builtin://upcoming' ORDER BY c.id LIMIT 1"
        ).fetchone()[0]
        demo_read_id = con.execute(
            "SELECT id FROM reads WHERE source='demo' ORDER BY id LIMIT 1"
        ).fetchone()[0]
        con.execute(
            "UPDATE candidates SET status='read' WHERE id=?",
            (builtin_candidate_id,),
        )
        con.execute(
            "INSERT INTO recommendation_events(event_key,candidate_id,event_type,value,source,metadata) "
            "VALUES(?,?,'read',5.0,'manual_read',?)",
            (
                f"manual-read:{builtin_candidate_id}:{demo_read_id:08d}",
                builtin_candidate_id,
                json.dumps({"read_source": "demo", "time_precision": "timestamp"}),
            ),
        )

    assert row("SELECT COUNT(*) count FROM reads WHERE source='demo'")["count"] == 4
    assert row("SELECT COUNT(*) count FROM embeddings WHERE entity_type='read'")["count"] == 4

    client = TestClient(app, headers=authenticated_headers())
    initial_overview = client.get("/api/overview").json()
    builtin = next(item for item in initial_overview["sources"] if item["is_default"])
    assert builtin["enabled"] == 1

    toggled = client.put(f"/api/sources/{builtin['id']}/toggle")
    assert toggled.status_code == 200
    job_id = toggled.json()["job_id"]
    assert job_id is not None

    assert row("SELECT kind,status FROM jobs WHERE id=?", (job_id,)) == {
        "kind": "score",
        "status": "queued",
    }
    assert row("SELECT COUNT(*) count FROM reads WHERE source='demo'")["count"] == 0
    assert row("SELECT COUNT(*) count FROM embeddings WHERE entity_type='read'")["count"] == 0
    assert row("SELECT COUNT(*) count FROM recommendation_outcomes")["count"] == 0
    assert row("SELECT COUNT(*) count FROM recommendation_events WHERE event_type='read'")["count"] == 0
    assert row("SELECT COUNT(*) count FROM recommendation_evidence_outcomes")["count"] == 0
    assert row("SELECT COUNT(*) count FROM recommendation_evidence_events")["count"] == 0
    assert row("SELECT COUNT(*) count FROM scoring_batches")["count"] == 0
    assert row("SELECT COUNT(*) count FROM scoring_batch_reads")["count"] == 0
    assert row("SELECT COUNT(*) count FROM recommendation_run_evidence")["count"] == 0
    assert row("SELECT COUNT(*) count FROM recommendation_evidence_vectors")["count"] == 0
    expected_exposure_bytes = row(
        "SELECT COALESCE(SUM(160 + length(CAST(candidate_snapshot_json AS BLOB))), 0) bytes "
        "FROM recommendation_evidence_exposures"
    )["bytes"]
    assert (
        row("SELECT used_bytes FROM recommendation_evidence_budget WHERE id=1")["used_bytes"]
        == expected_exposure_bytes
    )
    assert row("SELECT status,score FROM candidates WHERE id=?", (builtin_candidate_id,)) == {
        "status": "new",
        "score": 0.0,
    }
    assert row("SELECT status FROM candidates WHERE id=?", (piranesi_candidate_id,)) == {
        "status": "recommended",
    }
    assert row("SELECT enabled,is_default FROM sources WHERE url='builtin://upcoming'") == {
        "enabled": 0,
        "is_default": 1,
    }

    updated_overview = client.get("/api/overview").json()
    assert "Piranesi" in {item["title"] for item in updated_overview["recommendations"]}

    # Even if both candidates and reads are empty, re-running initialize(seed_demo=True)
    # must respect the user's toggle-off and never re-enable or re-seed demo data.
    with transaction() as con:
        con.execute("DELETE FROM candidates")
    init_db(seed_demo=True)
    assert row("SELECT enabled FROM sources WHERE url='builtin://upcoming'") == {
        "enabled": 0,
    }
    assert row("SELECT COUNT(*) count FROM candidates")["count"] == 0
    assert row("SELECT COUNT(*) count FROM reads")["count"] == 0


def test_goodreads_csv_import_updates_matching_demo_title_and_cleans_derived_artifacts(
    database,
):
    piranesi_id, _run_id = _seed_demo_derived_artifacts_for_test()

    # While synthetic demo reads and status='read' are present, Piranesi is hidden.
    assert "Piranesi" not in {item["title"] for item in recommendation_list()}

    csv_payload = (
        b"Title,Author,My Rating,Date Read,ISBN13,Exclusive Shelf\n"
        b"Sea of Tranquility,Emily St. John Mandel,3,2025/04/01,9780593321447,read\n"
        b"Project Hail Mary,Andy Weir,5,2025/05/01,9780593135204,read\n"
    )
    assert import_goodreads_csv(csv_payload) == 2
    assert row("SELECT COUNT(*) count FROM reads WHERE source='demo'")["count"] == 0
    assert row("SELECT COUNT(*) count FROM recommendation_outcomes")["count"] == 0
    assert row("SELECT COUNT(*) count FROM scoring_batch_reads")["count"] == 0
    assert row("SELECT COUNT(*) count FROM recommendation_run_evidence")["count"] == 0
    assert row(
        "SELECT rating,source,isbn FROM reads WHERE title='Sea of Tranquility'"
    ) == {
        "rating": 3.0,
        "source": "goodreads_csv",
        "isbn": "9780593321447",
    }
    assert row("SELECT COUNT(*) count FROM reads")["count"] == 2
    assert row("SELECT status FROM candidates WHERE id=?", (piranesi_id,)) == {
        "status": "recommended",
    }
    assert "Piranesi" in {item["title"] for item in recommendation_list()}

    asyncio.run(score_all("local", "hashing-768"))
    assert "Piranesi" in {item["title"] for item in recommendation_list()}


def test_goodreads_rss_and_manual_read_replace_demo_source_preserve_already_present_and_reset_candidates(
    database, monkeypatch
):
    async def fake_fetch_bytes(*_args, **_kwargs):
        return b"", "application/rss+xml"

    class Feed:
        entries = [
            {
                "title": "Piranesi",
                "author_name": "Susanna Clarke",
                "user_rating": "4",
                "user_read_at": "2025/03/10",
                "isbn13": "9781635575637",
            },
            {
                "title": "Dune",
                "author_name": "Frank Herbert",
                "user_rating": "5",
                "user_read_at": "2025/03/12",
                "isbn13": "9780441172719",
            },
        ]

    monkeypatch.setattr(ingestion, "fetch_bytes", fake_fetch_bytes)
    monkeypatch.setattr(ingestion.feedparser, "parse", lambda _content: Feed())

    assert asyncio.run(
        import_goodreads_rss("https://www.goodreads.com/review/list_rss/reader?shelf=read")
    ) == 2
    assert row("SELECT COUNT(*) count FROM reads WHERE source='demo'")["count"] == 0
    assert row("SELECT rating,source,isbn FROM reads WHERE title='Piranesi'") == {
        "rating": 4.0,
        "source": "goodreads_rss",
        "isbn": "9781635575637",
    }

    # Re-seed synthetic demo reads to verify Mark Read preserves already_present=True
    # for a matching demo row without inheriting its synthetic rating, purges other
    # demo reads, and resets stale status='read' on other demo-backed candidates.
    with transaction() as con:
        con.execute("DELETE FROM reads")
        con.execute(
            "INSERT INTO reads(title,author,rating,source) VALUES"
            "('Sea of Tranquility','Emily St. John Mandel',5,'demo'),"
            "('The Fifth Season','N. K. Jemisin',5,'demo')"
        )
        web_source_id = con.execute(
            "SELECT id FROM sources WHERE is_default=0 AND enabled=1 LIMIT 1"
        ).fetchone()[0]
        candidate_id = con.execute(
            "INSERT INTO candidates(title,author,source_id,score,status,normalized_key) "
            "VALUES('Sea of Tranquility','Emily St. John Mandel',?,82,'recommended',?)",
            (web_source_id, normalize_key("Sea of Tranquility", "Emily St. John Mandel")),
        ).lastrowid
        fifth_season_candidate_id = con.execute(
            "INSERT INTO candidates(title,author,source_id,score,status,normalized_key) "
            "VALUES('The Fifth Season','N. K. Jemisin',?,85,'read',?)",
            (web_source_id, normalize_key("The Fifth Season", "N. K. Jemisin")),
        ).lastrowid
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted' WHERE candidate_id IN (?,?)",
            (candidate_id, fifth_season_candidate_id),
        )

    client = TestClient(app, headers=authenticated_headers())
    marked = client.post(f"/api/recommendations/{candidate_id}/read", json={})

    assert marked.status_code == 200
    assert marked.json()["already_present"] is True
    assert row("SELECT COUNT(*) count FROM reads WHERE source='demo'")["count"] == 0
    assert row("SELECT rating,source FROM reads WHERE title='Sea of Tranquility'") == {
        "rating": None,
        "source": "manual",
    }
    assert row("SELECT status FROM candidates WHERE id=?", (candidate_id,)) == {
        "status": "read",
    }
    assert row("SELECT status FROM candidates WHERE id=?", (fifth_season_candidate_id,)) == {
        "status": "recommended",
    }


def test_migration_31_purges_stale_demo_reads_derived_rows_and_is_idempotent(
    tmp_path, monkeypatch
):
    from afterword_engine.database import initialize as init_db

    monkeypatch.setattr(settings, "db", str(tmp_path / "upgrade-migration-31.db"))
    init_db(seed_demo=True)
    piranesi_candidate_id, _run_id = _seed_demo_derived_artifacts_for_test()

    with transaction() as con:
        web_source_id = con.execute(
            "SELECT id FROM sources WHERE is_default=0 AND enabled=1 LIMIT 1"
        ).fetchone()[0]
        mandel_candidate_id = con.execute(
            "INSERT INTO candidates(title,author,description,genres,source_id,score,status,normalized_key) "
            "VALUES(?,?,?,?,?,0,'new',?)",
            (
                "The Glass Hotel",
                "Emily St. John Mandel",
                "A financial collapse and disappearance at the edge of the wilderness.",
                json.dumps(["literary fiction"]),
                web_source_id,
                normalize_key("The Glass Hotel", "Emily St. John Mandel"),
            ),
        ).lastrowid
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',metadata_confidence=0.9 "
            "WHERE candidate_id=?",
            (mandel_candidate_id,),
        )
        # Simulate an installation upgraded from before demo-read purging where real
        # reads coexist alongside the 4 seeded demo reads.
        con.execute(
            "INSERT INTO reads(title,author,rating,source) "
            "VALUES('Dune','Frank Herbert',5,'goodreads_csv')"
        )

    asyncio.run(score_all("local", "hashing-768"))
    pre_upgrade_reasons = json.loads(
        row("SELECT explanation FROM candidates WHERE id=?", (mandel_candidate_id,))["explanation"]
    )
    assert "You rated a book by this author highly" in pre_upgrade_reasons
    assert row("SELECT COUNT(*) count FROM recommendation_outcomes")["count"] >= 1
    assert row("SELECT COUNT(*) count FROM scoring_batch_reads")["count"] >= 5

    with transaction() as con:
        con.execute("DELETE FROM jobs")
        con.execute("DELETE FROM schema_migrations WHERE version=31")

    init_db(seed_demo=True)

    assert row("SELECT COUNT(*) count FROM reads WHERE source='demo'")["count"] == 0
    assert row("SELECT COUNT(*) count FROM reads WHERE title='Dune' AND source='goodreads_csv'")["count"] == 1
    assert row("SELECT COUNT(*) count FROM embeddings WHERE entity_type='read'")["count"] == 1
    assert row("SELECT COUNT(*) count FROM recommendation_outcomes")["count"] == 0
    assert row("SELECT COUNT(*) count FROM scoring_batches")["count"] == 0
    assert row("SELECT COUNT(*) count FROM scoring_batch_reads")["count"] == 0
    assert row("SELECT COUNT(*) count FROM recommendation_run_evidence")["count"] == 0
    assert row("SELECT status FROM candidates WHERE id=?", (piranesi_candidate_id,)) == {
        "status": "recommended",
    }
    assert row("SELECT COUNT(*) count FROM jobs WHERE kind='score' AND status='queued'")["count"] == 1

    asyncio.run(score_all("local", "hashing-768"))
    post_upgrade_reasons = json.loads(
        row("SELECT explanation FROM candidates WHERE id=?", (mandel_candidate_id,))["explanation"]
    )
    assert "You rated a book by this author highly" not in post_upgrade_reasons

    # Re-running initialize() and re-applying migration 31 must be idempotent and
    # preserve post-upgrade real scoring batches without queuing duplicate jobs.
    with transaction() as con:
        con.execute("DELETE FROM jobs")
    init_db(seed_demo=True)
    assert row("SELECT COUNT(*) count FROM jobs WHERE kind='score'")["count"] == 0
    assert row("SELECT COUNT(*) count FROM scoring_batches")["count"] == 1

    with transaction() as con:
        con.execute("DELETE FROM schema_migrations WHERE version=31")
    init_db(seed_demo=True)
    assert row("SELECT COUNT(*) count FROM jobs WHERE kind='score'")["count"] == 0
    assert row("SELECT COUNT(*) count FROM reads WHERE title='Dune'")["count"] == 1
    assert row("SELECT COUNT(*) count FROM scoring_batches")["count"] == 1


def test_migration_31_purges_demo_reads_when_builtin_disabled_with_zero_real_reads(
    tmp_path, monkeypatch
):
    from afterword_engine.database import initialize as init_db

    monkeypatch.setattr(
        settings, "db", str(tmp_path / "upgrade-migration-31-disabled-builtin.db")
    )
    init_db(seed_demo=True)
    piranesi_candidate_id, _run_id = _seed_demo_derived_artifacts_for_test()

    with transaction() as con:
        builtin_candidate_id = con.execute(
            "SELECT c.id FROM candidates c JOIN sources s ON s.id=c.source_id "
            "WHERE s.url='builtin://upcoming' ORDER BY c.id LIMIT 1"
        ).fetchone()[0]
        demo_read_id = con.execute(
            "SELECT id FROM reads WHERE source='demo' ORDER BY id LIMIT 1"
        ).fetchone()[0]
        con.execute(
            "UPDATE candidates SET status='read' WHERE id=?",
            (builtin_candidate_id,),
        )
        # Every event-key form a demo read can leave behind must be purged.
        for demo_event_key in (
            f"manual-read:{builtin_candidate_id}:{demo_read_id:08d}",
            f"read:{demo_read_id:08d}:{builtin_candidate_id}",
            f"attribution:read:{demo_read_id:08d}:{builtin_candidate_id}",
        ):
            con.execute(
                "INSERT INTO recommendation_events(event_key,candidate_id,event_type,value,source,metadata) "
                "VALUES(?,?,'read',5.0,'manual_read',?)",
                (
                    demo_event_key,
                    builtin_candidate_id,
                    json.dumps({"read_source": "demo", "time_precision": "timestamp"}),
                ),
            )
        con.execute("UPDATE sources SET enabled=0 WHERE url='builtin://upcoming'")
        con.execute("DELETE FROM jobs")
        con.execute("DELETE FROM schema_migrations WHERE version=31")

    init_db(seed_demo=True)

    assert row("SELECT COUNT(*) count FROM reads")["count"] == 0
    assert row("SELECT COUNT(*) count FROM embeddings WHERE entity_type='read'")["count"] == 0
    assert row("SELECT COUNT(*) count FROM recommendation_outcomes")["count"] == 0
    assert row("SELECT COUNT(*) count FROM recommendation_events WHERE event_type='read'")["count"] == 0
    assert row("SELECT COUNT(*) count FROM scoring_batches")["count"] == 0
    assert row("SELECT COUNT(*) count FROM scoring_batch_reads")["count"] == 0
    assert row("SELECT COUNT(*) count FROM recommendation_run_evidence")["count"] == 0
    assert row("SELECT status,score FROM candidates WHERE id=?", (builtin_candidate_id,)) == {
        "status": "new",
        "score": 0.0,
    }
    assert row("SELECT status FROM candidates WHERE id=?", (piranesi_candidate_id,)) == {
        "status": "recommended",
    }
    assert row("SELECT enabled FROM sources WHERE url='builtin://upcoming'") == {
        "enabled": 0,
    }
    assert row("SELECT COUNT(*) count FROM jobs WHERE kind='score' AND status='queued'")["count"] == 1

    # Re-running migration 31 when already clean is a no-op.
    with transaction() as con:
        con.execute("DELETE FROM jobs")
        con.execute("DELETE FROM schema_migrations WHERE version=31")
    init_db(seed_demo=True)
    assert row("SELECT COUNT(*) count FROM jobs WHERE kind='score'")["count"] == 0
    assert row("SELECT COUNT(*) count FROM reads")["count"] == 0


def test_migration_31_keeps_demo_only_reads_when_builtin_enabled(tmp_path, monkeypatch):
    from afterword_engine.database import initialize as init_db

    monkeypatch.setattr(
        settings, "db", str(tmp_path / "upgrade-migration-31-keep-demo.db")
    )
    init_db(seed_demo=True)
    assert asyncio.run(score_all("local", "hashing-768")) == 4
    served, run_id = tracked_recommendations()
    assert len(served) == 4 and run_id

    with transaction() as con:
        con.execute("DELETE FROM jobs")
        con.execute("DELETE FROM schema_migrations WHERE version=31")

    init_db(seed_demo=True)

    assert row("SELECT enabled FROM sources WHERE url='builtin://upcoming'") == {
        "enabled": 1,
    }
    assert row("SELECT COUNT(*) count FROM reads WHERE source='demo'")["count"] == 4
    assert row("SELECT COUNT(*) count FROM embeddings WHERE entity_type='read'")["count"] == 4
    assert row("SELECT COUNT(*) count FROM scoring_batches")["count"] == 1
    assert row("SELECT COUNT(*) count FROM scoring_batch_reads")["count"] == 4
    assert row("SELECT COUNT(*) count FROM recommendation_run_evidence")["count"] == 1
    assert row("SELECT COUNT(*) count FROM jobs WHERE kind='score'")["count"] == 0


def _seed_shared_run_with_demo_and_real_data():
    """Create a serving run shared by a demo read outcome, a real read outcome, and real feedback."""
    from datetime import datetime, timedelta, timezone
    from afterword_engine.learning import attribute_read_outcomes

    past_ts = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    real_read_ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

    with transaction() as con:
        web_source_id = con.execute(
            "SELECT id FROM sources WHERE is_default=0 AND enabled=1 LIMIT 1"
        ).fetchone()[0]
        piranesi_candidate_id = con.execute(
            "INSERT INTO candidates(title,author,description,genres,source_id,score,status,normalized_key) "
            "VALUES(?,?,?,?,?,80,'recommended',?)",
            (
                "Piranesi",
                "Susanna Clarke",
                "A house of infinite halls and statues flooded by tides.",
                json.dumps(["fantasy"]),
                web_source_id,
                normalize_key("Piranesi", "Susanna Clarke"),
            ),
        ).lastrowid
        dune_candidate_id = con.execute(
            "INSERT INTO candidates(title,author,description,genres,source_id,score,status,normalized_key) "
            "VALUES(?,?,?,?,?,88,'recommended',?)",
            (
                "Dune",
                "Frank Herbert",
                "Politics and ecology on a desert world.",
                json.dumps(["science fiction"]),
                web_source_id,
                normalize_key("Dune", "Frank Herbert"),
            ),
        ).lastrowid
        jemisin_candidate_id = con.execute(
            "INSERT INTO candidates(title,author,description,genres,source_id,score,status,normalized_key) "
            "VALUES(?,?,?,?,?,85,'read',?)",
            (
                "The Fifth Season",
                "N. K. Jemisin",
                "Orogenes and seismic catastrophe across the Stillness.",
                json.dumps(["fantasy"]),
                web_source_id,
                normalize_key("The Fifth Season", "N. K. Jemisin"),
            ),
        ).lastrowid
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',metadata_confidence=0.9 "
            "WHERE candidate_id IN (?,?,?)",
            (piranesi_candidate_id, dune_candidate_id, jemisin_candidate_id),
        )
        attr_demo_cid = con.execute(
            "INSERT INTO candidates(title,author,description,genres,source_id,score,status,normalized_key) "
            "VALUES(?,?,?,?,?,81,'recommended',?)",
            (
                "Mongrels",
                "Stephen Graham Jones",
                "Coming-of-age werewolf road novel across the American South.",
                json.dumps(["horror"]),
                web_source_id,
                normalize_key("Mongrels", "Stephen Graham Jones"),
            ),
        ).lastrowid
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',metadata_confidence=0.9 "
            "WHERE candidate_id=?",
            (attr_demo_cid,),
        )

    assert asyncio.run(score_all("local", "hashing-768")) >= 6
    served, shared_run_id = tracked_recommendations()
    assert len(served) >= 6 and shared_run_id
    builtin_ids = [
        int(item["id"])
        for item in served
        if int(item["id"])
        not in {
            piranesi_candidate_id,
            dune_candidate_id,
            jemisin_candidate_id,
            attr_demo_cid,
        }
    ]
    feedback_candidate_id, mark_read_candidate_id, stale_prior_cid, manual_demo_cid = (
        builtin_ids[0],
        builtin_ids[1],
        builtin_ids[2],
        builtin_ids[3],
    )

    with transaction() as con:
        for rank_offset, cid in enumerate(
            (
                piranesi_candidate_id,
                dune_candidate_id,
                jemisin_candidate_id,
                mark_read_candidate_id,
                manual_demo_cid,
                attr_demo_cid,
            ),
            start=90,
        ):
            con.execute(
                "INSERT INTO recommendation_impressions"
                "(run_id,candidate_id,rank,score,propensity,presented_at,visible_at) "
                "VALUES(?,?,?,82.0,1.0,?,?) "
                "ON CONFLICT(run_id,candidate_id) DO UPDATE SET presented_at=excluded.presented_at, visible_at=excluded.visible_at",
                (shared_run_id, cid, rank_offset, past_ts, past_ts),
            )
        jemisin_demo_id = int(
            con.execute(
                "SELECT id FROM reads WHERE source='demo' AND title='The Fifth Season'"
            ).fetchone()[0]
        )
        from afterword_engine.learning import record_event_in_connection

        # Seed a prior 2-part read event on stale_prior_cid so attribute_read_outcomes()
        # natively emits a 3-part read:<demo_id>:<jemisin_candidate_id> event and an
        # unlinked retraction revision for the superseded 2-part key.
        record_event_in_connection(
            con,
            event_key=f"read:{jemisin_demo_id:08d}",
            candidate_id=stale_prior_cid,
            event_type="read",
            run_id=shared_run_id,
            value=5.0,
            source="read_import",
            metadata={"read_source": "demo", "time_precision": "timestamp"},
            occurred_at=real_read_ts,
        )
        real_read_id = con.execute(
            "INSERT INTO reads(title,author,rating,read_at,source) "
            "VALUES('Dune','Frank Herbert',5,?,'goodreads_csv')",
            (real_read_ts,),
        ).lastrowid

    profiles.initialize_auth()
    client = TestClient(app, headers=authenticated_headers())
    fb = client.post(
        f"/api/recommendations/{feedback_candidate_id}/feedback",
        json={"action": "save", "run_id": shared_run_id},
    )
    assert fb.status_code == 200
    feedback_decision_id = fb.json()["decision_id"]

    attributed = attribute_read_outcomes()
    assert attributed["reads_attributed"] == 3

    with transaction() as con:
        from afterword_engine.learning import record_event_in_connection

        piranesi_demo_id = int(
            con.execute(
                "SELECT id FROM reads WHERE source='demo' AND title='Piranesi'"
            ).fetchone()[0]
        )
        mandel_demo_id = int(
            con.execute(
                "SELECT id FROM reads WHERE source='demo' AND title='Sea of Tranquility'"
            ).fetchone()[0]
        )
        jones_demo_id = int(
            con.execute(
                "SELECT id FROM reads WHERE source='demo' AND title='The Only Good Indians'"
            ).fetchone()[0]
        )
        demo_manual_key = f"manual-read:{manual_demo_cid}:{mandel_demo_id:08d}"
        demo_attr_key = f"attribution:read:{jones_demo_id:08d}:{attr_demo_cid}"
        record_event_in_connection(
            con,
            event_key=demo_manual_key,
            candidate_id=manual_demo_cid,
            event_type="read",
            run_id=shared_run_id,
            value=5.0,
            source="manual_read",
            metadata={"read_source": "demo", "time_precision": "timestamp"},
            occurred_at=real_read_ts,
            label=1.0,
            label_kind="rating_positive",
            confidence=1.0,
            read_id=mandel_demo_id,
        )
        record_event_in_connection(
            con,
            event_key=demo_attr_key,
            candidate_id=attr_demo_cid,
            event_type="read",
            run_id=shared_run_id,
            value=4.0,
            source="read_import",
            metadata={"read_source": "demo", "time_precision": "timestamp"},
            occurred_at=real_read_ts,
            label=1.0,
            label_kind="rating_positive",
            confidence=1.0,
            read_id=jones_demo_id,
        )

    demo_keys = {
        f"read:{piranesi_demo_id:08d}",
        f"read:{jemisin_demo_id:08d}:{jemisin_candidate_id}",
        demo_manual_key,
        demo_attr_key,
    }
    for table in (
        "recommendation_events",
        "recommendation_evidence_events",
        "recommendation_evidence_outcomes",
    ):
        present = {
            item["event_key"]
            for item in rows(f"SELECT DISTINCT event_key FROM {table}")
        }
        assert demo_keys.issubset(present)
    assert (
        row(
            "SELECT COUNT(*) count FROM recommendation_evidence_events "
            "WHERE event_key=? AND run_id IS NULL",
            (f"read:{jemisin_demo_id:08d}",),
        )["count"]
        == 1
    )
    assert (
        row(
            "SELECT COUNT(*) count FROM recommendation_outcomes o "
            "JOIN recommendation_impressions i ON i.id=o.impression_id "
            "WHERE i.run_id=?",
            (shared_run_id,),
        )["count"]
        == 6
    )
    return {
        "shared_run_id": shared_run_id,
        "piranesi_candidate_id": piranesi_candidate_id,
        "dune_candidate_id": dune_candidate_id,
        "real_read_id": real_read_id,
        "feedback_candidate_id": feedback_candidate_id,
        "feedback_event_key": f"feedback:{feedback_decision_id}",
        "mark_read_candidate_id": mark_read_candidate_id,
        "three_part_demo_cid": jemisin_candidate_id,
    }


def test_runtime_purge_and_migration_31_preserve_real_outcomes_and_feedback_on_shared_run(
    tmp_path, monkeypatch
):
    from afterword_engine.database import initialize as init_db

    # 1. Runtime purge_demo_reads via manual Mark Read on a shared run.
    monkeypatch.setattr(settings, "db", str(tmp_path / "shared-run-runtime.db"))
    init_db(seed_demo=True)
    ctx = _seed_shared_run_with_demo_and_real_data()
    shared_run_id = ctx["shared_run_id"]
    real_event_key = f"read:{ctx['real_read_id']:08d}"
    feedback_event_key = ctx["feedback_event_key"]
    impression_count_before = row(
        "SELECT COUNT(*) count FROM recommendation_impressions WHERE run_id=?",
        (shared_run_id,),
    )["count"]
    exposure_count_before = row(
        "SELECT COUNT(*) count FROM recommendation_evidence_exposures WHERE run_id=?",
        (shared_run_id,),
    )["count"]

    client = TestClient(app, headers=authenticated_headers())
    marked = client.post(
        f"/api/recommendations/{ctx['mark_read_candidate_id']}/read",
        json={"rating": 4},
    )
    assert marked.status_code == 200
    manual_read_id = marked.json()["read_id"]
    manual_event_key = f"manual-read:{ctx['mark_read_candidate_id']}:{manual_read_id:08d}"

    assert row("SELECT COUNT(*) count FROM reads WHERE source='demo'")["count"] == 0
    assert row("SELECT status FROM candidates WHERE id=?", (ctx["three_part_demo_cid"],)) == {
        "status": "recommended",
    }
    assert row("SELECT 1 ok FROM recommendation_runs WHERE id=?", (shared_run_id,)) == {"ok": 1}
    assert (
        row(
            "SELECT COUNT(*) count FROM recommendation_impressions WHERE run_id=?",
            (shared_run_id,),
        )["count"]
        == impression_count_before
    )
    assert (
        row(
            "SELECT COUNT(*) count FROM recommendation_evidence_exposures WHERE run_id=?",
            (shared_run_id,),
        )["count"]
        == exposure_count_before
    )
    # Demo outcome and event are gone, while real read outcome, explicit feedback outcome,
    # and the newly recorded manual read outcome on the shared run all remain intact.
    remaining_events = rows(
        "SELECT event_key, run_id, event_type FROM recommendation_events ORDER BY event_key"
    )
    assert {item["event_key"] for item in remaining_events} == {
        feedback_event_key,
        manual_event_key,
        real_event_key,
    }
    assert all(item["run_id"] == shared_run_id for item in remaining_events)
    remaining_outcomes = rows(
        "SELECT o.read_id, o.label_kind, e.event_key "
        "FROM recommendation_outcomes o "
        "JOIN recommendation_events e ON e.id=o.event_id "
        "ORDER BY e.event_key"
    )
    assert remaining_outcomes == [
        {
            "read_id": None,
            "label_kind": "explicit_feedback",
            "event_key": feedback_event_key,
        },
        {
            "read_id": manual_read_id,
            "label_kind": "rating_positive",
            "event_key": manual_event_key,
        },
        {
            "read_id": ctx["real_read_id"],
            "label_kind": "rating_positive",
            "event_key": real_event_key,
        },
    ]
    assert {
        item["event_key"]
        for item in rows(
            "SELECT event_key FROM recommendation_evidence_outcomes WHERE run_id=?",
            (shared_run_id,),
        )
    } == {feedback_event_key, manual_event_key, real_event_key}
    assert {
        item["event_key"]
        for item in rows("SELECT event_key FROM recommendation_evidence_events")
    } == {feedback_event_key, manual_event_key, real_event_key}

    # 2. Migration 31 on a shared run with demo outcome + real outcome + real feedback.
    monkeypatch.setattr(settings, "db", str(tmp_path / "shared-run-migration-31.db"))
    init_db(seed_demo=True)
    mctx = _seed_shared_run_with_demo_and_real_data()
    mrun_id = mctx["shared_run_id"]
    m_real_event_key = f"read:{mctx['real_read_id']:08d}"
    m_feedback_event_key = mctx["feedback_event_key"]
    m_impressions_before = row(
        "SELECT COUNT(*) count FROM recommendation_impressions WHERE run_id=?",
        (mrun_id,),
    )["count"]
    m_exposures_before = row(
        "SELECT COUNT(*) count FROM recommendation_evidence_exposures WHERE run_id=?",
        (mrun_id,),
    )["count"]

    with transaction() as con:
        con.execute("DELETE FROM jobs")
        con.execute("DELETE FROM schema_migrations WHERE version=31")

    init_db(seed_demo=True)

    assert row("SELECT COUNT(*) count FROM reads WHERE source='demo'")["count"] == 0
    assert row("SELECT COUNT(*) count FROM reads WHERE id=?", (mctx["real_read_id"],))["count"] == 1
    assert row("SELECT status FROM candidates WHERE id=?", (mctx["three_part_demo_cid"],)) == {
        "status": "recommended",
    }
    assert row("SELECT 1 ok FROM recommendation_runs WHERE id=?", (mrun_id,)) == {"ok": 1}
    assert (
        row(
            "SELECT COUNT(*) count FROM recommendation_impressions WHERE run_id=?",
            (mrun_id,),
        )["count"]
        == m_impressions_before
    )
    assert (
        row(
            "SELECT COUNT(*) count FROM recommendation_evidence_exposures WHERE run_id=?",
            (mrun_id,),
        )["count"]
        == m_exposures_before
    )
    assert rows(
        "SELECT event_key, run_id FROM recommendation_events ORDER BY event_key"
    ) == [
        {"event_key": m_feedback_event_key, "run_id": mrun_id},
        {"event_key": m_real_event_key, "run_id": mrun_id},
    ]
    assert rows(
        "SELECT o.read_id, o.label_kind, e.event_key "
        "FROM recommendation_outcomes o "
        "JOIN recommendation_events e ON e.id=o.event_id "
        "ORDER BY e.event_key"
    ) == [
        {
            "read_id": None,
            "label_kind": "explicit_feedback",
            "event_key": m_feedback_event_key,
        },
        {
            "read_id": mctx["real_read_id"],
            "label_kind": "rating_positive",
            "event_key": m_real_event_key,
        },
    ]
    assert {
        item["event_key"]
        for item in rows(
            "SELECT event_key FROM recommendation_evidence_outcomes WHERE run_id=?",
            (mrun_id,),
        )
    } == {m_feedback_event_key, m_real_event_key}
    assert {
        item["event_key"]
        for item in rows("SELECT event_key FROM recommendation_evidence_events")
    } == {m_feedback_event_key, m_real_event_key}
    assert row("SELECT COUNT(*) count FROM recommendation_run_evidence")["count"] == 0


def test_update_custom_source_persists_name_and_url_and_returns_409_on_duplicate(
    database, monkeypatch
):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    scanned_urls: list[str] = []

    async def fake_scan_source(source):
        scanned_urls.append(source["url"])
        with transaction() as con:
            con.execute(
                "UPDATE sources SET last_scanned_at=CURRENT_TIMESTAMP, last_status='ok:2' WHERE id=?",
                (source["id"],),
            )
        return 2

    monkeypatch.setattr(main, "scan_source", fake_scan_source)

    with transaction() as con:
        first_id = con.execute(
            "INSERT INTO sources(name,url,enabled,lifecycle,last_status,last_scanned_at) "
            "VALUES(?,?,1,'one_time','error:404','2026-01-01T00:00:00+00:00')",
            ("goodreads.com", "https://books.example.test/old-feed.xml"),
        ).lastrowid
        second_id = con.execute(
            "INSERT INTO sources(name,url,enabled,lifecycle) VALUES(?,?,1,'permanent')",
            ("goodreads.com", "https://books.example.test/other-feed.xml"),
        ).lastrowid

    client = TestClient(app, headers=authenticated_headers())
    listed = {item["id"]: item for item in client.get("/api/sources").json()}
    assert listed[first_id]["is_custom"] is True
    default_row = next(
        item
        for item in listed.values()
        if item["url"] == engine_database.DEFAULT_SOURCES[0][1]
    )
    assert default_row["is_custom"] is False

    updated = client.put(
        f"/api/sources/{first_id}",
        json={
            "name": "  Sci-Fi Weekly  ",
            "url": "https://books.example.test/moved-feed.xml",
        },
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["name"] == "Sci-Fi Weekly"
    assert body["url"] == "https://books.example.test/moved-feed.xml"
    assert body["job_id"]

    stored = row(
        "SELECT name,url,last_scanned_at,last_status FROM sources WHERE id=?",
        (first_id,),
    )
    assert stored == {
        "name": "Sci-Fi Weekly",
        "url": "https://books.example.test/moved-feed.xml",
        "last_scanned_at": None,
        "last_status": None,
    }

    conflict = client.put(
        f"/api/sources/{first_id}",
        json={
            "name": "Conflicting Feed",
            "url": "https://books.example.test/other-feed.xml",
        },
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"] == "A source with that URL already exists"
    assert row("SELECT name,url FROM sources WHERE id=?", (first_id,)) == {
        "name": "Sci-Fi Weekly",
        "url": "https://books.example.test/moved-feed.xml",
    }

    with transaction() as con:
        con.execute("CREATE UNIQUE INDEX idx_test_unique_source_name ON sources(name)")
    try:
        name_conflict = client.put(
            f"/api/sources/{second_id}",
            json={"name": "Sci-Fi Weekly"},
        )
        assert name_conflict.status_code == 409
        assert (
            name_conflict.json()["detail"]
            == "A source with that name already exists"
        )
    finally:
        with transaction() as con:
            con.execute("DROP INDEX idx_test_unique_source_name")

    token = client.post(
        "/api/settings/api-tokens", json={"name": "Source update v1"}
    ).json()["token"]
    v1_headers = {"Authorization": f"Bearer {token}"}
    v1_ok = client.put(
        f"/api/v1/sources/{second_id}",
        headers=v1_headers,
        json={"name": "Mystery Shelf", "url": "https://books.example.test/mystery.xml"},
    )
    assert v1_ok.status_code == 200
    assert v1_ok.json()["name"] == "Mystery Shelf"
    assert v1_ok.json()["url"] == "https://books.example.test/mystery.xml"

    v1_conflict = client.put(
        f"/api/v1/sources/{second_id}",
        headers=v1_headers,
        json={"url": "https://books.example.test/moved-feed.xml"},
    )
    assert v1_conflict.status_code == 409

    monkeypatch.setattr(main, "score_all", lambda *_args, **_kwargs: asyncio.sleep(0, result=2))
    result = asyncio.run(handle_job(f"source:{first_id}"))
    assert result["collected"] == 2
    assert scanned_urls == ["https://books.example.test/moved-feed.xml"]

    with transaction() as con:
        con.execute("DELETE FROM jobs WHERE kind='score'")
    renamed_only = client.put(
        f"/api/sources/{first_id}",
        json={"name": "Sci-Fi Weekly Digest"},
    )
    assert renamed_only.status_code == 200
    assert renamed_only.json()["name"] == "Sci-Fi Weekly Digest"
    assert renamed_only.json()["job_id"] is None
    renamed_same_url = client.put(
        f"/api/sources/{first_id}",
        json={
            "name": "Sci-Fi Weekly Archive",
            "url": "https://books.example.test/moved-feed.xml",
        },
    )
    assert renamed_same_url.status_code == 200
    assert renamed_same_url.json()["name"] == "Sci-Fi Weekly Archive"
    assert renamed_same_url.json()["job_id"] is None
    assert row("SELECT 1 FROM jobs WHERE kind='score' LIMIT 1") is None


def test_update_or_refresh_during_running_source_scan_reenqueues_and_scans_one_time_source(
    database, monkeypatch
):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    monkeypatch.setattr(
        main, "score_all", lambda *_args, **_kwargs: asyncio.sleep(0, result=1)
    )
    with transaction() as con:
        source_id = con.execute(
            "INSERT INTO sources(name,url,enabled,lifecycle) VALUES(?,?,1,'one_time')",
            ("Initial Feed", "https://books.example.test/initial-one-time.xml"),
        ).lastrowid
        con.execute(
            "INSERT INTO jobs(id,kind,status) VALUES(?,?, 'running')",
            ("running-initial-job", f"source:{source_id}"),
        )

    client = TestClient(app, headers=authenticated_headers())
    scanned_urls: list[str] = []
    mid_run_edit_response: dict[str, object] = {}

    async def scan_with_mid_run_url_edit(source):
        scanned_urls.append(source["url"])
        if len(scanned_urls) == 1:
            resp = client.put(
                f"/api/sources/{source['id']}",
                json={
                    "name": "Edited Mid-Scan",
                    "url": "https://books.example.test/edited-mid-scan.xml",
                },
            )
            assert resp.status_code == 200
            mid_run_edit_response.update(resp.json())
        status = "ok:1" if len(scanned_urls) == 1 else "ok:3"
        with transaction() as con:
            con.execute(
                "UPDATE sources SET last_scanned_at=CURRENT_TIMESTAMP, last_status=? WHERE id=?",
                (status, source["id"]),
            )
        return 1 if len(scanned_urls) == 1 else 3

    monkeypatch.setattr(main, "scan_source", scan_with_mid_run_url_edit)

    first_run = asyncio.run(handle_job(f"source:{source_id}"))
    assert first_run["collected"] == 1
    followup_job_id = mid_run_edit_response["job_id"]
    assert followup_job_id and followup_job_id != "running-initial-job"
    assert row("SELECT kind,status FROM jobs WHERE id=?", (followup_job_id,)) == {
        "kind": f"source:{source_id}",
        "status": "queued",
    }
    assert row(
        "SELECT name,url,last_scanned_at,last_status FROM sources WHERE id=?",
        (source_id,),
    ) == {
        "name": "Edited Mid-Scan",
        "url": "https://books.example.test/edited-mid-scan.xml",
        "last_scanned_at": None,
        "last_status": None,
    }

    with transaction() as con:
        con.execute(
            "UPDATE jobs SET status='complete' WHERE id='running-initial-job'"
        )
        con.execute(
            "UPDATE jobs SET status='running' WHERE id=?",
            (followup_job_id,),
        )

    second_run = asyncio.run(handle_job(f"source:{source_id}"))
    assert "skipped" not in second_run
    assert second_run["collected"] == 3
    assert scanned_urls == [
        "https://books.example.test/initial-one-time.xml",
        "https://books.example.test/edited-mid-scan.xml",
    ]
    after_second = row(
        "SELECT last_scanned_at,last_status FROM sources WHERE id=?",
        (source_id,),
    )
    assert after_second["last_scanned_at"] is not None
    assert after_second["last_status"] == "ok:3"

    # Now verify a manual refresh landing while a one-time scan is running.
    with transaction() as con:
        con.execute(
            "UPDATE jobs SET status='complete' WHERE id=?",
            (followup_job_id,),
        )
        con.execute(
            "UPDATE sources SET last_scanned_at=NULL WHERE id=?",
            (source_id,),
        )
        con.execute(
            "INSERT INTO jobs(id,kind,status) VALUES(?,?, 'running')",
            ("running-refresh-job", f"source:{source_id}"),
        )

    refresh_scans: list[str] = []
    mid_run_refresh_response: dict[str, object] = {}

    async def scan_with_mid_run_refresh(source):
        refresh_scans.append(source["url"])
        if len(refresh_scans) == 1:
            resp = client.post(f"/api/sources/{source['id']}/refresh")
            assert resp.status_code == 200
            mid_run_refresh_response.update(resp.json())
        with transaction() as con:
            con.execute(
                "UPDATE sources SET last_scanned_at=CURRENT_TIMESTAMP, last_status='ok:2' WHERE id=?",
                (source["id"],),
            )
        return 2

    monkeypatch.setattr(main, "scan_source", scan_with_mid_run_refresh)

    asyncio.run(handle_job(f"source:{source_id}"))
    refresh_followup_id = mid_run_refresh_response["job_id"]
    assert refresh_followup_id and refresh_followup_id != "running-refresh-job"
    assert row(
        "SELECT kind,status FROM jobs WHERE id=?", (refresh_followup_id,)
    ) == {
        "kind": f"source:{source_id}",
        "status": "queued",
    }
    assert (
        row("SELECT last_scanned_at FROM sources WHERE id=?", (source_id,))[
            "last_scanned_at"
        ]
        is None
    )

    with transaction() as con:
        con.execute(
            "UPDATE jobs SET status='complete' WHERE id='running-refresh-job'"
        )
        con.execute(
            "UPDATE jobs SET status='running' WHERE id=?",
            (refresh_followup_id,),
        )

    followup_refresh_run = asyncio.run(handle_job(f"source:{source_id}"))
    assert "skipped" not in followup_refresh_run
    assert followup_refresh_run["collected"] == 2
    assert len(refresh_scans) == 2
    assert (
        row("SELECT last_scanned_at FROM sources WHERE id=?", (source_id,))[
            "last_scanned_at"
        ]
        is not None
    )


def test_switching_scanned_one_time_source_to_permanent_reenqueues_and_scans(
    database, monkeypatch
):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    monkeypatch.setattr(
        main, "score_all", lambda *_args, **_kwargs: asyncio.sleep(0, result=2)
    )
    with transaction() as con:
        source_id = con.execute(
            "INSERT INTO sources(name,url,enabled,lifecycle,last_status,last_scanned_at,last_attempted_at) "
            "VALUES(?,?,1,'one_time','ok:2','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')",
            ("Promoted Feed", "https://books.example.test/promoted.xml"),
        ).lastrowid

    skipped = asyncio.run(handle_job(f"source:{source_id}"))
    assert skipped == {"skipped": "already_scanned", "source_id": source_id}

    client = TestClient(app, headers=authenticated_headers())
    noop_one_time = client.put(
        f"/api/sources/{source_id}", json={"lifecycle": "one_time"}
    )
    assert noop_one_time.status_code == 200
    assert noop_one_time.json()["job_id"] is None
    same_url_rename = client.put(
        f"/api/sources/{source_id}",
        json={
            "name": "Promoted Feed Renamed",
            "url": "https://books.example.test/promoted.xml",
        },
    )
    assert same_url_rename.status_code == 200
    assert same_url_rename.json()["job_id"] is None
    assert row(
        "SELECT name,last_scanned_at,last_attempted_at FROM sources WHERE id=?",
        (source_id,),
    ) == {
        "name": "Promoted Feed Renamed",
        "last_scanned_at": "2026-01-01T00:00:00+00:00",
        "last_attempted_at": "2026-01-01T00:00:00+00:00",
    }

    scanned_urls: list[str] = []

    async def fake_scan_source(source):
        scanned_urls.append(source["url"])
        with transaction() as con:
            con.execute(
                "UPDATE sources SET last_scanned_at=CURRENT_TIMESTAMP, last_status='ok:2' WHERE id=?",
                (source["id"],),
            )
        return 2

    monkeypatch.setattr(main, "scan_source", fake_scan_source)

    promoted = client.put(
        f"/api/sources/{source_id}", json={"lifecycle": "permanent"}
    )
    assert promoted.status_code == 200
    body = promoted.json()
    assert body["lifecycle"] == "permanent"
    assert body["job_id"]
    assert row("SELECT kind,status FROM jobs WHERE id=?", (body["job_id"],)) == {
        "kind": f"source:{source_id}",
        "status": "queued",
    }

    result = asyncio.run(handle_job(f"source:{source_id}"))
    assert result["collected"] == 2
    assert scanned_urls == ["https://books.example.test/promoted.xml"]

    with transaction() as con:
        con.execute("DELETE FROM jobs WHERE kind=?", (f"source:{source_id}",))
    noop_permanent = client.put(
        f"/api/sources/{source_id}", json={"lifecycle": "permanent"}
    )
    assert noop_permanent.status_code == 200
    assert noop_permanent.json()["job_id"] is None
    assert (
        row(
            "SELECT 1 FROM jobs WHERE kind=? AND status='queued' LIMIT 1",
            (f"source:{source_id}",),
        )
        is None
    )


def test_permanent_source_mid_run_url_edit_and_error_path_reenqueue_cleanly(
    database, monkeypatch
):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    monkeypatch.setattr(
        main, "score_all", lambda *_args, **_kwargs: asyncio.sleep(0, result=2)
    )
    with transaction() as con:
        source_id = con.execute(
            "INSERT INTO sources(name,url,enabled,lifecycle,last_status,last_scanned_at) "
            "VALUES(?,?,1,'permanent','ok:1','2026-01-01T00:00:00+00:00')",
            ("Permanent Feed", "https://books.example.test/perm-initial.xml"),
        ).lastrowid
        con.execute(
            "INSERT INTO jobs(id,kind,status) VALUES(?,?, 'running')",
            ("running-perm-job", f"source:{source_id}"),
        )

    client = TestClient(app, headers=authenticated_headers())
    perm_scanned_urls: list[str] = []
    mid_run_perm_edit: dict[str, object] = {}

    async def scan_perm_with_mid_run_edit(source):
        perm_scanned_urls.append(source["url"])
        if len(perm_scanned_urls) == 1:
            resp = client.put(
                f"/api/sources/{source['id']}",
                json={
                    "name": "Permanent Moved",
                    "url": "https://books.example.test/perm-updated.xml",
                },
            )
            assert resp.status_code == 200
            mid_run_perm_edit.update(resp.json())
        status = "ok:1" if len(perm_scanned_urls) == 1 else "ok:4"
        with transaction() as con:
            con.execute(
                "UPDATE sources SET last_scanned_at=CURRENT_TIMESTAMP, last_status=? WHERE id=?",
                (status, source["id"]),
            )
        return 1 if len(perm_scanned_urls) == 1 else 4

    monkeypatch.setattr(main, "scan_source", scan_perm_with_mid_run_edit)

    first_perm_run = asyncio.run(handle_job(f"source:{source_id}"))
    assert first_perm_run["collected"] == 1
    perm_followup_id = mid_run_perm_edit["job_id"]
    assert perm_followup_id and perm_followup_id != "running-perm-job"
    assert row(
        "SELECT COUNT(*) count FROM jobs WHERE kind=? AND status='queued'",
        (f"source:{source_id}",),
    ) == {"count": 1}
    assert row(
        "SELECT name,url,last_scanned_at,last_status FROM sources WHERE id=?",
        (source_id,),
    ) == {
        "name": "Permanent Moved",
        "url": "https://books.example.test/perm-updated.xml",
        "last_scanned_at": None,
        "last_status": None,
    }

    with transaction() as con:
        con.execute("UPDATE jobs SET status='complete' WHERE id='running-perm-job'")
        con.execute("UPDATE jobs SET status='running' WHERE id=?", (perm_followup_id,))

    second_perm_run = asyncio.run(handle_job(f"source:{source_id}"))
    assert second_perm_run["collected"] == 4
    assert perm_scanned_urls == [
        "https://books.example.test/perm-initial.xml",
        "https://books.example.test/perm-updated.xml",
    ]
    assert (
        row("SELECT last_status FROM sources WHERE id=?", (source_id,))["last_status"]
        == "ok:4"
    )

    # Error-path mid-run case: old scan raises after a mid-run URL edit.
    with transaction() as con:
        con.execute("UPDATE jobs SET status='complete' WHERE id=?", (perm_followup_id,))
        con.execute(
            "INSERT INTO jobs(id,kind,status) VALUES(?,?, 'running')",
            ("running-error-job", f"source:{source_id}"),
        )

    error_path_scans: list[str] = []
    mid_error_edit: dict[str, object] = {}

    async def failing_scan_after_mid_run_edit(source):
        error_path_scans.append(source["url"])
        if len(error_path_scans) == 1:
            resp = client.put(
                f"/api/sources/{source['id']}",
                json={"url": "https://books.example.test/perm-recovered.xml"},
            )
            assert resp.status_code == 200
            mid_error_edit.update(resp.json())
            raise RuntimeError("old feed upstream 502")
        with transaction() as con:
            con.execute(
                "UPDATE sources SET last_scanned_at=CURRENT_TIMESTAMP, last_status='ok:5' WHERE id=?",
                (source["id"],),
            )
        return 5

    monkeypatch.setattr(main, "scan_source", failing_scan_after_mid_run_edit)

    with pytest.raises(RuntimeError, match="old feed upstream 502"):
        asyncio.run(handle_job(f"source:{source_id}"))

    error_followup_id = mid_error_edit["job_id"]
    assert error_followup_id and error_followup_id != "running-error-job"
    assert row(
        "SELECT url,last_scanned_at,last_status FROM sources WHERE id=?",
        (source_id,),
    ) == {
        "url": "https://books.example.test/perm-recovered.xml",
        "last_scanned_at": None,
        "last_status": None,
    }
    assert row("SELECT kind,status FROM jobs WHERE id=?", (error_followup_id,)) == {
        "kind": f"source:{source_id}",
        "status": "queued",
    }

    with transaction() as con:
        con.execute("UPDATE jobs SET status='failed' WHERE id='running-error-job'")
        con.execute("UPDATE jobs SET status='running' WHERE id=?", (error_followup_id,))

    recovered_run = asyncio.run(handle_job(f"source:{source_id}"))
    assert recovered_run["collected"] == 5
    assert error_path_scans == [
        "https://books.example.test/perm-updated.xml",
        "https://books.example.test/perm-recovered.xml",
    ]
    assert (
        row("SELECT last_status FROM sources WHERE id=?", (source_id,))["last_status"]
        == "ok:5"
    )


def test_update_source_url_rejects_unsafe_addresses_and_inline_nyt_keys(
    database, monkeypatch
):
    with transaction() as con:
        source_id = con.execute(
            "INSERT INTO sources(name,url,enabled) VALUES(?,?,1)",
            ("Custom Feed", "https://books.example.test/feed.xml"),
        ).lastrowid

    with TestClient(app, headers=authenticated_headers()) as client:
        for unsafe_url in (
            "http://books.example.test/plain-http.xml",
            "https://127.0.0.1/loopback.xml",
            "https://localhost/loopback.xml",
            "https://10.0.0.12/private.xml",
            "https://169.254.169.254/latest/meta-data",
            "https://internal.local/feed.xml",
        ):
            response = client.put(
                f"/api/sources/{source_id}", json={"url": unsafe_url}
            )
            assert response.status_code == 400, unsafe_url

        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *args, **kwargs: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.25", 443))
            ],
        )
        dns_private = client.put(
            f"/api/sources/{source_id}",
            json={"url": "https://rebind.example.test/feed.xml"},
        )
        assert dns_private.status_code == 400

        nyt_inline = client.put(
            f"/api/sources/{source_id}",
            json={
                "url": "https://api.nytimes.com/svc/books/v3/lists/overview.json?api-key=never-echo-secret"
            },
        )
        assert nyt_inline.status_code == 400
        assert "encrypted NYT API settings" in nyt_inline.json()["detail"]
        assert "never-echo-secret" not in nyt_inline.text

        bad_name = client.put(f"/api/sources/{source_id}", json={"name": " "})
        assert bad_name.status_code == 400

    assert row("SELECT name,url FROM sources WHERE id=?", (source_id,)) == {
        "name": "Custom Feed",
        "url": "https://books.example.test/feed.xml",
    }


def test_update_source_guardrails_protect_builtin_association_and_seeded_defaults(
    database, monkeypatch
):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    with transaction() as con:
        builtin_id = con.execute(
            "SELECT id FROM sources WHERE url='builtin://upcoming'"
        ).fetchone()["id"]
        assoc_id = con.execute(
            "INSERT INTO sources(name,url,kind,enabled) VALUES(?,?,?,1)",
            ("Open Library Lists", "association://openlibrary_lists", "association"),
        ).lastrowid
        seeded = con.execute(
            "SELECT id,url FROM sources WHERE url=?",
            (engine_database.DEFAULT_SOURCES[0][1],),
        ).fetchone()
        con.execute(
            "UPDATE sources SET lifecycle='permanent', last_scanned_at=?, last_status='ok:3' WHERE id=?",
            ("2026-01-01T00:00:00+00:00", seeded["id"]),
        )

    with TestClient(app, headers=authenticated_headers()) as client:
        for locked_id in (builtin_id, assoc_id):
            for payload in (
                {"name": "Renamed Locked Source"},
                {"url": "https://books.example.test/override.xml"},
                {"filters": {"include_genres": ["Fantasy"], "exclude_genres": []}},
                {"lifecycle": "one_time"},
            ):
                resp = client.put(f"/api/sources/{locked_id}", json=payload)
                assert resp.status_code == 400, (locked_id, payload)

        seeded_url_resp = client.put(
            f"/api/sources/{seeded['id']}",
            json={"url": "https://books.example.test/override-default.xml"},
        )
        assert seeded_url_resp.status_code == 400
        assert row("SELECT url FROM sources WHERE id=?", (seeded["id"],))["url"] == seeded["url"]

        to_one_time = client.put(
            f"/api/sources/{seeded['id']}", json={"lifecycle": "one_time"}
        )
        assert to_one_time.status_code == 200
        assert to_one_time.json()["lifecycle"] == "one_time"
        assert row(
            "SELECT last_scanned_at FROM sources WHERE id=?", (seeded["id"],)
        )["last_scanned_at"] is None

        to_permanent = client.put(
            f"/api/sources/{seeded['id']}", json={"lifecycle": "permanent"}
        )
        assert to_permanent.status_code == 200
        assert to_permanent.json()["lifecycle"] == "permanent"


def test_refresh_single_source_scans_one_time_and_preserves_recommended_scores(
    database, monkeypatch
):
    with transaction() as con:
        builtin_id = con.execute(
            "SELECT id FROM sources WHERE url='builtin://upcoming'"
        ).fetchone()["id"]
        assoc_id = con.execute(
            "INSERT INTO sources(name,url,kind,enabled) VALUES(?,?,?,1)",
            ("Assoc Provider", "association://test_provider", "association"),
        ).lastrowid
        disabled_id = con.execute(
            "INSERT INTO sources(name,url,enabled) VALUES(?,?,0)",
            ("Disabled Feed", "https://books.example.test/disabled.xml"),
        ).lastrowid
        source_id = con.execute(
            "INSERT INTO sources(name,url,enabled,lifecycle,last_status,last_scanned_at) "
            "VALUES(?,?,1,'one_time','error:timeout','2026-01-01T00:00:00+00:00')",
            ("Retry Feed", "https://books.example.test/retry.xml"),
        ).lastrowid
        candidate_id = 1
        con.execute(
            "UPDATE candidates SET source_id=?, status='recommended', score=92.4 WHERE id=?",
            (source_id, candidate_id),
        )

    client = TestClient(app, headers=authenticated_headers())
    assert client.post("/api/sources/999999/refresh").status_code == 404
    assert client.post(f"/api/sources/{builtin_id}/refresh").status_code == 400
    assert client.post(f"/api/sources/{assoc_id}/refresh").status_code == 400
    assert client.post(f"/api/sources/{disabled_id}/refresh").status_code == 400

    refreshed = client.post(f"/api/sources/{source_id}/refresh")
    assert refreshed.status_code == 200
    job_id = refreshed.json()["job_id"]
    assert job_id
    assert row("SELECT kind FROM jobs WHERE id=?", (job_id,)) == {
        "kind": f"source:{source_id}"
    }
    assert row("SELECT last_scanned_at FROM sources WHERE id=?", (source_id,)) == {
        "last_scanned_at": None
    }
    assert row(
        "SELECT status,score FROM candidates WHERE id=?", (candidate_id,)
    ) == {"status": "recommended", "score": 92.4}

    token = client.post(
        "/api/settings/api-tokens", json={"name": "Source refresh v1"}
    ).json()["token"]
    v1_refreshed = client.post(
        f"/api/v1/sources/{source_id}/refresh",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert v1_refreshed.status_code == 200
    assert v1_refreshed.json()["job_id"] == job_id

    async def failing_scan(_source):
        raise RuntimeError("temporary feed outage")

    monkeypatch.setattr(main, "scan_source", failing_scan)
    with pytest.raises(RuntimeError, match="temporary feed outage"):
        asyncio.run(handle_job(f"source:{source_id}"))

    assert row("SELECT status,score FROM candidates WHERE id=?", (candidate_id,)) == {
        "status": "recommended",
        "score": 92.4,
    }

    scanned_ids: list[int] = []

    async def succeeding_scan(source):
        scanned_ids.append(source["id"])
        with transaction() as con:
            con.execute(
                "UPDATE sources SET last_scanned_at=CURRENT_TIMESTAMP, last_status='ok:1' WHERE id=?",
                (source["id"],),
            )
        return 1

    monkeypatch.setattr(main, "scan_source", succeeding_scan)
    monkeypatch.setattr(main, "score_all", lambda *_args, **_kwargs: asyncio.sleep(0, result=1))
    result = asyncio.run(handle_job(f"source:{source_id}"))
    assert result["source_id"] == source_id
    assert result["collected"] == 1
    assert result["scored"] == 1
    assert scanned_ids == [source_id]
    assert row("SELECT status,score FROM candidates WHERE id=?", (candidate_id,)) == {
        "status": "recommended",
        "score": 92.4,
    }


def test_storygraph_csv_imports_completed_books_fractional_ratings_dates_and_isbns(database):
    payload = (
        "Title,Authors,Contributors,ISBN/UID,Format,Read Status,Date Added,Last Date Read,Dates Read,Star Rating\n"
        '"This Is How You Lose the Time War","Amal El-Mohtar, Max Gladstone",,9781534431003,hardcover,read,2024/01/01,2024/01/18,2024/01/05-2024/01/18,4.75\n'
        '"A Canticle for Leibowitz","Walter M. Miller, Jr.",,0553273817,paperback,read,2023/06/01,,2021/03/01-2021/03/10; 2023/06/02-2023/06/19,4.25\n'
        '"Indie Digital Book",Solo Writer,,B08FHBV4ZX,digital,read,2024/02/01,,2024/02/10-,3.5\n'
        '"UUID Edition",Another Writer,,a1b2c3d4-e5f6-4789-abcd-ef0123456789,audio,read,2024/03/01,2024-03-15,,\n'
    ).encode("utf-8")

    result = import_csv(payload)
    assert result == {"imported": 4, "source": "storygraph_csv"}

    imported_rows = rows(
        "SELECT title, author, rating, read_at, isbn, source "
        "FROM reads WHERE source='storygraph_csv' ORDER BY title"
    )
    assert imported_rows == [
        {
            "title": "A Canticle for Leibowitz",
            "author": "Walter M. Miller, Jr.",
            "rating": 4.25,
            "read_at": "2023/06/19",
            "isbn": "9780553273816",
            "source": "storygraph_csv",
        },
        {
            "title": "Indie Digital Book",
            "author": "Solo Writer",
            "rating": 3.5,
            "read_at": None,
            "isbn": None,
            "source": "storygraph_csv",
        },
        {
            "title": "This Is How You Lose the Time War",
            "author": "Amal El-Mohtar, Max Gladstone".split(",")[0],
            "rating": 4.75,
            "read_at": "2024/01/18",
            "isbn": "9781534431003",
            "source": "storygraph_csv",
        },
        {
            "title": "UUID Edition",
            "author": "Another Writer",
            "rating": None,
            "read_at": "2024-03-15",
            "isbn": None,
            "source": "storygraph_csv",
        },
    ]


def test_invalid_storygraph_rating_rolls_back_all_rows(database):
    payload = (
        "Title,Authors,Read Status,Star Rating\n"
        "First Book,Writer One,read,4.5\n"
        "Out Of Range,Writer Two,read,5.25\n"
    ).encode("utf-8")
    with pytest.raises(ValueError, match="CSV row 3.*Out Of Range.*No books were imported"):
        import_csv(payload)
    assert row("SELECT COUNT(*) count FROM reads WHERE source='storygraph_csv'")["count"] == 0


def test_unsupported_csv_headers_name_supported_formats(database):
    payload = b"Heading,Writer,Rating\nSome Book,Someone,4\n"
    with pytest.raises(ValueError, match="Goodreads.*StoryGraph"):
        import_csv(payload)
    calibre_like = b"Title,Authors,Publisher\nSome Book,Someone,Press\n"
    with pytest.raises(ValueError, match="Goodreads.*StoryGraph"):
        import_csv(calibre_like)


def test_csv_upload_routes_handle_storygraph_and_goodreads(database):
    storygraph_csv = (
        "Title,Authors,ISBN/UID,Read Status,Last Date Read,Star Rating\n"
        "Piranesi,Susanna Clarke,9781635575637,read,2025/08/12,4.75\n"
    ).encode("utf-8")
    goodreads_csv_bytes = (
        "Title,Author,My Rating,Date Read,Exclusive Shelf\n"
        "Dune,Frank Herbert,5,2025/07/01,read\n"
    ).encode("utf-8")

    with TestClient(app, headers=authenticated_headers()) as client:
        sg_res = client.post(
            "/api/import/csv",
            files={"file": ("storygraph.csv", storygraph_csv, "text/csv")},
        )
        assert sg_res.status_code == 200
        assert sg_res.json()["imported"] == 1
        assert sg_res.json()["source"] == "storygraph_csv"
        assert sg_res.json()["job_id"]

        gr_res = client.post(
            "/api/import/goodreads/csv",
            files={"file": ("goodreads.csv", goodreads_csv_bytes, "text/csv")},
        )
        assert gr_res.status_code == 200
        assert gr_res.json()["imported"] == 1
        assert gr_res.json()["source"] == "goodreads_csv"

        bad_res = client.post(
            "/api/import/csv",
            files={"file": ("other.csv", b"Name,Price\nTea,5\n", "text/csv")},
        )
        assert bad_res.status_code == 400
        assert "StoryGraph" in bad_res.json()["detail"]
