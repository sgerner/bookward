# Recommendation research roadmap: learning what each reader will value

**Status:** proposal for future implementation and experiments, not an implementation specification already deployed.

**Prepared:** 2026-09-22.

**Code reference:** Bookward `main` at `b9f557c` before the accompanying evaluator corrections. Recheck integration points before implementing a future experiment.

**Evidence:** source inspection, the existing September 20 production-snapshot study, and primary research linked below. No new production database was accessed, no production experiment was run, and no improvement claimed here has been measured.

**Navigation:** [priorities](#1-what-to-pursue) · [current system](#2-what-exists-and-what-the-evidence-actually-says) · [data and evaluation foundations](#3-foundations-required-before-trustworthy-experiments) · [18 experiment designs](#4-experiments-to-implement-and-test) · [counterfactual estimation](#5-counterfactual-estimation-exact-scope-and-limits) · [implementation architecture](#6-implementation-architecture-for-future-work) · [delivery plan](#7-ordered-delivery-plan-and-decision-gates) · [experiment template](#8-reusable-experiment-specification) · [completion checklist](#9-completion-checklist-for-each-future-implementation) · [research sources](#10-research-sources-and-boundaries).

## 1. What to pursue

Build an engine that improves **the probability of finding a book worth reading**, while respecting different tastes, limited reading time, sparse histories, local hardware, and uncertainty. A better average score is insufficient if cold-start readers, niche readers, or readers with limited metadata receive worse recommendations.

The most promising sequence is:

1. Make the production evidence trustworthy: correct evaluation, capture immutable decision inputs, distinguish exposure from visibility, and retain outcome timing.
2. Improve the information available to the existing cheap ranker: verified work metadata, separate content facets, and more balanced candidate discovery.
3. Learn a small, regularized model of each reader's preferences and uncertainty; represent multiple interests instead of collapsing them into one neighborhood.
4. Optimize the **slate** of recommendations, balancing relevance, useful novelty, breadth, and series constraints.
5. Learn through limited, explicitly randomized exploration and optional questions chosen for information value.
6. Add delayed-outcome models, drift adaptation, and population-level learning only as sufficient production evidence becomes available.

These are testable hypotheses. Mathematical sophistication is not a promotion criterion. Every approach must beat a simpler baseline on the same population and data budget, or deliver a measured benefit in cost, accessibility, coverage, or reliability without unacceptable quality loss.

### Experiment index

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

## 2. What exists and what the evidence actually says

### 2.1 Codebase map

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

### 2.2 Existing production evidence is historical and narrow

The [existing quality study](recommendation-quality.md) reports a September 20 snapshot with 1,834 read entries and 2,283 candidates, and evaluates cached `qwen3-embedding:4b` vectors. Its deduplicated rating test set contains 354 works from one reader. Neighborhood scoring improved descriptive test NDCG@20 from 0.622 to 0.896; the paired AUC-difference interval included zero. The study explicitly does not establish live acceptance, catalog discovery, or generalization across users.

Use that corpus as a regression reference, not a repeatedly reused tuning benchmark. Refresh coverage counts from new snapshots. Do not assume old findings about missing telemetry still apply: the current checkout contains telemetry added after that earlier audit.

### 2.3 What “all users” means in this architecture

An installation currently has shared reading/profile state. A browser session is neither a durable reader identity nor an independent reader. Multiple browsers using one library must not be counted as multiple users.

For initial work, improve the per-installation engine and evaluate on independently participating installations, reporting the actual unit as **installation/profile**. If an installation is shared by a household, record that limitation rather than attributing its history to one person. Before supporting separate people within an installation, add explicit profile ownership to reads, feedback, runs, events, and personalized candidate state; add isolation tests and a migration from the existing single profile. Public catalog/work metadata may remain shared.

Cross-installation analysis requires an opt-in study and pseudonymous installation identifiers. Raw histories need not be centrally collected: a common local evaluator can return bounded aggregate results. No result from one installation can justify “improves recommendations for all users.” The actionable standard is broad coverage, measured cohort effects, conservative fallback, and continued monitoring—not a guarantee of benefit to every individual.

### 2.4 Required reader coverage

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

## 3. Foundations required before trustworthy experiments

### F0. Repair and validate the evaluator

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

### F1. Define signals and denominators

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

### F2. Add a reproducible decision-data contract

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

### F3. Production-data acquisition and replay runbook

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

### F4. Shadow and live experiment protocol

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

## 4. Experiments to implement and test

All experiment cards inherit F0–F4, matched baselines, cohort reporting, cost measurement, and untouched-test requirements. Numerical grids are intentionally small starting candidates, not optimized settings. Do not launch the entire grid as simultaneous live experiments.

### E01. Resolve works as uncertain entities, not just title strings

**Hypothesis.** Edition mismatches and missing historical metadata create more ranking error than a more complex scoring formula can remove. Work-level identity improves both labels and discovery.

**Mathematics.** Model `P(same_work | ISBN, title, author, language, edition, series)` and assign three decisions: confident match, confident mismatch, or unresolved. Use precision-oriented thresholds, not forced nearest matches. A work is distinct from its translation, edition, format, and narrator; the right identity granularity depends on the decision.

**Implementation and real-data test.**

1. Reuse candidate-quality identifiers and existing normalization. Add a versioned work/edition map covering reads and candidates, plus evidence and resolution confidence. Consult verified provider records such as [Open Library work/edition APIs](https://openlibrary.org/developers/api); confirm current provider rules when implementing. For large historical enrichment, use permitted bulk datasets or approved batch access rather than thousands of single-book API calls; the current Open Library guidance distinguishes low-volume lookup from bulk access.
2. Build a stratified, privately reviewed match set from production: exact ISBNs, audiobook suffixes, omnibus/boxed sets, translated titles, author aliases, and ambiguous short titles. Mask experiment outcomes while reviewing identity.
3. Compare current identity, exact verified IDs only, and the conservative probabilistic resolver. Measure false merges, false splits, remaining repeats, excluded-valid-work rate, and label-attribution changes.
4. Re-run the champion on identical accepted works before and after enrichment, keeping identity changes separate from representation changes. Report all exclusions rather than silently improving the test population.

**Integration:** `identity.py`, `quality.py`, `covers.py`, `ingestion.py`, `database.py`; proposed work-resolution module. Metadata fetches run asynchronously with provider budgets and expiration.

**Ship/stop:** promote only after false-merge precision meets a registered audit target and repeats decrease without unacceptable catalog loss. Abstain on ambiguous mappings. Never let an uncertain match silently turn a book into a negative training example or erase a shortlist entry. All later experiments depend on this distinction.

### E02. Learn from multiple views of a book

**Hypothesis.** A reader may love a subject but dislike its treatment. Separating theme, prose, pacing, emotional tone, structure, length, and format can explain preferences that a single concatenated embedding obscures.

**Mathematics.** Use a late-fusion kernel `K_u(i,j) = sum_v w_uv K_v(i,j)`, where each normalized view has nonnegative weights summing to one. Keep author identity in a separate feature so thematic similarity cannot be dominated invisibly by author names. The [information bottleneck](https://www.princeton.edu/~wbialek/our_papers/tishby%2Bal_99.pdf) motivates retaining features predictive of utility while limiting irrelevant detail; it does not itself establish which book facets matter.

**Implementation and real-data test.**

1. Start with verified description, subjects, title/author, and available format/length metadata. Add speculative facets such as prose style only when grounded in auditable text or an independently checked extraction process. Missing is unknown, not a negative facet value.
2. Extend document construction with explicit view/version keys and separate cache identities. The current embedding primary key does not include view/version; migrate it or use a new feature-artifact table rather than overwriting one view with another.
3. Compare title/author-only, the current concatenation, enriched concatenation, and separate-view fusion on identical held-out works. This isolates the value of more metadata from the value of the mathematical representation.
4. Fit only a handful of fusion weights, with strong shrinkage. Test unfamiliar authors, missing descriptions, different languages when present, and local hashing versus stronger semantic embedding backends independently.

**Integration:** `scoring.document`, `cached_vectors`, `embeddings.py`, a proposed feature pipeline. Precompute expensive views; serving should reuse them.

**Ship/stop:** require gains beyond enriched concatenation at comparable cost. If the benefit disappears when author names are masked or fails on sparse metadata, retain the simpler representation. Do not market model-generated facets as catalog facts.

### E03. Bayesian ordinal preferences with partial pooling

**Hypothesis.** Learn a small reader-specific weighting of positive similarity, negative similarity, author familiarity, content facets, source, and uncertainty instead of fixing all coefficients for everyone.

**Mathematics.** For ratings r in 1…5, fit an ordinal model `P(r <= k | x,u) = sigmoid(tau_k - theta_u^T x)` with ordered thresholds. Use `theta_u ~ Normal(theta_global, Sigma)` only when a legitimate multi-reader training population exists. Within one installation, shrink toward fixed validated defaults. The posterior represents uncertainty; a high heuristic score is not a probability.

**Implementation and real-data test.**

1. Start with approximately 8–20 interpretable features and regularized logistic/ordinal regression, not a deep network. Normalize using training data only. Compare fixed neighborhoods, a global regularized model where available, and locally adapted parameters.
2. Model 3-star ratings as their own ordinal category. Fit saves and passes as separate auxiliary signals, never as converted 5/1-star ratings. Keep unknown ratings out of the ordinal loss.
3. Evaluate prefix-history sizes 0, 1–9, 10–49, and 50+ as prespecified analysis buckets; use learning curves to decide routing rather than treating these counts as guarantees of adequate support.
4. In multi-reader evaluation, freeze priors on training readers and adapt to held-out readers using only their earlier events. Compare score calibration and ranking, and stress-test contradictory ratings or a prolific single author.

**Integration:** proposed preference-model module behind the pure `rank_candidates` interface; versioned local artifacts; retain original score/explanation semantics for the fallback.

**Ship/stop:** require improvement or noninferior quality with useful calibration across history-size cohorts. Reject parameter instability or a model that only wins for heavy readers. Initial linear methods should remain CPU-friendly and explainable.

### E04. Represent a reader as a mixture of interests

**Hypothesis.** Someone who likes both literary fiction and space opera should not receive books halfway between them or have the smaller interest erased by the dominant one.

**Mathematics.** Fit a small mixture of taste components with weights pi_uk. Score utility as `sum_k pi_uk f_k(i)` and optionally test a temperature-controlled log-sum-exp alternative for “strong match to any interest.” Mixture weights express frequency/evidence, not rigid quotas. Cluster entropy can summarize breadth but should not automatically force broad recommendations.

**Implementation and real-data test.**

1. On deduplicated positive history, compare one component with two and four components, using deterministic spherical clustering and minimum support per component. Fit on earlier data only; retain negatives to distinguish disliked books within a liked topic.
2. Route sparse histories back to E03. Maintain minority components through smoothing, then allow an explicit current-interest choice to override recent behavior without rewriting lifelong taste.
3. Retrieve candidates from each component, then score and diversify their union. Separate this retrieval benefit from scoring by running a fixed-pool ablation.
4. Measure worst-interest recall, unfamiliar-author success, breadth of saved works, and irrelevant outliers. Evaluate stable specialists as well as broad readers.

**Integration:** proposed local profile-artifact builder; association seed selection; ranking features; optional UI context later.

**Ship/stop:** promote if minority interests become usefully represented and overall satisfaction remains noninferior. Merge poorly supported components; do not let clustering noise create invented identities or sensitive reader categories.

### E05. Distinguish enduring taste from temporary intent

**Hypothesis.** A temporary series binge should influence near-term recommendations without permanently deleting a reader's other interests.

**Mathematics.** Blend a long-term preference state with a short-term state: `theta_t = (1-g_t) theta_long + g_t theta_short`. Start with exponential event-time decay `exp(-age/tau)`; only then test a state-space filter or [Bayesian change-point detection](https://arxiv.org/abs/0710.3742) to change the blending weight when evidence supports a new regime.

**Implementation and real-data test.**

1. Use completion/event time and known availability time; never interpret a bulk CSV import as hundreds of new same-day preferences. Exclude undated events from time-sensitive updates or place them solely in the long-term profile.
2. Compare no decay, 90/365-day half-lives, and a two-timescale blend. Add change detection only if those baselines fail identifiable shifts.
3. Replay production histories sequentially and identify evaluation windows before examining model wins. Measure adaptation after a shift, recovery after a temporary binge, and stable-reader regressions.
4. Test import backfills, long gaps, and a new low rating for a previously liked author. Reset short-term intent after inactivity only through a registered rule.

**Integration:** profile feature computation and versioned state updates; never change historical records to implement forgetting.

**Ship/stop:** require sustained sequential gains beyond simple decay. False change alarms and sudden shortlist churn are stop signals. Sparse readers retain the stable model because absence of events is not evidence of changed taste.

### E06. Retrieve through a typed book graph

**Hypothesis.** A ranking model cannot recommend a valuable book absent from its candidate pool. Typed paths through works, subjects, authors, and series can discover useful candidates without a shared user graph.

**Mathematics.** Construct nonnegative, confidence-weighted transitions P and compute personalized diffusion `r = (1-alpha)s + alpha P^T r`, with seed vector s from earlier liked works. Use bounded diffusion depth/restart and relation-specific weights. A path explains retrieval evidence, not causation or guaranteed similarity.

**Implementation and real-data test.**

1. Build a sparse graph from verified work IDs and permitted association evidence. Separate `same_series`, `same_author`, `shared_subject`, and provider-recommended edges. Expire stale edges; do not persist an ephemeral provider response by laundering it into graph storage.
2. Compare existing seed associations, one-hop typed expansion, and diffusion with alpha in a small grid such as 0.3/0.6/0.85. Normalize node degree so broad subjects and famous authors do not absorb all traffic.
3. Prevent already-read works from appearing as outputs while allowing them to seed retrieval. Pass every result through quality, identity, source, and explicit user constraints.
4. Evaluate candidate recall on temporally eligible future positives, distinct eligible works per provider request, new-author coverage, and per-reader yield. Include a source-held-out test to detect a graph that simply reproduces one provider's list.

**Integration:** `associations.py`, adapters, `association_evidence`, asynchronous ingestion; proposed sparse graph cache. Retrieval stays outside the synchronous UI request.

**Ship/stop:** require useful added coverage at a fixed fetch/candidate budget. Avoid a graph neural network initially. [LightGCN](https://arxiv.org/abs/2002.02126) is a later collaborative-graph baseline if a real multi-user interaction graph becomes available; this metadata graph is a different object.

### E07. Allocate source budgets like a diversified portfolio

**Hypothesis.** Ten lists containing the same popular books supply less discovery value than several complementary sources with slightly lower individual yield.

**Mathematics.** Treat source selection as a budgeted coverage problem. A proposed objective combines expected qualified yield, marginal coverage of taste components, request cost, and redundancy. Estimate source overlap covariance only as a diagnostic; correlated lists are not independent confirmations of quality. A Beta-Binomial yield model can shrink noisy source estimates.

**Implementation and real-data test.**

1. Extend provenance to retain all contributing sources for a work; the current candidate `source_id` alone cannot describe a multi-source union. Attribute retrieval contribution separately from user-outcome credit.
2. Compare equal budget, manual source weights, greedy marginal unique coverage, and uncertainty-aware allocation. Keep hard limits per provider and minimum exploration budgets for new permitted sources.
3. Use historical first-seen times and frozen catalogs to estimate unique eligible additions. Measure overlap before spending extra requests. Audit sources that appear low-yield only because their books were never exposed.
4. In shadow, reallocate the same total request/enrichment budget. In live tests, hold the scorer fixed and measure satisfied discovery plus catalog starvation for niche interests.

**Integration:** association/source scheduler, provenance schema, candidate generation. Do not immediately replace manual source trust settings with a learned preference score.

**Ship/stop:** require added useful coverage or equal quality at lower provider cost. Keep reliability and factual quality separate from popularity. Stop if automated allocation converges to one source or eliminates newly introduced sources before they receive measurable opportunities.

### E08. Recommend a useful set, not eight near-duplicates

**Hypothesis.** A slate with complementary good choices helps more readers than the individually highest scores when those choices repeat the same author, series, or idea.

**Mathematics.** Baseline maximum marginal relevance selects `argmax_i [lambda relevance(i) - (1-lambda) max_(j in S) similarity(i,j)]`. Test a nonnegative facility-location coverage objective for diminishing returns. Then test a fixed-size determinantal objective `log det(L_S + epsilon I)`, with `L = diag(q) K diag(q)` and positive-semidefinite similarity kernel K. [Fast greedy DPP inference](https://arxiv.org/abs/1709.05135) provides a practical method; it is greedy inference, not a universal exact global optimizer.

**Implementation and real-data test.**

1. Compare score-only, author/series caps, MMR, facility-location coverage, and DPP on the same top-50/100 candidate pool and same display size. Enforce eligibility before optimization.
2. Scale relevance comparably to similarity; use validation to choose the relevance/diversity tradeoff. Build K from normalized features with a PSD construction rather than an arbitrary similarity table. Handle duplicate vectors and singular matrices.
3. Add a relevance floor or bounded score sacrifice, and a deterministic fallback when too few eligible diverse works exist. Sparse catalogs may return fewer choices; never violate a user's hard constraints to fill a slate.
4. Measure intra-list redundancy, taste-component coverage, new-author saves, qualified saves, and later ratings. Include readers who deliberately want a single series; excessive diversity can be harmful.

**Integration:** proposed pure slate-selection layer after scoring and before `tracked_recommendations` records the final order; `digest.py` needs a separately evaluated surface policy.

**Ship/stop:** retain a simple author cap/MMR if it matches DPP. Diversity exposure alone is not success. Validate added latency at actual pool sizes and retain reproducible tie-breaking.

### E09. Use optimal transport to balance interests gracefully

**Hypothesis.** Matching a reader's broad taste distribution can avoid both overspecialization and arbitrary quotas. Nearby themes should substitute more easily than unrelated themes.

**Mathematics.** Let p be a smoothed target distribution over verified taste facets and q(S) the slate's facet distribution. Optimize `sum_(i in S) utility(i) - lambda W_C(p,q(S))`, where W_C transports mass with semantic cost C. Entropically regularized [Sinkhorn transport](https://arxiv.org/abs/1306.0895) can make the calculation practical. This application is a proposed experiment, not a result from that paper.

**Implementation and real-data test.**

1. Derive p from earlier positive evidence, shrink for sparse readers, and permit explicit “explore more” intent. Do not require every new slate to reproduce historical proportions.
2. Start with 10–30 auditable facets and a validated ground-cost matrix. Compare no calibration, genre quotas, smoothed Jensen–Shannon divergence, and transport on the same candidate pool.
3. Treat multi-label books as fractional mass and include unknown metadata explicitly. A relaxed optimization requires a deterministic rounding stage back to actual eligible works; evaluate the rounded slate, not only the relaxed objective.
4. Report taste coverage, unwanted topic drift, relevance loss, minority-interest saves, and solver time. Test absent facets, specialist readers, and p concentrated on one subject.

**Integration:** E08 slate layer; cached facet vectors and transport costs. Use only if the metadata is sufficiently trustworthy.

**Ship/stop:** the semantic-distance benefit must beat simple quotas/divergence penalties. If metadata gaps dominate transport cost or tuning is unstable, defer. Historical exposure bias in p must not be mistaken for a user's desired future reading distribution.

### E10. Explore where a recommendation can teach the most

**Hypothesis.** Carefully chosen unfamiliar books can reveal whether a reader likes a topic, style, or author, improving future decisions with fewer disappointing exposures than uniform exploration.

**Mathematics.** Information-directed sampling balances expected regret and information gain about the best action; see [Russo and Van Roy](https://arxiv.org/abs/1403.5556). Begin with a simpler explicit mixture policy `b(a|x) = (1-epsilon) pi_champion(a|x) + epsilon q(a|x)`, with a tractable, normalized exploration distribution q. A conservative reward budget is inspired by [Conservative Bandits](https://proceedings.mlr.press/v48/wu16.html); its formal guarantees do not automatically transfer to delayed, misspecified book preferences.

**Implementation and real-data test.**

1. Start with a single designated exploration slot among near-qualified candidates and a frozen prefix. Compare current uniform-tail exploration, uncertainty-weighted exploration, and an information-gain approximation from E03. Register a score-sacrifice/utility budget.
2. Log the complete supported choice set and exact action probability. For sequential slate sampling, log conditional probabilities at each draw; do not multiply marginal slot probabilities. Avoid claiming exact Thompson-sampling propensities from a single posterior draw; use an explicitly computable policy initially.
3. On production contexts, use simulation only for correctness and stress testing. Then measure actual learning curves on randomized traffic, delayed regret, later qualified saves, and posterior predictive improvement per exploratory exposure.
4. Update only with observed, correctly timed feedback. Do not reward exploration simply for provoking clicks or uncertainty. Allow a reader to select conservative recommendations.

**Integration:** `exploration.py`, F2 logging, model state, assignment routing. Existing tail exploration only supports permutations inside the already-returned tail; it cannot identify effects of moving a new item into the protected top four or retrieving unseen books.

**Ship/stop:** require faster verified learning within the registered harm budget. Roll back for repeated poor outcomes, probability errors, or model overconfidence. Keep exploration optional and retain deterministic serving as a supported mode.

### E11. Ask the question that most changes the next decision

**Hypothesis.** One well-chosen optional comparison may teach more than asking for many ratings, especially for a new reader.

**Mathematics.** Choose a question q by expected information gain `H(theta|D) - E_y H(theta|D,q,y)` minus an interaction-cost penalty. A practical linear-model approximation uses the increase in `log det` of the information matrix. A pairwise Bradley–Terry likelihood can model “which would you prefer?” without pretending the answer is a completed-book rating.

**Implementation and real-data test.**

1. Offer small, skippable choices using verified book descriptions or plain facet choices. Include “neither,” “both,” and “not sure” where appropriate; unknown books must not force arbitrary preferences.
2. Compare no prompt, one random balanced question, and an uncertainty-targeted question. Start with one optional question per onboarding episode and a strict later frequency cap.
3. Store elicitation events separately with question version, presented options, order randomization, answer, and skip. Keep display framing comparable so wording does not explain the entire effect.
4. Measure answer/skip rate, time burden, first useful save, and later preference accuracy. Analyze everyone assigned the prompt policy, not only those who answered; respondents are a selected sample.

**Integration:** new UI interaction and event type, E03 likelihood/update path, profile settings. The no-history fallback must remain usable without answering.

**Ship/stop:** require net improvement after abandonment/burden effects. Do not optimize questionnaire completion as the goal. If a plain “what are you in the mood for?” control performs equally well, prefer it.

### E12. Model the path from interest to reading over time

**Hypothesis.** A save, acquisition, completion, and high rating represent different stages. Slow readers and books with long acquisition delays should not be penalized as failures.

**Mathematics.** Fit separate but related probabilities for intent and satisfaction, plus a discrete-time completion hazard `h(t|x)=P(T=t | T>=t,x)`, giving `S(t)=product_(s<=t)(1-h(s|x))`. Right-censored cases contribute survival likelihood, not a negative satisfaction label. [Multi-behavior recommendation](https://arxiv.org/abs/1809.08161) and [survival modeling](https://arxiv.org/abs/1809.02403) supply methodological precedents; begin with simple logistic heads and discrete-time hazards rather than their neural complexity.

**Implementation and real-data test.**

1. Preserve all event families, repeat-exposure episodes, import cadence, and observation cutoff. Define an episode at first qualifying exposure and avoid crediting one read to every repeated impression.
2. Compare separate regularized classifiers, a shared-feature multi-task model, and a completion-time model. Do not assume every completed book was first saved; user paths can skip stages.
3. Distinguish completion time from observation/import time. Date-only observations can be interval-censored; irregular imports can create observation censoring that a standard independent-censoring assumption does not resolve.
4. Evaluate calibrated completion incidence at 30/90/180 days, satisfaction when observed, and useful recommendations per reader-period. Compare fast/slow import and reading cohorts using pre-treatment history.

**Integration:** `learning.py`, event-history export, proposed outcome models. Add features for format availability only when known at the decision time; acquisition failures should not teach “dislikes this subject.”

**Ship/stop:** require gains over simpler separate endpoints, with sensitivity to informative censoring and missing ratings. Never deploy a “fastest to finish” objective that crowds out longer or more challenging books the reader values.

### E13. Optimize for underserved cohorts, not just the average

**Hypothesis.** A model selected on total interactions will favor prolific readers and well-described mainstream books. Explicit cohort constraints can improve readers the average hides.

**Mathematics.** Compare ordinary reader-weighted empirical risk with a regularized worst-group objective `min_theta max_g E[loss(theta)|g]`, or constrained optimization with bounds on each cohort's regression. [Group distributionally robust optimization](https://arxiv.org/abs/1911.08731) motivates this approach and highlights the importance of regularization; it does not guarantee gains for small Bookward cohorts.

**Implementation and real-data test.**

1. Define cohorts using pre-treatment product-relevant data: history size, activity, specialist/broad interests, metadata completeness, preferred language/format when explicitly known, and embedding/backend resource tier. Do not infer protected demographics from reading history.
2. Give readers bounded/equal influence before adding DRO. Compare reader-weighted ERM, balanced cohort sampling, and a strongly regularized robust objective. Cap influence from tiny noisy groups and report their uncertainty instead of overfitting them.
3. Hold out complete readers and inspect intersections only where support permits. Use repeated training splits for robustness; keep one final untouched test population.
4. Report mean, lower-decile reader utility where estimable, worst sufficiently supported cohort, uncertainty, fallback coverage, and fraction of readers with reliable evidence of harm. Individual treatment effects are generally not identified from a standard parallel A/B test; do not label raw per-person differences as causal effects.

**Integration:** model-selection and training weights first, runtime routing only if separately validated.

**Ship/stop:** prefer a simple balanced model when it performs as well. If some cohort cannot be evaluated, retain its champion fallback and explicitly withhold a universal-benefit claim. This experiment requires independent readers; slicing one person's books is not equivalent.

### E14. Know when the engine does not know

**Hypothesis.** Calibrated uncertainty helps choose between personalization, trusted defaults, asking a question, and showing fewer low-confidence digest items.

**Mathematics.** Fit calibration on a separate held-out period for a precisely defined endpoint, using logistic calibration or isotonic regression when data supports it. Evaluate Brier score and reliability curves. Explore [conformal risk control](https://proceedings.iclr.cc/paper_files/paper/2024/hash/f3549ef9b5ff520a7e41ff3cc306ab2b-Abstract-Conference.html) only with its bounded-loss and calibration assumptions explicitly checked; adaptive exposure, drift, and sparse groups defeat casual distribution-free claims.

**Implementation and real-data test.**

1. Keep raw ranking scores and endpoint probabilities separate. Never divide the current 0–100 score by 100 and call it likelihood of enjoyment.
2. Compare no calibration, global calibration on appropriate participating-reader data, and shrunk local calibration. Fit the calibrator without using the final evaluation labels. Keep save probability distinct from satisfaction conditional on rating.
3. Define a low-confidence fallback: champion ranker, trusted-source selection, optional question, or smaller digest. Evaluate the whole policy, including who receives fallback and who receives no recommendation.
4. Measure calibration by cohort, risk-versus-coverage curves, starvation, and decision utility. For conformal-style experiments, empirically audit coverage/risk in later time blocks; do not claim per-reader guarantees from population-average calibration.

**Integration:** score metadata, E03 posterior, slate/digest selection, explanation wording.

**Ship/stop:** uncertainty must improve decisions or transparency without denying service to sparse readers. Do not reuse an old digest threshold such as 80 on a new calibrated scale; register threshold selection and digest volume as a distinct policy experiment.

### E15. Recommend the next appropriate step in a reading journey

**Hypothesis.** The best next book depends on prerequisites, series position, format, and recent repetition, not just isolated item relevance.

**Mathematics.** Represent verified series/prerequisite relationships as a directed graph. Optimize a short horizon `sum_t gamma^t U(i_t | history_t)` subject to hard eligibility and user constraints. Start with deterministic next-unread rules and receding-horizon planning; a general reinforcement-learning system is premature without reliable trajectories and a valid environment model.

**Implementation and real-data test.**

1. Resolve verified series order, companion/standalone exceptions, and edition/format availability. Make strict ordering a reader preference; publication order is not always intended reading order.
2. Compare current ranking, a first-unread-series rule, and a two-step plan with a repetition penalty and optional “continue series” intent.
3. Treat availability as a choice constraint or acquisition friction, not dislike. Never infer ownership or completion solely from an import request.
4. Replay eligible production states and audit ordering errors. Live endpoints include useful saves, series continuation, explicit skips, and long-term satisfaction; longer sessions are not inherently better.

**Integration:** E01 metadata, eligibility/slate layer, optional context controls. Preserve saved/imported records regardless of their discovery eligibility.

**Ship/stop:** unknown order falls back to unconstrained ranking with no invented series claims. Reject planning if a simple next-unread rule matches it or if it traps readers into endless continuation at the expense of desired discovery.

### E16. Use LLMs where grounded reasoning adds measurable value

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

### E17. Turn explanations into useful preference controls

**Hypothesis.** A reader can correct a model faster when it exposes a specific, grounded reason and permits a reversible adjustment.

**Mathematics.** For an interpretable scorer, compute feature contributions or a constrained counterfactual change: what change in an editable preference would alter this rank? This is a model sensitivity explanation, not a causal statement about why the person will enjoy a book.

**Implementation and real-data test.**

1. Continue naming actual supporting reads, as the champion already does. Add verified facets only when the evidence exists. Separate reasons for candidate retrieval from reasons for ranking.
2. Offer scoped controls such as “less of this topic,” “not now,” “already read,” or “continue this series.” Distinguish temporary intent from enduring preferences and identity corrections.
3. Store the chosen control, scope, expiry, evidence version, and undo event. Do not convert every “not now” into a low rating or alter the underlying imported history.
4. Compare grounded explanations alone with the same explanations plus controls. Measure comprehension with a small optional sample, subsequent relevant saves, corrections, undo rate, and repeated unwanted recommendations. Keep initial slates identical to isolate control effects.

**Integration:** `ranking.py` explanations, UI, new event semantics, preference model, identity correction queue where relevant.

**Ship/stop:** require improved subsequent outcomes or demonstrably better user control. More persuasive explanations that only increase immediate clicks are not enough. User controls take precedence over inferred preferences and should be easy to reverse.

### E18. Learn shared priors without centralizing everyone's library

**Hypothesis.** Sparse readers can benefit from patterns learned across opt-in installations while retaining local personalization and an offline-capable engine.

**Mathematics.** Begin with shared low-dimensional priors/hyperparameters and local adaptation. If scale later justifies it, compare item-neighborhood collaborative filtering, regularized matrix factorization, and a graph baseline before neural sequence models. [Federated averaging](https://arxiv.org/abs/1602.05629) is a possible training mechanism, not a privacy guarantee; updates can leak information and non-IID readers can suffer negative transfer.

**Implementation and real-data test.**

1. Establish explicit participation, profile boundaries, verified cross-installation work identity, contribution caps, and deletion/retraining behavior. Keep nonparticipants on the complete local engine.
2. First distribute a frozen public/opt-in prior and collect only local aggregate evaluation summaries. Compare local-only defaults, shared prior plus local adaptation, and centrally evaluated opt-in baselines where authorized.
3. Hold out entire installations and test niche, sparse, different-language, and low-resource readers. Weight participating readers rather than event volume so one large library does not dominate.
4. Only then consider federated training with secure aggregation, minimum cohort sizes, authenticated update validation, and an explicit privacy threat model. If differential privacy is used, register clipping, noise, epsilon/delta accounting, utility loss, and membership-risk evaluation; do not label raw federated updates anonymous.

**Integration:** separate optional research/training service and signed/versioned prior artifacts. Do not add a remote dependency to the serving path.

**Ship/stop:** require held-out-reader improvement beyond locally shrunk E03, reasonable participation/communication cost, and acceptable privacy properties. Insufficient independent installations or negative transfer is a reason to defer, not to invent synthetic users and declare success.

## 5. Counterfactual estimation: exact scope and limits

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

## 6. Implementation architecture for future work

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

## 7. Ordered delivery plan and decision gates

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

## 8. Reusable experiment specification

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

## 9. Completion checklist for each future implementation

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

## 10. Research sources and boundaries

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
