import asyncio
from datetime import datetime, timedelta, timezone
import json
import socket
import sqlite3

from fastapi.testclient import TestClient
import httpx
import pytest
import respx

from pathlib import Path
from auth_helpers import authenticated_headers

from afterword_engine import profiles
from afterword_engine.config import settings
from afterword_engine.covers import GOOGLE_BOOKS_SEARCH, METADATA_NORMALIZATION_VERSION, OPEN_LIBRARY_SEARCH
from afterword_engine.database import MIGRATIONS, initialize, row, rows, transaction
from afterword_engine.ingestion import (
    candidate_metadata_refresh_remaining,
    fetch_bytes,
    refresh_missing_candidate_metadata,
    scan_source,
)
from afterword_engine.main import (
    SOURCE_SYNC_ERROR_RETRY_SECONDS,
    app,
    handle_job,
    source_sync_due_sources,
)
from afterword_engine.secrets import seal
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
    initialize(seed_demo=False)
    assert row("SELECT 1 FROM schema_migrations WHERE version=27") is not None
    upgraded = row("SELECT last_scanned_at, last_attempted_at FROM sources WHERE url=?", ("https://example.test/feed.xml",))
    assert upgraded == {
        "last_scanned_at": "2026-04-01T12:00:00Z",
        "last_attempted_at": "2026-04-01T12:00:00Z",
    }


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
    assert "books.example.test" in str(exc_info.value)
    assert "93.184.216.34" not in str(exc_info.value)

    stored = row("SELECT * FROM sources WHERE id=?", (source_id,))
    assert stored["last_scanned_at"] == good_scan
    assert stored["last_attempted_at"] != good_scan
    assert "books.example.test/feed.xml?[redacted]" in stored["last_status"]
    assert "93.184.216.34" not in stored["last_status"]
    assert "super-secret-token" not in stored["last_status"]

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
        side_effect=httpx.ConnectTimeout("Timed out connecting to 93.184.216.34:443")
    )
    with pytest.raises(httpx.ConnectTimeout) as exc_info:
        asyncio.run(fetch_bytes("https://books.example.test/timeout"))

    assert "books.example.test" in str(exc_info.value)
    assert "93.184.216.34" not in str(exc_info.value)
    assert exc_info.value.request.url.host == "books.example.test"


def test_empty_and_zero_match_filter_scans_retain_candidates_and_preserve_good_scan(database, monkeypatch):
    good_scan = "2026-04-08T09:15:00+00:00"
    with transaction() as con:
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

    async def non_empty_romance_feed(_url):
        return "application/json", [
            {"title": "Unmatched Book", "author": "Author Three", "genres": ["Romance"]}
        ]

    async def no_enrichment(items):
        return items

    monkeypatch.setattr("afterword_engine.ingestion._fetch_and_parse_source", non_empty_romance_feed)
    monkeypatch.setattr("afterword_engine.ingestion.enrich_book_metadata", no_enrichment)

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

    async def empty_upstream_feed(_url):
        return "application/json", []

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

        # Editing a starter source URL does not re-seed a duplicate default row on restart
        starter = row("SELECT id, name FROM sources WHERE kind='web' ORDER BY id ASC LIMIT 1")
        starter_edit = client.put(
            f"/api/sources/{starter['id']}",
            json={"url": "https://books.example.test/starter-custom"},
        )
        assert starter_edit.status_code == 200
        initialize()
        assert (
            len(rows("SELECT id FROM sources WHERE name=? AND kind='web'", (starter["name"],)))
            == 1
        )


@pytest.mark.parametrize("status_code", [400, 403, 429, 503])
@respx.mock
def test_sparse_candidates_hitting_catalog_errors_retry_after_24_hours(database, status_code):
    t0 = datetime(2026, 4, 15, 12, 0, 0, tzinfo=timezone.utc)
    with transaction() as con:
        con.execute("DELETE FROM candidates")
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

    respx.get(OPEN_LIBRARY_SEARCH).mock(return_value=httpx.Response(200, json={"docs": []}))
    google_route = respx.get(GOOGLE_BOOKS_SEARCH).mock(
        return_value=httpx.Response(
            status_code,
            json={"error": {"code": status_code, "message": "Provider rejected request"}},
        )
    )

    asyncio.run(refresh_missing_candidate_metadata(now=t0))
    quality = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (candidate_id,))
    assert quality["metadata_retry_status"] == "transient"
    assert quality["metadata_checked_at"] is not None

    # Not due after 23 hours, due after 25 hours (instead of 30 days!)
    assert candidate_metadata_refresh_remaining(now=t0 + timedelta(hours=23)) == 0
    assert candidate_metadata_refresh_remaining(now=t0 + timedelta(hours=25)) == 1

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
    refreshed_quality = row("SELECT metadata_retry_status FROM candidate_quality WHERE candidate_id=?", (candidate_id,))
    assert refreshed_candidate["description"] == "Recovered synopsis after 24-hour retry."
    assert refreshed_quality["metadata_retry_status"] == ""


@respx.mock
def test_google_books_key_validation_clears_lockouts_and_queues_enrichment(database):
    locked_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    with transaction() as con:
        con.execute(
            "INSERT INTO settings(key,value,secret) VALUES('association_google_books_api_key',?,1) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value,secret=1",
            (seal("existing-good-key"),),
        )
        candidate_id = con.execute(
            """INSERT INTO candidates(
                title,author,description,cover_url,source_url,status,normalized_key
            ) VALUES(?,?,?,?,?,?,?)""",
            (
                "Locked Candidate",
                "Author Name",
                "",
                "https://placehold.co/640x960",
                "https://example.com/locked",
                "recommended",
                "locked candidate author name",
            ),
        ).lastrowid
        con.execute(
            """UPDATE candidate_quality
            SET quality_status='accepted',
                metadata_version=?,
                metadata_checked_at=?,
                metadata_retry_status='transient'
            WHERE candidate_id=?""",
            (METADATA_NORMALIZATION_VERSION, locked_at, candidate_id),
        )
        quarantined_id = con.execute(
            """INSERT INTO candidates(
                title,author,description,cover_url,source_url,status,normalized_key
            ) VALUES(?,?,?,?,?,?,?)""",
            (
                "Throttled Candidate",
                "Author Two",
                "",
                "",
                "https://example.com/throttled",
                "new",
                "throttled candidate author two",
            ),
        ).lastrowid
        con.execute(
            """UPDATE candidate_quality
            SET quality_status='quarantine',
                flags_json='["catalog_provider_unavailable","catalog_provider_rate_limited"]'
            WHERE candidate_id=?""",
            (quarantined_id,),
        )
        read_id = con.execute(
            "INSERT INTO reads(title,author,isbn,rating,source) VALUES(?,?,?,?,?)",
            ("Read Book", "Author Three", "9780441172719", 5, "manual"),
        ).lastrowid
        con.execute(
            """INSERT INTO read_metadata(
                read_id,verified_work_id,identity_hash,description,genres,cover_url,metadata_checked_at
            ) VALUES(?,?,?,?,?,?,?)""",
            (read_id, "", "hash", "", "[]", "", locked_at),
        )

    probe_route = respx.get(GOOGLE_BOOKS_SEARCH).mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "code": 400,
                    "message": "API key not valid: invalid-secret-key",
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
        # Previous valid key and lockouts remain untouched on failed validation
        assert (
            row("SELECT metadata_checked_at FROM candidate_quality WHERE candidate_id=?", (candidate_id,))[
                "metadata_checked_at"
            ]
            == locked_at
        )

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
        assert body["job_ids"]["metadata"] is not None
        assert body["job_ids"]["read_metadata"] is not None

    cleared_quality = row(
        "SELECT metadata_checked_at,metadata_retry_status FROM candidate_quality WHERE candidate_id=?",
        (candidate_id,),
    )
    assert cleared_quality["metadata_checked_at"] is None
    assert cleared_quality["metadata_retry_status"] == ""
    assert (
        row("SELECT quality_status FROM candidate_quality WHERE candidate_id=?", (quarantined_id,))[
            "quality_status"
        ]
        == "pending"
    )
    assert (
        row("SELECT metadata_checked_at FROM read_metadata WHERE read_id=?", (read_id,))[
            "metadata_checked_at"
        ]
        is None
    )
    queued_kinds = {
        job["kind"]
        for job in rows("SELECT kind FROM jobs WHERE status='queued'")
    }
    assert {"metadata", "read_metadata"} <= queued_kinds
