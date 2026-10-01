# Ranking methodology audit: September 30, 2026

The earlier conclusion was too broad. The tested title-only and frozen ordinal
variants did not establish a scorer replacement, but actual read enrichment was
not scored, source and retrieval diagnostics lack power, and an interest model
has a small exploratory tail-utility signal. Those hypothesis families remain
open. This audit uses three independent GPT-6-luna xhigh agents and the frozen
private production corpus; its [aggregate evidence](ranking-methodology-audit-2026-09-30.json)
contains no individual reading records.

## What was verified

Direct calls to the serving ranker reproduce all three title views on the same
355 validation and 356 later targets. Cached title/author, freshly encoded
identical title/author, and title-only scores have zero per-target discrepancies
against their original scoring helpers. The rich and compatible representations
also reproduce all 127 action scores. The independent sklearn AUC calculation
agrees within 1.1e-16, a tied-score toy agrees with brute-force pair counting,
and reversing scores for low ratings gives the intended label direction.

The audit found no same-day or target-ID history inclusion in the 711 rating
queries. Six rating-query and four action-query target-rating mutations leave
scores unchanged; these are sampled checks, not exhaustive proof. Fixed-seed
label shuffles yield rating AUC means near 0.5. The corrected public ordinal
replay additionally reproduces all 1,677 serving baseline scores exactly.

These checks support the implemented replay. They do not reconstruct the model
as it existed on each historical date: present metadata, final ratings, and
embeddings can postdate reads and actions. The later period has already been
examined in earlier studies, so it is exploratory, not independent confirmation.

## The missing actual enrichment test

The previous experiment encoded enriched vectors but only reported coverage;
it never supplied them to the rating replay. Removing author text is not a test
of adding descriptions and subjects.

The new paired replay uses fresh title/author vectors as its execution-matched
control. It holds targets and strictly earlier UTC-day histories constant, calls
`rank_candidates` directly, and tests enriched target vectors alone and enriched
target vectors plus available enriched prior-read vectors. Source and catalog
terms are neutral read-like defaults in every arm. Descriptions and subjects
change embeddings only; they do not change other score fields.

The primary cohort requires OpenLibrary metadata, both title/author acceptance
gates, and a stable identifier or exact title/author link. It contains 19 rated
books: 11 high, two low, six neutral.

| Representation | High AUC | Reversed low AUC |
| --- | ---: | ---: |
| Fresh title/author | 0.653 | 0.574 |
| Enriched target only | 0.693 | 0.529 |
| Enriched target and available prior history | 0.705 | 0.559 |

These are pooled exploratory measurements, including earlier training-period
books. The primary validation slice contains only three neutral books; the
later slice contains four books, only one low. A broader 108-book content-bearing
cohort has high AUC 0.622 → 0.707 and low AUC 0.752 → 0.671 when both sides use
available enrichment, but its catalog metadata is not fully verified. It has
only seven low ratings, just one in the later slice.

Coverage is selected: 56.5% of those 108 books are high-rated and 6.5% low-rated,
versus 36.2% and 19.9% in the full history. Enriched earlier vectors replace only
about 5% of history in that broader cohort. This is not a fully enriched reader
profile. All linked candidate rows were created after their corresponding read
dates, and historical catalog verification timestamps are largely absent.
Existing enriched-vector caches lack per-row document hashes, so the audit
verifies generation-code lineage and array joins, rather than claiming complete
cryptographic vector-to-text provenance.

The high-rating point gains justify keeping enrichment under investigation.
The low-rating results and sparse coverage justify neither promotion nor
abandonment.

## Corrected daily model metric

Daily ordinal refits had been compared by pooling raw latent scores. Separately
fitted models need not share latent origins or thresholds. The evaluator now
uses posterior expected ratings on a common 1–5 scale; regression tests cover
models with equivalent posteriors but shifted latent origins. Frozen-model
comparisons are unaffected.

Corrected later high/low AUC is 0.6203/0.4980 versus current 0.6021/0.5429.
The high improvement and low regression persist. Paired book-bootstrap intervals
for the differences are [-0.0176, 0.0513] and [-0.0990, 0.0044]; they ignore
serial dependence. Comparable pairs within actual same-day groups are sparse
(34 high and 15 low pairs in the later period), so this replay cannot reliably
estimate ranking quality within daily recommendation slates.

A canonical descending stable ordering now defines both top and bottom tails
in the ordinal report. That tie-policy correction changes none of the observed
current, ordinal, or three-cluster later tail counts.

The ordinal calibration mapping was fitted using model scores on the same
rows whose labels trained that model. Later labels are not used to fit the
calibrator, so later MAE is still measured out of sample, but the calibration
training scores can be optimistic and unrepresentative. A causal or out-of-fold
score stream should train future calibrators; existing calibrated MAE should
not be used as decisive comparative evidence. This does not affect raw AUC or
uncalibrated posterior expected-rating comparisons.

## Interpreting the other ideas

The three-cluster model identifies 16 high books in the top 20 and six low books
in the bottom 20, versus current 14 and five. Its global AUC regresses, but this
small tail signal was overlooked in the broad failure conclusion. Tail
uncertainty and recommendation-slate utility were not established. Frozen
prefix clusters with two added features do not exhaust multi-interest methods.

Source/catalog confidence cannot be evaluated from rated reads that lack those
fields. Exposed action subsets have only three or two saves. Their mixed results
cannot reject the broader idea. A bounded discovery probe returned zero edges
in every arm; that is an inconclusive measurement, not evidence that policies
are equivalent. Recent seeds did improve the overall similarity proxy, although
not its unseen-author slice.

## Requirements for the next enrichment experiment

Collect metadata independently of recommendation acceptance, across every
rating class, with sufficient low-rated books. Freeze identity checks and
metadata before scoring, record document hashes and encoder versions, and use
the same representation for targets and prior history. Compare enriched and
fresh title/author controls on identical targets and coverage, including a
prespecified missing-metadata fallback.

Prespecify high/low discrimination, top/bottom utility, temporal or author-block
uncertainty, and an acceptable low-rating tradeoff before inspecting outcomes.
Use a new prospective period for confirmation. Keep retrospective taste replay
separate from historical availability and from live exposure effects. The
current corpus can screen hypotheses; it cannot provide an untouched holdout
again after repeated experiments.
