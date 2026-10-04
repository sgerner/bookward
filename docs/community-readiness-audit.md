# Community readiness audit — 2026-10-04

This audit reviewed the web application, API proxy, authentication and profile boundaries, persistence and jobs, ingestion, recommendation numerics, notifications, and release configuration. The audited fixes were merged to `main` in PRs #150 (frontend), #149 (engine), and #151 (release safeguards). The pre-existing edit to `docs/scoped-user-profiles.md` was preserved and excluded from those PRs.

## Concrete issues and repairs

| Issue | Impact | Repair and regression coverage |
| --- | --- | --- |
| Running jobs were requeued on every worker poll | Competing workers could repeat ingestion, scoring, or external deliveries | Renewable leases, expired-job recovery, and per-claim completion fencing; competing-worker and stale-result tests |
| Goodreads re-imports erased known completion dates | Missing or malformed feed dates removed recency and outcome evidence | Validate incoming dates and retain the previous date when incoming evidence is missing; CSV/RSS re-import tests |
| Malformed CSV rows could raise internal exceptions | Truncated or invalid exports could fail as server errors | Normalize missing fields and return validation errors for unsupported encoding, ratings, and malformed input |
| Vector normalization could overflow on finite embeddings | Valid vectors could silently become zero vectors and neutral rankings | Scale before normalization, validate shape and finite values, and test extreme magnitudes |
| Embedding responses accepted absent, duplicate, or partial indexes | A remote provider could assign vectors to the wrong books | Require response count and unique contiguous integer indexes; reorder valid indexed responses and reject ambiguous payloads |
| LibraryThing assumed provider ranks were parseable integers | One malformed rank could fail the entire provider refresh | Validate positive integer ranks and fall back to response position; malformed-rank tests |
| The API proxy decoded route parameters twice | Encoded path data could become route separators | Encode SvelteKit's already-decoded parameters once; escaped-separator and literal-percent tests |
| Native forms received profile binding only after hydration | Early submissions or disabled JavaScript could fail with a profile conflict | Render profile bindings in the server response and cover them in the integration smoke |
| Digest retries required an exact match to historical channels | Removing an already-successful channel could keep a retry marked failed | Check whether all currently configured channels succeeded; partial-delivery reconfiguration test |
| Invalid legacy timezone keys could raise errors | Scheduler/settings access could fail on malformed stored configuration | Normalize invalid legacy keys to UTC and reject them on write; malformed-key tests |
| Discord catalog text could trigger mentions | Imported titles or reasons could ping users or everyone | Disable allowed mentions on both fresh delivery and retries of historical payloads; payload and delivery tests |
| Release publishing bypassed full tests and accepted branches | Untested releases or manual branch runs could overwrite `latest` | Reuse the complete CI workflow as a publishing prerequisite and require a numeric version tag |
| Web Docker context included nested environments and data files | Unnecessary context size and potential local-data inclusion in the build stage | Exclude nested virtual environments, Python caches, databases, and generated secret keys |
| First-admin setup documentation still described a setup token | New users could look for a token that is no longer generated | Correct `.env.example` to describe first-admin setup and localhost binding |

## Residual risks and mitigations

- **External deliveries remain at-least-once under process failure.** If Discord or SMTP accepts a message and the process fails before recording success, retrying can duplicate delivery. Job fencing protects stored results; delivery records suppress confirmed duplicates. Run the default single engine process and treat uncertain notification outcomes as retryable, potentially duplicated operations.
- **Live integrations were not contacted.** Provider fixtures cover malformed responses and failure paths, but this audit does not certify an operator's OIDC, SMTP, Discord, Librarr, Ollama, or remote embedding configuration. Use the in-app connection tests with disposable accounts before enabling polling or digests.
- **Embedding protocol compatibility is stricter.** OpenAI-compatible endpoints that omit vector indexes are now rejected rather than trusted to return vectors in input order. This prevents silent misattribution; select the Ollama adapter for Ollama's distinct protocol.
- **Container builds cannot run on this host because Docker is unavailable.** CI builds both images, and release publishing now waits for those builds plus the tests and integration smoke. A green workflow is required before tagging a public release.
- **HTTP smoke is not a full browser interaction suite.** It verifies production SSR, first-admin setup, cookies, authenticated views, form bindings, stale-profile rejection, and the public API authentication boundary with temporary data. Hydrated interactions and browser-specific behavior still benefit from a small community pilot across browsers and mobile devices.
- **Recommendation usefulness is not established by correctness tests.** Existing offline evaluation and finite-vector tests guard implementation behavior. Cross-reader quality and long-term feedback need community observations; describe scores as ranking signals, as the README already does.
- **Two upstream test deprecation warnings remain.** Starlette's HTTPX test-client compatibility and an AnyIO alias emit warnings. They do not fail the tests; dependency upgrades should continue running the full regression suite.

## Verification

The baseline passed 500 engine tests and 77 frontend tests. Final post-repair checks passed:

- 524 engine tests on Python 3.13, matching CI; two upstream deprecation warnings.
- An earlier full 518-test engine run on Python 3.14 also passed; the subsequently added six embedding-protocol cases passed separately before the final Python 3.13 run.
- 82 frontend tests.
- Svelte/type checks with zero errors and warnings, and the production build.
- Production web/engine smoke: first-admin setup, logout and re-login, authenticated Discover/Sources/Settings/Account/admin pages, server-rendered form bindings, stale-profile rejection, and API authentication.
- Python compilation, locked container-dependency export comparison, workflow YAML/dependency-gate checks, and `git diff --check`.
- A clean `npm ci` reported zero dependency vulnerabilities.

Container image builds passed in the protected CI checks for PRs #149 and #151, although Docker was unavailable on the local host. Live third-party integration tests remain unverified locally, as described above.

The reproducible first-run check is `uv run --project engine python scripts/community_smoke.py` after `npm run build`. It runs services on loopback with temporary databases, backups, keys, and credentials, then stops both services and removes the temporary data. It is now part of CI and the release gate.
