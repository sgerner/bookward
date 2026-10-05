"""Cover metadata lookup and safe URL fallbacks.

Book-list pages are inconsistent about exposing cover art.  A source can have
valid JSON-LD but no image, or it can point at an Open Library ISBN URL that
returns the service's 1x1 "not found" GIF.  This module keeps cover discovery
independent from the source parser and only talks to fixed, public metadata
providers.  It deliberately does not download arbitrary source-provided URLs
on the server, which keeps the cover enrichment path out of SSRF territory.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unicodedata
import weakref
from datetime import date, datetime, timezone
from urllib.parse import urlencode, urlparse

import httpx
from bs4 import BeautifulSoup

from .config import settings
from .identity import book_author_identity_key, book_catalog_title_identity_key
from .isbn import canonical_isbn, isbn_parts
from .subjects import normalize_subjects

OPEN_LIBRARY_SEARCH = "https://openlibrary.org/search.json"
GOOGLE_BOOKS_SEARCH = "https://www.googleapis.com/books/v1/volumes"
PLACEHOLDER_COVER = "https://placehold.co/640x960"
OPEN_LIBRARY_WEB = "https://openlibrary.org"

# These hosts are common book-cover CDNs.  A source-host image is also allowed
# by ``safe_cover_url`` so publisher pages can keep their own artwork.
KNOWN_COVER_HOSTS = frozenset(
    {
        "covers.openlibrary.org",
        "images.openlibrary.org",
        "books.google.com",
        "books.googleusercontent.com",
        "googleusercontent.com",
        "images-na.ssl-images-amazon.com",
        "images.amazon.com",
        "static01.nyt.com",
        "penguinrandomhouse.com",
        "mzstatic.com",
        "gr-assets.com",
        "placehold.co",
    }
)

METADATA_USER_AGENT = "Bookward/0.1 (+self-hosted book recommender)"
METADATA_NORMALIZATION_VERSION = "catalog-metadata-v1"
METADATA_GENRE_LIMIT = 12
SOURCE_PAYLOAD_LIMIT_BYTES = 8192
OPEN_LIBRARY_MIN_REQUEST_INTERVAL_SECONDS = 1.0
_CATALOG_PROVIDER_FAILURE_STATUSES = frozenset({
    "rate_limited",
    "http_error",
    "request_error",
    "invalid_json",
    "invalid_response",
})
_OPEN_LIBRARY_RATE_LIMITERS = weakref.WeakKeyDictionary()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def _openlibrary_rate_sleep(delay: float) -> None:
    await asyncio.sleep(delay)


def _openlibrary_rate_clock(loop: asyncio.AbstractEventLoop) -> float:
    return loop.time()


async def wait_for_openlibrary_request_slot() -> None:
    """Reserve the shared one-request-per-second slot for Open Library."""
    loop = asyncio.get_running_loop()
    state = _OPEN_LIBRARY_RATE_LIMITERS.get(loop)
    if state is None:
        state = {"lock": asyncio.Lock(), "last_started": None}
        _OPEN_LIBRARY_RATE_LIMITERS[loop] = state
    async with state["lock"]:
        now = _openlibrary_rate_clock(loop)
        previous = state["last_started"]
        if previous is not None:
            delay = OPEN_LIBRARY_MIN_REQUEST_INTERVAL_SECONDS - (now - previous)
            if delay > 0:
                await _openlibrary_rate_sleep(delay)
        state["last_started"] = _openlibrary_rate_clock(loop)


async def _openlibrary_request(
    client: httpx.AsyncClient,
    url: str,
    **kwargs,
) -> httpx.Response:
    """Send a request through the shared Open Library rate budget."""

    await wait_for_openlibrary_request_slot()
    return await client.get(url, **kwargs)


def _bounded_source_payload(value: object) -> dict[str, object]:
    """Return a compact JSON-safe projection of a public provider record."""

    if not isinstance(value, dict):
        return {}
    node_budget = [128]

    def sanitize(item: object, depth: int = 0) -> object:
        if node_budget[0] <= 0:
            return "[omitted]"
        node_budget[0] -= 1
        if depth >= 5:
            return "[depth limited]"
        if isinstance(item, dict):
            return {
                str(key)[:48]: sanitize(child, depth + 1)
                for key, child in list(item.items())[:12]
            }
        if isinstance(item, (list, tuple)):
            return [sanitize(child, depth + 1) for child in list(item)[:12]]
        if isinstance(item, set):
            return [sanitize(child, depth + 1) for child in sorted(item, key=str)[:12]]
        if isinstance(item, str):
            return item[:12000]
        if item is None or isinstance(item, (bool, int, float)):
            return item
        return str(item)[:512]

    payload = sanitize(value)
    if not isinstance(payload, dict):
        return {}

    def encode() -> str:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)

    changed = payload != value
    while len(encode().encode("utf-8")) > SOURCE_PAYLOAD_LIMIT_BYTES - 32:
        string_values: list[tuple[object, object, str]] = []

        def collect(node: object) -> None:
            if isinstance(node, dict):
                for key, child in node.items():
                    if isinstance(child, str):
                        string_values.append((node, key, child))
                    else:
                        collect(child)
            elif isinstance(node, list):
                for index, child in enumerate(node):
                    if isinstance(child, str):
                        string_values.append((node, index, child))
                    else:
                        collect(child)

        collect(payload)
        if not string_values:
            encoded = encode()
            preview = encoded[: SOURCE_PAYLOAD_LIMIT_BYTES // 16]
            return {"truncated": True, "preview": preview}
        container, key, longest = max(string_values, key=lambda item: len(item[2].encode("utf-8")))
        next_length = max(0, len(longest) // 2)
        container[key] = longest[:next_length]
        changed = True
    if changed:
        payload["_truncated"] = True
    # Reserve space for the marker above; retain this final guard against
    # unusual dictionary shapes or escaping expansion.
    encoded = encode()
    if len(encoded.encode("utf-8")) > SOURCE_PAYLOAD_LIMIT_BYTES:
        return {"truncated": True, "preview": encoded[: SOURCE_PAYLOAD_LIMIT_BYTES // 16]}
    return payload


def _field_provenance(
    provider: str,
    provider_id: str,
    value: object,
    *,
    kind: str = "",
    source_field: str = "",
) -> dict[str, str]:
    normalized = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) if isinstance(value, (list, dict)) else str(value or "")
    return {
        "provider": provider,
        "provider_id": provider_id,
        "kind": kind,
        "source_field": source_field,
        "value_sha256": hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        "normalization_version": METADATA_NORMALIZATION_VERSION,
    }


def metadata_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=min(settings.source_timeout_seconds, 6.0),
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": METADATA_USER_AGENT},
    )


def is_weak_cover_url(value: object) -> bool:
    """Identify known placeholder cover URLs that should be retried."""

    try:
        parsed = httpx.URL(str(value))
    except Exception:
        return False
    return (
        parsed.host == "covers.openlibrary.org"
        and parsed.path.casefold().startswith("/b/isbn/")
    ) or parsed.host == "placehold.co"


def safe_cover_url(value: object, source_url: str = "") -> str:
    """Return a browser-safe HTTPS cover URL or an empty string.

    Image URLs are not fetched by the engine, but they are still untrusted
    content rendered by the web app.  HTTPS, no credentials, and either a
    known cover CDN or the source's own public host are required.  Query
    parameters are allowed because Google Books uses them for image variants.
    """

    try:
        parsed = httpx.URL(str(value))
        source = httpx.URL(source_url) if source_url else None
    except Exception:
        return ""
    if parsed.scheme != "https" or not parsed.host or parsed.username or parsed.password:
        return ""
    host = parsed.host.casefold().rstrip(".")
    source_host = (source.host or "").casefold().rstrip(".") if source else ""
    known = host in KNOWN_COVER_HOSTS or any(host.endswith(f".{suffix}") for suffix in KNOWN_COVER_HOSTS)
    if not known and host != source_host:
        return ""
    # Fragments are never sent to the image server and can carry misleading
    # state; remove them before serializing the URL returned to the browser.
    return str(parsed.copy_with(fragment=None))


def placeholder_cover_url(title: str, author: str) -> str:
    """Build a stable image URL for books with no provider artwork.

    This is intentionally a real image endpoint rather than a blank value, so
    cards retain their visual rhythm while a provider is unavailable.  The
    title and author are encoded as text by the placeholder service and never
    interpreted as markup or a URL by Bookward.
    """

    text = f"{title}\n{author}".strip()[:180]
    return f"{PLACEHOLDER_COVER}/0f172a/f8fafc/png?{urlencode({'text': text})}"


def cover_url_from_open_library(cover_id: object) -> str:
    try:
        identifier = int(cover_id)
    except (TypeError, ValueError):
        return ""
    if identifier <= 0:
        return ""
    return f"https://covers.openlibrary.org/b/id/{identifier}-L.jpg"


def canonical_book_source_url(title: str, author: str, source_url: object = "") -> str:
    """Return a durable public book link for recommendation cards.

    Open Library's ISBN route is only resolvable when that exact ISBN exists in
    its catalog. Upcoming-book feeds frequently publish provisional ISBNs, so
    those links become dead 404s even though the title is searchable. Reader
    list pages identify a recommendation source but not the individual book.
    Use the public search route for both cases; it remains useful if metadata
    is eventually added and never requires a network lookup during rendering.
    """

    value = str(source_url or "").strip()
    try:
        parsed = urlparse(value)
    except ValueError:
        parsed = None
    is_openlibrary = (
        parsed
        and parsed.scheme in {"http", "https"}
        and (parsed.hostname or "").casefold().rstrip(".")
        in {"openlibrary.org", "www.openlibrary.org"}
    )
    path = parsed.path.casefold() if parsed else ""
    is_list_page = path.startswith("/people/") and "/lists/" in path
    if is_openlibrary and (path.startswith("/isbn/") or is_list_page):
        params = urlencode({"title": title[:300], "author": author[:200]})
        return f"{OPEN_LIBRARY_WEB}/search?{params}"
    return value


def _google_cover_url(image_links: object) -> str:
    if not isinstance(image_links, dict):
        return ""
    # Prefer the largest variant while accepting the shape returned by older
    # Google Books records.
    for key in ("extraLarge", "large", "medium", "thumbnail", "smallThumbnail"):
        value = image_links.get(key)
        if value:
            return safe_cover_url(str(value).replace("http://", "https://"))
    return ""


async def _lookup_open_library(
    title: str, author: str, client: httpx.AsyncClient | None = None
) -> str:
    record = await _lookup_open_library_record(title, author, client=client)
    return str(record.get("cover_url", ""))


def _identity_text(value: object) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(
        "".join(char if char.isalnum() else " " for char in value).split()
    )


def _normalized_title(value: object) -> str:
    return book_catalog_title_identity_key(value)


def _title_matches(wanted: str, candidate: object) -> bool:
    """Require a normalized full-title match before using catalog metadata."""

    normalized = _normalized_title(candidate)
    return bool(wanted and normalized and wanted == normalized)


def _author_matches(wanted: str, candidates: object) -> bool:
    """Match a full author name or its first initial and surname.

    Catalogs vary in whether they include middle names and initials. A shared
    surname alone is too weak to attach descriptions or subjects to a work.
    """

    if isinstance(candidates, str):
        candidates = [candidates]
    if not isinstance(candidates, (list, tuple)):
        return False
    wanted_parts = [part.strip() for part in re.split(r"\s*(?:,|;|&|\band\b)\s*", wanted, flags=re.I) if part.strip()]
    for wanted_author in wanted_parts:
        wanted_normalized = _identity_text(wanted_author)
        wanted_tokens = wanted_normalized.split()
        if not wanted_tokens:
            continue
        for candidate in candidates:
            candidate_normalized = _identity_text(candidate)
            candidate_tokens = candidate_normalized.split()
            if not candidate_tokens:
                continue
            if book_author_identity_key(wanted_author) == book_author_identity_key(candidate):
                return True
            if wanted_normalized == candidate_normalized:
                return True
            if (
                len(wanted_tokens) >= 2
                and len(candidate_tokens) >= 2
                and wanted_tokens[-1] == candidate_tokens[-1]
                and wanted_tokens[0][0] == candidate_tokens[0][0]
                and (
                    wanted_tokens[0] == candidate_tokens[0]
                    or len(wanted_tokens[0]) == 1
                    or len(candidate_tokens[0]) == 1
                )
            ):
                return True
    return False


def _catalog_genres(value: object, limit: int = 8) -> list[str]:
    values = normalize_subjects(value, limit=limit)
    return [_clean_metadata_text(genre, 80) for genre in values]


def _author_display(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return ", ".join(str(author) for author in value if author)
    return ""


def _clean_metadata_text(value: object, limit: int = 4000) -> str:
    if isinstance(value, dict):
        value = value.get("value") or value.get("text") or ""
    if isinstance(value, list):
        value = " ".join(
            str(item.get("value") or item.get("text") or "")
            if isinstance(item, dict)
            else str(item)
            for item in value
        )
    text = BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _publication_date(value: object) -> tuple[str, str] | tuple[None, None]:
    raw = str(value or "").strip()
    if not raw:
        return None, None
    if match := re.match(r"^(\d{4})-(\d{2})-(\d{2})", raw):
        try:
            date.fromisoformat(match.group(0))
        except ValueError:
            return None, None
        return match.group(0), "day"
    if match := re.fullmatch(r"(\d{4})-(\d{2})", raw):
        try:
            date.fromisoformat(f"{match.group(1)}-{match.group(2)}-01")
        except ValueError:
            return None, None
        return f"{match.group(1)}-{match.group(2)}-01", "month"
    if match := re.fullmatch(r"(\d{4})", raw):
        return f"{match.group(1)}-01-01", "year"
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            parsed = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return parsed.date().isoformat(), "day"
    return None, None


def _openlibrary_publication_date(record: object) -> tuple[str | None, str | None, str]:
    """Return the parsed date and the exact Open Library field that supplied it."""

    if not isinstance(record, dict):
        return None, None, ""
    source_field = (
        "first_publish_date" if record.get("first_publish_date")
        else "first_publish_year" if record.get("first_publish_year")
        else ""
    )
    if not source_field:
        return None, None, ""
    release_date, date_kind = _publication_date(record.get(source_field))
    return release_date, date_kind, source_field


async def _lookup_open_library_record(
    title: str,
    author: str,
    client: httpx.AsyncClient | None = None,
    *,
    isbn: str = "",
) -> dict[str, object]:
    params = {
        ("isbn" if isbn else "title"): isbn or title[:500],
        **({} if isbn else {"author": author[:300]}),
        "limit": 5,
        "fields": "key,title,author_name,cover_i,first_publish_year,first_publish_date,first_sentence,description,subject,isbn,isbn13",
    }
    trace: dict[str, object] = {
        "provider": "openlibrary",
        "fetched_at": _utc_now(),
        "query_kind": "isbn" if isbn else "title_author",
        "status": "request_error",
    }
    try:
        if client is None:
            async with metadata_client() as owned_client:
                response = await _openlibrary_request(owned_client, OPEN_LIBRARY_SEARCH, params=params)
        else:
            response = await _openlibrary_request(client, OPEN_LIBRARY_SEARCH, params=params)
        trace["http_status"] = response.status_code
        if response.is_error:
            trace["status"] = "http_error"
            return {"_fetch_trace": trace}
        try:
            payload = response.json()
        except ValueError:
            trace["status"] = "invalid_json"
            return {"_fetch_trace": trace}
    except Exception as exc:
        trace["error_type"] = type(exc).__name__
        return {"_fetch_trace": trace}
    docs = payload.get("docs", []) if isinstance(payload, dict) else []
    if not isinstance(docs, list):
        trace["status"] = "invalid_response"
        return {"_fetch_trace": trace}
    wanted = _normalized_title(title)
    matched: list[dict[str, object]] = []
    mismatched_identifiers = 0
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        returned_isbns = [doc.get("isbn13"), doc.get("isbn")]
        if isbn and not _query_isbn_matches(isbn, returned_isbns):
            mismatched_identifiers += 1
            continue
        authors = doc.get("author_name")
        if not _title_matches(wanted, doc.get("title")) or not _author_matches(author, authors):
            continue
        release_date, date_kind, release_date_source_field = _openlibrary_publication_date(doc)
        synopsis = _clean_metadata_text(doc.get("description"))
        opening_sentence = _clean_metadata_text(doc.get("first_sentence"), 1500)
        description = synopsis or opening_sentence
        description_kind = "synopsis" if synopsis else "opening_sentence" if opening_sentence else ""
        provider_id = str(doc.get("key") or "")
        genres = _catalog_genres(doc.get("subject"), limit=METADATA_GENRE_LIMIT)
        identifiers = _isbn_provenance_values(isbn, returned_isbns)
        matched.append({
            "cover_url": cover_url_from_open_library(doc.get("cover_i")),
            "description": description,
            "description_kind": description_kind,
            "description_source_field": "description" if synopsis else "first_sentence" if opening_sentence else "",
            "release_date": release_date or "",
            "date_kind": date_kind or "",
            "release_date_source_field": release_date_source_field if release_date else "",
            "genres": genres,
            "provider": "openlibrary",
            "provider_id": provider_id,
            "catalog_title": str(doc.get("title") or ""),
            "catalog_author": _author_display(authors),
            "title_match": 1.0,
            "author_match": 1.0,
            "_fetched_at": trace["fetched_at"],
            "_source_payload": _bounded_source_payload({
                "key": provider_id,
                "title": _clean_metadata_text(doc.get("title"), 500),
                "authors": [_clean_metadata_text(value, 300) for value in (authors if isinstance(authors, list) else [authors]) if value],
                "description": synopsis,
                "opening_sentence": opening_sentence,
                "genres": genres,
                "identifiers": identifiers,
                "publication_date": _clean_metadata_text(doc.get("first_publish_date") or doc.get("first_publish_year"), 40),
                "publication_date_source_field": release_date_source_field if release_date else "",
                "cover_id": str(doc.get("cover_i") or "")[:40],
            }),
        })
    trace["matched_count"] = len(matched)
    trace["mismatched_identifier_count"] = mismatched_identifiers
    if not matched:
        trace["status"] = "no_match"
        if isbn:
            trace["identifier_evidence"] = "mismatch" if mismatched_identifiers else "no_result"
            trace["identifier_verified"] = False
        return {"_fetch_trace": trace}
    date_rank = {"day": 3, "month": 2, "year": 1}
    best = max(
        matched,
        key=lambda value: (
            value.get("description_kind") == "synopsis",
            len(str(value.get("description") or "")),
            date_rank.get(str(value.get("date_kind") or ""), 0),
            len(value.get("genres") or []),
            bool(value.get("cover_url")),
        ),
    )
    trace.update({
        "status": "matched",
        "provider_id": best.get("provider_id", ""),
        "available_fields": [
            field for field in ("description", "genres", "release_date", "cover_url")
            if best.get(field)
        ],
    })
    source_payload = best.get("_source_payload", {})
    returned_identifiers = source_payload.get("identifiers", []) if isinstance(source_payload, dict) else []
    evidence, verified = _isbn_identifier_evidence(isbn, returned_identifiers)
    trace["identifier_evidence"] = evidence
    trace["identifier_verified"] = verified
    best["_fetch_trace"] = trace
    return best


async def _lookup_google_books(
    title: str, author: str, client: httpx.AsyncClient | None = None
) -> str:
    record = await _lookup_google_books_record(title, author, client=client)
    return str(record.get("cover_url", ""))


async def _lookup_google_books_record(
    title: str,
    author: str,
    client: httpx.AsyncClient | None = None,
    *,
    isbn: str = "",
    google_books_api_key: str = "",
) -> dict[str, object]:
    params = {"q": f"isbn:{isbn}" if isbn else f"intitle:{title[:300]} inauthor:{author[:200]}", "maxResults": 5}
    if google_books_api_key:
        # Google Books accepts credentials as a query parameter. The URL is
        # intentionally excluded from traces and logs so this stays private.
        params["key"] = google_books_api_key
    trace: dict[str, object] = {
        "provider": "google_books",
        "fetched_at": _utc_now(),
        "query_kind": "isbn" if isbn else "title_author",
        "status": "request_error",
    }
    try:
        if client is None:
            async with metadata_client() as owned_client:
                response = await owned_client.get(GOOGLE_BOOKS_SEARCH, params=params)
        else:
            response = await client.get(GOOGLE_BOOKS_SEARCH, params=params)
        trace["http_status"] = response.status_code
        if response.is_error:
            trace["status"] = "rate_limited" if response.status_code == 429 else "http_error"
            return {"_fetch_trace": trace}
        try:
            payload = response.json()
        except ValueError:
            trace["status"] = "invalid_json"
            return {"_fetch_trace": trace}
    except Exception as exc:
        trace["error_type"] = type(exc).__name__
        return {"_fetch_trace": trace}
    items = payload.get("items", []) if isinstance(payload, dict) else []
    if not isinstance(items, list):
        trace["status"] = "invalid_response"
        return {"_fetch_trace": trace}
    wanted = _normalized_title(title)
    matched: list[dict[str, object]] = []
    mismatched_identifiers = 0
    for item in items:
        info = item.get("volumeInfo", {}) if isinstance(item, dict) else {}
        if not isinstance(info, dict):
            continue
        identifiers = [
            value.get("identifier")
            for value in info.get("industryIdentifiers", [])
            if isinstance(value, dict) and value.get("type") in {"ISBN_10", "ISBN_13"}
        ]
        if isbn and not _query_isbn_matches(isbn, identifiers):
            mismatched_identifiers += 1
            continue
        if not _title_matches(wanted, info.get("title")) or not _author_matches(author, info.get("authors")):
            continue
        release_date, date_kind = _publication_date(info.get("publishedDate"))
        description = _clean_metadata_text(info.get("description"))
        genres = _catalog_genres(info.get("categories"), limit=METADATA_GENRE_LIMIT)
        provider_id = str(item.get("id") or "")
        matched.append({
            "cover_url": _google_cover_url(info.get("imageLinks")),
            "description": description,
            "description_kind": "synopsis" if description else "",
            "description_source_field": "description" if description else "",
            "release_date": release_date or "",
            "date_kind": date_kind or "",
            "release_date_source_field": "publishedDate" if release_date else "",
            "genres": genres,
            "provider": "google_books",
            "provider_id": provider_id,
            "catalog_title": str(info.get("title") or ""),
            "catalog_author": _author_display(info.get("authors")),
            "title_match": 1.0,
            "author_match": 1.0,
            "_fetched_at": trace["fetched_at"],
            "_source_payload": _bounded_source_payload({
                "id": provider_id,
                "title": _clean_metadata_text(info.get("title"), 500),
                "authors": [_clean_metadata_text(value, 300) for value in (info.get("authors") if isinstance(info.get("authors"), list) else [info.get("authors")]) if value],
                "description": description,
                "genres": genres,
                "identifiers": _isbn_provenance_values(isbn, identifiers),
                "publication_date": _clean_metadata_text(info.get("publishedDate"), 40),
                "publication_date_source_field": "publishedDate" if release_date else "",
                "cover_url": _google_cover_url(info.get("imageLinks")),
            }),
        })
    trace["matched_count"] = len(matched)
    trace["mismatched_identifier_count"] = mismatched_identifiers
    if not matched:
        trace["status"] = "no_match"
        if isbn:
            trace["identifier_evidence"] = "mismatch" if mismatched_identifiers else "no_result"
            trace["identifier_verified"] = False
        return {"_fetch_trace": trace}
    date_rank = {"day": 3, "month": 2, "year": 1}
    best = max(
        matched,
        key=lambda value: (
            value.get("description_kind") == "synopsis",
            len(str(value.get("description") or "")),
            date_rank.get(str(value.get("date_kind") or ""), 0),
            len(value.get("genres") or []),
            bool(value.get("cover_url")),
        ),
    )
    trace.update({
        "status": "matched",
        "provider_id": best.get("provider_id", ""),
        "available_fields": [
            field for field in ("description", "genres", "release_date", "cover_url")
            if best.get(field)
        ],
    })
    source_payload = best.get("_source_payload", {})
    returned_identifiers = source_payload.get("identifiers", []) if isinstance(source_payload, dict) else []
    evidence, verified = _isbn_identifier_evidence(isbn, returned_identifiers)
    trace["identifier_evidence"] = evidence
    trace["identifier_verified"] = verified
    best["_fetch_trace"] = trace
    return best


def _isbn_display_values(values: object) -> list[str]:
    result: list[str] = []

    def collect(value: object) -> None:
        if isinstance(value, (list, tuple, set)):
            for item in value:
                collect(item)
            return
        raw = re.sub(r"[^0-9Xx]", "", str(value or ""))
        if len(raw) in {10, 13} and canonical_isbn(raw):
            normalized = raw.upper()
            if normalized not in result:
                result.append(normalized)

    collect(values)
    return result[:8]


def _matching_returned_isbn(query: object, returned: object) -> str:
    """Find an exact ISBN match anywhere in a provider's identifier list."""

    query13, query10 = isbn_parts(query)
    if not (query13 or query10):
        return ""

    def visit(value: object):
        if isinstance(value, (list, tuple, set)):
            for item in value:
                yield from visit(item)
            return
        raw = re.sub(r"[^0-9Xx]", "", str(value or ""))
        if len(raw) in {10, 13} and canonical_isbn(raw):
            yield raw.upper()

    for value in visit(returned):
        returned13, returned10 = isbn_parts(value)
        if (query13 and returned13 == query13) or (query10 and returned10 == query10):
            return value
    return ""


def _isbn_provenance_values(query: object, returned: object) -> list[str]:
    """Keep a bounded identifier projection with the query match promoted."""

    values = _isbn_display_values(returned)
    matching = _matching_returned_isbn(query, returned)
    if matching and matching not in values:
        values = [matching, *values[:7]]
    elif matching:
        values = [matching, *(value for value in values if value != matching)][:8]
    return values


def _query_isbn_matches(query: object, returned: object) -> bool:
    """Reject ISBN-query results whose returned valid ISBNs contradict it."""

    evidence, _ = _isbn_identifier_evidence(query, returned)
    return evidence != "mismatch"


def _isbn_identifier_evidence(query: object, returned: object) -> tuple[str, bool]:
    """Distinguish exact returned identifiers from providers that omit them."""

    query13, query10 = isbn_parts(query)
    if not (query13 or query10):
        return "not_applicable", False
    matched = _matching_returned_isbn(query, returned)
    if matched:
        return "matched", True
    returned_values = _isbn_display_values(returned)
    if not returned_values:
        # Some provider work records omit edition identifiers altogether.
        return "absent", False
    return "mismatch", False


async def resolve_book_metadata(
    title: str,
    author: str,
    supplied: object = "",
    source_url: str = "",
    description: object = "",
    release_date: object = "",
    genres: object = (),
    client: httpx.AsyncClient | None = None,
    lookup_cache: dict[tuple[str, ...], asyncio.Task[dict[str, object]]] | None = None,
    expected_provider: str = "",
    expected_provider_id: str = "",
    isbn13: object = "",
    isbn10: object = "",
    prefer_catalog_synopsis: bool = False,
    google_books_api_key: str = "",
) -> dict[str, object]:
    """Resolve summary, publication date, and cover from public book catalogs.

    Source pages frequently expose covers but omit descriptions or dates.  This
    enrichment is best-effort and only uses fixed public providers; callers can
    safely persist the returned fields without making recommendation rendering
    depend on a remote catalog being available.
    """

    supplied_url = "" if is_weak_cover_url(supplied) else safe_cover_url(supplied, source_url)
    source_isbn13, source_isbn10 = isbn_parts(isbn13, isbn10)
    source_genres = _catalog_genres(genres, limit=METADATA_GENRE_LIMIT)
    result: dict[str, object] = {
        "cover_url": supplied_url,
        "description": _clean_metadata_text(description),
        "release_date": str(release_date or ""),
        "release_date_source_field": "",
        "date_kind": "",
        "genres": [],
        "provider": "",
        "provider_id": "",
        "work_id": "",
        "catalog_title": "",
        "catalog_author": "",
        "title_match": 0.0,
        "author_match": 0.0,
        "description_provider": "",
        "description_provider_id": "",
        "description_kind": "",
        "description_candidate": {},
        "genres_provider": "",
        "genres_provider_id": "",
        "cover_provider": "",
        "cover_provider_id": "",
        "release_date_provider": "",
        "release_date_provider_id": "",
    }
    needs_description = not result["description"]
    needs_release_date = not result["release_date"]
    needs_genres = len(source_genres) < METADATA_GENRE_LIMIT
    traces: list[dict[str, object]] = []
    records: list[dict[str, object]] = []
    if (needs_description or needs_release_date or needs_genres or not supplied_url) and title and author:
        for lookup in (_lookup_open_library_record, _lookup_google_books_record):
            async def cached_lookup(query_isbn: str):
                kwargs = (
                    {"google_books_api_key": google_books_api_key}
                    if lookup is _lookup_google_books_record
                    else {}
                )
                if lookup_cache is None:
                    return await lookup(title, author, client=client, isbn=query_isbn, **kwargs)
                cache_key = (
                    lookup.__name__,
                    _normalized_title(title),
                    _normalized_title(author),
                    query_isbn,
                    "google-key" if google_books_api_key else "no-google-key",
                )
                task = lookup_cache.get(cache_key)
                if task is None:
                    task = asyncio.create_task(
                        lookup(title, author, client=client, isbn=query_isbn, **kwargs)
                    )
                    lookup_cache[cache_key] = task
                return await task

            record = await cached_lookup(source_isbn13 or source_isbn10)
            trace = record.get("_fetch_trace")
            if isinstance(trace, dict):
                traces.append(trace)
            if not record.get("provider") and (source_isbn13 or source_isbn10):
                # An ISBN miss or contradiction can still be followed by an
                # identity-verified title/author query. The ISBN result itself
                # is never accepted when its returned identifier disagrees.
                # Do not repeat a failed provider request immediately with a
                # broader query; it cannot recover from an outage in this job.
                if isinstance(trace, dict) and trace.get("status") in _CATALOG_PROVIDER_FAILURE_STATUSES:
                    continue
                record = await cached_lookup("")
                trace = record.get("_fetch_trace")
                if isinstance(trace, dict):
                    traces.append(trace)
            if (
                record.get("provider")
                and
                expected_provider
                and record.get("provider") == expected_provider
                and expected_provider_id
                and record.get("provider_id") != expected_provider_id
            ):
                continue
            if record.get("provider"):
                records.append(record)

    description_records = [record for record in records if record.get("description")]
    description_record = max(
        description_records,
        key=lambda record: (
            record.get("description_kind") == "synopsis",
            len(str(record.get("description") or "")),
        ),
        default=None,
    )
    if description_record:
        description_payload = description_record.get("_source_payload", {})
        opening_sentence = (
            str(description_payload.get("opening_sentence") or "")
            if isinstance(description_payload, dict)
            else ""
        )
        result["description_candidate"] = {
            "provider": description_record.get("provider", ""),
            "provider_id": description_record.get("provider_id", ""),
            "text": description_record.get("description", ""),
            "kind": description_record.get("description_kind", ""),
            "source_field": description_record.get("description_source_field", ""),
            "opening_sentence": opening_sentence,
            "content_quality_heuristic": (
                0.5 + 0.5 * min(1.0, len(str(description_record.get("description") or "")) / 600.0)
                if description_record.get("description_kind") == "synopsis"
                else 0.5 * min(1.0, len(str(description_record.get("description") or "")) / 600.0)
            ),
            "content_quality_note": "Heuristic from field type and normalized character length; not a calibrated probability.",
        }
        source_description_matches_opening = bool(
            result["description"]
            and opening_sentence
            and _clean_metadata_text(result["description"]) == opening_sentence
        )
        same_expected_record = bool(
            expected_provider
            and expected_provider_id
            and description_record.get("provider") == expected_provider
            and description_record.get("provider_id") == expected_provider_id
        )
        should_select = needs_description or bool(
            prefer_catalog_synopsis
            and description_record.get("description_kind") == "synopsis"
            and source_description_matches_opening
            and same_expected_record
        )
        if should_select:
            result["description"] = description_record["description"]
            result["description_provider"] = description_record.get("provider", "")
            result["description_provider_id"] = description_record.get("provider_id", "")
            result["description_kind"] = description_record.get("description_kind", "")

    if not result["cover_url"]:
        cover_record = next((record for record in records if record.get("cover_url")), None)
        if cover_record:
            result["cover_url"] = cover_record["cover_url"]
            result["cover_provider"] = cover_record.get("provider", "")
            result["cover_provider_id"] = cover_record.get("provider_id", "")

    date_rank = {"day": 3, "month": 2, "year": 1}
    date_record = max(
        (record for record in records if record.get("release_date")),
        key=lambda record: date_rank.get(str(record.get("date_kind") or ""), 0),
        default=None,
    )
    if needs_release_date and date_record:
        result["release_date"] = date_record["release_date"]
        result["date_kind"] = date_record.get("date_kind", "")
        result["release_date_source_field"] = date_record.get("release_date_source_field", "")
        result["release_date_provider"] = date_record.get("provider", "")
        result["release_date_provider_id"] = date_record.get("provider_id", "")

    remaining_genres = max(0, METADATA_GENRE_LIMIT - len(source_genres))
    catalog_genres: list[str] = []
    genre_sources: list[dict[str, str]] = []
    for record in records:
        if not remaining_genres:
            break
        additions = _catalog_genres(record.get("genres"), limit=METADATA_GENRE_LIMIT)
        prior = len(catalog_genres)
        catalog_genres = _catalog_genres(catalog_genres + additions, limit=METADATA_GENRE_LIMIT)
        if len(catalog_genres) > prior:
            provider = str(record.get("provider") or "")
            genre_sources.append({
                "provider": provider,
                "provider_id": str(record.get("provider_id") or ""),
                "kind": "subjects",
                "source_field": "subject" if provider == "openlibrary" else "categories",
                "normalization_version": METADATA_NORMALIZATION_VERSION,
            })
            remaining_genres = max(0, METADATA_GENRE_LIMIT - len(source_genres) - len(catalog_genres))
    result["genres"] = catalog_genres
    if genre_sources:
        result["genres_provider"] = genre_sources[0]["provider"]
        result["genres_provider_id"] = genre_sources[0]["provider_id"]

    primary_record = (
        description_record if result["description_provider"] else None
    ) or next(
        (record for record in records if record.get("provider") == result["genres_provider"] and record.get("provider_id") == result["genres_provider_id"]),
        None,
    ) or next(
        (record for record in records if record.get("provider") == result["cover_provider"] and record.get("provider_id") == result["cover_provider_id"]),
        None,
    ) or next(
        (record for record in records if record.get("provider") == result["release_date_provider"] and record.get("provider_id") == result["release_date_provider_id"]),
        None,
    )
    if primary_record:
        result["provider"] = primary_record.get("provider", "")
        result["provider_id"] = primary_record.get("provider_id", "")
        result["work_id"] = result["provider_id"] if result["provider"] == "openlibrary" else ""
        for field in ("catalog_title", "catalog_author", "title_match", "author_match"):
            result[field] = primary_record.get(field, result[field])
    fields: dict[str, object] = {}
    if result["description_provider"]:
        fields["description"] = _field_provenance(
            str(result["description_provider"]), str(result["description_provider_id"]),
            result["description"], kind=str(result["description_kind"]),
            source_field=str(description_record.get("description_source_field", "") if description_record else ""),
        )
    if genre_sources:
        fields["genres"] = {
            "provider": genre_sources[0]["provider"],
            "provider_id": genre_sources[0]["provider_id"],
            "sources": genre_sources,
            "kind": "subjects",
            "source_field": "subject" if genre_sources[0]["provider"] == "openlibrary" else "categories",
            "value_sha256": hashlib.sha256(json.dumps(catalog_genres, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
            "normalization_version": METADATA_NORMALIZATION_VERSION,
        }
    if result["cover_provider"]:
        fields["cover_url"] = _field_provenance(
            str(result["cover_provider"]), str(result["cover_provider_id"]), result["cover_url"],
            kind="cover", source_field="cover_i" if result["cover_provider"] == "openlibrary" else "imageLinks",
        )
    if result["release_date_provider"]:
        fields["release_date"] = _field_provenance(
            str(result["release_date_provider"]), str(result["release_date_provider_id"]), result["release_date"],
            kind=str(result["date_kind"]), source_field=str(result["release_date_source_field"] or ""),
        )
    result["metadata_provenance"] = {
        "fetched_at": _utc_now(),
        "normalization_version": METADATA_NORMALIZATION_VERSION,
        "fields": fields,
        "fetch_trace": traces,
        "source_payloads": [
            {"provider": record.get("provider", ""), "provider_id": record.get("provider_id", ""), "payload": record.get("_source_payload", {})}
            for record in records
        ],
    }
    result["cover_url"] = result["cover_url"] or placeholder_cover_url(title, author)
    return result


async def resolve_openlibrary_work_metadata(
    work_id: str,
    title: str = "",
    author: str = "",
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, object]:
    """Fetch metadata for a previously verified Open Library work ID only."""

    result: dict[str, object] = {
        "cover_url": "",
        "description": "",
        "release_date": "",
        "release_date_source_field": "",
        "date_kind": "",
        "genres": [],
        "provider": "openlibrary",
        "provider_id": work_id,
        "work_id": work_id,
        "catalog_title": "",
        "catalog_author": "",
        "title_match": 1.0,
        "author_match": 1.0,
        "description_provider": "",
        "description_provider_id": "",
        "description_kind": "",
        "genres_provider": "",
        "genres_provider_id": "",
        "cover_provider": "",
        "cover_provider_id": "",
        "release_date_provider": "",
        "release_date_provider_id": "",
    }
    if not re.fullmatch(r"/works/OL\d+W", str(work_id or "")):
        result["metadata_provenance"] = {
            "fetched_at": _utc_now(),
            "normalization_version": METADATA_NORMALIZATION_VERSION,
            "fields": {},
            "fetch_trace": [{"provider": "openlibrary", "status": "invalid_work_id"}],
            "source_payloads": [],
        }
        return result

    fetched_at = _utc_now()
    trace: dict[str, object] = {
        "provider": "openlibrary",
        "provider_id": work_id,
        "fetched_at": fetched_at,
        "query_kind": "verified_work_id",
        "status": "request_error",
    }
    payload: object = None
    try:
        url = f"{OPEN_LIBRARY_WEB}{work_id}.json"
        if client is None:
            async with metadata_client() as owned_client:
                response = await _openlibrary_request(owned_client, url)
        else:
            response = await _openlibrary_request(client, url)
        trace["http_status"] = response.status_code
        if response.is_error:
            trace["status"] = "http_error"
        else:
            try:
                payload = response.json()
            except ValueError:
                payload = None
                trace["status"] = "invalid_json"
    except Exception as exc:
        trace["error_type"] = type(exc).__name__

    fields: dict[str, object] = {}
    source_payloads: list[dict[str, object]] = []
    if isinstance(payload, dict):
        returned_id = str(payload.get("key") or "")
        trace["returned_work_id"] = returned_id
        if returned_id != work_id:
            trace["status"] = "work_id_mismatch"
        else:
            synopsis = _clean_metadata_text(payload.get("description"))
            opening_sentence = _clean_metadata_text(payload.get("first_sentence"), 1500)
            description = synopsis or opening_sentence
            description_kind = "synopsis" if synopsis else "opening_sentence" if opening_sentence else ""
            release_date, date_kind, release_date_source_field = _openlibrary_publication_date(payload)
            raw_covers = payload.get("covers") or []
            cover_url = ""
            if isinstance(raw_covers, list):
                cover_url = next((cover_url_from_open_library(value) for value in raw_covers if cover_url_from_open_library(value)), "")
            genres = _catalog_genres(payload.get("subjects"), limit=METADATA_GENRE_LIMIT)
            result.update({
                "catalog_title": _clean_metadata_text(payload.get("title"), 500),
                "catalog_author": _clean_metadata_text(author, 300),
                "description": description,
                "description_provider": "openlibrary" if description else "",
                "description_provider_id": work_id if description else "",
                "description_kind": description_kind,
                "release_date": release_date or "",
                "release_date_source_field": release_date_source_field if release_date else "",
                "date_kind": date_kind or "",
                "genres": genres,
                "genres_provider": "openlibrary" if genres else "",
                "genres_provider_id": work_id if genres else "",
                "cover_url": cover_url,
                "cover_provider": "openlibrary" if cover_url else "",
                "cover_provider_id": work_id if cover_url else "",
                "release_date_provider": "openlibrary" if release_date else "",
                "release_date_provider_id": work_id if release_date else "",
            })
            if description:
                fields["description"] = _field_provenance(
                    "openlibrary", work_id, description, kind=description_kind,
                    source_field="description" if synopsis else "first_sentence",
                )
            if genres:
                fields["genres"] = _field_provenance(
                    "openlibrary", work_id, genres, kind="subjects", source_field="subjects",
                )
            if cover_url:
                fields["cover_url"] = _field_provenance("openlibrary", work_id, cover_url, kind="cover", source_field="covers")
            if release_date:
                fields["release_date"] = _field_provenance(
                    "openlibrary", work_id, release_date, kind=date_kind or "", source_field=release_date_source_field,
                )
            trace["status"] = "matched"
            trace["available_fields"] = [field for field in ("description", "genres", "cover_url", "release_date") if result.get(field)]
            source_payloads.append({
                "provider": "openlibrary",
                "provider_id": work_id,
                "payload": _bounded_source_payload({
                    "key": returned_id,
                    "title": _clean_metadata_text(payload.get("title"), 500),
                    "description": synopsis,
                    "opening_sentence": opening_sentence,
                    "genres": genres,
                    "publication_date": _clean_metadata_text(payload.get("first_publish_date") or payload.get("first_publish_year"), 40),
                    "publication_date_source_field": release_date_source_field if release_date else "",
                    "cover_ids": [str(value)[:40] for value in raw_covers[:8]] if isinstance(raw_covers, list) else [],
                }),
            })
    result["metadata_provenance"] = {
        "fetched_at": fetched_at,
        "normalization_version": METADATA_NORMALIZATION_VERSION,
        "fields": fields,
        "fetch_trace": [trace],
        "source_payloads": source_payloads,
    }
    return result


async def resolve_cover_url(
    title: str,
    author: str,
    supplied: object = "",
    source_url: str = "",
    client: httpx.AsyncClient | None = None,
    google_books_api_key: str = "",
) -> str:
    """Resolve a useful cover URL, always returning a non-empty value."""

    supplied_url = "" if is_weak_cover_url(supplied) else safe_cover_url(supplied, source_url)
    if supplied_url:
        # Source-provided artwork is preferred.  We still normalize it to HTTPS
        # and never ask the engine to fetch it.
        return supplied_url
    cover = await _lookup_open_library(title, author, client=client)
    if cover:
        return cover
    record = await _lookup_google_books_record(
        title, author, client=client, google_books_api_key=google_books_api_key
    )
    cover = str(record.get("cover_url") or "")
    return cover or placeholder_cover_url(title, author)


def fallback_cover_url(title: str, author: str, supplied: object = "", source_url: str = "") -> str:
    """Synchronous, network-free fallback for API responses and bootstrapping."""

    supplied_url = "" if is_weak_cover_url(supplied) else safe_cover_url(supplied, source_url)
    return supplied_url or placeholder_cover_url(title, author)
