# Recommendation quality study

## Production snapshot

Read-only snapshot reviewed on 2026-09-20: 1,834 read entries, 2,283
candidates, and cached `qwen3-embedding:4b` vectors. Personal records and
vectors stay outside this repository.

The current discovery pool contained 203 recommended books from enabled
sources. Applying the engine's conservative title/author identity found 36
already-read candidates, including all ten of the first ten and 19 of the
first twenty results in score order. Recognized audiobook and numbered
series suffixes account for many of these matches. Excluding these books
leaves 167 candidates. This measures removal of known repeats, not a gain in
predicted enjoyment or a guarantee that every remaining edition is novel.

Discovery, scoring, and digest selection now exclude known reads, including
unrated reads, before applying result limits. Saved and imported records
remain accessible. This is a query-time rule: a newly imported reading entry
takes effect without waiting for a scoring job, and no candidate feedback or
status is rewritten.

## Evidence constraints

- Ratings are concentrated around three stars: 781 of 1,784 rated entries.
  The original scorer ignores those ratings, retaining 649 positive and 354
  negative entries.
- The 132 feedback events represent only 66 distinct candidates: 40 saves and
  26 passes, with duplicate events. The 2,040 rejected candidate statuses are
  workflow state; 1,988 have no explicit rejection event. They must not become
  negative training labels.
- Read documents contain titles and authors but no descriptions or genres.
  A strong embedding model cannot recover reliable, auditable thematic
  evidence from missing catalog metadata.
- Of the 203 discovery candidates, nine lack descriptions. Across the whole
  retained catalog, 1,354 of 2,283 lack descriptions. Archived candidates and
  the currently visible pool have substantially different metadata coverage.
- Read dates mix ISO dates and RFC-style RSS timestamps. Evaluations must
  parse both, deduplicate works before splitting, and keep future ratings out
  of training.
- All 1,774 cached read vectors match current documents. Candidate caches
  include 166 stale document hashes and nine orphan entries; an offline
  evaluation must exclude these or regenerate them.

## Next experiments

1. Enrich read history with verified work identifiers, descriptions, subjects,
   and series information. There are 1,593 populated read ISBNs. Resolve an
   edition to its work, verify author/title agreement, cache provenance, and
   compare enriched and title-only documents on the same held-out books.
   [Open Library's APIs](https://openlibrary.org/developers/api) expose work,
   edition, and subject records; this is a better starting point than
   generating book facts from memory.
2. Separate candidate discovery from ranking. Expand from liked authors and
   subjects, then measure coverage and first-unread-series availability.
   Improve coverage before making an expensive reranker responsible for a
   small or repetitive candidate pool.
3. Evaluate explicit feedback only after deduplicating events and separating
   save intent from an actual high reading rating. Sixty-six labeled
   candidates are too few to justify a large learned model on their own.
4. Trial an optional LLM reranker over a bounded shortlist with verified
   summaries and retrieved positive/negative examples. Keep an offline
   fallback, cache results by input hash, and compare blinded judgments,
   held-out ranking, latency, and cost. Ship it only if it beats the cheaper
   scorer; generated explanations alone are not evidence of better ranking.

Scores are ranking heuristics, not calibrated probabilities of liking a
book. Do not raise scores simply to clear a digest threshold.
