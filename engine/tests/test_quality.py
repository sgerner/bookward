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
    v4_no_isbn_catalog_accepted = add_candidate("V4 Catalog Accepted Without ISBN", "A Writer")
    v4_no_isbn_unattributed = add_candidate("V4 Unattributed Accepted Without ISBN", "A Writer")
    v4_no_isbn_quarantine = add_candidate("V4 Quarantine Without ISBN", "A Writer")
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
            (v4_no_isbn_catalog_accepted, "accepted", "candidate-quality-v4"),
            (v4_no_isbn_unattributed, "accepted", "candidate-quality-v4"),
            (v4_no_isbn_quarantine, "quarantine", "candidate-quality-v4"),
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
        con.execute(
            "UPDATE candidate_quality SET provider='openlibrary',provider_id='/works/OLV4W' WHERE candidate_id=?",
            (v4_no_isbn_catalog_accepted,),
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
        v4_no_isbn_catalog_accepted,
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
    assert current_accepted not in selected_ids
    assert curated not in selected_ids
    assert fresh_outage not in selected_ids
    assert fresh_accepted_outage not in selected_ids
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
    assert candidate["release_date"] == "2026-10-06"
    assert "600x600bb" in candidate["cover_url"]


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
    with transaction() as con:
        for idx, cid in enumerate(cids):
            con.execute(
                "UPDATE candidate_quality SET quality_status='accepted',audit_version='candidate-quality-v4',"
                "provider='openlibrary',provider_id=?,work_id=? WHERE candidate_id=?",
                (f"/works/OLOLD{idx}W", f"OLOLD{idx}W", cid),
            )

    # 1. count_only makes zero HTTP requests and mutates nothing.
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

    summary = engine_main.candidate_quality()
    assert summary["due"] == 15
    assert summary["due_breakdown"] == {"prior_version_isbn": 15}

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
    monkeypatch.setattr(engine_main, "_recovery_monotonic", lambda: chain_clock[0])
    engine_main._recovery_chain_started_at.clear()

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

    # Periodic scheduler later starts a fresh bounded recovery window.
    monkeypatch.setattr(engine_main, "candidate_metadata_refresh_remaining", lambda: 0)
    monkeypatch.setattr(engine_main, "read_metadata_refresh_remaining", lambda: 0)
    requeued = engine_main._queue_due_enrichment_retries()
    assert "candidate_quality_recovery" in requeued
