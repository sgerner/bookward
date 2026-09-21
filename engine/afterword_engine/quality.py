"""Candidate identity, metadata quality, and catalog enrichment.

Source feeds are useful discovery signals, but they are not authoritative book
records.  This module keeps the ingest boundary conservative: malformed rows
are rejected, plausible rows without a catalog match are quarantined, and
accepted rows carry a stable provider/work/ISBN match when one is available.

The catalog lookups are deliberately best-effort.  A provider outage must not
erase a candidate that a reader explicitly saved, so the audit records the
failure and leaves the row in quarantine for a later retry.
"""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any
from urllib.parse import urlparse

import httpx

from .covers import (
    GOOGLE_BOOKS_SEARCH,
    OPEN_LIBRARY_SEARCH,
    metadata_client,
    safe_cover_url,
)
from .database import rows, transaction
from .identity import book_identity


QUALITY_VERSION = "candidate-quality-v1"
CATALOG_CONCURRENCY = 8
CATALOG_LIMIT = 10
UNKNOWN_AUTHOR_RE = re.compile(
    r"^(?:unknown(?:\s+author)?|n/?a|none|null|various(?:\s+authors?)?|anonymous|staff)$",
    re.IGNORECASE,
)
NOISE_TITLE_RE = re.compile(
    r"^(?:untitled|unknown|new releases?|coming soon|book list|books?|n/?a|none|null)$",
    re.IGNORECASE,
)


def _text(value: object, limit: int) -> str:
    value = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(value.strip().split())[:limit]


def _catalog_text(value: object) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    chars = [char if char.isalnum() or char.isspace() else " " for char in value]
    return " ".join("".join(chars).split())


def _tokens(value: object) -> set[str]:
    return set(_catalog_text(value).split())


def _title_similarity(left: object, right: object) -> float:
    a = _catalog_text(left)
    b = _catalog_text(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    token_a, token_b = _tokens(a), _tokens(b)
    shared = token_a & token_b
    overlap = len(shared) / max(1, len(token_a | token_b))
    return max(overlap, SequenceMatcher(None, a, b).ratio())


def _author_similarity(left: object, right: object) -> float:
    a = _catalog_text(left)
    b = _catalog_text(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    token_a, token_b = _tokens(a), _tokens(b)
    if not token_a or not token_b:
        return 0.0
    shared = token_a & token_b
    overlap = len(shared) / max(1, len(token_a | token_b))
    # Catalogs frequently omit middle initials or suffixes.  A matching
    # surname is useful evidence, but never enough to pass without a title
    # match because author names are not unique.
    parts_a = _catalog_text(left).split()
    parts_b = _catalog_text(right).split()
    surname_match = bool(shared) and parts_a[-1] == parts_b[-1]
    initial_match = surname_match and parts_a[0][:1] == parts_b[0][:1]
    # A surname alone is weak evidence ("A Writer" and "B Writer" are not
    # the same author). Matching first initials makes common catalog variants
    # such as "J.R.R. Tolkien" and "John Tolkien" useful without making an
    # exact title override an author mismatch.
    surname_score = 0.85 if initial_match else (0.55 if surname_match else 0.0)
    return max(overlap, surname_score)


def canonical_isbn(value: object) -> str:
    """Return a checksum-valid ISBN-10/13, or an empty string."""

    raw = re.sub(r"[^0-9Xx]", "", str(value or ""))
    if len(raw) == 13 and raw.isdigit():
        total = sum((1 if index % 2 == 0 else 3) * int(char) for index, char in enumerate(raw))
        return raw if total % 10 == 0 else ""
    if len(raw) == 10 and re.fullmatch(r"[0-9]{9}[0-9Xx]", raw):
        total = sum((10 - index) * (10 if char.upper() == "X" else int(char)) for index, char in enumerate(raw))
        if total % 11 != 0:
            return ""
        prefix = "978" + raw[:9]
        check = (10 - sum((1 if index % 2 == 0 else 3) * int(char) for index, char in enumerate(prefix)) % 10) % 10
        return prefix + str(check)
    return ""


def _first_isbn(values: object) -> tuple[str, str]:
    if not isinstance(values, (list, tuple, set)):
        values = [values]
    isbn13 = ""
    isbn10 = ""
    for value in values:
        raw = re.sub(r"[^0-9Xx]", "", str(value or ""))
        if len(raw) == 10 and not isbn10 and canonical_isbn(raw):
            isbn10 = raw.upper()
            isbn13 = canonical_isbn(raw)
        elif len(raw) == 13 and not isbn13 and canonical_isbn(raw):
            isbn13 = raw
    return isbn13, isbn10


def local_flags(candidate: dict[str, Any]) -> list[str]:
    """Return deterministic defects that do not require a network lookup."""

    title = _text(candidate.get("title"), 500)
    author = _text(candidate.get("author"), 300)
    flags: list[str] = []
    if len(title) < 2:
        flags.append("missing_title")
    elif NOISE_TITLE_RE.fullmatch(title):
        flags.append("noise_title")
    if len(author) < 2:
        flags.append("missing_author")
    elif UNKNOWN_AUTHOR_RE.fullmatch(author):
        flags.append("unknown_author")
    if title and author and _catalog_text(title) == _catalog_text(author):
        flags.append("title_equals_author")
    if title and ("http://" in title.casefold() or "https://" in title.casefold()):
        flags.append("title_contains_url")
    if len(_tokens(title)) < 1:
        flags.append("empty_title_tokens")
    source_url = str(candidate.get("source_url") or "").strip()
    if source_url and not source_url.startswith(("https://", "http://", "association://")):
        flags.append("invalid_source_url")
    return sorted(set(flags))


def _provider_match(
    *,
    provider: str,
    provider_id: str,
    work_id: str,
    title: str,
    author: str,
    catalog_title: str,
    catalog_author: str,
    isbn13: str = "",
    isbn10: str = "",
    description: str = "",
    release_date: str = "",
    cover_url: str = "",
) -> dict[str, Any]:
    title_match = _title_similarity(title, catalog_title)
    author_match = _author_similarity(author, catalog_author)
    confidence = min(1.0, (title_match * 0.65) + (author_match * 0.35))
    if title_match >= 0.98 and author_match >= 0.98:
        confidence = 1.0
    return {
        "provider": provider,
        "provider_id": provider_id,
        "work_id": work_id,
        "isbn13": isbn13,
        "isbn10": isbn10,
        "title_match": round(title_match, 4),
        "author_match": round(author_match, 4),
        "quality_score": round(confidence, 4),
        "catalog_title": _text(catalog_title, 500),
        "catalog_author": _text(catalog_author, 300),
        "description": _text(description, 4000),
        "release_date": _text(release_date, 32),
        "cover_url": cover_url,
    }


async def _open_library_match(title: str, author: str, client: httpx.AsyncClient) -> dict[str, Any] | None:
    params = {
        "title": title[:500],
        "author": author[:300],
        "limit": CATALOG_LIMIT,
        "fields": "key,title,author_name,cover_i,first_publish_year,first_publish_date,first_sentence,description,isbn,isbn13,edition_key",
    }
    try:
        response = await client.get(OPEN_LIBRARY_SEARCH, params=params)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return None
    docs = payload.get("docs", []) if isinstance(payload, dict) else []
    best: dict[str, Any] | None = None
    for doc in docs if isinstance(docs, list) else []:
        if not isinstance(doc, dict):
            continue
        authors = doc.get("author_name") or []
        if isinstance(authors, str):
            authors = [authors]
        catalog_author = ", ".join(str(value) for value in authors if value)
        isbn13, isbn10 = _first_isbn(doc.get("isbn13") or doc.get("isbn") or [])
        cover = ""
        if str(doc.get("cover_i") or "").isdigit():
            cover = f"https://covers.openlibrary.org/b/id/{int(doc['cover_i'])}-L.jpg"
        match = _provider_match(
            provider="openlibrary",
            provider_id=str(doc.get("key") or ""),
            work_id=str(doc.get("key") or ""),
            title=title,
            author=author,
            catalog_title=str(doc.get("title") or ""),
            catalog_author=catalog_author,
            isbn13=isbn13,
            isbn10=isbn10,
            description=str(doc.get("first_sentence") or doc.get("description") or ""),
            release_date=str(doc.get("first_publish_date") or doc.get("first_publish_year") or ""),
            cover_url=safe_cover_url(cover),
        )
        if best is None or match["quality_score"] > best["quality_score"]:
            best = match
    return best


async def _google_books_match(title: str, author: str, client: httpx.AsyncClient) -> dict[str, Any] | None:
    params = {"q": f"intitle:{title[:300]} inauthor:{author[:200]}", "maxResults": CATALOG_LIMIT}
    try:
        response = await client.get(GOOGLE_BOOKS_SEARCH, params=params)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return None
    best: dict[str, Any] | None = None
    for item in payload.get("items", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        info = item.get("volumeInfo") or {}
        if not isinstance(info, dict):
            continue
        authors = info.get("authors") or []
        if isinstance(authors, str):
            authors = [authors]
        identifiers = [value.get("identifier") for value in info.get("industryIdentifiers", []) if isinstance(value, dict)]
        isbn13, isbn10 = _first_isbn(identifiers)
        image_links = info.get("imageLinks") or {}
        image = ""
        if isinstance(image_links, dict):
            for key in ("extraLarge", "large", "medium", "thumbnail", "smallThumbnail"):
                if image_links.get(key):
                    image = safe_cover_url(str(image_links[key]).replace("http://", "https://"))
                    if image:
                        break
        match = _provider_match(
            provider="google_books",
            provider_id=str(item.get("id") or ""),
            work_id=str(item.get("id") or ""),
            title=title,
            author=author,
            catalog_title=str(info.get("title") or ""),
            catalog_author=", ".join(str(value) for value in authors if value),
            isbn13=isbn13,
            isbn10=isbn10,
            description=str(info.get("description") or ""),
            release_date=str(info.get("publishedDate") or ""),
            cover_url=image,
        )
        if best is None or match["quality_score"] > best["quality_score"]:
            best = match
    return best


async def resolve_catalog_match(
    title: str,
    author: str,
    *,
    client: httpx.AsyncClient,
    cache: dict[tuple[str, str], asyncio.Task[dict[str, Any] | None]],
) -> dict[str, Any] | None:
    """Find the strongest title/author match in public catalogs."""

    key = (_catalog_text(title), _catalog_text(author))
    existing = cache.get(key)
    if existing is not None:
        return await existing

    async def lookup():
        # Open Library is the primary public catalog.  Only fall through to
        # Google Books when it cannot produce a strong match; this halves the
        # normal request volume and makes a full audit kinder to both services.
        open_library = await _open_library_match(title, author, client)
        if open_library and open_library["quality_score"] >= 0.82:
            return open_library
        google = await _google_books_match(title, author, client)
        return max((item for item in (open_library, google) if item), key=lambda item: item["quality_score"], default=None)

    task = asyncio.create_task(lookup())
    cache[key] = task
    return await task


async def _audit_one(candidate: dict[str, Any], client: httpx.AsyncClient, cache):
    flags = local_flags(candidate)
    if any(flag in flags for flag in ("missing_title", "noise_title", "missing_author", "unknown_author", "title_equals_author", "title_contains_url")):
        return {
            "candidate_id": int(candidate["id"]),
            "quality_status": "rejected",
            "quality_score": 0.0,
            "flags": flags,
        }
    match = await resolve_catalog_match(candidate["title"], candidate["author"], client=client, cache=cache)
    if not match:
        return {
            "candidate_id": int(candidate["id"]),
            "quality_status": "quarantine",
            "quality_score": 0.0,
            "flags": sorted(set(flags + ["catalog_unmatched"])),
        }
    accepted = match["quality_score"] >= 0.82 and match["title_match"] >= 0.82 and match["author_match"] >= 0.65
    return {
        "candidate_id": int(candidate["id"]),
        "quality_status": "accepted" if accepted else "quarantine",
        "quality_score": match["quality_score"],
        "flags": sorted(set(flags + ([] if accepted else ["catalog_match_ambiguous"]))),
        **match,
    }


def _store_result(con, candidate: dict[str, Any], result: dict[str, Any]) -> bool:
    candidate_id = int(candidate["id"])
    flags = result.get("flags", [])
    con.execute(
        """INSERT INTO candidate_quality(
            candidate_id,quality_status,quality_score,flags_json,provider,provider_id,
            work_id,isbn13,isbn10,title_match,author_match,audit_version,audited_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
        ON CONFLICT(candidate_id) DO UPDATE SET
            quality_status=excluded.quality_status,
            quality_score=excluded.quality_score,
            flags_json=excluded.flags_json,
            provider=excluded.provider,
            provider_id=excluded.provider_id,
            work_id=excluded.work_id,
            isbn13=excluded.isbn13,
            isbn10=excluded.isbn10,
            title_match=excluded.title_match,
            author_match=excluded.author_match,
            audit_version=excluded.audit_version,
            audited_at=excluded.audited_at,
            updated_at=CURRENT_TIMESTAMP""",
        (
            candidate_id,
            result.get("quality_status", "quarantine"),
            float(result.get("quality_score", 0) or 0),
            json.dumps(flags, separators=(",", ":")),
            str(result.get("provider", "")),
            str(result.get("provider_id", "")),
            str(result.get("work_id", "")),
            str(result.get("isbn13", "")),
            str(result.get("isbn10", "")),
            float(result.get("title_match", 0) or 0),
            float(result.get("author_match", 0) or 0),
            QUALITY_VERSION,
        ),
    )
    if result.get("quality_status") == "rejected" and candidate.get("status") in {"new", "recommended"}:
        con.execute(
            "UPDATE candidates SET status='rejected',updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (candidate_id,),
        )
    # Catalog fields are additive.  A source's richer description/cover/date
    # remains authoritative, while empty or placeholder fields are replaced.
    description = _text(result.get("description"), 4000)
    cover_url = str(result.get("cover_url") or "")
    release_date = _text(result.get("release_date"), 32)
    if description or cover_url or release_date:
        con.execute(
            """UPDATE candidates SET
                description=CASE WHEN description='' AND ?!='' THEN ? ELSE description END,
                cover_url=CASE WHEN (cover_url='' OR cover_url LIKE '%/b/isbn/%') AND ?!='' THEN ? ELSE cover_url END,
                release_date=CASE WHEN release_date IS NULL AND ?!='' THEN ? ELSE release_date END,
                date_kind=CASE WHEN release_date IS NULL AND ?!='' THEN 'catalog' ELSE date_kind END,
                updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (description, description, cover_url, cover_url, release_date, release_date, release_date, candidate_id),
        )
    return bool(description or cover_url or release_date)


def _dedupe_and_hide(con) -> int:
    """Reject exact read overlaps and lower-quality active duplicates."""

    changed = 0
    overlap = con.execute(
        """SELECT c.id FROM candidates c
        JOIN candidate_quality q ON q.candidate_id=c.id
        WHERE c.status IN ('new','recommended')
          AND book_identity(c.title,c.author) IN (SELECT book_identity(title,author) FROM reads)"""
    ).fetchall()
    for item in overlap:
        con.execute("UPDATE candidates SET status='rejected',updated_at=CURRENT_TIMESTAMP WHERE id=?", (item[0],))
        con.execute(
            "UPDATE candidate_quality SET quality_status='rejected',flags_json=?,updated_at=CURRENT_TIMESTAMP WHERE candidate_id=?",
            (json.dumps(["read_overlap"]), item[0]),
        )
        changed += 1

    duplicate_groups = con.execute(
        """SELECT book_identity(c.title,c.author) AS identity
        FROM candidates c JOIN candidate_quality q ON q.candidate_id=c.id
        WHERE c.status IN ('new','recommended') AND q.quality_status='accepted'
        GROUP BY book_identity(c.title,c.author) HAVING COUNT(*) > 1"""
    ).fetchall()
    for group in duplicate_groups:
        items = con.execute(
            """SELECT c.id FROM candidates c JOIN candidate_quality q ON q.candidate_id=c.id
            WHERE c.status IN ('new','recommended') AND q.quality_status='accepted'
              AND book_identity(c.title,c.author)=?
            ORDER BY q.quality_score DESC,
                (length(c.description)>0) DESC,
                (length(c.cover_url)>0) DESC,
                c.score DESC,c.id ASC""",
            (group[0],),
        ).fetchall()
        for item in items[1:]:
            con.execute("UPDATE candidates SET status='rejected',updated_at=CURRENT_TIMESTAMP WHERE id=?", (item[0],))
            con.execute(
                "UPDATE candidate_quality SET quality_status='rejected',flags_json=?,updated_at=CURRENT_TIMESTAMP WHERE candidate_id=?",
                (json.dumps(["duplicate_identity"]), item[0]),
            )
            changed += 1
    return changed


async def audit_candidates(*, only_pending: bool = False, limit: int | None = None) -> dict[str, Any]:
    """Audit and enrich candidates, returning a deterministic quality report."""

    where = "c.status!='rejected'"
    if only_pending:
        where += " AND COALESCE(q.quality_status,'pending')='pending'"
    query = (
        "SELECT c.*,s.url AS source_root FROM candidates c "
        "LEFT JOIN sources s ON s.id=c.source_id "
        "LEFT JOIN candidate_quality q ON q.candidate_id=c.id "
        f"WHERE {where} ORDER BY c.id"
    )
    params = ()
    if limit is not None:
        query += " LIMIT ?"
        params = (max(1, int(limit)),)
    candidates = rows(query, params)
    if not candidates:
        with transaction() as con:
            deduped = _dedupe_and_hide(con)
        return {"audited": 0, "accepted": 0, "quarantine": 0, "rejected": 0, "deduped": deduped, "enriched": 0}

    semaphore = asyncio.Semaphore(CATALOG_CONCURRENCY)
    cache: dict[tuple[str, str], asyncio.Task[dict[str, Any] | None]] = {}
    async with metadata_client() as client:
        async def run(candidate):
            async with semaphore:
                return await _audit_one(candidate, client, cache)

        results = await asyncio.gather(*(run(candidate) for candidate in candidates))

    counts = {"accepted": 0, "quarantine": 0, "rejected": 0}
    enriched = 0
    with transaction() as con:
        for candidate, result in zip(candidates, results):
            status = result.get("quality_status", "quarantine")
            counts[status] = counts.get(status, 0) + 1
            if _store_result(con, candidate, result):
                enriched += 1
        deduped = _dedupe_and_hide(con)
    return {
        "audited": len(candidates),
        **counts,
        "deduped": deduped,
        "enriched": enriched,
        "quality_version": QUALITY_VERSION,
    }


def quality_summary() -> dict[str, Any]:
    summary = rows(
        "SELECT q.quality_status,COUNT(*) AS count FROM candidate_quality q "
        "GROUP BY q.quality_status ORDER BY q.quality_status"
    )
    identifiers = rows(
        "SELECT COUNT(*) AS total, SUM(CASE WHEN isbn13!='' THEN 1 ELSE 0 END) AS with_isbn13, "
        "SUM(CASE WHEN provider!='' THEN 1 ELSE 0 END) AS with_catalog_match "
        "FROM candidate_quality"
    )[0]
    flag_counts: dict[str, int] = {}
    for item in rows("SELECT flags_json FROM candidate_quality"):
        try:
            flags = json.loads(item.get("flags_json") or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            flags = []
        for flag in flags if isinstance(flags, list) else []:
            flag_counts[str(flag)] = flag_counts.get(str(flag), 0) + 1
    return {
        "statuses": summary,
        "identifiers": identifiers,
        "flags": dict(sorted(flag_counts.items(), key=lambda item: (-item[1], item[0]))),
        "version": QUALITY_VERSION,
    }
