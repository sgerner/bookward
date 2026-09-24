import asyncio
import csv
import html
import io
import json
import re
import math
import unicodedata
from datetime import date, datetime, timezone
from urllib.parse import urljoin, urlparse
import feedparser
import httpx
from bs4 import BeautifulSoup
from .config import settings
from .covers import is_weak_cover_url, metadata_client, resolve_book_metadata, resolve_cover_url, safe_cover_url
from .database import normalize_key, rows, transaction
from .isbn import isbn_parts_from_source
from .security import resolve_public_target

GOODREADS_TIP_RE = re.compile(
    r'''new\s+Tip\(\$\('(?P<id>bookCover[^']+)'\),\s*"(?P<body>(?:\\.|[^"\\])*)"\s*,''',
    re.S,
)
GOODREADS_TITLE_RE = re.compile(r'class=\\"readable bookTitle\\"[^>]*>(.*?)<\\/a>', re.S)
GOODREADS_AUTHOR_RE = re.compile(r'class=\\"authorName\\"[^>]*>(.*?)<\\/a>', re.S)
GOODREADS_LINK_RE = re.compile(r'href=\\"(https://www\.goodreads\.com/book/show/[^"?]+)', re.S)
GOODREADS_YEAR_RE = re.compile(r'(?:published|release date:)\s+(\d{4})', re.I)
GOODREADS_BOOK_ID_RE = re.compile(r"/book/show/(\d+)", re.I)
GOODREADS_TOOLTIPS_URL = "https://www.goodreads.com/tooltips"
GOODREADS_BLOG_HOSTS = frozenset({"goodreads.com", "www.goodreads.com"})
EDITORIAL_SOURCE_HOSTS = frozenset(
    {"andrewliptak.com", "www.andrewliptak.com", "transfer-orbit.ghost.io"}
)
SOURCE_MAX_REDIRECTS = 4
SOURCE_FILTER_MAX_GENRES = 12
SOURCE_FILTER_GENRE_MAX_LENGTH = 80


def _filter_genre_values(value):
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            value = value.split(",")
        else:
            value = decoded if isinstance(decoded, (list, tuple)) else [value]
    if not isinstance(value, (list, tuple)):
        return []
    values = []
    seen = set()
    for raw in value:
        genre = re.sub(r"\s+", " ", str(raw or "")).strip()
        if not genre:
            continue
        genre = genre[:SOURCE_FILTER_GENRE_MAX_LENGTH]
        key = genre.casefold()
        if key in seen:
            continue
        seen.add(key)
        values.append(genre)
        if len(values) >= SOURCE_FILTER_MAX_GENRES:
            break
    return values


def _stored_genre_values(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
        if not isinstance(value, (list, tuple)):
            return []
    return _filter_genre_values(value)[:8]


def normalize_source_filters(value):
    """Return the small, forward-compatible filter shape stored per source."""

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = {}
    if not isinstance(value, dict):
        value = {}
    return {
        "include_genres": _filter_genre_values(value.get("include_genres")),
        "exclude_genres": _filter_genre_values(value.get("exclude_genres")),
    }


def _genre_matches(wanted, available):
    def words(value):
        value = unicodedata.normalize("NFKC", str(value or "")).casefold()
        return re.findall(r"[\w]+", value)

    wanted_words, available_words = words(wanted), words(available)
    if not wanted_words or not available_words:
        return False

    def contains_phrase(phrase, text):
        return any(text[index:index + len(phrase)] == phrase for index in range(len(text) - len(phrase) + 1))

    return contains_phrase(wanted_words, available_words)


def filter_source_items(items, filters):
    """Apply optional genre filters without losing untagged source records.

    An explicit include filter requires a matching genre. Exclude-only filters
    keep untagged records because there is no evidence that they match an
    excluded genre.
    """

    configured = normalize_source_filters(filters)
    include = configured["include_genres"]
    exclude = configured["exclude_genres"]
    if not include and not exclude:
        return list(items)
    filtered = []
    for item in items:
        genres = item.get("genres") or []
        if isinstance(genres, str):
            try:
                genres = json.loads(genres)
            except (TypeError, ValueError):
                genres = [genres]
        genres = [str(genre) for genre in genres if str(genre).strip()]
        if include and not genres:
            continue
        if genres:
            if include and not any(_genre_matches(wanted, genre) for wanted in include for genre in genres):
                continue
            if exclude and any(_genre_matches(blocked, genre) for blocked in exclude for genre in genres):
                continue
        filtered.append(item)
    return filtered


def _clean_text(value, limit=4000):
    text = BeautifulSoup(html.unescape(str(value or "")), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _clean_editorial_title(value):
    # Ghost sometimes splits an italicized word across an inline text node
    # (for example ``T`` + ``he Tower``), which BeautifulSoup renders as
    # ``T he Tower``. Rejoin only a single letter followed by lowercase text.
    return re.sub(r"\b([A-Za-z])\s+([a-z])", r"\1\2", _clean_text(value, 500))


def _date_value(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    if re.fullmatch(r"\d{4}", raw):
        try:
            return date(int(raw), 1, 1).isoformat()
        except ValueError:
            return None
    if re.fullmatch(r"\d{4}-\d{2}", raw):
        try:
            year, month = (int(part) for part in raw.split("-"))
            return date(year, month, 1).isoformat()
        except ValueError:
            return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            return date.fromisoformat(raw).isoformat()
        except ValueError:
            return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S%z", "%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _apple_value(entry, *keys):
    for key in keys:
        value = entry.get(key) if isinstance(entry, dict) else entry.get(key)
        if isinstance(value, dict):
            value = value.get("label") or value.get("attributes", {}).get("label")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _apple_link(entry):
    value = entry.get("link") if isinstance(entry, dict) else entry.get("link")
    if isinstance(value, str):
        return metadata_url(value)
    if isinstance(value, dict):
        return metadata_url(value.get("attributes", {}).get("href"))
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            href = item.get("attributes", {}).get("href")
            if href and item.get("attributes", {}).get("type") == "text/html":
                return metadata_url(href)
        for item in value:
            if isinstance(item, dict):
                href = item.get("attributes", {}).get("href")
                if href:
                    return metadata_url(href)
    return ""


def _apple_cover(entry, summary, source_url):
    value = entry.get("im:image") if isinstance(entry, dict) else None
    if isinstance(value, list):
        values = [item.get("label") for item in value if isinstance(item, dict) and item.get("label")]
        if values:
            return safe_cover_url(values[-1], source_url)
    soup = BeautifulSoup(str(summary or ""), "html.parser")
    images = [image.get("src") for image in soup.select("img[src]")]
    return safe_cover_url(images[0] if images else "", source_url)


def _with_source_isbn(item, values=()):
    """Attach only checksum-valid source ISBNs to a parsed item."""

    isbn13, isbn10 = isbn_parts_from_source(values, item.get("source_url", ""))
    if not isbn13 and not isbn10:
        return item
    enriched = dict(item)
    if isbn13:
        enriched["isbn13"] = isbn13
    if isbn10:
        enriched["isbn10"] = isbn10
    return enriched


def _parse_apple_entries(entries, source_url):
    items = []
    for entry in entries[: settings.source_max_items]:
        summary = _apple_value(entry, "summary")
        title = _apple_value(entry, "im:name", "im_name")
        author = _apple_value(entry, "im:artist", "im_artist")
        if not title:
            raw_title = _apple_value(entry, "title")
            title, _, inferred_author = raw_title.rpartition(" - ")
            title, author = title or raw_title, author or inferred_author
        if not author:
            raw_title = _apple_value(entry, "title")
            _, _, author = raw_title.rpartition(" - ")
        link = _apple_link(entry) or source_url
        release = _apple_value(entry, "im:releaseDate", "im_releasedate")
        if isinstance(entry, dict) and isinstance(entry.get("im:releaseDate"), dict):
            release = entry["im:releaseDate"].get("label") or entry["im:releaseDate"].get("attributes", {}).get("label") or release
        tags = entry.get("category") if isinstance(entry, dict) else None
        genres = []
        if isinstance(tags, dict):
            term = tags.get("attributes", {}).get("term")
            if term:
                genres.append(str(term))
        if not genres and not isinstance(entry, dict):
            genres = [str(tag.get("term")) for tag in entry.get("tags", []) if tag.get("term")]
        if not genres:
            genre_match = re.search(
                r"Genre:\s*(.*?)(?:\s+Price:|\s+Publish Date:|$)",
                _clean_text(summary),
                re.I,
            )
            if genre_match:
                genres = [genre_match.group(1).strip()]
        if title and author:
            items.append(
                _with_source_isbn(
                    {
                        "title": title[:500],
                        "author": author[:300],
                        "description": _clean_text(summary),
                        "cover_url": _apple_cover(entry, summary, source_url),
                        "source_url": link,
                        "release_date": _date_value(release),
                        "genres": genres[:8],
                    },
                    [entry.get(key) for key in ("isbn13", "isbn10", "isbn")]
                    if isinstance(entry, dict)
                    else (),
                )
            )
    return items


def _parse_open_library(payload, source_url):
    works = payload.get("works", []) if isinstance(payload, dict) else []
    if not isinstance(works, list):
        works = []
    items = []
    seen = set()
    for work in works[: settings.source_max_items]:
        if not isinstance(work, dict):
            continue
        title = str(work.get("title", "")).strip()
        authors = work.get("authors") or []
        names = [str(author.get("name", "")).strip() for author in authors if isinstance(author, dict) and author.get("name")]
        author = names[0] if names else "Unknown author"
        key = normalize_key(title, author)
        if not title or key in seen:
            continue
        seen.add(key)
        description = work.get("description", "")
        if isinstance(description, dict):
            description = description.get("value", "")
        cover_id = work.get("cover_id") or work.get("cover_i")
        work_key = str(work.get("key", ""))
        source = metadata_url(f"https://openlibrary.org{work_key}" if work_key.startswith("/") else work_key, source_url)
        items.append(
            _with_source_isbn(
                {
                    "title": title[:500],
                    "author": author[:300],
                    "description": _clean_text(description),
                    "cover_url": safe_cover_url(f"https://covers.openlibrary.org/b/id/{int(cover_id)}-L.jpg", source_url) if str(cover_id).isdigit() else "",
                    "source_url": source,
                    "release_date": None,
                    "genres": [str(subject)[:80] for subject in (work.get("subject") or [])[:8] if subject],
                },
                [work.get(key) for key in ("isbn13", "isbn10", "isbn")],
            )
        )
    return items


def _parse_nytimes(payload, source_url):
    results = payload.get("results", {}) if isinstance(payload, dict) else {}
    lists = results.get("lists", []) if isinstance(results, dict) else []
    books = [book for listing in lists if isinstance(listing, dict) for book in listing.get("books", []) if isinstance(book, dict)]
    if isinstance(results, dict) and isinstance(results.get("books"), list):
        books.extend(book for book in results["books"] if isinstance(book, dict))
    items = []
    for book in books[: settings.source_max_items]:
        title = str(book.get("title", "")).strip()
        author = str(book.get("author", "Unknown author")).strip()
        if not title:
            continue
        source = metadata_url(book.get("amazon_product_url"), source_url)
        items.append(
            _with_source_isbn(
                {
                    "title": title[:500],
                    "author": author[:300],
                    "description": _clean_text(book.get("description", "")),
                    "cover_url": safe_cover_url(book.get("book_image", ""), source_url),
                    "source_url": source,
                    "release_date": _date_value(book.get("published_date")),
                    "genres": [str(book.get("list_name", "")).strip()] if book.get("list_name") else [],
                },
                [
                    book.get("primary_isbn13"),
                    book.get("primary_isbn10"),
                    book.get("isbn13"),
                    book.get("isbn10"),
                    book.get("isbn"),
                ],
            )
        )
    return items


def _decode_goodreads_fragment(value):
    return html.unescape(value.replace(r"\/", "/").replace(r'\"', '"').replace(r"\'", "'").replace(r"\n", "\n"))


def _source_matches(source_url, host, path_prefix=None):
    """Match a provider using parsed URL components, not raw URL text."""

    try:
        parsed = urlparse(source_url)
    except ValueError:
        return False
    parsed_host = (parsed.hostname or "").casefold().rstrip(".")
    if parsed_host != host:
        return False
    return path_prefix is None or parsed.path.startswith(path_prefix)


def _source_host(source_url):
    try:
        return (urlparse(source_url).hostname or "").casefold().rstrip(".")
    except ValueError:
        return ""


def _is_apple_source(source_url):
    """Recognize both legacy iTunes and current Apple RSS endpoints."""

    return any(
        _source_matches(source_url, host)
        for host in ("itunes.apple.com", "rss.marketingtools.apple.com")
    )


def _is_goodreads_blog_source(source_url):
    return _source_host(source_url) in GOODREADS_BLOG_HOSTS and urlparse(source_url).path.startswith(
        "/blog/show/"
    )


def _is_editorial_source(source_url):
    return _source_host(source_url) in EDITORIAL_SOURCE_HOSTS


def _parse_goodreads_genre(content, source_url):
    soup = BeautifulSoup(content, "html.parser")
    covers = {}
    for wrapper in soup.select("div.coverWrapper[id]"):
        image = wrapper.select_one("img.bookImage, img.bookCover")
        covers[wrapper.get("id")] = safe_cover_url(image.get("src") if image else "", source_url)
    items = []
    seen = set()
    for script in soup.find_all("script"):
        script_text = script.get_text() or ""
        for match in GOODREADS_TIP_RE.finditer(script_text):
            body = match.group("body")
            title_match = GOODREADS_TITLE_RE.search(body)
            author_match = GOODREADS_AUTHOR_RE.search(body)
            if not title_match or not author_match:
                continue
            title = _clean_text(_decode_goodreads_fragment(title_match.group(1)), 500)
            author = _clean_text(_decode_goodreads_fragment(author_match.group(1)), 300)
            key = normalize_key(title, author)
            if not title or not author or key in seen:
                continue
            seen.add(key)
            decoded = _decode_goodreads_fragment(body)
            description_match = re.search(r'<div class="addBookTipDescription">(.*?)</div>', decoded, re.S)
            description = description_match.group(1) if description_match else ""
            description_soup = BeautifulSoup(description, "html.parser")
            visible_description = description_soup.select_one("span[id^='freeTextContainer']")
            link_match = GOODREADS_LINK_RE.search(body)
            year_match = GOODREADS_YEAR_RE.search(decoded)
            slug = urlparse(source_url).path.rstrip("/").rsplit("/", 1)[-1]
            items.append({
                "title": title,
                "author": author,
                "description": _clean_text(visible_description or description),
                "cover_url": covers.get(match.group("id"), ""),
                "source_url": metadata_url(link_match.group(1) if link_match else "", source_url),
                "release_date": _date_value(year_match.group(1)) if year_match else None,
                "genres": [slug.replace("-", " ").title()] if slug else [],
            })
            if len(items) >= settings.source_max_items:
                return items
    return items


def _parse_goodreads_blog(content, source_url):
    """Parse Goodreads editorial pages without relying on legacy genre markup.

    Blog pages render each recommendation as a tooltip trigger.  The card has
    a stable Goodreads book id and cover, while the author and description are
    supplied by the batched ``/tooltips`` endpoint and filled in by the async
    enrichment step below.
    """

    soup = BeautifulSoup(content, "html.parser")
    items = []
    seen = set()
    for card in soup.select(".tooltipTrigger.book[data-resource-id]"):
        resource_id = str(card.get("data-resource-id") or "").strip()
        if not resource_id or resource_id in seen:
            continue
        seen.add(resource_id)
        image = card.select_one("img[src]")
        title = _clean_text(image.get("alt") if image else "", 500)
        link = card.select_one("a[href*='/book/show/']")
        href = urljoin("https://www.goodreads.com", link.get("href", "")) if link else ""
        if not title and link:
            title = _clean_text(link.get_text(" ", strip=True), 500)
        if not title or not href:
            continue
        items.append(
            {
                "title": title,
                "author": "Unknown author",
                "description": "",
                "cover_url": safe_cover_url(image.get("src") if image else "", source_url),
                "source_url": metadata_url(href, source_url),
                "release_date": None,
                "genres": [],
            }
        )
        if len(items) >= settings.source_max_items:
            break
    return items


def _editorial_book_link(href):
    try:
        parsed = urlparse(href)
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold().rstrip(".")
    return parsed.scheme in {"http", "https"} and host not in EDITORIAL_SOURCE_HOSTS


def _editorial_date(value, source_url):
    match = re.search(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+(\d{1,2})(?:st|nd|rd|th)?\b",
        str(value or ""),
        re.I,
    )
    if not match:
        return None
    year_match = re.search(r"\b(20\d{2})\b", source_url)
    year = int(year_match.group(1)) if year_match else datetime.now().year
    try:
        return datetime.strptime(
            f"{match.group(1)} {int(match.group(2))} {year}", "%B %d %Y"
        ).date().isoformat()
    except ValueError:
        return None


def _text_before(node, child):
    parts = []
    for part in node.contents:
        if part is child:
            break
        parts.append(part.get_text(" ", strip=True) if hasattr(part, "get_text") else str(part))
    return _clean_text(" ".join(parts), 500)


def _parse_editorial_html(content, source_url):
    """Parse Transfer Orbit/Andrew Liptak Ghost book-list headings."""

    soup = BeautifulSoup(content, "html.parser")
    items = []
    seen = set()
    for heading in soup.select("h2, h3, h4"):
        raw_links = [
            link
            for link in heading.select("a[href]")
            if _editorial_book_link(link.get("href", ""))
        ]
        if not raw_links:
            continue
        heading_text = _clean_text(heading.get_text(" ", strip=True), 1200)
        marker = re.search(r"\s+(?:edited\s+by|by)\s+", heading_text, re.I)
        if not marker:
            continue
        attribution_text = heading_text[marker.end() :]
        date_match = re.search(r"\s+\((?P<date>[^()]*)\)\s*$", attribution_text)
        author_text = attribution_text[: date_match.start()] if date_match else attribution_text
        author = _clean_text(author_text, 300)
        # Keep the primary author usable for metadata lookup while retaining
        # ordinary multi-author and editor attributions.
        author = re.split(r"(?:,|\s+and)\s+translated\s+by\s+", author, maxsplit=1, flags=re.I)[0].strip()
        date_value = _editorial_date(date_match.group("date") if date_match else "", source_url)
        grouped_links = []
        for link in raw_links:
            href = link.get("href", "")
            title_text = link.get_text(" ", strip=True)
            if grouped_links and grouped_links[-1][0] == href:
                grouped_links[-1][1] = f"{grouped_links[-1][1]} {title_text}"
            else:
                grouped_links.append([href, title_text])
        prefix = _text_before(heading, raw_links[0]) if len(grouped_links) > 1 else ""
        for href, title_text in grouped_links:
            title = _clean_editorial_title(title_text)
            if prefix:
                title = _clean_editorial_title(f"{prefix} {title}")
            if not title or not author:
                continue
            key = normalize_key(title, author)
            if key in seen:
                continue
            seen.add(key)
            book_url = metadata_url(urljoin(source_url, href), source_url)
            items.append(
                {
                    "title": title,
                    "author": author,
                    "description": "",
                    "cover_url": "",
                    "source_url": book_url,
                    "release_date": date_value,
                    "genres": [],
                }
            )
            if len(items) >= settings.source_max_items:
                return items
    return items

def metadata_url(value, fallback=""):
    for candidate in (value, fallback):
        try:
            parsed = httpx.URL(str(candidate))
        except Exception:
            continue
        if parsed.scheme in ("http", "https") and parsed.host and not parsed.username and not parsed.password:
            return str(parsed)
    return ""

def import_goodreads_csv(content: bytes):
    if len(content) > 10_000_000: raise ValueError("CSV is larger than 10 MB")
    text = content.decode("utf-8-sig", errors="strict")
    reader = csv.DictReader(io.StringIO(text))
    required = {"Title", "Author"}
    if not reader.fieldnames or not required.issubset(reader.fieldnames): raise ValueError("This does not look like a Goodreads export")
    count = 0
    with transaction() as con:
        for row in reader:
            if count >= 20_000: raise ValueError("CSV exceeds the 20,000-book limit")
            title, author = row.get("Title", "").strip()[:500], row.get("Author", "").strip()[:300]
            if not title or not author: continue
            if row.get("Exclusive Shelf") and row.get("Exclusive Shelf", "").strip().lower() != "read": continue
            rating = float(row.get("My Rating") or 0) or None
            if rating is not None and (not math.isfinite(rating) or not 0 <= rating <= 5): raise ValueError("Ratings must be between 0 and 5")
            con.execute("INSERT INTO reads(title,author,rating,read_at,isbn,source) VALUES(?,?,?,?,?,'goodreads_csv') ON CONFLICT(title,author) DO UPDATE SET rating=excluded.rating,read_at=excluded.read_at,isbn=excluded.isbn", (title, author, rating, row.get("Date Read") or None, (row.get("ISBN13") or row.get("ISBN") or "").strip('="') or None))
            count += 1
    # Importing a history is also the point at which naturally supplied
    # ratings can become outcomes for recommendations shown earlier. Import
    # lazily to keep the ingestion module independent of the telemetry module
    # during application startup.
    from .learning import attribute_read_outcomes
    attribute_read_outcomes()
    return count

def _source_client():
    return httpx.AsyncClient(
        timeout=settings.source_timeout_seconds,
        follow_redirects=False,
        trust_env=False,
        headers={
            "User-Agent": "Bookward/0.1 (+self-hosted book recommender)",
            "Accept-Encoding": "identity",
        },
    )


async def _request_pinned(client, method, url, *, params=None, allow_http=False):
    _, address, hostname = resolve_public_target(url, allow_http=allow_http)
    original = httpx.URL(url)
    pinned = original.copy_with(host=address)
    default_port = 443 if original.scheme == "https" else 80
    host_header = hostname if original.port in (None, default_port) else f"{hostname}:{original.port}"
    return await client.request(
        method,
        pinned,
        params=params,
        headers={"Host": host_header},
        extensions={"sni_hostname": hostname},
    )


async def _fetch_bytes_with_client(client, url: str, allow_goodreads_http=False):
    current_url = str(url)
    for _ in range(SOURCE_MAX_REDIRECTS + 1):
        response = await _request_pinned(
            client,
            "GET",
            current_url,
            allow_http=allow_goodreads_http,
        )
        if response.is_redirect:
            location = response.headers.get("location")
            if not location:
                raise ValueError("Source returned an invalid redirect")
            current_url = str(httpx.URL(current_url).join(location))
            continue
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").lower()
        if not any(
            kind in content_type
            for kind in ("html", "xml", "rss", "atom", "json", "javascript", "text/plain")
        ):
            raise ValueError("Source returned an unsupported content type")
        chunks, size = [], 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > settings.source_max_bytes:
                raise ValueError("Source response is too large")
            chunks.append(chunk)
        return b"".join(chunks), content_type
    raise ValueError("Source returned too many redirects")


async def fetch_bytes(url: str, allow_goodreads_http=False, client=None):
    if client is not None:
        return await _fetch_bytes_with_client(client, url, allow_goodreads_http)
    async with _source_client() as owned_client:
        return await _fetch_bytes_with_client(owned_client, url, allow_goodreads_http)


def _goodreads_tooltip_params(resource_ids):
    params = []
    for resource_id in resource_ids:
        key = f"Book.{resource_id}"
        params.extend(
            [
                (f"resources[{key}][type]", "Book"),
                (f"resources[{key}][id]", resource_id),
            ]
        )
    return params


async def _goodreads_tooltips(client, resource_ids):
    if not resource_ids:
        return {}

    async def request_batches():
        collected = {}
        for start in range(0, len(resource_ids), 50):
            response = await _request_pinned(
                client,
                "GET",
                GOODREADS_TOOLTIPS_URL,
                params=_goodreads_tooltip_params(resource_ids[start : start + 50]),
            )
            response.raise_for_status()
            payload = response.json()
            batch = payload.get("tooltips", {}) if isinstance(payload, dict) else {}
            if isinstance(batch, dict):
                collected.update(batch)
        return collected

    try:
        return await request_batches()
    except httpx.HTTPError:
        # Goodreads normally accepts the session established by the source
        # page. If an edge node requires a fresh session, seed it once and
        # retry the same bounded batches; the fixed public host is pinned and
        # revalidated on every request.
        home = await _request_pinned(client, "GET", "https://www.goodreads.com/")
        home.raise_for_status()
        return await request_batches()


async def enrich_goodreads_blog_items(items, client):
    resource_ids = []
    for item in items:
        match = GOODREADS_BOOK_ID_RE.search(item.get("source_url", ""))
        if match and match.group(1) not in resource_ids:
            resource_ids.append(match.group(1))
    try:
        tooltips = await _goodreads_tooltips(client, resource_ids)
    except Exception:
        # The page itself remains a valid source if Goodreads temporarily
        # blocks or changes the tooltip endpoint; retain card-level metadata.
        return items
    enriched = []
    for item in items:
        match = GOODREADS_BOOK_ID_RE.search(item.get("source_url", ""))
        tooltip_html = tooltips.get(f"Book.{match.group(1)}") if match else ""
        if not tooltip_html:
            enriched.append(item)
            continue
        soup = BeautifulSoup(tooltip_html, "html.parser")
        title_node = soup.select_one("h2 a, a.readable")
        authors = [
            _clean_text(author.get_text(" ", strip=True), 300)
            for author in soup.select("a.authorName")
            if author.get_text(strip=True)
        ]
        description_node = soup.select_one("span[id^='freeTextContainer']")
        published = soup.select_one(".bookRatingAndPublishing")
        published_text = published.get_text(" ", strip=True) if published else ""
        year_match = GOODREADS_YEAR_RE.search(published_text)
        book_link = title_node.get("href") if title_node else ""
        updated = {
            **item,
            "title": _clean_text(title_node.get_text(" ", strip=True), 500)
            if title_node
            else item["title"],
            "author": ", ".join(authors)[:300] if authors else item["author"],
            "description": _clean_text(description_node or ""),
            "source_url": metadata_url(
                urljoin("https://www.goodreads.com", book_link), item.get("source_url", "")
            ),
            "release_date": f"{year_match.group(1)}-01-01" if year_match else None,
            "date_kind": "year" if year_match else item.get("date_kind", "source"),
        }
        enriched.append(updated)
    return enriched


async def _fetch_and_parse_source(url):
    if _is_goodreads_blog_source(url):
        async with _source_client() as client:
            content, content_type = await fetch_bytes(url, client=client)
            items = parse_book_items(content, content_type, url)
            return content_type, await enrich_goodreads_blog_items(items, client)
    content, content_type = await fetch_bytes(url)
    return content_type, parse_book_items(content, content_type, url)

async def import_goodreads_rss(url: str):
    parsed = httpx.URL(url); host = parsed.host or ""
    if not (host == "goodreads.com" or host.endswith(".goodreads.com")) or not parsed.path.startswith("/review/list_rss/"): raise ValueError("Use a Goodreads read-shelf RSS URL")
    shelf = parsed.params.get("shelf")
    if shelf and shelf.casefold() != "read": raise ValueError("Use the Goodreads read shelf")
    content, _ = await fetch_bytes(url, allow_goodreads_http=True)
    feed = feedparser.parse(content)
    count = 0
    with transaction() as con:
        for entry in feed.entries[:500]:
            title = str(entry.get("title", "")).strip()[:500]
            author = str(entry.get("author_name") or entry.get("book_author_name") or "Unknown author").strip()[:300]
            if not title: continue
            raw_rating = entry.get("user_rating")
            try: rating = float(raw_rating) if raw_rating else None
            except ValueError: rating = None
            if rating is not None and (not math.isfinite(rating) or not 0 <= rating <= 5): rating = None
            con.execute("INSERT INTO reads(title,author,rating,read_at,source) VALUES(?,?,?,?,'goodreads_rss') ON CONFLICT(title,author) DO UPDATE SET rating=excluded.rating,read_at=excluded.read_at", (title, author, rating, entry.get("user_read_at")))
            count += 1
    from .learning import attribute_read_outcomes
    attribute_read_outcomes()
    return count


def _schema_type_matches(value, *wanted):
    raw_types = value.get("@type", []) if isinstance(value, dict) else []
    types = [raw_types] if isinstance(raw_types, str) else raw_types
    if not isinstance(types, (list, tuple)):
        return False
    wanted_types = set(wanted)
    return any(str(raw).rsplit("/", 1)[-1].rsplit("#", 1)[-1] in wanted_types for raw in types)


def _iter_jsonld_nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from _iter_jsonld_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_jsonld_nodes(child)


def _schema_text(value, limit=4000):
    if isinstance(value, str):
        return _clean_text(value, limit)
    if isinstance(value, (int, float)):
        return str(value)[:limit]
    if isinstance(value, dict):
        for key in ("name", "@value", "value", "text"):
            if value.get(key) not in (None, ""):
                return _schema_text(value[key], limit)
        return ""
    if isinstance(value, (list, tuple)):
        values = []
        for child in value:
            text = _schema_text(child, limit)
            if text and text not in values:
                values.append(text)
        return ", ".join(values)[:limit]
    return ""


def _schema_url(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("url", "contentUrl", "@id"):
            if value.get(key):
                return _schema_url(value[key])
    if isinstance(value, (list, tuple)):
        for child in value:
            url = _schema_url(child)
            if url:
                return url
    return ""


def _schema_author(value):
    return _schema_text(value, 300) or "Unknown author"


def _schema_image(value):
    if isinstance(value, (list, tuple)):
        values = value
    else:
        values = [value]
    for child in values:
        image = _schema_url(child)
        if image and not image.startswith("data:"):
            return image
    return ""


def _schema_genres(value):
    raw_genres = value.get("genre", []) if isinstance(value, dict) else []
    values = raw_genres if isinstance(raw_genres, (list, tuple)) else [raw_genres]
    genres = []
    for raw in values:
        genre = _schema_text(raw, 80)
        if genre and genre not in genres:
            genres.append(genre)
    return genres[:8]


def _date_kind(raw_value):
    raw = str(raw_value or "").strip()
    if not raw or not _date_value(raw):
        return ""
    if re.fullmatch(r"\d{4}", raw):
        return "year"
    if re.fullmatch(r"\d{4}-\d{2}", raw):
        return "month"
    return "day"


def _schema_release(value):
    raw = _schema_text(value, 100)
    release_date = _date_value(raw)
    return release_date, _date_kind(raw) if release_date else ""


def _book_metadata_url(value, source_url):
    fallback = metadata_url(source_url)
    candidate = _schema_url(value)
    if not candidate:
        return fallback
    resolved = metadata_url(urljoin(source_url, candidate), fallback)
    if resolved:
        parsed = urlparse(resolved)
        resolved = parsed._replace(fragment="").geturl()
    return resolved or fallback


def _merge_book_item(items, item, source_url):
    title = _clean_text(item.get("title"), 500)
    if not title:
        return
    item_source_url = metadata_url(item.get("source_url"), source_url)
    isbn13, isbn10 = isbn_parts_from_source(
        [item.get("isbn13"), item.get("isbn10"), item.get("isbn")],
        item_source_url,
    )
    normalized = {
        "title": title,
        "author": _clean_text(item.get("author") or "Unknown author", 300),
        "description": _clean_text(item.get("description"), 4000),
        "cover_url": item.get("cover_url", "") or "",
        "source_url": item_source_url,
        "release_date": item.get("release_date"),
        "date_kind": item.get("date_kind", "") or "",
        "genres": list(item.get("genres") or [])[:8],
    }
    if isbn13:
        normalized["isbn13"] = isbn13
    if isbn10:
        normalized["isbn10"] = isbn10
    title_key = normalize_key(title, "")
    author_key = normalize_key(normalized["author"], "")
    unknown_authors = {"", normalize_key("Unknown author", "")}
    for existing in items:
        if normalize_key(existing.get("title", ""), "") != title_key:
            continue
        existing_author_key = normalize_key(existing.get("author", ""), "")
        authors_unknown = existing_author_key in unknown_authors or author_key in unknown_authors
        if authors_unknown:
            existing_source = existing.get("source_url", "")
            same_specific_source = (
                existing_source
                and normalized["source_url"]
                and existing_source != source_url
                and normalized["source_url"] != source_url
                and existing_source == normalized["source_url"]
            )
            if not same_specific_source:
                continue
        elif existing_author_key != author_key:
            continue
        existing_precision = {"year": 1, "month": 2, "day": 3}.get(existing.get("date_kind", ""), 0)
        incoming_precision = {"year": 1, "month": 2, "day": 3}.get(normalized.get("date_kind", ""), 0)
        for field in ("description", "cover_url", "isbn13", "isbn10"):
            if not existing.get(field) and normalized.get(field):
                existing[field] = normalized[field]
        if normalized.get("release_date") and (
            not existing.get("release_date") or incoming_precision > existing_precision
        ):
            existing["release_date"] = normalized["release_date"]
            existing["date_kind"] = normalized.get("date_kind", "")
        elif not existing.get("date_kind") and normalized.get("date_kind"):
            existing["date_kind"] = normalized["date_kind"]
        if normalized.get("genres"):
            existing["genres"] = list(dict.fromkeys((existing.get("genres") or []) + normalized["genres"]))[:8]
        if existing_author_key in unknown_authors and author_key not in unknown_authors:
            existing["author"] = normalized["author"]
        if existing.get("source_url") in ("", source_url) and normalized.get("source_url") != source_url:
            existing["source_url"] = normalized["source_url"]
        return
    items.append(normalized)


def _parse_jsonld_books(payloads, source_url):
    items = []
    for payload in payloads:
        for value in _iter_jsonld_nodes(payload):
            if not _schema_type_matches(value, "Book", "Audiobook"):
                continue
            release_date, date_kind = _schema_release(value.get("datePublished"))
            image = _schema_image(value.get("image"))
            _merge_book_item(
                items,
                {
                    "title": _schema_text(value.get("name"), 500),
                    "author": _schema_author(value.get("author")),
                    "description": _schema_text(value.get("description"), 4000),
                    "cover_url": safe_cover_url(urljoin(source_url, image), source_url) if image else "",
                    "source_url": _book_metadata_url(value.get("url") or value.get("sameAs") or value.get("@id"), source_url),
                    "isbn13": value.get("isbn13"),
                    "isbn10": value.get("isbn10"),
                    "isbn": value.get("isbn"),
                    "release_date": release_date,
                    "date_kind": date_kind,
                    "genres": _schema_genres(value),
                },
                source_url,
            )
            if len(items) >= settings.source_max_items:
                return items
    return items


def _html_node_value(node):
    if not node:
        return ""
    return node.get("content") or node.get("datetime") or node.get_text(" ", strip=True)


def _generic_card_container(heading):
    for ancestor in heading.parents:
        if not ancestor or ancestor.name in ("body", "html"):
            break
        classes = {str(value).casefold() for value in (ancestor.get("class") or [])}
        itemtype = str(ancestor.get("itemtype") or "").casefold()
        if ancestor.name in ("article", "li") or "book" in itemtype or classes.intersection(
            {"book", "book-card", "book-item", "book-listing", "book-blurb", "card", "listing"}
        ):
            return ancestor
    if heading.parent and heading.parent.select_one(".author-name, [itemprop='author']"):
        return heading.parent
    return None


def _has_generic_book_semantics(heading):
    for ancestor in [heading, *heading.parents]:
        if not ancestor or ancestor.name in ("body", "html"):
            break
        classes = {str(value).casefold() for value in (ancestor.get("class") or [])}
        itemtype = str(ancestor.get("itemtype") or "").casefold()
        if "book" in itemtype or classes.intersection(
            {"book", "book-card", "book-item", "book-listing", "book-blurb"}
        ):
            return True
    return False


def _parse_generic_book_cards(soup, source_url):
    items = []
    for heading in soup.select(
        "h1.book-title, h2.book-title, h3.book-title, h4.book-title, "
        "h1[itemprop='name'], h2[itemprop='name'], h3[itemprop='name'], h4[itemprop='name']"
    ):
        title = _clean_text(heading.get_text(" ", strip=True), 500)
        container = _generic_card_container(heading)
        author_node = container.select_one(".author-name, [itemprop='author']") if container else None
        author = _clean_text(_html_node_value(author_node), 300)
        author = re.sub(r"^\s*(?:by|author)\s*:?[\s]+", "", author, flags=re.I)
        if not title or not author:
            continue
        link = heading.find_parent("a", href=True) or heading.find("a", href=True)
        if not link and container:
            link = container.select_one("a[itemprop='url'][href], a[href]")
        href = link.get("href", "") if link else ""
        description_node = container.select_one(".blurb-content, .description, [itemprop='description']") if container else None
        paragraphs = description_node.select("p") if description_node else []
        paragraph_text = []
        for paragraph in paragraphs:
            text = _clean_text(paragraph, 4000)
            if text:
                paragraph_text.append(text)
        description = " ".join(paragraph_text)
        if not description and description_node:
            description = _clean_text(description_node, 4000)
        genres = [
            _clean_text(node, 80)
            for node in (container.select(".genre-tag, [itemprop='genre']") if container else [])
            if _clean_text(node, 80)
        ]
        image = ""
        for node in (container.select("img, [itemprop='image']") if container else []):
            image = (
                node.get("data-lazy-src")
                or node.get("data-src")
                or node.get("data-original")
                or node.get("src")
                or _html_node_value(node)
                or ""
            )
            if image and not image.startswith("data:"):
                break
        date_node = container.select_one("[itemprop='datePublished'], time") if container else None
        raw_date = _html_node_value(date_node)
        _merge_book_item(
            items,
            {
                "title": title,
                "author": author,
                "description": description,
                "cover_url": safe_cover_url(urljoin(source_url, image), source_url) if image else "",
                "source_url": metadata_url(urljoin(source_url, href), source_url),
                "release_date": _date_value(raw_date),
                "date_kind": _date_kind(raw_date),
                "genres": list(dict.fromkeys(genres))[:8],
            },
            source_url,
        )
        if len(items) >= settings.source_max_items:
            break
    return items


def _generic_author_from_line(value):
    text = _clean_text(value, 600)
    if not text:
        return ""
    leading_by = bool(re.match(r"^(?:by|written\s+by|author)\s+", text, re.I))
    text = re.sub(r"^(?:by|written\s+by|author)\s*:?[\s]+", "", text, flags=re.I).strip()
    isbn_match = re.search(r"\bISBN(?:-\d+)?\s*[:#]?\s*[0-9Xx][0-9Xx -]{8,17}", text, re.I)
    if isbn_match:
        text = text[: isbn_match.start()].strip(" .,-")
        price_match = re.search(r"\s[$€£]\s*[\d,.]+", text)
        if price_match:
            text = text[: price_match.start()].strip(" .,-")
        parts = re.split(r"\.\s+", text, maxsplit=1)
        return _clean_text(parts[0], 300) if parts else ""
    parenthesized = re.fullmatch(r"(.+?)\s+\([^()]+\)", text)
    if parenthesized:
        return _clean_text(parenthesized.group(1), 300)
    if leading_by and text:
        return _clean_text(text, 300)
    return ""


def _parse_generic_heading_pairs(soup, source_url):
    items = []
    for heading in soup.select("h2, h3, h4"):
        heading_classes = {str(value).casefold() for value in (heading.get("class") or [])}
        if heading_classes.intersection({"book-title", "author", "authors", "contributor", "contributors", "isbn-related"}):
            continue
        title = _clean_text(heading.get_text(" ", strip=True), 500)
        link = heading.find("a", href=True)
        metadata = heading.find_next_sibling()
        book_container = _generic_card_container(heading)
        is_semantic_book = heading.get("itemprop") == "name" or _has_generic_book_semantics(heading)
        bounded_heading_pair = (
            book_container is not None
            and book_container.name in ("article", "li")
            and metadata is not None
            and metadata.name in ("h3", "h4")
        )
        if not link and not is_semantic_book and not bounded_heading_pair:
            continue
        if link and re.match(r"^by\s+", title, re.I) and "author" in link.get("href", "").casefold():
            continue
        if metadata and metadata.name == heading.name and heading.name in ("h1", "h2", "h3"):
            continue
        author = _generic_author_from_line(metadata.get_text(" ", strip=True) if metadata else "")
        if not title or not author:
            continue
        href = link.get("href", "") if link else ""
        description_node = metadata.find_next_sibling() if metadata else None
        description = _clean_text(description_node, 4000) if description_node and description_node.name == "p" else ""
        _merge_book_item(
            items,
            {
                "title": title,
                "author": author,
                "description": description,
                "cover_url": "",
                "source_url": metadata_url(urljoin(source_url, href), source_url),
                "release_date": None,
                "date_kind": "",
                "genres": [],
            },
            source_url,
        )
        if len(items) >= settings.source_max_items:
            break
    return items


def _parse_generic_html(soup, source_url):
    items = _parse_generic_book_cards(soup, source_url)
    for item in _parse_generic_heading_pairs(soup, source_url):
        _merge_book_item(items, item, source_url)
        if len(items) >= settings.source_max_items:
            break
    return items[: settings.source_max_items]

def parse_book_items(content: bytes, content_type: str, source_url: str):
    if "xml" in content_type or "rss" in content_type or content.lstrip().startswith(b"<?xml"):
        feed = feedparser.parse(content)
        if _is_apple_source(source_url):
            return _parse_apple_entries(feed.entries, source_url)
        return [
            _with_source_isbn(
                {
                    "title": str(e.get("title", ""))[:500],
                    "author": str(e.get("author", "Unknown author"))[:300],
                    "description": BeautifulSoup(str(e.get("summary", "")), "html.parser").get_text(" ")[:4000],
                    "source_url": metadata_url(e.get("link"), source_url),
                },
                [e.get(key) for key in ("isbn13", "isbn10", "isbn")],
            )
            for e in feed.entries[:settings.source_max_items]
            if e.get("title")
        ]
    payloads = []
    soup = None
    if "json" in content_type or "javascript" in content_type:
        try: payloads = [json.loads(content)]
        except (json.JSONDecodeError, UnicodeDecodeError): return []
        payload = payloads[0]
        if _is_apple_source(source_url) and isinstance(payload, dict):
            return _parse_apple_entries(payload.get("feed", {}).get("entry", []), source_url)
        if _source_matches(source_url, "openlibrary.org", "/subjects/") and isinstance(payload, dict):
            return _parse_open_library(payload, source_url)
        if _source_matches(source_url, "api.nytimes.com") and isinstance(payload, dict):
            return _parse_nytimes(payload, source_url)
    else:
        if _is_goodreads_blog_source(source_url):
            return _parse_goodreads_blog(content, source_url)
        if _is_editorial_source(source_url):
            return _parse_editorial_html(content, source_url)
        if _source_matches(source_url, "goodreads.com", "/genres/") or _source_matches(source_url, "www.goodreads.com", "/genres/"):
            return _parse_goodreads_genre(content, source_url)
        soup = BeautifulSoup(content, "html.parser")
        for node in soup.select('script[type="application/ld+json"]'):
            try: payloads.append(json.loads(node.string or node.get_text() or "null"))
            except json.JSONDecodeError: continue
    items = _parse_jsonld_books(payloads, source_url)
    if soup is not None:
        for item in _parse_generic_html(soup, source_url):
            _merge_book_item(items, item, source_url)
            if len(items) >= settings.source_max_items:
                break
    return items[: settings.source_max_items]


async def enrich_cover_urls(items):
    """Fill missing/unsafe artwork URLs without blocking source parsing.

    Provider calls are bounded so a source with many books cannot create an
    unbounded fan-out.  A provider outage is intentionally non-fatal: each
    item receives a deterministic placeholder URL from ``resolve_cover_url``.
    """

    if not items:
        return []
    semaphore = asyncio.Semaphore(8)

    async with metadata_client() as client:
        async def enrich(item):
            async with semaphore:
                cover = await resolve_cover_url(
                    item["title"],
                    item.get("author", "Unknown author"),
                    item.get("cover_url", ""),
                    item.get("source_url", ""),
                    client=client,
                )
                return {**item, "cover_url": cover}

        return await asyncio.gather(*(enrich(item) for item in items))


async def enrich_book_metadata(items):
    """Fill summaries and publication dates while preserving source metadata."""

    if not items:
        return []
    semaphore = asyncio.Semaphore(8)
    lookup_cache = {}

    async with metadata_client() as client:
        async def enrich(item):
            async with semaphore:
                metadata = await resolve_book_metadata(
                    item["title"],
                    item.get("author", "Unknown author"),
                    item.get("cover_url", ""),
                    item.get("source_url", ""),
                    item.get("description", ""),
                    item.get("release_date", ""),
                    item.get("genres", []),
                    client=client,
                    lookup_cache=lookup_cache,
                    expected_provider=item.get("_expected_provider", ""),
                    expected_provider_id=item.get("_expected_provider_id", ""),
                )
                enriched = {**item, "cover_url": metadata["cover_url"]}
                if not enriched.get("description") and metadata["description"]:
                    enriched["description"] = metadata["description"]
                if not enriched.get("release_date") and metadata["release_date"]:
                    enriched["release_date"] = metadata["release_date"]
                    enriched["date_kind"] = metadata.get("date_kind") or "day"
                elif enriched.get("release_date") and not enriched.get("date_kind"):
                    enriched["date_kind"] = "source"
                existing_genres = _filter_genre_values(enriched.get("genres", []))
                genre_keys = {genre.casefold() for genre in existing_genres}
                for genre in _filter_genre_values(metadata.get("genres", [])):
                    if genre.casefold() not in genre_keys:
                        existing_genres.append(genre)
                        genre_keys.add(genre.casefold())
                    if len(existing_genres) >= 8:
                        break
                enriched["genres"] = existing_genres
                enriched["_metadata_provider"] = metadata.get("provider", "")
                enriched["_metadata_provider_id"] = metadata.get("provider_id", "")
                enriched["_metadata_work_id"] = metadata.get("work_id", "")
                enriched["_metadata_title_match"] = metadata.get("title_match", 0.0)
                enriched["_metadata_author_match"] = metadata.get("author_match", 0.0)
                return enriched

        return await asyncio.gather(*(enrich(item) for item in items))


async def refresh_missing_candidate_metadata():
    """Backfill sparse metadata for catalog-accepted candidates only."""

    candidates = rows(
        "SELECT c.id,c.title,c.author,c.description,c.cover_url,c.source_url,c.release_date,c.date_kind,c.genres,"
        "q.provider AS _expected_provider,q.provider_id AS _expected_provider_id "
        "FROM candidates c JOIN candidate_quality q ON q.candidate_id=c.id "
        "WHERE c.status!='rejected' AND q.quality_status='accepted' AND ("
        "cover_url='' OR cover_url LIKE '%/b/isbn/%' OR cover_url LIKE 'https://placehold.co/%' "
        "OR description='' OR release_date IS NULL "
        "OR CASE WHEN json_valid(genres) THEN json_array_length(genres) ELSE 0 END=0) "
        "ORDER BY q.metadata_checked_at ASC,c.score DESC,c.id LIMIT 50"
    )
    if not candidates:
        return 0
    enriched = await enrich_book_metadata(
        [{**candidate, "genres": _stored_genre_values(candidate.get("genres"))} for candidate in candidates]
    )
    changed = 0
    with transaction() as con:
        for previous, item in zip(candidates, enriched):
            description = item.get("description", "")
            cover_url = item.get("cover_url", "")
            release_date = item.get("release_date") or None
            date_kind = item.get("date_kind") or previous.get("date_kind") or "unknown"
            previous_genres = _stored_genre_values(previous.get("genres", []))
            item_genres = _filter_genre_values(item.get("genres", []))
            genres = list(previous_genres)
            genre_keys = {genre.casefold() for genre in genres}
            for genre in item_genres:
                if genre.casefold() not in genre_keys:
                    genres.append(genre)
                    genre_keys.add(genre.casefold())
                if len(genres) >= 8:
                    break
            genres_json = json.dumps(genres, ensure_ascii=False, separators=(",", ":"))
            metadata_provider = str(item.get("_metadata_provider", "") or "")
            metadata_provider_id = str(item.get("_metadata_provider_id", "") or "")
            metadata_work_id = str(item.get("_metadata_work_id", "") or "")
            if (
                (description and not previous.get("description"))
                or (cover_url and (not previous.get("cover_url") or is_weak_cover_url(previous.get("cover_url"))))
                or (release_date and not previous.get("release_date"))
                or (len(genres) > len(previous_genres))
            ):
                changed += 1
            con.execute(
                """UPDATE candidates SET
                description=CASE WHEN description='' AND ?!='' THEN ? ELSE description END,
                cover_url=CASE WHEN (cover_url='' OR cover_url LIKE '%/b/isbn/%' OR cover_url LIKE 'https://placehold.co/%') AND ?!='' THEN ? ELSE cover_url END,
                release_date=COALESCE(release_date, ?),
                date_kind=CASE WHEN release_date IS NULL AND ? IS NOT NULL THEN ? ELSE date_kind END,
                genres=CASE WHEN ?!='[]' THEN ? ELSE genres END,
                updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (description, description, cover_url, cover_url, release_date, release_date, date_kind, genres_json, genres_json, item["id"]),
            )
            con.execute(
                """UPDATE candidate_quality SET metadata_checked_at=?,
                    metadata_provider=CASE WHEN ?!='' THEN ? ELSE metadata_provider END,
                    metadata_provider_id=CASE WHEN ?!='' THEN ? ELSE metadata_provider_id END,
                    provider=CASE WHEN provider='' THEN ? ELSE provider END,
                    provider_id=CASE WHEN provider='' THEN ? ELSE provider_id END,
                    work_id=CASE WHEN provider='' THEN ? ELSE work_id END,
                    title_match=CASE WHEN provider='' THEN ? ELSE title_match END,
                    author_match=CASE WHEN provider='' THEN ? ELSE author_match END,
                    updated_at=CURRENT_TIMESTAMP
                    WHERE candidate_id=? AND quality_status='accepted'""",
                (
                    datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                    metadata_provider,
                    metadata_provider,
                    metadata_provider_id,
                    metadata_provider_id,
                    metadata_provider,
                    metadata_provider_id,
                    metadata_work_id,
                    float(item.get("_metadata_title_match", 0) or 0),
                    float(item.get("_metadata_author_match", 0) or 0),
                    item["id"],
                )
            )
    return changed


async def refresh_missing_candidate_covers():
    """Compatibility wrapper for callers that previously backfilled covers."""

    return await refresh_missing_candidate_metadata()

async def preview_source(url, filters=None):
    content_type, items = await _fetch_and_parse_source(url)
    filtered = filter_source_items(items, filters)
    return {
        "url": url,
        "content_type": content_type,
        "count": len(filtered),
        "raw_count": len(items),
        "sample": filtered[:5],
    }

async def scan_source(source):
    _, items = await _fetch_and_parse_source(source["url"])
    raw_count = len(items)
    items = filter_source_items(items, source.get("filters"))
    items = await enrich_book_metadata(items)
    with transaction() as con:
        seen = []
        for item in items:
            key = normalize_key(item["title"], item.get("author", "Unknown author"))
            seen.append(key)
            isbn13, isbn10 = isbn_parts_from_source(
                [item.get("isbn13"), item.get("isbn10"), item.get("isbn")],
                item.get("source_url", source["url"]),
            )
            existing = con.execute(
                "SELECT id,isbn13,isbn10,genres FROM candidates WHERE normalized_key=?",
                (key,),
            ).fetchone()
            genres = _stored_genre_values(existing["genres"] if existing else [])
            genre_keys = {genre.casefold() for genre in genres}
            for genre in _filter_genre_values(item.get("genres", [])):
                if genre.casefold() not in genre_keys:
                    genres.append(genre)
                    genre_keys.add(genre.casefold())
                if len(genres) >= 8:
                    break
            genres_json = json.dumps(genres, ensure_ascii=False, separators=(",", ":"))
            con.execute("""INSERT INTO candidates(title,author,description,cover_url,source_url,source_id,release_date,date_kind,genres,isbn13,isbn10,normalized_key)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(normalized_key) DO UPDATE SET
            description=CASE WHEN description='' AND excluded.description!='' THEN excluded.description ELSE description END,
            cover_url=CASE WHEN length(excluded.cover_url)>0 THEN excluded.cover_url ELSE cover_url END,
            source_url=CASE WHEN length(excluded.source_url)>0 THEN excluded.source_url ELSE source_url END,
            release_date=COALESCE(excluded.release_date, release_date),
            date_kind=CASE WHEN excluded.release_date IS NOT NULL AND release_date IS NULL THEN excluded.date_kind ELSE date_kind END,
            genres=CASE WHEN excluded.genres!='[]' THEN excluded.genres ELSE genres END,
            isbn13=CASE WHEN isbn13='' AND excluded.isbn13!='' THEN excluded.isbn13 ELSE isbn13 END,
            isbn10=CASE WHEN isbn10='' AND excluded.isbn10!='' THEN excluded.isbn10 ELSE isbn10 END,
            updated_at=CURRENT_TIMESTAMP""", (item["title"], item.get("author", "Unknown author"), item.get("description", ""), item.get("cover_url", ""), item.get("source_url", source["url"]), source["id"], item.get("release_date"), item.get("date_kind", "source"), genres_json, isbn13, isbn10, key))
            candidate = con.execute(
                "SELECT id,isbn13,isbn10 FROM candidates WHERE normalized_key=?",
                (key,),
            ).fetchone()
            if candidate and (isbn13 or isbn10):
                con.execute(
                    """UPDATE candidate_quality SET
                        isbn13=CASE WHEN isbn13='' THEN ? ELSE isbn13 END,
                        isbn10=CASE WHEN isbn10='' THEN ? ELSE isbn10 END,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE candidate_id=?""",
                    (isbn13, isbn10, candidate["id"]),
                )
            isbn_added = bool(
                (isbn13 and not (existing["isbn13"] if existing else ""))
                or (isbn10 and not (existing["isbn10"] if existing else ""))
            )
            if existing and isbn_added:
                # A newly discovered ISBN is evidence for a retry, not proof
                # of identity. Requeue only quarantined rows; accepted rows
                # remain untouched and therefore cannot change ranking here.
                con.execute(
                    """UPDATE candidate_quality SET quality_status='pending',
                        updated_at=CURRENT_TIMESTAMP
                    WHERE candidate_id=? AND quality_status='quarantine'""",
                    (candidate["id"],),
                )
        if seen:
            placeholders = ",".join("?" for _ in seen)
            con.execute(f"DELETE FROM candidates WHERE source_id=? AND status IN ('new','recommended') AND normalized_key NOT IN ({placeholders})", (source["id"], *seen))
        elif raw_count:
            con.execute(
                "DELETE FROM candidates WHERE source_id=? AND status IN ('new','recommended')",
                (source["id"],),
            )
        # An empty response can be a transient block page, parser mismatch,
        # or upstream outage. It is not safe to interpret it as proof that a
        # source no longer contains any books, so retain existing candidates.
        status = f"ok:{len(items)}" if items else "empty:0"
        con.execute("UPDATE sources SET last_status=?, last_scanned_at=CURRENT_TIMESTAMP WHERE id=?", (status, source["id"]))
    return len(items)
