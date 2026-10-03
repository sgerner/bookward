import asyncio
import csv
import hashlib
import html
import io
import json
import re
import math
import unicodedata
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse
import feedparser
import httpx
from bs4 import BeautifulSoup
from .config import settings
from .covers import (
    METADATA_GENRE_LIMIT,
    METADATA_NORMALIZATION_VERSION,
    _publication_date,
    is_weak_cover_url,
    metadata_client,
    resolve_book_metadata,
    resolve_cover_url,
    resolve_openlibrary_work_metadata,
    safe_cover_url,
    wait_for_openlibrary_request_slot,
)
from .database import normalize_key, persist_metadata_field_provenance, private_setting, row, rows, transaction
from .identity import (
    book_author_identity_key,
    book_catalog_title_identity_key,
    book_identity_match_keys,
    book_openlibrary_work_id,
    book_row_identity_match_keys,
)
from .isbn import canonical_isbn, isbn_parts, isbn_parts_from_source
from .security import resolve_public_target
from .subjects import normalize_subjects

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
PENGUIN_RANDOM_HOUSE_HOSTS = frozenset(
    {"penguinrandomhouse.com", "www.penguinrandomhouse.com"}
)
SOURCE_MAX_REDIRECTS = 4
SOURCE_FILTER_MAX_GENRES = 12
SOURCE_FILTER_GENRE_MAX_LENGTH = 80
READ_WORK_IDENTITY_BATCH_SIZE = 25
READ_WORK_IDENTITY_RETRY_DAYS = 30
READ_METADATA_BATCH_SIZE = 5
READ_METADATA_RETRY_DAYS = 30
READ_METADATA_TRANSIENT_RETRY_HOURS = 24
CANDIDATE_METADATA_BATCH_SIZE = 50
CANDIDATE_METADATA_RETRY_DAYS = 30
SOURCE_METADATA_ENRICHMENT_BATCH_SIZE = 50
READ_WORK_IDENTITY_MAX_REDIRECTS = 3


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
    return normalize_subjects(value, limit=METADATA_GENRE_LIMIT)


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
        if isinstance(authors, (str, dict)):
            authors = [authors]
        names = [
            str(author.get("name", "")).strip()
            if isinstance(author, dict)
            else str(author).strip()
            for author in authors
            if (author.get("name") if isinstance(author, dict) else author)
        ]
        if not names:
            author_names = work.get("author_name") or work.get("author_names") or []
            if isinstance(author_names, str):
                author_names = [author_names]
            if isinstance(author_names, list):
                names = [str(author).strip() for author in author_names if author]
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
        raw_release_date = work.get("first_publish_date") or work.get("first_publish_year")
        subjects = work.get("subject") or work.get("subjects") or []
        if isinstance(subjects, str):
            subjects = [subjects]
        items.append(
            _with_source_isbn(
                {
                    "title": title[:500],
                    "author": author[:300],
                    "description": _clean_text(description),
                    "cover_url": safe_cover_url(f"https://covers.openlibrary.org/b/id/{int(cover_id)}-L.jpg", source_url) if str(cover_id).isdigit() else "",
                    "source_url": source,
                    "release_date": _date_value(raw_release_date),
                    "date_kind": _date_kind(raw_release_date),
                    "genres": [str(subject)[:80] for subject in subjects[:8] if subject],
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
            con.execute(
                "INSERT INTO reads(title,author,rating,read_at,isbn,source) "
                "VALUES(?,?,?,?,?,'goodreads_csv') "
                "ON CONFLICT(title,author) DO UPDATE SET "
                "rating=excluded.rating,read_at=excluded.read_at,"
                "openlibrary_work_id=CASE WHEN excluded.isbn IS NOT NULL "
                "AND excluded.isbn IS NOT reads.isbn THEN '' ELSE reads.openlibrary_work_id END,"
                "openlibrary_lookup_attempted_at=CASE WHEN excluded.isbn IS NOT NULL "
                "AND excluded.isbn IS NOT reads.isbn THEN NULL "
                "ELSE reads.openlibrary_lookup_attempted_at END,"
                "isbn=COALESCE(excluded.isbn,reads.isbn)",
                (
                    title,
                    author,
                    rating,
                    row.get("Date Read") or None,
                    (row.get("ISBN13") or row.get("ISBN") or "").strip('="') or None,
                ),
            )
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


async def _request_openlibrary_edition(client, isbn13: str):
    """Follow Open Library's ISBN-to-edition redirect with every hop pinned.

    The generic pinned request intentionally does not follow redirects because
    doing so without validating each destination would bypass SSRF protections.
    Open Library's ISBN endpoint redirects to ``/books/<edition>.json``; allow
    only HTTPS redirects back to the same public catalog host.
    """

    url = f"https://openlibrary.org/isbn/{isbn13}.json"
    for _ in range(READ_WORK_IDENTITY_MAX_REDIRECTS + 1):
        await wait_for_openlibrary_request_slot()
        response = await _request_pinned(client, "GET", url)
        location = response.headers.get("location", "").strip()
        if not response.is_redirect or not location:
            return response

        target = urljoin(url, location)
        parsed = urlparse(target)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "openlibrary.org"
            or parsed.port not in (None, 443)
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Open Library ISBN endpoint returned an unsafe redirect")
        url = target

    raise httpx.TooManyRedirects("Too many redirects from the Open Library ISBN endpoint")


def _openlibrary_work_from_edition(payload, requested_isbn13: str) -> str:
    """Return a single verified Open Library work key for an ISBN edition."""

    if not isinstance(payload, dict):
        return ""
    edition_isbns = set()
    for field in ("isbn_13", "isbn_10"):
        values = payload.get(field) or []
        if isinstance(values, (str, int)):
            values = [values]
        if not isinstance(values, (list, tuple, set)):
            continue
        for value in values:
            isbn13, _isbn10 = isbn_parts(value)
            if isbn13:
                edition_isbns.add(isbn13)
    if not requested_isbn13 or requested_isbn13 not in edition_isbns:
        return ""

    works = payload.get("works") or []
    if not isinstance(works, list):
        return ""
    work_ids = set()
    for work in works:
        if not isinstance(work, dict):
            continue
        work_id = book_openlibrary_work_id(
            {"openlibrary_work_id": work.get("key", "")}
        )
        if work_id:
            work_ids.add(work_id)
    # An edition linked to several works is ambiguous for automatic exclusion.
    return next(iter(work_ids)) if len(work_ids) == 1 else ""


async def refresh_read_work_identities(
    limit: int = READ_WORK_IDENTITY_BATCH_SIZE,
) -> dict[str, int]:
    """Cache verified Open Library work IDs for reads that could collide.

    ISBN lookups are limited to reads by an author with an active, accepted
    Open Library candidate whose title/edition identity does not already match.
    This keeps requests focused and lets the exact work key, rather than fuzzy
    title translation, decide whether two rows describe one work.
    """

    reads = rows("SELECT * FROM reads ORDER BY id")
    candidates = rows(
        "SELECT c.*,q.work_id quality_work_id,q.provider quality_provider,"
        "q.isbn13 quality_isbn13,q.isbn10 quality_isbn10 "
        "FROM candidates c JOIN candidate_quality q ON q.candidate_id=c.id "
        "JOIN sources s ON s.id=c.source_id "
        "WHERE c.status IN ('new','recommended') "
        "AND q.quality_status='accepted' AND s.enabled=1"
    )

    candidates_by_author: dict[str, list[tuple[dict, set]]] = {}
    for candidate in candidates:
        if not book_openlibrary_work_id(candidate):
            continue
        author_key = book_author_identity_key(candidate.get("author"))
        if not author_key:
            continue
        candidates_by_author.setdefault(author_key, []).append(
            (candidate, book_row_identity_match_keys(candidate))
        )

    now = datetime.now(timezone.utc)
    retry_before = now - timedelta(days=READ_WORK_IDENTITY_RETRY_DAYS)
    targets: dict[int, tuple[float, dict]] = {}
    for read in reads:
        if not read.get("isbn") or read.get("openlibrary_work_id"):
            continue
        attempted_at = read.get("openlibrary_lookup_attempted_at")
        if attempted_at:
            try:
                attempted = datetime.fromisoformat(
                    str(attempted_at).replace("Z", "+00:00")
                )
            except ValueError:
                attempted = retry_before - timedelta(seconds=1)
            if attempted.tzinfo is None:
                attempted = attempted.replace(tzinfo=timezone.utc)
            if attempted > retry_before:
                continue

        author_key = book_author_identity_key(read.get("author"))
        author_candidates = candidates_by_author.get(author_key, [])
        if not author_candidates:
            continue
        read_keys = book_row_identity_match_keys(read)
        unmatched = [
            candidate
            for candidate, candidate_keys in author_candidates
            if not (read_keys & candidate_keys)
        ]
        if not unmatched:
            continue
        isbn13, _isbn10 = isbn_parts(read.get("isbn"))
        if not isbn13:
            continue
        priority = max(float(candidate.get("score") or 0) for candidate in unmatched)
        previous = targets.get(int(read["id"]))
        if previous is None or priority > previous[0]:
            targets[int(read["id"])] = (priority, read)

    batch_size = max(1, min(int(limit), READ_WORK_IDENTITY_BATCH_SIZE))
    batch = sorted(
        targets.values(),
        key=lambda item: (-item[0], int(item[1]["id"])),
    )[:batch_size]
    if not batch:
        return {"checked": 0, "matched": 0, "lookup_failures": 0, "remaining": 0}

    updates = []
    matched = 0
    lookup_failures = 0
    async with metadata_client() as client:
        for _priority, read in batch:
            isbn13, _isbn10 = isbn_parts(read.get("isbn"))
            work_id = ""
            try:
                response = await _request_openlibrary_edition(client, isbn13)
                if response.status_code != 404:
                    response.raise_for_status()
                    work_id = _openlibrary_work_from_edition(
                        response.json(), isbn13
                    )
            except Exception:
                # A failed optional lookup must not block recommendation
                # serving. Its attempt timestamp also prevents a tight retry.
                lookup_failures += 1
            updates.append(
                (
                    work_id,
                    datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                    int(read["id"]),
                )
            )
            matched += bool(work_id)

    with transaction() as con:
        con.executemany(
            "UPDATE reads SET openlibrary_work_id=?,"
            "openlibrary_lookup_attempted_at=? WHERE id=?",
            updates,
        )
    return {
        "checked": len(batch),
        "matched": int(matched),
        "lookup_failures": lookup_failures,
        "remaining": max(0, len(targets) - len(batch)),
    }


def _read_metadata_identity_hash(read):
    """Fingerprint the read's bibliographic identity and verified work key."""

    isbn13, isbn10 = isbn_parts(read.get("isbn"))
    identity = {
        "title": normalize_key(read.get("title", ""), ""),
        "author": normalize_key("", read.get("author", "")),
        "source_isbn": re.sub(r"[^0-9xX]", "", str(read.get("isbn") or "")).upper(),
        "isbn13": isbn13,
        "isbn10": isbn10,
        "openlibrary_work_id": book_openlibrary_work_id(read),
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _returned_identifiers_match_read(returned, wanted13, wanted10):
    """Compare every projected provider identifier with the read's ISBN."""

    values = list(returned) if isinstance(returned, (list, tuple, set)) else [returned]
    for value in values:
        returned13, returned10 = isbn_parts(value)
        canonical = canonical_isbn(value)
        if canonical and (canonical == wanted13 or canonical == wanted10):
            return True
        if (wanted13 and returned13 == wanted13) or (wanted10 and returned10 == wanted10):
            return True
    return False


def _read_metadata_rows():
    return rows(
        """SELECT r.*,m.verified_work_id AS cached_work_id,
            m.identity_provider AS cached_identity_provider,
            m.identity_provider_id AS cached_identity_provider_id,
            m.identity_hash AS cached_identity_hash,
            m.description AS cached_description,
            m.description_kind AS cached_description_kind,
            m.genres AS cached_genres,m.cover_url AS cached_cover_url,
            m.release_date AS cached_release_date,m.date_kind AS cached_date_kind,
            m.metadata_provenance AS cached_metadata_provenance,
            m.metadata_checked_at
        FROM reads r LEFT JOIN read_metadata m ON m.read_id=r.id
        WHERE r.rating IS NOT NULL AND TRIM(COALESCE(r.isbn,''))!=''
        ORDER BY COALESCE(m.metadata_checked_at,'') ASC,r.id"""
    )


def _read_metadata_attempt_is_due(read, retry_before):
    if not any(isbn_parts(read.get("isbn"))):
        return False
    current_work_id = book_openlibrary_work_id(read)
    cached_work_id = book_openlibrary_work_id(
        {"openlibrary_work_id": read.get("cached_work_id") or ""}
    )
    if (
        str(read.get("cached_identity_hash") or "") != _read_metadata_identity_hash(read)
        or (current_work_id and cached_work_id and cached_work_id != current_work_id)
    ):
        return True
    attempted_at = read.get("metadata_checked_at")
    if not attempted_at:
        return True
    try:
        attempted = datetime.fromisoformat(str(attempted_at).replace("Z", "+00:00"))
    except ValueError:
        return True
    if attempted.tzinfo is None:
        attempted = attempted.replace(tzinfo=timezone.utc)
    try:
        provenance = json.loads(read.get("cached_metadata_provenance") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        provenance = {}
    attempt = provenance.get("last_attempt", provenance) if isinstance(provenance, dict) else {}
    traces = attempt.get("fetch_trace", []) if isinstance(attempt, dict) else []
    transient_statuses = {"request_error", "http_error", "rate_limited", "server_error", "timeout", "provider_unavailable"}
    transient = bool(
        isinstance(attempt, dict)
        and (attempt.get("error_type") or attempt.get("attempt_status") in {"request_error", "provider_unavailable"})
    ) or (
        isinstance(traces, list)
        and any(isinstance(trace, dict) and trace.get("status") in transient_statuses for trace in traces)
    )
    if transient:
        transient_retry_before = datetime.now(timezone.utc) - timedelta(hours=READ_METADATA_TRANSIENT_RETRY_HOURS)
        return attempted <= transient_retry_before
    return attempted <= retry_before


def _verified_read_metadata(metadata, read):
    """Keep only fields from an exact ISBN result or a cached exact work key."""

    provenance = metadata.get("metadata_provenance", {})
    if not isinstance(provenance, dict):
        return {}, {}, ""
    traces = provenance.get("fetch_trace", [])
    payloads = provenance.get("source_payloads", [])
    if not isinstance(traces, list) or not isinstance(payloads, list):
        return {}, {}, ""

    known_work_id = book_openlibrary_work_id(read)
    verified: dict[tuple[str, str], dict] = {}
    if known_work_id:
        returned_id = book_openlibrary_work_id(
            {"openlibrary_work_id": metadata.get("work_id") or metadata.get("provider_id", "")}
        )
        candidate_title = book_catalog_title_identity_key(metadata.get("catalog_title", ""))
        read_title = book_catalog_title_identity_key(read.get("title", ""))
        matching_trace = any(
            isinstance(trace, dict)
            and trace.get("provider") == "openlibrary"
            and trace.get("status") == "matched"
            and trace.get("provider_id") == known_work_id
            for trace in traces
        )
        if returned_id == known_work_id and candidate_title == read_title and matching_trace:
            verified[("openlibrary", known_work_id)] = {"kind": "verified_work_id"}
    else:
        wanted13, wanted10 = isbn_parts(read.get("isbn"))
        read_title = book_catalog_title_identity_key(read.get("title", ""))
        read_author = book_author_identity_key(read.get("author", ""))
        matched_isbn_traces = [
            trace for trace in traces
            if isinstance(trace, dict)
            and trace.get("query_kind") == "isbn"
            and trace.get("status") == "matched"
            and trace.get("provider")
            and trace.get("provider_id")
        ]
        for trace in matched_isbn_traces:
            provider = str(trace.get("provider") or "")
            provider_id = str(trace.get("provider_id") or "")
            source = next(
                (
                    item for item in payloads
                    if isinstance(item, dict)
                    and item.get("provider") == provider
                    and str(item.get("provider_id") or "") == provider_id
                    and isinstance(item.get("payload"), dict)
                ),
                None,
            )
            if not source:
                continue
            source_payload = source["payload"]
            identifier_matches = _returned_identifiers_match_read(
                source_payload.get("identifiers", []), wanted13, wanted10
            )
            catalog_title = book_catalog_title_identity_key(source_payload.get("title", ""))
            catalog_authors = source_payload.get("authors", [])
            if not isinstance(catalog_authors, (list, tuple)):
                catalog_authors = [catalog_authors]
            author_matches = any(
                book_author_identity_key(author) == read_author
                for author in catalog_authors
                if author
            )
            if identifier_matches and catalog_title == read_title and author_matches:
                verified[(provider, provider_id)] = {
                    "kind": "isbn_record",
                    "query_kind": "isbn",
                    "identifier_verified": True,
                }

    if not verified:
        return {}, {}, ""

    fields = provenance.get("fields", {})
    if not isinstance(fields, dict):
        fields = {}
    trusted_payloads = [
        source for source in payloads
        if isinstance(source, dict)
        and (str(source.get("provider") or ""), str(source.get("provider_id") or "")) in verified
    ]
    filtered_fields = {}
    selected_by_field = {}
    for field in ("description", "genres", "cover_url", "release_date"):
        info = fields.get(field)
        if not isinstance(info, dict):
            continue
        sources = info.get("sources")
        if not isinstance(sources, list):
            sources = [info]
        selected = [
            source for source in sources
            if isinstance(source, dict)
            and (str(source.get("provider") or ""), str(source.get("provider_id") or "")) in verified
        ]
        if selected:
            # Never carry a merged field's top-level attribution through after
            # its source has been filtered out. In particular, genres can be
            # merged from one verified and one unrelated catalog result.
            selected_by_field[field] = selected
            selected_field = dict(selected[0])
            selected_field["provider"] = str(selected[0].get("provider") or "")
            selected_field["provider_id"] = str(selected[0].get("provider_id") or "")
            selected_field["source_field"] = str(selected[0].get("source_field") or "")
            if field == "genres":
                selected_values = normalize_subjects(
                    [
                        value
                        for source in selected
                        for payload in trusted_payloads
                        if payload.get("provider") == source.get("provider")
                        and str(payload.get("provider_id") or "") == str(source.get("provider_id") or "")
                        and isinstance(payload.get("payload"), dict)
                        for value in payload["payload"].get("genres", [])
                    ],
                    limit=METADATA_GENRE_LIMIT,
                )
                selected_field["value_sha256"] = hashlib.sha256(
                    json.dumps(selected_values, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
            filtered_fields[field] = (
                {**info, **selected_field, "sources": selected}
                if len(selected) != 1 or info.get("sources")
                else selected_field
            )

    filtered = dict(metadata)
    filtered["metadata_provenance"] = {
        **provenance,
        "fields": filtered_fields,
        "fetch_trace": [
            trace for trace in traces
            if isinstance(trace, dict)
            and (str(trace.get("provider") or ""), str(trace.get("provider_id") or "")) in verified
        ],
        "source_payloads": trusted_payloads,
    }
    for field, provider_key in (
        ("description", "description"),
        ("cover_url", "cover"),
        ("release_date", "release_date"),
        ("genres", "genres"),
    ):
        selected = selected_by_field.get(field, [])
        if not selected:
            filtered[field] = [] if field == "genres" else ""
            filtered[f"{provider_key}_provider"] = ""
            filtered[f"{provider_key}_provider_id"] = ""
            continue
        first = selected[0]
        filtered[f"{provider_key}_provider"] = str(first.get("provider") or "")
        filtered[f"{provider_key}_provider_id"] = str(first.get("provider_id") or "")
        if field == "genres":
            filtered["genres"] = normalize_subjects(
                [
                    value
                    for source in selected
                    for payload in trusted_payloads
                    if payload.get("provider") == source.get("provider")
                    and str(payload.get("provider_id") or "") == str(source.get("provider_id") or "")
                    and isinstance(payload.get("payload"), dict)
                    for value in payload["payload"].get("genres", [])
                ],
                limit=METADATA_GENRE_LIMIT,
            )

    identity_provider, identity_provider_id = next(iter(verified))
    verified_work_id = (
        book_openlibrary_work_id({"openlibrary_work_id": identity_provider_id})
        if identity_provider == "openlibrary"
        else ""
    )
    evidence = {
        "kind": verified[(identity_provider, identity_provider_id)]["kind"],
        "provider": identity_provider,
        "provider_id": identity_provider_id,
        "isbn_sha256": hashlib.sha256("|".join(isbn_parts(read.get("isbn"))).encode()).hexdigest(),
    }
    return filtered, evidence, verified_work_id


def _read_metadata_attempt_provenance(provenance, *, status, error_type=""):
    """Keep bounded request evidence without caching an unverified book record."""

    traces = provenance.get("fetch_trace", []) if isinstance(provenance, dict) else []
    safe_traces = []
    if isinstance(traces, list):
        for trace in traces[:12]:
            if not isinstance(trace, dict):
                continue
            item = {
                key: str(trace[key])[:100]
                for key in ("provider", "status", "query_kind", "identifier_evidence", "error_type")
                if trace.get(key) is not None
            }
            if isinstance(trace.get("http_status"), int):
                item["http_status"] = trace["http_status"]
            if isinstance(trace.get("matched_count"), int):
                item["matched_count"] = max(0, trace["matched_count"])
            if item:
                safe_traces.append(item)
    fetched_at = provenance.get("fetched_at") if isinstance(provenance, dict) else ""
    result = {
        "fetched_at": str(fetched_at or datetime.now(timezone.utc).isoformat(timespec="seconds"))[:40],
        "attempt_status": str(status)[:60],
        "fetch_trace": safe_traces,
        "source_payloads": [],
    }
    if error_type:
        result["error_type"] = str(error_type)[:100]
    return result


def read_metadata_refresh_remaining(
    retry_days: int = READ_METADATA_RETRY_DAYS,
) -> int:
    """Count rated reads with a valid ISBN due for bounded enrichment."""

    retry_before = datetime.now(timezone.utc) - timedelta(days=max(1, int(retry_days)))
    return sum(
        1 for read in _read_metadata_rows()
        if _read_metadata_attempt_is_due(read, retry_before)
    )


def _read_field_sources(provenance, field):
    fields = provenance.get("fields", {}) if isinstance(provenance, dict) else {}
    info = fields.get(field) if isinstance(fields, dict) else None
    if not isinstance(info, dict):
        return []
    sources = info.get("sources")
    if not isinstance(sources, list):
        sources = [info]
    return [source for source in sources if isinstance(source, dict)]


def _read_description_upgrade_allowed(con, read_id, existing, metadata, description, description_kind):
    """Allow opener-to-synopsis replacement only for the exact cached source."""

    if (
        not existing
        or not existing["description"]
        or existing["description_kind"] != "opening_sentence"
        or description_kind != "synopsis"
        or not description
    ):
        return False

    cached_source = con.execute(
        "SELECT provider,provider_id FROM metadata_field_provenance "
        "WHERE entity_type='read' AND entity_id=? AND field='description' "
        "ORDER BY verified_at DESC LIMIT 1",
        (read_id,),
    ).fetchone()
    if cached_source:
        cached_pair = (str(cached_source["provider"] or ""), str(cached_source["provider_id"] or ""))
    else:
        try:
            prior = json.loads(existing["metadata_provenance"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            prior = {}
        sources = _read_field_sources(prior, "description")
        if not sources:
            return False
        cached_pair = (str(sources[0].get("provider") or ""), str(sources[0].get("provider_id") or ""))
    if not all(cached_pair):
        return False

    provenance = metadata.get("metadata_provenance", {})
    for source in _read_field_sources(provenance, "description"):
        pair = (str(source.get("provider") or ""), str(source.get("provider_id") or ""))
        if pair != cached_pair or str(source.get("kind") or "") != "synopsis":
            continue
        for payload in provenance.get("source_payloads", []) if isinstance(provenance, dict) else []:
            if (
                isinstance(payload, dict)
                and str(payload.get("provider") or "") == pair[0]
                and str(payload.get("provider_id") or "") == pair[1]
                and isinstance(payload.get("payload"), dict)
                and str(payload["payload"].get("opening_sentence") or "") == existing["description"]
            ):
                return True
    return False


def _read_metadata_provenance_for_storage(existing, attempt, fields_added, verified):
    """Retain provenance for cached primitives while recording each attempt."""

    try:
        previous = json.loads(existing["metadata_provenance"] or "{}") if existing else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        previous = {}
    if not isinstance(previous, dict):
        previous = {}
    try:
        previous_evidence = json.loads(existing["identity_evidence"] or "{}") if existing else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        previous_evidence = {}
    has_previous_identity = bool(
        existing
        and str(existing["identity_provider"] or "")
        and str(existing["identity_provider_id"] or "")
        and isinstance(previous_evidence, dict)
        and previous_evidence
    )
    if not verified:
        if has_previous_identity:
            previous["last_attempt"] = attempt
            return previous
        return attempt

    if not has_previous_identity:
        return attempt

    old_fields = previous.get("fields", {})
    if not isinstance(old_fields, dict):
        old_fields = {}
    new_fields = attempt.get("fields", {}) if isinstance(attempt, dict) else {}
    if not isinstance(new_fields, dict):
        new_fields = {}
    stored_fields = dict(old_fields)
    for field, contributed in fields_added.items():
        if contributed and field in new_fields:
            stored_fields[field] = new_fields[field]

    needed_sources = set()
    for field_info in stored_fields.values():
        sources = field_info.get("sources") if isinstance(field_info, dict) else None
        if not isinstance(sources, list):
            sources = [field_info] if isinstance(field_info, dict) else []
        for source in sources:
            if isinstance(source, dict) and source.get("provider") and source.get("provider_id"):
                needed_sources.add((str(source["provider"]), str(source["provider_id"])))
    payload_by_source = {}
    for provenance in (previous, attempt):
        payloads = provenance.get("source_payloads", []) if isinstance(provenance, dict) else []
        if not isinstance(payloads, list):
            continue
        for source in payloads:
            if not isinstance(source, dict):
                continue
            pair = (str(source.get("provider") or ""), str(source.get("provider_id") or ""))
            if pair in needed_sources and isinstance(source.get("payload"), dict):
                payload_by_source[pair] = source

    combined = dict(attempt)
    combined["fields"] = stored_fields
    combined["source_payloads"] = [payload_by_source[pair] for pair in sorted(payload_by_source)]
    combined["attempt_status"] = "verified"
    return combined


async def refresh_read_metadata(
    limit: int = READ_METADATA_BATCH_SIZE,
) -> dict[str, int]:
    """Cache exact-identity public metadata without editing reads or ratings."""

    retry_before = datetime.now(timezone.utc) - timedelta(days=READ_METADATA_RETRY_DAYS)
    due = [
        read for read in _read_metadata_rows()
        if _read_metadata_attempt_is_due(read, retry_before)
    ]
    batch_size = max(1, min(int(limit), READ_METADATA_BATCH_SIZE))
    batch = due[:batch_size]
    if not batch:
        return {"checked": 0, "updated": 0, "lookup_failures": 0, "remaining": 0}

    checked_at = lambda: datetime.now(timezone.utc).isoformat(timespec="microseconds")
    updated = 0
    lookup_failures = 0
    async with metadata_client() as client:
        for read in batch:
            read_isbn13, read_isbn10 = isbn_parts(read.get("isbn"))
            work_id = book_openlibrary_work_id(read)
            metadata = {}
            attempt_provenance = {}
            attempt_error_type = ""
            try:
                if work_id:
                    metadata = await resolve_openlibrary_work_metadata(
                        work_id,
                        title=str(read.get("title") or ""),
                        author=str(read.get("author") or ""),
                        client=client,
                    )
                    attempt_provenance = metadata.get("metadata_provenance", {})
                    traces = metadata.get("metadata_provenance", {}).get("fetch_trace", [])
                    if not any(
                        isinstance(trace, dict)
                        and trace.get("status") == "matched"
                        and trace.get("provider_id") == work_id
                        for trace in traces
                    ):
                        metadata = {}
                else:
                    metadata = await resolve_book_metadata(
                        str(read.get("title") or ""),
                        str(read.get("author") or ""),
                        isbn13=read_isbn13,
                        isbn10=read_isbn10,
                        google_books_api_key=private_setting("association_google_books_api_key", "").strip(),
                        client=client,
                    )
                    attempt_provenance = metadata.get("metadata_provenance", {})
                metadata, identity_evidence, verified_work_id = _verified_read_metadata(metadata, read)
                if identity_evidence:
                    attempt_provenance = {
                        **metadata.get("metadata_provenance", {}),
                        "attempt_status": "verified",
                    }
                else:
                    attempt_provenance = _read_metadata_attempt_provenance(
                        attempt_provenance,
                        status="identity_unverified",
                    )
            except Exception as exc:
                # Provider failures and invalid identity matches receive the
                # same negative-cache window, so one bad record cannot spin.
                lookup_failures += 1
                attempt_error_type = type(exc).__name__
                attempt_provenance = _read_metadata_attempt_provenance(
                    attempt_provenance,
                    status="request_error",
                    error_type=attempt_error_type,
                )
                metadata, identity_evidence, verified_work_id = {}, {}, ""

            identity_hash = _read_metadata_identity_hash(read)
            identity_provider = str(identity_evidence.get("provider") or "")
            identity_provider_id = str(identity_evidence.get("provider_id") or "")
            if identity_evidence or attempt_error_type:
                metadata_provenance = attempt_provenance
            else:
                attempt_status = (
                    "provider_unavailable"
                    if any(
                        isinstance(trace, dict)
                        and trace.get("status") in {"request_error", "http_error", "rate_limited", "server_error", "timeout"}
                        for trace in attempt_provenance.get("fetch_trace", [])
                    )
                    else "no_verified_record"
                )
                metadata_provenance = _read_metadata_attempt_provenance(
                    attempt_provenance,
                    status=attempt_status,
                )
            description = str(metadata.get("description") or "").strip()
            description_kind = str(metadata.get("description_kind") or "")
            genres = normalize_subjects(metadata.get("genres", []), limit=METADATA_GENRE_LIMIT)
            cover_url = safe_cover_url(metadata.get("cover_url", ""))
            release_date = str(metadata.get("release_date") or "")
            date_kind = str(metadata.get("date_kind") or "")

            with transaction() as con:
                current = con.execute("SELECT * FROM reads WHERE id=?", (read["id"],)).fetchone()
                if current is None or _read_metadata_identity_hash(dict(current)) != identity_hash:
                    # A title, author, or ISBN edit landed while fetching.
                    continue
                if book_openlibrary_work_id(dict(current)) != work_id:
                    # Do not attach work-specific metadata after identity
                    # verification changed in another job.
                    continue
                existing = con.execute("SELECT * FROM read_metadata WHERE read_id=?", (read["id"],)).fetchone()
                if existing and existing["identity_hash"] != identity_hash:
                    con.execute(
                        "DELETE FROM metadata_field_provenance WHERE entity_type='read' AND entity_id=?",
                        (read["id"],),
                    )
                    con.execute("DELETE FROM read_metadata WHERE read_id=?", (read["id"],))
                    existing = None

                previous_genres = normalize_subjects(existing["genres"] if existing else [], limit=METADATA_GENRE_LIMIT)
                merged_genres = normalize_subjects(previous_genres + genres, limit=METADATA_GENRE_LIMIT)
                previous_description = existing["description"] if existing else ""
                previous_cover = existing["cover_url"] if existing else ""
                previous_release = existing["release_date"] if existing else ""
                description_upgrade = _read_description_upgrade_allowed(
                    con,
                    int(read["id"]),
                    existing,
                    metadata,
                    description,
                    description_kind,
                )
                new_description = (
                    description
                    if not previous_description or description_upgrade
                    else previous_description
                )
                new_description_kind = (
                    description_kind
                    if not previous_description or description_upgrade
                    else existing["description_kind"]
                )
                new_cover = cover_url if cover_url and (not previous_cover or is_weak_cover_url(previous_cover)) else previous_cover
                new_release = previous_release or release_date
                new_date_kind = existing["date_kind"] if existing and previous_release else date_kind
                fields_added = {
                    "description": bool(description and (not previous_description or description_upgrade)),
                    "genres": merged_genres != previous_genres,
                    "cover_url": bool(new_cover and new_cover != previous_cover),
                    "release_date": bool(release_date and not previous_release),
                }
                try:
                    existing_identity_evidence = json.loads(existing["identity_evidence"] or "{}") if existing else {}
                except (TypeError, ValueError, json.JSONDecodeError):
                    existing_identity_evidence = {}
                preserve_identity = bool(
                    existing
                    and not identity_evidence
                    and existing["identity_provider"]
                    and existing["identity_provider_id"]
                    and isinstance(existing_identity_evidence, dict)
                    and existing_identity_evidence
                )
                stored_identity_provider = (
                    identity_provider if identity_evidence else str(existing["identity_provider"] or "") if preserve_identity else ""
                )
                stored_identity_provider_id = (
                    identity_provider_id if identity_evidence else str(existing["identity_provider_id"] or "") if preserve_identity else ""
                )
                stored_verified_work_id = (
                    verified_work_id if identity_evidence else str(existing["verified_work_id"] or "") if preserve_identity else ""
                )
                stored_identity_evidence = (
                    identity_evidence
                    if identity_evidence
                    else existing_identity_evidence if preserve_identity
                    else {}
                )
                metadata_provenance = _read_metadata_provenance_for_storage(
                    existing,
                    metadata_provenance,
                    fields_added,
                    bool(identity_evidence),
                )
                con.execute(
                    """INSERT INTO read_metadata(
                        read_id,verified_work_id,identity_provider,identity_provider_id,
                        identity_evidence,metadata_provenance,identity_hash,
                        description,description_kind,genres,cover_url,release_date,
                        date_kind,metadata_checked_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(read_id) DO UPDATE SET
                        verified_work_id=excluded.verified_work_id,
                        identity_provider=excluded.identity_provider,
                        identity_provider_id=excluded.identity_provider_id,
                        identity_evidence=excluded.identity_evidence,
                        metadata_provenance=excluded.metadata_provenance,
                        identity_hash=excluded.identity_hash,
                        description=excluded.description,description_kind=excluded.description_kind,
                        genres=excluded.genres,cover_url=excluded.cover_url,
                        release_date=excluded.release_date,date_kind=excluded.date_kind,
                        metadata_checked_at=excluded.metadata_checked_at""",
                    (
                        read["id"], stored_verified_work_id, stored_identity_provider, stored_identity_provider_id,
                        json.dumps(stored_identity_evidence, ensure_ascii=False, separators=(",", ":")),
                        json.dumps(metadata_provenance, ensure_ascii=False, separators=(",", ":")),
                        identity_hash, new_description, new_description_kind,
                        json.dumps(merged_genres, ensure_ascii=False, separators=(",", ":")),
                        new_cover, new_release, new_date_kind, checked_at(),
                    ),
                )
                provenance_item = dict(metadata)
                provenance_item["_metadata_provenance"] = metadata.get("metadata_provenance", {})
                for field, contributed in fields_added.items():
                    if contributed:
                        _persist_metadata_field_provenance(
                            con,
                            "read",
                            int(read["id"]),
                            field,
                            provenance_item,
                            default_provider=identity_provider,
                            default_provider_id=identity_provider_id,
                            default_confidence=1.0,
                        )
                updated += int(any(fields_added.values()))

    return {
        "checked": len(batch),
        "updated": updated,
        "lookup_failures": lookup_failures,
        "remaining": read_metadata_refresh_remaining(),
    }

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


def _nyt_request_url(url: str) -> str:
    """Attach the encrypted Books API key only to the outgoing NYT request."""

    parsed = urlparse(url)
    if (parsed.hostname or "").casefold().rstrip(".") != "api.nytimes.com":
        return url
    api_key = private_setting("nyt_api_key").strip()
    if not api_key:
        return url
    query = [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
             if key.casefold() != "api-key"]
    query.append(("api-key", api_key))
    return parsed._replace(query=urlencode(query)).geturl()


def _clean_parsed_subjects(items):
    return [
        {**item, "genres": normalize_subjects(item.get("genres"), limit=METADATA_GENRE_LIMIT)}
        for item in items
    ]


async def _fetch_and_parse_source(url):
    request_url = _nyt_request_url(url)
    if _is_goodreads_blog_source(url):
        async with _source_client() as client:
            content, content_type = await fetch_bytes(url, client=client)
            items = parse_book_items(content, content_type, url)
            items = await enrich_goodreads_blog_items(items, client)
            return content_type, _clean_parsed_subjects(items)
    content, content_type = await fetch_bytes(request_url)
    return content_type, _clean_parsed_subjects(
        parse_book_items(content, content_type, url)
    )

def validate_goodreads_rss_url(url: str) -> str:
    try:
        parsed = httpx.URL(url)
    except (TypeError, httpx.InvalidURL) as exc:
        raise ValueError("Use a Goodreads read-shelf RSS URL") from exc
    host = parsed.host or ""
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username
        or parsed.password
        or parsed.fragment
        or not (host == "goodreads.com" or host.endswith(".goodreads.com"))
        or not parsed.path.startswith("/review/list_rss/")
    ):
        raise ValueError("Use a Goodreads read-shelf RSS URL")
    shelf = parsed.params.get("shelf")
    if shelf and shelf.casefold() != "read":
        raise ValueError("Use the Goodreads read shelf")
    return str(parsed)


async def import_goodreads_rss(url: str):
    url = validate_goodreads_rss_url(url)
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
    return normalize_subjects(raw_genres, limit=METADATA_GENRE_LIMIT)


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
        "genres": normalize_subjects(item.get("genres"), limit=METADATA_GENRE_LIMIT),
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
            existing["genres"] = normalize_subjects(
                (existing.get("genres") or []) + normalized["genres"], limit=METADATA_GENRE_LIMIT
            )
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
        if (
            _source_matches(source_url, "openlibrary.org")
            and isinstance(payload, dict)
            and isinstance(payload.get("works"), list)
        ):
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
                    google_books_api_key=private_setting("association_google_books_api_key", "").strip(),
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
                    isbn13=item.get("isbn13", ""),
                    isbn10=item.get("isbn10", ""),
                    google_books_api_key=private_setting("association_google_books_api_key", "").strip(),
                    prefer_catalog_synopsis=bool(item.get("_prefer_catalog_synopsis")),
                )
                enriched = {**item, "cover_url": metadata["cover_url"]}
                if not enriched.get("description") and metadata["description"]:
                    enriched["description"] = metadata["description"]
                if not enriched.get("release_date") and metadata["release_date"]:
                    enriched["release_date"] = metadata["release_date"]
                    enriched["date_kind"] = metadata.get("date_kind") or "day"
                elif enriched.get("release_date") and not enriched.get("date_kind"):
                    enriched["date_kind"] = "source"
                enriched["genres"] = normalize_subjects(
                    normalize_subjects(enriched.get("genres", []), limit=METADATA_GENRE_LIMIT)
                    + normalize_subjects(metadata.get("genres", []), limit=METADATA_GENRE_LIMIT),
                    limit=METADATA_GENRE_LIMIT,
                )
                enriched["_metadata_provider"] = metadata.get("provider", "")
                enriched["_metadata_provider_id"] = metadata.get("provider_id", "")
                enriched["_metadata_work_id"] = metadata.get("work_id", "")
                enriched["_metadata_title_match"] = metadata.get("title_match", 0.0)
                enriched["_metadata_author_match"] = metadata.get("author_match", 0.0)
                for field in ("description", "genres", "cover", "release_date"):
                    provider_field = "cover_url" if field == "cover" else field
                    enriched[f"_{provider_field}_provider"] = (
                        metadata.get(f"{field}_provider")
                        or item.get(f"_{provider_field}_provider", "")
                    )
                    enriched[f"_{provider_field}_provider_id"] = (
                        metadata.get(f"{field}_provider_id")
                        or item.get(f"_{provider_field}_provider_id", "")
                    )
                enriched["_description_candidate"] = metadata.get("description_candidate")
                catalog_provenance = metadata.get("metadata_provenance", {})
                prior_provenance = item.get("_metadata_provenance", {})
                if isinstance(prior_provenance, dict) and isinstance(catalog_provenance, dict):
                    fields = dict(prior_provenance.get("fields", {}))
                    fields.update(catalog_provenance.get("fields", {}))
                    payloads = list(prior_provenance.get("source_payloads", []))
                    payload_keys = {
                        (entry.get("provider"), entry.get("provider_id"))
                        for entry in payloads
                        if isinstance(entry, dict)
                    }
                    payloads.extend(
                        entry
                        for entry in catalog_provenance.get("source_payloads", [])
                        if isinstance(entry, dict)
                        and (entry.get("provider"), entry.get("provider_id")) not in payload_keys
                    )
                    enriched["_metadata_provenance"] = {
                        **prior_provenance,
                        **catalog_provenance,
                        "fields": fields,
                        "source_payloads": payloads,
                    }
                else:
                    enriched["_metadata_provenance"] = catalog_provenance or prior_provenance
                return enriched

        return await asyncio.gather(*(enrich(item) for item in items))


def _penguin_random_house_book_url(value):
    """Accept only HTTPS Penguin Random House product pages for detail fetches."""

    try:
        parsed = urlparse(str(value or ""))
        host = (parsed.hostname or "").casefold().rstrip(".")
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and host in PENGUIN_RANDOM_HOUSE_HOSTS
        and port in (None, 443)
        and not parsed.username
        and not parsed.password
        and re.match(r"^/books/\d+(?:/|$)", parsed.path)
    )


def _penguin_random_house_page_metadata(content, source_url):
    """Extract the full description drawer and selected-edition ISBN from PRH."""

    if not _penguin_random_house_book_url(source_url):
        return {}
    soup = BeautifulSoup(content, "html.parser")
    description_node = (
        soup.select_one("#book-description-copy .copy-height")
        or soup.select_one("#book-description-copy")
        or soup.select_one(".book-description-content .drawer-copy-text")
    )
    description = _clean_text(str(description_node), 4000) if description_node else ""
    if not description:
        description_meta = (
            soup.select_one('meta[itemprop="description"]')
            or soup.select_one('meta[property="og:description"]')
            or soup.select_one('meta[name="description"]')
        )
        if description_meta:
            description = _clean_text(description_meta.get("content", ""), 4000)
    isbn_value = ""
    tealium_node = soup.select_one('meta[name="Tealium"]')
    isbn_node = soup.select_one('meta[name="twitter:text:isbn"]')
    if isbn_node:
        isbn_value = isbn_node.get("content", "")
    if not isbn_value and tealium_node:
        isbn_value = tealium_node.get("data-book-isbn", "")
    isbn13, isbn10 = isbn_parts(isbn_value)
    return {
        "title": tealium_node.get("data-book-title", "") if tealium_node else "",
        "author": tealium_node.get("data-book-authors", "") if tealium_node else "",
        "description": description,
        "isbn13": isbn13,
        "isbn10": isbn10,
    }


async def _load_penguin_random_house_product_items(source_url, client=None):
    """Fetch and parse one allowlisted publisher page with its page metadata."""

    if not _penguin_random_house_book_url(source_url):
        return []
    content, content_type = await fetch_bytes(source_url, client=client)
    detail_items = parse_book_items(content, content_type, source_url)
    page_metadata = _penguin_random_house_page_metadata(content, source_url)
    if not detail_items and page_metadata.get("title") and page_metadata.get("author"):
        detail_items = [{**page_metadata, "source_url": source_url}]
    for detail in detail_items:
        for field in ("description", "isbn13", "isbn10"):
            if not detail.get(field) and page_metadata.get(field):
                detail[field] = page_metadata[field]
    return detail_items


def _penguin_random_house_item_matches(item, detail):
    wanted = book_identity_match_keys(item.get("title", ""), item.get("author", ""))
    if not wanted or not (wanted & book_identity_match_keys(detail.get("title", ""), detail.get("author", ""))):
        return False
    supplied_isbn = item.get("isbn13") or item.get("isbn10") or item.get("isbn")
    if not supplied_isbn:
        return True
    returned_isbns = [detail.get("isbn13"), detail.get("isbn10"), detail.get("isbn")]
    wanted13, wanted10 = isbn_parts(supplied_isbn)
    return _returned_identifiers_match_read(returned_isbns, wanted13, wanted10)


async def resolve_penguin_random_house_product_description(item, client=None):
    """Resolve a description from the exact linked PRH product page.

    When the candidate has an ISBN, the product page must return that same
    ISBN as well as a matching title and author. The result carries the field
    provenance needed by private pilot cards and metadata storage.
    """

    source_url = str(item.get("source_url") or "")
    if not (
        _penguin_random_house_book_url(source_url)
        and item.get("title")
        and item.get("author")
    ):
        return {}
    detail_items = await _load_penguin_random_house_product_items(source_url, client=client)
    matches = [
        detail for detail in detail_items
        if _penguin_random_house_item_matches(item, detail)
        and _clean_text(detail.get("description"), 4000)
    ]
    if not matches:
        return {}
    detail = max(
        matches,
        key=lambda value: (
            len(_clean_text(value.get("description"), 4000)),
            bool(value.get("isbn13") or value.get("isbn10")),
        ),
    )
    parsed_url = urlparse(source_url)
    provider_id = f"https://{parsed_url.netloc}{parsed_url.path}"
    description = _clean_text(detail.get("description"), 4000)
    identifiers = [
        value for value in (detail.get("isbn13"), detail.get("isbn10")) if value
    ]
    payload = {
        "title": str(detail.get("title") or item.get("title") or "")[:500],
        "authors": [str(detail.get("author") or item.get("author") or "")[:300]],
        "description": description,
        "identifiers": identifiers,
    }
    return {
        "description": description,
        "isbn13": str(detail.get("isbn13") or ""),
        "isbn10": str(detail.get("isbn10") or ""),
        "provider": "penguinrandomhouse",
        "provider_id": provider_id,
        "kind": "publisher_product",
        "source_field": "description",
        "source_payload": payload,
    }


async def enrich_penguin_random_house_items(items):
    """Follow PRH product links for publisher descriptions and ISBNs.

    The new-releases page lists a book and author but leaves the full
    description and ISBN on the linked product page. ``parse_book_items``
    reads the product's structured identity; this adds a bounded,
    host-restricted detail fetch for the description drawer and only merges
    metadata when the page identifies the same book and author.
    """

    if not items:
        return []
    linked_items = [
        item
        for item in items
        if not _clean_text(item.get("description"), 4000)
        and item.get("title")
        and item.get("author")
        and _penguin_random_house_book_url(item.get("source_url"))
    ]
    if not linked_items:
        return items
    book_keys = list(
        dict.fromkeys(
            normalize_key(item.get("title", ""), item.get("author", ""))
            for item in linked_items
            if item.get("title")
        )
    )
    existing_descriptions = {}
    if book_keys:
        placeholders = ",".join("?" for _ in book_keys)
        existing_descriptions = {
            record["normalized_key"]: record.get("description", "")
            for record in rows(
                "SELECT normalized_key,description FROM candidates "
                f"WHERE normalized_key IN ({placeholders})",
                tuple(book_keys),
            )
        }
    linked_items = [
        item
        for item in linked_items
        if not _clean_text(
            existing_descriptions.get(
                normalize_key(item.get("title", ""), item.get("author", ""))
            ),
            4000,
        )
    ]
    if not linked_items:
        return items

    semaphore = asyncio.Semaphore(8)
    page_tasks = {}
    async with _source_client() as client:
        async def load_product_page(url):
            async with semaphore:
                try:
                    return await _load_penguin_random_house_product_items(url, client=client)
                except Exception:
                    # A single unavailable product page must not fail its feed.
                    return []

        async def enrich(item):
            if _clean_text(item.get("description"), 4000):
                return item
            key = normalize_key(item.get("title", ""), item.get("author", ""))
            if _clean_text(existing_descriptions.get(key), 4000):
                return item
            url = str(item.get("source_url") or "")
            if not _penguin_random_house_book_url(url):
                return item
            task = page_tasks.get(url)
            if task is None:
                task = asyncio.create_task(load_product_page(url))
                page_tasks[url] = task
            detail_items = await task
            matches = [
                detail
                for detail in detail_items
                if _penguin_random_house_item_matches(item, detail)
            ]
            if not matches:
                return item
            detail = max(
                matches,
                key=lambda value: (
                    bool(_clean_text(value.get("description"), 4000)),
                    len(_clean_text(value.get("description"), 4000)),
                    bool(value.get("isbn13") or value.get("isbn10")),
                ),
            )
            enriched = dict(item)
            publisher_fields = {}
            for field in (
                "description",
                "cover_url",
                "isbn13",
                "isbn10",
                "release_date",
                "date_kind",
            ):
                if not enriched.get(field) and detail.get(field):
                    enriched[field] = detail[field]
                    if field == "description":
                        publisher_fields[field] = detail[field]
            enriched["genres"] = normalize_subjects(
                normalize_subjects(enriched.get("genres", []), limit=METADATA_GENRE_LIMIT)
                + normalize_subjects(detail.get("genres", []), limit=METADATA_GENRE_LIMIT),
                limit=METADATA_GENRE_LIMIT,
            )
            if publisher_fields:
                parsed_url = urlparse(url)
                provider_id = f"https://{parsed_url.netloc}{parsed_url.path}"
                payload = {
                    "title": str(detail.get("title") or item.get("title") or "")[:500],
                    "authors": [str(detail.get("author") or item.get("author") or "")[:300]],
                    "description": str(publisher_fields["description"])[:4000],
                    "identifiers": [
                        value for value in (detail.get("isbn13"), detail.get("isbn10")) if value
                    ],
                }
                provenance = dict(enriched.get("_metadata_provenance") or {})
                fields = dict(provenance.get("fields", {}))
                fields["description"] = {
                    "provider": "penguinrandomhouse",
                    "provider_id": provider_id,
                    "kind": "publisher_product",
                    "source_field": "description",
                }
                payloads = list(provenance.get("source_payloads", []))
                payloads.append({
                    "provider": "penguinrandomhouse",
                    "provider_id": provider_id,
                    "payload": payload,
                })
                enriched["_description_provider"] = "penguinrandomhouse"
                enriched["_description_provider_id"] = provider_id
                enriched["_metadata_provenance"] = {
                    **provenance,
                    "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "fields": fields,
                    "source_payloads": payloads,
                }
            return enriched

        return await asyncio.gather(*(enrich(item) for item in items))


def normalize_stored_candidate_subjects() -> int:
    """Clean legacy source tags and keep stored subjects normalized."""

    candidates = rows("SELECT id,genres FROM candidates")
    updates = []
    for candidate in candidates:
        normalized = json.dumps(
            normalize_subjects(candidate.get("genres"), limit=METADATA_GENRE_LIMIT),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if normalized != (candidate.get("genres") or "[]"):
            updates.append((normalized, candidate["id"]))
    if updates:
        with transaction() as con:
            con.executemany(
                "UPDATE candidates SET genres=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                updates,
            )
    return len(updates)


def normalize_stored_candidate_catalog_dates() -> int:
    """Canonicalize legacy catalog dates without inventing field provenance."""

    candidates = rows(
        "SELECT id,release_date,date_kind FROM candidates "
        "WHERE date_kind='catalog' AND release_date IS NOT NULL AND TRIM(release_date)!=''"
    )
    updates = []
    for candidate in candidates:
        canonical_date, precision = _publication_date(candidate.get("release_date"))
        if not canonical_date or not precision:
            # Keep malformed legacy text visible for manual/source review.
            continue
        try:
            parsed_date = date.fromisoformat(canonical_date)
        except ValueError:
            # In particular, reject year zero even though a year-only catalog
            # value can otherwise be expanded into an ISO-looking string.
            continue
        if not 1 <= parsed_date.year <= 9999:
            continue
        if (canonical_date, precision) != (candidate.get("release_date"), candidate.get("date_kind")):
            updates.append((canonical_date, precision, candidate["id"]))
    if updates:
        with transaction() as con:
            con.executemany(
                "UPDATE candidates SET release_date=?,date_kind=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                updates,
            )
    return len(updates)


def _persist_metadata_field_provenance(
    con,
    entity_type,
    entity_id,
    field,
    item,
    *,
    default_provider="",
    default_provider_id="",
    default_confidence=0.0,
):
    return persist_metadata_field_provenance(
        con,
        entity_type,
        entity_id,
        field,
        item,
        default_provider=default_provider,
        default_provider_id=default_provider_id,
        default_confidence=default_confidence,
    )


def candidate_metadata_refresh_remaining(
    retry_days: int = CANDIDATE_METADATA_RETRY_DAYS,
) -> int:
    """Count accepted candidates due for a sparse-field or version refresh."""

    retry_before = (
        datetime.now(timezone.utc) - timedelta(days=max(1, int(retry_days)))
    ).isoformat(timespec="seconds")
    result = row(
        """SELECT COUNT(*) AS count FROM candidates c
        JOIN candidate_quality q ON q.candidate_id=c.id
        WHERE c.status!='rejected' AND q.quality_status='accepted'
          AND (
            q.metadata_version!=?
            OR c.cover_url='' OR c.cover_url LIKE '%/b/isbn/%'
            OR c.cover_url LIKE 'https://placehold.co/%'
            OR c.description='' OR c.release_date IS NULL
            OR CASE WHEN json_valid(c.genres) THEN json_array_length(c.genres) ELSE 0 END=0
          )
          AND (
            q.metadata_version!=?
            OR q.metadata_checked_at IS NULL
            OR q.metadata_checked_at<=?
          )""",
        (METADATA_NORMALIZATION_VERSION, METADATA_NORMALIZATION_VERSION, retry_before),
    )
    return int(result["count"] if result else 0)


async def refresh_missing_candidate_metadata():
    """Backfill sparse metadata for catalog-accepted candidates only."""

    normalize_stored_candidate_subjects()
    normalized_catalog_dates = normalize_stored_candidate_catalog_dates()

    candidates = rows(
        "SELECT c.id,c.title,c.author,c.description,c.cover_url,c.source_url,c.release_date,c.date_kind,c.genres,"
        "q.provider AS _expected_provider,q.provider_id AS _expected_provider_id,"
        "q.metadata_provider AS _legacy_metadata_provider,"
        "q.metadata_provider_id AS _legacy_metadata_provider_id,"
        "q.metadata_version AS _metadata_version "
        "FROM candidates c JOIN candidate_quality q ON q.candidate_id=c.id "
        "WHERE c.status!='rejected' AND q.quality_status='accepted' AND ("
        "q.metadata_version!=? OR cover_url='' OR cover_url LIKE '%/b/isbn/%' "
        "OR cover_url LIKE 'https://placehold.co/%' OR description='' OR release_date IS NULL "
        "OR CASE WHEN json_valid(genres) THEN json_array_length(genres) ELSE 0 END=0) "
        "AND (q.metadata_version!=? OR q.metadata_checked_at IS NULL OR q.metadata_checked_at<=?) "
        "ORDER BY CASE WHEN q.metadata_version!=? THEN 0 ELSE 1 END,"
        "q.metadata_checked_at ASC,c.score DESC,c.id LIMIT ?",
        (
            METADATA_NORMALIZATION_VERSION,
            METADATA_NORMALIZATION_VERSION,
            (datetime.now(timezone.utc) - timedelta(days=CANDIDATE_METADATA_RETRY_DAYS)).isoformat(timespec="seconds"),
            METADATA_NORMALIZATION_VERSION,
            CANDIDATE_METADATA_BATCH_SIZE,
        ),
    )
    if not candidates:
        return normalized_catalog_dates
    inputs = []
    for candidate in candidates:
        item = {**candidate, "genres": _stored_genre_values(candidate.get("genres"))}
        has_legacy_description = bool(
            candidate.get("description")
            and candidate.get("_legacy_metadata_provider")
            and not row(
                "SELECT 1 FROM metadata_field_provenance "
                "WHERE entity_type='candidate' AND entity_id=? AND field='description' LIMIT 1",
                (candidate["id"],),
            )
        )
        item["_legacy_description_upgrade"] = has_legacy_description
        if has_legacy_description:
            item["_expected_provider"] = candidate.get("_legacy_metadata_provider") or ""
            item["_expected_provider_id"] = candidate.get("_legacy_metadata_provider_id") or ""
            item["_prefer_catalog_synopsis"] = True
        inputs.append(item)
    enriched = await enrich_book_metadata(inputs)
    changed = 0
    with transaction() as con:
        for previous, item in zip(candidates, enriched):
            description = item.get("description", "")
            description_upgrade = False
            candidate_description = item.get("_description_candidate")
            if (
                previous.get("description")
                and item.get("_legacy_description_upgrade")
                and isinstance(candidate_description, dict)
            ):
                description_upgrade = bool(
                    candidate_description.get("kind") == "synopsis"
                    and candidate_description.get("opening_sentence") == previous.get("description")
                    and candidate_description.get("provider") == previous.get("_legacy_metadata_provider")
                    and candidate_description.get("provider_id") == previous.get("_legacy_metadata_provider_id")
                    and candidate_description.get("text")
                )
                if description_upgrade:
                    description = str(candidate_description["text"])
            cover_url = item.get("cover_url", "")
            release_date = item.get("release_date") or None
            date_kind = item.get("date_kind") or previous.get("date_kind") or "unknown"
            previous_genres = _stored_genre_values(previous.get("genres", []))
            item_genres = normalize_subjects(item.get("genres", []), limit=METADATA_GENRE_LIMIT)
            genres = normalize_subjects(previous_genres + item_genres, limit=METADATA_GENRE_LIMIT)
            genres_json = json.dumps(genres, ensure_ascii=False, separators=(",", ":"))
            metadata_provider = str(item.get("_metadata_provider", "") or "")
            metadata_provider_id = str(item.get("_metadata_provider_id", "") or "")
            cover_changed = bool(
                cover_url
                and not is_weak_cover_url(cover_url)
                and (
                    not previous.get("cover_url")
                    or is_weak_cover_url(previous.get("cover_url"))
                    or cover_url != previous.get("cover_url")
                )
            )
            description_changed = bool(description and (not previous.get("description") or description_upgrade))
            release_changed = bool(release_date and not previous.get("release_date"))
            genres_changed = len(genres) > len(previous_genres)
            if description_changed or cover_changed or release_changed or genres_changed:
                changed += 1
            con.execute(
                """UPDATE candidates SET
                description=CASE WHEN (description='' AND ?!='') OR ?=1 THEN ? ELSE description END,
                cover_url=CASE WHEN ?=1 THEN ? ELSE cover_url END,
                release_date=COALESCE(release_date, ?),
                date_kind=CASE WHEN release_date IS NULL AND ? IS NOT NULL THEN ? ELSE date_kind END,
                genres=CASE WHEN ?!='[]' THEN ? ELSE genres END,
                updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (
                    description,
                    int(description_upgrade),
                    description,
                    int(cover_changed),
                    cover_url,
                    release_date,
                    release_date,
                    date_kind,
                    genres_json,
                    genres_json,
                    item["id"],
                ),
            )
            for field, contributed in (
                ("description", description_changed),
                ("cover_url", cover_changed),
                ("release_date", release_changed),
                ("genres", genres_changed),
            ):
                if contributed:
                    _persist_metadata_field_provenance(
                        con,
                        "candidate",
                        int(item["id"]),
                        field,
                        item,
                        default_provider=metadata_provider or "catalog",
                        default_provider_id=metadata_provider_id,
                        default_confidence=1.0,
                    )
            con.execute(
                """UPDATE candidate_quality SET metadata_checked_at=?,metadata_version=?,
                    metadata_provider=CASE WHEN ?=1 AND ?!='' THEN ? ELSE metadata_provider END,
                    metadata_provider_id=CASE WHEN ?=1 AND ?!='' THEN ? ELSE metadata_provider_id END,
                    updated_at=CURRENT_TIMESTAMP
                    WHERE candidate_id=? AND quality_status='accepted'""",
                (
                    datetime.now(timezone.utc).isoformat(timespec="microseconds"),
                    METADATA_NORMALIZATION_VERSION,
                    int(description_changed),
                    metadata_provider,
                    metadata_provider,
                    int(description_changed),
                    metadata_provider_id,
                    metadata_provider_id,
                    item["id"],
                )
            )
    return changed + normalized_catalog_dates


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
    configured_filters = normalize_source_filters(source.get("filters"))
    cleaned = _clean_parsed_subjects(items)
    if configured_filters["include_genres"]:
        # Keep untagged rows in the candidate pool until the catalog has had a
        # chance to supply subjects. Known non-matches can still be removed
        # before the expensive provider pass.
        possible = [
            item for item in cleaned
            if not item.get("genres")
            or (
                any(
                    _genre_matches(wanted, genre)
                    for wanted in configured_filters["include_genres"]
                    for genre in item.get("genres", [])
                )
                and not any(
                    _genre_matches(blocked, genre)
                    for blocked in configured_filters["exclude_genres"]
                    for genre in item.get("genres", [])
                )
            )
        ]
    else:
        possible = filter_source_items(cleaned, configured_filters)
    for item in possible:
        item["_source_provided_fields"] = [
            field for field in ("description", "cover_url", "release_date", "genres")
            if item.get(field)
        ]
        item["_source_provider_id"] = str(source["id"])
    enrichment_pool = (
        sorted(possible, key=lambda item: bool(item.get("genres")))
        if configured_filters["include_genres"]
        else possible
    )
    publisher_batch = await enrich_penguin_random_house_items(
        enrichment_pool[:SOURCE_METADATA_ENRICHMENT_BATCH_SIZE]
    )
    publisher_by_key = {
        normalize_key(item.get("title", ""), item.get("author", "Unknown author")): item
        for item in publisher_batch
    }
    enrichment_batch = [
        publisher_by_key.get(
            normalize_key(item.get("title", ""), item.get("author", "Unknown author")),
            item,
        )
        for item in enrichment_pool[:SOURCE_METADATA_ENRICHMENT_BATCH_SIZE]
    ]
    enriched_batch = await enrich_book_metadata(_clean_parsed_subjects(enrichment_batch))
    enriched_by_key = {
        normalize_key(item.get("title", ""), item.get("author", "Unknown author")): item
        for item in enriched_batch
    }
    items = [
        enriched_by_key.get(
            normalize_key(item.get("title", ""), item.get("author", "Unknown author")),
            item,
        )
        for item in possible
    ]
    items = filter_source_items(_clean_parsed_subjects(items), configured_filters)
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
                "SELECT id,isbn13,isbn10,description,cover_url,release_date,genres FROM candidates WHERE normalized_key=?",
                (key,),
            ).fetchone()
            genres = normalize_subjects(
                _stored_genre_values(existing["genres"] if existing else [])
                + normalize_subjects(item.get("genres", []), limit=METADATA_GENRE_LIMIT),
                limit=METADATA_GENRE_LIMIT,
            )
            genres_json = json.dumps(genres, ensure_ascii=False, separators=(",", ":"))
            con.execute("""INSERT INTO candidates(title,author,description,cover_url,source_url,source_id,release_date,date_kind,genres,isbn13,isbn10,normalized_key)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(normalized_key) DO UPDATE SET
            description=CASE WHEN description='' AND excluded.description!='' THEN excluded.description ELSE description END,
            cover_url=CASE WHEN length(excluded.cover_url)>0 AND excluded.cover_url NOT LIKE 'https://placehold.co/%' AND excluded.cover_url NOT LIKE '%/b/isbn/%' THEN excluded.cover_url ELSE cover_url END,
            source_url=CASE WHEN length(excluded.source_url)>0 THEN excluded.source_url ELSE source_url END,
            release_date=COALESCE(excluded.release_date, release_date),
            date_kind=CASE WHEN excluded.release_date IS NOT NULL AND release_date IS NULL THEN excluded.date_kind ELSE date_kind END,
            genres=CASE WHEN excluded.genres!='[]' THEN excluded.genres ELSE genres END,
            isbn13=CASE WHEN isbn13='' AND excluded.isbn13!='' THEN excluded.isbn13 ELSE isbn13 END,
            isbn10=CASE WHEN isbn10='' AND excluded.isbn10!='' THEN excluded.isbn10 ELSE isbn10 END,
            updated_at=CURRENT_TIMESTAMP""", (item["title"], item.get("author", "Unknown author"), item.get("description", ""), item.get("cover_url", ""), item.get("source_url", source["url"]), source["id"], item.get("release_date"), item.get("date_kind", "source"), genres_json, isbn13, isbn10, key))
            candidate = con.execute(
                "SELECT id,isbn13,isbn10,description,cover_url,release_date,genres FROM candidates WHERE normalized_key=?",
                (key,),
            ).fetchone()
            if candidate:
                description_added = bool(
                    item.get("description")
                    and not (existing["description"] if existing else "")
                    and candidate["description"] == item.get("description")
                )
                cover_added = bool(
                    item.get("cover_url")
                    and not is_weak_cover_url(item.get("cover_url"))
                    and candidate["cover_url"] == item.get("cover_url")
                    and (not existing or existing["cover_url"] != candidate["cover_url"])
                )
                release_added = bool(
                    item.get("release_date")
                    and not (existing["release_date"] if existing else "")
                    and candidate["release_date"] == item.get("release_date")
                )
                previous_genres = _stored_genre_values(existing["genres"] if existing else [])
                genres_added = len(_stored_genre_values(candidate["genres"])) > len(previous_genres)
                source_fields = set(item.get("_source_provided_fields", []))
                source_provenance = {
                    "metadata_provenance": {
                        "fields": {
                            field: {
                                "provider": "source",
                                "provider_id": str(source["id"]),
                                "kind": "source_item",
                            }
                            for field in source_fields
                        },
                        "source_payloads": [],
                    }
                }
                contributed = {
                    "description": description_added,
                    "cover_url": cover_added,
                    "release_date": release_added,
                    "genres": genres_added,
                }
                for field, applied in contributed.items():
                    if not applied:
                        continue
                    source_provided = field in source_fields
                    if source_provided:
                        _persist_metadata_field_provenance(
                            con,
                            "candidate",
                            int(candidate["id"]),
                            field,
                            source_provenance,
                            default_provider="source",
                            default_provider_id=str(source["id"]),
                            default_confidence=1.0,
                        )
                    _persist_metadata_field_provenance(
                        con,
                        "candidate",
                        int(candidate["id"]),
                        field,
                        item,
                    )
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
