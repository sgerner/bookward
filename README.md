# Bookward

Bookward is a completely independent, self-hosted reading recommender. A SvelteKit/Skeleton UI web application talks to its own Python ingestion and scoring engine, backed by SQLite. It imports Goodreads CSV or RSS history, scans trusted book-list sources, generates explainable recommendations, and sends approved titles to a Librarr wishlist.

## Run locally

```bash
npm install
```

In another terminal:

```bash
cd engine
python -m venv .venv
. .venv/bin/activate
pip install -e .
AFTERWORD_DB=../data/afterword.db uvicorn afterword_engine.main:app --reload
```

Leave that engine terminal running while the web server is open. If the engine is stopped, the web app intentionally shows a retryable `503` page instead of a blank dashboard.

Then start the web application:

```bash
npm run dev
```

The engine creates a versioned database and a clearly labeled, sanitized offline demo automatically. The bundled books are sample data—not a live editorial feed—and can be disabled. No production credentials or personal reading records are included.

## Run with Docker

```bash
docker compose up --build -d
```

Copy `.env.example` to `.env`, set `ORIGIN` and `AFTERWORD_PUBLIC_URL` to the exact public URL you will open, and set a strong `AFTERWORD_AUTH_PASSWORD` before binding beyond localhost. Open `http://127.0.0.1:3000`. SQLite and the generated encryption key are stored in the `afterword-data` volume. Put Bookward and Librarr on the same Docker network, keep `librarr` in `LIBRARR_ALLOWED_HOSTS`, then enter its internal URL (normally `http://librarr:5050`) and API key under Settings. `AFTERWORD_PUBLIC_URL` seeds links in Discord and email digests; it can be changed later in the digest panel.

For an optional Ollama service:

```bash
docker compose --profile ollama up --build -d
docker compose exec ollama ollama pull qwen3-embedding:0.6b
```

For NVIDIA GPU acceleration, install the NVIDIA Container Toolkit and add `-f compose.yaml -f compose.gpu.yaml` to the Compose command. The ordinary profile remains CPU-compatible.

The default `local` backend needs no model download and works on CPU-only machines. For stronger CPU embeddings, select FastEmbed with `BAAI/bge-small-en-v1.5`; the model is downloaded once into the data volume. Settings also supports any Ollama Qwen embedding model or an OpenAI-compatible remote endpoint. For an embedding server running directly on the Docker host, use `http://host.docker.internal:PORT`.

## Product flow

Appearance is personal to each browser. The header appearance picker offers every bundled Skeleton theme with primary, secondary, and tertiary color previews, plus Light, Dark, and System modes. Theme and mode are remembered locally and applied before the page renders; System follows changes to the device appearance. All application surfaces use Skeleton's paired light/dark tokens.

1. Import a complete Goodreads CSV export, with optional RSS refreshes for recent reads.
2. Keep or disable the built-in upcoming-books source and add trusted public source links. Each source can be permanent (kept fresh) or one-time (imported once and retained without polling).
3. Review evidence-backed recommendations, shortlist the good ones, and pass on the rest.
4. Send shortlisted books to Librarr via its server-side `POST /api/wishlist` integration. Settings lets each user choose ebook or audiobook as the default waitlist format; connected users can also search Librarr and call its direct ebook/audiobook download endpoint from the recommendation card.
5. Optionally enable a weekly digest. The engine evaluates the schedule independently of the browser, selects new high-scoring recommendations, and delivers the same report to Discord, SMTP email, or both. Each delivery is recorded with an idempotency key, encrypted credentials, and a retry action for transient failures. The message links back to a period-specific `?view=discover&digest=1&digest_period=YYYY-Www` review, where the reader can select several books and add them to the shortlist in one action.

Fresh installs also receive an idempotent catalog of curated sources: Apple Books top audiobooks and paid ebooks, Open Library science-fiction and fantasy subjects, and Goodreads science-fiction, speculative-fiction, mystery-thriller, and literary-fiction pages. They are ordinary user-toggleable sources, so an installation can keep only the shelves that fit its taste. Open Library's mystery and literary subjects are included disabled as additional options. The New York Times Books overview is listed disabled because the API requires a user key; Amazon list pages are intentionally not enabled because their public pages are bot-protected and do not expose a stable book feed.

Permanent sources are scanned automatically once per day (UTC) by the engine, even when the browser is closed. The Sources view can switch this cadence to manual-only, every 6 hours, daily, or weekly. A manual refresh still scans all enabled permanent sources immediately. One-time sources receive an initial background scan when added and are then excluded from scheduled and manual feed refreshes until they are toggled off and on again.

Digest delivery is off by default. Under Settings, choose the weekday, local time, IANA timezone, minimum match score, maximum number of books, and whether a recommendation may appear only once. Discord webhooks are restricted to Discord's HTTPS webhook hosts. Email uses the standard-library SMTP client with no credentials sent to the browser; STARTTLS is the default, with SSL/TLS and no transport encryption available for private networks. Use the per-channel test buttons before enabling the schedule in production.

Cover art is enriched during source scans and on startup for older databases. Bookward first uses safe HTTPS artwork from the source, then looks up matching editions through Open Library and Google Books. If a title has not received a published cover yet, it receives a deterministic image placeholder instead of leaving an empty card; known Open Library ISBN no-cover URLs are automatically replaced. Open Library ISBN source links are rendered as durable title/author search links so provisional upcoming-book ISBNs do not lead to dead 404 pages.

The Librarr and remote embedding keys are encrypted in the engine database and are never returned to the browser. Bookward includes optional HTTP Basic authentication and binds to localhost by default. Internet-facing deployments should additionally use HTTPS through an authenticated reverse proxy.

## Independent architecture

The installation has no Hermes dependency, no external scheduler, and no hard-coded host paths. The engine includes its own lightweight persisted source scheduler:

- `afterword`: SvelteKit UI and same-origin form layer
- `engine`: FastAPI ingestion, jobs, source scheduler, scoring, feedback, SQLite, and Librarr integration
- optional `ollama`: local CPU/GPU embedding server selected through a Compose profile
- `afterword-data`: the only required persistent volume

Custom source fetching rejects private, loopback, link-local, and metadata addresses. Source responses, imports, and feed sizes are bounded. Goodreads credentials are never requested.

## Verify

```bash
npm run check
npm run test
npm run build
uv sync --project engine --extra dev
uv run --project engine pytest -q engine/tests
uv run --project engine python -m compileall engine/afterword_engine
docker compose build
```

Docker is optional for development. `uv.lock` pins Python transitive dependencies, `package-lock.json` does the same for Node, and production base images are pinned to multi-platform manifest digests.

## Project automation

Every push and pull request runs the frontend checks, engine tests, Python compilation, CodeQL analysis, dependency review, and both Docker image builds. Dependabot checks npm, Python, and GitHub Actions dependencies weekly; incompatible TypeScript 7 majors are held back until the Svelte checker supports them. Pushing a `v*.*.*` tag publishes the Bookward and engine images to GitHub Container Registry as both the version tag and `latest`.
