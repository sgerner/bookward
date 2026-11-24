import asyncio
from datetime import datetime, timedelta, timezone
import json
import socket
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient
import httpx
import pytest
import respx

from auth_helpers import authenticated_headers

from afterword_engine import profiles
from afterword_engine.association_sources.common import fetch_pinned_association_json
from afterword_engine.config import settings
from afterword_engine.covers import (
    GOOGLE_BOOKS_SEARCH,
    METADATA_NORMALIZATION_VERSION,
    OPEN_LIBRARY_SEARCH,
)
from afterword_engine.database import (
    DEFAULT_SOURCES,
    MIGRATIONS,
    initialize,
    row,
    rows,
    transaction,
)
from afterword_engine.ingestion import (
    candidate_metadata_refresh_remaining,
    fetch_bytes,
    read_metadata_refresh_remaining,
    refresh_missing_candidate_metadata,
    refresh_read_metadata,
    scan_source,
)
from afterword_engine.main import (
    SOURCE_SYNC_ERROR_RETRY_SECONDS,
    app,
    handle_job,
    source_sync_due_sources,
)
from afterword_engine.secrets import seal
from afterword_engine.security import sanitize_pinned_error_message
from afterword_engine.tenancy import profile_scope


@pytest.fixture()
def database(tmp_path: Path):
    settings.db = str(tmp_path / "test.db")
    initialize()
    profiles.initialize_auth()
    return settings.db


def _auth_headers() -> dict[str, str]:
    return authenticated_headers()


def test_additive_migration_applies_across_all_profile_databases(database, tmp_path):
    profile_ids = [
        "legacy",
        "0123456789abcdef0123456789abcdef",
        "fedcba9876543210fedcba9876543210",
    ]
    for profile_id in profile_ids:
        with profile_scope(profile_id):
            initialize(seed_demo=False)
            source_columns = {
                col["name"] for col in rows("PRAGMA table_info(sources)")
            }
            quality_columns = {
                col["name"] for col in rows("PRAGMA table_info(candidate_quality)")
            }
            assert "last_attempted_at" in source_columns
            assert "metadata_retry_status" in quality_columns

    v26_db = tmp_path / "upgrade_v26.db"
    settings.db = str(v26_db)
    with sqlite3.connect(v26_db) as con:
        con.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        for version, script in MIGRATIONS:
            if version > 26:
                break
            con.executescript(script)
            con.execute("INSERT INTO schema_migrations(version) VALUES(?)", (version,))
        con.execute(
            "INSERT INTO sources(name, url, last_scanned_at) VALUES(?, ?, ?)",
            ("Existing Feed", "https://example.test/feed.xml", "2026-04-01T12:00:00Z"),
        )
        existing_candidate_id = con.execute(
            "INSERT INTO candidates(title, author, normalized_key) VALUES(?, ?, ?)",
            ("Pre-existing Candidate", "Author Zero", "pre existing candidate author zero"),
        ).lastrowid

    initialize(seed_demo=False)
    assert row("SELECT 1 FROM schema_migrations WHERE version=27") is not None
    upgraded = row(
        "SELECT last_scanned_at, last_attempted_at FROM sources WHERE url=?",
        ("https://example.test/feed.xml",),
    )
    assert upgraded == {
        "last_scanned_at": "2026-04-01T12:00:00Z",
        "last_attempted_at": "2026-04-01T12:00:00Z",
    }
    upgraded_quality = row(
        "SELECT metadata_retry_status FROM candidate_quality WHERE candidate_id=?",
        (existing_candidate_id,),
    )
    assert upgraded_quality == {"metadata_retry_status": ""}

    with transaction() as con:
        new_candidate_id = con.execute(
            "INSERT INTO candidates(title, author, normalized_key) VALUES(?, ?, ?)",
            ("Post-migration Candidate", "Author One", "post migration candidate author one"),
        ).lastrowid
    assert row(
        "SELECT metadata_retry_status FROM candidate_quality WHERE candidate_id=?",
        (new_candidate_id,),
    ) == {"metadata_retry_status": ""}

    # Re-running initialize() is idempotent and preserves migrated columns.
    initialize(seed_demo=False)
    assert row(
        "SELECT last_scanned_at, last_attempted_at FROM sources WHERE url=?",
        ("https://example.test/feed.xml",),
    ) == upgraded
    assert row(
        "SELECT metadata_retry_status FROM candidate_quality WHERE candidate_id=?",
        (existing_candidate_id,),
    ) == {"metadata_retry_status": ""}


def test_default_sources_seed_even_when_custom_source_shares_default_name(tmp_path):
    custom_db = tmp_path / "custom_named_source.db"
    settings.db = str(custom_db)
    default_name, default_url, _ = DEFAULT_SOURCES[0]
    custom_url = "https://custom.example.test/my-science-fiction-feed"

    with sqlite3.connect(custom_db) as con:
        con.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        for version, script in MIGRATIONS:
            con.executescript(script)
            con.execute("INSERT INTO schema_migrations(version) VALUES(?)", (version,))
        con.execute(
            "INSERT INTO sources(name, url, kind, enabled, is_default) VALUES(?, ?, 'web', 1, 0)",
            (default_name, custom_url),
        )

    initialize(seed_demo=False)
    same_name_urls = {
        item["url"]
        for item in rows("SELECT url FROM sources WHERE name=? AND kind='web'", (default_name,))
    }
    assert {custom_url, default_url} <= same_name_urls

    # Once seeded, editing that starter URL does not re-insert the default URL on restart.
    edited_url = "https://custom.example.test/edited-default-feed"
    with transaction() as con:
        con.execute("UPDATE sources SET url=? WHERE url=?", (edited_url, default_url))

    initialize(seed_demo=False)
    urls_after_restart = {
        item["url"]
        for item in rows("SELECT url FROM sources WHERE name=? AND kind='web'", (default_name,))
    }
    assert urls_after_restart == {custom_url, edited_url}


@respx.mock
def test_pinned_request_errors_report_hostname_and_preserve_last_scanned_at(database, monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    pinned_route = respx.get("https://93.184.216.34/feed.xml").mock(
        return_value=httpx.Response(404, text="Moved")
    )
    good_scan = "2026-04-05T08:00:00+00:00"
    with transaction() as con:
        con.execute("UPDATE sources SET enabled=0")
        source_id = con.execute(
            "INSERT INTO sources(name,url,enabled,lifecycle,last_status,last_scanned_at,last_attempted_at) "
            "VALUES(?,?,1,'permanent','ok:5',?,?)",
            (
                "Moved Publisher Feed",
                "https://books.example.test/feed.xml?api_key=super-secret-token",
                good_scan,
                good_scan,
            ),
        ).lastrowid

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        asyncio.run(handle_job(f"source:{source_id}"))

    assert pinned_route.called
    assert pinned_route.calls[0].request.headers["host"] == "books.example.test"
    assert "books.example.test/feed.xml?[redacted]" in str(exc_info.value)
    assert "93.184.216.34" not in str(exc_info.value)
    assert "super-secret-token" not in str(exc_info.value)

    stored = row("SELECT * FROM sources WHERE id=?", (source_id,))
    assert stored["last_scanned_at"] == good_scan
    assert stored["last_attempted_at"] != good_scan
    assert "books.example.test/feed.xml?[redacted]" in stored["last_status"]
    assert "93.184.216.34" not in stored["last_status"]
    assert "super-secret-token" not in stored["last_status"]

    # Association pinned JSON fetches also redact query secrets directly on HTTPStatusError.
    respx.get("https://93.184.216.34/svc/books/v3/lists/overview.json").mock(
        return_value=httpx.Response(401, json={"fault": {"faultstring": "Invalid ApiKey"}})
    )
    with pytest.raises(httpx.HTTPStatusError) as nyt_exc:
        asyncio.run(
            fetch_pinned_association_json(
                "https://api.nytimes.com/svc/books/v3/lists/overview.json",
                params={"api-key": "nyt-inline-secret"},
                provider="nyt",
                request_key="overview",
                source_label="NYT Overview",
                timeout_seconds=5.0,
                max_bytes=65536,
            )
        )
    assert "api.nytimes.com/svc/books/v3/lists/overview.json?[redacted]" in str(nyt_exc.value)
    assert "93.184.216.34" not in str(nyt_exc.value)
    assert "nyt-inline-secret" not in str(nyt_exc.value)

    # Despite last_scanned_at being days old, scheduling uses last_attempted_at.
    now = datetime.now(timezone.utc)
    assert [s["id"] for s in source_sync_due_sources(now)] == []
    after_retry_floor = now + timedelta(seconds=SOURCE_SYNC_ERROR_RETRY_SECONDS + 5)
    assert [s["id"] for s in source_sync_due_sources(after_retry_floor)] == [source_id]


@respx.mock
def test_pinned_transport_errors_also_hide_resolved_ip(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    respx.get("https://93.184.216.34/timeout").mock(
        side_effect=httpx.ConnectTimeout(
            "Timed out connecting to 93.184.216.34:443 (ref 93.184.216.340)"
        )
    )
    with pytest.raises(httpx.ConnectTimeout) as exc_info:
        asyncio.run(fetch_bytes("https://books.example.test/timeout?token=secret-query"))

    assert "books.example.test:443" in str(exc_info.value)
    assert "93.184.216.34:" not in str(exc_info.value)
    assert "93.184.216.340" in str(exc_info.value)
    assert "secret-query" not in str(exc_info.value)
    assert exc_info.value.request.url.host == "books.example.test"

    assert (
        sanitize_pinned_error_message(
            "connect [2001:db8::1]:443 vs 2001:db8::10",
            address="2001:db8::1",
            hostname="books.example.test",
        )
        == "connect books.example.test:443 vs 2001:db8::10"
    )


def test_empty_and_zero_match_filter_scans_retain_candidates_and_guard_one_time_sources(
    database, monkeypatch
):
    good_scan = "2026-04-08T09:15:00+00:00"
    with transaction() as con:
        con.execute("UPDATE sources SET enabled=0")
        source_id = con.execute(
            "INSERT INTO sources(name,url,enabled,lifecycle,filters,last_status,last_scanned_at,last_attempted_at) "
            "VALUES(?,?,1,'permanent',?,?,?,?)",
            (
                "Strict Genre Feed",
                "https://example.com/strict",
                json.dumps({"include_genres": ["hard sci-fi"]}),
                "ok:2",
                good_scan,
                good_scan,
            ),
        ).lastrowid
        con.execute(
            "INSERT INTO candidates(title,author,source_id,status,normalized_key) VALUES(?,?,?,?,?)",
            ("Existing New", "Author One", source_id, "new", "existing new author one"),
        )
        con.execute(
            "INSERT INTO candidates(title,author,source_id,status,normalized_key) VALUES(?,?,?,?,?)",
            (
                "Existing Recommended",
                "Author Two",
                source_id,
                "recommended",
                "existing recommended author two",
            ),
        )
        one_time_filtered_id = con.execute(
            "INSERT INTO sources(name,url,enabled,lifecycle,filters) VALUES(?,?,1,'one_time',?)",
            (
                "One-Time Filtered",
                "https://example.com/one-time-filtered",
                json.dumps({"include_genres": ["hard sci-fi"]}),
            ),
        ).lastrowid
        one_time_empty_id = con.execute(
            "INSERT INTO sources(name,url,enabled,lifecycle) VALUES(?,?,1,'one_time')",
            ("One-Time Empty", "https://example.com/one-time-empty"),
        ).lastrowid

    async def non_empty_romance_feed(_url):
        return "application/json", [
            {"title": "Unmatched Book", "author": "Author Three", "genres": ["Romance"]}
        ]

    async def no_enrichment(items):
        return items

    async def no_audit(*_args, **_kwargs):
        return {"checked": 0, "accepted": 0, "quarantined": 0, "rejected": 0}

    monkeypatch.setattr("afterword_engine.ingestion._fetch_and_parse_source", non_empty_romance_feed)
    monkeypatch.setattr("afterword_engine.ingestion.enrich_book_metadata", no_enrichment)
    monkeypatch.setattr("afterword_engine.main.audit_candidates", no_audit)

    source = row("SELECT * FROM sources WHERE id=?", (source_id,))
    assert asyncio.run(scan_source(source)) == 0
    after_filtered = row("SELECT * FROM sources WHERE id=?", (source_id,))
    assert after_filtered["last_status"] == "filtered:0"
    assert after_filtered["last_scanned_at"] == good_scan
    assert after_filtered["last_attempted_at"] != good_scan
    retained_statuses = {
        c["title"]: c["status"]
        for c in rows("SELECT title,status FROM candidates WHERE source_id=?", (source_id,))
    }
    assert retained_statuses == {
        "Existing New": "new",
        "Existing Recommended": "recommended",
    }

    # One-time source with 0 filter matches keeps last_scanned_at None, records
    # last_attempted_at + filtered:0, and trips already_scanned on subsequent runs.
    first_filtered_job = asyncio.run(handle_job(f"source:{one_time_filtered_id}"))
    assert first_filtered_job["collected"] == 0
    one_time_filtered = row("SELECT * FROM sources WHERE id=?", (one_time_filtered_id,))
    assert one_time_filtered["last_scanned_at"] is None
    assert one_time_filtered["last_attempted_at"] is not None
    assert one_time_filtered["last_status"] == "filtered:0"
    assert asyncio.run(handle_job(f"source:{one_time_filtered_id}")) == {
        "source_id": one_time_filtered_id,
        "skipped": "already_scanned",
    }

    async def empty_upstream_feed(_url):
        return "application/json", []

    with transaction() as con:
        con.execute(
            "UPDATE candidates SET status='recommended' WHERE source_id=? AND title='Existing Recommended'",
            (source_id,),
        )
    monkeypatch.setattr("afterword_engine.ingestion._fetch_and_parse_source", empty_upstream_feed)
    assert asyncio.run(scan_source(after_filtered)) == 0
    after_empty = row("SELECT * FROM sources WHERE id=?", (source_id,))
    assert after_empty["last_status"] == "empty:0"
    assert after_empty["last_scanned_at"] == good_scan
    retained_after_empty = {
        c["title"]: c["status"]
        for c in rows("SELECT title,status FROM candidates WHERE source_id=?", (source_id,))
    }
    assert retained_after_empty == {
        "Existing New": "new",
        "Existing Recommended": "recommended",
    }

    # One-time source with empty upstream also trips already_scanned until explicitly re-enabled or edited.
    first_empty_job = asyncio.run(handle_job(f"source:{one_time_empty_id}"))
    assert first_empty_job["collected"] == 0
    one_time_empty = row("SELECT * FROM sources WHERE id=?", (one_time_empty_id,))
    assert one_time_empty["last_scanned_at"] is None
    assert one_time_empty["last_attempted_at"] is not None
    assert one_time_empty["last_status"] == "empty:0"
    assert asyncio.run(handle_job(f"source:{one_time_empty_id}")) == {
        "source_id": one_time_empty_id,
        "skipped": "already_scanned",
    }

    with TestClient(app, headers=_auth_headers()) as client:
        assert client.put(f"/api/sources/{one_time_empty_id}/toggle").status_code == 200
        re_enabled = client.put(f"/api/sources/{one_time_empty_id}/toggle")
        assert re_enabled.status_code == 200
        reset_row = row("SELECT last_scanned_at, last_attempted_at FROM sources WHERE id=?", (one_time_empty_id,))
        assert reset_row == {"last_scanned_at": None, "last_attempted_at": None}


@respx.mock
def test_update_source_edits_url_toggles_lifecycles_both_ways_and_rescans_one_time(database, monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )
    with transaction() as con:
        source_id = con.execute(
            "INSERT INTO sources(name,url,enabled,lifecycle,last_status,last_scanned_at) "
            "VALUES(?,?,1,'one_time','ok:1','2026-04-01T00:00:00Z')",
            ("Editable One-Time", "https://books.example.test/v1"),
        ).lastrowid
        disabled_id = con.execute(
            "INSERT INTO sources(name,url,enabled,lifecycle) VALUES(?,?,0,'permanent')",
            ("Disabled Feed", "https://books.example.test/disabled-v1"),
        ).lastrowid

    respx.get("https://93.184.216.34/v2").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "application/rss+xml"},
            text=(
                "<?xml version='1.0' encoding='UTF-8'?>"
                "<rss version='2.0'><channel><title>Books</title>"
                "<item><title>Reloaded One-Time Book</title><author>Jane Doe</author>"
                "<link>https://books.example.test/book-1</link></item>"
                "</channel></rss>"
            ),
        )
    )
    respx.get(OPEN_LIBRARY_SEARCH).mock(return_value=httpx.Response(200, json={"docs": []}))
    respx.get(GOOGLE_BOOKS_SEARCH).mock(return_value=httpx.Response(200, json={"items": []}))

    with TestClient(app, headers=_auth_headers()) as client:
        # Reject inline NYT credentials on URL update
        nyt_attempt = client.put(
            f"/api/sources/{source_id}",
            json={
                "url": "https://api.nytimes.com/svc/books/v3/lists/overview.json?api-key=leak-me"
            },
        )
        assert nyt_attempt.status_code == 400
        assert "leak-me" not in nyt_attempt.text

        # Disabled sources save URL/filter edits without queuing a background scan job
        disabled_edit = client.put(
            f"/api/sources/{disabled_id}",
            json={"url": "https://books.example.test/disabled-v2"},
        )
        assert disabled_edit.status_code == 200
        assert disabled_edit.json()["job_id"] is None

        # Edit URL on one_time source -> clears last_scanned_at and queues rescan
        edited = client.put(
            f"/api/sources/{source_id}",
            json={"url": "https://books.example.test/v2"},
        )
        assert edited.status_code == 200
        assert edited.json()["url"] == "https://books.example.test/v2"
        assert edited.json()["lifecycle"] == "one_time"
        assert edited.json()["job_id"] is not None

        cleared = row("SELECT url,last_scanned_at FROM sources WHERE id=?", (source_id,))
        assert cleared["url"] == "https://books.example.test/v2"
        assert cleared["last_scanned_at"] is None

        # Running the queued source job rescans the edited one_time source
        job_result = asyncio.run(handle_job(f"source:{source_id}"))
        assert job_result["collected"] == 1
        assert row("SELECT last_scanned_at FROM sources WHERE id=?", (source_id,))["last_scanned_at"] is not None
        assert row("SELECT 1 FROM candidates WHERE title='Reloaded One-Time Book'") is not None

        # Toggle one_time -> permanent -> one_time
        to_permanent = client.put(
            f"/api/sources/{source_id}",
            json={"lifecycle": "permanent"},
        )
        assert to_permanent.status_code == 200
        assert to_permanent.json()["lifecycle"] == "permanent"
        assert to_permanent.json()["job_id"] is not None

        to_one_time = client.put(
            f"/api/sources/{source_id}",
            json={"lifecycle": "one_time"},
        )
        assert to_one_time.status_code == 200
        assert to_one_time.json()["lifecycle"] == "one_time"


@pytest.mark.parametrize(
    ("status_code", "payload"),
    [
        (429, {"error": {"code": 429, "message": "Rate limit exceeded"}}),
        (
            403,
            {
                "error": {
                    "code": 403,
                    "message": "Quota exceeded for quota metric",
                    "errors": [{"reason": "rateLimitExceeded"}],
                }
            },
        ),
        (503, {"error": {"code": 503, "message": "Service temporarily unavailable"}}),
    ],
)
@respx.mock
def test_sparse_candidates_and_reads_hitting_transient_catalog_errors_retry_after_24_hours(
    database, status_code, payload
):
    t0 = datetime(2026, 4, 15, 12, 0, 0, tzinfo=timezone.utc)
    with transaction() as con:
        con.execute("DELETE FROM candidates")
        con.execute("DELETE FROM reads")
        source_id = con.execute(
            "INSERT INTO sources(name,url,enabled) VALUES('Catalog Test','https://example.com/catalog',1)"
        ).lastrowid
        candidate_id = con.execute(
            """INSERT INTO candidates(
                title,author,description,cover_url,source_url,source_id,
                release_date,date_kind,genres,isbn13,status,normalized_key
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "Sparse Book",
                "Test Author",
                "",
                "https://covers.openlibrary.org/b/id/12345-L.jpg",
                "https://example.com/book",
                source_id,
                "2026-05-01",
                "day",
                '["Science fiction"]',
                "9780441172719",
                "recommended",
                "sparse book test author",
            ),
        ).lastrowid
        con.execute(
            """UPDATE candidate_quality
            SET quality_status='accepted',
                metadata_version=?,
                metadata_checked_at=NULL
            WHERE candidate_id=?""",
            (METADATA_NORMALIZATION_VERSION, candidate_id),
        )
        read_id = con.execute(
            "INSERT INTO reads(title,author,isbn,rating,source) VALUES(?,?,?,?,?)",
            ("Sparse Book", "Test Author", "9780441172719", 5, "manual"),
        ).lastrowid

    respx.get(OPEN_LIBRARY_SEARCH).mock(return_value=httpx.Response(200, json={"docs": []}))
    google_route = respx.get(GOOGLE_BOOKS_SEARCH).mock(
        return_value=httpx.Response(status_code, json=payload)
    )

    asyncio.run(refresh_missing_candidate_metadata(now=t0))
    quality = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (candidate_id,))
    assert quality["metadata_retry_status"] == "google_books_transient"
    assert quality["metadata_checked_at"] is not None

    # Candidate is not due after 23 hours, due after 25 hours (instead of 30 days)
    assert candidate_metadata_refresh_remaining(now=t0 + timedelta(hours=23)) == 0
    assert candidate_metadata_refresh_remaining(now=t0 + timedelta(hours=25)) == 1

    # Read metadata also treats 429 / quota-403 / 503 as transient (retries after 24h)
    asyncio.run(refresh_read_metadata())
    now_utc = datetime.now(timezone.utc)
    with transaction() as con:
        con.execute(
            "UPDATE read_metadata SET metadata_checked_at=? WHERE read_id=?",
            ((now_utc - timedelta(hours=23)).isoformat(), read_id),
        )
    assert read_metadata_refresh_remaining() == 0
    with transaction() as con:
        con.execute(
            "UPDATE read_metadata SET metadata_checked_at=? WHERE read_id=?",
            ((now_utc - timedelta(hours=25)).isoformat(), read_id),
        )
    assert read_metadata_refresh_remaining() == 1

    google_route.mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "gb-sparse-1",
                        "volumeInfo": {
                            "title": "Sparse Book",
                            "authors": ["Test Author"],
                            "description": "Recovered synopsis after 24-hour retry.",
                            "industryIdentifiers": [
                                {"type": "ISBN_13", "identifier": "9780441172719"}
                            ],
                        },
                    }
                ]
            },
        )
    )
    updated_count = asyncio.run(
        refresh_missing_candidate_metadata(now=t0 + timedelta(hours=25))
    )
    assert updated_count >= 1
    refreshed_candidate = row("SELECT description FROM candidates WHERE id=?", (candidate_id,))
    refreshed_quality = row(
        "SELECT metadata_retry_status FROM candidate_quality WHERE candidate_id=?",
        (candidate_id,),
    )
    assert refreshed_candidate["description"] == "Recovered synopsis after 24-hour retry."
    assert refreshed_quality["metadata_retry_status"] == ""


@pytest.mark.parametrize(
    ("status_code", "payload", "expected_retry_status"),
    [
        (400, {"error": {"code": 400, "message": "API key not valid"}}, "google_books_error"),
        (401, {"error": {"code": 401, "message": "Invalid Credentials"}}, "google_books_error"),
        (
            403,
            {
                "error": {
                    "code": 403,
                    "message": "The caller does not have permission",
                    "errors": [{"reason": "forbidden"}],
                }
            },
            "google_books_error",
        ),
        (404, {"error": {"code": 404, "message": "Volume not found"}}, ""),
    ],
)
@respx.mock
def test_permanent_and_credential_catalog_errors_do_not_retry_every_24_hours(
    database, status_code, payload, expected_retry_status
):
    t0 = datetime(2026, 4, 15, 12, 0, 0, tzinfo=timezone.utc)
    with transaction() as con:
        con.execute("DELETE FROM candidates")
        con.execute("DELETE FROM reads")
        source_id = con.execute(
            "INSERT INTO sources(name,url,enabled) VALUES('Catalog Test','https://example.com/catalog',1)"
        ).lastrowid
        candidate_id = con.execute(
            """INSERT INTO candidates(
                title,author,description,cover_url,source_url,source_id,
                release_date,date_kind,genres,isbn13,status,normalized_key
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "Permanent Miss Book",
                "Test Author",
                "",
                "https://covers.openlibrary.org/b/id/12345-L.jpg",
                "https://example.com/book",
                source_id,
                "2026-05-01",
                "day",
                '["Science fiction"]',
                "9780441172719",
                "recommended",
                "permanent miss book test author",
            ),
        ).lastrowid
        con.execute(
            """UPDATE candidate_quality
            SET quality_status='accepted',
                metadata_version=?,
                metadata_checked_at=NULL
            WHERE candidate_id=?""",
            (METADATA_NORMALIZATION_VERSION, candidate_id),
        )
        read_id = con.execute(
            "INSERT INTO reads(title,author,isbn,rating,source) VALUES(?,?,?,?,?)",
            ("Permanent Miss Book", "Test Author", "9780441172719", 5, "manual"),
        ).lastrowid

    respx.get(OPEN_LIBRARY_SEARCH).mock(return_value=httpx.Response(200, json={"docs": []}))
    respx.get(GOOGLE_BOOKS_SEARCH).mock(return_value=httpx.Response(status_code, json=payload))

    asyncio.run(refresh_missing_candidate_metadata(now=t0))
    quality = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (candidate_id,))
    assert quality["metadata_retry_status"] == expected_retry_status
    assert quality["metadata_checked_at"] is not None

    # Permanent 404s and 400/401/403 credential errors must NOT spin every 24h;
    # they wait the full 30-day cooldown unless unlocked by saving a new Google key.
    assert candidate_metadata_refresh_remaining(now=t0 + timedelta(hours=25)) == 0
    assert candidate_metadata_refresh_remaining(now=t0 + timedelta(days=31)) == 1

    asyncio.run(refresh_read_metadata())
    now_utc = datetime.now(timezone.utc)
    with transaction() as con:
        con.execute(
            "UPDATE read_metadata SET metadata_checked_at=? WHERE read_id=?",
            ((now_utc - timedelta(hours=25)).isoformat(), read_id),
        )
    assert read_metadata_refresh_remaining() == 0
    with transaction() as con:
        con.execute(
            "UPDATE read_metadata SET metadata_checked_at=? WHERE read_id=?",
            ((now_utc - timedelta(days=31)).isoformat(), read_id),
        )
    assert read_metadata_refresh_remaining() == 1


@respx.mock
def test_google_books_key_validation_scopes_lockout_clearing_and_defers_transient_failures(database):
    locked_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with transaction() as con:
        con.execute(
            "INSERT INTO settings(key,value,secret) VALUES('association_google_books_api_key',?,1) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,secret=1",
            (seal("existing-good-key"),),
        )
        google_candidate_id = con.execute(
            """INSERT INTO candidates(
                title,author,description,cover_url,source_url,status,normalized_key
            ) VALUES(?,?,?,?,?,?,?)""",
            (
                "Google Locked Candidate",
                "Author Name",
                "",
                "https://placehold.co/640x960",
                "https://example.com/google-locked",
                "recommended",
                "google locked candidate author name",
            ),
        ).lastrowid
        con.execute(
            """UPDATE candidate_quality
            SET quality_status='accepted',
                metadata_version=?,
                metadata_checked_at=?,
                metadata_retry_status='google_books_error'
            WHERE candidate_id=?""",
            (METADATA_NORMALIZATION_VERSION, locked_at, google_candidate_id),
        )
        openlibrary_candidate_id = con.execute(
            """INSERT INTO candidates(
                title,author,description,cover_url,source_url,status,normalized_key
            ) VALUES(?,?,?,?,?,?,?)""",
            (
                "OpenLibrary Transient Candidate",
                "Author OL",
                "",
                "https://placehold.co/640x960",
                "https://example.com/ol-locked",
                "recommended",
                "openlibrary transient candidate author ol",
            ),
        ).lastrowid
        con.execute(
            """UPDATE candidate_quality
            SET quality_status='accepted',
                metadata_version=?,
                metadata_checked_at=?,
                metadata_retry_status='transient'
            WHERE candidate_id=?""",
            (METADATA_NORMALIZATION_VERSION, locked_at, openlibrary_candidate_id),
        )
        rejected_google_id = con.execute(
            """INSERT INTO candidates(
                title,author,description,cover_url,source_url,status,normalized_key
            ) VALUES(?,?,?,?,?,?,?)""",
            (
                "Rejected Candidate",
                "Author Rejected",
                "",
                "",
                "https://example.com/rejected",
                "rejected",
                "rejected candidate author rejected",
            ),
        ).lastrowid
        con.execute(
            """UPDATE candidate_quality
            SET quality_status='quarantine',
                metadata_checked_at=?,
                metadata_retry_status='google_books_transient',
                flags_json='["catalog_provider_unavailable"]'
            WHERE candidate_id=?""",
            (locked_at, rejected_google_id),
        )
        google_quarantined_id = con.execute(
            """INSERT INTO candidates(
                title,author,description,cover_url,source_url,status,normalized_key
            ) VALUES(?,?,?,?,?,?,?)""",
            (
                "Google Throttled Candidate",
                "Author Two",
                "",
                "",
                "https://example.com/google-throttled",
                "new",
                "google throttled candidate author two",
            ),
        ).lastrowid
        con.execute(
            """UPDATE candidate_quality
            SET quality_status='quarantine',
                metadata_retry_status='google_books_transient',
                flags_json='["catalog_provider_unavailable","catalog_provider_rate_limited"]'
            WHERE candidate_id=?""",
            (google_quarantined_id,),
        )
        other_quarantined_id = con.execute(
            """INSERT INTO candidates(
                title,author,description,cover_url,source_url,status,normalized_key
            ) VALUES(?,?,?,?,?,?,?)""",
            (
                "OpenLibrary Throttled Candidate",
                "Author Other",
                "",
                "",
                "https://example.com/other-throttled",
                "new",
                "openlibrary throttled candidate author other",
            ),
        ).lastrowid
        con.execute(
            """UPDATE candidate_quality
            SET quality_status='quarantine',
                metadata_retry_status='',
                flags_json='["catalog_provider_unavailable"]'
            WHERE candidate_id=?""",
            (other_quarantined_id,),
        )
        google_read_id = con.execute(
            "INSERT INTO reads(title,author,isbn,rating,source) VALUES(?,?,?,?,?)",
            ("Google Read Book", "Author Three", "9780441172719", 5, "manual"),
        ).lastrowid
        con.execute(
            """INSERT INTO read_metadata(
                read_id,verified_work_id,identity_hash,description,genres,cover_url,metadata_provenance,metadata_checked_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                google_read_id,
                "",
                "hash-google",
                "",
                "[]",
                "",
                json.dumps(
                    {
                        "last_attempt": {
                            "attempt_status": "no_verified_match",
                            "fetch_trace": [
                                {"provider": "openlibrary", "status": "empty"},
                                {"provider": "google_books", "status": "credential_error", "http_status": 400},
                            ],
                        }
                    }
                ),
                locked_at,
            ),
        )
        other_read_id = con.execute(
            "INSERT INTO reads(title,author,isbn,rating,source) VALUES(?,?,?,?,?)",
            ("OpenLibrary Read Book", "Author Four", "9780261103573", 4, "manual"),
        ).lastrowid
        con.execute(
            """INSERT INTO read_metadata(
                read_id,verified_work_id,identity_hash,description,genres,cover_url,metadata_provenance,metadata_checked_at
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                other_read_id,
                "",
                "hash-ol",
                "",
                "[]",
                "",
                json.dumps(
                    {
                        "last_attempt": {
                            "attempt_status": "provider_unavailable",
                            "fetch_trace": [
                                {"provider": "openlibrary", "status": "server_error", "http_status": 503}
                            ],
                        }
                    }
                ),
                locked_at,
            ),
        )

    probe_route = respx.get(GOOGLE_BOOKS_SEARCH).mock(
        return_value=httpx.Response(
            401,
            json={
                "error": {
                    "code": 401,
                    "message": "Invalid Credentials for invalid-secret-key",
                }
            },
        )
    )

    with TestClient(app, headers=_auth_headers()) as client:
        rejected = client.put(
            "/api/settings/google-books",
            json={"api_key": "invalid-secret-key", "clear": False},
        )
        assert rejected.status_code == 400
        assert "Google Books rejected the API key" in rejected.json()["detail"]
        assert "invalid-secret-key" not in rejected.text
        assert (
            row(
                "SELECT metadata_checked_at FROM candidate_quality WHERE candidate_id=?",
                (google_candidate_id,),
            )["metadata_checked_at"]
            == locked_at
        )

        # Transient upstream network or 5xx failure during key save does NOT block saving the key.
        probe_route.mock(
            side_effect=httpx.ConnectTimeout("Timed out reaching Google Books with transient-valid-key")
        )
        deferred = client.put(
            "/api/settings/google-books",
            json={"api_key": "transient-valid-key", "clear": False},
        )
        assert deferred.status_code == 200
        deferred_body = deferred.json()
        assert deferred_body["saved"] is True
        assert deferred_body["google_books_api_key_set"] is True
        assert deferred_body["validation_status"] == "deferred"
        assert "Could not reach Google Books right now" in deferred_body["validation_warning"]
        assert "transient-valid-key" not in deferred.text

        probe_route.mock(
            return_value=httpx.Response(
                503,
                json={"error": {"code": 503, "message": "Backend unavailable for transient-503-key"}},
            )
        )
        deferred_503 = client.put(
            "/api/settings/google-books",
            json={"api_key": "transient-503-key", "clear": False},
        )
        assert deferred_503.status_code == 200
        assert deferred_503.json()["validation_status"] == "deferred"
        assert "HTTP 503 during live verification" in deferred_503.json()["validation_warning"]
        assert "transient-503-key" not in deferred_503.text

        # Verified save succeeds with verified status.
        probe_route.mock(
            return_value=httpx.Response(
                200,
                json={"kind": "books#volumes", "totalItems": 1, "items": [{"id": "dune"}]},
            )
        )
        accepted = client.put(
            "/api/settings/google-books",
            json={"api_key": "verified-google-key", "clear": False},
        )
        assert accepted.status_code == 200
        body = accepted.json()
        assert body["saved"] is True
        assert body["google_books_api_key_set"] is True
        assert body["validation_status"] == "verified"
        assert "validation_warning" not in body
        assert body["job_ids"]["metadata"] is not None
        assert body["job_ids"]["read_metadata"] is not None

    # Google-locked rows were cleared/requeued:
    cleared_quality = row(
        "SELECT metadata_checked_at,metadata_retry_status FROM candidate_quality WHERE candidate_id=?",
        (google_candidate_id,),
    )
    assert cleared_quality == {"metadata_checked_at": None, "metadata_retry_status": ""}
    assert (
        row("SELECT quality_status FROM candidate_quality WHERE candidate_id=?", (google_quarantined_id,))[
            "quality_status"
        ]
        == "pending"
    )
    assert (
        row("SELECT metadata_checked_at FROM read_metadata WHERE read_id=?", (google_read_id,))[
            "metadata_checked_at"
        ]
        is None
    )

    # Non-Google rows and rejected candidates were NOT touched:
    assert row(
        "SELECT metadata_checked_at,metadata_retry_status FROM candidate_quality WHERE candidate_id=?",
        (openlibrary_candidate_id,),
    ) == {"metadata_checked_at": locked_at, "metadata_retry_status": "transient"}
    assert row(
        "SELECT quality_status,metadata_checked_at FROM candidate_quality WHERE candidate_id=?",
        (rejected_google_id,),
    ) == {"quality_status": "quarantine", "metadata_checked_at": locked_at}
    assert (
        row("SELECT quality_status FROM candidate_quality WHERE candidate_id=?", (other_quarantined_id,))[
            "quality_status"
        ]
        == "quarantine"
    )
    assert (
        row("SELECT metadata_checked_at FROM read_metadata WHERE read_id=?", (other_read_id,))[
            "metadata_checked_at"
        ]
        == locked_at
    )

    queued_kinds = {
        job["kind"]
        for job in rows("SELECT kind FROM jobs WHERE status='queued'")
    }
    assert {"metadata", "read_metadata"} <= queued_kinds
