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
    CATALOG_QUARANTINE_RETRY_DAYS,
    QUALITY_VERSION,
    _apple_books_match,
    _author_similarity,
    _dedupe_and_hide,
    _fetch_catalog_json,
    _google_books_match,
    _open_library_match,
    _provider_match,
    _select_best_provider_match,
    _store_result,
    audit_candidates,
    candidate_quality_audit_candidates,
    canonical_isbn,
    has_candidate_quality_audit_candidates,
    local_flags,
)
from afterword_engine.scoring import score_all


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
    other_stamp_catalog_accepted = add_candidate("Other Stamp Catalog Accepted Without ISBN", "A Writer")
    v4_no_isbn_catalog_accepted = add_candidate("V4 Catalog Accepted Without ISBN", "A Writer")
    v4_no_isbn_unattributed = add_candidate("V4 Unattributed Accepted Without ISBN", "A Writer")
    v4_no_isbn_quarantine = add_candidate("V4 Quarantine Without ISBN", "A Writer")
    expired_v4_no_isbn_quarantine = add_candidate("Expired V4 Quarantine Without ISBN", "A Writer")
    expired_current_quarantine = add_candidate("Expired Current Quarantine", "A Writer")
    fresh_current_quarantine = add_candidate("Fresh Current Quarantine", "A Writer")
    invalid_isbn = add_candidate("Invalid ISBN", "A Writer", isbn13="9780307474279")
    current_accepted = add_candidate("Current Accepted Without ISBN", "A Writer")
    curated = add_candidate("Curated Seed", "A Writer", isbn13="9780307474278")
    expired_outage = add_candidate("Expired Outage", "A Writer")
    expired_accepted_outage = add_candidate("Expired Accepted Outage", "A Writer")
    fresh_outage = add_candidate("Fresh Outage", "A Writer", isbn13="9780307474278")
    fresh_accepted_outage = add_candidate("Fresh Accepted Outage", "A Writer")
    pending = add_candidate("Pending Without ISBN", "A Writer")
    legacy_pending = add_candidate("Legacy Pending Without ISBN", "A Writer")
    reference_time = datetime(2026, 10, 2, tzinfo=timezone.utc)

    with transaction() as con:
        for candidate_id, status, version in (
            (old_accepted, "accepted", "candidate-quality-v3"),
            (old_quarantine, "quarantine", "candidate-quality-v2"),
            (quality_isbn_accepted, "accepted", "candidate-quality-v3"),
            (quality_isbn_quarantine, "quarantine", "candidate-quality-v2"),
            (other_stamp_catalog_accepted, "accepted", "candidate-quality-v6"),
            (v4_no_isbn_catalog_accepted, "accepted", "candidate-quality-v4"),
            (v4_no_isbn_unattributed, "accepted", "candidate-quality-v4"),
            (v4_no_isbn_quarantine, "quarantine", "candidate-quality-v4"),
            (expired_v4_no_isbn_quarantine, "quarantine", "candidate-quality-v4"),
            (expired_current_quarantine, "quarantine", QUALITY_VERSION),
            (fresh_current_quarantine, "quarantine", QUALITY_VERSION),
            (invalid_isbn, "accepted", "candidate-quality-v3"),
            (current_accepted, "accepted", QUALITY_VERSION),
            (curated, "accepted", "builtin-curated-v1"),
            (expired_outage, "quarantine", QUALITY_VERSION),
            (expired_accepted_outage, "accepted", QUALITY_VERSION),
            (fresh_outage, "quarantine", QUALITY_VERSION),
            (fresh_accepted_outage, "accepted", QUALITY_VERSION),
            (pending, "pending", QUALITY_VERSION),
            (legacy_pending, "accepted", "legacy-pending-audit-v1"),
        ):
            con.execute(
                "UPDATE candidate_quality SET quality_status=?,audit_version=? WHERE candidate_id=?",
                (status, version, candidate_id),
            )
        for cat_cid, work_id in (
            (other_stamp_catalog_accepted, "/works/OLV6W"),
            (v4_no_isbn_catalog_accepted, "/works/OLV4W"),
        ):
            con.execute(
                "UPDATE candidate_quality SET provider='openlibrary',provider_id=? WHERE candidate_id=?",
                (work_id, cat_cid),
            )
        for fresh_quar_cid in (v4_no_isbn_quarantine, fresh_current_quarantine):
            con.execute(
                "UPDATE candidate_quality SET flags_json=?,audited_at=? WHERE candidate_id=?",
                ('["catalog_unmatched"]', "2026-09-25 12:00:00", fresh_quar_cid),
            )
        for expired_quar_cid in (expired_v4_no_isbn_quarantine, expired_current_quarantine):
            con.execute(
                "UPDATE candidate_quality SET flags_json=?,audited_at=? WHERE candidate_id=?",
                ('["catalog_unmatched"]', "2026-09-10 12:00:00", expired_quar_cid),
            )
        for outage_cid in (expired_outage, expired_accepted_outage):
            con.execute(
                "UPDATE candidate_quality SET flags_json=?,audited_at=? WHERE candidate_id=?",
                ('["catalog_provider_unavailable"]', "2026-09-30T22:00:00+00:00", outage_cid),
            )
        for fresh_cid in (fresh_outage, fresh_accepted_outage):
            con.execute(
                "UPDATE candidate_quality SET flags_json=?,audited_at=? WHERE candidate_id=?",
                ('["catalog_provider_unavailable"]', "2026-10-01T12:00:00+00:00", fresh_cid),
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
        other_stamp_catalog_accepted,
        v4_no_isbn_catalog_accepted,
        expired_v4_no_isbn_quarantine,
        expired_current_quarantine,
        expired_outage,
        expired_accepted_outage,
        pending,
        legacy_pending,
    } <= selected_ids
    selected_by_id = {int(item["id"]): item for item in selected}
    assert selected_by_id[quality_isbn_accepted]["isbn13"] == ""
    assert selected_by_id[quality_isbn_quarantine]["isbn10"] == ""
    assert invalid_isbn not in selected_ids
    assert v4_no_isbn_unattributed not in selected_ids
    assert v4_no_isbn_quarantine not in selected_ids
    assert fresh_current_quarantine not in selected_ids
    assert current_accepted not in selected_ids
    assert curated not in selected_ids
    assert fresh_outage not in selected_ids
    assert fresh_accepted_outage not in selected_ids

    manual_retry_ids = {
        int(item["id"])
        for item in candidate_quality_audit_candidates(
            limit=50, now=reference_time, include_quarantined=True
        )
    }
    assert {v4_no_isbn_quarantine, fresh_current_quarantine} <= manual_retry_ids
    assert curated not in manual_retry_ids
    assert invalid_isbn not in manual_retry_ids
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
def test_audit_populates_missing_work_dates_and_preserves_source_and_mixed_provenance(database):
    from afterword_engine.database import connect, persist_metadata_field_provenance
    from afterword_engine.publication_year_metadata import attach_publication_years

    undated_id = add_candidate("Classic Undated Reissue", "Agatha Christie")
    source_kind_id = add_candidate("Classic Source Date", "Dorothy L. Sayers")
    mixed_prov_id = add_candidate("Classic Mixed Provenance", "Ngaio Marsh")
    manual_id = add_candidate("Manual Classic", "Margery Allingham")

    with transaction() as con:
        source_id = con.execute("SELECT id FROM sources WHERE url='builtin://upcoming'").fetchone()[0]
        con.execute(
            "UPDATE candidates SET release_date='2023-12-25',date_kind='source' WHERE id=?",
            (source_kind_id,),
        )
        persist_metadata_field_provenance(
            con,
            "candidate",
            source_kind_id,
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
        # Mixed provenance set on release_date (source + google_books), with
        # empty description so _store_result runs the selected_fields write path.
        con.execute(
            "UPDATE candidates SET description='',release_date='2020-05-10',date_kind='day' WHERE id=?",
            (mixed_prov_id,),
        )
        for prov_name, prov_id, kind in (
            ("source", str(source_id), "source_item"),
            ("google_books", "gb-mixed-1", "day"),
        ):
            persist_metadata_field_provenance(
                con,
                "candidate",
                mixed_prov_id,
                "release_date",
                {
                    "metadata_provenance": {
                        "fields": {
                            "release_date": {
                                "provider": prov_name,
                                "provider_id": prov_id,
                                "kind": kind,
                            }
                        },
                        "fetch_trace": [],
                        "source_payloads": [],
                    }
                },
                default_provider=prov_name,
                default_provider_id=prov_id,
            )
        con.execute(
            "UPDATE candidates SET release_date='1999-01-01',date_kind='manual' WHERE id=?",
            (manual_id,),
        )

    def openlibrary_side_effect(request):
        title = request.url.params.get("title", "")
        if "Undated" in title:
            return httpx.Response(200, json={"docs": [{
                "key": "/works/OL471579W",
                "title": "Classic Undated Reissue",
                "author_name": ["Agatha Christie"],
                "first_publish_year": 1934,
                "description": "Original 1934 mystery novel.",
                "subject": ["Mystery"],
            }]})
        if "Source Date" in title:
            return httpx.Response(200, json={"docs": [{
                "key": "/works/OL193500W",
                "title": "Classic Source Date",
                "author_name": ["Dorothy L. Sayers"],
                "first_publish_date": "1935-10-01",
                "description": "Original 1935 mystery novel.",
                "subject": ["Mystery"],
            }]})
        if "Mixed" in title:
            return httpx.Response(200, json={"docs": [{
                "key": "/works/OL193700W",
                "title": "Classic Mixed Provenance",
                "author_name": ["Ngaio Marsh"],
                "first_publish_year": 1937,
                "description": "Original 1937 mystery novel.",
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
                "title": "Classic Undated Reissue",
                "authors": ["Agatha Christie"],
                "publishedDate": "2023-12-25",
            },
        }]})
    )
    respx.get(APPLE_BOOKS_SEARCH).mock(return_value=httpx.Response(200, json={"results": []}))

    asyncio.run(audit_candidates(only_pending=False))

    cand_undated = row("SELECT id,release_date,date_kind FROM candidates WHERE id=?", (undated_id,))
    cand_source = row("SELECT id,release_date,date_kind FROM candidates WHERE id=?", (source_kind_id,))
    cand_mixed = row("SELECT id,description,release_date,date_kind FROM candidates WHERE id=?", (mixed_prov_id,))
    cand_manual = row("SELECT id,release_date,date_kind FROM candidates WHERE id=?", (manual_id,))

    assert cand_undated["release_date"] == "1934-01-01"
    assert cand_undated["date_kind"] == "year"
    assert cand_source["release_date"] == "2023-12-25"
    assert cand_source["date_kind"] == "source"
    assert cand_mixed["description"] == "Original 1937 mystery novel."
    assert cand_mixed["release_date"] == "2020-05-10"
    assert cand_mixed["date_kind"] == "day"
    assert cand_manual["release_date"] == "1999-01-01"
    assert cand_manual["date_kind"] == "manual"

    undated_prov = rows(
        "SELECT provider,provider_id,source_payload FROM metadata_field_provenance "
        "WHERE entity_type='candidate' AND entity_id=? AND field='release_date'",
        (undated_id,),
    )
    assert len(undated_prov) == 1
    assert undated_prov[0]["provider"] == "openlibrary"
    assert undated_prov[0]["provider_id"] == "/works/OL471579W"
    assert json.loads(undated_prov[0]["source_payload"])["field_provenance"]["source_field"] == "first_publish_year"

    source_prov = rows(
        "SELECT provider FROM metadata_field_provenance "
        "WHERE entity_type='candidate' AND entity_id=? AND field='release_date'",
        (source_kind_id,),
    )
    assert [item["provider"] for item in source_prov] == ["source"]

    mixed_prov = rows(
        "SELECT provider,provider_id FROM metadata_field_provenance "
        "WHERE entity_type='candidate' AND entity_id=? AND field='release_date' ORDER BY provider",
        (mixed_prov_id,),
    )
    assert [(item["provider"], item["provider_id"]) for item in mixed_prov] == [
        ("google_books", "gb-mixed-1"),
        ("source", str(source_id)),
    ]
    assert {item["provider"] for item in mixed_prov} == {"google_books", "source"}

    # Both undated candidates (via release_date + OL provenance) and candidates
    # that gained Open Library description provenance resolve verified work years
    # without rewriting existing source release_date values, activating the
    # batch ranker's publication-era gate (ranking.py).
    from types import SimpleNamespace
    from afterword_engine import publication_era_signal
    from afterword_engine.ranking import rank_candidates

    items = [
        {**dict(cand_undated), "title": "Classic Undated Reissue", "author": "Agatha Christie"},
        {**dict(cand_mixed), "title": "Classic Mixed Provenance", "author": "Ngaio Marsh"},
    ]
    with connect() as con:
        attach_publication_years(con, [], items)
    assert items[0]["first_publication_year"] == 1934
    assert items[0]["publication_work_id"] == "/works/OL471579W"
    assert items[1]["first_publication_year"] == 1937
    assert items[1]["publication_work_id"] == "/works/OL193700W"

    reads = [{"id": 1, "title": "Modern Thriller", "author": "Contemporary Writer", "rating": 5}]
    vectors = [[1.0, 0.0]]
    cand_vectors = [[1.0, 0.0], [1.0, 0.0]]
    baseline = rank_candidates(reads, vectors, items, cand_vectors, publication_era=False)
    orig_fit = publication_era_signal.fit_publication_era_signal
    publication_era_signal.fit_publication_era_signal = lambda *_: SimpleNamespace(
        adjustment=lambda year: -1.5 if year and year < 1950 else 0.0,
        diagnostics={"enabled": True, "reason": "validated"},
    )
    try:
        era_ranked = rank_candidates(reads, vectors, items, cand_vectors)
    finally:
        publication_era_signal.fit_publication_era_signal = orig_fit
    assert [entry["score"] for entry in era_ranked] == [
        round(entry["score"] - 1.5, 1) for entry in baseline
    ]


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


@respx.mock
def test_total_provider_outage_on_prior_version_accepted_preserves_status_and_metadata(database):
    candidate_id = add_candidate(
        "Outage Resilient Book",
        "A Writer",
        isbn13="9780307474278",
        status="recommended",
    )
    with transaction() as con:
        con.execute("UPDATE candidates SET status='rejected' WHERE id!=?", (candidate_id,))
        con.execute(
            "UPDATE candidates SET description=?,cover_url=?,release_date=?,date_kind=?,genres=? WHERE id=?",
            (
                "Verified v4 synopsis that must survive an outage.",
                "https://covers.openlibrary.org/b/id/777-L.jpg",
                "2021-05-04",
                "day",
                json.dumps(["Literary fiction"]),
                candidate_id,
            ),
        )
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',quality_score=1.0,"
            "audit_version='candidate-quality-v4',audited_at='2026-09-01 00:00:00',"
            "provider='openlibrary',provider_id='/works/OLPRIORW',work_id='/works/OLPRIORW',"
            "isbn13='9780307474278',metadata_provider='openlibrary',metadata_provider_id='/works/OLPRIORW' "
            "WHERE candidate_id=?",
            (candidate_id,),
        )
        for field, payload in (
            ("description", {"payload": {"description": "Verified v4 synopsis that must survive an outage."}}),
            ("cover_url", {"payload": {"cover_id": "777"}}),
            ("release_date", {"payload": {"publication_date": "2021-05-04"}}),
            ("genres", {"payload": {"genres": ["Literary fiction"]}}),
        ):
            con.execute(
                "INSERT INTO metadata_field_provenance(entity_type,entity_id,field,provider,provider_id,confidence,source_payload) "
                "VALUES('candidate',?,?,'openlibrary','/works/OLPRIORW',1.0,?)",
                (candidate_id, field, json.dumps(payload)),
            )

    respx.get(OPEN_LIBRARY_SEARCH).mock(return_value=httpx.Response(503, json={"error": "unavailable"}))
    respx.get(GOOGLE_BOOKS_SEARCH).mock(return_value=httpx.Response(503, json={"error": "unavailable"}))
    respx.get(APPLE_BOOKS_SEARCH).mock(return_value=httpx.Response(503, json={"error": "unavailable"}))

    result = asyncio.run(audit_candidates(only_pending=True))
    assert result["audited"] == 1
    assert result["accepted"] == 1
    assert result["quarantine"] == 0

    after_candidate = row("SELECT * FROM candidates WHERE id=?", (candidate_id,))
    after_quality = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (candidate_id,))
    assert after_quality["quality_status"] == "accepted"
    assert after_quality["provider"] == "openlibrary"
    assert after_quality["work_id"] == "/works/OLPRIORW"
    assert after_quality["audit_version"] == QUALITY_VERSION
    assert "catalog_provider_unavailable" in json.loads(after_quality["flags_json"])
    assert after_candidate["description"] == "Verified v4 synopsis that must survive an outage."
    assert after_candidate["cover_url"] == "https://covers.openlibrary.org/b/id/777-L.jpg"
    assert after_candidate["release_date"] == "2021-05-04"
    assert json.loads(after_candidate["genres"]) == ["Literary fiction"]
    assert len(
        rows(
            "SELECT field FROM metadata_field_provenance WHERE entity_type='candidate' AND entity_id=?",
            (candidate_id,),
        )
    ) == 4

    # Not immediately retried before backoff expires, but retried after 24h.
    assert candidate_quality_audit_candidates(limit=10) == []
    with transaction() as con:
        con.execute(
            "UPDATE candidate_quality SET audited_at='2000-01-01 00:00:00' WHERE candidate_id=?",
            (candidate_id,),
        )
    assert [int(item["id"]) for item in candidate_quality_audit_candidates(limit=10)] == [candidate_id]


@respx.mock
def test_partial_outage_sparse_accepted_match_preserves_prior_fields_and_reaudits_after_backoff(database):
    candidate_id = add_candidate(
        "Partial Outage Book",
        "A Writer",
        isbn13="9780307474278",
        status="recommended",
    )
    with transaction() as con:
        con.execute("UPDATE candidates SET status='rejected' WHERE id!=?", (candidate_id,))
        con.execute(
            "UPDATE candidates SET description=?,cover_url=?,release_date=?,date_kind=?,genres=? WHERE id=?",
            (
                "Rich Open Library synopsis from v4.",
                "https://covers.openlibrary.org/b/id/888-L.jpg",
                "2020-08-18",
                "day",
                json.dumps(["Science fiction", "Space opera"]),
                candidate_id,
            ),
        )
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',quality_score=1.0,"
            "audit_version='candidate-quality-v4',audited_at='2026-09-01 00:00:00',"
            "provider='openlibrary',provider_id='/works/OLRICH1W',work_id='/works/OLRICH1W',"
            "isbn13='9780307474278',metadata_provider='openlibrary',metadata_provider_id='/works/OLRICH1W' "
            "WHERE candidate_id=?",
            (candidate_id,),
        )
        for field, payload in (
            ("description", {"payload": {"description": "Rich Open Library synopsis from v4."}}),
            ("cover_url", {"payload": {"cover_id": "888"}}),
            ("release_date", {"payload": {"publication_date": "2020-08-18"}}),
            ("genres", {"payload": {"genres": ["Science fiction", "Space opera"]}}),
        ):
            con.execute(
                "INSERT INTO metadata_field_provenance(entity_type,entity_id,field,provider,provider_id,confidence,source_payload) "
                "VALUES('candidate',?,?,'openlibrary','/works/OLRICH1W',1.0,?)",
                (candidate_id, field, json.dumps(payload)),
            )

    ol_healthy = [False]

    def ol_handler(_request):
        if not ol_healthy[0]:
            raise httpx.ReadTimeout("Open Library timed out")
        return httpx.Response(
            200,
            json={
                "docs": [
                    {
                        "key": "/works/OLRICH1W",
                        "title": "Partial Outage Book",
                        "author_name": ["A Writer"],
                        "isbn": ["9780307474278"],
                        "description": "Updated verified v5 Open Library synopsis after recovery.",
                        "cover_i": 999,
                        "first_publish_date": "2020-08-18",
                        "subject": ["Science fiction", "Hard science fiction"],
                        "language": ["eng"],
                    }
                ]
            },
        )

    def gb_sparse_handler(_request):
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "gb-sparse-vol",
                        "volumeInfo": {
                            "title": "Partial Outage Book",
                            "authors": ["A Writer"],
                            "industryIdentifiers": [
                                {"type": "ISBN_13", "identifier": "9780307474278"}
                            ],
                            "language": "en",
                        },
                    }
                ]
            },
        )

    respx.get(OPEN_LIBRARY_SEARCH).mock(side_effect=ol_handler)
    respx.get(GOOGLE_BOOKS_SEARCH).mock(side_effect=gb_sparse_handler)

    # 1. During Open Library timeout, Google Books returns an accepted sparse record.
    first = asyncio.run(audit_candidates(only_pending=True))
    assert first["audited"] == 1
    assert first["accepted"] == 1
    assert first["enriched"] == 0

    during_outage_candidate = row("SELECT * FROM candidates WHERE id=?", (candidate_id,))
    during_outage_quality = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (candidate_id,))
    assert during_outage_quality["quality_status"] == "accepted"
    assert during_outage_quality["audit_version"] == QUALITY_VERSION
    assert "catalog_provider_unavailable" in json.loads(during_outage_quality["flags_json"])
    assert during_outage_candidate["description"] == "Rich Open Library synopsis from v4."
    assert during_outage_candidate["cover_url"] == "https://covers.openlibrary.org/b/id/888-L.jpg"
    assert during_outage_candidate["release_date"] == "2020-08-18"
    assert json.loads(during_outage_candidate["genres"]) == ["Science fiction", "Space opera"]
    assert {
        item["field"]
        for item in rows(
            "SELECT field FROM metadata_field_provenance WHERE entity_type='candidate' AND entity_id=?",
            (candidate_id,),
        )
    } == {"description", "cover_url", "release_date", "genres"}

    # 2. Not re-selected before the 24h provider backoff expires.
    assert candidate_quality_audit_candidates(limit=10) == []

    # 3. After 24h backoff expires, the v5 accepted row IS re-audited, refreshes metadata,
    #    and clears catalog_provider_unavailable so it does not loop forever.
    with transaction() as con:
        con.execute(
            "UPDATE candidate_quality SET audited_at='2000-01-01 00:00:00' WHERE candidate_id=?",
            (candidate_id,),
        )
    assert [int(item["id"]) for item in candidate_quality_audit_candidates(limit=10)] == [candidate_id]

    ol_healthy[0] = True
    recovered = asyncio.run(audit_candidates(only_pending=True))
    assert recovered["audited"] == 1
    assert recovered["accepted"] == 1
    assert recovered["enriched"] == 1

    after_recovery_candidate = row("SELECT * FROM candidates WHERE id=?", (candidate_id,))
    after_recovery_quality = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (candidate_id,))
    assert after_recovery_quality["quality_status"] == "accepted"
    assert json.loads(after_recovery_quality["flags_json"]) == []
    assert after_recovery_candidate["description"] == "Updated verified v5 Open Library synopsis after recovery."
    assert after_recovery_candidate["cover_url"] == "https://covers.openlibrary.org/b/id/999-L.jpg"
    assert json.loads(after_recovery_candidate["genres"]) == ["Science fiction", "Hard science fiction"]
    assert candidate_quality_audit_candidates(limit=10) == []


@respx.mock
def test_unmatched_and_ambiguous_quarantine_preserve_catalog_fields_unlike_explicit_pollution(database):
    unmatched_id = add_candidate("Obscure Small Press Novel", "Clara Vance", status="saved")
    ambiguous_id = add_candidate("Collected Essays", "Clara Vance", status="saved")
    polluted_id = add_candidate("Starlight Atlas", "Clara Vance", status="saved")
    with transaction() as con:
        con.execute(
            "UPDATE candidates SET status='rejected' WHERE id NOT IN (?,?,?)",
            (unmatched_id, ambiguous_id, polluted_id),
        )
        for cid in (unmatched_id, ambiguous_id, polluted_id):
            con.execute(
                "UPDATE candidates SET description=?,cover_url=?,release_date=?,date_kind=?,genres=? WHERE id=?",
                (
                    f"Existing catalog description for {cid}.",
                    f"https://covers.openlibrary.org/b/id/{cid}00-L.jpg",
                    "2019-03-15",
                    "day",
                    json.dumps(["Essays"]),
                    cid,
                ),
            )
            con.execute(
                "UPDATE candidate_quality SET quality_status='accepted',audit_version='candidate-quality-v4',"
                "provider='openlibrary',provider_id='/works/OLPRIORW',work_id='/works/OLPRIORW',"
                "metadata_provider='openlibrary',metadata_provider_id='/works/OLPRIORW' WHERE candidate_id=?",
                (cid,),
            )
            for field, payload in (
                ("description", {"payload": {"description": f"Existing catalog description for {cid}."}}),
                ("cover_url", {"payload": {"cover_id": f"{cid}00"}}),
                ("release_date", {"payload": {"publication_date": "2019-03-15"}}),
                ("genres", {"payload": {"genres": ["Essays"]}}),
            ):
                con.execute(
                    "INSERT INTO metadata_field_provenance(entity_type,entity_id,field,provider,provider_id,confidence,source_payload) "
                    "VALUES('candidate',?,?,'openlibrary','/works/OLPRIORW',0.9,?)",
                    (cid, field, json.dumps(payload)),
                )

    def ol_handler(request):
        title = request.url.params.get("title", "")
        if "Obscure Small Press" in title:
            return httpx.Response(200, json={"docs": []})
        if "Collected Essays" in title:
            return httpx.Response(
                200,
                json={
                    "docs": [
                        {
                            "key": "/works/OLSUBTITLEW",
                            "title": "Collected Essays on Modern Architecture and Urban Memory",
                            "author_name": ["Clara Vance"],
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
                        "key": "/works/OLGUIDE99W",
                        "title": "Study Guide: Starlight Atlas",
                        "author_name": ["Clara Vance"],
                        "language": ["eng"],
                    }
                ]
            },
        )

    respx.get(OPEN_LIBRARY_SEARCH).mock(side_effect=ol_handler)
    respx.get(GOOGLE_BOOKS_SEARCH).mock(return_value=httpx.Response(200, json={"items": []}))
    respx.get(APPLE_BOOKS_SEARCH).mock(return_value=httpx.Response(200, json={"results": []}))

    outcome = asyncio.run(audit_candidates(only_pending=True))
    assert outcome["quarantine"] == 3

    for preserved_cid, expected_flag in (
        (unmatched_id, "catalog_unmatched"),
        (ambiguous_id, "catalog_match_ambiguous"),
    ):
        cand = row("SELECT * FROM candidates WHERE id=?", (preserved_cid,))
        qual = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (preserved_cid,))
        flags = json.loads(qual["flags_json"])
        assert qual["quality_status"] == "quarantine"
        assert expected_flag in flags
        assert cand["description"] == f"Existing catalog description for {preserved_cid}."
        assert cand["cover_url"] == f"https://covers.openlibrary.org/b/id/{preserved_cid}00-L.jpg"
        assert cand["release_date"] == "2019-03-15"
        assert json.loads(cand["genres"]) == ["Essays"]
        assert len(
            rows(
                "SELECT field FROM metadata_field_provenance WHERE entity_type='candidate' AND entity_id=?",
                (preserved_cid,),
            )
        ) == 4

    polluted_cand = row("SELECT * FROM candidates WHERE id=?", (polluted_id,))
    polluted_qual = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (polluted_id,))
    assert "catalog_derivative_edition" in json.loads(polluted_qual["flags_json"])
    assert polluted_cand["description"] == ""
    assert polluted_cand["cover_url"] == ""
    assert polluted_cand["release_date"] is None
    assert json.loads(polluted_cand["genres"]) == []
    assert rows(
        "SELECT field FROM metadata_field_provenance WHERE entity_type='candidate' AND entity_id=?",
        (polluted_id,),
    ) == []


@respx.mock
def test_legacy_description_without_provenance_is_preserved_when_cover_came_from_catalog(database):
    sparse_accepted_id = add_candidate("Legacy Blurb Book", "Marilynne Robinson", isbn13="9780307474278")
    polluted_homonym_id = add_candidate("Home", "Marilynne Robinson")
    with transaction() as con:
        con.execute(
            "UPDATE candidates SET status='rejected' WHERE id NOT IN (?,?)",
            (sparse_accepted_id, polluted_homonym_id),
        )
        for cid in (sparse_accepted_id, polluted_homonym_id):
            con.execute(
                "UPDATE candidates SET description=?,cover_url=? WHERE id=?",
                (
                    "Source-scraped description saved before provenance tracking.",
                    "https://covers.openlibrary.org/b/id/555-L.jpg",
                    cid,
                ),
            )
            con.execute(
                "UPDATE candidate_quality SET quality_status='accepted',audit_version='candidate-quality-v4',"
                "provider='openlibrary',provider_id='/works/OLLEGACYCOV',work_id='/works/OLLEGACYCOV',"
                "metadata_provider='openlibrary',metadata_provider_id='/works/OLLEGACYCOV' WHERE candidate_id=?",
                (cid,),
            )
            # Only cover_url has catalog provenance; description has NO provenance row.
            con.execute(
                "INSERT INTO metadata_field_provenance(entity_type,entity_id,field,provider,provider_id,confidence,source_payload) "
                "VALUES('candidate',?,'cover_url','openlibrary','/works/OLLEGACYCOV',0.95,'{}')",
                (cid,),
            )

    def ol_handler(request):
        isbn = request.url.params.get("isbn", "")
        title = request.url.params.get("title", "")
        if isbn == "9780307474278" or "Legacy Blurb Book" in title:
            return httpx.Response(
                200,
                json={
                    "docs": [
                        {
                            "key": "/works/OLLEGACYCOV",
                            "title": "Legacy Blurb Book",
                            "author_name": ["Marilynne Robinson"],
                            "isbn": ["9780307474278"],
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
                        "key": "/works/OLHOMONYM9W",
                        "title": "Home",
                        "author_name": ["Michael Robinson"],
                        "description": "Wrong homonym blurb.",
                        "language": ["eng"],
                    }
                ]
            },
        )

    respx.get(OPEN_LIBRARY_SEARCH).mock(side_effect=ol_handler)
    respx.get(GOOGLE_BOOKS_SEARCH).mock(return_value=httpx.Response(200, json={"items": []}))
    respx.get(APPLE_BOOKS_SEARCH).mock(return_value=httpx.Response(200, json={"results": []}))

    result = asyncio.run(audit_candidates(only_pending=True))
    assert result["accepted"] == 1
    assert result["quarantine"] == 1

    accepted_after = row("SELECT description,cover_url FROM candidates WHERE id=?", (sparse_accepted_id,))
    assert accepted_after["description"] == "Source-scraped description saved before provenance tracking."
    assert accepted_after["cover_url"] == ""

    homonym_after = row("SELECT description,cover_url FROM candidates WHERE id=?", (polluted_homonym_id,))
    assert homonym_after["description"] == "Source-scraped description saved before provenance tracking."
    assert homonym_after["cover_url"] == ""


@respx.mock
def test_audit_count_only_dry_run_time_budget_progress_and_endpoints(database, monkeypatch):
    from afterword_engine import main as engine_main
    from afterword_engine import quality as quality_mod

    def valid_isbn13(idx: int) -> str:
        prefix = f"978030747{idx:03d}"
        checksum = (10 - sum(int(ch) * (1 if pos % 2 == 0 else 3) for pos, ch in enumerate(prefix)) % 10) % 10
        return f"{prefix}{checksum}"

    cids = []
    isbn_to_idx: dict[str, int] = {}
    with transaction() as con:
        con.execute("UPDATE candidates SET status='rejected'")
    for idx in range(15):
        isbn = valid_isbn13(idx)
        isbn_to_idx[isbn] = idx
        cid = add_candidate(f"Timed Book {idx}", "A Writer", isbn13=isbn)
        cids.append(cid)
    already_current_cid = add_candidate("Already Current Accepted", "A Writer")
    curated_cid = add_candidate("Curated Accepted", "A Writer")
    with transaction() as con:
        for idx, cid in enumerate(cids):
            con.execute(
                "UPDATE candidate_quality SET quality_status='accepted',audit_version='candidate-quality-v4',"
                "provider='openlibrary',provider_id=?,work_id=? WHERE candidate_id=?",
                (f"/works/OLOLD{idx}W", f"OLOLD{idx}W", cid),
            )
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',audit_version=?,"
            "provider='openlibrary',provider_id='/works/OLCURW',work_id='OLCURW' WHERE candidate_id=?",
            (QUALITY_VERSION, already_current_cid),
        )
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',audit_version='builtin-curated-v1' WHERE candidate_id=?",
            (curated_cid,),
        )

    # 1. count_only makes zero HTTP requests, mutates nothing, and reports due rows even when only_pending=False.
    def ol_handler(request: httpx.Request) -> httpx.Response:
        isbn = request.url.params.get("isbn", "")
        idx = isbn_to_idx.get(isbn, 0)
        return httpx.Response(
            200,
            json={
                "docs": [
                    {
                        "key": f"/works/OLTIMED{idx}W",
                        "title": f"Timed Book {idx}",
                        "author_name": ["A Writer"],
                        "isbn": [isbn or valid_isbn13(idx)],
                        "description": "Fresh synopsis.",
                        "language": ["eng"],
                    }
                ]
            },
        )

    ol_route = respx.get(OPEN_LIBRARY_SEARCH).mock(side_effect=ol_handler)
    respx.get(GOOGLE_BOOKS_SEARCH).mock(return_value=httpx.Response(200, json={"items": []}))
    respx.get(APPLE_BOOKS_SEARCH).mock(return_value=httpx.Response(200, json={"results": []}))

    count_report = asyncio.run(engine_main.candidate_audit(payload=None, dry_run=False, count_only=True, only_pending=True, limit=10))
    assert count_report["count_only"] is True
    assert count_report["due"] == 15
    assert count_report["selected"] == 10
    assert count_report["remaining"] == 5
    assert count_report["breakdown"] == {"prior_version_isbn": 15}
    assert len(ol_route.calls) == 0

    full_count_report = asyncio.run(
        engine_main.candidate_audit(payload=None, dry_run=False, count_only=True, only_pending=False)
    )
    assert full_count_report["count_only"] is True
    assert full_count_report["only_pending"] is False
    assert full_count_report["due"] == 15
    assert full_count_report["selected"] == 15
    assert full_count_report["remaining"] == 0
    assert len(ol_route.calls) == 0

    summary = engine_main.candidate_quality()
    assert summary["due"] == 15
    assert summary["due_breakdown"] == {"prior_version_isbn": 15}
    assert summary["quarantine_retry_days"] == CATALOG_QUARANTINE_RETRY_DAYS

    # 2. dry_run previews enrichments without mutating SQLite rows.
    dry_report = asyncio.run(
        engine_main.candidate_audit(payload=None, dry_run=True, count_only=False, only_pending=True, limit=5)
    )
    assert dry_report["dry_run"] is True
    assert dry_report["audited"] == 5
    assert dry_report["enriched"] == 0
    assert dry_report["would_enrich"] >= 1
    assert row("SELECT description FROM candidates WHERE id=?", (cids[0],))["description"] == ""
    assert row("SELECT audit_version FROM candidate_quality WHERE candidate_id=?", (cids[0],))["audit_version"] == "candidate-quality-v4"

    # 3. Wall-clock max_duration_seconds bounds sub-batches and updates running job progress.
    with transaction() as con:
        con.execute(
            "INSERT INTO jobs(id,kind,status,progress) VALUES('job-progress-1','candidate_quality_recovery','running',0)"
        )
    fake_now = [100.0]

    def fake_monotonic():
        return fake_now[0]

    monkeypatch.setattr(quality_mod, "_audit_monotonic", fake_monotonic)
    progress_events: list[dict[str, object]] = []

    def on_progress(snapshot):
        progress_events.append(snapshot)
        fake_now[0] += 35.0

    bounded = asyncio.run(
        audit_candidates(
            only_pending=True,
            limit=15,
            max_duration_seconds=30.0,
            progress_callback=on_progress,
        )
    )
    assert bounded["audited"] == quality_mod.QUALITY_AUDIT_SUBBATCH_SIZE
    assert bounded["timed_out"] is True
    assert bounded["remaining"] == 5
    assert len(progress_events) == 1
    job_progress = row("SELECT progress FROM jobs WHERE id='job-progress-1'")["progress"]
    assert 0.6 <= job_progress < 1.0

    # 4. Continuous recovery chain defers immediate followup once chain budget is exhausted.
    chain_clock = [500.0]
    monkeypatch.setattr(engine_main, "_recovery_clock", lambda: chain_clock[0])
    engine_main._set_quality_recovery_settings(
        **{
            engine_main.QUALITY_RECOVERY_CHAIN_STARTED_AT_KEY: "",
            engine_main.QUALITY_RECOVERY_CHAIN_DEFERRED_UNTIL_KEY: "",
            engine_main.QUALITY_RETRY_QUARANTINED_BEFORE_KEY: "",
        }
    )

    async def fast_score(*_args, **_kwargs):
        return 0

    async def fake_bounded_audit(*, only_pending=False, limit=None):
        assert only_pending is True
        chain_clock[0] += engine_main.QUALITY_RECOVERY_CHAIN_BUDGET_SECONDS + 5.0
        return {"audited": 1, "accepted": 1, "quarantine": 0, "rejected": 0, "enriched": 0}

    monkeypatch.setattr(engine_main, "score_all", fast_score)
    monkeypatch.setattr(engine_main, "audit_candidates", fake_bounded_audit)
    with transaction() as con:
        con.execute("DELETE FROM jobs WHERE kind='candidate_quality_recovery'")

    recovery_outcome = asyncio.run(engine_main.handle_job("candidate_quality_recovery"))
    assert recovery_outcome["followup_deferred"] is True
    assert row("SELECT COUNT(*) AS count FROM jobs WHERE kind='candidate_quality_recovery' AND status='queued'")["count"] == 0

    # While still inside the cooldown window, the periodic scheduler will not immediately re-queue.
    monkeypatch.setattr(engine_main, "candidate_metadata_refresh_remaining", lambda: 0)
    monkeypatch.setattr(engine_main, "read_metadata_refresh_remaining", lambda: 0)
    assert "candidate_quality_recovery" not in engine_main._queue_due_enrichment_retries()

    # Once the cooldown window elapses, the periodic scheduler starts a fresh bounded recovery window.
    chain_clock[0] += engine_main.ENRICHMENT_RETRY_POLL_SECONDS + 1.0
    requeued = engine_main._queue_due_enrichment_retries()
    assert "candidate_quality_recovery" in requeued


@respx.mock
def test_partial_outage_non_empty_surviving_description_preserves_existing_description(database):
    existing_cid = add_candidate("Home", "Marilynne Robinson", isbn13="9780312428549")
    empty_cid = add_candidate("Lila", "Marilynne Robinson", isbn13="9780374187613")
    with transaction() as con:
        con.execute(
            "UPDATE candidates SET status='rejected' WHERE id NOT IN (?,?)",
            (existing_cid, empty_cid),
        )
        con.execute(
            "UPDATE candidates SET description=?,cover_url=?,release_date=?,date_kind='published',genres=? WHERE id=?",
            (
                "Rich existing Open Library synopsis that must survive partial outage.",
                "https://covers.openlibrary.org/b/id/901-L.jpg",
                "2008-09-02",
                '["Literary fiction"]',
                existing_cid,
            ),
        )
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',audit_version='candidate-quality-v4',"
            "provider='openlibrary',provider_id='/works/OLHOME1W',work_id='/works/OLHOME1W',"
            "isbn10='0312428545',metadata_provider='openlibrary',metadata_provider_id='/works/OLHOME1W' WHERE candidate_id=?",
            (existing_cid,),
        )
        for field in ("description", "cover_url", "release_date", "genres"):
            con.execute(
                "INSERT INTO metadata_field_provenance(entity_type,entity_id,field,provider,provider_id,confidence,source_payload) "
                "VALUES('candidate',?,?, 'openlibrary','/works/OLHOME1W',0.95,'{}')",
                (existing_cid, field),
            )

    def gb_handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params.get("q", "")
        if "9780312428549" in query or "Home" in query:
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "GBHOME1",
                            "volumeInfo": {
                                "title": "Home",
                                "authors": ["Marilynne Robinson"],
                                "description": "Short surviving Google blurb during partial outage.",
                                "publishedDate": "2009",
                                "language": "en",
                                "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9780312428549"}],
                                "imageLinks": {"thumbnail": "http://books.google.com/books/content?id=GBHOME1&img=1"},
                            },
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "GBLILA1",
                        "volumeInfo": {
                            "title": "Lila",
                            "authors": ["Marilynne Robinson"],
                            "description": "Surviving Google blurb fills an empty candidate.",
                            "publishedDate": "2014-10-07",
                            "language": "en",
                            "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9780374187613"}],
                        },
                    }
                ]
            },
        )

    ol_route = respx.get(OPEN_LIBRARY_SEARCH).mock(return_value=httpx.Response(503, json={"error": "down"}))
    respx.get(GOOGLE_BOOKS_SEARCH).mock(side_effect=gb_handler)
    respx.get(APPLE_BOOKS_SEARCH).mock(return_value=httpx.Response(200, json={"results": []}))

    report = asyncio.run(audit_candidates(only_pending=True))
    assert report["accepted"] == 2

    preserved_cand = row("SELECT * FROM candidates WHERE id=?", (existing_cid,))
    preserved_qual = row("SELECT * FROM candidate_quality WHERE candidate_id=?", (existing_cid,))
    assert preserved_qual["quality_status"] == "accepted"
    assert preserved_qual["audit_version"] == QUALITY_VERSION
    assert "catalog_provider_unavailable" in json.loads(preserved_qual["flags_json"])
    assert preserved_cand["description"] == "Rich existing Open Library synopsis that must survive partial outage."
    assert preserved_cand["cover_url"] == "https://covers.openlibrary.org/b/id/901-L.jpg"
    assert preserved_cand["release_date"] == "2008-09-02"
    assert preserved_qual["isbn10"] == "0312428545"
    assert preserved_qual["metadata_provider"] == "openlibrary"
    desc_prov = row(
        "SELECT provider,provider_id FROM metadata_field_provenance "
        "WHERE entity_type='candidate' AND entity_id=? AND field='description'",
        (existing_cid,),
    )
    assert desc_prov["provider"] == "openlibrary"
    assert desc_prov["provider_id"] == "/works/OLHOME1W"

    filled_cand = row("SELECT description FROM candidates WHERE id=?", (empty_cid,))
    assert filled_cand["description"] == "Surviving Google blurb fills an empty candidate."

    # Once the 24h outage backoff expires and Open Library recovers, canonical metadata reconciles cleanly.
    with transaction() as con:
        con.execute(
            "UPDATE candidate_quality SET audited_at='2000-01-01 00:00:00' WHERE candidate_id=?",
            (existing_cid,),
        )
    ol_route.mock(
        return_value=httpx.Response(
            200,
            json={
                "docs": [
                    {
                        "key": "/works/OLHOME1W",
                        "title": "Home",
                        "author_name": ["Marilynne Robinson"],
                        "isbn": ["9780312428549"],
                        "description": "Updated canonical Open Library synopsis after recovery when both providers are healthy.",
                        "language": ["eng"],
                    }
                ]
            },
        )
    )
    recovered = asyncio.run(audit_candidates(only_pending=True))
    assert recovered["accepted"] >= 1
    reconciled_cand = row("SELECT description FROM candidates WHERE id=?", (existing_cid,))
    reconciled_qual = row("SELECT flags_json FROM candidate_quality WHERE candidate_id=?", (existing_cid,))
    assert "catalog_provider_unavailable" not in json.loads(reconciled_qual["flags_json"])
    assert (
        reconciled_cand["description"]
        == "Updated canonical Open Library synopsis after recovery when both providers are healthy."
    )


@respx.mock
def test_quarantined_false_negative_recovers_after_cooldown_or_manual_retry(database, monkeypatch):
    from afterword_engine import main as engine_main

    v6_quarantined = add_candidate("Unindexed Forthcoming Novel", "Marilynne Robinson")
    v4_no_isbn_quarantined = add_candidate("Misflagged Prior Novel", "Marilynne Robinson")
    with transaction() as con:
        con.execute(
            "UPDATE candidates SET status='rejected' WHERE id NOT IN (?,?)",
            (v6_quarantined, v4_no_isbn_quarantined),
        )
        con.execute(
            "UPDATE candidate_quality SET quality_status='quarantine',audit_version=?,"
            "flags_json=?,audited_at='2026-10-01 12:00:00' WHERE candidate_id=?",
            (QUALITY_VERSION, '["catalog_unmatched"]', v6_quarantined),
        )
        con.execute(
            "UPDATE candidate_quality SET quality_status='quarantine',audit_version='candidate-quality-v4',"
            "flags_json=?,audited_at='2026-10-01 12:00:00' WHERE candidate_id=?",
            ('["catalog_homonym_author"]', v4_no_isbn_quarantined),
        )

    # Not selected within the 14-day quarantine cooldown window.
    early_ids = {
        int(item["id"])
        for item in candidate_quality_audit_candidates(
            now=datetime(2026, 10, 8, tzinfo=timezone.utc)
        )
    }
    assert v6_quarantined not in early_ids
    assert v4_no_isbn_quarantined not in early_ids

    # Automatically selected once CATALOG_QUARANTINE_RETRY_DAYS (14d) has elapsed.
    due_after_cooldown_ids = {
        int(item["id"])
        for item in candidate_quality_audit_candidates(
            now=datetime(2026, 10, 16, tzinfo=timezone.utc)
        )
    }
    assert {v6_quarantined, v4_no_isbn_quarantined} <= due_after_cooldown_ids

    async def fast_score(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(engine_main, "score_all", fast_score)
    ol_route = respx.get(OPEN_LIBRARY_SEARCH).mock(return_value=httpx.Response(200, json={"docs": []}))
    respx.get(GOOGLE_BOOKS_SEARCH).mock(return_value=httpx.Response(200, json={"items": []}))
    respx.get(APPLE_BOOKS_SEARCH).mock(return_value=httpx.Response(200, json={"results": []}))

    # Direct audit_candidates(retry_quarantined=True) uses a single start cutoff so still-quarantined rows report remaining == 0.
    direct_retry = asyncio.run(audit_candidates(retry_quarantined=True))
    assert direct_retry["audited"] == 2
    assert direct_retry["quarantine"] == 2
    assert direct_retry["remaining"] == 0

    # Manual retry=True via endpoint resets stale idle chain_started_at and re-audits quarantined rows without looping infinitely.
    engine_main._set_quality_recovery_settings(
        **{engine_main.QUALITY_RECOVERY_CHAIN_STARTED_AT_KEY: "1.000000"}
    )
    queued = asyncio.run(engine_main.candidate_audit(payload=None, retry=True))
    assert queued["retry_quarantined"] is True
    assert row(
        "SELECT value FROM settings WHERE key=?",
        (engine_main.QUALITY_RECOVERY_CHAIN_STARTED_AT_KEY,),
    )["value"] == ""
    with transaction() as con:
        con.execute("UPDATE jobs SET status='running' WHERE id=?", (queued["job_id"],))
    outcome_still_missing = asyncio.run(engine_main.handle_job("candidate_quality_recovery"))
    with transaction() as con:
        con.execute("UPDATE jobs SET status='complete' WHERE id=?", (queued["job_id"],))
    assert outcome_still_missing["audited"] == 2
    assert outcome_still_missing["quarantine"] == 2
    assert row(
        "SELECT COUNT(*) AS count FROM jobs WHERE kind='candidate_quality_recovery' AND status='queued'"
    )["count"] == 0

    # Once catalog records appear, manual retry promotes both false negatives to accepted.
    def ol_found(request: httpx.Request) -> httpx.Response:
        title = request.url.params.get("title", "")
        if "Unindexed Forthcoming Novel" in title:
            return httpx.Response(
                200,
                json={
                    "docs": [
                        {
                            "key": "/works/OLFORTH1W",
                            "title": "Unindexed Forthcoming Novel",
                            "author_name": ["Marilynne Robinson"],
                            "description": "Newly indexed work.",
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
                        "key": "/works/OLPRIOR1W",
                        "title": "Misflagged Prior Novel",
                        "author_name": ["Marilynne Robinson"],
                        "description": "Recovered false-negative work.",
                        "language": ["eng"],
                    }
                ]
            },
        )

    ol_route.mock(side_effect=ol_found)
    asyncio.run(engine_main.candidate_audit(payload=None, retry=True))
    outcome_recovered = asyncio.run(engine_main.handle_job("candidate_quality_recovery"))
    assert outcome_recovered["accepted"] == 2
    for cid in (v6_quarantined, v4_no_isbn_quarantined):
        assert row("SELECT quality_status FROM candidate_quality WHERE candidate_id=?", (cid,))["quality_status"] == "accepted"


def test_recovery_chain_budget_persists_across_simulated_restarts(database, monkeypatch):
    from afterword_engine import main as engine_main
    from afterword_engine.database import private_setting

    cid = add_candidate("Persistent Budget Book", "A Writer", isbn13="9780307474278")
    with transaction() as con:
        con.execute("UPDATE candidates SET status='rejected' WHERE id!=?", (cid,))
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',audit_version='candidate-quality-v4',"
            "provider='openlibrary',provider_id='/works/OLPERSIST1W' WHERE candidate_id=?",
            (cid,),
        )
        con.execute("DELETE FROM jobs WHERE kind='candidate_quality_recovery'")

    clock = [1000.0]
    monkeypatch.setattr(engine_main, "_recovery_clock", lambda: clock[0])
    monkeypatch.setattr(engine_main, "candidate_metadata_refresh_remaining", lambda: 0)
    monkeypatch.setattr(engine_main, "read_metadata_refresh_remaining", lambda: 0)
    engine_main._set_quality_recovery_settings(
        **{
            engine_main.QUALITY_RECOVERY_CHAIN_STARTED_AT_KEY: "",
            engine_main.QUALITY_RECOVERY_CHAIN_DEFERRED_UNTIL_KEY: "",
            engine_main.QUALITY_RETRY_QUARANTINED_BEFORE_KEY: "",
        }
    )

    async def fast_score(*_args, **_kwargs):
        return 0

    step_durations = [70.0, 65.0]

    async def fake_step_audit(*, only_pending=False, limit=None):
        assert only_pending is True
        clock[0] += step_durations.pop(0)
        return {"audited": 1, "accepted": 0, "quarantine": 0, "rejected": 0, "enriched": 0}

    monkeypatch.setattr(engine_main, "score_all", fast_score)
    monkeypatch.setattr(engine_main, "audit_candidates", fake_step_audit)

    # Batch 1 consumes 70s (< 120s budget): follow-up is queued and started_at is persisted in SQLite.
    batch1 = asyncio.run(engine_main.handle_job("candidate_quality_recovery"))
    assert batch1["followup_deferred"] is False
    assert private_setting(engine_main.QUALITY_RECOVERY_CHAIN_STARTED_AT_KEY) == "1000.000000"
    assert row(
        "SELECT COUNT(*) AS count FROM jobs WHERE kind='candidate_quality_recovery' AND status='queued'"
    )["count"] == 1

    # Simulate a process restart 5s later: queued job cleared and startup recovery check runs.
    clock[0] = 1075.0
    with transaction() as con:
        con.execute("DELETE FROM jobs WHERE kind='candidate_quality_recovery'")
    startup_queued = engine_main._queue_due_enrichment_retries()
    assert "candidate_quality_recovery" in startup_queued
    # The unfinished chain's start timestamp survives across the restart!
    assert private_setting(engine_main.QUALITY_RECOVERY_CHAIN_STARTED_AT_KEY) == "1000.000000"

    # Batch 2 consumes 65s (1140 - 1000 = 140s >= 120s continuous budget across the restart): deferred!
    with transaction() as con:
        con.execute("DELETE FROM jobs WHERE kind='candidate_quality_recovery'")
    batch2 = asyncio.run(engine_main.handle_job("candidate_quality_recovery"))
    assert batch2["followup_deferred"] is True
    assert private_setting(engine_main.QUALITY_RECOVERY_CHAIN_STARTED_AT_KEY) == ""
    deferred_until = float(private_setting(engine_main.QUALITY_RECOVERY_CHAIN_DEFERRED_UNTIL_KEY))
    assert deferred_until == pytest.approx(1140.0 + engine_main.ENRICHMENT_RETRY_POLL_SECONDS)

    # Another restart during the cooldown window still honors deferred_until from SQLite.
    clock[0] = 1200.0
    assert "candidate_quality_recovery" not in engine_main._queue_due_enrichment_retries()

    # After the cooldown window elapses, recovery resumes cleanly.
    clock[0] = deferred_until + 1.0
    assert "candidate_quality_recovery" in engine_main._queue_due_enrichment_retries()


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
