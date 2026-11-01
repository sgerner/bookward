# Jules insight review — 2026-10-31

This ledger records the CI and product insights reviewed for Bookward, the
current disposition, and the reason for each decision. “Accepted” means the
small change is in scope or underway; it does not imply a test or release was
run. The changes were validated as recorded below; declined portions remain unchanged.

## CI insights

| Insight | Disposition | Rationale and evidence |
| --- | --- | --- |
| Add a linter despite 317 existing findings | **Rejected for this change** | Enabling a strict gate would turn a bounded CI review into broad cleanup. The current workflow already runs Svelte/type checks, unit tests, production build, engine tests, compilation, integration smoke, and container builds. A linter can be considered separately with a baseline and focused cleanup. |
| Add a pre-commit hook | **Rejected** | This is optional local tooling and does not close a demonstrated CI gap. It would add setup/configuration for contributors without improving the existing CI checks. |
| Rebuild the web app in the smoke job | **Accepted** | CI uploads the production `build/` from the web job and downloads it in smoke, avoiding a second build. The artifact uses pinned actions and one-day retention. The current adapter-node output contains no hidden files required by `node build`; upload-artifact’s hidden-file exclusion remains enabled. See [ci.yml](../.github/workflows/ci.yml). |
| Add release provenance and an SBOM | **Partial** | This is a public repository and build-push-action already emits max-mode provenance by default for public repositories, so an explicit provenance input would be redundant. SBOM generation is opt-in, so `sbom: true` was added to both GHCR image builds. See [publish.yml](../.github/workflows/publish.yml). |
| Stop canceling in-progress main CI runs | **Accepted** | `cancel-in-progress` is now false for `refs/heads/main` and remains true for other refs. This preserves a running main push while retaining cancellation for superseded branch and PR runs. |
| Limit `packages: write` to publishing | **Accepted** | The workflow-level write permission was removed and granted only to the `publish` job. Validation and the reusable CI workflow keep read-only contents access. See [publish.yml](../.github/workflows/publish.yml). |
| Add migration, restore, and interrupted-restore steps to smoke | **Rejected** | Existing engine tests cover schema migrations and backup/restore recovery, including interrupted-restore recovery (`engine/tests/test_engine.py`, `test_profiles.py`, `test_enrichment_storage.py`, and `test_backups.py`). Duplicating those focused checks in the end-to-end smoke job would add setup without covering an untested path. |
| Add browser E2E infrastructure | **Rejected** | `scripts/community_smoke.py` already exercises setup, sign-in/out, authenticated views, and authentication boundaries over HTTP. The project has no Playwright/Cypress dependency; adding a browser runtime for this bounded review is not justified. |

## Product and evaluation insights

| Insight | Disposition | Rationale and evidence |
| --- | --- | --- |
| Add four discovery-pool health metrics | **Rejected for now** | The engine already exposes candidate-quality statuses, identifier/catalog-match counts, and flags through `/api/candidates/quality`; source cards show each source’s last scan and status. A new summary has no agreed metric definitions or denominators, and could duplicate those signals. Do not invent pool-health measures until there is a specific user decision they should support. |
| Bound the decisions payload | **Accepted** | The overview now returns 24 decisions plus `decisions_has_more` from a limit+1 query; the History view loads later pages through the existing `status=decisions` endpoint. Search/filter copy says it applies to loaded decisions, and partial-page counts were removed. This bounds the decision slice without imposing new paging semantics on other overview data. |
| Physically delete custom sources | **Rejected** | Disabling a source stops future scans while retaining its candidates and provenance. A physical delete conflicts with candidate-to-source references and could damage historical attribution. No current use case demonstrates a need for a separate archive/tombstone lifecycle. |
| Add blanket Mark Read undo | **Rejected as a low-risk suggestion** | Mark Read can create or update an existing read, rating, candidate status, learning event, and attributed outcome. A safe undo needs a complete prior-state snapshot and concurrency semantics; a generic undo could overwrite imported read metadata. Keep the current operation until that contract is designed. |
| Add a global background-job failure dashboard | **Rejected for now** | The relevant failure states are already exposed where readers act: source scan status on source cards, backup errors in recovery settings, digest delivery failures with retry, and immediate job failures from manual actions. A general dashboard would duplicate these surfaces without a defined retry/action model. |
| Import Goodreads `to-read` rows as a backlog | **Partial** | The importer remains a read-history importer; to-read titles should not be inserted as completed reads or treated as recommendation outcomes. Settings now labels this “Read-shelf history” and says it imports books on the Goodreads read shelf, with ratings and read dates. A separate shortlist import can be reconsidered with explicit semantics. |
| Require held-out evaluation and reader-metadata cohorts before serving promotions | **Partial** | The evaluation report uses chronological 60/20/20 run splits and run-cluster bootstrap, and now distinguishes intent from explicit ratings. The metric-threshold helper is not a serving gate and has no production caller. No automatic promotion or cross-profile telemetry is added; any serving change still needs an untouched later set, paired challenger/champion uncertainty, and relevant per-profile cohorts. Existing experiments include candidate-metadata coverage cohorts, not general reader-metadata cohorts. |

## Engine changes retained or reverted during this review

| Change | Disposition | Rationale and evidence |
| --- | --- | --- |
| Include CSV row number and title in invalid-rating errors | **Accepted** | The importer reports which logical CSV record failed and states that the import was rolled back. The transaction keeps the import atomic; `engine/tests/test_engine.py::test_invalid_goodreads_rating_rolls_back` checks that no earlier row remains. |
| Describe digest score as a ranking score, not a percentage | **Accepted** | The displayed score is a 0–100 ranking signal, not a probability. The digest now says “ranking score” without `%`; `engine/tests/test_digest_audit.py` guards the wording. |
| Add an author/subject detail to personalization explanations | **Accepted** | The detail is chosen from the strongest feature whose signed effect agrees with the actual score movement. Semantic-only adjustments retain the general explanation rather than claiming a specific seed-book match. See `engine/afterword_engine/interaction_personalization.py`. |
| Preserve all outcomes and report per-signal diagnostics | **Accepted** | `load_rows()` keeps observed outcomes, and `signal_diagnostics()` selects one strongest/latest outcome per impression within each signal so repeated events do not multiply evidence. The overall row still chooses one primary outcome. See `engine/afterword_engine/evaluation.py`. |
| Report enjoyment using explicit ratings only | **Accepted** | The `enjoyment` slice includes positive, negative, and neutral rating outcomes, excluding saves and unrated completions. The report labels other logged-policy metrics as mixed intent/outcome signals. |
| Clarify the promotion helper’s scope | **Accepted** | Its docstring states that metric/sample thresholds do not authorize serving promotion and callers must check held-out provenance, paired uncertainty, and cohort regressions. The helper has no production caller; this is a guardrail description, not an automated gate. |
| Batch every read’s impression history into one in-memory map | **Rejected / reverted** | The query batches limited candidate IDs but can still retain every matching impression row at once; the exposure history has no global retention bound. The prior indexed per-candidate lookup remains until a bounded query or measured performance need justifies a safer optimization. |

## Frontend changes retained or declined during this review

| Insight | Disposition | Rationale and evidence |
| --- | --- | --- |
| Fetch overview and backup status concurrently | **Accepted** | The independent requests start together; backup failure still falls back to the existing empty status while overview failure remains fatal. |
| Reuse recommendation-to-page formatting and cache history dates during sorting | **Accepted** | `+page.server.ts` now uses `toPageBook`; the history comparator parses each row’s date once per derived sort pass. |
| Bound decisions and avoid implying filters search unloaded items | **Accepted** | See the Product and evaluation table. The client exposes an explicit Load more action and labels the loaded search scope. |
| Add reusable modal focus handling and fix Librarr Escape behavior | **Accepted** | Both dialogs use a shared focus action that skips hidden, disabled, inert, and screen-reader-hidden controls, traps Tab, selects a visible initial target, and restores connected focus. Librarr no longer stops Escape before the page handler. |
| Align Mark Read behavior and theme contrast | **Accepted** | Discover and Shortlist open the same rating dialog. Its submit button uses the Skeleton filled-primary token instead of hard-coded white text. |
| Keep notifications accessible and usable | **Accepted** | A permanent live region announces updates; Undo notifications remain until dismissed or used, and timed notices pause while hovered or focused. |
| Add skip links and focusable main landmarks to secondary routes | **Accepted** | Account, admin, login, error, and the home view have skip targets and focusable semantic `<main>` elements. |
| Show source scan error details | **Accepted** | The UI displays a capped, escaped message from the engine’s credential-sanitized `safe_error_message` path instead of only “Needs attention.” |
| Describe Librarr as configured and provide a setup shortcut | **Accepted** | The badge reflects saved API-key configuration, not a live connection probe. The unconfigured Shortlist action opens Settings at the Librarr form. |
| Add recovery action for an exhausted Discover feed | **Accepted once for duplicate insights** | A contextual Refresh recommendations button appears only after the feed is exhausted without a search or digest filter; existing paged-load retries remain separate. |
| Add status-specific 404 recovery | **Accepted** | The error route explains a missing page and links to Bookward home; 503 recovery copy remains distinct. |
| Let multiple recommendation explanations stay open independently | **Accepted** | Expanded details now track book IDs in a set, so opening one explanation does not desynchronize another. |
| Show a second mobile cover thumbnail | **Rejected for now** | Mobile cards already use a blurred cover background. Adding another image would repeat a deliberate visual element without fixing a functional gap. |
| Expand pagination to every overview section | **Rejected for this change** | Only decisions had an existing paged endpoint and a concrete unbounded payload. Other overview collections keep their current contract until their page behavior is designed. |
| Remove the unused Vitest greeting example | **Accepted** | The example and its self-only test had no imports or product role and were deleted. |

## Additional engine insights

| Insight | Disposition | Rationale and evidence |
| --- | --- | --- |
| Avoid idle worker lease writes and bound crashed-job retries | **Accepted** | `jobs.attempts` is added in migration 23 and increments on claim. The worker checks leases read-only, rechecks expiry in each recovery update to preserve a concurrent heartbeat, and fails a job after three expired attempts. The existing 300-second lease remains so another healthy process is not mistaken for a crashed one. `engine/tests/test_profiles.py` covers live leases, lease-renewal races, recovery, and the retry cap. |
| Reclaim every running job immediately at startup | **Rejected** | There is no durable owner signal that distinguishes a prior process from another healthy worker. Immediate reclamation could steal a live lease; safe recovery uses lease age and a 30-second reaper cadence. |
| Normalize each candidate vector once during slate diversification | **Accepted** | `discovery_slate.py` caches normalized and invalid/missing vectors for the slate operation. `test_candidate_vectors_are_normalized_once_per_slate` checks one norm calculation per supplied vector. |
| Apply read identity and duplicate-edition filtering consistently | **Accepted** | Serving and scoring use `ReadIdentityIndex`, including its title fallback when a read lacks an author. Recommended rows retain the highest-ranked matching identity. `engine/tests/test_identity.py` covers missing-author reads, same-work exclusion, and title variants; the manual-read ISBN test now expects an identical ISBN alias to be hidden before and after marking the representative read. |
| Allow a verified full synopsis to replace a legacy source blurb | **Accepted** | A replacement requires no field-level description provenance and an exact matching verified provider, provider ID, and synopsis kind. Regular source imports carry field provenance and therefore are not overwritten. `test_verified_catalog_synopsis_replaces_an_initial_source_blurb` uses different source/catalog opening text. |
| Reuse one recommendation request database connection | **Accepted** | Both recommendation endpoints pass the request connection through pool selection, interaction loading, vector loading, and evidence construction. `test_tracked_recommendations_reuses_a_supplied_database_connection` verifies the supplied connection is reused. |
| Skip embedding provider health checks for unrelated settings changes | **Accepted** | `/api/settings` checks the embedder only when a supplied embedding field changes; omitted fields retain stored values. `test_librarr_only_setting_update_skips_embedding_health` verifies a Librarr update. |
| Consolidate association text and ISBN presentation helpers | **Accepted** | Provider text cleaning/title normalization is centralized in `association_sources/common.py`; punctuation-only ISBN normalization is in `isbn.py` and remains separate from checksum-validating `canonical_isbn`. Provider and typed-retrieval script call sites use the normalization helper. |
| Remove the candidate-quality compatibility shim | **Accepted** | The obsolete `_needs_candidate_quality_audit` alias is removed; tests use `has_candidate_quality_audit_candidates` directly. |
| Cache decision-evidence source hashes per request | **Rejected as already covered** | `_runtime_source_lineage` already has `@lru_cache(maxsize=1)`, so the source hashes are computed once per process rather than for each request. |
| Cache database PRAGMAs and permission checks | **Rejected** | Foreign keys and busy timeout are connection-scoped, journal mode can change during restore/replacement, and database/profile/WAL permissions are re-enforced for safety. Caching risks stale state or skipping permission guards. |
| Batch all read-outcome attribution history in memory | **Rejected / reverted** | The history has no global retention bound; holding all matching outcomes would make memory use unbounded. The indexed per-candidate query remains until profiling supports a bounded alternative. |

## Evidence persistence batching

The proposal to replace individual evidence artifact/exposure statements with a
large batch was declined. These are local SQLite statements within existing
transactions, not network round trips. Artifact deduplication and bounded archive
limits already constrain the work. No measured regression was supplied; changing
idempotency and retention behavior without a bounded design would add risk.

## Verification record

`rtk engine/.venv/bin/python -m pytest engine/tests -q`: **534 passed** in
97.31s (two existing Starlette/httpx deprecation warnings). CI workflow YAML
parsing and whitespace validation passed during the workflow edit. Web
`rtk npm run check`: **0 errors and 0 warnings**; `rtk npm test`: **79 passed**;
`npm run build` with `ORIGIN=http://127.0.0.1:54567`: passed; `git diff --check`:
passed. A direct same-origin sign-in against the disposable production build
also passed. The root agent owns final integration status.

Manual browser checks on synthetic disposable data verified modal initial focus,
Shift+Tab/Tab wrapping, Escape dismissal and trigger-focus restoration, a four-star
Mark Read submission, and decision paging from 24 to all 30 seeded decisions.
Restoring a decision loaded on the second page also succeeded.
