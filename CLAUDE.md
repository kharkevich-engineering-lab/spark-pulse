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

**Request path:** React SPA (`web/src`) → `web/src/lib/api.ts` (single `json()` fetch wrapper, 401 → `/login`) → FastAPI routers under `/api/*` (`spark_pulse/routers/`) → business logic in `spark_pulse/tools/`. Routers are thin; they call `tools.<module>.<fn>()` and raise `HTTPException`. `app.py` is the factory: middleware (`CsrfMiddleware`, CORS, `AuthMiddleware`), router registration, `/health`, `/version`, the `/mcp` JSON-RPC endpoint, and a catch-all that serves the SPA.

**Browser boundary.** With `auth_enabled` false — the default — this API answers every caller, so the operator's own browser is the boundary. Two rules hold it, and both are enforced server-side: CORS uses an explicit origin list (`config.cors_allowed_origins`, never `*` — Starlette *reflects* the caller's origin when the wildcard is combined with `allow_credentials`), and `CsrfMiddleware` refuses any state-changing request carrying a foreign `Origin`. A request with no `Origin` is a non-browser client (curl, MCP, the test client) and passes. `api.ts` also reads a `<meta name="csrf-token">` and sends `X-CSRF-Token`; nothing emits that tag today, so the header is inert plumbing — the origin check is the protection. `tests/test_security_boundaries.py` is the contract. `lifespan` runs startup work (deployment-state check, node registration and mDNS announcement, OCI background updater, `reconcile_all()`, the scheduled-deploy listener and reconcile, the engine-metrics sampler and the reconciler — the last two are stopped again on shutdown).

**Real vs mock tools (the central switch).** `spark_pulse/tools/__init__.py` reads `SIMULATION_MODE` once at import time and re-exports either `spark_pulse.tools.<x>` (real: subprocess, docker SDK, SSH, nvidia-smi) or `spark_pulse.mock.<x>` (canned data, in-memory/`spark_pulse/data/*.json` persistence). Every consumer must go through the package (`from spark_pulse import tools; tools.native_runtime...`) so the switch applies. Each module in `tools/` needs a same-named twin in `mock/`, and both `__init__` import lists must be updated when adding one (`labels`, `atomic_json`, `hub_cache`, `deployment_records`, `custom_files`, `custom_recipes`, `recipe_schema`, `recipe_sources`, `recipe_import`, `scheduled_deploys`, `reconciler` and `node_stats` are real-only and intentionally absent from the mock list). `tests/test_mock_contract.py` enforces the pairing.

Deployments run natively only: `tools.native_runtime` drives Docker, `tools.deployment_records` owns `deployments.json`, and `tools.deploy_dispatch` routes actions on an existing record by that record's own `runtime` so a deployment made by the removed upstream runner stays stoppable. `config.runtime` resolves anything to `native`.

Import gotcha under pytest (SIMULATION_MODE=1): `from spark_pulse.tools import recipes` yields the **mock** module, whereas `import spark_pulse.tools.recipes` / `from spark_pulse.tools.recipes import fn` imports the **real** submodule and also rebinds `spark_pulse.tools.recipes` to it for the rest of the process. Pick the form deliberately. Several mocks (e.g. `mock/system.py`) delegate pure parsing helpers to the real module so tests that patch `subprocess.getoutput` keep working.

**Settings** (`routers/settings.py`): three kinds, kept apart. *Editable* — an allowlist, because `config.update` writes into settings.json and that is where `auth_enabled`, `oidc_client_secret` and `mcp_api_token` are read from; without it, `PUT /api/settings {"auth_enabled": false}` would be the way past every other check. The nested `docker:` and `mod:` blocks have their own key allowlists. *Reported but not editable* — the `environment` block (database, CORS origins, auth, MCP, image registry), so an operator can see how the process is configured without a browser being able to change it; `database_url` comes back with any password stripped. *Secret* — `hf_token`, only ever returned masked. The UI is tabbed (Deployment / Containers / Cluster / Engines / Preferences / Secrets / Environment) over one form; Preferences holds the colour theme, which is browser-local and never sent to the server. `docker.cluster_image`, `docker.ray_port` and `docker.gpu_count` were removed: they had no reader anywhere in the backend, and the whole `docker` block was refused by the allowlist, so that card displayed literals from the JSX and saved nothing.

**Config layering** (`config.py`): bundled `spark_pulse/config.yaml` → `~/.config/spark-pulse/settings.json` → env vars (`SPARK_VLLM_PATH`, `WEBUI_PORT`, `GIT_UPDATE_*`, `SPARK_PULSE_AUTH_ENABLED`, `SPARK_PULSE_MCP_ENABLED`, `SPARK_PULSE_BENCHMARKING_ENABLED`). Secrets live in `~/.config/spark-pulse/secrets.json` (0600). The frontend fetches `/api/config` at startup (`web/src/lib/config.tsx`) to gate features such as the Benchmarking route.

**Auth** (`auth.py`): OIDC via Authlib, cookie session, middleware active only when `auth_enabled`. Public paths: `/health`, `/auth/*`, `/assets/*`, `/static/*`.

**MCP**: `mcp_http.py` owns the tool list and dispatch; it implements tools by calling the app's own REST API over `httpx` at `127.0.0.1:{webui_port}`, so MCP behaviour is always the REST behaviour. `mcp_server.py` is a thin stdio wrapper (`spark-pulse mcp`, needs the `mcp` extra). The HTTP endpoint is mounted on the same app and inherits auth.

**Engine metrics** (`tools/engine_metrics.py`): a background sampler, started by `lifespan`, scrapes each running deployment's engine `/metrics` (Prometheus text) every 5s into a `deque` of 720 readings per deployment — one hour, in memory only, lost on restart. `GET /api/deployments/{id}/metrics` returns that window plus an `available`/`reason`/`detail` triple: when the engine publishes nothing (SGLang without `--enable-metrics`, an unreachable endpoint, an unrecognised body) the UI shows the reason instead of an empty chart. Token throughput is differenced from counters, and a counter that goes backwards is an engine restart — the rate for that interval is `null`, never negative. No percentiles: both engines publish only histograms. There is deliberately no persistence; retention is Prometheus's job. There was a `HealthMonitor` here once; it was never started, tracked nothing, and `/sse/health` called a method it did not define. It was removed rather than repaired — see `docs/engine-metrics.md`.

**Monitoring across nodes** (`tools/node_stats.py`): `GET /api/memory` and `/sse/metrics` ask *every* registered node for its own stats through its own agent — `GetNodeStats` — including the machine this process runs on. `tools/system.py` and its mock are gone with the last of the local `nvidia-smi`/`free`/`df` shell-outs; the parsing lives in the agent (`agent/src/executor/stats.rs`) and its tests with it. The conversions that remain here are units (the protocol is bytes, the page reads megabytes) and absence (`memory_supported: false` is how a GB10's `[N/A]` renders, never a zero). Whether a GPU process is *ours* is decided from two halves: the node says which container the process is in, the control plane's managed-container list says which containers it started and for which deployment. `DELETE /api/memory/processes/{pid}?node=` stops that container when it is one of ours and signals the process otherwise (`TerminateProcess`), so the kill button works on a node that is not this one. A node that cannot be asked keeps its section and says why — unknown is not idle.

**Background reconciliation** (`tools/reconciler.py`): a mutating call records the intent and returns; one background thread converges the nodes. Two states are kept apart on the record — `status` is the lifecycle (running, pulling, stopped) and `sync` is convergence (`in_sync`, `in_progress`, `deleting`, `unknown`), because *running · deleting* is a real situation and collapsing them is how a page comes to say "stopped" about a container still holding 90 GB. `sync_intent` says what an in-progress change is: `DELETE /api/deployments/{id}` marks a live deployment `in_progress`/`stop` (the containers go, the record stays — a finished run is history somebody reads) and an already-finished one `deleting` (the record itself is what is being cleared). The reconciler sweeps every 5s and is nudged by the router so a single healthy node still looks instant; a node that cannot be asked leaves the record where it is and writes `sync_reason`, never a state inferred from silence. `StatusBadge` renders both, and `isSettling()` shuts the actions that would ask again. Records written before this existed carry no `sync` and are settled by definition.

**Deploying a model that is not downloaded** (`tools/scheduled_deploys.py`): a create whose model is absent raises `MissingModelError` and the router answers with a structured `detail.missing_model`, which the UI turns into an offer rather than an error. Accepting posts the same create body to `/api/scheduled-deploys`, which starts the download and records the deployment in a `scheduled_deploys` table. `tools.models.add_finish_listener` fires the deploy when the job reaches a terminal state; `reconcile()` at startup settles anything the hook could not see, because a restart mid-download never gets that call. The plan also carries `model_present`, so the deploy preview says so before the deploy refuses.

**Streaming**: `sse.py` exposes `/sse/*` (metrics every 5s, deployment event stream via `tools.events.EventBroadcaster`). Frontend consumes it through `hooks/useSSEConnection.ts`; long-running operations are tracked in a zustand store (`lib/operationStore.ts`, state machine in `lib/operations.ts`).

**i18n** (`lib/i18n.tsx`, `i18n/{en,fr}.json`): nested JSON per language addressed by dotted key, the same shape `kharkevich.com` uses; the choice lives in `localStorage` under `spark-pulse-lang` and the picker sits beside the theme on Settings → Preferences. What differs from that site is only what has to — it substitutes at build time and ships one HTML file per language, which a SPA cannot, so the dictionaries are bundled and selection is at runtime. **A missing key renders the key itself and warns once**, never the English fallback: a French page quietly showing English reads as a choice somebody made and never gets reported. `t("a.b")` interpolates `{name}`; `plural("a.b", n)` picks `.one`/`.other`. `tests/lib/i18n.test.tsx` holds the dictionaries to the same key set and refuses a French value identical to its English one unless the key is listed as deliberately identical.

**Frontend structure**: `pages/` map 1:1 to routes in `App.tsx`, which holds them as one `PAGES` list so `KNOWN_PATHS` — what the not-found page consults — is derived rather than maintained twice (`/` recipes, `/jobs`, `/cluster`, `/benchmarking`, `/monitoring`, `/models`, `/images`, `/cache`, `/mcp`, `/oci`, `/settings`; `/login` and the 404 render outside `Layout`, and carry the brand footer the sidebar otherwise provides).

The header holds only the signed-in user and the way out. It used to carry an SSE status dot, a refresh button and a theme cycler: the dot reported a connection nothing on the page depended on, refresh duplicated the browser's own reload (and its `lib/refresh.ts` registry went with it), and the theme is a preference — it lives on the Settings page's Preferences tab, in `localStorage` under `spark-pulse-theme`. Data fetching uses `hooks/useQuery.ts`; imports use the `@/` alias to `web/src`. Unit tests live in `web/src/tests/{components,hooks,lib}` with global mocks in `setupTests.ts`; vitest coverage thresholds are 95% lines, 93% statements, 90% functions, 85% branches, measured over every file under `src/` whether or not a test imports it — they are a ratchet, never to be lowered to make a build green.

**Persistence.** Structured state lives in one SQLAlchemy-backed database (`spark_pulse/db.py`): deployments, nodes, the enrollment ledger, benchmark results, recipe customizations and browser sessions. SQLite in WAL mode by default — `~/.config/spark-pulse/spark-pulse.db`, 0600 because it holds OIDC tokens; `spark_pulse/data/spark-pulse.db` (gitignored) in simulation. `docs/cluster-agent-plan.md` §3.3 chose it, and SQLAlchemy is there so the scale case is a URL: set `database_url` (or `SPARK_PULSE_DATABASE_URL`) to `postgresql+psycopg://…` and `pip install spark-pulse[postgres]`. `tests/test_db_and_sessions.py` compiles every table for the PostgreSQL dialect, so a SQLite-only column type fails in CI rather than in front of an operator.

Each store imports its old JSON file **once**, on first read, recorded in the `meta` table — not inferred from an empty table, because deleting the last row would re-import and resurrect what an operator removed. The legacy files are left where they are.

Still files on purpose: the CA key and node certificates (`~/.config/spark-pulse/agent/`, 0600, read by tooling), user-editable recipe and mod directories, `settings.json`/`secrets.json` (config, layered with env vars), `registries.yaml`, and the caches under `~/.cache/spark-pulse/`.

Tests get a database per test via an autouse fixture in `tests/conftest.py`, which also points every JSON migration source at `tmp_path` — without that a test would import the developer's real deployments.

**CLI** (`cli.py`, Click): `start`, `install/uninstall/status/start-service/stop-service [--user]` (systemd via `service.py`), `mcp`, plus `recipes` and `oci` groups for OCI-registry recipe collections (`tools/oci_registry.py`, defaults in `spark_pulse/registries.yaml`).

## Conventions

- PR titles must be conventional commits with a lowercase subject (`feat`, `fix`, `docs`, `ci`, `chore`, `build`, `test`); semantic-release on `main` derives the version, runs `scripts/release.sh` (builds UI, rewrites `pyproject.toml` version, `python -m build`), and publishes to PyPI. Don't hand-edit the version.
- CI (`unit-tests.yml`) runs pytest, vitest, Playwright against a `SIMULATION_MODE=1` backend, black/ruff, eslint, and `tsc --noEmit` as separate jobs. All must pass.
- Python is formatted by black and ruff-format (both in pre-commit); TS/TSX by eslint with `_`-prefixed unused vars allowed.
