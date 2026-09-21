import asyncio
import json
from pathlib import Path

import httpx
import pytest
import respx

from afterword_engine.covers import GOOGLE_BOOKS_SEARCH, OPEN_LIBRARY_SEARCH
from afterword_engine.config import settings
from afterword_engine.database import initialize, row, rows, transaction
from afterword_engine.quality import audit_candidates, canonical_isbn, local_flags, _author_similarity


@pytest.fixture()
def database(tmp_path: Path):
    settings.db = str(tmp_path / "quality.db")
    initialize()
    return settings.db


def add_candidate(title, author, *, source_url="https://example.com/book", status="new"):
    with transaction() as con:
        source_id = con.execute(
            "SELECT id FROM sources WHERE url='builtin://upcoming'"
        ).fetchone()[0]
        return con.execute(
            "INSERT INTO candidates(title,author,source_url,source_id,status,normalized_key) VALUES(?,?,?,?,?,?)",
            (title, author, source_url, source_id, status, f"quality-{title}-{author}"),
        ).lastrowid


def test_isbn_validation_and_local_quality(database):
    assert canonical_isbn("978-0-06-112008-4") == "9780061120084"
    assert canonical_isbn("9780061120085") == ""
    assert "unknown_author" in local_flags({"title": "A Book", "author": "Unknown author"})
    assert "noise_title" in local_flags({"title": "Coming soon", "author": "A Writer"})
    assert _author_similarity("A Writer", "B Writer") < 0.65
    assert _author_similarity("J.R.R. Tolkien", "John Ronald Tolkien") >= 0.65


@respx.mock
def test_audit_accepts_catalog_match_and_persists_identifier(database):
    candidate_id = add_candidate("A Book", "A Writer")
    respx.get(OPEN_LIBRARY_SEARCH).mock(
        return_value=httpx.Response(
            200,
            json={
                "docs": [
                    {
                        "key": "/works/OL1W",
                        "title": "A Book",
                        "author_name": ["A Writer"],
                        "isbn": ["0307474275"],
                        "first_publish_year": 2010,
                    }
                ]
            },
        )
    )
    respx.get(GOOGLE_BOOKS_SEARCH).mock(return_value=httpx.Response(200, json={"items": []}))

    result = asyncio.run(audit_candidates(only_pending=False))
    quality = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (candidate_id,))
    assert result["audited"] >= 1
    assert quality["quality_status"] == "accepted"
    assert quality["isbn13"] == "9780307474278"
    assert quality["work_id"] == "/works/OL1W"
    assert row("SELECT status FROM candidates WHERE id=?", (candidate_id,))["status"] == "new"


@respx.mock
def test_audit_quarantines_unmatched_candidate_and_hides_it(database):
    candidate_id = add_candidate("No Catalog Match", "A Writer")
    respx.get(OPEN_LIBRARY_SEARCH).mock(return_value=httpx.Response(200, json={"docs": []}))
    respx.get(GOOGLE_BOOKS_SEARCH).mock(return_value=httpx.Response(200, json={"items": []}))

    asyncio.run(audit_candidates(only_pending=False))
    quality = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (candidate_id,))
    assert quality["quality_status"] == "quarantine"
    assert "catalog_unmatched" in json.loads(quality["flags_json"])
    from afterword_engine.main import recommendation_list

    assert candidate_id not in {item["id"] for item in recommendation_list(limit=None)}


def test_audit_rejects_malformed_candidate_without_catalog_request(database):
    candidate_id = add_candidate("Coming soon", "Unknown author")
    asyncio.run(audit_candidates(only_pending=False))
    quality = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (candidate_id,))
    assert quality["quality_status"] == "rejected"
    assert row("SELECT status FROM candidates WHERE id=?", (candidate_id,))["status"] == "rejected"
