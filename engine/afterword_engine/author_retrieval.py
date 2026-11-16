"""Favorite-author retrieval channel via Open Library author works."""

from __future__ import annotations

import json
import re
from typing import Any, Sequence

import httpx

from .config import settings
from .database import connect, normalize_key, transaction
from .identity import ReadIdentityIndex, book_identity, book_row_identity_match_keys


FAVORITE_AUTHORS_SOURCE_URL = "https://openlibrary.org/authors/favorite"
FAVORITE_AUTHORS_SOURCE_NAME = "Open Library · Favorite authors"


def _openlibrary_headers() -> dict[str, str]:
    contact = (settings.openlibrary_contact or "").strip()
    agent = f"Bookward/0.1 ({contact})" if contact else "Bookward/0.1"
    return {"User-Agent": agent, "Accept": "application/json"}


def favorite_authors_from_reads(
    reads: Sequence[dict[str, Any]],
    *,
    min_reads: int = 2,
    min_shrunk_mean: float = 3.6,
    prior_weight: float = 5.0,
    limit: int = 25,
) -> list[dict[str, Any]]:
    """Return high-affinity authors ranked by shrunk mean rating."""
    rated = [
        row
        for row in reads
        if row.get("rating") is not None and 1.0 <= float(row["rating"]) <= 5.0
    ]
    if not rated:
        return []
    global_mean = sum(float(row["rating"]) for row in rated) / len(rated)
    grouped: dict[str, list[tuple[str, float]]] = {}
    for row in rated:
        author = str(row.get("author") or "").strip()
        if not author or author.casefold() in {"unknown", "unknown author"}:
            continue
        key = book_identity("", author)
        if not key:
            continue
        grouped.setdefault(key, []).append((author, float(row["rating"])))

    favorites: list[dict[str, Any]] = []
    for key, entries in grouped.items():
        if len(entries) < min_reads:
            continue
        author_mean = sum(rating for _, rating in entries) / len(entries)
        shrunk_mean = (len(entries) * author_mean + prior_weight * global_mean) / (
            len(entries) + prior_weight
        )
        if shrunk_mean < min_shrunk_mean:
            continue
        favorites.append(
            {
                "author_key": key,
                "author": entries[0][0],
                "read_count": len(entries),
                "mean_rating": round(author_mean, 3),
                "shrunk_mean": round(shrunk_mean, 3),
            }
        )
    favorites.sort(key=lambda item: (-item["shrunk_mean"], -item["read_count"], item["author"]))
    return favorites[: max(0, int(limit))]


async def fetch_author_works(author_name: str, *, limit: int = 10) -> list[dict[str, Any]]:
    """Fetch candidate works by ``author_name`` from Open Library search."""
    params = {
        "author": author_name,
        "limit": max(1, min(25, int(limit))),
        "fields": "key,title,author_name,first_publish_year,isbn,subject",
    }
    async with httpx.AsyncClient(
        timeout=settings.source_timeout_seconds,
        headers=_openlibrary_headers(),
        follow_redirects=True,
    ) as client:
        response = await client.get("https://openlibrary.org/search.json", params=params)
        response.raise_for_status()
        payload = response.json()

    works: list[dict[str, Any]] = []
    for doc in payload.get("docs") or []:
        title = str(doc.get("title") or "").strip()[:500]
        authors = doc.get("author_name") or []
        author = str(authors[0] if authors else author_name).strip()[:300]
        work_key = str(doc.get("key") or "").strip()
        if not title or not work_key.startswith("/works/"):
            continue
        isbns = [re.sub(r"[^0-9Xx]", "", str(code)) for code in (doc.get("isbn") or [])]
        isbn13 = next((code for code in isbns if len(code) == 13), None)
        isbn10 = next((code for code in isbns if len(code) == 10), None)
        year = doc.get("first_publish_year")
        release_date = f"{int(year):04d}-01-01" if isinstance(year, int) and 1400 <= year <= 2100 else None
        subjects = [str(item).strip() for item in (doc.get("subject") or [])[:6] if str(item).strip()]
        works.append(
            {
                "title": title,
                "author": author,
                "source_url": f"https://openlibrary.org{work_key}",
                "openlibrary_work_id": work_key,
                "isbn13": isbn13,
                "isbn10": isbn10,
                "release_date": release_date,
                "genres": subjects,
            }
        )
    return works


async def ingest_favorite_author_works(
    *,
    max_authors: int = 10,
    works_per_author: int = 8,
) -> dict[str, int]:
    """Retrieve unread works by favorite authors and insert new candidates."""
    with connect() as con:
        reads = [dict(row) for row in con.execute("SELECT * FROM reads ORDER BY id").fetchall()]
        existing_cands = [
            dict(row)
            for row in con.execute(
                "SELECT id, title, author, isbn13, isbn10, normalized_key FROM candidates"
            ).fetchall()
        ]
    favorites = favorite_authors_from_reads(reads, limit=max_authors)
    if not favorites:
        return {"authors_queried": 0, "added": 0}

    read_index = ReadIdentityIndex.from_reads(reads)
    existing_keys = {row["normalized_key"] for row in existing_cands}
    existing_match_keys: set[str] = set()
    for row in existing_cands:
        existing_match_keys.update(book_row_identity_match_keys(row))

    with transaction() as con:
        con.execute(
            "INSERT OR IGNORE INTO sources(name, url, kind, enabled, is_default, weight) "
            "VALUES(?, ?, 'openlibrary-authors', 1, 0, 1.0)",
            (FAVORITE_AUTHORS_SOURCE_NAME, FAVORITE_AUTHORS_SOURCE_URL),
        )
        source_row = con.execute(
            "SELECT id, enabled FROM sources WHERE url=?",
            (FAVORITE_AUTHORS_SOURCE_URL,),
        ).fetchone()
        if not source_row or not source_row["enabled"]:
            return {"authors_queried": 0, "added": 0}
        source_id = int(source_row["id"])

    added = 0
    authors_queried = 0
    for fav in favorites:
        authors_queried += 1
        try:
            works = await fetch_author_works(fav["author"], limit=works_per_author * 2)
        except Exception:
            continue
        author_added = 0
        with transaction() as con:
            for work in works:
                if author_added >= works_per_author:
                    break
                if read_index.matches(work):
                    continue
                cand_keys = set(book_row_identity_match_keys(work))
                nkey = normalize_key(work["title"], work["author"])
                if nkey in existing_keys or (cand_keys & existing_match_keys):
                    continue
                con.execute(
                    "INSERT INTO candidates("
                    "title, author, source_url, source_id, release_date, date_kind, "
                    "genres, status, normalized_key, isbn13, isbn10"
                    ") VALUES(?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?)",
                    (
                        work["title"],
                        work["author"],
                        work["source_url"],
                        source_id,
                        work["release_date"],
                        "year" if work["release_date"] else "unknown",
                        json.dumps(work["genres"]),
                        nkey,
                        work["isbn13"] or "",
                        work["isbn10"] or "",
                    ),
                )
                existing_keys.add(nkey)
                existing_match_keys.update(cand_keys)
                author_added += 1
                added += 1
    return {"authors_queried": authors_queried, "added": added}
