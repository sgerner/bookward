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
4. Trial alternate rankers only as bounded offline studies over verified
   summaries and retrieved positive/negative examples. Keep the existing
   scorer as the fallback, and compare held-out ranking, latency, and
   reproducibility before shipping a change.

Scores are ranking heuristics, not calibrated probabilities of liking a
book. Do not raise scores simply to clear a digest threshold.

## Neighborhood scoring evaluation

The second change retains the current embedding provider and negative
nearest-neighbor penalty. It averages the five nearest positive similarities,
adds a small rating adjustment from the five nearest rated books (including
three-star reads), and replaces the binary author bonus with the author's
average rating relative to the reader's overall mean. Five prior observations
at the overall mean temper authors with little history. Duplicate read
identities count once. Explanations identify actual neighboring titles rather
than asserting a thematic connection from title-only read documents.

The formula was selected from four simple alternatives on the validation
period. Its weights were then frozen. The committed evaluator calls the actual
production ranking function, verifies vector content hashes, removes duplicate
works, parses both date formats, and keeps whole days together at chronological
60/20/20 boundaries. Test profiles include the now-past validation period.

| Metric | Validation: previous → new | Test: previous → new |
| --- | --- | --- |
| AUC, 4–5 stars versus 1–3 stars | 0.578 → 0.640 | 0.591 → 0.629 |
| Highly rated books in top 20 | 7 → 11 | 13 → 17 |
| NDCG@20, binary relevance | 0.505 → 0.662 | 0.622 → 0.896 |

There were 1,766 usable distinct works: 1,059 initial training entries, 353
validation entries, and 354 test entries. The positive rate changed from 24%
in validation to 44% in test, so compare methods within each period. The
descriptive paired book-bootstrap 95% interval for test AUC improvement is
−0.009 to +0.086; it includes zero and is not evidence of a statistically
significant AUC improvement. Books by the same author are also correlated.
These results measure retrospective ranking among books this reader read,
not discovery coverage, new-author generalization, or live acceptance.

A separate diagnostic uses the latest explicit save/pass for each candidate,
only readings from earlier days, and no same-work reading evidence. Of 66
candidates, 47 have current usable vectors (27 saves, 20 passes). AUC improves
from 0.584 to 0.616 and saved books in the top 20 from 12 to 14. This is a small
intent sample with current metadata and no impression log, not an A/B test.
Feedback-neighbor learning was not shipped: the exploratory sample did not
support it. Candidate workflow statuses never supply labels.

Batched matrix scoring also eliminates repeated vector conversion and norm
calculation. A local single-thread run over 1,769 cached read vectors and 167
unread candidate vectors took 8.86 seconds with the old pairwise loop and 0.108
seconds with the new scorer (about 82× faster). This measures in-memory
scoring only; database queries, network time, and embedding generation are
excluded. Newly used three-star reads may require one initial embedding pass.

### Reproduce

Run against a private, consistent SQLite snapshot or a JSON export containing
`reads`, `candidates`, `feedback`, and `embeddings` tables (base64 vectors in
JSON). SQLite input is opened read-only, in a single read transaction. Settings,
credentials, and jobs are never queried. JSON may omit candidate/feedback
tables when only a rating evaluation is needed.

```sh
uv run --project engine python engine/scripts/evaluate_ranking.py /private/snapshot.db \
  --backend ollama --model qwen3-embedding:4b
```

The script performs no network calls or writes and prints aggregate metrics
only. Preserve the original snapshot to compare future changes on identical
data; fresh production snapshots are different evaluation populations.
[Recorded aggregate results](ranking-evaluation.json) contain no book records
or embedding vectors.
