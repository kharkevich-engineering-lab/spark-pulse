"""MCP (Model Context Protocol) HTTP server for spark-pulse.

Integrated directly into the main FastAPI app at /mcp.
No separate server — shares the same port and authentication.

The `spark-pulse mcp` CLI command still uses stdio mode.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from spark_pulse.config import config

# ── Tool definitions ─────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "list_recipes",
        "description": "List all deployment recipes",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_recipe",
        "description": "Get details of a specific recipe",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Recipe name"}},
            "required": ["name"],
        },
    },
    {
        "name": "create_deployment",
        "description": "Launch a new deployment",
        "inputSchema": {
            "type": "object",
            "properties": {
                "recipe_id": {"type": "string", "description": "Recipe name to deploy"},
                "name": {"type": "string", "description": "Deployment name"},
                "params": {"type": "object", "description": "Override parameters"},
            },
            "required": ["recipe_id", "name"],
        },
    },
    {
        "name": "list_deployments",
        "description": "List all deployments",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "stop_deployment",
        "description": "Stop a running deployment",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "Deployment ID"}},
            "required": ["id"],
        },
    },
    {
        "name": "get_deployment_logs",
        "description": "Get deployment logs",
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Deployment ID"},
                "lines": {
                    "type": "integer",
                    "description": "Number of lines (default 100)",
                    "default": 100,
                },
            },
            "required": ["id"],
        },
    },
    {
        "name": "get_memory",
        "description": "Get GPU/CPU/disk memory stats",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_cache",
        "description": "List cached models and artifacts",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "clean_cache",
        "description": "Clean cached models and artifacts",
        "inputSchema": {
            "type": "object",
            "properties": {
                "targets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Cache names to clean, or ['all']",
                },
            },
            "required": ["targets"],
        },
    },
    {
        "name": "list_images",
        "description": (
            "List the engine image catalogue: which images are present locally, "
            "their size and engine, and whether a newer digest is available"
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "pull_image",
        "description": "Start an engine image pull job and return the job record",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "Image reference, e.g. ghcr.io/org/vllm:0.1.0",
                },
            },
            "required": ["ref"],
        },
    },
    {
        "name": "list_models",
        "description": "List the model catalogue (cached HF models and local sources)",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "download_model",
        "description": "Start a model download job and return the job record",
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string", "description": "HuggingFace model id"},
                "source": {
                    "type": "string",
                    "description": "Model source name (default: first configured)",
                },
                "revision": {
                    "type": "string",
                    "description": "Model revision (branch, tag or commit sha)",
                },
                "allow_patterns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional glob filter for downloaded files",
                },
            },
            "required": ["model"],
        },
    },
    {
        "name": "model_download_status",
        "description": "Get one download job by id, or all jobs when id is omitted",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Download job ID"}
            },
        },
    },
    {
        "name": "list_benchmarks",
        "description": "List all model benchmarks",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_benchmark",
        "description": "Get details of a specific benchmark run",
        "inputSchema": {
            "type": "object",
            "properties": {"id": {"type": "string", "description": "Benchmark ID"}},
            "required": ["id"],
        },
    },
    {
        "name": "get_latest_by_recipe",
        "description": "Get the latest benchmark result for each model/recipe",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "compare_benchmarks",
        "description": "Compare multiple benchmark runs against each other",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of benchmark IDs to compare",
                },
            },
            "required": ["run_ids"],
        },
    },
    {
        "name": "list_engines",
        "description": "List available serving engines with capabilities, image ref and digest",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "render_launch",
        "description": (
            "Dry run: render the per-rank launch scripts for a recipe on an engine"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "recipe_id": {"type": "string", "description": "Recipe id or name"},
                "engine": {
                    "type": "string",
                    "description": "Engine override (vllm, sglang)",
                },
                "variant": {"type": "string", "description": "Engine variant"},
                "model": {"type": "string", "description": "Model override"},
                "params": {"type": "object", "description": "Parameter overrides"},
                "extra_args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Extra engine args appended to the command",
                },
                "nodes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Node hosts; head first. Empty means solo.",
                },
                "solo": {
                    "type": "boolean",
                    "description": "Force a single-node render",
                },
            },
            "required": ["recipe_id"],
        },
    },
    {
        "name": "plan_deployment",
        "description": (
            "Dry run a deployment: resolve engine, image, model, mods, port, "
            "rendered command and container profile without starting anything"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "recipe_id": {"type": "string", "description": "Recipe id or name"},
                "engine": {
                    "type": "string",
                    "description": "Engine override (vllm, sglang)",
                },
                "variant": {"type": "string", "description": "Engine variant"},
                "model": {"type": "string", "description": "Model override"},
                "params": {"type": "object", "description": "Parameter overrides"},
                "extra_args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Extra engine args appended to the command",
                },
                "nodes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Node hosts; head first. Empty means solo.",
                },
                "allow_missing_model": {
                    "type": "boolean",
                    "description": (
                        "Plan even when the model is not in the local catalogue "
                        "(default true for a dry run)"
                    ),
                },
            },
            "required": ["recipe_id"],
        },
    },
]


# ── Tool handlers ────────────────────────────────────────────────────────────


async def _http(
    method: str, path: str, json_body: dict | None = None, params: dict | None = None
) -> Any:
    url = f"http://127.0.0.1:{config.webui_port}/api{path}"
    async with httpx.AsyncClient(timeout=30) as client:
        if method == "GET":
            r = await client.get(url, params=params)
        elif method == "POST":
            r = await client.post(url, json=json_body)
        elif method == "DELETE":
            r = await client.delete(url)
        else:
            raise ValueError(f"Unsupported method: {method}")
        r.raise_for_status()
        return r.json()


HANDLERS: dict[str, Any] = {
    "list_recipes": lambda args: _http("GET", "/recipes"),
    "get_recipe": lambda args: _http("GET", f"/recipes/{args['name']}"),
    "create_deployment": lambda args: _http(
        "POST",
        "/deployments",
        json_body={
            "recipe_id": args["recipe_id"],
            "name": args["name"],
            "params": args.get("params", {}),
        },
    ),
    "list_deployments": lambda args: _http("GET", "/deployments"),
    "stop_deployment": lambda args: _http("DELETE", f"/deployments/{args['id']}"),
    "get_deployment_logs": lambda args: _http(
        "GET",
        f"/deployments/{args['id']}/logs",
        params={"lines": args.get("lines", 100)},
    ),
    "get_memory": lambda args: _http("GET", "/memory"),
    "list_cache": lambda args: _http("GET", "/cache"),
    "clean_cache": lambda args: _http(
        "POST", "/cache/clean", json_body={"targets": args["targets"]}
    ),
    "list_images": lambda args: _http("GET", "/images"),
    "pull_image": lambda args: _http(
        "POST", "/images/pull", json_body={"ref": args["ref"]}
    ),
    "list_models": lambda args: _http("GET", "/models"),
    "download_model": lambda args: _http(
        "POST",
        "/models/download",
        json_body={
            "model": args["model"],
            "source": args.get("source"),
            "revision": args.get("revision"),
            "allow_patterns": args.get("allow_patterns"),
        },
    ),
    "model_download_status": lambda args: _http(
        "GET",
        (
            f"/models/downloads/{args['job_id']}"
            if args.get("job_id")
            else "/models/downloads"
        ),
    ),
    "list_benchmarks": lambda args: _http("GET", "/benchmarks"),
    "get_benchmark": lambda args: _http("GET", f"/benchmarks/{args['id']}"),
    "get_latest_by_recipe": lambda args: _http("GET", "/benchmarks/latest-by-recipe"),
    "compare_benchmarks": lambda args: _http(
        "POST", "/benchmarks/compare", json_body={"run_ids": args["run_ids"]}
    ),
    "list_engines": lambda args: _http("GET", "/engines"),
    "render_launch": lambda args: _http(
        "POST",
        "/engines/render",
        json_body={
            "recipe_id": args["recipe_id"],
            "engine": args.get("engine"),
            "variant": args.get("variant"),
            "model": args.get("model"),
            "params": args.get("params", {}),
            "extra_args": args.get("extra_args", []),
            "nodes": args.get("nodes", []),
            "solo": args.get("solo", False),
        },
    ),
    "plan_deployment": lambda args: _http(
        "POST",
        "/deployments/plan",
        json_body={
            "recipe_id": args["recipe_id"],
            "engine": args.get("engine"),
            "variant": args.get("variant"),
            "model": args.get("model"),
            "params": args.get("params", {}),
            "extra_args": args.get("extra_args", []),
            "nodes": args.get("nodes", []),
            "allow_missing_model": args.get("allow_missing_model", True),
        },
    ),
}


# ── MCP JSON-RPC endpoint ────────────────────────────────────────────────────

MCP_PATH = config.mcp_path.strip("/")


async def handle_mcp(request_json: dict) -> dict:
    """Handle a single MCP JSON-RPC request."""
    method = request_json.get("method", "")
    params = request_json.get("params", {})
    req_id = request_json.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "spark-pulse", "version": "0.1.0"},
            },
        }

    if method == "notifications/initialized":
        return {"jsonrpc": "2.0", "result": None}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})
        handler = HANDLERS.get(tool_name)
        if not handler:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
            }
        try:
            result = await handler(tool_args)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps(result)}]},
            }
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": str(e)},
            }

    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"resources": []}}

    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": req_id, "result": {"prompts": []}}

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Unknown method: {method}"},
    }
