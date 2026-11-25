import asyncio
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timedelta, timezone

import pytest

from afterword_engine.config import settings
from afterword_engine.covers import METADATA_NORMALIZATION_VERSION
from afterword_engine.database import MIGRATIONS, initialize, row, rows, transaction
from afterword_engine.identity import read_metadata_identity_hash
from afterword_engine import ingestion
from afterword_engine.ingestion import refresh_missing_candidate_metadata
from afterword_engine import main as engine_main
from afterword_engine.main import handle_job
from afterword_engine.quality import QUALITY_AUDIT_BATCH_SIZE, QUALITY_VERSION, candidate_quality_audit_candidates


@pytest.fixture()
def database(tmp_path: Path):
    settings.db = str(tmp_path / "enrichment.db")
    initialize()
    return settings.db


def _catalog_result(title, author, requested_isbn, *, mismatch=False, identifiers=None):
    provider = "google_books"
    provider_id = f"volume:{title}"
    returned_isbn = "9780061120084" if mismatch else requested_isbn
    payload = {
        "title": title,
        "authors": [author],
        "identifiers": list(identifiers) if identifiers is not None else [returned_isbn],
        "genres": ["Literary fiction"],
    }
    fields = {
        field: {"provider": provider, "provider_id": provider_id}
        for field in ("description", "genres", "cover_url", "release_date")
    }
    return {
        "description": f"A catalog synopsis for {title}.",
        "description_kind": "synopsis",
        "genres": ["Literary fiction"],
        "cover_url": "https://covers.openlibrary.org/b/id/98765-L.jpg",
        "release_date": "2020-01-01",
        "date_kind": "day",
        "metadata_provenance": {
            "fetched_at": "2026-10-02T12:00:00Z",
            "fields": fields,
            "fetch_trace": [{
                "provider": provider,
                "provider_id": provider_id,
                "query_kind": "isbn",
                "status": "matched",
                "identifier_verified": not mismatch,
            }],
            "source_payloads": [{
                "provider": provider,
                "provider_id": provider_id,
                "payload": payload,
            }],
        },
    }


def test_read_metadata_job_drains_batches_and_negative_caches_mismatches(database, monkeypatch):
    requested_isbn = "9780307474278"
    read_ids = []
    with transaction() as con:
        for index in range(7):
            read_ids.append(con.execute(
                "INSERT INTO reads(title,author,rating,isbn,source) VALUES(?,?,?,?,?)",
                (f"Read {index}", f"Writer {index}", 3.5, requested_isbn, "goodreads_csv"),
            ).lastrowid)

    calls = []

    async def resolve(title, author, *, isbn13="", isbn10="", client=None, **_kwargs):
        calls.append(title)
        return _catalog_result(
            title,
            author,
            isbn13,
            mismatch=title == "Read 0",
            identifiers=(
                ["9780061120084", requested_isbn]
                if title == "Read 1"
                else None
            ),
        )

    monkeypatch.setattr(ingestion, "resolve_book_metadata", resolve)

    first = asyncio.run(handle_job("read_metadata"))
    assert first["checked"] == 5
    assert first["remaining"] == 2
    assert first["score_job_id"] is None
    assert row("SELECT COUNT(*) count FROM jobs WHERE kind='score'")["count"] == 0

    second = asyncio.run(handle_job("read_metadata"))
    third = asyncio.run(handle_job("read_metadata"))
    assert second["checked"] == 2
    assert second["remaining"] == 0
    assert third["checked"] == 0
    assert len(calls) == 7

    reads = rows(
        "SELECT id,title,rating FROM reads WHERE id IN (%s) ORDER BY id"
        % ",".join("?" for _ in read_ids),
        tuple(read_ids),
    )
    assert all(item["rating"] == 3.5 for item in reads)
    assert [item["title"] for item in reads] == [f"Read {index}" for index in range(7)]

    cache = rows(
        "SELECT read_id,identity_provider,description,genres,metadata_provenance,metadata_checked_at "
        "FROM read_metadata WHERE read_id IN (%s) ORDER BY read_id"
        % ",".join("?" for _ in read_ids),
        tuple(read_ids),
    )
    assert len(cache) == 7
    assert all(item["metadata_checked_at"] for item in cache)
    assert cache[0]["description"] == ""
    assert cache[0]["identity_provider"] == ""
    negative_provenance = json.loads(cache[0]["metadata_provenance"])
    assert negative_provenance["attempt_status"] == "no_verified_record"
    assert negative_provenance["source_payloads"] == []
    assert all("provider_id" not in trace for trace in negative_provenance["fetch_trace"])
    assert cache[1]["description"] == "A catalog synopsis for Read 1."
    assert json.loads(cache[1]["genres"]) == ["Literary fiction"]

    provenance = rows(
        "SELECT field,provider,provider_id,source_payload FROM metadata_field_provenance "
        "WHERE entity_type='read' AND entity_id=? ORDER BY field",
        (read_ids[1],),
    )
    assert {item["field"] for item in provenance} == {
        "description", "genres", "cover_url", "release_date"
    }
    assert all(item["provider"] == "google_books" for item in provenance)
    assert all(len(item["source_payload"].encode("utf-8")) <= 8192 for item in provenance)


def test_unrated_isbn_read_can_receive_verified_work_identity(database, monkeypatch):
    title, author, isbn = "Unrated Catalog Work", "A Reader", "9780307474278"
    with transaction() as con:
        read_id = con.execute(
            "INSERT INTO reads(title,author,rating,isbn,source) VALUES(?,?,NULL,?,'manual')",
            (title, author, isbn),
        ).lastrowid

    result = _catalog_result(title, author, isbn)
    work_id = "/works/OL123456W"
    provenance = result["metadata_provenance"]
    provenance["fields"] = {
        field: {"provider": "openlibrary", "provider_id": work_id}
        for field in ("description", "genres", "cover_url", "release_date")
    }
    provenance["fetch_trace"] = [{
        "provider": "openlibrary",
        "provider_id": work_id,
        "query_kind": "isbn",
        "status": "matched",
        "identifier_verified": True,
    }]
    provenance["source_payloads"] = [{
        "provider": "openlibrary",
        "provider_id": work_id,
        "payload": {
            "title": title,
            "authors": [author],
            "identifiers": [isbn],
        },
    }]

    async def resolve(*_args, **_kwargs):
        return result

    monkeypatch.setattr(ingestion, "resolve_book_metadata", resolve)
    assert ingestion.read_metadata_refresh_remaining() == 1
    outcome = asyncio.run(ingestion.refresh_read_metadata())
    assert outcome["checked"] == 1
    assert row(
        "SELECT verified_work_id FROM read_metadata WHERE read_id=?", (read_id,)
    )["verified_work_id"] == work_id
    assert row("SELECT rating FROM reads WHERE id=?", (read_id,))["rating"] is None


@pytest.mark.parametrize(
    ("attempt_status", "trace_status", "stale_after"),
    [
        ("no_verified_record", "no_match", timedelta(days=31)),
        ("provider_unavailable", "server_error", timedelta(hours=25)),
    ],
)
def test_known_work_negative_metadata_respects_retry_cooldown(
    database, attempt_status, trace_status, stale_after
):
    with transaction() as con:
        read_id = con.execute(
            "INSERT INTO reads(title,author,rating,isbn,source,openlibrary_work_id) VALUES(?,?,?,?,?,?)",
            ("Known work", "A Writer", 4.0, "9780307474278", "goodreads_csv", "/works/OL900W"),
        ).lastrowid
    read = row("SELECT * FROM reads WHERE id=?", (read_id,))
    attempt = {
        "attempt_status": attempt_status,
        "fetch_trace": [{
            "provider": "openlibrary",
            "provider_id": "/works/OL900W",
            "query_kind": "verified_work_id",
            "status": trace_status,
        }],
    }
    checked_at = datetime.now(timezone.utc).isoformat()
    with transaction() as con:
        con.execute(
            "INSERT INTO read_metadata(read_id,verified_work_id,identity_hash,metadata_provenance,metadata_checked_at) "
            "VALUES(?,?,?,?,?)",
            (
                read_id,
                "",
                read_metadata_identity_hash(read),
                json.dumps(attempt),
                checked_at,
            ),
        )

    assert ingestion.read_metadata_refresh_remaining() == 0

    stale_at = (datetime.now(timezone.utc) - stale_after).isoformat()
    with transaction() as con:
        con.execute(
            "UPDATE read_metadata SET metadata_checked_at=? WHERE read_id=?",
            (stale_at, read_id),
        )
    assert ingestion.read_metadata_refresh_remaining() == 1


def test_known_work_identity_change_bypasses_negative_retry_cooldown(database):
    with transaction() as con:
        read_id = con.execute(
            "INSERT INTO reads(title,author,rating,isbn,source,openlibrary_work_id) VALUES(?,?,?,?,?,?)",
            ("Known work", "A Writer", 4.0, "9780307474278", "goodreads_csv", "/works/OL900W"),
        ).lastrowid
    read = row("SELECT * FROM reads WHERE id=?", (read_id,))
    with transaction() as con:
        con.execute(
            "INSERT INTO read_metadata(read_id,verified_work_id,identity_hash,metadata_provenance,metadata_checked_at) "
            "VALUES(?,?,?,?,?)",
            (
                read_id,
                "",
                read_metadata_identity_hash(read),
                json.dumps({"attempt_status": "no_verified_record", "fetch_trace": []}),
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    assert ingestion.read_metadata_refresh_remaining() == 0
    with transaction() as con:
        con.execute("UPDATE reads SET openlibrary_work_id=? WHERE id=?", ("/works/OL901W", read_id))
    assert ingestion.read_metadata_refresh_remaining() == 1


def test_read_metadata_normalizes_work_url_before_storing(database, monkeypatch):
    raw_work_url = "https://openlibrary.org/works/OL900W"
    normalized_work_id = "/works/OL900W"
    with transaction() as con:
        read_id = con.execute(
            "INSERT INTO reads(title,author,rating,isbn,source,openlibrary_work_id) VALUES(?,?,?,?,?,?)",
            ("URL form work", "A Writer", 4.0, "9780307474278", "goodreads_csv", raw_work_url),
        ).lastrowid

    async def resolve_work(work_id, *, title="", author="", **_kwargs):
        assert work_id == normalized_work_id
        return {
            "description": "A verified synopsis.",
            "description_kind": "synopsis",
            "genres": ["Fantasy"],
            "work_id": normalized_work_id,
            "catalog_title": "URL form work",
            "metadata_provenance": {
                "fetched_at": "2026-10-02T12:00:00Z",
                "fields": {
                    "description": {
                        "provider": "openlibrary",
                        "provider_id": normalized_work_id,
                        "kind": "synopsis",
                        "source_field": "description",
                    },
                    "genres": {
                        "provider": "openlibrary",
                        "provider_id": normalized_work_id,
                        "source_field": "subjects",
                    },
                },
                "fetch_trace": [{
                    "provider": "openlibrary",
                    "provider_id": normalized_work_id,
                    "query_kind": "verified_work_id",
                    "status": "matched",
                }],
                "source_payloads": [{
                    "provider": "openlibrary",
                    "provider_id": normalized_work_id,
                    "payload": {
                        "key": normalized_work_id,
                        "title": "URL form work",
                        "description": "A verified synopsis.",
                        "genres": ["Fantasy"],
                    },
                }],
            },
        }

    monkeypatch.setattr(ingestion, "resolve_openlibrary_work_metadata", resolve_work)
    result = asyncio.run(handle_job("read_metadata"))

    assert result["checked"] == 1
    assert result["updated"] == 1
    assert result["remaining"] == 0
    cached = row(
        "SELECT verified_work_id,identity_provider,identity_provider_id,description,genres "
        "FROM read_metadata WHERE read_id=?",
        (read_id,),
    )
    assert cached["verified_work_id"] == normalized_work_id
    assert cached["identity_provider"] == "openlibrary"
    assert cached["identity_provider_id"] == normalized_work_id
    assert cached["description"] == "A verified synopsis."
    assert json.loads(cached["genres"]) == ["Fantasy"]


def test_read_metadata_failure_preserves_verified_cache_and_records_attempt(database, monkeypatch):
    with transaction() as con:
        read_id = con.execute(
            "INSERT INTO reads(title,author,rating,isbn,source) VALUES(?,?,?,?,?)",
            ("Cached read", "A Writer", 4.25, "9780307474278", "goodreads_csv"),
        ).lastrowid

    async def successful(title, author, *, isbn13="", **_kwargs):
        return _catalog_result(title, author, isbn13)

    monkeypatch.setattr(ingestion, "resolve_book_metadata", successful)
    first = asyncio.run(handle_job("read_metadata"))
    assert first["updated"] == 1
    original = row(
        "SELECT verified_work_id,identity_provider,identity_provider_id,identity_evidence,"
        "description,genres,metadata_provenance FROM read_metadata WHERE read_id=?",
        (read_id,),
    )

    old_check = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    with transaction() as con:
        con.execute("UPDATE read_metadata SET metadata_checked_at=? WHERE read_id=?", (old_check, read_id))

    async def failed(*_args, **_kwargs):
        raise TimeoutError("catalog timed out")

    monkeypatch.setattr(ingestion, "resolve_book_metadata", failed)
    second = asyncio.run(handle_job("read_metadata"))
    assert second["checked"] == 1
    assert second["updated"] == 0
    assert second["lookup_failures"] == 1

    cached = row(
        "SELECT verified_work_id,identity_provider,identity_provider_id,identity_evidence,"
        "description,genres,metadata_provenance,metadata_checked_at FROM read_metadata WHERE read_id=?",
        (read_id,),
    )
    assert cached["verified_work_id"] == original["verified_work_id"]
    assert cached["identity_provider"] == original["identity_provider"]
    assert cached["identity_provider_id"] == original["identity_provider_id"]
    assert cached["identity_evidence"] == original["identity_evidence"]
    assert cached["description"] == original["description"]
    assert cached["genres"] == original["genres"]
    assert json.loads(cached["metadata_provenance"])["fields"] == json.loads(original["metadata_provenance"])["fields"]
    assert json.loads(cached["metadata_provenance"])["last_attempt"]["attempt_status"] == "request_error"
    assert cached["metadata_checked_at"] != old_check
    with transaction() as con:
        con.execute(
            "UPDATE read_metadata SET metadata_checked_at=? WHERE read_id=?",
            ((datetime.now(timezone.utc) - timedelta(hours=23)).isoformat(), read_id),
        )
    assert ingestion.read_metadata_refresh_remaining() == 0
    with transaction() as con:
        con.execute(
            "UPDATE read_metadata SET metadata_checked_at=? WHERE read_id=?",
            ((datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(), read_id),
        )
    assert ingestion.read_metadata_refresh_remaining() == 1
    assert row("SELECT rating FROM reads WHERE id=?", (read_id,))["rating"] == 4.25


def test_read_metadata_upgrades_only_same_verified_opening_sentence(database, monkeypatch):
    with transaction() as con:
        read_id = con.execute(
            "INSERT INTO reads(title,author,rating,isbn,source,openlibrary_work_id) VALUES(?,?,?,?,?,?)",
            ("The Cache Book", "A Writer", 4.75, "9780307474278", "goodreads_csv", "/works/OL900W"),
        ).lastrowid

    def openlibrary_metadata(*, kind, opening_sentence="The opening sentence."):
        description = "A complete catalog synopsis." if kind == "synopsis" else "The opening sentence."
        return {
            "description": description,
            "description_kind": kind,
            "description_provider": "openlibrary",
            "description_provider_id": "/works/OL900W",
            "work_id": "/works/OL900W",
            "catalog_title": "The Cache Book",
            "metadata_provenance": {
                "fetched_at": "2026-10-02T12:00:00Z",
                "fields": {
                    "description": {
                        "provider": "openlibrary",
                        "provider_id": "/works/OL900W",
                        "kind": kind,
                        "source_field": "first_sentence" if kind == "opening_sentence" else "description",
                    }
                },
                "fetch_trace": [{
                    "provider": "openlibrary",
                    "provider_id": "/works/OL900W",
                    "query_kind": "verified_work_id",
                    "status": "matched",
                }],
                "source_payloads": [{
                    "provider": "openlibrary",
                    "provider_id": "/works/OL900W",
                    "payload": {
                        "key": "/works/OL900W",
                        "title": "The Cache Book",
                        "description": description if kind == "synopsis" else "",
                        "opening_sentence": opening_sentence,
                    },
                }],
            },
        }

    calls = []

    async def resolve_work(work_id, *, title="", author="", **_kwargs):
        calls.append(work_id)
        if len(calls) == 1:
            return openlibrary_metadata(kind="opening_sentence")
        if len(calls) == 2:
            return openlibrary_metadata(kind="synopsis", opening_sentence="A different sentence.")
        return openlibrary_metadata(kind="synopsis")

    monkeypatch.setattr(ingestion, "resolve_openlibrary_work_metadata", resolve_work)
    first = asyncio.run(handle_job("read_metadata"))
    assert first["updated"] == 1
    cached = row("SELECT description,description_kind FROM read_metadata WHERE read_id=?", (read_id,))
    assert cached == {"description": "The opening sentence.", "description_kind": "opening_sentence"}

    old_check = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    with transaction() as con:
        con.execute("UPDATE read_metadata SET metadata_checked_at=? WHERE read_id=?", (old_check, read_id))
    second = asyncio.run(handle_job("read_metadata"))
    assert second["updated"] == 0
    cached = row("SELECT description,description_kind FROM read_metadata WHERE read_id=?", (read_id,))
    assert cached == {"description": "The opening sentence.", "description_kind": "opening_sentence"}

    with transaction() as con:
        con.execute("UPDATE read_metadata SET metadata_checked_at=? WHERE read_id=?", (old_check, read_id))
    third = asyncio.run(handle_job("read_metadata"))
    assert third["updated"] == 1
    cached = row("SELECT description,description_kind FROM read_metadata WHERE read_id=?", (read_id,))
    assert cached == {"description": "A complete catalog synopsis.", "description_kind": "synopsis"}
    description_source = row(
        "SELECT provider,provider_id,source_payload FROM metadata_field_provenance "
        "WHERE entity_type='read' AND entity_id=? AND field='description'",
        (read_id,),
    )
    assert description_source["provider"] == "openlibrary"
    assert description_source["provider_id"] == "/works/OL900W"
    assert json.loads(description_source["source_payload"])["field_provenance"]["kind"] == "synopsis"
    assert row("SELECT rating FROM reads WHERE id=?", (read_id,))["rating"] == 4.75
    assert calls == ["/works/OL900W", "/works/OL900W", "/works/OL900W"]


def test_verified_read_genres_drop_unverified_merged_provider(database):
    read = {
        "title": "A Shared Book",
        "author": "A Writer",
        "isbn": "9780307474278",
        "openlibrary_work_id": "",
    }
    metadata = {
        "genres": ["Science fiction", "Romance"],
        "metadata_provenance": {
            "fields": {
                "genres": {
                    "provider": "openlibrary",
                    "provider_id": "/works/OLwrongW",
                    "source_field": "subjects",
                    "sources": [
                        {
                            "provider": "openlibrary",
                            "provider_id": "/works/OLwrongW",
                            "source_field": "subjects",
                        },
                        {
                            "provider": "google_books",
                            "provider_id": "volume:verified",
                            "source_field": "categories",
                        },
                    ],
                }
            },
            "fetch_trace": [
                {"provider": "openlibrary", "provider_id": "/works/OLwrongW", "query_kind": "isbn", "status": "matched"},
                {"provider": "google_books", "provider_id": "volume:verified", "query_kind": "isbn", "status": "matched"},
            ],
            "source_payloads": [
                {
                    "provider": "openlibrary",
                    "provider_id": "/works/OLwrongW",
                    "payload": {
                        "title": "A Shared Book",
                        "authors": ["A Writer"],
                        "identifiers": ["9780061120084"],
                        "genres": ["Romance"],
                    },
                },
                {
                    "provider": "google_books",
                    "provider_id": "volume:verified",
                    "payload": {
                        "title": "A Shared Book",
                        "authors": ["A Writer"],
                        "identifiers": ["9780307474278"],
                        "genres": ["Science fiction"],
                    },
                },
            ],
        },
    }

    filtered, evidence, work_id = ingestion._verified_read_metadata(metadata, read)
    assert evidence["provider"] == "google_books"
    assert work_id == ""
    assert filtered["genres"] == ["Science fiction"]
    assert filtered["genres_provider"] == "google_books"
    assert filtered["genres_provider_id"] == "volume:verified"
    field = filtered["metadata_provenance"]["fields"]["genres"]
    assert field["provider"] == "google_books"
    assert field["provider_id"] == "volume:verified"
    assert field["source_field"] == "categories"
    assert field["sources"] == [field["sources"][0]]
    assert len(field["sources"]) == 1


def test_read_verifier_accepts_only_known_title_suffixes_and_keeps_isbn_author_strict(database):
    isbn = "9780307474278"

    def verify(read_title, record_title, *, author="Frank Herbert", record_author=None, mismatch=False):
        read = {
            "title": read_title,
            "author": author,
            "isbn": isbn,
            "openlibrary_work_id": "",
        }
        result = _catalog_result(
            record_title,
            record_author or author,
            isbn,
            mismatch=mismatch,
        )
        _filtered, evidence, _work_id = ingestion._verified_read_metadata(result, read)
        return bool(evidence)

    assert verify("Dune", "Dune (Unabridged)")
    assert verify("Ninth House", "Ninth House (Alex Stern, Book 1)", author="Leigh Bardugo")
    assert not verify("Dune", "Dune: Messiah")
    assert not verify("Dune", "Dune (Unabridged)", record_author="Robert Jordan")
    assert not verify("Dune", "Dune (Unabridged)", mismatch=True)


def test_read_identity_edit_invalidates_cache_without_changing_rating(database):
    with transaction() as con:
        read_id = con.execute(
            "INSERT INTO reads(title,author,rating,isbn,source) VALUES(?,?,?,?,?)",
            ("Original title", "A Writer", 4.5, "9780307474278", "goodreads_csv"),
        ).lastrowid
        con.execute(
            "INSERT INTO read_metadata(read_id,verified_work_id,identity_hash,description,metadata_checked_at) "
            "VALUES(?,?,?,?,?)",
            (read_id, "", "old-hash", "Cached synopsis", "2026-10-01T00:00:00Z"),
        )
        con.execute(
            "INSERT INTO metadata_field_provenance(entity_type,entity_id,field,provider,provider_id,source_payload) "
            "VALUES('read',?,'description','google_books','volume:old','{}')",
            (read_id,),
        )

    with transaction() as con:
        con.execute("UPDATE reads SET title='Edited title' WHERE id=?", (read_id,))

    assert row("SELECT rating FROM reads WHERE id=?", (read_id,))["rating"] == 4.5
    assert row("SELECT read_id FROM read_metadata WHERE read_id=?", (read_id,)) is None
    assert row(
        "SELECT id FROM metadata_field_provenance WHERE entity_type='read' AND entity_id=?",
        (read_id,),
    ) is None


def test_verified_catalog_synopsis_replaces_an_initial_source_blurb(database, monkeypatch):
    with transaction() as con:
        candidate_id = con.execute(
            "INSERT INTO candidates(title,author,description,source_url,normalized_key) "
            "VALUES(?,?,?,?,?)",
            (
                "Legacy catalog book", "A Writer", "Initial source blurb.",
                "https://example.com/book", "legacy catalog book a writer",
            ),
        ).lastrowid
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',"
            "metadata_provider='openlibrary',metadata_provider_id='/works/OL987W' "
            "WHERE candidate_id=?",
            (candidate_id,),
        )

    async def enrich(items):
        item = dict(items[0])
        item.update({
            "description": "The complete verified synopsis.",
            "_description_candidate": {
                "provider": "openlibrary",
                "provider_id": "/works/OL987W",
                "kind": "synopsis",
                "opening_sentence": "Different catalog opening sentence.",
                "text": "The complete verified synopsis.",
            },
            "_metadata_provider": "openlibrary",
            "_metadata_provider_id": "/works/OL987W",
            "_description_provider": "openlibrary",
            "_description_provider_id": "/works/OL987W",
            "_metadata_provenance": {
                "fetched_at": "2026-10-02T12:00:00Z",
                "fields": {"description": {
                    "provider": "openlibrary",
                    "provider_id": "/works/OL987W",
                    "kind": "synopsis",
                    "source_field": "description",
                }},
                "source_payloads": [{
                    "provider": "openlibrary",
                    "provider_id": "/works/OL987W",
                    "payload": {"key": "/works/OL987W", "description": "The complete verified synopsis."},
                }],
            },
        })
        return [item]

    monkeypatch.setattr(ingestion, "enrich_book_metadata", enrich)
    assert ingestion.candidate_metadata_refresh_remaining() == 1

    assert asyncio.run(refresh_missing_candidate_metadata()) == 1

    candidate = row("SELECT description FROM candidates WHERE id=?", (candidate_id,))
    quality = row(
        "SELECT metadata_version,metadata_checked_at FROM candidate_quality WHERE candidate_id=?",
        (candidate_id,),
    )
    provenance = row(
        "SELECT provider,provider_id,source_payload FROM metadata_field_provenance "
        "WHERE entity_type='candidate' AND entity_id=? AND field='description'",
        (candidate_id,),
    )
    assert candidate["description"] == "The complete verified synopsis."
    assert quality["metadata_version"] == METADATA_NORMALIZATION_VERSION
    assert quality["metadata_checked_at"]
    assert provenance["provider"] == "openlibrary"
    assert provenance["provider_id"] == "/works/OL987W"


def test_legacy_catalog_dates_are_normalized_only_when_valid(database):
    values = [
        ("Legacy year", "1998", "catalog"),
        ("Legacy month", "2001-04", "catalog"),
        ("Legacy day", "2004-06-07", "catalog"),
        ("Invalid year zero", "0000", "catalog"),
        ("Invalid day", "2024-02-30", "catalog"),
        ("Source year", "1998", "source"),
        ("Manual year", "1998", "manual"),
    ]
    ids = {}
    with transaction() as con:
        con.execute("UPDATE candidates SET date_kind='source'")
        source_id = con.execute("SELECT id FROM sources WHERE url='builtin://upcoming'").fetchone()[0]
        for title, release_date, date_kind in values:
            ids[title] = con.execute(
                "INSERT INTO candidates(title,author,release_date,date_kind,source_url,source_id,normalized_key) "
                "VALUES(?,?,?,?,?,?,?)",
                (title, "A Writer", release_date, date_kind, "https://example.com/book", source_id, title.casefold()),
            ).lastrowid

    assert ingestion.normalize_stored_candidate_catalog_dates() == 3
    normalized = rows(
        "SELECT title,release_date,date_kind FROM candidates WHERE id IN (%s) ORDER BY title"
        % ",".join("?" for _ in ids),
        tuple(ids.values()),
    )
    found = {item["title"]: (item["release_date"], item["date_kind"]) for item in normalized}
    assert found == {
        "Invalid day": ("2024-02-30", "catalog"),
        "Invalid year zero": ("0000", "catalog"),
        "Legacy day": ("2004-06-07", "day"),
        "Legacy month": ("2001-04-01", "month"),
        "Legacy year": ("1998-01-01", "year"),
        "Manual year": ("1998", "manual"),
        "Source year": ("1998", "source"),
    }
    assert row(
        "SELECT id FROM metadata_field_provenance WHERE entity_type='candidate' "
        "AND entity_id IN (%s) AND field='release_date' LIMIT 1"
        % ",".join("?" for _ in ids),
        tuple(ids.values()),
    ) is None


def test_catalog_date_repair_counts_as_metadata_update_and_queues_rescore(database):
    with transaction() as con:
        con.execute("UPDATE candidates SET status='rejected'")
        source_id = con.execute("SELECT id FROM sources WHERE url='builtin://upcoming'").fetchone()[0]
        candidate_id = con.execute(
            "INSERT INTO candidates(title,author,description,cover_url,release_date,date_kind,genres,source_url,source_id,normalized_key) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "Year only catalog book", "A Writer", "Existing synopsis.",
                "https://images.example.com/cover.jpg", "1998", "catalog", json.dumps(["Fantasy"]),
                "https://example.com/book", source_id, "year only catalog book",
            ),
        ).lastrowid
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',metadata_version=?,metadata_checked_at=CURRENT_TIMESTAMP "
            "WHERE candidate_id=?",
            (METADATA_NORMALIZATION_VERSION, candidate_id),
        )

    result = asyncio.run(handle_job("metadata"))
    candidate = row("SELECT release_date,date_kind FROM candidates WHERE id=?", (candidate_id,))
    assert candidate == {"release_date": "1998-01-01", "date_kind": "year"}
    assert result["metadata"] == 1
    assert result["remaining"] == 0
    assert result["score_job_id"]
    assert row("SELECT status FROM jobs WHERE id=?", (result["score_job_id"],))["status"] == "queued"


def test_candidate_quality_recovery_batches_and_schedules_one_followup(database, monkeypatch):
    candidate_ids = []
    with transaction() as con:
        source_id = con.execute("SELECT id FROM sources WHERE url='builtin://upcoming'").fetchone()[0]
        for index in range(QUALITY_AUDIT_BATCH_SIZE + 1):
            candidate_id = con.execute(
                "INSERT INTO candidates(title,author,isbn13,source_url,source_id,normalized_key) "
                "VALUES(?,?,?,?,?,?)",
                (
                    f"Legacy accepted {index}", "A Writer", "9780307474278",
                    "https://example.com/book", source_id, f"legacy accepted {index}",
                ),
            ).lastrowid
            candidate_ids.append(candidate_id)
            con.execute(
                "UPDATE candidate_quality SET quality_status='accepted',audit_version='candidate-quality-v3' "
                "WHERE candidate_id=?",
                (candidate_id,),
            )

    limits = []

    async def audit(*, only_pending=False, limit=None):
        assert only_pending is True
        limits.append(limit)
        batch = candidate_quality_audit_candidates(limit=limit)
        with transaction() as con:
            for candidate in batch:
                con.execute(
                    "UPDATE candidate_quality SET audit_version=?,audited_at=CURRENT_TIMESTAMP "
                    "WHERE candidate_id=?",
                    (QUALITY_VERSION, candidate["id"]),
                )
        return {"audited": len(batch), "accepted": len(batch), "quarantine": 0, "rejected": 0, "enriched": 0}

    async def score_all(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(engine_main, "audit_candidates", audit)
    monkeypatch.setattr(engine_main, "score_all", score_all)
    assert engine_main.has_candidate_quality_audit_candidates()

    first = asyncio.run(handle_job("candidate_quality_recovery"))
    assert first["audited"] == QUALITY_AUDIT_BATCH_SIZE
    assert len(candidate_quality_audit_candidates(limit=QUALITY_AUDIT_BATCH_SIZE)) == 1
    assert row("SELECT COUNT(*) count FROM jobs WHERE kind='candidate_quality_recovery' AND status='queued'")["count"] == 1

    # The queued continuation is now the current worker's claimed job.
    with transaction() as con:
        con.execute("DELETE FROM jobs WHERE kind='candidate_quality_recovery'")
    second = asyncio.run(handle_job("candidate_quality_recovery"))
    assert second["audited"] == 1
    assert limits == [QUALITY_AUDIT_BATCH_SIZE, QUALITY_AUDIT_BATCH_SIZE]
    assert candidate_quality_audit_candidates(limit=1) == []
    assert row("SELECT COUNT(*) count FROM jobs WHERE kind='candidate_quality_recovery' AND status='queued'")["count"] == 0


def test_source_triggered_candidate_quality_audit_is_bounded(database, monkeypatch):
    with transaction() as con:
        source_id = con.execute(
            "INSERT INTO sources(name,url,enabled,lifecycle) VALUES(?,?,1,'one_time')",
            ("Bounded source", "https://example.com/bounded"),
        ).lastrowid

    async def scan(_source):
        return 0

    async def refresh_metadata():
        return 0

    limits = []
    followups = []

    async def audit(*, only_pending=False, limit=None):
        limits.append((only_pending, limit))
        return {"audited": 0, "accepted": 0, "quarantine": 0, "rejected": 0, "enriched": 0}

    async def score_all(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(engine_main, "scan_source", scan)
    monkeypatch.setattr(engine_main, "refresh_missing_candidate_metadata", refresh_metadata)
    monkeypatch.setattr(engine_main, "candidate_metadata_refresh_remaining", lambda: 0)
    monkeypatch.setattr(engine_main, "audit_candidates", audit)
    monkeypatch.setattr(engine_main, "_enqueue_candidate_quality_followup", lambda: followups.append(True))
    monkeypatch.setattr(engine_main, "score_all", score_all)
    monkeypatch.setattr(engine_main, "queue_digest_if_due", lambda: None)

    asyncio.run(handle_job(f"source:{source_id}"))
    assert limits == [(True, QUALITY_AUDIT_BATCH_SIZE)]
    assert followups == [True]


def test_periodic_retry_scheduler_queues_quality_recovery_without_provider_work(database, monkeypatch):
    monkeypatch.setattr(engine_main, "has_candidate_quality_audit_candidates", lambda: True)
    monkeypatch.setattr(engine_main, "candidate_metadata_refresh_remaining", lambda: 0)
    monkeypatch.setattr(engine_main, "read_metadata_refresh_remaining", lambda: 0)

    queued = engine_main._queue_due_enrichment_retries()
    assert "candidate_quality_recovery" in queued
    assert row("SELECT kind,status FROM jobs WHERE id=?", (queued["candidate_quality_recovery"],)) == {
        "kind": "candidate_quality_recovery",
        "status": "queued",
    }
    assert engine_main._queue_due_enrichment_retries() == {}


def test_manual_candidate_audit_defaults_incremental_supports_full_and_reports_summary(
    database, monkeypatch
):
    with transaction() as con:
        source_id = con.execute(
            "SELECT id FROM sources WHERE url='builtin://upcoming'"
        ).fetchone()[0]
        accepted_ids = []
        for index in range(120):
            cid = con.execute(
                "INSERT INTO candidates(title,author,source_url,source_id,normalized_key) "
                "VALUES(?,?,?,?,?)",
                (
                    f"Catalog Accepted {index}",
                    "A Writer",
                    "https://example.com/accepted",
                    source_id,
                    f"catalog accepted {index}",
                ),
            ).lastrowid
            accepted_ids.append(cid)
            con.execute(
                "UPDATE candidate_quality SET quality_status='accepted',audit_version=?,audited_at='2026-01-01 00:00:00' "
                "WHERE candidate_id=?",
                (QUALITY_VERSION, cid),
            )
        pending_id = con.execute(
            "INSERT INTO candidates(title,author,source_url,source_id,normalized_key) "
            "VALUES(?,?,?,?,?)",
            (
                "Catalog Pending",
                "A Writer",
                "https://example.com/pending",
                source_id,
                "catalog pending",
            ),
        ).lastrowid
        con.execute(
            "UPDATE candidate_quality SET quality_status='pending',audit_version=? "
            "WHERE candidate_id=?",
            (QUALITY_VERSION, pending_id),
        )

    audited_batches: list[list[int]] = []

    async def fake_audit_one(candidate, *_args, **_kwargs):
        audited_batches[-1].append(int(candidate["id"]))
        return {
            "candidate_id": int(candidate["id"]),
            "quality_status": "accepted",
            "quality_score": 0.91,
            "flags": [],
            "provider": "openlibrary",
            "provider_id": f"/works/OL{candidate['id']}",
            "work_id": f"/works/OL{candidate['id']}",
            "title_match": 1.0,
            "author_match": 1.0,
        }

    from afterword_engine import quality as quality_module

    monkeypatch.setattr(quality_module, "_audit_one", fake_audit_one)

    # Empty-body manual audit defaults to incremental mode (checking only pending/due rows).
    queued_default = asyncio.run(engine_main.candidate_audit(None))
    assert queued_default["mode"] == "incremental"
    assert queued_default["remaining"] == 1
    summary_during_incremental = engine_main.candidate_quality()
    assert summary_during_incremental["audit"]["mode"] == "incremental"
    assert summary_during_incremental["audit"]["active"] is True
    assert summary_during_incremental["audit"]["remaining"] == 1

    audited_batches.append([])
    with transaction() as con:
        con.execute("DELETE FROM jobs WHERE kind='candidate_quality'")
    inc_result = asyncio.run(handle_job("candidate_quality"))
    assert inc_result["mode"] == "incremental"
    assert inc_result["audited"] == 1
    assert inc_result["remaining"] == 0
    assert inc_result["followup_job_id"] is None
    assert inc_result["score_job_id"] is not None
    assert audited_batches[-1] == [pending_id]

    with transaction() as con:
        con.execute("DELETE FROM jobs WHERE kind='score'")

    # Explicit full mode sweeps all 121 non-curated candidates in 50-row continuation batches.
    queued_full = asyncio.run(
        engine_main.candidate_audit(engine_main.CandidateAuditIn(mode="full"))
    )
    assert queued_full["mode"] == "full"
    assert queued_full["remaining"] == 121

    with transaction() as con:
        con.execute("DELETE FROM jobs WHERE kind='candidate_quality'")

    audited_batches.append([])
    batch_one = asyncio.run(handle_job("candidate_quality"))
    assert batch_one["audited"] == QUALITY_AUDIT_BATCH_SIZE
    assert batch_one["remaining"] == 71
    assert batch_one["followup_job_id"] is not None
    # Unchanged accepted rows in batch 1 do not force mid-sweep rescoring; worker yields cleanly.
    assert batch_one["score_job_id"] is None
    assert (
        row(
            "SELECT COUNT(*) count FROM jobs WHERE kind='candidate_quality' AND status='queued'"
        )["count"]
        == 1
    )

    # Simulate a failed second batch and verify quality_summary surfaces error + retry state.
    failed_job_id = batch_one["followup_job_id"]
    with transaction() as con:
        con.execute(
            "UPDATE jobs SET status='failed',progress=0.2,error='Catalog provider timed out' WHERE id=?",
            (failed_job_id,),
        )
    failed_summary = engine_main.candidate_quality()
    assert failed_summary["audit"]["status"] == "failed"
    assert failed_summary["audit"]["mode"] == "full"
    assert failed_summary["audit"]["remaining"] == 71
    assert failed_summary["audit"]["last_error"] == "Catalog provider timed out"
    assert failed_summary["audit"]["can_retry"] is True

    # Retry preserves sweep_started_at so the first 50 audited rows are not repeated.
    retried = asyncio.run(
        engine_main.candidate_audit(engine_main.CandidateAuditIn(retry=True))
    )
    assert retried["mode"] == "full"
    assert retried["started_at"] == queued_full["started_at"]
    assert retried["remaining"] == 71

    with transaction() as con:
        con.execute("DELETE FROM jobs WHERE kind='candidate_quality' AND status='queued'")

    audited_batches.append([])
    batch_two = asyncio.run(handle_job("candidate_quality"))
    assert batch_two["audited"] == QUALITY_AUDIT_BATCH_SIZE
    assert batch_two["remaining"] == 21
    assert batch_two["followup_job_id"] is not None

    with transaction() as con:
        con.execute("DELETE FROM jobs WHERE kind='candidate_quality' AND status='queued'")

    audited_batches.append([])
    batch_three = asyncio.run(handle_job("candidate_quality"))
    assert batch_three["audited"] == 21
    assert batch_three["remaining"] == 0
    assert batch_three["followup_job_id"] is None
    assert batch_three["score_job_id"] is not None

    full_audited_ids = audited_batches[1] + audited_batches[2] + audited_batches[3]
    assert len(full_audited_ids) == 121
    assert set(full_audited_ids) == set(accepted_ids + [pending_id])
    done_summary = engine_main.candidate_quality()
    assert done_summary["audit"]["status"] == "complete"
    assert done_summary["audit"]["active"] is False
    assert done_summary["audit"]["remaining"] == 0


def test_candidate_quality_queues_score_behind_running_score_and_dedupes_queued(
    database, monkeypatch
):
    from afterword_engine import quality as quality_module

    with transaction() as con:
        source_id = con.execute(
            "SELECT id FROM sources WHERE url='builtin://upcoming'"
        ).fetchone()[0]
        cid = con.execute(
            "INSERT INTO candidates(title,author,source_url,source_id,normalized_key) "
            "VALUES(?,?,?,?,?)",
            (
                "Final Batch Score Book",
                "A Writer",
                "https://example.com/final-score",
                source_id,
                "final batch score book",
            ),
        ).lastrowid
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',audit_version=?,audited_at='2026-01-01 00:00:00' "
            "WHERE candidate_id=?",
            (QUALITY_VERSION, cid),
        )
        con.execute(
            "INSERT INTO jobs(id,kind,status) VALUES('running-score','score','running')"
        )

    async def fake_audit_one(candidate, *_args, **_kwargs):
        return {
            "candidate_id": int(candidate["id"]),
            "quality_status": "accepted",
            "quality_score": 0.91,
            "flags": [],
            "provider": "openlibrary",
            "provider_id": f"/works/OL{candidate['id']}",
            "work_id": f"/works/OL{candidate['id']}",
            "title_match": 1.0,
            "author_match": 1.0,
        }

    monkeypatch.setattr(quality_module, "_audit_one", fake_audit_one)

    # A running score job must not swallow the final batch's score trigger.
    quality_module.configure_candidate_quality_sweep(mode="full")
    with transaction() as con:
        con.execute("DELETE FROM jobs WHERE kind='candidate_quality'")
    first_result = asyncio.run(handle_job("candidate_quality"))
    assert first_result["remaining"] == 0
    assert first_result["score_job_id"] is not None
    assert first_result["score_job_id"] != "running-score"
    queued_scores = rows(
        "SELECT id,status FROM jobs WHERE kind='score' ORDER BY rowid ASC"
    )
    assert queued_scores == [
        {"id": "running-score", "status": "running"},
        {"id": first_result["score_job_id"], "status": "queued"},
    ]

    # When a score job is already queued alongside the running one, dedupe against queued.
    quality_module.configure_candidate_quality_sweep(mode="full")
    with transaction() as con:
        con.execute("DELETE FROM jobs WHERE kind='candidate_quality'")
    second_result = asyncio.run(handle_job("candidate_quality"))
    assert second_result["score_job_id"] == first_result["score_job_id"]
    assert (
        row("SELECT COUNT(*) AS count FROM jobs WHERE kind='score' AND status='queued'")[
            "count"
        ]
        == 1
    )


def test_retry_resumes_stored_full_sweep_after_crash_without_repeating_committed_rows(
    database, monkeypatch
):
    from afterword_engine import profiles
    from afterword_engine import quality as quality_module
    from afterword_engine.jobs import worker_loop

    monkeypatch.setattr(profiles, "profile_ids", lambda: ["legacy"])
    monkeypatch.setattr(
        engine_main, "score_all", lambda *_a, **_k: asyncio.sleep(0, result=1)
    )

    candidate_ids: list[int] = []
    with transaction() as con:
        source_id = con.execute(
            "SELECT id FROM sources WHERE url='builtin://upcoming'"
        ).fetchone()[0]
        for index in range(65):
            cid = con.execute(
                "INSERT INTO candidates(title,author,source_url,source_id,normalized_key) "
                "VALUES(?,?,?,?,?)",
                (
                    f"Crashed Full Sweep {index}",
                    "Sweep Writer",
                    "https://example.com/crashed-full",
                    source_id,
                    f"crashed full sweep {index}",
                ),
            ).lastrowid
            candidate_ids.append(cid)
            con.execute(
                "UPDATE candidate_quality SET quality_status='accepted',audit_version=?,audited_at='2026-01-01 00:00:00' "
                "WHERE candidate_id=?",
                (QUALITY_VERSION, cid),
            )

    queued_full = asyncio.run(
        engine_main.candidate_audit(engine_main.CandidateAuditIn(mode="full"))
    )
    sweep_started_at = queued_full["started_at"]
    initial_job_id = queued_full["job_id"]
    assert queued_full["mode"] == "full"
    assert queued_full["remaining"] == 65

    seen_before_crash: list[int] = []
    seen_after_retry: list[int] = []
    should_crash = True

    async def crashing_audit_one(candidate, *_args, **_kwargs):
        cid = int(candidate["id"])
        if should_crash:
            if cid == candidate_ids[20]:
                raise RuntimeError("Simulated crash at row 21 during full sweep")
            seen_before_crash.append(cid)
        else:
            seen_after_retry.append(cid)
        return {
            "candidate_id": cid,
            "quality_status": "accepted",
            "quality_score": 0.93,
            "flags": [],
            "provider": "openlibrary",
            "provider_id": f"/works/OL{cid}",
            "work_id": f"/works/OL{cid}",
            "title_match": 1.0,
            "author_match": 1.0,
        }

    monkeypatch.setattr(quality_module, "_audit_one", crashing_audit_one)

    crash_stop = asyncio.Event()

    async def crash_once_handler(kind: str):
        try:
            return await handle_job(kind)
        finally:
            if kind == "candidate_quality":
                crash_stop.set()

    asyncio.run(worker_loop(crash_once_handler, crash_stop))

    failed_job = row("SELECT status,error FROM jobs WHERE id=?", (initial_job_id,))
    assert failed_job["status"] == "failed"
    assert "Simulated crash at row 21" in failed_job["error"]

    committed_ids = [
        item["candidate_id"]
        for item in rows(
            "SELECT candidate_id FROM candidate_quality "
            "WHERE julianday(audited_at)>=julianday(?) ORDER BY candidate_id",
            (sweep_started_at,),
        )
    ]
    assert committed_ids == candidate_ids[:20]

    crashed_summary = engine_main.candidate_quality()
    assert crashed_summary["audit"]["status"] == "failed"
    assert crashed_summary["audit"]["active"] is False
    assert crashed_summary["audit"]["mode"] == "full"
    assert crashed_summary["audit"]["remaining"] == 45
    assert crashed_summary["audit"]["can_retry"] is True

    # Non-retry default call with no active job preserves the unfinished full sweep instead of overwriting it.
    preserved = asyncio.run(engine_main.candidate_audit(None))
    assert preserved["mode"] == "full"
    assert preserved["started_at"] == sweep_started_at
    assert preserved["remaining"] == 45
    with transaction() as con:
        con.execute("DELETE FROM jobs WHERE id=?", (preserved["job_id"],))

    # Explicit retry=True resumes the stored full sweep from row 21 without repeating committed rows 1..20.
    should_crash = False
    retried = asyncio.run(
        engine_main.candidate_audit(engine_main.CandidateAuditIn(retry=True))
    )
    assert retried["mode"] == "full"
    assert retried["started_at"] == sweep_started_at
    assert retried["remaining"] == 45

    resume_stop = asyncio.Event()

    async def resume_handler(kind: str):
        result = await handle_job(kind)
        if kind == "candidate_quality" and result["remaining"] == 0:
            resume_stop.set()
        return result

    asyncio.run(worker_loop(resume_handler, resume_stop))

    assert seen_after_retry == candidate_ids[20:]
    completed_summary = engine_main.candidate_quality()
    assert completed_summary["audit"]["status"] == "complete"
    assert completed_summary["audit"]["active"] is False
    assert completed_summary["audit"]["remaining"] == 0
    assert (
        row("SELECT COUNT(*) AS count FROM jobs WHERE kind='score' AND status='queued'")[
            "count"
        ]
        >= 1
    )


def test_candidate_audit_http_endpoint_accepts_empty_body_and_explicit_modes(
    database, monkeypatch
):
    from fastapi.testclient import TestClient
    from afterword_engine import profiles

    monkeypatch.delenv("AFTERWORD_SECRET_KEY", raising=False)
    profiles.initialize_auth()
    admin = profiles.create_first_admin("owner", "owner-password-with-enough-length")
    session, _ = profiles.create_session(admin["id"])
    headers = {"x-bookward-session": session}

    client = TestClient(engine_main.app)
    # Empty-body POST with no JSON payload remains valid and defaults to incremental.
    empty_res = client.post("/api/candidates/audit", headers=headers)
    assert empty_res.status_code == 200
    empty_payload = empty_res.json()
    assert empty_payload["mode"] == "incremental"
    assert empty_payload["job_id"]

    quality_res = client.get("/api/candidates/quality", headers=headers)
    assert quality_res.status_code == 200
    quality_payload = quality_res.json()
    assert quality_payload["audit"]["mode"] == "incremental"
    assert quality_payload["audit"]["active"] is True

    # Explicit full mode upgrades an active incremental sweep to full.
    full_res = client.post(
        "/api/candidates/audit", json={"mode": "full"}, headers=headers
    )
    assert full_res.status_code == 200
    assert full_res.json()["mode"] == "full"

    invalid_res = client.post(
        "/api/candidates/audit", json={"mode": "bogus"}, headers=headers
    )
    assert invalid_res.status_code == 422


def test_metadata_worker_requeues_past_first_candidate_batch_and_scores_changes(database, monkeypatch):
    candidate_ids = []
    with transaction() as con:
        source_id = con.execute("SELECT id FROM sources WHERE url='builtin://upcoming'").fetchone()[0]
        for index in range(51):
            candidate_id = con.execute(
                "INSERT INTO candidates(title,author,source_url,source_id,normalized_key) "
                "VALUES(?,?,?,?,?)",
                (f"Pending metadata {index}", "A Writer", "https://example.com", source_id, f"pending metadata {index}"),
            ).lastrowid
            candidate_ids.append(candidate_id)
            con.execute(
                "UPDATE candidate_quality SET quality_status='accepted' WHERE candidate_id=?",
                (candidate_id,),
            )

    calls = []

    async def enrich(items):
        calls.extend(item["id"] for item in items)
        enriched = [dict(item) for item in items]
        first = enriched[0]
        first.update({
            "description": "A newly available synopsis.",
            "_description_provider": "google_books",
            "_description_provider_id": "volume:pending",
            "_metadata_provenance": {
                "fields": {"description": {"provider": "google_books", "provider_id": "volume:pending"}},
                "source_payloads": [{
                    "provider": "google_books",
                    "provider_id": "volume:pending",
                    "payload": {"description": "A newly available synopsis."},
                }],
            },
        })
        return enriched

    monkeypatch.setattr(ingestion, "enrich_book_metadata", enrich)

    first = asyncio.run(handle_job("metadata"))
    assert len(calls) == 50
    assert first["remaining"] == 1
    assert first["score_job_id"]
    assert row("SELECT status FROM jobs WHERE id=?", (first["score_job_id"],))["status"] == "queued"

    second = asyncio.run(handle_job("metadata"))
    assert len(calls) == 51
    assert second["remaining"] == 0
    assert set(calls) == set(candidate_ids)
    assert row(
        "SELECT metadata_version FROM candidate_quality WHERE candidate_id=?",
        (candidate_ids[-1],),
    )["metadata_version"] == METADATA_NORMALIZATION_VERSION


def test_genre_filter_enriches_untagged_items_and_never_replaces_real_cover(database, monkeypatch):
    async def fake_fetch(_url):
        return "application/json", [{
            "title": "Catalog tagged book",
            "author": "A Writer",
            "genres": [],
            "cover_url": "https://placehold.co/400x600?text=temporary",
        }]

    async def enrich(items):
        assert len(items) == 1
        return [{**items[0], "genres": ["Fantasy"]}]

    monkeypatch.setattr(ingestion, "_fetch_and_parse_source", fake_fetch)
    monkeypatch.setattr(ingestion, "enrich_penguin_random_house_items", enrich)
    monkeypatch.setattr(ingestion, "enrich_book_metadata", enrich)
    with transaction() as con:
        source_id = con.execute(
            "INSERT INTO sources(name,url,filters) VALUES(?,?,?)",
            ("Fantasy source", "https://example.com/fantasy", json.dumps({"include_genres": ["fantasy"]})),
        ).lastrowid
        con.execute(
            "INSERT INTO candidates(title,author,cover_url,source_id,status,normalized_key) "
            "VALUES(?,?,?,?,?,?)",
            (
                "Catalog tagged book", "A Writer", "https://images.example.com/real.jpg",
                source_id, "new", "catalog tagged book a writer",
            ),
        )

    assert asyncio.run(ingestion.scan_source(row("SELECT * FROM sources WHERE id=?", (source_id,)))) == 1
    candidate = row("SELECT genres,cover_url FROM candidates WHERE normalized_key='catalog tagged book a writer'")
    assert json.loads(candidate["genres"]) == ["Fantasy"]
    assert candidate["cover_url"] == "https://images.example.com/real.jpg"


def test_scan_source_preserves_verified_release_date_and_applies_feed_corrections(database, monkeypatch):
    import hashlib
    from afterword_engine.database import connect, persist_metadata_field_provenance
    from afterword_engine.publication_year_metadata import attach_publication_years

    classic_title = "Murder on the Orient Express"
    classic_author = "Agatha Christie"
    work_id = "/works/OL471579W"
    verified_date = "1934-01-01"
    expected_sha = hashlib.sha256(verified_date.encode("utf-8")).hexdigest()

    mixed_title = "Death on the Nile"
    mixed_work_id = "/works/OL471580W"
    mixed_date = "1937-01-01"
    mixed_sha = hashlib.sha256(mixed_date.encode("utf-8")).hexdigest()

    feed_title = "Forthcoming Mystery"
    feed_author = "Modern Crime Writer"
    feed_date_state = ["2026-11-01", "day"]

    async def fake_fetch(_url):
        feed_item = {
            "title": feed_title,
            "author": feed_author,
            "genres": ["Mystery"],
        }
        if feed_date_state[0] is not None:
            feed_item["release_date"] = feed_date_state[0]
            feed_item["date_kind"] = feed_date_state[1]
        return "application/json", [
            {
                "title": classic_title,
                "author": classic_author,
                "release_date": "2023-12-25",
                "date_kind": "day",
                "genres": ["Mystery"],
            },
            {
                "title": mixed_title,
                "author": classic_author,
                "release_date": "2024-06-01",
                "date_kind": "day",
                "genres": ["Mystery"],
            },
            feed_item,
        ]

    async def passthrough(items):
        return items

    monkeypatch.setattr(ingestion, "_fetch_and_parse_source", fake_fetch)
    monkeypatch.setattr(ingestion, "enrich_penguin_random_house_items", passthrough)
    monkeypatch.setattr(ingestion, "enrich_book_metadata", passthrough)

    classic_key = ingestion.normalize_key(classic_title, classic_author)
    mixed_key = ingestion.normalize_key(mixed_title, classic_author)
    feed_key = ingestion.normalize_key(feed_title, feed_author)
    with transaction() as con:
        source_id = con.execute(
            "INSERT INTO sources(name,url,filters) VALUES(?,?,?)",
            ("Mystery feed", "https://example.com/mystery", "{}"),
        ).lastrowid
        classic_id = con.execute(
            "INSERT INTO candidates(title,author,release_date,date_kind,genres,source_id,status,normalized_key) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (classic_title, classic_author, verified_date, "year", json.dumps(["Mystery"]), source_id, "recommended", classic_key),
        ).lastrowid
        mixed_id = con.execute(
            "INSERT INTO candidates(title,author,release_date,date_kind,genres,source_id,status,normalized_key) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (mixed_title, classic_author, mixed_date, "year", json.dumps(["Mystery"]), source_id, "recommended", mixed_key),
        ).lastrowid
        for cid, wid, yr, sha in (
            (classic_id, work_id, "1934", expected_sha),
            (mixed_id, mixed_work_id, "1937", mixed_sha),
        ):
            con.execute(
                "INSERT INTO candidate_quality(candidate_id,quality_status,quality_score,provider,provider_id,work_id,audit_version) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(candidate_id) DO UPDATE SET "
                "quality_status=excluded.quality_status,provider=excluded.provider,provider_id=excluded.provider_id,work_id=excluded.work_id",
                (cid, "accepted", 0.96, "openlibrary", wid, wid, "test"),
            )
            persist_metadata_field_provenance(
                con,
                "candidate",
                cid,
                "release_date",
                {
                    "metadata_provenance": {
                        "fields": {
                            "release_date": {
                                "provider": "openlibrary",
                                "provider_id": wid,
                                "kind": "year",
                                "source_field": "first_publish_year",
                                "value_sha256": sha,
                            }
                        },
                        "fetch_trace": [],
                        "source_payloads": [{
                            "provider": "openlibrary",
                            "provider_id": wid,
                            "payload": {
                                "key": wid,
                                "title": classic_title if cid == classic_id else mixed_title,
                                "authors": [classic_author],
                                "publication_date": yr,
                                "publication_date_source_field": "first_publish_year",
                            },
                        }],
                    }
                },
                default_provider="openlibrary",
                default_provider_id=wid,
                default_confidence=0.96,
            )
        # Mixed provenance set on mixed_id: source + openlibrary.
        persist_metadata_field_provenance(
            con,
            "candidate",
            mixed_id,
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

    source_row = row("SELECT * FROM sources WHERE id=?", (source_id,))
    assert asyncio.run(ingestion.scan_source(source_row)) == 3

    # Initial ingest sets source-owned release_date on feed_title while keeping
    # catalog-verified and mixed-provenance work dates intact.
    assert row("SELECT release_date,date_kind FROM candidates WHERE normalized_key=?", (feed_key,)) == {
        "release_date": "2026-11-01",
        "date_kind": "day",
    }

    # Feed correction updates source-owned release_date and date_kind on rescan.
    feed_date_state[:] = ["2027-02-01", "month"]
    assert asyncio.run(ingestion.scan_source(source_row)) == 3
    assert row("SELECT release_date,date_kind FROM candidates WHERE normalized_key=?", (feed_key,)) == {
        "release_date": "2027-02-01",
        "date_kind": "month",
    }
    feed_prov = rows(
        "SELECT provider FROM metadata_field_provenance "
        "WHERE entity_type='candidate' AND entity_id=(SELECT id FROM candidates WHERE normalized_key=?) "
        "AND field='release_date'",
        (feed_key,),
    )
    assert [item["provider"] for item in feed_prov] == ["source"]

    # Feed rescan omitting release_date preserves the corrected source date.
    feed_date_state[:] = [None, ""]
    assert asyncio.run(ingestion.scan_source(source_row)) == 3
    assert row("SELECT release_date,date_kind FROM candidates WHERE normalized_key=?", (feed_key,)) == {
        "release_date": "2027-02-01",
        "date_kind": "month",
    }

    stored_classic = row("SELECT id,release_date,date_kind FROM candidates WHERE id=?", (classic_id,))
    stored_mixed = row("SELECT id,release_date,date_kind FROM candidates WHERE id=?", (mixed_id,))
    assert stored_classic["release_date"] == "1934-01-01"
    assert stored_classic["date_kind"] == "year"
    assert stored_mixed["release_date"] == "1937-01-01"
    assert stored_mixed["date_kind"] == "year"

    mixed_prov = rows(
        "SELECT provider FROM metadata_field_provenance "
        "WHERE entity_type='candidate' AND entity_id=? AND field='release_date' ORDER BY provider",
        (mixed_id,),
    )
    assert [item["provider"] for item in mixed_prov] == ["openlibrary", "source"]

    mutable = [dict(stored_classic), dict(stored_mixed)]
    with connect() as con:
        attach_publication_years(con, [], mutable)
    assert mutable[0]["first_publication_year"] == 1934
    assert mutable[0]["publication_work_id"] == work_id
    assert mutable[1]["first_publication_year"] == 1937
    assert mutable[1]["publication_work_id"] == mixed_work_id


def test_scan_source_preserves_legacy_release_dates_without_provenance_rows(database, monkeypatch):
    """Legacy catalog/manual dates with no provenance rows must not be overwritten by feed rescans."""
    from datetime import datetime, timezone

    legacy_day_title = "The Catcher in the Rye"
    legacy_day_author = "J. D. Salinger"
    legacy_year_title = "Lord of the Flies"
    legacy_year_author = "William Golding"
    source_owned_title = "Upcoming Debut"
    source_owned_author = "New Novelist"

    year_feed_date = ["1954-01-01", "day"]
    source_feed_date = ["2026-09-01", "month"]

    async def fake_fetch(_url):
        return "application/json", [
            {
                "title": legacy_day_title,
                "author": legacy_day_author,
                "release_date": "2024-10-15",
                "date_kind": "day",
                "genres": ["Fiction"],
            },
            {
                "title": legacy_year_title,
                "author": legacy_year_author,
                "release_date": year_feed_date[0],
                "date_kind": year_feed_date[1],
                "genres": ["Fiction"],
            },
            {
                "title": source_owned_title,
                "author": source_owned_author,
                "release_date": source_feed_date[0],
                "date_kind": source_feed_date[1],
                "genres": ["Fiction"],
            },
        ]

    async def passthrough(items):
        return items

    monkeypatch.setattr(ingestion, "_fetch_and_parse_source", fake_fetch)
    monkeypatch.setattr(ingestion, "enrich_penguin_random_house_items", passthrough)
    monkeypatch.setattr(ingestion, "enrich_book_metadata", passthrough)
    # Force one-item enrichment chunks so per-chunk commits and unenriched
    # remaining_items commits both run during scan_source.
    monkeypatch.setattr(ingestion, "SOURCE_METADATA_ENRICHMENT_BATCH_SIZE", 1)

    legacy_day_key = ingestion.normalize_key(legacy_day_title, legacy_day_author)
    legacy_year_key = ingestion.normalize_key(legacy_year_title, legacy_year_author)
    source_owned_key = ingestion.normalize_key(source_owned_title, source_owned_author)
    fresh_checked_at = datetime.now(timezone.utc).isoformat(timespec="microseconds")

    with transaction() as con:
        source_id = con.execute(
            "INSERT INTO sources(name,url,filters) VALUES(?,?,?)",
            ("Fiction feed", "https://example.com/fiction", "{}"),
        ).lastrowid
        legacy_day_id = con.execute(
            "INSERT INTO candidates(title,author,release_date,date_kind,genres,source_id,status,normalized_key) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (legacy_day_title, legacy_day_author, "1951-07-16", "day", json.dumps(["Fiction"]), source_id, "recommended", legacy_day_key),
        ).lastrowid
        legacy_year_id = con.execute(
            "INSERT INTO candidates(title,author,description,cover_url,release_date,date_kind,genres,source_id,status,normalized_key) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                legacy_year_title,
                legacy_year_author,
                "Existing synopsis.",
                "https://images.example.com/lotf.jpg",
                "1954-01-01",
                "year",
                json.dumps(["Fiction"]),
                source_id,
                "recommended",
                legacy_year_key,
            ),
        ).lastrowid
        con.execute(
            "INSERT INTO candidate_quality(candidate_id,quality_status,quality_score,metadata_checked_at,metadata_version,audit_version) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(candidate_id) DO UPDATE SET "
            "metadata_checked_at=excluded.metadata_checked_at,metadata_version=excluded.metadata_version",
            (legacy_year_id, "accepted", 0.95, fresh_checked_at, ingestion.METADATA_NORMALIZATION_VERSION, "test"),
        )

    source_row = row("SELECT * FROM sources WHERE id=?", (source_id,))
    assert asyncio.run(ingestion.scan_source(source_row)) == 3

    # Neither legacy candidate has provenance rows, so neither date nor date_kind
    # may be overwritten or claimed by source provenance (even when scan 1 sent
    # the matching date string 1954-01-01 for legacy_year_id).
    assert row("SELECT release_date,date_kind FROM candidates WHERE id=?", (legacy_day_id,)) == {
        "release_date": "1951-07-16",
        "date_kind": "day",
    }
    assert row("SELECT release_date,date_kind FROM candidates WHERE id=?", (legacy_year_id,)) == {
        "release_date": "1954-01-01",
        "date_kind": "year",
    }
    assert row("SELECT release_date,date_kind FROM candidates WHERE normalized_key=?", (source_owned_key,)) == {
        "release_date": "2026-09-01",
        "date_kind": "month",
    }

    # Rescan with updated feed dates: source-owned candidate updates, while both
    # no-provenance candidates stay untouched with zero release_date provenance rows.
    year_feed_date[:] = ["2025-03-01", "day"]
    source_feed_date[:] = ["2026-11-15", "day"]
    assert asyncio.run(ingestion.scan_source(source_row)) == 3

    assert row("SELECT release_date,date_kind FROM candidates WHERE id=?", (legacy_day_id,)) == {
        "release_date": "1951-07-16",
        "date_kind": "day",
    }
    assert row("SELECT release_date,date_kind FROM candidates WHERE id=?", (legacy_year_id,)) == {
        "release_date": "1954-01-01",
        "date_kind": "year",
    }
    assert rows(
        "SELECT provider FROM metadata_field_provenance "
        "WHERE entity_type='candidate' AND entity_id IN (?,?) AND field='release_date'",
        (legacy_day_id, legacy_year_id),
    ) == []
    assert row("SELECT release_date,date_kind FROM candidates WHERE normalized_key=?", (source_owned_key,)) == {
        "release_date": "2026-11-15",
        "date_kind": "day",
    }


def test_migration_adds_enrichment_storage_without_changing_existing_user_data(tmp_path):
    settings.db = str(tmp_path / "legacy-v17.db")
    con = sqlite3.connect(settings.db)
    con.execute("CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
    for version, script in MIGRATIONS:
        if version >= 18:
            break
        con.executescript(script)
        con.execute("INSERT INTO schema_migrations(version) VALUES(?)", (version,))
    con.execute(
        "INSERT INTO reads(title,author,rating,isbn,source) VALUES(?,?,?,?,?)",
        ("Preserved read", "Reader", 4.75, "9780307474278", "goodreads_csv"),
    )
    con.execute(
        "INSERT INTO settings(key,value,secret) VALUES('custom_api_secret','sealed-secret',1)"
    )
    con.commit()
    con.close()

    initialize(seed_demo=False)

    assert row("SELECT rating,isbn FROM reads WHERE title='Preserved read'") == {
        "rating": 4.75,
        "isbn": "9780307474278",
    }
    assert row("SELECT value,secret FROM settings WHERE key='custom_api_secret'") == {
        "value": "sealed-secret",
        "secret": 1,
    }
    assert row("SELECT version FROM schema_migrations WHERE version=18") == {"version": 18}
    assert "metadata_provenance" in {
        item["name"] for item in rows("PRAGMA table_info(read_metadata)")
    }
    assert row("SELECT name FROM sqlite_master WHERE type='table' AND name='read_metadata'")


def test_migration_32_decouples_work_ids_verifies_source_isbns_and_requeues_only_non_user_overlaps(tmp_path):
    from afterword_engine.database import _execute_migration_script
    from afterword_engine.identity import ReadIdentityIndex
    from afterword_engine.main import recommendation_list

    settings.db = str(tmp_path / "legacy-v31.db")
    con = sqlite3.connect(settings.db)
    con.execute(
        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    for version, script in MIGRATIONS:
        if version >= 32:
            break
        _execute_migration_script(con, script)
        con.execute("INSERT INTO schema_migrations(version) VALUES(?)", (version,))
    con.commit()

    source_id = con.execute(
        "INSERT INTO sources(name,url) VALUES('Test Feed','https://example.com/feed')"
    ).lastrowid

    def insert_legacy(
        title,
        *,
        status="new",
        source_url="",
        c_isbn13="",
        c_isbn10="",
        q_status="accepted",
        provider="openlibrary",
        work_id="",
        q_isbn13="",
        q_isbn10="",
        flags="[]",
        score=85.0,
    ):
        cid = con.execute(
            "INSERT INTO candidates(title,author,source_url,source_id,status,isbn13,isbn10,score,normalized_key) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (
                title,
                "Writer",
                source_url,
                source_id,
                status,
                c_isbn13,
                c_isbn10,
                score,
                title.lower().replace(" ", "-"),
            ),
        ).lastrowid
        con.execute(
            "UPDATE candidate_quality SET quality_status=?,provider=?,work_id=?,isbn13=?,isbn10=?,flags_json=?,audit_version='candidate-quality-v4' "
            "WHERE candidate_id=?",
            (q_status, provider, work_id, q_isbn13, q_isbn10, flags, cid),
        )
        return cid

    ol_verified = insert_legacy(
        "OL Verified Work",
        c_isbn13="9780307474278",
        work_id="/works/OL100W",
        q_isbn13="9780307474278",
    )
    cross_978_c10_q13 = insert_legacy(
        "Cross 978 C10 To Q13",
        c_isbn10="0307474275",
        q_isbn13="9780307474278",
    )
    cross_978_c13_q10 = insert_legacy(
        "Cross 978 C13 To Q10",
        c_isbn13="9780307474278",
        q_isbn10="0307474275",
    )
    direct_979 = insert_legacy(
        "Direct 979 Match",
        c_isbn13="9791032305690",
        q_isbn13="9791032305690",
    )
    non_equiv_979 = insert_legacy(
        "979 Non Equivalent Stem",
        c_isbn10="0307474275",
        q_isbn13="9790307474277",
    )
    lowercase_x_direct = insert_legacy(
        "Lowercase x ISBN10 Direct",
        c_isbn10="043942089x",
        q_isbn10="043942089X",
    )
    lowercase_x_cross = insert_legacy(
        "Lowercase x ISBN10 Cross 978",
        c_isbn10="043942089x",
        q_isbn13="9780439420891",
    )
    gb_with_source_work = insert_legacy(
        "Google With Source Work",
        status="recommended",
        source_url="https://openlibrary.org/works/OL200W/Book_Title?edition=key%3A/books/OL1M#details",
        c_isbn13="9780804172448",
        c_isbn10="043942089X",
        provider="google_books",
        work_id="gb-volume-42",
        q_isbn13="9780143127550",
        q_isbn10="0307474275",
    )
    ol_edition = insert_legacy(
        "OL Edition Key",
        provider="openlibrary",
        work_id="/books/OL300M",
    )
    requeued_overlap = insert_legacy(
        "False Box Set Read Overlap",
        status="rejected",
        q_status="rejected",
        q_isbn13="9780345538376",
        q_isbn10="0345538374",
        flags='["catalog_match_ambiguous","read_overlap","short_description"]',
    )
    requeued_dup = insert_legacy(
        "False Duplicate Identity",
        status="rejected",
        q_status="rejected",
        q_isbn13="9780345538376",
        flags='["duplicate_identity"]',
    )
    hard_flag_overlap = insert_legacy(
        "Hard Flag Plus Overlap",
        status="rejected",
        q_status="rejected",
        q_isbn13="9780345538376",
        flags='["unknown_author","read_overlap"]',
    )
    no_quality_isbn_overlap = insert_legacy(
        "Title Author Read Overlap Without Quality ISBN",
        status="rejected",
        q_status="rejected",
        flags='["read_overlap"]',
    )
    url_query_work_requeued = insert_legacy(
        "Source URL Work With Query And Unverified ISBN Overlap",
        status="rejected",
        source_url="https://openlibrary.org/works/OL777W?edition=1",
        q_status="rejected",
        q_isbn13="9780345538376",
        flags='["read_overlap"]',
    )
    source_isbn_overlap = insert_legacy(
        "True Source ISBN Overlap",
        status="rejected",
        c_isbn13="9780307474278",
        q_status="rejected",
        q_isbn13="9780307474278",
        flags='["read_overlap"]',
    )
    user_rejected = insert_legacy(
        "User Explicitly Rejected",
        status="rejected",
        q_status="rejected",
        q_isbn13="9780345538376",
        flags='["read_overlap"]',
    )
    con.execute(
        "INSERT INTO feedback(candidate_id,action) VALUES(?,'reject')",
        (user_rejected,),
    )
    reject_then_restored = insert_legacy(
        "User Rejected Then Restored",
        status="rejected",
        q_status="rejected",
        q_isbn13="9780345538376",
        flags='["read_overlap"]',
    )
    con.execute(
        "INSERT INTO feedback(candidate_id,action) VALUES(?,'reject')",
        (reject_then_restored,),
    )
    con.execute(
        "INSERT INTO feedback(candidate_id,action) VALUES(?,'restore')",
        (reject_then_restored,),
    )
    undone_reject = insert_legacy(
        "User Undid Reject",
        status="rejected",
        q_status="rejected",
        q_isbn13="9780345538376",
        flags='["duplicate_identity"]',
    )
    con.execute(
        "INSERT INTO feedback(candidate_id,action,undone_at) VALUES(?,'reject','2026-02-01T00:00:00+00:00')",
        (undone_reject,),
    )
    saved_row = insert_legacy("Saved Shortlist Book", status="saved", q_isbn13="9780345538376")
    imported_row = insert_legacy("Imported Book", status="imported", q_isbn13="9780345538376")
    read_row = insert_legacy("Completed Read Book", status="read", q_isbn13="9780345538376")
    amazon_979_pending = insert_legacy(
        "Amazon 979 Pending Backfill",
        source_url="https://www.amazon.com/dp/9791032305690",
        q_status="pending",
    )
    amazon_requeued_conflict = insert_legacy(
        "Amazon URL With Conflicting Quality ISBN",
        status="rejected",
        source_url="https://www.amazon.com/dp/0307474275",
        q_status="rejected",
        q_isbn13="9780345538376",
        flags='["read_overlap"]',
    )
    con.execute(
        "INSERT INTO reads(title,author,rating,isbn,source) VALUES('Different Read Title','Different Author',5,'9780143127550','goodreads_csv')"
    )
    con.commit()
    con.close()

    initialize(seed_demo=False)

    assert row("SELECT version FROM schema_migrations WHERE version=31") == {"version": 31}
    assert row("SELECT version FROM schema_migrations WHERE version=32") == {"version": 32}
    quality_columns = {
        col["name"]: col for col in rows("PRAGMA table_info(candidate_quality)")
    }
    assert "openlibrary_work_id" in quality_columns
    assert "isbn_verified" in quality_columns
    assert row(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_candidate_quality_openlibrary_work_id'"
    ) == {"name": "idx_candidate_quality_openlibrary_work_id"}
    assert row(
        "SELECT openlibrary_work_id,work_id,isbn_verified FROM candidate_quality WHERE candidate_id=?",
        (ol_verified,),
    ) == {
        "openlibrary_work_id": "/works/OL100W",
        "work_id": "/works/OL100W",
        "isbn_verified": 1,
    }
    for verified_cid in (
        cross_978_c10_q13,
        cross_978_c13_q10,
        direct_979,
        lowercase_x_direct,
        lowercase_x_cross,
    ):
        assert row(
            "SELECT isbn_verified FROM candidate_quality WHERE candidate_id=?",
            (verified_cid,),
        ) == {"isbn_verified": 1}
    assert row(
        "SELECT isbn_verified FROM candidate_quality WHERE candidate_id=?",
        (non_equiv_979,),
    ) == {"isbn_verified": 0}

    # Non-destructive preservation of candidates.isbn10, candidate_quality.isbn10, and work_id
    assert row("SELECT isbn13,isbn10 FROM candidates WHERE id=?", (gb_with_source_work,)) == {
        "isbn13": "9780804172448",
        "isbn10": "043942089X",
    }
    assert row(
        "SELECT openlibrary_work_id,work_id,isbn13,isbn10,isbn_verified FROM candidate_quality WHERE candidate_id=?",
        (gb_with_source_work,),
    ) == {
        "openlibrary_work_id": "/works/OL200W",
        "work_id": "gb-volume-42",
        "isbn13": "9780143127550",
        "isbn10": "0307474275",
        "isbn_verified": 0,
    }
    assert row(
        "SELECT openlibrary_work_id,work_id FROM candidate_quality WHERE candidate_id=?",
        (ol_edition,),
    ) == {"openlibrary_work_id": "", "work_id": "/books/OL300M"}

    # Requeued rows keep quality ISBNs (with isbn_verified=0) and retain non-overlap flags
    assert row("SELECT status FROM candidates WHERE id=?", (requeued_overlap,)) == {"status": "new"}
    requeued_overlap_q = row(
        "SELECT quality_status,isbn13,isbn10,isbn_verified,flags_json FROM candidate_quality WHERE candidate_id=?",
        (requeued_overlap,),
    )
    assert requeued_overlap_q["quality_status"] == "pending"
    assert requeued_overlap_q["isbn13"] == "9780345538376"
    assert requeued_overlap_q["isbn10"] == "0345538374"
    assert requeued_overlap_q["isbn_verified"] == 0
    assert json.loads(requeued_overlap_q["flags_json"]) == [
        "catalog_match_ambiguous",
        "short_description",
    ]

    for cid in (requeued_dup, reject_then_restored, undone_reject):
        assert row("SELECT status FROM candidates WHERE id=?", (cid,)) == {"status": "new"}
        assert row(
            "SELECT quality_status,isbn13,isbn_verified,flags_json FROM candidate_quality WHERE candidate_id=?",
            (cid,),
        ) == {
            "quality_status": "pending",
            "isbn13": "9780345538376",
            "isbn_verified": 0,
            "flags_json": "[]",
        }

    assert row("SELECT status FROM candidates WHERE id=?", (url_query_work_requeued,)) == {
        "status": "new"
    }
    assert row(
        "SELECT openlibrary_work_id,quality_status,isbn13,isbn_verified,flags_json FROM candidate_quality WHERE candidate_id=?",
        (url_query_work_requeued,),
    ) == {
        "openlibrary_work_id": "/works/OL777W",
        "quality_status": "pending",
        "isbn13": "9780345538376",
        "isbn_verified": 0,
        "flags_json": "[]",
    }

    for still_rejected_cid in (
        hard_flag_overlap,
        no_quality_isbn_overlap,
        source_isbn_overlap,
        user_rejected,
    ):
        assert row("SELECT status FROM candidates WHERE id=?", (still_rejected_cid,)) == {
            "status": "rejected"
        }
        assert row(
            "SELECT quality_status FROM candidate_quality WHERE candidate_id=?",
            (still_rejected_cid,),
        ) == {"quality_status": "rejected"}

    assert row("SELECT status FROM candidates WHERE id=?", (saved_row,)) == {"status": "saved"}
    assert row("SELECT status FROM candidates WHERE id=?", (imported_row,)) == {"status": "imported"}
    assert row("SELECT status FROM candidates WHERE id=?", (read_row,)) == {"status": "read"}
    assert row(
        "SELECT isbn13,isbn10,isbn_verified FROM candidate_quality WHERE candidate_id=?",
        (amazon_979_pending,),
    ) == {"isbn13": "9791032305690", "isbn10": "", "isbn_verified": 1}
    assert row(
        "SELECT status,isbn13,isbn10 FROM candidates WHERE id=?",
        (amazon_requeued_conflict,),
    ) == {"status": "new", "isbn13": "9780307474278", "isbn10": "0307474275"}
    assert row(
        "SELECT quality_status,isbn13,isbn10,isbn_verified FROM candidate_quality WHERE candidate_id=?",
        (amazon_requeued_conflict,),
    ) == {
        "quality_status": "pending",
        "isbn13": "9780345538376",
        "isbn10": "",
        "isbn_verified": 0,
    }

    # Pre-v6 accepted row keeps catalog-ISBN dedupe active until v6 re-audit, while pending requeued row does not
    visible_ids = {item["id"] for item in recommendation_list(status="recommended", limit=None)}
    assert gb_with_source_work not in visible_ids
    box_set_index = ReadIdentityIndex.from_reads(
        [{"title": "Box Set Read", "author": "Other", "isbn": "9780345538376"}]
    )
    assert not box_set_index.matches(
        {
            "title": "False Box Set Read Overlap",
            "author": "Writer",
            "quality_status": "pending",
            "quality_audit_version": "candidate-quality-v4",
            "quality_isbn13": "9780345538376",
            "quality_isbn10": "0345538374",
            "quality_isbn_verified": 0,
        }
    )

    # Idempotency check on second initialize(): post-migration rejections and rows are untouched
    with transaction() as con:
        con.execute("UPDATE candidates SET status='rejected' WHERE id=?", (requeued_dup,))
        con.execute(
            "UPDATE candidate_quality SET quality_status='rejected',audit_version=?,flags_json='[\"duplicate_identity\"]' "
            "WHERE candidate_id=?",
            (QUALITY_VERSION, requeued_dup),
        )
    snapshot_candidates = rows("SELECT id,status,isbn13,isbn10,updated_at FROM candidates ORDER BY id")
    snapshot_quality = rows(
        "SELECT candidate_id,quality_status,provider,work_id,openlibrary_work_id,isbn13,isbn10,isbn_verified,flags_json,audit_version,updated_at "
        "FROM candidate_quality ORDER BY candidate_id"
    )

    initialize(seed_demo=False)

    assert rows("SELECT id,status,isbn13,isbn10,updated_at FROM candidates ORDER BY id") == snapshot_candidates
    assert (
        rows(
            "SELECT candidate_id,quality_status,provider,work_id,openlibrary_work_id,isbn13,isbn10,isbn_verified,flags_json,audit_version,updated_at "
            "FROM candidate_quality ORDER BY candidate_id"
        )
        == snapshot_quality
    )


def test_upgrade_from_main_migration_28_applies_through_migration_32_columns_index_and_backfill(
    tmp_path,
):
    from afterword_engine.database import (
        MIGRATIONS,
        _execute_migration_script,
    )

    settings.db = str(tmp_path / "legacy-v28.db")
    con = sqlite3.connect(settings.db)
    con.execute(
        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
    )
    for version, script in MIGRATIONS:
        if version > 28:
            break
        _execute_migration_script(con, script)
        con.execute("INSERT INTO schema_migrations(version) VALUES(?)", (version,))
    con.commit()

    source_id = con.execute(
        "INSERT INTO sources(name,url) VALUES('Test Feed v28','https://example.com/feed-v28')"
    ).lastrowid

    def insert_v28_candidate(
        title,
        *,
        status="new",
        source_url="",
        c_isbn13="",
        c_isbn10="",
        q_status="accepted",
        provider="openlibrary",
        work_id="",
        q_isbn13="",
        q_isbn10="",
        flags="[]",
    ):
        cid = con.execute(
            "INSERT INTO candidates(title,author,source_url,source_id,status,isbn13,isbn10,score,normalized_key) "
            "VALUES(?,?,?,?,?,?,?,80.0,?)",
            (
                title,
                "Writer",
                source_url,
                source_id,
                status,
                c_isbn13,
                c_isbn10,
                title.lower().replace(" ", "-"),
            ),
        ).lastrowid
        con.execute(
            "UPDATE candidate_quality SET quality_status=?,provider=?,work_id=?,isbn13=?,isbn10=?,flags_json=?,audit_version='candidate-quality-v4' "
            "WHERE candidate_id=?",
            (q_status, provider, work_id, q_isbn13, q_isbn10, flags, cid),
        )
        return cid

    ol_verified = insert_v28_candidate(
        "V28 OpenLibrary Verified",
        c_isbn13="9780307474278",
        work_id="/works/OL888W",
        q_isbn13="9780307474278",
    )
    gb_source_work = insert_v28_candidate(
        "V28 Google Books With Source Work",
        source_url="https://openlibrary.org/works/OL889W?edition=1",
        c_isbn13="9780804172448",
        c_isbn10="043942089X",
        provider="google_books",
        work_id="gb-v28-vol",
        q_isbn13="9780143127550",
        q_isbn10="0307474275",
    )
    requeued_overlap = insert_v28_candidate(
        "V28 False Catalog Read Overlap",
        status="rejected",
        q_status="rejected",
        q_isbn13="9780345538376",
        flags='["read_overlap","short_description"]',
    )
    con.commit()
    con.close()

    initialize(seed_demo=False)

    applied_versions = {
        item["version"]
        for item in rows("SELECT version FROM schema_migrations WHERE version >= 28")
    }
    assert {28, 29, 30, 31, 32}.issubset(applied_versions)
    quality_columns = {
        col["name"]: col for col in rows("PRAGMA table_info(candidate_quality)")
    }
    assert "openlibrary_work_id" in quality_columns
    assert "isbn_verified" in quality_columns
    assert row(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_candidate_quality_openlibrary_work_id'"
    ) == {"name": "idx_candidate_quality_openlibrary_work_id"}

    assert row(
        "SELECT openlibrary_work_id,work_id,isbn_verified FROM candidate_quality WHERE candidate_id=?",
        (ol_verified,),
    ) == {
        "openlibrary_work_id": "/works/OL888W",
        "work_id": "/works/OL888W",
        "isbn_verified": 1,
    }
    assert row(
        "SELECT openlibrary_work_id,work_id,isbn13,isbn10,isbn_verified FROM candidate_quality WHERE candidate_id=?",
        (gb_source_work,),
    ) == {
        "openlibrary_work_id": "/works/OL889W",
        "work_id": "gb-v28-vol",
        "isbn13": "9780143127550",
        "isbn10": "0307474275",
        "isbn_verified": 0,
    }
    assert row("SELECT status FROM candidates WHERE id=?", (requeued_overlap,)) == {
        "status": "new"
    }
    assert row(
        "SELECT quality_status,isbn13,isbn_verified,flags_json FROM candidate_quality WHERE candidate_id=?",
        (requeued_overlap,),
    ) == {
        "quality_status": "pending",
        "isbn13": "9780345538376",
        "isbn_verified": 0,
        "flags_json": '["short_description"]',
    }


def test_mark_recommendation_read_carries_decoupled_openlibrary_work_id(database):
    from fastapi.testclient import TestClient
    from auth_helpers import authenticated_headers

    headers = authenticated_headers()
    with transaction() as con:
        source_id = con.execute(
            "SELECT id FROM sources WHERE is_default=1 LIMIT 1"
        ).fetchone()[0]
        gb_cid = con.execute(
            "INSERT INTO candidates(title,author,score,status,source_id,normalized_key) "
            "VALUES('Google Books Audited Recommendation','Author One',90,'recommended',?,'gb-audited-rec')",
            (source_id,),
        ).lastrowid
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',provider='google_books',"
            "work_id='gb-vol-1',openlibrary_work_id='/works/OL555W',isbn13='9780345538376',isbn_verified=0 "
            "WHERE candidate_id=?",
            (gb_cid,),
        )

    with TestClient(engine_main.app, headers=headers) as client:
        resp = client.post(f"/api/recommendations/{gb_cid}/read", json={"rating": 4})
    assert resp.status_code == 200
    assert row(
        "SELECT isbn,openlibrary_work_id FROM reads WHERE title='Google Books Audited Recommendation'"
    ) == {"isbn": "9780345538376", "openlibrary_work_id": "/works/OL555W"}


def test_boot_worker_throttles_quality_recovery_across_multiple_profiles(tmp_path, monkeypatch):
    import time
    from fastapi.testclient import TestClient
    from afterword_engine import jobs as jobs_module
    from afterword_engine import profiles
    from afterword_engine.database import connect
    from afterword_engine.tenancy import current_profile_id, profile_scope

    monkeypatch.setattr(settings, "db", str(tmp_path / "multi-profile-boot.db"))
    monkeypatch.setattr(settings, "backup_dir", "")
    monkeypatch.setattr(settings, "enable_background_loops", True)
    monkeypatch.setattr(jobs_module, "QUALITY_RECOVERY_COOLDOWN_SECONDS", 60)
    monkeypatch.delenv("AFTERWORD_SECRET_KEY", raising=False)

    profiles.initialize_auth()
    profiles.create_first_admin("owner", "owner-password-with-enough-length")
    alice = profiles.create_account("alice", "alice-password-with-enough-length")
    profile_list = ("legacy", alice["profile_id"])

    for pid in profile_list:
        with profile_scope(pid):
            initialize(seed_demo=False)
            with transaction() as con:
                source_id = con.execute(
                    "SELECT id FROM sources WHERE url='builtin://upcoming'"
                ).fetchone()[0]
                for idx in range(QUALITY_AUDIT_BATCH_SIZE + 15):
                    cid = con.execute(
                        "INSERT INTO candidates(title,author,isbn13,source_url,source_id,status,normalized_key) "
                        "VALUES(?,?,?,?,?,'new',?)",
                        (
                            f"{pid} Legacy {idx}",
                            "A Writer",
                            "9780307474278",
                            "https://example.com/book",
                            source_id,
                            f"{pid}-legacy-{idx}",
                        ),
                    ).lastrowid
                    con.execute(
                        "UPDATE candidate_quality SET quality_status='accepted',audit_version='candidate-quality-v3',"
                        "openlibrary_work_id='',isbn13='9780307474278',isbn_verified=1 "
                        "WHERE candidate_id=?",
                        (cid,),
                    )

    audited_batches: list[tuple[str, int]] = []
    score_calls: list[str] = []

    async def fake_audit(*, only_pending=False, limit=None):
        assert only_pending is True
        batch = candidate_quality_audit_candidates(limit=limit)
        with transaction() as con:
            for item in batch:
                con.execute(
                    "UPDATE candidate_quality SET audit_version=?,openlibrary_work_id='/works/OL1W',audited_at=CURRENT_TIMESTAMP "
                    "WHERE candidate_id=?",
                    (QUALITY_VERSION, item["id"]),
                )
        audited_batches.append((current_profile_id(), len(batch)))
        return {
            "audited": len(batch),
            "accepted": len(batch),
            "quarantine": 0,
            "rejected": 0,
            "deduped": 0,
            "enriched": 0,
        }

    async def fake_score_all(*_args, **_kwargs):
        score_calls.append(current_profile_id())
        return 0

    async def noop_async(*_args, **_kwargs):
        return 0

    async def noop_read_work_identities(*_args, **_kwargs):
        return {"matched": 0, "remaining": 0}

    async def noop_read_metadata(*_args, **_kwargs):
        return {"refreshed": 0, "remaining": 0}

    def idle_scheduler(stop):
        async def _loop():
            await stop.wait()

        return _loop()

    monkeypatch.setattr(engine_main, "audit_candidates", fake_audit)
    monkeypatch.setattr(engine_main, "score_all", fake_score_all)
    monkeypatch.setattr(engine_main, "refresh_missing_candidate_metadata", noop_async)
    monkeypatch.setattr(engine_main, "candidate_metadata_refresh_remaining", lambda: 0)
    monkeypatch.setattr(engine_main, "refresh_read_work_identities", noop_read_work_identities)
    monkeypatch.setattr(engine_main, "refresh_read_metadata", noop_read_metadata)
    for scheduler_name in (
        "source_scheduler_loop",
        "digest_scheduler_loop",
        "goodreads_rss_scheduler_loop",
        "backup_scheduler_loop",
    ):
        monkeypatch.setattr(engine_main, scheduler_name, idle_scheduler)

    with TestClient(engine_main.app) as client:
        assert client.get("/api/health").status_code == 200
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            non_recovery_remaining = 0
            completed_recovery = 0
            for pid in profile_list:
                with profile_scope(pid), connect() as con:
                    non_recovery_remaining += con.execute(
                        "SELECT COUNT(*) FROM jobs WHERE kind!='candidate_quality_recovery' AND status IN ('queued','running')"
                    ).fetchone()[0]
                    completed_recovery += con.execute(
                        "SELECT COUNT(*) FROM jobs WHERE kind='candidate_quality_recovery' AND status='complete'"
                    ).fetchone()[0]
            if non_recovery_remaining == 0 and completed_recovery >= 1:
                # Give worker_loop one extra beat to ensure it does not claim any more throttled recovery batches
                time.sleep(0.15)
                break
            time.sleep(0.05)

        assert non_recovery_remaining == 0
        assert len(audited_batches) == 1
        assert sum(count for _, count in audited_batches) == QUALITY_AUDIT_BATCH_SIZE
        # Intermediate recovery pass defers score_all when nothing was rejected/deduped/enriched
        assert score_calls == []
        # Both profiles still have queued recovery continuations waiting for the cooldown window
        queued_continuations = 0
        for pid in profile_list:
            with profile_scope(pid), connect() as con:
                queued_continuations += con.execute(
                    "SELECT COUNT(*) FROM jobs WHERE kind='candidate_quality_recovery' AND status='queued'"
                ).fetchone()[0]
        assert queued_continuations == 2


def test_candidate_quality_recovery_job_reaudits_non_isbn_v4_rows_and_preserves_source_provenance(
    database, monkeypatch
):
    with transaction() as con:
        con.execute("UPDATE candidates SET status='rejected'")
        source_id = con.execute("SELECT id FROM sources WHERE url='builtin://upcoming'").fetchone()[0]
        homonym_cid = con.execute(
            "INSERT INTO candidates(title,author,description,genres,source_url,source_id,status,normalized_key) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                "Home",
                "Marilynne Robinson",
                "Wrong Michael Robinson catalog blurb.",
                json.dumps(["Literary fiction", "Biography"]),
                "https://example.com/home",
                source_id,
                "recommended",
                "home marilynne robinson v4",
            ),
        ).lastrowid
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',audit_version='candidate-quality-v4',"
            "provider='openlibrary',provider_id='/works/OLMICHAELW',work_id='/works/OLMICHAELW',"
            "metadata_provider='openlibrary',metadata_provider_id='/works/OLMICHAELW' "
            "WHERE candidate_id=?",
            (homonym_cid,),
        )
        con.execute(
            "INSERT INTO metadata_field_provenance(entity_type,entity_id,field,provider,provider_id,confidence,source_payload) "
            "VALUES('candidate',?,'genres','source',?,1.0,'{}')",
            (homonym_cid, str(source_id)),
        )
        con.execute(
            "INSERT INTO metadata_field_provenance(entity_type,entity_id,field,provider,provider_id,confidence,source_payload) "
            "VALUES('candidate',?,'description','openlibrary','/works/OLMICHAELW',0.95,?)",
            (homonym_cid, json.dumps({"payload": {"description": "Wrong Michael Robinson catalog blurb."}})),
        )
        con.execute(
            "INSERT INTO metadata_field_provenance(entity_type,entity_id,field,provider,provider_id,confidence,source_payload) "
            "VALUES('candidate',?,'genres','openlibrary','/works/OLMICHAELW',0.95,?)",
            (homonym_cid, json.dumps({"payload": {"genres": ["Biography"]}})),
        )

    from afterword_engine import quality as quality_mod

    async def fake_resolve(title, author, **_kwargs):
        return {
            "provider": "openlibrary",
            "provider_id": "/works/OLMICHAELW",
            "work_id": "/works/OLMICHAELW",
            "isbn13": "",
            "isbn10": "",
            "title_match": 1.0,
            "author_match": 0.55,
            "quality_score": 0.8425,
            "metadata_provenance": {"fields": {}, "fetch_trace": [], "source_payloads": []},
        }

    scored_calls = []

    async def fake_score_all(*_args, **_kwargs):
        scored_calls.append(True)
        return 0

    monkeypatch.setattr(quality_mod, "resolve_catalog_match", fake_resolve)
    monkeypatch.setattr(engine_main, "score_all", fake_score_all)

    outcome = asyncio.run(handle_job("candidate_quality_recovery"))
    assert outcome["audited"] == 1
    assert outcome["quarantine"] == 1
    assert scored_calls == [True]

    after_candidate = row("SELECT description,genres FROM candidates WHERE id=?", (homonym_cid,))
    after_quality = row(
        "SELECT quality_status,audit_version,work_id,metadata_provider FROM candidate_quality WHERE candidate_id=?",
        (homonym_cid,),
    )
    assert after_quality == {
        "quality_status": "quarantine",
        "audit_version": QUALITY_VERSION,
        "work_id": "",
        "metadata_provider": "",
    }
    assert after_candidate["description"] == ""
    assert json.loads(after_candidate["genres"]) == ["Literary fiction"]


def test_load_llm_rerank_inputs_projects_decoupled_quality_identity_fields(database):
    from afterword_engine.database import connect
    from afterword_engine.main import _load_llm_rerank_inputs

    with transaction() as con:
        source_id = con.execute(
            "SELECT id FROM sources WHERE is_default=1 LIMIT 1"
        ).fetchone()[0]
        cid = con.execute(
            "INSERT INTO candidates(title,author,score,status,source_id,isbn13,normalized_key) "
            "VALUES('LLM Rerank Identity Book','Author One',88,'recommended',?,'','llm-rerank-identity-book')",
            (source_id,),
        ).lastrowid
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',provider='google_books',provider_id='gb-id-9',"
            "work_id='gb-vol-9',openlibrary_work_id='/works/OL999W',isbn13='9780345538376',isbn_verified=0,"
            "audit_version=? WHERE candidate_id=?",
            (QUALITY_VERSION, cid),
        )

    with connect() as con:
        _read_rows, llm_events = _load_llm_rerank_inputs(
            con,
            [{"candidate_id": cid, "action": "save", "title": "LLM Rerank Identity Book", "author": "Author One"}],
        )

    assert len(llm_events) == 1
    assert llm_events[0]["quality_openlibrary_work_id"] == "/works/OL999W"
    assert llm_events[0]["quality_provider_id"] == "gb-id-9"
    assert llm_events[0]["quality_isbn_verified"] == 0
    assert llm_events[0]["quality_status"] == "accepted"
    assert llm_events[0]["quality_audit_version"] == QUALITY_VERSION