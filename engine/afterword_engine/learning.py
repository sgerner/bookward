"""Recommendation telemetry and conservative outcome attribution.

This module records behavior without changing ranking. A run captures the
exact candidates, ranks, scores, and propensities returned to a caller. Later
actions and imported reads can then be joined to those observations without
treating a missing action as a negative label.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from .database import transaction
from .identity import book_identity


POLICY = "rating-neighborhood"
POLICY_VERSION = "rating-neighborhood-v1"
EVENT_TYPES = {
    "visible",
    "detail_open",
    "source_open",
    "librarr_search",
    "librarr_import",
    "save",
    "reject",
    "restore",
    "read",
}
EVENT_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
MAX_METADATA_BYTES = 4096


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_metadata(metadata: dict[str, Any] | None) -> str:
    value = metadata or {}
    if not isinstance(value, dict) or len(value) > 32:
        raise ValueError("Event metadata must be an object with at most 32 fields")
    if any(
        not isinstance(key, str)
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", key)
        for key in value
    ):
        raise ValueError("Event metadata keys must be simple names")
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("Event metadata must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ValueError("Event metadata is too large")
    return encoded


def _validate_identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not RUN_ID_RE.fullmatch(value):
        raise ValueError(f"Invalid {label}")
    return value


def _validate_event_key(value: str) -> str:
    if not isinstance(value, str) or not EVENT_KEY_RE.fullmatch(value):
        raise ValueError("Invalid event key")
    return value


def _validate_event_type(value: str) -> str:
    if value not in EVENT_TYPES:
        raise ValueError("Unsupported recommendation event type")
    return value


def _validate_value(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Event value must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError("Event value must be finite")
    return number


def create_recommendation_run(
    recommendations: Iterable[dict[str, Any]],
    *,
    session_id: str = "",
    status: str | None = None,
    limit: int | None = None,
) -> str:
    """Persist a ranked response and return its opaque run identifier.

    Callers may attach the exact marginal ``propensity`` emitted by an
    exploration policy.  Deterministic responses default to one.
    """

    if session_id:
        _validate_identifier(session_id, "session identifier")
    items = list(recommendations)
    run_id = str(uuid.uuid4())
    metadata = {"status": status or "all"}
    if limit is not None:
        metadata["limit"] = int(limit)
    now = utc_now()
    with transaction() as con:
        con.execute(
            "INSERT INTO recommendation_runs(id,policy,policy_version,session_id,candidate_count,metadata,created_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                run_id,
                POLICY,
                POLICY_VERSION,
                session_id,
                len(items),
                _json_metadata(metadata),
                now,
            ),
        )
        for rank, item in enumerate(items, 1):
            try:
                propensity = float(item.get("propensity", 1.0))
            except (TypeError, ValueError) as exc:
                raise ValueError("Recommendation propensity must be numeric") from exc
            if not math.isfinite(propensity) or not 0 < propensity <= 1:
                raise ValueError("Recommendation propensity must be between 0 and 1")
            con.execute(
                "INSERT INTO recommendation_impressions(run_id,candidate_id,rank,score,propensity,presented_at) "
                "VALUES(?,?,?,?,?,?)",
                (run_id, int(item["id"]), rank, float(item.get("score") or 0), propensity, now),
            )
    return run_id


def _candidate_exists(con, candidate_id: int) -> bool:
    return con.execute("SELECT 1 FROM candidates WHERE id=?", (candidate_id,)).fetchone() is not None


def _run_impression(con, run_id: str, candidate_id: int):
    return con.execute(
        "SELECT id,run_id,rank,score,propensity,presented_at,visible_at "
        "FROM recommendation_impressions WHERE run_id=? AND candidate_id=?",
        (run_id, candidate_id),
    ).fetchone()


def _existing_event_matches(
    existing,
    *,
    run_id: str | None,
    candidate_id: int,
    event_type: str,
    value: float | None,
    metadata: str,
) -> bool:
    # Goodreads can replace a previously unrated row with a later rating. The
    # stable read event key is allowed to update its numeric value; every
    # browser event remains immutable and idempotent.
    value_matches = event_type == "read" or (
        existing["value"] is None
        and value is None
        or existing["value"] is not None
        and value is not None
        and float(existing["value"]) == float(value)
    )
    return (
        existing["run_id"] == run_id
        and int(existing["candidate_id"]) == int(candidate_id)
        and existing["event_type"] == event_type
        and value_matches
        and existing["metadata"] == metadata
    )


def _find_latest_impression(con, candidate_id: int, run_id: str | None = None):
    if run_id:
        return _run_impression(con, run_id, candidate_id)
    return con.execute(
        "SELECT id,run_id,rank,score,propensity,presented_at,visible_at "
        "FROM recommendation_impressions WHERE candidate_id=? "
        "ORDER BY COALESCE(visible_at,presented_at) DESC, id DESC LIMIT 1",
        (candidate_id,),
    ).fetchone()


def _record_outcome(
    con,
    *,
    impression_id: int | None,
    event_id: int,
    read_id: int | None,
    label: float,
    label_kind: str,
    confidence: float,
):
    if impression_id is None:
        return "none"
    existing = con.execute(
        "SELECT id,read_id,label,label_kind,confidence FROM recommendation_outcomes WHERE impression_id=? AND event_id=?",
        (impression_id, event_id),
    ).fetchone()
    if existing and (
        existing["read_id"] == read_id
        and float(existing["label"]) == float(label)
        and existing["label_kind"] == label_kind
        and float(existing["confidence"]) == float(confidence)
    ):
        return "unchanged"
    con.execute(
        "INSERT INTO recommendation_outcomes(impression_id,event_id,read_id,label,label_kind,confidence,attributed_at) "
        "VALUES(?,?,?,?,?,?,?) ON CONFLICT(impression_id,event_id) DO UPDATE SET "
        "read_id=excluded.read_id,label=excluded.label,label_kind=excluded.label_kind,"
        "confidence=excluded.confidence,attributed_at=excluded.attributed_at",
        (
            impression_id,
            event_id,
            read_id,
            label,
            label_kind,
            confidence,
            utc_now(),
        ),
    )
    return "updated" if existing else "created"


def record_event_in_connection(
    con,
    *,
    event_key: str,
    candidate_id: int,
    event_type: str,
    run_id: str | None = None,
    value: float | None = None,
    source: str = "ui",
    metadata: dict[str, Any] | None = None,
    occurred_at: str | None = None,
    label: float | None = None,
    label_kind: str | None = None,
    confidence: float | None = None,
    read_id: int | None = None,
):
    """Insert one validated event inside a caller-owned transaction."""

    _validate_event_key(event_key)
    _validate_event_type(event_type)
    candidate_id = int(candidate_id)
    if candidate_id <= 0 or not _candidate_exists(con, candidate_id):
        raise ValueError("Recommendation not found")
    if run_id is not None:
        _validate_identifier(run_id, "recommendation run")
        if _run_impression(con, run_id, candidate_id) is None:
            raise ValueError("Recommendation run does not contain this candidate")
    value = _validate_value(value)
    if not isinstance(source, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,31}", source):
        raise ValueError("Invalid event source")
    encoded_metadata = _json_metadata(metadata)
    timestamp = occurred_at or utc_now()
    existing = con.execute(
        "SELECT id,event_key,run_id,candidate_id,event_type,value,metadata "
        "FROM recommendation_events WHERE event_key=?",
        (event_key,),
    ).fetchone()
    if existing:
        if not _existing_event_matches(
            existing,
            run_id=run_id,
            candidate_id=candidate_id,
            event_type=event_type,
            value=value,
            metadata=encoded_metadata,
        ):
            raise ValueError("Event key was already used for a different event")
        event_id = int(existing["id"])
        duplicate = True
        if event_type == "read" and existing["value"] != value:
            con.execute(
                "UPDATE recommendation_events SET value=?,occurred_at=? WHERE id=?",
                (value, timestamp, event_id),
            )
    else:
        cursor = con.execute(
            "INSERT INTO recommendation_events(event_key,run_id,candidate_id,event_type,value,source,occurred_at,metadata) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                event_key,
                run_id,
                candidate_id,
                event_type,
                value,
                source,
                timestamp,
                encoded_metadata,
            ),
        )
        event_id = int(cursor.lastrowid)
        duplicate = False
    if event_type == "visible" and run_id is not None:
        con.execute(
            "UPDATE recommendation_impressions SET visible_at=COALESCE(visible_at,?) "
            "WHERE run_id=? AND candidate_id=?",
            (timestamp, run_id, candidate_id),
        )
    if label is not None:
        label = _validate_value(label)
        confidence = 0.5 if confidence is None else float(confidence)
        if not 0 <= confidence <= 1:
            raise ValueError("Outcome confidence must be between 0 and 1")
        impression = _find_latest_impression(con, candidate_id, run_id)
        outcome_status = _record_outcome(
            con,
            impression_id=int(impression["id"]) if impression else None,
            event_id=event_id,
            read_id=read_id,
            label=float(label),
            label_kind=label_kind or event_type,
            confidence=confidence,
        )
    else:
        outcome_status = "none"
    return {"id": event_id, "duplicate": duplicate, "outcome": outcome_status}


def record_events(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    values = list(events)
    if not values or len(values) > 100:
        raise ValueError("Send between 1 and 100 recommendation events")
    accepted = 0
    duplicates = 0
    ids: list[int] = []
    with transaction() as con:
        for event in values:
            result = record_event_in_connection(con, **event)
            ids.append(result["id"])
            if result["duplicate"]:
                duplicates += 1
            else:
                accepted += 1
    return {"accepted": accepted, "duplicates": duplicates, "ids": ids}


def _read_time(value: Any):
    raw = str(value or "").strip()
    if not raw:
        return None, False
    try:
        if len(raw) == 10:
            try:
                parsed = datetime.fromisoformat(raw)
            except ValueError:
                parsed = datetime.strptime(raw, "%Y/%m/%d")
            return parsed.replace(tzinfo=timezone.utc), True
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            parsed = datetime.strptime(raw, "%Y/%m/%d")
    except (TypeError, ValueError):
        return None, False
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc), False


def _impression_precedes(impression, read_time: datetime, day_precision: bool) -> bool:
    impression_time, _ = _read_time(impression["presented_at"])
    if impression_time is None:
        return False
    # A Goodreads date without a time is deliberately strict: an exposure on
    # the same date could have happened after the book was finished.
    return impression_time.date() < read_time.date() if day_precision else impression_time < read_time


def _read_label(read: dict[str, Any]):
    rating = read.get("rating")
    if rating is None:
        return 0.75, "completed_unrated", 0.4
    try:
        rating = float(rating)
    except (TypeError, ValueError):
        return 0.75, "completed_unrated", 0.4
    if rating >= 4:
        return 1.0, "rating_positive", 1.0
    if rating <= 2:
        return 0.0, "rating_negative", 1.0
    return 0.5, "rating_neutral", 0.25


def attribute_read_outcomes() -> dict[str, int]:
    """Join imported reads to the latest preceding recommendation exposure.

    No exposure means no outcome. This function is safe to call repeatedly;
    event and outcome keys are stable across Goodreads re-imports.
    """

    scanned = created = updated = attributed = 0
    with transaction() as con:
        reads = [dict(item) for item in con.execute("SELECT * FROM reads ORDER BY id").fetchall()]
        candidates = [dict(item) for item in con.execute("SELECT id,title,author FROM candidates").fetchall()]
        by_identity = {book_identity(item["title"], item["author"]): item for item in candidates}
        for read in reads:
            scanned += 1
            candidate = by_identity.get(book_identity(read["title"], read["author"]))
            if not candidate:
                continue
            read_time, day_precision = _read_time(read.get("read_at"))
            if read_time is None:
                read_time, day_precision = _read_time(read.get("created_at"))
            if read_time is None:
                continue
            impression_rows = con.execute(
                "SELECT id,run_id,rank,score,propensity,presented_at,visible_at "
                "FROM recommendation_impressions WHERE candidate_id=? "
                "ORDER BY COALESCE(visible_at,presented_at) DESC,id DESC",
                (candidate["id"],),
            ).fetchall()
            impression = next(
                (item for item in impression_rows if _impression_precedes(item, read_time, day_precision)),
                None,
            )
            if impression is None:
                continue
            label, label_kind, confidence = _read_label(read)
            event_key = f"read:{int(read['id']):08d}"
            metadata = {
                "read_source": str(read.get("source") or "unknown")[:32],
                "time_precision": "day" if day_precision else "timestamp",
            }
            result = record_event_in_connection(
                con,
                event_key=event_key,
                candidate_id=int(candidate["id"]),
                event_type="read",
                run_id=str(impression["run_id"]),
                value=float(read["rating"]) if read.get("rating") is not None else None,
                source="read_import",
                metadata=metadata,
                occurred_at=read.get("read_at") or read.get("created_at") or utc_now(),
                label=label,
                label_kind=label_kind,
                confidence=confidence,
                read_id=int(read["id"]),
            )
            if result["outcome"] == "created":
                created += 1
            elif result["outcome"] == "updated":
                updated += 1
            if result["outcome"] in {"created", "updated"}:
                attributed += 1
    return {
        "reads_scanned": scanned,
        "outcomes_created": created,
        "outcomes_updated": updated,
        "reads_attributed": attributed,
    }
