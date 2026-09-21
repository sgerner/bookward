import asyncio
import csv
import html
import io
import json
import re
import math
from datetime import datetime
from urllib.parse import urlparse
import feedparser
import httpx
from bs4 import BeautifulSoup
from .config import settings
from .covers import is_weak_cover_url, metadata_client, resolve_book_metadata, resolve_cover_url, safe_cover_url
from .database import normalize_key, rows, transaction
from .security import resolve_public_target

GOODREADS_TIP_RE = re.compile(
    r'''new\s+Tip\(\$\('(?P<id>bookCover[^']+)'\),\s*"(?P<body>(?:\\.|[^"\\])*)"\s*,''',
    re.S,
)
GOODREADS_TITLE_RE = re.compile(r'class=\\"readable bookTitle\\"[^>]*>(.*?)<\\/a>', re.S)
GOODREADS_AUTHOR_RE = re.compile(r'class=\\"authorName\\"[^>]*>(.*?)<\\/a>', re.S)
GOODREADS_LINK_RE = re.compile(r'href=\\"(https://www\.goodreads\.com/book/show/[^"?]+)', re.S)
GOODREADS_YEAR_RE = re.compile(r'(?:published|release date:)\s+(\d{4})', re.I)


def _clean_text(value, limit=4000):
    text = BeautifulSoup(html.unescape(str(value or "")), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _date_value(value):
    raw = str(value or "").strip()
    if not raw:
        return None
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
            items.append({
                "title": title[:500],
                "author": author[:300],
                "description": _clean_text(summary),
                "cover_url": _apple_cover(entry, summary, source_url),
                "source_url": link,
                "release_date": _date_value(release),
                "genres": genres[:8],
            })
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
        items.append({
            "title": title[:500],
            "author": author[:300],
            "description": _clean_text(description),
            "cover_url": safe_cover_url(f"https://covers.openlibrary.org/b/id/{int(cover_id)}-L.jpg", source_url) if str(cover_id).isdigit() else "",
            "source_url": source,
            "release_date": None,
            "genres": [str(subject)[:80] for subject in (work.get("subject") or [])[:8] if subject],
        })
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
        items.append({
            "title": title[:500],
            "author": author[:300],
            "description": _clean_text(book.get("description", "")),
            "cover_url": safe_cover_url(book.get("book_image", ""), source_url),
            "source_url": metadata_url(book.get("amazon_product_url"), source_url),
            "release_date": _date_value(book.get("published_date")),
            "genres": [str(book.get("list_name", "")).strip()] if book.get("list_name") else [],
        })
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


def _is_apple_source(source_url):
    """Recognize both legacy iTunes and current Apple RSS endpoints."""

    return any(
        _source_matches(source_url, host)
        for host in ("itunes.apple.com", "rss.marketingtools.apple.com")
    )


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

def metadata_url(value, fallback=""):
    try:
        parsed = httpx.URL(str(value))
        return str(parsed) if parsed.scheme in ("http","https") and parsed.host else fallback
    except Exception: return fallback

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

async def fetch_bytes(url: str, allow_goodreads_http=False):
    _, address, hostname = resolve_public_target(url, allow_http=allow_goodreads_http)
    original = httpx.URL(url)
    pinned = original.copy_with(host=address)
    default_port = 443 if original.scheme == "https" else 80
    host_header = hostname if original.port in (None, default_port) else f"{hostname}:{original.port}"
    async with httpx.AsyncClient(timeout=settings.source_timeout_seconds, follow_redirects=False, trust_env=False, headers={"User-Agent":"Bookward/0.1 (+self-hosted book recommender)","Accept-Encoding":"identity"}) as client:
        async with client.stream("GET", pinned, headers={"Host":host_header}, extensions={"sni_hostname":hostname}) as response:
            if response.is_redirect: raise ValueError("Redirects are disabled; use the final HTTPS URL")
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").lower()
            if not any(kind in content_type for kind in ("html","xml","rss","atom","json","javascript","text/plain")): raise ValueError("Source returned an unsupported content type")
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > settings.source_max_bytes: raise ValueError("Source response is too large")
                chunks.append(chunk)
            return b"".join(chunks), content_type

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

def parse_book_items(content: bytes, content_type: str, source_url: str):
    if "xml" in content_type or "rss" in content_type or content.lstrip().startswith(b"<?xml"):
        feed = feedparser.parse(content)
        if _is_apple_source(source_url):
            return _parse_apple_entries(feed.entries, source_url)
        return [{"title": str(e.get("title", ""))[:500], "author": str(e.get("author", "Unknown author"))[:300], "description": BeautifulSoup(str(e.get("summary", "")), "html.parser").get_text(" ")[:4000], "source_url": metadata_url(e.get("link"), source_url)} for e in feed.entries[:settings.source_max_items] if e.get("title")]
    items = []
    payloads = []
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
        if _source_matches(source_url, "goodreads.com", "/genres/") or _source_matches(source_url, "www.goodreads.com", "/genres/"):
            return _parse_goodreads_genre(content, source_url)
        soup = BeautifulSoup(content, "html.parser")
        for node in soup.select('script[type="application/ld+json"]'):
            try: payloads.append(json.loads(node.string or "null"))
            except json.JSONDecodeError: continue
    for payload in payloads:
        values = payload if isinstance(payload, list) else [payload]
        values = [child for value in values for child in (value.get("@graph", []) if isinstance(value, dict) and isinstance(value.get("@graph"), list) else [value])]
        for value in values:
            kinds = value.get("@type", []) if isinstance(value, dict) else []
            kinds = [kinds] if isinstance(kinds, str) else kinds
            if isinstance(value, dict) and any(kind in ("Book", "Audiobook") for kind in kinds):
                author = value.get("author", "Unknown author")
                if isinstance(author, list): author = author[0] if author else "Unknown author"
                if isinstance(author, dict): author = author.get("name", "Unknown author")
                image = value.get("image", "")
                if isinstance(image, dict): image = image.get("url", "")
                if isinstance(image, list): image = image[0] if image else ""
                raw_date = str(value.get("datePublished") or "")[:10]
                release_date = raw_date if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_date) else None
                items.append({"title": str(value.get("name", ""))[:500], "author": str(author)[:300], "description": BeautifulSoup(str(value.get("description", "")), "html.parser").get_text(" ")[:4000], "cover_url": safe_cover_url(image, source_url), "source_url": source_url, "release_date": release_date})
    return [item for item in items[:settings.source_max_items] if item["title"]]


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
                    client=client,
                    lookup_cache=lookup_cache,
                )
                enriched = {**item, "cover_url": metadata["cover_url"]}
                if not enriched.get("description") and metadata["description"]:
                    enriched["description"] = metadata["description"]
                if not enriched.get("release_date") and metadata["release_date"]:
                    enriched["release_date"] = metadata["release_date"]
                    enriched["date_kind"] = metadata.get("date_kind") or "day"
                elif enriched.get("release_date") and not enriched.get("date_kind"):
                    enriched["date_kind"] = "source"
                return enriched

        return await asyncio.gather(*(enrich(item) for item in items))


async def refresh_missing_candidate_metadata():
    """Backfill artwork, summaries, and dates for older candidates."""

    candidates = rows(
        "SELECT id,title,author,description,cover_url,source_url,release_date,date_kind "
        "FROM candidates WHERE status!='rejected' AND ("
        "cover_url='' OR cover_url LIKE '%/b/isbn/%' OR description='' OR release_date IS NULL) "
        "ORDER BY id LIMIT 50"
    )
    if not candidates:
        return 0
    enriched = await enrich_book_metadata(candidates)
    changed = 0
    with transaction() as con:
        for previous, item in zip(candidates, enriched):
            description = item.get("description", "")
            cover_url = item.get("cover_url", "")
            release_date = item.get("release_date") or None
            date_kind = item.get("date_kind") or previous.get("date_kind") or "unknown"
            if (
                (description and len(description) > len(previous.get("description", "")))
                or (cover_url and (not previous.get("cover_url") or is_weak_cover_url(previous.get("cover_url"))))
                or (release_date and not previous.get("release_date"))
            ):
                changed += 1
            con.execute(
                """UPDATE candidates SET
                description=CASE WHEN length(?) > length(description) THEN ? ELSE description END,
                cover_url=CASE WHEN (cover_url='' OR cover_url LIKE '%/b/isbn/%') AND ?!='' THEN ? ELSE cover_url END,
                release_date=COALESCE(release_date, ?),
                date_kind=CASE WHEN release_date IS NULL AND ? IS NOT NULL THEN ? ELSE date_kind END,
                updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (description, description, cover_url, cover_url, release_date, release_date, date_kind, item["id"]),
            )
    return changed


async def refresh_missing_candidate_covers():
    """Compatibility wrapper for callers that previously backfilled covers."""

    return await refresh_missing_candidate_metadata()

async def preview_source(url):
    content, content_type = await fetch_bytes(url)
    items = parse_book_items(content, content_type, url)
    return {"url": url, "content_type": content_type, "count": len(items), "sample": items[:5]}

async def scan_source(source):
    content, content_type = await fetch_bytes(source["url"])
    items = parse_book_items(content, content_type, source["url"])
    items = await enrich_book_metadata(items)
    with transaction() as con:
        seen = []
        for item in items:
            key = normalize_key(item["title"], item.get("author", "Unknown author"))
            seen.append(key)
            con.execute("""INSERT INTO candidates(title,author,description,cover_url,source_url,source_id,release_date,date_kind,genres,normalized_key)
            VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(normalized_key) DO UPDATE SET
            description=CASE WHEN length(excluded.description)>length(description) THEN excluded.description ELSE description END,
            cover_url=CASE WHEN length(excluded.cover_url)>0 THEN excluded.cover_url ELSE cover_url END,
            source_url=CASE WHEN length(excluded.source_url)>0 THEN excluded.source_url ELSE source_url END,
            release_date=COALESCE(excluded.release_date, release_date),
            date_kind=CASE WHEN excluded.release_date IS NOT NULL AND release_date IS NULL THEN excluded.date_kind ELSE date_kind END,
            genres=CASE WHEN excluded.genres!='[]' THEN excluded.genres ELSE genres END,
            updated_at=CURRENT_TIMESTAMP""", (item["title"], item.get("author", "Unknown author"), item.get("description", ""), item.get("cover_url", ""), item.get("source_url", source["url"]), source["id"], item.get("release_date"), item.get("date_kind", "source"), json.dumps(item.get("genres", [])), key))
        if seen:
            placeholders = ",".join("?" for _ in seen)
            con.execute(f"DELETE FROM candidates WHERE source_id=? AND status IN ('new','recommended') AND normalized_key NOT IN ({placeholders})", (source["id"], *seen))
        # An empty response can be a transient block page, parser mismatch,
        # or upstream outage. It is not safe to interpret it as proof that a
        # source no longer contains any books, so retain existing candidates.
        status = f"ok:{len(items)}" if items else "empty:0"
        con.execute("UPDATE sources SET last_status=?, last_scanned_at=CURRENT_TIMESTAMP WHERE id=?", (status, source["id"]))
    return len(items)
