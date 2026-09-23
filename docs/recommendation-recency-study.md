# Recency-weighted recommendation study

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
