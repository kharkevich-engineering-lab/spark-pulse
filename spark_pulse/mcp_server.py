"""MCP (Model Context Protocol) server for spark-vllm-docker operations.

Exposes spark-vllm-docker management as MCP tools that AI assistants
(Claude Desktop, ChatGPT, etc.) can call.

Usage: spark-pulse mcp

The server runs in stdio mode by default (for embedding in AI assistants).
"""

from __future__ import annotations

import json
import sys
from typing import Any

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False


def _get_tools() -> list[dict[str, Any]]:
    """Build list of MCP tools from spark-pulse operations."""
    return [
        {
            "name": "list_recipes",
            "description": "List all available deployment recipes for spark-vllm-docker.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_recipe",
            "description": "Get detailed information about a specific deployment recipe.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Recipe name (e.g., 'qwen3.5-397b-int4')"},
                },
                "required": ["name"],
            },
        },
        {
            "name": "create_deployment",
            "description": "Launch a new model deployment from a recipe. Returns the deployment ID.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "recipe_id": {"type": "string", "description": "Recipe name to deploy"},
                    "name": {"type": "string", "description": "Deployment name"},
                    "params": {
                        "type": "object",
                        "description": "Override parameters (port, tensor_parallel, gpu_memory_utilization, etc.)",
                    },
                },
                "required": ["recipe_id", "name"],
            },
        },
        {
            "name": "list_deployments",
            "description": "List all current deployments with their status, port, and PID.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "stop_deployment",
            "description": "Stop a running deployment by its ID.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Deployment ID to stop"},
                },
                "required": ["id"],
            },
        },
        {
            "name": "get_deployment_logs",
            "description": "Get recent logs for a deployment.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Deployment ID"},
                    "lines": {"type": "integer", "description": "Number of recent lines (default 100)", "default": 100},
                },
                "required": ["id"],
            },
        },
        {
            "name": "get_memory",
            "description": "Get current GPU, CPU, and disk memory usage stats.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "list_cache",
            "description": "List all cache directories with their sizes and file counts.",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "clean_cache",
            "description": "Clean specified cache directories. Use 'all' to clean everything.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "targets": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Cache names to clean, or ['all'] to clean everything",
                    },
                },
                "required": ["targets"],
            },
        },
    ]


def _handle_tool(name: str, arguments: dict[str, Any]) -> list[dict]:
    """Execute a tool and return MCP-formatted result."""
    try:
        import httpx
        base = "http://localhost:8100/api"

        handlers = {
            "list_recipes": lambda: httpx.get(f"{base}/recipes").json(),
            "get_recipe": lambda: httpx.get(f"{base}/recipes/{arguments['name']}").json(),
            "create_deployment": lambda: httpx.post(f"{base}/deployments", json=arguments).json(),
            "list_deployments": lambda: httpx.get(f"{base}/deployments").json(),
            "stop_deployment": lambda: httpx.delete(f"{base}/deployments/{arguments['id']}").json(),
            "get_deployment_logs": lambda: httpx.get(f"{base}/deployments/{arguments['id']}/logs", params={"lines": arguments.get("lines", 100)}).json(),
            "get_memory": lambda: httpx.get(f"{base}/memory").json(),
            "list_cache": lambda: httpx.get(f"{base}/cache").json(),
            "clean_cache": lambda: httpx.post(f"{base}/cache/clean", json=arguments).json(),
        }

        handler = handlers.get(name)
        if not handler:
            return [{"type": "text", "text": f"Unknown tool: {name}"}]

        result = handler()
        return [{"type": "text", "text": json.dumps(result, indent=2)}]

    except httpx.HTTPError as e:
        return [{"type": "text", "text": f"HTTP error: {e}"}]
    except Exception as e:
        return [{"type": "text", "text": f"Error: {e}"}]


async def run_mcp_server():
    """Run the MCP server in stdio mode."""
    if not MCP_AVAILABLE:
        print("Error: mcp package not installed. Run: pip install mcp", file=sys.stderr)
        sys.exit(1)

    app = Server("spark-pulse-mcp")

    @app.list_tools()
    async def list_tools():
        tools = _get_tools()
        return [
            Tool(
                name=t["name"],
                description=t["description"],
                inputSchema=t["inputSchema"],
            )
            for t in tools
        ]

    @app.call_tool()
    async def call_tool(name: str, arguments: dict):
        result = _handle_tool(name, arguments)
        return [TextContent(type="text", text=r["text"]) for r in result]

    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


def main():
    """Entry point for `spark-pulse mcp` CLI command."""
    import asyncio
    print("Starting Spark Pulse MCP server...", file=sys.stderr)
    asyncio.run(run_mcp_server())
