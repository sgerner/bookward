import json
import math

import numpy as np

from .database import rows, transaction
from .embeddings import get_embedder, content_hash, vector_blob, blob_vector
from .ranking import rank_candidates
SCORING_BATCH_SIZE = 256

def document(item):
    genres = item.get("genres", "[]")
    return f"{item.get('title','')} {item.get('author','')} {genres} {item.get('description','')}"

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
    candidates = rows("SELECT c.*, s.name source_name, s.weight source_weight FROM candidates c JOIN sources s ON s.id=c.source_id WHERE c.status IN ('new','recommended') AND s.enabled=1 AND book_identity(c.title,c.author) NOT IN (SELECT book_identity(title,author) FROM reads)")
    if not candidates: return 0
    embedder = embedder or get_embedder(backend, model, url, api_key)
    read_vectors = await cached_vectors(embedder,"read",reads)
    candidate_vectors = await cached_vectors(embedder,"candidate",candidates)
    ranked = rank_candidates(reads, read_vectors, candidates, candidate_vectors)
    with transaction() as con:
        for candidate in ranked:
            con.execute("UPDATE candidates SET score=?, explanation=?, status=CASE WHEN status='new' THEN 'recommended' ELSE status END, updated_at=CURRENT_TIMESTAMP WHERE id=?", (candidate["score"], json.dumps(candidate["explanation"]), candidate["id"]))
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
