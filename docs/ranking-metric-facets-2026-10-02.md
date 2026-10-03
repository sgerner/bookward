# Constrained metric and grounded facet ranking study: October 2, 2026

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
