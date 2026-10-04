# Recommendation experiments and findings

Last reviewed: October 4, 2026. Current implementation baseline: main `184c269` (PRs #146 and #147). This is the central record of completed recommendation experiments, corrections, and historical proposals. Operational guides remain separate. Private captures, individual books, ratings, vectors, and judgments remain outside Git; aggregate JSON reports and executable evaluators remain available where already tracked.

## Current decisions

| Area | Decision and evidence |
| --- | --- |
| Rated-neighbor scoring and recency | Shipped the 40-neighbor adjustment and eight-year read-recency weighting. Earlier quality claims were narrowed after date parsing and refreshed-corpus corrections; retrospective changes do not establish live enjoyment gains. |
| Kernel uncertainty | Shipped ESS/(ESS+5) shrinkage. Small balanced AUC gains came with a high-rating tradeoff and intervals including zero; no live benefit established. |
| Read enrichment and 75/25 fusion | Not adopted. The full-corpus, corrected-capture comparison supersedes the promising subset result: every tested fusion had lower validation balanced AUC than corrected capture. Rich read metadata remains separate from scoring text. |
| Ordinal learning, interests, text views, metric/facets, pairwise and combinations | Closed without a serving-policy change. Positive development point estimates were inconsistent or uncertain; strict-v2 provenance reruns supersede initial metadata results. |
| Reading-experience cues | Not adopted: source coverage failed the frozen gate. Missing cues remain unknown. |
| Fresh interest correction | Closed without adoption. Both fresh top-eight slates had three interested, three rejected, two already-read responses; the changed slot replaced one rejection with another. Four recovered-synopsis follow-ups were all negative and kept separate from fitting. |
| Publication era | Ungated historical era models not adopted. A separately implemented per-profile confidence gate can apply at most ±2 points only after all three held-out periods improve; the captured production history abstained. See the operational guide for the implemented policy. |
| Identity, history and replay integrity | Shipped ISBN preservation, verified metadata work-ID exclusions, full-library association exclusions, conservative known/unknown-author identity handling, import-date safeguards, interaction-vector handoff and immutable scoring/serving evidence. Eight confirmed missing original works were repaired as unrated historical entries; no invented dates or ratings. |
| October 4 retrieval and power-6 tests | Closed without adoption. No held-out favorite retrieval gain; top-eight enjoyment unknown. Power 6 failed to establish improvement, including twenty independent Goodbooks readers. Keep power 8 and existing retrieval. |
| Embedding cache | PR #146 shipped validated float32 shape/finite/dimension checks and atomic rebuild safeguards. No production corruption observed; all 358 captured scores reproduced exactly and 2,352 valid cache vectors reused without inference. PR #147 fixed only the offline bare Open Library ID parser. |

## How to read the record

The dated sections below preserve original methods, results, limitations, hashes and reproduction notes. Their past-tense deployment statuses and phrases such as “current”, “open”, “pending” or “experimental” describe that capture date, not today's shipping decision. The table above and later explicitly corrected results take precedence. Reused historical labels are development/regression evidence, not untouched confirmation. A missing judgment is unknown; interest, completion and enjoyment are different endpoints. The independent-reader benchmark uses source order without timestamps and has substantial popular-book/prolific-reader bias.

The September 22 roadmap and October 2 plan are archived proposals, not tests already executed or authorization to deploy. Some October 3 evaluators were retained only in private study archives: reproduction commands in those sections refer to the archived checkout, not files promised in current main. No new experiment or model tuning was performed for this documentation consolidation.

### Delivered follow-up to the already-read audit

The original audit below predates delivery. PR #136 preserved valid ISBNs; #141 admitted fresh, provider-consistent verified metadata work IDs with archived identity proof; #142/#144 corrected association title collisions while retaining unknown-author fallback; #143 prevented undated historical imports from fabricating completion outcomes. Eight confirmed missing originals were subsequently imported without ratings or read dates, including Shakespeare's original rather than the separately credited adaptation. Production verification retained 1,811 reads / 1,794 rated, found zero read leaks in the captured first eight, and confirmed idempotent repair. These are correctness and history repairs, not measured preference lift. PRs #127, #128 and #130 pipeline fixes/evidence capture and PRs #146/#147 are deployed; old “pending” notes below are historical.

## Study index

- [Recommendation quality study](#recommendation-quality)
- [Already-read eligibility audit and repair](#already-read-exclusion-audit-2026-10-03)
- [Association source identity correction](#association-source-identity)
- [Optional disinterest reasons and fresh interest confirmation](#discovery-follow-up-protocol-2026-10-03)
- [Discovery follow-up scoring audit](#discovery-followup-scoring-audit-2026-10-03)
- [Discovery interest pilot — October 2, 2026](#discovery-interest-pilot-2026-10-02)
- [Embedding cache integrity correction](#embedding-cache-integrity-2026-10-04)
- [Full-Corpus Enrichment Evaluation](#enrichment-full-corpus-evaluation)
- [Explicit-interest reranker development study (2026-10-03)](#explicit-interest-reranker-2026-10-03)
- [Four focused ranking tests: October 4, 2026](#four-focused-ranking-tests-2026-10-04)
- [Kernel uncertainty shrinkage](#kernel-uncertainty-shrinkage)
- [Complete production ranking pipeline audit — October 2, 2026](#production-ranking-pipeline-audit-2026-10-02)
- [Ranking evaluation across users](#ranking-cross-profile-evaluation-2026-10-03)
- [Embedding representation screen: October 2, 2026; strict-v2 rerun October 3](#ranking-embedding-representations-2026-10-02)
- [Five ranking hypotheses: September 30, 2026](#ranking-five-options-2026-09-30)
- [Ranking methodology audit: September 30, 2026](#ranking-methodology-audit-2026-09-30)
- [Constrained metric and grounded facet ranking study: October 2, 2026](#ranking-metric-facets-2026-10-02)
- [Five recommendation experiments and their combinations: October 2, 2026](#ranking-next-five-2026-10-02)
- [Ordinal preferences and interest neighborhoods: September 30, 2026](#ranking-ordinal-interests-2026-09-30)
- [Production ranking review — September 30, 2026](#ranking-production-review-2026-09-30)
- [Publication-era preference signal study — October 3, 2026](#ranking-publication-era-2026-10-03)
- [Ranking synergy follow-up — October 1, 2026](#ranking-synergy-2026-10-01)
- [Tail-head and pairwise ranking tests: October 2, 2026](#ranking-tail-pairwise-2026-10-02)
- [Ranking combinations: strict-v2 verified results, October 3, 2026](#ranking-track-combinations-2026-10-02)
- [Typed retrieval probe: October 2, 2026](#ranking-typed-retrieval-2026-10-02)
- [Reading-experience source pilot — October 2, 2026](#reading-experience-source-pilot-2026-10-02)
- [Next recommendation tests](#recommendation-next-test-plan-2026-10-02)
- [Production recommendation experiments — September 22, 2026](#recommendation-production-study)
- [Recency-weighted recommendation study](#recommendation-recency-study)
- [Recommendation research roadmap: learning what each reader will value](#recommendation-research-roadmap)

## Detailed study records

<a id="recommendation-quality"></a>

## Recommendation quality study

Historical source: `recommendation-quality.md`.

<a id="recommendation-quality--recommendation-quality-study"></a>
### Recommendation quality study

> **Follow-up (2026-09-22):** The [production study](#recommendation-production-study)
> found that the historical evaluator excluded most Goodreads slash-formatted
> read dates. On a later production snapshot, the corrected evaluator produced
> different ranking results. The figures below describe the earlier snapshot;
> do not use them as a current or population-wide quality claim.

<a id="recommendation-quality--production-snapshot"></a>
#### Production snapshot

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

<a id="recommendation-quality--evidence-constraints"></a>
#### Evidence constraints

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

<a id="recommendation-quality--next-experiments"></a>
#### Next experiments

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

<a id="recommendation-quality--neighborhood-scoring-evaluation"></a>
#### Neighborhood scoring evaluation

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

<a id="recommendation-quality--reproduce"></a>
##### Reproduce

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

<a id="already-read-exclusion-audit-2026-10-03"></a>

## Already-read eligibility audit and repair

Historical source: `already-read-exclusion-audit-2026-10-03.md`.

<a id="already-read-exclusion-audit-2026-10-03--already-read-eligibility-audit-and-repair"></a>
### Already-read eligibility audit and repair

<a id="already-read-exclusion-audit-2026-10-03--decision"></a>
#### Decision

Keep conservative work/edition identity matching. Do not add fuzzy suppression
or change recommendation weights based on these cases. Fix the confirmed
identifier-loss defect in manual read recording. The explicit-interest
reranker study remains closed and not adopted.

<a id="already-read-exclusion-audit-2026-10-03--production-evidence"></a>
#### Production evidence

The completed 32-card study contained eight already-read responses. A newer
read-only production capture has 1,802 reads (1,793 rated), one more than the
study snapshot. Seven exact book claims have no recorded matching work, ISBN,
safe title variant, author, or author surname in this current read ledger.
This supports unrecorded history, not a demonstrated alias collision. Without
the original export, an importer omission cannot be distinguished from a book
never included in that export.

The eighth card is a Romeo and Juliet adaptation credited to Shakespeare and
John McDonald. The reader explicitly confirmed Shakespeare's original instead.
The original is also absent. Do not mark the adaptation read or collapse the
two works. Do not derive an enjoyment rating from an already-read response.

<a id="already-read-exclusion-audit-2026-10-03--implemented-fix"></a>
#### Implemented fix

Both Mark read and the saved-list Finished action now carry checksum-valid
catalog ISBNs into the unrated read record. ISBN-10 is canonicalized to ISBN-13.
An existing ISBN and rating are preserved when the action omits a rating;
invalid ISBNs are not stored. The existing same-edition exclusion works across
alternative catalog titles/authors immediately and after rescoring/import.
No scoring weights or text embedding representations change.

Tests cover immediate recommendation filtering, rescoring, unrated records,
and a Goodreads reimport without an ISBN. Further focused cases cover ISBN-10,
existing history, invalid identifiers, and the shared Finished path. All 250
engine tests passed in the primary checkout. Replacing the helper in memory
with the original implementation makes the new ISBN regression fail at the
stored-ISBN assertion; the corrected implementation passes.

<a id="already-read-exclusion-audit-2026-10-03--history-repair"></a>
#### History repair

A private CSV outside Git contains the seven confirmed books plus Shakespeare's
original Romeo and Juliet. Its rating and reading-date fields are empty in the
resulting imported records. A temporary-database import verified all eight
unrated records and confirmed the adaptation is not included. This file has
not been applied to production. Full CSV import or existing Mark read are the
appropriate repair paths; a broader blacklist would affect other readers.

<a id="already-read-exclusion-audit-2026-10-03--enriched-work-identity-follow-up"></a>
#### Enriched work identity follow-up

The newer metadata-aware study checkout also exposes a latent exclusion gap:
verified work IDs in read_metadata are not consulted by raw read identity
indices. In the captured production snapshot, 928 metadata work IDs are fresh
and provider-consistent; 820 corresponding raw reads lack a work ID. No active
candidate in that snapshot matches solely through these metadata identities,
so this gap does not explain the eight study responses. The current primary
checkout predates that metadata schema; do not port its schema merely to fix
manual read ISBN persistence. The metadata-aware study checkout now admits only fresh, provider-consistent
verified work IDs, rejects conflicts with existing raw work IDs, and archives
the admitted identity keys and validation hash for scoring replay. Rated read
inputs and their embedding documents remain unchanged. This fixes a latent
alternate-title exclusion path; it is not evidence that these eight books
were present under aliases. Request decision evidence still captures the
post-filter serving pool rather than a complete exclusion universe.

The three new metadata-aware request/rescore/replay/conflict regressions pass,
and independent review found no material issue. Its broader selected test run
has 151 passes and two unrelated failures from concurrent publication-era
policy assertions still expecting an earlier version. That working checkout
is not claimed to have a fully green suite. The primary checkout's ISBN fix
has a green full suite of 250 tests. Whitespace checks are clean in both.

All book-level response data and import files stay outside Git. No production
writes, commits, pushes, or deployments were performed by this task.

<a id="association-source-identity"></a>

## Association source identity correction

Historical source: `association-source-identity.md`.

<a id="association-source-identity--association-source-identity-correction"></a>
### Association source identity correction

The Open Library and Google Books association adapters now share the same
conservative read-identity rules used by recommendation eligibility. A known
author match uses normalized title/author aliases; a matching provider-scoped
work ID or checksum-valid ISBN can identify the same work across title or
author variations. Work IDs stay within their provider namespace. If the
source result or read history gives no author or the exact `Unknown author`
placeholder, a title-only fallback remains to avoid reintroducing a possible
read. Distinct books with known authors on both sides remain eligible.

This corrects a source-stage false exclusion: a candidate with the same title
as a read but a different known author is no longer discarded solely because
the title matches. The adapters collect optional association evidence and do
not change visible recommendation scores.

<a id="association-source-identity--offline-evidence-and-limits"></a>
#### Offline evidence and limits

An audit of one current production profile compared its 1,801-read ledger with
an 858-work frozen source pool. Five candidates had a normalized title match
to a read but a different author and no matching title/author, work ID, or
ISBN identity. Four remained among 739 candidates that passed the current
policy-eligibility checks; one of those four had a verified description among
the 89-card fresh synopsis pool. None of the five appeared in the 32 completed
fresh judgments.

This establishes a bounded correctness case in the source filter. The fresh
judgments provide no preference evidence for these candidates, and the single
profile audit does not establish a user-quality lift or a result for all
profiles. No scoring or visible source policy is changed.

<a id="discovery-follow-up-protocol-2026-10-03"></a>

## Optional disinterest reasons and fresh interest confirmation

Historical source: `discovery-follow-up-protocol-2026-10-03.md`.

<a id="discovery-follow-up-protocol-2026-10-03--optional-disinterest-reasons-and-fresh-interest-confirmation"></a>
### Optional disinterest reasons and fresh interest confirmation

This protocol separates explanation gathering from model development and
profile-specific confirmation. The work is limited to one reader. It does not
establish that an interest adjustment transfers to other readers or belongs in
a global default.

<a id="discovery-follow-up-protocol-2026-10-03--optional-reason-form"></a>
#### Optional reason form

The default local form covers the four books whose publisher descriptions were
verified and whose follow-up answers were explicitly “not interested.” An
optional expanded copy adds 16 earlier explicit negatives whose displayed text
hash matches a source-verified record. It preserves that earlier card text as
shown; the expanded set is not represented as 20 verified synopses. The form
allows multiple reasons, permits skipping every card, saves only in browser
storage until the user downloads a response file, and does not revise earlier
judgments. “Not now” and “not enough information” are self-reported reasons,
not inferred taste labels. Reason answers do not enter the model.

<a id="discovery-follow-up-protocol-2026-10-03--development-labels-and-score-history"></a>
#### Development labels and score history

The explicit-interest development model was fit from the 30 original pilot
examples with aligned, source-bound vectors: 14 interested and 16 not
interested. The other original responses were unknown or did not have an
eligible aligned vector. The four later publisher-description answers were
kept out of fitting and model selection. They remain a separate diagnostic
instrument. The fresh cards described below are also held out from model
fitting and selection; their optional labels can only describe this reader's
fresh-slate experience.

The development model retains the original 30-example labels and vectors.
Both fresh scoring arms use a separate current, read-only snapshot containing
1,801 reads: 1,792 rated and nine unrated. All 1,792 rated vectors are valid
and match production's document hashes. No production data were changed.

<a id="discovery-follow-up-protocol-2026-10-03--fresh-32-card-sample"></a>
#### Fresh 32-card sample

The source pool has 858 canonical work groups. All 44 earlier exposures,
read identities, current decisions, source/quality eligibility, and saved
progress are checked. Read/prior exclusions leave 812 groups; current policy
screening leaves 739. The original seeded 160-work synopsis batch is preserved,
with its 12 policy-ineligible entries recorded but unfetched.

The 148 bounded source attempts yield 89 identity-checked descriptions with
usable text, 52 missing descriptions, six short descriptions, and one internal
validation interruption after a response that was not retried. No opening
sentence substitution or adaptive replacement was used. Descriptions are
normalized and capped uniformly. Both arms use fresh Qwen3 4b embeddings of
production's full candidate document with exactly the displayed description.
Display text and full scoring document have separately verified hashes.

Before score outputs were examined, selection was frozen to the union of
both arms' top eight, eight largest rank-percentile disagreements, and a
stratified random fill to 32. An independent shuffle determines card order.
This score-informed sample is a descriptive confirmation for one profile;
it does not support an unbiased full-pool AUC estimate.

The form displays only title, author, description, and a random response
token. Scores, sources, strata, model names, and work identifiers stay in a
private map. Interested, not interested, unsure, already read, and unanswered
are distinct. Unsure/unanswered remain unknown; already read is an eligibility
report. Responses remain in browser storage until explicitly downloaded as
`reading-feedback-responses.json`; nothing is automatically submitted.

The paired endpoint is the current base-ranker score before interaction
personalization and slate diversification. The fixed challenger adds a capped
interest correction. Fresh answers never fit or select this challenger, and
one reader's confirmation does not establish benefit for other users.

An independent replay checks all 89 paired score rows and all 32 selected
score-to-card joins. The selected descriptions are independently fetched
again in one bounded 32-request capture, with raw responses archived and exact
work-ID/display-text hashes required before form activation. This is a new
verification capture, not a reconstruction of the original provider responses.
The earlier historical-vector 32-card draft remains a separate diagnostic and
is not shown as the current-history comparison.

<a id="discovery-follow-up-protocol-2026-10-03--private-artifacts-and-verification"></a>
#### Private artifacts and verification

The reason-form, pool-freezing, sample-preparation, and reason-audit tools are
`engine/scripts/build_discovery_reason_form.py`,
`engine/scripts/freeze_fresh_interest_pool.py`,
`engine/scripts/prepare_fresh_interest_sample.py`, and
`engine/scripts/audit_discovery_followup.py`. Their focused tests are under
`engine/tests/`. Private manifests, response files, raw descriptions, source
identities, candidate vectors, work-group lists, and read hashes stay outside
Git. The four-card reason form already served to the reader is preserved
byte-for-byte; the expanded form uses a separate private file.

<a id="discovery-follow-up-protocol-2026-10-03--closed-study"></a>
#### Closed study

The completed 32-response comparison did not improve the first eight. The
interest correction is not adopted; no further labels or tuning are pending
for this proposal. See the reranker study for aggregate outcomes and the
next eligibility/import investigation.

<a id="discovery-followup-scoring-audit-2026-10-03"></a>

## Discovery follow-up scoring audit

Historical source: `discovery-followup-scoring-audit-2026-10-03.md`.

<a id="discovery-followup-scoring-audit-2026-10-03--discovery-follow-up-scoring-audit"></a>
### Discovery follow-up scoring audit

This read-only audit traces four later publisher-description judgments back to
the frozen discovery pool and the scoring snapshot, then checks two bounded
scoring hypotheses against the same reader's chronological rating history. It
is a diagnostic for one reader and four cards, not evidence for a global source
penalty, a title rule, or a production weight change.

<a id="discovery-followup-scoring-audit-2026-10-03--four-card-trace"></a>
#### Four-card trace

All four follow-up items matched the frozen discovery sample, score-eligible
corpus, publisher product URL, ISBN, and stored description hash. The original
blind cards had no verified synopsis. The follow-up descriptions therefore
add traceable catalog evidence, while the original answers remain unchanged.
The exact ranker reproduced all four frozen-pool and corpus scores. Each row
had source weight 1.0 and metadata confidence 0.85; the score range was 54.0 to
61.3 (mean 57.95). Source-neutral scoring moved none of the four scores.
Removing the metadata-confidence shrinkage raised scores by 0.8 to 2.0 points.
Disabling semantic neighborhoods lowered them by 10.8 to 18.1 points. There
was no direct author-term effect for these four cases. Personalization changed
none of their scores, and three appeared in the captured final top eight.

The follow-up arm orderings are descriptive only: current ranking placed three
of the four cases in its top eight (median position 6); source-neutral placed
two (median 11); metadata-neutral placed two (median 15); and the combined
source/metadata-neutral arm placed one (median 31). The four responses contain
only one binary class, so interest AUC is undefined. These ranks cannot measure
whether any arm is better at identifying interested books.

The four records all came from one publisher family. Their source weight was
already neutral, which rules out source weighting as the cause in these cases.
The observed differences instead track metadata-confidence shrinkage and
semantic-neighbor scores. This small set cannot establish that either signal
should change globally.

<a id="discovery-followup-scoring-audit-2026-10-03--historical-same-reader-checks"></a>
#### Historical same-reader checks

The frozen cohort contains 1,777 distinct dated rated works. After a 100-work
warm-up, the audit evaluated 1,677 chronological targets. The current replay
matched the production ranker on all 1,677 scores at served precision, with
zero mismatches. The rating-vector cache was complete for the eligible reads.

Two counterfactuals were evaluated on those same targets:

| Scoring arm | High-rating AUC (4–5 vs. 1–3) | Low-rating AUC (1–2 vs. 3–5) | High-vs-low AUC |
| --- | ---: | ---: | ---: |
| Current | 0.5873 | 0.5799 | 0.6197 |
| Semantic neighborhoods disabled | 0.5504 | 0.5311 | 0.5567 |
| Direct author term disabled | 0.5880 | 0.5815 | 0.6213 |

Disabling semantic neighborhoods changed all 1,677 scores, with mean score
change of −13.902 points and mean absolute change of 13.92 points. The
closed-form zero-vector calculation matched direct ranker scores on 12
chronologically spaced checks. Disabling the direct author term changed 582
scores, with mean absolute change of 0.19 points; its AUC differences were
small (between +0.0007 and +0.0016). The author contribution itself averaged
0.08 points and was nonzero for 579 targets.

The historical targets do not record source weight or catalog confidence, so
their effects cannot be separated or estimated from this retrospective
cohort. These measurements are within-reader, temporally ordered comparisons
on cached embeddings. They are not a cross-user evaluation, randomized test,
or causal measure of expressed interest. The results give no basis for
publisher-wide downweighting or title-specific exceptions.

As a separate development sensitivity, joining the four later negatives yields
27 known binary responses among sampled current-pool cards. Descriptive
interest AUC is 0.57099 for current, 0.50309 for neutral source weights, 0.54938
for neutral metadata confidence, and 0.48765 when both are neutral. The original
answers remain unchanged; the four later answers retain their separate
instrument. Sampling and response availability are selective, several
alternative first-eight cards were not judged, and this is not independent
policy-selection evidence or a full-pool utility estimate.

<a id="discovery-followup-scoring-audit-2026-10-03--reproduction-and-limits"></a>
#### Reproduction and limits

Run the audit with the frozen private artifacts available locally:

```sh
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
  python engine/scripts/audit_discovery_followup.py \
  <frozen-corpus-snapshot.json> <private-discovery-artifact-directory> \
  --output <aggregate-report.json>
```

The inputs are bound by the frozen manifest hash, the audit protocol's source
hash allowlist, and cached-vector content hashes. A changed snapshot or
runtime fails closed; capture a new, explicitly versioned protocol before
using different artifacts. The report contains aggregate counts and scores,
not titles, candidate IDs, or individual judgments. The private input files
and output report must remain outside the repository.

Focused regression coverage:

```sh
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
  python -m pytest -q engine/tests/test_discovery_followup_audit.py
```

<a id="discovery-interest-pilot-2026-10-02"></a>

## Discovery interest pilot — October 2, 2026

Historical source: `discovery-interest-pilot-2026-10-02.md`.

<a id="discovery-interest-pilot-2026-10-02--discovery-interest-pilot--october-2-2026"></a>
### Discovery interest pilot — October 2, 2026

The completed blinded interest form does **not** support deploying the tested
expanded retrieval policy. It does reveal useful new books in a random sample,
but the current scorer did not place those books in the first eight.

<a id="discovery-interest-pilot-2026-10-02--protocol-and-coverage"></a>
#### Protocol and coverage

The source comparison used equal ceilings of 20 public catalog requests per
retrieval arm, at most 50 items per request, and one shared request per second.
The existing source path used six requests and returned no eligible new works;
one request failed. Typed subject/association queries used 16 requests and
returned 494 eligible works. Eleven already existed among the 375 eligible
production candidates, leaving 483 additional works. The existing arm's failure
is a coverage limitation; this capture cannot establish long-term superiority.

All 1,797 library entries, including unrated books, were excluded using the
current identity rules. New works were scored with the existing production read
vectors and policy. The four frozen policies crossed current/expanded pools
with the current slate/a bounded reserved-slot variant. No eligible swap met
the reserved-slot rule, so there were only two distinct lists in this snapshot.

The 44 cards contained the union of policy top-eight books and stratified random
coverage samples. Book order and opaque IDs were fixed before judgments; source
and model identities were hidden. All descriptions were fetched using the same
Open Library work endpoint. Random-sample inclusion probabilities were retained.
The target population is this single reader and these captured pools.

<a id="discovery-interest-pilot-2026-10-02--judgments"></a>
#### Judgments

| Response | Count |
| --- | ---: |
| Interested | 14 |
| Not interested | 18 |
| Unsure / not now | 10 |
| Already read | 2 |

Ten cards had no qualifying synopsis. Nine received an unsure response and one
an already-read response. Missing descriptions and unsure responses are
**unknown**, not negative judgments. Already-read books are ineligible for
future discovery and are not dislike labels. Both already-read reports were
absent from the captured library; they expose incomplete known history rather
than a reproduced identity-exclusion failure.

| Frozen first-eight list | Interested | Not interested | Unsure | Already read |
| --- | ---: | ---: | ---: | ---: |
| Current pool and policy | 4 | 1 | 3 | 0 |
| Expanded pool and current policy | 3 | 2 | 2 | 1 |

The expansion removed one interested and one unknown book and added one
not-interested and one already-read book. Among known, eligible judgments,
interest was 4/5 versus 3/5. Allowing every unknown to be either uninterested or
interested gives current-list interest bounds of 4/8–7/8 and expanded-list bounds
of 3/7–5/7 after excluding the already-read book. These intervals overlap; they
do not justify a policy-quality claim or an automatic rollout.

The random typed-only sample had five interested, three not-interested, and
four unsure responses: 5/8 among known responses, with four unresolved.
This suggests retrieval can surface interesting books, but the sample is small
and description absence is informative. It is not an estimate of all expanded
candidates' utility under a missing-at-random assumption.

<a id="discovery-interest-pilot-2026-10-02--source-and-ranking-follow-up"></a>
#### Source and ranking follow-up

A uniform metadata recovery retry yielded no verified synopsis for the ten
missing cards: Open Library supplied no qualifying synopsis, and all ten Google
Books calls were rate-limited. Production has no Google Books key configured,
matching the retry's authentication setup. A separately bounded publisher
check found exact ISBN/title/author descriptions for four, work-level
descriptions with unresolved editions for two, and no usable matching source
for four. See [the source pilot](#reading-experience-source-pilot-2026-10-02).
The four exact publisher cases already had nonempty production descriptions;
the existing publisher enrichment path reproduced all four descriptions and
matched their ISBNs. The form missed these because it fetched only Open Library
work descriptions. This is a form coverage gap, not evidence that production
publisher enrichment failed. The original answers are preserved; judgments
after seeing recovered text must be a separate follow-up, not retroactive edits
to this pilot.

For a newly frozen form, the card builder now uses one resolver across policy
arms: it first checks the canonical Open Library work description, then may use
the exact-ISBN Penguin Random House description only when the candidate already
has a direct HTTPS product link on an allowlisted publisher host. The source,
identifier, and description hash remain in private evidence files. The blinded
page embeds only the opaque card ID, title, author, and synopsis; it escapes
script-significant `<` characters in the JSON payload and renders text with
`textContent`. This changes future form generation only: the completed 44-card
form and its answers are not regenerated or edited. Any judgments made after
seeing recovered descriptions belong to a separate follow-up dataset.

Pipeline ablations were also scored against the sampled current-pool judgments.
Only 23 current-candidate cards had known binary judgments. Removing source or
metadata confidence changed rankings without supporting a utility improvement;
several ablation top-eight books were never sampled. Those partial, selected
labels cannot establish full-slate precision or select a new production policy.

<a id="discovery-interest-pilot-2026-10-02--limits-and-reproduction"></a>
#### Limits and reproduction

This is one blinded interest judgment per displayed card, not a randomized
production A/B test or a measured reading outcome. Partial top-20 coverage is
reported as unobserved rather than converted to negatives. Some catalog
descriptions were reviews, opening prose, or contained HTML; a later form should
apply stronger display-quality checks consistently across arms before collecting
new answers. Do not alter this completed form's displayed evidence afterward.

Raw books, vectors, opaque card mappings, and judgments remain outside Git in
private local artifacts under `bookward-tracks-20261002/discovery`. Preparation,
blinded-card construction, and analysis are implemented by
`prepare_discovery_judgments.py`, `build_discovery_pilot.py`,
`analyze_discovery_judgments.py`, and `evaluate_pipeline_judgments.py` in
`engine/scripts`. Do not treat a changed input capture as a replay of this pilot.

<a id="embedding-cache-integrity-2026-10-04"></a>

## Embedding cache integrity correction

Historical source: `embedding-cache-integrity-2026-10-04.md`.

<a id="embedding-cache-integrity-2026-10-04--embedding-cache-integrity-correction"></a>
### Embedding cache integrity correction

<a id="embedding-cache-integrity-2026-10-04--finding"></a>
#### Finding

The cache lookup previously accepted an embedding when its content hash
matched, without checking whether the stored BLOB was a usable float32 vector
or agreed with the stored dimension count. Controlled matching-hash fixtures
reproduced reuse of NaN, empty, malformed, and declared-dimension-mismatched
values. The fixture showed the generic reliability failure; no corrupted
production cache entry was observed.

<a id="embedding-cache-integrity-2026-10-04--correction"></a>
#### Correction

`cached_vectors` now reuses a cached value only when its BLOB is nonempty,
float32-aligned, finite, and exactly matches the positive integer dimensions
stored with it. A zero vector remains valid. Invalid matching-hash entries are
treated as missing and regenerated. Provider results are converted to
float32 and validated before any cache transaction, catching overflow such
as finite Python `1e100` becoming float32 infinity. The engine rejects
inconsistent dimensions between valid cached vectors and new provider output
before persisting, instead of silently mixing vector spaces.

The expected dimension also crosses the engine's separate read and candidate
cache calls. A scoring pass establishes it from validated read vectors;
candidate cache hits and newly generated candidate vectors must match before
they can be returned or written. Reading-history scoring uses the same guard.
Forced full-cache rebuilds propagate the freshly generated read dimension to
candidate generation while remaining non-persistent until both sets pass
validation.

<a id="embedding-cache-integrity-2026-10-04--verification"></a>
#### Verification

The focused cache-recovery selection passed: 15 passed, 88 deselected. It covers
the four corrupt-cache forms, zero-vector reuse, cached/new dimension mismatch,
cross-call expected-dimension failures for cached and newly generated
candidate vectors, forced rebuild behavior, float32 overflow, invalid provider
values, and full `score_all` recovery. The end-to-end test records normal
deterministic scores, corrupts one matching-hash cached candidate vector,
reruns scoring, then asserts exact score equality and finite repaired cache
contents. The focused production change passed all 499 engine tests on the
latest main dependency lock, with two existing deprecation warnings. The
separate research archive also passed 512 tests, including 13 benchmark fixtures.

An independent valid-cache parity check covered 1,803 matching read vectors
(including 9 unrated reads) and 549 matching candidate vectors. Every value
was reused exactly and the provider was called zero times. Private receipt SHA-256:
`7852e9cb4336cce68429ac5215992a71e6af419d5b6f880eb4cf0be547214745`.

This is a generic correctness and reliability fix. It has no measured effect
on user preferences, ranking quality, or the case for changing shared ranker
weights.

<a id="enrichment-full-corpus-evaluation"></a>

## Full-Corpus Enrichment Evaluation

Historical source: `enrichment-full-corpus-evaluation.md`.

<a id="enrichment-full-corpus-evaluation--full-corpus-enrichment-evaluation"></a>
### Full-Corpus Enrichment Evaluation

This retrospective study tested whether verified catalog descriptions and subjects improve the current ESS5 recommendation scorer when used to represent a reader's history. The frozen comparison found no enrichment arm that met the preregistered gain criteria, so rich read embeddings and enrichment score blends remain disabled.

<a id="enrichment-full-corpus-evaluation--data-and-matching"></a>
#### Data and matching

The study used a frozen export of 1,797 library reads and 1,934 candidate books. It evaluated 1,777 reads with valid dates and ratings after excluding 9 unrated reads, 9 undated reads, 2 duplicate works, and no reads with a missing or stale current vector. The full capture included 1,590 reads with checksum-valid ISBNs; 928 received a strict Open Library identity match requiring agreement on ISBN, normalized title, and author. The capture returned a nonempty Open Library description for 682 matched records and subjects for 810. Seven additional verified local `Summary` sections brought the total descriptions to 689; their text is not included here. The enriched text retained 803 normalized subject sets, and 581 descriptions met the 300-character quality threshold. Opening sentences were tracked separately and were not treated as full synopses. Provider requests used ISBN only. Ratings, reviews, and read dates were excluded from queries and embedded text; local notes contributed only the seven verified Summary sections. No generated descriptions were used.

The primary held-out validation contains 324 valid-ISBN targets. It uses the full eligible ISBN cohort rather than the earlier 978-record Google-selected subset, which had materially different results and is reported only as a selection-bias diagnostic. A further 309 valid-ISBN targets form the later chronological period. Results on that later period are descriptive only because it had already been inspected before the final all-ISBN evaluation was frozen.

<a id="enrichment-full-corpus-evaluation--comparisons"></a>
#### Comparisons

Every arm used the current production ESS5 ranker, the production `qwen3-embedding:4b` model with 2,560-dimensional vectors, and the same held-out target labels. The fixed whole-day split used 100 warm-up records, 966 development targets, 355 validation targets, and 356 later targets; the primary ISBN cohort is a subset of the latter two groups. The `existing actual proxy` uses exported historical `read_candidate` query vectors, which are mostly title-and-author text. The export has no separate positive serving-score cache artifact, so this is a proxy for the historical serving representation, not proof of the cache that existed at each past date. One stale query hash was re-embedded from the current production query document.

The `corrected-capture query control` leaves read-history vectors title-and-author-only while representing each held-out candidate using its verified available description and subjects. This isolates the effect of using the richer current candidate query. Enrichment arms then compare that corrected control with description-only, subject-only, symmetric full-metadata history, and bounded score-fusion variants. Missing descriptions in the observed-content arm contribute no content vector; they are not silently replaced by title-and-author vectors. A no-description query receives no enrichment blend. The fixed score-fusion weights were 10% and 25%; a single preregistered quality gate and content-expert arm were also evaluated. All arms used confidence 1 to isolate representation changes.

The target is a historical read, not an observed recommendation impression. Chronological whole-day splits prevent the target rating from entering its own ranking context. The primary validation includes 76 high-rated targets, 61 low-rated targets, and 187 neutral targets. The metrics are high-rating AUC (4–5 vs. other ratings), low-rating AUC (1–2 vs. other ratings, reversed so higher is better), and their mean, balanced AUC. A 2,000-replicate whole-target-day bootstrap estimates validation uncertainty. Top-20 and bottom-20 precision are reported as secondary checks.

<a id="enrichment-full-corpus-evaluation--primary-validation-results"></a>
#### Primary validation results

| Representation or control | Balanced AUC | High AUC | Low AUC | Top-20 high precision | Bottom-20 low precision |
| --- | ---: | ---: | ---: | ---: | ---: |
| Existing actual proxy (n=324) | 0.6024 | 0.5519 | 0.6529 | 0.25 | 0.30 |
| Corrected-capture query control | 0.5843 | 0.5518 | 0.6168 | 0.40 | 0.15 |
| 10% score fusion | 0.5808 | 0.5497 | 0.6119 | 0.45 | 0.15 |
| 25% score fusion | 0.5751 | 0.5485 | 0.6018 | 0.45 | 0.15 |
| Quality-gated 25% fusion | 0.5740 | 0.5449 | 0.6032 | 0.40 | 0.10 |
| Observed-content 25% fusion | 0.5771 | 0.5501 | 0.6042 | 0.40 | 0.15 |
| Description-only history | 0.5742 | 0.5711 | 0.5773 | 0.45 | 0.10 |
| Full-metadata history | 0.5462 | 0.5506 | 0.5418 | 0.45 | 0.10 |
| Subject-only history | 0.5239 | 0.5244 | 0.5234 | 0.40 | 0.10 |

The best bounded fusion, at 10%, is 0.0035 below the corrected-capture control in balanced AUC (95% bootstrap interval for the difference: −0.0086 to 0.0014). Its high- and low-rating AUCs are also lower by 0.0021 and 0.0049. The 25% fusion is 0.0092 below the control (95% interval: −0.0217 to 0.0027). The quality-gated arm is 0.0103 below the control, with a 95% interval of −0.0185 to −0.0034. The frozen adoption gate required at least +0.02 balanced AUC against corrected capture, a positive lower bootstrap bound, and no high- or low-rating AUC regression against either control. Later-period checks could veto a severe deterioration but could not select a winner. No arm passes this gate. In fact, every tested arm has lower balanced validation AUC than corrected capture, so reducing the gain threshold to admit modest improvements would not change the decision.

The result is not an endorsement of the query-control uplift over the historical proxy. On all 355 prepared validation targets, the existing proxy scores 0.5884 balanced AUC and the corrected-capture query control scores 0.5726. Among the 309 valid-ISBN targets in the later period, the control scores 0.5430 while 10% fusion scores 0.5448. Other arms also change direction across periods. These variations reinforce that richer catalog text does not automatically improve recommendations and do not justify selecting an arm from the previously inspected later period.

<a id="enrichment-full-corpus-evaluation--serving-metadata-confidence-sensitivity"></a>
#### Serving metadata-confidence sensitivity

A separately preregistered full-pipeline query sensitivity reused frozen vectors and applied the serving metadata-confidence formula with identity confidence fixed at 1.0. Both arms used the same title-and-author history, identities, authors, and source weights; descriptions and subjects matched each arm’s query representation. No embeddings or blend weights were changed.

On the 324 primary validation targets, current-query high/low AUC was 0.5547/0.6608; captured-query AUC was 0.5496/0.6154. Mean metadata confidence increased from 0.4612 to 0.5839, but balanced AUC decreased by 0.0253 (paired whole-day bootstrap 95% interval −0.0680 to 0.0156). Captured queries also decreased balanced AUC by 0.0199 against the earlier neutral proxy (interval −0.0655 to 0.0225). All 355 validation targets likewise showed worse low-rating discrimination. Later, previously inspected targets were mixed across high and low ratings. This exploratory historical proxy sensitivity did not select an enrichment arm.

<a id="enrichment-full-corpus-evaluation--persistence-rehearsal"></a>
#### Persistence rehearsal

An offline replay against a private copy of the production database exercised the actual resolver, strict identity verifier, and metadata persistence. It exposed 337 valid ISBN matches rejected by literal title comparison: every difference was a parenthetical numbered-series annotation. The pipeline now shares the existing conservative full-title identity key, removing only known format annotations while keeping substantive subtitles distinct. No ISBN or author requirement was relaxed.

The corrected rehearsal persisted 743 verified read-metadata records, including 610 descriptions, 707 subject sets, and 742 year-precision dates. Stored field provenance retained the original catalog request timestamps and identified `description` versus `first_sentence`, `subject`, and `first_publish_year` correctly. The source read rows and settings stayed unchanged, and SQLite integrity passed. This rehearsal used only cached public Open Library responses; the seven local summaries were excluded and Google Books was explicitly recorded as skipped rather than given fabricated responses.

A separate read-only test added these revalidated work IDs to the production read-exclusion keys. They caught zero additional books among the 403 active accepted candidates remaining after the existing read filter, so no cached-work exclusion change was adopted.

<a id="enrichment-full-corpus-evaluation--serving-precision-and-limitations"></a>
#### Serving precision and limitations

The production scorer returns Python one-decimal scores. The replay preserved those component values as float64 before weighted fusion and applied Python `round(score, 1)` to each final score before computing metrics. Compared with the earlier NumPy float32 fusion path, the 10% arm's balanced AUC changes from 0.580707 to 0.580800; using unrounded fused scores instead would yield 0.580150. This precision sensitivity does not change the decision.

This is a single-reader retrospective evaluation. Read-as-candidate vectors approximate historical ranking inputs but do not represent a logged set of unread recommendations or user impressions. The strict ISBN match cohort is smaller than the full library, metadata coverage is incomplete, and catalog subject quality varies. A real-time randomized or prospective evaluation would be needed before claiming recommendation lift. The repository contains only aggregate counts and metrics; book identities, provider descriptions, ratings, and private vectors remain outside it.

<a id="explicit-interest-reranker-2026-10-03"></a>

## Explicit-interest reranker development study (2026-10-03)

Historical source: `explicit-interest-reranker-2026-10-03.md`.

<a id="explicit-interest-reranker-2026-10-03--explicit-interest-reranker-development-study-2026-10-03"></a>
### Explicit-interest reranker development study (2026-10-03)

This note records a small offline experiment that tests whether explicit
"interested" / "not interested" judgments can make a bounded adjustment to a
profile's existing recommendation score. It is a reusable, profile-keyed
development tool, not a production feature. The study has one profile, so it
does not establish transfer to other readers or justify global defaults.

<a id="explicit-interest-reranker-2026-10-03--decision-not-adopted-study-closed"></a>
#### Decision: not adopted; study closed

Do not integrate the explicit-interest correction into production scoring or
add a feature flag for it. The fresh comparison did not improve the first
eight recommendations: both arms returned three interested, three rejected,
and two already-read books. The changed slot substituted one rejection for
another. The selected-sample AUC increase is insufficient to justify a shared
ranking change, especially with evidence from only one profile.

Retain the evaluator and private artifacts for reproducibility, not as a
pending deployment candidate. This line of model work is closed; do not run
another tuning or judgment round for this correction. A future proposal must
present a materially different hypothesis and independent evidence.

The four tracks are resolved: the miss audit does not justify removing
semantic similarity; optional reasons remain diagnostic without automatic
preference updates; the bounded interest correction is not adopted; the fresh
comparison is completed and archived. No recommendation formula change ships
from this study.

Next investigate the eight already-read reports as an eligibility/import
problem. Compare identities and import coverage, then fix a demonstrated
cross-user defect or provide durable explicit already-read exclusion if that
capability is missing. Do not fabricate ratings or blacklist these titles for
other readers. That investigation is separate from scorer tuning.

<a id="explicit-interest-reranker-2026-10-03--frozen-method"></a>
#### Frozen method

The evaluator is `engine/scripts/evaluate_explicit_interest_reranker.py` (archived study checkout).
It uses only source-bound binary judgments from the original blinded instrument
for fitting. Unknown, unsure, already-read, and judgments from other
instruments are not negative labels. The four later publisher-description
follow-ups were all negative but were held out from fitting and model
selection; they are reported only as diagnostic development observations.

For each labeled work, the fixed feature vector contains the maximum and top-3
mean cosine similarity to positive examples, the same two summaries for
negative examples, and the top-3 positive-minus-negative difference. Features
for training rows omit every judgment from the same work group. A weighted,
standardized L2 logistic regression uses `C=0.05`; inverse inclusion weights
are normalized to mean one. A predicted probability is an uncalibrated
`interest_probability`, not a probability of enjoyment. The score correction
is `clip(2 * (interest_probability - weighted_training_prevalence), -1, +1)`;
the corrected score is then clipped to the existing 0–100 range.

The fixed diagnostic uses three stratified group-held-out folds (seed
`20261003`), requires at least six training labels in each class and at least
two test labels per class, and reports both inverse-weighted and unweighted
results. The four-fold class-count gate had failed before any metric review;
the frozen, class-count-only amendment selected three folds. No hyperparameter
or feature search was performed. The four follow-up labels were already known
during development and are not independent validation.

The original 44-card pilot contains 14 interested, 18 not-interested, 10
unsure, and 2 already-read responses. Thirty-two were binary labeled; 30 had
usable, exactly aligned vectors (14 interested, 16 not interested). Two
not-interested examples were excluded because their current vectors were
missing or stale. “Source-bound description” means a non-empty description
whose source and content hash were recorded. It does not mean a clean,
semantic synopsis: some original pilot descriptions were reviews, opening
prose, or HTML. No post-hoc text-quality filter was applied to those labels.

<a id="explicit-interest-reranker-2026-10-03--development-results"></a>
#### Development results

On the 30 usable labeled works, group-held-out ROC AUC changed from `0.602679`
to `0.620536` without inclusion weighting (`+0.017857`), and from `0.625550`
to `0.683795` with normalized inverse-probability weighting (`+0.058245`). The
weighted Brier score for the uncalibrated logistic output was `0.254645`.
Fold test counts were 5/5, 4/6, and 5/5 (interested/not interested). These are
small, single-profile development results with wide uncertainty; weighting
does not repair non-random or unknown responses. The unweighted result is
included as a sensitivity check. These OOF baseline scores use the earlier
frozen scoring snapshot with 1,788 rated reads; they are not a current
production-baseline comparison. The separate fresh paired score export will
replay both arms against the lineage-verified current snapshot of 1,792 rated
reads and will report its endpoint and text/vector hashes independently.

A separate retrospective compatibility check applied the fitted interest
adjustment to the profile's historical direct scores. Of 1,777 distinct dated
rated works, 1,677 had an eligible direct historical score; the clear-outcome
subset contained 601 ratings of 4–5 and 344 ratings of 1–2 (neutral ratings
were omitted). AUC on those 945 works changed from `0.619711` to `0.620076`
(`+0.000365`). This is only a retrospective diagnostic: the interest labels and
current embeddings postdate some ratings, the books were selected through
historical reading, and a rating is not an explicit-interest judgment. It is
not a causal forecast, independent validation, or evidence about a live
recommendation slate.

<a id="explicit-interest-reranker-2026-10-03--fresh-paired-comparison-status"></a>
#### Fresh paired comparison status

The completed fresh paired export scores 89 work groups against the same
current snapshot of 1,792 rated reads. All read vectors match production's
existing document representation. Each candidate was freshly embedded with
Qwen3 4b using the deployed `scoring.document` representation and the exact
verified description displayed to the reader. Display-description and full
scoring-document hashes are separate and verified. This compares base scores
before interaction personalization and slate diversification.

From 858 work groups, read/prior-exposure exclusions leave 812, and current
status, source, quality, saved-progress, and identity checks leave 739.
The original seeded 160-work draw was preserved; 148 passed those policy
checks. The bounded catalog fetch accepted 89 descriptions, excluded 52
missing descriptions and six short descriptions, and recorded one internal
validation interruption after a response without retrying it.

Before paired outputs were examined, the selection rule was frozen: union
both arms' top eight with eight largest rank-percentile disagreements, then
stratified random fill to 32. Independent seeded shuffling blinds display
order. The two top-eight sets differ by one book. The fresh response results are recorded below. This targeted sample
supports a descriptive profile-level comparison, not full-pool AUC or global
adoption. Original training text was sometimes noisy; the new verified
synopsis representation may shift the feature distribution.

Private v2 scripts, input bindings, vectors, paired outputs, selection rule,
HTML, and response-token map are retained outside Git. The former historical
32-card draft remains a separate diagnostic: its cached scores reproduced for
only 23 cases, corrected baselines reproduced for all 32, and its selected
descriptions were independently rechecked. Its typed vectors still lack
per-row text provenance, so it is not the active questionnaire.

An independent replay of the current-history comparison reproduces all 89
paired outputs and all 32 selected score-to-card bindings. A separate, bounded
catalog capture archives raw provider responses for the selected 32 cards;
every work ID and displayed-description hash must match before serving the
form. The form's context text is corrected for the current-history run; the
original unserved HTML is preserved. These checks establish reproducibility,
not a measured recommendation-quality improvement.

<a id="explicit-interest-reranker-2026-10-03--fresh-judgments-confirmation-result"></a>
#### Fresh judgments: confirmation result

All 32 exported tokens match the immutable served form and private token map.
The responses contain eight interested, 16 not interested, and eight already
read; none are unsure or unanswered. Already-read responses are excluded from
binary interest metrics and reported separately as eligibility findings.

Both frozen top-eight sets have the same outcome: three interested, three not
interested, and two already read. The sole changed slot replaces one rejected
book with another rejected book. Thus this study finds no improvement in
first-eight utility. Descriptive AUC across the 24 binary-labeled selected
books changes from 0.7109375 to 0.7421875 (+0.03125). This score-informed,
single-profile sample cannot estimate full-pool AUC, cross-user benefit, or
statistical significance. The fresh labels have not refit or tuned the model.

None of the eight user-reported already-read works matches a recorded read
under the current production identity keys, including verified metadata
aliases. This cannot distinguish missing imports from unresolved aliases;
it does not establish a runtime exclusion bug or unread status. A durable
already-read feedback/exclusion path and identity/import investigation are
more actionable than adopting this correction solely for the AUC increase.
The correction is not adopted and this study is closed. The response archive, aggregate report, and eligibility audit remain
private outside Git.

<a id="explicit-interest-reranker-2026-10-03--reproduction-and-provenance"></a>
#### Reproduction and provenance

The canonical run used Python `3.13.14`, NumPy `2.5.3`, and scikit-learn
`1.9.1` from `engine/.venv`. With private, profile-keyed input JSON and a
private NPZ archive containing a finite `vectors` matrix, run:

```sh
engine/.venv/bin/python engine/scripts/evaluate_explicit_interest_reranker.py \
  /path/to/private-input.json \
  /path/to/private-vectors.npz \
  /path/to/private-output.json
```

Inputs and outputs contain private profile and book-level material and belong
outside version control. The evaluator requires `schema_version: 1`, a frozen
`protocol_sha256`, and a unique `profile_id` per profile. It rejects nested
rows whose `profile_id` disagrees with their owning profile, repeated work
groups, work-group leakage, malformed work-group IDs, and score rows that
overlap any original card or follow-up exposure, even if that row had an
unknown, already-read, source-unverified, or other-instrument response. It also
checks explicit prior-exposure and library-read groups. A profile with fewer
than six eligible labels in either class abstains from fitting. The report
always sets global adoption authorization to false.

Canonical private receipt: `/home/steven/.local/share/bookward-tracks-20261002/discovery/explicit-interest-reranker-v1/locked-env-1.9.1-source-bound/run-receipt-private.json`.
The base protocol SHA-256 is
`114310d8dcf804e3861bdad5e5a875b6af17f94b966ed6e8606993450d28bc77`; the
frozen method amendments cover fold feasibility, the locked environment, and
source-quality terminology. The final source-quality amendment SHA-256 is
`735a31e03516bca8a65a082fb6d88509bdcac9385ca4087f3fe1a67038d7d22a`. The
canonical locked-environment result SHA-256 is
`a767fc5a046dc02f460d6462eb5de8c3873856af4f6e57a8c8abe7c7a103436b`; the
retrospective compatibility result SHA-256 is
`a7eb5235b4026be16811f29d1824f831bfd11726beeb04b87d6dd715acb955e7`.
After that receipt, a frozen all-exposure guard amendment
(`exposure-gate-amendment-04-private.json`, SHA-256
`a7c2e6a42045c9498abb549cb2ec89e33db3a363d0123b070ee0535e8e84b764`) bound
the stricter eligibility checks to source hash
`370e8f13e4065c20a26416fecf313e70e6ab2574819e4ada114f2e6036c23d8a` and test
hash `d187997651a40e13ba168f3a83d9f70a055c8c7c95a15e0a32cf614aab0e5de1`.
The frozen development inputs were rerun with these checks; the aggregate
result remained byte-identical at SHA-256
`a767fc5a046dc02f460d6462eb5de8c3873856af4f6e57a8c8abe7c7a103436b`. This
amendment changes only fail-closed row validation and exposure exclusion, not
the fitted model or metrics. The corresponding private rerun receipt is
`exposure-gate-04-run-receipt-private.json` (SHA-256
`eca2429bfdb4d63326597f9365a0cfc2a499f63988b1caba26bb8715eeaaceb4`); its
private rerun output is stored beside it in `exposure-gate-04-rerun/`.

The regression suite is `engine/tests/test_explicit_interest_reranker.py`.
The amended code passed all thirteen tests with warnings treated as errors.

<a id="four-focused-ranking-tests-2026-10-04"></a>

## Four focused ranking tests: October 4, 2026

Historical source: `four-focused-ranking-tests-2026-10-04.md`.

<a id="four-focused-ranking-tests-2026-10-04--four-focused-ranking-tests-october-4-2026"></a>
### Four focused ranking tests: October 4, 2026

<a id="four-focused-ranking-tests-2026-10-04--decisions"></a>
#### Decisions

Keep the production ranking kernel at power 8 and retain existing retrieval
policy. These tests did not establish a preference improvement worth shipping.
Ship the reproduced embedding-cache reliability correction and correct the
Open Library evaluator's identifier parsing. The kernel and retrieval proposals are
closed without adoption; no experimental serving flags or weights were added.
Unknown judgments remain unknown, rather than being counted as dislikes.

<a id="four-focused-ranking-tests-2026-10-04--inputs-and-controls"></a>
#### Inputs and controls

The read-only production snapshot contains 1,811 reads and 1,794 valid rated
vectors. Historical preparation retained 1,780 dated, distinct rated works.
After a 100-row warmup, whole-UTC-day 60/20/20 cuts yielded prefixes of 1,108
and 1,444 works with 336 validation and 336 later targets. Verified work IDs,
valid ISBNs, and exact title-plus-author keys found no target aliases in
training, and no extra duplicates within either target cohort. Historical
outcomes from this one reader are development evidence, not independent-user
confirmation. Current catalog availability cannot establish historical supply.

The unchanged full ranker reproduced all 358 captured serving scores exactly.
The private snapshot SHA-256 is
`a5b38ef82525d7fa127b1938524da46b03ebdd50cf86a85d2607080b9e723737`;
the shared pre-results contract is
`96ec139d1187f3b5e324c154d50c7ba1d582c47bd667dcd282e28b0251c443e7`.
Source began at main `8dba4b3cb8e8f4ba8ea0fc898571c2c9b442d2e5`.
Exact corpora, vectors, raw responses, aligned outcomes, protocols, and
executable study artifacts are retained outside Git. This record contains
aggregate results only.

<a id="four-focused-ranking-tests-2026-10-04--1-retrieval-ceiling"></a>
#### 1. Retrieval ceiling

Today's accepted, enabled catalog across all action statuses contains 2/80
validation favorites and 10/147 later favorites. The current active,
read-excluded 358-item pool contains none of those held-out identities.
Today's statuses can reflect subsequent actions, so this is catalog-presence
and active-pool coverage, not historical retrieval recall.

An oracle diagnostic injected missing favorites using their validated
read-side vectors: 4/80 validation and 4/147 later favorites entered the top
eight; median ranks were 151.5 and 178. Read and candidate representations can
differ. This is neither an actual retrieval treatment nor a measured live
utility gain; it also does not establish retrieval as the dominant bottleneck.
No policy change follows from this diagnostic.

<a id="four-focused-ranking-tests-2026-10-04--2-verified-author-versus-subject-retrieval"></a>
#### 2. Verified author versus subject retrieval

Use the same four earlier favorite seeds at each boundary, with four seed
lookups and twelve expansion requests per arm: 16 successful GETs per arm per
boundary, 64 total in the final corrected run, no errors or retries. Author
retrieval returned 147 eligible works at each boundary, compared with 496 and
495 subject works. Neither retrieved a held-out favorite. Author retrieval
matched one neutral later work; subject retrieval matched one neutral
validation work and one low-rated later work. Both top-eight lists contained
only unknown judgments, and no target was common to both arms, so comparative
AUC is undefined. Pool size alone does not establish recommendation quality.

The first run wrongly rejected Open Library's documented bare author IDs.
The corrected parser recovered all four paired seeds at both boundaries and
reused the original seed identities and order. A correction attempt then
failed before label evaluation because a Compose-only embedding hostname was
unavailable outside Docker. The final run used the local Qwen service and
persisted private raw responses and pools before inference. The investigation
used 16 requests in the invalid parser run, 14–32 in the aborted correction
(exact count lost), and 64 in the final measurement: 94–112 requests overall.
The aborts are not folded into the final equal-budget result.

A separate post-result, descriptive 2×2 reused saved vectors with **no further
HTTP or inference**: subject-only versus a union adding 145 author-only works,
each scored at kernel power 8 and 6. All eight slots remained unknown in every
cell at both boundaries. The union added no known high/low coverage; its one
new later label was neutral. Subject-versus-union top-eight overlap was 3/8
and 4/8 in validation, and 5/8 and 4/8 later, at powers 8 and 6 respectively.
Changes in unknown books cannot confirm or refute a utility improvement.
Retain existing retrieval policy; this comparison is closed without adoption.

The identifier defect was in evaluation code. The production adapter already
accepts bare book IDs. See the corrected scope of the
[earlier typed-retrieval study](#ranking-typed-retrieval-2026-10-02) and the
[official Search API](https://openlibrary.org/dev/docs/api/search).

<a id="four-focused-ranking-tests-2026-10-04--3-embedding-and-kernel-stability"></a>
#### 3. Embedding and kernel stability

With Ollama 0.30.5 and the fixed Qwen3-embedding:4b digest, the same 96 inputs
sent twice using production's 64+32 batching repeated bitwise exactly.
A diagnostic 96-item default-body batch repeated exactly for 94/96 vectors;
explicit 4,096-context calls repeated exactly for 96/96. Archived versus
current vectors differed (mean cosine about 0.998216), but historical batch
composition and effective context are unknown. These differences cannot be
attributed to a runtime cause from this experiment and do not justify rebuilding
production embeddings or changing serving batch/context settings.

Synthetic angular jitter at cosines .999999, .99999, .9999, and .999 produced
mean top-eight overlaps of 8.00, 7.67, 6.90, and 5.37. These are 30 deterministic
replicates per level in the power-8 embedding-fed core, with publication-era
scoring disabled: a sensitivity diagnostic, not observed service noise or a
full-serving noise estimate.

The sole kernel challenger, power 6, kept all other terms fixed. Full-era
fixed-prefix cohort scoring gave:

| Cohort | Power 8 AUC | Power 6 AUC | Favorite precision@8, 8 → 6 | Disliked top-eight count, 8 → 6 |
| --- | ---: | ---: | ---: | ---: |
| Validation, 80 high / 64 low | .59766 | .59844 | .500 → .625 | 0 → 0 |
| Later, 147 high / 53 low | .45097 | .44885 | .250 → .250 | 1 → 1 |

The later AUC delta is −.00212; its paired book-bootstrap interval is
[−.01034, .00608]. Book resampling does not create independent readers.
Unfamiliar-author AUC changed by +.00577 and +.00250. No later top-eight
utility improvement supports adoption. Keep power 8.

<a id="four-focused-ranking-tests-2026-10-04--4-sparse-and-diverse-histories"></a>
#### 4. Sparse and diverse histories

A label-blind hash selected 20 independent readers from 19,146 eligible
readers in [Goodbooks-10k](https://github.com/zygmuntz/goodbooks-10k), pinned to
commit `6dd165b555a7b47b2dd36743a425776e641ff50c`,
[CC BY-SA 4.0](https://github.com/zygmuntz/goodbooks-10k/blob/6dd165b555a7b47b2dd36743a425776e641ff50c/LICENSE).
The dataset is not redistributed. Histories of 0, 5, 20, and 100 works used
the same following 20 targets per reader. Source row order is a proxy:
timestamps are absent. Prolific-reader and popular-book selection limit
representativeness. Global average ratings and popularity were not features.

Correct production read and candidate templates produced 1,873 Qwen vectors
for 1,501 public works. At 100 reads, power 6 increased macro favorite
precision@8 by .00625 (reader-bootstrap interval [0, .01875]) but reduced
high/low AUC by .0268 (interval [−.0653, 0]). Only six readers had both high
and low target ratings. Disliked inclusion was unchanged. Other cutoffs had
zero mean precision gain. The one-reader 100-history replay also regressed:
precision .375 → .250, disliked inclusion .125 → .375. Reject power 6.

Sparse, constant-rating, empty, duplicate, unfamiliar-author, zero-vector,
missing-vector, and invalid-input fixtures passed their documented fallback
or rejection contracts. Those fixtures are not user-outcome evidence.

<a id="four-focused-ranking-tests-2026-10-04--reliability-correction-and-methodology-record"></a>
#### Reliability correction and methodology record

Matching text hashes previously allowed unusable cached BLOBs to reach scoring.
Disposable fixtures reproduced NaN, empty/misaligned bytes, and declared-width
mismatch failures. The fix validates cached vectors, regenerates invalid hits,
rejects float32 provider overflow, and carries read dimensions into candidate
validation across normal scoring, reading-history scoring, and full rebuilds.
Failed rebuilds preserve the old cache. Zero vectors remain valid. All 2,352
valid matching captured vectors were reused exactly without provider calls;
no production cache corruption was observed. This improves reliability and
has no measured preference lift. See
[the focused cache record](#embedding-cache-integrity-2026-10-04).

Corrections were explicit: top-eight retrieval analysis now sorts scorer
outputs; documented bare IDs are normalized; the kernel comparison uses the
common 100-warmup cohorts; and the public benchmark uses separate read and
candidate representations, keeping labels outside ranker inputs. Some
corrections followed provisional results, so corrected measurements are not
presented as untouched confirmation data. No formula grid or seed policy was
chosen from those results. Ranking weight and retrieval decisions are closed.

<a id="four-focused-ranking-tests-2026-10-04--artifact-identities-and-verification"></a>
#### Artifact identities and verification

Public aggregate artifact hashes, retained with the study sources outside Git:

- Retrieval decision note: `49a8ad161e1a20b196594a2bb3dd57c3aca0d755f7acea8b1b160f7daee854a5`.
- Embedding/kernel decision note: `7f248b4e8630008e00113da42fb70634be44b8c298189e51d648d334d6660a65`.
- Independent-reader aggregate: `d79e1feb162f554321b501278462a07ac1d998dd08415504731c345a78d44e56`.

Focused verification: retrieval and combination fixtures 19 passed; stability
fixtures 23 passed; independent-reader fixtures 13 passed; cache selection
15 passed. The cache production change passed 499 engine tests on the latest
main lock; its broader research archive passed 512, including 13 study fixtures.
The documented bare-ID parser regression passed in the 11-test evaluator suite.

<a id="kernel-uncertainty-shrinkage"></a>

## Kernel uncertainty shrinkage

Historical source: `kernel-uncertainty-shrinkage.md`.

<a id="kernel-uncertainty-shrinkage--kernel-uncertainty-shrinkage"></a>
### Kernel uncertainty shrinkage

The serving ranker reduces the rating-neighborhood adjustment when its similarity
weight is concentrated in only a few reads. The selected formula is the ESS5 arm
from the [October 1 synergy study](#ranking-synergy-2026-10-01).

For the existing 40-neighbor, cosine-to-the-eighth-power weights, define effective
sample size as `ESS = sum(weights)^2 / sum(weights^2)`. Multiply the existing
kernel rating adjustment by `ESS / (ESS + 5)`. One dominant neighbor retains
about one-sixth of the adjustment; 40 equally weighted neighbors retain eight-ninths.
This reduces both upward and downward adjustments. The history-size ramp still
applies, and same-work exclusion and deduplication precede the calculation.

<a id="kernel-uncertainty-shrinkage--decision-and-evidence"></a>
#### Decision and evidence

We recommend this as a small reliability correction. Average high/low AUC
improved in validation and the later period, and the change requires no new
metadata or embeddings. The earlier study chose to preserve its baseline;
this follow-up accepts the modest average gain and its documented tradeoff.
The evidence does not establish a live recommendation benefit.

| Period | Previous high / low AUC | ESS5 high / low AUC | Previous / ESS5 average AUC | Previous / ESS5 top-high, bottom-low counts |
| --- | --- | --- | --- | --- |
| Validation, 355 books | .5240 / .5917 | .5244 / .5939 | .5579 / .5591 | 8, 4 / 7, 5 |
| Later, 356 books | .6021 / .5429 | .5988 / .5500 | .5725 / .5744 | 14, 5 / 14, 5 |

ESS5 was selected from the fixed eight-arm author/uncertainty/negative-neighbor
factorial using validation average AUC. The later high-AUC change is −.0033;
low AUC improves .0071. Paired whole-calendar-day 95% intervals include zero:
high [−.0079, .0017], low [−.0021, .0198]. The later period was reused in prior
studies; these are exploratory results from one reader's chosen books.

<a id="kernel-uncertainty-shrinkage--enrichment-blend"></a>
#### Enrichment blend

The exact production read-symmetric 75/25 score blend remains promising, with
validation average AUC +.0021 and later average AUC +.0045 on all targets.
Its second later half loses .0049 average AUC and .0199 low AUC, and its
metadata-covered later bottom-20 low precision falls from .20 to .15.
The blend was examined after constituent outcomes were known and was not the
validation-selected fusion arm.

A matched-score follow-up with ESS5 included does not resolve that concern.
The symmetric blend improves average AUC .0015 on the 191 metadata-covered
validation targets and .0054 across all 356 later targets, but loses .0025
average AUC and .0164 low AUC in the second later half. The closer production
candidate-query text format instead loses .0007 average AUC on validation.
This is another descriptive comparison on the existing arrays, without a
weight search or new independent outcomes.

Serving that blend would also require verified read metadata, a second embedding
view, and the symmetric target representation tested in the experiment.
Current candidate documents use a different text layout. We therefore recommend
retaining the blend for a new evaluation rather than adopting it as the default.

The study's private corpus, embeddings, and reading records remain outside the
repository. Historical study reports retain their original baseline labels;
new evaluator runs use the deployed formula as `current` and retain the previous
unshrunk kernel as a comparator.

The serving implementation and optimized evaluator reproduce all 1,677 archived
ESS5 target scores exactly at the displayed one-decimal precision. Reproducing
an older study's complete grid requires that study's code revision; its archived
`current` label refers to the formula deployed when the study was run.

<a id="production-ranking-pipeline-audit-2026-10-02"></a>

## Complete production ranking pipeline audit — October 2, 2026

Historical source: `production-ranking-pipeline-audit-2026-10-02.md`.

<a id="production-ranking-pipeline-audit-2026-10-02--complete-production-ranking-pipeline-audit--october-2-2026"></a>
### Complete production ranking pipeline audit — October 2, 2026

The audit found a concrete interaction-vector ID mismatch and reproduced the
production pipeline running at capture. Source weights and metadata-confidence
shrinkage substantially affect that recommendation list. The available
historical evidence cannot determine whether removing either improves choices.

The initial audit performed read-only production capture and local replay/tests;
it did not change production behavior, settings, or library rows. Follow-up
implementation fixed the candidate-vector handoff in [PR #127](https://github.com/sgerner/bookward/pull/127)
and preserved online replay evidence in [PR #128](https://github.com/sgerner/bookward/pull/128).
Both are merged and deployed. [PR #130](https://github.com/sgerner/bookward/pull/130)
adds base-score lineage evidence and is merged into the repository; production
deployment is pending. The original capture and ablations below precede these
changes; their counts describe that frozen capture.
Three gpt-6-luna xhigh subagents reviewed replay, telemetry, and code mechanics.
They hit their usage limit after saving the code/history findings; the parent
agent completed and verified the replay implementation and integrated tests.

<a id="production-ranking-pipeline-audit-2026-10-02--capture-and-reproduction"></a>
#### Capture and reproduction

Production was healthy at capture and ran main commit
`5ff2368c618d17f2c5d18edebb955d2221ea457b`, as recorded in the operator capture
notes. The frozen JSON stores the ten runtime source hashes but has no explicit
Git commit field. The commit attribution comes from those capture notes, not
from the frozen export's runtime metadata. The consistent SQLite read-only
transaction was captured at **2026-10-02 09:43:58 Arizona time**
(`2026-10-02T16:43:58.094Z`). It contains:

| Record | Count |
| --- | ---: |
| Reads / valid-rated reads | 1,797 / 1,788 |
| Candidates / sources | 1,925 / 42 |
| Currently eligible recommended books | 375 |
| Full visible pool, including saved/imported | 432 |
| Historical runs / retained impressions | 199 / 12,076 |
| Events / feedback rows | 782 / 134 |
| Recorded outcome rows | 66 |
| Cached embedding rows | 5,978 |

SQLite integrity passed and foreign-key violations were zero. At the original
reproduction, all ten captured ranking-related runtime source hashes matched
the study checkout at commit `5ff2368c618d17f2c5d18edebb955d2221ea457b`. The
capture allowlists only ranking/telemetry data and the embedding model/backend settings;
it excludes credentials and general settings. Raw books, actions, and vectors
remain privately stored outside Git with directory mode `0700` and file mode
`0600`.

The exporter invoked production's **read-only helpers** on the same transaction
to record both request contexts. It did not call the API or
`tracked_recommendations`, which would insert a run. The local in-memory replay
matches every base, personalized, and final ID/order/score/confidence row,
cache membership, and interaction/slate diagnostic: **zero mismatches** for
both 375-row recommended and 432-row all-visible contexts. Exploration is off.

The five fixed arms were recorded before their output was inspected. The
protocol clarification separates stored-score serving from recomputation.
All 1,788 rated-read and all 375 eligible recommended candidate vectors have
valid current content hashes and compatible dimensions. No eligible row was
dropped or re-embedded. Recomputed current scores match all 375 stored scores
at serving precision, with zero score drift. Earlier inspection of a stale
pre-exclusion candidate does not describe the actual eligible cohort.

<a id="production-ranking-pipeline-audit-2026-10-02--five-stage-ablations"></a>
#### Five stage ablations

These comparisons use the same 375 eligible recommended books and production
rounding/order rules. The all-visible replay preserves all 57 saved/imported
rows. Source neutralization removes only the preference-score source term;
metadata neutralization removes score shrinkage and the live confidence scale.
Quality, identity, read/shortlist exclusions, and source enablement remain active.

| Arm | Books with changed scores | Largest score change | Books retained from current top 8 / 20 |
| --- | ---: | ---: | ---: |
| Current | 0 | 0 | 8 / 20 |
| Source contribution neutral | 171 | 4.3 | 6 / 11 |
| Metadata confidence neutral | 272 | 7.2 | 3 / 10 |
| Live interaction adjustment off | 0 | 0 | 8 / 20 |
| Slate selection off | 0 | 0 | 8 / 20 |

Source weights are 1.0 for 204 eligible books and between 0.15 and 0.45 for
171. Neutralization raises the latter scores by an average of about 2.7 points
(mean across all 375 books: +1.2312). These are configured priorities, not
verified differences in enjoyment. Removing them is a substantial policy change.

There are 38 books with current metadata confidence below 0.50, 111 between
0.50 and 0.75, and 226 at least 0.75. Confidence neutralization increases the
low-confidence group's mean score by 3.6026 points, versus +0.4153 and +0.1513
in the other groups. Shrinkage can also raise a below-neutral preference score;
272 changed scores are not 272 increases. The whole-cohort mean absolute
change is 0.9845. Confidence describes available catalog evidence; it is not a
calibrated probability of enjoyment.

The cohort has 55 authors represented in rated history and 320 unfamiliar-author
books. Source neutralization changes 29/55 known-author and 142/320 unfamiliar-
author scores. Metadata neutralization changes 39/55 and 233/320 respectively.
These slices describe mechanisms and coverage, not preference accuracy.

Current author/subject interaction gates qualify no features. The semantic
cache handoff bug below suppresses its usable evidence. The slate selector
finds no qualifying local anti-redundancy swaps in any primary arm. Neither
stage being active in code implies it changes this particular snapshot.

<a id="production-ranking-pipeline-audit-2026-10-02--confirmed-interaction-vector-id-bug"></a>
#### Confirmed interaction-vector ID bug

`load_interaction_events` returns the event primary key as `id`, with the book's
key separately in `candidate_id`. `tracked_recommendations` passes those event
rows alongside candidate rows to `load_cached_candidate_vectors`, which keys
every input by `id`.

This queries candidate embeddings using event IDs. An event ID colliding with
a visible candidate ID also replaces that candidate's document hash and drops
its otherwise valid cached vector. The problem is the handoff, not missing
production history or an embedding-provider failure.

The UI initially requests an overview with a 24-book recommendation prefix and
the saved/imported list. It later requests `status=recommended` for pagination.
Their semantic evidence currently differs because of this bug:

| Context | Current action-book vectors | Corrected lookup | Semantic observations, current → corrected |
| --- | ---: | ---: | ---: |
| Initial overview / all-visible pool | 56 | 132 | 55 → 125 |
| Recommended-only / load-more pool | 0 | 132 | 0 → 125 |

There are 133 distinct action candidates; one lacks the selected-model vector.
The corrected lookup also restores the dropped recommendation vectors: 374→375
for recommended-only and 430→432 for the full visible pool.

The proposed correction normalizes **cache input IDs only** to `candidate_id`.
Original event IDs remain intact for timestamp ties and latest-event semantics.
Both independently reviewed and final replay paths give the same result:

- Two candidates gain **1.0 and 1.4 score points**.
- The top 8, top 20, initial 24, and next 24 recommendations are unchanged.
- 64 rank positions change further down the list; maximum displacement is 63.
  A small score movement can cross many books with rounded-score ties.
- No slate swaps occur. The correction makes semantic evidence consistent
  across the two request contexts but does not establish greater satisfaction.

The five ablations were also rerun with the corrected lookup as a separate
correctness diagnostic. Source-neutral and confidence-neutral top-8/top-20
overlaps remain 6/11 and 3/10. Turning off interaction now removes the two
adjustments; turning off slate still changes nothing. This is not a fitted new
ranking model or a selected sixth challenger.

Relevant deployed-code positions: `main.py:1249–1263`,
`interaction_personalization.py:55–88` (event loading) and `:96–142` (cache
lookup). Existing tests exercised the learner but did not cover this ID handoff.
A small in-memory regression reproduces both the missing action vector and
visible-candidate collision without any network access.

<a id="production-ranking-pipeline-audit-2026-10-02--why-historical-utility-cannot-be-recovered-from-this-capture"></a>
#### Why historical utility cannot be recovered from this capture

The 199 runs declare 16,450 original slots, but only 12,076 impression rows
remain. **4,374 declared slots are unrepresented across 188 runs.** Actual
retained rank numbers preserve those gaps; renumbering would change the
meaning of top-k metrics. Normal source cleanup deletes active candidates, and
the schema cascades candidate deletion into impressions/events/outcomes.
That is a demonstrated mechanism for losing history, although the snapshot
cannot attribute each missing row to a particular deletion.

Of the retained impressions, 551 have a visibility timestamp and only 63 have
any recorded outcome. The 66 outcome rows include 64 explicit-feedback rows
(17 positive, 47 negative, across 61 candidates), plus **one positive and one
negative reading-rating outcome**. Only 19 visible impressions have an outcome.
Missing visibility and missing actions remain unknown. Multiple outcomes for
one impression and repeated runs from one reader do not create new independent
evidence. All retained propensities equal one.

Final served ranks and scores are recorded. Complete eligible candidate pools,
immutable content/vector/profile state, component-stage scores, and per-run
code/model/settings lineage are not. Current candidate rows were updated after
their historical run for 9,356 retained impressions; quality rows were updated
afterward for 10,689. An unchanged timestamp would still not prove every input
was historically available.

A jobs table exists, but job history was not included in this allowlisted
capture. The audit therefore does **not** assert that job timing history is
absent. Completion timestamps alone would not restore overwritten candidate,
profile, and cache versions. Historical stage effects on outcomes are not
identifiable from these records, so no historical AUC or causal lift is assigned
to these five pipeline alternatives.

<a id="production-ranking-pipeline-audit-2026-10-02--recommended-follow-up"></a>
#### Recommended follow-up

1. Fix the cache handoff, preserving original event IDs and testing both
   overview and paginated requests. This is a demonstrated correctness repair;
   immediate top-of-list utility remains unproven.
2. Capture immutable, run-linked candidate/profile/vector versions and stage
   scores, with actual policy/build/model/settings identifiers. Hashes need
   retained, retrievable artifacts rather than overwritten cache entries.
3. Preserve historical exposure evidence through ordinary catalog cleanup,
   or explicitly record missingness/tombstones under the applicable retention
   policy. Do not silently treat surviving rows as a complete original slate.
4. Use fresh blinded choices or prospective outcomes to compare the current
   versus source-neutral/confidence-neutral top lists. Their substantial list
   differences make them worth testing; they do not justify automatic removal.

<a id="production-ranking-pipeline-audit-2026-10-02--validation-and-reproduction"></a>
#### Validation and reproduction

The original capture-matching audit run passed **379 engine tests**, including
14 audit tests. The publication update adds two fail-closed source-hash guard
tests; the focused audit suite now passes **16 tests**. These cover UTC
chronology, unknown outcomes, incomplete historical state, metadata/source
operation order, recomputed SQL ordering, stale-vector coverage, unpromoted new
rows, identity/quality exclusions, ID-based paired comparisons, cache
collision/event-ID preservation, complete source-hash coverage, and explicit
versus unknown capture commit metadata. Existing dependency deprecation warnings
were present in the original run.

The audit scripts and tests are included in this repository revision. The
allowlisted production capture and aggregate artifacts remain private and outside
Git:

- `engine/scripts/audit_production_pipeline.py`
- `engine/scripts/audit_pipeline_history.py`
- `engine/tests/test_audit_production_pipeline.py`
- `engine/tests/test_audit_pipeline_history.py`
- `engine/tests/test_pipeline_mechanical_audit.py`

The following records the original command, run when all ten local runtime
sources matched the 5ff capture. It will fail closed from the current checkout:
merged PRs #127, #128, and #130 changed three of the captured source files. The
current tool requires the exact audit-v1 source-file set and matching hashes,
then exact production-helper parity. It does not offer a hash bypass or a
runtime-root override. Replaying this frozen capture requires the captured
runtime; a fresh capture needs its own complete source hashes and an explicit
commit field if the report should identify a Git commit.

```sh
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 engine/.venv/bin/python \
  engine/scripts/audit_production_pipeline.py \
  /home/steven/.local/share/bookward-pipeline-audit-20261002/corpus-with-reference-private.json \
  --output /home/steven/.local/share/bookward-pipeline-audit-20261002/pipeline-aggregate.json
```

The original report's `5ff` commit came from the operator capture record. Since
the frozen JSON has no explicit commit metadata, the current report generator
returns `unknown` for that field. Audit v1 fixes the ten runtime source filenames
it checks; changing that set requires a separately reviewed protocol version.
The primary ablations preserve the deployed bug for parity; the corrected
lookup is explicitly separate. Implementation defects found in the audit draft
were repaired before final output: an incorrect import, stale tie ordering in
the interaction-off arm, and confidence-slice assignment based on the
neutralized value instead of the original confidence. No model parameters or
experiment grid were changed to improve results.

| Private evidence | SHA-256 |
| --- | --- |
| Consistent corpus and production references | `2c43b50c8eced84ec5265b7b78382bb473bc66478c919e593f370cf4254c38a4` |
| Frozen five-arm protocol | `bc8a86a69bbbcdb5fb7f949993db9ad2de48934aa45e1aa5080a20aa4f776683` |
| Pre-output clarification | `b497ea60167a2225db08474cde34ec3bd250321b77155d78ebc70bce576c5366` |
| Final full-pipeline aggregate | `ffff0d98cc51de4b6cb944fc32cf73c5da1f703b1f62f2c8de82c03627be363f` |
| Final historical coverage aggregate | `b738c17e708ed7eff86140dabbe2505f2b34260c421c67129838b8d6574407f8` |
| Independent cache/mechanics review | `7a4596399876876d2cdfcfcd53f2404d328e3c105f25b4d90acda8536ef80f7b` |

<a id="ranking-cross-profile-evaluation-2026-10-03"></a>

## Ranking evaluation across users

Historical source: `ranking-cross-profile-evaluation-2026-10-03.md`.

<a id="ranking-cross-profile-evaluation-2026-10-03--ranking-evaluation-across-users"></a>
### Ranking evaluation across users

The October 3 follow-up supplies four explicit negative interest judgments from
one reader. These are development labels, kept separately from the original
blinded answers. They cannot establish that a default improves recommendations
across users. The frozen historical ratings also come from one reader;
time periods, genres, and author groups do not turn that reader into multiple
independent users.

`engine/scripts/evaluate_profile_interest.py` evaluates paired scores on one
shared, verified-synopsis pool per independent profile. Its private JSON input
declares a frozen policy SHA256 and either development or confirmation phase.
Each item provides a canonical work ID, explicit judgment, paired baseline and
challenger scores, and verified-synopsis eligibility. The tool rejects duplicate
profiles, duplicate works within a profile, missing paired scores, nonfinite
scores, invalid judgments, and unverified synopsis eligibility. It does not
infer canonical work identity or authenticate the supplied profile IDs; the
capture pipeline must establish both.

The report gives each profile equal weight, regardless of library size. It
reports known-answer AUC separately from first-eight interest bounds that retain
unknown answers. Already-read works are excluded from the slate, not counted as
dislikes. Profiles without both binary classes have no AUC; they remain visible
in the coverage report. Bootstrap intervals resample independent profiles and
are unavailable when fewer than two profiles have both classes. These intervals
are conditional on the frozen challenger, not adjusted for searching policies.

<a id="ranking-cross-profile-evaluation-2026-10-03--validation-and-adoption"></a>
#### Validation and adoption

Unit fixtures cover opposite preferences for the same work, cold starts, sparse
labels, a large and a small profile with conflicting results, and invalid input.
They verify software behavior; they are not empirical multi-user quality tests.
No independent multi-user confirmation dataset is available in this study yet.

A reusable personalized learner can fit within each profile while sharing its
algorithm and safeguards. A change to shared defaults needs a separately frozen
confirmation study with independent profiles, adequate outcome coverage,
per-profile harm checks, and uncertainty appropriate to the sample. A fresh
form answered by the existing reader can test a targeted personal challenger;
it cannot validate cross-user transfer. The evaluator never authorizes automatic
adoption, even when an estimated mean improves.

Private input example:

```json
{
  "phase": "confirmation",
  "policy_sha256": "<64 lowercase hexadecimal characters>",
  "profiles": [{
    "profile_id": "<opaque independent profile ID>",
    "items": [{
      "work_id": "<canonical work ID>",
      "judgment": "unsure",
      "baseline_score": 60.0,
      "challenger_score": 61.0,
      "synopsis_verified": true
    }]
  }]
}
```

Run `engine/.venv/bin/python engine/scripts/evaluate_profile_interest.py
/private/frozen-profile-scores-and-judgments.json`. Keep raw books, vectors,
identities, judgments, and input captures outside Git.

<a id="ranking-embedding-representations-2026-10-02"></a>

## Embedding representation screen: October 2, 2026; strict-v2 rerun October 3

Historical source: `ranking-embedding-representations-2026-10-02.md`.

<a id="ranking-embedding-representations-2026-10-02--embedding-representation-screen-october-2-2026-strict-v2-rerun-october-3"></a>
### Embedding representation screen: October 2, 2026; strict-v2 rerun October 3

The strict-v2 rerun is the primary representation result below. The initial v1 run checked read identity and a nonempty matched work ID but did not verify each description/subject field against the frozen field hash, provider, confidence, and synopsis rules. Its metadata results are retained later as a superseded diagnostic and must not be used for provenance-verified claims.

This is development evidence only. Historical labels were inspected in earlier studies, the 44 fresh blinded judgments were excluded from every representation choice and score, and the target set is a synthetic read-as-candidate pool rather than observed recommendation slates. Strict-v2 verification and the selection rule were frozen before its embeddings and scores were produced.

<a id="ranking-embedding-representations-2026-10-02--strict-v2-field-provenance-result"></a>
#### Strict-v2 field-provenance result

Strict-v2 preserved the original eight arms, representation grids, fold boundaries, fit rules, selection rule, and 4,096-token Qwen runner context. It used the frozen provenance supplement and the corrected verifier: descriptions qualified only as verified synopsis fields with matching provenance; subject fields had to pass provenance and full production-list hash verification at the 12-subject limit, while the encoded representation remained capped at eight normalized subjects. Prepared `opening_sentence` fields were excluded (16 prepared reads, including 15 targets). Before output generation, the strict protocol amendment (`8d6c9062…`), method amendment (`57995112…`), provenance supplement (`3776d54f…`), and verifier function source (`9c89447f…`) were pinned; the 44 fresh blinded judgments remained excluded.

The CLI is strict-only: it requires the strict-v2 amendment, method amendment, and provenance supplement and has no identity-only scoring mode. Its internal identity-only text view is used only to compare the prior cached text hashes and select unchanged vectors; scored metadata is built from the strict field-provenance view. To reproduce the run, set the two roots below to the private artifact locations and use an empty output directory. Keep all inputs and outputs private.

```sh
BW_NEXT_PRIVATE_ROOT=/path/to/bookward-next-five-20261002
BW_FRESH_PRIVATE_ROOT=/path/to/bookward-study-20260930

engine/.venv/bin/python engine/scripts/evaluate_embedding_representations.py \
  --corpus "$BW_NEXT_PRIVATE_ROOT/corpus-private.json" \
  --features "$BW_NEXT_PRIVATE_ROOT/features-private.npz" \
  --feature-manifest "$BW_NEXT_PRIVATE_ROOT/features-manifest-private.json" \
  --protocol "$BW_NEXT_PRIVATE_ROOT/representation-study/track2-protocol-frozen-private.json" \
  --strict-amendment "$BW_NEXT_PRIVATE_ROOT/representation-study/track2-protocol-amendment-strict-v2-private.json" \
  --method-amendment "$BW_NEXT_PRIVATE_ROOT/metric-facets-genre-correction-20261003/metric-facets-method-amendment-private.json" \
  --provenance-supplement "$BW_NEXT_PRIVATE_ROOT/provenance-supplement-private.json" \
  --prior-vectors "$BW_NEXT_PRIVATE_ROOT/representation-study/embedding-vectors-private.npz" \
  --prior-aggregate "$BW_NEXT_PRIVATE_ROOT/representation-study/embedding-representation-aggregate-private.json" \
  --fresh-corpus "$BW_FRESH_PRIVATE_ROOT/corpus.json" \
  --fresh-vectors "$BW_FRESH_PRIVATE_ROOT/options-20260930/option2-fresh-title-author-reads-private.npz" \
  --fresh-manifest "$BW_FRESH_PRIVATE_ROOT/options-20260930/option2-fresh-title-author-manifest-private.json" \
  --private-output "$BW_NEXT_PRIVATE_ROOT/representation-study/strict-v2-reproduction" \
  --bge-cache-dir "$BW_NEXT_PRIVATE_ROOT/representation-study/models"
```

The study retained the earlier vectors wherever both the row and exact formatted text matched. All 1,777 title-author vectors were reused for Qwen and BGE; for the metadata view, Qwen and BGE each reused 1,761 of 1,777 history vectors and encoded only 16 changed rows. In the instructed-query arm, Qwen reused all 1,677 title-author queries and 1,662 metadata queries, encoding only 15 changed metadata queries. TF-IDF was refit within the same causal folds. The strict target pool contains 1,677 rows: 759 had at least one verified metadata field (610 synopsis, 724 verified subject sets, 575 both); 918 fell back to title-author.

| Representation and text view | Direct ranker balanced AUC | Representation Ridge | Current + representation Ridge |
| --- | ---: | ---: | ---: |
| Current Qwen, title-author | 0.5983 | 0.5306 | 0.5360 |
| Current Qwen, verified metadata | 0.5956 | 0.5369 | 0.5451 |
| Instructed Qwen, title-author | 0.5733 | 0.5331 | 0.5409 |
| Instructed Qwen, verified metadata | 0.5703 | 0.5401 | 0.5491 |
| BGE small, title-author | 0.5479 | 0.5346 | 0.5594 |
| **BGE small, verified metadata (selected)** | **0.5446** | **0.5352** | **0.5558** |
| TF-IDF, title-author | 0.5555 | 0.5262 | 0.5510 |
| TF-IDF, verified metadata | 0.5564 | 0.5131 | 0.5390 |

The frozen rule selected BGE small with verified metadata because it had the largest mean three-fold development change for the current-score-plus-representation Ridge relative to current-score-only Ridge: **−0.0298 balanced AUC**. The selected complement scored 0.5477, 0.5983, and 0.5681 in the three folds, versus 0.6024, 0.5872, and 0.6139 for the current-only Ridge. On the pooled 666 development OOF targets, the selected complement scored 0.5558 versus 0.5982 for current-only Ridge; direct replacement scored 0.5446 versus 0.6040 for the served current score. The capped residual scored 0.5996 versus 0.6040. These results do not support shipping a representation change.

The selected-complement paired 95% balanced-AUC intervals were [−0.0837, −0.0002] by UTC day, [−0.0863, +0.0022] by author, and [−0.0810, −0.0039] by 30-day block. The capped-residual interval by day was [−0.0155, +0.0062]. These development-only grouped intervals describe the frozen exploratory procedure; validation (355 targets) and later (356) partitions did not select an arm and remain exploratory.

As a separate execution-matched diagnostic, direct arms were compared with the freshly re-embedded current-Qwen title-author control. Current Qwen with verified metadata was best by mean fold change, at **−0.0004**; its fold changes were +0.0243, +0.0094, and −0.0350. Its paired interval crossed zero by day [−0.0310, +0.0287] and author [−0.0323, +0.0266], and it was not the frozen selection criterion. The same cached title-author vectors were reused: their mean cosine to the earlier fresh-Qwen artifact was 0.999965, while serving replay differed on 1,544 of 1,677 targets (maximum score change 3.9). Strict-v2 measured the Qwen runner context at 4,096 tokens; the prior fresh run did not record its effective runner context, so the cause of the drift remains unresolved. The stored current scorer still reproduced all 1,677 scores exactly.

Strict-v2 has 666 development OOF rows across 612 UTC days, 467 distinct authors, and 82 thirty-day blocks. The selected fixed capped-residual score used train-only standardization and calibration, multiplied the predicted rating residual by 25, capped each fold’s correction at ±min(5, 0.25 × the training current-score SD), then clipped to 0–100 and rounded to 0.1. The first 300 fit-only development targets retain the unadjusted current score; no OOF residual is imputed for them. High-AUC, reversed-low-AUC, favorite precision, disliked inclusion, and NDCG remain descriptive slices, not adoption criteria.

Private row-aligned strict-v2 evidence is stored outside the repository at `/home/steven/.local/share/bookward-next-five-20261002/representation-study/strict-v2/embedding-scores-aligned-private.npz` (SHA-256 `a76a828140eeef6d1a713056038e9e323a189ed72f4a672937614c7f9e63616f`). It carries 1,677 `read_ids`, `record_indexes`, and exactly aligned `train_mask`, `validation_mask`, `later_mask`, and `oof_selection_mask`; `selected_capped_residual_score` is the score key for the parent combination rerun. No row-level IDs, titles, labels, or vectors are included here. The 44 fresh blinded judgments were not read or used, and the study made no runtime or production writes.

<a id="ranking-embedding-representations-2026-10-02--superseded-v1-identity-matched-metadata-diagnostic"></a>
#### Superseded v1 identity-matched metadata diagnostic

The evaluator used the 1,777 prepared rated, dated, distinct reads, with the first 100 reads as warm-up and 1,677 common targets thereafter. All eight v1 arms covered the same rows: current Qwen, query-instructed Qwen, BGE small, and training-only TF-IDF, each with exact title-author text or identity-matched cached metadata text. Missing fields fell back to the exact title-author document. The metadata text itself was identity matched, but field-level source verification was absent; metadata arms and their selection are superseded. The formatter used at most eight normalized subjects and no rating, review, or read-date text.

Development selection used three forward whole-day folds (300–500, 500–700, and 700–966), for 666 OOF targets. The first 300 development targets were fit-only. Validation (355) and later (356) results are separated and did not select the arm. Ridge learners used alpha 10 and training-prefix-only feature standardization. TF-IDF vocabularies and IDF values were refit within each fold using only the earlier text prefix. Arm order was frozen to break ties.

| Representation and text view | Direct ranker balanced AUC | Representation Ridge | Current + representation Ridge |
| --- | ---: | ---: | ---: |
| Current Qwen, title-author | 0.5983 | 0.5306 | 0.5360 |
| Current Qwen, verified metadata | 0.5935 | 0.5364 | 0.5455 |
| Instructed Qwen, title-author | 0.5733 | 0.5331 | 0.5409 |
| Instructed Qwen, verified metadata | 0.5699 | 0.5383 | 0.5483 |
| BGE small, title-author | 0.5479 | 0.5346 | 0.5594 |
| **BGE small, verified metadata (selected)** | **0.5417** | **0.5382** | **0.5581** |
| TF-IDF, title-author | 0.5555 | 0.5262 | 0.5510 |
| TF-IDF, verified metadata | 0.5583 | 0.5098 | 0.5366 |

For reference, the exact stored current score had balanced AUC 0.6040 on these 666 OOF rows; a training-only Ridge calibration of that score had balanced AUC 0.5982. The selected BGE metadata direct replacement scored 0.5417. The selected current-plus-representation Ridge scored 0.5581. The direct score, Ridge-only score, and complement are separate alternatives; the Ridge combination was used only for the frozen representation selection rule.

The selected fixed capped-residual score had balanced AUC 0.6004 versus the stored current score at 0.6040. Its paired whole-day 95% interval for balanced-AUC change was [−0.0146, +0.0074]; high-AUC change was [−0.0092, +0.0155], and reversed-low-AUC change was [−0.0246, +0.0032]. This residual uses train-only standardization and calibration, multiplies the predicted rating residual by 25, caps each fold’s correction at ±min(5, 0.25 × the training current-score SD), then clips to 0–100 and rounds to 0.1. The first 300 fit-only targets retain the unadjusted current score; no OOF residual is imputed for them.

On the pooled 666-row synthetic development target set, favorite precision@8 / disliked inclusion@8 were 0.75 / 0.00 for the stored current score, 0.25 / 0.00 for direct BGE metadata replacement, 0.50 / 0.125 for the selected Ridge complement, and 0.875 / 0.00 for the capped residual. Pooled top-list ties use ascending read ID, matching the first-eight study tie rule. These small pooled top-list summaries are not actual recommendation-slate outcomes.

The selected BGE metadata current-plus-representation Ridge was −0.0402 balanced AUC versus the current-score-only Ridge over the full development OOF rows. Its paired intervals were [−0.0827, +0.0027] by UTC day, [−0.0847, +0.0051] by author, and [−0.0795, −0.0002] by 30-day block. These historical intervals describe sensitivity to grouping; they do not establish confirmatory lift.

<a id="ranking-embedding-representations-2026-10-02--execution-matched-direct-control"></a>
#### Execution-matched direct control

The direct arm scores were also compared fold-by-fold with the freshly re-embedded current-Qwen title-author score, so re-embedding drift is not mistaken for a semantic gain. The best direct alternative by mean development-fold balanced-AUC change was current Qwen with verified metadata, at **−0.0024** versus the fresh title-author control. Its paired 95% interval crossed zero by day [−0.0335, +0.0264] and by author [−0.0346, +0.0245]. This is a diagnostic comparison only and was not substituted for the frozen composite selection.

| Direct alternative vs fresh Qwen title-author control | Fold 1 | Fold 2 | Fold 3 |
| --- | ---: | ---: | ---: |
| Current Qwen, verified metadata | +0.0241 | +0.0092 | −0.0406 |
| Instructed Qwen, title-author | −0.0167 | −0.0072 | −0.0354 |
| Instructed Qwen, verified metadata | +0.0019 | −0.0015 | −0.0704 |
| BGE small, title-author | −0.0476 | −0.0380 | −0.0691 |
| BGE small, verified metadata | −0.0672 | −0.0157 | −0.0912 |
| TF-IDF, title-author | −0.0841 | −0.0030 | −0.0494 |
| TF-IDF, verified metadata | −0.0069 | −0.0524 | −0.0563 |

The study Qwen vectors used the exact title-author `document()` text and matched every read ID in the earlier fresh-Qwen artifact; both runs used model digest `df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907`. Yet their mean vector cosine was 0.999965 and the one-decimal serving replay differed on 1,544 of 1,677 targets (maximum score change 3.9). This shows that small re-embedding differences can shift the serving ranker’s top-neighbor summary; it does not show a representation improvement. The stored current scorer itself reproduced all 1,677 scores exactly. The study runner used context length 4,096, while the [Qwen model card](https://huggingface.co/Qwen/Qwen3-Embedding-4B/blob/main/README.md) describes a 32,768-token model context; the earlier fresh run did not record its effective runner context, so the source of re-embedding drift is unresolved.

In the superseded v1 identity-matched view, at least one description or subject field appeared for 763 of 1,677 targets; the other 914 fell back to title-author. Target-level raw counts were 625 descriptions, 724 subject sets, and 586 with both. These counts do not establish field-level provenance. Prepared-history totals (including the 100 warm-up reads) were 691 descriptions and 793 subject sets. Candidate confidence and source-weight terms were fixed across all representation arms: both were 1.0 for all 1,677 candidates. Therefore, these score differences come from the representation inputs, not a change in candidate confidence or source weighting.

<a id="ranking-embedding-representations-2026-10-02--model-and-input-limits"></a>
#### Model and input limits

The BGE arm used the existing FastEmbed 0.8.1 adapter and the official [BAAI BGE small English model card](https://huggingface.co/BAAI/bge-small-en-v1.5). Its private ONNX artifact was 66,465,124 bytes (SHA-256 `51f1bd0addd6e859e42c2c8021a5e5461385bb676a649f4b269aa445449f2431`); total unique cache size was 67,181,387 bytes, under the frozen 100 MiB limit. FastEmbed reports 384 dimensions and a 512-token input limit. The tokenizer found 0 of 1,777 title-author inputs over 512 tokens and 16 of 1,777 verified-metadata inputs over the limit (maximum 916); the encoder truncates those longer descriptions. No text rule was adjusted after seeing scores.

The Qwen instruction format followed its model card: task instruction on target queries only, with history documents unprefixed. The fixed task was “Given a book a reader enjoyed, retrieve other books with similar likely reader appeal.” The four Qwen embedding calls used Ollama 0.30.5 and together took about 210 seconds on the observed local runner. BGE encoded 3,554 text-view documents in about 131 seconds. TF-IDF was fit separately within five causal cuts for each text view.

<a id="ranking-embedding-representations-2026-10-02--coverage-limitations-and-artifacts"></a>
#### Coverage, limitations, and artifacts

The selected arm’s development OOF familiar/unfamiliar-author splits were small and unstable across folds, so they are retained as descriptive slices in the private aggregate rather than used to qualify the result. The full fold metrics include high AUC, reversed low AUC, favorite precision@8/@20, disliked inclusion@8/@20, and NDCG@8/@20. Whole-day, 30-day-block, and author-group paired resampling used 2,000 replicates.

Private row-aligned evidence is stored outside the repository. The mask-aligned score archive has 1,677 IDs and includes exact `train_mask`, `validation_mask`, `later_mask`, `oof_selection_mask`, and `record_indexes` arrays. `selected_direct_replacement_score` is finite on all 1,677 targets. `selected_current_plus_representation_oof_score` and `selected_representation_ridge_oof_score` are NaN on the first 300 fit-only rows and finite on the remaining 1,377 held-out targets. `selected_capped_residual_score` is finite on all 1,677 targets: it equals the stored current score on the first 300 fit-only rows and uses fold-specific capped residual predictions on the remaining rows. No books, titles, labels, vectors, or candidate IDs are included in this report.

The original study and BGE/integration amendments were frozen before experiment embeddings and scores. The strict-v2 and genre-verification amendments are preserved with the private artifacts; the v1 score archive remains available only for audit and must not be treated as the corrected strict-metadata output. This study did not switch a model or model setting.

<a id="ranking-five-options-2026-09-30"></a>

## Five ranking hypotheses: September 30, 2026

Historical source: `ranking-five-options-2026-09-30.md`.

<a id="ranking-five-options-2026-09-30--five-ranking-hypotheses-september-30-2026"></a>
### Five ranking hypotheses: September 30, 2026

Narrow candidates from five hypothesis families were tested against the private
production snapshot. Actual read enrichment initially received only a coverage
check, not a scored replay. The [methodology audit](#ranking-methodology-audit-2026-09-30)
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

See the [ordinal and interest study](#ranking-ordinal-interests-2026-09-30) for
the first and fourth experiments and the public reproduction command.

<a id="ranking-five-options-2026-09-30--text-views"></a>
#### Text views

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

<a id="ranking-five-options-2026-09-30--source-and-catalog-confidence"></a>
#### Source and catalog confidence

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

<a id="ranking-five-options-2026-09-30--history-availability-correction"></a>
#### History availability correction

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

<a id="ranking-five-options-2026-09-30--discovery-seed-proxy"></a>
#### Discovery seed proxy

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

<a id="ranking-five-options-2026-09-30--scope"></a>
#### Scope

Private snapshots, identity crosswalks, and newly generated vectors remain
outside the repository. Production SQLite access was read-only. No scoring
settings, catalog acceptance gates, or production records were changed.

<a id="ranking-methodology-audit-2026-09-30"></a>

## Ranking methodology audit: September 30, 2026

Historical source: `ranking-methodology-audit-2026-09-30.md`.

<a id="ranking-methodology-audit-2026-09-30--ranking-methodology-audit-september-30-2026"></a>
### Ranking methodology audit: September 30, 2026

The earlier conclusion was too broad. The tested title-only and frozen ordinal
variants did not establish a scorer replacement, but actual read enrichment was
not scored, source and retrieval diagnostics lack power, and an interest model
has a small exploratory tail-utility signal. Those hypothesis families remain
open. This audit uses three independent GPT-6-luna xhigh agents and the frozen
private production corpus; its [aggregate evidence](ranking-methodology-audit-2026-09-30.json)
contains no individual reading records.

<a id="ranking-methodology-audit-2026-09-30--what-was-verified"></a>
#### What was verified

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

<a id="ranking-methodology-audit-2026-09-30--the-missing-actual-enrichment-test"></a>
#### The missing actual enrichment test

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

<a id="ranking-methodology-audit-2026-09-30--corrected-daily-model-metric"></a>
#### Corrected daily model metric

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

<a id="ranking-methodology-audit-2026-09-30--interpreting-the-other-ideas"></a>
#### Interpreting the other ideas

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

<a id="ranking-methodology-audit-2026-09-30--requirements-for-the-next-enrichment-experiment"></a>
#### Requirements for the next enrichment experiment

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

<a id="ranking-metric-facets-2026-10-02"></a>

## Constrained metric and grounded facet ranking study: October 2, 2026

Historical source: `ranking-metric-facets-2026-10-02.md`.

<a id="ranking-metric-facets-2026-10-02--constrained-metric-and-grounded-facet-ranking-study-october-2-2026"></a>
### Constrained metric and grounded facet ranking study: October 2, 2026

This retrospective study tested two bounded changes to similarity scoring: a
low-rank supervised cosine metric and explicit, source-grounded synopsis
facets. It used the frozen next-five protocol and development-only selection.
The target is a proxy based on later recorded ratings for one reader; these
results do not establish live recommendation utility.

On October 3, a provenance-verification defect was corrected and the full
registered metric/facet grid and rich transport were rerun. Production hashes
genre metadata after normalizing up to 12 subjects, while the evaluator had
used the helper's default limit of 8 for the provenance hash. That incorrectly
rejected 402 otherwise valid genre fields among the 1,677 targets. Verification
now uses the production limit of 12; the grounded facet input remains capped at
the first eight normalized subjects. The original corpus, feature archive,
labels, split masks, candidate grid, and selection rule are unchanged. The
selected configurations stayed the same, though facet and joint metrics moved
slightly. The later partition remains previously inspected and exploratory.

The aggregate before/after comparison is:

| Measure | Original evaluator | Corrected verifier |
| --- | ---: | ---: |
| Verified genre fields among 1,677 targets | 322 | 724 |
| Verified synopsis fields among 1,677 targets | 610 | 610 |
| Verified genre fields across all prepared records | 335 | 793 |
| Verified synopsis fields across all prepared records | 675 | 675 |
| Facet mean OOF balanced AUC | 0.594851 | 0.594318 |
| Joint mean OOF balanced AUC | 0.595173 | 0.594871 |
| Standalone validation Δ vs cosine, facets / joint | +0.010973 / +0.012021 | +0.010971 / +0.010859 |
| Standalone later Δ vs cosine, facets / joint | −0.003538 / −0.003602 | −0.004337 / −0.005109 |
| 57-arm master validation Δ, facets / joint | +0.010857 / +0.012168 | +0.011018 / +0.010849 |
| 57-arm master later Δ, facets / joint | −0.003553 / −0.003586 | −0.004368 / −0.005046 |
| Rich validation Δ vs matched control, facets / joint | −0.019305 / −0.019697 | −0.015723 / −0.015978 |
| Rich later Δ vs matched control, facets / joint | −0.001330 / −0.000888 | +0.005246 / +0.004960 |
| 57-arm master selected scorer | `pair_metric_pairwise` | `pair_metric_pairwise` |

The metric-only scores and their deltas are unchanged. Standalone comparisons
use aligned, one-decimal arrays; the master grid uses raw candidate arrays
before its combination export. Serving rounding can change a few tied ranks,
so the two aggregate paths differ slightly. The corrected 57-arm master
selection was saved separately and still chooses the original winner.

The eligible cohort contained 1,677 targets: 966 development, 355 validation,
and 356 in a later period that had already been inspected and is exploratory.
Selection used three expanding, whole-day out-of-fold development blocks
covering development positions 300–966. Every fit and feature vocabulary used
only records before its evaluation cutoff. Models for validation were fit only
before validation; later fits through validation are descriptive. The metric
and facet selection artifacts preserve the split masks and the OOF scores.

The metric fit one ridge direction (alpha 10) inside the first 4 or 8
training-prefix PCA components. The serving similarity is cosine under the
rank-one positive-semidefinite metric `M = I + λvvᵀ`, with λ in {0, 0.025,
0.05}; λ=0 is the exact cosine control. This limits the learned transform to
one direction instead of fitting a free high-dimensional matrix. The
calculation uses float32-normalized serving vectors. An audit corrected the
analytic numerator to use λ; the square-root vector transform uses
`γ = sqrt(1 + λ) − 1`, which induces `2γ + γ² = λ`. Synthetic checks confirm
direct transformed-vector cosine equivalence and unit diagonal. Held-out
ratings are excluded from fit validation and do not affect the fitted
direction, including when held-out labels are poisoned with NaN.

Facet values came only from verified catalog subjects and explicit synopsis
topic or setting-time cues. Each populated value keeps private source-field,
source-reference, and exact-span provenance. Unsupported values stay unknown;
unknown pairs add no agreement and preserve the base cosine exactly. Subject
vocabulary is fit on each training prefix. The score adds a fixed weight times
mean Jaccard agreement over known groups, clips to the cosine range, and tests
weights 0.10 and 0.25. Ratings, reviews, and user notes are not inputs. The
time-cue audit removed bare `future`, `historical`, and `contemporary`: a time
facet now requires explicit temporal-setting phrasing or a named era in
temporal context. This lexical rule and its cue hash were registered before
fits. Literal evidence spans provide traceability, but do not prove that a
phrase was interpreted correctly; synopsis topic cues can still describe
themes rather than the setting.

The registered grid selected these family representatives using only mean
balanced AUC across the three OOF folds:

| Family | OOF-selected configuration | Mean OOF balanced AUC |
| --- | --- | ---: |
| Supervised metric | 4 components, λ=0.05 | 0.6015 |
| Grounded facets | weight 0.10 | 0.5943 |
| Joint metric and facets | 8 components, λ=0.025, weight 0.10 | 0.5949 |

Against the unchanged cosine scorer, the selected metric, facet, and joint
representatives changed validation balanced AUC by +0.0014, +0.0110, and
+0.0109 respectively. In the previously inspected later period, the
corresponding balanced changes were −0.0007, −0.0043, and −0.0051. These are
descriptive comparisons after configuration selection, not promotion tests.
The 57-arm master development lock was rechecked after the correction and
still selects `pair_metric_pairwise`;
the constrained metric and facet families were not selected for standalone
adoption. Recomputed paired validation intervals for the five primary scorer
families each include zero; for the selected master combination,
`pair_metric_pairwise`, the author-clustered interval also includes zero.

The prior-signal combinations use the selected scorer aligned to the current
score scale, then add `0.25 × current-score SD × z(signal)`. The
recency-plus-cluster arm sums separately training-standardized signals. A
protocol-parity fix changed an earlier mean to this required sum. Only the
combination exports were regenerated: the fitted models and raw predictions
were unchanged, and the corrected master lock again selected
`pair_metric_pairwise`.

A bounded transport check used the previously frozen Qwen rich-view archive
for rich query and history vectors. Its 762/1,677 coverage is a
vector-availability count, not a strict-field provenance count for the new
encoder. The matched control used rich queries against the original
title-author history; both replays had zero parity mismatches over all targets.
Primary OOF-selected settings were transported without rich-view tuning. On
validation, the metric, facet, and joint arms changed balanced AUC versus the
matched-query control by −0.0313, −0.0157, and −0.0160. Later exploratory
changes were +0.0064, +0.0052, and +0.0050. This limited, mixed transport
result does not support adding rich views or these arms on its own.

The provenance audit checked identity and value hashes for every consumed
field. Across 1,581 metadata records it found 1,496 field-provenance records;
the eligible target cohort had 610 verified synopsis fields and 724 verified
subject fields after correction, up from 322. Across all prepared records,
675 synopsis and 793 subject fields were verified. The production-equivalent
cosine replay matched on all 1,677 targets.
Synthetic tests cover training-prefix isolation, poisoned held-out labels,
zero and all-negative cosine fallback, metric-transform equivalence, unknown
facet behavior, explicit time cues, and the frozen combination formula.

Private artifacts are identified here by hash only; row-level scores,
identities, vectors, synopsis text, and evidence spans remain in the private
study store.

- Frozen protocol: `8c58739a896acb2d0284f3c51eb5752121ed9262a415ecd313b8f124d7dd935d`
- Base feature archive: `43e2c09b6251b9bb6fee521cb5df565952f2ee9306c3afb3850d51518a88c329`
- Feature manifest: `b31118fa0d4ace9bb0f6b4a9e418ff4a6e416338b2db896b54c8e082f7a698`
- Original pre-genre-correction score archive, preserved: `bad725137b6745202a3fd27be9eb7cb498beafee313c0b86abfe5148736af66d`
- Genre-correction amendment: `5799511207fb055b5322df17b6afacfdefa141bc64001b790e3cff499ddd8256`
- Corrected metric/facet score archive: `93b33de2c27eb01801dc99c86ad9fb1d0112b2e00a7f408cb5f309b741105c29`
- Corrected metric/facet OOF selection: `ed08fa667a5db4d6246d5b24d334ef607eff5b8304ce1b696388229a387d5ef3`
- Corrected private aggregate report: `42e6b320c5a80f507c589c82af49e0d573048c7caf02b63dc6516c33b16c52c1`
- Original master selection, preserved: `ea6ace0631b67fcf770ea27267a5bef0230abe232afa914ca41f57ade2d3ac50`
- Corrected master-grid amendment: `f59679751773e3c96e570e9f5f309f75d7cf5584412b92e424f6e59add318745`
- Corrected 57-arm master selection: `7779739bf96e562d2f580685d52ce81d1591eac7f79dd20e2184c110cd08b6dd`
- Corrected 57-arm master report: `05c7f8e2d894506065c680de9b3311a9749ce16d1dffae890e4a6336bca0d539`
- Rich transport input addendum: `5b92a3527ca0a5e126f31017df40960c4f26607d1fa148348583b578f1e039c2`
- Corrected rich-view score archive: `dc4e9007ba858d3db5d77391b52bf4c714b6487db961abe6cac4fa4c9958c8e1`
- Corrected rich-view aggregate report: `2e8b7af612118684e43d56e7789cfb8726ac6ace93cfcd1a367b066a49fef72b`
- Corrected rich views input: `0ad49c091c0dafff336e0c17e47bfcff7d3b0a91295aa5d83a28e8ac8c646875`

<a id="ranking-next-five-2026-10-02"></a>

## Five recommendation experiments and their combinations: October 2, 2026

Historical source: `ranking-next-five-2026-10-02.md`.

<a id="ranking-next-five-2026-10-02--five-recommendation-experiments-and-their-combinations-october-2-2026"></a>
### Five recommendation experiments and their combinations: October 2, 2026

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
see [the corrected metric/facets report](#ranking-metric-facets-2026-10-02)
for the full before/after comparison.

A separate discovery correctness bug was found, fixed, merged, and deployed
in [PR #126](https://github.com/sgerner/bookward/pull/126): association providers
now receive the complete library, and LibraryThing excludes every known
title/author identity. Previously the runner supplied only 25 favorites, so
providers could rediscover other already-read books.

<a id="ranking-next-five-2026-10-02--data-and-method"></a>
#### Data and method

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

<a id="ranking-next-five-2026-10-02--results"></a>
#### Results

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

<a id="ranking-next-five-2026-10-02--combinations-and-enrichment-controls"></a>
#### Combinations and enrichment controls

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

- [Favorite/dislike heads and pairwise learning](#ranking-tail-pairwise-2026-10-02)
- [Supervised metric and grounded facets](#ranking-metric-facets-2026-10-02)
- [Typed retrieval, seed policies, and diffusion](#ranking-typed-retrieval-2026-10-02)

<a id="ranking-next-five-2026-10-02--correctness-work-and-delivery"></a>
#### Correctness work and delivery

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

<a id="ranking-next-five-2026-10-02--final-evidence-identities"></a>
#### Final evidence identities

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

<a id="ranking-ordinal-interests-2026-09-30"></a>

## Ordinal preferences and interest neighborhoods: September 30, 2026

Historical source: `ranking-ordinal-interests-2026-09-30.md`.

<a id="ranking-ordinal-interests-2026-09-30--ordinal-preferences-and-interest-neighborhoods-september-30-2026"></a>
### Ordinal preferences and interest neighborhoods: September 30, 2026

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

<a id="ranking-ordinal-interests-2026-09-30--methods"></a>
#### Methods

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

<a id="ranking-ordinal-interests-2026-09-30--reproduction-and-limits"></a>
#### Reproduction and limits

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

<a id="ranking-ordinal-interests-2026-09-30--methodology-audit"></a>
#### Methodology audit

The [methodology audit](#ranking-methodology-audit-2026-09-30) corrects the daily
score scale and distinguishes global discrimination from tail utility. The
three-cluster model finds 16 high-rated books in its top 20 and six low-rated
books in its bottom 20, versus 14 and five for current scores. That exploratory
tail signal deserves a prespecified follow-up; worse global AUC does not prove
that every interest-based approach lacks utility. The canonical tail ordering
correction changes none of these observed counts.

<a id="ranking-production-review-2026-09-30"></a>

## Production ranking review — September 30, 2026

Historical source: `ranking-production-review-2026-09-30.md`.

<a id="ranking-production-review-2026-09-30--production-ranking-review--september-30-2026"></a>
### Production ranking review — September 30, 2026

The current formula offers modest ordering of highly rated books and weaker identification of low-rated books. Four families of alternative scoring were tested against the production reader's rated history; none demonstrated a reliable improvement worth deploying. The serving weights remain unchanged. The review improves the evaluation tools and records the evidence so future changes can be compared honestly.

<a id="ranking-production-review-2026-09-30--data-and-protocol"></a>
#### Data and protocol

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

<a id="ranking-production-review-2026-09-30--tested-ideas"></a>
#### Tested ideas

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

<a id="ranking-production-review-2026-09-30--are-high-and-low-scores-useful"></a>
#### Are high and low scores useful?

For the current formula on the 356 later books, high-versus-low AUC excluding three-star books is 0.5953. Fourteen of the top 20 books were rated 4–5 stars; five of the bottom 20 were rated 1–2 stars. The highest score decile (66.0–89.0) averaged 3.69 stars, with 60% highly rated and 8.6% low-rated books. The lowest decile (35.1–49.4) averaged 3.22 stars, with 30.6% highly rated and 16.7% low-rated books. The whole block contains 43.0% highly rated and 15.2% low-rated books.

High scores enrich for highly rated books, but low scores provide little assurance of a dislike. Intermediate deciles are not consistently ordered by mean rating. These are retrospective rank cohorts, not reusable enjoyment probabilities or score cutoffs. Equal scores can cross decile/cutoff boundaries; stable chronological tie-breaking can affect small top/bottom counts.

<a id="ranking-production-review-2026-09-30--performance-check"></a>
#### Performance check

The pure ranker took a median 0.109 seconds for a 431-candidate workload proxy and 0.135 seconds for all 541 hash-valid candidate vectors available to this private benchmark, over three warm runs on the study machine. These timings exclude embedding generation, database access, and live interaction/slate adjustments. Candidate status was not included in the export, so these are workload proxies rather than a reconstruction of production eligibility. Batched sorting and per-call metadata caches showed only roughly 2–4% improvements in small noisy measurements; no performance change was promoted.

<a id="ranking-production-review-2026-09-30--actual-production-use"></a>
#### Actual production use

Production has 190 recommendation runs, 11,478 impressions, and 557 impressions marked visible. All logged propensities are 1.0. Visibility and action linkage are incomplete, so these data cannot identify an unbiased policy effect.

The separate latest save/reject replay has 127 usable candidates: 57 saves and 70 rejects. Current-formula AUC is 0.406; the original pre-neighborhood formula scores 0.705, and cosine centering scores 0.428. This is a warning about transferring the read-book benchmark to discovery, not a reason to revert automatically. Current catalog metadata, source weights, and selected/exposed populations differ, and this replay excludes the live interaction and slate adjustments.

Exact same-run action/impression matches, keeping the latest action per candidate, contain only three saves and 44 rejects. Recorded score AUC there is 0.773 and rank AUC is 0.852; the visible subset has just two saves and 15 rejects. Neither sample supports a strong quality claim. Latest saved candidates also have lower mean source weight (0.410 versus 0.638) and catalog confidence (0.711 versus 0.941) than rejected candidates. Their median description length is 44 versus 118 characters. Source and metadata terms deserve future stratified testing with better exposure coverage; removing catalog safeguards on these confounded observations would be premature.

<a id="ranking-production-review-2026-09-30--limits-and-next-evidence"></a>
#### Limits and next evidence

This is one installation's reader-selected history. Eventual ratings are treated as if they were known on read dates; imports and updates do not retain reliable historical rating-availability times. Cached vectors and current metadata may postdate reads. Held-out books form synthetic time-block groups, not actual candidate slates. The historical periods were already examined during earlier tuning; the usable count is only four higher than the 1,773 in [the September 22 study](recommendation-historical-replay.md), which does not establish a fresh confirmatory cohort. The book bootstrap ignores temporal dependence and cannot establish generalization to other readers.

The next useful study should freeze a new prospective period, record actual candidate sets and policy versions, improve same-run feedback coverage, and compare source/metadata strata. A new scoring term should improve both high and low discrimination without sacrificing safeguards, and should be checked against actual discovery behavior before promotion.

<a id="ranking-publication-era-2026-10-03"></a>

## Publication-era preference signal study — October 3, 2026

Historical source: `ranking-publication-era-2026-10-03.md`.

<a id="ranking-publication-era-2026-10-03--publication-era-preference-signal-study--october-3-2026"></a>
### Publication-era preference signal study — October 3, 2026

<a id="ranking-publication-era-2026-10-03--frozen-protocol"></a>
#### Frozen protocol

This is a fixed, per-reader retrospective test of whether a reader's ratings
vary nonlinearly with a book's **first work-publication year**. It is a ranking
feature experiment, not a reader-preference default. The historical export
contains one reader, so the result cannot establish behavior across users.
Production ranking code is not changed by this study.

Before calculating ranking metrics, publication dates were audited against
field-level provenance. A historical year is usable only when the stored field
comes from Open Library's work-level `first_publish_year` or
`first_publish_date`, its provider ID matches the verified work ID, the
provenance hash matches the current normalized date value, and the publication
year is no later than that read's recorded year. Edition `publishedDate`,
generic `release_date`, and undated or unverified values are unknown. Current
production candidate dates use the same source and hash requirements against
the candidate values in the same database snapshot. A second, explicitly
separate production provenance tier recovers an original year from any
candidate metadata-field ledger row only when its provider and provider ID are
Open Library and the current candidate's verified work ID, the retained source
payload's work key exactly matches that ID, and the payload itself records
`publication_date_source_field` as `first_publish_year` or
`first_publish_date`. The recovered source-payload year does not replace or
reinterpret the candidate's current `release_date`. Conflicting verified years
for one candidate are treated as unknown. This recovery rule was frozen before
ranking metrics were calculated.

Each evaluation target is scored from ratings strictly before its UTC reading
day. The target and all same-day ratings are hidden. The first 100 prepared
reads remain warmup context, as in the frozen ranking cohort; learned year
signals use only previously read works. A profile receives no era adjustment
unless the prior history contains at least 20 provenance-valid publication
years across at least 8 distinct years. A target year must fall within that
prefix's observed minimum and maximum year. These rules make unknown dates,
low-coverage prefixes, and unsupported extrapolation neutral.

The primary nonlinear signal is one fixed Gaussian kernel over publication
year with a 10-year standard deviation. It estimates the local mean rating
near the target year, subtracts the earlier-history mean rating, shrinks by
`effective_sample_size / (effective_sample_size + 50)`, converts one star to
10 current-score points, and clips the correction to ±5 points. Kernel
effective sample size must be at least 3. This single smooth function can
represent multiple high- or low-rated eras; there is no bandwidth or
hyperparameter grid.

The fixed linear recency comparator fits one least-squares rating slope over
the same verified earlier-history years, centers at the prior mean year,
shrinks by `dated_history_count / (dated_history_count + 50)`, and uses the
same score conversion, ±5-point cap, and support checks. Both corrections are
added to the exact current serving score and rounded to one decimal after
clipping to 0–100. Current is the control. Publication year is not used to
infer identity or representation preferences.

One fixed semantic-content-conditioned sensitivity is included in the
historical replay because the prepared, content-hash-verified vectors cover
every target and reproduce the current ranker. For each target, prior verified
reads receive a nonnegative cosine-similarity weight raised to the fourth
power; the fixed 10-year Gaussian year weight is multiplied by it. The joint
weighted rating mean is compared with the semantic-only weighted rating mean.
The joint kernel effective sample size must be at least 3, as must the
semantic-only effective sample size. The same prefix coverage, year-support,
shrinkage prior, 10-point-per-star conversion, and ±5-point correction cap
apply. This one sensitivity has no grid and cannot replace the primary kernel
based on validation results. It is historical only and uses no genre inference
or demographic cues.

The frozen selection evidence is the unweighted mean balanced high/low AUC
across the three existing expanding development folds. The current-only,
linear, and kernel arms are fixed; no settings are selected from validation or
later data. The 355-row validation period is descriptive, and paired 95%
intervals resample complete UTC reading-day groups and author groups with
2,000 replicates (day seed `20261003`, author seed `20261004`). The 356-row later period is exploratory
because these outcomes were inspected in earlier studies. Top/bottom-20
precision, high/low AUCs, temporal halves, unseen-author slices, and score
movement are diagnostics, not separate tuning criteria.

<a id="ranking-publication-era-2026-10-03--frozen-input-and-date-audit"></a>
#### Frozen input and date audit

The historical screen uses the already prepared 1,677-target cohort: 966
development targets, 355 validation targets, and 356 later exploratory
targets. Three expanding development folds and the same-day hidden-row masks
come from the frozen feature manifest. The feature artifact has zero current
ranker parity mismatches at served precision. All 767 target publication years
that passed the provenance test also passed the no-future-publication-year
check; they span 57 distinct years across the full rated history. Six further
provenance-valid read metadata rows have no parseable reading date and are
excluded.

The historical private input identities are checked by the evaluator against
their manifests. An additional read-only live production capture was taken on
October 3 at `2026-10-03T23:48:00.668Z` from one SQLite read transaction in
schema version 20. It reran the production recommendation helpers without
network requests or writes and contains 370 current recommended candidates,
their current serving scores, rated-read metadata, and 377 metadata-field
ledger rows for those same candidate values. The direct current `release_date`
path yields 7 hash-verified Open Library first-publication years. The separately
audited raw-payload tier yields 195 unambiguous work years in total across 93
distinct years (1595–2026); 188 of those use explicit first-publication
evidence preserved on other verified metadata-field rows. No candidate has
conflicting verified years. The 29 generic `source` ledger rows remain
unknown. The private capture is stored outside Git with mode 0600. Only strict
first-publication fields are used for its bounded ranking diagnostic. The
production capture does not include candidate vectors, so its score-only
screen uses the predeclared linear and primary kernel corrections; the
semantic sensitivity remains historical only.

<a id="ranking-publication-era-2026-10-03--results"></a>
#### Results

The frozen protocol was checked before scoring, and the private aggregate was
written outside the repository. The initial aggregate SHA-256 was
`b6822a31abc998529a9c2159154f619a256de713a0fc178d116ad492b2a0a78a`. After
that run, two fail-closed checks were tightened: a source-payload ledger field
must match its enclosing field name, and rated reads later than the production
capture are excluded. The evaluator was rerun to a separate output; its
aggregate SHA-256 is
`81fcb4369e6604d896541342eaa7cd169621ad810411bb2b4c2b82c3449fd516`. Historical
metrics and production score movements are identical across the two runs.

The current arm's mean balanced high/low AUC across the three development
folds was `0.601188`; the fixed kernel was `0.601255`, linear was `0.600982`,
and semantic-conditioned era was `0.601092`. Kernel lost to current in folds
one and two and won in fold three. This small, inconsistent difference gives
no useful development separation.

On the 355-row validation period, current balanced high/low AUC was `0.559129`.
Kernel reached `0.561951` (`+0.002823`), linear reached `0.562026` (`+0.002897`),
and semantic-conditioned era reached `0.561370` (`+0.002242`). For the kernel,
high-rated AUC improved by `0.001516` and reversed low-rated AUC by `0.004130`;
linear gains were `0.001867` and `0.003927`, respectively. All 2,000 UTC-day
and author bootstrap draws were valid. The kernel's paired 95% intervals were
`[-0.000749, 0.006579]` by day and `[-0.000772, 0.006646]` by author. The
linear and semantic-conditioned intervals also crossed zero. Kernel and
linear each reduced top-20 high-rated items by one, without changing the
bottom-20 low-rated count.

The validation kernel difference was positive in both temporal halves
(`+0.001668`, `+0.003651` balanced AUC) and in the 245 targets whose authors
were not in the prior read history (`+0.001964`). Those slices are small
diagnostics, not separate confirmation. The later 356-row period was previously
inspected; kernel, linear, and semantic-conditioned changes were `+0.000921`,
`+0.001233`, and `+0.000461` balanced AUC and remain exploratory only.

The production score-only screen used 822 verified dated rated works and 195
verified candidate first-publication years. Of 370 candidates, 175 had unknown
years and 22 dated candidates fell outside training-year support. Kernel
changed 136 scores by at least 0.1 points (mean absolute change `0.156`, maximum
`0.8`) and changed six top-20 membership slots by symmetric difference (17 of
20 items overlapped). Linear changed 32 top-20 slots (4 of 20 overlapped).
This is counterfactual rank movement on one captured candidate pool, not an
outcome or utility test.

No production adoption is supported. Effects are tiny in development and
uncertain in validation, and this historical data represents one reader.
Although the nonlinear kernel can represent both middle-era windows and
multiple favored eras in controlled tests, the historical screen does not
establish reliable gains across profiles. The aggregate report and ID-aligned
score arrays remain in mode-0600 private artifacts outside the repository;
this study document contains no private book labels or identifiers.

<a id="ranking-publication-era-2026-10-03--reproduction"></a>
#### Reproduction

The evaluator checks the frozen method and every input hash before scoring.
Keep the captures, protocol, and resulting score arrays outside version control:

```sh
OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 \
  engine/.venv/bin/python engine/scripts/evaluate_publication_era.py \
  --corpus /private/corpus.json \
  --provenance /private/metadata-provenance.json \
  --features /private/features.npz \
  --feature-manifest /private/feature-manifest.json \
  --frozen-protocol /private/ranking-protocol.json \
  --era-protocol /private/publication-era-protocol.json \
  --production-capture /private/production-capture.json \
  --output-dir /private/new-publication-era-run
```

The regression suite is `engine/tests/test_publication_era.py`. Its twelve
tests cover opposing profiles, a favored middle-era window, multiple favored
eras, content confounding, missing or edition-only dates, mismatched and
conflicting provenance, malformed dates, same-day hiding, future reads,
unsupported extrapolation, sparse bootstrap draws, and bounded corrections.

<a id="ranking-synergy-2026-10-01"></a>

## Ranking synergy follow-up — October 1, 2026

Historical source: `ranking-synergy-2026-10-01.md`.

<a id="ranking-synergy-2026-10-01--ranking-synergy-follow-up--october-1-2026"></a>
### Ranking synergy follow-up — October 1, 2026

This report summarizes fixed tests of ranking-formula edits, history-signal
blends, metadata representations, and score-level fusion. Several modest
signals remain candidates for a prospectively frozen test, especially the
production read-symmetric 25% fusion. No result supports changing the serving
ranker today. The [aggregate JSON](ranking-synergy-2026-10-01.json) contains
only whitelisted metrics and private-artifact hashes.

<a id="ranking-synergy-2026-10-01--corpus-and-protocol"></a>
#### Corpus and protocol

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

<a id="ranking-synergy-2026-10-01--fixed-history-and-ranker-tests"></a>
#### Fixed history and ranker tests

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
review](#ranking-production-review-2026-09-30).

<a id="ranking-synergy-2026-10-01--metadata-vectors-and-exact-templates"></a>
#### Metadata vectors and exact templates

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

<a id="ranking-synergy-2026-10-01--fixed-7525-score-fusion"></a>
#### Fixed 75/25 score fusion

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

<a id="ranking-synergy-2026-10-01--limits-and-reproduction"></a>
#### Limits and reproduction

Other bounded families remain unresolved. The [five-hypothesis
review](#ranking-five-options-2026-09-30) reports that recent-first seeds
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
For prior detailed results, see the [production review](#ranking-production-review-2026-09-30),
[ordinal and interest study](#ranking-ordinal-interests-2026-09-30), and
[methodology audit](#ranking-methodology-audit-2026-09-30).

<a id="ranking-tail-pairwise-2026-10-02"></a>

## Tail-head and pairwise ranking tests: October 2, 2026

Historical source: `ranking-tail-pairwise-2026-10-02.md`.

<a id="ranking-tail-pairwise-2026-10-02--tail-head-and-pairwise-ranking-tests-october-2-2026"></a>
### Tail-head and pairwise ranking tests: October 2, 2026

Neither the separate tail heads nor the pairwise residual shows a reliable
standalone OOF gain over the current ranker. The primary-view pairwise result is
effectively tied with current; the heads lose materially in both feature views.
Validation gains for the heads are recorded below, but they do not reverse the
OOF evidence or establish a standalone selection win. The root aggregate owns
the separate comparison of cross-family combinations.

The frozen export contains 1,677 eligible reads after the first 100 reads
establish history: 966 development, 355 validation, and 356 later exploratory
targets. Each query hides all reads from its UTC day. The existing ESS5 scorer
reproduced all 1,677 served scores exactly. Development configuration selection
uses only three forward, whole-day OOF folds (200, 200, and 266 evaluation
reads); the first 300 development rows are fit-only and have no OOF predictions.

| Feature view | Current OOF balanced AUC | Tail heads | Pairwise residual | Pairwise change vs current |
| --- | ---: | ---: | ---: | ---: |
| Primary | 0.60119 | 0.55189 | 0.60124 | +0.00006 |
| Rich | 0.59221 | 0.54693 | 0.59151 | −0.00070 |

The selected regularization was `C=0.1` for both families in both views, from
the frozen grid `{0.1, 1, 10}`. The heads' primary OOF mean was 0.55641 high AUC
and 0.54738 low-tail reversed AUC, compared with 0.59677 and 0.60561 for
current. In the rich view, the heads averaged 0.55852 high and 0.53533 low,
compared with 0.59892 and 0.58550 for rich current. Pairwise changes were tiny:
primary high −0.00030 and low +0.00041; rich high −0.00072 and low −0.00068.

The validation point estimates are more favorable for the heads: primary heads
are +0.00759 balanced AUC versus primary current, and rich heads are +0.04613
versus rich current. Rich heads are +0.01397 versus the matched-query control,
which uses the same rich query with the original title-author history. The
pairwise arm is +0.00182 versus primary current and +0.00295 versus rich
current. These are confirmation-period summaries, not a basis for replacing
the OOF selection result; the rich comparisons also keep their three reference
scorers distinct.

The high head predicts `rating >= 4`; the low head predicts `rating <= 2`.
Both logistic models use every training rating, so three-star reads are
negative for both heads and neutral between the two endpoints. Their utility is
`50 + 50 * (p_high - p_low)`, clipped to 0–100 and rounded with Python's
one-decimal serving rule. The probabilities are model outputs, not calibrated
enjoyment likelihoods.

The pairwise model fits a regularized residual around the standardized current
score using every unequal-rating unordered training pair. Its objective is
mean logistic loss plus `0.5 * ||beta||^2 / C`; pair terms are not treated as
independent observations. Features and the current-score offset are fitted from
the training prefix only. At serving time the residual is capped at
`±min(5 points, 0.25 * training current-score SD)`, clipped to 0–100, and
rounded to a tenth.

The exports also include the frozen heads/pairwise 50:50 blend, 25:75 blends
with current, and fixed 0.25-training-SD signal blends with recency, cluster3,
ordinal expected rating, and the recency-plus-cluster3 pair. Signal scalers use
only the applicable training prefix. For the first fold's ordinal scaler only,
an ordinal fit on that fold's training prefix supplies in-sample prefix values;
the missing OOF rows remain missing and are never used as evaluation scores.
The primary pairwise arm's nominal `+0.00006` is the only in-family OOF
exceedance over current; its high-tail point estimate falls while its low-tail
point estimate rises, and the gain is effectively zero at this sample size.
Every other fixed in-family blend is below current in both views. This does
not select or reject combinations with the other experiment families.

The later period had been inspected in earlier work and remains descriptive.
The study is retrospective for one reader and does not establish live
recommendation lift. No individual reading identities or rating rows are
included here. Identity-aligned score NPZs remain in the private study folder
with mode `0600`.

<a id="ranking-tail-pairwise-2026-10-02--reproducibility-hashes"></a>
#### Reproducibility hashes

| Artifact | SHA-256 |
| --- | --- |
| Frozen protocol | `8c58739a896acb2d0284f3c51eb5752121ed9262a415ecd313b8f124d7dd935d` |
| Primary feature NPZ | `43e2c09b6251b9bb6fee521cb5df565952f2ee9306c3afb3850d51518a88c329` |
| Rich feature NPZ | `ac30a796e11ecf7ea01af009645bdd3c8ee2e8cf894e1c1fbe0be22e5e4d0b0e` |
| Tail/pairwise evaluator | `18fffc667006898b981d72bfe1682294bc3a97564fad0163b50cdba5a2e44e8b` |
| Primary score NPZ | `f39ad82420fb023f3cd66b41dbfe6997854f3e221edfc70c8f2bb03757d62ab6` |
| Rich score NPZ | `695c614ea252b1760b51c17f36700e9374d42be0f0ffb234dd5fb0f98619817a` |
| Aggregate JSON | `7c668ed3c7c81f902bfef7e795e1b43672499f97c1a80916e92268f861fb1ca8` |

<a id="ranking-track-combinations-2026-10-02"></a>

## Ranking combinations: strict-v2 verified results, October 3, 2026

Historical source: `ranking-track-combinations-2026-10-02.md`.

<a id="ranking-track-combinations-2026-10-02--ranking-combinations-strict-v2-verified-results-october-3-2026"></a>
### Ranking combinations: strict-v2 verified results, October 3, 2026

The strict-v2 verified reruns supersede the initial identity-only metadata combination results below. With the provenance-verified embedding residual, the four-arm screen selects the top-weighted correction by itself; the old embedding-plus-top-weighted winner is no longer selected. The frozen eight-arm extension selects top-weighted plus the corrected metric residual, but its development gain is very small and its grouped intervals include zero. Neither result supports a production scoring change.

These are retrospective development results on synthetic read-as-candidate pools, not observed discovery slates or independent confirmation. Historical labels had been inspected in earlier studies, and no quantitative adoption threshold was preregistered.

<a id="ranking-track-combinations-2026-10-02--strict-v2-input-and-selection-contract"></a>
#### Strict-v2 input and selection contract

The combination rerun used the strict-v2 selected capped embedding residual and the unchanged selected first-eight top-weighted correction. Metadata descriptions were included only when verified as synopsis fields with matching provenance; genre hashes covered the full production list at limit 12, while representation text remained capped at eight normalized subjects. `opening_sentence` fields were excluded. Strict-v2 reused exact cached vectors where text and model identity matched, re-embedding only the changed metadata rows: 16 prepared history rows and 15 target queries. The 44 fresh blinded judgments were excluded.

The original appeal source gate failed, so appeal-on and appeal-off both equal the current score. The nominal 2×2×2 factorial therefore has four distinct configurations. Active corrections are summed relative to current, share a total cap of `min(5, 0.25 × earlier-training current-score SD)`, then clipped to 0–100 and rounded to one decimal. Selection is the mean of the three fixed development-fold balanced high/low AUCs; ties prefer fewer active factors and then the frozen name order. The old pairwise and cluster-three corrections are references only, excluded from selection. The first 300 development targets remain fit-only.

The verified four-arm protocol hash is `8d4b2e8ed4b43f4716a43508bf837d7dfe68df9d4b37237294170b8affbc909a`; its strict-v2 amendment hash is `d42b988ac7bd09b6cc408b958f2e8e6c95a0b911f1f4b80868f9eada35923e65` and records that the correction was frozen before corrected-v2 outputs. The chosen arm is `top_weighted`, with prediction key `score__top_weighted_pairwise`. The strict embedding input key is `selected_capped_residual_score`.

| Four-arm configuration | Mean development-fold balanced AUC |
| --- | ---: |
| Current | 0.601188 |
| Strict-v2 embedding residual | 0.600566 |
| Embedding residual + top-weighted | 0.601235 |
| **Top-weighted (selected)** | **0.601326** |

The selected top-weighted correction changed balanced AUC by **+0.000139** on mean development folds, **+0.001231** in previous validation, and **+0.000229** in the later partition, compared with current. Its top-eight favorite precision and dislike inclusion matched current in both holdout partitions: 4/8 favorites and 0/8 dislikes in validation; 6/8 favorites and 0/8 dislikes later.

| Selected top-weighted change vs current | UTC-day 95% CI | Author 95% CI | 30-day-block 95% CI |
| --- | ---: | ---: | ---: |
| Previous validation | [−0.001006, +0.003557] | [−0.001071, +0.003546] | [−0.000886, +0.003652] |
| Later | [−0.001736, +0.002336] | [−0.001650, +0.002243] | [−0.001641, +0.002016] |

Each interval is from 2,000 paired group-bootstrap replicates in the canonical aggregate. All include zero. Validation and later results were descriptive and did not select the arm.

<a id="ranking-track-combinations-2026-10-02--strict-v2-eight-arm-extension"></a>
#### Strict-v2 eight-arm extension

The separately frozen extension added the existing development-selected metric correction, using its original current-75/aligned-metric-25 blend as a residual contribution. Its three factors were the strict-v2 selected embedding residual, unchanged top-weighted correction, and corrected selected metric residual; all eight distinct configurations used the same shared cap and original three-fold selection endpoint. The selected configuration was `top_weighted_plus_metric`, with mean development balanced AUC 0.601372 versus current 0.601188.

| Selected top-weighted + metric change vs current | Balanced AUC change |
| --- | ---: |
| Mean development folds | +0.000184 |
| Previous validation | +0.001574 |
| Later | −0.000041 |

Top-eight favorite precision and dislike inclusion again matched current: 4/8 favorites and 0/8 dislikes in validation; 6/8 favorites and 0/8 dislikes later.

| Selected top-weighted + metric change vs current | UTC-day 95% CI | Author 95% CI | 30-day-block 95% CI |
| --- | ---: | ---: | ---: |
| Previous validation | [−0.000677, +0.003884] | [−0.000827, +0.003923] | [−0.000622, +0.004103] |
| Later | [−0.002257, +0.002539] | [−0.002237, +0.002277] | [−0.002291, +0.002040] |

The extension’s selected intervals also come from the canonical aggregate’s 2,000 paired bootstrap replicates; every interval includes zero. The source-binding amendment was metadata-only and added after the initial valid extension run to bind the selected metric artifact to its upstream protocol hash. It did not alter factors, model settings, grid, endpoints, splits, or selection. Independent comparison of initial and verified score archives found identical NPZ keys and arrays, including IDs and all four masks.

The extension protocol froze its three factors, model settings, grid, 75/25 metric blend, shared cap, endpoints, splits, and selection before its original outputs (base SHA-256 `21597f672e1f39ee412e4d5fc367ab85941d57851e4dd82b8ad852ab6cc749b2`). The source-binding amendment was added afterward as a metadata-only guard; it records the upstream selected-metric protocol hash and changes none of those frozen terms. The amendment hash is `8bad26a858905f61a767356998033d7a6ce400d34e60c2f48c3e8729cc4a95c6`; the unchanged, source-bound protocol hash is `5a36ebb2b67b3b02c94bf049f906f8a72808cc387c7b5708135ae734c7e1cfc4`. The bound metric-source protocol hash is `8c58739a896acb2d0284f3c51eb5752121ed9262a415ecd313b8f124d7dd935d`. Selected prediction keys are `selected_capped_residual_score`, `score__top_weighted_pairwise`, and `aligned_metric`.

<a id="ranking-track-combinations-2026-10-02--superseded-identity-only-v1-development-results"></a>
#### Superseded identity-only v1 development results

The initial run lacked field-level provenance verification. Its `verified_metadata` arm label overstated what the input audit established. Preserve these figures only as historical development evidence; they are not evidence for the strict-v2 result.

| V1 configuration | Mean development balanced-AUC change | Previous validation change | Previous later change |
| --- | ---: | ---: | ---: |
| Identity-only embedding residual | +0.000276 | +0.002793 | −0.001793 |
| Top-weighted pairwise | +0.000139 | +0.001231 | +0.000229 |
| **Identity-only embedding + top-weighted (v1 selected)** | **+0.000426** | **+0.004404** | **−0.001601** |

For that superseded v1 combination, the paired UTC-day intervals were [−0.002196, +0.011232] in previous validation and [−0.009538, +0.005574] later. Its development-fold top-eight favorite counts were 4/8, 5/8, and 5/8 versus current 3/8, 5/8, and 4/8; these small synthetic-pool summaries were never actual discovery-slate outcomes. Strict-v2 changes the metadata gate and therefore the selected embedding residual; the v1 embedding-plus-top result must not be substituted for the strict-v2 selection.

<a id="ranking-track-combinations-2026-10-02--private-artifact-hashes"></a>
#### Private artifact hashes

No row IDs, titles, ratings, vectors, or candidate-level scores are published here. Private verified outputs are stored under `/home/steven/.local/share/bookward-tracks-20261002/`:

| Artifact | Private relative path | SHA-256 |
| --- | --- | --- |
| Four-arm frozen protocol | `combination-protocol-private.json` | `8d4b2e8ed4b43f4716a43508bf837d7dfe68df9d4b37237294170b8affbc909a` |
| Four-arm strict-v2 amendment | `combination-amendment-v2-private.json` | `d42b988ac7bd09b6cc408b958f2e8e6c95a0b911f1f4b80868f9eada35923e65` |
| Four-arm verified aggregate | `combinations-strict-v2-verified/aggregate.json` | `3089cf4496cdb61d192be47368ffde39213be859a2c1a553657fbaf094386a22` |
| Four-arm verified score archive | `combinations-strict-v2-verified/scores-private.npz` | `8d98ea4f0a89fa681f74c6f056935c206fc99806a709018ac1344daddf7e0d04` |
| Four-arm verified selection record | `combinations-strict-v2-verified/selection-private.json` | `29277ee6b4b286378a6e3b0c4caae13aaa2bf82c51ab988e5db08c90e7ab0d59` |
| Extension base protocol | `extended-synergy-v2-protocol-private.json` | `21597f672e1f39ee412e4d5fc367ab85941d57851e4dd82b8ad852ab6cc749b2` |
| Extension source-binding amendment | `extended-synergy-v2-protocol-source-binding-amendment-private.json` | `8bad26a858905f61a767356998033d7a6ce400d34e60c2f48c3e8729cc4a95c6` |
| Source-bound extension protocol | `extended-synergy-v2-protocol-sourcebound-private.json` | `5a36ebb2b67b3b02c94bf049f906f8a72808cc387c7b5708135ae734c7e1cfc4` |
| Extended verified aggregate | `extended-synergy-strict-v2-verified/aggregate.json` | `ce8d11200c58095e28d507fd0c2b5b5b827c793b5cff930a8db1a8d2a296d98c` |
| Extended verified score archive | `extended-synergy-strict-v2-verified/scores-private.npz` | `5dd271cac92fc7889f698b8ea54ad6c02c2279a77227ee1e2fcc80ecae3a6d3d` |
| Extended verified selection record | `extended-synergy-strict-v2-verified/selection-private.json` | `eecbe3cd0c242371280b2a1477656d90b1be3ea987a72a92029628f5ea28b9c9` |
| Strict-v2 embedding score input | `../bookward-next-five-20261002/representation-study/strict-v2/embedding-scores-aligned-private.npz` | `a76a828140eeef6d1a713056038e9e323a189ed72f4a672937614c7f9e63616f` |
| Corrected metric/facets score input | `../bookward-next-five-20261002/metric-facets-genre-correction-20261003/metric-facets-scores-private.npz` | `93b33de2c27eb01801dc99c86ad9fb1d0112b2e00a7f408cb5f309b741105c29` |
| Feature artifact | `../bookward-next-five-20261002/features-private.npz` | `43e2c09b6251b9bb6fee521cb5df565952f2ee9306c3afb3850d51518a88c329` |
| Feature manifest | `../bookward-next-five-20261002/features-manifest-private.json` | `b31118fa0d4ace9bb0f6b4a9e418ff4a6e416338b2db896b54c8e082d4f7a698` |
| First-eight score artifact | `first-eight/scores-aligned-private.npz` | `2e39141f3b714b190057fb5f451183dcbc6ceffaaa17e8fe6637e5e1154693c0` |

These experiments did not adopt or deploy a scoring-policy change.

<a id="ranking-typed-retrieval-2026-10-02"></a>

## Typed retrieval probe: October 2, 2026

Historical source: `ranking-typed-retrieval-2026-10-02.md`.

<a id="ranking-typed-retrieval-2026-10-02--typed-retrieval-probe-october-2-2026"></a>
### Typed retrieval probe: October 2, 2026

The author-ID absence conclusions are superseded by the Search-ID parser
correction below. The captured subject-pool observations remain recorded.

The bounded current-catalog probe found that verified author/subject expansion
can produce a large pool of quality-gated candidate works under a small public
request budget. It found no evidence that those pools cover the reader's future
favorite or disliked books, and the unchanged ranker's top 20 surfaced none of
those targets. Pool size by itself is not utility evidence, so this result does
not support a production change.

The study used the frozen private corpus, protocol, and feature cuts. Each
retrieval arm received a ceiling of 20 GET requests at each of two whole-day
boundaries, with at most 50 response items per GET, a shared global rate of one
GET per second, and no automatic retries. The run stopped when an arm's
predeclared seed lookups and expansions were exhausted; it did not send filler
requests. Baseline and typed arms used the same strict-prefix current query at
each boundary. The recent and mixed arms used four distinct, earlier, 4–5-star
seeds. Diffusion used only the typed-current capture and added no requests.

| Arm | Validation GETs | Qualified works | Works / GET | Later GETs | Qualified works | Works / GET | Unseen author-name works, validation / later |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Existing list provider (`list_current`) | 6 | 0 | 0.0 | 6 | 0 | 0.0 | 0 / 0 |
| Typed current seeds | 16 | 496 | 31.0 | 16 | 495 | 30.9 | 487 / 484 |
| Typed recent seeds | 17 | 485 | 28.5 | 4 | 0 | 0.0 | 476 / 0 |
| Typed mixed seeds | 17 | 439 | 25.8 | 10 | 241 | 24.1 | 431 / 234 |
| Diffusion, alpha 0.6 | 0 extra | 496 | — | 0 extra | 495 | — | Same pool as typed current |

The current-seed policy had the highest qualified-work yield at both
boundaries. Typed pools were almost entirely shared-subject edges. Validation
had 2/641 same-author/shared-subject edges for current, 0/715 for recent, and
6/613 for mixed; the later boundary had 2/639, 0/0, and 6/248. No explicit
series edges were verified. The later recent arm resolved four seed works but
none had a verified author key, so it stopped after its four seed-search GETs.
Missing author IDs were counted rather than inferred from names.

The unseen-author work count compares normalized author names to the earlier
read history; it is a coverage proxy, not verified unique-person identity.

Candidate gates required a canonical Open Library work ID, a trusted public
source URL, a passing local title/author quality check, no identity conflict,
and no match to the strict-prefix read history. Known first-publication dates
after the boundary were excluded; unknown and year-only same-year dates stayed
eligible and were counted. One validation mixed-arm candidate was excluded as
known post-cutoff. Among eligible candidates, publication dates were unknown
for 2 validation current-seed works and 1 later current-seed work. These are
current catalog metadata checks: Open Library's present-day record cannot
reconstruct what was available on the historical read date.

The existing list-provider baseline returned no qualified works and had one
`list_items` HTTP status failure at each boundary. Its empty pool is therefore
not a clean estimate of a functioning list source. The typed current arm used
four seed searches and twelve subject requests at each boundary, with no HTTP
failures. Recent and mixed used up to four seed searches, one author expansion
when a verified author key existed, and up to twelve subject requests; their
actual request counts are shown above. All requests and failed requests count
against the arm's ceiling.

Retrieval was frozen before consulting future ratings. The fixed future-rated
cohort contained 355 validation targets (84 high, 69 low, 202 neutral) and 356
later targets (153 high, 54 low, 149 neutral). Candidate matching used verified
Open Library work IDs, ISBNs, or exact title-plus-author identity; title-only
matches were prohibited. The first replay used work IDs already present on the
prepared read rows. A separate identity sensitivity joined `read_metadata`
only where its `identity_hash` exactly matched the original read under the
production identity-hash function, retained original work IDs, and added 820
source-verified cached Open Library work IDs to prepared reads; no ID conflicts
were skipped. This removed no additional already-read candidate from any saved
pool. With those IDs, validation overlap remains zero high and low targets, with
one neutral target in typed-current and diffusion. Later overlap is zero high,
one low, and zero neutral targets in typed-current and diffusion; the other
arms have no target overlap. Thus the pool still retrieves no future favorites,
but the identity-complete pool sensitivity does find one future low-rated
target. No fold has both a matched high and low outcome, so the target-vector
AUC remains undefined. These identity joins do not depend on candidate text.

The saved ranker replay retained only aggregate top-20 counts, so its original
zero high/low result cannot establish the position of that newly matched low
target. A local compatible-text replay of only the later 495-work typed-current
pool used the frozen encoder and fixed ranker; its candidate-text fingerprint
matched the saved compatible replay exactly. The low target ranked 166 of 495,
scored 53.3, and was at the 66.6th best-score percentile ((495 - rank) / 494),
outside the top 20.
This rerun embedded 494 unique candidate texts locally and made no public HTTP
requests. Its full ranks and candidate vectors are saved privately for audit.
A separate read-only check of the 1,934 current production candidates found no
additional already-read matches from these cached IDs.

The prior enriched-text sensitivity is retained only as a legacy projection,
not evidence. A post-run audit found that the evaluator populated its
description field from `description` or `first_sentence`, and could stringify
Open Library's `{type, value}` description object. The saved captures contain
only flattened description strings and omit the original catalog field shape
and provenance, so the projection cannot be safely reconstructed. No new
catalog requests or embeddings were made to revise it. The corrected helper
accepts only a description string or a string `description.value`; it never
uses `first_sentence`.

The compatible title-author candidate-text replay used the frozen local
`qwen3-embedding:4b` model (2,560 dimensions; digest
`df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907`). The
saved capture was replayed without more catalog requests. The current ranker
and query vectors were fixed across arms. The later partition has been
examined in prior work and is exploratory; this remains one retrospective
reader's future-rated target cohort, not an evaluation of all unseen books or a
live recommendation outcome. The old run's 1,998 distinct candidate-text
embeddings describe its workload only; they do not validate the legacy
enriched-text sensitivity. The 1,000-candidate scoring cap covered every
captured pool; the largest pool had 496 works.

For any follow-up runtime comparison, preserve the four-seed current policy as
the pool-yield reference, but do not promote it from this result. Pass the
provider the full strict-prefix read history so its own seed policy and
already-read exclusion see all earlier reads; pre-truncating to 25 favorite
seeds would change recent/mixed coverage and exclusion behavior. Keep absent
author and series metadata unknown. A useful next probe needs reliable seed
author IDs and enough verified identity overlap with relevant outcomes to
measure high/low target coverage. No runtime or production data was changed in
this study.

Private artifact identities:

- Frozen protocol: `8c58739a896acb2d0284f3c51eb5752121ed9262a415ecd313b8f124d7dd935d`
- Corpus: `0973bf5e029f1ee0f2a6ad8957313e64da639b813e20d31491a9ba4b8558eeb9`
- Exact captured pools: `f631848e42ef9ca1552332d24f2ce674d49b7b30e0e665624beab3056a0a707e`
- Aggregate retrieval report: `24357479913825fd7b65003d9871a8a180c1b3de6c27b7c7fc3d42dd9480435f`
- Fixed-ranker replay: `7ea169674bea086330117d0dac718dd9ac103cbc950e51b74794782c243f9aec`
- Cached-work-ID identity audit: `b977ae0d224664bdc97232de5e59a41836ecf63652b1fd30c219fb6c22d7c666`
- Later typed-current compatible full-rank replay: `33e3109c36f4f1ad532d69e62b34a230d61d34645fa1ffb9eda77f34c551c030`
- Later typed-current compatible candidate vectors: `d6a9f5a0aa2116224e13619ba3a22917aa0c3db87ad27bbf849a8f66df8c026a`

<a id="ranking-typed-retrieval-2026-10-02--search-id-parser-correction"></a>
#### Search-ID parser correction

The October 4 audit found that this evaluator's shared author/work parsers
accepted only path-prefixed keys. The official [Search API response
example](https://openlibrary.org/dev/docs/api/search) also uses bare identifiers
such as `OL26320A`. Those author keys were discarded, so the earlier
missing-author-ID counts cannot establish catalog absence. The mostly
subject-based captures above did not test a fully functioning verified-author
expansion arm. Their measured subject-pool coverage remains an observation;
it should not be generalized to the corrected author arm.

The correction accepts only validated Open Library identifiers and retains
trusted-host checks for URLs. The October 4 rerun recovered all four verified
author seeds at both boundaries using the same frozen seeds and budgets.
Neither corrected arm retrieved a held-out favorite; their top-eight books
have unknown judgments. This does not demonstrate a production retrieval
bug or establish that author retrieval cannot help other readers. See the
[four-test decision record](#four-focused-ranking-tests-2026-10-04).

<a id="reading-experience-source-pilot-2026-10-02"></a>

## Reading-experience source pilot — October 2, 2026

Historical source: `reading-experience-source-pilot-2026-10-02.md`.

<a id="reading-experience-source-pilot-2026-10-02--reading-experience-source-pilot--october-2-2026"></a>
### Reading-experience source pilot — October 2, 2026

The fixed, evidence-backed reading-experience cues did not meet the frozen
source-coverage gate. The appeal component remains disabled, and no appeal or
residual model was fitted. Every aligned target retains the current score.

<a id="reading-experience-source-pilot-2026-10-02--historical-synopsis-pilot"></a>
#### Historical synopsis pilot

The cohort contains 1,677 eligible targets, with verified synopsis provenance
for 610 and verified catalog subjects for 724. The original report showed 322
verified subject fields because its provenance check used the eight-subject
feature limit; the corrected check now hashes all 12 subjects accepted by
production. The downstream topic representation remains capped at eight
subjects, and its subject-known mask covers 724 targets.

The original label-blind stratified sample of 60 targets is unchanged: its
frozen pre-correction strata contained 34 targets with no verified synopsis,
10 rich-fiction targets, 4 rich-nonfiction targets, and 12 with unknown genre.
Those are the strata used to select the original sample, not a resample under
the corrected verifier. The corrected 12-subject descriptive classification
has 1,067 targets with no verified synopsis, 479 rich-fiction targets, 7
rich-nonfiction targets, and 124 with unknown genre. The same 60 sample IDs
still contain a verified synopsis for 26; only one sampled synopsis has a span
matching a frozen cue phrase.

Across the full cohort, 45 targets have at least one literal cue: 10 pace, 33
tone, 1 density, and 2 structure matches. Only tone reaches the frozen minimum
of 20 matches in a cue family. In the first 300 targets, the family counts are
3 pace, 7 tone, 0 density, and 0 structure, below the required early-prefix
minimum of 8 per family. The corrected rerun reproduces these synopsis and cue
counts exactly.

The gate still fails. It required at least 100 targets with cues, at least 20
matches in each of three families, and sufficient early-prefix and source-audit
support. Unmatched cue families remain unknown; they are not treated as
evidence against a book. No outcome labels were accessed for this pilot. An
aligned fallback archive records the current score for all 1,677 targets
without fitting an appeal model. Its scores, IDs, and train/validation/later/OOF
masks match the original fallback archive exactly.

<a id="reading-experience-source-pilot-2026-10-02--publisher-source-check"></a>
#### Publisher-source check

A targeted check of ten previously unresolved cards found official publisher
description text with an exact ISBN and author/title identity for 4. Two more
descriptions matched only at the work/title-author level, with the card edition
unresolved. The remaining four were excluded because readable source text or a
unique matching official record was unavailable. Among the four exact-identity
descriptions, one contained a literal frozen cue phrase. This source check was
targeted rather than randomly sampled; it does not change the historical pilot's
coverage gate or show that other publishers or extraction methods would fail.

<a id="reading-experience-source-pilot-2026-10-02--why-four-cards-still-said-no-verified-synopsis"></a>
#### Why four cards still said no verified synopsis

The four exact-identity cases are Penguin Random House candidates with direct
publisher product links and ISBNs. Their candidate records already contained
publisher descriptions. Re-running the existing host-restricted publisher
enrichment path on those links returned the same description text in all four
cases, and every page ISBN matched its candidate ISBN. No production write or
runtime change was needed to recover them.

The completed interest form used a deliberately narrower shared card resolver:
it fetched only each candidate's canonical Open Library work description so
current and union-pool cards received the same source treatment. The recovery
script likewise called the Open Library/Google Books catalog resolver using
title, author, and ISBN; it did not pass the candidate's publisher URL to the
existing Penguin Random House enrichment path. Thus “No verified synopsis
available” described that form's Open Library-only evidence path. It did not
mean that the candidate record lacked publisher copy or that production's
publisher enrichment had failed. Keep the completed form and first-round
answers unchanged; any form that uses publisher descriptions should be a
separate follow-up with the source and edition ISBN recorded for each card.

<a id="reading-experience-source-pilot-2026-10-02--runtime-implication"></a>
#### Runtime implication

The runtime metadata resolver uses Open Library and Google Books catalog
records. Ingestion also has an HTTPS-host-restricted Penguin Random House
product-page enrichment path for items already linked from that publisher; it
checks page identity before applying publisher description metadata and records
field provenance. That path already served the four exact-identity candidate
descriptions. The ten-card check does not support adding generic publisher-page
scraping: publisher markup and edition identity vary, and several source records
were work-level or ambiguous. Keep unsupported descriptions unknown. A future
pilot should use one shared description resolver for every arm, preserve its
provider and edition evidence, and audit the source before extracting features.

A focused correctness follow-up makes the existing linked-product path require
a matching page ISBN whenever the input candidate supplies an ISBN, in addition
to its title/author match. Candidates without an ISBN still use the existing
work identity check. A reusable resolver returns the verified description and
provider evidence for future pilot cards. This prevents substituting another
edition's metadata; it does not establish recommendation lift. The four traced
production cases already pass the stricter identity check.

The cue extractor deliberately uses a small, fixed literal lexicon. Its low
coverage may reflect both sparse verified synopsis sources and alternate
wording the lexicon cannot recognize. These results do not establish that
publisher excerpts or other evidence-grounded approaches are ineffective.

<a id="recommendation-next-test-plan-2026-10-02"></a>

## Next recommendation tests

Historical source: `recommendation-next-test-plan-2026-10-02.md`.

<a id="recommendation-next-test-plan-2026-10-02--next-recommendation-tests"></a>
### Next recommendation tests

Prepared October 2, 2026. Status: proposed plan; no new experiments, production
changes, or Git operations are authorized by this document itself.

<a id="recommendation-next-test-plan-2026-10-02--what-the-previous-round-established"></a>
#### What the previous round established

The October 2 study tested five families, 57 primary score arms, and 24 rich-view
arms on the same reader's historical outcomes. The selected metric/pairwise
combination improved validation balanced AUC by 0.001602, with a paired day
95% interval of [-0.000180, +0.003415], and declined by 0.000588 later.
This is inconclusive evidence of a small scoring benefit, not proof that
improvement is impossible. PR #126 fixed full-library discovery exclusions
and is deployed; that correctness result is separate from recommendation lift.

Three constraints should determine the next round:

1. The historical validation and later outcomes have been inspected repeatedly.
   They are development/regression data now. Changing the split or taking
   another snapshot does not create independent confirmation.
2. The principal rating replay uses title/author queries, neutral metadata
   confidence and source weights, and excludes downstream interaction/slate
   effects. Production candidate representations and final ordering differ.
3. More descriptions, generic subjects, and different global blend weights
   have already received substantial testing. New tests should introduce new
   evidence or a materially different representation or objective.

Prior aggregate reports are in this repository and the existing study checkout
at `/home/steven/.local/share/bookward-study-20260930/checkout/docs/`.
The study summary is `ranking-next-five-2026-10-02.md`; the source-quality and
full-corpus enrichment study is `enrichment-full-corpus-evaluation.md`.

<a id="recommendation-next-test-plan-2026-10-02--first-establish-one-reusable-evaluation-contract"></a>
#### First: establish one reusable evaluation contract

- Reuse the production corpus and audited causal evaluator. Verify parity
  against the current deployed version before any new comparison.
- Treat all previously inspected ratings as development evidence. Use nested,
  whole-day rolling folds with training-only fitting, keeping every model on
  the same eligible targets. Report author/time-block sensitivity and actual
  high/low counts; pair counts are not independent sample sizes.
- Freeze and timestamp each experiment's representations, small parameter grid,
  primary metric, subgroup checks, costs, and decision rule before fitting.
- Measure useful top-of-list behavior alongside favorite/dislike AUC. For
  historical screens report precision and disliked-book inclusion at 8 and 20,
  plus NDCG. A block of eventually read books remains a synthetic pool.
- Start an immutable prospective record of real eligible candidate pools,
  field/vector hashes, stage scores, final ranks, policy versions, visibility,
  and outcome availability. Inspect existing logging and add only missing
  information. Current run/impression records alone do not preserve the full
  eligible pool or every input needed for exact replay.
- Keep saves/interest, explicit rejection, completion, and enjoyment ratings
  as separate endpoints. Missing feedback is unknown. “Not now” is not dislike.
- Estimate achievable precision from observed traffic and outcome rates before
  setting a live sample target. One reader's repeated impressions are not
  independent readers. No arbitrary two-percent improvement floor is required.

<a id="recommendation-next-test-plan-2026-10-02--test-1--find-which-production-stage-helps-or-hurts"></a>
#### Test 1 — Find which production stage helps or hurts

**Question:** Does a useful base preference score get weakened by metadata
confidence, source weighting, interaction adjustments, or slate selection?

Capture actual current eligible pools and replay the complete pipeline locally.
Produce a stage-by-stage decomposition and five fixed comparisons: current
pipeline, neutral source contribution, neutral metadata shrinkage, interaction
adjustments off, and slate adjustments off. Identity/eligibility protections
remain active in every arm. Inspect errors across sparse/rich metadata,
known/unfamiliar authors, and disagreement between stages.

Historical feedback is usable only where pre-action inputs and the relevant
candidate/exposure population can be reconstructed. Otherwise use the current
snapshot for a mechanical diagnostic and gather prospective labels. Avoid
filling missing historical source/confidence fields with invented values.

**Why this is different:** earlier source/confidence comparisons used sparse,
selected action subsets and did not reconstruct the complete final policy.

**Advance if:** an identified stage produces reproducible errors or a fixed
alternative improves prospectively observed choice quality. A mechanical bug
can receive its own fix without waiting for a statistical utility claim.

<a id="recommendation-next-test-plan-2026-10-02--test-2--challenge-the-embedding-representation"></a>
#### Test 2 — Challenge the embedding representation

**Question:** Are our embeddings capturing the distinctions this reader cares
about, and does richer text become useful with a different representation?

Compare four bounded approaches on identical text and cohorts:

1. Fresh current Qwen embeddings as the execution-matched control.
2. The same Qwen model with one fixed book-retrieval instruction on the query
   side, using the documented query/document convention.
3. One small different-family encoder, initially `BAAI/bge-small-en-v1.5`,
   which has an existing adapter in the repository.
4. A word/character TF-IDF baseline, with vocabulary and IDF fitted on earlier
   training text only.

Cross these with title/author-only versus corrected verified metadata input.
Preserve missing-metadata fallback and report full-cohort and coverage-slice
results. Each encoder uses its own compatible dimensions and cache namespace.
Record model digest, prompt, preprocessing, truncation, latency, and memory.

Test direct replacement first, and also use the same small causal learner for
each representation with training-only normalization. Different cosine scales
must not be confused with different information quality. An instructed query
view requires separate query/history vectors and its own parity tests.

**Why this is different:** prior enrichment tests largely held the Qwen
representation fixed. This tests the interaction between text and encoder.

**Advance if:** the selected representation improves rolling-fold utility,
including unfamiliar authors, and offers complementary errors to the current
score. Preserve only one selected alternative for prospective confirmation.

Qwen supports task-specific instructions; this motivates a test and does not
predict a Bookward gain. See the [Qwen model card](https://huggingface.co/Qwen/Qwen3-Embedding-4B)
and [BGE model card](https://huggingface.co/BAAI/bge-small-en-v1.5).

<a id="recommendation-next-test-plan-2026-10-02--test-3--enrich-for-reading-experience"></a>
#### Test 3 — Enrich for reading experience

**Question:** Can evidence about pace, prose, tone, narrative structure, or
technical difficulty explain different ratings for books on similar subjects?

Begin with a bounded source-quality pilot before enriching the full library.
Use a label-blind, reproducibly sampled set spanning sparse/rich metadata and
fiction/nonfiction where those categories are verified. Inspect whether
existing descriptions or available publisher excerpts support a small fixed
schema: pace, tonal qualities, prose/technical density, and narrative or
argument structure. Preserve source evidence, provenance, and unknown values.

Use model extraction only to identify supported claims. Do not generate book
facts from titles or model memory. Independently audit a sample against the
source text with ratings hidden. If the source does not support the features,
stop the extraction arm rather than fabricate coverage.

If the pilot establishes sufficient evidence and coverage for a useful test,
freeze the extractor and run the full-corpus comparison: current features,
appeal features alone, and a bounded residual added to current scoring.
Include a topic-only control to establish whether the new fields add anything
beyond the grounded subjects/setting already tested. Missing features must
retain the current fallback and stay in whole-cohort evaluation.

Existing reader-written reviews, if available, may inform prior-history taste
features only when available before the decision. Never feed a held-out book's
later review or rating into its candidate features.

**Why this is different:** the earlier structured facets described topics,
places, and eras; this tests how the book is experienced.

**Advance if:** extraction is supported by evidence and its selected features
add predictive value beyond topics and metadata completeness. More populated
fields alone are not success.

<a id="recommendation-next-test-plan-2026-10-02--test-4--optimize-the-books-at-the-top-of-the-list"></a>
#### Test 4 — Optimize the books at the top of the list

**Question:** Can a small change improve the first eight recommendations even
when its effect on global AUC is modest?

Compare the current score, the frozen previous capped pairwise residual, and
one new capped pairwise objective that weights mistakes near the top more
strongly. Construct training comparisons using earlier data only; hold the
feature set, regularization budget, and correction cap constant. Give the
previous three-interest model one fixed comparator slot because its earlier
tail results were promising, without retuning its cluster count.

Register top-8 favorite precision as the historical screening endpoint, with
disliked-book inclusion and broad high/low AUC as guardrails. Report top-20
and rank stability as secondary checks. Evaluate all comparisons on common
temporal blocks; explicitly distinguish these synthetic pools from real slates.
Do not select a winner from a single top-8 list or a post-hoc favorable cutoff.

**Why this is different:** the previous pairwise objective weighted all unequal
rating pairs, and model selection centered on broad AUC. This changes the
training objective and tests a declared practical benefit.

**Advance if:** gains recur across folds without unacceptable dislike
inclusion or instability, followed by confirmation on actual candidate lists.
Treat a small but consistent gain as a candidate, not an automatic failure.

<a id="recommendation-next-test-plan-2026-10-02--test-5--measure-whether-discovery-finds-better-choices"></a>
#### Test 5 — Measure whether discovery finds better choices

**Question:** Does typed retrieval add books this reader wants, and can the
slate expose them without crowding out stronger recommendations?

Use a small 2×2 experiment: current versus current-plus-typed candidate
retrieval, crossed with the current slate versus a simple bounded rule that
reserves one of eight positions for a close-scoring, nonredundant alternative.
Retain complete-library exclusions and verified series-order constraints if
that information is actually available; unknown series order is not guessed.

Hold total request/time budgets comparable, preserve provider failures, and
compare against spending the same incremental retrieval budget on the current
sources. Record unique eligible yield, source overlap, and candidate quality.
These are mechanism diagnostics, not the utility endpoint.

Begin with an optional pilot of roughly 40–60 blinded unread-book judgments,
drawn from both shared and source-exclusive candidates with predefined strata.
Use identical factual descriptions, randomize presentation order, and hide
model/source labels. Permit “interested,” “not interested,” and “unsure/not now.”
Include a random coverage sample as well as disagreement cases, and account
for the sampling design when reporting aggregate results.

Pilot judgments measure interest and feasibility, not enjoyment after reading
or statistical proof of a small gain. Keep a subsequent fresh judgment batch
for confirmation if pilot labels influence the model. Later live evaluation
should compare frozen policies with recorded assignment, stable decisions,
visibility, and mature outcomes; account for repeated exposure and carryover.
Use time blocks for a single-reader analysis and state its limited scope.

**Why this is different:** the previous typed retrieval study found many
eligible books but almost no rated future matches, so retrospective recall
could not establish usefulness. New user judgments directly address that gap.

**Advance if:** additional candidates or slate changes improve useful choices
under matched budgets. Extra candidates, novelty, or diversity alone do not
justify promotion.

<a id="recommendation-next-test-plan-2026-10-02--test-combinations-without-an-unrestricted-search"></a>
#### Test combinations without an unrestricted search

After individual development screens, select at most one alternative
representation, one supported appeal-feature residual, and one ranking
residual. The last slot compares the frozen old pairwise candidate with the
new top-weighted objective inside development only.

Run the resulting 2×2×2 factorial, eight configurations including current.
Keep weak individual components eligible when their errors plausibly differ;
the registered factorial is how to test whether their combination helps.
Use identical rows/folds and the full serving pipeline. Report the interaction
contrast `M(A+B) - M(A) - M(B) + M(current)` with paired uncertainty, as well as
the combined policy's total effect. Account for the grid when assessing
evidence; do not treat the best of eight ordinary intervals as confirmatory.

Keep retrieval/slate testing separate initially because changing the candidate
pool changes the question. Once one ranking challenger is frozen, compare it
on both retrieval policies with a common union-pool diagnostic and a distinct
end-to-end policy comparison.

The previously failed 75/25 enrichment blend is a fixed reference, not a new
weight sweep. New representation or appeal features must beat their matched
controls, so a combined win has an attributable explanation.

<a id="recommendation-next-test-plan-2026-10-02--order-ownership-and-delivery"></a>
#### Order, ownership, and delivery

1. Audit the full pipeline and start collecting reproducible decision evidence.
2. Run representation tests and the appeal-source pilot in parallel.
3. Run the tail-objective screen and the bounded combination experiment.
4. Conduct the small discovery-judgment pilot, then freeze one challenger for
   fresh confirmation. Estimate sample requirements from actual event rates;
   do not promise completion in a fixed number of days.
5. Implement supported changes in focused PRs, with a separate PR for each
   correctness fix or independently justified behavior change. Update the
   README only for behavior that ships; record unsuccessful experiments in
   aggregate research reports. Preserve private corpus artifacts outside Git.

Use gpt-6-luna xhigh subagents for the pipeline/evaluation audit,
representation tests, and metadata/discovery work. The coordinating agent
owns the frozen protocol, blind selection, combined analysis, and final
review. With four concurrency slots, schedule the tail-objective work when
one research agent finishes; have a different agent audit each evaluator.

A modest gain may advance to a reversible prospective trial if guardrails and
fallback pass. Broad promotion requires fresh evidence for the chosen
endpoint, acceptable uncertainty about harm, and reasonable cost. If the data
remain insufficient, report the detectable effect and the missing evidence
instead of declaring the idea ineffective or continuing parameter searches.

<a id="recommendation-production-study"></a>

## Production recommendation experiments — September 22, 2026

Historical source: `recommendation-production-study.md`.

<a id="recommendation-production-study--production-recommendation-experiments--september-22-2026"></a>
### Production recommendation experiments — September 22, 2026

This study tests the recommendation research roadmap against a real production snapshot. It does not establish population-wide quality gains. The independent observation unit is one installation with a shared reading history.

<a id="recommendation-production-study--data-and-protocol"></a>
#### Data and protocol

The private snapshot was exported at 2026-09-22 15:09:47 UTC through SSH from the production engine, using a single SQLite read-only transaction and an explicit allowlist of research tables. SQLite integrity check returned `ok`. Credentials, settings, notification payloads, and LLM connection records were excluded. Raw histories and embedding vectors remain outside this repository.

Snapshot SHA-256: `04a978b8c5c949a646d52501d206e37839a7b7f6e35f95c958de43b2bb5cea52`.

| Table | Rows |
| --- | ---: |
| Reads | 1,790 |
| Candidates | 1,975 |
| Feedback | 76 |
| Embeddings | 3,712 |
| Recommendation runs | 83 |
| Returned impressions | 6,838 |
| Events | 287 |
| Attributed outcomes | 10 |

No production data was modified and no live treatment or exploration was enabled. Statistical model selection uses earlier chronological validation data; previously studied historical test periods cannot become independent confirmation by taking a new snapshot.

The raw export is access-controlled outside the repository. After obtaining an
equivalent private JSON export with base64 embedding vectors, reproduce the
aggregate ranking report with:

```sh
uv run --project engine python engine/scripts/evaluate_ranking.py \
  /private/bookward-study/snapshot.json \
  --backend ollama --model qwen3-embedding:4b

uv run --project engine python engine/scripts/evaluate_recommendations.py \
  /private/bookward-study/snapshot.json --bootstrap-iterations 2000
```

Select the backend/model actually present in a different snapshot. The second
command analyzes the sparse logged outcomes; it does not estimate the live
effect of a new ranker. Retain the snapshot hash, acquisition method, code
revision, and exclusion counts when comparing reports.

<a id="recommendation-production-study--f0-evaluation-correctness"></a>
#### F0: evaluation correctness

The main-branch evaluator at `b9f557c` pooled observations across recommendation runs before truncating top-k, and removed unlabelled slots before computing position discounts. Three executable counterexamples demonstrate why its results cannot support model promotion:

| Case | Previous result | Correct diagnostic |
| --- | ---: | ---: |
| Two independent runs, each with one rank-1 label: one positive and one negative; precision@1 | 1.0 | 0.5 when macro-averaged over runs |
| Only labelled item is rank 25; precision@20 | 1.0 | Undefined: no observed labels inside top 20 |
| Only positive is at rank 20; observed-gain NDCG@20 | 1.0 | 1/log2(21), approximately 0.22767 |

In the actual snapshot, the old pooled report gives AUC=1 and NDCG@20=1 despite only ten labels (one positive, nine negative). This is evidence of unreliable measurement, not a successful recommendation engine.

The accompanying evaluator correction preserves logged ranks, computes top-k within runs, macro-averages only defined run metrics, and preserves repeated cluster draws during bootstrap. Its output is a descriptive, confidence/propensity-weighted observed-label diagnostic rather than a causal IPS estimate. Unknown outcomes remain unknown. The correction changes the production snapshot’s reported precision@20 from 0.10 to 0.125; its NDCG@20 remains 1.0 because the sole positive label is at the first observed slot in its run. Neither number is reliable evidence of recommendation quality with ten labels.

<a id="recommendation-production-study--read-date-coverage"></a>
#### Read-date coverage

Production Goodreads history uses slash-formatted `YYYY/MM/DD` dates. The published historical evaluator recognized 100 dated, distinct, rated works and marked 1,681 rated reads undated. The accompanying parser correction accepts these calendar dates without weakening invalid-date handling; the same private snapshot then yields 1,773 usable works after excluding 9 unrated, 7 genuinely undated, and 1 duplicate work. All 1,790 read vectors have valid content hashes. This restores the population for chronological offline analysis but cannot turn a single installation into a population study.

The corrected evaluator was run against the exact production scorer and its earlier-formula baseline using whole-day chronological 60/20/20 splits. It used the cached production embedding model and no network calls or embedding regeneration. The test profile included the earlier validation period, as defined by the existing script.

| Period (held-out works) | Metric | Earlier formula | Current neighborhood scorer |
| --- | --- | ---: | ---: |
| Validation (355) | AUC | 0.521 | 0.523 |
| Validation (355) | Precision@20 | 0.350 | 0.400 |
| Validation (355) | NDCG@20 | 0.458 | 0.483 |
| Later test (355) | AUC | 0.481 | 0.475 |
| Later test (355) | Precision@20 | 0.350 | 0.300 |
| Later test (355) | NDCG@20 | 0.352 | 0.334 |

The paired book-bootstrap 95% interval for AUC difference (current minus earlier) is −0.045 to +0.045 on validation and −0.047 to +0.034 on the later test. Both contain zero. The current neighborhood scorer therefore has no supported improvement on this refreshed retrospective comparison. These results differ substantially from the earlier [quality study](#recommendation-quality); the datasets were captured at different times and the earlier study's exact private snapshot is not available here. Do not treat either test as an untouched confirmatory experiment. Investigate corpus, timestamp, and source-version differences before drawing conclusions from the historical discrepancy.

The feedback diagnostic includes 75 explicit latest-action candidates, with 41 saves. Its current-score precision@20 is 0.50 versus 0.45 for the older formula, while AUC is 0.541 versus 0.584. Those actions lack exposure logging in this historic feedback table, and the mixed metrics do not resolve which ranker readers prefer. No visible ranking switch is proposed.

<a id="recommendation-production-study--e08-slate-diversity-feasibility"></a>
#### E08: slate diversity feasibility

A private read-only probe selected the top 100 recommended candidates from enabled sources with accepted quality, excluding already-read and shortlisted identities. All 100 had current hash-valid `ollama/qwen3-embedding:4b` candidate vectors. The slate size was eight. This is a current-catalog feasibility comparison, not historical replay or a live experiment. Current source filters are applied at ingestion; the probe uses the retained candidate and source status at snapshot time.

MMR used `lambda * (champion_score / 100) - (1-lambda) * maximum_similarity_to_selected`, with deterministic tie handling. The champion score is only a ranking heuristic, not a probability of enjoyment.

| Slate | Mean pairwise cosine | Mean champion score | Distinct authors |
| --- | ---: | ---: | ---: |
| Score order | 0.45635 | 56.7250 | 8 |
| MMR, lambda=0.90 | 0.36627 | 56.2750 | 8 |
| MMR, lambda=0.75 | 0.27839 | 55.0625 | 8 |
| MMR, lambda=0.50 | 0.24123 | 53.2250 | 8 |
| Maximum two books per author | 0.45635 | 56.7250 | 8 |

Mild MMR reduces pairwise cosine approximately 19.7%, with a 0.45-point decrease in mean champion score. This is a useful candidate for a later randomized slate test. It does not show that readers prefer the diversified slate. Author caps have no effect on this particular candidate pool.

<a id="recommendation-production-study--decisions"></a>
#### Decisions

- Correct mathematically demonstrated evaluation errors and slash-date parsing before using reports to select a production policy.
- Do not enable MMR from cosine improvement alone. Register a live qualified-save/satisfaction endpoint and adequate follow-up first.
- Do not promote another rating ranker from the refreshed single-installation retrospective comparison; it produced mixed validation/holdout results and is not independent live evidence.
- Do not turn the ten observed recommendation outcomes into a claim about all users.
- Preserve the current serving champion unless a candidate passes the registered ranking and reliability checks.

<a id="recommendation-recency-study"></a>

## Recency-weighted recommendation study

Historical source: `recommendation-recency-study.md`.

<a id="recommendation-recency-study--recency-weighted-recommendation-study"></a>
### Recency-weighted recommendation study

This follow-up tests whether similar books read recently should contribute more to the rated-neighbor adjustment merged in [PR #76](https://github.com/sgerner/bookward/pull/76). It uses the same private production snapshot and the same strictly earlier-day, whole-day 60/20/20 replay described in [the historical replay](recommendation-historical-replay.md): 1,773 distinct rated works, with 963 usable calibration examples, 355 validation books, and 355 later books. Each formula receives a separate linear mapping from score to 1–5 stars fitted only on earlier examples.

The selected formula retains the 40 most cosine-similar rated reads. It compares their original weighted rating with a rating that additionally discounts older reads by `exp(-age_years / 8)`, then adds 20 score points per star of difference. Relative ages are measured against the most recently dated neighbor; the query-date factor cancels after weight normalization. Undated reads receive the median known neighbor age, and an all-undated neighborhood keeps its previous score. The adjustment ramps up over the first 100 rated works. An 8-year decay timescale and 20-point strength had the lowest validation star MAE in a small grid of timescales and strengths.

| Period | Formula | Star MAE ↓ | Star RMSE ↓ | Spearman ↑ | Graded NDCG@20 ↑ | 4–5-star AUC ↑ | 1–2-star AUC ↑ | Low books in bottom 20 ↑ |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Validation | PR #76 | 0.575 | 0.771 | 0.108 | **0.488** | **0.528** | 0.589 | 3 |
| Validation | Recency weighted | **0.573** | **0.770** | 0.108 | 0.478 | 0.526 | **0.592** | **5** |
| Later period | PR #76 | 0.688 | 0.889 | 0.110 | 0.455 | 0.581 | 0.520 | 3 |
| Later period | Recency weighted | **0.683** | **0.882** | **0.149** | **0.486** | **0.601** | **0.546** | **5** |

The exact serving scorer reproduced the exploratory calculation. On the later period, paired book-bootstrap 95% intervals for the new-minus-old differences were −0.0096 to −0.0004 stars for MAE, +0.0021 to +0.0358 for 4–5-star AUC, and −0.0056 to +0.0553 for 1–2-star AUC. The last interval includes zero. On a broader diagnostic pool of 863 active-status candidates with valid vectors, mean absolute score movement was 1.23 points; five of the old top eight and 15 of the old top 20 remained. This pool does not reconstruct every production eligibility rule.

Other follow-up ideas were not promoted. Causal mean-centering of read embeddings lowered validation star MAE slightly but worsened later MAE, RMSE, and broad AUC. Stronger negative-neighbor penalties improved validation low-rating discrimination but lost it later. An explicit title-suffix series feature covered only 20 validation and 28 later books with prior same-series reads; its later series-subgroup star error did not consistently improve. These experiments are private diagnostics, not committed user data.

This remains one reader's chosen books. Cached embeddings may postdate some historical reads; the later period had been inspected before; many configurations were explored; and the book bootstrap ignores temporal dependence. The result supports a small scorer change for this historical corpus, not a claim of benefit for every user or for finding unread candidates.

<a id="recommendation-research-roadmap"></a>

## Recommendation research roadmap: learning what each reader will value

Historical source: `recommendation-research-roadmap.md`.

<a id="recommendation-research-roadmap--recommendation-research-roadmap-learning-what-each-reader-will-value"></a>
### Recommendation research roadmap: learning what each reader will value

**Status:** proposal for future implementation and experiments, not an implementation specification already deployed.

**Prepared:** 2026-09-22.

**Code reference:** Bookward `main` at `b9f557c` before the accompanying evaluator corrections. Recheck integration points before implementing a future experiment.

**Evidence:** source inspection, the existing September 20 production-snapshot study, and primary research linked below. No new production database was accessed, no production experiment was run, and no improvement claimed here has been measured.

**Navigation:** [priorities](#recommendation-research-roadmap--1-what-to-pursue) · [current system](#recommendation-research-roadmap--2-what-exists-and-what-the-evidence-actually-says) · [data and evaluation foundations](#recommendation-research-roadmap--3-foundations-required-before-trustworthy-experiments) · [18 experiment designs](#recommendation-research-roadmap--4-experiments-to-implement-and-test) · [counterfactual estimation](#recommendation-research-roadmap--5-counterfactual-estimation-exact-scope-and-limits) · [implementation architecture](#recommendation-research-roadmap--6-implementation-architecture-for-future-work) · [delivery plan](#recommendation-research-roadmap--7-ordered-delivery-plan-and-decision-gates) · [experiment template](#recommendation-research-roadmap--8-reusable-experiment-specification) · [completion checklist](#recommendation-research-roadmap--9-completion-checklist-for-each-future-implementation) · [research sources](#recommendation-research-roadmap--10-research-sources-and-boundaries).

<a id="recommendation-research-roadmap--1-what-to-pursue"></a>
#### 1. What to pursue

Build an engine that improves **the probability of finding a book worth reading**, while respecting different tastes, limited reading time, sparse histories, local hardware, and uncertainty. A better average score is insufficient if cold-start readers, niche readers, or readers with limited metadata receive worse recommendations.

The most promising sequence is:

1. Make the production evidence trustworthy: correct evaluation, capture immutable decision inputs, distinguish exposure from visibility, and retain outcome timing.
2. Improve the information available to the existing cheap ranker: verified work metadata, separate content facets, and more balanced candidate discovery.
3. Learn a small, regularized model of each reader's preferences and uncertainty; represent multiple interests instead of collapsing them into one neighborhood.
4. Optimize the **slate** of recommendations, balancing relevance, useful novelty, breadth, and series constraints.
5. Learn through limited, explicitly randomized exploration and optional questions chosen for information value.
6. Add delayed-outcome models, drift adaptation, and population-level learning only as sufficient production evidence becomes available.

These are testable hypotheses. Mathematical sophistication is not a promotion criterion. Every approach must beat a simpler baseline on the same population and data budget, or deliver a measured benefit in cost, accessibility, coverage, or reliability without unacceptable quality loss.

<a id="recommendation-research-roadmap--experiment-index"></a>
##### Experiment index

Priority describes dependency and expected practicality, not a measured return. Effort is relative: S = bounded module/evaluator work; M = several data and serving changes; L = new data infrastructure or a research program. All estimates exclude the time needed to accumulate outcomes.

| ID | Approach and mathematical idea | Priority / effort | Main prerequisite | Principal falsification test |
| --- | --- | --- | --- | --- |
| E01 | Verified work identity and probabilistic record linkage | P1 / M | F0–F2 | Does stricter identity reduce false matches without suppressing valid books? |
| E02 | Multi-view embeddings and information bottlenecks | P1 / M | E01 | Do facets improve unfamiliar-author ranking beyond metadata alone? |
| E03 | Bayesian ordinal preference learning and shrinkage | P1 / M | F0–F3 | Does it beat frozen neighborhoods for sparse and established readers? |
| E04 | Mixtures of interests instead of one taste vector | P1 / M | E02, E03 | Does it recover minority interests without recommending irrelevant outliers? |
| E05 | Temporal state estimation and change detection | P2 / M | E03, dated events | Does adaptation beat simple decay without forgetting enduring interests? |
| E06 | Typed graph diffusion for candidate discovery | P1 / M | E01, association evidence | Are additional eligible, later-valued works found at fixed retrieval budget? |
| E07 | Source selection as a portfolio and coverage problem | P1 / M | F2, E06 | Does the union improve beyond more calls to the strongest source? |
| E08 | Submodular and determinantal slate diversity | P1 / S–M | E02, corrected evaluation | Does diversity improve useful discovery at noninferior satisfaction? |
| E09 | Optimal transport for personalized slate balance | P2 / M | E02, E04, E08 | Does semantic balancing beat simple quotas and divergence penalties? |
| E10 | Information-directed, conservative exploration | P2 / L | F0–F4, E03 | Is learning faster per displaced recommendation? |
| E11 | Active preference elicitation and optimal experimental design | P2 / M | E03, interaction support | Do optional questions improve outcomes enough to justify their burden? |
| E12 | Multi-task and survival models for delayed reading outcomes | P2 / L | Mature event history | Does modeling delay beat separate simple outcome models? |
| E13 | Distributionally robust optimization across reader cohorts | P2 / M | Multiple independent readers | Does worst-cohort utility improve without excessive average loss? |
| E14 | Calibration, uncertainty, and selective recommendation | P1–P2 / M | E03, held-out calibration set | Can confidence guide fallback without excluding sparse readers? |
| E15 | Reading sequences as constrained planning | P2 / M | Verified series/format data | Does the first appropriate unread work beat generic similarity? |
| E16 | Evidence-grounded LLM features and selective reranking | P3 / M–L | E02, F0–F4 | Does it add human-outcome value beyond cheap verified features? |
| E17 | Counterfactual explanations and controllable preferences | P2 / M | E02, E03 | Do controls improve future choices, beyond making explanations persuasive? |
| E18 | Local-first collaborative transfer and federated priors | P3 / L | Opt-in multi-installation program | Is transfer beneficial for held-out readers after communication/privacy costs? |

F0–F4 are the foundation milestones in section 3. E10 and E18 are not prerequisites for improving the existing local engine.

<a id="recommendation-research-roadmap--2-what-exists-and-what-the-evidence-actually-says"></a>
#### 2. What exists and what the evidence actually says

<a id="recommendation-research-roadmap--21-codebase-map"></a>
##### 2.1 Codebase map

Paths below are repository-relative navigation links, not new modules to assume exist.

| Component | Observed behavior | Implication for the roadmap |
| --- | --- | --- |
| [ranking.py](../engine/afterword_engine/ranking.py), `rank_candidates` | Similarity-weighted top-five positive neighborhood, strongest negative similarity, local five-neighbor rating adjustment, shrunk author affinity, source weight; bounded 0–100 score | Strong cheap champion; preserve it as a versioned baseline. Scores are not probabilities. |
| [scoring.py](../engine/afterword_engine/scoring.py), `document`, `score_all` | One document concatenates title, author, genres, description; rated reads and accepted active candidates are embedded; cached vectors checked against document hashes | Candidate/read information is asymmetric; richer models need richer history documents and versioned feature caches. |
| [database.py](../engine/afterword_engine/database.py) | SQLite reads, candidates, feedback, embeddings, quality, association, and recommendation telemetry; legacy LLM tables may remain in migrations | Extend these concepts; do not build a parallel unconnected feedback system. No reader/profile foreign key currently scopes these tables. |
| [identity.py](../engine/afterword_engine/identity.py), [quality.py](../engine/afterword_engine/quality.py) | Conservative title/author identity and candidate-quality gates; candidate-quality work identifiers and ISBN fields exist | Reuse these protections. Work resolution must cover reads and editions as well as candidates. |
| [main.py](../engine/afterword_engine/main.py), `_recommendation_rows`, `tracked_recommendations` | Discovery excludes known reads and shortlisted works; responses can record runs and explore the already-returned tail | Preserve eligibility before every experimental ranking; current exploration does not expand retrieval support. |
| [learning.py](../engine/afterword_engine/learning.py) | Runs, returned impressions, visibility, action events, and attributed reads; conservative same-day handling | Telemetry exists, but attribution is not proof that a recommendation caused a read. |
| [telemetry.ts](../src/lib/telemetry.ts), [discovery UI](../src/routes/+page.svelte) | First-party browser events; best-effort delivery can drop batches | Measure missingness. A served card is not necessarily visible, and missing telemetry is not dislike. |
| [exploration.py](../engine/afterword_engine/exploration.py) | Optional epsilon mixture of deterministic tail and uniform tail permutation; default stable prefix is four | Logged probabilities are item-at-slot marginals for that returned tail, not joint slate or candidate-inclusion probabilities. |
| [associations.py](../engine/afterword_engine/associations.py), [provider adapters](../engine/afterword_engine/association_sources/) | Seeded associations, provenance, cache/run records; seed selection caps author repetition | Good foundation for graph retrieval and source allocation; provider data retention and ephemeral paths must be preserved. |
| LLM integration | Retired from the current serving code on `main` | Any future E16 trial needs a new optional, separately validated shadow adapter; no LLM runner should be assumed available. |
| [evaluation.py](../engine/afterword_engine/evaluation.py), [evaluate_recommendations.py](../engine/scripts/evaluate_recommendations.py) | Logged-outcome metrics, temporal run splits, run bootstrap, numeric promotion gate | Useful scaffolding; correct the issues below before relying on promotion results. |
| [evaluate_ranking.py](../engine/scripts/evaluate_ranking.py) | Read-only historical ranking comparison with hash checks and chronological splits | Suitable retrospective baseline, not proof of new-reader or live causal gains. |

**Operational caveat:** the LLM runtime has since been removed from `main`. E16 describes a possible future experiment, not an existing feature to turn on.

<a id="recommendation-research-roadmap--22-existing-production-evidence-is-historical-and-narrow"></a>
##### 2.2 Existing production evidence is historical and narrow

The [existing quality study](#recommendation-quality) reports a September 20 snapshot with 1,834 read entries and 2,283 candidates, and evaluates cached `qwen3-embedding:4b` vectors. Its deduplicated rating test set contains 354 works from one reader. Neighborhood scoring improved descriptive test NDCG@20 from 0.622 to 0.896; the paired AUC-difference interval included zero. The study explicitly does not establish live acceptance, catalog discovery, or generalization across users.

Use that corpus as a regression reference, not a repeatedly reused tuning benchmark. Refresh coverage counts from new snapshots. Do not assume old findings about missing telemetry still apply: the current checkout contains telemetry added after that earlier audit.

<a id="recommendation-research-roadmap--23-what-all-users-means-in-this-architecture"></a>
##### 2.3 What “all users” means in this architecture

An installation currently has shared reading/profile state. A browser session is neither a durable reader identity nor an independent reader. Multiple browsers using one library must not be counted as multiple users.

For initial work, improve the per-installation engine and evaluate on independently participating installations, reporting the actual unit as **installation/profile**. If an installation is shared by a household, record that limitation rather than attributing its history to one person. Before supporting separate people within an installation, add explicit profile ownership to reads, feedback, runs, events, and personalized candidate state; add isolation tests and a migration from the existing single profile. Public catalog/work metadata may remain shared.

Cross-installation analysis requires an opt-in study and pseudonymous installation identifiers. Raw histories need not be centrally collected: a common local evaluator can return bounded aggregate results. No result from one installation can justify “improves recommendations for all users.” The actionable standard is broad coverage, measured cohort effects, conservative fallback, and continued monitoring—not a guarantee of benefit to every individual.

<a id="recommendation-research-roadmap--24-required-reader-coverage"></a>
##### 2.4 Required reader coverage

| Reader situation | Explicit design requirement | Evidence to collect |
| --- | --- | --- |
| No history or no positive ratings | Trusted eligible candidates plus optional declared interests; never require onboarding answers | Time to first useful save, abandonment, fallback coverage |
| Sparse or contradictory history | Strong shrinkage, uncertainty, reversible controls | Prefix learning curves, unstable-rank rate, correction/undo rate |
| Established specialist | Preserve depth and series intent; make diversity adjustable | Qualified outcomes within the niche, unwanted-topic rate |
| Broad or changing interests | Multiple taste components and separate temporary intent | Minority-interest success, adaptation without permanent forgetting |
| Slow reader or irregular importer | Mature windows, event-time modeling, honest censoring | Outcome-observation coverage, delayed satisfaction |
| Different language or preferred format | Use explicitly known preferences and verified availability; never treat unknown availability as dislike | Eligible-catalog coverage and format/language mismatch rate |
| Local-only or low-resource installation | CPU-compatible champion and bounded background work; no mandatory remote model | Quality by backend, memory/latency, offline fallback success |
| No telemetry participation | Full basic recommendation service and local controls remain available | Local regression diagnostics; no invented live-outcome claims |
| Shared household installation | Do not pretend mixed preferences are one person's stable taste | Explicit profile separation before person-level personalization claims |

New preference questions and controls should remain keyboard/screen-reader usable and skippable. Do not make access to improved recommendations depend on fine pointer interaction, long questionnaires, or a paid model provider.

<a id="recommendation-research-roadmap--3-foundations-required-before-trustworthy-experiments"></a>
#### 3. Foundations required before trustworthy experiments

<a id="recommendation-research-roadmap--f0-repair-and-validate-the-evaluator"></a>
##### F0. Repair and validate the evaluator

These findings are from source inspection; reproduce them with small mathematical fixtures before changing code.

1. **Compute ranking metrics within each run.** The pre-study top-k functions sorted pooled usable rows by logged rank and truncated the pooled list. The accompanying evaluator correction computes DCG/precision separately for each run and preserves original slot discounts when a label is missing. Keep analytical fixtures for this invariant as the evaluator evolves.
2. **Separate model ranking from logged ranking.** The corrected top-k functions are diagnostics of the logged order; passing another `score_key` does not evaluate a challenger order. A future offline reranking diagnostic must sort each common candidate set by the challenger score and use that new position. A logged-policy value estimator instead keeps the actual action and uses policy ratios. These answer different questions and need different functions.
3. **Use paired populations.** Any future shadow report must compare champion and challenger on the same rows/runs and expose missing-score rates. Also evaluate the actual deployed challenger-plus-fallback policy across all assigned traffic; silently dropping timeouts can make an expensive model look better.
4. **Stop treating inverse logging probability as a universal causal correction.** A target policy requires its own action probability in the numerator, compatible support, a defined reward, and an appropriate action unit. Confidence weights are subjective label reliability, not propensities. Pairwise AUC weighting additionally needs a defensible pair-inclusion model; multiplying slot marginals does not establish one for dependent permutations.
5. **Separate signal families.** `load_rows` currently chooses one outcome per impression by confidence/latest attribution; downstream signal diagnostics therefore see that chosen subset. Export complete event histories and compute separate save, pass, completion, and rating metrics. Do not let a save disappear from its diagnostic because a rating supersedes it for another endpoint.
6. **Reconstruct as-of state.** Require feature/model availability before serving, outcome availability before fitting, and label maturity before evaluation. For any future async shadow run, `finished_at <= decision_time` matters, not merely `created_at`. A manually selected later model run is not an as-of live comparison. Import-time fallback when completion date is unknown must not manufacture a post-exposure reading outcome.
7. **Use an independent-unit uncertainty estimate.** Run bootstrap preserves within-run dependence, but repeated visits from one reader remain correlated. Use reader/installation clusters for population experiments and block-by-time uncertainty for single-installation diagnostics. Bootstrap paired treatment-minus-control differences where pairing is valid. Separate confidence intervals for two arms are not a confidence interval for their difference.
8. **Replace numeric sample minima as promotion evidence.** Existing defaults of 20 runs, 200 labeled impressions, and 20 positives/negatives are software gates, not a power calculation or statistical significance test. Gates also need mature outcomes, coverage, uncertainty, and cohort constraints.

Required fixtures: reversed challenger ranking changes within-run NDCG; two disjoint runs never compete for one top-k; changing input order leaves results unchanged; a missing middle label preserves its actual discount; shadow missingness cannot improve the paired population; future features/results are excluded; deterministic unsupported actions produce “not identifiable”; duplicated exports do not create new independent evidence. Include an analytically enumerable tiny randomized policy to validate IPS and doubly robust estimates. Synthetic fixtures establish correctness; they do not replace production-data validation.

<a id="recommendation-research-roadmap--f1-define-signals-and-denominators"></a>
##### F1. Define signals and denominators

| Signal | Training interpretation | Evaluation interpretation |
| --- | --- | --- |
| Imported historical rating | Ordinal preference evidence available after import | Retrospective rating prediction; no recommendation attribution without eligible earlier exposure |
| Explicit save | Intent, weaker than eventual enjoyment | Save event in a predefined window; keep repeated save/restore semantics explicit |
| Explicit pass/reject | Contextual weak negative; possibly already owned, wrong format, or wrong time | Report separately; never infer from bulk workflow status |
| Restore | Retraction/state change | Not a new positive preference by itself |
| Detail/source open | Interest or uncertainty | Secondary diagnostic, not primary utility |
| Librarr search/import | Acquisition intent, affected by availability | Separate acquisition endpoint, never completion |
| Verified completion, no rating | Completion evidence with unknown satisfaction | Completion endpoint, not a fabricated positive rating |
| Post-exposure 1–5 rating | Ordinal satisfaction, with identity/time confidence | Satisfaction conditional on observed rating, plus rating-observation coverage |
| No event | No direct preference label | Censored satisfaction; a zero **recorded action** only for a matured fixed-window endpoint with functioning observation |
| Missing/failed visibility telemetry | Unknown visibility | Track delivery/missingness separately; do not fill as “not interested” |

The existing `completed_unrated` numeric label is a heuristic; do not reinterpret it as measured enjoyment. Retain raw events and train endpoint-specific models rather than learning one universal reward from all current labels.

For live causal A/B analysis, the primary denominator should be eligible randomized readers or predeclared reader-periods. Visibility is downstream of ranking and scrolling. Conditioning the primary treatment comparison on “became visible” can select different populations across arms. Visibility-based rates are valuable diagnostics; randomization-level intention-to-treat analysis estimates the effect of the complete serving policy, including visibility and failures.

<a id="recommendation-research-roadmap--f2-add-a-reproducible-decision-data-contract"></a>
##### F2. Add a reproducible decision-data contract

Extend current telemetry with versioned records; do not overload the 4 KB event-metadata field with full candidate snapshots.

| Proposed record | Minimum contents | Why it is needed |
| --- | --- | --- |
| Experiment assignment | Experiment/version, pseudonymous unit, assignment time, arm, assignment probability, eligibility rule | Reconstruct intention-to-treat populations and detect sample-ratio mismatch |
| Decision snapshot | Run ID, installation/profile, surface, timestamp, eligible set or reconstructible immutable reference, exclusions, retrieval ranks, source provenance, canonical work IDs | Ranking and retrieval replay on the actual choice set |
| Policy identity | Code/model/document/feature/identity versions, artifact hash, training cutoff, exploration configuration, candidate-set hash | Avoid attributing model changes to an unchanged hard-coded policy name |
| Policy output | Every scored candidate or explicitly bounded shortlist, raw score, components, selected slots, fallback reason, latency/cost | Compare algorithms and account for failures |
| Randomization record | Action definition, support, normalized behavior probabilities, selected action, RNG/policy version | Correct off-policy estimation; record whether probabilities are conditional or marginal |
| Work metadata history | Work/edition links, feature values, provider, retrieval time, valid-from time if known, verification status, expiration | Prevent future metadata and erroneous identity from leaking into training |
| Outcome history | Event time, ingestion time, correction time, read-date precision, linked work, attribution window/method/confidence | Handle imports, delayed labels, corrections, and repeated exposure |
| Observation coverage | Client/surface telemetry version, failed/drop counts where measurable, last successful import/observation | Distinguish no observed outcome from incomplete follow-up |

Candidate IDs are local to an installation; join cross-installation catalogs through verified work identities, not integer IDs. Namespace all event/run identifiers in pooled analysis. Randomization seeds aid reproducibility but are not a substitute for action probabilities.

Retain an immutable experiment snapshot or tombstoned evidence under the study's retention rules: current cascade deletions can remove historical impressions/outcomes. Support deletion requests and exclude removed observations from later artifacts; do not promise permanent retention of personal histories. Exclude settings, API tokens, LLM connection secrets, recipient addresses, and notification payloads from the study export. Keep the snapshot and vectors outside the repository.

<a id="recommendation-research-roadmap--f3-production-data-acquisition-and-replay-runbook"></a>
##### F3. Production-data acquisition and replay runbook

This is future operational work, not an instruction to modify production now.

1. Register an experiment with a hypothesis, target population, endpoints, outcome windows, data fields, owner, and stop rule before examining its test results.
2. Obtain a consistent private production snapshot using SQLite's online backup facility or an equivalent transactionally consistent backup process. Do not copy only the main `.db` file while WAL writes are active. Verify `PRAGMA integrity_check` on the private copy and record its hash, acquisition time, schema version, and row counts.
3. Produce an allowlisted research export from that private backup. Strip unrelated operational tables; keep a manifest listing included fields. Use an access-controlled location outside the checkout. Never publish raw titles/history, vectors, credentials, or individual-level reports in a committed artifact.
4. Audit work duplicates, missing dates, timestamp precision, rating range, metadata coverage, stale vector hashes, provider/model dimensions, unknown profiles, duplicate outcomes, and visibility/event coverage by cohort. Freeze exclusion reasons and counts.
5. Run the existing tools as descriptive baselines. The commands below exist today; substitute the backend/model actually present in the snapshot. The first example reproduces the earlier study's model choice, not a required production setting.

   ```sh
   uv run --project engine python engine/scripts/evaluate_ranking.py /private/bookward-study/snapshot.db \
     --backend ollama --model qwen3-embedding:4b

   uv run --project engine python engine/scripts/evaluate_recommendations.py /private/bookward-study/snapshot.db \
     --bootstrap-iterations 2000 --bootstrap-seed 20260922
   ```

   These scripts read their inputs and print aggregate reports. The second report remains diagnostic until F0 is complete. Use a static snapshot: its table reads are not currently wrapped in the explicit read transaction used by `evaluate_ranking.py`. Running either tool through `uv` may prepare local dependencies; this is separate from accessing production.
6. Build a rolling-origin evaluator: train with information available before cutoff T, tune on the next period, and evaluate once on a later untouched period after outcomes mature. Preserve whole decision runs and reader grouping. Add an embargo for delayed label availability; do not train on a rating whose completion date precedes T but whose rating was imported after T.
7. Use two complementary generalization tracks: future outcomes for established readers, and entirely held-out readers/installations whose local adaptation only sees their prefix histories. Freeze population priors before exposing held-out readers. Add unfamiliar-author/series and sparse-metadata slices.
8. Keep retrieval and ranking tests separate. Replay the known eligible catalog at each decision. If historical catalog snapshots are absent, label the result “retrospective current-catalog diagnostic,” not as-of retrieval recall. Future books may serve as later labels, never as earlier profile features.
9. Preserve test-set isolation. Parameter grids, feature selection, thresholds, and model routing use training/validation only. A test inspected repeatedly becomes validation; acquire a later untouched period before the next claim.
10. Export only aggregate metrics with cohort support counts, uncertainty, exclusion counts, missingness, model/data hashes, and reproducible configuration. Suppress identifying tiny-cohort details in shared reports.

<a id="recommendation-research-roadmap--f4-shadow-and-live-experiment-protocol"></a>
##### F4. Shadow and live experiment protocol

Shadow first means the challenger runs on the same decision input while the champion serves the user. It measures feasibility, disagreements, cost, and retrospective associations; it does not observe how users would react to the challenger ordering.

For the first live study, randomize a versioned serving policy persistently by independent reader/profile, stratified by history size and major pre-treatment cohorts. For single-profile self-hosted deployments, the installation is usually the unit. Cross-session contamination makes session A/B testing inappropriate for long-term learning. Freeze training during an initial ranking test, or isolate each arm's learning state and define the experiment as a learning-policy comparison. Keep retrieval fixed while testing ranking, and ranking fixed while testing retrieval.

If there are too few independent installations, use honest single-installation longitudinal evidence. A randomized time-block switchback can inform short-term endpoints, with block length and washout chosen for carryover; it cannot erase the persistent effect of a saved or read book. Do not use it as a shortcut to population-wide or long-term causal claims.

**Proposed measurement contract—final values must be registered using observed traffic:**

| Measure | Definition | Role |
| --- | --- | --- |
| Qualified save rate | Fraction of eligible randomized reader-weeks with at least one new work saved within 7 days of an eligible recommendation opportunity | Practical early primary endpoint; deduplicate works and exclude already-shortlisted books |
| Satisfied discovery | Number of newly recommended works completed within 90 days and rated 4–5 by the reporting cutoff, per assigned reader-period | Long-term confirmation; show rating/import coverage and sensitivity to 30/180-day windows |
| Poor-outcome rate | Post-exposure 1–2 ratings and explicit passes, reported separately | Harm guardrails; no invented dislike from no action |
| Candidate coverage | Later positively rated eligible works retrieved at cutoff / such works in the known catalog | Retrieval diagnostic conditional on observed reads; not recall of every book a person could enjoy |
| Reader-weighted ranking | Per-reader mean of per-run NDCG@K on explicitly observed relevant labels with stated missing-label limitations | Offline diagnostic; report K matching the actual UI and also K=20 |
| Useful novelty | Distinct unfamiliar authors/subjects among saved or positively rated works, per assigned reader-period | Prevent exposure-only diversity from being mistaken for useful discovery |
| Coverage/fallback | Fraction of eligible readers/requests served, candidate starvation, fallback/error rates | Protect sparse and low-resource readers |
| Operating cost | CPU/memory, embedding/enrichment/model cost, cache hit rate, p50/p95/p99 latency, job/backlog load | Budget and deployability gates |

Treat satisfaction among rated books as selection-biased unless missing-rating assumptions are justified. Also report observed satisfied-discovery counts per assigned unit; changes in rating/import behavior can affect that endpoint. Run sensitivity analyses, and collect a small optional satisfaction sample if needed to understand missingness. Nonresponse remains a limitation.

Power planning precedes launch. For a rough independent two-arm binary endpoint with baseline p and absolute detectable difference delta, `n_per_arm ≈ 2 × (z_(1-alpha/2) + z_(1-beta))² × p(1-p) / delta²`. For p=0.10, delta=0.02, alpha=0.05, and power=0.80, this is about **3,530 independent units per arm**, before clustering, attrition, multiple testing, or delayed outcomes. It is illustrative, not a forecast of Bookward traffic. Plan the actual study with observed reader-level variance and simulation/cluster resampling; thousands of impressions from one reader are not thousands of independent units.

Predeclare one primary endpoint, minimum worthwhile effect, cohort noninferiority margins, and a maximum study duration. Prefer a fixed analysis horizon; if continuous statistical stopping is required, implement a valid sequential method rather than repeatedly checking ordinary p-values. Technical failures can always trigger immediate rollback. Limit concurrent hypotheses; validate exploratory winners in a later confirmatory period.

**Illustrative deployment guardrails:** zero eligibility/identity/isolation violations; no >0.5 percentage-point increase in request failures; no >20% p95 latency increase or violation of the existing service SLO; no >1 percentage-point absolute deterioration in qualified saves for a sufficiently measured priority cohort. These are starting proposals, not measured tolerances. Register final margins and sufficient precision before launch. Sparse cohorts remain “unknown,” receive a proven fallback, and require further study.

Progression: replay → shadow → small canary (for example 1–5%) → powered randomized study → gradual rollout with a persistent holdout. Canary size is an operational choice, not a sample-size calculation. Promote for a prespecified benefit with uncertainty excluding unacceptable harm, or for a prespecified cost improvement with demonstrated quality noninferiority. “No significant difference” alone does not establish noninferiority. Roll back by switching a policy pointer/flag to the frozen champion; retain study logs, stop challenger learning, and never undo users' saved books or feedback.

<a id="recommendation-research-roadmap--4-experiments-to-implement-and-test"></a>
#### 4. Experiments to implement and test

All experiment cards inherit F0–F4, matched baselines, cohort reporting, cost measurement, and untouched-test requirements. Numerical grids are intentionally small starting candidates, not optimized settings. Do not launch the entire grid as simultaneous live experiments.

<a id="recommendation-research-roadmap--e01-resolve-works-as-uncertain-entities-not-just-title-strings"></a>
##### E01. Resolve works as uncertain entities, not just title strings

**Hypothesis.** Edition mismatches and missing historical metadata create more ranking error than a more complex scoring formula can remove. Work-level identity improves both labels and discovery.

**Mathematics.** Model `P(same_work | ISBN, title, author, language, edition, series)` and assign three decisions: confident match, confident mismatch, or unresolved. Use precision-oriented thresholds, not forced nearest matches. A work is distinct from its translation, edition, format, and narrator; the right identity granularity depends on the decision.

**Implementation and real-data test.**

1. Reuse candidate-quality identifiers and existing normalization. Add a versioned work/edition map covering reads and candidates, plus evidence and resolution confidence. Consult verified provider records such as [Open Library work/edition APIs](https://openlibrary.org/developers/api); confirm current provider rules when implementing. For large historical enrichment, use permitted bulk datasets or approved batch access rather than thousands of single-book API calls; the current Open Library guidance distinguishes low-volume lookup from bulk access.
2. Build a stratified, privately reviewed match set from production: exact ISBNs, audiobook suffixes, omnibus/boxed sets, translated titles, author aliases, and ambiguous short titles. Mask experiment outcomes while reviewing identity.
3. Compare current identity, exact verified IDs only, and the conservative probabilistic resolver. Measure false merges, false splits, remaining repeats, excluded-valid-work rate, and label-attribution changes.
4. Re-run the champion on identical accepted works before and after enrichment, keeping identity changes separate from representation changes. Report all exclusions rather than silently improving the test population.

**Integration:** `identity.py`, `quality.py`, `covers.py`, `ingestion.py`, `database.py`; proposed work-resolution module. Metadata fetches run asynchronously with provider budgets and expiration.

**Ship/stop:** promote only after false-merge precision meets a registered audit target and repeats decrease without unacceptable catalog loss. Abstain on ambiguous mappings. Never let an uncertain match silently turn a book into a negative training example or erase a shortlist entry. All later experiments depend on this distinction.

<a id="recommendation-research-roadmap--e02-learn-from-multiple-views-of-a-book"></a>
##### E02. Learn from multiple views of a book

**Hypothesis.** A reader may love a subject but dislike its treatment. Separating theme, prose, pacing, emotional tone, structure, length, and format can explain preferences that a single concatenated embedding obscures.

**Mathematics.** Use a late-fusion kernel `K_u(i,j) = sum_v w_uv K_v(i,j)`, where each normalized view has nonnegative weights summing to one. Keep author identity in a separate feature so thematic similarity cannot be dominated invisibly by author names. The [information bottleneck](https://www.princeton.edu/~wbialek/our_papers/tishby%2Bal_99.pdf) motivates retaining features predictive of utility while limiting irrelevant detail; it does not itself establish which book facets matter.

**Implementation and real-data test.**

1. Start with verified description, subjects, title/author, and available format/length metadata. Add speculative facets such as prose style only when grounded in auditable text or an independently checked extraction process. Missing is unknown, not a negative facet value.
2. Extend document construction with explicit view/version keys and separate cache identities. The current embedding primary key does not include view/version; migrate it or use a new feature-artifact table rather than overwriting one view with another.
3. Compare title/author-only, the current concatenation, enriched concatenation, and separate-view fusion on identical held-out works. This isolates the value of more metadata from the value of the mathematical representation.
4. Fit only a handful of fusion weights, with strong shrinkage. Test unfamiliar authors, missing descriptions, different languages when present, and local hashing versus stronger semantic embedding backends independently.

**Integration:** `scoring.document`, `cached_vectors`, `embeddings.py`, a proposed feature pipeline. Precompute expensive views; serving should reuse them.

**Ship/stop:** require gains beyond enriched concatenation at comparable cost. If the benefit disappears when author names are masked or fails on sparse metadata, retain the simpler representation. Do not market model-generated facets as catalog facts.

<a id="recommendation-research-roadmap--e03-bayesian-ordinal-preferences-with-partial-pooling"></a>
##### E03. Bayesian ordinal preferences with partial pooling

**Hypothesis.** Learn a small reader-specific weighting of positive similarity, negative similarity, author familiarity, content facets, source, and uncertainty instead of fixing all coefficients for everyone.

**Mathematics.** For ratings r in 1…5, fit an ordinal model `P(r <= k | x,u) = sigmoid(tau_k - theta_u^T x)` with ordered thresholds. Use `theta_u ~ Normal(theta_global, Sigma)` only when a legitimate multi-reader training population exists. Within one installation, shrink toward fixed validated defaults. The posterior represents uncertainty; a high heuristic score is not a probability.

**Implementation and real-data test.**

1. Start with approximately 8–20 interpretable features and regularized logistic/ordinal regression, not a deep network. Normalize using training data only. Compare fixed neighborhoods, a global regularized model where available, and locally adapted parameters.
2. Model 3-star ratings as their own ordinal category. Fit saves and passes as separate auxiliary signals, never as converted 5/1-star ratings. Keep unknown ratings out of the ordinal loss.
3. Evaluate prefix-history sizes 0, 1–9, 10–49, and 50+ as prespecified analysis buckets; use learning curves to decide routing rather than treating these counts as guarantees of adequate support.
4. In multi-reader evaluation, freeze priors on training readers and adapt to held-out readers using only their earlier events. Compare score calibration and ranking, and stress-test contradictory ratings or a prolific single author.

**Integration:** proposed preference-model module behind the pure `rank_candidates` interface; versioned local artifacts; retain original score/explanation semantics for the fallback.

**Ship/stop:** require improvement or noninferior quality with useful calibration across history-size cohorts. Reject parameter instability or a model that only wins for heavy readers. Initial linear methods should remain CPU-friendly and explainable.

<a id="recommendation-research-roadmap--e04-represent-a-reader-as-a-mixture-of-interests"></a>
##### E04. Represent a reader as a mixture of interests

**Hypothesis.** Someone who likes both literary fiction and space opera should not receive books halfway between them or have the smaller interest erased by the dominant one.

**Mathematics.** Fit a small mixture of taste components with weights pi_uk. Score utility as `sum_k pi_uk f_k(i)` and optionally test a temperature-controlled log-sum-exp alternative for “strong match to any interest.” Mixture weights express frequency/evidence, not rigid quotas. Cluster entropy can summarize breadth but should not automatically force broad recommendations.

**Implementation and real-data test.**

1. On deduplicated positive history, compare one component with two and four components, using deterministic spherical clustering and minimum support per component. Fit on earlier data only; retain negatives to distinguish disliked books within a liked topic.
2. Route sparse histories back to E03. Maintain minority components through smoothing, then allow an explicit current-interest choice to override recent behavior without rewriting lifelong taste.
3. Retrieve candidates from each component, then score and diversify their union. Separate this retrieval benefit from scoring by running a fixed-pool ablation.
4. Measure worst-interest recall, unfamiliar-author success, breadth of saved works, and irrelevant outliers. Evaluate stable specialists as well as broad readers.

**Integration:** proposed local profile-artifact builder; association seed selection; ranking features; optional UI context later.

**Ship/stop:** promote if minority interests become usefully represented and overall satisfaction remains noninferior. Merge poorly supported components; do not let clustering noise create invented identities or sensitive reader categories.

<a id="recommendation-research-roadmap--e05-distinguish-enduring-taste-from-temporary-intent"></a>
##### E05. Distinguish enduring taste from temporary intent

**Hypothesis.** A temporary series binge should influence near-term recommendations without permanently deleting a reader's other interests.

**Mathematics.** Blend a long-term preference state with a short-term state: `theta_t = (1-g_t) theta_long + g_t theta_short`. Start with exponential event-time decay `exp(-age/tau)`; only then test a state-space filter or [Bayesian change-point detection](https://arxiv.org/abs/0710.3742) to change the blending weight when evidence supports a new regime.

**Implementation and real-data test.**

1. Use completion/event time and known availability time; never interpret a bulk CSV import as hundreds of new same-day preferences. Exclude undated events from time-sensitive updates or place them solely in the long-term profile.
2. Compare no decay, 90/365-day half-lives, and a two-timescale blend. Add change detection only if those baselines fail identifiable shifts.
3. Replay production histories sequentially and identify evaluation windows before examining model wins. Measure adaptation after a shift, recovery after a temporary binge, and stable-reader regressions.
4. Test import backfills, long gaps, and a new low rating for a previously liked author. Reset short-term intent after inactivity only through a registered rule.

**Integration:** profile feature computation and versioned state updates; never change historical records to implement forgetting.

**Ship/stop:** require sustained sequential gains beyond simple decay. False change alarms and sudden shortlist churn are stop signals. Sparse readers retain the stable model because absence of events is not evidence of changed taste.

<a id="recommendation-research-roadmap--e06-retrieve-through-a-typed-book-graph"></a>
##### E06. Retrieve through a typed book graph

**Hypothesis.** A ranking model cannot recommend a valuable book absent from its candidate pool. Typed paths through works, subjects, authors, and series can discover useful candidates without a shared user graph.

**Mathematics.** Construct nonnegative, confidence-weighted transitions P and compute personalized diffusion `r = (1-alpha)s + alpha P^T r`, with seed vector s from earlier liked works. Use bounded diffusion depth/restart and relation-specific weights. A path explains retrieval evidence, not causation or guaranteed similarity.

**Implementation and real-data test.**

1. Build a sparse graph from verified work IDs and permitted association evidence. Separate `same_series`, `same_author`, `shared_subject`, and provider-recommended edges. Expire stale edges; do not persist an ephemeral provider response by laundering it into graph storage.
2. Compare existing seed associations, one-hop typed expansion, and diffusion with alpha in a small grid such as 0.3/0.6/0.85. Normalize node degree so broad subjects and famous authors do not absorb all traffic.
3. Prevent already-read works from appearing as outputs while allowing them to seed retrieval. Pass every result through quality, identity, source, and explicit user constraints.
4. Evaluate candidate recall on temporally eligible future positives, distinct eligible works per provider request, new-author coverage, and per-reader yield. Include a source-held-out test to detect a graph that simply reproduces one provider's list.

**Integration:** `associations.py`, adapters, `association_evidence`, asynchronous ingestion; proposed sparse graph cache. Retrieval stays outside the synchronous UI request.

**Ship/stop:** require useful added coverage at a fixed fetch/candidate budget. Avoid a graph neural network initially. [LightGCN](https://arxiv.org/abs/2002.02126) is a later collaborative-graph baseline if a real multi-user interaction graph becomes available; this metadata graph is a different object.

<a id="recommendation-research-roadmap--e07-allocate-source-budgets-like-a-diversified-portfolio"></a>
##### E07. Allocate source budgets like a diversified portfolio

**Hypothesis.** Ten lists containing the same popular books supply less discovery value than several complementary sources with slightly lower individual yield.

**Mathematics.** Treat source selection as a budgeted coverage problem. A proposed objective combines expected qualified yield, marginal coverage of taste components, request cost, and redundancy. Estimate source overlap covariance only as a diagnostic; correlated lists are not independent confirmations of quality. A Beta-Binomial yield model can shrink noisy source estimates.

**Implementation and real-data test.**

1. Extend provenance to retain all contributing sources for a work; the current candidate `source_id` alone cannot describe a multi-source union. Attribute retrieval contribution separately from user-outcome credit.
2. Compare equal budget, manual source weights, greedy marginal unique coverage, and uncertainty-aware allocation. Keep hard limits per provider and minimum exploration budgets for new permitted sources.
3. Use historical first-seen times and frozen catalogs to estimate unique eligible additions. Measure overlap before spending extra requests. Audit sources that appear low-yield only because their books were never exposed.
4. In shadow, reallocate the same total request/enrichment budget. In live tests, hold the scorer fixed and measure satisfied discovery plus catalog starvation for niche interests.

**Integration:** association/source scheduler, provenance schema, candidate generation. Do not immediately replace manual source trust settings with a learned preference score.

**Ship/stop:** require added useful coverage or equal quality at lower provider cost. Keep reliability and factual quality separate from popularity. Stop if automated allocation converges to one source or eliminates newly introduced sources before they receive measurable opportunities.

<a id="recommendation-research-roadmap--e08-recommend-a-useful-set-not-eight-near-duplicates"></a>
##### E08. Recommend a useful set, not eight near-duplicates

**Hypothesis.** A slate with complementary good choices helps more readers than the individually highest scores when those choices repeat the same author, series, or idea.

**Mathematics.** Baseline maximum marginal relevance selects `argmax_i [lambda relevance(i) - (1-lambda) max_(j in S) similarity(i,j)]`. Test a nonnegative facility-location coverage objective for diminishing returns. Then test a fixed-size determinantal objective `log det(L_S + epsilon I)`, with `L = diag(q) K diag(q)` and positive-semidefinite similarity kernel K. [Fast greedy DPP inference](https://arxiv.org/abs/1709.05135) provides a practical method; it is greedy inference, not a universal exact global optimizer.

**Implementation and real-data test.**

1. Compare score-only, author/series caps, MMR, facility-location coverage, and DPP on the same top-50/100 candidate pool and same display size. Enforce eligibility before optimization.
2. Scale relevance comparably to similarity; use validation to choose the relevance/diversity tradeoff. Build K from normalized features with a PSD construction rather than an arbitrary similarity table. Handle duplicate vectors and singular matrices.
3. Add a relevance floor or bounded score sacrifice, and a deterministic fallback when too few eligible diverse works exist. Sparse catalogs may return fewer choices; never violate a user's hard constraints to fill a slate.
4. Measure intra-list redundancy, taste-component coverage, new-author saves, qualified saves, and later ratings. Include readers who deliberately want a single series; excessive diversity can be harmful.

**Integration:** proposed pure slate-selection layer after scoring and before `tracked_recommendations` records the final order; `digest.py` needs a separately evaluated surface policy.

**Ship/stop:** retain a simple author cap/MMR if it matches DPP. Diversity exposure alone is not success. Validate added latency at actual pool sizes and retain reproducible tie-breaking.

<a id="recommendation-research-roadmap--e09-use-optimal-transport-to-balance-interests-gracefully"></a>
##### E09. Use optimal transport to balance interests gracefully

**Hypothesis.** Matching a reader's broad taste distribution can avoid both overspecialization and arbitrary quotas. Nearby themes should substitute more easily than unrelated themes.

**Mathematics.** Let p be a smoothed target distribution over verified taste facets and q(S) the slate's facet distribution. Optimize `sum_(i in S) utility(i) - lambda W_C(p,q(S))`, where W_C transports mass with semantic cost C. Entropically regularized [Sinkhorn transport](https://arxiv.org/abs/1306.0895) can make the calculation practical. This application is a proposed experiment, not a result from that paper.

**Implementation and real-data test.**

1. Derive p from earlier positive evidence, shrink for sparse readers, and permit explicit “explore more” intent. Do not require every new slate to reproduce historical proportions.
2. Start with 10–30 auditable facets and a validated ground-cost matrix. Compare no calibration, genre quotas, smoothed Jensen–Shannon divergence, and transport on the same candidate pool.
3. Treat multi-label books as fractional mass and include unknown metadata explicitly. A relaxed optimization requires a deterministic rounding stage back to actual eligible works; evaluate the rounded slate, not only the relaxed objective.
4. Report taste coverage, unwanted topic drift, relevance loss, minority-interest saves, and solver time. Test absent facets, specialist readers, and p concentrated on one subject.

**Integration:** E08 slate layer; cached facet vectors and transport costs. Use only if the metadata is sufficiently trustworthy.

**Ship/stop:** the semantic-distance benefit must beat simple quotas/divergence penalties. If metadata gaps dominate transport cost or tuning is unstable, defer. Historical exposure bias in p must not be mistaken for a user's desired future reading distribution.

<a id="recommendation-research-roadmap--e10-explore-where-a-recommendation-can-teach-the-most"></a>
##### E10. Explore where a recommendation can teach the most

**Hypothesis.** Carefully chosen unfamiliar books can reveal whether a reader likes a topic, style, or author, improving future decisions with fewer disappointing exposures than uniform exploration.

**Mathematics.** Information-directed sampling balances expected regret and information gain about the best action; see [Russo and Van Roy](https://arxiv.org/abs/1403.5556). Begin with a simpler explicit mixture policy `b(a|x) = (1-epsilon) pi_champion(a|x) + epsilon q(a|x)`, with a tractable, normalized exploration distribution q. A conservative reward budget is inspired by [Conservative Bandits](https://proceedings.mlr.press/v48/wu16.html); its formal guarantees do not automatically transfer to delayed, misspecified book preferences.

**Implementation and real-data test.**

1. Start with a single designated exploration slot among near-qualified candidates and a frozen prefix. Compare current uniform-tail exploration, uncertainty-weighted exploration, and an information-gain approximation from E03. Register a score-sacrifice/utility budget.
2. Log the complete supported choice set and exact action probability. For sequential slate sampling, log conditional probabilities at each draw; do not multiply marginal slot probabilities. Avoid claiming exact Thompson-sampling propensities from a single posterior draw; use an explicitly computable policy initially.
3. On production contexts, use simulation only for correctness and stress testing. Then measure actual learning curves on randomized traffic, delayed regret, later qualified saves, and posterior predictive improvement per exploratory exposure.
4. Update only with observed, correctly timed feedback. Do not reward exploration simply for provoking clicks or uncertainty. Allow a reader to select conservative recommendations.

**Integration:** `exploration.py`, F2 logging, model state, assignment routing. Existing tail exploration only supports permutations inside the already-returned tail; it cannot identify effects of moving a new item into the protected top four or retrieving unseen books.

**Ship/stop:** require faster verified learning within the registered harm budget. Roll back for repeated poor outcomes, probability errors, or model overconfidence. Keep exploration optional and retain deterministic serving as a supported mode.

<a id="recommendation-research-roadmap--e11-ask-the-question-that-most-changes-the-next-decision"></a>
##### E11. Ask the question that most changes the next decision

**Hypothesis.** One well-chosen optional comparison may teach more than asking for many ratings, especially for a new reader.

**Mathematics.** Choose a question q by expected information gain `H(theta|D) - E_y H(theta|D,q,y)` minus an interaction-cost penalty. A practical linear-model approximation uses the increase in `log det` of the information matrix. A pairwise Bradley–Terry likelihood can model “which would you prefer?” without pretending the answer is a completed-book rating.

**Implementation and real-data test.**

1. Offer small, skippable choices using verified book descriptions or plain facet choices. Include “neither,” “both,” and “not sure” where appropriate; unknown books must not force arbitrary preferences.
2. Compare no prompt, one random balanced question, and an uncertainty-targeted question. Start with one optional question per onboarding episode and a strict later frequency cap.
3. Store elicitation events separately with question version, presented options, order randomization, answer, and skip. Keep display framing comparable so wording does not explain the entire effect.
4. Measure answer/skip rate, time burden, first useful save, and later preference accuracy. Analyze everyone assigned the prompt policy, not only those who answered; respondents are a selected sample.

**Integration:** new UI interaction and event type, E03 likelihood/update path, profile settings. The no-history fallback must remain usable without answering.

**Ship/stop:** require net improvement after abandonment/burden effects. Do not optimize questionnaire completion as the goal. If a plain “what are you in the mood for?” control performs equally well, prefer it.

<a id="recommendation-research-roadmap--e12-model-the-path-from-interest-to-reading-over-time"></a>
##### E12. Model the path from interest to reading over time

**Hypothesis.** A save, acquisition, completion, and high rating represent different stages. Slow readers and books with long acquisition delays should not be penalized as failures.

**Mathematics.** Fit separate but related probabilities for intent and satisfaction, plus a discrete-time completion hazard `h(t|x)=P(T=t | T>=t,x)`, giving `S(t)=product_(s<=t)(1-h(s|x))`. Right-censored cases contribute survival likelihood, not a negative satisfaction label. [Multi-behavior recommendation](https://arxiv.org/abs/1809.08161) and [survival modeling](https://arxiv.org/abs/1809.02403) supply methodological precedents; begin with simple logistic heads and discrete-time hazards rather than their neural complexity.

**Implementation and real-data test.**

1. Preserve all event families, repeat-exposure episodes, import cadence, and observation cutoff. Define an episode at first qualifying exposure and avoid crediting one read to every repeated impression.
2. Compare separate regularized classifiers, a shared-feature multi-task model, and a completion-time model. Do not assume every completed book was first saved; user paths can skip stages.
3. Distinguish completion time from observation/import time. Date-only observations can be interval-censored; irregular imports can create observation censoring that a standard independent-censoring assumption does not resolve.
4. Evaluate calibrated completion incidence at 30/90/180 days, satisfaction when observed, and useful recommendations per reader-period. Compare fast/slow import and reading cohorts using pre-treatment history.

**Integration:** `learning.py`, event-history export, proposed outcome models. Add features for format availability only when known at the decision time; acquisition failures should not teach “dislikes this subject.”

**Ship/stop:** require gains over simpler separate endpoints, with sensitivity to informative censoring and missing ratings. Never deploy a “fastest to finish” objective that crowds out longer or more challenging books the reader values.

<a id="recommendation-research-roadmap--e13-optimize-for-underserved-cohorts-not-just-the-average"></a>
##### E13. Optimize for underserved cohorts, not just the average

**Hypothesis.** A model selected on total interactions will favor prolific readers and well-described mainstream books. Explicit cohort constraints can improve readers the average hides.

**Mathematics.** Compare ordinary reader-weighted empirical risk with a regularized worst-group objective `min_theta max_g E[loss(theta)|g]`, or constrained optimization with bounds on each cohort's regression. [Group distributionally robust optimization](https://arxiv.org/abs/1911.08731) motivates this approach and highlights the importance of regularization; it does not guarantee gains for small Bookward cohorts.

**Implementation and real-data test.**

1. Define cohorts using pre-treatment product-relevant data: history size, activity, specialist/broad interests, metadata completeness, preferred language/format when explicitly known, and embedding/backend resource tier. Do not infer protected demographics from reading history.
2. Give readers bounded/equal influence before adding DRO. Compare reader-weighted ERM, balanced cohort sampling, and a strongly regularized robust objective. Cap influence from tiny noisy groups and report their uncertainty instead of overfitting them.
3. Hold out complete readers and inspect intersections only where support permits. Use repeated training splits for robustness; keep one final untouched test population.
4. Report mean, lower-decile reader utility where estimable, worst sufficiently supported cohort, uncertainty, fallback coverage, and fraction of readers with reliable evidence of harm. Individual treatment effects are generally not identified from a standard parallel A/B test; do not label raw per-person differences as causal effects.

**Integration:** model-selection and training weights first, runtime routing only if separately validated.

**Ship/stop:** prefer a simple balanced model when it performs as well. If some cohort cannot be evaluated, retain its champion fallback and explicitly withhold a universal-benefit claim. This experiment requires independent readers; slicing one person's books is not equivalent.

<a id="recommendation-research-roadmap--e14-know-when-the-engine-does-not-know"></a>
##### E14. Know when the engine does not know

**Hypothesis.** Calibrated uncertainty helps choose between personalization, trusted defaults, asking a question, and showing fewer low-confidence digest items.

**Mathematics.** Fit calibration on a separate held-out period for a precisely defined endpoint, using logistic calibration or isotonic regression when data supports it. Evaluate Brier score and reliability curves. Explore [conformal risk control](https://proceedings.iclr.cc/paper_files/paper/2024/hash/f3549ef9b5ff520a7e41ff3cc306ab2b-Abstract-Conference.html) only with its bounded-loss and calibration assumptions explicitly checked; adaptive exposure, drift, and sparse groups defeat casual distribution-free claims.

**Implementation and real-data test.**

1. Keep raw ranking scores and endpoint probabilities separate. Never divide the current 0–100 score by 100 and call it likelihood of enjoyment.
2. Compare no calibration, global calibration on appropriate participating-reader data, and shrunk local calibration. Fit the calibrator without using the final evaluation labels. Keep save probability distinct from satisfaction conditional on rating.
3. Define a low-confidence fallback: champion ranker, trusted-source selection, optional question, or smaller digest. Evaluate the whole policy, including who receives fallback and who receives no recommendation.
4. Measure calibration by cohort, risk-versus-coverage curves, starvation, and decision utility. For conformal-style experiments, empirically audit coverage/risk in later time blocks; do not claim per-reader guarantees from population-average calibration.

**Integration:** score metadata, E03 posterior, slate/digest selection, explanation wording.

**Ship/stop:** uncertainty must improve decisions or transparency without denying service to sparse readers. Do not reuse an old digest threshold such as 80 on a new calibrated scale; register threshold selection and digest volume as a distinct policy experiment.

<a id="recommendation-research-roadmap--e15-recommend-the-next-appropriate-step-in-a-reading-journey"></a>
##### E15. Recommend the next appropriate step in a reading journey

**Hypothesis.** The best next book depends on prerequisites, series position, format, and recent repetition, not just isolated item relevance.

**Mathematics.** Represent verified series/prerequisite relationships as a directed graph. Optimize a short horizon `sum_t gamma^t U(i_t | history_t)` subject to hard eligibility and user constraints. Start with deterministic next-unread rules and receding-horizon planning; a general reinforcement-learning system is premature without reliable trajectories and a valid environment model.

**Implementation and real-data test.**

1. Resolve verified series order, companion/standalone exceptions, and edition/format availability. Make strict ordering a reader preference; publication order is not always intended reading order.
2. Compare current ranking, a first-unread-series rule, and a two-step plan with a repetition penalty and optional “continue series” intent.
3. Treat availability as a choice constraint or acquisition friction, not dislike. Never infer ownership or completion solely from an import request.
4. Replay eligible production states and audit ordering errors. Live endpoints include useful saves, series continuation, explicit skips, and long-term satisfaction; longer sessions are not inherently better.

**Integration:** E01 metadata, eligibility/slate layer, optional context controls. Preserve saved/imported records regardless of their discovery eligibility.

**Ship/stop:** unknown order falls back to unconstrained ranking with no invented series claims. Reject planning if a simple next-unread rule matches it or if it traps readers into endless continuation at the expense of desired discovery.

<a id="recommendation-research-roadmap--e16-use-llms-where-grounded-reasoning-adds-measurable-value"></a>
##### E16. Use LLMs where grounded reasoning adds measurable value

**Hypothesis.** An LLM may extract useful distinctions or resolve difficult comparisons, but a full expensive reranker is unlikely to be necessary for every request.

**Implementation and real-data test.**

1. Design a new optional runtime and shadow adapter if this experiment is ever prioritized. Keep the current champion as the fallback. Do not re-enable retired provider paths merely to run this roadmap.
2. Compare four arms offline: verified metadata with cheap ranker; cached evidence-grounded facet extraction plus cheap ranker; bounded LLM reranking of the same shortlist; selective LLM use only when cheap models disagree or confidence is low. An extraction-only arm isolates information quality from ranking capability.
3. Require structured candidate IDs, supporting metadata references, model/prompt/input hashes, unknown-value support, and strict validation of missing/duplicate/invented IDs. Treat provider text as data rather than instructions. Randomize candidate input order in robustness tests to expose position bias.
4. Freeze factual content and explanation wording during the first live ranking comparison. Otherwise more persuasive prose may increase saves without improving the chosen books. Human outcomes are the endpoint; LLM-as-judge scores are diagnostics only.
5. Use per-decision completed-at timestamps, request linkage, policy identity, cache freshness, latency/tokens, provider failures, and fallback logs. The existing as-of lookup by candidate/time needs strengthening before attributing a score to a particular decision.

**Mathematical extension.** A value-of-computation gate invokes the expensive model only when predicted decision improvement exceeds cost/latency. Fit that gate on earlier paired evaluations and test it as a full policy, including cases routed to the cheap model. Do not train only on cases where the expensive model happened to succeed.

**Integration:** proposed optional LLM adapter and generic shadow interface, plus E02 feature artifacts. Distillation to a small local model is a later subexperiment; include human labels and provenance so teacher errors are not silently treated as truth.

**Ship/stop:** require incremental production benefit beyond E02/E03/E08 at an agreed budget. A better explanation, a higher self-reported confidence, or a benchmark win from a different domain is insufficient. No LLM should be necessary for basic recommendations on CPU-only installations.

<a id="recommendation-research-roadmap--e17-turn-explanations-into-useful-preference-controls"></a>
##### E17. Turn explanations into useful preference controls

**Hypothesis.** A reader can correct a model faster when it exposes a specific, grounded reason and permits a reversible adjustment.

**Mathematics.** For an interpretable scorer, compute feature contributions or a constrained counterfactual change: what change in an editable preference would alter this rank? This is a model sensitivity explanation, not a causal statement about why the person will enjoy a book.

**Implementation and real-data test.**

1. Continue naming actual supporting reads, as the champion already does. Add verified facets only when the evidence exists. Separate reasons for candidate retrieval from reasons for ranking.
2. Offer scoped controls such as “less of this topic,” “not now,” “already read,” or “continue this series.” Distinguish temporary intent from enduring preferences and identity corrections.
3. Store the chosen control, scope, expiry, evidence version, and undo event. Do not convert every “not now” into a low rating or alter the underlying imported history.
4. Compare grounded explanations alone with the same explanations plus controls. Measure comprehension with a small optional sample, subsequent relevant saves, corrections, undo rate, and repeated unwanted recommendations. Keep initial slates identical to isolate control effects.

**Integration:** `ranking.py` explanations, UI, new event semantics, preference model, identity correction queue where relevant.

**Ship/stop:** require improved subsequent outcomes or demonstrably better user control. More persuasive explanations that only increase immediate clicks are not enough. User controls take precedence over inferred preferences and should be easy to reverse.

<a id="recommendation-research-roadmap--e18-learn-shared-priors-without-centralizing-everyones-library"></a>
##### E18. Learn shared priors without centralizing everyone's library

**Hypothesis.** Sparse readers can benefit from patterns learned across opt-in installations while retaining local personalization and an offline-capable engine.

**Mathematics.** Begin with shared low-dimensional priors/hyperparameters and local adaptation. If scale later justifies it, compare item-neighborhood collaborative filtering, regularized matrix factorization, and a graph baseline before neural sequence models. [Federated averaging](https://arxiv.org/abs/1602.05629) is a possible training mechanism, not a privacy guarantee; updates can leak information and non-IID readers can suffer negative transfer.

**Implementation and real-data test.**

1. Establish explicit participation, profile boundaries, verified cross-installation work identity, contribution caps, and deletion/retraining behavior. Keep nonparticipants on the complete local engine.
2. First distribute a frozen public/opt-in prior and collect only local aggregate evaluation summaries. Compare local-only defaults, shared prior plus local adaptation, and centrally evaluated opt-in baselines where authorized.
3. Hold out entire installations and test niche, sparse, different-language, and low-resource readers. Weight participating readers rather than event volume so one large library does not dominate.
4. Only then consider federated training with secure aggregation, minimum cohort sizes, authenticated update validation, and an explicit privacy threat model. If differential privacy is used, register clipping, noise, epsilon/delta accounting, utility loss, and membership-risk evaluation; do not label raw federated updates anonymous.

**Integration:** separate optional research/training service and signed/versioned prior artifacts. Do not add a remote dependency to the serving path.

**Ship/stop:** require held-out-reader improvement beyond locally shrunk E03, reasonable participation/communication cost, and acceptable privacy properties. Insufficient independent installations or negative transfer is a reason to defer, not to invent synthetic users and declare success.

<a id="recommendation-research-roadmap--5-counterfactual-estimation-exact-scope-and-limits"></a>
#### 5. Counterfactual estimation: exact scope and limits

For an explicitly defined supported action a, context x, observed reward r, behavior policy b, and target policy pi:

```text
w_i       = pi(a_i | x_i) / b(a_i | x_i)
V_IPS     = mean_i(w_i * r_i)
V_SNIPS   = sum_i(w_i * r_i) / sum_i(w_i)
V_DR      = mean_i[ sum_a pi(a|x_i) * m_hat(x_i,a)
                   + w_i * (r_i - m_hat(x_i,a_i)) ]
ESS       = (sum_i w_i)^2 / sum_i(w_i^2)
```

The [doubly robust policy evaluation paper](https://arxiv.org/abs/1103.4601) motivates combining a reward model with importance weighting. Fit `m_hat` on separate folds/earlier data, preserve clustering, and report reward-model error. Double robustness does not repair zero support, incorrect action definitions, unobserved satisfaction, arbitrary confidence weights, or a data leak.

For the existing tail of m books, the observed item-at-slot probability is `(1-epsilon) + epsilon/m` when the item stays at its original tail slot, and `epsilon/m` otherwise. The probability of an **entire tail permutation** is `(1-epsilon) + epsilon/m!` for the identity permutation and `epsilon/m!` for another permutation. The product of marginal slot probabilities is not that joint probability. Whole-slate IPS can consequently have extreme variance even when a marginal-slot diagnostic looks well supported.

Item-slot estimators require an outcome model in which other slate items do not create unmodeled interference, or a specifically justified slate estimator. Competing books and position-dependent scrolling make that assumption questionable. Use live randomized policy comparisons for whole-slate utility and treat limited-action off-policy estimates as screening evidence.

For every off-policy report, include:

- Fraction of target-policy probability mass supported by logged data; unsupported comparisons must say **not identifiable**.
- Behavior/target action definitions, randomization scope, and whether visibility/examination was modeled separately.
- Weight histogram, maximum weight, ESS, and weight-clipping sensitivity. Clipping reduces variance while introducing bias; report both.
- Label and observation coverage, maturity, attribution confidence sensitivity, and endpoint definition.
- Reader/installation-cluster uncertainty. ESS is a weight diagnostic, not the number of independent readers.

Deterministic logged propensity 1 means certainty for the action actually taken under that context. It does not imply a new policy has support for every alternative. A completed shadow run provides predictions, not new randomized exposure data.

<a id="recommendation-research-roadmap--6-implementation-architecture-for-future-work"></a>
#### 6. Implementation architecture for future work

Use the existing engine boundaries and introduce small pure components. Names below are proposed responsibilities, not existing files or CLI flags.

```text
verified catalog + historical observations available at time T
    -> eligibility and canonical work resolution
    -> versioned candidate generators (with source provenance)
    -> versioned feature construction and caches
    -> champion + optional challenger scorers
    -> constrained slate selector
    -> optional, explicitly logged randomization
    -> final response and immutable decision record
    -> visible/action events + later imported outcomes
    -> offline study and separately controlled model updates
```

**Scorer contract:** input contains one profile snapshot, decision time, an immutable eligible candidate set, feature/artifact versions, and context. Output contains candidate IDs, raw utility, optional calibrated endpoint probabilities, uncertainty, grounded reason codes, and model version. Scorers must not mutate candidate status, feedback, source settings, or the user's library.

**Slate contract:** receives eligible scored candidates, hard constraints, desired K, and a versioned objective. Returns an ordered subset plus constraint/fallback diagnostics. Save the final post-exploration order; a score is not enough to reconstruct what the person saw.

**Shadow contract:** support cheap challengers first; add any future LLM trial through the same generic interface. Save exact decision linkage and completion time. Background shadows should not block the UI or compete unboundedly with source ingestion.

**Artifact contract:** atomic activation of model, feature schema, calibration, and policy configuration as one compatible version. Validate cache hashes and embedding dimensions. Retain a tested champion artifact; startup, timeout, unsupported backend, stale metadata, and invalid output all route to a documented fallback.

**Surface separation:** discovery, saved lists, search, and weekly digests have different intents. Discovery experiments must not rerank away a user's explicit saved/imported state. Digest sends are not visible impressions; digest links need their own attribution context and outcomes. Avoid synchronous model calls in notification delivery and API requests.

<a id="recommendation-research-roadmap--7-ordered-delivery-plan-and-decision-gates"></a>
#### 7. Ordered delivery plan and decision gates

The order is milestone-based because event accrual, rather than coding speed, will often determine duration.

| Milestone | Deliverables | Exit evidence |
| --- | --- | --- |
| M0: establish truth | F0 evaluator corrections; F1 metric registry; audit current deployment/schema and runtime flags | Analytic fixtures pass; reported metrics change appropriately under known ranking changes; no unsupported causal claims |
| M1: capture usable production evidence | F2 decision/assignment contract; F3 private snapshot/export; data-quality dashboard | Reproducible snapshot report, joined events, versioned policies, quantified missingness, working rollback |
| M2: improve information and discovery | E01/E02/E06/E07, each with a separate ablation | Better verified metadata/eligible candidate coverage at fixed budgets; no identity regressions |
| M3: build cheap personalization and slates | E03/E04/E08 plus E14 calibration diagnostics | Matched offline wins or cost-quality benefit across sufficiently measured cohorts; CPU fallback intact |
| M4: first confirmatory live test | One strongest isolated candidate through F4 | Powered mature endpoint, cohort guardrails, complete failure/cost accounting; otherwise inconclusive |
| M5: learn adaptively | E05/E10/E11/E12, separately staged | Reliable propensities/observation windows, faster useful learning, acceptable burden and regret |
| M6: broader policy improvements | E09/E13/E15/E17 | Added value beyond simple rules, wider reader coverage, no new starvation |
| M7: selective research bets | E16/E18 | Incremental gains beyond cheaper local methods and sufficient independent-reader evidence |

**Recommended first experiment bundle:** finish F0, then run a controlled offline 2×2 comparison of current versus enriched metadata and score-only versus simple MMR. Keep retrieval and embedding backend fixed. This isolates representation and slate effects with relatively little model complexity. Advance only the supported component(s), then test E03 against that stronger baseline. Do not attribute a combined metadata+retrieval+LLM change to any one technique.

**Defer until evidence demands them:** deep sequential recommenders, unrestricted reinforcement learning, large GNNs, a global collaborative service, and topology-heavy models. They may eventually help, but the current architecture and sparse independent outcomes make simpler, identifiable experiments more valuable first. Do not collect extra sensitive data just to make a sophisticated method possible.

<a id="recommendation-research-roadmap--8-reusable-experiment-specification"></a>
#### 8. Reusable experiment specification

Copy this block into a private study record or a new documentation proposal before implementing a challenger. It intentionally has no executable deployment commands.

```yaml
experiment_id: E##_descriptive_name_v1
status: proposed
owner: unassigned
hypothesis: one falsifiable sentence
target_population: explicit reader/installations and eligibility rules
surface: discovery
champion_version: required
challenger_version: required
changed_component: retrieval_or_features_or_scoring_or_slate_or_learning
unchanged_components: list_and_freeze
data:
  private_snapshot_manifest: required
  observation_cutoff: required
  training_cutoff_and_label_availability_rule: required
  validation_and_untouched_test_windows: required
  work_identity_and_feature_versions: required
  missingness_and_exclusion_policy: required
  reader_partition_and_namespacing: required
  retention_and_deletion_policy: required
randomization:
  independent_unit: installation_or_explicit_reader_profile
  assignment_and_support: required_for_live_or_off_policy_studies
  learning_state_isolation: required_if_adaptive
metrics:
  primary_endpoint_and_denominator: required
  minimum_worthwhile_effect: required
  delayed_confirmation_and_maturity: required
  cohort_noninferiority_margins: required
  latency_cost_failure_and_coverage_limits: required
analysis:
  baselines_and_ablation_grid: required
  power_or_precision_plan: required
  uncertainty_and_multiple_testing_method: required
  causal_assumptions_and_unidentified_quantities: required
  maximum_duration_and_inconclusive_rule: required
operations:
  shadow_budget_and_failure_fallback: required
  canary_and_ramp_plan: required
  rollback_policy_and_owner: required
decision:
  promote_if: prespecified
  stop_if: prespecified
  follow_up_if_inconclusive: more_data_or_simpler_model_or_defer
```

<a id="recommendation-research-roadmap--9-completion-checklist-for-each-future-implementation"></a>
#### 9. Completion checklist for each future implementation

- [ ] Candidate eligibility, read/shortlist exclusions, profile isolation, quality gates, and source rules remain correct.
- [ ] A simple baseline and a component ablation use the same information and compute budget where feasible.
- [ ] Immutable decision snapshots reproduce the observed policy, including post-processing and exploration.
- [ ] Dates, label availability, work identity, metadata versions, and observation windows prevent leakage.
- [ ] Missing events, workflow statuses, incomplete reads, and unknown metadata are not silently converted to dislike.
- [ ] Offline diagnostics, off-policy estimates, and randomized causal results are named distinctly.
- [ ] Population claims use independent participating readers/installations and report cohort uncertainty.
- [ ] Serving failures, missing challenger scores, fallbacks, and unavailable providers remain in full-policy evaluation.
- [ ] Measured cost, latency, coverage, and mature user outcomes satisfy the registered gates.
- [ ] Runtime contracts and meaningful mathematical fixtures are tested; production data stays in private study storage.
- [ ] Rollback restores the champion policy without rewriting user actions or deleting experiment evidence outside the retention policy.
- [ ] Shared documentation records aggregate results, limitations, reproduction details, and the next decision—including a negative or inconclusive result.

<a id="recommendation-research-roadmap--10-research-sources-and-boundaries"></a>
#### 10. Research sources and boundaries

Sources were checked during preparation. They establish methods and assumptions, not expected lift for Bookward. The experiment designs, priorities, numerical starting grids, architecture proposals, and rollout thresholds above are project-specific proposals.

| Primary source | Why it matters here | What it does not establish |
| --- | --- | --- |
| [Dudík, Langford, Li: Doubly Robust Policy Evaluation and Learning](https://arxiv.org/abs/1103.4601) | Policy evaluation combining reward models and importance weights | Identifiability under zero support or missing enjoyment labels |
| [Tishby, Pereira, Bialek: The Information Bottleneck Method](https://www.princeton.edu/~wbialek/our_papers/tishby%2Bal_99.pdf) | A principle for useful compressed representations | That any proposed literary facet is factual or predictive |
| [Chen, Zhang, Zhou: Fast Greedy MAP Inference for DPPs](https://arxiv.org/abs/1709.05135) | Efficient relevance/diversity set selection | Guaranteed better reader outcomes or globally optimal constrained slates |
| [Cuturi: Sinkhorn Distances](https://arxiv.org/abs/1306.0895) | Efficient regularized optimal transport | A correct taste target or appropriate semantic cost matrix |
| [Russo, Van Roy: Information-Directed Sampling](https://arxiv.org/abs/1403.5556) | Balancing information gain against regret | Reliable utility estimates with sparse delayed feedback |
| [Wu et al.: Conservative Bandits](https://proceedings.mlr.press/v48/wu16.html) | Exploration with baseline-performance constraints | Automatic safety guarantees for this engine's slates |
| [Adams, MacKay: Bayesian Online Changepoint Detection](https://arxiv.org/abs/0710.3742) | Detecting changes in sequential data | That a sparse reading sequence contains an identifiable change |
| [Gao et al.: Learning to Recommend with Multiple Cascading Behaviors](https://arxiv.org/abs/1809.08161) | Distinct related behavior signals | That saves, acquisitions, and completion form a mandatory sequence |
| [Ren et al.: Deep Recurrent Survival Analysis](https://arxiv.org/abs/1809.02403) | Modeling event timing with censored observations | That imports are noninformatively censored or deep models are needed |
| [Sagawa et al.: Distributionally Robust Neural Networks for Group Shifts](https://arxiv.org/abs/1911.08731) | Regularized worst-group optimization | Sufficient cohort support or universal individual benefit |
| [Angelopoulos et al.: Conformal Risk Control](https://proceedings.iclr.cc/paper_files/paper/2024/hash/f3549ef9b5ff520a7e41ff3cc306ab2b-Abstract-Conference.html) | Calibration-based control of bounded losses | Unconditional validity under adaptive, drifting recommender data |
| [He et al.: LightGCN](https://arxiv.org/abs/2002.02126) | A relatively simple collaborative graph baseline | Existence of a suitable multi-reader graph in Bookward |
| [McMahan et al.: Communication-Efficient Learning from Decentralized Data](https://arxiv.org/abs/1602.05629) | Federated model training | Privacy, robustness, or utility without additional mechanisms |

The strongest future outcome is not implementing every method. It is a reproducible body of production evidence that identifies which small set of methods helps which readers, preserves a reliable fallback, and makes unsuccessful ideas cheap to reject.

## Consolidation provenance

Original source content was retained with scoped heading anchors and updated links. SHA-256 below identifies each input before consolidation; the seven additional records came from the local study checkout or untracked historical audit/plan. Git branch inventory found no additional uniquely named study Markdown files absent from main. Original frozen artifacts outside Git were not modified.

| Original record | SHA-256 |
| --- | --- |
| `recommendation-quality.md` | `a02a968f985a41e8b02086cd60ccff7235a0516896fc59df62e63b73fed20653` |
| `already-read-exclusion-audit-2026-10-03.md` | `dc19f172fa11ab0178da779178606bcbb02dd89aa203628ab0c189f825febf11` |
| `association-source-identity.md` | `df032a4a4412b7be8e850b5874d654e95a29d738ca30db361ae2300d72d43b95` |
| `discovery-follow-up-protocol-2026-10-03.md` | `7f4ebce113dd88cc0a41484b9f036d2a0f5c77c1f4fd58a2bcced9c55a754ac5` |
| `discovery-followup-scoring-audit-2026-10-03.md` | `521abaff6bd370ad0c882f22742454e697013f3a974ec5a31d0407a12f80a9f8` |
| `discovery-interest-pilot-2026-10-02.md` | `6fe0ff1d72cce684c190c0ebfdce089fe51d301c0bdf3ba9b59ba360afa5df02` |
| `embedding-cache-integrity-2026-10-04.md` | `29b37eff9385c88b8931edc2ad2ed0ad8670bd5b313dd20a2e5b4df05dfc85a8` |
| `enrichment-full-corpus-evaluation.md` | `6e52d622e9f58c25799142b0b0a586d6d9720d54d6ae87b91cf8c285ca780cc2` |
| `explicit-interest-reranker-2026-10-03.md` | `1310fea8ca4b2d635ce252e585ef8520e250314402ac9ac3b280d161458532a7` |
| `four-focused-ranking-tests-2026-10-04.md` | `a7c5928998516bad38036cc619a831c34c334023326079d57ecaf984a99a425b` |
| `kernel-uncertainty-shrinkage.md` | `ab616554d5f99ca6d223f28798733acbaa16655f9d8c201281db8cd75d19521d` |
| `production-ranking-pipeline-audit-2026-10-02.md` | `bd47c417168a465f543e030bb34045dfcd898999504126bce3aa9299b2124158` |
| `ranking-cross-profile-evaluation-2026-10-03.md` | `e66b3b234fb1e5dede5d912d11e9a7a870947aeda6d7ee2fce6a1c0e37c55146` |
| `ranking-embedding-representations-2026-10-02.md` | `b9c7f29500de5b1922ddcfc3942b8555a40ee7b3a508d173551c92cbe5ba7574` |
| `ranking-five-options-2026-09-30.md` | `b4695e3e7ef5f4e8077cd6838058eab5f9f272b48ed1f8b3b1e0a8c617908705` |
| `ranking-methodology-audit-2026-09-30.md` | `3097f7f867d01f5d1e15ad1de3eb6a18779b12938b091f191060dc6cdaed3e0a` |
| `ranking-metric-facets-2026-10-02.md` | `877c7a541de27479d7cea2750f637bc73510d08c6e8eb1d1646e04576d105f6e` |
| `ranking-next-five-2026-10-02.md` | `dd60daa4a7a0704b327a5b10d4edc870efa379ffead171e4dfb9a0c199dbc89c` |
| `ranking-ordinal-interests-2026-09-30.md` | `31e1c2c18cc7b8b25d84d1e010331f5629262442315fd162d6865d08f436b697` |
| `ranking-production-review-2026-09-30.md` | `fa4500862ae2f2623c345ecb889ae9b2d565189134c7e1ea1035c76e98a8145c` |
| `ranking-publication-era-2026-10-03.md` | `555b8dcd5df661cd7f7dd628b282a0ae61dfe14f56e6633b3ffcf4f9d9ac711f` |
| `ranking-synergy-2026-10-01.md` | `4f8ec8e44ce66d359c9b4955c6360baf13b3a90b0f96c215cee21456ccaed59e` |
| `ranking-tail-pairwise-2026-10-02.md` | `607e027cc3e401eae9624dad2dbfe8d7ef5a410b9692bb64259e750fb725764f` |
| `ranking-track-combinations-2026-10-02.md` | `fc573a54afb2d5e5d411370c30557d88ad6c556e3a9b1d4b089aa305b900db48` |
| `ranking-typed-retrieval-2026-10-02.md` | `25c97d5603d6b8325c549d63f74cc54584c500a33100fa7ea5f61e7638ac9e92` |
| `reading-experience-source-pilot-2026-10-02.md` | `ff5837f249336cf2a6eabf98ee43db08cd10975fc48cb150b9656f869662e62f` |
| `recommendation-next-test-plan-2026-10-02.md` | `f4a62b3a9d1def12f1cc6aac0a6458b770a8a8c084eafdd43037bc3ca7f6c258` |
| `recommendation-production-study.md` | `ad1cb1730064372c70a32cc8decac51bcb2162804c0cdd4ae33fd07f0db41875` |
| `recommendation-recency-study.md` | `c5bcc107c14c10fb8a3f387ff1954d9020f4d498e72e0d14397db55c2467e768` |
| `recommendation-research-roadmap.md` | `93d8a6788cb5a16c86f0cdce8a6b591606e6513644cea85d1ed08dc41d620561` |
