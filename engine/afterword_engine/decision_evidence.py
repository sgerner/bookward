"""Bounded, run-linked snapshots for reproducing served recommendation decisions.

Snapshots deliberately live outside the mutable catalog foreign-key graph.
Ordinary feed cleanup may remove a candidate while its run evidence remains
available. A caller handling an intentional privacy deletion must use the
explicit purge helper before removing the catalog row.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import zlib
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np

from .config import settings
from .database import connect
from .embeddings import content_hash
from .identity import book_identity
from .scoring import document


LOGGER = logging.getLogger(__name__)

POLICY = "rating-neighborhood"
POLICY_VERSION = "rating-kernel-recency-interaction-installation-slate-v1"
EVIDENCE_SCHEMA_VERSION = 1

# Request-time caps keep evidence work predictable. Current recommendation
# responses contain at most 100 served rows and production pools are far below
# these limits. A cap failure records an explicit incomplete run and leaves
# serving untouched.
MAX_POOL_ITEMS = 5_000
MAX_PROFILE_ITEMS = 5_000
MAX_VECTOR_COUNT = 6_000
MAX_VECTOR_DIMENSIONS = 8_192
MAX_VECTOR_BYTES = 32 * 1024 * 1024
MAX_EVIDENCE_JSON_BYTES = 8 * 1024 * 1024
MAX_STORED_EVIDENCE_BYTES = 512 * 1024 * 1024
MAX_EXPOSURES_PER_RUN = 5_000
MAX_ARCHIVED_EVENTS_PER_RUN = 2_000
MAX_ARCHIVED_OUTCOMES_PER_RUN = 2_000
MAX_FIELD_BYTES = 24 * 1024
MAX_JSON_DEPTH = 8

POOL_FIELDS = (
    "id", "title", "author", "description", "genres", "score", "explanation",
    "status", "source_id", "source_name", "source_weight", "catalog_confidence",
    "quality_score", "quality_status", "quality_work_id", "quality_provider",
    "quality_isbn13", "quality_isbn10", "metadata_confidence", "release_date",
    "date_kind", "reading_status", "up_next", "reading_rating", "started_at",
    "finished_at", "propensity",
)
PROFILE_FIELDS = (
    "id", "candidate_id", "event_type", "value", "occurred_at", "title",
    "author", "genres", "description",
)
REQUEST_FIELDS = (
    "status", "limit", "offset", "recommended_limit", "build_id",
    "engine_revision", "pipeline_version", "ranking_at", "exploration_enabled",
    "exploration_epsilon", "exploration_stable_top_k",
)
RANKING_DIAGNOSTIC_FIELDS = (
    "mode", "scope", "applied", "observed_books", "qualified_features",
    "semantic_evidence_books", "semantic_adjusted_candidates",
    "adjusted_candidates", "max_score_adjustment",
)
SLATE_DIAGNOSTIC_FIELDS = (
    "policy_version", "scope", "applied", "reason", "slate_size",
    "max_displacement", "max_score_sacrifice", "max_books_per_author",
    "near_duplicate_cosine", "author_repeat_swaps", "similarity_swaps",
    "reordered_items",
)


class _EvidenceLimitError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _incomplete(reason: str) -> dict[str, Any]:
    LOGGER.warning("Recommendation decision evidence is incomplete: %s", reason)
    return {
        "schema_version": EVIDENCE_SCHEMA_VERSION,
        "capture_status": "incomplete",
        "reason": reason,
        "captured_at": _utc_now(),
    }


def _json_value(value: Any, *, depth: int = 0) -> Any:
    if depth > MAX_JSON_DEPTH:
        raise _EvidenceLimitError("json_depth_exceeded")
    if isinstance(value, np.generic):
        value = value.item()
    if value is None or isinstance(value, (str, bool, int)):
        if isinstance(value, str) and len(value.encode("utf-8")) > MAX_FIELD_BYTES:
            raise _EvidenceLimitError("field_limit_exceeded")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _EvidenceLimitError("non_finite_value")
        return value
    if isinstance(value, (list, tuple)):
        if len(value) > 1_000:
            raise _EvidenceLimitError("field_item_limit_exceeded")
        return [_json_value(item, depth=depth + 1) for item in value]
    if isinstance(value, Mapping):
        if len(value) > 128:
            raise _EvidenceLimitError("field_item_limit_exceeded")
        output: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > 128:
                raise _EvidenceLimitError("invalid_json_key")
            output[key] = _json_value(item, depth=depth + 1)
        return output
    raise _EvidenceLimitError("unsupported_json_value")


def _snapshot(item: Mapping[str, Any], fields: Iterable[str]) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise _EvidenceLimitError("invalid_record")
    result = {
        key: _json_value(item[key])
        for key in fields
        if key in item
    }
    return result


def _bounded_materialize(values: Iterable[Any], *, limit: int, reason: str) -> list[Any]:
    result = []
    for value in values:
        if len(result) >= limit:
            raise _EvidenceLimitError(reason)
        result.append(value)
    return result


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise _EvidenceLimitError("serialization_failed") from exc


def _safe_identifier(value: Any, *, fallback: str = "unknown") -> str:
    if not isinstance(value, str):
        return fallback
    candidate = value.strip()
    if (
        not candidate
        or len(candidate) > 128
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/+-]*", candidate)
        or "://" in candidate
    ):
        return fallback
    return candidate


@lru_cache(maxsize=1)
def _runtime_source_lineage() -> dict[str, Any]:
    """Hash the loaded pipeline's public source files once per engine process."""

    package = Path(__file__).resolve().parent
    modules = (
        "main.py",
        "database.py",
        "config.py",
        "scoring.py",
        "ranking.py",
        "interaction_personalization.py",
        "discovery_slate.py",
        "exploration.py",
        "embeddings.py",
        "subjects.py",
        "identity.py",
        "learning.py",
        "decision_evidence.py",
    )
    digests: dict[str, str] = {}
    for name in modules:
        try:
            digests[name] = hashlib.sha256((package / name).read_bytes()).hexdigest()
        except OSError:
            digests[name] = "unavailable"
    combined = "".join(f"{name}:{digests[name]}\n" for name in modules).encode()
    return {
        "source_hashes": digests,
        "runtime_code_id": "source:" + hashlib.sha256(combined).hexdigest(),
    }


def _safe_context(context: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(context, Mapping):
        return {}
    output: dict[str, Any] = {}
    for key in REQUEST_FIELDS:
        if key not in context:
            continue
        value = context[key]
        if key in {"build_id", "engine_revision", "pipeline_version"}:
            output[key] = _safe_identifier(value)
        else:
            output[key] = _json_value(value)
    return output


def _diagnostics(value: Mapping[str, Any] | None, fields: Iterable[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {
        key: _json_value(value[key])
        for key in fields
        if key in value
    }


def _candidate_id(item: Mapping[str, Any]) -> int | None:
    try:
        value = int(item.get("id"))
    except (TypeError, ValueError, OverflowError):
        return None
    return value if value > 0 else None


def _profile_candidate_id(item: Mapping[str, Any]) -> int | None:
    try:
        value = int(item.get("candidate_id"))
    except (TypeError, ValueError, OverflowError):
        return None
    return value if value > 0 else None


def _identity_hash(title: Any, author: Any) -> str:
    identity = book_identity(str(title or ""), str(author or ""))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _budget_allows(connection, additional_bytes: int) -> bool:
    row = connection.execute(
        "SELECT used_bytes FROM recommendation_evidence_budget WHERE id=1"
    ).fetchone()
    return int(row["used_bytes"] if row else 0) + max(0, int(additional_bytes)) <= MAX_STORED_EVIDENCE_BYTES


def _embedding_configuration(connection) -> tuple[str, str]:
    configured = {
        str(row["key"]): str(row["value"])
        for row in connection.execute(
            "SELECT key,value FROM settings WHERE key IN ('embedding_backend','embedding_model')"
        ).fetchall()
    }
    backend = _safe_identifier(
        configured.get("embedding_backend", settings.embedding_backend)
    )
    model = _safe_identifier(configured.get("embedding_model", settings.embedding_model))
    return backend, model


def _embedding_hashes(connection, backend: str, model: str, candidate_ids: list[int]):
    result: dict[int, tuple[str, int]] = {}
    for start in range(0, len(candidate_ids), 500):
        batch = candidate_ids[start : start + 500]
        if not batch:
            continue
        marks = ",".join("?" for _ in batch)
        found = connection.execute(
            "SELECT entity_id,content_hash,dimensions FROM embeddings "
            "WHERE entity_type='candidate' AND backend=? AND model=? "
            f"AND entity_id IN ({marks})",
            (backend, model, *batch),
        ).fetchall()
        for row in found:
            result[int(row["entity_id"])] = (
                str(row["content_hash"] or ""), int(row["dimensions"] or 0)
            )
    return result


def build_decision_evidence(
    connection,
    *,
    base_pool,
    personalized_pool,
    final_pool,
    interaction_events,
    cached_vectors,
    ranking_metadata,
    discovery_slate_metadata,
    request_context,
) -> dict[str, Any]:
    """Capture bounded exact serving inputs and actual vector artifacts.

    Candidate ``score`` values in ``base_pool`` are materialized outputs from
    the latest scoring job. The request path does not hold that job's read and
    vector inputs, so the evidence explicitly marks their derivation unknown.
    The stored score itself, every serving-stage order, current interaction
    profile, and exact cached vectors used by the online stages are preserved.
    """

    owned_connection = None
    try:
        if connection is None:
            owned_connection = connect()
            connection = owned_connection
        base_items = _bounded_materialize(
            base_pool, limit=MAX_POOL_ITEMS, reason="pool_limit_exceeded"
        )
        personalized_items = _bounded_materialize(
            personalized_pool, limit=MAX_POOL_ITEMS, reason="pool_limit_exceeded"
        )
        final_items = _bounded_materialize(
            final_pool, limit=MAX_POOL_ITEMS, reason="pool_limit_exceeded"
        )
        profile_items = _bounded_materialize(
            interaction_events, limit=MAX_PROFILE_ITEMS, reason="profile_limit_exceeded"
        )

        pools = {
            "base": [_snapshot(item, POOL_FIELDS) for item in base_items],
            "personalized": [_snapshot(item, POOL_FIELDS) for item in personalized_items],
            "final": [_snapshot(item, POOL_FIELDS) for item in final_items],
        }
        profile = [_snapshot(item, PROFILE_FIELDS) for item in profile_items]
        candidate_by_id: dict[int, Mapping[str, Any]] = {}
        pool_ids: set[int] = set()
        identity_records: dict[int, tuple[str, str]] = {}
        for items in (base_items, personalized_items, final_items):
            for item in items:
                candidate_id = _candidate_id(item)
                if candidate_id is None:
                    continue
                pool_ids.add(candidate_id)
                candidate_by_id.setdefault(candidate_id, item)
                identity_records.setdefault(
                    candidate_id,
                    (str(item.get("title") or ""), str(item.get("author") or "")),
                )
        interaction_ids: set[int] = set()
        for item in profile_items:
            candidate_id = _profile_candidate_id(item)
            if candidate_id is None:
                continue
            interaction_ids.add(candidate_id)
            candidate_by_id.setdefault(candidate_id, item)
            identity_records.setdefault(
                candidate_id,
                (str(item.get("title") or ""), str(item.get("author") or "")),
            )

        # The embedding document is recomputed from the same captured fields
        # supplied to the cache loader. Keep that actual text hash even if a
        # concurrent cache refresh has since replaced the mutable row.
        actual_document_hashes: dict[int, str] = {}
        for candidate_id, item in candidate_by_id.items():
            try:
                actual_document_hashes[candidate_id] = content_hash(document(dict(item)))
            except Exception as exc:
                raise _EvidenceLimitError("document_hash_failed") from exc

        vectors_input = cached_vectors if isinstance(cached_vectors, Mapping) else {}
        vector_ids: list[int] = []
        for key in vectors_input:
            try:
                candidate_id = int(key)
            except (TypeError, ValueError, OverflowError):
                continue
            if candidate_id in pool_ids or candidate_id in interaction_ids:
                vector_ids.append(candidate_id)
        vector_ids = sorted(set(vector_ids))
        if len(vector_ids) > MAX_VECTOR_COUNT:
            raise _EvidenceLimitError("vector_count_exceeded")

        backend, model = _embedding_configuration(connection)
        embedding_rows = _embedding_hashes(connection, backend, model, vector_ids)
        vector_descriptors: list[dict[str, Any]] = []
        vector_bytes_total = 0
        for candidate_id in vector_ids:
            try:
                vector = np.asarray(vectors_input[candidate_id], dtype=np.float32)
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise _EvidenceLimitError("invalid_vector") from exc
            if (
                vector.ndim != 1
                or not vector.size
                or vector.size > MAX_VECTOR_DIMENSIONS
                or not np.isfinite(vector).all()
            ):
                raise _EvidenceLimitError("invalid_vector")
            raw_vector = vector.astype(np.dtype("<f4"), copy=False).tobytes(order="C")
            vector_bytes_total += len(raw_vector)
            if vector_bytes_total > MAX_VECTOR_BYTES:
                raise _EvidenceLimitError("vector_bytes_exceeded")
            source_hash, stored_dimensions = embedding_rows.get(candidate_id, ("", 0))
            descriptor = {
                "entity_type": "candidate" if candidate_id in pool_ids else "interaction",
                "entity_id": candidate_id,
                "backend": backend,
                "model": model,
                "dimensions": int(vector.size),
                "encoding": "float32-le",
                "source_content_hash": actual_document_hashes.get(candidate_id, ""),
                "stored_cache_content_hash": source_hash,
                "source_hash_status": (
                    "cache_match" if source_hash and source_hash == actual_document_hashes.get(candidate_id)
                    else "cache_mismatch" if source_hash
                    else "cache_row_missing"
                ),
                "stored_cache_dimensions": stored_dimensions,
                "dimension_status": (
                    "cache_match" if stored_dimensions == int(vector.size)
                    else "cache_mismatch" if stored_dimensions
                    else "cache_row_missing"
                ),
                "vector_bytes": raw_vector,
            }
            vector_descriptors.append(descriptor)

        safe_context = _safe_context(request_context)
        lineage = _runtime_source_lineage()
        requested_build_id = safe_context.get("build_id")
        environment_build_id = _safe_identifier(os.environ.get("BOOKWARD_BUILD_ID"))
        build_id = (
            requested_build_id
            if isinstance(requested_build_id, str) and requested_build_id != "unknown"
            else environment_build_id
            if environment_build_id != "unknown"
            else lineage["runtime_code_id"]
        )
        evidence: dict[str, Any] = {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "capture_status": "complete",
            "captured_at": _utc_now(),
            "request_context": safe_context,
            "policy": {
                "name": POLICY,
                "version": POLICY_VERSION,
                "build_id": build_id,
                "build_id_source": (
                    "request_context"
                    if requested_build_id and requested_build_id != "unknown"
                    else "environment"
                    if environment_build_id != "unknown"
                    else "runtime_source_hash"
                ),
                "runtime_code_id": lineage["runtime_code_id"],
                "source_hashes": lineage["source_hashes"],
                "embedding_backend": backend,
                "embedding_model": model,
                "ranking": _diagnostics(ranking_metadata, RANKING_DIAGNOSTIC_FIELDS),
                "discovery_slate": _diagnostics(
                    discovery_slate_metadata, SLATE_DIAGNOSTIC_FIELDS
                ),
            },
            "base_score_provenance": {
                "kind": "stored_candidate_score",
                "derivation_status": "unknown",
                "detail": (
                    "The serving request used candidate.score materialized by the latest "
                    "scoring job. That job's read/vector input artifact was not attached."
                ),
            },
            "pools": pools,
            "interaction_profile": profile,
            "vectors": [],
        }
        # The extra manifest is small; vector bytes are stored separately and
        # deduplicated by content hash when the run transaction is committed.
        evidence["vectors"] = [
            {key: value for key, value in item.items() if key != "vector_bytes"}
            for item in vector_descriptors
        ]
        encoded = _canonical_json(evidence)
        if len(encoded) + vector_bytes_total > MAX_EVIDENCE_JSON_BYTES + MAX_VECTOR_BYTES:
            raise _EvidenceLimitError("evidence_size_exceeded")
        if len(encoded) > MAX_EVIDENCE_JSON_BYTES:
            raise _EvidenceLimitError("evidence_json_bytes_exceeded")

        evidence["_vector_artifacts"] = vector_descriptors
        evidence["_candidate_identities"] = [
            {
                "candidate_id": candidate_id,
                "identity_hash": _identity_hash(title, author),
            }
            for candidate_id, (title, author) in sorted(identity_records.items())
        ]
        return evidence
    except _EvidenceLimitError as exc:
        return _incomplete(exc.reason)
    except Exception:
        LOGGER.warning("Recommendation decision evidence capture failed")
        return _incomplete("capture_failed")
    finally:
        if owned_connection is not None:
            owned_connection.close()


def _candidate_snapshot_json(item: Mapping[str, Any]) -> tuple[str, str]:
    """Build a compact served-card snapshot, falling back to safe identity fields."""

    try:
        candidate = _snapshot(item, POOL_FIELDS)
        payload = _canonical_json(candidate).decode("utf-8")
        if len(payload.encode("utf-8")) <= MAX_FIELD_BYTES:
            return payload, "complete"
    except _EvidenceLimitError:
        pass
    minimal = {
        key: _json_value(item[key])
        for key in ("id", "title", "author", "score", "status")
        if key in item
    }
    return _canonical_json(minimal).decode("utf-8"), "minimal"


def archive_served_exposures(
    connection,
    *,
    run_id: str,
    recommendations: Iterable[Mapping[str, Any]],
    presented_at: str,
) -> int:
    """Copy actual returned slots into the cleanup-independent evidence tables."""

    items = list(recommendations)
    if len(items) > MAX_EXPOSURES_PER_RUN:
        LOGGER.warning("Recommendation evidence exposure cap exceeded")
        return 0
    archived = 0
    for rank, item in enumerate(items, 1):
        try:
            candidate_id = _candidate_id(item)
            if candidate_id is None:
                continue
            propensity = float(item.get("propensity", 1.0))
            score = float(item.get("score") or 0.0)
            if not math.isfinite(score) or not math.isfinite(propensity):
                continue
            if not 0 < propensity <= 1:
                continue
            payload, snapshot_status = _candidate_snapshot_json(item)
            identity_hash = _identity_hash(item.get("title"), item.get("author"))
            estimated_bytes = 160 + len(payload.encode("utf-8"))
            if not _budget_allows(connection, estimated_bytes):
                LOGGER.warning("Recommendation evidence storage quota reached for exposures")
                break
            connection.execute(
                "INSERT INTO recommendation_evidence_exposures(" 
                "run_id,candidate_id,identity_hash,rank,score,propensity,presented_at," 
                "candidate_snapshot_json,snapshot_status) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    candidate_id,
                    identity_hash,
                    rank,
                    score,
                    propensity,
                    presented_at,
                    payload,
                    snapshot_status,
                ),
            )
            archived += 1
        except Exception:
            # Serving telemetry is best-effort; the canonical impression insert
            # remains authoritative for the current retention window.
            LOGGER.warning("Could not archive one recommendation exposure")
    return archived


def _incomplete_row(connection, run_id: str, reason: str, captured_at: str) -> None:
    connection.execute(
        "INSERT INTO recommendation_run_evidence(" 
        "run_id,schema_version,capture_status,reason,captured_at) VALUES(?,?,?,?,?)",
        (run_id, EVIDENCE_SCHEMA_VERSION, "incomplete", reason, captured_at),
    )


def persist_run_evidence(connection, *, run_id: str, evidence: Mapping[str, Any]) -> str:
    """Persist a complete snapshot atomically, or a small incomplete marker."""

    status = evidence.get("capture_status")
    captured_at = str(evidence.get("captured_at") or _utc_now())
    if status != "complete":
        reason = str(evidence.get("reason") or "capture_unavailable")[:80]
        _incomplete_row(connection, run_id, reason, captured_at)
        return "incomplete"
    try:
        schema_version = int(evidence.get("schema_version", 0))
    except (TypeError, ValueError, OverflowError):
        schema_version = 0
    if schema_version != EVIDENCE_SCHEMA_VERSION:
        _incomplete_row(connection, run_id, "unsupported_schema", captured_at)
        return "incomplete"
    if any(
        not isinstance(evidence.get(key), Mapping)
        for key in ("request_context", "policy", "base_score_provenance", "pools")
    ) or not isinstance(evidence.get("interaction_profile"), list):
        _incomplete_row(connection, run_id, "invalid_evidence_payload", captured_at)
        return "incomplete"
    pool_payload = evidence["pools"]
    if any(
        not isinstance(pool_payload.get(key), list)
        or len(pool_payload[key]) > MAX_POOL_ITEMS
        or any(not isinstance(item, Mapping) for item in pool_payload[key])
        for key in ("base", "personalized", "final")
    ) or len(evidence["interaction_profile"]) > MAX_PROFILE_ITEMS or any(
        not isinstance(item, Mapping) for item in evidence["interaction_profile"]
    ):
        _incomplete_row(connection, run_id, "invalid_evidence_payload", captured_at)
        return "incomplete"

    artifacts = evidence.get("_vector_artifacts", [])
    identities = evidence.get("_candidate_identities", [])
    if not isinstance(artifacts, list) or len(artifacts) > MAX_VECTOR_COUNT:
        _incomplete_row(connection, run_id, "invalid_vector_manifest", captured_at)
        return "incomplete"
    if not isinstance(identities, list) or len(identities) > MAX_POOL_ITEMS * 3 + MAX_PROFILE_ITEMS:
        _incomplete_row(connection, run_id, "invalid_candidate_manifest", captured_at)
        return "incomplete"
    if any(
        not isinstance(item, Mapping)
        or _profile_candidate_id(item) is None
        or not isinstance(item.get("identity_hash"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("identity_hash") or ""))
        for item in identities
    ):
        _incomplete_row(connection, run_id, "invalid_candidate_manifest", captured_at)
        return "incomplete"

    vector_records: list[tuple[dict[str, Any], bytes, str]] = []
    new_vector_bytes = 0
    manifest = []
    try:
        for artifact in artifacts:
            if not isinstance(artifact, Mapping):
                raise _EvidenceLimitError("invalid_vector_manifest")
            raw = artifact.get("vector_bytes")
            if not isinstance(raw, bytes) or not raw or len(raw) % 4:
                raise _EvidenceLimitError("invalid_vector_artifact")
            dimensions = int(artifact.get("dimensions") or 0)
            if (
                dimensions <= 0
                or dimensions > MAX_VECTOR_DIMENSIONS
                or len(raw) != dimensions * 4
                or artifact.get("encoding") != "float32-le"
            ):
                raise _EvidenceLimitError("invalid_vector_artifact")
            backend = _safe_identifier(artifact.get("backend"))
            model = _safe_identifier(artifact.get("model"))
            entity_type = artifact.get("entity_type")
            if entity_type not in {"candidate", "interaction"}:
                raise _EvidenceLimitError("invalid_vector_artifact")
            entity_id = int(artifact.get("entity_id") or 0)
            if entity_id <= 0:
                raise _EvidenceLimitError("invalid_vector_artifact")
            spec = _canonical_json(
                {
                    "backend": backend,
                    "model": model,
                    "dimensions": dimensions,
                    "encoding": "float32-le",
                }
            )
            artifact_hash = hashlib.sha256(spec + b"\0" + raw).hexdigest()
            vector_sha256 = hashlib.sha256(raw).hexdigest()
            descriptor = {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "backend": backend,
                "model": model,
                "dimensions": dimensions,
                "encoding": "float32-le",
                "source_content_hash": str(artifact.get("source_content_hash") or ""),
                "artifact_hash": artifact_hash,
                "vector_sha256": vector_sha256,
            }
            existing = connection.execute(
                "SELECT length(vector) bytes FROM recommendation_evidence_vectors "
                "WHERE artifact_hash=?",
                (artifact_hash,),
            ).fetchone()
            if existing is None:
                new_vector_bytes += len(raw)
            vector_records.append((descriptor, raw, artifact_hash))
            manifest.append(descriptor)
        payload_value = {
            key: value
            for key, value in evidence.items()
            if not key.startswith("_") and key != "vectors"
        }
        payload_value["vectors"] = manifest
        raw_json = _canonical_json(payload_value)
        if len(raw_json) > MAX_EVIDENCE_JSON_BYTES:
            raise _EvidenceLimitError("evidence_json_bytes_exceeded")
        packed_json = zlib.compress(raw_json, level=1)
        if len(packed_json) > MAX_EVIDENCE_JSON_BYTES:
            raise _EvidenceLimitError("compressed_evidence_bytes_exceeded")
        budget = connection.execute(
            "SELECT used_bytes FROM recommendation_evidence_budget WHERE id=1"
        ).fetchone()
        used_bytes = int(budget["used_bytes"] if budget else 0)
        if used_bytes + len(packed_json) + new_vector_bytes > MAX_STORED_EVIDENCE_BYTES:
            raise _EvidenceLimitError("storage_quota_exceeded")
        reference_bytes = 96 * len(vector_records) + sum(
            72 + len(str(item.get("identity_hash") or "").encode("utf-8"))
            for item in identities
            if isinstance(item, Mapping)
        )
        if used_bytes + len(packed_json) + new_vector_bytes + reference_bytes > MAX_STORED_EVIDENCE_BYTES:
            raise _EvidenceLimitError("storage_quota_exceeded")
    except _EvidenceLimitError as exc:
        _incomplete_row(connection, run_id, exc.reason, captured_at)
        return "incomplete"
    except Exception:
        LOGGER.warning("Recommendation decision evidence validation failed")
        _incomplete_row(connection, run_id, "validation_failed", captured_at)
        return "incomplete"

    payload_sha256 = hashlib.sha256(raw_json).hexdigest()
    connection.execute(
        "INSERT INTO recommendation_run_evidence(" 
        "run_id,schema_version,capture_status,reason,captured_at,payload_encoding," 
        "payload,payload_sha256,payload_bytes,vector_count) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            run_id,
            EVIDENCE_SCHEMA_VERSION,
            "complete",
            "",
            captured_at,
            "zlib-json",
            packed_json,
            payload_sha256,
            len(raw_json),
            len(vector_records),
        ),
    )
    for descriptor, raw, artifact_hash in vector_records:
        connection.execute(
            "INSERT OR IGNORE INTO recommendation_evidence_vectors(" 
            "artifact_hash,backend,model,dimensions,encoding,vector) "
            "VALUES(?,?,?,?,?,?)",
            (
                artifact_hash,
                descriptor["backend"],
                descriptor["model"],
                descriptor["dimensions"],
                "float32-le",
                raw,
            ),
        )
        stored = connection.execute(
            "SELECT vector FROM recommendation_evidence_vectors WHERE artifact_hash=?",
            (artifact_hash,),
        ).fetchone()
        if stored is None or bytes(stored["vector"]) != raw:
            raise ValueError("Recommendation vector artifact hash collision")
        connection.execute(
            "INSERT INTO recommendation_run_evidence_vectors(" 
            "run_id,entity_type,entity_id,artifact_hash) VALUES(?,?,?,?)",
            (run_id, descriptor["entity_type"], descriptor["entity_id"], artifact_hash),
        )
    for identity in identities:
        candidate_id = int(identity.get("candidate_id") or 0)
        if candidate_id <= 0:
            continue
        connection.execute(
            "INSERT OR IGNORE INTO recommendation_run_evidence_candidates(" 
            "run_id,candidate_id,identity_hash) VALUES(?,?,?)",
            (run_id, candidate_id, str(identity.get("identity_hash") or "")),
        )
    return "complete"


def load_decision_evidence(connection, run_id: str) -> dict[str, Any] | None:
    """Load and verify a replay artifact, including its actual vector bytes."""

    row = connection.execute(
        "SELECT schema_version,capture_status,reason,captured_at,payload_encoding," 
        "payload,payload_sha256,payload_bytes,vector_count "
        "FROM recommendation_run_evidence WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    result: dict[str, Any] = {
        "schema_version": int(row["schema_version"]),
        "capture_status": str(row["capture_status"]),
        "reason": str(row["reason"] or ""),
        "captured_at": str(row["captured_at"]),
    }
    if row["capture_status"] != "complete":
        return result
    if row["payload_encoding"] != "zlib-json" or row["payload"] is None:
        raise ValueError("Unsupported recommendation evidence encoding")
    try:
        raw_json = zlib.decompress(bytes(row["payload"]))
        if len(raw_json) != int(row["payload_bytes"]):
            raise ValueError("Recommendation evidence size does not match")
        if hashlib.sha256(raw_json).hexdigest() != str(row["payload_sha256"]):
            raise ValueError("Recommendation evidence checksum does not match")
        payload = json.loads(raw_json)
    except (zlib.error, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Recommendation evidence payload is unreadable") from exc
    if not isinstance(payload, dict) or int(payload.get("schema_version", 0)) != EVIDENCE_SCHEMA_VERSION:
        raise ValueError("Unsupported recommendation evidence schema")

    vectors = []
    for descriptor in payload.get("vectors", []):
        if not isinstance(descriptor, dict):
            raise ValueError("Recommendation evidence vector manifest is invalid")
        artifact_hash = str(descriptor.get("artifact_hash") or "")
        entity_type = str(descriptor.get("entity_type") or "")
        entity_id = int(descriptor.get("entity_id") or 0)
        linked = connection.execute(
            "SELECT v.backend,v.model,v.dimensions,v.encoding," 
            "v.vector FROM recommendation_run_evidence_vectors r "
            "JOIN recommendation_evidence_vectors v ON v.artifact_hash=r.artifact_hash "
            "WHERE r.run_id=? AND r.entity_type=? AND r.entity_id=? AND r.artifact_hash=?",
            (run_id, entity_type, entity_id, artifact_hash),
        ).fetchone()
        if linked is None:
            raise ValueError("Recommendation evidence vector artifact is missing")
        vector_bytes = bytes(linked["vector"])
        dimensions = int(linked["dimensions"])
        spec = _canonical_json(
            {
                "backend": str(linked["backend"]),
                "model": str(linked["model"]),
                "dimensions": dimensions,
                "encoding": str(linked["encoding"]),
            }
        )
        if (
            str(linked["encoding"]) != "float32-le"
            or len(vector_bytes) != dimensions * 4
            or hashlib.sha256(spec + b"\0" + vector_bytes).hexdigest() != artifact_hash
            or hashlib.sha256(vector_bytes).hexdigest() != descriptor.get("vector_sha256")
        ):
            raise ValueError("Recommendation evidence vector checksum does not match")
        vectors.append({**descriptor, "vector_bytes": vector_bytes})
    if len(vectors) != int(row["vector_count"]):
        raise ValueError("Recommendation evidence vector count does not match")
    payload["vectors"] = vectors
    result["decision"] = payload
    result["served_exposures"] = [
        dict(item)
        for item in connection.execute(
            "SELECT candidate_id,identity_hash,rank,score,propensity,presented_at," 
            "candidate_snapshot_json,snapshot_status "
            "FROM recommendation_evidence_exposures WHERE run_id=? ORDER BY rank",
            (run_id,),
        ).fetchall()
    ]
    result["events"] = [
        dict(item)
        for item in connection.execute(
            "SELECT event_key,revision,candidate_id,event_type,value,source,occurred_at "
            "FROM recommendation_evidence_events WHERE run_id=? ORDER BY id",
            (run_id,),
        ).fetchall()
    ]
    result["outcomes"] = [
        dict(item)
        for item in connection.execute(
            "SELECT event_key,event_revision,candidate_id,rank,score,propensity," 
            "presented_at,visible_at,label,label_kind,confidence,attributed_at "
            "FROM recommendation_evidence_outcomes WHERE run_id=? ORDER BY id",
            (run_id,),
        ).fetchall()
    ]
    return result


def archive_recommendation_event(
    connection,
    *,
    event_key: str,
    run_id: str | None,
    candidate_id: int,
    event_type: str,
    value: float | None,
    source: str,
    occurred_at: str,
) -> int | None:
    """Append a run-linked event revision without mutable-catalog references."""

    if not run_id:
        return None
    count = connection.execute(
        "SELECT COUNT(*) count FROM recommendation_evidence_events WHERE run_id=?",
        (run_id,),
    ).fetchone()["count"]
    if int(count) >= MAX_ARCHIVED_EVENTS_PER_RUN:
        LOGGER.warning("Recommendation evidence event cap exceeded")
        return None
    estimated_bytes = (
        128
        + len(event_key.encode("utf-8"))
        + len(event_type.encode("utf-8"))
        + len(source.encode("utf-8"))
        + len(occurred_at.encode("utf-8"))
    )
    if not _budget_allows(connection, estimated_bytes):
        LOGGER.warning("Recommendation evidence storage quota reached for events")
        return None
    revision = int(
        connection.execute(
            "SELECT COALESCE(MAX(revision),0)+1 revision "
            "FROM recommendation_evidence_events WHERE event_key=?",
            (event_key,),
        ).fetchone()["revision"]
    )
    try:
        cursor = connection.execute(
            "INSERT INTO recommendation_evidence_events(" 
            "event_key,revision,run_id,candidate_id,event_type,value,source,occurred_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                event_key,
                revision,
                run_id,
                candidate_id,
                event_type,
                value,
                source,
                occurred_at,
            ),
        )
        return int(cursor.lastrowid)
    except Exception:
        LOGGER.warning("Could not archive a recommendation event")
        return None


def archive_recommendation_outcome(
    connection,
    *,
    event_key: str,
    event_revision: int,
    candidate_id: int,
    impression: Mapping[str, Any],
    label: float,
    label_kind: str,
    confidence: float,
    attributed_at: str,
) -> bool:
    """Append the impression state attached to a newly captured outcome."""

    try:
        run_id = str(impression["run_id"])
        count = connection.execute(
            "SELECT COUNT(*) count FROM recommendation_evidence_outcomes WHERE run_id=?",
            (run_id,),
        ).fetchone()["count"]
        if int(count) >= MAX_ARCHIVED_OUTCOMES_PER_RUN:
            LOGGER.warning("Recommendation evidence outcome cap exceeded")
            return False
        visible = connection.execute(
            "SELECT MIN(occurred_at) visible_at FROM recommendation_evidence_events "
            "WHERE run_id=? AND candidate_id=? AND event_type='visible'",
            (run_id, candidate_id),
        ).fetchone()
        event_key_bytes = len(event_key.encode("utf-8"))
        label_kind_bytes = len(str(label_kind).encode("utf-8"))
        presented_at = str(impression["presented_at"])
        attributed_bytes = len(attributed_at.encode("utf-8"))
        presented_bytes = len(presented_at.encode("utf-8"))
        visible_bytes = len(str(visible["visible_at"]).encode("utf-8")) if visible and visible["visible_at"] else 0
        if not _budget_allows(
            connection,
            192 + event_key_bytes + label_kind_bytes + presented_bytes + attributed_bytes + visible_bytes,
        ):
            LOGGER.warning("Recommendation evidence storage quota reached for outcomes")
            return False
        connection.execute(
            "INSERT INTO recommendation_evidence_outcomes(" 
            "run_id,event_key,event_revision,candidate_id,rank,score,propensity," 
            "presented_at,visible_at,label,label_kind,confidence,attributed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                event_key,
                event_revision,
                candidate_id,
                int(impression["rank"]),
                float(impression["score"]),
                float(impression["propensity"]),
                presented_at,
                visible["visible_at"] if visible else None,
                float(label),
                str(label_kind),
                float(confidence),
                attributed_at,
            ),
        )
        return True
    except Exception:
        LOGGER.warning("Could not archive a recommendation outcome")
        return False


def purge_recommendation_evidence(
    connection,
    *,
    candidate_ids: Iterable[int] = (),
    run_ids: Iterable[str] = (),
) -> dict[str, int]:
    """Explicitly erase archived and live telemetry for a candidate or run.

    Candidate purge removes every run snapshot that contained that candidate,
    because its title, text, and vectors may also appear in shared pool
    artifacts. It also removes the candidate's ordinary recommendation events
    and feedback. Callers may then delete the catalog row. Whole-profile
    deletion continues to remove the profile database itself.
    """

    candidates = sorted({int(item) for item in candidate_ids if int(item) > 0})
    selected_runs = {str(item) for item in run_ids if item}
    if not candidates and not selected_runs:
        raise ValueError("Candidate IDs or run IDs are required for evidence purge")
    marks = ",".join("?" for _ in candidates)
    if candidates:
        rows = connection.execute(
            "SELECT run_id FROM recommendation_run_evidence_candidates "
            f"WHERE candidate_id IN ({marks}) UNION "
            "SELECT run_id FROM recommendation_evidence_exposures "
            f"WHERE candidate_id IN ({marks}) UNION "
            "SELECT run_id FROM recommendation_impressions "
            f"WHERE candidate_id IN ({marks})",
            (*candidates, *candidates, *candidates),
        ).fetchall()
        selected_runs.update(str(row["run_id"]) for row in rows if row["run_id"])
    run_list = sorted(selected_runs)
    removed_runs = 0
    if run_list:
        run_marks = ",".join("?" for _ in run_list)
        removed_runs = int(
            connection.execute(
                f"SELECT COUNT(*) count FROM recommendation_runs WHERE id IN ({run_marks})",
                run_list,
            ).fetchone()["count"]
        )
        # Existing run_id links use ON DELETE SET NULL, so remove those mutable
        # event rows explicitly before deleting the parent run.
        connection.execute(
            f"DELETE FROM recommendation_events WHERE run_id IN ({run_marks})",
            run_list,
        )
        connection.execute(
            f"DELETE FROM recommendation_runs WHERE id IN ({run_marks})",
            run_list,
        )
    if candidates:
        connection.execute(
            f"DELETE FROM recommendation_events WHERE candidate_id IN ({marks})",
            candidates,
        )
        connection.execute(
            f"DELETE FROM feedback WHERE candidate_id IN ({marks})", candidates
        )
        connection.execute(
            f"DELETE FROM recommendation_evidence_events WHERE candidate_id IN ({marks})",
            candidates,
        )
        connection.execute(
            f"DELETE FROM recommendation_evidence_outcomes WHERE candidate_id IN ({marks})",
            candidates,
        )
        connection.execute(
            f"DELETE FROM recommendation_evidence_exposures WHERE candidate_id IN ({marks})",
            candidates,
        )
    removed_vectors = connection.execute(
        "DELETE FROM recommendation_evidence_vectors WHERE NOT EXISTS ("
        "SELECT 1 FROM recommendation_run_evidence_vectors r "
        "WHERE r.artifact_hash=recommendation_evidence_vectors.artifact_hash)"
    ).rowcount
    return {"runs_removed": removed_runs, "vectors_removed": max(0, int(removed_vectors))}
