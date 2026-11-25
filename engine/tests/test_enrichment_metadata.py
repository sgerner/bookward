import asyncio
from datetime import datetime, timedelta, timezone
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
    assert (
        isbn_parts_from_source(
            (),
            "https://www.publishersweekly.com/pw/reviews/review.html?isbn=978-0-306-40615-7",
        )
        == ("9780306406157", "")
    )
    assert (
        isbn_parts_from_source(
            (),
            "https://www.publishersweekly.com/reviews?isbn10=0-06-112008-1",
        )
        == ("9780061120084", "0061120081")
    )
    assert (
        isbn_parts_from_source(
            (),
            "https://www.publishersweekly.com/pw/reviews/review.html?isbn=9780306406150",
        )
        == ("", "")
    )
    assert isbn_parts_from_source((), "https://www.publishersweekly.com/pw/by-topic/industry-news/article/9780306406157.html") == ("", "")
    assert (
        isbn_parts_from_source(
            (),
            "https://www.publishersweekly.com/pw/by-topic/industry-news/article.html?isbn=9780306406157",
        )
        == ("", "")
    )
    assert isbn_parts_from_source((), "https://www.publishersweekly.com/search?isbn=9780306406157") == ("", "")
    assert isbn_parts_from_source((), "https://www.publishersweekly.com/?isbn=9780306406157") == ("", "")
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


def test_scan_source_bounds_chunked_enrichment_and_hydrates_stored_metadata(tmp_path, monkeypatch):
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


def test_scan_source_warm_rescan_skips_items_missing_genres_or_release_date(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db", str(tmp_path / "warm-rescan-sparse.db"))
    initialize(seed_demo=False)
    source_url = "https://www.publishersweekly.com/pw/reviews/starred.html"
    prh_product_url = "https://www.penguinrandomhouse.com/books/777/genreless-prh-book/"
    markup = f"""<table>
      <tr>
        <td class="starredreview-grid">
          <img src="/cover/genreless.jpg">
          <h3><a href="/9780306406157">Genreless Starred Book</a></h3>
          <h4>Ada Writer. Press One, $28 ISBN 978-0-306-40615-7</h4>
          <p><a href="/9780306406157">Complete Review »</a></p>
        </td>
        <td class="starredreview-grid">
          <img src="/cover/dateless.jpg">
          <h3><a href="/0061120081">Dateless Starred Book</a></h3>
          <h4>Grace Author. Press Two, $27 ISBN 0-06-112008-1</h4>
          <p>Editorial blurb without publication date.</p>
        </td>
        <td class="starredreview-grid">
          <img src="https://images.penguinrandomhouse.com/cover/9780143127741">
          <h3><a href="{prh_product_url}">Genreless PRH Book</a></h3>
          <h4>By Solo Author</h4>
          <p>Publisher blurb already present, cover present, genres absent.</p>
        </td>
      </tr>
    </table>""".encode("utf-8")

    product_fetches = []
    catalog_requests = []

    async def fake_fetch(url, client=None):
        if url == source_url:
            return markup, "text/html"
        product_fetches.append(url)
        return b"<html></html>", "text/html"

    def provider_handler(request):
        catalog_requests.append(str(request.url))
        if request.url.host == "openlibrary.org":
            isbn_param = request.url.params.get("isbn", "")
            if isbn_param == "9780306406157":
                # Supplies synopsis and release_date, but never genres.
                return httpx.Response(
                    200,
                    json={"docs": [{
                        "key": "/works/OLGENRELESS1W",
                        "title": "Genreless Starred Book",
                        "author_name": ["Ada Writer"],
                        "isbn": ["9780306406157"],
                        "description": "Catalog synopsis without subjects.",
                        "subject": [],
                        "first_publish_year": 2026,
                    }]},
                )
            if isbn_param == "9780061120084":
                # Supplies genres, but never release_date.
                return httpx.Response(
                    200,
                    json={"docs": [{
                        "key": "/works/OLDATELESS1W",
                        "title": "Dateless Starred Book",
                        "author_name": ["Grace Author"],
                        "isbn": ["9780061120084"],
                        "subject": ["Literary fiction"],
                    }]},
                )
        return httpx.Response(200, json={"docs": [], "items": []})

    monkeypatch.setattr(ingestion, "fetch_bytes", fake_fetch)
    monkeypatch.setattr(ingestion, "metadata_client", lambda: _provider_client(provider_handler))

    source = row("SELECT * FROM sources WHERE url=?", (source_url,))
    assert asyncio.run(ingestion.scan_source(source)) == 3
    assert catalog_requests
    # PRH book already has description and non-weak cover, so PRH product page is not fetched for missing genres.
    assert product_fetches == []

    genreless = row(
        "SELECT c.id,c.genres,c.release_date,q.metadata_checked_at,q.metadata_version "
        "FROM candidates c JOIN candidate_quality q ON q.candidate_id=c.id "
        "WHERE c.title='Genreless Starred Book'"
    )
    dateless = row(
        "SELECT c.id,c.genres,c.release_date,q.metadata_checked_at,q.metadata_version "
        "FROM candidates c JOIN candidate_quality q ON q.candidate_id=c.id "
        "WHERE c.title='Dateless Starred Book'"
    )
    assert json.loads(genreless["genres"]) == []
    assert genreless["release_date"] == "2026-01-01"
    assert genreless["metadata_checked_at"]
    assert genreless["metadata_version"] == covers.METADATA_NORMALIZATION_VERSION
    assert json.loads(dateless["genres"]) == ["Literary fiction"]
    assert dateless["release_date"] is None
    assert dateless["metadata_checked_at"]

    # Warm rescan within retry window must make zero PRH or catalog enrichment calls.
    catalog_requests.clear()

    async def fail_if_enriched(_items):
        raise AssertionError("warm rescan should not re-enrich recently checked sparse items")

    monkeypatch.setattr(ingestion, "enrich_penguin_random_house_items", fail_if_enriched)
    monkeypatch.setattr(ingestion, "enrich_book_metadata", fail_if_enriched)
    assert asyncio.run(ingestion.scan_source(source)) == 3
    assert catalog_requests == []
    assert product_fetches == []

    # Once metadata_checked_at ages past CANDIDATE_METADATA_RETRY_DAYS, item becomes eligible again.
    expired_checked_at = (
        datetime.now(timezone.utc)
        - timedelta(days=ingestion.CANDIDATE_METADATA_RETRY_DAYS + 1)
    ).isoformat(timespec="microseconds")
    with transaction() as con:
        con.execute(
            "UPDATE candidate_quality SET metadata_checked_at=? WHERE candidate_id=?",
            (expired_checked_at, genreless["id"]),
        )

    retried_titles = []

    async def passthrough_prh(items):
        return items

    async def record_retry(items):
        retried_titles.extend(item["title"] for item in items)
        return items

    monkeypatch.setattr(ingestion, "enrich_penguin_random_house_items", passthrough_prh)
    monkeypatch.setattr(ingestion, "enrich_book_metadata", record_retry)
    assert asyncio.run(ingestion.scan_source(source)) == 3
    assert retried_titles == ["Genreless Starred Book"]


def test_scan_source_commits_completed_chunks_before_mid_scan_failure_and_resumes(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db", str(tmp_path / "chunk-failure.db"))
    initialize(seed_demo=False)
    monkeypatch.setattr(ingestion, "SOURCE_METADATA_ENRICHMENT_BATCH_SIZE", 2)
    monkeypatch.setattr(ingestion, "SOURCE_METADATA_ENRICHMENT_MAX_BATCHES", 2)

    source_url = "https://www.publishersweekly.com/pw/reviews/starred.html"
    source = row("SELECT * FROM sources WHERE url=?", (source_url,))
    with transaction() as con:
        con.execute(
            """INSERT INTO candidates(
                title,author,description,cover_url,source_url,source_id,status,normalized_key
            ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                "Stale Prior Candidate",
                "Prior Writer",
                "Prior blurb.",
                "https://www.publishersweekly.com/cover/stale.jpg",
                source_url,
                source["id"],
                "new",
                normalize_key("Stale Prior Candidate", "Prior Writer"),
            ),
        )

    # 5 incomplete items in inline HTML -> capped to 2 batches * 2 = 4 items on first scan.
    cards_html = "\n".join(
        f"""<tr><td class="starredreview-grid">
          <img src="/cover/book-{idx}.jpg">
          <h3><a href="/books/book-{idx}">Chunk Book {idx}</a></h3>
          <h4>By Author {idx}</h4>
          <p>Complete Review »</p>
        </td></tr>"""
        for idx in range(5)
    )
    markup = f"<table>{cards_html}</table>".encode("utf-8")

    async def fake_fetch(_url, client=None):
        return markup, "text/html"

    seen_batches = []
    fail_second_chunk = True

    async def passthrough_prh(items):
        return items

    async def flaky_catalog_enrich(items):
        titles = [item["title"] for item in items]
        seen_batches.append(titles)
        if fail_second_chunk and len(seen_batches) == 2:
            raise RuntimeError("transient catalog timeout in chunk 2")
        return [
            {
                **item,
                "description": f"Enriched synopsis for {item['title']}.",
                "genres": ["Literary fiction"],
            }
            for item in items
        ]

    monkeypatch.setattr(ingestion, "fetch_bytes", fake_fetch)
    monkeypatch.setattr(ingestion, "enrich_penguin_random_house_items", passthrough_prh)
    monkeypatch.setattr(ingestion, "enrich_book_metadata", flaky_catalog_enrich)

    with pytest.raises(RuntimeError, match="transient catalog timeout in chunk 2"):
        asyncio.run(ingestion.scan_source(row("SELECT * FROM sources WHERE id=?", (source["id"],))))

    assert seen_batches == [["Chunk Book 0", "Chunk Book 1"], ["Chunk Book 2", "Chunk Book 3"]]
    persisted_after_failure = rows(
        "SELECT c.title,c.description,q.metadata_checked_at "
        "FROM candidates c JOIN candidate_quality q ON q.candidate_id=c.id "
        "WHERE c.source_id=? ORDER BY c.title",
        (source["id"],),
    )
    persisted_by_title = {item["title"]: item for item in persisted_after_failure}
    # Chunk 1 committed its two enriched books; stale candidate was NOT prematurely deleted; last_status stayed unset.
    assert set(persisted_by_title) == {"Chunk Book 0", "Chunk Book 1", "Stale Prior Candidate"}
    assert persisted_by_title["Chunk Book 0"]["description"] == "Enriched synopsis for Chunk Book 0."
    assert persisted_by_title["Chunk Book 0"]["metadata_checked_at"]
    assert persisted_by_title["Chunk Book 1"]["metadata_checked_at"]
    assert row("SELECT last_status FROM sources WHERE id=?", (source["id"],))["last_status"] is None

    # Retry scan after transient failure clears: Chunk 0 & 1 are skipped via metadata_checked_at,
    # remaining unchecked books (Chunk Book 2, 3, 4) are enriched, and stale candidate is cleaned up.
    seen_batches.clear()
    fail_second_chunk = False
    collected = asyncio.run(ingestion.scan_source(row("SELECT * FROM sources WHERE id=?", (source["id"],))))
    assert collected == 5
    assert seen_batches == [["Chunk Book 2", "Chunk Book 3"], ["Chunk Book 4"]]
    final_titles = {
        item["title"]
        for item in rows("SELECT title FROM candidates WHERE source_id=?", (source["id"],))
    }
    assert final_titles == {f"Chunk Book {idx}" for idx in range(5)}
    assert row("SELECT last_status FROM sources WHERE id=?", (source["id"],))["last_status"] == "ok:5"


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
    seen_queries: list[tuple[str, dict[str, list[str]]]] = []

    def handler(request):
        seen_queries.append((request.url.host, parse_qs(request.url.query.decode())))
        if request.url.host == "openlibrary.org":
            return httpx.Response(
                200,
                json={"docs": [{
                    "key": "/works/OLYEARPRECISION",
                    "title": "Precision Book",
                    "author_name": ["A Writer"],
                    "isbn": [requested_isbn],
                    # Polluted future work year (< 2100) must be rejected rather
                    # than outranking Google's verified day-precision date; omit
                    # cover_i too so existing_fields_complete=True proves title
                    # fallback is suppressed.
                    "first_publish_year": 2099,
                    "description": "Open Library synopsis.",
                    "subject": ["Fiction"],
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
    assert len(seen_queries) == 2
    assert match["release_date"] == "2005-06-07"
    assert match["release_date_kind"] == "day"
    assert match["release_date_provider"] == "google_books"
    assert match["metadata_provenance"]["fields"]["release_date"]["source_field"] == "publishedDate"
    ol_payload = next(
        entry["payload"]
        for entry in match["metadata_provenance"]["source_payloads"]
        if entry["provider"] == "openlibrary"
    )
    assert ol_payload["publication_date"] == ""
    assert ol_payload["publication_date_source_field"] == ""


@pytest.mark.parametrize(
    ("ol_doc_fields", "expected_date", "expected_kind", "expected_source_field"),
    [
        ({"first_publish_year": 2010}, "2010-01-01", "year", "first_publish_year"),
        ({"first_publish_date": "1934-05"}, "1934-05-01", "month", "first_publish_date"),
        (
            {"first_publish_date": "2099-01-01", "first_publish_year": 1984},
            "1984-01-01",
            "year",
            "first_publish_year",
        ),
    ],
)
def test_openlibrary_work_dates_outrank_storefront_edition_dates(
    ol_doc_fields, expected_date, expected_kind, expected_source_field
):
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
                    **ol_doc_fields,
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
        if request.url.host == "itunes.apple.com":
            return httpx.Response(
                200,
                json={"results": [{
                    "trackId": 999111,
                    "trackName": "Precision Book",
                    "artistName": "A Writer",
                    "releaseDate": "2023-12-25T08:00:00Z",
                    "description": "Apple Books synopsis.",
                    "genres": ["Fiction"],
                    "artworkUrl100": "https://is1-ssl.mzstatic.com/image/thumb/Publication/test.jpg/100x100bb.jpg",
                }]},
            )
        raise AssertionError(f"Unexpected provider request: {request.url.host}{request.url.path}")

    async def run():
        async with _provider_client(handler) as client:
            complete_match = await resolve_catalog_match(
                "Precision Book",
                "A Writer",
                client=client,
                cache={},
                isbn13=requested_isbn,
                existing_fields_complete=True,
            )
            incomplete_match = await resolve_catalog_match(
                "Precision Book",
                "A Writer",
                client=client,
                cache={},
                isbn13=requested_isbn,
                existing_fields_complete=False,
            )
            cover_match = await resolve_book_metadata(
                "Precision Book",
                "A Writer",
                isbn13=requested_isbn,
                client=client,
            )
            return complete_match, incomplete_match, cover_match

    complete_match, match, cover_match = asyncio.run(run())

    assert [
        (trace["provider"], trace["query_kind"])
        for trace in complete_match["metadata_provenance"]["fetch_trace"]
    ] == [
        ("openlibrary", "isbn"),
        ("google_books", "isbn"),
    ]
    for quality_result in (complete_match, match):
        assert quality_result is not None
        assert quality_result["release_date"] == expected_date
        assert quality_result["release_date_kind"] == expected_kind
        assert quality_result["release_date_provider"] == "openlibrary"
        assert quality_result["release_date_source_field"] == expected_source_field
        assert quality_result["metadata_provenance"]["fields"]["release_date"]["provider"] == "openlibrary"
        assert quality_result["metadata_provenance"]["fields"]["release_date"]["source_field"] == expected_source_field

    assert cover_match["release_date"] == expected_date
    assert cover_match["date_kind"] == expected_kind
    assert cover_match["release_date_provider"] == "openlibrary"
    assert cover_match["release_date_source_field"] == expected_source_field
    assert cover_match["metadata_provenance"]["fields"]["release_date"]["provider"] == "openlibrary"
    assert cover_match["metadata_provenance"]["fields"]["release_date"]["source_field"] == expected_source_field


def test_edition_release_dates_still_rank_by_precision_when_openlibrary_lacks_work_date():
    requested_isbn = "9780307474278"

    def handler(request):
        if request.url.host == "openlibrary.org":
            return httpx.Response(
                200,
                json={"docs": [{
                    "key": "/works/OLNODATEW",
                    "title": "Edition Date Book",
                    "author_name": ["A Writer"],
                    "isbn": [requested_isbn],
                    "description": "",
                    "subject": ["Fiction"],
                }]},
            )
        if request.url.host == "www.googleapis.com":
            is_isbn_query = "isbn:" in request.url.query.decode()
            return httpx.Response(
                200,
                json={"items": [{
                    "id": "google-year-edition" if is_isbn_query else "google-day-edition",
                    "volumeInfo": {
                        "title": "Edition Date Book",
                        "authors": ["A Writer"],
                        "industryIdentifiers": [{"type": "ISBN_13", "identifier": requested_isbn}],
                        "publishedDate": "2024" if is_isbn_query else "2024-09-17",
                        "description": "" if is_isbn_query else "Google Books full synopsis.",
                        "categories": ["Fiction"],
                    },
                }]},
            )
        if request.url.host == "itunes.apple.com":
            return httpx.Response(
                200,
                json={"results": [{
                    "trackId": 444555,
                    "trackName": "Edition Date Book",
                    "artistName": "A Writer",
                    "releaseDate": "2024-09-17T08:00:00Z",
                    "description": "Apple Books synopsis.",
                    "genres": ["Fiction"],
                    "artworkUrl100": "https://is1-ssl.mzstatic.com/image/thumb/Publication/ed.jpg/100x100bb.jpg",
                }]},
            )
        raise AssertionError(f"Unexpected provider request: {request.url.host}{request.url.path}")

    async def run():
        async with _provider_client(handler) as client:
            quality_match = await resolve_catalog_match(
                "Edition Date Book",
                "A Writer",
                client=client,
                cache={},
                isbn13=requested_isbn,
                existing_fields_complete=False,
            )
            cover_match = await resolve_book_metadata(
                "Edition Date Book",
                "A Writer",
                isbn13=requested_isbn,
                client=client,
            )
            return quality_match, cover_match

    match, cover_match = asyncio.run(run())

    assert match is not None
    assert match["release_date"] == "2024-09-17"
    assert match["release_date_kind"] == "day"
    assert match["release_date_provider"] == "google_books"
    assert cover_match["release_date"] == "2024-09-17"
    assert cover_match["date_kind"] == "day"
    assert cover_match["release_date_provider"] in {"google_books", "apple_books"}


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


def test_resolve_catalog_match_prefers_verified_isbn_synopsis_and_blocks_foreign_or_conflicting_editions():
    verified_synopsis = (
        "Twenty-year-old Violet Sorrengail was supposed to enter the Scribe Quadrant, "
        "living a quiet life among books and history."
    )
    assert len(verified_synopsis) == 121
    spanish_guide_blurb = (
        "Resumen completo y guía de estudio de Alas de Sangre: análisis detallado de capítulos, "
        "personajes, temas principales y preguntas de discusión para estudiantes y clubes de lectura "
        "que buscan comprender cada escena de esta edición especial en español ya"
    )
    assert len(spanish_guide_blurb) == 251

    def handler(request):
        params = parse_qs(request.url.query.decode())
        if request.url.host == "openlibrary.org":
            if "isbn" in params:
                return httpx.Response(
                    200,
                    json={
                        "docs": [
                            {
                                "key": "/works/OLFOURTHWINGW",
                                "title": "Fourth Wing",
                                "author_name": ["Rebecca Yarros"],
                                "isbn": ["9781649374042"],
                                "description": verified_synopsis,
                                "first_publish_date": "2023-05-02",
                                "subject": ["Fantasy", "Dragons"],
                                "cover_i": 14315081,
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
                            "title": "Fourth Wing",
                            "author_name": ["Rebecca Yarros"],
                            "isbn": ["9788408279990"],
                            "description": spanish_guide_blurb,
                            "subject": ["Study Aids"],
                            "language": ["spa"],
                        },
                        {
                            "key": "/works/OLCONFLICTISBNW",
                            "title": "Fourth Wing",
                            "author_name": ["Rebecca Yarros"],
                            "isbn": ["9780061120084"],
                            "description": "Longer unverified conflicting ISBN synopsis " * 8,
                            "first_publish_date": "2025-01-01",
                            "subject": ["Biography"],
                            "language": ["eng"],
                        },
                    ]
                },
            )
        if request.url.host == "www.googleapis.com":
            return httpx.Response(200, json={"items": []})
        raise AssertionError(f"Unexpected host: {request.url.host}")

    async def run():
        async with _provider_client(handler) as client:
            return await resolve_catalog_match(
                "Fourth Wing",
                "Rebecca Yarros",
                client=client,
                cache={},
                isbn13="9781649374042",
            )

    match = asyncio.run(run())
    assert match is not None
    assert match["provider"] == "openlibrary"
    assert match["work_id"] == "/works/OLFOURTHWINGW"
    assert match["description"] == verified_synopsis
    assert match["release_date"] == "2023-05-02"
    assert match["genres"] == ["Fantasy", "Dragons"]
    assert match["provider_lookup_unavailable"] is False


def test_resolvers_call_google_books_without_waiting_on_openlibrary_slot_and_preserve_trace_order(
    monkeypatch,
):
    requested_isbn = "9780307474278"
    call_order: list[str] = []
    active_gate: list[asyncio.Event] = []

    async def gated_openlibrary_slot():
        await asyncio.wait_for(active_gate[0].wait(), timeout=2.0)

    monkeypatch.setattr(covers, "wait_for_openlibrary_request_slot", gated_openlibrary_slot)

    def handler(request):
        if request.url.host == "openlibrary.org":
            call_order.append("openlibrary")
            return httpx.Response(
                200,
                json={
                    "docs": [{
                        "key": "/works/OL123456W",
                        "title": "Concurrent Book",
                        "author_name": ["A Writer"],
                        "isbn": [requested_isbn],
                        "first_sentence": ["Short opening."],
                        "subject": ["Historical fiction"],
                        "first_publish_year": 2021,
                    }],
                },
            )
        if request.url.host == "www.googleapis.com":
            call_order.append("google_books")
            active_gate[0].set()
            return httpx.Response(
                200,
                json={
                    "items": [{
                        "id": "gb-concurrent-1",
                        "volumeInfo": {
                            "title": "Concurrent Book",
                            "authors": ["A Writer"],
                            "description": "A complete verified Google Books synopsis.",
                            "categories": ["Mystery"],
                            "publishedDate": "2021-06-15",
                            "industryIdentifiers": [
                                {"type": "ISBN_13", "identifier": requested_isbn}
                            ],
                            "imageLinks": {
                                "thumbnail": "https://books.google.com/books/content?id=gb-concurrent-1"
                            },
                        },
                    }],
                },
            )
        raise AssertionError(f"Unexpected provider request: {request.url.host}{request.url.path}")

    async def run_covers():
        active_gate[:] = [asyncio.Event()]
        call_order.clear()
        async with _provider_client(handler) as client:
            return await resolve_book_metadata(
                "Concurrent Book",
                "A Writer",
                isbn13=requested_isbn,
                client=client,
            )

    metadata = asyncio.run(run_covers())
    assert call_order == ["google_books", "openlibrary"]
    assert [t["provider"] for t in metadata["metadata_provenance"]["fetch_trace"]] == [
        "openlibrary",
        "google_books",
    ]
    assert [p["provider"] for p in metadata["metadata_provenance"]["source_payloads"]] == [
        "openlibrary",
        "google_books",
    ]
    _filtered, evidence, verified_work_id = ingestion._verified_read_metadata(
        metadata,
        {
            "title": "Concurrent Book",
            "author": "A Writer",
            "isbn": requested_isbn,
            "openlibrary_work_id": "",
        },
    )
    assert evidence["provider"] == "openlibrary"
    assert verified_work_id == "/works/OL123456W"

    async def run_quality():
        active_gate[:] = [asyncio.Event()]
        call_order.clear()
        async with _provider_client(handler) as client:
            return await resolve_catalog_match(
                "Concurrent Book",
                "A Writer",
                client=client,
                cache={},
                isbn13=requested_isbn,
            )

    match = asyncio.run(run_quality())
    assert match is not None
    assert call_order == ["google_books", "openlibrary"]
    assert match["provider"] == "openlibrary"
    assert match["work_id"] == "/works/OL123456W"
    assert match["description_provider"] == "google_books"
    assert [t["provider"] for t in match["metadata_provenance"]["fetch_trace"]] == [
        "openlibrary",
        "google_books",
    ]
    assert [p["provider"] for p in match["metadata_provenance"]["source_payloads"]] == [
        "openlibrary",
        "google_books",
    ]


def test_provisional_isbn_skips_quality_fallback_and_runs_metadata_fallbacks_concurrently(
    monkeypatch,
):
    provisional_isbn = "9780307474278"
    quality_calls: list[tuple[str, str]] = []

    def provisional_quality_handler(request):
        provider = "openlibrary" if request.url.host == "openlibrary.org" else "google_books"
        kind = _fallback_test_query(request)
        quality_calls.append((provider, kind))
        if provider == "openlibrary":
            return httpx.Response(200, json={"docs": []})
        return httpx.Response(
            200,
            json={
                "items": [{
                    "id": "gb-provisional",
                    "volumeInfo": {
                        "title": "Upcoming Release",
                        "authors": ["A Writer"],
                        "description": "Publisher verified synopsis for upcoming release.",
                        "categories": ["Literary fiction"],
                        "publishedDate": "2027-02-01",
                        "industryIdentifiers": [
                            {"type": "ISBN_13", "identifier": provisional_isbn}
                        ],
                        "imageLinks": {
                            "thumbnail": "https://books.google.com/books/content?id=gb-provisional"
                        },
                    },
                }],
            },
        )

    async def run_quality():
        async with _provider_client(provisional_quality_handler) as client:
            return await resolve_catalog_match(
                "Upcoming Release",
                "A Writer",
                client=client,
                cache={},
                isbn13=provisional_isbn,
            )

    match = asyncio.run(run_quality())
    assert match is not None
    assert match["provider"] == "google_books"
    assert sorted(quality_calls) == [("google_books", "isbn"), ("openlibrary", "isbn")]

    fallback_gate: list[asyncio.Event] = []
    metadata_events: list[tuple[str, str]] = []

    async def wait_until_google_fallback():
        await asyncio.wait_for(fallback_gate[0].wait(), timeout=2.0)

    monkeypatch.setattr(covers, "wait_for_openlibrary_request_slot", wait_until_google_fallback)

    def metadata_fallback_handler(request):
        provider = "openlibrary" if request.url.host == "openlibrary.org" else "google_books"
        kind = _fallback_test_query(request)
        metadata_events.append((provider, kind))
        if provider == "google_books":
            if kind == "isbn":
                return httpx.Response(200, json={"items": []})
            fallback_gate[0].set()
            return _fallback_test_record("google_books")
        if kind == "isbn":
            return httpx.Response(200, json={"docs": []})
        return _fallback_test_record("openlibrary")

    async def run_metadata():
        fallback_gate[:] = [asyncio.Event()]
        async with _provider_client(metadata_fallback_handler) as client:
            return await resolve_book_metadata(
                "Fallback Book",
                "A Writer",
                isbn13=provisional_isbn,
                client=client,
            )

    metadata = asyncio.run(run_metadata())
    assert metadata_events[:2] == [("google_books", "isbn"), ("google_books", "title")]
    assert [(t["provider"], t["query_kind"]) for t in metadata["metadata_provenance"]["fetch_trace"]] == [
        ("openlibrary", "isbn"),
        ("openlibrary", "title_author"),
        ("google_books", "isbn"),
        ("google_books", "title_author"),
    ]


def _without_fetched_at(value):
    if isinstance(value, dict):
        return {k: _without_fetched_at(v) for k, v in value.items() if k != "fetched_at"}
    if isinstance(value, list):
        return [_without_fetched_at(v) for v in value]
    return value


@pytest.mark.parametrize("resolver_kind", ["quality", "covers"])
def test_concurrent_workers_space_openlibrary_starts_with_fake_clock_without_blocking_google(
    monkeypatch, resolver_kind
):
    clock = [10.0]
    ol_starts: list[float] = []
    gb_starts: list[float] = []
    ol_inflight = 0
    ol_peak = 0
    gb_inflight = 0
    gb_peak = 0

    monkeypatch.setattr(covers, "_openlibrary_rate_clock", lambda loop: clock[0])

    async def advance_clock(delay: float) -> None:
        await asyncio.sleep(0)
        clock[0] += delay

    monkeypatch.setattr(covers, "_openlibrary_rate_sleep", advance_clock)

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal ol_inflight, ol_peak, gb_inflight, gb_peak
        if request.url.host == "openlibrary.org":
            ol_starts.append(clock[0])
            ol_inflight += 1
            ol_peak = max(ol_peak, ol_inflight)
            await asyncio.sleep(0)
            ol_inflight -= 1
            return httpx.Response(200, json={"docs": []})
        if request.url.host == "www.googleapis.com":
            gb_starts.append(clock[0])
            q = request.url.params.get("q", "")
            isbn = q.split("isbn:")[1] if q.startswith("isbn:") else _isbn13_with_prefix(99)
            idx = int(isbn[3:12]) if q.startswith("isbn:") else int(q.split("Book ")[1].split(" ")[0])
            gb_inflight += 1
            gb_peak = max(gb_peak, gb_inflight)
            await asyncio.sleep(0)
            gb_inflight -= 1
            return httpx.Response(
                200,
                json={
                    "items": [{
                        "id": f"bench-vol-{idx}",
                        "volumeInfo": {
                            "title": f"Book {idx}",
                            "authors": ["Bench Author"],
                            "description": "Verified benchmark synopsis.",
                            "categories": ["Literary fiction"],
                            "publishedDate": "2026-06-01",
                            "industryIdentifiers": [{"type": "ISBN_13", "identifier": isbn}],
                            "imageLinks": {
                                "thumbnail": f"https://books.google.com/books/content?id=bench-{idx}"
                            },
                        },
                    }],
                },
            )
        return httpx.Response(200, json={"results": []})

    async def run():
        semaphore = asyncio.Semaphore(8)
        cache = {}
        # Include 12 distinct books plus a duplicate of Book 1 to verify single-flight cache sharing.
        book_indices = [*range(1, 13), 1]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            async def worker(idx: int):
                async with semaphore:
                    if resolver_kind == "quality":
                        return await resolve_catalog_match(
                            f"Book {idx}",
                            "Bench Author",
                            client=client,
                            cache=cache,
                            isbn13=_isbn13_with_prefix(idx),
                        )
                    return await resolve_book_metadata(
                        f"Book {idx}",
                        "Bench Author",
                        client=client,
                        lookup_cache=cache,
                    )

            return await asyncio.gather(*(worker(index) for index in book_indices))

    results = asyncio.run(run())
    assert len(results) == 13
    # fetched_at comes from the wall clock (second resolution), so the first and last
    # lookups can straddle a second boundary; compare everything else.
    assert _without_fetched_at(results[0]) == _without_fetched_at(results[-1])
    assert ol_peak == 1
    assert gb_peak == 8
    assert len(ol_starts) == 12
    assert len(gb_starts) == 12
    assert gb_starts[:8] == [10.0] * 8
    assert all(
        later - earlier >= covers.OPEN_LIBRARY_MIN_REQUEST_INTERVAL_SECONDS
        for earlier, later in zip(ol_starts, ol_starts[1:])
    )
    assert ol_starts == [10.0 + offset for offset in range(12)]


@pytest.mark.parametrize(
    ("failing_provider", "failure_mode"),
    [
        ("openlibrary", "http_503"),
        ("openlibrary", "connect_error"),
        ("google_books", "http_503"),
        ("google_books", "connect_error"),
        ("google_books", "http_429"),
    ],
)
def test_concurrent_lookup_preserves_surviving_provider_when_other_fails(
    monkeypatch, failing_provider, failure_mode
):
    requested_isbn = "9780307474278"
    call_order: list[str] = []
    ol_gate: list[asyncio.Event] = []

    async def gated_openlibrary_slot():
        await asyncio.wait_for(ol_gate[0].wait(), timeout=2.0)

    monkeypatch.setattr(covers, "wait_for_openlibrary_request_slot", gated_openlibrary_slot)

    def fail_response(request: httpx.Request) -> httpx.Response:
        if failure_mode == "connect_error":
            raise httpx.ConnectError("simulated provider network failure", request=request)
        if failure_mode == "http_429":
            return httpx.Response(429, json={"error": {"message": "quota exceeded"}})
        return httpx.Response(503, json={"error": "unavailable"})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "openlibrary.org":
            call_order.append("openlibrary")
            if failing_provider == "openlibrary":
                return fail_response(request)
            return httpx.Response(
                200,
                json={
                    "docs": [{
                        "key": "/works/OLSURVIVEW",
                        "title": "Resilient Book",
                        "author_name": ["Steady Author"],
                        "isbn": [requested_isbn],
                        "description": "Verified Open Library synopsis when Google fails.",
                        "subject": ["Literary fiction"],
                        "first_publish_year": 2022,
                        "cover_i": 54321,
                    }],
                },
            )
        if request.url.host == "www.googleapis.com":
            call_order.append("google_books")
            ol_gate[0].set()
            if failing_provider == "google_books":
                return fail_response(request)
            return httpx.Response(
                200,
                json={
                    "items": [{
                        "id": "gb-survive-1",
                        "volumeInfo": {
                            "title": "Resilient Book",
                            "authors": ["Steady Author"],
                            "description": "Verified Google Books synopsis when Open Library fails.",
                            "categories": ["Literary fiction"],
                            "publishedDate": "2022-09-10",
                            "industryIdentifiers": [
                                {"type": "ISBN_13", "identifier": requested_isbn}
                            ],
                            "imageLinks": {
                                "thumbnail": "https://books.google.com/books/content?id=gb-survive-1"
                            },
                        },
                    }],
                },
            )
        raise AssertionError(f"Unexpected provider request: {request.url.host}{request.url.path}")

    expected_survivor = "google_books" if failing_provider == "openlibrary" else "openlibrary"
    expected_failure_status = (
        "request_error"
        if failure_mode == "connect_error"
        else "rate_limited"
        if failure_mode == "http_429"
        else "server_error"
        if failure_mode == "http_503"
        else "http_error"
    )
    # The audit path (quality.py) keeps the coarser ``http_error`` status for 5xx.
    expected_audit_failure_status = (
        "request_error"
        if failure_mode == "connect_error"
        else "rate_limited"
        if failure_mode == "http_429"
        else "http_error"
    )
    expected_audit_flags = (
        ["catalog_provider_rate_limited", "catalog_provider_unavailable"]
        if failure_mode == "http_429"
        else ["catalog_provider_unavailable"]
    )

    async def run_covers():
        ol_gate[:] = [asyncio.Event()]
        call_order.clear()
        async with _provider_client(handler) as client:
            return await resolve_book_metadata(
                "Resilient Book",
                "Steady Author",
                isbn13=requested_isbn,
                client=client,
            )

    metadata = asyncio.run(run_covers())
    assert call_order == ["google_books", "openlibrary"]
    assert metadata["provider"] == expected_survivor
    assert metadata["description"].startswith(
        "Verified Google Books synopsis"
        if expected_survivor == "google_books"
        else "Verified Open Library synopsis"
    )
    assert metadata["genres"] == ["Literary fiction"]
    assert metadata["cover_url"] != ""
    assert metadata["release_date"].startswith("2022-")
    cover_traces = metadata["metadata_provenance"]["fetch_trace"]
    assert [t["provider"] for t in cover_traces] == ["openlibrary", "google_books"]
    failed_cover_trace = next(t for t in cover_traces if t["provider"] == failing_provider)
    assert failed_cover_trace["status"] == expected_failure_status

    async def run_audit():
        ol_gate[:] = [asyncio.Event()]
        call_order.clear()
        rate_limited: set[str] = set()
        async with _provider_client(handler) as client:
            first = await _audit_one(
                {"id": 101, "title": "Resilient Book", "author": "Steady Author", "isbn13": requested_isbn},
                client,
                {},
                rate_limited_providers=rate_limited,
            )
            second = None
            if failure_mode == "http_429":
                ol_gate[:] = [asyncio.Event()]
                ol_gate[0].set()
                second = await _audit_one(
                    {"id": 102, "title": "Resilient Book", "author": "Steady Author", "isbn13": requested_isbn},
                    client,
                    {},
                    rate_limited_providers=rate_limited,
                )
            return first, second

    audit_result, skipped_followup = asyncio.run(run_audit())
    assert audit_result["quality_status"] == "accepted"
    assert audit_result["provider"] == expected_survivor
    assert audit_result["provider_lookup_unavailable"] is True
    assert audit_result["flags"] == expected_audit_flags
    audit_traces = audit_result["metadata_provenance"]["fetch_trace"]
    assert [t["provider"] for t in audit_traces] == ["openlibrary", "google_books"]
    failed_audit_trace = next(t for t in audit_traces if t["provider"] == failing_provider)
    assert failed_audit_trace["status"] == expected_audit_failure_status
    if skipped_followup is not None:
        assert skipped_followup["quality_status"] == "accepted"
        assert skipped_followup["provider_lookup_unavailable"] is True
        assert skipped_followup["flags"] == expected_audit_flags
        skipped_gb_trace = next(
            t for t in skipped_followup["metadata_provenance"]["fetch_trace"]
            if t["provider"] == "google_books"
        )
        assert skipped_gb_trace["skipped"] is True
        assert skipped_gb_trace["status"] == "rate_limited"
