import { Bot, Code2, Copy, Check, Plug, Globe, Key, Lock, Terminal, ChevronDown } from "lucide-react";
import { useState } from "react";
import { fetchSettings } from "@/lib/api";
import { useQuery } from "@/hooks/useQuery";

const TOOLS = [
  { name: "list_recipes", desc: "List all deployment recipes" },
  { name: "get_recipe", desc: "Get details of a specific recipe" },
  { name: "create_deployment", desc: "Launch a new deployment" },
  { name: "stop_deployment", desc: "Stop a running deployment" },
  { name: "list_deployments", desc: "List all deployments and status" },
  { name: "get_deployment_logs", desc: "Stream or fetch deployment logs" },
  { name: "get_memory", desc: "Get GPU / CPU / disk memory stats" },
  { name: "list_cache", desc: "List cached models and artifacts" },
  { name: "clean_cache", desc: "Delete cached models and artifacts" },
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
        <span className="text-xs text-text-muted font-mono">{label}</span>
        <button onClick={copy} className="flex items-center gap-1.5 text-xs text-text-muted hover:text-text transition-colors">
          {copied ? <Check size={13} className="text-success" /> : <Copy size={13} />}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <pre className="p-4 text-sm font-mono overflow-x-auto leading-relaxed"><code>{code}</code></pre>
    </div>
  );
}

function SetupSection({ title, children }: { title: string; children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-xl bg-surface border border-border overflow-hidden">
      <button onClick={() => setOpen(v => !v)} className="w-full flex items-center justify-between px-5 py-4 hover:bg-surface-hover transition-colors">
        <span className="font-semibold text-sm">{title}</span>
        <ChevronDown size={16} className={`text-text-muted transition-transform ${open ? "rotate-180" : ""}`} />
      </button>
      {open && <div className="px-5 pb-5 space-y-4 border-t border-border">{children}</div>}
    </div>
  );
}

export default function MCPPage() {
  const { data: settings } = useQuery(fetchSettings);

  const port = settings?.webui_port ?? 8100;
  const mcpPath = "/mcp";
  const endpoint = `http://127.0.0.1:${port}${mcpPath}`;
  const enabled = true; // MCP is always running alongside the web UI

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold">MCP Server</h2>
        <p className="text-text-muted mt-1">Model Context Protocol — connect AI assistants to manage deployments</p>
      </div>

      {/* Status + connection */}
      <div className="rounded-xl bg-surface border border-border p-5 space-y-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Bot size={18} className="text-primary" />
            <span className="font-semibold">Server Status</span>
          </div>
          {enabled
            ? <span className="flex items-center gap-1.5 text-xs text-success font-medium px-2.5 py-1 rounded-full bg-success/15"><span className="w-1.5 h-1.5 rounded-full bg-success animate-pulse" />Active</span>
            : <span className="text-xs text-text-muted px-2.5 py-1 rounded-full bg-bg">Disabled</span>}
        </div>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-sm">
          <div className="flex items-center justify-between p-2.5 rounded-lg bg-bg gap-3">
            <span className="flex items-center gap-2 text-text-muted shrink-0"><Globe size={13} />Endpoint</span>
            <span className="font-mono text-xs truncate">{endpoint}</span>
          </div>
          <div className="flex items-center justify-between p-2.5 rounded-lg bg-bg gap-3">
            <span className="flex items-center gap-2 text-text-muted shrink-0"><Plug size={13} />Transport</span>
            <span className="font-mono text-xs">HTTP (JSON-RPC 2.0)</span>
          </div>
          <div className="flex items-center justify-between p-2.5 rounded-lg bg-bg gap-3">
            <span className="flex items-center gap-2 text-text-muted shrink-0"><Lock size={13} />Security</span>
            <span className="font-mono text-xs">Shared with web UI</span>
          </div>
          <div className="flex items-center justify-between p-2.5 rounded-lg bg-bg gap-3">
            <span className="flex items-center gap-2 text-text-muted shrink-0"><Key size={13} />API Token</span>
            <span className="font-mono text-xs">Optional — set in Settings</span>
          </div>
        </div>
      </div>

      {/* Tools grid */}
      <div>
        <h3 className="text-base font-semibold mb-3 flex items-center gap-2"><Code2 size={16} className="text-primary" />Available Tools ({TOOLS.length})</h3>
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
          {TOOLS.map((tool) => (
            <div key={tool.name} className="p-4 rounded-xl bg-surface border border-border hover:border-border-hover transition-colors group">
              <p className="font-mono text-sm text-primary group-hover:text-primary-hover transition-colors mb-1">{tool.name}</p>
              <p className="text-xs text-text-muted">{tool.desc}</p>
            </div>
          ))}
        </div>
      </div>

      {/* Setup guides (collapsible) */}
      <div>
        <h3 className="text-base font-semibold mb-3 flex items-center gap-2"><Terminal size={16} className="text-primary" />Setup Guides</h3>
        <div className="space-y-2">
          <SetupSection title="Claude Desktop (HTTP transport)">
            <p className="text-xs text-text-muted pt-1">Add to your <code className="font-mono bg-bg px-1 py-0.5 rounded">claude_desktop_config.json</code>:</p>
            <CodeBlock label="claude_desktop_config.json" code={`{
  "mcpServers": {
    "spark-pulse": {
      "command": "npx",
      "args": [
        "-y",
        "@modelcontextprotocol/server-http",
        "--url",
        "${endpoint}",
        "--headers",
        "{\\"Authorization\\": \\"Bearer YOUR_TOKEN\\"}"
      ]
    }
  }
}`} />
          </SetupSection>

          <SetupSection title="Cursor / Windsurf (stdio)">
            <p className="text-xs text-text-muted pt-1">Install and run the stdio server:</p>
            <CodeBlock label="install" code={`pip install -e '.[mcp]'`} />
            <CodeBlock label="stdio server" code={`spark-pulse mcp`} />
          </SetupSection>

          <SetupSection title="Python client">
            <CodeBlock label="python" code={`from mcp import Client

async with Client("${endpoint}") as client:
    tools = await client.list_tools()
    for t in tools:
        print(t.name)`} />
          </SetupSection>

          <SetupSection title="curl — quick test">
            <CodeBlock label="list tools" code={`curl -X POST ${endpoint} \\
  -H "Content-Type: application/json" \\
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'`} />
            <CodeBlock label="call get_memory" code={`curl -X POST ${endpoint} \\
  -H "Content-Type: application/json" \\
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_memory","arguments":{}}}'`} />
          </SetupSection>
        </div>
      </div>
    </div>
  );
}

