# Historical 1–5 star recommendation replay

`engine/scripts/evaluate_historical_ratings.py` hides each rated book from its own reading history, scores it with the production ranker using books read on strictly earlier days, and compares the score with its actual 1–5 star rating. Books completed on the same day are hidden together. The script uses the existing content-hash checks and work deduplication, and prints aggregate results only.

Run against a private read-only snapshot containing reads and their cached embeddings:

```sh
cd engine
uv run python scripts/evaluate_historical_ratings.py /path/to/private-snapshot.json \
  --backend ollama --model qwen3-embedding:4b
```

The replay splits distinct dated rated works chronologically at whole-day 60/20/20 boundaries. The first period supplies expanding-history calibration examples; the second selects between the current and previous ranking formulas and linear or isotonic 0–100-to-star calibration by mean absolute error; the final period reports the selected choice and comparators. It reports star MAE/RMSE, Spearman correlation, graded NDCG@20, and AUC for 4–5 versus 1–3 stars. A prior-history mean and median are reference predictors. The median can minimize star error while carrying no useful book ordering, so error and ranking metrics should be read together.

On the 2026-09-22 private production snapshot, 1,773 distinct rated works passed date and vector validation; the usable split was 963 calibration examples, 355 validation books, and 355 later books. A 140-configuration similarity-kernel sweep selected 40 nearest rated books with positive cosine similarities raised to the eighth power by validation graded NDCG@20. The serving change adds 10 score points per star of difference between that neighbor-weighted rating and the reader's earlier average rating. It ramps the term up over the first 100 rated works. A 10-point strength was chosen from a small inspected grid to limit score movement while retaining an improvement in both validation and later-period metrics.

| Period | Formula | Star MAE ↓ | Star RMSE ↓ | Spearman ↑ | Graded NDCG@20 ↑ | 4–5-star AUC ↑ |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Validation | Previous production | 0.579 | 0.774 | 0.104 | 0.390 | 0.523 |
| Validation | Kernel adjustment | **0.575** | **0.771** | **0.108** | **0.488** | **0.528** |
| Later period | Previous production | 0.694 | 0.895 | 0.092 | 0.408 | 0.564 |
| Later period | Kernel adjustment | **0.688** | **0.889** | **0.110** | **0.455** | **0.581** |

Both formulas received their own linear score-to-star calibration fitted on earlier examples. On the later period, a paired book bootstrap gave a 95% interval of −0.0120 to −0.0001 stars for the MAE change and −0.0020 to +0.0362 for AUC. The AUC interval includes zero. The new formula lowered MAE in both halves of that later period and for books by previously seen and unseen authors. Error on 1–2-star books rose from 1.163 to 1.172 stars (53 books), while error on 4–5-star books fell from 1.061 to 1.038 (152 books); the aggregate gain favors highly rated books. Discrimination of 1–2-star books from the rest fell slightly (AUC 0.530 to 0.520), and the number of low-rated books in the bottom 20 fell from four to three. On a broader diagnostic pool of 863 active-status candidates with valid vectors, four of the previous top eight remained in the new top eight; this is a meaningful slate change, and the pool was not a reconstruction of every production eligibility rule.

The prior-history median still had lower later-period MAE (**0.687**) than either ranking formula, but no discriminating rank signal (AUC **0.500**). This is why the benchmark reports both rating error and ordering quality.

This is one installation's historical record. Its books were selected by the reader, and current cached embeddings may postdate some read dates. The later period had already been inspected during earlier research, and the kernel/strength grid involved many comparisons, so the result is exploratory rather than untouched confirmation. The book bootstrap ignores temporal dependence. The replay tests preference among read books, not candidate retrieval or benefit to other readers. No private titles, ratings, or embeddings are committed with this document.
