import json
import math
from .database import rows, row, transaction
from .embeddings import get_embedder, cosine, content_hash, vector_blob, blob_vector

def document(item):
    genres = item.get("genres", "[]")
    return f"{item.get('title','')} {item.get('author','')} {genres} {item.get('description','')}"

async def cached_vectors(embedder, entity_type, items, *, force=False, persist=True):
    vectors, missing = [None] * len(items), []
    for index, item in enumerate(items):
        text = document(item); digest = content_hash(text)
        cached = None if force else row("SELECT vector FROM embeddings WHERE entity_type=? AND entity_id=? AND backend=? AND model=? AND content_hash=?", (entity_type,item["id"],embedder.name,embedder.model,digest))
        if cached: vectors[index] = blob_vector(cached["vector"])
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

async def score_all(backend=None, model=None, url=None, api_key=None, embedder=None):
    reads = rows("SELECT * FROM reads WHERE rating IS NOT NULL")
    candidates = rows("SELECT c.*, s.name source_name, s.weight source_weight FROM candidates c JOIN sources s ON s.id=c.source_id WHERE c.status IN ('new','recommended') AND s.enabled=1 AND book_identity(c.title,c.author) NOT IN (SELECT book_identity(title,author) FROM reads)")
    if not candidates: return 0
    positives = [r for r in reads if (r.get("rating") or 0) >= 4]
    negatives = [r for r in reads if 0 < (r.get("rating") or 0) <= 2]
    embedder = embedder or get_embedder(backend, model, url, api_key)
    pos_vectors = await cached_vectors(embedder,"read",positives)
    neg_vectors = await cached_vectors(embedder,"read",negatives)
    candidate_vectors = await cached_vectors(embedder,"candidate",candidates)
    with transaction() as con:
        for candidate, vector in zip(candidates, candidate_vectors):
            best_positive = max((cosine(vector, other) for other in pos_vectors), default=.25)
            best_negative = max((cosine(vector, other) for other in neg_vectors), default=0)
            author_match = any(r["author"].casefold() == candidate["author"].casefold() for r in positives)
            source_weight = float(candidate.get("source_weight") or 1)
            score = max(0, min(100, 42 + best_positive * 48 - best_negative * 24 + (7 if author_match else 0) + (source_weight - 1) * 5))
            explanation = []
            if author_match: explanation.append("An author you have rated highly")
            if best_positive > .45: explanation.append("Strong thematic similarity to books you loved")
            elif best_positive > .25: explanation.append("Moderate similarity to your positive reading history")
            if best_negative > .5: explanation.append("Reduced for similarity to books you disliked")
            if candidate.get("source_name"): explanation.append(f"From {candidate['source_name']}")
            con.execute("UPDATE candidates SET score=?, explanation=?, status=CASE WHEN status='new' THEN 'recommended' ELSE status END, updated_at=CURRENT_TIMESTAMP WHERE id=?", (round(score, 1), json.dumps(explanation), candidate["id"]))
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
