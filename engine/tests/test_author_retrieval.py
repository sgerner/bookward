import asyncio
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from afterword_engine.author_retrieval import (
    favorite_authors_from_reads,
    ingest_favorite_author_works,
)
from afterword_engine.config import settings
from afterword_engine.database import initialize


def test_favorite_authors_from_reads_uses_shrunk_mean() -> None:
    reads = [
        {"title": "Book 1", "author": "Fav Author", "rating": 5.0},
        {"title": "Book 2", "author": "Fav Author", "rating": 5.0},
        {"title": "Book 3", "author": "Fav Author", "rating": 4.0},
        {"title": "One Hit", "author": "Single Author", "rating": 5.0},
        {"title": "Meh 1", "author": "Low Author", "rating": 2.0},
        {"title": "Meh 2", "author": "Low Author", "rating": 2.0},
    ]
    favs = favorite_authors_from_reads(reads, min_reads=2, min_shrunk_mean=3.6)
    assert len(favs) == 1
    assert favs[0]["author"] == "Fav Author"
    assert favs[0]["read_count"] == 3


def test_ingest_favorite_author_works_skips_read_and_existing_candidates(tmp_path: Path, monkeypatch) -> None:
    db_path = tmp_path / "fav_authors.db"
    monkeypatch.setattr(settings, "db", str(db_path))
    initialize(seed_demo=False)

    con = sqlite3.connect(db_path)
    try:
        con.execute("INSERT INTO reads(title, author, rating, source) VALUES('Dune', 'Frank Herbert', 5, 'goodreads_rss')")
        con.execute("INSERT INTO reads(title, author, rating, source) VALUES('Dune Messiah', 'Frank Herbert', 5, 'goodreads_rss')")
        con.commit()
    finally:
        con.close()

    async def fake_fetch(author_name: str, *, limit: int = 10):
        return [
            {
                "title": "Dune",
                "author": "Frank Herbert",
                "source_url": "https://openlibrary.org/works/OL1W",
                "openlibrary_work_id": "/works/OL1W",
                "isbn13": None,
                "isbn10": None,
                "release_date": "1965-01-01",
                "genres": ["Science fiction"],
            },
            {
                "title": "Children of Dune",
                "author": "Frank Herbert",
                "source_url": "https://openlibrary.org/works/OL2W",
                "openlibrary_work_id": "/works/OL2W",
                "isbn13": "9780441104024",
                "isbn10": None,
                "release_date": "1976-01-01",
                "genres": ["Science fiction"],
            },
        ]

    monkeypatch.setattr("afterword_engine.author_retrieval.fetch_author_works", fake_fetch)
    res = asyncio.run(ingest_favorite_author_works(max_authors=5, works_per_author=5))
    assert res == {"authors_queried": 1, "added": 1}

    con = sqlite3.connect(db_path)
    try:
        titles = [r[0] for r in con.execute("SELECT title FROM candidates WHERE title LIKE '%Dune%'").fetchall()]
    finally:
        con.close()
    assert titles == ["Children of Dune"]
