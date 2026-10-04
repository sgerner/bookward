import json
import logging
import uuid
from datetime import datetime, timezone

import numpy as np

from .database import rows, transaction, connect
from .embeddings import get_embedder, content_hash, vector_blob
from .ranking import rank_candidates
from .identity import book_identity_match_index, book_row_identity_match_keys
from .subjects import normalize_subjects
SCORING_BATCH_SIZE = 256
LOGGER = logging.getLogger(__name__)

def document(item):
    # Read embeddings already exist in production with this exact representation.
    # Keep it stable so adding cleaner candidate subjects does not invalidate the
    # cache for every historical read.
    if "rating" in item:
        return (
            f"{item.get('title', '')} {item.get('author', '')} "
            f"{item.get('genres') or '[]'} {item.get('description', '')}"
        )
    genres = normalize_subjects(item.get("genres", "[]"), limit=8)
    subjects = "; ".join(genres)
    return " ".join(
        str(value).strip()
        for value in (
            item.get("title", ""),
            item.get("author", ""),
            item.get("description", ""),
            f"Subjects: {subjects}" if subjects else "",
        )
        if str(value or "").strip()
    )

async def cached_vectors(
    embedder,
    entity_type,
    items,
    *,
    force=False,
    persist=True,
    expected_dimensions=None,
):
    if expected_dimensions is not None and (
        isinstance(expected_dimensions, bool)
        or not isinstance(expected_dimensions, int)
        or expected_dimensions <= 0
    ):
        raise ValueError("Expected embedding dimensions must be a positive integer")
    vectors, missing = [None] * len(items), []

    cached_by_id = {}
    entity_ids = [item["id"] for item in items]
    # Keep the lookup bounded for large Goodreads imports while replacing the
    # per-item connection/query loop with a small number of batch reads.
    for start in range(0, len(entity_ids), 500):
        batch_ids = entity_ids[start:start + 500]
        if not batch_ids:
            continue
        placeholders = ",".join("?" for _ in batch_ids)
        cached_by_id.update(
            {
                int(cached["entity_id"]): cached
                for cached in rows(
                    f"SELECT entity_id,vector,dimensions,content_hash FROM embeddings "
                    f"WHERE entity_type=? AND backend=? AND model=? "
                    f"AND entity_id IN ({placeholders})",
                    (entity_type, embedder.name, embedder.model, *batch_ids),
                )
            }
        )

    cached_dimensions = set()
    for index, item in enumerate(items):
        text = document(item); digest = content_hash(text)
        cached = None if force else cached_by_id.get(item["id"])
        if cached and cached["content_hash"] == digest:
            vector = _cached_vector_values(cached)
            if vector is not None:
                if expected_dimensions is not None and len(vector) != expected_dimensions:
                    raise ValueError(
                        "Cached embedding dimensions differ from expected dimensions; "
                        "rebuild the affected cache"
                    )
                vectors[index] = vector
                cached_dimensions.add(len(vector))
                continue
        missing.append((index,item,text,digest))
    if len(cached_dimensions) > 1:
        raise ValueError("Cached embedding dimensions are inconsistent; rebuild the affected cache")
    if missing:
        generated = []
        for start in range(0, len(missing), 64):
            generated.extend(await embedder.embed([entry[2] for entry in missing[start:start + 64]]))
        if len(generated) != len(missing): raise ValueError("Embedding provider returned the wrong number of vectors")
        generated, dimensions = _provider_vector_values(generated)
        if not dimensions:
            raise ValueError("Embedding provider returned invalid or inconsistent vectors")
        if expected_dimensions is not None and dimensions != expected_dimensions:
            raise ValueError(
                "Embedding provider dimensions differ from expected dimensions; "
                "rebuild the affected cache"
            )
        if cached_dimensions and dimensions not in cached_dimensions:
            raise ValueError("Embedding provider dimensions differ from valid cached vectors; rebuild the full cache")
        if persist:
            with transaction() as con:
                for (index,item,_text,digest), vector in zip(missing,generated):
                    vectors[index] = vector
                    con.execute("INSERT INTO embeddings(entity_type,entity_id,backend,model,vector,dimensions,content_hash) VALUES(?,?,?,?,?,?,?) ON CONFLICT(entity_type,entity_id,backend,model) DO UPDATE SET vector=excluded.vector,dimensions=excluded.dimensions,content_hash=excluded.content_hash,updated_at=CURRENT_TIMESTAMP",(entity_type,item["id"],embedder.name,embedder.model,vector_blob(vector),len(vector),digest))
        else:
            for (index,_item,_text,_digest), vector in zip(missing,generated):
                vectors[index] = vector
    return vectors


def _vector_dimensions(vectors):
    """Return the established dimension for a validated vector set, if any."""
    if not vectors:
        return None
    dimensions = {len(vector) for vector in vectors}
    if len(dimensions) != 1 or next(iter(dimensions)) <= 0:
        raise ValueError("Embedding dimensions are inconsistent; rebuild the affected cache")
    return next(iter(dimensions))


def _cached_vector_values(cached):
    """Decode a cached float32 vector only when its stored shape is trustworthy."""
    try:
        raw = bytes(cached["vector"])
        dimensions = cached["dimensions"]
        if isinstance(dimensions, bool) or not isinstance(dimensions, int):
            return None
        if dimensions <= 0 or not raw or len(raw) % np.dtype(np.float32).itemsize:
            return None
        vector = np.frombuffer(raw, dtype=np.float32)
        if len(vector) != dimensions or not np.isfinite(vector).all():
            return None
        # Detach from SQLite's buffer and preserve intentional zero vectors.
        return vector.copy().tolist()
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _provider_vector_values(vectors):
    """Validate and canonicalize provider output before any vector is persisted."""
    validated = []
    dimensions = None
    for vector in vectors:
        try:
            with np.errstate(over="ignore", invalid="ignore"):
                array = np.asarray(vector, dtype=np.float32)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Embedding provider returned invalid vectors") from exc
        if array.ndim != 1 or not len(array) or not np.isfinite(array).all():
            raise ValueError("Embedding provider returned invalid or inconsistent vectors")
        if dimensions is None:
            dimensions = len(array)
        elif len(array) != dimensions:
            raise ValueError("Embedding provider returned invalid or inconsistent vectors")
        validated.append(array.tolist())
    return validated, dimensions


def _normalized_vectors(vectors):
    if not vectors:
        return np.empty((0, 0), dtype=np.float32)

    matrix = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    return np.divide(matrix, norms, out=np.zeros_like(matrix), where=norms != 0)


def _max_cosine_similarities(vectors, references, default):
    if not vectors:
        return np.empty(0, dtype=np.float32)
    if not references:
        return np.full(len(vectors), default, dtype=np.float32)

    normalized_vectors = _normalized_vectors(vectors)
    normalized_references = _normalized_vectors(references)
    best = np.empty(len(vectors), dtype=np.float32)
    reference_transpose = normalized_references.T

    for start in range(0, len(vectors), SCORING_BATCH_SIZE):
        end = start + SCORING_BATCH_SIZE
        similarities = normalized_vectors[start:end] @ reference_transpose
        best[start:end] = similarities.max(axis=1)

    return best

async def score_all(backend=None, model=None, url=None, api_key=None, embedder=None):
    reads = rows("SELECT * FROM reads WHERE rating BETWEEN 1 AND 5 ORDER BY id")
    read_history = rows("SELECT * FROM reads ORDER BY id")
    read_metadata_rows = rows(
        "SELECT read_id,verified_work_id,identity_provider,identity_provider_id,identity_hash "
        "FROM read_metadata WHERE verified_work_id!=''"
    )
    from .ingestion import read_identity_rows_with_current_verified_work_ids

    read_identity_history = read_identity_rows_with_current_verified_work_ids(
        read_history, read_metadata_rows
    )
    all_read_keys = book_identity_match_index(read_identity_history)
    candidates = rows(
        "SELECT c.*, s.name source_name, s.weight source_weight, "
        "q.work_id quality_work_id,q.provider quality_provider,"
        "q.isbn13 quality_isbn13,q.isbn10 quality_isbn10,"
        "CASE WHEN q.quality_score>0 THEN q.quality_score "
        "WHEN q.quality_status='accepted' THEN 0.85 ELSE 0 END AS catalog_confidence "
        "FROM candidates c JOIN sources s ON s.id=c.source_id "
        "JOIN candidate_quality q ON q.candidate_id=c.id "
        "WHERE c.status IN ('new','recommended') AND q.quality_status='accepted' "
        "AND s.enabled=1"
    )
    candidates = [
        item for item in candidates
        if not book_row_identity_match_keys(item) & all_read_keys
    ]
    if not candidates: return 0
    interaction_candidates = rows(
        """SELECT DISTINCT c.* FROM candidates c WHERE c.id IN (
            SELECT candidate_id FROM (
                SELECT candidate_id,occurred_at FROM recommendation_events
                WHERE event_type IN ('save','reject','restore','read','librarr_import')
                UNION ALL
                SELECT f.candidate_id,f.created_at FROM feedback f
                WHERE f.action IN ('save','reject','restore')
                  AND NOT EXISTS (
                    SELECT 1 FROM recommendation_events e
                    WHERE e.event_key='feedback:' || f.id
                  )
            ) ORDER BY datetime(occurred_at) DESC LIMIT 5000
        ) ORDER BY c.id"""
    )
    candidate_items_by_id = {int(item["id"]): item for item in candidates}
    for item in interaction_candidates:
        candidate_items_by_id.setdefault(int(item["id"]), item)
    candidate_items = list(candidate_items_by_id.values())
    from .publication_year_metadata import attach_publication_years
    with connect() as con:
        attach_publication_years(con, reads, candidates)
    embedder = embedder or get_embedder(backend, model, url, api_key)
    read_vectors = await cached_vectors(embedder,"read",reads)
    all_candidate_vectors = await cached_vectors(
        embedder,
        "candidate",
        candidate_items,
        expected_dimensions=_vector_dimensions(read_vectors),
    )
    vectors_by_id = {
        int(item["id"]): vector
        for item, vector in zip(candidate_items, all_candidate_vectors)
    }
    candidate_vectors = [vectors_by_id[int(item["id"])] for item in candidates]
    ranked = rank_candidates(reads, read_vectors, candidates, candidate_vectors)
    candidate_inputs_by_id = {int(item["id"]): item for item in candidates}
    batch_id = uuid.uuid4().hex
    captured_at = datetime.now(timezone.utc).isoformat()
    with transaction() as con:
        written = []
        for candidate in ranked:
            cursor = con.execute(
                "UPDATE candidates SET score=?, explanation=?, score_batch_id=NULL, "
                "status=CASE WHEN status='new' THEN 'recommended' ELSE status END, "
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (candidate["score"], json.dumps(candidate["explanation"]), candidate["id"]),
            )
            if cursor.rowcount != 1:
                continue
            con.execute(
                "UPDATE candidate_quality SET metadata_confidence=?,updated_at=CURRENT_TIMESTAMP WHERE candidate_id=?",
                (candidate["metadata_confidence"], candidate["id"]),
            )
            written.append(candidate)
        if written:
            written_ids = {int(item["id"]) for item in written}
            written_inputs = [
                candidate_inputs_by_id[candidate_id]
                for candidate_id in candidate_inputs_by_id
                if candidate_id in written_ids
            ]
            written_vectors = {
                candidate_id: vectors_by_id[candidate_id]
                for candidate_id in written_ids
            }
            try:
                from .scoring_evidence import (
                    build_scoring_batch_evidence,
                    persist_scoring_batch,
                )

                con.execute("SAVEPOINT scoring_batch_evidence")
                evidence = build_scoring_batch_evidence(
                    con,
                    captured_at=captured_at,
                    read_history=read_identity_history,
                    rated_reads=reads,
                    candidates=written_inputs,
                    ranked=written,
                    read_vectors=read_vectors,
                    candidate_vectors=written_vectors,
                    backend=embedder.name,
                    model=embedder.model,
                    scoring_batch_size=SCORING_BATCH_SIZE,
                )
                capture_status = persist_scoring_batch(
                    con, batch_id=batch_id, evidence=evidence
                )
                if capture_status in {"complete", "incomplete"}:
                    marks = ",".join("?" for _ in written_ids)
                    con.execute(
                        f"UPDATE candidates SET score_batch_id=? WHERE id IN ({marks})",
                        (batch_id, *sorted(written_ids)),
                    )
                con.execute("RELEASE SAVEPOINT scoring_batch_evidence")
            except Exception:
                try:
                    con.execute("ROLLBACK TO SAVEPOINT scoring_batch_evidence")
                    con.execute("RELEASE SAVEPOINT scoring_batch_evidence")
                except Exception:
                    pass
                LOGGER.warning("Could not persist base-score evidence; scores were still written")
    return len(candidates)


async def rebuild_all_embeddings(backend=None, model=None, url=None, api_key=None):
    """Generate a fresh embedding set for every stored read and candidate.

    Scoring intentionally embeds only rated reads and active candidates for
    speed. A provider/model change needs a stronger guarantee: every stored
    entity should be ready for the next scoring or discovery run. Existing
    vectors for the selected provider are replaced only after the complete
    replacement set has been generated successfully.
    """
    embedder = get_embedder(backend, model, url, api_key)
    reads = rows("SELECT * FROM reads")
    candidates = rows("SELECT * FROM candidates")
    # Generate off to the side first. A provider failure must leave both the
    # current provider cache and caches for other providers untouched.
    read_vectors = await cached_vectors(embedder, "read", reads, force=True, persist=False)
    candidate_vectors = await cached_vectors(
        embedder,
        "candidate",
        candidates,
        force=True,
        persist=False,
        expected_dimensions=_vector_dimensions(read_vectors),
    )
    with transaction() as con:
        con.execute(
            "DELETE FROM embeddings WHERE backend=? AND model=?",
            (embedder.name, embedder.model),
        )
        for entity_type, items, vectors in (
            ("read", reads, read_vectors),
            ("candidate", candidates, candidate_vectors),
        ):
            for item, vector in zip(items, vectors):
                con.execute(
                    "INSERT INTO embeddings(entity_type,entity_id,backend,model,vector,dimensions,content_hash) VALUES(?,?,?,?,?,?,?)",
                    (
                        entity_type,
                        item["id"],
                        embedder.name,
                        embedder.model,
                        vector_blob(vector),
                        len(vector),
                        content_hash(document(item)),
                    ),
                )
    scored = await score_all(backend, model, url, api_key, embedder=embedder)
    return {
        "embeddings": len(reads) + len(candidates),
        "scored": scored,
        "backend": embedder.name,
        "model": embedder.model,
    }
