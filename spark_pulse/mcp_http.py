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
    "list_benchmarks": lambda args: _http("GET", "/benchmarks"),
    "get_benchmark": lambda args: _http("GET", f"/benchmarks/{args['id']}"),
    "get_latest_by_recipe": lambda args: _http("GET", "/benchmarks/latest-by-recipe"),
    "compare_benchmarks": lambda args: _http(
        "POST", "/benchmarks/compare", json_body={"run_ids": args["run_ids"]}
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
