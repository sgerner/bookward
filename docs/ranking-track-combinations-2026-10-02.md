# Ranking combinations: strict-v2 verified results, October 3, 2026

The strict-v2 verified reruns supersede the initial identity-only metadata combination results below. With the provenance-verified embedding residual, the four-arm screen selects the top-weighted correction by itself; the old embedding-plus-top-weighted winner is no longer selected. The frozen eight-arm extension selects top-weighted plus the corrected metric residual, but its development gain is very small and its grouped intervals include zero. Neither result supports a production scoring change.

These are retrospective development results on synthetic read-as-candidate pools, not observed discovery slates or independent confirmation. Historical labels had been inspected in earlier studies, and no quantitative adoption threshold was preregistered.

## Strict-v2 input and selection contract

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

## Strict-v2 eight-arm extension

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

## Superseded identity-only v1 development results

The initial run lacked field-level provenance verification. Its `verified_metadata` arm label overstated what the input audit established. Preserve these figures only as historical development evidence; they are not evidence for the strict-v2 result.

| V1 configuration | Mean development balanced-AUC change | Previous validation change | Previous later change |
| --- | ---: | ---: | ---: |
| Identity-only embedding residual | +0.000276 | +0.002793 | −0.001793 |
| Top-weighted pairwise | +0.000139 | +0.001231 | +0.000229 |
| **Identity-only embedding + top-weighted (v1 selected)** | **+0.000426** | **+0.004404** | **−0.001601** |

For that superseded v1 combination, the paired UTC-day intervals were [−0.002196, +0.011232] in previous validation and [−0.009538, +0.005574] later. Its development-fold top-eight favorite counts were 4/8, 5/8, and 5/8 versus current 3/8, 5/8, and 4/8; these small synthetic-pool summaries were never actual discovery-slate outcomes. Strict-v2 changes the metadata gate and therefore the selected embedding residual; the v1 embedding-plus-top result must not be substituted for the strict-v2 selection.

## Private artifact hashes

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
