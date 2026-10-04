"""Open Library list-graph association provider.

Open Library does not expose a direct "similar books" endpoint.  This adapter
uses works that share reader-created lists with a highly rated seed.  Requests
are bounded, identified, rate-limited, and cached; returned edges are evidence
only and do not change the visible recommendation score.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence
from urllib.parse import urlencode, urlsplit

import httpx

from ..associations import (
    Association,
    AssociationCache,
    OPEN_LIBRARY_LISTS_PROVIDER,
    RateLimiter,
    canonical_isbn,
    record_association_request,
)
from ..config import settings
from ..identity import ReadIdentityIndex, book_identity
from ..isbn import isbn_parts
from ..security import resolve_public_target


OPEN_LIBRARY_BASE = "https://openlibrary.org"
OPEN_LIBRARY_USER_AGENT = "Bookward/0.1 (+self-hosted book recommender)"
_OPEN_LIBRARY_BOOK_KEY = re.compile(r"^/(works|books)/(OL\d+[WM])/?$", re.IGNORECASE)


def _title_key(value: object) -> str:
    return book_identity(str(value or ""), "").split("\x1f", 1)[0]


def _clean(value: object, limit: int = 500) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


def _authors(value: object) -> list[str]:
    if isinstance(value, str):
        return [_clean(value, 300)] if _clean(value, 300) else []
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for entry in value:
        if isinstance(entry, str):
            name = _clean(entry, 300)
        elif isinstance(entry, Mapping):
            name = _clean(entry.get("name") or entry.get("author_name"), 300)
        else:
            name = ""
        if name and name not in names:
            names.append(name)
    return names


def _entry_authors(entry: Mapping[str, Any]) -> list[str]:
    names: list[str] = []
    for key in ("author_name", "authors", "author"):
        for name in _authors(entry.get(key)):
            if name not in names:
                names.append(name)
    if not names:
        statement = _clean(entry.get("by_statement"), 300)
        if statement:
            lower = statement.casefold()
            if lower.startswith("by "):
                names.append(statement[3:].strip())
    return names


def _entry_isbn(entry: Mapping[str, Any]) -> str:
    values = []
    for field in ("isbn", "isbn13", "isbn10", "isbn_13", "isbn_10"):
        value = entry.get(field)
        if isinstance(value, Mapping):
            value = value.get("identifier")
        if value:
            values.append(value)
    isbn13, isbn10 = isbn_parts(values)
    return isbn13 or isbn10


def _external_key(entry: Mapping[str, Any]) -> str:
    for key in ("work_key", "key", "work", "edition_key", "url"):
        value = entry.get(key)
        if isinstance(value, Mapping):
            value = value.get("key") or value.get("url")
        if isinstance(value, list):
            value = value[0] if value else ""
        normalized = _openlibrary_book_key(value)
        if normalized:
            return normalized
    for work in entry.get("works", []) if isinstance(entry.get("works"), list) else []:
        if isinstance(work, Mapping):
            normalized = _openlibrary_book_key(work.get("key"))
            if normalized:
                return normalized
    return ""


def _openlibrary_book_key(value: object) -> str:
    """Normalize a work or edition key, rejecting list and unrelated URLs."""

    candidate = _clean(value, 500)
    if not candidate:
        return ""
    if candidate.startswith(("http://", "https://")):
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            return ""
        if (parsed.hostname or "").casefold().rstrip(".") not in {
            "openlibrary.org",
            "www.openlibrary.org",
        }:
            return ""
        candidate = parsed.path
    elif candidate.startswith("OL"):
        suffix = candidate[-1:].upper()
        if suffix in {"W", "M"}:
            candidate = f"/{'works' if suffix == 'W' else 'books'}/{candidate}"
    match = _OPEN_LIBRARY_BOOK_KEY.fullmatch(candidate)
    if not match:
        return ""
    collection, identifier = match.groups()
    return f"/{collection.casefold()}/{identifier.upper()}"


def _book_search_url(title: str, author: str) -> str:
    params = {"title": title[:300]}
    if author and author.casefold() != "unknown author":
        params["author"] = author[:200]
    return f"{OPEN_LIBRARY_BASE}/search?{urlencode(params)}"


def _public_url(identifier: str, fallback: str) -> str:
    value = identifier.strip()
    if value.startswith("http://") or value.startswith("https://"):
        return value.replace("http://", "https://", 1)
    if value.startswith("/"):
        return f"{OPEN_LIBRARY_BASE}{value}"
    if value.startswith("OL") and value[-1:] in {"W", "M", "A"}:
        collection = {"W": "works", "M": "books", "A": "authors"}[value[-1]]
        return f"{OPEN_LIBRARY_BASE}/{collection}/{value}"
    return fallback


def _api_path(identifier: str) -> str:
    value = identifier.strip()
    if value.startswith("/"):
        return value
    if value.startswith("OL") and value[-1:] in {"W", "M", "A"}:
        collection = {"W": "works", "M": "books", "A": "authors"}[value[-1]]
        return f"/{collection}/{value}"
    return f"/{value}"


class OpenLibraryClient:
    """Small fixed-host JSON client with cache and SSRF-safe address pinning."""

    def __init__(
        self,
        *,
        cache: AssociationCache | None = None,
        rate_limiter: RateLimiter | None = None,
        contact: str | None = None,
        cache_ttl_seconds: float = 7 * 24 * 60 * 60,
        timeout_seconds: float | None = None,
    ):
        self.cache = cache or AssociationCache(max_entries=512)
        self.rate_limiter = rate_limiter or RateLimiter(1 / 3)
        self.contact = _clean(
            settings.openlibrary_contact if contact is None else contact,
            200,
        )
        self.cache_ttl_seconds = max(1.0, float(cache_ttl_seconds))
        self.timeout_seconds = timeout_seconds or min(settings.source_timeout_seconds, 15.0)

    def _cache_key(self, path: str, params: Mapping[str, Any]) -> str:
        values = [(str(key), str(value)) for key, value in sorted(params.items()) if value is not None]
        return f"{path}?{urlencode(values)}"

    async def get_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        if not path.startswith("/") or "//" in path:
            raise ValueError("Open Library path must be relative")
        params = dict(params or {})
        cache_key = self._cache_key(path, params)
        cached = self.cache.get(OPEN_LIBRARY_LISTS_PROVIDER, cache_key)
        if cached is not None:
            return cached
        await self.rate_limiter.wait()
        original = httpx.URL(OPEN_LIBRARY_BASE).join(path)
        _, address, hostname = resolve_public_target(str(original))
        pinned = original.copy_with(host=address)
        host_header = hostname if original.port in (None, 443) else f"{hostname}:{original.port}"
        headers = {"User-Agent": OPEN_LIBRARY_USER_AGENT}
        if self.contact:
            headers["User-Agent"] += f"; contact={self.contact}"
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
                headers=headers,
            ) as client:
                response = await client.request(
                    "GET",
                    pinned,
                    params=params,
                    headers={"Host": host_header},
                    extensions={"sni_hostname": hostname},
                )
                response.raise_for_status()
                if len(response.content) > settings.source_max_bytes:
                    raise ValueError("Open Library response is too large")
                payload = response.json()
        except Exception:
            record_association_request(OPEN_LIBRARY_LISTS_PROVIDER, cache_key, "error")
            raise
        record_association_request(OPEN_LIBRARY_LISTS_PROVIDER, cache_key, "ok")
        self.cache.put(
            OPEN_LIBRARY_LISTS_PROVIDER,
            cache_key,
            payload,
            ttl_seconds=self.cache_ttl_seconds,
        )
        return payload


class OpenLibraryListProvider:
    provider = OPEN_LIBRARY_LISTS_PROVIDER

    def __init__(
        self,
        *,
        client: OpenLibraryClient | None = None,
        max_seeds: int = 25,
        max_per_author: int = 2,
        max_lists_per_seed: int = 3,
        max_list_size: int = 500,
        max_items_per_list: int = 50,
        max_resolutions: int = 75,
    ):
        self.client = client or OpenLibraryClient()
        self.max_seeds = max(1, int(max_seeds))
        self.max_per_author = max(1, int(max_per_author))
        self.max_lists_per_seed = max(1, int(max_lists_per_seed))
        self.max_list_size = max(1, int(max_list_size))
        self.max_items_per_list = max(1, int(max_items_per_list))
        self.max_resolutions = max(0, int(max_resolutions))
        self._resolutions = 0

    async def _resolve_work(self, read: Mapping[str, Any]) -> tuple[str, dict[str, Any]] | None:
        isbn = canonical_isbn(read.get("isbn"))
        params: dict[str, Any] = {
            "limit": 5,
            "fields": "key,title,author_name,isbn,isbn13,author_key",
        }
        if isbn:
            params["isbn"] = isbn
        else:
            params["title"] = str(read.get("title") or "")[:300]
            params["author"] = str(read.get("author") or "")[:200]
        payload = await self.client.get_json("/search.json", params)
        docs = payload.get("docs", []) if isinstance(payload, Mapping) else []
        wanted_title = _title_key(read.get("title"))
        wanted_author = _title_key(read.get("author"))
        for doc in docs if isinstance(docs, list) else []:
            if not isinstance(doc, Mapping):
                continue
            key = _clean(doc.get("key"), 300)
            if not key:
                continue
            title = _title_key(doc.get("title"))
            authors = _authors(doc.get("author_name"))
            author_match = any(_title_key(author) == wanted_author for author in authors)
            if title == wanted_title and (author_match or isbn):
                return key, dict(doc)
        return None

    async def _resolve_entry(
        self, entry: Mapping[str, Any]
    ) -> tuple[str, str, str, str] | None:
        title = _clean(entry.get("title"), 500)
        if not title:
            return None
        authors = _entry_authors(entry)
        book_key = _external_key(entry)
        candidate_isbn = _entry_isbn(entry)
        if (not authors or not book_key) and self._resolutions < self.max_resolutions:
            self._resolutions += 1
            params: dict[str, Any] = {
                "title": title[:300],
                "limit": 5,
                "fields": "key,title,author_name,isbn,isbn13",
            }
            if authors:
                params["author"] = authors[0][:200]
            payload = await self.client.get_json(
                "/search.json",
                params,
            )
            docs = payload.get("docs", []) if isinstance(payload, Mapping) else []
            wanted = _title_key(title)
            for doc in docs if isinstance(docs, list) else []:
                if not isinstance(doc, Mapping) or _title_key(doc.get("title")) != wanted:
                    continue
                resolved_authors = _authors(doc.get("author_name"))
                if authors and not any(
                    _title_key(author) == _title_key(resolved_author)
                    for author in authors
                    for resolved_author in resolved_authors
                ):
                    continue
                book_key = book_key or _external_key(doc)
                candidate_isbn = candidate_isbn or _entry_isbn(doc)
                if not authors and resolved_authors:
                    authors = resolved_authors
                if book_key:
                    break
        author = authors[0] if authors else "Unknown author"
        return title, author, book_key, candidate_isbn

    async def _list_entries(self, list_url: str) -> list[Mapping[str, Any]]:
        path = list_url
        if path.startswith(OPEN_LIBRARY_BASE):
            path = path[len(OPEN_LIBRARY_BASE) :]
        path = path.split("?", 1)[0].rstrip("/")
        if not path.endswith("/seeds.json"):
            path = f"{path}/seeds.json"
        payload = await self.client.get_json(path, {"limit": self.max_items_per_list})
        values = payload.get("entries", []) if isinstance(payload, Mapping) else []
        return [entry for entry in values if isinstance(entry, Mapping)]

    async def collect(self, reads: Sequence[Mapping[str, Any]]) -> list[Association]:
        from ..associations import select_seed_reads

        seeds = select_seed_reads(
            reads,
            max_seeds=self.max_seeds,
            max_per_author=self.max_per_author,
        )
        self._resolutions = 0
        read_identity_index = ReadIdentityIndex.from_reads(reads)
        output: list[Association] = []
        seen: set[tuple[int, str]] = set()
        for seed in seeds:
            try:
                resolved = await self._resolve_work(seed)
                if resolved is None:
                    continue
                work_key, _doc = resolved
                list_payload = await self.client.get_json(f"{_api_path(work_key)}/lists.json", {})
                list_entries = list_payload.get("entries", []) if isinstance(list_payload, Mapping) else []
                selected_lists = []
                for entry in list_entries if isinstance(list_entries, list) else []:
                    if not isinstance(entry, Mapping):
                        continue
                    list_size = entry.get("seed_count")
                    try:
                        if list_size is not None and not 2 <= int(list_size) <= self.max_list_size:
                            continue
                    except (TypeError, ValueError):
                        continue
                    list_url = _clean(entry.get("url") or entry.get("full_url"), 500)
                    if list_url:
                        selected_lists.append((entry, list_url))
                    if len(selected_lists) >= self.max_lists_per_seed:
                        break
                for list_entry, list_url in selected_lists:
                    try:
                        entries = await self._list_entries(list_url)
                    except (httpx.HTTPError, ValueError, json.JSONDecodeError):
                        continue
                    list_name = _clean(list_entry.get("name"), 300)
                    for position, entry in enumerate(entries[: self.max_items_per_list], start=1):
                        resolved_entry = await self._resolve_entry(entry)
                        if resolved_entry is None:
                            continue
                        title, author, book_key, candidate_isbn = resolved_entry
                        external_id = book_key or book_identity(title, author)
                        candidate_identity = {
                            "title": title,
                            "author": author,
                            "quality_work_id": book_key,
                            "quality_provider": "openlibrary",
                            "isbn": candidate_isbn,
                        }
                        if read_identity_index.matches(candidate_identity):
                            continue
                        key = (int(seed["id"]), external_id)
                        if key in seen:
                            continue
                        seen.add(key)
                        source_url = _public_url(
                            book_key,
                            _book_search_url(title, author),
                        )
                        output.append(
                            Association(
                                provider=self.provider,
                                seed_read_id=int(seed["id"]),
                                external_id=external_id,
                                title=title,
                                author=author,
                                source_url=source_url,
                                isbn=candidate_isbn,
                                rank=position,
                                metadata={
                                    "work_key": work_key,
                                    "list_url": _public_url(list_url, OPEN_LIBRARY_BASE),
                                    "list_name": list_name,
                                },
                            )
                        )
            except (httpx.HTTPError, ValueError, json.JSONDecodeError):
                # A provider failure for one seed must not discard evidence
                # already collected for other seeds.
                continue
        return output
