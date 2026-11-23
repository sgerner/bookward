import asyncio
from pathlib import Path
import socket
import sqlite3
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from afterword_engine import ingestion
from afterword_engine.config import settings
from afterword_engine.database import initialize
from afterword_engine.ingestion import goodreads_shelf_rss_url, import_goodreads_shelves, validate_goodreads_rss_url


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
            "SELECT shelf, title, author FROM goodreads_shelf_items ORDER BY shelf"
        ).fetchall()
    finally:
        con.close()

    assert reads_count == 0
    assert shelf_rows == [
        ("currently-reading", "Piranesi", "Susanna Clarke"),
        ("to-read", "Project Hail Mary", "Andy Weir"),
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
            "INSERT INTO goodreads_shelf_items(shelf, title, author, isbn) VALUES(?, ?, ?, ?)",
            [
                ("to-read", "Project Hail Mary", "Andy Weir", "9780593135204"),
                ("currently-reading", "Project Hail Mary", "Andy Weir", "9780593135204"),
                ("to-read", "project hail mary", "Andy Weir", None),
                ("to-read", "Piranesi", "Susanna Clarke", "9781635575637"),
                ("currently-reading", "Piranesi", "Susanna Clarke", "9781635575637"),
                ("to-read", "Dune", "Frank Herbert", "9780441172719"),
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
            "INSERT INTO goodreads_shelf_items(shelf, title, author) "
            "VALUES('currently-reading', 'Old Reading', 'Author Two')"
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

