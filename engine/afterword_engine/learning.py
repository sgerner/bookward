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
import uuid
from collections.abc import Iterable, Mapping, Sequence
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
from .identity import book_identity
from .tenancy import current_profile_id


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
HISTORICAL_READ_SOURCES = frozenset({"manual_history", "goodreads_csv", "storygraph_csv"})
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


DISCOVERY_RUN_STATUSES = frozenset({"", "recommended", "all"})
NON_DISCOVERY_ITEM_STATUSES = frozenset(
    {"saved", "imported", "rejected", "maybe_later", "decisions", "excluded", "read"}
)


def active_profile_key() -> str:
    """Return the canonical profile identifier for run ownership metadata."""

    selected = (current_profile_id() or "").strip()
    return selected or "legacy"


def _parse_run_metadata(raw_metadata: Any) -> Mapping[str, Any] | None:
    if raw_metadata is None or raw_metadata == "":
        return {}
    if isinstance(raw_metadata, str):
        try:
            parsed = json.loads(raw_metadata)
        except (TypeError, ValueError):
            return None
    elif isinstance(raw_metadata, Mapping):
        parsed = raw_metadata
    else:
        return None
    return parsed if isinstance(parsed, Mapping) else None


def is_discovery_run_metadata(raw_metadata: Any) -> bool:
    """Return True when run metadata belongs to a discovery recommendation run."""

    if raw_metadata is None or raw_metadata == "":
        return True
    parsed = _parse_run_metadata(raw_metadata)
    if parsed is None:
        return False
    status = parsed.get("status")
    if status is None:
        return True
    return isinstance(status, str) and status.strip().lower() in DISCOVERY_RUN_STATUSES


def is_matching_run_profile(
    raw_metadata: Any,
    expected_profile_id: str | None = None,
) -> bool:
    """Return True when run metadata belongs to the active profile (or legacy unlabeled row)."""

    parsed = _parse_run_metadata(raw_metadata)
    if parsed is None:
        return False
    recorded_profile = parsed.get("profile_id")
    if recorded_profile is None or recorded_profile == "":
        return True
    if not isinstance(recorded_profile, str):
        return False
    target = (expected_profile_id or active_profile_key()).strip() or "legacy"
    return recorded_profile.strip() == target


def create_recommendation_run(
    recommendations: Iterable[dict[str, Any]],
    *,
    run_id: str | None = None,
    session_id: str = "",
    exploration_seed: str | None = None,
    status: str | None = None,
    limit: int | None = None,
    offset: int = 0,
    ranking_metadata: dict[str, Any] | None = None,
    discovery_slate_metadata: dict[str, Any] | None = None,
    decision_evidence: dict[str, Any] | None = None,
    ranks: Sequence[int] | None = None,
) -> str:
    """Persist a ranked discovery slate and return its opaque run identifier.

    Callers may attach the exact marginal ``propensity`` emitted by an
    exploration policy.  Deterministic responses default to one.
    """

    if run_id is not None:
        _validate_identifier(run_id, "recommendation run")
    if session_id:
        _validate_identifier(session_id, "session identifier")
    if exploration_seed:
        _validate_identifier(exploration_seed, "exploration seed")
    if isinstance(offset, bool) or int(offset) < 0:
        raise ValueError("Recommendation offset must be a non-negative integer")
    if status is not None and (
        not isinstance(status, str) or status.strip().lower() not in DISCOVERY_RUN_STATUSES
    ):
        raise ValueError("Recommendation runs may only be created for discovery slates")
    items = list(recommendations)
    for item in items:
        item_status = item.get("status")
        if (
            isinstance(item_status, str)
            and item_status.strip().lower() in NON_DISCOVERY_ITEM_STATUSES
        ):
            raise ValueError(
                "Recommendation exposure runs may not log shortlist, imported, or archived items"
            )
    page_offset = int(offset)
    resolved_ranks = (
        [int(rank) for rank in ranks]
        if ranks is not None
        else list(range(page_offset + 1, page_offset + len(items) + 1))
    )
    if len(resolved_ranks) != len(items):
        raise ValueError("Recommendation ranks must match recommendation count")
    if any(rank <= 0 for rank in resolved_ranks):
        raise ValueError("Recommendation ranks must be positive")
    resolved_run_id = run_id or str(uuid.uuid4())
    normalized_status = status.strip() if isinstance(status, str) else ""
    metadata: dict[str, Any] = {
        "status": "recommended",
        "offset": page_offset,
        "profile_id": active_profile_key(),
    }
    if exploration_seed:
        metadata["exploration_seed"] = exploration_seed
    if normalized_status and normalized_status != "recommended":
        metadata["request_status"] = normalized_status
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
                resolved_run_id,
                POLICY,
                POLICY_VERSION,
                session_id,
                len(items),
                _json_metadata(metadata),
                now,
            ),
        )
        for rank, item in zip(resolved_ranks, items):
            try:
                propensity = float(item.get("propensity", 1.0))
            except (TypeError, ValueError) as exc:
                raise ValueError("Recommendation propensity must be numeric") from exc
            if not math.isfinite(propensity) or not 0 < propensity <= 1:
                raise ValueError("Recommendation propensity must be between 0 and 1")
            con.execute(
                "INSERT INTO recommendation_impressions(run_id,candidate_id,rank,score,propensity,presented_at) "
                "VALUES(?,?,?,?,?,?)",
                (resolved_run_id, int(item["id"]), int(rank), float(item.get("score") or 0), propensity, now),
            )
        try:
            con.execute("SAVEPOINT archive_served_exposures")
            archive_served_exposures(
                con,
                run_id=resolved_run_id,
                recommendations=items,
                presented_at=now,
                offset=page_offset,
                ranks=resolved_ranks,
            )
            con.execute("RELEASE SAVEPOINT archive_served_exposures")
        except Exception:
            con.execute("ROLLBACK TO SAVEPOINT archive_served_exposures")
            con.execute("RELEASE SAVEPOINT archive_served_exposures")
            LOGGER.warning("Recommendation exposure archive failed; serving telemetry continues")
        if decision_evidence is not None:
            try:
                con.execute("SAVEPOINT persist_decision_evidence")
                persist_run_evidence(con, run_id=resolved_run_id, evidence=decision_evidence)
                con.execute("RELEASE SAVEPOINT persist_decision_evidence")
            except Exception:
                con.execute("ROLLBACK TO SAVEPOINT persist_decision_evidence")
                con.execute("RELEASE SAVEPOINT persist_decision_evidence")
                LOGGER.warning("Recommendation decision evidence persistence failed")
    return resolved_run_id


def append_recommendation_run_impressions(
    run_id: str,
    recommendations: Iterable[dict[str, Any]],
    *,
    ranks: Sequence[int] | None = None,
    expected_profile_id: str | None = None,
) -> bool:
    """Append newly served discovery pagination candidates to an existing discovery run."""

    _validate_identifier(run_id, "recommendation run")
    items = list(recommendations)
    for item in items:
        item_status = item.get("status")
        if (
            isinstance(item_status, str)
            and item_status.strip().lower() in NON_DISCOVERY_ITEM_STATUSES
        ):
            raise ValueError(
                "Recommendation exposure runs may not log shortlist, imported, or archived items"
            )
    if ranks is not None and len(ranks) != len(items):
        raise ValueError("Recommendation ranks must match recommendation count")
    target_profile = (expected_profile_id or active_profile_key()).strip() or "legacy"
    now = utc_now()
    with transaction() as con:
        run_row = con.execute(
            "SELECT id, metadata FROM recommendation_runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if run_row is None:
            return False
        if not is_discovery_run_metadata(run_row["metadata"]) or not is_matching_run_profile(
            run_row["metadata"], target_profile
        ):
            raise ValueError("Invalid recommendation run")
        if not items:
            return True
        existing = con.execute(
            "SELECT candidate_id,rank FROM recommendation_impressions WHERE run_id=?",
            (run_id,),
        ).fetchall()
        existing_ids = {int(row["candidate_id"]) for row in existing}
        next_rank = max((int(row["rank"]) for row in existing), default=0) + 1
        new_items: list[dict[str, Any]] = []
        new_ranks: list[int] = []
        for idx, item in enumerate(items):
            candidate_id = int(item["id"])
            if candidate_id in existing_ids:
                continue
            rank = int(ranks[idx]) if ranks is not None else next_rank
            if rank <= 0:
                raise ValueError("Recommendation ranks must be positive")
            if ranks is None:
                next_rank += 1
            try:
                propensity = float(item.get("propensity", 1.0))
            except (TypeError, ValueError) as exc:
                raise ValueError("Recommendation propensity must be numeric") from exc
            if not math.isfinite(propensity) or not 0 < propensity <= 1:
                raise ValueError("Recommendation propensity must be between 0 and 1")
            con.execute(
                "INSERT INTO recommendation_impressions(run_id,candidate_id,rank,score,propensity,presented_at) "
                "VALUES(?,?,?,?,?,?)",
                (run_id, candidate_id, rank, float(item.get("score") or 0), propensity, now),
            )
            existing_ids.add(candidate_id)
            new_items.append(item)
            new_ranks.append(rank)
        if new_items:
            con.execute(
                "UPDATE recommendation_runs SET candidate_count=("
                "SELECT COUNT(*) FROM recommendation_impressions WHERE run_id=?) WHERE id=?",
                (run_id, run_id),
            )
            try:
                con.execute("SAVEPOINT archive_served_exposures")
                archive_served_exposures(
                    con,
                    run_id=run_id,
                    recommendations=new_items,
                    presented_at=now,
                    ranks=new_ranks,
                )
                con.execute("RELEASE SAVEPOINT archive_served_exposures")
            except Exception:
                con.execute("ROLLBACK TO SAVEPOINT archive_served_exposures")
                con.execute("RELEASE SAVEPOINT archive_served_exposures")
                LOGGER.warning("Recommendation exposure archive failed; serving telemetry continues")
    return True


def _candidate_exists(con, candidate_id: int) -> bool:
    return con.execute("SELECT 1 FROM candidates WHERE id=?", (candidate_id,)).fetchone() is not None


def _run_impression(con, run_id: str, candidate_id: int):
    return con.execute(
        "SELECT i.id,i.run_id,i.rank,i.score,i.propensity,i.presented_at,i.visible_at,"
        "r.metadata AS run_metadata "
        "FROM recommendation_impressions i "
        "LEFT JOIN recommendation_runs r ON r.id=i.run_id "
        "WHERE i.run_id=? AND i.candidate_id=?",
        (run_id, candidate_id),
    ).fetchone()


def validate_candidate_run_id(con, candidate_id: int | None, run_id: str | None) -> str | None:
    if run_id is None:
        return None
    _validate_identifier(run_id, "recommendation run")
    if candidate_id is None or int(candidate_id) <= 0 or not _candidate_exists(con, int(candidate_id)):
        raise ValueError("Recommendation not found")
    if _run_impression(con, run_id, int(candidate_id)) is None:
        raise ValueError("Recommendation run does not contain this candidate")
    return run_id


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
        matched = _run_impression(con, run_id, candidate_id)
        if matched is not None and is_discovery_run_metadata(matched["run_metadata"]):
            return matched
    rows = con.execute(
        "SELECT i.id,i.run_id,i.rank,i.score,i.propensity,i.presented_at,i.visible_at,"
        "r.metadata AS run_metadata "
        "FROM recommendation_impressions i "
        "JOIN recommendation_runs r ON r.id=i.run_id "
        "WHERE i.candidate_id=? "
        "ORDER BY COALESCE(i.visible_at,i.presented_at) DESC, i.id DESC",
        (candidate_id,),
    ).fetchall()
    return next(
        (row for row in rows if is_discovery_run_metadata(row["run_metadata"])),
        None,
    )


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
        "SELECT id,event_key,run_id,candidate_id,event_type,value,metadata "
        "FROM recommendation_events WHERE event_key=?",
        (event_key,),
    ).fetchone()
    read_value_changed = False
    active_import = None
    if not existing and event_type == "librarr_import":
        latest_lifecycle = con.execute(
            "SELECT id,event_key,run_id,event_type FROM recommendation_events "
            "WHERE candidate_id=? AND event_type IN "
            "('save','reject','maybe_later','restore','read','librarr_import') "
            "ORDER BY datetime(occurred_at) DESC, id DESC LIMIT 1",
            (candidate_id,),
        ).fetchone()
        if latest_lifecycle and latest_lifecycle["event_type"] == "librarr_import":
            active_import = latest_lifecycle
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
            read_value_changed = True
    elif active_import is not None:
        event_id = int(active_import["id"])
        event_key = str(active_import["event_key"])
        if active_import["run_id"] is None and run_id is not None:
            con.execute(
                "UPDATE recommendation_events SET run_id=? WHERE id=?",
                (run_id, event_id),
            )
        else:
            run_id = active_import["run_id"] or run_id
        duplicate = True
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
    archived_revision = None
    if not duplicate or read_value_changed:
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
        if (
            active_import is not None
            and con.execute(
                "SELECT 1 FROM recommendation_outcomes WHERE event_id=? LIMIT 1",
                (event_id,),
            ).fetchone()
            is not None
        ):
            impression = None
            outcome_status = "unchanged"
        else:
            impression = _find_latest_impression(con, candidate_id, run_id)
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
            if archived_revision is None and active_import is not None and outcome_status in {"created", "updated"}:
                rev_row = con.execute(
                    "SELECT MAX(id) id FROM recommendation_evidence_events WHERE event_key=?",
                    (event_key,),
                ).fetchone()
                archived_revision = int(rev_row["id"]) if rev_row and rev_row["id"] is not None else None
        if (
            archived_revision is not None
            and impression is not None
            and outcome_status in {"created", "updated"}
        ):
            archive_recommendation_outcome(
                con,
                event_key=event_key,
                event_revision=archived_revision,
                candidate_id=candidate_id,
                impression=impression,
                label=float(label),
                label_kind=label_kind or event_type,
                confidence=confidence,
                attributed_at=outcome_at,
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
    event and outcome keys are stable across Goodreads re-imports. Historical
    imports need an explicit completion date; their creation time is import
    time, not evidence of when the book was read.
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
            source = str(read.get("source") or "").strip().casefold()
            if read_time is None and source not in HISTORICAL_READ_SOURCES:
                read_time, day_precision = _read_time(read.get("created_at"))
            if read_time is None:
                continue
            impression_rows = con.execute(
                "SELECT i.id,i.run_id,i.rank,i.score,i.propensity,i.presented_at,i.visible_at,"
                "r.metadata AS run_metadata "
                "FROM recommendation_impressions i "
                "JOIN recommendation_runs r ON r.id=i.run_id "
                "WHERE i.candidate_id=? "
                "ORDER BY COALESCE(i.visible_at,i.presented_at) DESC,i.id DESC",
                (candidate["id"],),
            ).fetchall()
            impression = next(
                (
                    item
                    for item in impression_rows
                    if is_discovery_run_metadata(item["run_metadata"])
                    and _impression_precedes(item, read_time, day_precision)
                ),
                None,
            )
            if impression is None:
                continue
            label, label_kind, confidence = _read_label(read)
            event_key = f"read:{int(read['id']):08d}"
            manual_event_key = (
                f"manual-read:{int(candidate['id'])}:{int(read['id']):08d}"
            )
            if con.execute(
                "SELECT 1 FROM recommendation_events WHERE event_key=?",
                (manual_event_key,),
            ).fetchone():
                event_key = manual_event_key
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
