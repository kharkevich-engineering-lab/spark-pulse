# Spark Pulse

Spark Pulse is a web control plane for [spark-vllm-docker](https://github.com/eugr/spark-vllm-docker). It brings recipe discovery, deployment management, live monitoring, cache cleanup, and configuration into one interface for NVIDIA DGX Spark hardware.

**License:** [MIT](LICENSE) — Copyright © 2026 Kharkevich Engineering Lab

> **Disclaimer:** This project is not sponsored by, endorsed by, or affiliated with NVIDIA Corporation or any of its subsidiaries. NVIDIA, DGX, and related trademarks are property of their respective owners.

## Features

- **Recipe browsing** - Explore deployment recipes, model variants, and mod combinations in a clean catalog view.
- **Deployment jobs** - Launch deployments, watch live logs, inspect status, and stop running jobs from the UI.
- **Real-time monitoring** - Track GPU, CPU, RAM, and disk usage with streaming updates.
- **Cache management** - Review and clean Hugging Face, wheel, ccache, Triton, and related caches.
- **Settings and auth** - Configure the backend path, defaults, and OIDC authentication.
- **MCP server** - Expose the same operations to Model Context Protocol clients and automation.

## Screenshots

### Recipes

![Recipes view](https://raw.githubusercontent.com/kharkevich-engineering-lab/spark-pulse/main/docs/static/images/recipes.jpeg)

Browse available deployment recipes, inspect model details, and compare supported variants at a glance.

### Jobs

![Jobs view](https://raw.githubusercontent.com/kharkevich-engineering-lab/spark-pulse/main/docs/static/images/jobs.jpeg)

Monitor deployments, stream logs, and manage running jobs without leaving the dashboard.

### Monitoring

![Monitoring view](https://raw.githubusercontent.com/kharkevich-engineering-lab/spark-pulse/main/docs/static/images/monitoring.jpeg)

Watch GPU, CPU, and disk usage in real time alongside active GPU processes.

## Installation

Install the published package from PyPI:

```bash
python3 -m pip install spark-pulse
```

This installs the `spark-pulse` command-line interface.

## Usage

Start the web app after installation:

```bash
spark-pulse start
```

Then open the UI in your browser. The default port is `8100` unless you changed it in configuration.

Common runtime commands:

```bash
# Start the MCP server for assistants and automation
spark-pulse mcp

# Install and manage the app as a systemd service
spark-pulse install
spark-pulse status
spark-pulse start-service
spark-pulse stop-service
spark-pulse uninstall
```

Add `--user` to any of the service commands if you want a user-scoped systemd unit.

Authentication is optional. When enabled, Spark Pulse redirects users through your configured OIDC provider and protects the UI and API routes.

## Configuration

Spark Pulse reads settings from `config.yaml` (bundled with the package) and merges user overrides from `~/.config/spark-pulse/settings.json`. Environment variables take highest priority.

### config.yaml Reference

| Key | Type | Default | Description |
|---|---|---|---|
| `spark_vllm_path` | string | `/tmp/spark-vllm-docker` | Absolute path to the spark-vllm-docker installation directory. |
| `webui_port` | int | `8100` | TCP port the web UI listens on. |
| `default_container` | string | `vllm-node` | Default Docker container name for deployments. |
| `default_gpu_mem_util` | float | `0.8` | Default GPU memory utilization fraction (0.0–1.0). |
| `default_port_range_start` | int | `9000` | Start of the ephemeral port range for deployments. |
| `default_port_range_end` | int | `9100` | End of the ephemeral port range for deployments. |
| `job_retention_days` | int | `7` | Number of days to retain completed job records. |
| `cluster_enabled` | bool | `false` | Enable multi-node cluster mode. |
| `mcp_enabled` | bool | `true` | Enable the MCP server endpoint. |
| `mcp_path` | string | `/mcp` | HTTP path for the MCP server. |
| `mcp_api_token` | string | *(empty)* | Optional API token to protect MCP requests. |
| `auth_enabled` | bool | `false` | Enable OIDC authentication. |
| `oidc_provider_url` | string | *(empty)* | OIDC provider URL (e.g. `https://keycloak.example.com/realms/myrealm`). |
| `oidc_client_id` | string | *(empty)* | OIDC client ID. |
| `oidc_client_secret` | string | *(empty)* | OIDC client secret — stored securely in `~/.config/spark-pulse/secrets.json`. |

### Environment Variable Overrides

The following environment variables override their corresponding config keys:

| Environment Variable | Config Key | Description |
|---|---|---|
| `SPARK_VLLM_PATH` | `spark_vllm_path` | Override the spark-vllm-docker path. |
| `WEBUI_PORT` | `webui_port` | Override the web UI port. |

### File Locations

| File | Purpose |
|---|---|
| `config.yaml` | Bundled defaults (read-only, overwritten on package update). |
| `~/.config/spark-pulse/settings.json` | Persistent user overrides (survives package updates). |
| `~/.config/spark-pulse/secrets.json` | Securely stored secrets (mode `0600`). |

### Example: Enabling OIDC Authentication

```yaml
# config.yaml or settings.json
auth_enabled: true
oidc_provider_url: https://keycloak.example.com/realms/myrealm
oidc_client_id: spark-pulse
```

Then set the client secret via the UI Settings page or directly in `secrets.json`:

```json
{
  "oidc_client_secret": "your-secret-here"
}
```

### Development Mode with Mock OIDC Provider

For local development, Spark Pulse ships with convenience scripts that start a [mock OIDC provider](https://github.com/geigerzaehler/oidc-provider-mock) alongside the dev server. This lets you test the full SSO login flow without a real identity provider.

**Full stack (backend + frontend with hot-reload):**

```bash
./scripts/run-dev-oidc-full.sh
```

This script:
1. Starts the mock OIDC provider on `http://localhost:9400`
2. Creates `~/.config/spark-pulse/settings.json` and `secrets.json` with dev credentials
3. Launches the backend with `--reload` (serves both API and built frontend)

**Backend only (for API testing):**

```bash
./scripts/run-dev-oidc.sh
```

Dev credentials (both scripts):
- **Provider URL:** `http://localhost:9400`
- **Client ID:** `spark-pulse-dev`
- **Client Secret:** `dev-secret`

To stop, press `Ctrl+C` — the scripts clean up all background processes.

## Development

Clone the repository and install local development dependencies:

```bash
python3 -m pip install -e ".[dev]"
```

Build the frontend and run the app locally:

```bash
cd web && npm install && npm run build && cd ..
./scripts/run-dev-server.sh
```

Useful development commands:

```bash
./scripts/run-backend.sh
./scripts/run-production.sh
./scripts/build-ui.sh
pytest
python -m build
```

## API

The app exposes REST endpoints under `/api/*` for recipes, deployments, memory, cache, and settings, plus `/auth/*` routes for OIDC login and session handling.

## License

MIT. See [LICENSE](LICENSE).
