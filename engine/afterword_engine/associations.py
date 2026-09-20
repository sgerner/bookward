"""Provider-neutral association evidence and bounded provider orchestration.

Association providers are deliberately separate from the visible recommendation
pool.  A provider can discover a title and leave durable evidence behind while
the provider source remains disabled until an offline evaluation proves that
the evidence improves ranking quality.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
import time
from typing import Any, Awaitable, Callable, Mapping, Protocol, Sequence
from uuid import uuid4

from .database import normalize_key, row, rows, transaction
from .identity import book_identity


OPEN_LIBRARY_LISTS_PROVIDER = "openlibrary_lists"


@dataclass(frozen=True, slots=True)
class Association:
    """One provider's relationship between a read seed and a book."""

    provider: str
    seed_read_id: int
    external_id: str
    title: str
    author: str
    source_url: str = ""
    isbn: str = ""
    rank: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    expires_at: str | None = None


@dataclass(frozen=True, slots=True)
class AssociationRunResult:
    id: str
    provider: str
    status: str
    seeds: int
    edges: int
    persisted: int


class AssociationProvider(Protocol):
    provider: str

    async def collect(
        self, reads: Sequence[Mapping[str, Any]]
    ) -> list[Association]: ...


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_timestamp(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)


def canonical_isbn(value: object) -> str:
    """Return an ISBN-like identifier without presentation punctuation."""

    return "".join(character for character in str(value or "").upper() if character.isdigit() or character == "X")


def select_seed_reads(
    reads: Sequence[Mapping[str, Any]],
    *,
    max_seeds: int = 25,
    max_per_author: int = 2,
) -> list[dict[str, Any]]:
    """Select a small, diverse set of highly rated read seeds.

    Three-star and unrated entries are intentionally excluded.  This method is
    deterministic so a provider run can be reproduced in an offline study.
    """

    if max_seeds < 1 or max_per_author < 1:
        return []
    eligible = []
    for read in reads:
        try:
            rating = float(read.get("rating") or 0)
            read_id = int(read.get("id"))
        except (TypeError, ValueError):
            continue
        if not 4 <= rating <= 5:
            continue
        if not str(read.get("title") or "").strip() or not str(read.get("author") or "").strip():
            continue
        eligible.append((
            -rating,
            str(read.get("read_at") or ""),
            read_id,
            dict(read),
        ))
    eligible.sort(key=lambda item: (item[0], item[1], item[2]))
    selected: list[dict[str, Any]] = []
    seen_works: set[str] = set()
    author_counts: dict[str, int] = {}
    for _rating, _date, _read_id, read in eligible:
        work_key = book_identity(read.get("title", ""), read.get("author", ""))
        author_key = book_identity("", read.get("author", ""))
        if work_key in seen_works or author_counts.get(author_key, 0) >= max_per_author:
            continue
        seen_works.add(work_key)
        author_counts[author_key] = author_counts.get(author_key, 0) + 1
        selected.append(read)
        if len(selected) >= max_seeds:
            break
    return selected


class AssociationCache:
    """Small SQLite-backed JSON cache with expiry and an entry bound."""

    def __init__(self, *, max_entries: int = 512):
        self.max_entries = max(1, int(max_entries))

    def get(self, provider: str, cache_key: str) -> Any | None:
        found = row(
            "SELECT payload,expires_at FROM association_cache WHERE provider=? AND cache_key=?",
            (provider, cache_key),
        )
        if not found:
            return None
        expires = _parse_timestamp(found["expires_at"])
        if expires is None or expires <= _utc_now():
            with transaction() as con:
                con.execute(
                    "DELETE FROM association_cache WHERE provider=? AND cache_key=?",
                    (provider, cache_key),
                )
            return None
        try:
            return json.loads(found["payload"])
        except (TypeError, ValueError):
            with transaction() as con:
                con.execute(
                    "DELETE FROM association_cache WHERE provider=? AND cache_key=?",
                    (provider, cache_key),
                )
            return None

    def put(
        self,
        provider: str,
        cache_key: str,
        payload: Any,
        *,
        ttl_seconds: float,
    ) -> None:
        if ttl_seconds <= 0:
            return
        now = _utc_now()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with transaction() as con:
            con.execute(
                """INSERT INTO association_cache(provider,cache_key,payload,fetched_at,expires_at)
                VALUES(?,?,?,?,?)
                ON CONFLICT(provider,cache_key) DO UPDATE SET
                payload=excluded.payload,fetched_at=excluded.fetched_at,expires_at=excluded.expires_at""",
                (
                    provider,
                    cache_key,
                    encoded,
                    _timestamp(now),
                    _timestamp(now + timedelta(seconds=ttl_seconds)),
                ),
            )
            keys = [
                item["cache_key"]
                for item in con.execute(
                    "SELECT cache_key FROM association_cache WHERE provider=? "
                    "ORDER BY fetched_at DESC,cache_key DESC",
                    (provider,),
                ).fetchall()
            ]
            for stale_key in keys[self.max_entries :]:
                con.execute(
                    "DELETE FROM association_cache WHERE provider=? AND cache_key=?",
                    (provider, stale_key),
                )

    def purge(self, provider: str | None = None) -> int:
        now = _timestamp(_utc_now())
        with transaction() as con:
            if provider:
                con.execute(
                    "DELETE FROM association_cache WHERE provider=? AND expires_at<=?",
                    (provider, now),
                )
            else:
                cursor = con.execute(
                    "DELETE FROM association_cache WHERE expires_at<=?", (now,)
                )
            return cursor.rowcount


class RateLimiter:
    """Serialize requests and enforce a minimum interval between them."""

    def __init__(
        self,
        minimum_interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.minimum_interval = max(0.0, float(minimum_interval))
        self._clock = clock
        self._sleeper = sleeper
        self._last_request: float | None = None
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = self._clock()
            if self._last_request is not None:
                delay = self.minimum_interval - (now - self._last_request)
                if delay > 0:
                    await self._sleeper(delay)
            self._last_request = self._clock()


def begin_association_run(provider: str, *, seed_count: int = 0) -> str:
    run_id = uuid4().hex
    with transaction() as con:
        con.execute(
            "INSERT INTO association_runs(id,provider,status,seed_count) VALUES(?,?,?,?)",
            (run_id, provider, "running", seed_count),
        )
    return run_id


def finish_association_run(
    run_id: str,
    *,
    status: str,
    edge_count: int = 0,
    error: str | None = None,
) -> None:
    with transaction() as con:
        con.execute(
            "UPDATE association_runs SET status=?,edge_count=?,error=?,finished_at=CURRENT_TIMESTAMP WHERE id=?",
            (status, edge_count, error[:1000] if error else None, run_id),
        )


def record_association_request(provider: str, request_key: str, status: str) -> None:
    with transaction() as con:
        con.execute(
            "INSERT INTO association_requests(provider,request_key,status) VALUES(?,?,?)",
            (provider, request_key[:500], status[:100]),
        )


def persist_associations(
    associations: Sequence[Association], *, run_id: str | None = None
) -> int:
    """Persist candidates and their provider evidence without changing scores."""

    if not associations:
        return 0
    persisted = 0
    source_ids: dict[str, int] = {}
    with transaction() as con:
        for association in associations:
            title = str(association.title or "").strip()[:500]
            author = str(association.author or "").strip()[:300]
            if not title or not author:
                continue
            provider = str(association.provider or "").strip()[:100]
            external_id = str(association.external_id or "").strip()[:300]
            if not provider or not external_id:
                continue
            if provider not in source_ids:
                cursor = con.execute(
                    """INSERT OR IGNORE INTO sources(name,url,kind,enabled,is_default,lifecycle)
                    VALUES(?,?,?,?,?,?)""",
                    (
                        f"Association · {provider}",
                        f"association://{provider}",
                        "association",
                        0,
                        0,
                        "permanent",
                    ),
                )
                source = con.execute(
                    "SELECT id FROM sources WHERE url=?", (f"association://{provider}",)
                ).fetchone()
                if source is None:
                    continue
                source_ids[provider] = int(source[0])
            key = normalize_key(title, author)
            con.execute(
                """INSERT INTO candidates(title,author,source_url,source_id,status,normalized_key)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(normalized_key) DO NOTHING""",
                (
                    title,
                    author,
                    association.source_url[:1000],
                    source_ids[provider],
                    "new",
                    key,
                ),
            )
            candidate = con.execute(
                "SELECT id FROM candidates WHERE normalized_key=?", (key,)
            ).fetchone()
            if candidate is None:
                continue
            candidate_id = int(candidate[0])
            metadata = json.dumps(
                dict(association.metadata), ensure_ascii=False, separators=(",", ":")
            )
            con.execute(
                """INSERT INTO association_evidence(
                    provider,seed_read_id,candidate_id,external_id,provider_rank,
                    source_url,metadata_json,run_id,expires_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(provider,seed_read_id,external_id) DO UPDATE SET
                    candidate_id=excluded.candidate_id,
                    provider_rank=excluded.provider_rank,
                    source_url=excluded.source_url,
                    metadata_json=excluded.metadata_json,
                    run_id=excluded.run_id,
                    fetched_at=CURRENT_TIMESTAMP,
                    expires_at=excluded.expires_at""",
                (
                    provider,
                    int(association.seed_read_id),
                    candidate_id,
                    external_id,
                    association.rank,
                    association.source_url[:1000],
                    metadata,
                    run_id,
                    association.expires_at,
                ),
            )
            persisted += 1
    return persisted


async def run_association_provider(
    provider: AssociationProvider,
    reads: Sequence[Mapping[str, Any]],
) -> AssociationRunResult:
    """Run, persist, and audit a provider without touching recommendation scores."""

    seeds = select_seed_reads(reads)
    run_id = begin_association_run(provider.provider, seed_count=len(seeds))
    try:
        associations = await provider.collect(seeds)
        persisted = persist_associations(associations, run_id=run_id)
    except Exception as exc:
        finish_association_run(run_id, status="failed", error=str(exc))
        raise
    finish_association_run(run_id, status="complete", edge_count=len(associations))
    return AssociationRunResult(
        id=run_id,
        provider=provider.provider,
        status="complete",
        seeds=len(seeds),
        edges=len(associations),
        persisted=persisted,
    )
