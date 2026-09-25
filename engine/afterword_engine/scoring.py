import json
import math

import numpy as np

from .database import rows, transaction
from .embeddings import get_embedder, content_hash, vector_blob, blob_vector
from .ranking import rank_candidates
from .identity import book_identity_match_index, book_row_identity_match_keys
from .subjects import normalize_subjects
SCORING_BATCH_SIZE = 256

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

async def cached_vectors(embedder, entity_type, items, *, force=False, persist=True):
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
                    f"SELECT entity_id,vector,content_hash FROM embeddings "
                    f"WHERE entity_type=? AND backend=? AND model=? "
                    f"AND entity_id IN ({placeholders})",
                    (entity_type, embedder.name, embedder.model, *batch_ids),
                )
            }
        )

    for index, item in enumerate(items):
        text = document(item); digest = content_hash(text)
        cached = None if force else cached_by_id.get(item["id"])
        if cached and cached["content_hash"] == digest:
            vectors[index] = blob_vector(cached["vector"])
        else: missing.append((index,item,text,digest))
    if missing:
        generated = []
        for start in range(0, len(missing), 64):
            generated.extend(await embedder.embed([entry[2] for entry in missing[start:start + 64]]))
        if len(generated) != len(missing): raise ValueError("Embedding provider returned the wrong number of vectors")
        dimensions = len(generated[0]) if generated else 0
        if not dimensions or any(len(vector) != dimensions or not all(math.isfinite(float(value)) for value in vector) for vector in generated):
            raise ValueError("Embedding provider returned invalid or inconsistent vectors")
        if persist:
            with transaction() as con:
                for (index,item,_text,digest), vector in zip(missing,generated):
                    if not vector: raise ValueError("Embedding provider returned an empty vector")
                    vectors[index] = vector
                    con.execute("INSERT INTO embeddings(entity_type,entity_id,backend,model,vector,dimensions,content_hash) VALUES(?,?,?,?,?,?,?) ON CONFLICT(entity_type,entity_id,backend,model) DO UPDATE SET vector=excluded.vector,dimensions=excluded.dimensions,content_hash=excluded.content_hash,updated_at=CURRENT_TIMESTAMP",(entity_type,item["id"],embedder.name,embedder.model,vector_blob(vector),len(vector),digest))
        else:
            for (index,_item,_text,_digest), vector in zip(missing,generated):
                if not vector: raise ValueError("Embedding provider returned an empty vector")
                vectors[index] = vector
    return vectors


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
    all_read_keys = book_identity_match_index(rows("SELECT * FROM reads"))
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
    embedder = embedder or get_embedder(backend, model, url, api_key)
    read_vectors = await cached_vectors(embedder,"read",reads)
    all_candidate_vectors = await cached_vectors(embedder,"candidate",candidate_items)
    vectors_by_id = {
        int(item["id"]): vector
        for item, vector in zip(candidate_items, all_candidate_vectors)
    }
    candidate_vectors = [vectors_by_id[int(item["id"])] for item in candidates]
    ranked = rank_candidates(reads, read_vectors, candidates, candidate_vectors)
    with transaction() as con:
        for candidate in ranked:
            con.execute("UPDATE candidates SET score=?, explanation=?, status=CASE WHEN status='new' THEN 'recommended' ELSE status END, updated_at=CURRENT_TIMESTAMP WHERE id=?", (candidate["score"], json.dumps(candidate["explanation"]), candidate["id"]))
            con.execute(
                "UPDATE candidate_quality SET metadata_confidence=?,updated_at=CURRENT_TIMESTAMP WHERE candidate_id=?",
                (candidate["metadata_confidence"], candidate["id"]),
            )
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
    candidate_vectors = await cached_vectors(embedder, "candidate", candidates, force=True, persist=False)
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
