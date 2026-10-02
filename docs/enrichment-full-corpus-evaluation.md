# Full-Corpus Enrichment Evaluation

This retrospective study tested whether verified catalog descriptions and subjects improve the current ESS5 recommendation scorer when used to represent a reader's history. The frozen comparison found no enrichment arm that met the preregistered gain criteria, so rich read embeddings and enrichment score blends remain disabled.

## Data and matching

The study used a frozen export of 1,797 library reads and 1,934 candidate books. It evaluated 1,777 reads with valid dates and ratings after excluding 9 unrated reads, 9 undated reads, 2 duplicate works, and no reads with a missing or stale current vector. The full capture included 1,590 reads with checksum-valid ISBNs; 928 received a strict Open Library identity match requiring agreement on ISBN, normalized title, and author. The capture returned a nonempty Open Library description for 682 matched records and subjects for 810. Seven additional verified local `Summary` sections brought the total descriptions to 689; their text is not included here. The enriched text retained 803 normalized subject sets, and 581 descriptions met the 300-character quality threshold. Opening sentences were tracked separately and were not treated as full synopses. Provider requests used ISBN only. Ratings, reviews, and read dates were excluded from queries and embedded text; local notes contributed only the seven verified Summary sections. No generated descriptions were used.

The primary held-out validation contains 324 valid-ISBN targets. It uses the full eligible ISBN cohort rather than the earlier 978-record Google-selected subset, which had materially different results and is reported only as a selection-bias diagnostic. A further 309 valid-ISBN targets form the later chronological period. Results on that later period are descriptive only because it had already been inspected before the final all-ISBN evaluation was frozen.

## Comparisons

Every arm used the current production ESS5 ranker, the production `qwen3-embedding:4b` model with 2,560-dimensional vectors, and the same held-out target labels. The fixed whole-day split used 100 warm-up records, 966 development targets, 355 validation targets, and 356 later targets; the primary ISBN cohort is a subset of the latter two groups. The `existing actual proxy` uses exported historical `read_candidate` query vectors, which are mostly title-and-author text. The export has no separate positive serving-score cache artifact, so this is a proxy for the historical serving representation, not proof of the cache that existed at each past date. One stale query hash was re-embedded from the current production query document.

The `corrected-capture query control` leaves read-history vectors title-and-author-only while representing each held-out candidate using its verified available description and subjects. This isolates the effect of using the richer current candidate query. Enrichment arms then compare that corrected control with description-only, subject-only, symmetric full-metadata history, and bounded score-fusion variants. Missing descriptions in the observed-content arm contribute no content vector; they are not silently replaced by title-and-author vectors. A no-description query receives no enrichment blend. The fixed score-fusion weights were 10% and 25%; a single preregistered quality gate and content-expert arm were also evaluated. All arms used confidence 1 to isolate representation changes.

The target is a historical read, not an observed recommendation impression. Chronological whole-day splits prevent the target rating from entering its own ranking context. The primary validation includes 76 high-rated targets, 61 low-rated targets, and 187 neutral targets. The metrics are high-rating AUC (4–5 vs. other ratings), low-rating AUC (1–2 vs. other ratings, reversed so higher is better), and their mean, balanced AUC. A 2,000-replicate whole-target-day bootstrap estimates validation uncertainty. Top-20 and bottom-20 precision are reported as secondary checks.

## Primary validation results

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

## Persistence rehearsal

An offline replay against a private copy of the production database exercised the actual resolver, strict identity verifier, and metadata persistence. It exposed 337 valid ISBN matches rejected by literal title comparison: every difference was a parenthetical numbered-series annotation. The pipeline now shares the existing conservative full-title identity key, removing only known format annotations while keeping substantive subtitles distinct. No ISBN or author requirement was relaxed.

The corrected rehearsal persisted 743 verified read-metadata records, including 610 descriptions, 707 subject sets, and 742 year-precision dates. Stored field provenance retained the original catalog request timestamps and identified `description` versus `first_sentence`, `subject`, and `first_publish_year` correctly. The source read rows and settings stayed unchanged, and SQLite integrity passed. This rehearsal used only cached public Open Library responses; the seven local summaries were excluded and Google Books was explicitly recorded as skipped rather than given fabricated responses.

A separate read-only test added these revalidated work IDs to the production read-exclusion keys. They caught zero additional books among the 403 active accepted candidates remaining after the existing read filter, so no cached-work exclusion change was adopted.

## Serving precision and limitations

The production scorer returns Python one-decimal scores. The replay preserved those component values as float64 before weighted fusion and applied Python `round(score, 1)` to each final score before computing metrics. Compared with the earlier NumPy float32 fusion path, the 10% arm's balanced AUC changes from 0.580707 to 0.580800; using unrounded fused scores instead would yield 0.580150. This precision sensitivity does not change the decision.

This is a single-reader retrospective evaluation. Read-as-candidate vectors approximate historical ranking inputs but do not represent a logged set of unread recommendations or user impressions. The strict ISBN match cohort is smaller than the full library, metadata coverage is incomplete, and catalog subject quality varies. A real-time randomized or prospective evaluation would be needed before claiming recommendation lift. The repository contains only aggregate counts and metrics; book identities, provider descriptions, ratings, and private vectors remain outside it.
