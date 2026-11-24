# AGENTS.md

Guidelines for coding agents working in the Bookward repository.

## Repository overview

- `src/`: SvelteKit web application (Node 24, TypeScript, Svelte 5), server form actions, and unit tests (`src/**/*.spec.ts`).
- `engine/`: Python 3.12 FastAPI ingestion, scoring, SQLite, and recommendation engine (`engine/afterword_engine/`) with pytest tests (`engine/tests/`).
- `scripts/community_smoke.py`: End-to-end integration smoke test using isolated temporary storage.
- `Dockerfile` and `engine/Dockerfile`: multi-stage container definitions used by Compose and CI.

## Local verification checks

Run the same local checks enforced by `.github/workflows/ci.yml` before opening a pull request:

~~~bash
# Frontend
npm ci
npm run check
npm test
npm run build

# Engine
uv sync --project engine --extra dev --locked
uv run --project engine python -c 'import pathlib, tomllib; d = tomllib.loads(pathlib.Path("engine/pyproject.toml").read_text(encoding="utf-8")); req, grp = d["build-system"]["requires"], d["dependency-groups"]["build"]; assert req == grp, f"build-system.requires ({req}) != dependency-groups.build ({grp})"'
uv run --project engine pytest -q engine/tests
uv run --project engine python -m compileall -q engine/afterword_engine

# First-run web/engine integration (requires npm run build)
uv run --project engine python scripts/community_smoke.py

# Container builds
docker compose build
~~~

## Frontend checks and accessibility

- `npm run check` fails on TypeScript errors and Svelte compiler, accessibility (`a11y_*`), or CSS (`css_*`) warnings. This matches CI and the web Docker build.
- `npm run check:watch` omits `--fail-on-warnings` for interactive edits. Run the strict check before finishing changes.
- Fix warnings at their source. Keep controls keyboard reachable, label inputs, preserve visible focus, and respect `prefers-reduced-motion`. For a necessary exception, use a narrowly scoped `<!-- svelte-ignore ... -->` on the specific element.

## Engine dependencies and lockfiles

`engine/uv.lock` is the single canonical Python lockfile. `engine/Dockerfile` exports SHA-256 wheel hashes from it in an isolated stage, installs build and runtime packages with `pip install --require-hashes`, and builds the `bookward-engine` wheel offline (`--no-build-isolation --no-index`).

When modifying `engine/pyproject.toml` or updating Python dependencies:

1. Keep `[build-system].requires` and `[dependency-groups].build` identical and version-pinned.
2. Refresh `engine/uv.lock` with `uv lock --project engine`; CI syncs with `--locked` and fails if the manifest and lockfile drift.

## Hygiene and security

- Never commit `.env` files, SQLite database files (`*.db*`, `*.sqlite*`), API keys, webhook URLs, credentials, or `secret.key` files.

## Offline engine test harness and background loops

### `AFTERWORD_ENABLE_BACKGROUND_LOOPS` (`settings.enable_background_loops`)

- `Settings.enable_background_loops` in `engine/afterword_engine/config.py` defaults to `True` (`AFTERWORD_ENABLE_BACKGROUND_LOOPS=true`) so production deployments, Docker containers, and `scripts/community_smoke.py` start `worker_loop`, periodic schedulers (`source_scheduler_loop`, `digest_scheduler_loop`, `goodreads_rss_scheduler_loop`, `backup_scheduler_loop`), and startup catalog backfill jobs automatically.
- Synchronous startup initialization in `afterword_engine.main:lifespan` — auth registry setup, interrupted restore recovery, legacy and per-profile SQLite initialization, and legacy API token migration — always runs regardless of `enable_background_loops`.
- When `settings.enable_background_loops` is `False`, `lifespan` skips creating worker/scheduler tasks, skips queuing startup backfill jobs, and skips awaiting background tasks on shutdown.

### Default fixtures in `engine/tests/conftest.py`

Every engine test runs with three autouse fixtures applied before test bodies:

1. `disable_background_loops`: sets `settings.enable_background_loops = False` so `TestClient(app)` initializes schemas without spawning background catalog workers.
2. `no_real_rate_limit_wait`: stubs `covers._openlibrary_rate_sleep` as a no-op so catalog tests never sleep on the 1-request/sec Open Library rate budget.
3. `block_non_loopback_sockets`: blocks non-loopback TCP/UDP socket connections (`socket`, `asyncio`, `anyio`, `httpcore`) and raises `ExternalSocketBlockedError` immediately on unmocked external traffic. Loopback addresses (`127.0.0.1`, `::1`, `localhost`), Unix domain sockets (`AF_UNIX`), FastAPI `TestClient` ASGI transports, `httpx.MockTransport`, and `respx` mocks are allowed.

### Per-test overrides

- **Enable background loops**: set `monkeypatch.setattr(settings, "enable_background_loops", True)` before entering `TestClient(app)`, and mock any outbound HTTP calls or worker handlers.
- **Test rate-limit timing**: override `monkeypatch.setattr(covers, "_openlibrary_rate_sleep", fake_sleep)` and `covers._openlibrary_rate_clock` inside the test body.
- **Mock external catalogs**: decorate tests with `@respx.mock` or pass `httpx.MockTransport`. When testing `audit_candidates(only_pending=False)` without mocked catalog routes, clear or skip seeded demo rows (`initialize(seed_demo=False)` or `DELETE FROM candidates`) so curated demo entries do not trigger catalog lookups.
