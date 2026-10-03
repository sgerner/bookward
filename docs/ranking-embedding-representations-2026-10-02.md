# Embedding representation screen: October 2, 2026; strict-v2 rerun October 3

The strict-v2 rerun is the primary representation result below. The initial v1 run checked read identity and a nonempty matched work ID but did not verify each description/subject field against the frozen field hash, provider, confidence, and synopsis rules. Its metadata results are retained later as a superseded diagnostic and must not be used for provenance-verified claims.

This is development evidence only. Historical labels were inspected in earlier studies, the 44 fresh blinded judgments were excluded from every representation choice and score, and the target set is a synthetic read-as-candidate pool rather than observed recommendation slates. Strict-v2 verification and the selection rule were frozen before its embeddings and scores were produced.

## Strict-v2 field-provenance result

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

## Superseded v1 identity-matched metadata diagnostic

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

## Execution-matched direct control

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

## Model and input limits

The BGE arm used the existing FastEmbed 0.8.1 adapter and the official [BAAI BGE small English model card](https://huggingface.co/BAAI/bge-small-en-v1.5). Its private ONNX artifact was 66,465,124 bytes (SHA-256 `51f1bd0addd6e859e42c2c8021a5e5461385bb676a649f4b269aa445449f2431`); total unique cache size was 67,181,387 bytes, under the frozen 100 MiB limit. FastEmbed reports 384 dimensions and a 512-token input limit. The tokenizer found 0 of 1,777 title-author inputs over 512 tokens and 16 of 1,777 verified-metadata inputs over the limit (maximum 916); the encoder truncates those longer descriptions. No text rule was adjusted after seeing scores.

The Qwen instruction format followed its model card: task instruction on target queries only, with history documents unprefixed. The fixed task was “Given a book a reader enjoyed, retrieve other books with similar likely reader appeal.” The four Qwen embedding calls used Ollama 0.30.5 and together took about 210 seconds on the observed local runner. BGE encoded 3,554 text-view documents in about 131 seconds. TF-IDF was fit separately within five causal cuts for each text view.

## Coverage, limitations, and artifacts

The selected arm’s development OOF familiar/unfamiliar-author splits were small and unstable across folds, so they are retained as descriptive slices in the private aggregate rather than used to qualify the result. The full fold metrics include high AUC, reversed low AUC, favorite precision@8/@20, disliked inclusion@8/@20, and NDCG@8/@20. Whole-day, 30-day-block, and author-group paired resampling used 2,000 replicates.

Private row-aligned evidence is stored outside the repository. The mask-aligned score archive has 1,677 IDs and includes exact `train_mask`, `validation_mask`, `later_mask`, `oof_selection_mask`, and `record_indexes` arrays. `selected_direct_replacement_score` is finite on all 1,677 targets. `selected_current_plus_representation_oof_score` and `selected_representation_ridge_oof_score` are NaN on the first 300 fit-only rows and finite on the remaining 1,377 held-out targets. `selected_capped_residual_score` is finite on all 1,677 targets: it equals the stored current score on the first 300 fit-only rows and uses fold-specific capped residual predictions on the remaining rows. No books, titles, labels, vectors, or candidate IDs are included in this report.

The original study and BGE/integration amendments were frozen before experiment embeddings and scores. The strict-v2 and genre-verification amendments are preserved with the private artifacts; the v1 score archive remains available only for audit and must not be treated as the corrected strict-metadata output. This study did not switch a model or model setting.
