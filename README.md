# Bookward

[![CI](https://github.com/sgerner/bookward/actions/workflows/ci.yml/badge.svg)](https://github.com/sgerner/bookward/actions/workflows/ci.yml)

### A private, explainable way to find your next great read.

Bookward turns your reading history and the book lists you trust into a calm, personal shortlist. It runs on your own machine, shows why a recommendation appeared, and lets you send only the books you approve to Librarr.

It is for readers who want recommendations that feel personal without handing their entire library to another black-box service.

## See it in action

These screenshots use Bookward's bundled, sanitized demo catalog and default connection settings. A fresh install includes a small sample so you can explore before connecting your own library. The captures use dark mode with the Sahara theme. Desktop images are 1600 × 1000; mobile images use a 390 × 844 CSS pixel viewport and are saved at 2× resolution.

### Desktop

![Bookward's Discover screen in dark Sahara mode, with ranked books, covers, metadata, an expanded recommendation explanation, and shortlist or pass actions.](docs/screenshots/discover.png)

![Bookward's Sources screen in dark Sahara mode, showing the bundled demo list, public feeds, refresh cadence, source controls, and add-source form.](docs/screenshots/sources.png)

![Bookward's Settings screen in dark Sahara mode, showing Goodreads import, Librarr, and the local embedding provider.](docs/screenshots/settings.png)

### Mobile

<table>
  <thead>
    <tr><th scope="col">Discover</th><th scope="col">Sources</th><th scope="col">Settings</th></tr>
  </thead>
  <tbody>
    <tr>
      <td><a href="docs/screenshots/discover-mobile.png"><img src="docs/screenshots/discover-mobile.png" alt="Bookward's Discover page on a mobile viewport, showing ranked book cards and the bottom navigation." width="220"></a></td>
      <td><a href="docs/screenshots/sources-mobile.png"><img src="docs/screenshots/sources-mobile.png" alt="Bookward's Sources page on a mobile viewport, showing the source list and filters." width="220"></a></td>
      <td><a href="docs/screenshots/settings-mobile.png"><img src="docs/screenshots/settings-mobile.png" alt="Bookward's Settings page on a mobile viewport, showing Goodreads import and connection settings." width="220"></a></td>
    </tr>
  </tbody>
</table>

Choose the feeds that shape your recommendations. A permanent source can refresh on a schedule; a one-time import stays in your discovery pool without being polled again. Settings is where you import reading history, choose an embedding provider, connect Librarr, and optionally configure a weekly digest.

## Why Bookward?

- **Your data stays with you.** Bookward is self-hosted, uses SQLite, and does not ask for Goodreads credentials. The default Docker binding is localhost.
- **Recommendations are explainable.** Each book includes a match score, source, and plain-language reasons instead of an unexplained ranking.
- **You control the inputs.** Import your Goodreads history, keep or disable the bundled upcoming-book sample, and add public lists from publishers, booksellers, newsletters, or other trusted sources.
- **Discovery can become a reading habit.** Shortlist books, pin a few as Up next, track what you are Reading, and mark books Finished with an optional rating. You can also add a title to a Librarr ebook or audiobook waitlist.
- **Automation is optional and gentle.** Scheduled source refreshes and weekly Discord or email digests are off until you choose them. Delivery is recorded and retryable.
- **It is comfortable to use.** The responsive interface includes keyboard-friendly controls, a skip-to-content link, accessible labels, light/dark/system modes, and reduced-motion support.

## Features

### For readers

- Import a complete Goodreads CSV export, including ratings and read dates.
- Refresh recent Goodreads reads through a public read-shelf RSS feed.
- Combine reading history with author, subject, and text-similarity signals.
- Review recommendations in Discover, then shortlist, pass, or set one aside for later. Undo a recent choice, or revisit passed and deferred books from Past decisions. Move saved books through Saved, Reading, and Finished, with an optional rating when you finish.
- Pin a saved book as Up next to keep it at the front of your shortlist.
- Search Librarr from a recommendation and add a matching ebook or audiobook directly.
- Choose the visual theme and appearance mode that work best for you.

### For curious tinkerers

- Start with a zero-download local hashing embedder that works on CPU-only machines.
- Switch to FastEmbed, Ollama, or an OpenAI-compatible remote embedding endpoint when you want to experiment.
- Add permanent feeds or one-time imports from public, HTTPS-accessible pages.
- Set source refresh cadence to manual-only, every 6 hours, daily, or weekly.
- Send weekly digests to Discord, SMTP email, or both, with minimum-score and “only new books” controls.

### For self-hosters

- SvelteKit frontend and FastAPI engine run independently, with no external scheduler required.
- SQLite stores a separate catalog, reading history, shortlist, settings, and jobs for each account in one persistent volume.
- Scheduled, WAL-safe database snapshots, manual backups, and an in-app restore flow help recover a self-hosted installation.
- Integration keys are encrypted in the engine database and are never returned to the browser.
- Public source fetching rejects private, loopback, link-local, and metadata addresses.
- Docker images are built in CI and published to GitHub Container Registry from version tags.

## API

Bookward exposes a versioned API for automations and other applications. Open Settings → API access, name a token, and select **Generate token**. The full token is displayed only once; store it in the calling application and revoke it from the same screen if it is no longer needed.

When upgrading an existing install, restart the engine once so pending SQLite migrations are applied. Existing saved and Librarr-imported books remain on your shortlist and start in the Saved shelf.

The public base URL is:

~~~
https://your-bookward-host.example/api/v1
~~~

Send the token with every request:

~~~bash
curl https://your-bookward-host.example/api/v1/recommendations \
  -H 'Authorization: Bearer bkw_…'
~~~

The v1 API supports recommendations and feedback, source listing and management, Goodreads RSS imports, sync and scoring jobs, job status, and the Librarr search/download integration. The most commonly used routes are:

- `GET /api/v1/overview` — recommendations, reading history, sources, and safe settings.
- `GET /api/v1/recommendations` — filter with `status=recommended|saved|imported|decisions|rejected|maybe_later|all`, cap with `limit`, and page with `offset`.
- `POST /api/v1/recommendations/{id}/feedback` — send `{"action":"save"}`, `reject`, `maybe_later`, or `restore`. Only `reject` is a negative learning signal; `maybe_later` is neutral.
- `POST /api/v1/recommendations/{id}/undo` — send `{"decision_id":123}` from a recent feedback response to undo the latest choice.
- `GET /api/v1/reading-list` — list shortlisted books with their `reading_status`, `up_next`, rating, and timestamps.
- `PUT /api/v1/reading-list/{id}` — set `{"status":"reading"}`, `{"status":"finished","rating":5}`, `{"status":"saved"}`, or `{"up_next":true}`.
- `GET /api/v1/sources` — list configured sources.
- `POST /api/v1/sync` — queue a source refresh and return a job ID.
- `GET /api/v1/jobs/{id}` — check a background job.

The engine also publishes the complete interactive OpenAPI schema at `/openapi.json` and Swagger UI at `/docs` when it is run directly. The web application proxy keeps `/api/v1` available at the public Bookward URL.

Keep the engine port private; the versioned web proxy is the intended external API boundary. API tokens currently grant the full v1 API and do not expire, so revoke them promptly if they are compromised.

## How recommendations work

Bookward ranks books that it can find through your enabled sources; it does not invent titles or search every book in print. The ranking process is:

1. **Build a candidate pool.** The bundled demo list, public feeds, and one-time imports provide books to consider. Goodreads CSV and RSS imports describe what you have read and how you rated it.
2. **Fill in available details.** The engine checks catalog sources for descriptions, subjects, publication dates, and covers. It prefers a synopsis over an opening sentence, checks returned identifiers, and combines useful fields from verified records. Rated reads with valid ISBNs also receive a separate metadata cache. It removes books it recognizes as already read or already shortlisted from the recommendation feed.
3. **Compare book text.** The engine turns each book's title, author, description, and available subjects into a numeric representation called an *embedding*. For the default local provider, similar representations mainly reflect shared words and two-word phrases. Optional model-based providers can also match related wording. These representations are cached and refreshed when the source text changes.
4. **Rank likely matches.** Books that resemble highly rated reads move up; close matches to low-rated books move down. Ratings for the same author, reading dates, and the weight of a source also contribute. The adjustment from nearby rated books is smaller when only a few books carry most of the similarity weight. The engine gives less weight to a candidate when its catalog details are sparse.
5. **Learn from clear choices.** Shortlisting or explicitly passing on a book, and some reads linked back to a recommendation, can refine later rankings when there is enough consistent evidence. Maybe later is neutral and does not count as a rejection. Simply seeing a book or opening its details is not counted as a dislike.
6. **Show the strongest reasons.** Each card can point to a similar rated book, an author pattern, its source, or missing catalog details. These notes summarize useful evidence; they are not a line-by-line account of every scoring factor.

The displayed score is a 0–100 ranking signal, not a percentage chance that you will enjoy the book. It helps order the current candidate pool.

Verified original publication year can contribute a [confidence-gated era preference](docs/publication-era-confidence-signal.md) learned separately for each profile. It changes a score by at most two points only after improving held-out rating predictions beyond the existing ranker in all three validation periods, with sufficient local era evidence. Missing, conflicting, edition-only, and unsupported years stay neutral. The current production-history replay abstained and changed no recommendations.

Scoring jobs now archive the exact read/candidate inputs, vectors, and outputs behind materialized base scores. Recommendation runs keep a separate bounded snapshot of serving inputs and actual cached vectors, plus copies of cards that were served and later attributed actions. These records survive routine catalog refreshes and can contain book text and reading interactions. See [decision evidence and its retention controls](docs/recommendation-decision-evidence.md) for replay limits, incomplete-capture behavior, and privacy deletion requirements.

To evaluate ranking changes against a private history of rated reads, run `engine/scripts/evaluate_historical_ratings.py` on a consistent read-only snapshot. Its `raw_ranking` report measures whether high scores identify 4–5-star books and low scores identify 1–2-star books, with top/bottom groups and score-decile summaries. Keep these ordering diagnostics separate from the calibrated star-error report: calibration can change score ordering. See [the historical replay protocol](docs/recommendation-historical-replay.md) for the command and limitations.

The [complete production pipeline audit](docs/production-ranking-pipeline-audit-2026-10-02.md) covers base scores, source priorities, metadata confidence, interaction adjustments, slate order, and gaps in historical outcome evidence. Its replay tools require the captured runtime's complete source-hash set and matching serving outputs.

The [metric and grounded-facet study](docs/ranking-metric-facets-2026-10-02.md) documents corrected metadata provenance verification and its before/after results. Historical improvements that vary across reading periods remain experimental.

The [embedding comparison](docs/ranking-embedding-representations-2026-10-02.md), [reading-experience coverage pilot](docs/reading-experience-source-pilot-2026-10-02.md), and [combined first-eight experiments](docs/ranking-track-combinations-2026-10-02.md) preserve frozen cohorts and provenance checks. Their corrected results do not establish a scoring-policy improvement; the experiment tools leave production rankings unchanged.

The [blinded discovery-interest pilot](docs/discovery-interest-pilot-2026-10-02.md) reports actual reader judgments and the synopsis gaps in its completed form. Future forms use a shared verified-source resolver; missing synopses and unsure answers remain unknown, and any judgments after recovered descriptions are recorded separately.

Marking a recommendation read, or marking a shortlisted book Finished, preserves its valid catalog ISBN even without a rating. Matching editions stay excluded when another source uses a different title or author spelling; current provider-verified read work IDs also exclude matching editions, with identity proof retained in scoring evidence for replay.

The [September 30 production review](docs/ranking-production-review-2026-09-30.md) tested stronger author evidence, uncertainty shrinkage, cosine centering, and robust negative neighborhoods on 1,777 rated works. None justified changing the serving weights. High scores showed modest preference ordering; low scores were weak predictors of dislike, and production action evidence remained too sparse and confounded for a policy-quality claim.

The [ordinal and interest-neighborhood follow-up](docs/ranking-ordinal-interests-2026-09-30.md) did not establish a scorer replacement; its cluster variant has an exploratory improvement in top and bottom tails despite weaker global discrimination. `engine/scripts/evaluate_ordinal_interests.py` reproduces the causal replay and compares small learned models, including a daily updating diagnostic, without changing the serving formula.

The [five-hypothesis follow-up](docs/ranking-five-options-2026-09-30.md) also tests compatible text views, source/catalog confidence, and discovery seeds. The [methodology audit](docs/ranking-methodology-audit-2026-09-30.md) corrects the daily model score scale and adds the previously missing actual read-enrichment replay; enrichment remains promising but unresolved. It adds a separate action-history availability diagnostic: reads imported after an action cannot reconstruct what the engine knew then, even when their reading dates are earlier.

The [October 1 ranking synergy follow-up](docs/ranking-synergy-2026-10-01.md) tests fixed history blends, production-template enrichment views, and an exploratory 75/25 score fusion. It keeps a modest production read-symmetric fusion as a prospective candidate while documenting the low-rating and temporal tradeoffs. At that stage, no serving scorer change was adopted.

Association providers select a bounded set of favorite seeds from the complete reading library. Their exclusion checks also cover disliked, unrated, and other books outside that seed set, so those books are not rediscovered as unread candidates.

Association adapters use shared title/author, provider-scoped work ID, and valid ISBN identity checks. Known-author title collisions alone no longer suppress candidates; exact unknown-author results retain a conservative title fallback. See [the association identity audit](docs/association-source-identity.md) for the offline evidence and limits.

The subsequent [kernel uncertainty decision](docs/kernel-uncertainty-shrinkage.md) adopts the small uncertainty adjustment from that study. It improves average high/low discrimination in both retrospective periods, with a small high-rating tradeoff. The subsequent [full-corpus enrichment evaluation](docs/enrichment-full-corpus-evaluation.md) found no reliable gain from richer read embeddings or score blends, so these remain disabled.

### Catalog enrichment

Metadata backfills run in bounded background batches, including on existing installations. Candidate changes queue rescoring; empty or unverified read lookups are cached for 30 days; temporary provider failures become eligible for retry after 24 hours. These cooldowns also apply to failed lookups for a known work ID; an identity change makes the read eligible immediately. Each stored field records its provider and a bounded source projection. Full-title matching ignores known format and numbered-series annotations while preserving substantive subtitles, and publication dates retain their year, month, or day precision. Open Library requests share a one-request-per-second budget, and an unsuccessful lookup preserves an existing real cover.

Open Library and Google Books can supply different parts of a record. The Google Books discovery API key is also used for metadata lookups when configured. A provider outage or rate limit is recorded separately from a catalog identity mismatch. Verified read metadata is cached separately for inspection and future evaluations; it is not added to the historical scoring embeddings. Enrichment preserves imported titles, authors, ISBNs, ratings, and read dates; changing a read's identity invalidates its cached metadata.

### Limitations

- **The source list sets the boundaries.** A book outside your enabled feeds and imports is not available to rank. Incomplete or quiet sources can make the pool small or repetitive.
- **Book details can be sparse or wrong.** Public catalogs do not always have reliable summaries, subjects, dates, or edition matches. Sparse details reduce the influence of text similarity, but the engine cannot fill in information that is missing.
- **Reading history only tells part of the story.** Ratings and titles do not explain why you liked a book, what mood you were in, or what you want to read next. The default local text matcher is simple; richer embedding models still depend on accurate book text.
- **Personalization needs feedback.** A small number of ratings or shortlist/pass actions may not reveal a stable preference. An unseen or unrated book is not treated as a negative signal.
- **The score is a heuristic.** Scores are designed to rank the available books, not to promise enjoyment or make an objective quality judgment. Explanations can also be brief when there is little evidence.

The default hashing provider runs on the Bookward engine host. If you choose a remote embedding endpoint, the book text used to create embeddings is sent to that endpoint for processing.

## How the app fits together

~~~
Goodreads CSV / RSS + trusted public lists
                    |
                    v
          FastAPI engine + SQLite
          import -> enrich -> score
                    |
                    v
              SvelteKit UI
       explain -> shortlist -> decide
                    |
          +---------+----------+
          |                    |
       Librarr            Discord / SMTP
     (optional)           weekly digest
~~~

Bookward is deliberately split into a friendly web UI and an independent engine. The engine keeps working when the browser is closed: it owns source refreshes, scoring jobs, cover enrichment, digest scheduling, and the SQLite reading list. See [the reading workflow guide](docs/reading-workflow.md) for state transitions and API examples.

The first eight discovery recommendations use a bounded anti-redundancy pass to reduce third-author repeats and near-duplicate books; see [the discovery slate policy](docs/discovery-slate-diversity.md) for its limits and telemetry.

## Quick start for development

### Prerequisites

- Node.js 24 or newer
- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/) for the Python environment
- Docker and Docker Compose are optional for local development

### 1. Install dependencies

~~~
git clone https://github.com/sgerner/bookward.git
cd bookward

npm ci
uv sync --project engine --extra dev --frozen
~~~

If you do not use `uv`, create a virtual environment and install the engine package directly:

~~~
python3.12 -m venv engine/.venv
. engine/.venv/bin/activate
pip install -e engine
~~~

### 2. Start the recommendation engine

From the repository root, in one terminal:

~~~
AFTERWORD_DB=./data/afterword.db \
  uv run --project engine uvicorn afterword_engine.main:app \
  --reload --host 127.0.0.1 --port 8000
~~~

The `AFTERWORD_` environment prefix and `afterword_engine` Python package are retained for compatibility with the project's earlier name.

### 3. Start the web app

In a second terminal:

~~~
ENGINE_URL=http://127.0.0.1:8000 \
  npm run dev -- --host 127.0.0.1 --port 5173
~~~

Open <http://127.0.0.1:5173>. The engine creates the SQLite database and a clearly labeled, sanitized demo catalog on a fresh install. Those books are sample data, not a live editorial feed; disable the demo source from **Sources** when you are ready to use your own inputs.

On a new install, open `/login` and choose a username and password for the first administrator. The first account created claims administration, and setup closes automatically afterward. Existing installs with `AFTERWORD_AUTH_USERNAME` and `AFTERWORD_AUTH_PASSWORD` migrate those credentials to the first administrator account automatically. After that migration, account passwords live as Argon2id hashes in Bookward's identity registry; the environment password is no longer used.

### 4. Make it yours

1. Open **Settings** and import a full Goodreads CSV export. RSS is useful for incremental refreshes, but a CSV export is the way to bring in your complete history.
2. Open **Sources** and keep only the public lists you trust. Add your own permanent feeds or one-time imports when you find a good list.
3. Review the explanation on each recommendation, then shortlist the books you want to keep.
4. Optionally connect Librarr under **Settings** and choose ebook or audiobook as the default waitlist format.
5. If you want a nudge, configure a weekly Discord or email digest. Use the per-channel test buttons before enabling it.

## Run with Docker

Docker is the easiest way to run Bookward as a small self-hosted service.

~~~
cp .env.example .env
~~~

Set `ORIGIN` and `AFTERWORD_PUBLIC_URL` to the exact URL readers will open. Generate a private service secret with `openssl rand -hex 32` and set `ENGINE_SERVICE_SECRET`; this protects private engine routes from other containers on the Docker network. The default `BIND_ADDRESS=127.0.0.1` keeps the service local. If `AFTERWORD_AUTH_PASSWORD` is already configured on an existing install, its username and password become the first administrator account on upgrade. For a new install, the first person to complete setup at `/login` becomes the administrator.

~~~
docker compose up --build -d
~~~

Open <http://127.0.0.1:3000>. Bookward stores the identity registry, each profile's SQLite database, and the generated encryption key in the `afterword-data` volume. Stop the stack with:

~~~
docker compose down
~~~

Bookward creates an online backup for each active profile every 24 hours and keeps the latest 7 profile snapshots in the separate `afterword-backups` Docker volume. It also backs up the account registry under `auth-registry/`. Each profile snapshot has a matching generated-key recovery file beside it; both files are owner-only in private directories. Settings shows the current profile's last successful backup, lets that profile create a snapshot, and restores only that profile after you type `RESTORE`. Restore makes an extra safety snapshot first. Configure `BACKUP_INTERVAL_HOURS=0` to disable scheduled snapshots; `BACKUP_RETENTION_COUNT` changes how many scheduled or manual snapshots are kept. Set `BACKUP_DIR` only to a directory mounted persistently into the engine container. For disaster recovery, preserve the `afterword-backups` volume separately from `afterword-data`; `docker compose down -v` removes both.

Profile snapshots include that profile's data and encrypted integration credentials. The shared identity registry has a separate operator recovery command. Stop the engine before restoring it: `docker compose run --rm engine python -m afterword_engine.auth_admin list`, then `docker compose run --rm engine python -m afterword_engine.auth_admin restore <snapshot-id> --confirm`. Restore revokes all restored sessions and API tokens; users sign in again and integrations need new tokens. When Bookward generated the Fernet key, the private backup volume keeps a matching `secret.key` recovery file for each profile snapshot; restoring a snapshot restores its generated key too. If you set `AFTERWORD_SECRET_KEY` yourself, that value is not copied into backups and must be restored from your deployment's secret store. Snapshot and key files never appear in browser responses. Keep both Docker volumes or copy the private backup directory to secure storage outside the host.

### Accounts and single sign-on

The first administrator can create accounts at **Manage accounts**. Each account owns one profile, which Bookward opens automatically after sign-in; there is no profile-selection step. A password is a way to authenticate an account, not a profile identifier. New accounts receive a temporary password set by the administrator and must change it on first sign-in. Readers can link an OpenID Connect identity under **Account** after signing in; later SSO sign-ins resolve that identity to the same account and profile. Matching email addresses never link accounts automatically.

Each profile has its own data store. Candidate pools and discovery decisions, read history, shortlists and reading progress, sources, recommendations and learning history, jobs, API tokens, and integration settings stay with that profile. Signing in to another account opens its separate data. Embeddings are also stored per profile today, so the same book may be embedded more than once.

Configure one OpenID Connect provider with `OIDC_ISSUER`, `OIDC_CLIENT_ID`, and `OIDC_CLIENT_SECRET`. `OIDC_REDIRECT_URI` is optional; when omitted, Bookward uses `{AFTERWORD_PUBLIC_URL}/auth/oidc/callback`. Set `OIDC_AUTO_PROVISION=true` only when every authenticated identity from the configured issuer should receive a profile automatically. Automatically provisioned identities receive regular reader accounts with new profiles; OIDC does not automatically create administrators. The default is `false`, so SSO identities must first be linked to an existing account. SSO uses authorization code flow with PKCE and validates issuer, audience, signature, state, nonce, and redirect configuration.

Set `OIDC_AUTO_LOGIN=true` to send unauthenticated visitors directly to the configured SSO provider instead of showing the Bookward login form first. After sign-out, Bookward shows the login form without immediately signing the same browser back in. Use `/login?manual=1` to open the local login form directly; `OIDC_AUTO_LOGIN` defaults to `false`.

Candidate pools, reads, shortlists, sources, learning history, jobs, API tokens, and integration settings are stored in separate SQLite files per profile. Bookward currently keeps embeddings inside each profile database; this spends more disk and compute while preserving the same isolation boundary.

The default `local` embedding backend needs no model download and works on CPU-only machines. Optional alternatives include FastEmbed, an Ollama model, or an OpenAI-compatible endpoint. To try Ollama locally:

~~~
docker compose --profile ollama up --build -d
docker compose exec ollama ollama pull qwen3-embedding:0.6b
~~~

For NVIDIA acceleration, install the NVIDIA Container Toolkit and add the GPU Compose override:

~~~
docker compose -f compose.yaml -f compose.gpu.yaml \
  --profile ollama up --build -d
~~~

If Librarr runs in another container, put both services on the same Docker network, allow its hostname through `LIBRARR_ALLOWED_HOSTS`, and use its internal URL (usually `http://librarr:5050`) in **Settings**.

## Configuration at a glance

Copy `.env.example` to `.env` for Docker, or export variables in the shell for a local run.

| Variable | Purpose |
| --- | --- |
| `ORIGIN` | Canonical browser origin used by the web server. |
| `AFTERWORD_PUBLIC_URL` | Base URL included in Discord and email digest links. |
| `BIND_ADDRESS` | Host interface for the Docker web port; keep `127.0.0.1` unless you have a secured deployment. |
| `AFTERWORD_AUTH_USERNAME` / `AFTERWORD_AUTH_PASSWORD` | Optional one-time bootstrap credentials for the first administrator, or compatibility migration from existing Basic authentication. |
| `ENGINE_SERVICE_SECRET` | Private credential shared by the web and engine containers. |
| `OIDC_ISSUER`, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | Optional OpenID Connect provider configuration. |
| `OIDC_REDIRECT_URI` | Optional exact callback URL registered with the provider. |
| `OIDC_AUTO_PROVISION` | Allow any valid identity from the configured issuer to create a profile; defaults to `false`. |
| `OIDC_AUTO_LOGIN` | Redirect unauthenticated visitors directly to the configured OIDC provider; defaults to `false`. |
| `AFTERWORD_DB` | SQLite path for a local engine run. Docker uses `/data/afterword.db`. |
| `EMBEDDING_BACKEND` / `EMBEDDING_MODEL` | Provider and model selected by the engine. |
| `EMBEDDING_URL` / `EMBEDDING_API_KEY` | Endpoint and optional key for remote or Ollama providers. |
| `SOURCE_SYNC_INTERVAL_HOURS` | First-install default for permanent-source polling; the UI can change it later. |
| `AFTERWORD_SECRET_KEY` | Optional Fernet key; if omitted, Docker generates one in its data volume. |
| `BACKUP_INTERVAL_HOURS` | Scheduled SQLite snapshot cadence; set `0` to disable. Defaults to 24 hours. |
| `BACKUP_RETENTION_COUNT` | Number of newest automatic and manual snapshots to retain. Defaults to 7. |
| `BACKUP_DIR` | Optional backup path inside the engine container; mount it persistently when set. Defaults to `/backups`, on its own `afterword-backups` Docker volume. |
| `LIBRARR_ALLOWED_HOSTS` | Comma-separated private hostnames allowed to receive the Librarr API key. |

The engine also persists source cadence and application settings in SQLite, so changes made in the UI survive restarts.

## Testing and quality checks

Run the same checks used by CI before opening a pull request:

~~~
# Frontend
npm run check
npm test
npm run build

# Engine
uv sync --project engine --extra dev --frozen
uv run --project engine pytest -q engine/tests
uv run --project engine python -m compileall -q engine/afterword_engine

# Container smoke build
docker compose build
~~~

The frontend suite covers Svelte/type checks and unit tests. The engine suite covers imports, source validation, scoring, jobs, Librarr idempotency, digest delivery, and secret handling. The GitHub Actions workflow also runs CodeQL, dependency review, and both Docker image builds on pull requests.

## Contributing

Bookward is easier to improve when changes are small, understandable, and easy to try. A good contribution can be a bug fix, a clearer explanation, an accessible UI improvement, a new source adapter, or a better developer workflow.

1. Open an issue for a larger behavior change, or start with a focused branch from `main`:

   ~~~
   git switch -c feat/describe-your-change
   ~~~

2. Make the smallest change that solves the problem. Keep product copy friendly and direct.
3. Run the relevant checks from [Testing and quality checks](#testing-and-quality-checks).
4. For UI changes, include before/after screenshots in the pull request and check the layout at narrow and wide widths.
5. Keep controls keyboard reachable, provide labels for new inputs, preserve visible focus, and respect `prefers-reduced-motion`.
6. Open a pull request against `main` with a short summary, testing notes, and any setup or migration detail a reviewer needs.

Please do not commit `.env` files, database files, API keys, webhook URLs, SMTP credentials, or generated secret keys. Use the sanitized demo catalog or redacted fixtures when adding examples. See [SECURITY.md](SECURITY.md) for vulnerability reports.

## Project map

~~~
src/                         SvelteKit UI, form actions, and browser theme handling
engine/                      FastAPI ingestion, jobs, scoring, SQLite, and integrations
engine/tests/                Python engine tests
src/**/*.spec.ts             Frontend and server unit tests
static/                      Favicon, manifest, and static web assets
docs/screenshots/            README screenshots from the sanitized demo
compose.yaml                 Local Docker stack
compose.gpu.yaml             Optional NVIDIA/Ollama override
.github/workflows/            CI, CodeQL, dependency review, and image publishing
~~~

## Troubleshooting

### The web app shows a 503 page

The UI intentionally shows a retryable 503 page when the engine is unavailable. Start the engine first and confirm that `ENGINE_URL` points to the same host and port as uvicorn.

### The first run looks too quiet

The default local embedder is intentionally small and predictable. FastEmbed, Ollama, and remote providers may download a model or take longer during their first indexing pass. Check **Settings** and the engine terminal for progress.

### Goodreads import is incomplete

Use the full Goodreads CSV export for historical data. The RSS feed is designed for incremental refreshes of recent reads, not for reconstructing an entire library.

### Librarr works in Docker but not locally (or vice versa)

The URL must be reachable from the engine process, not just from your browser. Use `http://librarr:5050` between containers and `http://127.0.0.1:5050` when both services run on the host.

## Automation and releases

Every push to `main` and every pull request runs frontend checks, engine tests, Python compilation, CodeQL analysis, dependency review, and both Docker builds. Dependabot checks npm, Python, and GitHub Actions dependencies weekly.

Pushing a tag matching `v*.*.*` publishes the Bookward and engine images to GitHub Container Registry as the version tag and `latest`.
