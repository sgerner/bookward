# Production recommendation experiments — September 22, 2026

This study tests the recommendation research roadmap against a real production snapshot. It does not establish population-wide quality gains. The independent observation unit is one installation with a shared reading history.

## Data and protocol

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

## F0: evaluation correctness

The main-branch evaluator at `b9f557c` pooled observations across recommendation runs before truncating top-k, and removed unlabelled slots before computing position discounts. Three executable counterexamples demonstrate why its results cannot support model promotion:

| Case | Previous result | Correct diagnostic |
| --- | ---: | ---: |
| Two independent runs, each with one rank-1 label: one positive and one negative; precision@1 | 1.0 | 0.5 when macro-averaged over runs |
| Only labelled item is rank 25; precision@20 | 1.0 | Undefined: no observed labels inside top 20 |
| Only positive is at rank 20; observed-gain NDCG@20 | 1.0 | 1/log2(21), approximately 0.22767 |

In the actual snapshot, the old pooled report gives AUC=1 and NDCG@20=1 despite only ten labels (one positive, nine negative). This is evidence of unreliable measurement, not a successful recommendation engine.

The accompanying evaluator correction preserves logged ranks, computes top-k within runs, macro-averages only defined run metrics, and preserves repeated cluster draws during bootstrap. Its output is a descriptive, confidence/propensity-weighted observed-label diagnostic rather than a causal IPS estimate. Unknown outcomes remain unknown. The correction changes the production snapshot’s reported precision@20 from 0.10 to 0.125; its NDCG@20 remains 1.0 because the sole positive label is at the first observed slot in its run. Neither number is reliable evidence of recommendation quality with ten labels.

## Read-date coverage

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

The paired book-bootstrap 95% interval for AUC difference (current minus earlier) is −0.045 to +0.045 on validation and −0.047 to +0.034 on the later test. Both contain zero. The current neighborhood scorer therefore has no supported improvement on this refreshed retrospective comparison. These results differ substantially from the earlier [quality study](recommendation-quality.md); the datasets were captured at different times and the earlier study's exact private snapshot is not available here. Do not treat either test as an untouched confirmatory experiment. Investigate corpus, timestamp, and source-version differences before drawing conclusions from the historical discrepancy.

The feedback diagnostic includes 75 explicit latest-action candidates, with 41 saves. Its current-score precision@20 is 0.50 versus 0.45 for the older formula, while AUC is 0.541 versus 0.584. Those actions lack exposure logging in this historic feedback table, and the mixed metrics do not resolve which ranker readers prefer. No visible ranking switch is proposed.

## E08: slate diversity feasibility

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

## Decisions

- Correct mathematically demonstrated evaluation errors and slash-date parsing before using reports to select a production policy.
- Do not enable MMR from cosine improvement alone. Register a live qualified-save/satisfaction endpoint and adequate follow-up first.
- Do not promote another rating ranker from the refreshed single-installation retrospective comparison; it produced mixed validation/holdout results and is not independent live evidence.
- Do not turn the ten observed recommendation outcomes into a claim about all users.
- Preserve the current serving champion unless a candidate passes the registered ranking and reliability checks.
