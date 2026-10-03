#!/usr/bin/env python3
"""Bounded Open Library typed-retrieval study with a frozen ranking baseline.

The public catalog is queried only when ``--run-public-probe`` is supplied.
Study output is aggregate-only. No candidate, association, or cache row is
written to the application database.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence
from urllib.parse import quote, urlsplit

import httpx
import numpy as np

ENGINE = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(ENGINE))

from afterword_engine.association_sources.openlibrary import (  # noqa: E402
    OPEN_LIBRARY_BASE,
    OpenLibraryListProvider,
    _title_key,
)
from afterword_engine.associations import (  # noqa: E402
    Association,
    RateLimiter,
    canonical_isbn,
    select_seed_reads,
)
from afterword_engine.embeddings import content_hash  # noqa: E402
from afterword_engine.identity import (  # noqa: E402
    book_identity,
    book_identity_matches,
    book_openlibrary_work_id,
    book_row_identity_match_keys,
    canonical_part,
)
from afterword_engine.quality import local_flags  # noqa: E402
from afterword_engine.scoring import document  # noqa: E402
from afterword_engine.security import resolve_public_target  # noqa: E402
from afterword_engine.subjects import normalize_subjects  # noqa: E402


REQUEST_CEILING = 20
SEED_BUDGET = 4
MAX_RESPONSE_ITEMS = 50
MAX_SUBJECT_QUERIES_PER_SEED = 3
MAX_DIFFUSION_STEPS = 32
FROZEN_DIFFUSION_ALPHA = 0.6
RELATION_WEIGHTS = {"same_author": 1.0, "shared_subject": 0.5, "same_series": 1.0}
OPEN_LIBRARY_USER_AGENT = "Bookward typed-retrieval offline study/1.0"
OPEN_LIBRARY_WORK_RE = re.compile(r"^/works/(OL\d+W)(?:\.json)?$", re.IGNORECASE)
OPEN_LIBRARY_AUTHOR_RE = re.compile(r"^/authors/(OL\d+A)(?:\.json)?$", re.IGNORECASE)
_SUBJECT_SLUG_RE = re.compile(r"^[a-z0-9_]{1,120}$")
PRIVATE_STUDY_DIR = Path("/home/steven/.local/share/bookward-next-five-20261002")
FROZEN_PROTOCOL_SHA256 = "8c58739a896acb2d0284f3c51eb5752121ed9262a415ecd313b8f124d7dd935d"
FROZEN_CORPUS_SHA256 = "0973bf5e029f1ee0f2a6ad8957313e64da639b813e20d31491a9ba4b8558eeb9"
FROZEN_MODEL_DIGEST = "df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907"
FROZEN_MODEL = "qwen3-embedding:4b"


class BudgetExhausted(ValueError):
    """Raised when another public HTTP request would exceed the fixed cap."""


def _utc_day(value: object) -> date | None:
    """Parse a read date conservatively into a UTC calendar day."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        return value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.strptime(value, "%Y/%m/%d")
            except ValueError:
                return None
    else:
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc).date()


def _clean_text(value: object, limit: int = 500) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


def _description_text(value: object) -> str:
    """Return only a catalog synopsis string, never an opening sentence.

    Open Library descriptions are either strings or mappings with a string
    ``value``.  Other shapes are not candidate description text and must not
    be stringified into the enriched-text sensitivity.
    """
    if isinstance(value, Mapping):
        value = value.get("value")
    if not isinstance(value, str):
        return ""
    return _clean_text(value, 4000)


def _work_key(value: object) -> str:
    """Accept only a canonical Open Library work identifier."""
    raw = _clean_text(value, 500)
    if raw.startswith(("http://", "https://")):
        try:
            parsed = urlsplit(raw)
        except ValueError:
            return ""
        if (parsed.hostname or "").casefold().rstrip(".") not in {
            "openlibrary.org", "www.openlibrary.org"
        }:
            return ""
        raw = parsed.path
    match = OPEN_LIBRARY_WORK_RE.fullmatch(raw)
    return f"/works/{match.group(1).upper()}" if match else ""


def _author_key(value: object) -> str:
    raw = _clean_text(value, 500)
    if raw.startswith(("http://", "https://")):
        try:
            parsed = urlsplit(raw)
        except ValueError:
            return ""
        if (parsed.hostname or "").casefold().rstrip(".") not in {
            "openlibrary.org", "www.openlibrary.org"
        }:
            return ""
        raw = parsed.path
    match = OPEN_LIBRARY_AUTHOR_RE.fullmatch(raw)
    return f"/authors/{match.group(1).upper()}" if match else ""


def _subject_slug(value: object) -> str:
    """Make a URL-safe subject slug from an explicit catalog label."""
    label = _clean_text(value, 160).casefold()
    slug = re.sub(r"[^a-z0-9]+", "_", label).strip("_")
    return slug if _SUBJECT_SLUG_RE.fullmatch(slug) else ""


def _field_list(value: object) -> list[object]:
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple)) else [value]


def _author_fields(item: Mapping[str, Any]) -> tuple[list[str], set[str]]:
    names: list[str] = []
    keys: set[str] = set()
    for raw in _field_list(item.get("author_name")) + _field_list(item.get("author")) + _field_list(item.get("authors")):
        if isinstance(raw, str):
            name = _clean_text(raw, 300)
            if name and name not in names:
                names.append(name)
            continue
        if not isinstance(raw, Mapping):
            continue
        name = _clean_text(raw.get("name") or raw.get("author_name"), 300)
        if name and name not in names:
            names.append(name)
        key = _author_key(raw.get("key") or raw.get("url"))
        if key:
            keys.add(key)
        nested = raw.get("author")
        if isinstance(nested, Mapping):
            nested_key = _author_key(nested.get("key") or nested.get("url"))
            if nested_key:
                keys.add(nested_key)
            nested_name = _clean_text(nested.get("name"), 300)
            if nested_name and nested_name not in names:
                names.append(nested_name)
    for raw in _field_list(item.get("author_key")):
        key = _author_key(raw)
        if key:
            keys.add(key)
    return names, keys


def _explicit_series_keys(value: object) -> set[str]:
    """Return only exact series values explicitly asserted by a catalog row.

    The function never extracts a series from a title, subtitle, or subject.
    Open Library records with no explicit ``series`` field yield no edge.
    """
    keys: set[str] = set()
    for raw in _field_list(value):
        if isinstance(raw, Mapping):
            raw = raw.get("key") or raw.get("id") or raw.get("url") or raw.get("name")
        label = _clean_text(raw, 300)
        normalized = canonical_part(label)
        if normalized:
            keys.add(normalized)
    return keys


def _catalog_year(item: Mapping[str, Any]) -> int | None:
    for field_name in ("first_publish_year", "first_publish_date", "published", "publication_date"):
        value = item.get(field_name)
        match = re.search(r"\b(1[0-9]{3}|20[0-9]{2})\b", str(value or ""))
        if match:
            year = int(match.group(1))
            if 1000 <= year <= 2099:
                return year
    return None


def _catalog_exact_date(item: Mapping[str, Any]) -> date | None:
    for field_name in ("first_publish_date", "published", "publication_date"):
        value = _clean_text(item.get(field_name), 80)
        if not value:
            continue
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            continue
        if 1000 <= parsed.year <= 2099:
            return parsed
    return None


def _reported_degree(payload: object) -> int:
    if not isinstance(payload, Mapping):
        return 0
    for name in ("work_count", "size", "numFound", "num_found", "count"):
        try:
            value = int(payload.get(name) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0


@dataclass(slots=True)
class Work:
    work_id: str
    title: str
    author: str
    author_keys: set[str] = field(default_factory=set)
    subjects: dict[str, str] = field(default_factory=dict)
    series_keys: set[str] = field(default_factory=set)
    genres: list[str] = field(default_factory=list)
    description: str = ""
    first_publish_year: int | None = None
    first_publish_date: date | None = None
    isbn: str = ""
    source_url: str = ""
    sources: set[str] = field(default_factory=set)
    identity_conflict: bool = False

    def candidate_row(self, *, enriched: bool = False) -> dict[str, Any]:
        return {
            "id": self.work_id,
            "title": self.title,
            "author": self.author,
            "work_id": self.work_id,
            "source_url": self.source_url or f"{OPEN_LIBRARY_BASE}{self.work_id}",
            "description": self.description if enriched else "",
            "genres": self.genres if enriched else [],
            "source_weight": 1.0,
            "catalog_confidence": 1.0,
            "first_publish_year": self.first_publish_year,
            "first_publish_date": self.first_publish_date.isoformat() if self.first_publish_date else "",
            "_probe_sources": sorted(self.sources),
        }


def _work_from_catalog(
    item: Mapping[str, Any],
    *,
    source: str,
    fallback_author: str = "",
    injected_author_key: str = "",
    injected_subject: tuple[str, str] | None = None,
) -> Work | None:
    work_id = _work_key(
        item.get("key") or item.get("work_key") or item.get("work") or item.get("url")
    )
    title = _clean_text(item.get("title"), 500)
    authors, author_keys = _author_fields(item)
    author = ", ".join(authors) if authors else _clean_text(fallback_author, 300)
    if injected_author_key:
        normalized_key = _author_key(injected_author_key)
        if normalized_key:
            author_keys.add(normalized_key)
    if not work_id or not title or not author:
        return None
    subjects = normalize_subjects(item.get("subject") or item.get("subjects"), limit=8)
    subject_map = {_subject_slug(label): label for label in subjects if _subject_slug(label)}
    if injected_subject:
        key, label = injected_subject
        if key and label:
            subject_map[key] = label
    genres = list(subject_map.values())
    description = _description_text(item.get("description"))
    isbn = ""
    for raw in _field_list(item.get("isbn13")) + _field_list(item.get("isbn")):
        isbn = canonical_isbn(raw)
        if len(isbn) in {10, 13}:
            break
        isbn = ""
    work = Work(
        work_id=work_id,
        title=title,
        author=author,
        author_keys=author_keys,
        subjects=subject_map,
        series_keys=_explicit_series_keys(item.get("series")),
        genres=genres,
        description=description,
        first_publish_year=_catalog_year(item),
        first_publish_date=_catalog_exact_date(item),
        isbn=isbn,
        source_url=f"{OPEN_LIBRARY_BASE}{work_id}",
        sources={source},
    )
    return work


def _merge_work(target: dict[str, Work], incoming: Work) -> None:
    current = target.get(incoming.work_id)
    if current is None:
        target[incoming.work_id] = incoming
        return
    if not book_identity_matches(current.title, current.author, incoming.title, incoming.author):
        current.identity_conflict = True
        return
    current.author_keys.update(incoming.author_keys)
    current.subjects.update(incoming.subjects)
    current.series_keys.update(incoming.series_keys)
    current.sources.update(incoming.sources)
    if not current.genres:
        current.genres = incoming.genres
    if not current.description:
        current.description = incoming.description
    if current.first_publish_year is None:
        current.first_publish_year = incoming.first_publish_year
    if current.first_publish_date is None:
        current.first_publish_date = incoming.first_publish_date
    if not current.isbn:
        current.isbn = incoming.isbn


def _publication_status(work: Work, cutoff_day: date) -> str:
    if work.first_publish_date is not None:
        return "eligible" if work.first_publish_date <= cutoff_day else "after_cutoff"
    if work.first_publish_year is None:
        return "unknown"
    if work.first_publish_year < cutoff_day.year:
        return "eligible"
    if work.first_publish_year > cutoff_day.year:
        return "after_cutoff"
    return "unknown_within_year"


def eligible_candidate(
    work: Work,
    prefix_reads: Sequence[Mapping[str, Any]],
    *,
    cutoff_day: date,
) -> tuple[bool, str]:
    """Apply the public-source, identity, local quality, and seen-work gates."""
    if work.identity_conflict:
        return False, "identity_conflict"
    candidate = work.candidate_row()
    if not _work_key(candidate.get("work_id")):
        return False, "unverified_work_id"
    if _publication_status(work, cutoff_day) == "after_cutoff":
        return False, "published_after_cutoff"
    source = urlsplit(str(candidate.get("source_url") or ""))
    if source.scheme != "https" or source.hostname not in {"openlibrary.org", "www.openlibrary.org"}:
        return False, "untrusted_source"
    flags = local_flags(candidate)
    if flags:
        return False, "local_quality_gate"
    candidate_keys = book_row_identity_match_keys(candidate)
    for read in prefix_reads:
        if candidate_keys & book_row_identity_match_keys(read):
            return False, "already_read"
    return True, "eligible"


class OpenLibraryProbeClient:
    """Uncached, read-only client with a per-capture GET ceiling."""

    def __init__(
        self,
        *,
        request_budget: int = REQUEST_CEILING,
        rate_limiter: RateLimiter | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 15.0,
        max_response_bytes: int = 2_000_000,
    ):
        self.request_budget = max(0, int(request_budget))
        self.rate_limiter = rate_limiter or RateLimiter(1.0)
        self.transport = transport
        self.timeout_seconds = timeout_seconds
        self.max_response_bytes = max_response_bytes
        self.requests = 0
        self.budget_blocked = 0
        self.statuses: Counter[str] = Counter()
        self.endpoint_counts: Counter[str] = Counter()
        self.response_rows: Counter[str] = Counter()

    @property
    def remaining(self) -> int:
        return max(0, self.request_budget - self.requests)

    def _endpoint(self, path: str) -> str:
        if path.endswith("/search.json") or path == "/search.json":
            return "search"
        if path.startswith("/authors/"):
            return "author_works"
        if path.startswith("/subjects/"):
            return "subject_works"
        if path.endswith("/lists.json"):
            return "work_lists"
        if path.endswith("/seeds.json"):
            return "list_items"
        return "other_catalog_get"

    async def get_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        if not path.startswith("/") or "//" in path:
            raise ValueError("Open Library path must be relative")
        category = self._endpoint(path)
        if self.requests >= self.request_budget:
            self.budget_blocked += 1
            raise BudgetExhausted("public request budget exhausted")
        self.requests += 1
        self.endpoint_counts[category] += 1
        try:
            await self.rate_limiter.wait()
            original = httpx.URL(OPEN_LIBRARY_BASE).join(path)
            _, address, hostname = resolve_public_target(str(original))
            pinned = original.copy_with(host=address)
            host_header = hostname if original.port in (None, 443) else f"{hostname}:{original.port}"
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
                headers={"User-Agent": OPEN_LIBRARY_USER_AGENT},
                transport=self.transport,
            ) as client:
                response = await client.request(
                    "GET",
                    pinned,
                    params=dict(params or {}),
                    headers={"Host": host_header},
                    extensions={"sni_hostname": hostname},
                )
                response.raise_for_status()
                if len(response.content) > self.max_response_bytes:
                    raise ValueError("Open Library response is too large")
                payload = response.json()
        except Exception as exc:
            self.statuses[type(exc).__name__] += 1
            raise
        self.statuses["ok"] += 1
        self.response_rows[category] += _count_rows(payload)
        return payload

    def aggregate(self) -> dict[str, Any]:
        return {
            "request_ceiling": self.request_budget,
            "requests_used": self.requests,
            "requests_remaining": self.remaining,
            "budget_blocked_attempts": self.budget_blocked,
            "successes": int(self.statuses.get("ok", 0)),
            "errors_by_type": {key: value for key, value in sorted(self.statuses.items()) if key != "ok"},
            "request_counts_by_endpoint": dict(sorted(self.endpoint_counts.items())),
            "response_rows_by_endpoint": dict(sorted(self.response_rows.items())),
            "automatic_retries": 0,
        }


def _count_rows(payload: object) -> int:
    if not isinstance(payload, Mapping):
        return 0
    for name in ("docs", "works", "entries"):
        value = payload.get(name)
        if isinstance(value, list):
            return len(value)
    return 0


@dataclass(slots=True)
class Capture:
    name: str
    policy: str
    seeds: list[dict[str, Any]]
    works: dict[str, Work] = field(default_factory=dict)
    seed_works: dict[str, Work] = field(default_factory=dict)
    eligible: dict[str, Work] = field(default_factory=dict)
    excluded: Counter[str] = field(default_factory=Counter)
    relation_degree_hints: dict[tuple[str, str], int] = field(default_factory=dict)
    request_client: OpenLibraryProbeClient | None = None
    provider_returned: int = 0
    graph_edge_counts: Counter[str] = field(default_factory=Counter)
    provider_order: dict[str, int] = field(default_factory=dict)


def choose_seed_reads(
    prefix_records: Sequence[tuple[date | datetime, Mapping[str, Any], np.ndarray]],
    policy: str,
    *,
    boundary_day: date,
    limit: int = SEED_BUDGET,
) -> list[dict[str, Any]]:
    """Select high-rated seed reads strictly before the whole-day boundary."""
    if policy == "current":
        earlier = [dict(item) for day, item, _vector in prefix_records if day.date() < boundary_day]
        return select_seed_reads(earlier, max_seeds=limit, max_per_author=2)
    if policy not in {"recent", "mixed"}:
        raise ValueError("unknown seed policy")
    eligible: dict[str, tuple[date | datetime, dict[str, Any]]] = {}
    for day, raw, _vector in prefix_records:
        if day.date() >= boundary_day:
            continue
        try:
            rating = float(raw.get("rating") or 0)
            read_id = int(raw.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if not 4 <= rating <= 5 or not _clean_text(raw.get("title")) or not _clean_text(raw.get("author")):
            continue
        item = dict(raw)
        key = book_identity(item.get("title"), item.get("author"))
        previous = eligible.get(key)
        if previous is None:
            eligible[key] = (day, item)
            continue
        prev_day, prev_item = previous
        prev_rating = float(prev_item.get("rating") or 0)
        prev_id = int(prev_item.get("id") or 0)
        if (-rating, day, read_id) < (-prev_rating, prev_day, prev_id):
            eligible[key] = (day, item)
    chronological = sorted(
        eligible.values(),
        key=lambda pair: (pair[0], int(pair[1].get("id") or 0)),
    )
    if policy == "recent":
        ordered = sorted(
            chronological,
            key=lambda pair: (-pair[0].toordinal(), -float(pair[1].get("rating") or 0), int(pair[1].get("id") or 0)),
        )
    else:
        ordered = []
        left, right = 0, len(chronological) - 1
        while left <= right:
            ordered.append(chronological[left])
            left += 1
            if left <= right:
                ordered.append(chronological[right])
                right -= 1
    selected: list[dict[str, Any]] = []
    seen_authors: Counter[str] = Counter()
    for _day, item in ordered:
        author = book_identity("", item.get("author"))
        if seen_authors[author] >= 2:
            continue
        selected.append(item)
        seen_authors[author] += 1
        if len(selected) >= limit:
            break
    return selected


async def _resolve_seed(
    read: Mapping[str, Any], client: OpenLibraryProbeClient
) -> Work | None:
    isbn = canonical_isbn(read.get("isbn"))
    params: dict[str, Any] = {
        "limit": 5,
        "fields": "key,title,author_name,author_key,isbn,isbn13,subject,series,first_publish_year",
    }
    if len(isbn) in {10, 13}:
        params["isbn"] = isbn
    else:
        params["title"] = _clean_text(read.get("title"), 300)
        params["author"] = _clean_text(read.get("author"), 200)
    payload = await client.get_json("/search.json", params)
    docs = payload.get("docs", []) if isinstance(payload, Mapping) else []
    for doc in docs if isinstance(docs, list) else []:
        if not isinstance(doc, Mapping):
            continue
        candidate = _work_from_catalog(doc, source="seed_search", fallback_author="")
        if candidate is None:
            continue
        title_match = _title_key(candidate.title) == _title_key(read.get("title"))
        names, _keys = _author_fields(doc)
        author_match = any(
            book_identity("", author) == book_identity("", read.get("author"))
            for author in names
        )
        doc_isbns = [canonical_isbn(value) for value in _field_list(doc.get("isbn13")) + _field_list(doc.get("isbn"))]
        isbn_match = bool(isbn and isbn in doc_isbns)
        if (title_match and author_match) or isbn_match:
            if not candidate.author_keys:
                candidate.author_keys.update(
                    key for value in _field_list(doc.get("author_key")) if (key := _author_key(value))
                )
            return candidate
    return None


def _works_from_payload(payload: object) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    for name in ("entries", "works", "docs"):
        values = payload.get(name)
        if isinstance(values, list):
            return [item for item in values[:MAX_RESPONSE_ITEMS] if isinstance(item, Mapping)]
    return []


def _payload_author_key(item: Mapping[str, Any]) -> str:
    for value in _field_list(item.get("key")):
        key = _author_key(value)
        if key:
            return key
    return ""


async def capture_typed_graph(
    *,
    name: str,
    seed_policy: str,
    prefix_records: Sequence[tuple[date | datetime, Mapping[str, Any], np.ndarray]],
    prefix_reads: Sequence[Mapping[str, Any]],
    boundary_day: date,
    request_client: OpenLibraryProbeClient,
) -> Capture:
    """Fetch verified author and subject edges for a strict-prefix seed set."""
    seeds = choose_seed_reads(prefix_records, seed_policy, boundary_day=boundary_day)
    capture = Capture(name=name, policy=seed_policy, seeds=seeds, request_client=request_client)
    for seed in seeds:
        try:
            resolved = await _resolve_seed(seed, request_client)
        except (httpx.HTTPError, ValueError, json.JSONDecodeError):
            capture.excluded["seed_resolution_failed"] += 1
            continue
        if resolved is None:
            capture.excluded["seed_identity_unverified"] += 1
            continue
        capture.seed_works[resolved.work_id] = resolved
        _merge_work(capture.works, resolved)

    for seed_work in list(capture.seed_works.values()):
        author_key = sorted(seed_work.author_keys)[0] if seed_work.author_keys else ""
        if author_key and request_client.remaining:
            try:
                payload = await request_client.get_json(
                    f"{author_key}/works.json", {"limit": MAX_RESPONSE_ITEMS}
                )
                capture.relation_degree_hints[("author", author_key)] = max(
                    capture.relation_degree_hints.get(("author", author_key), 0),
                    _reported_degree(payload),
                )
                capture.provider_returned += len(_works_from_payload(payload))
                for entry in _works_from_payload(payload):
                    work = _work_from_catalog(
                        entry,
                        source="author_works",
                        fallback_author=seed_work.author,
                        injected_author_key=author_key,
                    )
                    if work:
                        _merge_work(capture.works, work)
            except (httpx.HTTPError, ValueError, json.JSONDecodeError):
                capture.excluded["author_expansion_failed"] += 1
        elif author_key:
            pass
        else:
            capture.excluded["seed_author_key_missing"] += 1

        seed_subjects = list(seed_work.subjects.items())[:MAX_SUBJECT_QUERIES_PER_SEED]
        for subject_key, subject_label in seed_subjects:
            if not request_client.remaining:
                break
            if not _SUBJECT_SLUG_RE.fullmatch(subject_key):
                capture.excluded["invalid_subject_slug"] += 1
                continue
            try:
                payload = await request_client.get_json(
                    f"/subjects/{quote(subject_key, safe='_')}.json",
                    {"limit": MAX_RESPONSE_ITEMS},
                )
                capture.relation_degree_hints[("subject", subject_key)] = max(
                    capture.relation_degree_hints.get(("subject", subject_key), 0),
                    _reported_degree(payload),
                )
                capture.provider_returned += len(_works_from_payload(payload))
                for entry in _works_from_payload(payload):
                    work = _work_from_catalog(
                        entry,
                        source="subject_works",
                        injected_subject=(subject_key, subject_label),
                    )
                    if work:
                        _merge_work(capture.works, work)
            except (httpx.HTTPError, ValueError, json.JSONDecodeError):
                capture.excluded["subject_expansion_failed"] += 1

    seed_ids = set(capture.seed_works)
    for work_id, work in capture.works.items():
        if work_id in seed_ids:
            continue
        ok, reason = eligible_candidate(work, prefix_reads, cutoff_day=boundary_day)
        if not ok:
            capture.excluded[reason] += 1
            continue
        capture.eligible[work_id] = work
    for candidate in capture.eligible.values():
        for seed_work in capture.seed_works.values():
            if candidate.author_keys & seed_work.author_keys:
                capture.graph_edge_counts["same_author"] += 1
            if set(candidate.subjects) & set(seed_work.subjects):
                capture.graph_edge_counts["shared_subject"] += 1
            if candidate.series_keys & seed_work.series_keys:
                capture.graph_edge_counts["same_series"] += 1
    return capture


async def capture_list_baseline(
    *,
    name: str,
    prefix_reads: Sequence[Mapping[str, Any]],
    boundary_day: date,
    request_client: OpenLibraryProbeClient,
) -> Capture:
    """Run the existing current-seed list adapter without persistence/cache writes."""

    class CappedClient:
        async def get_json(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
            return await request_client.get_json(path, params)

    provider = OpenLibraryListProvider(
        client=CappedClient(),  # type: ignore[arg-type]
        max_seeds=SEED_BUDGET,
        max_per_author=2,
        max_lists_per_seed=1,
        max_list_size=500,
        max_items_per_list=MAX_RESPONSE_ITEMS,
        max_resolutions=8,
    )
    associations: list[Association] = await provider.collect(prefix_reads)
    capture = Capture(
        name=name,
        policy="current",
        seeds=select_seed_reads(prefix_reads, max_seeds=SEED_BUDGET, max_per_author=2),
        request_client=request_client,
        provider_returned=len(associations),
    )
    for provider_position, association in enumerate(associations):
        work_id = _work_key(association.external_id)
        if not work_id:
            capture.excluded["unverified_work_id"] += 1
            continue
        work = Work(
            work_id=work_id,
            title=_clean_text(association.title),
            author=_clean_text(association.author),
            source_url=f"{OPEN_LIBRARY_BASE}{work_id}",
            sources={"openlibrary_lists"},
        )
        ok, reason = eligible_candidate(work, prefix_reads, cutoff_day=boundary_day)
        if not ok:
            capture.excluded[reason] += 1
            continue
        _merge_work(capture.eligible, work)
        capture.provider_order.setdefault(work_id, provider_position)
    return capture


def one_hop_scores(capture: Capture) -> dict[str, float]:
    """Score verified two-edge typed paths with observed-degree normalization."""
    if not capture.seed_works:
        return {}
    result: dict[str, float] = defaultdict(float)
    seed_weight = 1.0 / len(capture.seed_works)
    for candidate_id, candidate in capture.eligible.items():
        for seed in capture.seed_works.values():
            shared: list[tuple[str, str]] = []
            shared.extend(("author", key) for key in candidate.author_keys & seed.author_keys)
            shared.extend(("subject", key) for key in candidate.subjects.keys() & seed.subjects.keys())
            shared.extend(("series", key) for key in candidate.series_keys & seed.series_keys)
            for relation, entity_key in shared:
                degree = sum(
                    1
                    for work in capture.seed_works.values()
                    if (entity_key in work.author_keys if relation == "author"
                        else entity_key in work.subjects if relation == "subject"
                        else entity_key in work.series_keys)
                )
                degree += sum(
                    1
                    for work in capture.eligible.values()
                    if (entity_key in work.author_keys if relation == "author"
                        else entity_key in work.subjects if relation == "subject"
                        else entity_key in work.series_keys)
                )
                hint = capture.relation_degree_hints.get((relation, entity_key), 0)
                degree = max(1, degree, hint)
                result[candidate_id] += seed_weight * RELATION_WEIGHTS[f"same_{relation}" if relation != "subject" else "shared_subject"] / degree
    return dict(result)


def _relation_values(work: Work, relation: str) -> set[str]:
    if relation == "author":
        return work.author_keys
    if relation == "subject":
        return set(work.subjects)
    return work.series_keys


def _transition_graph(capture: Capture) -> tuple[list[str], dict[str, dict[str, float]], dict[str, int]]:
    works = list(capture.seed_works.values()) + [
        work for key, work in capture.eligible.items() if key not in capture.seed_works
    ]
    nodes: set[str] = set()
    work_nodes: dict[str, str] = {}
    memberships: dict[tuple[str, str], set[str]] = defaultdict(set)
    relation_for_entity: dict[str, str] = {}
    for work in works:
        work_node = f"work:{work.work_id}"
        work_nodes[work.work_id] = work_node
        nodes.add(work_node)
        for relation in ("author", "subject", "series"):
            for entity in _relation_values(work, relation):
                entity_node = f"{relation}:{entity}"
                nodes.add(entity_node)
                memberships[(relation, entity_node)].add(work_node)
                relation_for_entity[entity_node] = relation
    ordered_nodes = sorted(nodes)
    transitions: dict[str, dict[str, float]] = {node: {} for node in ordered_nodes}
    degree_hints: dict[str, int] = {}
    for work in works:
        source = work_nodes[work.work_id]
        destinations: list[tuple[str, float]] = []
        for relation in ("author", "subject", "series"):
            weight = RELATION_WEIGHTS[
                "same_author" if relation == "author" else
                "shared_subject" if relation == "subject" else "same_series"
            ]
            for entity in _relation_values(work, relation):
                destinations.append((f"{relation}:{entity}", weight))
        total = sum(weight for _node, weight in destinations)
        if total:
            transitions[source] = {node: weight / total for node, weight in destinations}
    for entity_node, relation in relation_for_entity.items():
        entity = entity_node.split(":", 1)[1]
        works_for_entity = memberships[(relation, entity_node)]
        hint = capture.relation_degree_hints.get((relation, entity), 0)
        degree = max(1, len(works_for_entity), hint)
        degree_hints[entity_node] = degree
        transitions[entity_node] = {work_node: 1.0 / degree for work_node in sorted(works_for_entity)}
    return ordered_nodes, transitions, degree_hints


def diffusion_scores(capture: Capture, alpha: float) -> dict[str, float]:
    """Run bounded personalized PageRank over the captured typed work graph."""
    if not math.isfinite(alpha) or not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be strictly between zero and one")
    nodes, transitions, _degrees = _transition_graph(capture)
    if not nodes or not capture.seed_works:
        return {}
    indexes = {node: index for index, node in enumerate(nodes)}
    restart = np.zeros(len(nodes), dtype=np.float64)
    for work_id in capture.seed_works:
        node = f"work:{work_id}"
        if node in indexes:
            restart[indexes[node]] = 1.0 / len(capture.seed_works)
    rank = restart.copy()
    for _ in range(MAX_DIFFUSION_STEPS):
        updated = (1.0 - alpha) * restart
        for source, outgoing in transitions.items():
            mass = rank[indexes[source]]
            for target, probability in outgoing.items():
                updated[indexes[target]] += alpha * mass * probability
        if np.abs(updated - rank).sum() < 1e-10:
            rank = updated
            break
        rank = updated
    return {
        work_id: float(rank[indexes[f"work:{work_id}"]])
        for work_id in capture.eligible
        if f"work:{work_id}" in indexes
    }


def ranked_pool(
    capture: Capture,
    method: str,
    *,
    alpha: float | None = None,
) -> list[Work]:
    if capture.name == "baseline_current":
        return sorted(
            capture.eligible.values(),
            key=lambda work: (capture.provider_order.get(work.work_id, len(capture.provider_order)), work.work_id),
        )
    elif method == "one_hop":
        scores = one_hop_scores(capture)
    elif method == "diffusion":
        if alpha is None:
            raise ValueError("diffusion requires a preregistered alpha")
        scores = diffusion_scores(capture, alpha)
    else:
        raise ValueError("unknown retrieval method")
    return sorted(
        capture.eligible.values(),
        key=lambda work: (-scores.get(work.work_id, 0.0), work.work_id),
    )


def match_method(candidate: Mapping[str, Any], target: Mapping[str, Any]) -> str | None:
    """Return the strongest shared identity key without title-only matching."""
    left = book_row_identity_match_keys(candidate)
    right = book_row_identity_match_keys(target)
    shared = left & right
    if not shared:
        return None
    if any(len(key) == 3 and key[0] == "work" and key[1] == "openlibrary" for key in shared):
        return "openlibrary_work_id"
    if any(len(key) == 2 and key[0] == "isbn" for key in shared):
        return "isbn"
    return "exact_title_author"


def _target_match_map(
    works: Sequence[Work], targets: Sequence[tuple[date | datetime, Mapping[str, Any], np.ndarray]]
) -> tuple[dict[str, int], Counter[str]]:
    result: dict[str, int] = {}
    methods: Counter[str] = Counter()
    for work in works:
        candidate = work.candidate_row()
        matches = [
            index for index, (_day, target, _vector) in enumerate(targets)
            if match_method(candidate, target)
        ]
        if len(matches) == 1:
            result[work.work_id] = matches[0]
            method = match_method(candidate, targets[matches[0]][1])
            if method:
                methods[method] += 1
        elif len(matches) > 1:
            methods["ambiguous_identity"] += 1
    return result, methods


def _first_author_key(author: object) -> str:
    return book_identity("", author)


def _publication_guard(work: Work, cutoff_day: date) -> str:
    return _publication_status(work, cutoff_day)


def coverage_metrics(
    capture: Capture,
    ordered_pool: Sequence[Work],
    prefix_records: Sequence[tuple[date | datetime, Mapping[str, Any], np.ndarray]],
    targets: Sequence[tuple[date | datetime, Mapping[str, Any], np.ndarray]],
    *,
    requests_used: int,
    cutoff_day: date,
    score_top20: Sequence[str] | None = None,
) -> dict[str, Any]:
    target_indexes, join_methods = _target_match_map(ordered_pool, targets)
    prefix_reads = [item for _day, item, _vector in prefix_records]
    prefix_authors = {_first_author_key(item.get("author")) for item in prefix_reads}
    high_indexes = {i for i, (_day, item, _vector) in enumerate(targets) if float(item.get("rating") or 0) >= 4}
    low_indexes = {i for i, (_day, item, _vector) in enumerate(targets) if 1 <= float(item.get("rating") or 0) <= 2}
    unseen_author_indexes = {
        i for i, (_day, item, _vector) in enumerate(targets)
        if _first_author_key(item.get("author")) not in prefix_authors
    }
    matched_target_indexes = set(target_indexes.values())
    guarded_indexes: set[int] = set()
    unknown_publication_matches = 0
    future_publication_matches = 0
    works_by_id = {work.work_id: work for work in ordered_pool}
    for work_id, target_index in target_indexes.items():
        status = _publication_guard(works_by_id[work_id], cutoff_day)
        if status == "eligible":
            guarded_indexes.add(target_index)
        elif status in {"unknown", "unknown_within_year"}:
            unknown_publication_matches += 1
        else:
            future_publication_matches += 1
    high_unseen = high_indexes & unseen_author_indexes
    low_unseen = low_indexes & unseen_author_indexes
    target_key_match_methods = dict(sorted(join_methods.items()))
    top20_targets: set[int] = set()
    if score_top20:
        top20_targets = {target_indexes[key] for key in score_top20 if key in target_indexes}
    return {
        "eligible_unique_additions": len(ordered_pool),
        "retrieved_target_overlap": {
            "high_4_5": {
                "targets": len(high_indexes),
                "retrieved": len(matched_target_indexes & high_indexes),
                "recall": len(matched_target_indexes & high_indexes) / len(high_indexes) if high_indexes else None,
            },
            "low_1_2": {
                "targets": len(low_indexes),
                "retrieved": len(matched_target_indexes & low_indexes),
                "recall": len(matched_target_indexes & low_indexes) / len(low_indexes) if low_indexes else None,
            },
            "unseen_author_high_4_5": {
                "targets": len(high_unseen),
                "retrieved": len(matched_target_indexes & high_unseen),
                "recall": len(matched_target_indexes & high_unseen) / len(high_unseen) if high_unseen else None,
            },
            "unseen_author_low_1_2": {
                "targets": len(low_unseen),
                "retrieved": len(matched_target_indexes & low_unseen),
                "recall": len(matched_target_indexes & low_unseen) / len(low_unseen) if low_unseen else None,
            },
        },
        "publication_guarded_proxy": {
            "retrieved_targets_with_verified_publication_by_read_date": len(guarded_indexes),
            "high_4_5": len(guarded_indexes & high_indexes),
            "low_1_2": len(guarded_indexes & low_indexes),
            "unknown_publication_date_matches": unknown_publication_matches,
            "published_after_cutoff_matches_excluded": future_publication_matches,
            "unknown_publication_candidates_in_eligible_pool": sum(
                _publication_guard(work, cutoff_day) in {"unknown", "unknown_within_year"}
                for work in ordered_pool
            ),
            "source_timing": "current Open Library metadata; known publication after cutoff is excluded, and missing or year-only same-year dates are reported unknown",
        },
        "unique_new_author_candidates": sum(
            _first_author_key(work.author) not in prefix_authors for work in ordered_pool
        ),
        "target_identity_join_methods": target_key_match_methods,
        "ranker_top20_future_target_overlap": {
            "high_4_5": len(top20_targets & high_indexes),
            "low_1_2": len(top20_targets & low_indexes),
            "unseen_author_high_4_5": len(top20_targets & high_unseen),
        } if score_top20 is not None else None,
        "qualified_additions_per_request": len(ordered_pool) / requests_used if requests_used else None,
    }


def auc(labels: np.ndarray, scores: np.ndarray) -> float | None:
    positive, negative = scores[labels], scores[~labels]
    if not len(positive) or not len(negative):
        return None
    return float(((positive[:, None] > negative).sum() + .5 * (positive[:, None] == negative).sum()) / (len(positive) * len(negative)))


def target_vector_proxy(
    capture: Capture,
    ordered_pool: Sequence[Work],
    prefix_records: Sequence[tuple[date | datetime, Mapping[str, Any], np.ndarray]],
    targets: Sequence[tuple[date | datetime, Mapping[str, Any], np.ndarray]],
    *,
    score_limit: int = 250,
) -> dict[str, Any]:
    """Score retrieved held-out targets with their cached read vectors.

    This isolates retrieval coverage from current candidate text. Target ratings
    are read only after the pool and retrieval order have been frozen.
    """
    from afterword_engine.ranking import rank_candidates

    prefix_reads = [dict(item) for _day, item, _vector in prefix_records]
    prefix_vectors = np.stack([vector for _day, _item, vector in prefix_records]) if prefix_records else np.empty((0, 0), dtype=np.float32)
    target_map, _methods = _target_match_map(ordered_pool, targets)
    selected: list[tuple[Work, int]] = [
        (work, target_map[work.work_id])
        for work in ordered_pool[:score_limit]
        if work.work_id in target_map
    ]
    if not selected:
        return {"status": "no_matched_targets", "matched_targets_scored": 0}
    items = [work.candidate_row(enriched=False) for work, _index in selected]
    vectors = np.stack([targets[index][2] for _work, index in selected])
    if prefix_vectors.size and vectors.shape[1] != prefix_vectors.shape[1]:
        return {"status": "dimension_mismatch", "matched_targets_scored": 0}
    scores = rank_candidates(prefix_reads, prefix_vectors, items, vectors)
    by_work = {work.work_id: float(score["score"]) for (work, _), score in zip(selected, scores)}
    labels = np.asarray([float(targets[index][1].get("rating") or 0) >= 4 for _work, index in selected], dtype=bool)
    low_mask = np.asarray([1 <= float(targets[index][1].get("rating") or 0) <= 2 for _work, index in selected], dtype=bool)
    high_low = labels | low_mask
    high_low_scores = np.asarray([by_work[work.work_id] for work, _index in selected])
    return {
        "status": "scored",
        "matched_targets_scored": len(selected),
        "matched_high_targets": int(labels.sum()),
        "matched_low_targets": int(low_mask.sum()),
        "auc_high_4_5_vs_low_1_2": auc(labels[high_low], high_low_scores[high_low]),
        "highest_scored_target_ranks": [
            {"rank": rank + 1, "rating_class": "high" if float(targets[index][1].get("rating") or 0) >= 4 else "low" if float(targets[index][1].get("rating") or 0) <= 2 else "neutral"}
            for rank, (work, index) in enumerate(
                sorted(selected, key=lambda pair: (-by_work[pair[0].work_id], pair[0].work_id))[:20]
            )
        ],
        "limitation": "Target-vector ranking conditions on retrieved, future-rated identities; it is a representation-neutral retrieval proxy, not a production slate metric.",
    }


async def _verify_ollama_digest(url: str, model: str, expected_digest: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Ollama embedding URL must be loopback-only")
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        response = await client.get(f"{url.rstrip('/')}/api/tags")
        response.raise_for_status()
        payload = response.json()
    models = payload.get("models", []) if isinstance(payload, Mapping) else []
    actual = next(
        (str(entry.get("digest") or "") for entry in models if isinstance(entry, Mapping) and entry.get("name") == model),
        "",
    )
    if not actual:
        raise ValueError("requested local embedding model was not found in Ollama tags")
    if expected_digest and actual != expected_digest:
        raise ValueError("local embedding model digest does not match the frozen study digest")
    return actual


async def _embed_candidate_texts(
    works: Sequence[Work],
    *,
    model: str,
    url: str,
    enriched: bool,
    vector_cache: dict[str, list[float]],
) -> tuple[np.ndarray, str]:
    texts = [document(work.candidate_row(enriched=enriched)) for work in works]
    missing_texts = list(dict.fromkeys(text for text in texts if text not in vector_cache))
    for start in range(0, len(missing_texts), 64):
        batch = missing_texts[start:start + 64]
        async with httpx.AsyncClient(timeout=120, trust_env=False) as client:
            response = await client.post(
                f"{url.rstrip('/')}/api/embed",
                json={"model": model, "input": batch},
            )
            response.raise_for_status()
            payload = response.json()
        encoded = payload.get("embeddings", []) if isinstance(payload, Mapping) else []
        if len(encoded) != len(batch):
            raise ValueError("Ollama returned the wrong number of candidate vectors")
        for text, vector in zip(batch, encoded):
            values = np.asarray(vector, dtype=np.float32)
            if values.ndim != 1 or not values.size or not np.isfinite(values).all():
                raise ValueError("Ollama returned an invalid candidate vector")
            vector_cache[text] = values.tolist()
    matrix = np.asarray([vector_cache[text] for text in texts], dtype=np.float32)
    if matrix.ndim != 2 or (len(works) and not matrix.shape[1]) or not np.isfinite(matrix).all():
        raise ValueError("Ollama returned invalid candidate vectors")
    return matrix, hashlib.sha256("\n".join(document(work.candidate_row(enriched=enriched)) for work in works).encode()).hexdigest()


async def score_candidate_text_pool(
    ordered_pool: Sequence[Work],
    prefix_records: Sequence[tuple[date | datetime, Mapping[str, Any], np.ndarray]],
    targets: Sequence[tuple[date | datetime, Mapping[str, Any], np.ndarray]],
    *,
    model: str,
    url: str,
    enriched: bool,
    score_limit: int = 250,
    vector_cache: dict[str, list[float]] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Run the unchanged champion ranker on a deterministic bounded pool."""
    from afterword_engine.ranking import rank_candidates

    pool = list(ordered_pool[:score_limit])
    if len(ordered_pool) > score_limit:
        return {
            "status": "pool_exceeds_declared_cap",
            "captured_candidates": len(ordered_pool),
            "declared_cap": score_limit,
        }, []
    if not pool:
        return {"status": "empty_pool", "scored_candidates": 0}, []
    candidate_vectors, text_hash = await _embed_candidate_texts(
        pool,
        model=model,
        url=url,
        enriched=enriched,
        vector_cache=vector_cache if vector_cache is not None else {},
    )
    reads = [dict(item) for _day, item, _vector in prefix_records]
    read_vectors = np.stack([vector for _day, _item, vector in prefix_records]) if prefix_records else np.empty((0, candidate_vectors.shape[1]), dtype=np.float32)
    if read_vectors.size and read_vectors.shape[1] != candidate_vectors.shape[1]:
        return {"status": "dimension_mismatch", "scored_candidates": 0, "candidate_text_sha256": text_hash}, []
    candidates = [work.candidate_row(enriched=enriched) for work in pool]
    ranked = rank_candidates(reads, read_vectors, candidates, candidate_vectors)
    target_map, _methods = _target_match_map(pool, targets)
    top20 = [str(item.get("work_id")) for item in ranked[:20]]
    high = low = unseen_high = 0
    prefix_authors = {_first_author_key(item.get("author")) for item in reads}
    for row in ranked[:20]:
        key = str(row.get("work_id") or "")
        if key not in target_map:
            continue
        _day, target, _vector = targets[target_map[key]]
        rating = float(target.get("rating") or 0)
        high += rating >= 4
        low += 1 <= rating <= 2
        unseen_high += rating >= 4 and _first_author_key(target.get("author")) not in prefix_authors
    return {
        "status": "scored",
        "scored_candidates": len(pool),
        "candidate_text": "current_candidate_template_enriched" if enriched else "title_author_compatible",
        "candidate_text_sha256": text_hash,
        "backend": "ollama",
        "model": model,
        "vector_dimensions": int(candidate_vectors.shape[1]),
        "future_targets_in_ranker_top20": {
            "high_4_5": int(high),
            "low_1_2": int(low),
            "unseen_author_high_4_5": int(unseen_high),
        },
    }, top20


def _whole_day_cuts(records: Sequence[tuple[date | datetime, Mapping[str, Any], np.ndarray]]) -> tuple[int, int]:
    dates = [entry[0].date() if isinstance(entry[0], datetime) else entry[0] for entry in records]
    cuts = []
    for fraction in (0.6, 0.8):
        cut = int(len(records) * fraction)
        while cut < len(records) and cut > 0 and dates[cut] == dates[cut - 1]:
            cut += 1
        cuts.append(cut)
    return cuts[0], cuts[1]


def _arm_summary(capture: Capture) -> dict[str, Any]:
    result = {
        "seed_policy": capture.policy,
        "seeds": len(capture.seeds),
        "resolved_seed_works": len(capture.seed_works),
        "catalog_work_records_seen": len(capture.works),
        "provider_items_returned": capture.provider_returned,
        "eligible_unique_additions": len(capture.eligible),
        "exclusions": dict(sorted(capture.excluded.items())),
        "typed_edge_counts": dict(sorted(capture.graph_edge_counts.items())),
        "verified_series_edge_count": int(capture.graph_edge_counts.get("same_series", 0)),
    }
    if capture.request_client:
        result["public_request_budget"] = capture.request_client.aggregate()
    return result


def _private_capture_record(capture: Capture) -> dict[str, Any]:
    """Serialize the exact source graph for private, no-HTTP later replays.

    This intentionally contains catalog titles, Open Library identifiers,
    seed-read IDs, and explicit relation evidence. Callers must write it only
    under the mode-0700 study directory using _write_private_json(). Held-out
    target rows and ratings are never attached to a Capture.
    """
    def work_record(work: Work) -> dict[str, Any]:
        return {
            "work_id": work.work_id,
            "title": work.title,
            "author": work.author,
            "author_keys": sorted(work.author_keys),
            "subjects": dict(sorted(work.subjects.items())),
            "explicit_series_keys": sorted(work.series_keys),
            "genres": work.genres,
            "description": work.description,
            "first_publish_year": work.first_publish_year,
            "first_publish_date": work.first_publish_date.isoformat() if work.first_publish_date else None,
            "isbn": work.isbn,
            "source_url": work.source_url,
            "source_evidence": sorted(work.sources),
            "identity_conflict": work.identity_conflict,
        }

    return {
        "capture": capture.name,
        "seed_policy": capture.policy,
        "seed_reads": [
            {
                "read_id": int(seed.get("id") or 0),
                "title": _clean_text(seed.get("title"), 500),
                "author": _clean_text(seed.get("author"), 300),
                "read_at": _clean_text(seed.get("read_at") or seed.get("date_read"), 80),
            }
            for seed in capture.seeds
        ],
        "resolved_seed_works": [
            work_record(work) for _work_id, work in sorted(capture.seed_works.items())
        ],
        "catalog_works": [
            work_record(work) for _work_id, work in sorted(capture.works.items())
        ],
        "eligible_pool_in_retrieval_order": [
            work_record(work) for work in ranked_pool(
                capture,
                "baseline" if capture.name == "baseline_current" else "one_hop",
            )
        ],
        "relation_degree_hints": [
            {"relation": relation, "entity_key": entity_key, "degree_hint": degree}
            for (relation, entity_key), degree in sorted(capture.relation_degree_hints.items())
        ],
        "graph_edge_counts": dict(sorted(capture.graph_edge_counts.items())),
        "excluded": dict(sorted(capture.excluded.items())),
        "public_request_budget": capture.request_client.aggregate() if capture.request_client else None,
    }


def _work_from_private_record(row: Mapping[str, Any]) -> Work:
    parsed_date: date | None = None
    raw_date = row.get("first_publish_date")
    if raw_date:
        try:
            parsed_date = date.fromisoformat(str(raw_date))
        except ValueError:
            parsed_date = None
    return Work(
        work_id=str(row.get("work_id") or ""),
        title=str(row.get("title") or ""),
        author=str(row.get("author") or ""),
        author_keys={str(value) for value in row.get("author_keys", [])},
        subjects={str(key): str(value) for key, value in dict(row.get("subjects") or {}).items()},
        series_keys={str(value) for value in row.get("explicit_series_keys", [])},
        genres=[str(value) for value in row.get("genres", [])],
        description=str(row.get("description") or ""),
        first_publish_year=(int(row["first_publish_year"]) if row.get("first_publish_year") is not None else None),
        first_publish_date=parsed_date,
        isbn=str(row.get("isbn") or ""),
        source_url=str(row.get("source_url") or ""),
        sources={str(value) for value in row.get("source_evidence", [])},
        identity_conflict=bool(row.get("identity_conflict")),
    )


def _capture_from_private_record(row: Mapping[str, Any]) -> Capture:
    name = str(row.get("capture") or "")
    policy = str(row.get("seed_policy") or "")
    seed_works = {
        item.work_id: item
        for item in (_work_from_private_record(value) for value in row.get("resolved_seed_works", []))
    }
    works = {
        item.work_id: item
        for item in (_work_from_private_record(value) for value in row.get("catalog_works", []))
    }
    eligible_items = [
        _work_from_private_record(value)
        for value in row.get("eligible_pool_in_retrieval_order", [])
    ]
    eligible = {item.work_id: item for item in eligible_items}
    degree_hints = {
        (str(item.get("relation") or ""), str(item.get("entity_key") or "")): int(item.get("degree_hint") or 0)
        for item in row.get("relation_degree_hints", [])
    }
    return Capture(
        name=name,
        policy=policy,
        seeds=[],
        works=works,
        seed_works=seed_works,
        eligible=eligible,
        relation_degree_hints=degree_hints,
        graph_edge_counts=Counter({str(key): int(value) for key, value in dict(row.get("graph_edge_counts") or {}).items()}),
        provider_order={item.work_id: index for index, item in enumerate(eligible_items)},
    )


async def run_candidate_replay(args: argparse.Namespace) -> dict[str, Any]:
    """Run the fixed text ranker over the exact private pools without HTTP."""
    for path in (args.corpus, args.protocol, args.features_manifest, args.replay_captures):
        _require_private_file(path)
    protocol, feature_manifest, (validation_cut, later_cut) = _load_frozen_artifacts(args)
    if not args.score_candidate_text:
        raise ValueError("--score-candidate-text is required for capture replay")
    if args.candidate_model != FROZEN_MODEL or args.expected_model_digest != FROZEN_MODEL_DIGEST:
        raise ValueError("candidate replay must use the frozen local qwen model and digest")
    raw_captures = json.loads(args.replay_captures.read_text(encoding="utf-8"))
    if (
        raw_captures.get("protocol_sha256") != FROZEN_PROTOCOL_SHA256
        or raw_captures.get("corpus_sha256") != _manifest_digest(args.corpus)
        or raw_captures.get("target_rows_or_ratings_included") is not False
    ):
        raise ValueError("private capture artifact does not match the frozen protocol and corpus")

    import evaluate_ranking as historical

    data = historical.load_corpus(args.corpus)
    records, selected_embedding, excluded = historical.prepare(data, args.backend, args.read_model)
    if len(records) != int(feature_manifest.get("prepared_records") or -1):
        raise ValueError("prepared corpus row count differs from the frozen feature manifest")
    if _whole_day_cuts(records) != (validation_cut, later_cut):
        raise ValueError("prepared corpus whole-day cuts differ from the frozen feature manifest")
    encoder = protocol["encoder"]
    if selected_embedding != (encoder.get("backend"), encoder.get("model")):
        raise ValueError("read embedding cache differs from the frozen protocol")
    if not records or records[0][2].shape[0] != encoder.get("dimensions"):
        raise ValueError("read embedding dimensions differ from the frozen protocol")
    folds = {
        "validation": (records[:validation_cut], records[validation_cut:later_cut]),
        "later_test": (records[:later_cut], records[later_cut:]),
    }
    if set(raw_captures.get("folds", {})) != set(folds):
        raise ValueError("private capture artifact is missing a frozen temporal boundary")
    digest = await _verify_ollama_digest(args.ollama_url, args.candidate_model, FROZEN_MODEL_DIGEST)
    vector_cache: dict[str, list[float]] = {}
    result: dict[str, Any] = {
        "protocol_sha256": FROZEN_PROTOCOL_SHA256,
        "corpus_sha256": _manifest_digest(args.corpus),
        "capture_sha256": _manifest_digest(args.replay_captures),
        "candidate_embedding_model": {
            "backend": "ollama",
            "model": args.candidate_model,
            "digest": digest,
            "dimensions": encoder["dimensions"],
        },
        "public_http_requests": 0,
        "fixed_ranker": "afterword_engine.ranking.rank_candidates; no scorer tuning",
        "candidate_score_pool_cap": args.score_limit,
        "folds": {},
        "excluded_by_prepare": excluded,
    }
    for fold_name, (prefix_records, targets) in folds.items():
        private_fold = raw_captures["folds"][fold_name]
        captured = private_fold.get("captures", {})
        fold_result: dict[str, Any] = {"arms": {}}
        for arm_name in ("list_current", "typed_current", "typed_recent", "typed_mixed"):
            if arm_name not in captured:
                raise ValueError(f"private capture artifact is missing {fold_name}/{arm_name}")
            capture = _capture_from_private_record(captured[arm_name])
            ordered_pool = list(capture.eligible.values())
            fold_result["arms"][arm_name] = await _ranker_metrics_for_pool(
                capture,
                ordered_pool,
                prefix_records,
                targets,
                cutoff_day=(targets[0][0].date() if isinstance(targets[0][0], datetime) else targets[0][0]),
                model=args.candidate_model,
                ollama_url=args.ollama_url,
                score_limit=args.score_limit,
                vector_cache=vector_cache,
            )
        if private_fold.get("diffusion_reuses_capture") != "typed_current":
            raise ValueError("private capture artifact has an unsupported diffusion relationship")
        fold_result["arms"]["diffusion_alpha_0.6"] = await _ranker_metrics_for_pool(
            _capture_from_private_record(captured["typed_current"]),
            ranked_pool(_capture_from_private_record(captured["typed_current"]), "diffusion", alpha=FROZEN_DIFFUSION_ALPHA),
            prefix_records,
            targets,
            cutoff_day=(targets[0][0].date() if isinstance(targets[0][0], datetime) else targets[0][0]),
            model=args.candidate_model,
            ollama_url=args.ollama_url,
            score_limit=args.score_limit,
            vector_cache=vector_cache,
        )
        result["folds"][fold_name] = fold_result
        if args.progress:
            print(json.dumps({
                "replay_fold_complete": fold_name,
                "arms_scored": len(fold_result["arms"]),
                "unique_texts_embedded_so_far": len(vector_cache),
            }), flush=True)
    result["candidate_embedding_cache"] = {
        "unique_texts_embedded": len(vector_cache),
        "reuse": "identical candidate text is embedded once and reused across arms/folds",
    }
    return result


def _score_free_metrics(
    capture: Capture,
    method: str,
    prefix_records: Sequence[tuple[date | datetime, Mapping[str, Any], np.ndarray]],
    targets: Sequence[tuple[date | datetime, Mapping[str, Any], np.ndarray]],
    *,
    cutoff_day: date,
    alpha: float | None = None,
) -> tuple[dict[str, Any], list[Work]]:
    full_pool = ranked_pool(capture, method, alpha=alpha)
    metrics = coverage_metrics(
        capture,
        full_pool,
        prefix_records,
        targets,
        requests_used=capture.request_client.requests if capture.request_client else 0,
        cutoff_day=cutoff_day,
    )
    return metrics, full_pool


async def _ranker_metrics_for_pool(
    capture: Capture,
    ordered_pool: Sequence[Work],
    prefix_records: Sequence[tuple[date | datetime, Mapping[str, Any], np.ndarray]],
    targets: Sequence[tuple[date | datetime, Mapping[str, Any], np.ndarray]],
    *,
    cutoff_day: date,
    model: str,
    ollama_url: str,
    score_limit: int,
    vector_cache: dict[str, list[float]],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "target_vector_proxy": target_vector_proxy(
            capture, ordered_pool, prefix_records, targets, score_limit=score_limit
        ),
        "candidate_text_ranker": {},
    }
    for enriched in (False, True):
        label = "enriched" if enriched else "compatible"
        try:
            scored, top20 = await score_candidate_text_pool(
                ordered_pool,
                prefix_records,
                targets,
                model=model,
                url=ollama_url,
                enriched=enriched,
                score_limit=score_limit,
                vector_cache=vector_cache,
            )
        except Exception as exc:
            scored, top20 = {"status": "error", "error_type": type(exc).__name__}, []
        if top20:
            scored["future_target_overlap_in_top20"] = coverage_metrics(
                capture,
                ordered_pool,
                prefix_records,
                targets,
                requests_used=capture.request_client.requests if capture.request_client else 0,
                cutoff_day=cutoff_day,
                score_top20=top20,
            )["ranker_top20_future_target_overlap"]
        result["candidate_text_ranker"][label] = scored
    return result


def _manifest_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_private_file(path: Path) -> None:
    resolved = path.resolve()
    try:
        resolved.relative_to(PRIVATE_STUDY_DIR.resolve())
    except ValueError as exc:
        raise ValueError("private study inputs and outputs must stay under the study directory") from exc
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    if resolved.stat().st_mode & 0o077:
        raise ValueError("private study files must have mode 0600")


def _load_frozen_artifacts(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any], tuple[int, int]]:
    for path in (args.corpus, args.protocol, args.features_manifest):
        _require_private_file(path)
    if _manifest_digest(args.protocol) != FROZEN_PROTOCOL_SHA256:
        raise ValueError("protocol-private.json does not match the parent-approved frozen hash")
    protocol = json.loads(args.protocol.read_text(encoding="utf-8"))
    features = json.loads(args.features_manifest.read_text(encoding="utf-8"))
    corpus_hash = _manifest_digest(args.corpus)
    if corpus_hash != FROZEN_CORPUS_SHA256:
        raise ValueError("corpus-private.json does not match the frozen corpus hash")
    if protocol.get("corpus_sha256") != corpus_hash or features.get("corpus_sha256") != corpus_hash:
        raise ValueError("private corpus and frozen manifests do not describe the same snapshot")
    encoder = protocol.get("encoder", {})
    if encoder.get("model") != FROZEN_MODEL or encoder.get("digest") != FROZEN_MODEL_DIGEST or encoder.get("dimensions") != 2560:
        raise ValueError("frozen protocol embedding model does not match the approved model")
    retrieval = protocol.get("families", {}).get("retrieval", {})
    if retrieval.get("request_ceiling_each_arm_boundary") != REQUEST_CEILING:
        raise ValueError("request ceiling differs from the frozen protocol")
    if retrieval.get("max_items_per_response") != MAX_RESPONSE_ITEMS:
        raise ValueError("response-item limit differs from the frozen protocol")
    if retrieval.get("diffusion") != "sameevidence as typed_current,noextraHTTP,fixedalpha0.6":
        raise ValueError("diffusion setup differs from the frozen protocol")
    cuts = features.get("record_cuts")
    if not isinstance(cuts, list) or len(cuts) != 2 or not all(isinstance(value, int) for value in cuts):
        raise ValueError("feature manifest must contain the two frozen record cuts")
    return protocol, features, (cuts[0], cuts[1])


def _query_profile_hash(records: Sequence[tuple[date | datetime, Mapping[str, Any], np.ndarray]]) -> str:
    digest = hashlib.sha256()
    for _day, item, vector in records:
        digest.update(content_hash(document(item)).encode())
        digest.update(np.asarray(vector, dtype=np.float32).tobytes())
    return digest.hexdigest()


def _write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    try:
        path.resolve().relative_to(PRIVATE_STUDY_DIR.resolve())
    except ValueError as exc:
        raise ValueError("aggregate output must stay under the private study directory") from exc
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)


async def run_study(args: argparse.Namespace) -> dict[str, Any]:
    if not args.run_public_probe:
        raise ValueError("--run-public-probe is required; no public requests are made otherwise")
    protocol, feature_manifest, (validation_cut, later_cut) = _load_frozen_artifacts(args)
    if args.score_candidate_text:
        if args.candidate_model != FROZEN_MODEL or args.expected_model_digest != FROZEN_MODEL_DIGEST:
            raise ValueError("candidate scoring must use the frozen local qwen model and digest")

    import evaluate_ranking as historical

    data = historical.load_corpus(args.corpus)
    records, selected_embedding, excluded = historical.prepare(data, args.backend, args.read_model)
    if len(records) != int(feature_manifest.get("prepared_records") or -1):
        raise ValueError("prepared corpus row count differs from the frozen feature manifest")
    if _whole_day_cuts(records) != (validation_cut, later_cut):
        raise ValueError("prepared corpus whole-day cuts differ from the frozen feature manifest")
    encoder = protocol["encoder"]
    if selected_embedding != (encoder.get("backend"), encoder.get("model")):
        raise ValueError("read embedding cache differs from the frozen protocol")
    if not records or records[0][2].shape[0] != encoder.get("dimensions"):
        raise ValueError("read embedding dimensions differ from the frozen protocol")
    folds = (
        ("validation", records[:validation_cut], records[validation_cut:later_cut]),
        ("later_test", records[:later_cut], records[later_cut:]),
    )
    limiter = RateLimiter(1.0)
    candidate_vector_cache: dict[str, list[float]] = {}
    output: dict[str, Any] = {
        "protocol": {
            "version": 1,
            "frozen_protocol_sha256": FROZEN_PROTOCOL_SHA256,
            "feature_manifest_sha256": _manifest_digest(args.features_manifest),
            "registration_status": "matches parent-frozen private protocol; chronology is externally frozen",
            "folds": ["validation", "later_test"],
            "fold_cuts": {"validation_start": validation_cut, "later_start": later_cut},
            "whole_calendar_days_kept_together": True,
            "seed_history_strictly_precedes_fold": True,
            "seed_budget_per_capture": SEED_BUDGET,
            "public_http_get_ceiling_per_capture": REQUEST_CEILING,
            "public_http_rate_limit_seconds_global": 1.0,
            "automatic_retries": 0,
            "arms": ["list_current", "typed_current", "typed_recent", "typed_mixed", "diffusion_alpha_0.6"],
            "fixed_ranker": "afterword_engine.ranking.rank_candidates; no scorer tuning",
            "target_rating_classes": {"high": "4-5", "low": "1-2", "neutral": "3"},
            "target_ratings_used_only_after_retrieval_pool_is_frozen": True,
            "candidate_score_pool_cap": args.score_limit,
            "series_policy": "exact explicit series metadata only; no title-derived series membership/order",
            "query_profile_shared_across_all_arms_per_boundary": True,
            "retrieval_diffusion_alpha": FROZEN_DIFFUSION_ALPHA,
            "retrieval_source_snapshot": "current Open Library public catalog",
        },
        "embedding_cache": {"backend": selected_embedding[0], "model": selected_embedding[1]},
        "corpus_sha256": _manifest_digest(args.corpus),
        "excluded_by_prepare": excluded,
        "folds": {},
        "limitations": [
            "This is a current-catalog retrieval proxy; current Open Library pages cannot reconstruct candidate availability at historical read dates.",
            "The target universe is the reader's future dated rated-work cohort, not all books a reader could discover.",
            "Known first publication after a fold cutoff is excluded. Missing dates and year-only dates within the cutoff year remain eligible but are reported as unknown.",
            "Public API result pages provide only observed degree unless the response reports a larger work count; degree normalization can therefore be a lower bound.",
            "A verified series edge requires an explicit series field on both catalog work records. Titles and subjects never create series membership or order; no series order is inferred.",
            "Candidate-text ranker metrics use the same frozen local encoder digest for all arms. Compatible title-author text is the pool comparison; enriched text is a separate representation sensitivity.",
            "Exact association evidence and candidate rows are saved to a private mode-0600 replay artifact; no production database/cache/candidate writes are performed.",
        ],
    }
    private_captures: dict[str, Any] = {
        "protocol_sha256": FROZEN_PROTOCOL_SHA256,
        "corpus_sha256": _manifest_digest(args.corpus),
        "target_rows_or_ratings_included": False,
        "folds": {},
    }

    if args.score_candidate_text:
        digest = await _verify_ollama_digest(args.ollama_url, args.candidate_model, FROZEN_MODEL_DIGEST)
        output["candidate_embedding_model"] = {
            "backend": "ollama",
            "model": args.candidate_model,
            "digest": digest,
            "dimensions": encoder["dimensions"],
        }

    for fold_name, prefix_records, targets in folds:
        if not prefix_records or not targets:
            raise ValueError(f"{fold_name} split is empty")
        cutoff_record = targets[0][0]
        boundary_day = cutoff_record.date() if isinstance(cutoff_record, datetime) else cutoff_record
        prefix_reads = [dict(item) for _day, item, _vector in prefix_records]
        fold_output: dict[str, Any] = {
            "prefix_reads": len(prefix_records),
            "shared_query_profile_sha256": _query_profile_hash(prefix_records),
            "fixed_future_target_cohort": {
                "targets": len(targets),
                "high_4_5": sum(float(item.get("rating") or 0) >= 4 for _day, item, _vector in targets),
                "low_1_2": sum(1 <= float(item.get("rating") or 0) <= 2 for _day, item, _vector in targets),
                "neutral_3": sum(float(item.get("rating") or 0) == 3 for _day, item, _vector in targets),
            },
            "arms": {},
        }
        private_fold: dict[str, Any] = {"captures": {}}

        baseline_client = OpenLibraryProbeClient(rate_limiter=limiter)
        try:
            baseline = await capture_list_baseline(
                name="baseline_current",
                prefix_reads=prefix_reads,
                boundary_day=boundary_day,
                request_client=baseline_client,
            )
        except BudgetExhausted:
            baseline = Capture(name="baseline_current", policy="current", seeds=[], request_client=baseline_client)
        private_fold["captures"]["list_current"] = _private_capture_record(baseline)
        base_metrics, base_order = _score_free_metrics(
            baseline, "baseline", prefix_records, targets, cutoff_day=boundary_day
        )
        baseline_result = _arm_summary(baseline)
        baseline_result["retrieval_metrics"] = base_metrics
        if args.score_candidate_text:
            baseline_result["current_ranker"] = await _ranker_metrics_for_pool(
                baseline,
                base_order,
                prefix_records,
                targets,
                cutoff_day=boundary_day,
                model=args.candidate_model,
                ollama_url=args.ollama_url,
                score_limit=args.score_limit,
                vector_cache=candidate_vector_cache,
            )
        fold_output["arms"]["list_current"] = baseline_result

        typed_captures: dict[str, Capture] = {}
        for policy in ("current", "recent", "mixed"):
            typed_client = OpenLibraryProbeClient(rate_limiter=limiter)
            try:
                typed = await capture_typed_graph(
                    name=f"typed_{policy}",
                    seed_policy=policy,
                    prefix_records=prefix_records,
                    prefix_reads=prefix_reads,
                    boundary_day=boundary_day,
                    request_client=typed_client,
                )
            except BudgetExhausted:
                typed = Capture(name=f"typed_{policy}", policy=policy, seeds=[], request_client=typed_client)
            typed_captures[policy] = typed
            private_fold["captures"][f"typed_{policy}"] = _private_capture_record(typed)
            arm_output = _arm_summary(typed)
            one_metrics, one_order = _score_free_metrics(
                typed, "one_hop", prefix_records, targets, cutoff_day=boundary_day
            )
            arm_output["retrieval_metrics"] = one_metrics
            if args.score_candidate_text:
                arm_output["current_ranker"] = await _ranker_metrics_for_pool(
                    typed,
                    one_order,
                    prefix_records,
                    targets,
                    cutoff_day=boundary_day,
                    model=args.candidate_model,
                    ollama_url=args.ollama_url,
                    score_limit=args.score_limit,
                    vector_cache=candidate_vector_cache,
                )
            fold_output["arms"][f"typed_{policy}"] = arm_output

        # Diffusion is a pure reordering of the current-seed typed graph. It
        # shares the capture budget and consumes no additional public requests.
        typed_current = typed_captures["current"]
        private_fold["diffusion_reuses_capture"] = "typed_current"
        diffusion_metrics, diffusion_order = _score_free_metrics(
            typed_current,
            "diffusion",
            prefix_records,
            targets,
            cutoff_day=boundary_day,
            alpha=FROZEN_DIFFUSION_ALPHA,
        )
        diffusion_result: dict[str, Any] = {
            "same_evidence_as": "typed_current",
            "extra_public_http_requests": 0,
            "retrieval_metrics": diffusion_metrics,
        }
        if args.score_candidate_text:
            diffusion_result["current_ranker"] = await _ranker_metrics_for_pool(
                typed_current,
                diffusion_order,
                prefix_records,
                targets,
                cutoff_day=boundary_day,
                model=args.candidate_model,
                ollama_url=args.ollama_url,
                score_limit=args.score_limit,
                vector_cache=candidate_vector_cache,
            )
        fold_output["arms"]["diffusion_alpha_0.6"] = diffusion_result

        output["folds"][fold_name] = fold_output
        private_captures["folds"][fold_name] = private_fold
        if args.progress:
            progress = {
                "fold_complete": fold_name,
                "arms": {
                    arm_name: {
                        "eligible_unique_additions": arm_value.get("retrieval_metrics", {}).get("eligible_unique_additions"),
                        "requests_used": arm_value.get("public_request_budget", {}).get("requests_used"),
                    }
                    for arm_name, arm_value in fold_output["arms"].items()
                },
            }
            print(json.dumps(progress), flush=True)
    output["candidate_embedding_cache"] = {
        "unique_texts_embedded": len(candidate_vector_cache),
        "reuse": "identical candidate text is embedded once and reused across arms/folds",
    }
    # Kept out of the aggregate report by main(); exact candidate evidence is
    # private so an optional later scorer can replay the same graph without
    # spending more public requests.
    output["_private_captures"] = private_captures
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path, help="private fresh JSON or read-only SQLite snapshot")
    parser.add_argument("--output", type=Path, required=True, help="private aggregate JSON output path")
    parser.add_argument(
        "--capture-output",
        type=Path,
        help="private exact-capture artifact path (defaults beside --output)",
    )
    parser.add_argument("--backend", default="ollama", help="read-vector backend in the corpus")
    parser.add_argument("--read-model", default="qwen3-embedding:4b", help="read-vector model in the corpus")
    parser.add_argument("--protocol", type=Path, default=PRIVATE_STUDY_DIR / "protocol-private.json")
    parser.add_argument("--features-manifest", type=Path, default=PRIVATE_STUDY_DIR / "features-manifest-private.json")
    parser.add_argument("--run-public-probe", action="store_true", help="allow the bounded public Open Library GET study")
    parser.add_argument(
        "--replay-captures",
        type=Path,
        help="score an existing private capture artifact without making public requests",
    )
    parser.add_argument("--progress", action="store_true", help="print aggregate counts after each frozen boundary")
    parser.add_argument("--score-candidate-text", action="store_true", help="score candidate text locally with the frozen current ranker")
    parser.add_argument("--candidate-model", default=FROZEN_MODEL)
    parser.add_argument("--expected-model-digest", default=FROZEN_MODEL_DIGEST)
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--score-limit", type=int, default=1000)
    args = parser.parse_args()
    if args.score_limit < 1:
        parser.error("--score-limit must be positive")
    try:
        report = asyncio.run(
            run_candidate_replay(args) if args.replay_captures else run_study(args)
        )
    except Exception as exc:
        parser.exit(2, f"evaluation failed: {type(exc).__name__}: {exc}\n")
    if "_private_captures" in report:
        capture_path = args.capture_output or args.output.with_name(
            f"{args.output.stem}-captures-private.json"
        )
        private_captures = report.pop("_private_captures")
        _write_private_json(capture_path, private_captures)
    _write_private_json(args.output, report)
    print(json.dumps({
        "status": "complete",
        "mode": "capture_replay" if args.replay_captures else "public_capture",
        "output": str(args.output),
        "corpus_sha256": report["corpus_sha256"],
    }))


if __name__ == "__main__":
    main()
