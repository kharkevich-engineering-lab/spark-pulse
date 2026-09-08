# CLI

```bash
spark-pulse --help
```

## Running it

```bash
spark-pulse start [--host 0.0.0.0] [--port 8100] [--workers 1] [--env-file FILE] [--dry-run]
```

Starts the web app. `--dry-run` prints the command it would run and exits.

## As a service

```bash
spark-pulse install [--host …] [--port …] [--service-user …] [--user] [--no-start]
spark-pulse status  [--user]
spark-pulse start-service [--user]
spark-pulse stop-service  [--user]
spark-pulse uninstall [--user]
```

`--user` installs a user-scoped systemd unit instead of a system-wide one.

## MCP over stdio

```bash
spark-pulse mcp
```

A thin stdio wrapper around the same tools the HTTP endpoint serves. Needs the `mcp` extra: `pip install spark-pulse[mcp]`.

## Recipes

```bash
spark-pulse recipes validate <path…> [--json]
```

Validates recipe files against the schema — v1 or v2 — and reports what is wrong with each. Useful in CI for a repository of your own recipes.

```bash
spark-pulse recipes list [--registry NAME] [--version TAG] [--json] [--dry-run]
spark-pulse recipes install <name> [--version TAG] [--registry NAME] [--dry-run]
spark-pulse recipes update [--collection NAME] [--all] [--registry NAME]
spark-pulse recipes auto-update
```

Recipe collections published as OCI artifacts: browse one, install a recipe from it, and update what you installed. `auto-update` is what the scheduled check runs.

## OCI registries

```bash
spark-pulse oci add-registry <name> <url> [--default]
spark-pulse oci list-registries
spark-pulse oci remove-registry <name>
```

Registries are stored in `registries.yaml`. Each names its own token endpoint in the `WWW-Authenticate` header it returns — ghcr.io at `/token`, Docker Hub at `auth.docker.io`, nvcr.io at `/proxy_auth` — and Spark Pulse follows whichever the registry asks for rather than assuming one.

## Development scripts

Not part of the installed CLI, but in the repository:

```bash
./scripts/run-dev-server.sh      # backend :8100 (simulation, reload) + Vite :3000
./scripts/run-backend.sh         # backend only, simulation, Swagger at /docs
./scripts/run-production.sh      # real tools, built UI, no reload
./scripts/run-dev-oidc-full.sh   # + a mock OIDC provider on :9400
./scripts/run-e2e-tests.sh       # Playwright against a simulation backend
./scripts/check-agent-only.sh    # the "nothing runs locally" ratchet, and what is excused
./scripts/generate-proto.sh      # regenerate the agent protocol stubs
./scripts/build-agent.sh         # build the node agent binary
```
