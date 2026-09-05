"""Tests for the MCP surface: the JSON-RPC handler and the stdio wrapper.

`mcp_http.py` is the whole MCP feature — the tool list an assistant reads and
the dispatch that turns a tool call into a request against this server's own
REST API — and only its tool table was covered. `mcp_server.py`, the
`spark-pulse mcp` entry point, was at 0%: nothing had ever imported it.

Neither talks to a network here. `_http` is exercised against a recording
httpx client, and the stdio wrapper against a fake `mcp` package, because the
`mcp` extra is not installed in this environment — which is itself worth
covering, since that is the case a user hits when they run `spark-pulse mcp`
without it.
"""

from __future__ import annotations

import importlib
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from spark_pulse import mcp_http
from spark_pulse.app import create_app
from spark_pulse.config import config


class _Response:
    def __init__(self, payload, error=None):
        self._payload = payload
        self._error = error

    def raise_for_status(self):
        if self._error:
            raise self._error

    def json(self):
        return self._payload


class _Client:
    """An httpx.AsyncClient stand-in that records what it was asked for."""

    calls: list[tuple] = []
    payload: dict = {"ok": True}
    error: Exception | None = None
    init_kwargs: dict = {}

    def __init__(self, **kwargs):
        type(self).init_kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, url, params=None):
        self.calls.append(("GET", url, None, params))
        return _Response(self.payload, self.error)

    async def post(self, url, json=None):
        self.calls.append(("POST", url, json, None))
        return _Response(self.payload, self.error)

    async def delete(self, url):
        self.calls.append(("DELETE", url, None, None))
        return _Response(self.payload, self.error)


@pytest.fixture
def http_client(monkeypatch):
    """`_http` wired to a recording client instead of a real socket."""
    _Client.calls = []
    _Client.payload = {"ok": True}
    _Client.error = None
    _Client.init_kwargs = {}
    monkeypatch.setattr(mcp_http.httpx, "AsyncClient", _Client)
    return _Client


# ── The tool table ───────────────────────────────────────────────────────────


class TestToolTable:
    def test_every_advertised_tool_has_a_handler(self):
        """A tool an assistant can see but not call is worse than no tool."""
        advertised = {t["name"] for t in mcp_http.TOOLS}

        assert advertised == set(mcp_http.HANDLERS)

    def test_every_tool_declares_a_name_description_and_schema(self):
        for tool in mcp_http.TOOLS:
            assert tool["name"]
            assert tool["description"]
            assert tool["inputSchema"]["type"] == "object"


# ── The REST bridge ──────────────────────────────────────────────────────────


class TestHttpBridge:
    async def test_a_get_reaches_this_servers_own_api_on_its_own_port(
        self, http_client, monkeypatch
    ):
        monkeypatch.setitem(config._data, "webui_port", 8123)

        result = await mcp_http._http("GET", "/recipes", params={"lines": 5})

        assert result == {"ok": True}
        assert http_client.calls == [
            ("GET", "http://127.0.0.1:8123/api/recipes", None, {"lines": 5})
        ]

    async def test_a_post_sends_its_body(self, http_client):
        await mcp_http._http("POST", "/cache/clean", json_body={"targets": ["a"]})

        assert http_client.calls[0][0] == "POST"
        assert http_client.calls[0][2] == {"targets": ["a"]}

    async def test_a_delete_carries_no_body(self, http_client):
        await mcp_http._http("DELETE", "/deployments/dep-1")

        assert http_client.calls[0][:2] == (
            "DELETE",
            f"http://127.0.0.1:{config.webui_port}/api/deployments/dep-1",
        )

    async def test_an_unsupported_method_is_refused(self, http_client):
        with pytest.raises(ValueError, match="Unsupported method: PATCH"):
            await mcp_http._http("PATCH", "/recipes")

        assert http_client.calls == []

    async def test_an_error_status_is_raised_rather_than_returned(self, http_client):
        http_client.error = RuntimeError("500 Server Error")

        with pytest.raises(RuntimeError, match="500 Server Error"):
            await mcp_http._http("GET", "/recipes")

    async def test_the_bridge_does_not_wait_forever(self, http_client):
        await mcp_http._http("GET", "/recipes")

        assert http_client.init_kwargs["timeout"] == 30


# ── The JSON-RPC handler ─────────────────────────────────────────────────────


class TestHandleMcp:
    async def test_initialize_announces_the_server_and_its_tools(self):
        response = await mcp_http.handle_mcp(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
        )

        assert response["id"] == 1
        assert response["result"]["serverInfo"]["name"] == "spark-pulse"
        assert response["result"]["capabilities"]["tools"] == {"listChanged": False}

    async def test_the_initialized_notification_is_answered_without_an_id(self):
        assert await mcp_http.handle_mcp(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        ) == {"jsonrpc": "2.0", "result": None}

    async def test_tools_list_returns_the_table(self):
        response = await mcp_http.handle_mcp(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        )

        assert response["result"]["tools"] == mcp_http.TOOLS

    async def test_resources_and_prompts_are_empty_but_answered(self):
        for method, key in (
            ("resources/list", "resources"),
            ("prompts/list", "prompts"),
        ):
            response = await mcp_http.handle_mcp(
                {"jsonrpc": "2.0", "id": 3, "method": method}
            )

            assert response["result"][key] == []

    async def test_an_unknown_method_is_a_method_not_found_error(self):
        response = await mcp_http.handle_mcp(
            {"jsonrpc": "2.0", "id": 4, "method": "sing/aSong"}
        )

        assert response["error"] == {
            "code": -32601,
            "message": "Unknown method: sing/aSong",
        }

    async def test_a_missing_method_is_also_an_error(self):
        response = await mcp_http.handle_mcp({"jsonrpc": "2.0", "id": 5})

        assert response["error"]["code"] == -32601

    async def test_a_tool_result_comes_back_as_json_text_content(self, http_client):
        http_client.payload = [{"id": "qwen3-8b"}]

        response = await mcp_http.handle_mcp(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {"name": "list_recipes", "arguments": {}},
            }
        )

        content = response["result"]["content"]
        assert content[0]["type"] == "text"
        assert json.loads(content[0]["text"]) == [{"id": "qwen3-8b"}]

    async def test_an_unknown_tool_is_a_method_not_found_error(self, http_client):
        response = await mcp_http.handle_mcp(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "delete_everything", "arguments": {}},
            }
        )

        assert response["error"] == {
            "code": -32601,
            "message": "Unknown tool: delete_everything",
        }
        assert http_client.calls == []

    async def test_a_failing_tool_is_an_internal_error_carrying_the_reason(
        self, http_client
    ):
        http_client.error = RuntimeError("Docker daemon not available")

        response = await mcp_http.handle_mcp(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {"name": "list_deployments", "arguments": {}},
            }
        )

        assert response["error"] == {
            "code": -32603,
            "message": "Docker daemon not available",
        }

    async def test_a_tool_missing_a_required_argument_is_an_error_not_a_crash(
        self, http_client
    ):
        response = await mcp_http.handle_mcp(
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {"name": "get_recipe", "arguments": {}},
            }
        )

        assert response["error"]["code"] == -32603
        assert "name" in response["error"]["message"]


class TestHandlerRouting:
    """Each tool has to reach the right endpoint with the right body."""

    @pytest.mark.parametrize(
        ("tool", "arguments", "expected"),
        [
            ("list_recipes", {}, ("GET", "/api/recipes", None, None)),
            (
                "get_recipe",
                {"name": "qwen3-8b"},
                ("GET", "/api/recipes/qwen3-8b", None, None),
            ),
            (
                "create_deployment",
                {"recipe_id": "r", "name": "n"},
                (
                    "POST",
                    "/api/deployments",
                    {"recipe_id": "r", "name": "n", "params": {}},
                    None,
                ),
            ),
            ("list_deployments", {}, ("GET", "/api/deployments", None, None)),
            (
                "stop_deployment",
                {"id": "dep-1"},
                ("DELETE", "/api/deployments/dep-1", None, None),
            ),
            (
                "get_deployment_logs",
                {"id": "dep-1"},
                ("GET", "/api/deployments/dep-1/logs", None, {"lines": 100}),
            ),
            (
                "get_deployment_logs",
                {"id": "dep-1", "lines": 5},
                ("GET", "/api/deployments/dep-1/logs", None, {"lines": 5}),
            ),
            ("get_memory", {}, ("GET", "/api/memory", None, None)),
            ("list_cache", {}, ("GET", "/api/cache", None, None)),
            (
                "clean_cache",
                {"targets": ["CCache"]},
                ("POST", "/api/cache/clean", {"targets": ["CCache"]}, None),
            ),
            ("list_images", {}, ("GET", "/api/images", None, None)),
            (
                "pull_image",
                {"ref": "vllm:1"},
                ("POST", "/api/images/pull", {"ref": "vllm:1"}, None),
            ),
            ("list_models", {}, ("GET", "/api/models", None, None)),
            ("list_benchmarks", {}, ("GET", "/api/benchmarks", None, None)),
            ("get_benchmark", {"id": "b1"}, ("GET", "/api/benchmarks/b1", None, None)),
            (
                "get_latest_by_recipe",
                {},
                ("GET", "/api/benchmarks/latest-by-recipe", None, None),
            ),
            (
                "compare_benchmarks",
                {"run_ids": ["a", "b"]},
                ("POST", "/api/benchmarks/compare", {"run_ids": ["a", "b"]}, None),
            ),
            ("list_engines", {}, ("GET", "/api/engines", None, None)),
            (
                "model_download_status",
                {},
                ("GET", "/api/models/downloads", None, None),
            ),
            (
                "model_download_status",
                {"job_id": "j1"},
                ("GET", "/api/models/downloads/j1", None, None),
            ),
        ],
    )
    async def test_a_tool_call_reaches_its_endpoint(
        self, http_client, tool, arguments, expected
    ):
        await mcp_http.HANDLERS[tool](arguments)

        method, path, body, params = http_client.calls[0]
        assert (
            method,
            path.split(f":{config.webui_port}")[1],
            body,
            params,
        ) == expected

    async def test_downloading_a_model_passes_every_optional_field(self, http_client):
        await mcp_http.HANDLERS["download_model"]({"model": "org/m"})

        assert http_client.calls[0][2] == {
            "model": "org/m",
            "source": None,
            "revision": None,
            "allow_patterns": None,
        }

    async def test_rendering_a_launch_defaults_the_whole_form(self, http_client):
        await mcp_http.HANDLERS["render_launch"]({"recipe_id": "r"})

        assert http_client.calls[0][2] == {
            "recipe_id": "r",
            "engine": None,
            "variant": None,
            "model": None,
            "params": {},
            "extra_args": [],
            "nodes": [],
            "solo": False,
        }

    async def test_planning_a_deployment_allows_a_missing_model_by_default(
        self, http_client
    ):
        await mcp_http.HANDLERS["plan_deployment"]({"recipe_id": "r"})

        assert http_client.calls[0][2]["allow_missing_model"] is True


# ── The HTTP endpoint on the app ─────────────────────────────────────────────


class TestMcpEndpoint:
    def test_a_json_rpc_request_is_served_on_the_apps_own_port(self):
        with TestClient(create_app()) as client:
            response = client.post(
                f"/{mcp_http.MCP_PATH}",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )

        assert response.status_code == 200
        assert [t["name"] for t in response.json()["result"]["tools"]] == [
            t["name"] for t in mcp_http.TOOLS
        ]

    def test_a_body_that_is_not_json_is_a_parse_error(self):
        with TestClient(create_app()) as client:
            response = client.post(
                f"/{mcp_http.MCP_PATH}",
                content=b"not json",
                headers={"content-type": "application/json"},
            )

        assert response.status_code == 400
        assert response.json()["error"]["code"] == -32700


# ── The stdio wrapper ────────────────────────────────────────────────────────


class TestStdioServerWithoutTheExtra:
    """`spark-pulse mcp` without `pip install spark-pulse[mcp]`."""

    async def test_running_it_says_what_to_install_and_exits(self, monkeypatch, capsys):
        from spark_pulse import mcp_server

        monkeypatch.setattr(mcp_server, "MCP_AVAILABLE", False)

        with pytest.raises(SystemExit) as exit_info:
            await mcp_server.run_mcp_server()

        assert exit_info.value.code == 1
        assert "pip install mcp" in capsys.readouterr().err

    def test_the_entry_point_runs_the_server(self, monkeypatch, capsys):
        import asyncio

        from spark_pulse import mcp_server

        ran: list = []

        def fake_run(coro):
            ran.append(coro)
            coro.close()

        monkeypatch.setattr(asyncio, "run", fake_run)

        mcp_server.main()

        assert len(ran) == 1
        assert "Starting Spark Pulse MCP server" in capsys.readouterr().err


class _FakeServer:
    def __init__(self, name):
        self.name = name
        self.handlers: dict = {}
        self.ran = False

    def list_tools(self):
        def register(fn):
            self.handlers["list_tools"] = fn
            return fn

        return register

    def call_tool(self):
        def register(fn):
            self.handlers["call_tool"] = fn
            return fn

        return register

    def create_initialization_options(self):
        return {"init": True}

    async def run(self, read_stream, write_stream, options):
        self.ran = (read_stream, write_stream, options)


class _FakeStdio:
    def __init__(self):
        self.entered = False

    async def __aenter__(self):
        self.entered = True
        return ("read", "write")

    async def __aexit__(self, *_exc):
        return False


@pytest.fixture
def stdio_server_module(monkeypatch):
    """`mcp_server` reloaded against a fake `mcp` package.

    The extra is not installed here, so the whole stdio path — the tool
    listing and the JSON-RPC-to-TextContent translation an assistant actually
    consumes — is otherwise unreachable.
    """
    servers: list[_FakeServer] = []

    mcp_pkg = ModuleType("mcp")
    server_mod = ModuleType("mcp.server")
    stdio_mod = ModuleType("mcp.server.stdio")
    types_mod = ModuleType("mcp.types")

    def make_server(name):
        server = _FakeServer(name)
        servers.append(server)
        return server

    server_mod.Server = make_server
    stdio_mod.stdio_server = _FakeStdio
    types_mod.Tool = lambda **kwargs: SimpleNamespace(**kwargs)
    types_mod.TextContent = lambda **kwargs: SimpleNamespace(**kwargs)
    mcp_pkg.server = server_mod
    server_mod.stdio = stdio_mod

    monkeypatch.setitem(sys.modules, "mcp", mcp_pkg)
    monkeypatch.setitem(sys.modules, "mcp.server", server_mod)
    monkeypatch.setitem(sys.modules, "mcp.server.stdio", stdio_mod)
    monkeypatch.setitem(sys.modules, "mcp.types", types_mod)

    from spark_pulse import mcp_server

    reloaded = importlib.reload(mcp_server)
    yield reloaded, servers
    # Put the module back the way the rest of the suite expects it.
    monkeypatch.undo()
    importlib.reload(mcp_server)


class TestStdioServerWithTheExtra:
    async def test_it_serves_the_shared_tool_table_over_stdio(
        self, stdio_server_module
    ):
        mcp_server, servers = stdio_server_module

        assert mcp_server.MCP_AVAILABLE is True

        await mcp_server.run_mcp_server()
        server = servers[0]

        assert server.name == "spark-pulse-mcp"
        assert server.ran == ("read", "write", {"init": True})
        listed = await server.handlers["list_tools"]()
        assert [t.name for t in listed] == [t["name"] for t in mcp_http.TOOLS]
        assert listed[0].inputSchema == mcp_http.TOOLS[0]["inputSchema"]

    async def test_a_tool_result_is_handed_back_as_text(
        self, stdio_server_module, http_client
    ):
        mcp_server, servers = stdio_server_module
        http_client.payload = {"deployments": []}
        await mcp_server.run_mcp_server()

        content = await servers[0].handlers["call_tool"]("list_deployments", {})

        assert [c.type for c in content] == ["text"]
        assert json.loads(content[0].text) == {"deployments": []}

    async def test_a_failed_tool_call_is_reported_as_text_not_raised(
        self, stdio_server_module, http_client
    ):
        mcp_server, servers = stdio_server_module
        http_client.error = RuntimeError("docker is down")
        await mcp_server.run_mcp_server()

        content = await servers[0].handlers["call_tool"]("list_deployments", {})

        assert content[0].text == "Error: docker is down"

    async def test_an_unknown_tool_is_reported_as_text(self, stdio_server_module):
        mcp_server, servers = stdio_server_module
        await mcp_server.run_mcp_server()

        content = await servers[0].handlers["call_tool"]("nope", {})

        assert content[0].text == "Error: Unknown tool: nope"
