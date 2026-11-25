import asyncio
import json
from pathlib import Path
from datetime import datetime, timezone

import httpx
import pytest
import respx

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
    assert QUALITY_VERSION == "candidate-quality-v6"
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
            "provider='openlibrary',work_id='/works/OL123W',isbn13='9780306406157',"
            "isbn_verified=1 "
            "WHERE candidate_id=?",
            (same_work,),
        )
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',quality_score=0.8,"
            "isbn13='9780306406157',isbn_verified=1 WHERE candidate_id=?",
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
    with transaction() as con:
        con.execute("DELETE FROM candidates")
    candidate_id = add_candidate("Coming soon", "Unknown author")
    result = asyncio.run(audit_candidates(only_pending=False))
    assert result["audited"] == 1
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
    assert quality["work_id"] == "6758952748"
    assert "catalog_primary_unavailable" in json.loads(quality["flags_json"])
    assert candidate["release_date"] == "2026-10-06"
    assert "600x600bb" in candidate["cover_url"]


@respx.mock
def test_google_books_override_preserves_openlibrary_work_id_for_read_overlap_dedupe(database):
    from afterword_engine.quality import _dedupe_and_hide

    candidate_id = add_candidate("The Dispossessed", "Ursula K. Le Guin")
    with transaction() as con:
        con.execute("UPDATE sources SET enabled=1 WHERE url='builtin://upcoming'")
        con.execute(
            "UPDATE candidate_quality SET quality_status='pending',audit_version=? WHERE candidate_id=?",
            (QUALITY_VERSION, candidate_id),
        )

    respx.get(OPEN_LIBRARY_SEARCH).mock(
        return_value=httpx.Response(
            200,
            json={
                "docs": [
                    {
                        "key": "/works/OL59863W",
                        "title": "The Dispossessed",
                        "author_name": ["Ursula Le Guin"],
                        "first_publish_year": 1974,
                        "isbn": ["9780060512750"],
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
                        "id": "gb-vol-dispossessed",
                        "volumeInfo": {
                            "title": "The Dispossessed",
                            "authors": ["Ursula K. Le Guin"],
                            "description": "A physicist travels between twin worlds in search of a unified theory of time.",
                            "publishedDate": "2014-10-14",
                            "categories": ["Science Fiction"],
                            "industryIdentifiers": [
                                {"type": "ISBN_13", "identifier": "9780061054884"},
                            ],
                            "imageLinks": {
                                "thumbnail": "https://books.google.com/books/content?id=gb-vol-dispossessed"
                            },
                        },
                    }
                ]
            },
        )
    )

    result = asyncio.run(audit_candidates(only_pending=True))
    assert result["accepted"] == 1
    quality = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (candidate_id,))
    assert quality["provider"] == "google_books"
    assert quality["provider_id"] == "gb-vol-dispossessed"
    assert quality["work_id"] == ""
    assert quality["openlibrary_work_id"] == "/works/OL59863W"
    assert quality["isbn_verified"] == 0

    candidate = row("SELECT * FROM candidates WHERE id=?", (candidate_id,))
    assert candidate["release_date"] == "2014-10-14"

    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title,author,rating,source,openlibrary_work_id) "
            "VALUES('Les Dépossédés','U. Le Guin',5,'goodreads_csv','/works/OL59863W')"
        )
        deduped = _dedupe_and_hide(con)
    assert deduped == 1
    assert row("SELECT status FROM candidates WHERE id=?", (candidate_id,))["status"] == "rejected"
    assert "read_overlap" in json.loads(
        row("SELECT flags_json FROM candidate_quality WHERE candidate_id=?", (candidate_id,))["flags_json"]
    )


@respx.mock
def test_unverified_title_search_box_set_isbn_keeps_unread_sequels_eligible(database):
    box_set_isbn = "9780345538376"
    with transaction() as con:
        con.execute("UPDATE sources SET enabled=1 WHERE url='builtin://upcoming'")
        con.execute(
            "INSERT INTO reads(title,author,rating,isbn,source) VALUES(?,?,5,?,'goodreads_csv')",
            ("A Game of Thrones", "George R. R. Martin", box_set_isbn),
        )

    unread_sequel_id = add_candidate("A Clash of Kings", "George R. R. Martin")
    second_sequel_id = add_candidate("A Storm of Swords", "George R. R. Martin")
    verified_duplicate_id = add_candidate(
        "A Game of Thrones (Box Set Edition)",
        "George R. R. Martin",
        isbn13=box_set_isbn,
    )
    with transaction() as con:
        con.execute(
            "UPDATE candidate_quality SET quality_status='pending',audit_version=? "
            "WHERE candidate_id IN (?,?,?)",
            (QUALITY_VERSION, unread_sequel_id, second_sequel_id, verified_duplicate_id),
        )

    def ol_handler(request):
        isbn = request.url.params.get("isbn", "")
        title = request.url.params.get("title", "")
        if isbn == box_set_isbn:
            return httpx.Response(
                200,
                json={
                    "docs": [
                        {
                            "key": "/works/OL257943W",
                            "title": "A Game of Thrones",
                            "author_name": ["George R. R. Martin"],
                            "isbn": [box_set_isbn],
                        }
                    ]
                },
            )
        if "Clash" in title:
            return httpx.Response(
                200,
                json={
                    "docs": [
                        {
                            "key": "/works/OL257942W",
                            "title": "A Clash of Kings",
                            "author_name": ["George R. R. Martin"],
                            "isbn": [box_set_isbn],
                        }
                    ]
                },
            )
        if "Storm" in title:
            return httpx.Response(
                200,
                json={
                    "docs": [
                        {
                            "key": "/works/OL257958W",
                            "title": "A Storm of Swords",
                            "author_name": ["George R. R. Martin"],
                            "isbn": [box_set_isbn],
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"docs": []})

    respx.get(OPEN_LIBRARY_SEARCH).mock(side_effect=ol_handler)
    respx.get(GOOGLE_BOOKS_SEARCH).mock(return_value=httpx.Response(200, json={"items": []}))

    asyncio.run(audit_candidates(only_pending=True))

    clash_q = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (unread_sequel_id,))
    storm_q = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (second_sequel_id,))
    dup_q = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (verified_duplicate_id,))
    assert clash_q["isbn13"] == box_set_isbn
    assert clash_q["isbn_verified"] == 0
    assert clash_q["quality_status"] == "accepted"
    assert storm_q["isbn13"] == box_set_isbn
    assert storm_q["isbn_verified"] == 0
    assert storm_q["quality_status"] == "accepted"
    assert row("SELECT status FROM candidates WHERE id=?", (unread_sequel_id,))["status"] == "new"
    assert row("SELECT status FROM candidates WHERE id=?", (second_sequel_id,))["status"] == "new"

    assert dup_q["isbn_verified"] == 1
    assert dup_q["quality_status"] == "rejected"
    assert "read_overlap" in json.loads(dup_q["flags_json"])
    assert row("SELECT status FROM candidates WHERE id=?", (verified_duplicate_id,))["status"] == "rejected"


@respx.mock
def test_audit_rejects_non_equivalent_catalog_isbn10_pairing_for_source_isbn13(database):
    mismatched_id = add_candidate("Station Eleven", "Emily St. John Mandel", isbn13="9780804172448")
    equivalent_id = add_candidate("Sea of Tranquility", "Emily St. John Mandel", isbn13="9780143127550")
    with transaction() as con:
        con.execute("UPDATE sources SET enabled=1 WHERE url='builtin://upcoming'")
        con.execute(
            "UPDATE candidate_quality SET quality_status='pending',audit_version=? WHERE candidate_id IN (?,?)",
            (QUALITY_VERSION, mismatched_id, equivalent_id),
        )

    def ol_handler(request):
        isbn = request.url.params.get("isbn", "")
        if isbn == "9780804172448":
            return httpx.Response(
                200,
                json={
                    "docs": [
                        {
                            "key": "/works/OL17078986W",
                            "title": "Station Eleven",
                            "author_name": ["Emily St. John Mandel"],
                            "isbn": ["9780804172448", "0307474275"],
                        }
                    ]
                },
            )
        if isbn == "9780143127550":
            return httpx.Response(
                200,
                json={
                    "docs": [
                        {
                            "key": "/works/OL24636892W",
                            "title": "Sea of Tranquility",
                            "author_name": ["Emily St. John Mandel"],
                            "isbn": ["9780143127550", "0143127551"],
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"docs": []})

    respx.get(OPEN_LIBRARY_SEARCH).mock(side_effect=ol_handler)
    respx.get(GOOGLE_BOOKS_SEARCH).mock(return_value=httpx.Response(200, json={"items": []}))

    asyncio.run(audit_candidates(only_pending=True))
    mismatched_q = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (mismatched_id,))
    assert mismatched_q["isbn13"] == "9780804172448"
    assert mismatched_q["isbn10"] == ""
    assert mismatched_q["isbn_verified"] == 1

    equivalent_q = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (equivalent_id,))
    assert equivalent_q["isbn13"] == "9780143127550"
    assert equivalent_q["isbn10"] == "0143127551"
    assert equivalent_q["isbn_verified"] == 1


def test_bounded_v6_recovery_skips_already_verified_v4_v5_rows_with_work_id(database):
    legacy_google = add_candidate("Legacy Google Match", "A Writer")
    legacy_ol_verified_v4 = add_candidate("Legacy OL Verified v4", "A Writer", isbn13="9780307474278")
    legacy_ol_verified_v5 = add_candidate("Legacy OL Verified v5", "A Writer", isbn13="9780307474278")
    legacy_ol_no_isbn = add_candidate("Legacy OL Without ISBN", "A Writer")
    legacy_ol_unverified_isbn = add_candidate("Legacy OL Unverified Catalog ISBN", "A Writer")
    with transaction() as con:
        con.execute("UPDATE sources SET enabled=1 WHERE url='builtin://upcoming'")
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',audit_version='candidate-quality-v4',"
            "provider='google_books',provider_id='gb-legacy',work_id='gb-legacy',openlibrary_work_id='' "
            "WHERE candidate_id=?",
            (legacy_google,),
        )
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',audit_version='candidate-quality-v4',"
            "provider='openlibrary',provider_id='/works/OL999W',work_id='/works/OL999W',"
            "openlibrary_work_id='/works/OL999W',isbn13='9780307474278',isbn_verified=1 WHERE candidate_id=?",
            (legacy_ol_verified_v4,),
        )
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',audit_version='candidate-quality-v5',"
            "provider='openlibrary',provider_id='/works/OL998W',work_id='/works/OL998W',"
            "openlibrary_work_id='/works/OL998W',isbn13='9780307474278',isbn_verified=1 WHERE candidate_id=?",
            (legacy_ol_verified_v5,),
        )
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',audit_version='candidate-quality-v4',"
            "provider='openlibrary',provider_id='/works/OL997W',work_id='/works/OL997W',"
            "openlibrary_work_id='/works/OL997W',isbn13='',isbn10='',isbn_verified=0 WHERE candidate_id=?",
            (legacy_ol_no_isbn,),
        )
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',audit_version='candidate-quality-v4',"
            "provider='openlibrary',provider_id='/works/OL996W',work_id='/works/OL996W',"
            "openlibrary_work_id='/works/OL996W',isbn13='9780345538376',isbn_verified=0 WHERE candidate_id=?",
            (legacy_ol_unverified_isbn,),
        )

    selected_ids = {int(item["id"]) for item in candidate_quality_audit_candidates(limit=50)}
    assert legacy_google in selected_ids
    assert legacy_ol_unverified_isbn in selected_ids
    assert legacy_ol_verified_v4 not in selected_ids
    assert legacy_ol_verified_v5 not in selected_ids
    assert legacy_ol_no_isbn not in selected_ids


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


@respx.mock
def test_audit_matches_apple_books_audiobooks_and_filters_container_genres(database):
    sci_fi_id = add_candidate("The Ministry of Time (Unabridged)", "Kaliane Bradley")
    generic_genre_id = add_candidate("Orbital", "Samantha Harvey")
    with transaction() as con:
        con.execute("UPDATE sources SET enabled=1 WHERE url='builtin://upcoming'")
        con.execute(
            "UPDATE candidate_quality SET quality_status='pending',audit_version=? WHERE candidate_id IN (?,?)",
            (QUALITY_VERSION, sci_fi_id, generic_genre_id),
        )

    respx.get(OPEN_LIBRARY_SEARCH).mock(return_value=httpx.Response(200, json={"docs": []}))
    respx.get(GOOGLE_BOOKS_SEARCH).mock(return_value=httpx.Response(200, json={"items": []}))

    def apple_handler(request):
        media = request.url.params.get("media")
        term = request.url.params.get("term", "")
        if media == "ebook":
            return httpx.Response(200, json={"results": []})
        if "Ministry of Time" in term:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "wrapperType": "audiobook",
                            "collectionId": 1729854123,
                            "collectionName": "The Ministry of Time (Unabridged)",
                            "artistName": "Kaliane Bradley",
                            "description": "A time-travel romance and thriller.",
                            "primaryGenreName": "Sci-Fi & Fantasy",
                            "releaseDate": "2024-05-07T07:00:00Z",
                            "artworkUrl100": "https://is1-ssl.mzstatic.com/image/thumb/Music112/v4/aa/bb/cc/cover.jpg/100x100bb.jpg",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "wrapperType": "audiobook",
                        "collectionId": 1700009999,
                        "collectionName": "Orbital",
                        "artistName": "Samantha Harvey",
                        "description": "Six astronauts orbit Earth over twenty-four hours.",
                        "genres": ["Audiobooks", "Books"],
                        "primaryGenreName": "Audiobooks",
                        "releaseDate": "2023-11-09T08:00:00Z",
                    }
                ]
            },
        )

    respx.get(APPLE_BOOKS_SEARCH).mock(side_effect=apple_handler)

    result = asyncio.run(audit_candidates(only_pending=True))
    assert result["accepted"] == 2

    sci_fi_quality = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (sci_fi_id,))
    sci_fi_candidate = row("SELECT * FROM candidates WHERE id=?", (sci_fi_id,))
    assert sci_fi_quality["quality_status"] == "accepted"
    assert sci_fi_quality["provider"] == "apple_books"
    assert sci_fi_quality["provider_id"] == "1729854123"
    assert sci_fi_quality["work_id"] == "1729854123"
    assert json.loads(sci_fi_quality["flags_json"]) == []
    assert json.loads(sci_fi_candidate["genres"]) == ["Sci-Fi & Fantasy"]
    assert "600x600bb" in sci_fi_candidate["cover_url"]

    generic_quality = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (generic_genre_id,))
    generic_candidate = row("SELECT * FROM candidates WHERE id=?", (generic_genre_id,))
    assert generic_quality["work_id"] == "1700009999"
    assert json.loads(generic_candidate["genres"]) == []


@respx.mock
def test_audit_dedupes_cross_feed_duplicates_sharing_apple_books_work_id(database):
    ebook_feed_id = add_candidate("Glass Peninsula", "Maren Solis")
    audio_feed_id = add_candidate("Glass Peninsula: Special Audio Edition", "Maren Solis")
    with transaction() as con:
        con.execute("UPDATE sources SET enabled=1 WHERE url='builtin://upcoming'")
        con.execute(
            "UPDATE candidate_quality SET quality_status='pending',audit_version=? WHERE candidate_id IN (?,?)",
            (QUALITY_VERSION, ebook_feed_id, audio_feed_id),
        )

    respx.get(OPEN_LIBRARY_SEARCH).mock(return_value=httpx.Response(200, json={"docs": []}))
    respx.get(GOOGLE_BOOKS_SEARCH).mock(return_value=httpx.Response(429))
    respx.get(APPLE_BOOKS_SEARCH).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "collectionId": 1888777666,
                        "collectionName": "Glass Peninsula: Special Audio Edition",
                        "artistName": "Maren Solis",
                        "description": "An island mystery.",
                        "primaryGenreName": "Mystery & Thrillers",
                        "releaseDate": "2026-06-01T07:00:00Z",
                    },
                    {
                        "trackId": 1888777666,
                        "trackName": "Glass Peninsula",
                        "artistName": "Maren Solis",
                        "description": "An island mystery with full synopsis.",
                        "genres": ["Mysteries & Thrillers", "Books"],
                        "releaseDate": "2026-06-01T07:00:00Z",
                    },
                ]
            },
        )
    )

    result = asyncio.run(audit_candidates(only_pending=True))
    assert result["accepted"] == 2
    assert result["deduped"] >= 1
    ebook_cand = row("SELECT status FROM candidates WHERE id=?", (ebook_feed_id,))
    audio_cand = row("SELECT status FROM candidates WHERE id=?", (audio_feed_id,))
    ebook_q = row("SELECT quality_status,flags_json FROM candidate_quality WHERE candidate_id=?", (ebook_feed_id,))
    audio_q = row("SELECT quality_status,flags_json FROM candidate_quality WHERE candidate_id=?", (audio_feed_id,))
    assert ebook_cand["status"] == "new"
    assert ebook_q["quality_status"] == "accepted"
    assert audio_cand["status"] == "rejected"
    assert audio_q["quality_status"] == "rejected"
    assert json.loads(audio_q["flags_json"]) == ["duplicate_identity"]

    from afterword_engine.main import recommendation_list

    assert audio_feed_id not in {item["id"] for item in recommendation_list(status="decisions", limit=None)}


@respx.mock
def test_audit_skips_apple_books_for_isbn_when_primaries_healthy_but_falls_back_during_outage(database):
    healthy_isbn_id = add_candidate(
        "Healthy Primary Unmatched ISBN",
        "Cassandra Khaw",
        isbn13="9780307474278",
        isbn10="0307474275",
    )
    with transaction() as con:
        con.execute("UPDATE sources SET enabled=1 WHERE url='builtin://upcoming'")
        con.execute(
            "UPDATE candidate_quality SET quality_status='pending',audit_version=? WHERE candidate_id=?",
            (QUALITY_VERSION, healthy_isbn_id),
        )

    ol_route = respx.get(OPEN_LIBRARY_SEARCH).mock(return_value=httpx.Response(200, json={"docs": []}))
    gb_route = respx.get(GOOGLE_BOOKS_SEARCH).mock(return_value=httpx.Response(200, json={"items": []}))
    apple_route = respx.get(APPLE_BOOKS_SEARCH).mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "trackId": 6758952748,
                        "trackName": "Outage ISBN Candidate",
                        "artistName": "Cassandra Khaw",
                        "description": "Matched via Apple fallback during Google Books 429.",
                    }
                ]
            },
        )
    )

    healthy_result = asyncio.run(audit_candidates(only_pending=True))
    healthy_quality = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (healthy_isbn_id,))
    assert healthy_result["quarantine"] == 1
    assert len(apple_route.calls) == 0
    assert healthy_quality["quality_status"] == "quarantine"
    assert json.loads(healthy_quality["flags_json"]) == ["catalog_unmatched"]

    outage_isbn_id = add_candidate(
        "Outage ISBN Candidate",
        "Cassandra Khaw",
        isbn13="9780061120084",
        isbn10="0061120081",
    )
    with transaction() as con:
        con.execute(
            "UPDATE candidate_quality SET quality_status='pending',audit_version=? WHERE candidate_id=?",
            (QUALITY_VERSION, outage_isbn_id),
        )
    ol_route.mock(return_value=httpx.Response(200, json={"docs": []}))
    gb_route.mock(return_value=httpx.Response(429))

    outage_result = asyncio.run(audit_candidates(only_pending=True))
    outage_quality = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (outage_isbn_id,))
    assert outage_result["accepted"] == 1
    assert len(apple_route.calls) == 1
    assert outage_quality["quality_status"] == "accepted"
    assert outage_quality["provider"] == "apple_books"
    assert outage_quality["provider_id"] == "6758952748"
    assert outage_quality["work_id"] == "6758952748"
    assert outage_quality["isbn13"] == "9780061120084"
    assert outage_quality["isbn10"] == "0061120081"
    assert "catalog_primary_unavailable" in json.loads(outage_quality["flags_json"])


@respx.mock
def test_audit_classifies_apple_books_429_and_503_as_retryable_outages(database):
    first_429_id = add_candidate("Apple Rate Limited One", "Author One")
    second_429_id = add_candidate("Apple Rate Limited Two", "Author Two")
    with transaction() as con:
        con.execute("UPDATE sources SET enabled=1 WHERE url='builtin://upcoming'")
        con.execute(
            "UPDATE candidate_quality SET quality_status='pending',audit_version=? WHERE candidate_id IN (?,?)",
            (QUALITY_VERSION, first_429_id, second_429_id),
        )

    respx.get(OPEN_LIBRARY_SEARCH).mock(return_value=httpx.Response(200, json={"docs": []}))
    respx.get(GOOGLE_BOOKS_SEARCH).mock(return_value=httpx.Response(200, json={"items": []}))
    apple_route = respx.get(APPLE_BOOKS_SEARCH).mock(return_value=httpx.Response(429))

    result = asyncio.run(audit_candidates(only_pending=True))
    assert result["quarantine"] == 2
    assert len(apple_route.calls) == 1
    for cid in (first_429_id, second_429_id):
        flags = json.loads(row("SELECT flags_json FROM candidate_quality WHERE candidate_id=?", (cid,))["flags_json"])
        assert "catalog_provider_unavailable" in flags
        assert "catalog_provider_rate_limited" in flags
        assert "catalog_unmatched" not in flags

    service_503_id = add_candidate("Apple Unavailable Three", "Author Three")
    with transaction() as con:
        con.execute(
            "UPDATE candidate_quality SET quality_status='pending',audit_version=? WHERE candidate_id=?",
            (QUALITY_VERSION, service_503_id),
        )
    apple_route.mock(return_value=httpx.Response(503))

    result_503 = asyncio.run(audit_candidates(only_pending=True))
    assert result_503["audited"] == 1
    assert result_503["quarantine"] == 1
    flags_503 = json.loads(
        row("SELECT flags_json FROM candidate_quality WHERE candidate_id=?", (service_503_id,))["flags_json"]
    )
    assert flags_503 == ["catalog_provider_unavailable"]

    assert candidate_quality_audit_candidates(limit=50) == []
    with transaction() as con:
        con.execute(
            "UPDATE candidate_quality SET audited_at='2000-01-01 00:00:00' WHERE candidate_id IN (?,?,?)",
            (first_429_id, second_429_id, service_503_id),
        )
    due_ids = {int(item["id"]) for item in candidate_quality_audit_candidates(limit=50)}
    assert {first_429_id, second_429_id, service_503_id} <= due_ids


@respx.mock
def test_apple_books_outage_fallback_backfills_primary_identifiers_after_recovery(database):
    backfill_id = add_candidate("Recoverable Primary Work", "Clara Vance")
    apple_only_id = add_candidate("Apple Exclusive Audiobook", "Devon Hart")
    legacy_apple_id = add_candidate("Legacy Empty Work ID", "Elena Rostova")
    with transaction() as con:
        con.execute("UPDATE sources SET enabled=1 WHERE url='builtin://upcoming'")
        con.execute(
            "UPDATE candidate_quality SET quality_status='pending',audit_version=? WHERE candidate_id IN (?,?)",
            (QUALITY_VERSION, backfill_id, apple_only_id),
        )
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',provider='apple_books',"
            "provider_id='555444333',work_id='',audit_version=?,audited_at='2026-10-01T00:00:00+00:00' "
            "WHERE candidate_id=?",
            (QUALITY_VERSION, legacy_apple_id),
        )

    ol_route = respx.get(OPEN_LIBRARY_SEARCH).mock(return_value=httpx.Response(200, json={"docs": []}))
    gb_route = respx.get(GOOGLE_BOOKS_SEARCH).mock(return_value=httpx.Response(503))

    def apple_initial(request):
        term = request.url.params.get("term", "")
        media = request.url.params.get("media", "")
        if "Recoverable Primary Work" in term and media == "ebook":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "trackId": 111222333,
                            "trackName": "Recoverable Primary Work",
                            "artistName": "Clara Vance",
                            "description": "Apple fallback description.",
                            "genres": ["Fiction", "Books"],
                            "releaseDate": "2026-04-14T07:00:00Z",
                        }
                    ]
                },
            )
        if "Apple Exclusive Audiobook" in term and media == "audiobook":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "collectionId": 999888777,
                            "collectionName": "Apple Exclusive Audiobook",
                            "artistName": "Devon Hart",
                            "primaryGenreName": "Mystery & Thrillers",
                        }
                    ]
                },
            )
        if "Legacy Empty Work ID" in term and media == "ebook":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "trackId": 555444333,
                            "trackName": "Legacy Empty Work ID",
                            "artistName": "Elena Rostova",
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"results": []})

    respx.get(APPLE_BOOKS_SEARCH).mock(side_effect=apple_initial)

    first_run = asyncio.run(audit_candidates(only_pending=True))
    assert first_run["accepted"] == 3
    for cid in (backfill_id, apple_only_id):
        q_row = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (cid,))
        assert q_row["quality_status"] == "accepted"
        assert q_row["provider"] == "apple_books"
        assert "catalog_primary_unavailable" in json.loads(q_row["flags_json"])

    # Not eligible before CATALOG_PROVIDER_RETRY_HOURS.
    assert candidate_quality_audit_candidates(limit=50) == []

    # Advance past retry window and bring Open Library / Google Books back online.
    with transaction() as con:
        con.execute(
            "UPDATE candidate_quality SET audited_at='2000-01-01 00:00:00' WHERE candidate_id IN (?,?,?)",
            (backfill_id, apple_only_id, legacy_apple_id),
        )

    def ol_recovered(request):
        title = request.url.params.get("title", "")
        if "Recoverable Primary Work" in title:
            return httpx.Response(
                200,
                json={
                    "docs": [
                        {
                            "key": "/works/OL987654W",
                            "title": "Recoverable Primary Work",
                            "author_name": ["Clara Vance"],
                            "isbn": ["0307474275"],
                            "first_publish_year": 2026,
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"docs": []})

    ol_route.mock(side_effect=ol_recovered)
    gb_route.mock(return_value=httpx.Response(200, json={"items": []}))

    due_ids = {int(item["id"]) for item in candidate_quality_audit_candidates(limit=50)}
    assert {backfill_id, apple_only_id} <= due_ids

    recovery_run = asyncio.run(audit_candidates(only_pending=True))
    assert recovery_run["accepted"] >= 2

    recovered_q = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (backfill_id,))
    assert recovered_q["provider"] == "openlibrary"
    assert recovered_q["work_id"] == "/works/OL987654W"
    assert recovered_q["isbn13"] == "9780307474278"
    assert recovered_q["isbn10"] == "0307474275"
    assert json.loads(recovered_q["flags_json"]) == []

    apple_only_q = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (apple_only_id,))
    assert apple_only_q["provider"] == "apple_books"
    assert apple_only_q["work_id"] == "999888777"
    assert json.loads(apple_only_q["flags_json"]) == []

    legacy_q = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (legacy_apple_id,))
    assert legacy_q["work_id"] == "555444333"

    # Even if audited_at is aged again, cleared flags and populated work_id prevent repeat lookups.
    with transaction() as con:
        con.execute(
            "UPDATE candidate_quality SET audited_at='2000-01-01 00:00:00' WHERE candidate_id IN (?,?,?)",
            (backfill_id, apple_only_id, legacy_apple_id),
        )
    assert candidate_quality_audit_candidates(limit=50) == []


@respx.mock
def test_reaudit_keeps_accepted_status_when_apple_books_has_transient_outage(database):
    from afterword_engine.main import recommendation_list

    with transaction() as con:
        con.execute("UPDATE candidates SET status='rejected'")
        con.execute("UPDATE sources SET enabled=1 WHERE url='builtin://upcoming'")

    primary_outage_id = add_candidate(
        "Transient Reaudit Primary Outage",
        "Clara Vance",
        status="recommended",
    )
    legacy_empty_work_id = add_candidate(
        "Transient Reaudit Legacy Apple",
        "Elena Rostova",
        status="recommended",
    )
    with transaction() as con:
        con.execute("UPDATE candidates SET score=92 WHERE id IN (?,?)", (primary_outage_id, legacy_empty_work_id))
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',quality_score=0.94,"
            "provider='apple_books',provider_id='111222333',work_id='111222333',"
            "title_match=1.0,author_match=1.0,flags_json=?,audit_version=?,"
            "audited_at='2000-01-01 00:00:00' WHERE candidate_id=?",
            ('["catalog_primary_unavailable"]', QUALITY_VERSION, primary_outage_id),
        )
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',quality_score=0.91,"
            "provider='apple_books',provider_id='555444333',work_id='',"
            "title_match=0.95,author_match=1.0,flags_json='[]',audit_version=?,"
            "audited_at='2000-01-01 00:00:00' WHERE candidate_id=?",
            (QUALITY_VERSION, legacy_empty_work_id),
        )

    respx.get(OPEN_LIBRARY_SEARCH).mock(return_value=httpx.Response(200, json={"docs": []}))
    respx.get(GOOGLE_BOOKS_SEARCH).mock(return_value=httpx.Response(200, json={"items": []}))
    apple_route = respx.get(APPLE_BOOKS_SEARCH).mock(return_value=httpx.Response(503))

    outage_reaudit = asyncio.run(audit_candidates(only_pending=True))
    assert outage_reaudit["audited"] == 2
    assert outage_reaudit["accepted"] == 2
    assert outage_reaudit["quarantine"] == 0

    primary_q = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (primary_outage_id,))
    assert primary_q["quality_status"] == "accepted"
    assert primary_q["quality_score"] == pytest.approx(0.94)
    assert primary_q["provider"] == "apple_books"
    assert primary_q["provider_id"] == "111222333"
    assert primary_q["work_id"] == "111222333"
    assert set(json.loads(primary_q["flags_json"])) == {
        "catalog_primary_unavailable",
        "catalog_provider_unavailable",
    }

    legacy_q = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (legacy_empty_work_id,))
    assert legacy_q["quality_status"] == "accepted"
    assert legacy_q["quality_score"] == pytest.approx(0.91)
    assert legacy_q["provider"] == "apple_books"
    assert legacy_q["provider_id"] == "555444333"
    assert legacy_q["work_id"] == ""
    assert json.loads(legacy_q["flags_json"]) == ["catalog_provider_unavailable"]

    visible_ids = {item["id"] for item in recommendation_list(status="recommended", limit=None)}
    assert {primary_outage_id, legacy_empty_work_id} <= visible_ids
    assert candidate_quality_audit_candidates(limit=50) == []

    # Also verify HTTP 429 rate-limit during re-audit keeps accepted status, then recovers cleanly.
    with transaction() as con:
        con.execute(
            "UPDATE candidate_quality SET audited_at='2000-01-01 00:00:00' WHERE candidate_id IN (?,?)",
            (primary_outage_id, legacy_empty_work_id),
        )
    apple_route.mock(return_value=httpx.Response(429))

    rate_limit_reaudit = asyncio.run(audit_candidates(only_pending=True))
    assert rate_limit_reaudit["accepted"] == 2
    assert rate_limit_reaudit["quarantine"] == 0
    for cid in (primary_outage_id, legacy_empty_work_id):
        q_row = row("SELECT quality_status,flags_json FROM candidate_quality WHERE candidate_id=?", (cid,))
        assert q_row["quality_status"] == "accepted"
        assert "catalog_provider_rate_limited" in json.loads(q_row["flags_json"])

    with transaction() as con:
        con.execute(
            "UPDATE candidate_quality SET audited_at='2000-01-01 00:00:00' WHERE candidate_id IN (?,?)",
            (primary_outage_id, legacy_empty_work_id),
        )

    def apple_recovered(request):
        term = request.url.params.get("term", "")
        if "Primary Outage" in term:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "trackId": 111222333,
                            "trackName": "Transient Reaudit Primary Outage",
                            "artistName": "Clara Vance",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "trackId": 555444333,
                        "trackName": "Transient Reaudit Legacy Apple",
                        "artistName": "Elena Rostova",
                    }
                ]
            },
        )

    apple_route.mock(side_effect=apple_recovered)
    recovered = asyncio.run(audit_candidates(only_pending=True))
    assert recovered["accepted"] == 2
    for cid, expected_work_id in (
        (primary_outage_id, "111222333"),
        (legacy_empty_work_id, "555444333"),
    ):
        q_row = row("SELECT quality_status,work_id,flags_json FROM candidate_quality WHERE candidate_id=?", (cid,))
        assert q_row["quality_status"] == "accepted"
        assert q_row["work_id"] == expected_work_id
        assert json.loads(q_row["flags_json"]) == []


@respx.mock
def test_reaudit_keeps_accepted_status_on_weak_primary_match_plus_apple_503(database):
    from afterword_engine.main import recommendation_list

    with transaction() as con:
        con.execute("UPDATE candidates SET status='rejected'")
        con.execute("UPDATE sources SET enabled=1 WHERE url='builtin://upcoming'")

    unrelated_author_id = add_candidate(
        "Weak Primary Match Book",
        "Clara Vance",
        status="recommended",
    )
    surname_only_id = add_candidate(
        "Shared Surname Echoes",
        "Clara Vance",
        status="recommended",
    )
    with transaction() as con:
        con.execute(
            "UPDATE candidates SET score=93,description='Verified Apple synopsis.',"
            "genres='[\"Literary Fiction\"]' WHERE id IN (?,?)",
            (unrelated_author_id, surname_only_id),
        )
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',quality_score=0.96,"
            "provider='apple_books',provider_id='777888999',work_id='777888999',"
            "title_match=1.0,author_match=1.0,flags_json=?,audit_version=?,"
            "audited_at='2000-01-01 00:00:00' WHERE candidate_id=?",
            ('["catalog_primary_unavailable"]', QUALITY_VERSION, unrelated_author_id),
        )
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',quality_score=0.92,"
            "provider='apple_books',provider_id='888999000',work_id='',"
            "title_match=0.96,author_match=1.0,flags_json='[]',audit_version=?,"
            "audited_at='2000-01-01 00:00:00' WHERE candidate_id=?",
            (QUALITY_VERSION, surname_only_id),
        )

    def ol_weak_primary(request):
        title = request.url.params.get("title", "")
        if "Weak Primary Match Book" in title:
            # Non-accepted primary match: exact title, completely different author (quality_score=0.65 < 0.82).
            return httpx.Response(
                200,
                json={
                    "docs": [
                        {
                            "key": "/works/OLWEAK1W",
                            "title": "Weak Primary Match Book",
                            "author_name": ["Unrelated Author"],
                            "isbn": ["0306406152"],
                            "description": "Wrong book synopsis that must not overwrite candidate.",
                            "subject": ["Cooking"],
                        }
                    ]
                },
            )
        # Non-accepted primary match with surname-only author overlap (title_match=1.0, author_match=0.55 < 0.65,
        # quality_score=0.8425 >= 0.82): still non-accepted and must not quarantine on Apple 503.
        return httpx.Response(
            200,
            json={
                "docs": [
                    {
                        "key": "/works/OLWEAK2W",
                        "title": "Shared Surname Echoes",
                        "author_name": ["Robert Vance"],
                        "isbn": ["0306406152"],
                        "description": "Wrong surname-only synopsis.",
                        "subject": ["Gardening"],
                    }
                ]
            },
        )

    ol_route = respx.get(OPEN_LIBRARY_SEARCH).mock(side_effect=ol_weak_primary)
    respx.get(GOOGLE_BOOKS_SEARCH).mock(return_value=httpx.Response(200, json={"items": []}))
    apple_route = respx.get(APPLE_BOOKS_SEARCH).mock(return_value=httpx.Response(503))

    reaudit_503 = asyncio.run(audit_candidates(only_pending=True))
    assert len(ol_route.calls) == 2
    assert len(apple_route.calls) == 4  # ebook + audiobook for both candidates
    assert reaudit_503["audited"] == 2
    assert reaudit_503["accepted"] == 2
    assert reaudit_503["quarantine"] == 0

    first_q = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (unrelated_author_id,))
    assert first_q["quality_status"] == "accepted"
    assert first_q["quality_score"] == pytest.approx(0.96)
    assert first_q["provider"] == "apple_books"
    assert first_q["provider_id"] == "777888999"
    assert first_q["work_id"] == "777888999"
    assert first_q["isbn13"] == ""
    assert set(json.loads(first_q["flags_json"])) == {
        "catalog_primary_unavailable",
        "catalog_provider_unavailable",
    }

    second_q = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (surname_only_id,))
    assert second_q["quality_status"] == "accepted"
    assert second_q["quality_score"] == pytest.approx(0.92)
    assert second_q["provider"] == "apple_books"
    assert second_q["provider_id"] == "888999000"
    assert second_q["work_id"] == ""
    assert second_q["isbn13"] == ""
    assert json.loads(second_q["flags_json"]) == ["catalog_provider_unavailable"]

    for cid in (unrelated_author_id, surname_only_id):
        cand = row("SELECT description,genres FROM candidates WHERE id=?", (cid,))
        assert cand["description"] == "Verified Apple synopsis."
        assert json.loads(cand["genres"]) == ["Literary Fiction"]

    visible_ids = {item["id"] for item in recommendation_list(status="recommended", limit=None)}
    assert {unrelated_author_id, surname_only_id} <= visible_ids
    assert candidate_quality_audit_candidates(limit=50) == []

    # Also verify weak primary match + Apple 429 keeps accepted status, and recovery promotes/backfills cleanly.
    with transaction() as con:
        con.execute(
            "UPDATE candidate_quality SET audited_at='2000-01-01 00:00:00' WHERE candidate_id IN (?,?)",
            (unrelated_author_id, surname_only_id),
        )
    apple_route.mock(return_value=httpx.Response(429))

    reaudit_429 = asyncio.run(audit_candidates(only_pending=True))
    assert reaudit_429["accepted"] == 2
    assert reaudit_429["quarantine"] == 0
    for cid in (unrelated_author_id, surname_only_id):
        q_row = row("SELECT quality_status,flags_json FROM candidate_quality WHERE candidate_id=?", (cid,))
        assert q_row["quality_status"] == "accepted"
        assert "catalog_provider_rate_limited" in json.loads(q_row["flags_json"])

    # When Apple recovers with an accepted match (even if its score is slightly lower than the surname-only
    # non-accepted primary match's 0.8425), resolve_catalog_match chooses the verified accepted record.
    with transaction() as con:
        con.execute(
            "UPDATE candidate_quality SET audited_at='2000-01-01 00:00:00' WHERE candidate_id IN (?,?)",
            (unrelated_author_id, surname_only_id),
        )

    def apple_recovered(request):
        term = request.url.params.get("term", "")
        if "Weak Primary Match Book" in term:
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "trackId": 777888999,
                            "trackName": "Weak Primary Match Book",
                            "artistName": "Clara Vance",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "trackId": 888999000,
                        "trackName": "Shared Surname Echoes: A Novel",
                        "artistName": "C. Vance",
                    }
                ]
            },
        )

    apple_route.mock(side_effect=apple_recovered)
    recovered = asyncio.run(audit_candidates(only_pending=True))
    assert recovered["accepted"] == 2
    assert recovered["quarantine"] == 0
    for cid, expected_work_id in (
        (unrelated_author_id, "777888999"),
        (surname_only_id, "888999000"),
    ):
        q_row = row("SELECT quality_status,provider,work_id,flags_json FROM candidate_quality WHERE candidate_id=?", (cid,))
        assert q_row["quality_status"] == "accepted"
        assert q_row["provider"] == "apple_books"
        assert q_row["work_id"] == expected_work_id
        assert json.loads(q_row["flags_json"]) == []


def test_dedupe_and_hide_reject_candidates_handles_apple_work_keys_and_preserves_winner(database):
    from afterword_engine.main import recommendation_list

    with transaction() as con:
        con.execute("UPDATE candidates SET status='rejected'")
        con.execute("UPDATE sources SET enabled=1 WHERE url='builtin://upcoming'")

    imported_id = add_candidate("Saltwater House", "Nora Bell", status="imported")
    shortlist_dup_id = add_candidate(
        "Saltwater House: Collector's Edition",
        "Nora Bell",
        status="recommended",
    )
    winner_id = add_candidate("Cedar Ridge", "Maren Solis", status="recommended")
    loser_id = add_candidate(
        "Cedar Ridge: Deluxe Audio Edition",
        "Maren Solis",
        status="recommended",
    )
    read_dup_id = add_candidate("Already Finished Apple Book", "Devon Hart", status="recommended")

    with transaction() as con:
        con.execute(
            "INSERT INTO reads(title,author,rating,source) VALUES(?,?,5,'test')",
            ("Already Finished Apple Book", "Devon Hart"),
        )
        con.execute("UPDATE candidates SET score=70, description='Full synopsis.' WHERE id=?", (winner_id,))
        con.execute("UPDATE candidates SET score=99, description='' WHERE id=?", (loser_id,))
        con.execute("UPDATE candidates SET score=95 WHERE id IN (?,?)", (shortlist_dup_id, read_dup_id))
        for cid, pid, wid, qscore in (
            (imported_id, "900100200", "900100200", 0.90),
            (shortlist_dup_id, "900100200", "900100200", 0.90),
            (winner_id, "900100300", "900100300", 0.95),
            (loser_id, "900100300", "", 0.88),
            (read_dup_id, "900100400", "900100400", 0.92),
        ):
            con.execute(
                "UPDATE candidate_quality SET quality_status='accepted',quality_score=?,"
                "provider='apple_books',provider_id=?,work_id=? WHERE candidate_id=?",
                (qscore, pid, wid, cid),
            )

        # Both connection-backed and default rows()-backed serving filter consistently before dedupe
        # (serving orders by c.score DESC, so loser_id leads until _dedupe_and_hide promotes quality_score/synopsis).
        conn_visible = [item["id"] for item in recommendation_list(con, status="recommended", limit=None)]
    rows_visible = [item["id"] for item in recommendation_list(status="recommended", limit=None)]
    assert conn_visible == [loser_id]
    assert rows_visible == [loser_id]

    with transaction() as con:
        changed = _dedupe_and_hide(con)
        conn_after_dedupe = [item["id"] for item in recommendation_list(con, status="recommended", limit=None)]
    assert changed == 3
    assert conn_after_dedupe == [winner_id]
    assert [item["id"] for item in recommendation_list(status="recommended", limit=None)] == [winner_id]

    for cid, expected_flag in (
        (read_dup_id, "read_overlap"),
        (shortlist_dup_id, "shortlisted_overlap"),
        (loser_id, "duplicate_identity"),
    ):
        cand_row = row("SELECT status FROM candidates WHERE id=?", (cid,))
        qual_row = row("SELECT quality_status,flags_json FROM candidate_quality WHERE candidate_id=?", (cid,))
        assert cand_row["status"] == "rejected"
        assert qual_row["quality_status"] == "rejected"
        assert json.loads(qual_row["flags_json"]) == [expected_flag]

    winner_cand = row("SELECT status FROM candidates WHERE id=?", (winner_id,))
    winner_qual = row("SELECT quality_status FROM candidate_quality WHERE candidate_id=?", (winner_id,))
    assert winner_cand["status"] == "recommended"
    assert winner_qual["quality_status"] == "accepted"
    assert recommendation_list(status="decisions", limit=None) == []