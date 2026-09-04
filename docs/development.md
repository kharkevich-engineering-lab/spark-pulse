# Development

This document covers the development workflow: how to install dependencies, run the app locally in various modes, and use the provided scripts.

---

## Prerequisites

| Tool | Version | Purpose |
|---|---|---|
| Python | ≥3.10 (recommended: 3.14) | Backend |
| Node.js | Latest LTS | Frontend |
| npm | Latest | Frontend package manager |
| pip | Latest | Python package manager |

---

## Installation

```bash
# Python dependencies
python3 -m pip install -e ".[dev]"

# Frontend dependencies
cd web && npm install && cd ..
```

---

## Dev Scripts Overview

All scripts live in `scripts/` and are executable (`chmod +x`).

| Script | Mode | Hot-reload | Frontend | Auth | Use Case |
|---|---|---|---|---|---|
| [run-dev-server.sh](#run-dev-serversh) | Simulation | ✅ | Vite dev server | None | Full dev (frontend + backend, no auth) |
| [run-dev-oidc.sh](#run-dev-oidcsh) | Simulation | ✅ | Backend serves built UI | Mock OIDC | Backend + SSO dev (frontend on backend) |
| [run-dev-oidc-full.sh](#run-dev-oidc-fullsh) | Simulation | ✅ | Backend serves built UI | Mock OIDC | Full dev with backend reload |
| [run-backend.sh](#run-backends) | Simulation | ✅ | — | None | Backend only (API testing, `localhost:8100`) |
| [run-production.sh](#run-productionsh) | Production | ❌ | Built UI bundled | Configurable | Real environment (production mode) |
| [build-ui.sh](#build-uish) | N/A | N/A | N/A | N/A | Build frontend bundle only |
| [release.sh](#releasesh) | N/A | N/A | N/A | N/A | Bump version and build distributable |
| [native-deploy-check.sh](#native-deploy-checksh) | Production | N/A | — | N/A | Verify the native deploy path on a real Spark |
| [run-e2e-tests.sh](#run-e2e-testssh) | Simulation | ❌ | Built UI, served by the backend | Forced off | Run the Playwright end-to-end suite |

---

## Local Development Scripts

### `./scripts/run-dev-server.sh`

**Best for:** Frontend-heavy development (working on the React UI).

- Starts **backend** in simulation mode on port **8100** (mocks all tool calls)
- Starts **frontend** dev server on port **3000** (Vite, HMR)
- No authentication

```bash
./scripts/run-dev-server.sh
```

**Open:**
- Frontend: http://localhost:3000 (proxies API calls to backend)
- Backend API docs: http://localhost:8100/docs

The script creates the Python venv and installs frontend dependencies if missing.

---

### `./scripts/run-backend.sh`

**Best for:** Backend-only development or API testing without a frontend.

- Starts **backend** in simulation mode on port **8100** (mocks all tool calls)
- No frontend, no authentication

```bash
./scripts/run-backend.sh              # port 8100, hot-reload
./scripts/run-backend.sh --port 9000  # custom port
./scripts/run-backend.sh --no-reload  # disable auto-reload
```

**Open:** http://localhost:8100/docs (Swagger UI)

The script creates the Python venv if missing.

---

### `./scripts/run-dev-oidc.sh`

**Best for:** Backend development with OIDC auth enabled (SSO flow).

- Starts **mock OIDC provider** on port **9400**
- Creates `~/.config/spark-pulse/settings.json` and `secrets.json` with dev credentials
- Starts **backend** in simulation mode with auth enabled, serves built UI on port **8100**

```bash
./scripts/run-dev-oidc.sh
```

**Open:** http://localhost:8100

**Dev credentials:**
| Field | Value |
|---|---|
| Provider URL | `http://localhost:9400` |
| Client ID | `spark-pulse-dev` |
| Client Secret | `dev-secret` |

Press `Ctrl+C` — the script cleans up all background processes (OIDC mock + backend).

---

### `./scripts/run-dev-oidc-full.sh`

**Best for:** Full-stack development with OIDC auth and hot-reload on the backend.

This script is similar to `run-dev-oidc.sh` but:
- Also creates the Python venv from scratch
- Rebuilds frontend if `spark_pulse/ui/` is missing
- Waits for all services to be ready before printing the startup banner

```bash
./scripts/run-dev-oidc-full.sh
```

**Open:** http://localhost:8100

**Dev credentials** (same as `run-dev-oidc.sh`):
| Field | Value |
|---|---|
| Provider URL | `http://localhost:9400` |
| Client ID | `spark-pulse-dev` |
| Client Secret | `dev-secret` |

---

## Production

### `./scripts/run-production.sh`

Starts the backend in **production mode** — real tools (no simulation), bundled UI, no auto-reload.

```bash
./scripts/run-production.sh              # port 8100, 1 worker
./scripts/run-production.sh --port 443   # custom port
./scripts/run-production.sh --workers 4  # multiple workers
```

**Authentication:** Production auth mode is determined by your `settings.json`/`secrets.json` configuration or environment variables.

```bash
# Enable OIDC in production
SPARK_PULSE_AUTH_ENABLED=true spark-pulse start
```

> **Note:** In-memory token storage does not survive server restarts. For production deployments with persistent sessions, consider a Redis-backed token store.

---

## Deployment runtimes

`runtime` selects how a deployment is launched:

| Value | What happens |
|---|---|
| `upstream` *(default)* | Forks `spark-vllm-docker/run-recipe.sh` and tracks the PID. |
| `native` | Spark Pulse drives Docker itself: it starts an idle container from the engine's image, applies the recipe's mods over `docker exec`, execs the rendered launch script with its output redirected to PID 1's stdout (so `docker logs` carries it), and waits on the engine's readiness endpoint. |

`native` currently handles **solo deployments only**; a multi-node request is
refused with an explanation rather than silently falling back. Everything else
— stop, delete, logs, status — follows each deployment record's own `runtime`,
so records stay usable across a flag flip.

```bash
# Per-run
SPARK_PULSE_RUNTIME=native ./scripts/run-production.sh

# Or persist it in ~/.config/spark-pulse/settings.json
{"runtime": "native"}
```

`POST /api/deployments/plan` is the dry run for either mode: it resolves the
engine, image ref, model, mods, port, rendered command and container profile
without starting anything. The Deploy drawer's **Preview** button and the
`plan_deployment` MCP tool both call it.

### `./scripts/native-deploy-check.sh`

**Best for:** verifying the native path on real hardware — the one thing the
simulation-mode test suite cannot cover, since it needs a Docker daemon, a GPU
and the engine image pulled.

Deploys a recipe through the REST API, waits for readiness, hits `/v1/models`
on the served port, and tears the deployment down again. Any failure exits
non-zero after printing the deployment's log tail.

```bash
./scripts/native-deploy-check.sh qwen3-8b
./scripts/native-deploy-check.sh qwen3-8b --engine sglang --timeout 1200
./scripts/native-deploy-check.sh qwen3-8b --keep      # leave it running
./scripts/native-deploy-check.sh --help
```

**Prerequisites on the Spark:**

- spark-pulse running with `runtime: native` (the script refuses otherwise)
- the engine image pulled and the recipe's model already in the local catalogue
- `curl` and `jq` on `PATH`

---

## End-to-end tests

### `./scripts/run-e2e-tests.sh`

**Best for:** checking the primary journeys through the real built SPA before
opening a PR — the same thing CI's `Run E2E tests` job does.

The specs live in `web/tests/e2e/` and drive a `SIMULATION_MODE=1` backend that
serves the built UI, so they exercise the routers and the mock tools together
rather than a mocked `fetch`. The script builds `spark_pulse/ui/` if it is
missing, starts the backend on port 8100, runs Playwright against it and stops
the backend again. A backend already listening on that port is reused and left
running.

```bash
./scripts/run-e2e-tests.sh                                # the whole suite
./scripts/run-e2e-tests.sh --file tests/e2e/jobs.spec.ts  # one spec
./scripts/run-e2e-tests.sh --headed                       # watch it happen
./scripts/run-e2e-tests.sh --ui                           # Playwright UI mode
./scripts/run-e2e-tests.sh --debug                        # step through it
./scripts/run-e2e-tests.sh --port 8111                    # a different port
./scripts/run-e2e-tests.sh --help
```

First run only, to fetch the browser:

```bash
cd web && npx playwright install chromium
```

The backend log goes to `/tmp/spark-pulse-e2e-backend.log`; a failed run leaves
a report behind, viewable with `cd web; and npx playwright show-report`.

**Notes:**

- The script starts the backend with `SPARK_PULSE_AUTH_ENABLED=false`, so a
  `~/.config/spark-pulse/settings.json` left over from `run-dev-oidc-full.sh`
  cannot bounce the suite to the login page. CI has auth off already.
- `npm run test:e2e` from `web/` runs the suite against an
  already-running backend (`E2E_BASE_URL`, default `http://127.0.0.1:8100`).
  It has no `--pass-with-no-tests`: an empty suite is a failure.
- Specs are independent and run in any order. Anything that mutates backend
  state — the deploy journey, a model download — arranges and cleans up after
  itself over the REST API.
- `web/tests/e2e` is type-checked by `npm run build` via `tsconfig.e2e.json`.
- The Monitoring page's data is stubbed with `page.route`. Simulation mode
  delegates GPU stats to the real `nvidia-smi` parsing, so no CI runner or
  laptop produces a GPU card to assert on; and `GET /api/memory` rebinds
  `tools.deployments` to the real module for the life of the process, which
  would break simulated deploys for every spec that ran after it.

---

## Build & Release

### `./scripts/build-ui.sh`

Builds the React frontend bundle and copies it into `spark_pulse/ui/`:

```bash
./scripts/build-ui.sh
```

This is also run automatically by `run-production.sh` if the UI is missing.

---

### `./scripts/release.sh`

Bumps the version, builds the UI, and creates a Python source distribution and wheel:

```bash
./scripts/release.sh 1.2.3
```

This script:
1. Installs frontend dependencies and builds the React bundle
2. Updates the version in `pyproject.toml`
3. Runs `python3 -m build` to create `dist/` artifacts

---

## Configuration Quick Reference

| File | Purpose | Survives Updates? |
|---|---|---|
| `config.yaml` | Bundled defaults (read-only) | ❌ |
| `~/.config/spark-pulse/settings.json` | Persistent overrides | ✅ |
| `~/.config/spark-pulse/secrets.json` | Secure secrets (mode `0600`) | ✅ |

### Priority Order

```
Environment variables > ~/.config/spark-pulse/settings.json > config.yaml
```

### Common Settings

| Key | Type | Default | Description |
|---|---|---|---|
| `webui_port` | int | `8100` | Port the web UI listens on |
| `auth_enabled` | bool | `false` | Enable OIDC authentication |
| `oidc_provider_url` | string | *(empty)* | OIDC provider URL |
| `oidc_client_id` | string | *(empty)* | OIDC client ID |
| `mcp_enabled` | bool | `true` | Enable MCP server |
| `runtime` | string | `upstream` | `upstream` (run-recipe.sh) or `native` (Docker driven from Python, solo only). Env: `SPARK_PULSE_RUNTIME` |
| `deploy_ready_timeout_seconds` | int | `900` | How long a native deploy waits for the engine's readiness endpoint |
| `simulation_mode` | bool | *(env only)* | Set `SIMULATION_MODE=1` to mock tools |
