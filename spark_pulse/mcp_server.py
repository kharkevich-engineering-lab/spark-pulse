"""MCP stdio transport for spark-pulse.

Wraps the shared MCP handler from mcp_http so tool definitions and
business logic live in one place.  This module only handles the
stdio wire format required by AI assistants (Claude Desktop, etc.).

Usage: spark-pulse mcp
"""

from __future__ import annotations

import sys

try:
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import Tool, TextContent

    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False

from spark_pulse.mcp_http import TOOLS, handle_mcp


async def run_mcp_server() -> None:
    """Run the MCP server in stdio mode."""
    if not MCP_AVAILABLE:
        print("Error: mcp package not installed. Run: pip install mcp", file=sys.stderr)
        sys.exit(1)

    server = Server("spark-pulse-mcp")

    @server.list_tools()
    async def list_tools():
        return [
            Tool(
                name=t["name"],
                description=t["description"],
                inputSchema=t["inputSchema"],
            )
            for t in TOOLS
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        rpc = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        response = await handle_mcp(rpc)
        content = response.get("result", {}).get("content", [])
        if content:
            return [
                TextContent(type="text", text=c["text"])
                for c in content
                if c.get("type") == "text"
            ]
        error = response.get("error", {})
        return [
            TextContent(type="text", text=f"Error: {error.get('message', 'unknown')}")
        ]

    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


def main() -> None:
    """Entry point for `spark-pulse mcp` CLI command."""
    import asyncio

    print("Starting Spark Pulse MCP server...", file=sys.stderr)
    asyncio.run(run_mcp_server())
