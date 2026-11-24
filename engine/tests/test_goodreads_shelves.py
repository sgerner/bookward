import asyncio
from pathlib import Path
import socket
import sqlite3
import sys

from fastapi.testclient import TestClient
import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from auth_helpers import authenticated_headers
from afterword_engine import database as db_module
from afterword_engine import ingestion
from afterword_engine.config import settings
from afterword_engine.database import (
    MIGRATIONS,
    _execute_migration_script,
    initialize,
    normalize_key,
)
from afterword_engine.ingestion import (
    goodreads_shelf_rss_url,
    import_goodreads_csv,
    import_goodreads_rss,
    import_goodreads_shelves,
    validate_goodreads_rss_url,
)
from afterword_engine.main import app
from afterword_engine.scoring import score_all
from afterword_engine.secrets import seal
from afterword_engine.tenancy import profile_scope


def test_goodreads_shelf_rss_url_rewrites_shelf_param() -> None:
    read_url = "https://www.goodreads.com/review/list_rss/12345?key=secret&shelf=read"
    assert (
        goodreads_shelf_rss_url(read_url, "to-read")
        == "https://www.goodreads.com/review/list_rss/12345?key=secret&shelf=to-read"
    )
    assert (
        goodreads_shelf_rss_url(read_url, "currently-reading")
        == "https://www.goodreads.com/review/list_rss/12345?key=secret&shelf=currently-reading"
    )


def test_goodreads_read_feed_uses_only_the_read_shelf() -> None:
    url = "https://www.goodreads.com/review/list_rss/12345?key=secret"
    assert validate_goodreads_rss_url(url) == f"{url}&shelf=read"
    with pytest.raises(ValueError, match="read shelf"):
        validate_goodreads_rss_url(f"{url}&shelf=read&shelf=to-read")


def test_import_goodreads_shelves_stores_separately_from_reads(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "shelves.db"
    monkeypatch.setattr(settings, "db", str(db_path))
    initialize(seed_demo=False)

    to_read_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>Project Hail Mary</title>
        <author_name>Andy Weir</author_name>
        <isbn13>9780593135204</isbn13>
      </item>
    </channel></rss>"""
    reading_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>Piranesi</title>
        <author_name>Susanna Clarke</author_name>
      </item>
    </channel></rss>"""

    async def fake_fetch_bytes(url: str, **kwargs):
        if "shelf=to-read" in url:
            return to_read_xml, "application/xml"
        if "shelf=currently-reading" in url:
            return reading_xml, "application/xml"
        raise AssertionError(f"Unexpected url: {url}")

    monkeypatch.setattr("afterword_engine.ingestion.fetch_bytes", fake_fetch_bytes)

    counts = asyncio.run(
        import_goodreads_shelves("https://www.goodreads.com/review/list_rss/12345?key=secret&shelf=read")
    )
    assert counts == {"to-read": 1, "currently-reading": 1}

    con = sqlite3.connect(db_path)
    try:
        reads_count = con.execute("SELECT COUNT(*) FROM reads").fetchone()[0]
        shelf_rows = con.execute(
            "SELECT shelf, source, title, author FROM goodreads_shelf_items ORDER BY shelf"
        ).fetchall()
    finally:
        con.close()

    assert reads_count == 0
    assert shelf_rows == [
        ("currently-reading", "goodreads_rss", "Piranesi", "Susanna Clarke"),
        ("to-read", "goodreads_rss", "Project Hail Mary", "Andy Weir"),
    ]


def test_score_all_hides_currently_reading_and_boosts_to_read(tmp_path: Path, monkeypatch) -> None:
    from afterword_engine.scoring import score_all

    db_path = tmp_path / "score_shelves.db"
    monkeypatch.setattr(settings, "db", str(db_path))
    initialize(seed_demo=False)

    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT OR IGNORE INTO sources(id, name, url, kind, enabled) "
            "VALUES(999, 'List', 'https://example.com', 'rss', 1)"
        )
        con.execute(
            "INSERT INTO reads(title, author, rating, source) "
            "VALUES('The Martian', 'Andy Weir', 5, 'goodreads_rss')"
        )
        con.execute(
            "INSERT INTO goodreads_shelf_items(shelf, title, author) "
            "VALUES('to-read', 'Project Hail Mary', 'Andy Weir')"
        )
        con.execute(
            "INSERT INTO goodreads_shelf_items(shelf, title, author) "
            "VALUES('currently-reading', 'Piranesi', 'Susanna Clarke')"
        )
        for cid, title, author in [
            (10, "Project Hail Mary", "Andy Weir"),
            (11, "Piranesi", "Susanna Clarke"),
        ]:
            con.execute(
                "INSERT INTO candidates(id, source_id, title, author, status, normalized_key) "
                "VALUES(?, 999, ?, ?, 'new', ?)",
                (cid, title, author, f"{title.lower()}|{author.lower()}"),
            )
            con.execute(
                "UPDATE candidate_quality SET quality_status='accepted', metadata_confidence=1.0 "
                "WHERE candidate_id=?",
                (cid,),
            )
        con.commit()
    finally:
        con.close()

    asyncio.run(score_all())

    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT id, status, explanation FROM candidates WHERE id IN (10, 11) ORDER BY id"
        ).fetchall()
    finally:
        con.close()

    assert rows[0][1] == "recommended"
    assert "On your Goodreads to-read shelf" in rows[0][2]
    assert rows[1][1] == "hidden"


def test_import_goodreads_csv_populates_and_clears_shelf_items(tmp_path: Path, monkeypatch) -> None:
    from afterword_engine.ingestion import import_goodreads_csv

    db_path = tmp_path / "csv_shelves.db"
    monkeypatch.setattr(settings, "db", str(db_path))
    initialize(seed_demo=False)

    csv_data = (
        "Title,Author,My Rating,Date Read,Date Added,Exclusive Shelf,ISBN13\n"
        "Project Hail Mary,Andy Weir,0,,2026/01/10,to-read,=\"9780593135204\"\n"
        "Piranesi,Susanna Clarke,0,,2026/02/01,currently-reading,=\"9781635575637\"\n"
        "The Martian,Andy Weir,5,2025/05/01,2025/04/01,read,=\"9780804139021\"\n"
    ).encode("utf-8")

    count = import_goodreads_csv(csv_data)
    assert count == 1

    con = sqlite3.connect(db_path)
    try:
        shelves = con.execute(
            "SELECT shelf, title, author, isbn FROM goodreads_shelf_items ORDER BY shelf"
        ).fetchall()
    finally:
        con.close()

    assert shelves == [
        ("currently-reading", "Piranesi", "Susanna Clarke", "9781635575637"),
        ("to-read", "Project Hail Mary", "Andy Weir", "9780593135204"),
    ]


def test_goodreads_imports_delete_matching_items_from_both_shelves_using_title_author_index(
    tmp_path: Path, monkeypatch
) -> None:
    from afterword_engine import ingestion
    from afterword_engine.ingestion import import_goodreads_csv, import_goodreads_rss

    db_path = tmp_path / "shelf_delete_index.db"
    monkeypatch.setattr(settings, "db", str(db_path))
    initialize(seed_demo=False)

    con = sqlite3.connect(db_path)
    try:
        plan_details = [
            row[3]
            for row in con.execute(
                "EXPLAIN QUERY PLAN DELETE FROM goodreads_shelf_items WHERE title=? AND author=?",
                ("Project Hail Mary", "Andy Weir"),
            ).fetchall()
        ]
        shelf_plan_details = [
            row[3]
            for row in con.execute(
                "EXPLAIN QUERY PLAN DELETE FROM goodreads_shelf_items WHERE shelf=?",
                ("to-read",),
            ).fetchall()
        ]
        con.executemany(
            "INSERT INTO goodreads_shelf_items(shelf, source, title, author, isbn) VALUES(?, ?, ?, ?, ?)",
            [
                ("to-read", "goodreads_csv", "Project Hail Mary", "Andy Weir", "9780593135204"),
                ("currently-reading", "goodreads_rss", "Project Hail Mary", "Andy Weir", "9780593135204"),
                ("to-read", "goodreads_csv", "project hail mary", "Andy Weir", None),
                ("to-read", "goodreads_csv", "Piranesi", "Susanna Clarke", "9781635575637"),
                ("currently-reading", "goodreads_rss", "Piranesi", "Susanna Clarke", "9781635575637"),
                ("to-read", "goodreads_csv", "Dune", "Frank Herbert", "9780441172719"),
            ],
        )
        con.commit()
    finally:
        con.close()

    assert any(
        "SEARCH goodreads_shelf_items USING INDEX idx_goodreads_shelf_items_title_author" in detail
        for detail in plan_details
    )
    assert any(
        "SEARCH goodreads_shelf_items USING INDEX idx_goodreads_shelf_items_shelf" in detail
        for detail in shelf_plan_details
    )

    csv_promotion = (
        "Title,Author,My Rating,Date Read,Date Added,Exclusive Shelf,ISBN13\n"
        "Project Hail Mary,Andy Weir,5,2026/03/10,2026/01/10,read,=\"9780593135204\"\n"
    ).encode("utf-8")
    assert import_goodreads_csv(csv_promotion) == 1

    rss_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>Piranesi</title>
        <author_name>Susanna Clarke</author_name>
        <user_rating>5</user_rating>
        <user_read_at>2026/03/15</user_read_at>
        <isbn13>9781635575637</isbn13>
      </item>
    </channel></rss>"""

    async def fake_fetch_bytes(url: str, **kwargs):
        return rss_xml, "application/rss+xml"

    monkeypatch.setattr(ingestion, "fetch_bytes", fake_fetch_bytes)
    assert (
        asyncio.run(
            import_goodreads_rss("https://www.goodreads.com/review/list_rss/12345?key=secret&shelf=read")
        )
        == 1
    )

    con = sqlite3.connect(db_path)
    try:
        remaining = con.execute(
            "SELECT shelf, title, author FROM goodreads_shelf_items ORDER BY title, shelf"
        ).fetchall()
    finally:
        con.close()

    # Exact BINARY collation preserves differently cased non-matching shelf rows
    # while deleting both to-read and currently-reading rows for promoted books.
    assert remaining == [
        ("to-read", "Dune", "Frank Herbert"),
        ("to-read", "project hail mary", "Andy Weir"),
    ]


def test_import_goodreads_shelves_shares_client_fetches_concurrently_and_writes_in_order(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "concurrent_shelves.db"
    monkeypatch.setattr(settings, "db", str(db_path))
    initialize(seed_demo=False)

    to_read_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>Project Hail Mary</title>
        <author_name>Andy Weir</author_name>
      </item>
    </channel></rss>"""
    reading_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>Piranesi</title>
        <author_name>Susanna Clarke</author_name>
      </item>
    </channel></rss>"""

    created_clients: list[httpx.AsyncClient] = []
    seen_clients: list[httpx.AsyncClient | None] = []
    in_flight = 0
    max_in_flight = 0
    original_source_client = ingestion._source_client

    def tracking_source_client() -> httpx.AsyncClient:
        client = original_source_client()
        created_clients.append(client)
        return client

    async def fake_fetch_bytes(url: str, allow_goodreads_http: bool = False, client=None):
        nonlocal in_flight, max_in_flight
        seen_clients.append(client)
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            if "shelf=to-read" in url:
                # Finish after currently-reading so rowid order verifies deterministic SQLite writes.
                await asyncio.sleep(0.06)
                return to_read_xml, "application/xml"
            if "shelf=currently-reading" in url:
                await asyncio.sleep(0.02)
                return reading_xml, "application/xml"
            raise AssertionError(f"Unexpected url: {url}")
        finally:
            in_flight -= 1

    monkeypatch.setattr(ingestion, "_source_client", tracking_source_client)
    monkeypatch.setattr(ingestion, "fetch_bytes", fake_fetch_bytes)

    counts = asyncio.run(
        import_goodreads_shelves("https://www.goodreads.com/review/list_rss/12345?key=secret&shelf=read")
    )
    assert counts == {"to-read": 1, "currently-reading": 1}
    assert len(created_clients) == 1
    assert seen_clients == [created_clients[0], created_clients[0]]
    assert max_in_flight == 2

    con = sqlite3.connect(db_path)
    try:
        ordered_shelves = [
            shelf
            for (shelf,) in con.execute(
                "SELECT shelf FROM goodreads_shelf_items ORDER BY id ASC"
            ).fetchall()
        ]
    finally:
        con.close()

    assert ordered_shelves == ["to-read", "currently-reading"]


def test_import_goodreads_shelves_preserves_existing_rows_on_http_404(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "private_shelves.db"
    monkeypatch.setattr(settings, "db", str(db_path))
    initialize(seed_demo=False)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
    )

    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO goodreads_shelf_items(shelf, title, author) "
            "VALUES('to-read', 'Kept To Read', 'Author One')"
        )
        con.execute(
            "INSERT INTO goodreads_shelf_items(shelf, source, title, author) "
            "VALUES('currently-reading', 'goodreads_rss', 'Old Reading', 'Author Two')"
        )
        con.commit()
    finally:
        con.close()

    reading_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>New Reading</title>
        <author_name>Author Three</author_name>
      </item>
    </channel></rss>"""

    async def handler(request: httpx.Request) -> httpx.Response:
        shelf = request.url.params.get("shelf")
        if shelf == "to-read":
            return httpx.Response(404, content=b"Not Found", headers={"content-type": "text/plain"})
        if shelf == "currently-reading":
            return httpx.Response(200, content=reading_xml, headers={"content-type": "application/rss+xml"})
        raise AssertionError(f"Unexpected shelf: {shelf}")

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

    counts = asyncio.run(
        import_goodreads_shelves("https://www.goodreads.com/review/list_rss/12345?key=secret&shelf=read")
    )
    assert counts == {"to-read": 0, "currently-reading": 1}

    con = sqlite3.connect(db_path)
    try:
        shelf_rows = con.execute(
            "SELECT shelf, title, author FROM goodreads_shelf_items ORDER BY shelf"
        ).fetchall()
    finally:
        con.close()

    assert shelf_rows == [
        ("currently-reading", "New Reading", "Author Three"),
        ("to-read", "Kept To Read", "Author One"),
    ]


def test_import_storygraph_csv_populates_shelves_skips_dnf_and_clears_completed_reads(
    tmp_path: Path, monkeypatch
) -> None:
    from afterword_engine.ingestion import import_csv

    db_path = tmp_path / "storygraph_shelves.db"
    monkeypatch.setattr(settings, "db", str(db_path))
    initialize(seed_demo=False)

    csv_data = (
        "Title,Authors,Contributors,ISBN/UID,Format,Read Status,Date Added,Last Date Read,Dates Read,Star Rating\n"
        'Project Hail Mary,Andy Weir,,9780593135204,hardcover,to-read,2026/01/10,,,\n'
        'Piranesi,"Susanna Clarke, Coauthor",,b4f901a2-7c4d-4f1b-9a2e-1234567890ab,digital,currently-reading,2026/02/01,,,\n'
        'Abandoned Tome,Some Writer,,9780306406157,paperback,did-not-finish,2026/02/10,2026/02/15,2026/02/10-2026/02/15,1.5\n'
        'The Martian,Andy Weir,,0553418025,paperback,read,2025/04/01,2025/05/01,2025/04/20-2025/05/01,4.75\n'
    ).encode("utf-8")

    result = import_csv(csv_data)
    assert result == {"imported": 1, "source": "storygraph_csv"}

    con = sqlite3.connect(db_path)
    try:
        reads_rows = con.execute(
            "SELECT title, author, rating, read_at, isbn, source FROM reads"
        ).fetchall()
        shelves = con.execute(
            "SELECT shelf, title, author, isbn FROM goodreads_shelf_items ORDER BY shelf"
        ).fetchall()
        shortlist_count = con.execute(
            "SELECT COUNT(*) FROM candidates WHERE status='saved'"
        ).fetchone()[0]
    finally:
        con.close()

    assert reads_rows == [
        ("The Martian", "Andy Weir", 4.75, "2025/05/01", "9780553418026", "storygraph_csv")
    ]
    assert shelves == [
        ("currently-reading", "Piranesi", "Susanna Clarke", None),
        ("to-read", "Project Hail Mary", "Andy Weir", "9780593135204"),
    ]
    assert shortlist_count == 0

    followup_csv = (
        "Title,Authors,ISBN/UID,Read Status,Last Date Read,Star Rating\n"
        "Project Hail Mary,Andy Weir,9780593135204,read,2026/03/01,5.0\n"
    ).encode("utf-8")
    assert import_csv(followup_csv) == {"imported": 1, "source": "storygraph_csv"}

    con = sqlite3.connect(db_path)
    try:
        remaining_shelves = con.execute(
            "SELECT shelf, title, author FROM goodreads_shelf_items"
        ).fetchall()
    finally:
        con.close()

    assert remaining_shelves == [
        ("currently-reading", "Piranesi", "Susanna Clarke"),
    ]


def test_migration_28_recovers_hidden_rows_preserving_user_feedback_dedupes_shelves_and_isolates_profiles(
    tmp_path: Path, monkeypatch
) -> None:
    root_db = tmp_path / "afterword.db"
    monkeypatch.setattr(settings, "db", str(root_db))

    profile_a_id = "a" * 32
    profile_b_id = "b" * 32
    profile_a_db = tmp_path / "profiles" / f"{profile_a_id}.sqlite3"
    profile_b_db = tmp_path / "profiles" / f"{profile_b_id}.sqlite3"
    profile_a_db.parent.mkdir(parents=True, exist_ok=True)

    # Construct a genuine schema-v26 database for profile_a with dual-shelf
    # stale rows, legacy currently-reading status='hidden' candidates, and
    # user-rejected / user-deferred candidates that must NOT be reset to 'new'.
    con = sqlite3.connect(profile_a_db)
    try:
        con.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        for version, script in MIGRATIONS:
            if version > 26:
                continue
            _execute_migration_script(con, script)
            con.execute("INSERT INTO schema_migrations(version) VALUES(?)", (version,))
        con.execute(
            "INSERT INTO sources(id, name, url, kind, enabled) VALUES(999, 'Feed', 'https://example.com/rss', 'rss', 1)"
        )
        con.execute(
            "INSERT INTO reads(title, author, rating, source) VALUES('The Martian', 'Andy Weir', 5, 'goodreads_csv')"
        )
        seeded_candidates = [
            # 50: legacy currently-reading hidden candidate -> must reset to 'new'
            (50, "Piranesi", "Susanna Clarke", "hidden", 0.0, "[]"),
            # 51: user-rejected candidate -> must stay 'rejected'
            (51, "Rejected Novel", "Author R", "rejected", 79.5, '["Not for me"]'),
            # 52: user maybe_later candidate -> must stay 'maybe_later'
            (52, "Deferred Novel", "Author D", "maybe_later", 81.0, '["Later"]'),
            # 53: hidden candidate that also has an active non-undone user reject -> must NOT reset to 'new'
            (53, "Hidden After Reject", "Author H", "hidden", 0.0, "[]"),
            # 54: hidden candidate whose earlier reject was undone -> must reset to 'new'
            (54, "Undone Reject Novel", "Author U", "hidden", 0.0, "[]"),
        ]
        for cid, title, author, status, score, explanation in seeded_candidates:
            con.execute(
                "INSERT INTO candidates(id, source_id, title, author, status, score, explanation, normalized_key) "
                "VALUES(?, 999, ?, ?, ?, ?, ?, ?)",
                (cid, title, author, status, score, explanation, normalize_key(title, author)),
            )
            con.execute(
                "UPDATE candidate_quality SET quality_status='accepted', metadata_confidence=1.0 WHERE candidate_id=?",
                (cid,),
            )
        con.execute(
            "INSERT INTO feedback(id, candidate_id, action, previous_status) VALUES(1, 51, 'reject', 'recommended')"
        )
        con.execute(
            "INSERT INTO recommendation_events(event_key, candidate_id, event_type, source) "
            "VALUES('feedback:1', 51, 'reject', 'ui')"
        )
        con.execute(
            "INSERT INTO feedback(id, candidate_id, action, previous_status) VALUES(2, 52, 'maybe_later', 'recommended')"
        )
        con.execute(
            "INSERT INTO recommendation_events(event_key, candidate_id, event_type, source) "
            "VALUES('feedback:2', 52, 'maybe_later', 'ui')"
        )
        con.execute(
            "INSERT INTO feedback(id, candidate_id, action, previous_status) VALUES(3, 53, 'reject', 'recommended')"
        )
        con.execute(
            "INSERT INTO recommendation_events(event_key, candidate_id, event_type, source) "
            "VALUES('feedback:3', 53, 'reject', 'ui')"
        )
        con.execute(
            "INSERT INTO feedback(id, candidate_id, action, previous_status, undone_at) "
            "VALUES(4, 54, 'reject', 'recommended', '2026-01-15 00:00:00')"
        )
        con.execute(
            "INSERT INTO recommendation_events(event_key, candidate_id, event_type, source) "
            "VALUES('feedback:4', 54, 'reject', 'ui')"
        )
        con.execute(
            "INSERT INTO recommendation_events(event_key, candidate_id, event_type, source) "
            "VALUES('undo-feedback:4', 54, 'restore', 'ui_undo')"
        )
        con.execute(
            "INSERT INTO goodreads_shelf_items(shelf, title, author, isbn, updated_at) "
            "VALUES('currently-reading', 'Piranesi', 'Susanna Clarke', '9781635575637', '2026-01-01 00:00:00')"
        )
        con.execute(
            "INSERT INTO goodreads_shelf_items(shelf, title, author, isbn, updated_at) "
            "VALUES('to-read', 'Piranesi', 'Susanna Clarke', NULL, '2026-02-01 00:00:00')"
        )
        con.commit()
    finally:
        con.close()

    with profile_scope(profile_b_id):
        initialize(seed_demo=False)
        con_b = sqlite3.connect(profile_b_db)
        try:
            con_b.execute(
                "INSERT INTO goodreads_shelf_items(shelf, source, title, author) "
                "VALUES('currently-reading', 'goodreads_csv', 'Isolated Book', 'Other Author')"
            )
            con_b.commit()
        finally:
            con_b.close()

    with profile_scope(profile_a_id):
        initialize(seed_demo=False)
        con_a = sqlite3.connect(profile_a_db)
        try:
            migrated_shelves = con_a.execute(
                "SELECT shelf, source, title, author, isbn FROM goodreads_shelf_items"
            ).fetchall()
            migrated_statuses = {
                cid: (status, score)
                for cid, status, score in con_a.execute(
                    "SELECT id, status, score FROM candidates WHERE id IN (50, 51, 52, 53, 54) ORDER BY id"
                ).fetchall()
            }
        finally:
            con_a.close()

        assert migrated_shelves == [
            ("to-read", "legacy", "Piranesi", "Susanna Clarke", "9781635575637")
        ]
        assert migrated_statuses[50] == ("new", 0.0)
        assert migrated_statuses[51] == ("rejected", 79.5)
        assert migrated_statuses[52] == ("maybe_later", 81.0)
        assert migrated_statuses[53] == ("hidden", 0.0)
        assert migrated_statuses[54] == ("new", 0.0)

        asyncio.run(score_all())
        # Second initialize() call verifies idempotency without re-running Migration 28.
        initialize(seed_demo=False)
        con_a = sqlite3.connect(profile_a_db)
        try:
            rescored = {
                cid: (status, score)
                for cid, status, score in con_a.execute(
                    "SELECT id, status, score FROM candidates WHERE id IN (50, 51, 52, 53, 54) ORDER BY id"
                ).fetchall()
            }
        finally:
            con_a.close()
        assert rescored[50][0] == "recommended" and rescored[50][1] > 0
        assert rescored[51] == ("rejected", 79.5)
        assert rescored[52] == ("maybe_later", 81.0)
        assert rescored[53] == ("hidden", 0.0)
        assert rescored[54][0] == "recommended" and rescored[54][1] > 0

    # Profile B remains completely isolated.
    con_b = sqlite3.connect(profile_b_db)
    try:
        b_shelves = con_b.execute(
            "SELECT shelf, source, title, author FROM goodreads_shelf_items"
        ).fetchall()
    finally:
        con_b.close()
    assert b_shelves == [("currently-reading", "goodreads_csv", "Isolated Book", "Other Author")]


def test_migration_28_preserves_csv_only_rows_with_rss_residue_and_clears_scheduled_date_on_disconnect(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "v26_csv_with_rss_residue.db"
    monkeypatch.setattr(settings, "db", str(db_path))
    rss_url = "https://www.goodreads.com/review/list_rss/12345?key=secret&shelf=read"

    # Populate a schema-v26 DB representing a CSV user whose Goodreads RSS was
    # disconnected earlier (`goodreads_rss_url=''`), leaving `goodreads_rss_last_scheduled_date`,
    # `jobs.kind='goodreads_rss'`, and `reads.source='goodreads_rss'` residue.
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        for version, script in MIGRATIONS:
            if version > 26:
                continue
            _execute_migration_script(con, script)
            con.execute("INSERT INTO schema_migrations(version) VALUES(?)", (version,))
        con.executemany(
            "INSERT INTO settings(key, value, secret) VALUES(?, ?, ?)",
            [
                ("goodreads_rss_url", "", 1),
                ("goodreads_rss_last_scheduled_date", "2026-11-20", 0),
                ("goodreads_rss_last_sync_at", "", 0),
                ("goodreads_rss_last_attempt_at", "", 0),
            ],
        )
        con.execute(
            "INSERT INTO jobs(id, kind, status) VALUES('job-rss-old', 'goodreads_rss', 'done')"
        )
        con.execute(
            "INSERT INTO reads(title, author, rating, source) "
            "VALUES('The Martian', 'Andy Weir', 5, 'goodreads_rss')"
        )
        con.execute(
            "INSERT INTO reads(title, author, rating, source) "
            "VALUES('Dune', 'Frank Herbert', 5, 'goodreads_csv')"
        )
        con.executemany(
            "INSERT INTO goodreads_shelf_items(shelf, title, author, isbn, updated_at) VALUES(?, ?, ?, ?, ?)",
            [
                ("to-read", "Project Hail Mary", "Andy Weir", "9780593135204", "2026-11-21 00:00:00"),
                ("currently-reading", "Piranesi", "Susanna Clarke", "9781635575637", "2026-11-21 00:00:00"),
            ],
        )
        con.commit()
    finally:
        con.close()

    initialize(seed_demo=False)

    con = sqlite3.connect(db_path)
    try:
        migrated = con.execute(
            "SELECT shelf, source, title, author FROM goodreads_shelf_items ORDER BY shelf"
        ).fetchall()
    finally:
        con.close()
    assert migrated == [
        ("currently-reading", "legacy", "Piranesi", "Susanna Clarke"),
        ("to-read", "legacy", "Project Hail Mary", "Andy Weir"),
    ]

    with TestClient(app, headers=authenticated_headers()) as client:
        # Connect RSS, record a scheduled date + RSS row, then disconnect RSS:
        # goodreads_rss_last_scheduled_date is cleared and 'legacy' CSV rows survive.
        connect_res = client.put("/api/settings/goodreads-rss", json={"url": rss_url})
        assert connect_res.status_code == 200
        con = sqlite3.connect(db_path)
        try:
            con.execute(
                "INSERT INTO settings(key, value, secret) VALUES('goodreads_rss_last_scheduled_date', '2026-11-24', 0) "
                "ON CONFLICT(key) DO UPDATE SET value='2026-11-24'"
            )
            con.execute(
                "INSERT INTO goodreads_shelf_items(shelf, source, title, author) "
                "VALUES('to-read', 'goodreads_rss', 'Transient RSS Item', 'Author T')"
            )
            con.commit()
        finally:
            con.close()

        disconnect_res = client.put("/api/settings/goodreads-rss", json={"url": ""})
        assert disconnect_res.status_code == 200
        assert disconnect_res.json() == {"saved": True, "configured": False}

    con = sqlite3.connect(db_path)
    try:
        scheduled_val = con.execute(
            "SELECT value FROM settings WHERE key='goodreads_rss_last_scheduled_date'"
        ).fetchone()[0]
        remaining = con.execute(
            "SELECT shelf, source, title, author FROM goodreads_shelf_items ORDER BY shelf, title"
        ).fetchall()
    finally:
        con.close()
    assert scheduled_val == ""
    assert remaining == [
        ("currently-reading", "legacy", "Piranesi", "Susanna Clarke"),
        ("to-read", "legacy", "Project Hail Mary", "Andy Weir"),
    ]


def test_migration_28_csv_plus_rss_on_v26_db_preserves_csv_across_disconnects_and_cleans_on_csv_reimport(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "v26_csv_plus_rss.db"
    monkeypatch.setattr(settings, "db", str(db_path))
    rss_url = "https://www.goodreads.com/review/list_rss/12345?key=secret&shelf=read"

    # Populate a schema-v26 DB where the user imported CSV shelves AND currently
    # has Goodreads RSS connected.
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        for version, script in MIGRATIONS:
            if version > 26:
                continue
            _execute_migration_script(con, script)
            con.execute("INSERT INTO schema_migrations(version) VALUES(?)", (version,))
        con.executemany(
            "INSERT INTO settings(key, value, secret) VALUES(?, ?, ?)",
            [
                ("goodreads_rss_url", seal(rss_url), 1),
                ("goodreads_rss_last_sync_at", "2026-11-20T02:00:00+00:00", 0),
                ("goodreads_rss_last_scheduled_date", "2026-11-20", 0),
            ],
        )
        con.executemany(
            "INSERT INTO reads(title, author, rating, source) VALUES(?, ?, ?, ?)",
            [
                ("CSV Read", "CSV Author", 4, "goodreads_csv"),
                ("RSS Read", "RSS Author", 5, "goodreads_rss"),
            ],
        )
        con.executemany(
            "INSERT INTO goodreads_shelf_items(shelf, title, author, isbn, updated_at) VALUES(?, ?, ?, ?, ?)",
            [
                ("to-read", "CSV Queue Book", "Ursula K. Le Guin", "9780441478125", "2026-11-19 00:00:00"),
                ("to-read", "Removed From CSV Later", "Old Author", None, "2026-11-19 00:00:00"),
            ],
        )
        con.commit()
    finally:
        con.close()

    initialize(seed_demo=False)

    # Post-upgrade RSS sync adds 'goodreads_rss' rows alongside neutral 'legacy' rows.
    to_read_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>RSS Queue Book</title>
        <author_name>Octavia E. Butler</author_name>
      </item>
    </channel></rss>"""
    reading_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>RSS Reading Book</title>
        <author_name>Ted Chiang</author_name>
      </item>
    </channel></rss>"""

    async def ok_fetch(url: str, **kwargs):
        if "shelf=to-read" in url:
            return to_read_xml, "application/xml"
        if "shelf=currently-reading" in url:
            return reading_xml, "application/xml"
        raise AssertionError(url)

    monkeypatch.setattr("afterword_engine.ingestion.fetch_bytes", ok_fetch)
    assert asyncio.run(import_goodreads_shelves(rss_url)) == {
        "to-read": 1,
        "currently-reading": 1,
    }

    # 1) HTTP 404 private-shelf disconnect path deletes 'goodreads_rss' on to-read
    #    without deleting pre-v28 'legacy' CSV rows.
    async def private_to_read_fetch(url: str, **kwargs):
        if "shelf=to-read" in url:
            req = httpx.Request("GET", url)
            resp = httpx.Response(404, request=req)
            raise httpx.HTTPStatusError("404 Not Found", request=req, response=resp)
        if "shelf=currently-reading" in url:
            return reading_xml, "application/xml"
        raise AssertionError(url)

    monkeypatch.setattr("afterword_engine.ingestion.fetch_bytes", private_to_read_fetch)
    assert asyncio.run(import_goodreads_shelves(rss_url)) == {
        "to-read": 0,
        "currently-reading": 1,
    }

    # 2) Explicit RSS settings disconnect deletes remaining 'goodreads_rss' rows
    #    and clears goodreads_rss_last_scheduled_date without deleting 'legacy' CSV rows.
    with TestClient(app, headers=authenticated_headers()) as client:
        disconnect_res = client.put("/api/settings/goodreads-rss", json={"url": ""})
        assert disconnect_res.status_code == 200

    con = sqlite3.connect(db_path)
    try:
        after_disconnect = con.execute(
            "SELECT shelf, source, title FROM goodreads_shelf_items ORDER BY source, title"
        ).fetchall()
        scheduled_after_disconnect = con.execute(
            "SELECT value FROM settings WHERE key='goodreads_rss_last_scheduled_date'"
        ).fetchone()[0]
    finally:
        con.close()
    assert scheduled_after_disconnect == ""
    assert after_disconnect == [
        ("to-read", "legacy", "CSV Queue Book"),
        ("to-read", "legacy", "Removed From CSV Later"),
    ]

    # 3) Re-enable RSS and re-import CSV: CSV re-import cleans 'legacy' rows and
    #    replaces them with 'goodreads_csv' while preserving active 'goodreads_rss' rows.
    monkeypatch.setattr("afterword_engine.ingestion.fetch_bytes", ok_fetch)
    asyncio.run(import_goodreads_shelves(rss_url))

    revised_csv = (
        "Title,Author,My Rating,Date Read,Date Added,Exclusive Shelf,ISBN13\n"
        "CSV Queue Book,Ursula K. Le Guin,0,,2026/01/05,to-read,=\"9780441478125\"\n"
    ).encode("utf-8")
    import_goodreads_csv(revised_csv)

    con = sqlite3.connect(db_path)
    try:
        after_csv_reimport = con.execute(
            "SELECT shelf, source, title FROM goodreads_shelf_items ORDER BY source, shelf, title"
        ).fetchall()
    finally:
        con.close()
    assert after_csv_reimport == [
        ("to-read", "goodreads_csv", "CSV Queue Book"),
        ("currently-reading", "goodreads_rss", "RSS Reading Book"),
        ("to-read", "goodreads_rss", "RSS Queue Book"),
    ]


def test_migration_28_table_rebuild_preserves_extra_columns_via_pragma_table_info(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "v27_with_extra_shelf_columns.db"
    monkeypatch.setattr(settings, "db", str(db_path))

    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        for version, script in MIGRATIONS:
            if version >= 28:
                continue
            _execute_migration_script(con, script)
            con.execute("INSERT INTO schema_migrations(version) VALUES(?)", (version,))
        # Simulate an upstream migration (e.g. #295) adding `additional_authors` and
        # an extra index onto `goodreads_shelf_items` before this table rebuild runs.
        con.execute(
            "ALTER TABLE goodreads_shelf_items ADD COLUMN additional_authors TEXT NOT NULL DEFAULT ''"
        )
        con.execute(
            "CREATE INDEX idx_goodreads_shelf_items_additional_authors "
            "ON goodreads_shelf_items(additional_authors)"
        )
        con.executemany(
            "INSERT INTO goodreads_shelf_items(shelf, title, author, additional_authors, updated_at) "
            "VALUES(?, ?, ?, ?, ?)",
            [
                ("currently-reading", "Good Omens", "Terry Pratchett", "Neil Gaiman", "2026-01-01 00:00:00"),
                ("to-read", "Good Omens", "Terry Pratchett", "", "2026-02-01 00:00:00"),
                ("to-read", "The Talisman", "Stephen King", "Peter Straub", "2026-02-05 00:00:00"),
            ],
        )
        con.commit()
    finally:
        con.close()

    initialize(seed_demo=False)

    con = sqlite3.connect(db_path)
    try:
        cols = {
            row[1]: (row[2], row[3], row[4])
            for row in con.execute("PRAGMA table_info(goodreads_shelf_items)").fetchall()
        }
        indexes = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='goodreads_shelf_items'"
            ).fetchall()
        }
        migrated_rows = con.execute(
            "SELECT shelf, source, title, author, additional_authors "
            "FROM goodreads_shelf_items ORDER BY title"
        ).fetchall()
    finally:
        con.close()

    assert "additional_authors" in cols
    assert cols["additional_authors"] == ("TEXT", 1, "''")
    assert "idx_goodreads_shelf_items_additional_authors" in indexes
    assert migrated_rows == [
        ("to-read", "legacy", "Good Omens", "Terry Pratchett", "Neil Gaiman"),
        ("to-read", "legacy", "The Talisman", "Stephen King", "Peter Straub"),
    ]


def test_initialize_rejects_duplicate_migration_version_numbers(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "db", str(tmp_path / "collision.db"))
    latest_version = MIGRATIONS[-1][0]
    monkeypatch.setattr(
        db_module,
        "MIGRATIONS",
        [*MIGRATIONS, (latest_version, "SELECT 1;")],
    )
    with pytest.raises(RuntimeError, match="unique sequential versions"):
        initialize(seed_demo=False)


def test_rss_url_switch_and_404_clear_stale_rss_preserving_csv_and_undo_hidden(
    tmp_path: Path, monkeypatch
) -> None:
    db_path = tmp_path / "rss_lifecycle.db"
    monkeypatch.setattr(settings, "db", str(db_path))
    initialize(seed_demo=False)

    # 1. Import CSV shelf rows, then re-import revised CSV moving/removing items.
    csv_first = (
        "Title,Author,My Rating,Date Read,Date Added,Exclusive Shelf,ISBN13\n"
        "CSV Fallback Book,Ursula K. Le Guin,0,,2026/01/05,currently-reading,=\"9780441478125\"\n"
        "Removed Queue Book,Author Old,0,,2026/01/06,to-read,\n"
    ).encode("utf-8")
    import_goodreads_csv(csv_first)

    rss_url = "https://www.goodreads.com/review/list_rss/12345?key=secret&shelf=read"
    other_rss_url = "https://www.goodreads.com/review/list_rss/99999?key=other&shelf=read"
    to_read_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>RSS Queued Book</title>
        <author_name>Octavia E. Butler</author_name>
        <isbn13>="9780446675505"</isbn13>
      </item>
    </channel></rss>"""
    reading_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <item>
        <title>RSS Nightstand Book</title>
        <author_name>Ted Chiang</author_name>
      </item>
    </channel></rss>"""

    async def initial_fetch(url: str, **kwargs):
        if "shelf=to-read" in url:
            return to_read_xml, "application/xml"
        if "shelf=currently-reading" in url:
            return reading_xml, "application/xml"
        raise AssertionError(url)

    monkeypatch.setattr("afterword_engine.ingestion.fetch_bytes", initial_fetch)
    assert asyncio.run(import_goodreads_shelves(rss_url)) == {
        "to-read": 1,
        "currently-reading": 1,
    }

    csv_second = (
        "Title,Author,My Rating,Date Read,Date Added,Exclusive Shelf,ISBN13\n"
        "CSV Fallback Book,Ursula K. Le Guin,0,,2026/01/05,to-read,=\"9780441478125\"\n"
    ).encode("utf-8")
    import_goodreads_csv(csv_second)

    # 2. HTTP 404 on private to-read feed clears 'goodreads_rss' to-read row while keeping CSV.
    async def private_to_read_fetch(url: str, **kwargs):
        if "shelf=to-read" in url:
            req = httpx.Request("GET", url)
            resp = httpx.Response(404, request=req)
            raise httpx.HTTPStatusError("404 Not Found", request=req, response=resp)
        if "shelf=currently-reading" in url:
            return reading_xml, "application/xml"
        raise AssertionError(url)

    monkeypatch.setattr("afterword_engine.ingestion.fetch_bytes", private_to_read_fetch)
    assert asyncio.run(import_goodreads_shelves(rss_url)) == {
        "to-read": 0,
        "currently-reading": 1,
    }

    con = sqlite3.connect(db_path)
    try:
        after_404 = con.execute(
            "SELECT shelf, source, title FROM goodreads_shelf_items ORDER BY source, shelf"
        ).fetchall()
    finally:
        con.close()
    assert after_404 == [
        ("to-read", "goodreads_csv", "CSV Fallback Book"),
        ("currently-reading", "goodreads_rss", "RSS Nightstand Book"),
    ]

    # 3. Switching Goodreads RSS URL clears old 'goodreads_rss' rows and scheduled date,
    #    and undo_feedback normalizes legacy previous_status='hidden' to 'new'.
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT OR IGNORE INTO sources(id, name, url, kind, enabled) "
            "VALUES(999, 'Curated', 'https://example.com/feed', 'rss', 1)"
        )
        con.execute(
            "INSERT INTO candidates(id, source_id, title, author, status, normalized_key) "
            "VALUES(801, 999, 'Legacy Hidden Undo', 'Author L', 'rejected', ?)",
            (normalize_key("Legacy Hidden Undo", "Author L"),),
        )
        con.execute(
            "INSERT INTO feedback(id, candidate_id, action, previous_status) "
            "VALUES(91, 801, 'reject', 'hidden')"
        )
        con.commit()
    finally:
        con.close()

    with TestClient(app, headers=authenticated_headers()) as client:
        assert client.put("/api/settings/goodreads-rss", json={"url": rss_url}).status_code == 200
        con = sqlite3.connect(db_path)
        try:
            con.execute(
                "INSERT INTO settings(key, value, secret) VALUES('goodreads_rss_last_scheduled_date', '2026-11-24', 0) "
                "ON CONFLICT(key) DO UPDATE SET value='2026-11-24'"
            )
            con.commit()
        finally:
            con.close()

        switch_res = client.put("/api/settings/goodreads-rss", json={"url": other_rss_url})
        assert switch_res.status_code == 200
        assert switch_res.json() == {"saved": True, "configured": True}

        undo_res = client.post("/api/recommendations/801/undo", json={"decision_id": 91})
        assert undo_res.status_code == 200

    con = sqlite3.connect(db_path)
    try:
        scheduled_after_switch = con.execute(
            "SELECT value FROM settings WHERE key='goodreads_rss_last_scheduled_date'"
        ).fetchone()[0]
        after_switch = con.execute(
            "SELECT shelf, source, title FROM goodreads_shelf_items ORDER BY source, shelf"
        ).fetchall()
        restored_status = con.execute(
            "SELECT status FROM candidates WHERE id=801"
        ).fetchone()[0]
    finally:
        con.close()
    assert scheduled_after_switch == ""
    assert after_switch == [("to-read", "goodreads_csv", "CSV Fallback Book")]
    assert restored_status == "new"
