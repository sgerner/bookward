import asyncio
import json
from pathlib import Path
from datetime import datetime, timezone

import httpx
import pytest
import respx

from afterword_engine import covers
from afterword_engine.covers import GOOGLE_BOOKS_SEARCH, OPEN_LIBRARY_SEARCH
from afterword_engine.config import settings
from afterword_engine.database import initialize, row, rows, transaction
from afterword_engine.isbn import normalize_isbn_identifier
from afterword_engine.quality import (
    APPLE_BOOKS_SEARCH,
    QUALITY_VERSION,
    _author_similarity,
    _dedupe_and_hide,
    _store_result,
    audit_candidates,
    candidate_quality_audit_candidates,
    canonical_isbn,
    has_candidate_quality_audit_candidates,
    local_flags,
)
from afterword_engine.scoring import score_all


@pytest.fixture(autouse=True)
def no_real_rate_limit_wait(monkeypatch):
    async def advance_without_wait(delay):
        return None

    monkeypatch.setattr(covers, "_openlibrary_rate_sleep", advance_without_wait)


@pytest.fixture()
def database(tmp_path: Path):
    settings.db = str(tmp_path / "quality.db")
    initialize()
    return settings.db


def add_candidate(
    title,
    author,
    *,
    source_url="https://example.com/book",
    status="new",
    isbn13="",
    isbn10="",
):
    with transaction() as con:
        source_id = con.execute(
            "SELECT id FROM sources WHERE url='builtin://upcoming'"
        ).fetchone()[0]
        return con.execute(
            "INSERT INTO candidates(title,author,source_url,source_id,status,isbn13,isbn10,normalized_key) VALUES(?,?,?,?,?,?,?,?)",
            (title, author, source_url, source_id, status, isbn13, isbn10, f"quality-{title}-{author}"),
        ).lastrowid


def test_isbn_validation_and_local_quality(database):
    assert canonical_isbn("978-0-06-112008-4") == "9780061120084"
    assert canonical_isbn("9780061120085") == ""
    assert normalize_isbn_identifier("0-306-40615-2") == "0306406152"
    assert normalize_isbn_identifier("9780061120085") == "9780061120085"
    assert "unknown_author" in local_flags({"title": "A Book", "author": "Unknown author"})
    assert "noise_title" in local_flags({"title": "Coming soon", "author": "A Writer"})
    assert _author_similarity("A Writer", "B Writer") < 0.65
    assert _author_similarity("J.R.R. Tolkien", "John Ronald Tolkien") >= 0.65


@respx.mock
def test_audit_accepts_catalog_match_and_persists_identifier(database):
    candidate_id = add_candidate("A Book", "A Writer")
    with transaction() as con:
        con.execute("UPDATE candidates SET genres=? WHERE id=?", ('["Source genre"]', candidate_id))
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
                        "first_sentence": ["A checked catalog summary."],
                        "subject": ["Literary fiction", "Family life"],
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
    candidate = row("SELECT description,release_date,date_kind,genres FROM candidates WHERE id=?", (candidate_id,))
    assert candidate["description"] == "A checked catalog summary."
    assert candidate["release_date"] == "2010-01-01"
    assert candidate["date_kind"] == "year"
    assert json.loads(candidate["genres"]) == ["Source genre", "Literary fiction", "Family life"]
    provenance = rows(
        "SELECT field,provider,provider_id,source_payload FROM metadata_field_provenance WHERE entity_type='candidate' AND entity_id=?",
        (candidate_id,),
    )
    assert {item["field"] for item in provenance} >= {"description", "genres"}
    description_source = next(item for item in provenance if item["field"] == "description")
    assert description_source["provider"] == "openlibrary"
    assert description_source["provider_id"] == "/works/OL1W"
    assert json.loads(description_source["source_payload"])["payload"]["opening_sentence"] == "A checked catalog summary."


@respx.mock
def test_audit_tries_source_isbn_before_title_author_search(database):
    candidate_id = add_candidate(
        "Recovered Book",
        "A Writer",
        isbn13="9780307474278",
        isbn10="0307474275",
    )
    def openlibrary_response(request):
        return httpx.Response(
            200,
            json={
                "docs": [
                    {
                        "key": "/works/OLISBN",
                        "title": "Recovered Book",
                        "author_name": ["A Writer"],
                        "isbn": ["0307474275"],
                        "first_publish_year": 2010,
                    }
                ]
            },
        )

    def google_response(request):
        if request.url.params.get("q", "").startswith("isbn:"):
            info = {
                "title": "Recovered Book",
                "authors": ["A Writer"],
                "description": "Wrong ISBN metadata must be rejected.",
                "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9780061120084"}],
            }
            return httpx.Response(200, json={"items": [{"id": "wrong-volume", "volumeInfo": info}]})
        info = {
            "title": "Recovered Book",
            "authors": ["A Writer"],
            "description": "Verified fallback synopsis.",
            "categories": ["Historical fiction"],
            "publishedDate": "2010",
            "imageLinks": {"thumbnail": "https://books.google.com/books/content?id=verified-volume"},
        }
        return httpx.Response(200, json={"items": [{"id": "verified-volume", "volumeInfo": info}]})

    isbn_route = respx.get(OPEN_LIBRARY_SEARCH).mock(side_effect=openlibrary_response)
    google_route = respx.get(GOOGLE_BOOKS_SEARCH).mock(side_effect=google_response)

    asyncio.run(audit_candidates(only_pending=False))

    quality = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (candidate_id,))
    candidate = row("SELECT * FROM candidates WHERE id=?", (candidate_id,))
    assert quality["quality_status"] == "accepted"
    assert quality["isbn13"] == "9780307474278"
    assert quality["provider"] == "openlibrary"
    assert quality["provider_id"] == "/works/OLISBN"
    assert quality["metadata_provider"] == "google_books"
    assert quality["metadata_provider_id"] == "verified-volume"
    assert candidate["description"] == "Verified fallback synopsis."
    assert "Wrong ISBN metadata" not in candidate["description"]
    isbn_call = next(
        call
        for call in isbn_route.calls
        if call.request.url.params.get("isbn") == "9780307474278"
    )
    assert "title" not in isbn_call.request.url.params
    assert any(
        call.request.url.params.get("title") == "Recovered Book"
        for call in isbn_route.calls
    )
    assert any(call.request.url.params.get("q", "").startswith("isbn:") for call in google_route.calls)
    assert any(call.request.url.params.get("q", "").startswith("intitle:") for call in google_route.calls)


@respx.mock
def test_provider_unavailable_quarantine_retries_after_backoff(database):
    candidate_id = add_candidate(
        "Rate Limited Candidate",
        "A Writer",
        isbn13="9780307474278",
        isbn10="0307474275",
    )
    respx.get(OPEN_LIBRARY_SEARCH).mock(return_value=httpx.Response(429, json={"error": "rate limited"}))
    respx.get(GOOGLE_BOOKS_SEARCH).mock(return_value=httpx.Response(429, json={"error": "quota"}))

    asyncio.run(audit_candidates(only_pending=False))
    quality = row("SELECT quality_status,flags_json,audited_at FROM candidate_quality WHERE candidate_id=?", (candidate_id,))
    assert quality["quality_status"] == "quarantine"
    assert "catalog_provider_unavailable" in json.loads(quality["flags_json"])
    assert asyncio.run(audit_candidates(only_pending=True))["audited"] == 0

    with transaction() as con:
        con.execute(
            "UPDATE candidate_quality SET audited_at='2000-01-01 00:00:00' WHERE candidate_id=?",
            (candidate_id,),
        )
    assert asyncio.run(audit_candidates(only_pending=True))["audited"] >= 1
    quality = row("SELECT audited_at FROM candidate_quality WHERE candidate_id=?", (candidate_id,))
    assert datetime.fromisoformat(quality["audited_at"]) > datetime(2000, 1, 1)
    assert asyncio.run(audit_candidates(only_pending=True))["audited"] == 0


def test_bounded_quality_recovery_selects_due_rows_and_skips_curated(database):
    assert QUALITY_VERSION == "candidate-quality-v5"
    old_accepted = add_candidate("Old Accepted ISBN", "A Writer", isbn13="9780307474278")
    old_quarantine = add_candidate("Old Quarantine ISBN", "A Writer", isbn10="0307474275")
    quality_isbn_accepted = add_candidate("Old Accepted Quality ISBN", "A Writer")
    quality_isbn_quarantine = add_candidate("Old Quarantine Quality ISBN", "A Writer")
    v4_no_isbn_accepted = add_candidate("V4 Accepted Without ISBN", "A Writer")
    v4_no_isbn_quarantine = add_candidate("V4 Quarantine Without ISBN", "A Writer")
    invalid_isbn = add_candidate("Invalid ISBN", "A Writer", isbn13="9780307474279")
    current_accepted = add_candidate("Current Accepted Without ISBN", "A Writer")
    curated = add_candidate("Curated Seed", "A Writer", isbn13="9780307474278")
    expired_outage = add_candidate("Expired Outage", "A Writer")
    fresh_outage = add_candidate("Fresh Outage", "A Writer", isbn13="9780307474278")
    pending = add_candidate("Pending Without ISBN", "A Writer")
    legacy_pending = add_candidate("Legacy Pending Without ISBN", "A Writer")
    reference_time = datetime(2026, 10, 2, tzinfo=timezone.utc)

    with transaction() as con:
        for candidate_id, status, version in (
            (old_accepted, "accepted", "candidate-quality-v3"),
            (old_quarantine, "quarantine", "candidate-quality-v2"),
            (quality_isbn_accepted, "accepted", "candidate-quality-v3"),
            (quality_isbn_quarantine, "quarantine", "candidate-quality-v2"),
            (v4_no_isbn_accepted, "accepted", "candidate-quality-v4"),
            (v4_no_isbn_quarantine, "quarantine", "candidate-quality-v4"),
            (invalid_isbn, "accepted", "candidate-quality-v3"),
            (current_accepted, "accepted", QUALITY_VERSION),
            (curated, "accepted", "builtin-curated-v1"),
            (expired_outage, "quarantine", QUALITY_VERSION),
            (fresh_outage, "quarantine", QUALITY_VERSION),
            (pending, "pending", QUALITY_VERSION),
            (legacy_pending, "accepted", "legacy-pending-audit-v1"),
        ):
            con.execute(
                "UPDATE candidate_quality SET quality_status=?,audit_version=? WHERE candidate_id=?",
                (status, version, candidate_id),
            )
        con.execute(
            "UPDATE candidate_quality SET flags_json=?,audited_at=? WHERE candidate_id=?",
            ('["catalog_provider_unavailable"]', "2026-09-30T22:00:00+00:00", expired_outage),
        )
        con.execute(
            "UPDATE candidate_quality SET flags_json=?,audited_at=? WHERE candidate_id=?",
            ('["catalog_provider_unavailable"]', "2026-10-01T12:00:00+00:00", fresh_outage),
        )
        con.execute(
            "UPDATE candidate_quality SET isbn13=? WHERE candidate_id=?",
            ("9780307474278", quality_isbn_accepted),
        )
        con.execute(
            "UPDATE candidate_quality SET isbn10=? WHERE candidate_id=?",
            ("0307474275", quality_isbn_quarantine),
        )
        source_id = con.execute("SELECT id FROM sources WHERE url='builtin://upcoming'").fetchone()[0]
        for index in range(55):
            cursor = con.execute(
                "INSERT INTO candidates(title,author,source_url,source_id,status,normalized_key) VALUES(?,?,?,?,?,?)",
                (
                    f"Pending batch {index}",
                    "A Writer",
                    "https://example.com/book",
                    source_id,
                    "new",
                    f"quality-pending-batch-{index}",
                ),
            )
            con.execute(
                "UPDATE candidate_quality SET quality_status='pending',audit_version=? WHERE candidate_id=?",
                (QUALITY_VERSION, cursor.lastrowid),
            )

    selected = candidate_quality_audit_candidates(limit=50, now=reference_time)
    selected_ids = {int(item["id"]) for item in selected}
    assert {
        old_accepted,
        old_quarantine,
        quality_isbn_accepted,
        quality_isbn_quarantine,
        v4_no_isbn_accepted,
        v4_no_isbn_quarantine,
        invalid_isbn,
        expired_outage,
        pending,
        legacy_pending,
    } <= selected_ids
    selected_by_id = {int(item["id"]): item for item in selected}
    assert selected_by_id[quality_isbn_accepted]["isbn13"] == ""
    assert selected_by_id[quality_isbn_quarantine]["isbn10"] == ""
    assert current_accepted not in selected_ids
    assert curated not in selected_ids
    assert fresh_outage not in selected_ids
    assert len(candidate_quality_audit_candidates(limit=500, now=reference_time)) == 50
    assert has_candidate_quality_audit_candidates(now=reference_time)


@respx.mock
def test_audit_quarantines_unmatched_candidate_and_hides_it(database):
    candidate_id = add_candidate("No Catalog Match", "A Writer")
    respx.get(OPEN_LIBRARY_SEARCH).mock(return_value=httpx.Response(200, json={"docs": []}))
    respx.get(GOOGLE_BOOKS_SEARCH).mock(return_value=httpx.Response(200, json={"items": []}))
    respx.get(APPLE_BOOKS_SEARCH).mock(return_value=httpx.Response(200, json={"results": []}))

    asyncio.run(audit_candidates(only_pending=False))
    quality = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (candidate_id,))
    assert quality["quality_status"] == "quarantine"
    assert "catalog_unmatched" in json.loads(quality["flags_json"])
    from afterword_engine.main import recommendation_list

    assert candidate_id not in {item["id"] for item in recommendation_list(limit=None)}


def test_quality_dedupes_accepted_rows_by_shared_work_and_isbn(database):
    first = add_candidate("First Edition", "A Writer")
    same_work = add_candidate("Second Edition", "A Writer")
    same_isbn = add_candidate("Catalog Alias", "Another Author")
    with transaction() as con:
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',quality_score=0.7,"
            "provider='openlibrary',work_id='/works/OL123W' WHERE candidate_id=?",
            (first,),
        )
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',quality_score=0.9,"
            "provider='openlibrary',work_id='/works/OL123W',isbn13='9780306406157' "
            "WHERE candidate_id=?",
            (same_work,),
        )
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',quality_score=0.8,"
            "isbn13='9780306406157' WHERE candidate_id=?",
            (same_isbn,),
        )
        con.execute("UPDATE candidates SET isbn13='9780306406157' WHERE id=?", (first,))
        changed = _dedupe_and_hide(con)

    assert changed == 2
    assert row("SELECT status FROM candidates WHERE id=?", (first,))["status"] == "rejected"
    assert row("SELECT status FROM candidates WHERE id=?", (same_work,))["status"] == "new"
    assert row("SELECT status FROM candidates WHERE id=?", (same_isbn,))["status"] == "rejected"


@respx.mock
def test_initialize_backfills_quarantined_amazon_candidate_for_retry(database):
    candidate_id = add_candidate(
        "Legacy Upcoming Book",
        "A Writer",
        source_url="https://www.amazon.com/gp/product/0061120081?tag=bookward",
    )
    with transaction() as con:
        source_id = con.execute(
            "INSERT INTO sources(name,url) VALUES(?,?) RETURNING id",
            ("Legacy Amazon source", "https://example.com/legacy-amazon"),
        ).fetchone()[0]
        con.execute("UPDATE candidates SET source_id=? WHERE id=?", (source_id, candidate_id))
        con.execute(
            "UPDATE candidate_quality SET quality_status='quarantine',audit_version='candidate-quality-v1' WHERE candidate_id=?",
            (candidate_id,),
        )

    initialize()
    candidate = row("SELECT * FROM candidates WHERE id=?", (candidate_id,))
    quality = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (candidate_id,))
    assert candidate["isbn13"] == "9780061120084"
    assert candidate["isbn10"] == "0061120081"
    assert quality["quality_status"] == "quarantine"

    respx.get(OPEN_LIBRARY_SEARCH).mock(
        return_value=httpx.Response(
            200,
            json={
                "docs": [
                    {
                        "key": "/works/OLLEGACY",
                        "title": "Legacy Upcoming Book",
                        "author_name": ["A Writer"],
                    }
                ]
            },
        )
    )
    result = asyncio.run(audit_candidates(only_pending=True))
    quality = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (candidate_id,))
    assert result["accepted"] >= 1
    assert quality["quality_status"] == "accepted"


@respx.mock
def test_isbn_recovery_does_not_reaudit_accepted_candidates(database):
    quarantined_id = add_candidate(
        "Recoverable Book",
        "A Writer",
        isbn13="9780307474278",
        isbn10="0307474275",
    )
    accepted_id = add_candidate("Already Accepted", "A Different Writer")
    with transaction() as con:
        con.execute(
            "UPDATE candidate_quality SET quality_status='quarantine',audit_version='candidate-quality-v1' WHERE candidate_id=?",
            (quarantined_id,),
        )
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',audit_version=? WHERE candidate_id=?",
            (QUALITY_VERSION, accepted_id),
        )
    accepted_before = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (accepted_id,))

    isbn_route = respx.get(OPEN_LIBRARY_SEARCH).mock(
        return_value=httpx.Response(
            200,
            json={
                "docs": [
                    {
                        "key": "/works/OLRECOVERED",
                        "title": "Recoverable Book",
                        "author_name": ["A Writer"],
                    }
                ]
            },
        )
    )

    result = asyncio.run(audit_candidates(only_pending=True))

    assert result["audited"] == 1
    assert row("SELECT quality_status FROM candidate_quality WHERE candidate_id=?", (quarantined_id,))["quality_status"] == "accepted"
    accepted_quality = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (accepted_id,))
    assert accepted_quality["quality_status"] == "accepted"
    assert accepted_quality["audit_version"] == accepted_before["audit_version"]
    assert any(call.request.url.params.get("isbn") == "9780307474278" for call in isbn_route.calls)


def test_audit_rejects_malformed_candidate_without_catalog_request(database):
    candidate_id = add_candidate("Coming soon", "Unknown author")
    asyncio.run(audit_candidates(only_pending=False))
    quality = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (candidate_id,))
    assert quality["quality_status"] == "rejected"
    assert row("SELECT status FROM candidates WHERE id=?", (candidate_id,))["status"] == "rejected"


@respx.mock
def test_audit_circuit_breaks_google_books_rate_limit(database):
    first_id = add_candidate("Unmatched One", "Author One")
    second_id = add_candidate("Unmatched Two", "Author Two")
    with transaction() as con:
        con.execute(
            "UPDATE candidate_quality SET quality_status='pending' WHERE candidate_id IN (?,?)",
            (first_id, second_id),
        )
    respx.get(OPEN_LIBRARY_SEARCH).mock(return_value=httpx.Response(200, json={"docs": []}))
    gb_route = respx.get(GOOGLE_BOOKS_SEARCH).mock(return_value=httpx.Response(429))

    result = asyncio.run(audit_candidates(only_pending=True))
    assert result["audited"] == 2
    assert result["quarantine"] == 2
    assert len(gb_route.calls) == 1
    for cid in (first_id, second_id):
        flags = json.loads(row("SELECT flags_json FROM candidate_quality WHERE candidate_id=?", (cid,))["flags_json"])
        assert "catalog_provider_rate_limited" in flags


@respx.mock
def test_audit_matches_format_and_parenthetical_series_suffixes(database):
    audiobook_id = add_candidate("Greenlights (Unabridged)", "Matthew McConaughey")
    series_id = add_candidate("The Fellowship of the Ring (Lord of the Rings)", "J. R. R. Tolkien")
    with transaction() as con:
        con.execute(
            "UPDATE candidate_quality SET quality_status='pending' WHERE candidate_id IN (?,?)",
            (audiobook_id, series_id),
        )

    def ol_handler(request):
        title = request.url.params.get("title", "")
        if "Greenlights" in title:
            return httpx.Response(200, json={"docs": [{"key": "/works/OLGREEN", "title": "Greenlights", "author_name": ["Matthew McConaughey"]}]})
        if "Fellowship" in title:
            return httpx.Response(200, json={"docs": [{"key": "/works/OLFOTR", "title": "The Fellowship of the Ring", "author_name": ["J. R. R. Tolkien"]}]})
        return httpx.Response(200, json={"docs": []})

    ol_route = respx.get(OPEN_LIBRARY_SEARCH).mock(side_effect=ol_handler)
    respx.get(GOOGLE_BOOKS_SEARCH).mock(return_value=httpx.Response(429))

    result = asyncio.run(audit_candidates(only_pending=True))
    assert result["accepted"] == 2
    assert all("Unabridged" not in call.request.url.params.get("title", "") for call in ol_route.calls)


def test_audit_skips_disabled_source_candidates(database):
    with transaction() as con:
        con.execute(
            "INSERT INTO sources(name,url,enabled,lifecycle) VALUES('Disabled','https://example.com/disabled',0,'permanent')"
        )
        disabled_source_id = con.execute("SELECT id FROM sources WHERE name='Disabled'").fetchone()[0]
        cursor = con.execute(
            "INSERT INTO candidates(title,author,source_url,source_id,status,normalized_key) VALUES(?,?,?,?,?,?)",
            ("Disabled Book", "Some Author", "https://example.com/disabled/1", disabled_source_id, "new", "disabled-book-1"),
        )
        disabled_cid = cursor.lastrowid
        con.execute(
            "UPDATE candidate_quality SET quality_status='pending',audit_version=? WHERE candidate_id=?",
            (QUALITY_VERSION, disabled_cid),
        )

    selected_ids = {int(item["id"]) for item in candidate_quality_audit_candidates(limit=50)}
    assert disabled_cid not in selected_ids


@respx.mock
def test_audit_recovers_candidate_via_apple_books_when_google_books_rate_limited(database):
    candidate_id = add_candidate("Find Me Where It Ends", "Cassandra Khaw")
    with transaction() as con:
        con.execute("UPDATE sources SET enabled=1 WHERE url='builtin://upcoming'")
        con.execute(
            "UPDATE candidate_quality SET quality_status='pending',audit_version=? WHERE candidate_id=?",
            (QUALITY_VERSION, candidate_id),
        )
    respx.get(OPEN_LIBRARY_SEARCH).mock(return_value=httpx.Response(200, json={"docs": []}))
    respx.get(GOOGLE_BOOKS_SEARCH).mock(return_value=httpx.Response(429))
    respx.get(APPLE_BOOKS_SEARCH).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "trackId": 6758952748,
                        "trackName": "Find Me Where It Ends",
                        "artistName": "Cassandra Khaw",
                        "description": "A haunting contemporary horror novel.",
                        "genres": ["Horror", "Books", "Sci-Fi & Fantasy"],
                        "releaseDate": "2026-10-06T07:00:00Z",
                        "artworkUrl100": "https://is1-ssl.mzstatic.com/image/thumb/Publication211/v4/8c/c0/68/test.jpg/100x100bb.jpg",
                    }
                ]
            },
        )
    )

    result = asyncio.run(audit_candidates(only_pending=True))
    quality = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (candidate_id,))
    candidate = row("SELECT * FROM candidates WHERE id=?", (candidate_id,))
    assert result["accepted"] == 1
    assert quality["quality_status"] == "accepted"
    assert quality["provider"] == "apple_books"
    assert quality["provider_id"] == "6758952748"
    assert candidate["release_date"] == "2026-10-06"
    assert "600x600bb" in candidate["cover_url"]


@respx.mock
def test_audit_quarantines_homonym_author_without_attaching_wrong_work(database):
    candidate_id = add_candidate("Home", "Marilynne Robinson")
    with transaction() as con:
        con.execute(
            "UPDATE candidate_quality SET quality_status='pending',audit_version=? WHERE candidate_id=?",
            (QUALITY_VERSION, candidate_id),
        )
    assert _author_similarity("Marilynne Robinson", "Michael Robinson") < 0.65

    respx.get(OPEN_LIBRARY_SEARCH).mock(
        return_value=httpx.Response(
            200,
            json={
                "docs": [
                    {
                        "key": "/works/OLHOMONYMW",
                        "title": "Home",
                        "author_name": ["Michael Robinson"],
                        "isbn": ["9780061120084"],
                        "description": "Unrelated homonym book blurb.",
                        "first_publish_year": 2014,
                        "subject": ["Biography"],
                    }
                ]
            },
        )
    )
    respx.get(GOOGLE_BOOKS_SEARCH).mock(return_value=httpx.Response(200, json={"items": []}))
    respx.get(APPLE_BOOKS_SEARCH).mock(return_value=httpx.Response(200, json={"results": []}))

    result = asyncio.run(audit_candidates(only_pending=True))
    quality = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (candidate_id,))
    candidate = row("SELECT * FROM candidates WHERE id=?", (candidate_id,))

    assert result["quarantine"] == 1
    assert quality["quality_status"] == "quarantine"
    assert quality["author_match"] < 0.65
    assert "catalog_match_ambiguous" in json.loads(quality["flags_json"])
    assert quality["provider"] == ""
    assert quality["provider_id"] == ""
    assert quality["work_id"] == ""
    assert quality["isbn13"] == ""
    assert candidate["description"] == ""
    assert candidate["release_date"] is None


@respx.mock
def test_audit_rejects_derivatives_and_foreign_language_records(database):
    parenthetical_id = add_candidate("Fourth Wing", "Rebecca Yarros")
    prefix_id = add_candidate("Tomorrow, and Tomorrow, and Tomorrow", "Gabrielle Zevin")
    spanish_id = add_candidate("The Shadow Wind", "Carlos Writer")
    with transaction() as con:
        con.execute(
            "UPDATE candidate_quality SET quality_status='pending',audit_version=? WHERE candidate_id IN (?,?,?)",
            (QUALITY_VERSION, parenthetical_id, prefix_id, spanish_id),
        )

    def ol_handler(request):
        assert "language" in request.url.params.get("fields", "").split(",")
        title = request.url.params.get("title", "")
        if "Fourth Wing" in title:
            return httpx.Response(
                200,
                json={
                    "docs": [
                        {
                            "key": "/works/OLGUIDE1W",
                            "title": "Fourth Wing (Summary & Study Guide)",
                            "author_name": ["Rebecca Yarros"],
                            "description": "Chapter-by-chapter study guide.",
                            "language": ["eng"],
                        }
                    ]
                },
            )
        if "Tomorrow" in title:
            return httpx.Response(
                200,
                json={
                    "docs": [
                        {
                            "key": "/works/OLGUIDE2W",
                            "title": "Summary of Tomorrow, and Tomorrow, and Tomorrow",
                            "author_name": ["Gabrielle Zevin"],
                            "description": "Quick summary and analysis.",
                            "language": ["eng"],
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "docs": [
                    {
                        "key": "/works/OLSPANISHW",
                        "title": "The Shadow Wind",
                        "author_name": ["Carlos Writer"],
                        "description": "Una novela en español.",
                        "language": ["spa"],
                    }
                ]
            },
        )

    def gb_handler(request):
        q = request.url.params.get("q", "")
        if "Shadow Wind" in q:
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "spanish-vol",
                            "volumeInfo": {
                                "title": "The Shadow Wind",
                                "authors": ["Carlos Writer"],
                                "description": "Edición española.",
                                "language": "es",
                            },
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"items": []})

    respx.get(OPEN_LIBRARY_SEARCH).mock(side_effect=ol_handler)
    respx.get(GOOGLE_BOOKS_SEARCH).mock(side_effect=gb_handler)
    respx.get(APPLE_BOOKS_SEARCH).mock(return_value=httpx.Response(200, json={"results": []}))

    result = asyncio.run(audit_candidates(only_pending=True))
    assert result["audited"] == 3
    assert result["quarantine"] == 3
    for cid in (parenthetical_id, prefix_id, spanish_id):
        quality = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (cid,))
        candidate = row("SELECT * FROM candidates WHERE id=?", (cid,))
        assert quality["quality_status"] == "quarantine"
        assert candidate["description"] == ""


@respx.mock
def test_v5_reaudit_quarantine_clears_catalog_fields_and_preserves_source_fields(database):
    catalog_polluted_id = add_candidate("Home", "Marilynne Robinson")
    source_curated_id = add_candidate("Home", "Marilynne R. Author")
    with transaction() as con:
        source_id = str(con.execute("SELECT id FROM sources WHERE url='builtin://upcoming'").fetchone()[0])
        con.execute(
            "UPDATE candidates SET description=?,cover_url=?,release_date=?,date_kind=?,genres=? WHERE id=?",
            (
                "Polluted homonym synopsis.",
                "https://covers.openlibrary.org/b/id/999-L.jpg",
                "2014-01-01",
                "year",
                json.dumps(["Literary fiction", "Biography"]),
                catalog_polluted_id,
            ),
        )
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',audit_version='candidate-quality-v4',"
            "provider='openlibrary',provider_id='/works/OLHOMONYMW',work_id='/works/OLHOMONYMW',"
            "metadata_provider='openlibrary',metadata_provider_id='/works/OLHOMONYMW' WHERE candidate_id=?",
            (catalog_polluted_id,),
        )
        con.execute(
            "INSERT INTO metadata_field_provenance(entity_type,entity_id,field,provider,provider_id,confidence,source_payload) "
            "VALUES('candidate',?,'genres','source',?,1.0,'{}')",
            (catalog_polluted_id, source_id),
        )
        for field, payload in (
            ("description", {"payload": {"description": "Polluted homonym synopsis."}}),
            ("cover_url", {"payload": {"cover_id": "999"}}),
            ("release_date", {"payload": {"publication_date": "2014"}}),
            ("genres", {"payload": {"genres": ["Biography"]}}),
        ):
            con.execute(
                "INSERT INTO metadata_field_provenance(entity_type,entity_id,field,provider,provider_id,confidence,source_payload) "
                "VALUES('candidate',?,?,'openlibrary','/works/OLHOMONYMW',0.94,?)",
                (catalog_polluted_id, field, json.dumps(payload)),
            )

        con.execute(
            "UPDATE candidates SET description=?,cover_url=?,release_date=?,date_kind=?,genres=? WHERE id=?",
            (
                "Curated feed blurb from discovery source.",
                "https://covers.openlibrary.org/b/id/111-L.jpg",
                "2008-09-02",
                "source",
                json.dumps(["Literary fiction"]),
                source_curated_id,
            ),
        )
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',audit_version='candidate-quality-v4',"
            "provider='openlibrary',provider_id='/works/OLHOMONYM2W',work_id='/works/OLHOMONYM2W' WHERE candidate_id=?",
            (source_curated_id,),
        )
        for field in ("description", "cover_url", "release_date", "genres"):
            con.execute(
                "INSERT INTO metadata_field_provenance(entity_type,entity_id,field,provider,provider_id,confidence,source_payload) "
                "VALUES('candidate',?,?,'source',?,1.0,'{}')",
                (source_curated_id, field, source_id),
            )

    selected_ids = {int(item["id"]) for item in candidate_quality_audit_candidates(limit=50)}
    assert {catalog_polluted_id, source_curated_id} <= selected_ids

    respx.get(OPEN_LIBRARY_SEARCH).mock(
        return_value=httpx.Response(
            200,
            json={
                "docs": [
                    {
                        "key": "/works/OLHOMONYMW",
                        "title": "Home",
                        "author_name": ["Michael Robinson"],
                        "description": "Polluted homonym synopsis.",
                    }
                ]
            },
        )
    )
    respx.get(GOOGLE_BOOKS_SEARCH).mock(return_value=httpx.Response(200, json={"items": []}))
    respx.get(APPLE_BOOKS_SEARCH).mock(return_value=httpx.Response(200, json={"results": []}))

    result = asyncio.run(audit_candidates(only_pending=True))
    assert result["quarantine"] == 2

    polluted_after = row("SELECT * FROM candidates WHERE id=?", (catalog_polluted_id,))
    polluted_quality = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (catalog_polluted_id,))
    assert polluted_quality["quality_status"] == "quarantine"
    assert polluted_quality["audit_version"] == QUALITY_VERSION
    assert polluted_quality["work_id"] == ""
    assert polluted_quality["metadata_provider"] == ""
    assert polluted_after["description"] == ""
    assert polluted_after["cover_url"] == ""
    assert polluted_after["release_date"] is None
    assert json.loads(polluted_after["genres"]) == ["Literary fiction"]
    remaining_prov = rows(
        "SELECT field,provider FROM metadata_field_provenance WHERE entity_type='candidate' AND entity_id=?",
        (catalog_polluted_id,),
    )
    assert remaining_prov == [{"field": "genres", "provider": "source"}]

    curated_after = row("SELECT * FROM candidates WHERE id=?", (source_curated_id,))
    assert curated_after["description"] == "Curated feed blurb from discovery source."
    assert curated_after["cover_url"] == "https://covers.openlibrary.org/b/id/111-L.jpg"
    assert curated_after["release_date"] == "2008-09-02"
    assert json.loads(curated_after["genres"]) == ["Literary fiction"]


def test_store_result_replaces_polluted_v4_catalog_description_keeps_source_and_updates_embeddings(database):
    verified_synopsis = (
        "Twenty-year-old Violet Sorrengail was supposed to enter the Scribe Quadrant, "
        "living a quiet life among books and history."
    )
    assert len(verified_synopsis) == 121
    polluted_blurb = (
        "Resumen completo y guía de estudio de Alas de Sangre: análisis detallado de capítulos, "
        "personajes, temas principales y preguntas de discusión para estudiantes y clubes de lectura "
        "que buscan comprender cada escena de esta edición especial en español ya"
    )
    assert len(polluted_blurb) == 251

    with transaction() as con:
        con.execute("UPDATE candidates SET status='rejected'")
        con.execute("DELETE FROM reads")
        read_id = con.execute(
            "INSERT INTO reads(title,author,rating,source) VALUES(?,?,?,?)",
            ("Dragonflight", "Anne McCaffrey", 5.0, "test"),
        ).lastrowid
        source_id = str(con.execute("SELECT id FROM sources WHERE url='builtin://upcoming'").fetchone()[0])

    catalog_cid = add_candidate("Fourth Wing", "Rebecca Yarros", isbn13="9781649374042", status="recommended")
    source_cid = add_candidate("Source Curated Fantasy", "Rebecca Yarros", status="recommended")

    with transaction() as con:
        con.execute(
            "UPDATE candidates SET description=?,genres=? WHERE id=?",
            (polluted_blurb, json.dumps(["Fantasy", "Study Aids"]), catalog_cid),
        )
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',audit_version='candidate-quality-v4',"
            "provider='google_books',provider_id='spanish-guide',metadata_provider='google_books',"
            "metadata_provider_id='spanish-guide',isbn13='9781649374042' WHERE candidate_id=?",
            (catalog_cid,),
        )
        con.execute(
            "INSERT INTO metadata_field_provenance(entity_type,entity_id,field,provider,provider_id,confidence,source_payload) "
            "VALUES('candidate',?,'description','google_books','spanish-guide',0.95,?)",
            (catalog_cid, json.dumps({"payload": {"description": polluted_blurb}})),
        )
        con.execute(
            "INSERT INTO metadata_field_provenance(entity_type,entity_id,field,provider,provider_id,confidence,source_payload) "
            "VALUES('candidate',?,'genres','google_books','spanish-guide',0.95,?)",
            (catalog_cid, json.dumps({"payload": {"genres": ["Fantasy", "Study Aids"]}})),
        )

        con.execute(
            "UPDATE candidates SET description=?,genres=? WHERE id=?",
            ("Editorial source description that must remain intact.", json.dumps(["Fantasy"]), source_cid),
        )
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',audit_version='candidate-quality-v4',"
            "provider='openlibrary',provider_id='/works/OLSOURCEW' WHERE candidate_id=?",
            (source_cid,),
        )
        con.execute(
            "INSERT INTO metadata_field_provenance(entity_type,entity_id,field,provider,provider_id,confidence,source_payload) "
            "VALUES('candidate',?,'description','source',?,1.0,'{}')",
            (source_cid, source_id),
        )

    class TrackingEmbedder:
        name, model = "test", "v5-refresh"

        def __init__(self):
            self.calls: list[str] = []

        async def embed(self, texts):
            self.calls.extend(texts)
            return [[1.0, 0.2] if "Violet Sorrengail" in text else [0.2, 1.0] for text in texts]

    embedder = TrackingEmbedder()
    assert asyncio.run(score_all(embedder=embedder)) == 2
    initial_hashes = {
        (item["entity_type"], int(item["entity_id"])): item["content_hash"]
        for item in rows("SELECT entity_type,entity_id,content_hash FROM embeddings WHERE backend='test' AND model='v5-refresh'")
    }
    embedder.calls.clear()

    clean_catalog_result = {
        "quality_status": "accepted",
        "quality_score": 1.0,
        "title_match": 1.0,
        "author_match": 1.0,
        "flags": [],
        "provider": "openlibrary",
        "provider_id": "/works/OLFOURTHWINGW",
        "work_id": "/works/OLFOURTHWINGW",
        "isbn13": "9781649374042",
        "isbn10": "1649374046",
        "description": verified_synopsis,
        "description_kind": "synopsis",
        "description_provider": "openlibrary",
        "description_provider_id": "/works/OLFOURTHWINGW",
        "genres": ["Fantasy"],
        "genres_provider": "openlibrary",
        "genres_provider_id": "/works/OLFOURTHWINGW",
        "metadata_provenance": {
            "fetched_at": "2026-11-01T00:00:00Z",
            "fields": {
                "description": {"provider": "openlibrary", "provider_id": "/works/OLFOURTHWINGW", "kind": "synopsis"},
                "genres": {"provider": "openlibrary", "provider_id": "/works/OLFOURTHWINGW", "kind": "subjects"},
            },
            "source_payloads": [
                {
                    "provider": "openlibrary",
                    "provider_id": "/works/OLFOURTHWINGW",
                    "payload": {"description": verified_synopsis, "genres": ["Fantasy"]},
                }
            ],
        },
    }

    with transaction() as con:
        catalog_candidate = dict(con.execute("SELECT * FROM candidates WHERE id=?", (catalog_cid,)).fetchone())
        source_candidate = dict(con.execute("SELECT * FROM candidates WHERE id=?", (source_cid,)).fetchone())
        assert _store_result(con, catalog_candidate, clean_catalog_result) is True
        assert _store_result(con, source_candidate, clean_catalog_result) is False

    updated_catalog = row("SELECT description,genres FROM candidates WHERE id=?", (catalog_cid,))
    updated_source = row("SELECT description,genres FROM candidates WHERE id=?", (source_cid,))
    assert updated_catalog["description"] == verified_synopsis
    assert json.loads(updated_catalog["genres"]) == ["Fantasy"]
    assert updated_source["description"] == "Editorial source description that must remain intact."
    desc_prov = rows(
        "SELECT provider,provider_id FROM metadata_field_provenance "
        "WHERE entity_type='candidate' AND entity_id=? AND field='description'",
        (catalog_cid,),
    )
    assert desc_prov == [{"provider": "openlibrary", "provider_id": "/works/OLFOURTHWINGW"}]

    assert asyncio.run(score_all(embedder=embedder)) == 2
    assert len(embedder.calls) == 1
    assert verified_synopsis in embedder.calls[0]
    refreshed_hashes = {
        (item["entity_type"], int(item["entity_id"])): item["content_hash"]
        for item in rows("SELECT entity_type,entity_id,content_hash FROM embeddings WHERE backend='test' AND model='v5-refresh'")
    }
    assert refreshed_hashes[("candidate", catalog_cid)] != initial_hashes[("candidate", catalog_cid)]
    assert refreshed_hashes[("candidate", source_cid)] == initial_hashes[("candidate", source_cid)]
    assert refreshed_hashes[("read", read_id)] == initial_hashes[("read", read_id)]