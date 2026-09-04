# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Spark Pulse is a FastAPI + React control plane and CLI for [spark-vllm-docker](https://github.com/eugr/spark-vllm-docker) on NVIDIA DGX Spark. `AGENTS.md` holds a longer (partly stale) project overview; `docs/development.md` documents the dev scripts in depth.

## Commands

```bash
# Setup
pip install -e ".[dev]"                 # Python (>=3.10; scripts default to python3.14)
cd web && npm install                   # Frontend

# Backend tests (pytest-env forces SIMULATION_MODE=1, asyncio_mode=auto)
pytest                                  # all
pytest tests/test_tools_recipes.py      # one file
pytest tests/test_tools_recipes.py::test_list_recipes_parses_valid_and_skips_bad_yaml

# Frontend (run from web/)
npm run test:run                        # vitest, jsdom; `npm test` for watch mode
npm run test:run -- src/tests/hooks/useOperation.test.ts
npm run lint                            # eslint src/
npx tsc --noEmit                        # type check (CI runs this)
npm run build                           # tsc -b && vite build -> ../spark_pulse/ui/

# E2E (Playwright, config at web/tests/e2e/playwright.config.ts, baseURL :8100)
./scripts/run-e2e-tests.sh              # builds UI if missing, starts SIMULATION backend, runs tests
./scripts/run-e2e-tests.sh --file <spec> --headed

# Python lint/format (pre-commit runs black + ruff --fix + ruff-format + eslint + prettier for yaml)
black --check --diff tests/ spark_pulse/ && ruff check tests/ spark_pulse/
pre-commit run --all-files

# Run locally
./scripts/run-dev-server.sh             # backend :8100 (simulation, --reload) + Vite :3000 (proxies /api,/sse,/health)
./scripts/run-backend.sh [--port N] [--no-reload]   # backend only, simulation; Swagger at /docs
./scripts/run-dev-oidc-full.sh          # + mock OIDC provider on :9400 (writes ~/.config/spark-pulse/settings.json + secrets.json)
./scripts/run-production.sh             # real tools, serves built UI, no reload
```

The FastAPI app refuses to serve the SPA unless `spark_pulse/ui/index.html` exists, so build the frontend before running the backend standalone. `spark_pulse/ui/` is gitignored build output.

## Architecture

**Request path:** React SPA (`web/src`) → `web/src/lib/api.ts` (single `json()` fetch wrapper, CSRF header, 401 → `/login`) → FastAPI routers under `/api/*` (`spark_pulse/routers/`) → business logic in `spark_pulse/tools/`. Routers are thin; they call `tools.<module>.<fn>()` and raise `HTTPException`. `app.py` is the factory: middleware (CORS, `AuthMiddleware`), router registration, `/health`, `/version`, the `/mcp` JSON-RPC endpoint, and a catch-all that serves the SPA. `lifespan` runs startup work (deployment-state check, node registration and mDNS announcement, OCI background updater, `reconcile_all()`, health-monitor restore).

**Real vs mock tools (the central switch).** `spark_pulse/tools/__init__.py` reads `SIMULATION_MODE` once at import time and re-exports either `spark_pulse.tools.<x>` (real: subprocess, docker SDK, SSH, nvidia-smi) or `spark_pulse.mock.<x>` (canned data, in-memory/`spark_pulse/data/*.json` persistence). Every consumer must go through the package (`from spark_pulse import tools; tools.native_runtime...`) so the switch applies. Each module in `tools/` needs a same-named twin in `mock/`, and both `__init__` import lists must be updated when adding one (`labels`, `atomic_json`, `hub_cache`, `deployment_records`, `custom_files`, `custom_recipes`, `recipe_schema` and `recipe_sources` are real-only and intentionally absent from the mock list).

Deployments run natively only: `tools.native_runtime` drives Docker, `tools.deployment_records` owns `deployments.json`, and `tools.deploy_dispatch` routes actions on an existing record by that record's own `runtime` so a deployment made by the removed upstream runner stays stoppable. `config.runtime` resolves anything to `native`.

Import gotcha under pytest (SIMULATION_MODE=1): `from spark_pulse.tools import recipes` yields the **mock** module, whereas `import spark_pulse.tools.recipes` / `from spark_pulse.tools.recipes import fn` imports the **real** submodule and also rebinds `spark_pulse.tools.recipes` to it for the rest of the process. Pick the form deliberately. Several mocks (e.g. `mock/system.py`) delegate pure parsing helpers to the real module so tests that patch `subprocess.getoutput` keep working.

**Config layering** (`config.py`): bundled `spark_pulse/config.yaml` → `~/.config/spark-pulse/settings.json` → env vars (`SPARK_VLLM_PATH`, `WEBUI_PORT`, `GIT_UPDATE_*`, `SPARK_PULSE_AUTH_ENABLED`, `SPARK_PULSE_MCP_ENABLED`, `SPARK_PULSE_BENCHMARKING_ENABLED`). Secrets live in `~/.config/spark-pulse/secrets.json` (0600). The frontend fetches `/api/config` at startup (`web/src/lib/config.tsx`) to gate features such as the Benchmarking route.

**Auth** (`auth.py`): OIDC via Authlib, cookie session, middleware active only when `auth_enabled`. Public paths: `/health`, `/auth/*`, `/assets/*`, `/static/*`.

**MCP**: `mcp_http.py` owns the tool list and dispatch; it implements tools by calling the app's own REST API over `httpx` at `127.0.0.1:{webui_port}`, so MCP behaviour is always the REST behaviour. `mcp_server.py` is a thin stdio wrapper (`spark-pulse mcp`, needs the `mcp` extra). The HTTP endpoint is mounted on the same app and inherits auth.

**Streaming**: `sse.py` exposes `/sse/*` (metrics every 5s, deployment event stream via `tools.events.EventBroadcaster`). Frontend consumes it through `hooks/useSSEConnection.ts`; long-running operations are tracked in a zustand store (`lib/operationStore.ts`, state machine in `lib/operations.ts`).

**Frontend structure**: `pages/` map 1:1 to routes in `App.tsx` (`/` recipes, `/jobs`, `/cluster`, `/benchmarking`, `/monitoring`, `/cache`, `/mcp`, `/oci`, `/settings`, `/login` outside `Layout`). Data fetching uses `hooks/useQuery.ts`; imports use the `@/` alias to `web/src`. Unit tests live in `web/src/tests/{components,hooks,lib}` with global mocks in `setupTests.ts`; vitest coverage thresholds are 60% lines/statements, 50% functions/branches.

**Persistence in real mode** goes under `~/.config/spark-pulse/` (deployments.json, logs/, registries.yaml override). In simulation mode, mock deployments write to `spark_pulse/data/deployments.json` (gitignored).

**CLI** (`cli.py`, Click): `start`, `install/uninstall/status/start-service/stop-service [--user]` (systemd via `service.py`), `mcp`, plus `recipes` and `oci` groups for OCI-registry recipe collections (`tools/oci_registry.py`, defaults in `spark_pulse/registries.yaml`).

## Conventions

- PR titles must be conventional commits with a lowercase subject (`feat`, `fix`, `docs`, `ci`, `chore`, `build`, `test`); semantic-release on `main` derives the version, runs `scripts/release.sh` (builds UI, rewrites `pyproject.toml` version, `python -m build`), and publishes to PyPI. Don't hand-edit the version.
- CI (`unit-tests.yml`) runs pytest, vitest, Playwright against a `SIMULATION_MODE=1` backend, black/ruff, eslint, and `tsc --noEmit` as separate jobs. All must pass.
- Python is formatted by black and ruff-format (both in pre-commit); TS/TSX by eslint with `_`-prefixed unused vars allowed.
