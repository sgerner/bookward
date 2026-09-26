# Discovery slate diversity

The engine applies a small, deterministic anti-redundancy pass to the first
eight `recommended` books after the existing score and interaction
personalization steps. It runs on the complete eligible pool before pagination,
so repeated requests and page boundaries use the same final order.

The pass considers only local swaps. A book can move at most three positions,
and a replacement must be within five points of the displaced book's score.
This lets a candidate from as low as rank 11 move into the first eight while
keeping the rank movement bounded. The pass tries to avoid a third book by the
same author in the visible slate and pairs whose current hash-valid candidate
vectors have cosine similarity of at least 0.90. If it cannot find a
qualifying alternative, it leaves the book in place. Scores and explanations
remain those produced by the existing ranker; only the item order changes.

The author limit and similarity threshold are soft selection rules, not
eligibility filters. They do not remove an author or a subject from the
candidate pool, and a reader with a narrow or series-focused catalog may still
see repeats when no close-scoring local alternative exists. Similarity uses
only vectors already cached for the current candidate text; serving never
calls an embedding provider. Missing, stale, malformed, or dimension-mismatched
vectors simply cannot trigger a similarity swap.

Recommendation-run metadata includes the `discovery-slate-v1` policy version,
the active bounds, and aggregate swap counts. It does not include author names,
titles, vectors, or interaction text. The recommendation run's policy version
also changed to `rating-kernel-recency-interaction-installation-slate-v1` so
outcomes can be separated from earlier ranking behavior.

This is a conservative serving heuristic, not evidence that readers prefer a
more varied list. Evaluate saves, passes, attributed ratings, and later
reading outcomes by policy version before considering a broader MMR or other
slate optimizer.

Engine checks cover author-repeat swaps, near-duplicate swaps, score and
displacement bounds, deterministic pagination, and aggregate run metadata.
