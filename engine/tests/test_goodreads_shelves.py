import asyncio
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from afterword_engine.config import settings
from afterword_engine.database import initialize
from afterword_engine.ingestion import goodreads_shelf_rss_url, import_goodreads_shelves


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
