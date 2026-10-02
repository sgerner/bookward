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
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any
import httpx

from .covers import (
    GOOGLE_BOOKS_SEARCH,
    METADATA_GENRE_LIMIT,
    METADATA_NORMALIZATION_VERSION,
    OPEN_LIBRARY_SEARCH,
    _CATALOG_PROVIDER_FAILURE_STATUSES,
    _bounded_source_payload,
    _catalog_genres,
    _clean_metadata_text,
    _field_provenance,
    _isbn_identifier_evidence,
    _isbn_provenance_values,
    _openlibrary_request,
    _query_isbn_matches,
    _utc_now,
    is_weak_cover_url,
    metadata_client,
    safe_cover_url,
)
from .database import persist_metadata_field_provenance, private_setting, rows, transaction
from .identity import book_identity, book_identity_match_index, book_identity_match_keys
from .isbn import canonical_isbn, isbn_parts, isbn_parts_from_source


QUALITY_VERSION = "candidate-quality-v4"
CATALOG_CONCURRENCY = 8
CATALOG_LIMIT = 10
CATALOG_PROVIDER_RETRY_HOURS = 24
QUALITY_AUDIT_BATCH_SIZE = 50
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


def _first_isbn(values: object) -> tuple[str, str]:
    return isbn_parts(values)


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
    description_kind: str = "",
    description_source_field: str = "",
    opening_sentence: str = "",
    release_date: str = "",
    cover_url: str = "",
    genres: list[str] | None = None,
    source_payload: object = None,
) -> dict[str, Any]:
    title_match = _title_similarity(title, catalog_title)
    author_match = _author_similarity(author, catalog_author)
    confidence = min(1.0, (title_match * 0.65) + (author_match * 0.35))
    if title_match >= 0.98 and author_match >= 0.98:
        confidence = 1.0
    cleaned_description = _clean_metadata_text(description, 4000)
    normalized_genres = _catalog_genres(genres, limit=METADATA_GENRE_LIMIT)
    description_content_quality_heuristic = 0.0
    if cleaned_description:
        length_quality = min(1.0, len(cleaned_description) / 600.0)
        description_content_quality_heuristic = round(
            (0.5 + 0.5 * length_quality)
            if description_kind == "synopsis"
            else 0.5 * length_quality,
            4,
        )
    fields: dict[str, Any] = {}
    if cleaned_description:
        fields["description"] = _field_provenance(
            provider,
            provider_id,
            cleaned_description,
            kind=description_kind,
            source_field=description_source_field,
        )
        fields["description"]["identity_quality_score"] = round(confidence, 4)
        fields["description"]["content_quality_heuristic"] = description_content_quality_heuristic
        fields["description"]["content_quality_note"] = "Heuristic from field type and normalized character length; not a calibrated probability."
    if normalized_genres:
        fields["genres"] = _field_provenance(
            provider, provider_id, normalized_genres, kind="subjects",
            source_field="subject" if provider == "openlibrary" else "categories",
        )
        fields["genres"]["identity_quality_score"] = round(confidence, 4)
    if cover_url:
        fields["cover_url"] = _field_provenance(
            provider, provider_id, cover_url, kind="cover",
            source_field="cover_i" if provider == "openlibrary" else "imageLinks",
        )
    if release_date:
        fields["release_date"] = _field_provenance(
            provider, provider_id, release_date, kind="date",
            source_field="first_publish_date" if provider == "openlibrary" else "publishedDate",
        )
    return {
        "provider": provider,
        "provider_id": provider_id,
        "work_id": work_id,
        "isbn13": isbn13,
        "isbn10": isbn10,
        "title_match": round(title_match, 4),
        "author_match": round(author_match, 4),
        "quality_score": round(confidence, 4),
        "identity_quality_score": round(confidence, 4),
        "catalog_title": _text(catalog_title, 500),
        "catalog_author": _text(catalog_author, 300),
        "description": cleaned_description,
        "description_kind": description_kind,
        "description_source_field": description_source_field,
        "description_opening_sentence": _clean_metadata_text(opening_sentence, 1500),
        "description_provider": provider if cleaned_description else "",
        "description_provider_id": provider_id if cleaned_description else "",
        "description_content_quality_heuristic": description_content_quality_heuristic,
        "release_date": _text(release_date, 32),
        "cover_url": cover_url,
        "genres": normalized_genres,
        "genres_provider": provider if normalized_genres else "",
        "genres_provider_id": provider_id if normalized_genres else "",
        "cover_provider": provider if cover_url else "",
        "cover_provider_id": provider_id if cover_url else "",
        "release_date_provider": provider if release_date else "",
        "release_date_provider_id": provider_id if release_date else "",
        "metadata_provenance": {
            "fetched_at": _utc_now(),
            "normalization_version": METADATA_NORMALIZATION_VERSION,
            "fields": fields,
            "fetch_trace": [],
            "source_payloads": [
                {"provider": provider, "provider_id": provider_id, "payload": _bounded_source_payload(source_payload)}
            ] if source_payload else [],
        },
    }


async def _open_library_match(
    title: str,
    author: str,
    client: httpx.AsyncClient,
    *,
    isbn: str = "",
    trace: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    params = {
        ("isbn" if isbn else "title"): isbn or title[:500],
        **({} if isbn else {"author": author[:300]}),
        "limit": CATALOG_LIMIT,
        "fields": "key,title,author_name,cover_i,first_publish_year,first_publish_date,first_sentence,description,subject,isbn,isbn13,edition_key",
    }
    fetch_trace: dict[str, Any] = {
        "provider": "openlibrary",
        "fetched_at": _utc_now(),
        "query_kind": "isbn" if isbn else "title_author",
        "status": "request_error",
    }
    if trace is not None:
        trace.append(fetch_trace)
    try:
        response = await _openlibrary_request(client, OPEN_LIBRARY_SEARCH, params=params)
        fetch_trace["http_status"] = response.status_code
        if response.is_error:
            fetch_trace["status"] = "http_error"
            return None
        try:
            payload = response.json()
        except ValueError:
            fetch_trace["status"] = "invalid_json"
            return None
    except Exception as exc:
        fetch_trace["error_type"] = type(exc).__name__
        return None
    docs = payload.get("docs", []) if isinstance(payload, dict) else []
    best: dict[str, Any] | None = None
    matched_count = 0
    mismatched_identifiers = 0
    for doc in docs if isinstance(docs, list) else []:
        if not isinstance(doc, dict):
            continue
        raw_identifiers = [doc.get("isbn13"), doc.get("isbn"), doc.get("isbn10")]
        if isbn and not _query_isbn_matches(isbn, raw_identifiers):
            mismatched_identifiers += 1
            continue
        authors = doc.get("author_name") or []
        if isinstance(authors, str):
            authors = [authors]
        catalog_author = ", ".join(str(value) for value in authors if value)
        projected_identifiers = _isbn_provenance_values(isbn, raw_identifiers)
        isbn13, isbn10 = _first_isbn(projected_identifiers)
        cover = ""
        if str(doc.get("cover_i") or "").isdigit():
            cover = f"https://covers.openlibrary.org/b/id/{int(doc['cover_i'])}-L.jpg"
        synopsis = _clean_metadata_text(doc.get("description"), 4000)
        opening_sentence = _clean_metadata_text(doc.get("first_sentence"), 1500)
        description = synopsis or opening_sentence
        description_kind = "synopsis" if synopsis else "opening_sentence" if opening_sentence else ""
        provider_id = str(doc.get("key") or "")
        genres = _catalog_genres(doc.get("subject"), limit=METADATA_GENRE_LIMIT)
        source_payload = {
            "key": provider_id,
            "title": _clean_metadata_text(doc.get("title"), 500),
            "authors": [_clean_metadata_text(value, 300) for value in authors if value],
            "description": synopsis,
            "opening_sentence": opening_sentence,
            "genres": genres,
            "identifiers": projected_identifiers,
            "publication_date": _clean_metadata_text(doc.get("first_publish_date") or doc.get("first_publish_year"), 40),
            "cover_id": str(doc.get("cover_i") or "")[:40],
        }
        match = _provider_match(
            provider="openlibrary",
            provider_id=provider_id,
            work_id=provider_id,
            title=title,
            author=author,
            catalog_title=str(doc.get("title") or ""),
            catalog_author=catalog_author,
            isbn13=isbn13,
            isbn10=isbn10,
            description=description,
            description_kind=description_kind,
            description_source_field="description" if synopsis else "first_sentence" if opening_sentence else "",
            opening_sentence=opening_sentence,
            release_date=str(doc.get("first_publish_date") or doc.get("first_publish_year") or ""),
            cover_url=safe_cover_url(cover),
            genres=genres,
            source_payload=source_payload,
        )
        matched_count += 1
        if best is None or (
            match["quality_score"],
            match["description_kind"] == "synopsis",
            match["description_content_quality_heuristic"],
            len(match["genres"]),
        ) > (
            best["quality_score"],
            best["description_kind"] == "synopsis",
            best["description_content_quality_heuristic"],
            len(best["genres"]),
        ):
            best = match
    fetch_trace["matched_count"] = matched_count
    fetch_trace["mismatched_identifier_count"] = mismatched_identifiers
    if best:
        fetch_trace["status"] = "matched"
        fetch_trace["provider_id"] = best["provider_id"]
        fetch_trace["available_fields"] = [field for field in ("description", "genres", "cover_url", "release_date") if best.get(field)]
        identifiers = best["metadata_provenance"]["source_payloads"][0]["payload"].get("identifiers", [])
        evidence, verified = _isbn_identifier_evidence(isbn, identifiers)
        fetch_trace["identifier_evidence"] = evidence
        fetch_trace["identifier_verified"] = verified
    else:
        fetch_trace["status"] = "no_match"
        if isbn:
            fetch_trace["identifier_evidence"] = "mismatch" if mismatched_identifiers else "no_result"
            fetch_trace["identifier_verified"] = False
    if best:
        best["metadata_provenance"]["fetch_trace"] = [fetch_trace]
    return best


async def _google_books_match(
    title: str,
    author: str,
    client: httpx.AsyncClient,
    *,
    isbn: str = "",
    trace: list[dict[str, Any]] | None = None,
    google_books_api_key: str = "",
) -> dict[str, Any] | None:
    params = {
        "q": f"isbn:{isbn}" if isbn else f"intitle:{title[:300]} inauthor:{author[:200]}",
        "maxResults": CATALOG_LIMIT,
    }
    if google_books_api_key:
        # Do not include the request URL in fetch traces or exception text.
        params["key"] = google_books_api_key
    fetch_trace: dict[str, Any] = {
        "provider": "google_books",
        "fetched_at": _utc_now(),
        "query_kind": "isbn" if isbn else "title_author",
        "status": "request_error",
    }
    if trace is not None:
        trace.append(fetch_trace)
    try:
        response = await client.get(GOOGLE_BOOKS_SEARCH, params=params)
        fetch_trace["http_status"] = response.status_code
        if response.is_error:
            fetch_trace["status"] = "rate_limited" if response.status_code == 429 else "http_error"
            return None
        try:
            payload = response.json()
        except ValueError:
            fetch_trace["status"] = "invalid_json"
            return None
    except Exception as exc:
        fetch_trace["error_type"] = type(exc).__name__
        return None
    best: dict[str, Any] | None = None
    matched_count = 0
    mismatched_identifiers = 0
    for item in payload.get("items", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        info = item.get("volumeInfo") or {}
        if not isinstance(info, dict):
            continue
        authors = info.get("authors") or []
        if isinstance(authors, str):
            authors = [authors]
        raw_identifiers = info.get("industryIdentifiers", [])
        if not isinstance(raw_identifiers, list):
            raw_identifiers = []
        identifiers = [
            value.get("identifier") for value in raw_identifiers
            if isinstance(value, dict) and value.get("type") in {"ISBN_10", "ISBN_13"}
        ]
        if isbn and not _query_isbn_matches(isbn, identifiers):
            mismatched_identifiers += 1
            continue
        projected_identifiers = _isbn_provenance_values(isbn, identifiers)
        isbn13, isbn10 = _first_isbn(projected_identifiers)
        image_links = info.get("imageLinks") or {}
        image = ""
        if isinstance(image_links, dict):
            for key in ("extraLarge", "large", "medium", "thumbnail", "smallThumbnail"):
                if image_links.get(key):
                    image = safe_cover_url(str(image_links[key]).replace("http://", "https://"))
                    if image:
                        break
        description = _clean_metadata_text(info.get("description"), 4000)
        provider_id = str(item.get("id") or "")
        genres = _catalog_genres(info.get("categories"), limit=METADATA_GENRE_LIMIT)
        source_payload = {
            "id": provider_id,
            "title": _clean_metadata_text(info.get("title"), 500),
            "authors": [_clean_metadata_text(value, 300) for value in authors if value],
            "description": description,
            "genres": genres,
                "identifiers": projected_identifiers,
            "publication_date": _clean_metadata_text(info.get("publishedDate"), 40),
            "cover_url": image,
        }
        match = _provider_match(
            provider="google_books",
            provider_id=provider_id,
            work_id=provider_id,
            title=title,
            author=author,
            catalog_title=str(info.get("title") or ""),
            catalog_author=", ".join(str(value) for value in authors if value),
            isbn13=isbn13,
            isbn10=isbn10,
            description=description,
            description_kind="synopsis" if description else "",
            description_source_field="description" if description else "",
            release_date=str(info.get("publishedDate") or ""),
            cover_url=image,
            genres=genres,
            source_payload=source_payload,
        )
        matched_count += 1
        if best is None or (
            match["quality_score"],
            match["description_kind"] == "synopsis",
            match["description_content_quality_heuristic"],
            len(match["genres"]),
        ) > (
            best["quality_score"],
            best["description_kind"] == "synopsis",
            best["description_content_quality_heuristic"],
            len(best["genres"]),
        ):
            best = match
    fetch_trace["matched_count"] = matched_count
    fetch_trace["mismatched_identifier_count"] = mismatched_identifiers
    if best:
        fetch_trace["status"] = "matched"
        fetch_trace["provider_id"] = best["provider_id"]
        fetch_trace["available_fields"] = [field for field in ("description", "genres", "cover_url", "release_date") if best.get(field)]
        identifiers = best["metadata_provenance"]["source_payloads"][0]["payload"].get("identifiers", [])
        evidence, verified = _isbn_identifier_evidence(isbn, identifiers)
        fetch_trace["identifier_evidence"] = evidence
        fetch_trace["identifier_verified"] = verified
        best["metadata_provenance"]["fetch_trace"] = [fetch_trace]
    else:
        fetch_trace["status"] = "no_match"
        if isbn:
            fetch_trace["identifier_evidence"] = "mismatch" if mismatched_identifiers else "no_result"
            fetch_trace["identifier_verified"] = False
    return best


async def resolve_catalog_match(
    title: str,
    author: str,
    *,
    client: httpx.AsyncClient,
    cache: dict[tuple[str, ...], asyncio.Task[dict[str, Any] | None]],
    isbn13: str = "",
    isbn10: str = "",
    isbn: str = "",
    google_books_api_key: str = "",
    existing_fields_complete: bool = False,
) -> dict[str, Any] | None:
    """Resolve identity first, then choose metadata independently per field."""

    source_isbn13, source_isbn10 = isbn_parts(isbn13, isbn10, isbn)
    key = (
        _catalog_text(title),
        _catalog_text(author),
        source_isbn13,
        source_isbn10,
        bool(google_books_api_key),
        bool(existing_fields_complete),
    )
    existing = cache.get(key)
    if existing is not None:
        return await existing

    def accepted(match: dict[str, Any] | None) -> bool:
        return bool(
            match
            and match.get("quality_score", 0) >= 0.82
            and match.get("title_match", 0) >= 0.82
            and match.get("author_match", 0) >= 0.65
        )

    async def lookup():
        fetch_trace: list[dict[str, Any]] = []
        matches: list[dict[str, Any]] = []
        query_isbn = source_isbn13 or source_isbn10
        if query_isbn:
            # Query both providers so a strong identity record with sparse
            # fields does not hide useful verified metadata from the other.
            for lookup_provider in (_open_library_match, _google_books_match):
                kwargs = {"google_books_api_key": google_books_api_key} if lookup_provider is _google_books_match else {}
                match = await lookup_provider(title, author, client, isbn=query_isbn, trace=fetch_trace, **kwargs)
                if match:
                    matches.append(match)

        def has_full_field_coverage(values: list[dict[str, Any]]) -> bool:
            return (
                any(value.get("description_kind") == "synopsis" for value in values)
                and any(value.get("genres") for value in values)
                and any(value.get("cover_url") for value in values)
                and any(value.get("release_date") for value in values)
            )

        if not query_isbn or (
            not has_full_field_coverage([value for value in matches if accepted(value)])
            and not existing_fields_complete
        ):
            unavailable_providers = {
                str(trace.get("provider") or "")
                for trace in fetch_trace
                if trace.get("query_kind") == "isbn"
                and trace.get("status") in _CATALOG_PROVIDER_FAILURE_STATUSES
            }
            for lookup_provider in (_open_library_match, _google_books_match):
                provider_name = "openlibrary" if lookup_provider is _open_library_match else "google_books"
                if query_isbn and provider_name in unavailable_providers:
                    continue
                kwargs = {"google_books_api_key": google_books_api_key} if lookup_provider is _google_books_match else {}
                match = await lookup_provider(title, author, client, trace=fetch_trace, **kwargs)
                if match:
                    matches.append(match)

        if not matches:
            unavailable = any(
                trace.get("status") in _CATALOG_PROVIDER_FAILURE_STATUSES
                for trace in fetch_trace
            )
            if unavailable:
                return {
                    "provider_lookup_unavailable": True,
                    "provider_rate_limited": any(
                        trace.get("status") == "rate_limited" for trace in fetch_trace
                    ),
                    "quality_score": 0.0,
                    "title_match": 0.0,
                    "author_match": 0.0,
                    "metadata_provenance": {
                        "fetched_at": _utc_now(),
                        "normalization_version": METADATA_NORMALIZATION_VERSION,
                        "fields": {},
                        "fetch_trace": fetch_trace,
                        "source_payloads": [],
                    },
                }
            return None

        # De-duplicate an ISBN and a text result for the same provider record,
        # retaining whichever parsed record supplies more useful fields.
        by_record: dict[tuple[str, str], dict[str, Any]] = {}
        for match in matches:
            record_key = (str(match.get("provider") or ""), str(match.get("provider_id") or ""))
            prior = by_record.get(record_key)
            rank = (
                accepted(match),
                match.get("description_kind") == "synopsis",
                len(str(match.get("description") or "")),
                len(match.get("genres") or []),
                bool(match.get("cover_url")),
                bool(match.get("release_date")),
            )
            prior_rank = (
                accepted(prior),
                prior.get("description_kind") == "synopsis",
                len(str(prior.get("description") or "")),
                len(prior.get("genres") or []),
                bool(prior.get("cover_url")),
                bool(prior.get("release_date")),
            ) if prior else None
            if prior is None or rank > prior_rank:
                by_record[record_key] = match
        records = list(by_record.values())
        verified_records = [record for record in records if accepted(record)]
        identity_record = max(records, key=lambda record: record["quality_score"])
        metadata_records = verified_records or []
        if not metadata_records:
            identity_record = dict(identity_record)
            identity_record["metadata_provenance"] = {
                "fetched_at": _utc_now(),
                "normalization_version": METADATA_NORMALIZATION_VERSION,
                "fields": {},
                "fetch_trace": fetch_trace,
                "source_payloads": [],
            }
            identity_record["provider_lookup_unavailable"] = any(
                trace.get("status") in _CATALOG_PROVIDER_FAILURE_STATUSES
                for trace in fetch_trace
            )
            identity_record["provider_rate_limited"] = any(
                trace.get("status") == "rate_limited" for trace in fetch_trace
            )
            return identity_record

        description_records = [record for record in metadata_records if record.get("description")]
        description_record = max(
            description_records,
            key=lambda record: (
                record.get("description_kind") == "synopsis",
                len(str(record.get("description") or "")),
                record.get("quality_score", 0),
            ),
            default=None,
        )
        description = str(description_record.get("description") or "") if description_record else ""
        genres: list[str] = []
        genre_sources: list[dict[str, str]] = []
        for record in metadata_records:
            before = len(genres)
            genres = _catalog_genres(genres + list(record.get("genres") or []), limit=METADATA_GENRE_LIMIT)
            if len(genres) > before:
                genre_sources.append({
                    "provider": str(record.get("provider") or ""),
                    "provider_id": str(record.get("provider_id") or ""),
                })
        cover_record = next((record for record in metadata_records if record.get("cover_url")), None)
        date_rank = {"day": 3, "month": 2, "year": 1}
        date_record = max(
            (record for record in metadata_records if record.get("release_date")),
            key=lambda record: date_rank.get(_publication_date_kind(record.get("release_date")), 0),
            default=None,
        )

        result = dict(identity_record)
        result.update({
            "description": description,
            "description_kind": description_record.get("description_kind", "") if description_record else "",
            "description_source_field": description_record.get("description_source_field", "") if description_record else "",
            "description_opening_sentence": description_record.get("description_opening_sentence", "") if description_record else "",
            "description_provider": description_record.get("provider", "") if description_record else "",
            "description_provider_id": description_record.get("provider_id", "") if description_record else "",
            "description_content_quality_heuristic": description_record.get("description_content_quality_heuristic", 0.0) if description_record else 0.0,
            "description_candidate": {
                "provider": description_record.get("provider", ""),
                "provider_id": description_record.get("provider_id", ""),
                "text": description,
                "kind": description_record.get("description_kind", ""),
                "source_field": description_record.get("description_source_field", ""),
                "opening_sentence": description_record.get("description_opening_sentence", ""),
                "content_quality_heuristic": description_record.get("description_content_quality_heuristic", 0.0),
                "content_quality_note": "Heuristic from field type and normalized character length; not a calibrated probability.",
            } if description_record else {},
            "genres": genres,
            "genres_provider": genre_sources[0]["provider"] if genre_sources else "",
            "genres_provider_id": genre_sources[0]["provider_id"] if genre_sources else "",
            "cover_url": cover_record.get("cover_url", "") if cover_record else "",
            "cover_provider": cover_record.get("provider", "") if cover_record else "",
            "cover_provider_id": cover_record.get("provider_id", "") if cover_record else "",
            "release_date": date_record.get("release_date", "") if date_record else "",
            "release_date_provider": date_record.get("provider", "") if date_record else "",
            "release_date_provider_id": date_record.get("provider_id", "") if date_record else "",
            "metadata_provider": (
                description_record.get("provider", "") if description_record
                else genre_sources[0]["provider"] if genre_sources
                else cover_record.get("provider", "") if cover_record
                else date_record.get("provider", "") if date_record
                else ""
            ),
            "metadata_provider_id": (
                description_record.get("provider_id", "") if description_record
                else genre_sources[0]["provider_id"] if genre_sources
                else cover_record.get("provider_id", "") if cover_record
                else date_record.get("provider_id", "") if date_record
                else ""
            ),
        })
        fields: dict[str, Any] = {}
        if description_record:
            fields["description"] = _field_provenance(
                str(description_record.get("provider") or ""),
                str(description_record.get("provider_id") or ""),
                description,
                kind=str(description_record.get("description_kind") or ""),
                source_field=str(description_record.get("description_source_field") or ""),
            )
            fields["description"]["identity_quality_score"] = description_record.get("quality_score", 0.0)
            fields["description"]["content_quality_heuristic"] = description_record.get("description_content_quality_heuristic", 0.0)
            fields["description"]["content_quality_note"] = "Heuristic from field type and normalized character length; not a calibrated probability."
        if genre_sources:
            fields["genres"] = {
                "provider": genre_sources[0]["provider"],
                "provider_id": genre_sources[0]["provider_id"],
                "sources": genre_sources,
                "kind": "subjects",
                "value_sha256": _field_provenance("", "", genres)["value_sha256"],
                "normalization_version": METADATA_NORMALIZATION_VERSION,
            }
        if cover_record:
            fields["cover_url"] = _field_provenance(
                str(cover_record.get("provider") or ""), str(cover_record.get("provider_id") or ""),
                cover_record.get("cover_url", ""), kind="cover",
                source_field="cover_i" if cover_record.get("provider") == "openlibrary" else "imageLinks",
            )
        if date_record:
            fields["release_date"] = _field_provenance(
                str(date_record.get("provider") or ""), str(date_record.get("provider_id") or ""),
                date_record.get("release_date", ""), kind="date",
                source_field="first_publish_date" if date_record.get("provider") == "openlibrary" else "publishedDate",
            )
        result["metadata_provenance"] = {
            "fetched_at": _utc_now(),
            "normalization_version": METADATA_NORMALIZATION_VERSION,
            "fields": fields,
            "fetch_trace": fetch_trace,
            "source_payloads": [
                source
                for record in metadata_records
                for source in record.get("metadata_provenance", {}).get("source_payloads", [])
            ],
        }
        return result

    task = asyncio.create_task(lookup())
    cache[key] = task
    return await task


def _publication_date_kind(value: object) -> str:
    from .covers import _publication_date

    _, kind = _publication_date(value)
    return str(kind or "")


async def _audit_one(
    candidate: dict[str, Any], client: httpx.AsyncClient, cache, google_books_api_key: str = ""
):
    flags = local_flags(candidate)
    source_isbn13, source_isbn10 = isbn_parts_from_source(
        [candidate.get("isbn13"), candidate.get("isbn10"), candidate.get("isbn")],
        candidate.get("source_url", ""),
    )
    source_identifiers = {"isbn13": source_isbn13, "isbn10": source_isbn10}
    if any(flag in flags for flag in ("missing_title", "noise_title", "missing_author", "unknown_author", "title_equals_author", "title_contains_url")):
        return {
            "candidate_id": int(candidate["id"]),
            "quality_status": "rejected",
            "quality_score": 0.0,
            "flags": flags,
            **source_identifiers,
        }
    match = await resolve_catalog_match(
        candidate["title"],
        candidate["author"],
        client=client,
        cache=cache,
        isbn13=source_isbn13,
        isbn10=source_isbn10,
        google_books_api_key=google_books_api_key,
        existing_fields_complete=_candidate_fields_complete(candidate),
    )
    if not match or match.get("provider_lookup_unavailable"):
        unavailable = bool(match and match.get("provider_lookup_unavailable"))
        failure_flags = ["catalog_provider_unavailable"]
        if match and match.get("provider_rate_limited"):
            failure_flags.append("catalog_provider_rate_limited")
        return {
            "candidate_id": int(candidate["id"]),
            "quality_status": "quarantine",
            "quality_score": 0.0,
            "flags": sorted(set(flags + (failure_flags if unavailable else ["catalog_unmatched"]))),
            **({"metadata_provenance": match.get("metadata_provenance", {})} if unavailable else {}),
            **source_identifiers,
        }
    accepted = match["quality_score"] >= 0.82 and match["title_match"] >= 0.82 and match["author_match"] >= 0.65
    return {
        "candidate_id": int(candidate["id"]),
        "quality_status": "accepted" if accepted else "quarantine",
        "quality_score": match["quality_score"],
        "flags": sorted(set(flags + ([] if accepted else ["catalog_match_ambiguous"]))),
        **match,
        "isbn13": source_isbn13 or match.get("isbn13", ""),
        "isbn10": source_isbn10 or match.get("isbn10", ""),
    }


def _candidate_fields_complete(candidate: dict[str, Any]) -> bool:
    genres = candidate.get("genres") or []
    if isinstance(genres, str):
        try:
            genres = json.loads(genres)
        except (TypeError, ValueError, json.JSONDecodeError):
            genres = [genres] if genres.strip() else []
    genres = _catalog_genres(genres, limit=METADATA_GENRE_LIMIT)
    cover_url = str(candidate.get("cover_url") or "")
    return bool(
        _clean_metadata_text(candidate.get("description"))
        and genres
        and str(candidate.get("release_date") or "").strip()
        and cover_url
        and not is_weak_cover_url(cover_url)
        and safe_cover_url(cover_url, str(candidate.get("source_url") or ""))
    )


def candidate_quality_audit_eligibility(now: datetime | None = None) -> tuple[str, tuple[str, ...]]:
    """Return the shared SQL predicate and parameters for a due bounded audit."""

    reference_time = now or datetime.now(timezone.utc)
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=timezone.utc)
    retry_before = (
        reference_time - timedelta(hours=CATALOG_PROVIDER_RETRY_HOURS)
    ).isoformat(timespec="seconds")
    predicate = (
        "COALESCE(q.audit_version,'')!='builtin-curated-v1' AND ("
        "COALESCE(q.quality_status,'pending')='pending' "
        "OR q.audit_version='legacy-pending-audit-v1' "
        "OR (q.quality_status IN ('accepted','quarantine') "
        "AND COALESCE(q.audit_version,'')!=? "
        "AND (c.isbn13!='' OR c.isbn10!='' OR q.isbn13!='' OR q.isbn10!='')) "
        "OR (q.quality_status='quarantine' AND q.audit_version=? "
        "AND q.flags_json LIKE '%catalog_provider_unavailable%' "
        "AND datetime(q.audited_at)<=datetime(?)))"
    )
    return predicate, (QUALITY_VERSION, QUALITY_VERSION, retry_before)


def candidate_quality_audit_candidates(
    limit: int = QUALITY_AUDIT_BATCH_SIZE,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Select a bounded batch due for quality audit, validating old ISBN rows."""

    batch_limit = max(1, min(int(limit), QUALITY_AUDIT_BATCH_SIZE))
    predicate, params = candidate_quality_audit_eligibility(now)
    page_size = max(100, batch_limit * 4)
    selected_ids: list[int] = []
    offset = 0
    while len(selected_ids) < batch_limit:
        page = rows(
            "SELECT c.id,c.isbn13,c.isbn10,c.source_url,"
            "COALESCE(NULLIF(c.isbn13,''),q.isbn13) AS eligibility_isbn13,"
            "COALESCE(NULLIF(c.isbn10,''),q.isbn10) AS eligibility_isbn10,"
            "q.quality_status,q.audit_version,q.flags_json "
            "FROM candidates c LEFT JOIN candidate_quality q ON q.candidate_id=c.id "
            f"WHERE c.status!='rejected' AND {predicate} ORDER BY c.id LIMIT ? OFFSET ?",
            (*params, page_size, offset),
        )
        if not page:
            break
        for candidate in page:
            status = str(candidate.get("quality_status") or "pending")
            legacy_pending = candidate.get("audit_version") == "legacy-pending-audit-v1"
            try:
                flags = json.loads(candidate.get("flags_json") or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                flags = []
            expired_provider_outage = bool(
                status == "quarantine"
                and candidate.get("audit_version") == QUALITY_VERSION
                and isinstance(flags, list)
                and "catalog_provider_unavailable" in flags
            )
            if status == "pending" or legacy_pending or expired_provider_outage:
                selected_ids.append(int(candidate["id"]))
            elif any(
                isbn_parts_from_source(
                    [candidate.get("eligibility_isbn13"), candidate.get("eligibility_isbn10")],
                    candidate.get("source_url", ""),
                )
            ):
                selected_ids.append(int(candidate["id"]))
            if len(selected_ids) >= batch_limit:
                break
        offset += len(page)
        if len(page) < page_size:
            break
    if not selected_ids:
        return []
    placeholders = ",".join("?" for _ in selected_ids)
    return rows(
        "SELECT c.*,s.url AS source_root,q.metadata_provider AS _metadata_provider,"
        "q.metadata_provider_id AS _metadata_provider_id FROM candidates c "
        "LEFT JOIN sources s ON s.id=c.source_id "
        "LEFT JOIN candidate_quality q ON q.candidate_id=c.id "
        f"WHERE c.id IN ({placeholders}) ORDER BY c.id",
        tuple(selected_ids),
    )


def has_candidate_quality_audit_candidates(*, now: datetime | None = None) -> bool:
    """Return whether any candidate is due for a bounded quality audit."""

    return bool(candidate_quality_audit_candidates(limit=1, now=now))


def _store_result(con, candidate: dict[str, Any], result: dict[str, Any]) -> bool:
    candidate_id = int(candidate["id"])
    status = result.get("quality_status", "quarantine")
    flags = result.get("flags", [])
    description = _text(result.get("description"), 4000)
    description_candidate = result.get("description_candidate")
    if not isinstance(description_candidate, dict):
        description_candidate = {}
    existing_description = _clean_metadata_text(candidate.get("description"), 4000)
    existing_provider = str(candidate.get("_metadata_provider") or "")
    existing_provider_id = str(candidate.get("_metadata_provider_id") or "")
    can_upgrade_opening_sentence = bool(
        existing_description
        and description_candidate.get("kind") == "synopsis"
        and description_candidate.get("opening_sentence")
        and existing_description == _clean_metadata_text(description_candidate.get("opening_sentence"), 1500)
        and existing_provider
        and existing_provider == str(description_candidate.get("provider") or "")
        and existing_provider_id == str(description_candidate.get("provider_id") or "")
        and description
    )
    store_description = bool(description and (not existing_description or can_upgrade_opening_sentence))
    cover_url = str(result.get("cover_url") or "")
    existing_cover = str(candidate.get("cover_url") or "")
    store_cover = bool(
        cover_url
        and (
            not existing_cover
            or "/b/isbn/" in existing_cover
            or existing_cover.startswith("https://placehold.co/")
        )
    )
    release_date = _text(result.get("release_date"), 32)
    store_release_date = bool(release_date and not candidate.get("release_date"))
    existing_genres = candidate.get("genres", "[]")
    if isinstance(existing_genres, str):
        try:
            existing_genres = json.loads(existing_genres)
        except (TypeError, ValueError):
            existing_genres = [existing_genres] if existing_genres.strip() else []
    normalized_existing_genres = _catalog_genres(existing_genres, limit=METADATA_GENRE_LIMIT)
    merged_genres = _catalog_genres(
        normalized_existing_genres
        + _catalog_genres(result.get("genres", []), limit=METADATA_GENRE_LIMIT),
        limit=METADATA_GENRE_LIMIT,
    )
    store_genres = merged_genres != normalized_existing_genres
    genres_json = json.dumps(merged_genres, ensure_ascii=False, separators=(",", ":"))

    selected_fields = []
    if store_description:
        selected_fields.append(("description", result.get("description_provider"), result.get("description_provider_id")))
    if store_genres:
        selected_fields.append(("genres", result.get("genres_provider"), result.get("genres_provider_id")))
    if store_cover:
        selected_fields.append(("cover_url", result.get("cover_provider"), result.get("cover_provider_id")))
    if store_release_date:
        selected_fields.append(("release_date", result.get("release_date_provider"), result.get("release_date_provider_id")))
    if store_description:
        metadata_provider = str(result.get("description_provider") or "")
        metadata_provider_id = str(result.get("description_provider_id") or "")
    else:
        metadata_provider = str(candidate.get("_metadata_provider") or "")
        metadata_provider_id = str(candidate.get("_metadata_provider_id") or "")

    con.execute(
        """INSERT INTO candidate_quality(
            candidate_id,quality_status,quality_score,flags_json,provider,provider_id,
            work_id,isbn13,isbn10,title_match,author_match,audit_version,metadata_provider,
            metadata_provider_id,audited_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
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
            metadata_provider=CASE WHEN excluded.metadata_provider!='' THEN excluded.metadata_provider ELSE metadata_provider END,
            metadata_provider_id=CASE WHEN excluded.metadata_provider_id!='' THEN excluded.metadata_provider_id ELSE metadata_provider_id END,
            audited_at=excluded.audited_at,
            updated_at=CURRENT_TIMESTAMP""",
        (
            candidate_id,
            status,
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
            metadata_provider,
            metadata_provider_id,
        ),
    )
    if status == "rejected" and candidate.get("status") in {"new", "recommended"}:
        con.execute(
            "UPDATE candidates SET status='rejected',updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (candidate_id,),
        )
    if status != "accepted":
        return False

    if store_description or store_cover or store_release_date or store_genres:
        con.execute(
            """UPDATE candidates SET
                description=CASE WHEN ?=1 THEN ? ELSE description END,
                cover_url=CASE WHEN ?=1 THEN ? ELSE cover_url END,
                release_date=CASE WHEN ?=1 THEN ? ELSE release_date END,
                date_kind=CASE WHEN ?=1 THEN 'catalog' ELSE date_kind END,
                genres=CASE WHEN ?=1 THEN ? ELSE genres END,
                updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (
                int(store_description), description,
                int(store_cover), cover_url,
                int(store_release_date), release_date,
                int(store_release_date),
                int(store_genres), genres_json,
                candidate_id,
            ),
        )

        field_provider_keys = {
            "description": ("description_provider", "description_provider_id"),
            "genres": ("genres_provider", "genres_provider_id"),
            "cover_url": ("cover_provider", "cover_provider_id"),
            "release_date": ("release_date_provider", "release_date_provider_id"),
        }
        for field, provider, provider_id in selected_fields:
            provider_key, provider_id_key = field_provider_keys[field]
            persist_metadata_field_provenance(
                con,
                "candidate",
                candidate_id,
                field,
                result,
                default_provider=str(provider or result.get(provider_key) or ""),
                default_provider_id=str(provider_id or result.get(provider_id_key) or ""),
                default_confidence=float(result.get("quality_score", 0) or 0),
            )
    return bool(selected_fields)


def _dedupe_and_hide(con) -> int:
    """Reject read overlaps and active duplicates of saved/imported books."""

    changed = 0
    active = con.execute(
        """SELECT c.id,c.title,c.author FROM candidates c
        JOIN candidate_quality q ON q.candidate_id=c.id
        WHERE c.status IN ('new','recommended')"""
    ).fetchall()
    read_keys = book_identity_match_index(
        con.execute("SELECT title,author FROM reads").fetchall()
    )
    overlap = [
        item for item in active
        if book_identity_match_keys(item["title"], item["author"]) & read_keys
    ]
    for item in overlap:
        candidate_id = item["id"]
        con.execute("UPDATE candidates SET status='rejected',updated_at=CURRENT_TIMESTAMP WHERE id=?", (candidate_id,))
        con.execute(
            "UPDATE candidate_quality SET quality_status='rejected',flags_json=?,updated_at=CURRENT_TIMESTAMP WHERE candidate_id=?",
            (json.dumps(["read_overlap"]), candidate_id),
        )
        changed += 1
    overlap_ids = {item["id"] for item in overlap}

    shortlisted_keys = book_identity_match_index(
        con.execute(
            "SELECT title,author FROM candidates WHERE status IN ('saved','imported')"
        ).fetchall()
    )
    shortlisted_overlap = [
        item for item in active
        if item["id"] not in overlap_ids
        and book_identity_match_keys(item["title"], item["author"]) & shortlisted_keys
    ]
    for item in shortlisted_overlap:
        candidate_id = item["id"]
        con.execute("UPDATE candidates SET status='rejected',updated_at=CURRENT_TIMESTAMP WHERE id=?", (candidate_id,))
        con.execute(
            "UPDATE candidate_quality SET quality_status='rejected',flags_json=?,updated_at=CURRENT_TIMESTAMP WHERE candidate_id=?",
            (json.dumps(["shortlisted_overlap"]), candidate_id),
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

    if only_pending:
        candidates = candidate_quality_audit_candidates(
            limit=limit if limit is not None else QUALITY_AUDIT_BATCH_SIZE
        )
    else:
        query = (
            "SELECT c.*,s.url AS source_root,q.metadata_provider AS _metadata_provider,"
            "q.metadata_provider_id AS _metadata_provider_id FROM candidates c "
            "LEFT JOIN sources s ON s.id=c.source_id "
            "LEFT JOIN candidate_quality q ON q.candidate_id=c.id "
            "WHERE c.status!='rejected' ORDER BY c.id"
        )
        params: tuple[object, ...] = ()
        if limit is not None:
            query += " LIMIT ?"
            params = (max(1, int(limit)),)
        candidates = rows(query, params)
    if not candidates:
        with transaction() as con:
            deduped = _dedupe_and_hide(con)
        return {"audited": 0, "accepted": 0, "quarantine": 0, "rejected": 0, "deduped": deduped, "enriched": 0}

    semaphore = asyncio.Semaphore(CATALOG_CONCURRENCY)
    cache: dict[tuple[str, ...], asyncio.Task[dict[str, Any] | None]] = {}
    google_books_api_key = private_setting("association_google_books_api_key", "").strip()
    async with metadata_client() as client:
        async def run(candidate):
            async with semaphore:
                return await _audit_one(candidate, client, cache, google_books_api_key)

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
