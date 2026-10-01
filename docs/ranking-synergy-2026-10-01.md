# Ranking synergy follow-up — October 1, 2026

This report summarizes fixed tests of ranking-formula edits, history-signal
blends, metadata representations, and score-level fusion. Several modest
signals remain candidates for a prospectively frozen test, especially the
production read-symmetric 25% fusion. No result supports changing the serving
ranker today. The [aggregate JSON](ranking-synergy-2026-10-01.json) contains
only whitelisted metrics and private-artifact hashes.

## Corpus and protocol

The frozen, read-only corpus has 1,777 distinct dated rated works. After the
first 100 reads establish history, the chronological replay has 966 training,
355 validation, and 356 later-period targets. Same-UTC-day reads are hidden
together. High AUC treats 4–5 stars as positive versus 1–3; reversed low AUC
treats 1–2 as positive versus 3–5. Three stars are neutral. Top-20 high and
bottom-20 low counts are reported separately.

The later period has been examined in prior studies and is reused exploratory
evidence, not independent confirmation. The fixed score-fusion check was
registered after its constituent validation and later results were seen, so
its results are descriptive. No new production snapshot was taken; the frozen
corpus was used read-only.

Current-ranker parity is exact at served precision for all 1,677 eligible
targets. The production-template scorer also matches direct `rank_candidates`
scores for every tested view and target. Cached and fresh embedding executions
are not identical (repeat-check cosines 0.9967–0.9981; score differences up to
four points), so each comparison uses its own fresh title/author control.
Cross-execution score movement is not treated as a ranker parity failure.

## Fixed history and ranker tests

The history-signal grid used training-only standardization and the fixed blend
`z(current) + 0.25 × z(signal)`. Signals were causal k=3 cluster evidence,
recency, local rating, and ordinal expected stars; the registered pairs were
cluster+recency, cluster+ordinal, and local+recency. Validation selected the
ordinal blend. The table reports high/low AUC and top/bottom tail counts.

| Arm | Validation AUC H/L | Balanced | Top-20 H / bottom-20 L | Later AUC H/L | Balanced | Top-20 H / bottom-20 L |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Current | .5240 / .5917 | .5579 | 8 / 4 | .6021 / .5429 | .5725 | 14 / 5 |
| Ordinal addition | .5273 / .5933 | .5603 | 8 / 5 | .5990 / .5412 | .5701 | 15 / 5 |
| Recency addition | .5230 / .5917 | .5574 | 6 / 6 | .6176 / .5653 | .5914 | 16 / 7 |
| Cluster + recency | .5217 / .5872 | .5544 | 6 / 6 | .6181 / .5668 | .5925 | 15 / 7 |

Ordinal won by a small validation margin; its later AUCs were slightly below
current. Author-cluster and 30-day-block intervals for the selected ordinal
blend include zero in both directions. Recency and cluster+recency look
stronger later but failed validation and were not rescued from later outcomes.
The fixed current-ranker 2³ factorial crossed stronger author weighting,
ESS5 kernel shrinkage, and top-five negative aggregation. ESS5 alone won
validation (balanced AUC .5591 vs .5579 current; high/low .5244/.5939 vs
.5240/.5917), but later balanced AUC was .5744 vs .5725 with high AUC down
.0033, low up .0071, and unchanged 14/5 tails. Author+ESS5+negative had
.5495 validation balanced AUC and .5747 later, with 15/4 later tails. These
small, directionally mixed changes do not establish factorial synergy; paired
whole-calendar-day intervals include zero.
The separate frozen-prefix k=3 interest model remains a tail hypothesis: 16
high-rated works in its top 20 and 6 low-rated works in its bottom 20, versus
14 and 5 for current, despite weaker global discrimination. Tail utility has
not been established. The bounded author, ESS2/ESS5, cosine-centering, and
negative-neighborhood formula tests are detailed in the [production
review](ranking-production-review-2026-09-30.md).

## Metadata vectors and exact templates

There are 978 exact ISBN-and-identity enrichment matches among the prepared
reads, plus 143 separately reported verified title-author-only matches. Within
the 1,677 causal targets, 921 have primary metadata coverage and 134 have
secondary coverage; missing rows use fresh title/author vectors. Metadata was
collected in September 2026, so its historical availability is unknown.

The earlier labeled view encoded `Title: … Author: … Description: … Subjects:
…` on both sides. It is not the production text template. The later
production-template test used these fixed views, with missing rows falling
back to fresh title/author vectors:

| View | History representation | Target representation |
| --- | --- | --- |
| Fresh control | Title + author | Title + author |
| Read-rich history / candidate query | Production `scoring.document()` read branch: title, author, cleaned description (≤4,000 characters), up to 8 normalized subjects | Production candidate branch with the same available fields |
| Read-rich symmetric | Production read branch | Same read branch; a dummy rating key selects it but is not embedded |
| Canonical candidate-shared | Production candidate branch | Production candidate branch |

The primary validation cohort has 191 targets. Fresh title/author wins the
registered two-direction gate: the symmetric rich view reaches balanced AUC
.5857 vs .5837 but low AUC falls .0172, beyond the .01 allowance. On all 356
later targets, symmetric rich vectors score .6307 high AUC / .5214 low AUC vs
.5980 / .5422 for fresh. The gain favors high-rating discrimination while low
rating weakens. The new vectors matched direct ranker parity on all 1,677
targets; this is still retrospective, not historically available metadata.

## Fixed 75/25 score fusion

The fixed formula is `round(0.75 × fresh_TA_served_score + 0.25 × rich_served_score, 1)`.
Four rich views were fused with current scores and with their matching ESS5
scores; weights were not searched or calibrated. This composability check was
explicitly registered after constituent outcomes were viewed.

The exact production read-symmetric fusion is worth preserving for a new
prospective test. It was not the validation-selected fusion arm, and later
results remain exploratory.

| Cohort and split | Fresh H/L (balanced) | Symmetric 75/25 H/L (balanced) | Top-20 high; bottom-20 low, fresh → fused |
| --- | ---: | ---: | ---: |
| Primary validation, n=191 | .5391 / .6282 (.5837) | .5426 / .6290 (.5858), +.0021 | .30/.25 → .30/.25 |
| All later, n=356 | .5980 / .5422 (.5701) | .6105 / .5387 (.5746), +.0045 | .65/.25 → .70/.25 |
| Primary later, n=187 | .6378 / .5731 (.6055) | .6639 / .5814 (.6227), +.0172 | .50/.20 → .50/.15 |

Temporal results are mixed: in the first later half (174 targets), balanced AUC
moves .5600 → .5757; in the second half (182), it moves .5664 → .5615, and
reversed low AUC falls .5559 → .5360. The fusion selected on primary validation
was the older labeled metadata view, not the production read-symmetric view.
Its two-way author/date-block 95% intervals cross zero for high, low, and
balanced AUC. This supports retaining the symmetric fusion as a candidate, not
claiming a reliable gain.

In the separate joint replay, adding the fixed ordinal or cluster signal to
the older labeled metadata score improved later balanced AUC over metadata
alone by .0022 and .0036. Against matched fresh title/author history controls,
both metadata blends traded higher high-rating AUC for lower low-rating AUC.
Descriptive interaction residuals were near zero and paired block intervals
crossed zero; the old-template result is not production-template evidence.

## Limits and reproduction

Other bounded families remain unresolved. The [five-hypothesis
review](ranking-five-options-2026-09-30.md) reports that recent-first seeds
improved an all-book similarity proxy but not its unseen-author slice; a
bounded provider probe returned no recommendation edges. Source and catalog
confidence cannot be tested on rating rows that lack those fields. The 127
save/reject actions have propensity 1 and selected, incomplete exposure; pooled
ablations conflict with tiny same-run and visible-action subsets. The existing
[slate policy](discovery-slate-diversity.md) makes bounded local swaps in the
first eight results, but no test shows that readers prefer greater diversity.
In the fixed ESS5 source/confidence composition check, the score shift moved
latest-feedback AUC from .4060 to .4041; the 47 same-run impressions moved
from .7765 to .7879, with only three saves. These selected samples do not
support a full slate reconstruction or causal source/confidence claim.
These replays do not reconstruct historical candidate availability, the full
recommendation slate, or a causal live-policy outcome.

Run the history-signal evaluator with a local private corpus:

```sh
OPENBLAS_NUM_THREADS=1 uv run --project engine --extra dev \
  python engine/scripts/evaluate_ranking_synergy.py \
  --corpus /path/to/private-corpus.json \
  --backend ollama --model qwen3-embedding:4b \
  --output /path/to/private-aggregate.json
```

The joint replay also requires the matching private score archive, report,
history audit, and frozen preregistration; replace each placeholder with the
corresponding local private file:

```sh
OPENBLAS_NUM_THREADS=1 uv run --project engine --extra dev \
  python engine/scripts/evaluate_ranking_synergy.py \
  --corpus /path/to/private-corpus.json \
  --backend ollama --model qwen3-embedding:4b \
  --joint-metadata-scores /path/to/private-view-scores.npz \
  --joint-metadata-report /path/to/private-view-report.json \
  --joint-history-scores /path/to/private-current-audit.npz \
  --joint-preregistration /path/to/private-joint-preregistration.json \
  --joint-metadata-arm metadata_combined \
  --output /path/to/private-joint-aggregate.json
```

The evaluator joins by ID, verifies report/corpus/archive hashes and the
validation-selected view, checks current-score parity, and emits aggregates
only. The text/vector archives and their re-embedding drivers remain private.
Private artifact SHA-256 digests (artifacts are not published):

| Artifact | SHA-256 |
| --- | --- |
| Frozen corpus | `839ca5fcdafaf42e96f4551d66a72e426f31a7f22134bdf82f7e04e61cde6749` |
| History-signal aggregate | `8f03b69fe2c661e6089ea6f1dc5d7c603b546705e7db37dff95f6bbf8e519bc4` |
| Joint history/metadata aggregate | `7bfa93dd1632d08dde46cba9321b289d61e84987afafe707d4388766cb5ad743` |
| Labeled metadata score archive | `ddea89f120b21bed1950641e0650e8ad50c27ccc347c5488408c5f9718715fc4` |
| Production-template score archive | `2de90acdae692f1d669097fa2dfa0cbeff9c12ea9e0529f3a4a18e5d4d46e82e` |
| Fixed score-fusion preregistration | `727d437cb829094216cc99511d3f27ef62a49bd19c59997982d379eccdbdfe7e` |
| Fixed score-fusion aggregate | `89f8571c265b9c875b49cd0625cfe9e9ddab93c089ff9d9894cbdcfe6dbb7591` |
| Fixed score-fusion archive | `e23aacd6211ce7d59ceb54379c7224f7299d8d5b653de7ad598ef879f6ed6d5d` |

The complete engine suite passed 259 tests, including 12 focused synergy tests.
For prior detailed results, see the [production review](ranking-production-review-2026-09-30.md),
[ordinal and interest study](ranking-ordinal-interests-2026-09-30.md), and
[methodology audit](ranking-methodology-audit-2026-09-30.md).
