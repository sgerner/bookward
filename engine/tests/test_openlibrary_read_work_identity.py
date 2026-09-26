import asyncio
import socket
from pathlib import Path

import httpx
import pytest
import respx

from afterword_engine import ingestion
from afterword_engine.config import settings
from afterword_engine.database import initialize, row, transaction


ISBN13 = "9780593418574"
PINNED_ADDRESS = "93.184.216.34"


@pytest.fixture()
def database(tmp_path: Path):
    settings.db = str(tmp_path / "openlibrary-read-work.db")
    initialize()
    return settings.db


def pin_openlibrary_dns(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (PINNED_ADDRESS, 443))
        ],
    )


@respx.mock
def test_openlibrary_isbn_redirect_fetches_the_safe_edition_path(monkeypatch):
    pin_openlibrary_dns(monkeypatch)
    isbn_url = f"https://{PINNED_ADDRESS}/isbn/{ISBN13}.json"
    edition_url = f"https://{PINNED_ADDRESS}/books/OL123M.json"
    isbn_route = respx.get(isbn_url).mock(
        return_value=httpx.Response(302, headers={"location": "/books/OL123M.json"})
    )
    edition_route = respx.get(edition_url).mock(
        return_value=httpx.Response(200, json={"key": "/books/OL123M"})
    )

    async def fetch_edition():
        async with ingestion._source_client() as client:
            return await ingestion._request_openlibrary_edition(client, ISBN13)

    response = asyncio.run(fetch_edition())

    assert response.status_code == 200
    assert isbn_route.call_count == 1
    assert edition_route.call_count == 1
    assert isbn_route.calls[0].request.headers["host"] == "openlibrary.org"
    assert edition_route.calls[0].request.headers["host"] == "openlibrary.org"
    assert edition_route.calls[0].request.url.path == "/books/OL123M.json"


@pytest.mark.parametrize(
    "location",
    [
        "https://attacker.example/books/OL123M.json",
        "http://openlibrary.org/books/OL123M.json",
        "https://openlibrary.org:8443/books/OL123M.json",
        "https://user@openlibrary.org/books/OL123M.json",
    ],
)
@respx.mock
def test_openlibrary_isbn_redirect_rejects_unsafe_destinations(
    location, monkeypatch
):
    pin_openlibrary_dns(monkeypatch)
    isbn_url = f"https://{PINNED_ADDRESS}/isbn/{ISBN13}.json"
    route = respx.get(isbn_url).mock(
        return_value=httpx.Response(302, headers={"location": location})
    )

    async def fetch_edition():
        async with ingestion._source_client() as client:
            return await ingestion._request_openlibrary_edition(client, ISBN13)

    with pytest.raises(ValueError, match="unsafe redirect"):
        asyncio.run(fetch_edition())

    assert route.call_count == 1


@respx.mock
def test_openlibrary_isbn_redirect_stops_after_the_redirect_limit(monkeypatch):
    pin_openlibrary_dns(monkeypatch)
    isbn_url = f"https://{PINNED_ADDRESS}/isbn/{ISBN13}.json"
    edition_url = f"https://{PINNED_ADDRESS}/books/OL123M.json"
    isbn_route = respx.get(isbn_url).mock(
        return_value=httpx.Response(302, headers={"location": "/books/OL123M.json"})
    )
    edition_route = respx.get(edition_url).mock(
        return_value=httpx.Response(302, headers={"location": "/books/OL123M.json"})
    )

    async def fetch_edition():
        async with ingestion._source_client() as client:
            return await ingestion._request_openlibrary_edition(client, ISBN13)

    with pytest.raises(httpx.TooManyRedirects):
        asyncio.run(fetch_edition())

    assert isbn_route.call_count + edition_route.call_count == (
        ingestion.READ_WORK_IDENTITY_MAX_REDIRECTS + 1
    )


def test_edition_work_id_requires_the_requested_isbn_and_one_valid_work():
    valid_edition = {
        "isbn_13": [ISBN13],
        "works": [{"key": "/works/OL123W"}],
    }
    assert ingestion._openlibrary_work_from_edition(valid_edition, ISBN13) == "/works/OL123W"

    wrong_isbn = {**valid_edition, "isbn_13": ["9780061120084"]}
    assert ingestion._openlibrary_work_from_edition(wrong_isbn, ISBN13) == ""

    invalid_checksum = {**valid_edition, "isbn_13": ["9780593418575"]}
    assert ingestion._openlibrary_work_from_edition(invalid_checksum, ISBN13) == ""

    invalid_work = {**valid_edition, "works": [{"key": "/works/not-a-work"}]}
    assert ingestion._openlibrary_work_from_edition(invalid_work, ISBN13) == ""

    ambiguous_works = {
        **valid_edition,
        "works": [{"key": "/works/OL123W"}, {"key": "/works/OL456W"}],
    }
    assert ingestion._openlibrary_work_from_edition(ambiguous_works, ISBN13) == ""


def test_failed_lookup_timestamp_blocks_immediate_retry_but_allows_stale_retry(
    database, monkeypatch
):
    with transaction() as con:
        source_id = con.execute(
            "SELECT id FROM sources WHERE url='builtin://upcoming'"
        ).fetchone()[0]
        candidate_id = con.execute(
            "INSERT INTO candidates(title,author,source_url,source_id,score,status,normalized_key) "
            "VALUES(?,?,?,?,?,'new',?)",
            (
                "Another Book",
                "Retry Writer",
                "https://example.test/another-book",
                source_id,
                95,
                "another-book-retry-writer",
            ),
        ).lastrowid
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted',provider='openlibrary',"
            "provider_id='/works/OL456W',work_id='/works/OL456W' WHERE candidate_id=?",
            (candidate_id,),
        )
        read_id = con.execute(
            "INSERT INTO reads(title,author,isbn,source) VALUES(?,?,?,'test')",
            ("A Different Edition", "Retry Writer", ISBN13),
        ).lastrowid

    attempts = []

    async def failed_lookup(_client, isbn13):
        attempts.append(isbn13)
        raise httpx.ConnectError("temporary Open Library failure")

    monkeypatch.setattr(ingestion, "_request_openlibrary_edition", failed_lookup)
    monkeypatch.setattr(ingestion, "READ_WORK_IDENTITY_REQUEST_INTERVAL_SECONDS", 0)

    first = asyncio.run(ingestion.refresh_read_work_identities())
    attempted_at = row(
        "SELECT openlibrary_lookup_attempted_at FROM reads WHERE id=?", (read_id,)
    )["openlibrary_lookup_attempted_at"]
    assert first == {"checked": 1, "matched": 0, "lookup_failures": 1, "remaining": 0}
    assert attempted_at
    assert attempts == [ISBN13]

    recent_retry = asyncio.run(ingestion.refresh_read_work_identities())
    assert recent_retry["checked"] == 0
    assert attempts == [ISBN13]

    with transaction() as con:
        con.execute(
            "UPDATE reads SET openlibrary_lookup_attempted_at='2000-01-01T00:00:00+00:00' WHERE id=?",
            (read_id,),
        )

    stale_retry = asyncio.run(ingestion.refresh_read_work_identities())
    assert stale_retry == {"checked": 1, "matched": 0, "lookup_failures": 1, "remaining": 0}
    assert attempts == [ISBN13, ISBN13]
