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
import re
from datetime import date, datetime
from urllib.parse import urlencode, urlparse

import httpx
from bs4 import BeautifulSoup

from .config import settings

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
        "mzstatic.com",
        "gr-assets.com",
        "placehold.co",
    }
)

METADATA_USER_AGENT = "Bookward/0.1 (+self-hosted book recommender)"


def metadata_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=min(settings.source_timeout_seconds, 6.0),
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": METADATA_USER_AGENT},
    )


def is_weak_cover_url(value: object) -> bool:
    """Identify Open Library ISBN URLs, which may be a 1x1 no-cover GIF."""

    try:
        parsed = httpx.URL(str(value))
    except Exception:
        return False
    return parsed.host == "covers.openlibrary.org" and parsed.path.casefold().startswith("/b/isbn/")


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
    those links become dead 404s even though the title is searchable. Use the
    public search route for those cases; it remains useful when metadata is
    eventually added and never requires a network lookup during rendering.
    """

    value = str(source_url or "").strip()
    try:
        parsed = urlparse(value)
    except ValueError:
        parsed = None
    if parsed and parsed.scheme in {"http", "https"} and (parsed.hostname or "").casefold().rstrip(".") in {"openlibrary.org", "www.openlibrary.org"} and parsed.path.casefold().startswith("/isbn/"):
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


def _normalized_title(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _title_matches(wanted: str, candidate: object) -> bool:
    normalized = _normalized_title(candidate)
    return bool(wanted and normalized and (wanted in normalized or normalized in wanted))


def _clean_metadata_text(value: object, limit: int = 4000) -> str:
    if isinstance(value, dict):
        value = value.get("value") or value.get("text") or ""
    if isinstance(value, list):
        value = " ".join(str(item) for item in value)
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


async def _lookup_open_library_record(
    title: str,
    author: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, str]:
    params = {
        "title": title[:500],
        "author": author[:300],
        "limit": 5,
        "fields": "title,author_name,cover_i,first_publish_year,first_publish_date,first_sentence",
    }
    try:
        if client is None:
            async with metadata_client() as owned_client:
                response = await owned_client.get(OPEN_LIBRARY_SEARCH, params=params)
        else:
            response = await client.get(OPEN_LIBRARY_SEARCH, params=params)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return {}
    docs = payload.get("docs", []) if isinstance(payload, dict) else []
    if not isinstance(docs, list):
        return {}
    wanted = _normalized_title(title)
    for doc in docs:
        if not isinstance(doc, dict) or not _title_matches(wanted, doc.get("title")):
            continue
        release_date, date_kind = _publication_date(
            doc.get("first_publish_date") or doc.get("first_publish_year")
        )
        return {
            "cover_url": cover_url_from_open_library(doc.get("cover_i")),
            "description": _clean_metadata_text(
                doc.get("first_sentence") or doc.get("description")
            ),
            "release_date": release_date or "",
            "date_kind": date_kind or "",
        }
    return {}


async def _lookup_google_books(
    title: str, author: str, client: httpx.AsyncClient | None = None
) -> str:
    record = await _lookup_google_books_record(title, author, client=client)
    return str(record.get("cover_url", ""))


async def _lookup_google_books_record(
    title: str,
    author: str,
    client: httpx.AsyncClient | None = None,
) -> dict[str, str]:
    params = {"q": f"intitle:{title[:300]} inauthor:{author[:200]}", "maxResults": 5}
    try:
        if client is None:
            async with metadata_client() as owned_client:
                response = await owned_client.get(GOOGLE_BOOKS_SEARCH, params=params)
        else:
            response = await client.get(GOOGLE_BOOKS_SEARCH, params=params)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return {}
    items = payload.get("items", []) if isinstance(payload, dict) else []
    if not isinstance(items, list):
        return {}
    wanted = _normalized_title(title)
    for item in items:
        info = item.get("volumeInfo", {}) if isinstance(item, dict) else {}
        if not isinstance(info, dict):
            continue
        if not _title_matches(wanted, info.get("title")):
            continue
        release_date, date_kind = _publication_date(info.get("publishedDate"))
        return {
            "cover_url": _google_cover_url(info.get("imageLinks")),
            "description": _clean_metadata_text(info.get("description")),
            "release_date": release_date or "",
            "date_kind": date_kind or "",
        }
    return {}


async def resolve_book_metadata(
    title: str,
    author: str,
    supplied: object = "",
    source_url: str = "",
    description: object = "",
    release_date: object = "",
    client: httpx.AsyncClient | None = None,
    lookup_cache: dict[tuple[str, str], asyncio.Task[dict[str, str]]] | None = None,
) -> dict[str, str]:
    """Resolve summary, publication date, and cover from public book catalogs.

    Source pages frequently expose covers but omit descriptions or dates.  This
    enrichment is best-effort and only uses fixed public providers; callers can
    safely persist the returned fields without making recommendation rendering
    depend on a remote catalog being available.
    """

    supplied_url = "" if is_weak_cover_url(supplied) else safe_cover_url(supplied, source_url)
    result = {
        "cover_url": supplied_url,
        "description": _clean_metadata_text(description),
        "release_date": str(release_date or ""),
        "date_kind": "",
    }
    needs_description = not result["description"]
    needs_release_date = not result["release_date"]
    if needs_description or needs_release_date or not supplied_url:
        for lookup in (_lookup_open_library_record, _lookup_google_books_record):
            if lookup_cache is None:
                record = await lookup(title, author, client=client)
            else:
                cache_key = (lookup.__name__, f"{_normalized_title(title)}|{_normalized_title(author)}")
                task = lookup_cache.get(cache_key)
                if task is None:
                    task = asyncio.create_task(lookup(title, author, client=client))
                    lookup_cache[cache_key] = task
                record = await task
            if not result["cover_url"] and record.get("cover_url"):
                result["cover_url"] = record["cover_url"]
            if needs_description and record.get("description"):
                result["description"] = record["description"]
                needs_description = False
            if needs_release_date and record.get("release_date"):
                result["release_date"] = record["release_date"]
                result["date_kind"] = record.get("date_kind", "")
                needs_release_date = False
            if not needs_description and not needs_release_date and result["cover_url"]:
                break
    result["cover_url"] = result["cover_url"] or placeholder_cover_url(title, author)
    return result


async def resolve_cover_url(
    title: str,
    author: str,
    supplied: object = "",
    source_url: str = "",
    client: httpx.AsyncClient | None = None,
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
    cover = await _lookup_google_books(title, author, client=client)
    return cover or placeholder_cover_url(title, author)


def fallback_cover_url(title: str, author: str, supplied: object = "", source_url: str = "") -> str:
    """Synchronous, network-free fallback for API responses and bootstrapping."""

    supplied_url = "" if is_weak_cover_url(supplied) else safe_cover_url(supplied, source_url)
    return supplied_url or placeholder_cover_url(title, author)
