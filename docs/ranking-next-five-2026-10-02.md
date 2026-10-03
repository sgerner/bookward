# Five recommendation experiments and their combinations: October 2, 2026

All five ideas were tested with gpt-6-luna xhigh subagents. Several produced
positive point estimates, but none cleared the frozen scoring adoption rule.
The strongest development-selected combination was pairwise learning plus a
small supervised metric. It improved validation balanced AUC by 0.00160 and
put one additional favorite in the top 20, but its paired 95% intervals
include zero. The later period showed a small regression. The scoring
formula and enrichment scoring remain unchanged.

An October 3 provenance audit found that the metric/facets evaluator hashed
catalog genres with the normalizer's default eight-item limit, while production
stores provenance for up to 12 normalized subjects. This had rejected 402
otherwise valid genre fields in the 1,677-target cohort. The correction verifies
all 12 source subjects while retaining the existing eight-subject facet input.
The metric/facet grid, split masks, and selection rule were rerun, then the
corrected scores were evaluated in the original 57-arm master grid. Verified
target genres rose from 322 to 724; verified synopsis fields remained 610.
The corrected facet and joint results below supersede the earlier summaries;
see [the corrected metric/facets report](ranking-metric-facets-2026-10-02.md)
for the full before/after comparison.

A separate discovery correctness bug was found, fixed, merged, and deployed
in [PR #126](https://github.com/sgerner/bookward/pull/126): association providers
now receive the complete library, and LibraryThing excludes every known
title/author identity. Previously the runner supplied only 25 favorites, so
providers could rediscover other already-read books.

## Data and method

The fresh read-only production snapshot contains 1,797 reads, 1,934 candidates,
1,581 read-metadata cache rows, and 5,948 embedding rows. Integrity checks pass.
The prepared cohort contains 1,777 distinct, dated, rated works with valid
current vectors. Nine unrated rows, nine undated rows, and two duplicate works
are excluded. After 100 warmup reads, there are 1,677 targets: 966 development,
355 validation, and 356 later exploratory targets. Model selection uses three
expanding development folds with 200, 200, and 266 evaluation targets; the
first 300 eligible targets fit models only.

Every target hides its rating and all ratings on the same UTC calendar day.
Feature replay matches all 1,677 current production scores at serving precision.
All arms share the same cohort, chronology, original cached title-author query
vectors, source weight, and neutral query confidence. Learned scalers, PCA,
ridge/logistic coefficients, and subject vocabularies use earlier prefixes
only. Three-star ratings remain in training and count as neither favorite
(4–5 stars) nor dislike (1–2 stars). Scores use Python's one-decimal serving
rounding, including after clipping and combinations.

This is a retrospective read-as-candidate screen for one reader. Imported
reading dates do not reconstruct when production actually knew those rows.
The snapshot is fresh, but the outcomes are not: both held-out periods have
been examined in prior studies. Chronological fitting avoids target-label
leakage; it does not turn reused outcomes into new independent confirmation.
The study cannot establish live satisfaction lift or historical catalog
availability.

The pre-fit protocol selects configurations by the unweighted mean of three
within-fold balanced high/low AUCs. It allows modest gains without a two-percent
floor, but requires positive validation improvement in both endpoint
directions, a paired 95% interval above zero, consistent development evidence,
and no severe later regression. Validation runner-ups cannot replace the
development-selected winner.

## Results

Balanced AUC is the mean of favorite-vs-rest AUC and reversed
dislike-vs-rest AUC. Higher is better. Each family uses its development-selected
configuration; validation results below are descriptive comparisons.

| Scorer | Mean development AUC | Validation change vs current | Later change vs current |
| --- | ---: | ---: | ---: |
| Current | 0.601188 | — | — |
| Separate favorite/dislike heads | 0.551891 | +0.007591 | −0.020439 |
| Capped pairwise residual | 0.601244 | +0.001823 | +0.000294 |
| Rank-one supervised metric | 0.601531 | +0.001402 | −0.000657 |
| Grounded structured facets | 0.594318 | +0.011018 | −0.004368 |
| Joint metric and facets | 0.594871 | +0.010849 | −0.005046 |
| Selected `pair_metric_pairwise` combination | **0.601636** | **+0.001602** | **−0.000588** |

The selected `pair_metric_pairwise` combination improved all three development
folds. Its validation favorite AUC improved by 0.001406 and dislike AUC by
0.001799. Top-20 favorite precision
rose from 35% to 40% (seven to eight books); bottom-20 dislike precision stayed
at 25%. The later top and bottom tails were unchanged, while dislike AUC
decreased by 0.001257. Validation unseen-author balanced AUC rose by 0.001040;
the later unseen-author slice decreased by 0.002521.

Paired bootstraps resample complete groups, keeping the same sampled books in
both scorer arms. Each uses 2,000 replicates and seed 20261002. The selected
combination's validation balanced-AUC difference has these 95% intervals:

| Resampling unit | Interval |
| --- | ---: |
| UTC reading day | [−0.000180, +0.003415] |
| Author | [−0.000295, +0.003589] |
| 30-day block | [−0.000076, +0.003459] |

All include zero. The later day interval is [−0.002714, +0.001479]. Separate
validation day intervals also include zero for heads, pairwise, metric, facets,
and the metric/facet joint model. Pairwise's dislike-only interval is positive,
but its balanced interval is [−0.000381, +0.004103], and its development
favorite AUC falls slightly. These results support further observation, not
automatic scoring promotion.

## Combinations and enrichment controls

The main comparison covers 57 named score arms, including duplicated control
or equivalent blend outputs; these are not 57 independent experiments. It
includes the four selected scorers, every six unordered 50/50 scorer pairs,
75/25 current/new blends, the metric/facet grid, and fixed combinations with
previous causal recency, cluster3, and ordinal signals. Each new scorer also
receives the recency-plus-cluster3 two-signal combination. Normalization uses
training prefixes for development and fixed development-OOF scorer alignment
for validation/later. The first-fold ordinal scaler can use prefix-fit values
for fitting only; no missing evaluation predictions are backfilled.

The separate rich-view comparison covers 24 named arms. It reuses previously
frozen Qwen rich-view vectors, whose original title-author vectors are
byte-identical to the fresh cache. Rich queries are held identical
when comparing original and enriched history, with the same neutral confidence.
All 1,677 rich controls match the direct current scorer. Heads/pairwise are
refitted on rich causal features; metric/facet parameters are transported from
primary development selection without rich-view retuning. The original-view
cluster3 signal remains explicitly labelled when used with rich features.

The frozen Qwen rich-view vector archive covers 762 of 1,677 eligible targets;
uncovered books stay in the comparison. This is vector availability in the
earlier rich-view study, not strict-field provenance coverage for a new encoder.
The rich development-selected metric averages 0.592966, versus 0.592210 for
rich current and 0.601188 for primary current. In validation, transported rich
metric, facets, and their joint score underperform the same-query/original-history
control by 0.0313, 0.0157, and 0.0160 AUC, respectively. The facet and joint
figures replace the earlier provenance-limited results.
Rich heads show favorable validation point estimates but lose materially in
development. The rich metric's validation difference versus rich current is
+0.000840, with day interval [−0.002439, +0.004853]. Richer history did not
produce a robust scoring improvement in these matched tests.

The typed retrieval experiment used four earlier favorite seeds, a ceiling of
20 public GETs per arm/boundary, at most 50 items per response, one global GET
per second, and no retries. Typed current seeds yielded 496 and 495 eligible
works in 16 requests each, while the existing list provider yielded zero in
six requests each. The baseline had one HTTP failure per boundary, and actual
costs differ despite the shared ceiling. Recent/mixed seeds and offline
diffusion were also tested. The initial imported-row matcher found no future
favorite or dislike. A subsequent identity sensitivity added 820 source-hash-valid
cached Open Library work IDs, skipped zero conflicting IDs, and left all pools
unchanged. It recovered one later disliked target in typed-current/diffusion,
and still no favorites. A fresh compatible local replay placed that dislike
166th of 495, score 53.3, outside the top 20; it was also not near the bottom.
This used 494 unique candidate-text embeddings, the same text fingerprint and
model digest, and zero public requests. It is a one-book sanity check, not a
new estimate of overall utility. Its enriched-text sensitivity also needs the
legacy-projection caveat in the retrieval report: the old experimental
projection could use an opening sentence as a synopsis or stringify structured
description objects. The helper now accepts actual description strings or
their string `value` and excludes opening sentences; affected old enriched
results are retained as legacy diagnostics, not used for adoption.

Detailed family reports:

- [Favorite/dislike heads and pairwise learning](ranking-tail-pairwise-2026-10-02.md)
- [Supervised metric and grounded facets](ranking-metric-facets-2026-10-02.md)
- [Typed retrieval, seed policies, and diffusion](ranking-typed-retrieval-2026-10-02.md)

## Correctness work and delivery

The corrected standalone report compares aligned one-decimal score arrays,
while this master grid compares raw candidate arrays before combination
exports. Serving rounding can change a few tied ranks, so the corrected
standalone and master facet/joint deltas differ slightly.

Audits checked production parity, identity joins, complete-day cuts, endpoint
direction, unknown metadata handling, training-only fitting, serving rounding,
and hash-locked development selection. Field-level provenance was added to the
private export after checking that all 1,581 original metadata rows remained
identical. Facet setting cues were narrowed before the first fit to avoid
interpreting phrases such as “her future” as a future setting. A metric
square-root coefficient error was corrected before predictions, and direct
transformed-vector equivalence is tested. Held-out poisoned labels cannot
affect metric fitting.

One recency/cluster combination used a mean where the frozen clarification
required two additions. Only those combination arrays were corrected; all
raw and selected model arrays remained unchanged. The original artifacts were
preserved. After the genre-verification correction, the 57-arm grid was
reselected with the corrected metric/facet scores and the unchanged pairwise
archive; it again selected `pair_metric_pairwise`. No feature grid was
expanded in response to validation results.

The merged full-library association fix has regressions for disliked/unrated
reads, a library beyond the 25-seed runner cap, and preservation of an unread
candidate. Separate local stress tests used all 1,797 production reads and
synthetic responses of 50 actual non-seed library books. For both Open Library
and LibraryThing, the subset call emitted 50 already-read edges and the
corrected full-library call emitted zero. Those are exclusion tests, not
measured retrieval lift; they made no network requests or production writes.

PR #126 passed all checks on its exact final head and merged as
`5ff2368c618d17f2c5d18edebb955d2221ea457b`. Production is healthy and both changed
runtime files match that code. Full read and settings fingerprints are
unchanged, integrity passes, and foreign-key violations are zero. The README
describes complete-library exclusion. The complete local engine suite passed
365 tests after the final diagnostic projection correction. The retrieval
evaluator's focused suite also passed all 10 tests.

No scoring winner was implemented. Experimental evaluators and reports remain
uncommitted in the existing study checkout; private rows, vectors, provenance,
and source captures remain outside Git with directories `0700` and files `0600`.
The user's original checkout and its pre-existing documentation edits were
preserved.

## Final evidence identities

| Artifact | SHA-256 |
| --- | --- |
| Fresh private corpus | `0973bf5e029f1ee0f2a6ad8957313e64da639b813e20d31491a9ba4b8558eeb9` |
| Pre-fit protocol | `8c58739a896acb2d0284f3c51eb5752121ed9262a415ecd313b8f124d7dd935d` |
| Primary causal features | `43e2c09b6251b9bb6fee521cb5df565952f2ee9306c3afb3850d51518a88c329` |
| Genre-verification method amendment | `5799511207fb055b5322df17b6afacfdefa141bc64001b790e3cff499ddd8256` |
| Original pre-correction metric/facet scores, preserved | `bad725137b6745202a3fd27be9eb7cb498beafee313c0b86abfe5148736af66d` |
| Corrected metric/facet scores | `93b33de2c27eb01801dc99c86ad9fb1d0112b2e00a7f408cb5f309b741105c29` |
| Unchanged tail/pairwise scores used in corrected master grid | `f39ad82420fb023f3cd66b41dbfe6997854f3e221edfc70c8f2bb03757d62ab6` |
| Original master development lock, preserved | `ea6ace0631b67fcf770ea27267a5bef0230abe232afa914ca41f57ade2d3ac50` |
| Corrected master-grid method amendment | `f59679751773e3c96e570e9f5f309f75d7cf5584412b92e424f6e59add318745` |
| Corrected 57-arm master development lock | `7779739bf96e562d2f580685d52ce81d1591eac7f79dd20e2184c110cd08b6dd` |
| Corrected 57-arm master report | `05c7f8e2d894506065c680de9b3311a9749ce16d1dffae890e4a6336bca0d539` |
| Original main aggregate report, preserved | `d6e3d3d07675f8e06550b0bb787a835df89abbccff7d01f07d9da52f69756cd5` |
| Rich development lock | `9ef7774340f90b30437139b396b92b6b80f196c0a89f63fd90ff5b683de94b14` |
