"""Bounded immutable provenance for materialized base-score jobs."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import platform
import re
import zlib
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

import numpy as np

from .decision_evidence import (
    MAX_STORED_EVIDENCE_BYTES,
    _canonical_json,
    _identity_hash,
    _json_value,
    _runtime_source_lineage,
    _safe_identifier,
)
from .embeddings import content_hash
from .scoring import document


LOGGER = logging.getLogger(__name__)
SCORING_BATCH_SCHEMA_VERSION = 1
MAX_SCORING_READS = 10_000
MAX_SCORING_CANDIDATES = 5_000
MAX_SCORING_VECTORS = 15_000
MAX_SCORING_VECTOR_DIMENSIONS = 8_192
MAX_SCORING_VECTOR_BYTES = 64 * 1024 * 1024
MAX_SCORING_JSON_BYTES = 16 * 1024 * 1024
MAX_SCORING_FIELD_BYTES = 24 * 1024

READ_FIELDS = (
    "id", "title", "author", "rating", "read_at", "isbn",
    "openlibrary_work_id", "genres", "description", "first_publication_year", "publication_work_id",
)
IDENTITY_READ_FIELDS = (
    "id", "title", "author", "isbn", "openlibrary_work_id",
    "quality_work_id", "quality_provider",
    "read_metadata_work_id", "read_metadata_identity_provider",
    "read_metadata_identity_provider_id", "read_metadata_identity_hash",
)
CANDIDATE_FIELDS = (
    "id", "title", "author", "description", "genres", "status", "score",
    "source_id", "source_name", "source_weight", "quality_status",
    "quality_score", "catalog_confidence", "quality_work_id",
    "quality_provider", "quality_isbn13", "quality_isbn10", "score_batch_id",
    "first_publication_year", "publication_work_id",
)


class _LimitError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snapshot(item: Mapping[str, Any], fields: Iterable[str]) -> dict[str, Any]:
    if not isinstance(item, Mapping):
        raise _LimitError("invalid_input_row")
    result = {}
    for field in fields:
        if field not in item:
            continue
        value = _json_value(item[field])
        if isinstance(value, str) and len(value.encode("utf-8")) > MAX_SCORING_FIELD_BYTES:
            raise _LimitError("field_limit_exceeded")
        result[field] = value
    return result


def _vector_values(vector: Any) -> tuple[bytes, int]:
    try:
        values = np.asarray(vector, dtype=np.float32)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _LimitError("invalid_vector") from exc
    if (
        values.ndim != 1
        or not values.size
        or values.size > MAX_SCORING_VECTOR_DIMENSIONS
        or not np.isfinite(values).all()
    ):
        raise _LimitError("invalid_vector")
    raw = values.astype(np.dtype("<f4"), copy=False).tobytes(order="C")
    return raw, int(values.size)


def _vector_cache_rows(
    connection, *, backend: str, model: str, entities: list[tuple[str, int]]
):
    found = {}
    for entity_type in ("read", "candidate"):
        entity_ids = [entity_id for kind, entity_id in entities if kind == entity_type]
        for start in range(0, len(entity_ids), 400):
            batch = entity_ids[start : start + 400]
            if not batch:
                continue
            marks = ",".join("?" for _ in batch)
            rows = connection.execute(
                "SELECT entity_id,content_hash,dimensions FROM embeddings "
                "WHERE entity_type=? AND backend=? AND model=? "
                f"AND entity_id IN ({marks})",
                (entity_type, backend, model, *batch),
            ).fetchall()
            found.update(
                {
                    (entity_type, int(row["entity_id"])): (
                        str(row["content_hash"] or ""), int(row["dimensions"] or 0)
                    )
                    for row in rows
                }
            )
    return found


def _incomplete(
    reason: str,
    *,
    captured_at: str,
    read_count: int = 0,
    candidate_count: int = 0,
    vector_count: int = 0,
) -> dict[str, Any]:
    return {
        "schema_version": SCORING_BATCH_SCHEMA_VERSION,
        "capture_status": "incomplete",
        "reason": reason,
        "captured_at": captured_at,
        "read_count": max(0, int(read_count)),
        "candidate_count": max(0, int(candidate_count)),
        "vector_count": max(0, int(vector_count)),
    }


def build_scoring_batch_evidence(
    connection,
    *,
    captured_at: str,
    read_history,
    rated_reads,
    candidates,
    ranked,
    read_vectors,
    candidate_vectors,
    backend: str,
    model: str,
    scoring_batch_size: int,
) -> dict[str, Any]:
    """Build replay inputs for the exact inputs and outputs written by a scorer job.

    The active scorer's result is a materialized score. This artifact stores
    both the complete read identity projection used for eligibility and the
    rated reads, candidate inputs, exact vectors, and outputs passed/written
    by that particular invocation.
    """

    read_count = len(read_history) if hasattr(read_history, "__len__") else 0
    candidate_count = len(candidates) if hasattr(candidates, "__len__") else 0
    vector_count = 0
    try:
        all_reads = _bounded_list(read_history, MAX_SCORING_READS, "read_limit_exceeded")
        reads = _bounded_list(rated_reads, MAX_SCORING_READS, "read_limit_exceeded")
        candidate_items = _bounded_list(candidates, MAX_SCORING_CANDIDATES, "candidate_limit_exceeded")
        ranked_items = _bounded_list(ranked, MAX_SCORING_CANDIDATES, "candidate_limit_exceeded")
        if len(all_reads) > MAX_SCORING_READS:
            raise _LimitError("read_limit_exceeded")
        if len(candidate_items) > MAX_SCORING_CANDIDATES or len(ranked_items) > MAX_SCORING_CANDIDATES:
            raise _LimitError("candidate_limit_exceeded")
        if len(reads) != len(read_vectors):
            raise _LimitError("read_vector_count_mismatch")
        by_candidate = {int(item["id"]): item for item in candidate_items}
        output_by_id = {int(item["id"]): item for item in ranked_items}
        if (
            len(by_candidate) != len(candidate_items)
            or len(output_by_id) != len(ranked_items)
            or set(by_candidate) != set(output_by_id)
        ):
            raise _LimitError("candidate_output_mismatch")
        candidate_ids = list(by_candidate)
        candidate_vector_ids = set(candidate_ids)
        if not candidate_vector_ids.issubset(candidate_vectors):
            raise _LimitError("candidate_vector_missing")

        read_snapshots = [_snapshot(item, READ_FIELDS) for item in all_reads]
        read_identity = [_snapshot(item, IDENTITY_READ_FIELDS) for item in all_reads]
        rated_read_ids = [int(item["id"]) for item in reads]
        rated_read_snapshots = [_snapshot(item, READ_FIELDS) for item in reads]
        candidate_snapshots = [_snapshot(item, CANDIDATE_FIELDS) for item in candidate_items]
        outputs = []
        candidate_descriptors = {}
        for candidate_id in candidate_ids:
            output = output_by_id[candidate_id]
            try:
                score = float(output["score"])
                metadata_confidence = float(output["metadata_confidence"])
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise _LimitError("invalid_candidate_output") from exc
            if not math.isfinite(score) or not math.isfinite(metadata_confidence):
                raise _LimitError("invalid_candidate_output")
            candidate_input = by_candidate[candidate_id]
            input_text_hash = content_hash(document(dict(candidate_input)))
            identity_hash = _identity_hash(candidate_input.get("title"), candidate_input.get("author"))
            candidate_descriptors[candidate_id] = {
                "score": score,
                "identity_hash": identity_hash,
                "input_content_hash": input_text_hash,
            }
            outputs.append(
                {
                    "candidate_id": candidate_id,
                    "score": score,
                    "metadata_confidence": metadata_confidence,
                    "explanation": _json_value(output.get("explanation") or []),
                    **({"publication_era": _json_value(output["publication_era"])}
                       if "publication_era" in output else {}),
                }
            )

        configured_backend = _safe_identifier(backend)
        configured_model = _safe_identifier(model)
        vector_artifacts = []
        vector_bytes_total = 0
        source_documents: dict[tuple[str, int], tuple[str, Mapping[str, Any]]] = {}
        for read in reads:
            read_id = int(read["id"])
            source_documents[("read", read_id)] = (content_hash(document(dict(read))), read)
        for candidate_id in candidate_ids:
            source_documents[("candidate", candidate_id)] = (
                content_hash(document(dict(by_candidate[candidate_id]))),
                by_candidate[candidate_id],
            )
        vector_entities = [
            *(('read', read_id) for read_id in rated_read_ids),
            *(("candidate", candidate_id) for candidate_id in candidate_ids),
        ]
        vector_rows = _vector_cache_rows(
            connection,
            backend=configured_backend,
            model=configured_model,
            entities=vector_entities,
        )
        vector_specs = [
            *(('read', read_id, read_vectors[position]) for position, read_id in enumerate(rated_read_ids)),
            *(("candidate", candidate_id, candidate_vectors[candidate_id]) for candidate_id in candidate_ids),
        ]
        if len(vector_specs) > MAX_SCORING_VECTORS:
            raise _LimitError("vector_count_exceeded")
        seen_vector_keys: set[tuple[str, int]] = set()
        for entity_type, entity_id, vector in vector_specs:
            key = (entity_type, int(entity_id))
            if key in seen_vector_keys:
                raise _LimitError("duplicate_vector_identity")
            seen_vector_keys.add(key)
            raw, dimensions = _vector_values(vector)
            vector_bytes_total += len(raw)
            vector_count += 1
            if vector_bytes_total > MAX_SCORING_VECTOR_BYTES:
                raise _LimitError("vector_bytes_exceeded")
            source_hash, _source_item = source_documents[key]
            stored_hash, stored_dimensions = vector_rows.get(key, ("", 0))
            artifact_spec = _canonical_json(
                {
                    "backend": configured_backend,
                    "model": configured_model,
                    "dimensions": dimensions,
                    "encoding": "float32-le",
                }
            )
            vector_artifacts.append(
                {
                    "entity_type": entity_type,
                    "entity_id": int(entity_id),
                    "backend": configured_backend,
                    "model": configured_model,
                    "dimensions": dimensions,
                    "encoding": "float32-le",
                    "source_content_hash": source_hash,
                    "stored_cache_content_hash": stored_hash,
                    "source_hash_status": (
                        "cache_match" if stored_hash and stored_hash == source_hash
                        else "cache_mismatch" if stored_hash
                        else "cache_row_missing"
                    ),
                    "stored_cache_dimensions": stored_dimensions,
                    "artifact_hash": hashlib.sha256(artifact_spec + b"\0" + raw).hexdigest(),
                    "vector_sha256": hashlib.sha256(raw).hexdigest(),
                    "dimension_status": (
                        "cache_match" if stored_dimensions == dimensions
                        else "cache_mismatch" if stored_dimensions
                        else "cache_row_missing"
                    ),
                    "vector_bytes": raw,
                }
            )

        lineage = _runtime_source_lineage()
        try:
            import sklearn
            sklearn_version = str(sklearn.__version__)
        except Exception:
            sklearn_version = "unavailable"
        evidence = {
            "schema_version": SCORING_BATCH_SCHEMA_VERSION,
            "capture_status": "complete",
            "captured_at": captured_at,
            "policy": {
                "name": "materialized_base_score",
                "version": "rank-candidates-source-confidence-era-gated-v2",
                "runtime_code_id": lineage["runtime_code_id"],
                "source_hashes": lineage["source_hashes"],
                "deployment_build_id": _safe_identifier(os.environ.get("BOOKWARD_BUILD_ID")),
                "build_id_source": (
                    "environment"
                    if _safe_identifier(os.environ.get("BOOKWARD_BUILD_ID")) != "unknown"
                    else "runtime_source_hash"
                ),
                "embedding_backend": configured_backend,
                "embedding_model": configured_model,
                "scoring_batch_size": int(scoring_batch_size),
                "python_version": platform.python_version(),
                "numpy_version": str(np.__version__),
                "scikit_learn_version": sklearn_version,
            },
            "input_selection": {
                "rated_read_ids": rated_read_ids,
                "candidate_filter": (
                    "active_new_or_recommended_accepted_enabled_not_in_any_read_identity_or_"
                    "current_verified_openlibrary_read_work_id"
                ),
                "candidate_count": len(candidate_items),
            },
            "read_history": read_snapshots,
            "read_identity_projection": read_identity,
            # Preserve the exact records passed to rank_candidates separately
            # from the full-history identity exclusion projection.
            "rated_read_inputs": rated_read_snapshots,
            "candidate_inputs": candidate_snapshots,
            "candidate_outputs": outputs,
            "vectors": [
                {key: value for key, value in item.items() if key != "vector_bytes"}
                for item in vector_artifacts
            ],
        }
        raw_json = _canonical_json(evidence)
        if len(raw_json) > MAX_SCORING_JSON_BYTES:
            raise _LimitError("evidence_json_bytes_exceeded")
        evidence["_raw_json"] = raw_json
        evidence["_vector_artifacts"] = vector_artifacts
        evidence["_candidate_descriptors"] = [
            {"candidate_id": candidate_id, **candidate_descriptors[candidate_id]}
            for candidate_id in candidate_ids
        ]
        evidence["_vector_bytes"] = vector_bytes_total
        return evidence
    except _LimitError as exc:
        return _incomplete(
            exc.reason,
            captured_at=captured_at,
            read_count=read_count,
            candidate_count=candidate_count,
            vector_count=vector_count,
        )
    except Exception:
        LOGGER.warning("Scoring batch evidence capture failed")
        return _incomplete(
            "capture_failed",
            captured_at=captured_at,
            read_count=read_count,
            candidate_count=candidate_count,
            vector_count=vector_count,
        )


def _bounded_list(values, limit: int, reason: str) -> list[Any]:
    result = []
    for value in values:
        if len(result) >= limit:
            raise _LimitError(reason)
        result.append(value)
    return result


def _insert_incomplete(connection, *, batch_id: str, evidence: Mapping[str, Any], reason: str) -> str:
    bounded_reason = str(reason)[:80]
    captured_at = str(evidence.get("captured_at") or _utc_now())
    marker_bytes = 192 + len(bounded_reason.encode("utf-8")) + len(captured_at.encode("utf-8"))
    budget = connection.execute(
        "SELECT used_bytes FROM recommendation_evidence_budget WHERE id=1"
    ).fetchone()
    used_bytes = int(budget["used_bytes"] if budget else 0)
    if used_bytes + marker_bytes > MAX_STORED_EVIDENCE_BYTES:
        LOGGER.warning("Scoring batch evidence marker skipped because the shared evidence budget is full")
        return "unavailable"
    connection.execute(
        "INSERT INTO scoring_batches(" 
        "id,schema_version,capture_status,reason,captured_at,read_count,candidate_count,vector_count) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (
            batch_id,
            SCORING_BATCH_SCHEMA_VERSION,
            "incomplete",
            bounded_reason,
            captured_at,
            max(0, int(evidence.get("read_count") or 0)),
            max(0, int(evidence.get("candidate_count") or 0)),
            max(0, int(evidence.get("vector_count") or 0)),
        ),
    )
    return "incomplete"


def persist_scoring_batch(connection, *, batch_id: str, evidence: Mapping[str, Any]) -> str:
    """Persist a complete batch or an explicit small incomplete marker."""

    if evidence.get("capture_status") != "complete":
        return _insert_incomplete(
            connection,
            batch_id=batch_id,
            evidence=evidence,
            reason=str(evidence.get("reason") or "capture_unavailable"),
        )
    artifacts = evidence.get("_vector_artifacts")
    candidates = evidence.get("_candidate_descriptors")
    raw_json = evidence.get("_raw_json")
    if not isinstance(artifacts, list) or not isinstance(candidates, list) or not isinstance(raw_json, bytes):
        return _insert_incomplete(connection, batch_id=batch_id, evidence=evidence, reason="invalid_batch_manifest")

    packed = zlib.compress(raw_json, level=1)
    new_vector_bytes = 0
    counted_new_hashes: set[str] = set()
    vector_records = []
    try:
        for descriptor in artifacts:
            raw = descriptor.get("vector_bytes")
            dimensions = int(descriptor.get("dimensions") or 0)
            if (
                not isinstance(raw, bytes)
                or not raw
                or dimensions <= 0
                or dimensions > MAX_SCORING_VECTOR_DIMENSIONS
                or len(raw) != dimensions * 4
                or descriptor.get("encoding") != "float32-le"
            ):
                raise _LimitError("invalid_vector_artifact")
            backend = _safe_identifier(descriptor.get("backend"))
            model = _safe_identifier(descriptor.get("model"))
            entity_type = str(descriptor.get("entity_type") or "")
            entity_id = int(descriptor.get("entity_id") or 0)
            if entity_type not in {"read", "candidate"} or entity_id <= 0:
                raise _LimitError("invalid_vector_artifact")
            spec = _canonical_json(
                {"backend": backend, "model": model, "dimensions": dimensions, "encoding": "float32-le"}
            )
            artifact_hash = hashlib.sha256(spec + b"\0" + raw).hexdigest()
            if (
                descriptor.get("artifact_hash") != artifact_hash
                or descriptor.get("vector_sha256") != hashlib.sha256(raw).hexdigest()
            ):
                raise _LimitError("invalid_vector_checksum")
            exists = connection.execute(
                "SELECT 1 FROM recommendation_evidence_vectors WHERE artifact_hash=?",
                (artifact_hash,),
            ).fetchone()
            if exists is None and artifact_hash not in counted_new_hashes:
                new_vector_bytes += len(raw)
                counted_new_hashes.add(artifact_hash)
            vector_records.append((entity_type, entity_id, backend, model, dimensions, raw, artifact_hash))

        candidate_records = []
        for item in candidates:
            if not isinstance(item, Mapping):
                raise _LimitError("invalid_candidate_manifest")
            candidate_id = int(item.get("candidate_id") or 0)
            score = float(item.get("score"))
            identity_hash = str(item.get("identity_hash") or "")
            input_hash = str(item.get("input_content_hash") or "")
            if (
                candidate_id <= 0
                or not math.isfinite(score)
                or not re.fullmatch(r"[0-9a-f]{64}", identity_hash)
                or not re.fullmatch(r"[0-9a-f]{64}", input_hash)
            ):
                raise _LimitError("invalid_candidate_manifest")
            candidate_records.append((candidate_id, score, identity_hash, input_hash))
        budget_row = connection.execute(
            "SELECT used_bytes FROM recommendation_evidence_budget WHERE id=1"
        ).fetchone()
        used_bytes = int(budget_row["used_bytes"] if budget_row else 0)
        reference_bytes = 96 * len(vector_records) + sum(
            96 + len(identity_hash) + len(input_hash)
            for _, _, identity_hash, input_hash in candidate_records
        )
        history_ids = [int(item["id"]) for item in evidence.get("read_history", [])]
        rated_input_ids = [
            int(item["id"]) for item in evidence.get("rated_read_inputs", [])
        ]
        read_ids = sorted(set(history_ids) | set(rated_input_ids))
        history_count = len(history_ids)
        if (
            history_count != int(evidence.get("read_count", history_count))
            or len(set(history_ids)) != len(history_ids)
            or len(set(rated_input_ids)) != len(rated_input_ids)
            or any(read_id <= 0 for read_id in read_ids)
        ):
            raise _LimitError("invalid_read_manifest")
        read_reference_bytes = 64 * len(read_ids)
        captured_at = str(evidence.get("captured_at") or _utc_now())
        header_bytes = 192 + len(captured_at.encode("utf-8"))
        if (
            len(raw_json) > MAX_SCORING_JSON_BYTES
            or len(packed) > MAX_SCORING_JSON_BYTES
            or used_bytes + header_bytes + len(packed) + new_vector_bytes
                + reference_bytes + read_reference_bytes > MAX_STORED_EVIDENCE_BYTES
        ):
            raise _LimitError("storage_quota_exceeded")
    except _LimitError as exc:
        return _insert_incomplete(connection, batch_id=batch_id, evidence=evidence, reason=exc.reason)
    except Exception:
        LOGGER.warning("Scoring batch evidence validation failed")
        return _insert_incomplete(connection, batch_id=batch_id, evidence=evidence, reason="validation_failed")

    payload_sha256 = hashlib.sha256(raw_json).hexdigest()
    connection.execute(
        "INSERT INTO scoring_batches(" 
        "id,schema_version,capture_status,reason,captured_at,payload_encoding,payload,payload_sha256," 
        "payload_bytes,read_count,candidate_count,vector_count) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            batch_id,
            SCORING_BATCH_SCHEMA_VERSION,
            "complete",
            "",
            str(evidence["captured_at"]),
            "zlib-json",
            packed,
            payload_sha256,
            len(raw_json),
            len(evidence["read_history"]),
            len(evidence["candidate_outputs"]),
            len(vector_records),
        ),
    )
    for entity_type, entity_id, backend, model, dimensions, raw, artifact_hash in vector_records:
        connection.execute(
            "INSERT OR IGNORE INTO recommendation_evidence_vectors(" 
            "artifact_hash,backend,model,dimensions,encoding,vector) VALUES(?,?,?,?,?,?)",
            (artifact_hash, backend, model, dimensions, "float32-le", raw),
        )
        stored = connection.execute(
            "SELECT vector FROM recommendation_evidence_vectors WHERE artifact_hash=?",
            (artifact_hash,),
        ).fetchone()
        if stored is None or bytes(stored["vector"]) != raw:
            raise ValueError("Scoring vector artifact hash collision")
        connection.execute(
            "INSERT INTO scoring_batch_vectors(batch_id,entity_type,entity_id,artifact_hash) "
            "VALUES(?,?,?,?)",
            (batch_id, entity_type, entity_id, artifact_hash),
        )
    for candidate_id, score, identity_hash, input_hash in candidate_records:
        connection.execute(
            "INSERT INTO scoring_batch_candidates(batch_id,candidate_id,score,identity_hash,input_content_hash) "
            "VALUES(?,?,?,?,?)",
            (batch_id, candidate_id, score, identity_hash, input_hash),
        )
    for read_id in read_ids:
        connection.execute(
            "INSERT INTO scoring_batch_reads(batch_id,read_id) VALUES(?,?)",
            (batch_id, read_id),
        )
    return "complete"


def load_scoring_batch(connection, batch_id: str) -> dict[str, Any] | None:
    """Load and verify a scoring replay artifact and all referenced vectors."""

    row = connection.execute(
        "SELECT schema_version,capture_status,reason,captured_at,payload_encoding,payload," 
        "payload_sha256,payload_bytes,read_count,candidate_count,vector_count "
        "FROM scoring_batches WHERE id=?",
        (batch_id,),
    ).fetchone()
    if row is None:
        return None
    result = {
        "schema_version": int(row["schema_version"]),
        "capture_status": str(row["capture_status"]),
        "reason": str(row["reason"] or ""),
        "captured_at": str(row["captured_at"]),
    }
    if row["capture_status"] != "complete":
        return result
    if row["payload_encoding"] != "zlib-json" or row["payload"] is None:
        raise ValueError("Unsupported scoring batch encoding")
    try:
        raw_json = zlib.decompress(bytes(row["payload"]))
        if len(raw_json) != int(row["payload_bytes"]):
            raise ValueError("Scoring batch size does not match")
        if hashlib.sha256(raw_json).hexdigest() != str(row["payload_sha256"]):
            raise ValueError("Scoring batch checksum does not match")
        payload = json.loads(raw_json)
    except (zlib.error, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Scoring batch payload is unreadable") from exc
    if not isinstance(payload, dict) or int(payload.get("schema_version", 0)) != SCORING_BATCH_SCHEMA_VERSION:
        raise ValueError("Unsupported scoring batch schema")
    vectors = []
    for descriptor in payload.get("vectors", []):
        if not isinstance(descriptor, dict):
            raise ValueError("Scoring batch vector manifest is invalid")
        linked = connection.execute(
            "SELECT v.backend,v.model,v.dimensions,v.encoding,v.vector "
            "FROM scoring_batch_vectors r JOIN recommendation_evidence_vectors v "
            "ON v.artifact_hash=r.artifact_hash WHERE r.batch_id=? AND r.entity_type=? "
            "AND r.entity_id=? AND r.artifact_hash=?",
            (
                batch_id,
                str(descriptor.get("entity_type") or ""),
                int(descriptor.get("entity_id") or 0),
                str(descriptor.get("artifact_hash") or ""),
            ),
        ).fetchone()
        if linked is None:
            raise ValueError("Scoring batch vector artifact is missing")
        raw = bytes(linked["vector"])
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
            linked["encoding"] != "float32-le"
            or len(raw) != dimensions * 4
            or hashlib.sha256(spec + b"\0" + raw).hexdigest() != descriptor.get("artifact_hash")
            or hashlib.sha256(raw).hexdigest() != descriptor.get("vector_sha256")
        ):
            raise ValueError("Scoring batch vector checksum does not match")
        vectors.append({**descriptor, "vector_bytes": raw})
    if len(vectors) != int(row["vector_count"]):
        raise ValueError("Scoring batch vector count does not match")
    candidates = [
        dict(item)
        for item in connection.execute(
            "SELECT candidate_id,score,identity_hash,input_content_hash "
            "FROM scoring_batch_candidates WHERE batch_id=? ORDER BY rowid",
            (batch_id,),
        ).fetchall()
    ]
    if len(candidates) != int(row["candidate_count"]):
        raise ValueError("Scoring batch candidate count does not match")
    outputs = payload.get("candidate_outputs")
    if not isinstance(outputs, list) or len(outputs) != len(candidates):
        raise ValueError("Scoring batch candidate output count does not match")
    expected = {
        int(item["candidate_id"]): float(item["score"])
        for item in outputs
        if isinstance(item, dict) and "candidate_id" in item and "score" in item
    }
    actual = {
        int(item["candidate_id"]): float(item["score"])
        for item in candidates
    }
    if len(expected) != len(outputs) or expected != actual:
        raise ValueError("Scoring batch candidate outputs do not match immutable links")
    input_by_id = {
        int(item["id"]): item
        for item in payload.get("candidate_inputs", [])
        if isinstance(item, dict) and "id" in item
    }
    if len(input_by_id) != int(row["candidate_count"]) or set(input_by_id) != set(expected):
        raise ValueError("Scoring batch candidate inputs do not match outputs")
    for link in candidates:
        candidate_id = int(link["candidate_id"])
        item = input_by_id[candidate_id]
        if (
            str(link["identity_hash"])
            != _identity_hash(item.get("title"), item.get("author"))
            or str(link["input_content_hash"])
            != content_hash(document(dict(item)))
        ):
            raise ValueError("Scoring batch candidate input hash does not match")
    if len(payload.get("read_history", [])) != int(row["read_count"]):
        raise ValueError("Scoring batch read count does not match")
    linked_reads = {
        int(item["read_id"])
        for item in connection.execute(
            "SELECT read_id FROM scoring_batch_reads WHERE batch_id=?", (batch_id,)
        ).fetchall()
    }
    try:
        history_ids = [int(item["id"]) for item in payload.get("read_history", [])]
        rated_input_ids = [int(item["id"]) for item in payload.get("rated_read_inputs", [])]
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ValueError("Scoring batch read manifest is invalid") from exc
    if (
        len(history_ids) != int(row["read_count"])
        or len(set(history_ids)) != len(history_ids)
        or len(set(rated_input_ids)) != len(rated_input_ids)
        or any(read_id <= 0 for read_id in (*history_ids, *rated_input_ids))
    ):
        raise ValueError("Scoring batch read manifest is invalid")
    payload_reads = set(history_ids) | set(rated_input_ids)
    if linked_reads != payload_reads:
        raise ValueError("Scoring batch read links do not match payload")
    payload["vectors"] = vectors
    result["decision"] = payload
    result["candidate_links"] = candidates
    return result
