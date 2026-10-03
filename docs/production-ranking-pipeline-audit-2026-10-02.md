# Complete production ranking pipeline audit — October 2, 2026

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

## Capture and reproduction

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

## Five stage ablations

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

## Confirmed interaction-vector ID bug

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

## Why historical utility cannot be recovered from this capture

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

## Recommended follow-up

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

## Validation and reproduction

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
