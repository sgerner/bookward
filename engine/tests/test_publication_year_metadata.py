from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from afterword_engine.config import settings
from afterword_engine.database import initialize, normalize_key, transaction
from afterword_engine.publication_year_metadata import attach_publication_years


@pytest.fixture()
def database(tmp_path: Path):
    settings.db = str(tmp_path / "publication-year.db")
    initialize()
    return settings.db


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _insert_ledger(con, entity_type, entity_id, field, work_id, publication_value, source_field):
    source_payload = {
        "field": field,
        "field_provenance": {
            "provider": "openlibrary",
            "provider_id": work_id,
            "source_field": field,
        },
        # This is the shape persisted in metadata_field_provenance.source_payload.
        "payload": {
            "key": work_id,
            "publication_date": publication_value,
            "publication_date_source_field": source_field,
        },
    }
    con.execute(
        "INSERT INTO metadata_field_provenance("
        "entity_type,entity_id,field,provider,provider_id,source_payload) "
        "VALUES(?,?,?,?,?,?)",
        (
            entity_type,
            entity_id,
            field,
            "openlibrary",
            work_id,
            json.dumps(source_payload, sort_keys=True),
        ),
    )


def _insert_candidate(con, title, work_id, *, release_date=""):
    source_id = con.execute(
        "SELECT id FROM sources WHERE url='builtin://upcoming'"
    ).fetchone()[0]
    candidate_id = con.execute(
        "INSERT INTO candidates(title,author,release_date,normalized_key,source_id) "
        "VALUES(?,?,?,?,?)",
        (title, "A Writer", release_date, normalize_key(title, "A Writer"), source_id),
    ).lastrowid
    con.execute(
        "UPDATE candidate_quality SET quality_status='accepted',provider='openlibrary',"
        "provider_id=?,work_id=? WHERE candidate_id=?",
        (work_id, work_id, candidate_id),
    )
    return int(candidate_id)


def _insert_read(con, title, work_id, *, read_at="2024-01-01", release_date="1984-01-01"):
    read_id = con.execute(
        "INSERT INTO reads(title,author,rating,read_at,source,openlibrary_work_id) "
        "VALUES(?,?,5,?,'fixture',?)",
        (title, "A Reader", read_at, work_id),
    ).lastrowid
    read = dict(con.execute("SELECT * FROM reads WHERE id=?", (read_id,)).fetchone())
    provenance = {
        "fields": {
            "release_date": {
                "provider": "openlibrary",
                "provider_id": work_id,
                "source_field": "first_publish_date",
                "value_sha256": _hash(release_date),
            }
        }
    }
    identity = {
        "title": normalize_key(title, ""),
        "author": normalize_key("", "A Reader"),
        "source_isbn": "",
        "isbn13": "",
        "isbn10": "",
        "openlibrary_work_id": work_id,
    }
    identity_hash = _hash(json.dumps(identity, sort_keys=True, separators=(",", ":")))
    con.execute(
        "INSERT INTO read_metadata(read_id,verified_work_id,identity_hash,release_date,metadata_provenance) "
        "VALUES(?,?,?,?,?)",
        (read_id, work_id, identity_hash, release_date, json.dumps(provenance)),
    )
    return read


def test_loads_hash_verified_read_and_original_work_payload_candidate(database):
    work_id = "/works/OL123W"
    with transaction() as con:
        read = _insert_read(con, "A Read Novel", work_id)
        candidate_id = _insert_candidate(
            con, "An Unread Novel", "/works/OL456W", release_date="2018-06-01"
        )
        _insert_ledger(
            con,
            "candidate",
            candidate_id,
            "description",
            "/works/OL456W",
            "1984",
            "first_publish_year",
        )
        # The candidate's edition-level release date must not override its
        # explicitly retained work-level first-publication payload.
        _insert_ledger(
            con,
            "candidate",
            candidate_id,
            "release_date",
            "/works/OL456W",
            "2018-06-01",
            "publishedDate",
        )
        candidates = [{
            "id": candidate_id,
            "quality_work_id": "/works/OL456W",
            "release_date": "2018-06-01",
        }]

        attach_publication_years(con, [read], candidates)

    assert read["first_publication_year"] == 1984
    assert candidates[0]["first_publication_year"] == 1984


def test_rejects_edition_fields_mismatched_work_ids_and_stale_read_identity(database):
    with transaction() as con:
        read = _insert_read(con, "A Dated Read", "/works/OL123W")
        future_read = _insert_read(
            con, "A Future Dated Read", "/works/OL124W", read_at="1980-01-01"
        )
        stale_read = _insert_read(con, "A Stale Read", "/works/OL125W")
        con.execute(
            "UPDATE read_metadata SET identity_hash='stale' WHERE read_id=?",
            (stale_read["id"],),
        )
        edition_candidate_id = _insert_candidate(
            con, "Edition Date Only", "/works/OL201W", release_date="2018-06-01"
        )
        mismatch_candidate_id = _insert_candidate(
            con, "Mismatched Work", "/works/OL202W"
        )
        _insert_ledger(
            con,
            "candidate",
            edition_candidate_id,
            "release_date",
            "/works/OL201W",
            "2018-06-01",
            "publishedDate",
        )
        _insert_ledger(
            con,
            "candidate",
            mismatch_candidate_id,
            "description",
            "/works/OL-otherW",
            "1984",
            "first_publish_year",
        )
        candidates = [
            {"id": edition_candidate_id, "quality_work_id": "/works/OL201W", "release_date": "2018-06-01"},
            {"id": mismatch_candidate_id, "quality_work_id": "/works/OL202W"},
        ]
        reads = [read, future_read, stale_read]

        attach_publication_years(con, reads, candidates)

    assert reads[0]["first_publication_year"] == 1984
    assert reads[1]["first_publication_year"] is None
    assert reads[2]["first_publication_year"] is None
    assert [item["first_publication_year"] for item in candidates] == [None, None]


def test_conflicting_verified_work_years_fail_closed(database):
    work_id = "/works/OL303W"
    with transaction() as con:
        candidate_id = _insert_candidate(
            con, "Conflicting Evidence", work_id, release_date="1984-01-01"
        )
        direct = {
            "field": "release_date",
            "field_provenance": {
                "provider": "openlibrary",
                "provider_id": work_id,
                "source_field": "first_publish_date",
                "value_sha256": _hash("1984-01-01"),
            },
            "payload": {
                "key": work_id,
                "publication_date": "1984-01-01",
                "publication_date_source_field": "first_publish_date",
            },
        }
        con.execute(
            "INSERT INTO metadata_field_provenance("
            "entity_type,entity_id,field,provider,provider_id,source_payload) "
            "VALUES('candidate',?,'release_date','openlibrary',?,?)",
            (candidate_id, work_id, json.dumps(direct)),
        )
        _insert_ledger(
            con,
            "candidate",
            candidate_id,
            "description",
            work_id,
            "1985",
            "first_publish_year",
        )
        candidate = {
            "id": candidate_id,
            "quality_work_id": work_id,
            "release_date": "1984-01-01",
            "first_publication_year": 1990,
        }

        attach_publication_years(con, [], [candidate])

    assert candidate["first_publication_year"] is None
