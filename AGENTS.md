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
