# Scoped user profiles: feature specification and implementation plan

Status: implemented in the current worktree for review. Date: 2026-09-26.
This document records the feature scope and implementation plan. The code uses a separate SQLite database for each profile plus a central identity registry, rather than adding `profile_id` to every existing data table. Profiles receive independent embeddings; cross-profile embedding reuse remains a possible later optimization.

## 1. Outcome and scope

Signing in selects the user's profile automatically. Password authentication and SSO both resolve to a stable profile ID. There is no profile-selection screen after login. Candidates, read history, shortlists, reading progress, and all personalization inputs belong exclusively to that profile.

Required behavior:

- Alice and Bob can discover the same book independently. Saving, dismissing, rating, importing, or finishing it in one profile never changes the other profile.
- Alice's read history cannot suppress Bob's recommendations or influence his scores, explanations, or association seeds.
- An SSO identity returns to the same profile on every login, including after display-name or email changes.
- Password changes preserve the profile and its data.
- Reusable embeddings may be shared, but shared cache entries never confer access to another profile's books or activity.

Recommended scope for the first release: local username/password accounts, generic OpenID Connect SSO, one profile per account, optional explicit linking of multiple login methods, profile-owned API tokens, administrator provisioning, migration of the existing installation, and isolation of all current workflows. No profile picker, household switching, collaborative lists, account merging, SAML, or cross-profile recommendation learning.

Interpretation of “based on the login password”: the authenticated local account owns the profile; the password itself is not the profile identifier. Use username plus password, consistent with the existing username configuration. Two accounts may choose the same password and still remain separate. Password-only entry would need a different credential-routing design and is not assumed here.

## 2. Original single-profile baseline

| Area | Current code | Consequence |
| --- | --- | --- |
| Browser authentication | `src/hooks.server.ts`: optional single Basic-auth username/password; no password means unrestricted browser access | Replaced with server-side account sessions. Existing Basic credentials bootstrap the legacy administrator on upgrade. |
| Browser-to-engine calls | `src/lib/server/engine.ts`, `src/routes/+page.server.ts`, recommendation/telemetry/Librarr routes | Calls currently have no authenticated user context |
| Public API | `src/lib/server/api-proxy.ts`; `require_api_token` and `api_v1` in `engine/afterword_engine/main.py` | Tokens resolve to their owning profile in the central registry |
| Storage | `engine/afterword_engine/database.py`, migrations through v17 | Existing data tables remain unchanged and each profile receives its own database |
| Shortlist | Candidate status plus `reading_progress`; documented in `docs/reading-workflow.md` | No separate shortlist table is necessary; both structures must be scoped |
| Recommendation learning | `scoring.py`, `learning.py`, `interaction_personalization.py`, ranking/exploration/discovery modules | These run against the selected profile database |
| Background work | `jobs.py`: handler receives only kind; deduplication uses kind | Queue and scheduler operations switch into each active profile database |
| Embeddings | `scoring.py`: cache keyed by entity type/ID/backend/model with content hash | Vectors remain per profile; shared cache is deferred |
| Deployment | `compose.yaml`: engine exposed on internal Docker network | Network placement alone must not authorize private engine routes |

The difficult part is tracing ownership through the entire pipeline, not adding a login form. The implementation routes each request to the SQLite file selected by its verified session or API token. Existing SQL therefore operates inside one profile database without relying on every query author to remember a filter.

## 3. Identity and login behavior

### Local accounts

The first administrator uses the one-use setup token emitted by the engine, unless existing Basic-auth credentials bootstrap that account during upgrade. Administrators create additional accounts with a unique normalized username and a temporary password; the user must change that password at first sign-in. Public signup is disabled by default. Display name is optional and never used for identity matching.

Passwords are stored as salted Argon2id hashes through a maintained library. The implementation applies login throttling, bounded input sizes, generic login errors, and secret redaction. See [OWASP password storage guidance](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html).

Password changes verify the current password when one is set; SSO-only accounts must have signed in within the last 10 minutes to add a local password. Administrator resets set a new temporary password and revoke existing sessions; the next login must change it. Email recovery is not included. The last usable login method and last active administrator are protected from removal or disabling.

### SSO

Use generic OIDC Authorization Code flow with PKCE S256 through a maintained client library. Start with one configured provider per deployment; support issuer-qualified identities in the schema. Resolve identities by validated `(issuer, subject)` rather than email. Validate signature, issuer, audience/authorized party where applicable, expiry, nonce, state, and exact callback URI. Discovery is restricted to the administrator-configured issuer; never discover from an arbitrary login parameter. The identity semantics and ID-token validation follow [OpenID Connect Core](https://openid.net/specs/openid-connect-core-1_0.html).

A successful callback finds the identity's existing account/profile. Unknown identities are denied unless the operator enables just-in-time provisioning, in which case any valid identity from the configured issuer may create a user account with an empty profile. First login cannot become administrator or claim legacy data merely because it arrived first. Concurrent callbacks are serialized by identity uniqueness, producing exactly one account.

No profile picker is shown. A new permitted user goes directly to an empty personal workspace with onboarding actions. If the identity is unknown and provisioning is closed, show an access-denied message without revealing other accounts.

To link SSO to an existing local account, start in that account's authenticated settings and require a session created within the last 10 minutes. Bind the OIDC transaction to that account and the browser's one-use state cookie; the callback must still have a valid session for the same account. Never automatically link on matching email, even verified email. Reject an identity already linked elsewhere. Unlinking cannot remove the last usable login method. Changing providers or OIDC client subject semantics requires explicit relinking, not email-based reassignment.

### Sessions and logout

The engine owns authentication records and opaque server-side sessions; SvelteKit handles the browser cookie and acts as the browser-facing backend. Store only a digest of a random session token, with account ID, creation/last-seen/absolute expiry, and revocation state. The implementation uses a 24-hour idle expiry and 7-day absolute expiry. Sessions rotate at login and password changes.

Use an HttpOnly, Secure, SameSite=Lax cookie with Path=/ and no Domain in HTTPS deployments. The implementation enables non-Secure cookies on HTTP because local development and some reverse-proxy setups use it; deployments should set the public origin to HTTPS. SvelteKit rejects cross-origin state-changing requests and OIDC login/link uses one-use browser-bound state. These controls follow [OWASP session guidance](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html).

Logout revokes the session, clears the cookie, and sends a cross-tab logout notification. The page's profile-bound requests and telemetry queues are discarded on teardown. Protected HTML/JSON uses `Cache-Control: private, no-store`. Local app logout is guaranteed; provider-wide logout is optional and provider-dependent. Signing in as another person requires logout and login, not selecting another profile.

## 4. Authorization architecture

The engine resolves an immutable request identity from the verified session or profile-owned API token, then places its profile in request-local context. No browser-supplied profile identifier selects data; background work enters an explicit profile scope.

Browser request path:

1. SvelteKit reads its session cookie and verifies it through the engine for route gating and `event.locals`.
2. A request-bound engine client forwards the opaque session token plus a server-only service credential over the trusted backend connection. These values are created from server state; never forward client-supplied identity headers.
3. Engine dependencies validate the service credential and session, derive the principal, and pass it into scoped services. The engine remains the authorization authority.

Public `/api/v1` continues to accept bearer API tokens or `X-API-Key`, deriving the profile from the token row. Browser sessions cannot authenticate this API implicitly. Supplying conflicting credentials must fail rather than select a surprising profile. Tokens cannot select a profile using headers, query strings, or request bodies; reject reserved identity override fields.

Replace module-global calls with `engineFor(event)` or an equivalent explicitly request-bound client for JSON, uploads, job polling, and SSE. No mutable global “current user.” Background workers receive explicit scope without depending on request-local state.

Protect every private `/api/*` engine route, including settings, token creation, backups, imports, and job endpoints. An internal service credential alone is insufficient for personal data access. Keep a minimal unauthenticated liveness endpoint with no settings, job, or profile details. Inventory `/docs`, OpenAPI, health, and static assets deliberately.

Object lookups and mutations must include ownership predicates, for example `WHERE profile_id=? AND id=?`. Return the same 404 for another profile's ID as for a missing ID. Validate ownership of every supplied candidate/run/event/read/source/delivery/job ID and all relationships. Bulk operations containing any unavailable ID fail atomically with no partial writes. Administrative role authorizes explicit installation operations, not arbitrary profile impersonation through normal data endpoints.

## 5. Data model and isolation boundary

The implementation uses two SQLite layers:

| Store | Contents | Access rule |
| --- | --- | --- |
| `afterword.db.auth.sqlite3` | Accounts, profile IDs/status, Argon2id password hashes, OIDC identities and pending transactions, server-side sessions, login throttles, and API token digests | Accessed only by the engine identity module. Never returned to browser routes. |
| `afterword.db` | The existing installation's data, retained as the `legacy` profile | Selected only when the verified principal maps to profile `legacy`. |
| `profiles/<profile-id>.sqlite3` | The existing engine tables for one additional profile | Selected from a server-verified session or profile-owned API token. IDs cannot contain path characters. |

Each account owns exactly one opaque profile ID. Usernames, display names, passwords, email addresses, and SSO claims are not profile identifiers. The engine places the authenticated profile in request-local context and `database.connect()` resolves to that profile's file. Existing SQL, uniqueness constraints, triggers, and foreign keys then run inside one profile database. No client-supplied profile ID selects data. Direct object IDs from another profile resolve as missing because that database cannot contain the other profile's row.

Every existing personal table lives in the profile file: candidates, reads, sources and their filters, reading progress, feedback, candidate quality, recommendation runs/impressions/events/outcomes, association runs/evidence/cache, settings and encrypted integration credentials, jobs, digest records, Librarr import history, and embedding vectors. The profile file's SQLite integer IDs may overlap with other profiles safely. New profile setup creates the existing schema without copying reads, candidates, progress, history, tokens, or integration credentials. It receives its own default public-source rows. Demo books and ratings remain in the legacy profile only.

The identity registry maps the special `legacy` profile ID to the already deployed database, so existing data files do not need a destructive table rewrite. Startup creates the registry once, hashes configured legacy credentials into the administrator account when available, and moves legacy API token records into the registry with their profile owner. If no password is configured, startup creates a random one-use setup token and logs it once. New accounts get independent database files. Every database runs its own normal schema migrations when initialized.

API token hashes and profile ownership live centrally because the profile must be known before the engine opens a profile database. Listing or revoking a token is restricted to the active profile. Disabled accounts revoke their sessions and tokens, disable the profile, and stop its scheduled work. Account management can provision, disable, re-enable, and reset passwords; it does not impersonate or merge profiles.

All settings are stored per profile database in this implementation, including embedding provider configuration. Environment values seed defaults; a profile's changes do not alter another profile. This means users may choose different embedding providers. Installation OIDC configuration and service credentials remain environment-level settings.

## 6. Embeddings and resource use

The implementation keeps existing embedding rows in each profile database. That provides a clear ownership boundary and avoids revealing whether another profile has encountered a book. Identical books can therefore be embedded more than once across profiles, which costs extra disk and provider calls.

Cross-profile embedding reuse is an optional later optimization. If added, it should use an immutable cache keyed by the exact bibliographic embedding input plus provider namespace, model revision, and document-format version. The embedding text must exclude ratings, read dates, profile details, interactions, and credentials. Profile-specific rankings, scores, explanations, read vectors, and learned taste adjustments must remain private even when raw book vectors are shared. Sharing can be added without changing the account-to-profile resolution model.

## 7. Background jobs and integrations

Change enqueueing to require declared scope: `enqueue_job(kind, profile_id=..., payload=..., dedupe=...)` for personal work, with a separate installation-maintenance entry point. Deduplication includes scope, kind, and relevant payload/configuration revision. The worker passes the persisted job context, not only kind, to handlers.

Source sync, scoring, quality audit, associations, read attribution, digest preview/send/retry, and Librarr actions all execute within their owning profile. Global schedulers enumerate active profiles and enqueue independent jobs; they do not combine personal datasets. Restart recovery preserves scope, and a disabled/deleted profile prevents queued jobs from running. Running jobs recheck state before committing or performing external actions. Work already accepted by an external service may not be retractable; record that outcome only under its original owner.

Use per-profile concurrency/queue limits with a shared provider budget so one profile cannot monopolize embedding/API capacity. Public responses expose only the caller's job status and sanitized errors. Installation maintenance progress is administrator-only. Logs should carry opaque profile/job IDs, never credentials or full personal payloads.

Digests read only the owning profile's candidates and recipients. Review links require authentication as that profile; a period key is not authorization. Librarr credentials are independent by default. If users configure the same remote library themselves, that external library may share inventory; Bookward still isolates local import status and never returns another profile's remote response/history.

## 8. API and user interface contract

Retain existing recommendation/read-list URLs and response fields for compatibility. Ownership is implicit in credentials; callers do not append a profile ID. Add authenticated `GET /api/v1/me` for integration diagnostics, returning only the current account/profile summary. Tokens are personal and cannot call installation administration endpoints.

Proposed browser-facing routes:

| Route | Purpose |
| --- | --- |
| `GET /login`, `POST /auth/login` | Local sign-in and enabled SSO choice |
| `GET /auth/oidc/start`, `GET /auth/oidc/callback` | Bound OIDC login/link transaction |
| `POST /auth/logout` | Revoke session and clear browser state |
| `GET /auth/me` | Current identity, role, profile display name, session state |
| `POST /auth/password/change` | Reauthenticated password change |
| `GET /auth/oidc/start?intent=link`, `DELETE /auth/identities/{id}` | Explicit login-method management |
| `/admin/accounts/*` | Create/invite, disable, reset access; server-enforced administrator role |

Corresponding engine authentication routes require the backend service credential; authenticated operations also require the current session. Login/setup/callback completion have narrowly defined pre-authentication exceptions, with throttling and transaction validation. Return 401 for invalid/expired credentials, 403 for forbidden administrative operations, and 404 for unavailable personal objects. Redirect browser page loads to login; JSON/SSE consumers receive structured authentication failures rather than login HTML.

Show the current display name and sign-out action in the app header. The Account page manages password and SSO login methods; Settings holds profile-owned sources, integrations, and API access. Administrators also get account management. Empty-profile onboarding offers read import and source configuration, not a profile choice. Refresh all page data after login, and namespace any persistent browser data by opaque profile ID; theme alone may remain device-wide. Telemetry queues must not replay across accounts.

## 9. Migration, rollout, and recovery

On first engine start after deployment, the engine creates the central identity registry beside the configured SQLite database and creates a `legacy` profile record that points at the existing database. Existing candidate/read/shortlist/settings/job IDs and rows remain in place. Existing schema migrations still run on the legacy database through the regular migration runner.

If the existing Basic-auth username and password are supplied to the engine, startup hashes the password with Argon2id and creates the first administrator account for `legacy`. The username remains the login name. If the password is absent, startup stores only a digest of a random, one-use setup token and logs the token once; `/login` accepts it to create the administrator. If the log is unavailable, the operator can issue another token with `python -m afterword_engine.auth_admin setup-token` while no account exists. Once any administrator account exists, that setup command is unavailable.

Existing API token hashes and metadata are copied once into the central registry and assigned to `legacy`, preserving those integrations. Each new account receives a fresh empty database under `profiles/<opaque-profile-id>.sqlite3`; its first connection runs the existing schema migrations and initializes default public sources without demo reading history. The web service no longer checks Basic credentials. After verifying that legacy login works, operators should remove `AFTERWORD_AUTH_PASSWORD` from the deployment configuration.

Personal API tokens are stored in the central registry with profile ownership, because the token must be resolved before choosing the data file. A valid browser session or token creates a request-local profile scope in the engine. All SQL then runs within that profile's SQLite database. Background workers and source/digest/backup schedulers enumerate active profiles and switch scope before reading or writing. Disabled accounts lose sessions/tokens and no longer get scheduled jobs.

Each active profile receives its own rolling SQLite snapshots in the backup volume. The central account registry is backed up separately under `auth-registry/`. A normal in-app restore replaces the current profile's data file only. Registry disaster recovery is an operator-only command run with the engine stopped; it invalidates restored sessions and API tokens to prevent old credentials from becoming active again. If `AFTERWORD_SECRET_KEY` is configured, keep its value in the deployment's secret store. Generated-key recovery files remain beside each profile snapshot.

Run one engine process/container against these SQLite volumes. Database locks and background job/scheduler loops are process-local; multiple engine replicas can duplicate scheduled work and do not coordinate an in-place restore. The web tier may be scaled separately if it shares the same engine service and session cookies.

Rollback of the entire feature requires stopping services and restoring the original application version and pre-upgrade database backup. This discards later writes. Account disabling is reversible and retains profile data. Account deletion and data export are not included in the current implementation.

## 10. Implementation status and completion gates

| Phase | Status | Delivered behavior |
| --- | --- | --- |
| Identity and migration | Implemented | Central registry, legacy profile mapping, password bootstrap/setup token, and existing API token ownership migration |
| Profile storage | Implemented | One SQLite data file per account; request-local engine routing; isolated settings, candidates, reads, shortlists, integrations, jobs, and learning state |
| Browser authentication | Implemented | Argon2id credentials, opaque server-side sessions, rolling idle expiry, logout, password changes, forced first password change, and CSRF/profile binding |
| SSO | Implemented | OIDC authorization code plus PKCE, issuer/audience/signature/nonce/state validation, explicit identity linking, and configurable just-in-time provisioning |
| User experience | Implemented | Login/setup page, account settings, sign-out, administrator account creation/disable/reset, and cross-tab profile refresh |
| Public API and jobs | Implemented | Profile-owned API tokens, `/api/v1/me`, profile-scoped job queues, per-profile schedulers and snapshots |
| Shared embedding cache | Deferred | Embeddings remain private in each profile database; content-addressed sharing can be added later |
| Release validation | Partial | Automated engine/web checks and a local browser smoke test pass; production OIDC, ingress, representative-load, and operator recovery rehearsal remain pending |

Primary implementation files include `engine/afterword_engine/profiles.py`, `tenancy.py`, `database.py`, `main.py`, `jobs.py`, and `backups.py`; `src/hooks.server.ts`, `src/lib/server/engine.ts`, the login/account/admin routes, the OIDC handlers, and `src/routes/+page.svelte`; plus `compose.yaml`, `.env.example`, and this deployment documentation.

## 11. Acceptance tests and release criteria

Use two accounts with overlapping books and intentionally different ratings. Exercise web routes, direct engine routes, public tokens, jobs, and SSE independently; a frontend-only test is insufficient.

| Test | Required result |
| --- | --- |
| Local login, password change, reset, repeated SSO login | Same account returns to same profile without choosing one; revoked sessions fail |
| Same local password on two accounts; same email on two SSO subjects | Distinct profiles; no accidental linking |
| OIDC callback replay, bad state/nonce/issuer/audience, expired token, duplicate callback | Invalid callbacks denied; one identity/profile for valid concurrent provisioning |
| Save/dismiss/undo/read/rate/import shared book | Only the acting profile changes; other profile's snapshots remain identical |
| Cross-profile IDs in single, bulk, telemetry and relationship payloads | Resolve IDs only in the caller's profile; a missing local object returns 404 and does not change another profile. Numeric IDs may coincide between separate profile databases. |
| Recommendations, counts, explanations, source filters and outcomes | Derived solely from the requesting profile's inputs; Alice's read book remains eligible for Bob |
| Personal settings, tokens, jobs, digests and association histories | Other profiles cannot list, mutate, poll, retry, or infer them |
| Direct unauthenticated engine request; forged profile header; user calling admin API | Denied by engine authorization, regardless of UI behavior |
| Job dedupe/restart and overlapping profile jobs | Independent execution/results with preserved scope and no starvation |
| Logout/login in same browser, stale in-flight fetch/SSE and queued telemetry | No previous profile data reappears or is attributed to the next user |
| v17 upgrade and interrupted migration | Data preserved; interruption leaves recoverable old or complete new state, never partially served data |
| Backup restore and profile disable/re-enable | Restored sessions/tokens cannot revive unintentionally; disabled profiles are inaccessible and retain their data |

Embeddings remain per-profile in this release. Shared-cache instrumentation and reference-lifecycle cases are deferred until that optional optimization is implemented.

The automated engine and frontend suites, type checks, and production build pass. Additional automated tests cover the legacy single-profile upgrade, profile-owned shortlist and job access, password-reset session revocation, repeated OIDC login and callback replay, invalid state/nonce/issuer/audience/expiry, separate subjects sharing an email address, profile-key recovery, identity-registry restore, and profile reactivation migrations. A local browser smoke test covers first-admin setup, account provisioning, sign-out, and automatic reader sign-in with the forced-password-change page. This does not complete the acceptance matrix: browser-based shortlist transitions and same-browser account changes, production reverse-proxy cookies, a real provider callback, and restore rehearsal against a deployment copy remain to be verified.

Use a synthetic baseline of ten profiles with overlapping catalogs when the deployment's expected peak profile count is not yet known. Record Argon2 login latency/memory, read-path p50/p95 latency, SQLite busy errors, backup time/size, and per-profile job completion under concurrent requests. Repeat against representative deployment data before increasing the supported profile count. Scope-leading indexes should avoid scanning every profile for normal page loads. A local query-only smoke benchmark against one profile with 500 saved candidates and 100 reads measured p50/p95 of 0.21/0.28 ms serially and 1.62/2.64 ms with five threads. This does not cover Argon2, backup, job execution, overlapping catalogs, or representative production load.

The first release keeps signup operator-managed (`OIDC_AUTO_PROVISION=false`), supports reversible account disabling, and does not provide profile deletion or data export. Do not enable public auto-provisioning or offer the service to users who need self-service erasure/export until those flows and backup-retention policy are implemented.

Release is complete when every private route and domain entry point requires scope, the isolation matrix passes, existing single-profile behavior survives migration, and operator setup/rollback instructions have been rehearsed. Unresolved policy defaults (provider choice, provisioning restrictions, session durations) may be configured by the operator; none should fall back to shared authenticated data.
