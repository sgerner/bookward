"""Attach strictly verified Open Library first-work-publication years.

The ranker consumes only ``first_publication_year``. It never interprets a
generic release date itself, which keeps edition dates and unverified catalog
values out of the publication-era feature.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import re
from typing import Any

from .database import normalize_key
from .identity import book_openlibrary_work_id
from .isbn import isbn_parts


PUBLICATION_YEAR_FIELD = "first_publication_year"
_FIRST_PUBLICATION_FIELDS = frozenset({"first_publish_year", "first_publish_date"})
_MIN_YEAR = 1000
_MAX_YEAR = 2100
_QUERY_BATCH_SIZE = 400


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str) or not value:
        return {}
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _strict_work_id(value: Any) -> str:
    """Accept a canonical Open Library work path, never an edition or alias."""
    raw = str(value or "").strip()
    canonical = book_openlibrary_work_id({"openlibrary_work_id": raw})
    return canonical if raw and canonical == raw else ""


def _read_identity_hash(read: Mapping[str, Any]) -> str:
    isbn13, isbn10 = isbn_parts(read.get("isbn"))
    identity = {
        "title": normalize_key(str(read.get("title") or ""), ""),
        "author": normalize_key("", str(read.get("author") or "")),
        "source_isbn": re.sub(r"[^0-9xX]", "", str(read.get("isbn") or "")).upper(),
        "isbn13": isbn13,
        "isbn10": isbn10,
        "openlibrary_work_id": book_openlibrary_work_id(read),
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _read_year(value: Any) -> int | None:
    """Read the UTC year from the same date forms supported by history scoring."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        return value.year
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.strptime(value, "%Y/%m/%d")
            except ValueError:
                try:
                    parsed = parsedate_to_datetime(value)
                except (TypeError, ValueError, AttributeError, IndexError):
                    return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).year


def _explicit_payload_year(value: Any, source_field: Any) -> int | None:
    """Parse the retained raw Open Library work publication value strictly."""
    text = str(value or "")
    field = str(source_field or "")
    if field not in _FIRST_PUBLICATION_FIELDS:
        return None
    if field == "first_publish_year":
        if not re.fullmatch(r"\d{4}", text):
            return None
        year = int(text)
        return year if _MIN_YEAR <= year <= _MAX_YEAR else None

    if re.fullmatch(r"\d{4}", text):
        year = int(text)
        return year if _MIN_YEAR <= year <= _MAX_YEAR else None
    if re.fullmatch(r"\d{4}-\d{2}", text):
        text = f"{text}-01"
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    return parsed.year if _MIN_YEAR <= parsed.year <= _MAX_YEAR else None


def _hashed_release_year(
    value: Any,
    field_info: Any,
    *,
    work_id: str,
) -> int | None:
    if not isinstance(field_info, Mapping):
        return None
    text = str(value or "")
    if (
        not text
        or field_info.get("provider") != "openlibrary"
        or field_info.get("provider_id") != work_id
        or field_info.get("source_field") not in _FIRST_PUBLICATION_FIELDS
        or field_info.get("value_sha256") != hashlib.sha256(text.encode("utf-8")).hexdigest()
        or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text)
    ):
        return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    return parsed.year if _MIN_YEAR <= parsed.year <= _MAX_YEAR else None


def _ledger_payload_years(ledger_rows: Sequence[Mapping[str, Any]], work_id: str) -> set[int]:
    years: set[int] = set()
    for ledger in ledger_rows:
        if (
            ledger.get("provider") != "openlibrary"
            or ledger.get("provider_id") != work_id
        ):
            continue
        source = _json_object(ledger.get("source_payload"))
        field = str(ledger.get("field") or "")
        field_provenance = source.get("field_provenance")
        payload = source.get("payload")
        if (
            not field
            or source.get("field") != field
            or not isinstance(field_provenance, Mapping)
            or field_provenance.get("provider") != "openlibrary"
            or field_provenance.get("provider_id") != work_id
            or not isinstance(payload, Mapping)
            or payload.get("key") != work_id
            or payload.get("publication_date_source_field") not in _FIRST_PUBLICATION_FIELDS
        ):
            continue
        year = _explicit_payload_year(
            payload.get("publication_date"),
            payload.get("publication_date_source_field"),
        )
        if year is not None:
            years.add(year)
    return years


def _fetch_entity_ledger_rows(db, entity_type: str, entity_ids: Sequence[int]):
    found: dict[int, list[dict[str, Any]]] = {}
    for start in range(0, len(entity_ids), _QUERY_BATCH_SIZE):
        batch = entity_ids[start : start + _QUERY_BATCH_SIZE]
        if not batch:
            continue
        marks = ",".join("?" for _ in batch)
        rows = db.execute(
            "SELECT entity_id,field,provider,provider_id,source_payload "
            "FROM metadata_field_provenance WHERE entity_type=? "
            f"AND entity_id IN ({marks})",
            (entity_type, *batch),
        ).fetchall()
        for row in rows:
            item = dict(row)
            found.setdefault(int(item["entity_id"]), []).append(item)
    return found


def _fetch_read_metadata(db, read_ids: Sequence[int]):
    found: dict[int, dict[str, Any]] = {}
    for start in range(0, len(read_ids), _QUERY_BATCH_SIZE):
        batch = read_ids[start : start + _QUERY_BATCH_SIZE]
        if not batch:
            continue
        marks = ",".join("?" for _ in batch)
        rows = db.execute(
            "SELECT read_id,verified_work_id,identity_hash,release_date,metadata_provenance "
            "FROM read_metadata WHERE read_id IN (" + marks + ")",
            tuple(batch),
        ).fetchall()
        found.update({int(row["read_id"]): dict(row) for row in rows})
    return found


def _fetch_candidate_quality(db, candidate_ids: Sequence[int]):
    found: dict[int, dict[str, Any]] = {}
    for start in range(0, len(candidate_ids), _QUERY_BATCH_SIZE):
        batch = candidate_ids[start : start + _QUERY_BATCH_SIZE]
        if not batch:
            continue
        marks = ",".join("?" for _ in batch)
        rows = db.execute(
            "SELECT candidate_id,quality_status,provider,work_id "
            "FROM candidate_quality WHERE candidate_id IN (" + marks + ")",
            tuple(batch),
        ).fetchall()
        found.update({int(row["candidate_id"]): dict(row) for row in rows})
    return found


def _entity_ids(items: Sequence[Mapping[str, Any]]) -> list[int]:
    found = set()
    for item in items:
        try:
            entity_id = int(item.get("id"))
        except (TypeError, ValueError, OverflowError):
            continue
        if entity_id > 0:
            found.add(entity_id)
    return sorted(found)


def _verified_read_year(
    read: Mapping[str, Any],
    metadata: Mapping[str, Any] | None,
    ledger_rows: Sequence[Mapping[str, Any]],
) -> int | None:
    if not isinstance(metadata, Mapping):
        return None
    if str(metadata.get("identity_hash") or "") != _read_identity_hash(read):
        return None
    work_id = _strict_work_id(metadata.get("verified_work_id"))
    current_work_id = book_openlibrary_work_id(read)
    if not work_id or (current_work_id and current_work_id != work_id):
        return None

    provenance = _json_object(metadata.get("metadata_provenance"))
    fields = provenance.get("fields")
    release_info = fields.get("release_date") if isinstance(fields, Mapping) else None
    years = set(_ledger_payload_years(ledger_rows, work_id))
    direct_year = _hashed_release_year(metadata.get("release_date"), release_info, work_id=work_id)
    if direct_year is not None:
        years.add(direct_year)
    if len(years) != 1:
        return None

    year = next(iter(years))
    read_year = _read_year(read.get("read_at"))
    if read_year is None or year > read_year:
        return None
    return year


def _verified_candidate_year(
    candidate: Mapping[str, Any],
    quality: Mapping[str, Any] | None,
    ledger_rows: Sequence[Mapping[str, Any]],
) -> int | None:
    if not isinstance(quality, Mapping) or quality.get("quality_status") != "accepted":
        return None
    if quality.get("provider") != "openlibrary":
        return None
    work_id = _strict_work_id(quality.get("work_id"))
    if not work_id:
        return None
    passed_work_id = candidate.get("quality_work_id")
    if passed_work_id and _strict_work_id(passed_work_id) != work_id:
        return None

    years = set(_ledger_payload_years(ledger_rows, work_id))
    for ledger in ledger_rows:
        if (
            ledger.get("field") != "release_date"
            or ledger.get("provider") != "openlibrary"
            or ledger.get("provider_id") != work_id
        ):
            continue
        source = _json_object(ledger.get("source_payload"))
        if source.get("field") != "release_date":
            continue
        field_info = source.get("field_provenance")
        year = _hashed_release_year(
            candidate.get("release_date"), field_info, work_id=work_id
        )
        if year is not None:
            years.add(year)
    return next(iter(years)) if len(years) == 1 else None


def attach_publication_years(
    db,
    reads: Sequence[MutableMapping[str, Any]],
    candidates: Sequence[MutableMapping[str, Any]],
) -> None:
    """Attach a fail-closed ``first_publication_year`` to each mutable row.

    Read years require a current read-metadata identity hash and are bounded by
    the read year. Candidate years require accepted Open Library work identity.
    For both entities, direct dates need matching field hashes; retained raw
    payloads must identify the same work and name an explicit first-publication
    field. Conflicting evidence and malformed values remain unknown.
    """
    mutable_reads = [item for item in reads if isinstance(item, MutableMapping)]
    mutable_candidates = [item for item in candidates if isinstance(item, MutableMapping)]
    for item in (*mutable_reads, *mutable_candidates):
        item[PUBLICATION_YEAR_FIELD] = None
        item["publication_work_id"] = None

    read_ids = _entity_ids(mutable_reads)
    candidate_ids = _entity_ids(mutable_candidates)
    if not read_ids and not candidate_ids:
        return

    read_metadata = _fetch_read_metadata(db, read_ids)
    read_ledger = _fetch_entity_ledger_rows(db, "read", read_ids)
    candidate_quality = _fetch_candidate_quality(db, candidate_ids)
    candidate_ledger = _fetch_entity_ledger_rows(db, "candidate", candidate_ids)

    for read in mutable_reads:
        try:
            read_id = int(read.get("id"))
        except (TypeError, ValueError, OverflowError):
            continue
        read[PUBLICATION_YEAR_FIELD] = _verified_read_year(
            read,
            read_metadata.get(read_id),
            read_ledger.get(read_id, ()),
        )
        metadata = read_metadata.get(read_id)
        if (metadata and metadata.get("identity_hash") == _read_identity_hash(read)):
            work_id = _strict_work_id(metadata.get("verified_work_id"))
            current_work_id = book_openlibrary_work_id(read)
            if work_id and (not current_work_id or current_work_id == work_id):
                read["publication_work_id"] = work_id
    for candidate in mutable_candidates:
        try:
            candidate_id = int(candidate.get("id"))
        except (TypeError, ValueError, OverflowError):
            continue
        candidate[PUBLICATION_YEAR_FIELD] = _verified_candidate_year(
            candidate,
            candidate_quality.get(candidate_id),
            candidate_ledger.get(candidate_id, ()),
        )
        if candidate[PUBLICATION_YEAR_FIELD] is not None:
            candidate["publication_work_id"] = _strict_work_id(
                candidate_quality[candidate_id].get("work_id")
            )
