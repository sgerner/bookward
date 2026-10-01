# Ordinal preferences and interest neighborhoods: September 30, 2026

Neither learned alternative justified changing the serving ranker. This follow-up
tested whether learning from all five rating levels, or separating several
interest neighborhoods, would improve both high- and low-rating discrimination.
The [aggregate report](ranking-ordinal-interests-2026-09-30.json) records the
metrics, cohort sensitivities, and uncertainty intervals.

The private production snapshot contains 1,777 distinct dated rated works with
valid embeddings. After the first 100 reads establish history, there are 966
training, 355 validation, and 356 later-period predictions. Every query hides all
reads from its own calendar day. The evaluator reproduces the serving scores for
all 1,677 eligible queries before comparing alternatives.

| Model | Validation high AUC | Validation low AUC | Later high AUC | Later low AUC |
| --- | ---: | ---: | ---: | ---: |
| Current serving formula | 0.5240 | 0.5917 | 0.6021 | 0.5429 |
| Ten-component ordinal model | 0.5363 | 0.5994 | 0.5755 | 0.5268 |
| Ordinal model with three interest neighborhoods | 0.5552 | 0.6206 | 0.5388 | 0.5312 |
| Daily expanding ordinal diagnostic, corrected common scale | 0.5226 | 0.5957 | 0.6203 | 0.4980 |

High AUC distinguishes 4–5-star books from 1–3 stars; low AUC reverses score
direction to distinguish 1–2 stars from 3–5. The frozen ordinal and interest
models improve validation but regress on the later period. Their calibrated
star errors also worsen: mean absolute errors are 0.7045 and 0.7610 stars,
compared with 0.6861 for current scores. The ordinal calibration mapping uses
in-sample training scores; later MAE is out of sample, but causal out-of-fold
calibration is needed before using these errors as decisive model evidence.

The daily expanding model is an additional exploratory diagnostic, added after
the initial frozen-model result. It refits on strictly earlier causal examples
with the same regularization and feature set. Pooled comparisons now use posterior expected ratings on a common 1–5 scale;
raw latent scores from separately refitted models have unaligned origins and
thresholds. Its later high-AUC gain of 0.0182
comes with a low-AUC loss of 0.0449. Paired 2,000-resample book-bootstrap intervals are
[-0.0176, 0.0513] for the high-AUC change and [-0.0990, 0.0044] for the low-AUC
change. Low-AUC regressions persist in both temporal halves and are larger for
authors absent from the earlier history. This is not a useful improvement in
both directions.

## Methods

The ordinal model is a regularized proportional-odds cumulative logistic model.
Its ten inputs summarize positive and negative neighbor similarities, local and
kernel ratings, recency, shrunk author evidence, the reader's earlier mean, and
history size. Standardization uses training examples only. Frozen models are
refit on training plus validation before the later comparison.

The interest variant adds rating evidence from three or five cosine-space
clusters. Cluster centers are learned from the first 100-read prefix and frozen;
new reads enter cluster evidence only after their query day. Validation selects
three clusters. No later-period search rescues that model.

The models are inexpensive to fit, but cost does not justify weaker ordering.
Daily ordinal fits take roughly 38–46 milliseconds on the study host, excluding
embedding generation and shared feature extraction.

## Reproduction and limits

```sh
OPENBLAS_NUM_THREADS=1 uv run --project engine --extra dev \
  python engine/scripts/evaluate_ordinal_interests.py \
  --corpus /path/to/private-corpus.json --output /path/to/aggregate-report.json
```

The script reports aggregates and fails if its reconstructed serving scores do
not match the ranker. The corpus and individual reading records are not included
in the repository.

This is retrospective evidence for one reader's chosen books. Final ratings and
embeddings may postdate reading dates, the later period was already inspected
in previous studies, and book bootstrap intervals omit temporal dependence.
The test does not measure discovery recall, unread-candidate availability, or
live recommendation outcomes. A prospective frozen evaluation is needed before
claiming general preference gains.

## Methodology audit

The [methodology audit](ranking-methodology-audit-2026-09-30.md) corrects the daily
score scale and distinguishes global discrimination from tail utility. The
three-cluster model finds 16 high-rated books in its top 20 and six low-rated
books in its bottom 20, versus 14 and five for current scores. That exploratory
tail signal deserves a prespecified follow-up; worse global AUC does not prove
that every interest-based approach lacks utility. The canonical tail ordering
correction changes none of these observed counts.
