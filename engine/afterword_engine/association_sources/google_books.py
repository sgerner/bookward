"""Google Books associated-volume preview provider.

Google API terms prohibit creating a permanent database of API content or
keeping cached copies longer than the response permits.  This adapter therefore
uses a bounded process-memory cache only when the response explicitly grants a
public ``max-age`` and is intended for preview/shadow calls.  It never writes
Google volume metadata or raw responses to SQLite.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import time
from typing import Any, Mapping, Sequence
from urllib.parse import quote, urlencode

import httpx

from ..associations import Association, RateLimiter, canonical_isbn, record_association_request, select_seed_reads
from ..config import settings
from ..identity import book_identity
from ..security import resolve_public_target


GOOGLE_BOOKS_PROVIDER = "google_books_associated"
GOOGLE_BOOKS_BASE = "https://www.googleapis.com"
GOOGLE_BOOKS_PATH = "/books/v1"
GOOGLE_BOOKS_USER_AGENT = "Bookward/0.1 (+self-hosted book recommender)"


def _clean(value: object, limit: int = 500) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


def _title_key(value: object) -> str:
    return book_identity(value, "").split("\x1f", 1)[0]


def _authors(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_clean(item, 300) for item in value if _clean(item, 300)]


@dataclass(slots=True)
class _MemoryCacheEntry:
    expires_at: float
    payload: Any


class GoogleMemoryCache:
    """Bounded, process-local cache used only when Google permits caching."""

    def __init__(self, *, max_entries: int = 64, clock=time.monotonic):
        self.max_entries = max(1, int(max_entries))
        self.clock = clock
        self._entries: dict[str, _MemoryCacheEntry] = {}

    def get(self, key: str) -> Any | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= self.clock():
            self._entries.pop(key, None)
            return None
        return entry.payload

    def put(self, key: str, payload: Any, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            return
        self._entries[key] = _MemoryCacheEntry(self.clock() + ttl_seconds, payload)
        while len(self._entries) > self.max_entries:
            oldest = min(self._entries, key=lambda item: self._entries[item].expires_at)
            self._entries.pop(oldest, None)

    def __len__(self) -> int:
        return len(self._entries)


def _cache_ttl(headers: Mapping[str, str]) -> float:
    value = str(headers.get("cache-control", "")).casefold()
    if any(token in value for token in ("no-store", "no-cache", "private")):
        return 0.0
    match = re.search(r"(?:^|,)\s*max-age\s*=\s*(\d+)", value)
    return float(match.group(1)) if match else 0.0


class GoogleBooksClient:
    def __init__(
        self,
        api_key: str = "",
        *,
        cache: GoogleMemoryCache | None = None,
        rate_limiter: RateLimiter | None = None,
        timeout_seconds: float | None = None,
        max_cache_ttl_seconds: float = 3600,
    ):
        self.api_key = str(api_key or "").strip()
        self.cache = cache or GoogleMemoryCache()
        self.rate_limiter = rate_limiter or RateLimiter(0.2)
        self.timeout_seconds = timeout_seconds or min(settings.source_timeout_seconds, 15.0)
        self.max_cache_ttl_seconds = max(1.0, float(max_cache_ttl_seconds))

    def _cache_key(self, path: str, params: Mapping[str, Any]) -> str:
        return f"{path}?{urlencode(sorted((str(key), str(value)) for key, value in params.items()))}"

    async def get_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        if not path.startswith("/") or "//" in path:
            raise ValueError("Google Books path must be relative")
        query = dict(params or {})
        if self.api_key:
            query["key"] = self.api_key
        cache_key = self._cache_key(path, {key: value for key, value in query.items() if key != "key"})
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        await self.rate_limiter.wait()
        original = httpx.URL(GOOGLE_BOOKS_BASE).join(path)
        _, address, hostname = resolve_public_target(str(original))
        pinned = original.copy_with(host=address)
        host_header = hostname if original.port in (None, 443) else f"{hostname}:{original.port}"
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
                headers={"User-Agent": GOOGLE_BOOKS_USER_AGENT},
            ) as client:
                response = await client.request(
                    "GET",
                    pinned,
                    params=query,
                    headers={"Host": host_header},
                    extensions={"sni_hostname": hostname},
                )
                response.raise_for_status()
                if len(response.content) > settings.source_max_bytes:
                    raise ValueError("Google Books response is too large")
                payload = response.json()
                if not isinstance(payload, Mapping):
                    raise ValueError("Google Books response must be an object")
                ttl = min(_cache_ttl(response.headers), self.max_cache_ttl_seconds)
        except Exception:
            record_association_request(GOOGLE_BOOKS_PROVIDER, cache_key, "error")
            raise
        record_association_request(GOOGLE_BOOKS_PROVIDER, cache_key, "ok")
        if ttl > 0:
            self.cache.put(cache_key, dict(payload), ttl)
        return dict(payload)


class GoogleBooksAssociatedProvider:
    provider = GOOGLE_BOOKS_PROVIDER

    def __init__(
        self,
        api_key: str = "",
        *,
        client: GoogleBooksClient | None = None,
        max_seeds: int = 5,
        max_items_per_seed: int = 25,
    ):
        self.client = client or GoogleBooksClient(api_key)
        self.max_seeds = max(1, int(max_seeds))
        self.max_items_per_seed = max(1, int(max_items_per_seed))

    async def _find_volume(self, read: Mapping[str, Any]) -> str | None:
        isbn = canonical_isbn(read.get("isbn"))
        query = f"isbn:{isbn}" if isbn else f"intitle:{read.get('title', '')} inauthor:{read.get('author', '')}"
        payload = await self.client.get_json(
            f"{GOOGLE_BOOKS_PATH}/volumes",
            {"q": query[:500], "maxResults": 5, "projection": "lite"},
        )
        wanted_title = _title_key(read.get("title"))
        wanted_author = _title_key(read.get("author"))
        for item in payload.get("items", []) if isinstance(payload, Mapping) else []:
            if not isinstance(item, Mapping):
                continue
            info = item.get("volumeInfo") if isinstance(item.get("volumeInfo"), Mapping) else {}
            title = _title_key(info.get("title"))
            authors = _authors(info.get("authors"))
            author_match = any(_title_key(author) == wanted_author for author in authors)
            volume_id = _clean(item.get("id"), 200)
            if volume_id and title == wanted_title and (author_match or isbn):
                return volume_id
        return None

    async def collect(self, reads: Sequence[Mapping[str, Any]]) -> list[Association]:
        seeds = select_seed_reads(reads, max_seeds=self.max_seeds, max_per_author=2)
        read_identities = {
            book_identity(read.get("title", ""), read.get("author", "")) for read in reads
        }
        read_titles = {_title_key(read.get("title", "")) for read in reads}
        output: list[Association] = []
        seen: set[tuple[int, str]] = set()
        for seed in seeds:
            try:
                volume_id = await self._find_volume(seed)
                if not volume_id:
                    continue
                payload = await self.client.get_json(
                    f"{GOOGLE_BOOKS_PATH}/volumes/{quote(volume_id, safe='')}/associated",
                    {"association": "end-of-volume", "maxAllowedMaturityRating": "not-mature"},
                )
            except (httpx.HTTPError, ValueError, json.JSONDecodeError):
                continue
            for rank, item in enumerate(
                payload.get("items", []) if isinstance(payload, Mapping) else [], start=1
            ):
                if rank > self.max_items_per_seed or not isinstance(item, Mapping):
                    break
                info = item.get("volumeInfo") if isinstance(item.get("volumeInfo"), Mapping) else {}
                title = _clean(info.get("title"), 500)
                authors = _authors(info.get("authors"))
                author = authors[0] if authors else ""
                external_id = _clean(item.get("id"), 200)
                if not title or not author or not external_id:
                    continue
                if book_identity(title, author) in read_identities or _title_key(title) in read_titles:
                    continue
                key = (int(seed["id"]), external_id)
                if key in seen:
                    continue
                seen.add(key)
                source_url = _clean(
                    info.get("canonicalVolumeLink") or info.get("infoLink"),
                    1000,
                ) or f"https://books.google.com/books?id={quote(external_id, safe='')}"
                output.append(
                    Association(
                        provider=self.provider,
                        seed_read_id=int(seed["id"]),
                        external_id=external_id,
                        title=title,
                        author=author,
                        source_url=source_url,
                        rank=rank,
                        metadata={
                            "seed_volume_id": volume_id,
                            "explanation": _clean(
                                (item.get("recommendedInfo") or {}).get("explanation")
                                if isinstance(item.get("recommendedInfo"), Mapping)
                                else "",
                                1000,
                            ),
                        },
                    )
                )
        return output
