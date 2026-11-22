import asyncio
import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from afterword_engine import covers
from afterword_engine import ingestion
from afterword_engine.config import settings
from afterword_engine.covers import (
    GOOGLE_BOOKS_SEARCH,
    OPEN_LIBRARY_SEARCH,
    _bounded_source_payload,
    _isbn_provenance_values,
    resolve_book_metadata,
    resolve_openlibrary_work_metadata,
)
from afterword_engine.database import initialize, normalize_key, row, rows, transaction
from afterword_engine.isbn import isbn_parts_from_source
from afterword_engine.quality import _audit_one, resolve_catalog_match


@pytest.fixture(autouse=True)
def no_real_rate_limit_wait(monkeypatch):
    async def advance_without_wait(delay):
        return None

    monkeypatch.setattr(covers, "_openlibrary_rate_sleep", advance_without_wait)


def _provider_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize(
    ("requested_title", "catalog_title", "expected_match"),
    [
        ("Dune (Unabridged)", "Dune", True),
        ("Dune", "Dune: Messiah", False),
    ],
)
def test_metadata_title_matching_strips_known_suffixes_but_keeps_subtitles(
    requested_title, catalog_title, expected_match
):
    def handler(request):
        if request.url.host == "openlibrary.org":
            return httpx.Response(
                200,
                json={"docs": [{
                    "key": "/works/OLTITLE",
                    "title": catalog_title,
                    "author_name": ["Kurt Vonnegut"],
                    "description": "A matched catalog synopsis.",
                }]},
            )
        return httpx.Response(200, json={"items": []})

    async def run():
        async with _provider_client(handler) as client:
            return await resolve_book_metadata(
                requested_title,
                "Kurt Vonnegut Jr.",
                client=client,
            )

    metadata = asyncio.run(run())

    assert bool(metadata["description"]) is expected_match
    if expected_match:
        assert metadata["description_provider"] == "openlibrary"
    else:
        assert metadata["description_provider"] == ""


def _isbn13_with_prefix(number):
    body = f"978{number:09d}"
    checksum = (10 - sum(int(digit) * (1 if index % 2 == 0 else 3) for index, digit in enumerate(body)) % 10) % 10
    return body + str(checksum)


def test_openlibrary_description_prefers_synopsis_and_records_field_provenance():
    opening = "An opening sentence that is deliberately longer than the synopsis."

    def handler(request):
        if request.url.host == "openlibrary.org" and request.url.path == "/search.json":
            return httpx.Response(
                200,
                json={
                    "docs": [
                        {
                            "key": "/works/OL100W",
                            "title": "A Book",
                            "author_name": ["An Author"],
                            "first_sentence": [opening],
                            "subject": ["General fiction"],
                        },
                        {
                            "key": "/works/OL101W",
                            "title": "A Book",
                            "author_name": ["An Author"],
                            "description": "A verified full synopsis.",
                            "first_sentence": [opening],
                            "subject": ["Literary fiction"],
                        },
                    ],
                },
            )
        if request.url.host == "www.googleapis.com":
            return httpx.Response(200, json={"items": []})
        raise AssertionError(f"Unexpected provider request: {request.url.host}{request.url.path}")

    async def run():
        async with _provider_client(handler) as client:
            return await resolve_book_metadata("A Book", "An Author", client=client)

    metadata = asyncio.run(run())

    assert metadata["description"] == "A verified full synopsis."
    assert metadata["description_kind"] == "synopsis"
    assert metadata["description_provider"] == "openlibrary"
    assert metadata["description_provider_id"] == "/works/OL101W"
    assert metadata["metadata_provenance"]["fields"]["description"]["source_field"] == "description"
    source = metadata["metadata_provenance"]["source_payloads"][0]
    assert source["provider"] == "openlibrary"
    assert source["provider_id"] == "/works/OL101W"
    assert source["payload"]["opening_sentence"] == opening


def test_penguin_random_house_description_requires_direct_link_identity_and_matching_isbn(monkeypatch):
    item = {
        "title": "Source Work",
        "author": "A Writer",
        "source_url": "https://www.penguinrandomhouse.com/books/123/source-work-by-a-writer",
        "isbn13": "9780306406157",
    }
    html = b'''<html><head>
      <meta name="Tealium" data-book-title="Source Work" data-book-authors="A Writer" data-book-isbn="9780306406157">
      <meta name="twitter:text:isbn" content="9780306406157">
    </head><body><div id="book-description-copy"><div class="copy-height">Official publisher description.</div></div></body></html>'''
    calls = []

    async def fetch(url, client=None):
        calls.append(url)
        return html, "text/html"

    monkeypatch.setattr(ingestion, "fetch_bytes", fetch)
    metadata = asyncio.run(ingestion.resolve_penguin_random_house_product_description(item))

    assert calls == [item["source_url"]]
    assert metadata["description"] == "Official publisher description."
    assert metadata["provider"] == "penguinrandomhouse"
    assert metadata["provider_id"] == item["source_url"]
    assert metadata["isbn13"] == item["isbn13"]
    assert metadata["source_payload"]["identifiers"] == [item["isbn13"]]

    async def mismatched_page(url, client=None):
        return html.replace(b"9780306406157", b"9780140328721"), "text/html"

    monkeypatch.setattr(ingestion, "fetch_bytes", mismatched_page)
    assert asyncio.run(ingestion.resolve_penguin_random_house_product_description(item)) == {}


def test_penguin_random_house_description_rejects_nonproduct_urls_without_fetch(monkeypatch):
    async def fail_if_fetched(*args, **kwargs):
        raise AssertionError("disallowed publisher URL was fetched")

    monkeypatch.setattr(ingestion, "fetch_bytes", fail_if_fetched)
    result = asyncio.run(ingestion.resolve_penguin_random_house_product_description({
        "title": "Source Work",
        "author": "A Writer",
        "source_url": "https://example.org/books/123",
        "isbn13": "9780306406157",
    }))
    assert result == {}


def test_publishers_weekly_grid_parser_scopes_covers_skips_cta_and_recovers_isbns():
    source_url = "https://www.publishersweekly.com/pw/reviews/starred.html"
    markup = """<table>
      <tr>
        <td class="starredreview-grid">
          <a href="/9780306406157"><img src="/cover/9780306406157"></a>
          <h3><a href="/9780306406157">First Starred Book</a></h3>
          <h4>Ada Writer. Press One, $28 (320p) ISBN 978-0-306-40615-7</h4>
          <p><a href="/9780306406157">Complete Review &#187;</a></p>
        </td>
        <td class="starredreview-grid">
          <a href="/0061120081"><img src="/cover/0061120081"></a>
          <h3><a href="/0061120081">Second Starred Book</a></h3>
          <h4>By Grace Author</h4>
          <p><a href="/0061120081">Read Full Review &raquo;</a></p>
        </td>
      </tr>
      <tr>
        <td class="starredreview-grid">
          <img src="/cover/row2-left.jpg">
          <h3><a href="/books/left">Row Two Left</a></h3>
          <h4>By Left Writer</h4>
          <p>Complete Review »</p>
        </td>
        <td class="starredreview-grid">
          <h3><a href="/9780306406150">Row Two Missing Cover</a></h3>
          <h4>Right Writer. Press Two, $26 ISBN 978-0-306-40615-0</h4>
          <p><a href="/9780306406150">Complete Review »</a></p>
        </td>
      </tr>
      <tr>
        <td><img src="/cover/single-row.jpg"></td>
        <td class="starredreview-grid">
          <h3><a href="/books/single-row">Single Row Fallback</a></h3>
          <h4>Solo Writer. Press Three, $24 (240p) ISBN 0-306-40615-2</h4>
          <p>A crisp literary mystery. <a href="/books/single-row">Complete Review »</a></p>
        </td>
      </tr>
    </table>""".encode("utf-8")

    items = ingestion.parse_book_items(markup, "text/html", source_url)
    by_title = {item["title"]: item for item in items}

    assert set(by_title) == {
        "First Starred Book",
        "Second Starred Book",
        "Row Two Left",
        "Row Two Missing Cover",
        "Single Row Fallback",
    }
    assert by_title["First Starred Book"]["cover_url"] == "https://www.publishersweekly.com/cover/9780306406157"
    assert by_title["First Starred Book"]["description"] == ""
    assert by_title["First Starred Book"]["isbn13"] == "9780306406157"

    assert by_title["Second Starred Book"]["cover_url"] == "https://www.publishersweekly.com/cover/0061120081"
    assert by_title["Second Starred Book"]["description"] == ""
    assert by_title["Second Starred Book"]["isbn13"] == "9780061120084"
    assert by_title["Second Starred Book"]["isbn10"] == "0061120081"

    assert by_title["Row Two Left"]["cover_url"] == "https://www.publishersweekly.com/cover/row2-left.jpg"
    assert by_title["Row Two Missing Cover"]["cover_url"] == ""
    assert "isbn13" not in by_title["Row Two Missing Cover"]
    assert "isbn10" not in by_title["Row Two Missing Cover"]

    assert by_title["Single Row Fallback"]["cover_url"] == "https://www.publishersweekly.com/cover/single-row.jpg"
    assert by_title["Single Row Fallback"]["description"] == "A crisp literary mystery."
    assert by_title["Single Row Fallback"]["isbn13"] == "9780306406157"
    assert by_title["Single Row Fallback"]["isbn10"] == "0306406152"

    assert isbn_parts_from_source((), "https://publishersweekly.com/978-0-306-40615-7") == ("9780306406157", "")
    assert isbn_parts_from_source((), "https://www.publishersweekly.com/pw/by-topic/industry-news/article/9780306406157.html") == ("", "")
    assert isbn_parts_from_source((), "https://publishersweekly.com.evil.example/9780306406157") == ("", "")
    assert isbn_parts_from_source((), "https://example.com/9780306406157") == ("", "")


def test_publishers_weekly_cta_skip_allows_catalog_synopsis_and_keeps_curated_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db", str(tmp_path / "pw-cta.db"))
    initialize(seed_demo=False)
    source_url = "https://www.publishersweekly.com/pw/reviews/starred.html"
    markup = """<table><tr>
      <td class="starredreview-grid">
        <img src="/cover/cta.jpg">
        <h3><a href="/9780306406157">CTA Starred Novel</a></h3>
        <h4>Ada Writer. Press One, $28 ISBN 978-0-306-40615-7</h4>
        <p><a href="/9780306406157">Complete Review »</a></p>
      </td>
      <td class="starredreview-grid">
        <img src="/cover/curated.jpg">
        <h3><a href="/0061120081">Curated Starred Novel</a></h3>
        <h4>Grace Author. Press Two, $27 ISBN 0-06-112008-1</h4>
        <p>Editorial review excerpt kept intact.</p>
      </td>
    </tr></table>""".encode("utf-8")

    async def fake_fetch(_url, client=None):
        return markup, "text/html"

    def provider_handler(request):
        if request.url.host == "openlibrary.org":
            isbn_param = request.url.params.get("isbn", "")
            title = "CTA Starred Novel" if isbn_param == "9780306406157" else "Curated Starred Novel"
            author = "Ada Writer" if isbn_param == "9780306406157" else "Grace Author"
            work_key = "/works/OLCTA1W" if isbn_param == "9780306406157" else "/works/OLCUR1W"
            return httpx.Response(
                200,
                json={"docs": [{
                    "key": work_key,
                    "title": title,
                    "author_name": [author],
                    "isbn": [isbn_param] if isbn_param else [],
                    "description": f"Catalog synopsis for {title}.",
                    "subject": ["Literary fiction"],
                    "first_publish_year": 2026,
                }]},
            )
        return httpx.Response(200, json={"items": []})

    monkeypatch.setattr(ingestion, "fetch_bytes", fake_fetch)
    monkeypatch.setattr(ingestion, "metadata_client", lambda: _provider_client(provider_handler))
    source = row("SELECT * FROM sources WHERE url=?", (source_url,))
    assert asyncio.run(ingestion.scan_source(source)) == 2

    cta_candidate = row("SELECT id,description,isbn13 FROM candidates WHERE title='CTA Starred Novel'")
    curated_candidate = row("SELECT id,description,isbn13 FROM candidates WHERE title='Curated Starred Novel'")
    assert cta_candidate["description"] == "Catalog synopsis for CTA Starred Novel."
    assert cta_candidate["isbn13"] == "9780306406157"
    assert curated_candidate["description"] == "Editorial review excerpt kept intact."
    assert curated_candidate["isbn13"] == "9780061120084"

    cta_provenance = row(
        "SELECT provider FROM metadata_field_provenance "
        "WHERE entity_type='candidate' AND entity_id=? AND field='description'",
        (cta_candidate["id"],),
    )
    curated_provenance = row(
        "SELECT provider FROM metadata_field_provenance "
        "WHERE entity_type='candidate' AND entity_id=? AND field='description'",
        (curated_candidate["id"],),
    )
    assert cta_provenance["provider"] == "openlibrary"
    assert curated_provenance["provider"] == "source"


def test_scan_source_chunks_all_incomplete_items_and_hydrates_stored_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db", str(tmp_path / "batch-scan.db"))
    initialize(seed_demo=False)
    batch_size = ingestion.SOURCE_METADATA_ENRICHMENT_BATCH_SIZE
    total_incomplete = batch_size * 2 + 5

    prh_url = "https://www.penguinrandomhouse.com/books/new-releases/"
    with transaction() as con:
        con.execute(
            "UPDATE sources SET filters=? WHERE url=?",
            (json.dumps({"include_genres": ["science fiction"]}), prh_url),
        )
        source_id = con.execute("SELECT id FROM sources WHERE url=?", (prh_url,)).fetchone()[0]
        for index in range(2):
            title = f"Stored Complete {index}"
            con.execute(
                """INSERT INTO candidates(
                    title,author,description,cover_url,release_date,date_kind,genres,isbn13,source_url,source_id,normalized_key
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    title,
                    "Known Writer",
                    "Stored synopsis.",
                    f"https://covers.openlibrary.org/b/id/{900 + index}-L.jpg",
                    "2026-05-01",
                    "day",
                    json.dumps(["Science fiction"]),
                    _isbn13_with_prefix(900 + index),
                    f"https://www.penguinrandomhouse.com/books/{900 + index}/stored/",
                    source_id,
                    normalize_key(title, "Known Writer"),
                ),
            )

    feed_items = [
        {
            "title": f"Stored Complete {index}",
            "author": "Known Writer",
            "description": "",
            "cover_url": f"https://images.penguinrandomhouse.com/cover/{900 + index}",
            "source_url": f"https://www.penguinrandomhouse.com/books/{900 + index}/stored/",
            "release_date": "2026-05-01",
            "date_kind": "day",
            "genres": [],
        }
        for index in range(2)
    ] + [
        {
            "title": f"Release {index}",
            "author": "Feed Writer",
            "description": "",
            "cover_url": f"https://images.penguinrandomhouse.com/cover/{index}",
            "source_url": f"https://www.penguinrandomhouse.com/books/{1000 + index}/release-{index}/",
            "release_date": "2026-06-01",
            "date_kind": "day",
            "genres": [],
        }
        for index in range(total_incomplete)
    ]

    prh_batches = []
    catalog_batches = []

    async def fake_fetch(_url):
        return "text/html", [dict(item) for item in feed_items]

    async def fake_prh_enrich(items):
        prh_batches.append([item["title"] for item in items])
        return [
            {
                **item,
                "description": f"Publisher synopsis for {item['title']}.",
                "isbn13": _isbn13_with_prefix(2000 + offset),
            }
            for offset, item in enumerate(items)
        ]

    async def fake_catalog_enrich(items):
        catalog_batches.append([item["title"] for item in items])
        return [{**item, "genres": ["Science fiction"]} for item in items]

    monkeypatch.setattr(ingestion, "_fetch_and_parse_source", fake_fetch)
    monkeypatch.setattr(ingestion, "enrich_penguin_random_house_items", fake_prh_enrich)
    monkeypatch.setattr(ingestion, "enrich_book_metadata", fake_catalog_enrich)

    source = row("SELECT * FROM sources WHERE id=?", (source_id,))
    collected = asyncio.run(ingestion.scan_source(source))
    assert collected == total_incomplete + 2
    assert [len(batch) for batch in prh_batches] == [batch_size, batch_size, 5]
    assert [len(batch) for batch in catalog_batches] == [batch_size, batch_size, 5]
    enriched_titles = {title for batch in catalog_batches for title in batch}
    assert "Stored Complete 0" not in enriched_titles
    assert "Stored Complete 1" not in enriched_titles
    assert f"Release {total_incomplete - 1}" in enriched_titles
    stored_cover = row("SELECT cover_url FROM candidates WHERE title='Stored Complete 0'")["cover_url"]
    assert stored_cover == "https://covers.openlibrary.org/b/id/900-L.jpg"

    # Warm rescan: previously enriched candidates hydrate from SQLite; only new incomplete item is enriched.
    prh_batches.clear()
    catalog_batches.clear()
    feed_items.append({
        "title": "Brand New Release",
        "author": "Feed Writer",
        "description": "",
        "cover_url": "https://images.penguinrandomhouse.com/cover/new",
        "source_url": "https://www.penguinrandomhouse.com/books/5000/brand-new-release/",
        "release_date": "2026-07-01",
        "date_kind": "day",
        "genres": [],
    })
    second_collected = asyncio.run(ingestion.scan_source(source))
    assert second_collected == total_incomplete + 3
    assert prh_batches == [["Brand New Release"]]
    assert catalog_batches == [["Brand New Release"]]


def test_refresh_missing_candidate_metadata_retries_penguin_random_house_product_pages(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db", str(tmp_path / "prh-refresh.db"))
    initialize(seed_demo=False)
    product_url = "https://www.penguinrandomhouse.com/books/777/retry-work-by-ada-writer"
    expected_isbn = "9780306406157"

    with transaction() as con:
        source_id = row(
            "SELECT id FROM sources WHERE url='https://www.penguinrandomhouse.com/books/new-releases/'"
        )["id"]
        candidate_id = con.execute(
            """INSERT INTO candidates(
                title,author,description,cover_url,release_date,date_kind,genres,source_url,source_id,normalized_key
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                "Retry Work",
                "Ada Writer",
                "",
                "https://images.penguinrandomhouse.com/cover/777",
                "2026-08-01",
                "day",
                "[]",
                product_url,
                source_id,
                normalize_key("Retry Work", "Ada Writer"),
            ),
        ).lastrowid
        con.execute(
            "UPDATE candidate_quality SET quality_status='accepted' WHERE candidate_id=?",
            (candidate_id,),
        )

    product_html = json.dumps({
        "@context": "https://schema.org",
        "@type": "Book",
        "name": "Retry Work",
        "author": {"@type": "Person", "name": "Ada Writer"},
        "isbn": expected_isbn,
        "genre": ["Science fiction"],
    }).encode()
    page = (
        b'<html><head><meta name="Tealium" data-book-title="Retry Work" '
        b'data-book-authors="Ada Writer" data-book-isbn="9780306406157">'
        b'<script type="application/ld+json">'
        + product_html
        + b'</script></head><body><div id="book-description-copy">'
        b'<div class="copy-height">Recovered publisher synopsis after retry.</div>'
        b'</div></body></html>'
    )

    async def fake_fetch(url, client=None):
        assert url == product_url
        return page, "text/html"

    def empty_catalog_handler(request):
        if request.url.host == "openlibrary.org":
            return httpx.Response(200, json={"docs": []})
        return httpx.Response(200, json={"items": []})

    monkeypatch.setattr(ingestion, "fetch_bytes", fake_fetch)
    monkeypatch.setattr(ingestion, "metadata_client", lambda: _provider_client(empty_catalog_handler))

    assert asyncio.run(ingestion.refresh_missing_candidate_metadata()) == 1
    candidate = row("SELECT description,isbn13,genres FROM candidates WHERE id=?", (candidate_id,))
    quality = row(
        "SELECT metadata_provider,metadata_provider_id,isbn13 FROM candidate_quality WHERE candidate_id=?",
        (candidate_id,),
    )
    provenance = row(
        "SELECT provider,provider_id FROM metadata_field_provenance "
        "WHERE entity_type='candidate' AND entity_id=? AND field='description'",
        (candidate_id,),
    )
    assert candidate["description"] == "Recovered publisher synopsis after retry."
    assert candidate["isbn13"] == expected_isbn
    assert json.loads(candidate["genres"]) == ["Science fiction"]
    assert quality["metadata_provider"] == "penguinrandomhouse"
    assert quality["metadata_provider_id"] == product_url
    assert quality["isbn13"] == expected_isbn
    assert provenance == {"provider": "penguinrandomhouse", "provider_id": product_url}


def test_source_description_is_preserved_and_legacy_opening_upgrade_is_exact_record_only():
    opening = "A recorded opening sentence."

    def handler(request):
        if request.url.host == "openlibrary.org":
            return httpx.Response(
                200,
                json={"docs": [{
                    "key": "/works/OL111W",
                    "title": "A Book",
                    "author_name": ["An Author"],
                    "description": "A longer, verified synopsis.",
                    "first_sentence": [opening],
                }]},
            )
        if request.url.host == "www.googleapis.com":
            return httpx.Response(200, json={"items": []})
        raise AssertionError(f"Unexpected provider request: {request.url.host}{request.url.path}")

    async def run(description, *, prefer_catalog_synopsis=False):
        async with _provider_client(handler) as client:
            return await resolve_book_metadata(
                "A Book",
                "An Author",
                description=description,
                expected_provider="openlibrary",
                expected_provider_id="/works/OL111W",
                prefer_catalog_synopsis=prefer_catalog_synopsis,
                client=client,
            )

    source_value = asyncio.run(run(opening))
    upgraded_value = asyncio.run(run(opening, prefer_catalog_synopsis=True))
    curated_value = asyncio.run(run("Publisher copy that does not match the catalog opening.", prefer_catalog_synopsis=True))

    assert source_value["description"] == opening
    assert source_value["description_provider"] == ""
    assert source_value["description_candidate"]["text"] == "A longer, verified synopsis."
    assert upgraded_value["description"] == "A longer, verified synopsis."
    assert upgraded_value["description_provider"] == "openlibrary"
    assert curated_value["description"] == "Publisher copy that does not match the catalog opening."


def test_metadata_resolver_uses_verified_per_field_fallback_and_merges_catalog_subjects():
    def handler(request):
        if request.url.host == "openlibrary.org" and request.url.path == "/search.json":
            return httpx.Response(
                200,
                json={
                    "docs": [{
                        "key": "/works/OL202W",
                        "title": "Fallback Book",
                        "author_name": ["A Writer"],
                        "first_sentence": ["An opening only."],
                        "subject": ["Historical fiction", "Women's fiction"],
                    }],
                },
            )
        if request.url.host == "www.googleapis.com":
            return httpx.Response(
                200,
                json={
                    "items": [{
                        "id": "google-volume-202",
                        "volumeInfo": {
                            "title": "Fallback Book",
                            "authors": ["A Writer"],
                            "description": "A verified Google synopsis with enough context to be useful.",
                            "categories": ["Mystery", "Literature"],
                            "publishedDate": "2004-05",
                            "imageLinks": {"thumbnail": "https://books.google.com/books/content?id=cover-202"},
                        },
                    }],
                },
            )
        raise AssertionError(f"Unexpected provider request: {request.url.host}{request.url.path}")

    async def run():
        async with _provider_client(handler) as client:
            return await resolve_book_metadata(
                "Fallback Book",
                "A Writer",
                genres=["Source genre"],
                client=client,
            )

    metadata = asyncio.run(run())

    assert metadata["description"].startswith("A verified Google synopsis")
    assert metadata["description_provider"] == "google_books"
    assert metadata["genres_provider"] == "openlibrary"
    assert metadata["genres"] == ["Historical fiction", "Women's fiction", "Mystery", "Literature"]
    assert metadata["release_date"] == "2004-05-01"
    assert metadata["release_date_provider"] == "google_books"
    assert metadata["metadata_provenance"]["fields"]["release_date"]["source_field"] == "publishedDate"
    assert metadata["cover_provider"] == "google_books"
    assert [trace["provider"] for trace in metadata["metadata_provenance"]["fetch_trace"]] == [
        "openlibrary",
        "google_books",
    ]
    assert metadata["metadata_provenance"]["fields"]["genres"]["sources"] == [
        {
            "provider": "openlibrary",
            "provider_id": "/works/OL202W",
            "kind": "subjects",
            "source_field": "subject",
            "normalization_version": covers.METADATA_NORMALIZATION_VERSION,
        },
        {
            "provider": "google_books",
            "provider_id": "google-volume-202",
            "kind": "subjects",
            "source_field": "categories",
            "normalization_version": covers.METADATA_NORMALIZATION_VERSION,
        },
    ]


def test_configured_google_books_key_is_sent_but_never_added_to_provenance():
    api_key = "test-google-books-secret"
    google_requests = []

    def handler(request):
        if request.url.host == "openlibrary.org":
            return httpx.Response(200, json={"docs": []})
        if request.url.host == "www.googleapis.com":
            google_requests.append(request)
            return httpx.Response(
                200,
                json={
                    "items": [{
                        "id": "volume-with-key",
                        "volumeInfo": {
                            "title": "Key Book",
                            "authors": ["A Writer"],
                            "description": "A usable verified synopsis.",
                        },
                    }],
                },
            )
        raise AssertionError(f"Unexpected provider request: {request.url.host}{request.url.path}")

    async def run():
        async with _provider_client(handler) as client:
            return await resolve_book_metadata(
                "Key Book",
                "A Writer",
                client=client,
                google_books_api_key=api_key,
            )

    metadata = asyncio.run(run())

    assert len(google_requests) == 1
    assert google_requests[0].url.params["key"] == api_key
    persisted_shape = json.dumps(metadata["metadata_provenance"], sort_keys=True)
    assert api_key not in persisted_shape
    assert "key=" not in persisted_shape
    assert metadata["metadata_provenance"]["fetch_trace"][-1]["provider"] == "google_books"


def test_google_books_rate_limit_is_explicit_in_fetch_trace():
    def handler(request):
        if request.url.host == "openlibrary.org":
            return httpx.Response(200, json={"docs": []})
        if request.url.host == "www.googleapis.com":
            return httpx.Response(429, json={"error": {"message": "quota exceeded"}})
        raise AssertionError(f"Unexpected provider request: {request.url.host}{request.url.path}")

    async def run():
        async with _provider_client(handler) as client:
            return await resolve_book_metadata("Rate Book", "A Writer", client=client)

    metadata = asyncio.run(run())

    google_trace = next(
        trace for trace in metadata["metadata_provenance"]["fetch_trace"]
        if trace["provider"] == "google_books"
    )
    assert google_trace["status"] == "rate_limited"
    assert google_trace["http_status"] == 429
    assert "quota exceeded" not in json.dumps(metadata["metadata_provenance"])


def test_quality_quarantines_rate_limited_lookup_as_provider_unavailable():
    def handler(request):
        if request.url.host == "openlibrary.org":
            return httpx.Response(200, json={"docs": []})
        if request.url.host == "www.googleapis.com":
            return httpx.Response(429, json={"error": {"message": "quota exceeded"}})
        raise AssertionError(f"Unexpected provider request: {request.url.host}{request.url.path}")

    async def run():
        async with _provider_client(handler) as client:
            return await _audit_one(
                {"id": 9, "title": "Rate Limited Book", "author": "A Writer"},
                client,
                {},
            )

    result = asyncio.run(run())

    assert result["quality_status"] == "quarantine"
    assert "catalog_provider_unavailable" in result["flags"]
    assert "catalog_provider_rate_limited" in result["flags"]
    assert "catalog_unmatched" not in result["flags"]
    trace = next(
        item for item in result["metadata_provenance"]["fetch_trace"]
        if item["provider"] == "google_books"
    )
    assert trace["status"] == "rate_limited"
    assert trace["http_status"] == 429


def _fallback_test_query(request):
    if request.url.host == "openlibrary.org":
        return "isbn" if request.url.params.get("isbn") else "title"
    return "isbn" if request.url.params.get("q", "").startswith("isbn:") else "title"


def _fallback_test_record(provider):
    if provider == "openlibrary":
        return httpx.Response(
            200,
            json={"docs": [{
                "key": "/works/OLFALLBACKW",
                "title": "Fallback Book",
                "author_name": ["A Writer"],
                "description": "A useful full synopsis.",
                "subject": ["Literary fiction"],
                "first_publish_year": 2020,
            }]},
        )
    return httpx.Response(
        200,
        json={"items": [{
            "id": "fallback-volume",
            "volumeInfo": {
                "title": "Fallback Book",
                "authors": ["A Writer"],
                "description": "A useful full synopsis.",
                "categories": ["Literary fiction"],
                "publishedDate": "2020",
            },
        }]},
    )


@pytest.mark.parametrize("failing_provider", ["openlibrary", "google_books"])
@pytest.mark.parametrize("failure", ["429", "500", "transport"])
def test_quality_does_not_text_retry_an_unavailable_isbn_provider_but_uses_other_provider(
    failing_provider, failure
):
    calls = []
    other_provider = "google_books" if failing_provider == "openlibrary" else "openlibrary"

    def handler(request):
        provider = "openlibrary" if request.url.host == "openlibrary.org" else "google_books"
        query_kind = _fallback_test_query(request)
        calls.append((provider, query_kind))
        if provider == failing_provider and query_kind == "isbn":
            if failure == "transport":
                raise httpx.ConnectError("offline", request=request)
            return httpx.Response(int(failure), json={"error": "provider unavailable"})
        if provider == other_provider and query_kind == "isbn":
            return httpx.Response(200, json={"docs": []} if provider == "openlibrary" else {"items": []})
        return _fallback_test_record(provider)

    async def run():
        async with _provider_client(handler) as client:
            return await resolve_catalog_match(
                "Fallback Book",
                "A Writer",
                client=client,
                cache={},
                isbn13="9780307474278",
            )

    match = asyncio.run(run())

    assert match is not None
    assert (failing_provider, "isbn") in calls
    assert (failing_provider, "title") not in calls
    assert (other_provider, "isbn") in calls
    assert (other_provider, "title") in calls


@pytest.mark.parametrize("failing_provider", ["openlibrary", "google_books"])
@pytest.mark.parametrize("failure", ["429", "500", "transport"])
def test_metadata_resolver_does_not_text_retry_an_unavailable_isbn_provider(
    failing_provider, failure
):
    calls = []
    other_provider = "google_books" if failing_provider == "openlibrary" else "openlibrary"

    def handler(request):
        provider = "openlibrary" if request.url.host == "openlibrary.org" else "google_books"
        query_kind = _fallback_test_query(request)
        calls.append((provider, query_kind))
        if provider == failing_provider and query_kind == "isbn":
            if failure == "transport":
                raise httpx.ConnectError("offline", request=request)
            return httpx.Response(int(failure), json={"error": "provider unavailable"})
        if provider == other_provider and query_kind == "isbn":
            return httpx.Response(200, json={"docs": []} if provider == "openlibrary" else {"items": []})
        return _fallback_test_record(provider)

    async def run():
        async with _provider_client(handler) as client:
            return await resolve_book_metadata(
                "Fallback Book",
                "A Writer",
                isbn13="9780307474278",
                client=client,
            )

    metadata = asyncio.run(run())

    assert metadata["description"] == "A useful full synopsis."
    assert (failing_provider, "isbn") in calls
    assert (failing_provider, "title") not in calls
    assert (other_provider, "isbn") in calls
    assert (other_provider, "title") in calls


def test_complete_existing_candidate_skips_redundant_title_provider_searches():
    requested_isbn = "9780307474278"
    seen = []

    def handler(request):
        seen.append((request.url.host, request.url.params.get("isbn"), request.url.params.get("q")))
        if request.url.host == "openlibrary.org":
            return httpx.Response(
                200,
                json={"docs": [{
                    "key": "/works/OL500W",
                    "title": "Complete Candidate",
                    "author_name": ["A Writer"],
                    "isbn": [requested_isbn],
                }]},
            )
        if request.url.host == "www.googleapis.com":
            return httpx.Response(200, json={"items": []})
        raise AssertionError(f"Unexpected provider request: {request.url.host}{request.url.path}")

    async def run():
        async with _provider_client(handler) as client:
            return await resolve_catalog_match(
                "Complete Candidate",
                "A Writer",
                client=client,
                cache={},
                isbn13=requested_isbn,
                existing_fields_complete=True,
            )

    match = asyncio.run(run())

    assert match is not None
    assert len(seen) == 2
    assert seen[0][1] == requested_isbn
    assert seen[1][2] == f"isbn:{requested_isbn}"


def test_quality_resolver_uses_configured_google_books_key_without_recording_it():
    api_key = "quality-google-books-secret"
    google_requests = []

    def handler(request):
        if request.url.host == "openlibrary.org":
            return httpx.Response(200, json={"docs": []})
        if request.url.host == "www.googleapis.com":
            google_requests.append(request)
            return httpx.Response(
                200,
                json={
                    "items": [{
                        "id": "quality-volume",
                        "volumeInfo": {
                            "title": "Quality Book",
                            "authors": ["A Writer"],
                            "description": "A complete and useful book summary.",
                            "categories": ["Mystery"],
                        },
                    }],
                },
            )
        raise AssertionError(f"Unexpected provider request: {request.url.host}{request.url.path}")

    async def run():
        async with _provider_client(handler) as client:
            return await resolve_catalog_match(
                "Quality Book",
                "A Writer",
                client=client,
                cache={},
                google_books_api_key=api_key,
            )

    match = asyncio.run(run())

    assert match is not None
    assert len(google_requests) == 1
    assert google_requests[0].url.params["key"] == api_key
    provenance = json.dumps(match["metadata_provenance"], sort_keys=True)
    assert api_key not in provenance
    assert "key=" not in provenance
    genres = match["metadata_provenance"]["fields"]["genres"]
    assert genres["source_field"] == "categories"
    assert genres["sources"][0]["source_field"] == "categories"


def test_isbn_fetch_trace_distinguishes_exact_identifiers_from_omitted_identifiers():
    requested_isbn = "9780307474278"

    def handler(request):
        if request.url.host == "openlibrary.org":
            return httpx.Response(
                200,
                json={"docs": [{
                    "key": "/works/OL222W",
                    "title": "ISBN Work",
                    "author_name": ["A Writer"],
                    "isbn": ["0307474275"],
                    "description": "Verified synopsis.",
                    "subject": ["Fiction"],
                    "cover_i": 22,
                    "first_publish_year": 2010,
                }]},
            )
        if request.url.host == "www.googleapis.com":
            return httpx.Response(200, json={"items": []})
        raise AssertionError(f"Unexpected provider request: {request.url.host}{request.url.path}")

    async def run():
        async with _provider_client(handler) as client:
            return await resolve_book_metadata(
                "ISBN Work",
                "A Writer",
                isbn13=requested_isbn,
                client=client,
            )

    metadata = asyncio.run(run())
    openlibrary_trace = next(
        trace for trace in metadata["metadata_provenance"]["fetch_trace"]
        if trace["provider"] == "openlibrary"
    )

    assert openlibrary_trace["query_kind"] == "isbn"
    assert openlibrary_trace["identifier_evidence"] == "matched"
    assert openlibrary_trace["identifier_verified"] is True


def test_isbn_match_after_projection_limit_is_verified_and_retained():
    requested_isbn = "9780307474278"
    provider_isbns = [_isbn13_with_prefix(index) for index in range(1, 13)]
    provider_isbns.append(requested_isbn)

    def handler(request):
        if request.url.host == "openlibrary.org":
            return httpx.Response(
                200,
                json={"docs": [{
                    "key": "/works/OL223W",
                    "title": "Long Identifier Book",
                    "author_name": ["A Writer"],
                    "isbn": provider_isbns,
                    "description": "Verified full synopsis.",
                    "subject": ["Literary fiction"],
                    "cover_i": 223,
                    "first_publish_year": 2009,
                }]},
            )
        if request.url.host == "www.googleapis.com":
            return httpx.Response(200, json={"items": []})
        raise AssertionError(f"Unexpected provider request: {request.url.host}{request.url.path}")

    async def run_quality():
        async with _provider_client(handler) as client:
            return await resolve_catalog_match(
                "Long Identifier Book",
                "A Writer",
                client=client,
                cache={},
                isbn13=requested_isbn,
            )

    async def run_covers():
        async with _provider_client(handler) as client:
            return await resolve_book_metadata(
                "Long Identifier Book",
                "A Writer",
                isbn13=requested_isbn,
                client=client,
            )

    match = asyncio.run(run_quality())
    cover_metadata = asyncio.run(run_covers())

    assert match is not None
    openlibrary_trace = next(
        trace for trace in match["metadata_provenance"]["fetch_trace"]
        if trace["provider"] == "openlibrary"
    )
    assert openlibrary_trace["identifier_evidence"] == "matched"
    assert openlibrary_trace["identifier_verified"] is True
    identifiers = match["metadata_provenance"]["source_payloads"][0]["payload"]["identifiers"]
    assert len(identifiers) <= 8
    assert identifiers[0] == requested_isbn
    assert _isbn_provenance_values(requested_isbn, provider_isbns)[0] == requested_isbn
    covers_trace = next(
        trace for trace in cover_metadata["metadata_provenance"]["fetch_trace"]
        if trace["provider"] == "openlibrary"
    )
    assert covers_trace["identifier_verified"] is True
    covers_ids = cover_metadata["metadata_provenance"]["source_payloads"][0]["payload"]["identifiers"]
    assert len(covers_ids) <= 8
    assert covers_ids[0] == requested_isbn
    assert match["metadata_provenance"]["fields"]["release_date"]["source_field"] == "first_publish_year"
    assert cover_metadata["metadata_provenance"]["fields"]["release_date"]["source_field"] == "first_publish_year"


@pytest.mark.parametrize(
    ("source_field", "source_value", "expected_release_date"),
    [
        ("first_publish_date", "2002-03-04", "2002-03-04"),
        ("first_publish_year", 2002, "2002-01-01"),
    ],
)
def test_openlibrary_release_date_provenance_tracks_actual_source_field(
    source_field, source_value, expected_release_date
):
    work_id = "/works/OL224W"
    requested_isbn = "9780307474278"

    def catalog_record(key):
        record = {
            "key": key,
            "title": "Year Only Work",
            "author_name": ["A Writer"],
            "isbn": [requested_isbn],
            "subjects": ["Literary fiction"],
        }
        record[source_field] = source_value
        return record

    def handler(request):
        if request.url.host == "openlibrary.org":
            if request.url.path == "/search.json":
                return httpx.Response(200, json={"docs": [catalog_record(work_id)]})
            assert request.url.path == f"{work_id}.json"
            return httpx.Response(200, json={**catalog_record(work_id), "key": work_id})
        if request.url.host == "www.googleapis.com":
            return httpx.Response(200, json={"items": []})
        raise AssertionError(f"Unexpected provider request: {request.url.host}{request.url.path}")

    async def run():
        async with _provider_client(handler) as client:
            metadata = await resolve_book_metadata(
                "Year Only Work",
                "A Writer",
                isbn13=requested_isbn,
                client=client,
            )
            quality_match = await resolve_catalog_match(
                "Year Only Work",
                "A Writer",
                client=client,
                cache={},
                isbn13=requested_isbn,
            )
            work_metadata = await resolve_openlibrary_work_metadata(
                work_id,
                title="Year Only Work",
                client=client,
            )
            return metadata, quality_match, work_metadata

    results = asyncio.run(run())

    for metadata in results:
        date_kind = metadata.get("release_date_kind") or metadata.get("date_kind")
        assert date_kind == ("year" if source_field == "first_publish_year" else "day")
        assert metadata["release_date"] == expected_release_date
        assert metadata["release_date_source_field"] == source_field
        assert metadata["metadata_provenance"]["fields"]["release_date"]["source_field"] == source_field
        payload = metadata["metadata_provenance"]["source_payloads"][0]["payload"]
        assert payload["publication_date_source_field"] == source_field


def test_quality_release_date_ranking_preserves_openlibrary_year_precision():
    requested_isbn = "9780307474278"

    def handler(request):
        if request.url.host == "openlibrary.org":
            return httpx.Response(
                200,
                json={"docs": [{
                    "key": "/works/OLYEARPRECISION",
                    "title": "Precision Book",
                    "author_name": ["A Writer"],
                    "isbn": [requested_isbn],
                    "first_publish_year": 2010,
                    "description": "Open Library synopsis.",
                    "subject": ["Fiction"],
                    "cover_i": 123,
                }]},
            )
        if request.url.host == "www.googleapis.com":
            return httpx.Response(
                200,
                json={"items": [{
                    "id": "google-day-precision",
                    "volumeInfo": {
                        "title": "Precision Book",
                        "authors": ["A Writer"],
                        "industryIdentifiers": [{"type": "ISBN_13", "identifier": requested_isbn}],
                        "publishedDate": "2005-06-07",
                        "description": "Google Books synopsis.",
                        "categories": ["Fiction"],
                        "imageLinks": {"thumbnail": "https://books.google.com/books/content?id=precision"},
                    },
                }]},
            )
        raise AssertionError(f"Unexpected provider request: {request.url.host}{request.url.path}")

    async def run():
        async with _provider_client(handler) as client:
            return await resolve_catalog_match(
                "Precision Book",
                "A Writer",
                client=client,
                cache={},
                isbn13=requested_isbn,
                existing_fields_complete=True,
            )

    match = asyncio.run(run())

    assert match is not None
    assert match["release_date"] == "2005-06-07"
    assert match["release_date_kind"] == "day"
    assert match["release_date_provider"] == "google_books"
    assert match["metadata_provenance"]["fields"]["release_date"]["source_field"] == "publishedDate"


def test_isbn_search_rejects_returned_identifiers_that_contradict_query():
    wrong_isbn = "9780061120084"
    requested_isbn = "9780307474278"
    seen = []

    def handler(request):
        seen.append((request.url.host, parse_qs(request.url.query.decode())))
        if request.url.host == "openlibrary.org":
            params = parse_qs(request.url.query.decode())
            if "isbn" in params:
                return httpx.Response(
                    200,
                    json={"docs": [{
                        "key": "/works/OL303W",
                        "title": "ISBN Book",
                        "author_name": ["A Writer"],
                        "isbn": [wrong_isbn],
                    }]},
                )
            return httpx.Response(200, json={"docs": []})
        if request.url.host == "www.googleapis.com":
            params = parse_qs(request.url.query.decode())
            if params.get("q", [""])[0].startswith("isbn:"):
                return httpx.Response(
                    200,
                    json={"items": [{
                        "id": "wrong-isbn-volume",
                        "volumeInfo": {
                            "title": "ISBN Book",
                            "authors": ["A Writer"],
                            "industryIdentifiers": [{"type": "ISBN_13", "identifier": wrong_isbn}],
                        },
                    }]},
                )
            return httpx.Response(200, json={"items": []})
        raise AssertionError(f"Unexpected provider request: {request.url.host}{request.url.path}")

    async def run():
        async with _provider_client(handler) as client:
            return await resolve_catalog_match(
                "ISBN Book",
                "A Writer",
                client=client,
                cache={},
                isbn13=requested_isbn,
            )

    match = asyncio.run(run())

    assert match is None
    assert any(params.get("isbn") == [requested_isbn] for host, params in seen if host == "openlibrary.org")
    assert any(params.get("q") == [f"isbn:{requested_isbn}"] for host, params in seen if host == "www.googleapis.com")
    assert len(seen) == 4  # The two rejected ISBN results are followed by title/author retries.


def test_verified_openlibrary_work_fetch_requires_exact_key_and_prefers_synopsis():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "key": "/works/OL404W",
                "title": "Read Book",
                "description": "The complete synopsis from this verified work.",
                "first_sentence": ["A short opening."],
                "subjects": ["Memoir", "Family life"],
                "covers": [812],
            },
        )

    async def run():
        async with _provider_client(handler) as client:
            return await resolve_openlibrary_work_metadata("/works/OL404W", "Read Book", "A Reader", client=client)

    metadata = asyncio.run(run())

    assert calls == ["/works/OL404W.json"]
    assert metadata["work_id"] == "/works/OL404W"
    assert metadata["description"] == "The complete synopsis from this verified work."
    assert metadata["description_kind"] == "synopsis"
    assert metadata["genres"] == ["Memoir", "Family life"]
    assert metadata["metadata_provenance"]["fetch_trace"][0]["status"] == "matched"


def test_verified_work_fetch_rejects_mismatched_key_without_search_fallback():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(
            200,
            json={"key": "/works/OL999W", "description": "Wrong work description."},
        )

    async def run():
        async with _provider_client(handler) as client:
            return await resolve_openlibrary_work_metadata("/works/OL404W", "Read Book", "A Reader", client=client)

    metadata = asyncio.run(run())

    assert paths == ["/works/OL404W.json"]
    assert metadata["description"] == ""
    assert metadata["genres"] == []
    assert metadata["metadata_provenance"]["fetch_trace"][0]["status"] == "work_id_mismatch"
    assert metadata["metadata_provenance"]["source_payloads"] == []


def test_bounded_source_payload_caps_unicode_nested_and_escaped_content():
    payload = {
        "title": "Café \\ \"title\"",
        "description": ("📚\\ \"synopsis\"\n" * 6000),
        "genres": [["Genre " + str(index)] * 20 for index in range(40)],
        "nested": {"level": {"deeper": {"still": {"too_deep": "x" * 20000}}}},
        "records": [{"record": index, "value": "value" * 40} for index in range(1000)],
    }

    bounded = _bounded_source_payload(payload)

    encoded = json.dumps(bounded, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert len(encoded) <= covers.SOURCE_PAYLOAD_LIMIT_BYTES
    assert len(bounded["genres"]) <= 12
    assert bounded["_truncated"] is True


def test_openlibrary_rate_budget_serializes_concurrent_requests_with_fake_clock(monkeypatch):
    clock = [10.0]
    starts = []

    monkeypatch.setattr(covers, "_openlibrary_rate_clock", lambda loop: clock[0])

    async def advance_clock(delay):
        clock[0] += delay

    monkeypatch.setattr(covers, "_openlibrary_rate_sleep", advance_clock)

    def handler(request):
        starts.append(clock[0])
        return httpx.Response(200, json={"ok": True})

    async def run():
        async with _provider_client(handler) as client:
            await asyncio.gather(*(
                covers._openlibrary_request(client, OPEN_LIBRARY_SEARCH, params={"title": str(index)})
                for index in range(5)
            ))

    asyncio.run(run())

    assert starts == [10.0, 11.0, 12.0, 13.0, 14.0]
