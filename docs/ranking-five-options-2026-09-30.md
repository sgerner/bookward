# Five ranking hypotheses: September 30, 2026

Narrow candidates from five hypothesis families were tested against the private
production snapshot. Actual read enrichment initially received only a coverage
check, not a scored replay. The [methodology audit](ranking-methodology-audit-2026-09-30.md)
adds that missing comparison and corrects the daily ordinal diagnostic. None yet
supports replacing the serving score formula; several ideas remain unresolved. This review distinguishes rating replay, action diagnostics, and
discovery proxies; a gain in one is not evidence of a gain in the others.
The [aggregate report](ranking-five-options-2026-09-30.json) records the text,
source-confidence, and seed-proxy measurements.

The corpus has 1,777 distinct dated rated works and 1,677 eligible chronological
queries after establishing 100 reads of history. Validation and the later period
contain 355 and 356 books. The later period has been inspected in previous work
and is exploratory rather than independent confirmation. Reads from a query's
calendar day are always hidden.

| Hypothesis | Test | Result |
| --- | --- | --- |
| Learned ordinal preferences | Small regularized five-level model, frozen refit and daily expanding update | Frozen models regress; daily updates improve high AUC but weaken low AUC. |
| Compatible and multiple text views | Title/author, title-only, and equal score fusion; paired rich/compatible candidate text | Validation selects title-only, but later low-rating AUC drops sharply. Action validation keeps the existing rich view. |
| Separate catalog/source trust from enjoyment | Four score arms, rated replay, latest actions, exact same-run and visible actions | Rated reads lack catalog/source evidence. Pooled action gains reverse on the sparse exposed cohorts. |
| Multiple interest neighborhoods | Three or five frozen-prefix clusters with causal rating evidence | Validation selects three; later high- and low-rating AUC both regress. |
| Discovery seed coverage | Equal budgets of four and 25 seeds, oldest/current versus recent and mixed policies | Recent seeds improve the overall 25-seed similarity proxy, but not unseen-author books; provider outcomes remain a separate test. |

See the [ordinal and interest study](ranking-ordinal-interests-2026-09-30.md) for
the first and fourth experiments and the public reproduction command.

## Text views

The production read document contains title, author, empty subjects, and an
empty description. New compatible candidate embeddings use exactly that
template. A true title-only view is encoded separately with the same local
`qwen3-embedding:4b` model and 2,560 dimensions. Three text choices and a
separate author-term sensitivity are compared on validation; later comparisons
use its selected configuration.

Title-only text with the existing author term improves validation high/low AUC
from 0.5240/0.5917 to 0.6171/0.6098. On the later 356 books, high AUC moves from
0.6021 to 0.6073, but low AUC falls from 0.5429 to 0.4375. The regression also
appears among unseen authors, so the view is not promoted.

A repeatability check re-encoded three unchanged read documents and found
cosines of 0.9967–0.9981 against their cached vectors. The model digest matches
on both hosts, but the current local and production Ollama versions differ
(0.30.5 and 0.35.0); the cache's original execution version is unknown. The
cached-versus-new comparison therefore includes execution variation as well as
the text change. An additional diagnostic re-encodes all 1,797 read documents
with the same local encoder and verifies every document hash. The fresh
title/author baseline yields later high/low AUC of 0.5980/0.5422. Title-only
still yields 0.6073/0.4375, while equal score fusion yields 0.6130/0.4792.
Validation still selects title-only, and its low-rating regression persists
against the fresh control. These fixed-view diagnostic results do not justify
promoting either new view.

The latest save/reject diagnostic has 127 cases, split by whole calendar days
into 76/33/18. Validation keeps the existing rich candidate representation;
its later AUC is 0.7222, with only three saves and 15 rejects. That tiny,
reader-selected sample cannot establish a live ranking benefit.

Existing catalog records can enrich 115 unambiguous read identities, of which
114 remain in the rated replay. A discovery rejection for `read_overlap` does
not invalidate otherwise usable book text. Even after restoring such matches,
later enrichment covers only 19 of 153 high-rated books, two of 54 low-rated
books, and 18 of 149 neutral books. Metadata coverage is too uneven for a global
enriched-text preference claim. This was a coverage check only: the generated
enriched vectors were not scored in the original experiment. The subsequent
paired audit finds a high-rating signal with sparse low-rating support; it does
not establish that enrichment failed.

## Source and catalog confidence

The fixed ablation turns source contribution and confidence shrinkage off
separately and together. Identity acceptance safeguards remain intact. All four
score arms match direct calls to the serving ranker on all 127 feedback cases.
Historical rated reads have no source or catalog ledger, so these arms are
identical in rated replay; this is a feature-coverage limit, not validation of
source-neutral ranking.

| Diagnostic | Current | Source term off | Confidence shrinkage off | Both off |
| --- | ---: | ---: | ---: | ---: |
| Latest actions, read-date history: 57 saves, 70 rejects | 0.4060 | 0.4380 | 0.4835 | 0.5708 |
| Latest actions, known row-availability history | 0.3436 | 0.3897 | 0.3419 | 0.4064 |
| Exact same-run actions: three saves, 44 rejects | 0.777 | 0.705 | 0.708 | 0.644 |
| Visible same-run actions: two saves, 15 rejects | 0.833 | 0.767 | 0.617 | 0.533 |

Values are save-versus-reject AUC. Exact same-run logged serving scores have
AUC 0.773, and the visible subset 0.800. Replayed current metadata is not
necessarily the metadata available at each action. These diagnostics omit
parts of the live interaction and slate policy and do not support a causal
policy comparison. Every propensity is one, so inverse-propensity weighting
cannot repair the missing counterfactual exposure.

Removing catalog shrinkage looks useful in the pooled action diagnostic but
hurts the tiny exposed cohorts. Keep source and confidence behavior unchanged.

## History availability correction

Reading completion dates and database import times answer different questions.
An old read imported after an action belongs in a retrospective taste history,
but was not a row the engine could use for that action. The availability audit
finds 66 of 127 action cases have no recorded history rows available then.
Current-score action AUC changes from 0.4060 to 0.3436 when the conservative
row-availability gate is applied.

The feedback evaluator therefore preserves the original read-date diagnostic
and adds a separate sensitivity using `reads.created_at`, when available. It
reports missing timestamps and empty histories instead of guessing. This
corrects the interpretation of an offline action test without changing the
historical rating protocol or the serving scorer. Current ratings, embeddings,
and candidate text can still postdate an action; row availability alone does
not reconstruct the historical model.

```sh
OPENBLAS_NUM_THREADS=1 uv run --project engine --extra dev \
  python engine/scripts/evaluate_ranking.py /path/to/private-snapshot.json \
  --backend ollama --model qwen3-embedding:4b
```

Compare `explicit_feedback_diagnostic` with
`explicit_feedback_engine_available_history_sensitivity` in its aggregate
output. JSON exports should include `reads.created_at` to evaluate known row
availability. SQLite snapshots already preserve that field. An export without
it yields conservative empty histories with unknown availability counted;
those scores do not estimate a historical serving result.

Only one feedback candidate overlaps the 115 verified read/candidate identity
links, and its read does not precede the action. Excluding known prior-completed
target identities therefore changes none of the 127 cases. Those links do not
explain the pooled intent discrepancy.

## Discovery seed proxy

The current selector prioritizes rating, then ascending reading date, with work
deduplication and a maximum of two seeds per author. Compare that order with
recent-first and alternating old/recent orders at identical four- and 25-seed
budgets. A target is scored by its maximum title/author cosine to the selected
strictly earlier high-rated seeds. This measures resemblance to known books,
not actual provider recall or candidate availability.

At 25 seeds, recent-first improves validation high/low AUC from 0.5013/0.4976 to
0.5164/0.5244, and later AUC from 0.5088/0.4873 to 0.5767/0.5525. Its mean seed
age drops from 8.19 years to 0.33 years. Paired 2,000-resample book-bootstrap intervals for
later AUC changes are [0.0152, 0.1193] high and [-0.0110, 0.1383] low; they omit
time dependence. Among 226 unseen-author targets, however, AUC changes from
0.5206/0.5341 to 0.5161/0.5164. Both four-seed variants reduce validation
balanced high/low AUC; the mixed policy slightly improves low AUC while reducing
high AUC.
These mixed results do not justify changing the default discovery policy.

The production association evidence covers only four seed reads, so the cached
graph cannot establish a historical provider-recall comparison. A bounded
current Open Library probe compared current and recent seed policies at the
validation and later boundaries, using four seeds per arm, one list per seed,
12 items per list, and at most two additional resolutions. The four arms made
21 successful HTTP requests and encountered two list endpoints returning 404.
The provider received each full earlier history for already-read exclusion,
with its four predeclared seeds fixed for the comparison. Every arm returned zero recommendation
edges, so none retrieved a held-out high- or low-rated target. This
information-poor probe establishes no provider advantage for either policy.
The endpoints describe the current catalog, not historical availability; the
probe used private in-memory caches and persisted no candidates.

## Scope

Private snapshots, identity crosswalks, and newly generated vectors remain
outside the repository. Production SQLite access was read-only. No scoring
settings, catalog acceptance gates, or production records were changed.
