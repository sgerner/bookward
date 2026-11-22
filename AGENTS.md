# AGENTS.md

Bookward is split into a SvelteKit web interface (`src/`) and a Python 3.12 FastAPI recommendation engine (`engine/`).

## Testing and quality checks

Run the same local checks used by CI (`.github/workflows/ci.yml`) before committing or opening a pull request:

~~~bash
# Frontend
npm run check
npm test
npm run build

# Engine
uv sync --project engine --extra dev --frozen
uv run --project engine pytest -q engine/tests
uv run --project engine python -m compileall -q engine/afterword_engine

# First-run web/engine integration, with isolated temporary data (requires the web build)
uv run --project engine python scripts/community_smoke.py

# Container smoke build
docker compose build
~~~

### Check behavior and guardrails

- **Strict Svelte warnings**: `npm run check` runs `svelte-kit sync && svelte-check --tsconfig ./tsconfig.json --fail-on-warnings` and fails with exit code `1` on TypeScript errors as well as Svelte compiler, accessibility (`a11y_*`), and CSS (`css_*`) warnings. This matches CI (`Web checks`) and the production `Dockerfile` build stage.
- **Interactive watch mode**: `npm run check:watch` omits `--fail-on-warnings` so watch mode keeps running while you iterate locally. Always run `npm run check` before finishing changes.
- **Warning exceptions**: Fix accessibility and unused-CSS warnings at the source. Keep controls keyboard reachable, label inputs, preserve visible focus, and respect `prefers-reduced-motion`. If a component genuinely requires a rare exception, add a narrowly scoped `<!-- svelte-ignore ... -->` comment on that specific element rather than removing `--fail-on-warnings`.
- **Engine lockfile parity**: If you change Python dependencies in `engine/pyproject.toml` or `engine/uv.lock`, regenerate `engine/requirements.lock` so `uv export --project engine --locked --no-dev --format requirements.txt --no-annotate --no-header --no-emit-project | diff -u engine/requirements.lock -` passes.
- **Secrets and local state**: Never commit `.env` files, SQLite database files (`*.db`, `*.sqlite*`), API keys, webhook URLs, SMTP credentials, or `secret.key` files.
