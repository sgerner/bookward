# Four focused ranking tests: October 4, 2026

## Decisions

Keep the production ranking kernel at power 8 and retain existing retrieval
policy. These tests did not establish a preference improvement worth shipping.
Ship the reproduced embedding-cache reliability correction and correct the
Open Library evaluator's identifier parsing. The kernel and retrieval proposals are
closed without adoption; no experimental serving flags or weights were added.
Unknown judgments remain unknown, rather than being counted as dislikes.

## Inputs and controls

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

## 1. Retrieval ceiling

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

## 2. Verified author versus subject retrieval

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
[earlier typed-retrieval study](ranking-typed-retrieval-2026-10-02.md) and the
[official Search API](https://openlibrary.org/dev/docs/api/search).

## 3. Embedding and kernel stability

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

## 4. Sparse and diverse histories

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

## Reliability correction and methodology record

Matching text hashes previously allowed unusable cached BLOBs to reach scoring.
Disposable fixtures reproduced NaN, empty/misaligned bytes, and declared-width
mismatch failures. The fix validates cached vectors, regenerates invalid hits,
rejects float32 provider overflow, and carries read dimensions into candidate
validation across normal scoring, reading-history scoring, and full rebuilds.
Failed rebuilds preserve the old cache. Zero vectors remain valid. All 2,352
valid matching captured vectors were reused exactly without provider calls;
no production cache corruption was observed. This improves reliability and
has no measured preference lift. See
[the focused cache record](embedding-cache-integrity-2026-10-04.md).

Corrections were explicit: top-eight retrieval analysis now sorts scorer
outputs; documented bare IDs are normalized; the kernel comparison uses the
common 100-warmup cohorts; and the public benchmark uses separate read and
candidate representations, keeping labels outside ranker inputs. Some
corrections followed provisional results, so corrected measurements are not
presented as untouched confirmation data. No formula grid or seed policy was
chosen from those results. Ranking weight and retrieval decisions are closed.

## Artifact identities and verification

Public aggregate artifact hashes, retained with the study sources outside Git:

- Retrieval decision note: `49a8ad161e1a20b196594a2bb3dd57c3aca0d755f7acea8b1b160f7daee854a5`.
- Embedding/kernel decision note: `7f248b4e8630008e00113da42fb70634be44b8c298189e51d648d334d6660a65`.
- Independent-reader aggregate: `d79e1feb162f554321b501278462a07ac1d998dd08415504731c345a78d44e56`.

Focused verification: retrieval and combination fixtures 19 passed; stability
fixtures 23 passed; independent-reader fixtures 13 passed; cache selection
15 passed. The cache production change passed 499 engine tests on the latest
main lock; its broader research archive passed 512, including 13 study fixtures.
The documented bare-ID parser regression passed in the 11-test evaluator suite.
