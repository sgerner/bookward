# Production ranking review — September 30, 2026

The current formula offers modest ordering of highly rated books and weaker identification of low-rated books. Four families of alternative scoring were tested against the production reader's rated history; none demonstrated a reliable improvement worth deploying. The serving weights remain unchanged. The review improves the evaluation tools and records the evidence so future changes can be compared honestly.

## Data and protocol

A consistent, allowlisted export was taken in one read-only SQLite transaction. Production was healthy and its database integrity check passed. The ranking and scoring source files matched the evaluated checkout byte for byte. The private export contains 1,797 reads, 1,931 candidates, 134 feedback entries, and 4,127 cached embeddings. It excludes settings and credentials and stays outside the repository.

The rating replay includes 1,777 distinct dated rated works: nine unrated, nine undated, and two duplicate identities were excluded; no read vector was missing or stale. Vectors use `ollama / qwen3-embedding:4b`, with 2,560 dimensions. The usable whole-day chronological split is 966 calibration examples, 355 validation books, and 356 later books. Each target is scored using only strictly earlier-day reads; same-day books are hidden together. The experimental implementation reproduced all 1,677 scored targets from the serving ranker exactly at its one-decimal precision.

High-rating AUC treats 4–5 stars as positive versus 1–3; low-rating AUC treats 1–2 stars as positive versus 3–5 and reverses score direction. Their average selects an alternative on validation only. Three-star books are not dislikes; the historical evaluator separately reports high-versus-low AUC excluding them. Star calibration is fitted on earlier examples and evaluated separately from raw ordering. Only the validation-selected alternative and current formula are compared on the later period.

Reproduce the read-book diagnostics and experimental comparison on the same private export:

```sh
OPENBLAS_NUM_THREADS=1 uv run --project engine python engine/scripts/evaluate_historical_ratings.py \
  /private/snapshot.json --backend ollama --model qwen3-embedding:4b
OPENBLAS_NUM_THREADS=1 uv run --project engine python engine/scripts/evaluate_ranking_experiments.py \
  /private/snapshot.json --backend ollama --model qwen3-embedding:4b \
  --bootstrap-iterations 2000 --bootstrap-seed 20260930
```

The experiment runner prints aggregates, verifies its reference formula against every serving-ranker prediction, and refuses to report a comparison if that parity check fails. Its feedback reference also matched all 127 usable intent cases. The [recorded aggregate experiment output](ranking-production-review-2026-09-30.json) contains no book records. It does not change settings, rescore the live database, or promote a formula. Live feedback and performance findings below came from separate read-only audits and private benchmarks rather than the read-book replay command. To reproduce source and catalog-confidence terms in the intent diagnostic, JSON candidate rows must include `source_weight` and `catalog_confidence` (or `quality_score`); the bare SQLite loader does not join those tables, and the report identifies missing feature coverage.

## Tested ideas

| Alternative | Rationale and fixed change | Validation high AUC | Validation low AUC | Validation star MAE |
| --- | --- | ---: | ---: | ---: |
| Current formula | Reference | 0.5240 | 0.5917 | 0.5711 |
| Stronger author evidence | Shrink author means toward the reader mean with three prior books rather than five; use five score points per star rather than 3.5 | 0.5227 | 0.5886 | 0.5709 |
| Kernel uncertainty, prior 2 | Effective neighbor count `sum(w)^2 / sum(w^2)` shrinks the kernel adjustment by `ESS / (ESS + 2)` | 0.5246 | 0.5931 | 0.5727 |
| Kernel uncertainty, prior 5 | Same uncertainty shrinkage with five prior observations | 0.5244 | 0.5939 | 0.5736 |
| Cosine centering | Replace each query's similarities with `clip(cosine - prior_history_mean_cosine + 0.25, 0, 1)` before similarity-based terms | 0.5321 | 0.6032 | 0.5720 |
| Robust negative neighborhood | Replace the single closest low-rated match with the existing similarity-weighted top-five mean | 0.5221 | 0.5808 | 0.5689 |

The ideas combine empirical-Bayes shrinkage, effective sample size, robust statistics, and embedding geometry. [Research on high-dimensional nearest-neighbor hubs](https://www.jmlr.org/beta/papers/v13/schnitzer12a.html) motivates checking density effects; the centering experiment here is a simple diagnostic, not an implementation of that paper's scaling algorithm.

Cosine centering won validation by average high/low AUC, but its ranking gains did not survive the later comparison:

| Later-period metric | Current | Cosine centering |
| --- | ---: | ---: |
| High-rating AUC | **0.6021** | 0.6010 |
| Low-rating AUC | **0.5429** | 0.5413 |
| Calibrated star MAE | 0.6861 | **0.6849** |
| Highly rated books in top 20 | 14 | 15 |

The high-AUC difference has a paired book-bootstrap 95% interval of −0.0132 to +0.0114; the low-AUC interval is −0.0186 to +0.0147 (2,000 resamples). Both include zero. Mean absolute score movement is 1.84 points. The roughly 0.0012-star error improvement is too small, with mixed discrimination, to justify changing visible recommendations.

## Are high and low scores useful?

For the current formula on the 356 later books, high-versus-low AUC excluding three-star books is 0.5953. Fourteen of the top 20 books were rated 4–5 stars; five of the bottom 20 were rated 1–2 stars. The highest score decile (66.0–89.0) averaged 3.69 stars, with 60% highly rated and 8.6% low-rated books. The lowest decile (35.1–49.4) averaged 3.22 stars, with 30.6% highly rated and 16.7% low-rated books. The whole block contains 43.0% highly rated and 15.2% low-rated books.

High scores enrich for highly rated books, but low scores provide little assurance of a dislike. Intermediate deciles are not consistently ordered by mean rating. These are retrospective rank cohorts, not reusable enjoyment probabilities or score cutoffs. Equal scores can cross decile/cutoff boundaries; stable chronological tie-breaking can affect small top/bottom counts.

## Performance check

The pure ranker took a median 0.109 seconds for a 431-candidate workload proxy and 0.135 seconds for all 541 hash-valid candidate vectors available to this private benchmark, over three warm runs on the study machine. These timings exclude embedding generation, database access, and live interaction/slate adjustments. Candidate status was not included in the export, so these are workload proxies rather than a reconstruction of production eligibility. Batched sorting and per-call metadata caches showed only roughly 2–4% improvements in small noisy measurements; no performance change was promoted.

## Actual production use

Production has 190 recommendation runs, 11,478 impressions, and 557 impressions marked visible. All logged propensities are 1.0. Visibility and action linkage are incomplete, so these data cannot identify an unbiased policy effect.

The separate latest save/reject replay has 127 usable candidates: 57 saves and 70 rejects. Current-formula AUC is 0.406; the original pre-neighborhood formula scores 0.705, and cosine centering scores 0.428. This is a warning about transferring the read-book benchmark to discovery, not a reason to revert automatically. Current catalog metadata, source weights, and selected/exposed populations differ, and this replay excludes the live interaction and slate adjustments.

Exact same-run action/impression matches, keeping the latest action per candidate, contain only three saves and 44 rejects. Recorded score AUC there is 0.773 and rank AUC is 0.852; the visible subset has just two saves and 15 rejects. Neither sample supports a strong quality claim. Latest saved candidates also have lower mean source weight (0.410 versus 0.638) and catalog confidence (0.711 versus 0.941) than rejected candidates. Their median description length is 44 versus 118 characters. Source and metadata terms deserve future stratified testing with better exposure coverage; removing catalog safeguards on these confounded observations would be premature.

## Limits and next evidence

This is one installation's reader-selected history. Eventual ratings are treated as if they were known on read dates; imports and updates do not retain reliable historical rating-availability times. Cached vectors and current metadata may postdate reads. Held-out books form synthetic time-block groups, not actual candidate slates. The historical periods were already examined during earlier tuning; the usable count is only four higher than the 1,773 in [the September 22 study](recommendation-historical-replay.md), which does not establish a fresh confirmatory cohort. The book bootstrap ignores temporal dependence and cannot establish generalization to other readers.

The next useful study should freeze a new prospective period, record actual candidate sets and policy versions, improve same-run feedback coverage, and compare source/metadata strata. A new scoring term should improve both high and low discrimination without sacrificing safeguards, and should be checked against actual discovery behavior before promotion.
