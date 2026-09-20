"""LibraryThing Multi-Recommendations association provider.

LibraryThing's free API is intentionally treated as a small, user-configured
source: requests are batched, persisted request accounting enforces the
documented 25-query daily budget, and the one-request-per-second limiter is
shared by every provider instance in the process.  The API returns ISBNs and
work IDs in normal production mode; titles are resolved through Open Library
instead of relying on the API's debugging-only title field.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any, Mapping, Sequence
from urllib.parse import urlencode

import httpx

from ..associations import (
    Association,
    AssociationCache,
    RateLimiter,
    canonical_isbn,
    record_association_request,
    select_seed_reads,
)
from ..database import rows
from ..identity import book_identity
from ..security import resolve_public_target
from .openlibrary import OpenLibraryClient


LIBRARYTHING_PROVIDER = "librarything_multirecommendations"
LIBRARYTHING_BASE = "https://www.librarything.com"
LIBRARYTHING_PATH = "/api/multirecommendations.php"
LIBRARYTHING_MAX_DAILY_REQUESTS = 25
LIBRARYTHING_USER_AGENT = "Bookward/0.1 (+self-hosted book recommender)"
_LIBRARYTHING_RATE_LIMITER = RateLimiter(1.0)


def _clean(value: object, limit: int = 500) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


def _title_key(value: object) -> str:
    return book_identity(value, "").split("\x1f", 1)[0]


def _today_start() -> str:
    current = datetime.now(timezone.utc)
    # SQLite's CURRENT_TIMESTAMP uses a space separator and no timezone
    # suffix.  Keep the comparison lexicographically compatible with the
    # persisted request timestamps.
    return current.strftime("%Y-%m-%d 00:00:00")


def librarything_requests_today() -> int:
    """Count attempts made today, including failed requests."""

    result = rows(
        "SELECT COUNT(*) AS count FROM association_requests "
        "WHERE provider=? AND requested_at>=?",
        (LIBRARYTHING_PROVIDER, _today_start()),
    )
    return int(result[0]["count"]) if result else 0


class LibraryThingClient:
    def __init__(
        self,
        api_key: str,
        *,
        cache: AssociationCache | None = None,
        rate_limiter: RateLimiter | None = None,
        cache_ttl_seconds: float = 24 * 60 * 60,
        timeout_seconds: float = 15.0,
        daily_limit: int = LIBRARYTHING_MAX_DAILY_REQUESTS,
    ):
        self.api_key = str(api_key or "").strip()
        if not self.api_key:
            raise ValueError("LibraryThing API key is required")
        self.cache = cache or AssociationCache(max_entries=64)
        # Keep the free API's one-request-per-second budget process-wide. A
        # worker creates a fresh provider for each job, so a per-instance
        # limiter would allow queued jobs to burst.
        self.rate_limiter = rate_limiter or _LIBRARYTHING_RATE_LIMITER
        self.cache_ttl_seconds = max(1.0, float(cache_ttl_seconds))
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.daily_limit = max(1, int(daily_limit))

    async def recommendations(
        self,
        isbns: Sequence[str],
        *,
        max_items: int = 50,
        max_per_author: int = 2,
    ) -> Mapping[str, Any]:
        normalized = []
        for isbn in isbns:
            value = canonical_isbn(isbn)
            if value and value not in normalized:
                normalized.append(value)
        if not normalized:
            return {"request": [], "recommendations": []}
        query = {
            "isbns": ",".join(normalized[:25]),
            "maxItems": max(1, min(100, int(max_items))),
            "maxPerAuthor": max(1, min(20, int(max_per_author))),
            "v": "1",
        }
        cache_key = "recommendations?" + urlencode(sorted(query.items()))
        cached = self.cache.get(LIBRARYTHING_PROVIDER, cache_key)
        if cached is not None:
            return cached if isinstance(cached, Mapping) else {}
        if librarything_requests_today() >= self.daily_limit:
            raise RuntimeError("LibraryThing daily request limit reached")
        await self.rate_limiter.wait()
        request_url = f"{LIBRARYTHING_BASE}{LIBRARYTHING_PATH}"
        original = httpx.URL(request_url)
        _, address, hostname = resolve_public_target(str(original))
        pinned = original.copy_with(host=address)
        host_header = hostname if original.port in (None, 443) else f"{hostname}:{original.port}"
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
                headers={"User-Agent": LIBRARYTHING_USER_AGENT},
            ) as client:
                response = await client.request(
                    "GET",
                    pinned,
                    params={**query, "apiKey": self.api_key},
                    headers={"Host": host_header},
                    extensions={"sni_hostname": hostname},
                )
                response.raise_for_status()
                if len(response.content) > 2_000_000:
                    raise ValueError("LibraryThing response is too large")
                payload = response.json()
                if not isinstance(payload, Mapping):
                    raise ValueError("LibraryThing response must be an object")
        except Exception:
            record_association_request(LIBRARYTHING_PROVIDER, cache_key, "error")
            raise
        record_association_request(LIBRARYTHING_PROVIDER, cache_key, "ok")
        normalized_payload = dict(payload)
        self.cache.put(
            LIBRARYTHING_PROVIDER,
            cache_key,
            normalized_payload,
            ttl_seconds=self.cache_ttl_seconds,
        )
        return normalized_payload


class LibraryThingProvider:
    provider = LIBRARYTHING_PROVIDER

    def __init__(
        self,
        api_key: str,
        *,
        client: LibraryThingClient | None = None,
        metadata_client: OpenLibraryClient | None = None,
        max_seeds: int = 25,
        max_per_author: int = 2,
        max_items: int = 50,
        max_metadata_lookups: int = 50,
    ):
        self.client = client or LibraryThingClient(api_key)
        self.metadata_client = metadata_client or OpenLibraryClient()
        self.max_seeds = max(1, int(max_seeds))
        self.max_per_author = max(1, int(max_per_author))
        self.max_items = max(1, int(max_items))
        self.max_metadata_lookups = max(0, int(max_metadata_lookups))

    async def _metadata_for_isbn(self, isbn: str) -> tuple[str, str, str] | None:
        payload = await self.metadata_client.get_json(
            "/search.json",
            {
                "isbn": canonical_isbn(isbn),
                "limit": 5,
                "fields": "key,title,author_name,isbn,isbn13",
            },
        )
        docs = payload.get("docs", []) if isinstance(payload, Mapping) else []
        for doc in docs if isinstance(docs, list) else []:
            if not isinstance(doc, Mapping):
                continue
            title = _clean(doc.get("title"), 500)
            authors = doc.get("author_name")
            if isinstance(authors, list):
                author = _clean(authors[0] if authors else "", 300)
            else:
                author = _clean(authors, 300)
            key = _clean(doc.get("key"), 300)
            if title and author:
                return title, author, key or f"isbn:{canonical_isbn(isbn)}"
        return None

    async def collect(self, reads: Sequence[Mapping[str, Any]]) -> list[Association]:
        seeds = select_seed_reads(
            reads,
            max_seeds=self.max_seeds,
            max_per_author=self.max_per_author,
        )
        seed_by_isbn: dict[str, Mapping[str, Any]] = {}
        for seed in seeds:
            isbn = canonical_isbn(seed.get("isbn"))
            if len(isbn) in {10, 13}:
                seed_by_isbn.setdefault(isbn, seed)
        if not seed_by_isbn:
            return []
        payload = await self.client.recommendations(
            list(seed_by_isbn),
            max_items=self.max_items,
            max_per_author=self.max_per_author,
        )
        work_to_seed: dict[str, Mapping[str, Any]] = {}
        requests = payload.get("request", []) if isinstance(payload, Mapping) else []
        for request in requests if isinstance(requests, list) else []:
            if not isinstance(request, Mapping):
                continue
            isbn = canonical_isbn(request.get("isbn"))
            seed = seed_by_isbn.get(isbn)
            work = _clean(request.get("work"), 100)
            if seed is not None and work:
                work_to_seed[work] = seed
        fallback_seed = next(iter(seed_by_isbn.values()))
        output: list[Association] = []
        seen: set[tuple[int, str]] = set()
        lookups = 0
        recommendations = payload.get("recommendations", []) if isinstance(payload, Mapping) else []
        for fallback_rank, recommendation in enumerate(
            recommendations if isinstance(recommendations, list) else [], start=1
        ):
            if not isinstance(recommendation, Mapping):
                continue
            isbns = recommendation.get("isbns")
            if not isinstance(isbns, list):
                continue
            candidate_isbn = next(
                (canonical_isbn(value) for value in isbns if len(canonical_isbn(value)) in {10, 13}),
                "",
            )
            if not candidate_isbn or lookups >= self.max_metadata_lookups:
                continue
            lookups += 1
            try:
                resolved = await self._metadata_for_isbn(candidate_isbn)
            except (httpx.HTTPError, ValueError, json.JSONDecodeError):
                continue
            if resolved is None:
                continue
            title, author, open_library_id = resolved
            recommendation_work = _clean(recommendation.get("work"), 100)
            fromworks = recommendation.get("fromworks")
            if isinstance(fromworks, list):
                from_work_ids = {_clean(value, 100) for value in fromworks}
            else:
                from_work_ids = set()
            source_seeds = [
                seed for work, seed in work_to_seed.items() if work in from_work_ids
            ] or [fallback_seed]
            for seed in source_seeds:
                identity = book_identity(title, author)
                if identity == book_identity(seed.get("title"), seed.get("author")):
                    continue
                external_id = recommendation_work or f"isbn:{candidate_isbn}"
                key = (int(seed["id"]), external_id)
                if key in seen:
                    continue
                seen.add(key)
                output.append(
                    Association(
                        provider=self.provider,
                        seed_read_id=int(seed["id"]),
                        external_id=external_id,
                        title=title,
                        author=author,
                        isbn=candidate_isbn,
                        rank=int(recommendation.get("rank") or fallback_rank),
                        source_url=(
                            f"https://www.librarything.com/work/{recommendation_work}"
                            if recommendation_work
                            else "https://www.librarything.com"
                        ),
                        metadata={
                            "librarything_work": recommendation_work,
                            "fromworks": list(fromworks) if isinstance(fromworks, list) else [],
                            "openlibrary_id": open_library_id,
                        },
                    )
                )
        return output
