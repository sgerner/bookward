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
    _apple_books_match,
    _author_similarity,
    _dedupe_and_hide,
    _fetch_catalog_json,
    _google_books_match,
    _open_library_match,
    _provider_match,
    _select_best_provider_match,
    audit_candidates,
    candidate_quality_audit_candidates,
    canonical_isbn,
    has_candidate_quality_audit_candidates,
    local_flags,
)


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
    assert QUALITY_VERSION == "candidate-quality-v4"
    old_accepted = add_candidate("Old Accepted ISBN", "A Writer", isbn13="9780307474278")
    old_quarantine = add_candidate("Old Quarantine ISBN", "A Writer", isbn10="0307474275")
    quality_isbn_accepted = add_candidate("Old Accepted Quality ISBN", "A Writer")
    quality_isbn_quarantine = add_candidate("Old Quarantine Quality ISBN", "A Writer")
    invalid_isbn = add_candidate("Invalid ISBN", "A Writer", isbn13="9780307474279")
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
            (invalid_isbn, "accepted", "candidate-quality-v3"),
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
        expired_outage,
        pending,
        legacy_pending,
    } <= selected_ids
    selected_by_id = {int(item["id"]): item for item in selected}
    assert selected_by_id[quality_isbn_accepted]["isbn13"] == ""
    assert selected_by_id[quality_isbn_quarantine]["isbn10"] == ""
    assert invalid_isbn not in selected_ids
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
def test_audit_replaces_unverified_source_dates_with_verified_openlibrary_work_dates(database):
    from afterword_engine.database import connect, persist_metadata_field_provenance
    from afterword_engine.publication_year_metadata import attach_publication_years

    source_kind_id = add_candidate("Classic Reissue One", "Agatha Christie")
    feed_prov_id = add_candidate("Classic Reissue Two", "Dorothy L. Sayers")
    manual_id = add_candidate("Manual Classic", "Margery Allingham")

    with transaction() as con:
        source_id = con.execute("SELECT id FROM sources WHERE url='builtin://upcoming'").fetchone()[0]
        con.execute(
            "UPDATE candidates SET release_date='2023-12-25',date_kind='source' WHERE id=?",
            (source_kind_id,),
        )
        con.execute(
            "UPDATE candidates SET release_date='2023-12-25',date_kind='day' WHERE id=?",
            (feed_prov_id,),
        )
        persist_metadata_field_provenance(
            con,
            "candidate",
            feed_prov_id,
            "release_date",
            {
                "metadata_provenance": {
                    "fields": {
                        "release_date": {
                            "provider": "source",
                            "provider_id": str(source_id),
                            "kind": "source_item",
                        }
                    },
                    "fetch_trace": [],
                    "source_payloads": [],
                }
            },
            default_provider="source",
            default_provider_id=str(source_id),
        )
        con.execute(
            "UPDATE candidates SET release_date='1999-01-01',date_kind='manual' WHERE id=?",
            (manual_id,),
        )

    def openlibrary_side_effect(request):
        title = request.url.params.get("title", "")
        if "One" in title:
            return httpx.Response(200, json={"docs": [{
                "key": "/works/OL471579W",
                "title": "Classic Reissue One",
                "author_name": ["Agatha Christie"],
                "first_publish_year": 1934,
                "description": "Original 1934 mystery novel.",
                "subject": ["Mystery"],
            }]})
        if "Two" in title:
            return httpx.Response(200, json={"docs": [{
                "key": "/works/OL193500W",
                "title": "Classic Reissue Two",
                "author_name": ["Dorothy L. Sayers"],
                "first_publish_date": "1935-10-01",
                "description": "Original 1935 mystery novel.",
                "subject": ["Mystery"],
            }]})
        return httpx.Response(200, json={"docs": [{
            "key": "/works/OL193600W",
            "title": "Manual Classic",
            "author_name": ["Margery Allingham"],
            "first_publish_year": 1936,
            "description": "Original 1936 mystery novel.",
            "subject": ["Mystery"],
        }]})

    respx.get(OPEN_LIBRARY_SEARCH).mock(side_effect=openlibrary_side_effect)
    respx.get(GOOGLE_BOOKS_SEARCH).mock(
        return_value=httpx.Response(200, json={"items": [{
            "id": "reissue-vol",
            "volumeInfo": {
                "title": "Classic Reissue One",
                "authors": ["Agatha Christie"],
                "publishedDate": "2023-12-25",
            },
        }]})
    )
    respx.get(APPLE_BOOKS_SEARCH).mock(return_value=httpx.Response(200, json={"results": []}))

    asyncio.run(audit_candidates(only_pending=False))

    cand_one = row("SELECT id,release_date,date_kind FROM candidates WHERE id=?", (source_kind_id,))
    cand_two = row("SELECT id,release_date,date_kind FROM candidates WHERE id=?", (feed_prov_id,))
    cand_manual = row("SELECT id,release_date,date_kind FROM candidates WHERE id=?", (manual_id,))

    assert cand_one["release_date"] == "1934-01-01"
    assert cand_one["date_kind"] == "year"
    assert cand_two["release_date"] == "1935-10-01"
    assert cand_two["date_kind"] == "day"
    assert cand_manual["release_date"] == "1999-01-01"
    assert cand_manual["date_kind"] == "manual"

    for cid, expected_work, expected_field in (
        (source_kind_id, "/works/OL471579W", "first_publish_year"),
        (feed_prov_id, "/works/OL193500W", "first_publish_date"),
    ):
        prov_rows = rows(
            "SELECT provider,provider_id,source_payload FROM metadata_field_provenance "
            "WHERE entity_type='candidate' AND entity_id=? AND field='release_date'",
            (cid,),
        )
        assert len(prov_rows) == 1
        assert prov_rows[0]["provider"] == "openlibrary"
        assert prov_rows[0]["provider_id"] == expected_work
        payload = json.loads(prov_rows[0]["source_payload"])
        assert payload["field_provenance"]["source_field"] == expected_field

    items = [dict(cand_one), dict(cand_two)]
    with connect() as con:
        attach_publication_years(con, [], items)
    assert items[0]["first_publication_year"] == 1934
    assert items[0]["publication_work_id"] == "/works/OL471579W"
    assert items[1]["first_publication_year"] == 1935
    assert items[1]["publication_work_id"] == "/works/OL193500W"


def test_fetch_catalog_json_registers_trace_before_await_and_handles_json_edges():
    observed_mid_flight: list[dict[str, object]] = []

    async def run():
        async with httpx.AsyncClient() as client:
            trace: list[dict[str, object]] = []

            async def inspect_then_fail(req_client, url, *, params):
                assert req_client is client
                assert url == OPEN_LIBRARY_SEARCH
                assert params == {"title": "Probe"}
                observed_mid_flight.extend(dict(entry) for entry in trace)
                raise httpx.ConnectError("offline")

            failed = await _fetch_catalog_json(
                client,
                OPEN_LIBRARY_SEARCH,
                provider="openlibrary",
                query_kind="title_author",
                params={"title": "Probe"},
                trace=trace,
                request=inspect_then_fail,
            )
            assert failed is None
            assert len(observed_mid_flight) == 1
            assert observed_mid_flight[0]["status"] == "request_error"
            assert "http_status" not in observed_mid_flight[0]
            assert trace[0]["status"] == "request_error"
            assert trace[0]["error_type"] == "ConnectError"

        def transport_handler(request: httpx.Request) -> httpx.Response:
            mode = request.url.params.get("title") or request.url.params.get("q") or request.url.params.get("term")
            if "invalid-json" in str(mode):
                return httpx.Response(200, content=b"{not-valid-json")
            if "null-json" in str(mode):
                return httpx.Response(200, content=b"null")
            return httpx.Response(200, content=b"[]")

        async with httpx.AsyncClient(transport=httpx.MockTransport(transport_handler)) as mock_client:
            for provider_fn, expected_provider in (
                (_open_library_match, "openlibrary"),
                (_google_books_match, "google_books"),
                (_apple_books_match, "apple_books"),
            ):
                bad_trace: list[dict[str, object]] = []
                assert await provider_fn("invalid-json", "A Writer", mock_client, trace=bad_trace) is None
                assert bad_trace[0]["provider"] == expected_provider
                assert bad_trace[0]["http_status"] == 200
                assert bad_trace[0]["status"] == "invalid_json"
                assert "error_type" not in bad_trace[0]

                for non_dict_title in ("null-json", "list-json"):
                    empty_trace: list[dict[str, object]] = []
                    assert await provider_fn(non_dict_title, "A Writer", mock_client, trace=empty_trace) is None
                    assert empty_trace[0]["status"] == "no_match"
                    assert empty_trace[0]["matched_count"] == 0

    asyncio.run(run())


@pytest.mark.parametrize(
    ("provider_fn", "status_code", "expected_status"),
    [
        (_open_library_match, 429, "http_error"),
        (_open_library_match, 403, "http_error"),
        (_open_library_match, 503, "http_error"),
        (_google_books_match, 429, "rate_limited"),
        (_google_books_match, 403, "http_error"),
        (_google_books_match, 503, "http_error"),
        (_apple_books_match, 403, "rate_limited"),
        (_apple_books_match, 429, "rate_limited"),
        (_apple_books_match, 503, "http_error"),
    ],
)
def test_provider_match_rate_limit_status_sets(provider_fn, status_code, expected_status):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": "throttled"})

    async def run():
        trace: list[dict[str, object]] = []
        kwargs = {"google_books_api_key": "secret-google-key"} if provider_fn is _google_books_match else {}
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            result = await provider_fn("A Book", "A Writer", client, trace=trace, **kwargs)
        assert result is None
        assert len(trace) == 1
        assert trace[0]["http_status"] == status_code
        assert trace[0]["status"] == expected_status
        assert "secret-google-key" not in json.dumps(trace)

    asyncio.run(run())


def test_select_best_provider_match_four_tuple_ranking_and_ties():
    def candidate(
        provider_id: str,
        *,
        catalog_title: str = "A Book",
        description: str = "Short synopsis.",
        description_kind: str = "synopsis",
        genres: list[str] | None = None,
    ):
        return _provider_match(
            provider="openlibrary",
            provider_id=provider_id,
            work_id=provider_id,
            title="A Book",
            author="A Writer",
            catalog_title=catalog_title,
            catalog_author="A Writer",
            description=description,
            description_kind=description_kind,
            description_source_field="description" if description_kind == "synopsis" else "first_sentence",
            genres=genres or [],
            source_payload={"key": provider_id, "identifiers": ["9780307474278"]},
        )

    # 1. quality_score dominates synopsis/heuristic/genre advantages.
    trace_score: dict[str, object] = {}
    best_score = _select_best_provider_match(
        [
            candidate("lower-score", catalog_title="A Book Subtitle", description="Longer synopsis." * 20, genres=["Fiction", "Drama"]),
            candidate("exact-score", catalog_title="A Book", description="Brief.", description_kind="opening_sentence"),
        ],
        trace_score,
        isbn="9780307474278",
    )
    assert best_score is not None and best_score["provider_id"] == "exact-score"
    assert trace_score["identifier_verified"] is True

    # 2. description_kind == "synopsis" beats non-synopsis at equal quality_score.
    best_kind = _select_best_provider_match(
        [
            candidate("opening", description="Long opening sentence. " * 25, description_kind="opening_sentence", genres=["Fiction", "Drama"]),
            candidate("synopsis", description="Short synopsis.", description_kind="synopsis"),
        ],
        {},
    )
    assert best_kind is not None and best_kind["provider_id"] == "synopsis"

    # 3. description_content_quality_heuristic breaks synopsis ties before genre count.
    best_heuristic = _select_best_provider_match(
        [
            candidate("shorter-synopsis", description="Brief synopsis.", genres=["Fiction", "Drama", "Mystery"]),
            candidate("longer-synopsis", description="Detailed synopsis content. " * 10, genres=["Fiction"]),
        ],
        {},
    )
    assert best_heuristic is not None and best_heuristic["provider_id"] == "longer-synopsis"

    # 4. len(genres) breaks ties when score, kind, and heuristic match; strict > keeps first on full ties.
    tie_trace: dict[str, object] = {}
    best_tie = _select_best_provider_match(
        [
            candidate("fewer-genres", description="Identical synopsis.", genres=["Fiction"]),
            candidate("first-tied-winner", description="Identical synopsis.", genres=["Fiction", "Mystery"]),
            candidate("second-tied-loser", description="Identical synopsis.", genres=["History", "Drama"]),
        ],
        tie_trace,
    )
    assert best_tie is not None and best_tie["provider_id"] == "first-tied-winner"
    assert tie_trace["matched_count"] == 3
    assert best_tie["metadata_provenance"]["fetch_trace"] == [tie_trace]


def test_apple_books_trace_keys_and_early_guards():
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "NoMatchBook" in request.url.params.get("term", ""):
            return httpx.Response(200, json={"results": []})
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "trackId": 101,
                        "trackName": "Apple Match",
                        "artistName": "A Writer",
                        "description": "Same synopsis.",
                        "genres": ["Books", "Fiction"],
                    },
                    {
                        "trackId": 202,
                        "trackName": "Apple Match",
                        "artistName": "A Writer",
                        "description": "Same synopsis.",
                        "genres": ["Books", "Mystery"],
                    },
                ]
            },
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            guard_trace: list[dict[str, object]] = []
            assert await _apple_books_match("Apple Match", "A Writer", client, isbn="9780307474278", trace=guard_trace) is None
            assert await _apple_books_match("   ", "   ", client, trace=guard_trace) is None
            assert guard_trace == []
            assert calls == []

            matched_trace: list[dict[str, object]] = []
            match = await _apple_books_match("Apple Match", "A Writer", client, media="audiobook", trace=matched_trace)
            assert match is not None
            assert match["provider_id"] == "101"
            assert set(matched_trace[0].keys()) == {
                "provider",
                "fetched_at",
                "query_kind",
                "status",
                "http_status",
                "matched_count",
                "provider_id",
                "available_fields",
            }
            assert matched_trace[0]["query_kind"] == "title_author_audiobook"
            assert matched_trace[0]["status"] == "matched"
            assert matched_trace[0]["matched_count"] == 2
            assert match["metadata_provenance"]["fetch_trace"] == [matched_trace[0]]

            no_match_trace: list[dict[str, object]] = []
            assert await _apple_books_match("NoMatchBook", "A Writer", client, trace=no_match_trace) is None
            assert set(no_match_trace[0].keys()) == {
                "provider",
                "fetched_at",
                "query_kind",
                "status",
                "http_status",
                "matched_count",
            }
            assert no_match_trace[0]["status"] == "no_match"
            assert no_match_trace[0]["matched_count"] == 0

    asyncio.run(run())
