"""Recommendation telemetry and conservative outcome attribution.

This module records behavior without changing ranking. A run captures the
exact candidates, ranks, scores, and propensities returned to a caller. Later
actions and imported reads can then be joined to those observations without
treating a missing action as a negative label.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import uuid
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from .database import transaction
from .decision_evidence import (
    POLICY,
    POLICY_VERSION,
    archive_recommendation_event,
    archive_recommendation_outcome,
    archive_served_exposures,
    persist_run_evidence,
    purge_recommendation_evidence,
    build_decision_evidence,
)
from .identity import book_identity, book_row_identity_match_keys


LOGGER = logging.getLogger(__name__)
EVENT_TYPES = {
    "visible",
    "detail_open",
    "source_open",
    "librarr_search",
    "librarr_import",
    "save",
    "reject",
    "maybe_later",
    "restore",
    "read",
}
HISTORICAL_READ_SOURCES = frozenset({"manual_history", "goodreads_csv"})
INACTIVE_CANDIDATE_STATUSES = frozenset({"rejected", "maybe_later", "hidden"})
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
    ranking_metadata: dict[str, Any] | None = None,
    discovery_slate_metadata: dict[str, Any] | None = None,
    decision_evidence: dict[str, Any] | None = None,
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
    if ranking_metadata:
        metadata["interaction_learning"] = ranking_metadata
    if discovery_slate_metadata:
        metadata["discovery_slate"] = discovery_slate_metadata
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
        try:
            con.execute("SAVEPOINT archive_served_exposures")
            archive_served_exposures(
                con,
                run_id=run_id,
                recommendations=items,
                presented_at=now,
            )
            con.execute("RELEASE SAVEPOINT archive_served_exposures")
        except Exception:
            con.execute("ROLLBACK TO SAVEPOINT archive_served_exposures")
            con.execute("RELEASE SAVEPOINT archive_served_exposures")
            LOGGER.warning("Recommendation exposure archive failed; serving telemetry continues")
        if decision_evidence is not None:
            try:
                con.execute("SAVEPOINT persist_decision_evidence")
                persist_run_evidence(con, run_id=run_id, evidence=decision_evidence)
                con.execute("RELEASE SAVEPOINT persist_decision_evidence")
            except Exception:
                con.execute("ROLLBACK TO SAVEPOINT persist_decision_evidence")
                con.execute("RELEASE SAVEPOINT persist_decision_evidence")
                LOGGER.warning("Recommendation decision evidence persistence failed")
    return run_id


def _candidate_exists(con, candidate_id: int) -> bool:
    return con.execute("SELECT 1 FROM candidates WHERE id=?", (candidate_id,)).fetchone() is not None


def _run_impression(con, run_id: str, candidate_id: int):
    return con.execute(
        "SELECT id,run_id,rank,score,propensity,presented_at,visible_at "
        "FROM recommendation_impressions WHERE run_id=? AND candidate_id=?",
        (run_id, candidate_id),
    ).fetchone()


def _values_equal(left: Any, right: float | None) -> bool:
    return (
        left is None
        and right is None
        or left is not None
        and right is not None
        and float(left) == float(right)
    )


def _existing_event_matches(
    existing,
    *,
    run_id: str | None,
    candidate_id: int,
    event_type: str,
    value: float | None,
    metadata: str,
) -> bool:
    if (
        int(existing["candidate_id"]) != int(candidate_id)
        or existing["event_type"] != event_type
    ):
        return False
    # Goodreads and manual reads reconcile run attribution, ratings, and
    # date-precision metadata across syncs; browser events remain immutable.
    if event_type == "read":
        return True
    return (
        existing["run_id"] == run_id
        and _values_equal(existing["value"], value)
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
    attributed_at: str | None = None,
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
            attributed_at or utc_now(),
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
        "SELECT id,event_key,run_id,candidate_id,event_type,value,occurred_at,metadata "
        "FROM recommendation_events WHERE event_key=?",
        (event_key,),
    ).fetchone()
    read_changed = False
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
        if event_type == "read" and (
            existing["run_id"] != run_id
            or not _values_equal(existing["value"], value)
            or existing["metadata"] != encoded_metadata
            or (occurred_at is not None and existing["occurred_at"] != timestamp)
        ):
            con.execute(
                "UPDATE recommendation_events SET run_id=?,value=?,occurred_at=?,metadata=? WHERE id=?",
                (run_id, value, timestamp, encoded_metadata, event_id),
            )
            read_changed = True
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
    impression = None
    outcome_at = None
    if label is not None:
        label = _validate_value(label)
        confidence = 0.5 if confidence is None else float(confidence)
        if not 0 <= confidence <= 1:
            raise ValueError("Outcome confidence must be between 0 and 1")
        impression = _find_latest_impression(con, candidate_id, run_id)
        deleted_superseded = 0
        if event_type == "read":
            if impression is None:
                deleted_superseded = con.execute(
                    "DELETE FROM recommendation_outcomes WHERE event_id=?",
                    (event_id,),
                ).rowcount
                con.execute(
                    "DELETE FROM recommendation_evidence_outcomes WHERE event_key=?",
                    (event_key,),
                )
            else:
                deleted_superseded = con.execute(
                    "DELETE FROM recommendation_outcomes WHERE event_id=? AND impression_id!=?",
                    (event_id, int(impression["id"])),
                ).rowcount
        outcome_at = utc_now()
        outcome_status = _record_outcome(
            con,
            impression_id=int(impression["id"]) if impression else None,
            event_id=event_id,
            read_id=read_id,
            label=float(label),
            label_kind=label_kind or event_type,
            confidence=confidence,
            attributed_at=outcome_at,
        )
        if outcome_status == "created" and deleted_superseded > 0:
            outcome_status = "updated"
    else:
        if event_type == "read" and duplicate:
            con.execute(
                "DELETE FROM recommendation_outcomes WHERE event_id=?",
                (event_id,),
            )
            con.execute(
                "DELETE FROM recommendation_evidence_outcomes WHERE event_key=?",
                (event_key,),
            )
        outcome_status = "none"
    if not duplicate or read_changed or (event_type == "read" and outcome_status == "updated"):
        if event_type == "read" and duplicate:
            con.execute(
                "DELETE FROM recommendation_evidence_outcomes WHERE event_key=?",
                (event_key,),
            )
        archived_revision = archive_recommendation_event(
            con,
            event_key=event_key,
            run_id=run_id,
            candidate_id=candidate_id,
            event_type=event_type,
            value=value,
            source=source,
            occurred_at=timestamp,
        )
        if (
            archived_revision is not None
            and impression is not None
            and label is not None
            and (read_changed or outcome_status in {"created", "updated"})
        ):
            archive_recommendation_outcome(
                con,
                event_key=event_key,
                event_revision=archived_revision,
                candidate_id=candidate_id,
                impression=impression,
                label=float(label),
                label_kind=label_kind or event_type,
                confidence=float(confidence),
                attributed_at=outcome_at or utc_now(),
            )
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


def _latest_preceding_impression(
    con,
    candidate_id: int,
    read_time: datetime | None,
    day_precision: bool,
):
    if read_time is None:
        return None
    impression_rows = con.execute(
        "SELECT id,run_id,rank,score,propensity,presented_at,visible_at "
        "FROM recommendation_impressions WHERE candidate_id=? "
        "ORDER BY COALESCE(visible_at,presented_at) DESC,id DESC",
        (candidate_id,),
    ).fetchall()
    return next(
        (item for item in impression_rows if _impression_precedes(item, read_time, day_precision)),
        None,
    )


def _attribute_read_outcomes_in_connection(con) -> dict[str, int]:
    from .ingestion import read_identity_rows_with_current_verified_work_ids

    scanned = created = updated = attributed = 0
    raw_reads = [dict(item) for item in con.execute("SELECT * FROM reads ORDER BY id").fetchall()]
    try:
        read_metadata_rows = [
            dict(item)
            for item in con.execute(
                "SELECT read_id,verified_work_id,identity_provider,identity_provider_id,identity_hash "
                "FROM read_metadata WHERE verified_work_id!=''"
            ).fetchall()
        ]
    except sqlite3.OperationalError as exc:
        if "no such table: read_metadata" not in str(exc).casefold():
            raise
        read_metadata_rows = []
    reads = read_identity_rows_with_current_verified_work_ids(raw_reads, read_metadata_rows)

    candidates = [
        dict(item)
        for item in con.execute(
            "SELECT c.id,c.title,c.author,c.source_url,c.status,c.score,c.isbn13,c.isbn10,"
            "q.work_id quality_work_id,q.provider quality_provider,"
            "q.isbn13 quality_isbn13,q.isbn10 quality_isbn10 "
            "FROM candidates c LEFT JOIN candidate_quality q ON q.candidate_id=c.id"
        ).fetchall()
    ]
    candidates_by_key: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    for item in candidates:
        for key in book_row_identity_match_keys(item):
            candidates_by_key[key].append(item)

    existing_read_events_by_read_id: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for event_row in con.execute(
        "SELECT id,event_key,run_id,candidate_id,occurred_at "
        "FROM recommendation_events WHERE event_type='read'"
    ).fetchall():
        event_dict = dict(event_row)
        key = str(event_dict["event_key"])
        parts = key.split(":")
        if len(parts) == 2 and parts[0] == "read" and parts[1].isdigit():
            existing_read_events_by_read_id[int(parts[1])].append(event_dict)
        elif len(parts) == 3 and parts[0] == "manual-read" and parts[2].isdigit():
            existing_read_events_by_read_id[int(parts[2])].append(event_dict)

    for read in reads:
        scanned += 1
        read_id = int(read["id"])
        matched_by_id: dict[int, dict[str, Any]] = {}
        for key in book_row_identity_match_keys(read):
            for item in candidates_by_key.get(key, ()):
                matched_by_id[int(item["id"])] = item

        read_time, day_precision = _read_time(read.get("read_at"))
        source = str(read.get("source") or "").strip().casefold()
        if read_time is None and source not in HISTORICAL_READ_SOURCES:
            read_time, day_precision = _read_time(read.get("created_at"))

        existing_events = existing_read_events_by_read_id.get(read_id, [])
        metadata = {
            "read_source": str(read.get("source") or "unknown")[:32],
            "time_precision": "day" if day_precision else "timestamp",
        }
        read_value = float(read["rating"]) if read.get("rating") is not None else None
        occurred_at = read.get("read_at") or read.get("created_at") or utc_now()

        if not matched_by_id or read_time is None:
            for existing_event in existing_events:
                record_event_in_connection(
                    con,
                    event_key=str(existing_event["event_key"]),
                    candidate_id=int(existing_event["candidate_id"]),
                    event_type="read",
                    run_id=None,
                    value=read_value,
                    source="read_import",
                    metadata=metadata,
                    occurred_at=read.get("read_at") or existing_event["occurred_at"] or occurred_at,
                )
            continue

        exact_identity = book_identity(read["title"], read["author"])
        impressions_by_candidate: dict[int, Any] = {
            cand_id: _latest_preceding_impression(con, cand_id, read_time, day_precision)
            for cand_id in matched_by_id
        }

        def _candidate_rank_key(item: dict[str, Any]):
            cand_id = int(item["id"])
            imp = impressions_by_candidate[cand_id]
            imp_time = str(imp["visible_at"] or imp["presented_at"] or "") if imp is not None else ""
            imp_id = int(imp["id"]) if imp is not None else 0
            return (
                1 if imp is not None else 0,
                1 if book_identity(item["title"], item["author"]) == exact_identity else 0,
                1 if str(item.get("status") or "") not in INACTIVE_CANDIDATE_STATUSES else 0,
                float(item.get("score") or 0.0),
                imp_time,
                imp_id,
                -cand_id,
            )

        candidate = max(matched_by_id.values(), key=_candidate_rank_key)
        candidate_id = int(candidate["id"])
        impression = impressions_by_candidate[candidate_id]
        if impression is None:
            for existing_event in existing_events:
                record_event_in_connection(
                    con,
                    event_key=str(existing_event["event_key"]),
                    candidate_id=int(existing_event["candidate_id"]),
                    event_type="read",
                    run_id=None,
                    value=read_value,
                    source="read_import",
                    metadata=metadata,
                    occurred_at=occurred_at,
                )
            continue

        label, label_kind, confidence = _read_label(read)
        manual_event_key = f"manual-read:{candidate_id}:{read_id:08d}"
        default_event_key = f"read:{read_id:08d}"
        existing_by_key = {str(item["event_key"]): item for item in existing_events}
        if manual_event_key in existing_by_key:
            event_key = manual_event_key
        elif (
            default_event_key not in existing_by_key
            or int(existing_by_key[default_event_key]["candidate_id"]) == candidate_id
        ):
            event_key = default_event_key
        else:
            event_key = f"read:{read_id:08d}:{candidate_id}"

        for existing_event in existing_events:
            if str(existing_event["event_key"]) != event_key:
                record_event_in_connection(
                    con,
                    event_key=str(existing_event["event_key"]),
                    candidate_id=int(existing_event["candidate_id"]),
                    event_type="read",
                    run_id=None,
                    value=read_value,
                    source="read_import",
                    metadata=metadata,
                    occurred_at=occurred_at,
                )

        result = record_event_in_connection(
            con,
            event_key=event_key,
            candidate_id=candidate_id,
            event_type="read",
            run_id=str(impression["run_id"]),
            value=read_value,
            source="read_import",
            metadata=metadata,
            occurred_at=occurred_at,
            label=label,
            label_kind=label_kind,
            confidence=confidence,
            read_id=read_id,
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


def attribute_read_outcomes(connection=None) -> dict[str, int]:
    """Join imported reads to the latest preceding recommendation exposure.

    No exposure means no outcome. This function is safe to call repeatedly;
    event and outcome keys are stable across Goodreads re-imports. Historical
    imports need an explicit completion date; their creation time is import
    time, not evidence of when the book was read.
    """

    if connection is not None:
        return _attribute_read_outcomes_in_connection(connection)
    with transaction() as con:
        return _attribute_read_outcomes_in_connection(con)
