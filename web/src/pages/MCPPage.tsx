import { Bot, Code, Copy, Check, ExternalLink, Plug, Rocket, Settings, Globe, Key, Lock } from "lucide-react";
import { useState } from "react";

const TOOLS = [
  { name: "list_recipes", desc: "List all deployment recipes" },
  { name: "get_recipe", desc: "Get details of a specific recipe" },
  { name: "create_deployment", desc: "Launch a new deployment" },
  { name: "stop_deployment", desc: "Stop a running deployment" },
  { name: "list_deployments", desc: "List all deployments" },
  { name: "get_deployment_logs", desc: "Get deployment logs" },
  { name: "get_memory", desc: "Get GPU/CPU/disk memory stats" },
  { name: "list_cache", desc: "List cached models and artifacts" },
  { name: "clean_cache", desc: "Clean cached models and artifacts" },
];

function CodeBlock({ code, label }: { code: string; label: string }) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    await navigator.clipboard.writeText(code);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="rounded-xl bg-surface border border-border overflow-hidden">
      <div className="flex items-center justify-between px-4 py-2 border-b border-border bg-bg">
        <span className="text-xs text-text-muted">{label}</span>
        <button
          onClick={copy}
          className="flex items-center gap-1.5 text-xs text-text-muted hover:text-text transition-colors"
        >
          {copied ? <Check size={14} className="text-success" /> : <Copy size={14} />}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre className="p-4 text-sm font-mono overflow-x-auto"><code>{code}</code></pre>
    </div>
  );
}

export default function MCPPage() {
  return (
    <div className="space-y-8 max-w-4xl">
      <div>
        <div className="flex items-center gap-3 mb-2">
          <Bot size={32} className="text-primary" />
          <h2 className="text-2xl font-bold">MCP Server</h2>
        </div>
        <p className="text-text-muted">
          Model Context Protocol server runs automatically alongside the web UI.
          AI assistants connect via HTTP to manage your spark-vllm-docker deployments.
        </p>
      </div>

      {/* HTTP Connection */}
      <section className="space-y-4">
        <h3 className="text-lg font-semibold flex items-center gap-2">
          <Globe size={20} />
          HTTP Server
        </h3>
        <p className="text-text-muted">
          The MCP server starts automatically when you run <code className="px-1.5 py-0.5 rounded bg-bg text-sm font-mono">spark-pulse start</code>.
          Connect to it via HTTP with token authentication.
        </p>

        <div className="rounded-xl bg-surface border border-border p-5 space-y-3">
          <div className="flex items-center justify-between text-sm">
            <span className="text-text-muted flex items-center gap-2"><Globe size={14} /> Endpoint</span>
            <span className="font-mono">http://127.0.0.1:8100/mcp</span>
          </div>
          <div className="flex items-center justify-between text-sm">
            <span className="text-text-muted flex items-center gap-2"><Plug size={14} /> Transport</span>
            <span className="font-mono">HTTP (JSON-RPC)</span>
          </div>
          <div className="flex items-center justify-between text-sm">
            <span className="text-text-muted flex items-center gap-2"><Lock size={14} /> Security</span>
            <span className="font-mono">Protected by auth middleware (same as web UI)</span>
          </div>
          <div className="flex items-center justify-between text-sm">
            <span className="text-text-muted flex items-center gap-2"><Key size={14} /> API Token</span>
            <span className="font-mono">Optional — shares auth with web UI</span>
          </div>
        </div>
      </section>

      {/* Claude Desktop */}
      <section className="space-y-4">
        <h3 className="text-lg font-semibold flex items-center gap-2">
          <Settings size={20} />
          Claude Desktop (HTTP)
        </h3>
        <p className="text-text-muted">
          Use HTTP transport for Claude Desktop. The server handles auth via the token you configure.
        </p>

        <CodeBlock
          code={`{
  "mcpServers": {
    "spark-pulse": {
      "command": "npx",
      "args": [
        "-y",
        "@modelcontextprotocol/server-http",
        "--url",
        "http://127.0.0.1:8100/mcp",
        "--headers",
        '{"Authorization": "Bearer YOUR_TOKEN"}'
      ]
    }
  }
}`}
          label="claude_desktop_config.json (HTTP transport)"
        />
      </section>

      {/* curl test */}
      <section className="space-y-4">
        <h3 className="text-lg font-semibold flex items-center gap-2">
          <Code size={20} />
          Test Connection
        </h3>

        <CodeBlock
          code={`# List available tools
curl -X POST http://127.0.0.1:8100/mcp \\
  -H "Content-Type: application/json" \\
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

# Get memory stats
curl -X POST http://127.0.0.1:8100/mcp \\
  -H "Content-Type: application/json" \\
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_memory","arguments":{}}}'`}
          label="curl examples"
        />
      </section>

      {/* Tools */}
      <section className="space-y-4">
        <h3 className="text-lg font-semibold flex items-center gap-2">
          <Rocket size={20} />
          Available Tools
        </h3>
        <p className="text-text-muted">
          The MCP server exposes these tools to AI assistants:
        </p>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          {TOOLS.map((tool) => (
            <div
              key={tool.name}
              className="rounded-xl bg-surface border border-border p-4 hover:border-border-hover transition-colors"
            >
              <div className="font-mono text-sm text-primary mb-1">{tool.name}</div>
              <div className="text-xs text-text-muted">{tool.desc}</div>
            </div>
          ))}
        </div>
      </section>

      {/* CLI */}
      <section className="space-y-4">
        <h3 className="text-lg font-semibold flex items-center gap-2">
          <Plug size={20} />
          CLI Transport (stdio)
        </h3>
        <p className="text-text-muted">
          For clients that support stdio transport (Claude Desktop native, Cursor, etc.),
          use the CLI command to start the server.
        </p>

        <CodeBlock code="$ pip install -e '.[mcp]'" label="Install extra" />
        <CodeBlock code="$ spark-pulse mcp" label="Start stdio server" />
      </section>

      {/* Other clients */}
      <section className="space-y-4">
        <h3 className="text-lg font-semibold flex items-center gap-2">
          <ExternalLink size={20} />
          Other Clients
        </h3>
        <div className="rounded-xl bg-surface border border-border p-5 space-y-4">
          <p className="text-text-muted text-sm">
            Any MCP-compatible client can connect via HTTP (recommended) or stdio transport.
            The HTTP endpoint works with any HTTP client library.
          </p>

          <div className="space-y-3">
            <h4 className="font-semibold text-sm">Cursor / Windsurf</h4>
            <p className="text-xs text-text-muted">
              Use the Claude Desktop HTTP config above, or run <code className="px-1 py-0.5 rounded bg-bg font-mono">spark-pulse mcp</code> for stdio mode.
            </p>
          </div>

          <div className="space-y-3">
            <h4 className="font-semibold text-sm">Python Client</h4>
            <CodeBlock
              code={`from mcp import Client
async with Client("http://127.0.0.1:8100/mcp") as client:
    tools = await client.list_tools()
    for t in tools:
        print(t.name)`}
              label="Python MCP client"
            />
          </div>
        </div>
      </section>
    </div>
  );
}
