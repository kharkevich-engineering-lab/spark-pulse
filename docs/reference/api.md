# HTTP API

Everything the UI does, it does through this API — there is no privileged path the browser has and a script does not. Interactive docs are served by the app itself at **`/docs`** (Swagger) and **`/redoc`**; this page is the map.

Base URL: `http://<host>:8100`.

## Deployments

| | |
|---|---|
| `GET /api/deployments` | Every deployment, reconciled against what the nodes are running. |
| `POST /api/deployments/plan` | Dry run: resolve engine, image, model, mods, port, ranks. Starts nothing. |
| `POST /api/deployments` | Create. Runs the pre-flight first; `409` with the report when it blocks, `400` with `detail.missing_model` when the model is not downloaded. |
| `GET /api/deployments/{id}` | One deployment, with live status. |
| `DELETE /api/deployments/{id}` | Records the intent and returns. `{"accepted": true, "sync": "in_progress"⎮"deleting", "intent": "stop"⎮"delete"}`. |
| `GET /api/deployments/{id}/logs?lines=` | Tail of the log. |
| `GET /api/deployments/{id}/metrics` | The engine's own metrics window, plus `available` / `reason` / `detail`. |

A create that needs a download goes through `POST /api/scheduled-deploys`, which starts the download and deploys when it finishes.

## Nodes

| | |
|---|---|
| `GET /api/nodes` | The registry. |
| `POST /api/nodes` | Add one. The id is minted server-side and is never sent by the client. |
| `PATCH /api/nodes/{id}` · `DELETE /api/nodes/{id}` | Rename or forget. |
| `GET /api/nodes/discover` | Peers found over mDNS — offered, never required. |
| `GET /api/nodes/diagnostics` | Findings, each with its remedy. |

## Monitoring

| | |
|---|---|
| `GET /api/memory` | `{"nodes": [...]}` — one block per registered node, control plane first. |
| `DELETE /api/memory/processes/{pid}?node=&force=` | End a GPU process on the node that has it. |
| `GET /sse/metrics` | The same answer, streamed every 5s. |
| `GET /sse/deployments` | Deployment events, including `deployment_sync` and `deployment_deleted`. |

## Models

| | |
|---|---|
| `GET /api/models` | The catalogue. |
| `GET /api/models/{id}` | One model, with config summary and revisions. |
| `POST /api/models/download` · `GET /api/models/downloads` · `POST /api/models/downloads/{job}/cancel` | Download jobs. |
| `GET /api/models/{id}/verify?revision=&deep=` | This node's copy: absent, partial or verified. |
| `GET /api/models/{id}/presence?nodes=a,b&deep=` | The same verdict per node. |
| `POST /api/models/{id}/sync` | Replicate to nodes. |
| `DELETE /api/models/{id}?nodes=a,b&revision=` | Remove here and on the nodes named; each answers for itself. |

## Engines and images

| | |
|---|---|
| `GET /api/engines` | Every engine, its spec and whether its image is available. |
| `GET /api/engines/{engine}/{variant}` | One spec in full. |
| `POST /api/engines/refresh` | Re-read the engine indexes. |
| `POST /api/engines/render` | Render the launch command for a plan. |
| `GET /api/images` · `POST /api/images/pull` · `DELETE /api/images/{ref}` | Image inventory, pulls, removal across nodes. |

## Recipes and mods

| | |
|---|---|
| `GET /api/recipes` · `GET /api/recipes/{id}` | The catalogue. |
| `PUT /api/recipes/customize/{id}` · `DELETE …` | Saved parameter overrides. |
| `GET/POST/PUT/DELETE /api/custom-files/recipes⎮mods` | Your own recipe and mod files. |
| `GET /api/mods` · `GET /api/mods/{id}` · `POST /api/mods/validate` | List, read, and check a mod for what it must never do. |

Applying a mod is not an endpoint: a recipe names its mods and they are copied into each rank's container at deploy time.

## OCI recipe collections

`/api/oci/registries…`, `/api/oci/collections…`, `/api/oci/recipes/install⎮update⎮meta`, `/api/oci/check`, `/api/oci/auto-update/settings`.

## Settings, config and health

| | |
|---|---|
| `GET /api/config` | What the frontend needs at startup to gate features. |
| `GET /api/settings` · `PUT /api/settings` | Read everything; write only the allowlist. |
| `GET /api/cache` · `POST /api/cache/clean` | Caches on this host. |
| `GET /health` · `GET /version` | Liveness and build. |

## Calling it from a script

```bash
# Plan without deploying
curl -s localhost:8100/api/deployments/plan \
  -H 'content-type: application/json' \
  -d '{"recipe_id": "bundled/qwen3.8-27b"}' | jq .

# Deploy
curl -s localhost:8100/api/deployments \
  -H 'content-type: application/json' \
  -d '{"recipe_id": "bundled/qwen3.8-27b", "name": "qwen"}' | jq .

# Stop it, then clear the record
curl -s -X DELETE localhost:8100/api/deployments/<id>
curl -s -X DELETE localhost:8100/api/deployments/<id>
```

A request with no `Origin` header — curl, a script, an MCP client — is not a browser and passes the CSRF check. A request carrying a *foreign* `Origin` is refused. See [the browser boundary](../architecture/overview.md).
