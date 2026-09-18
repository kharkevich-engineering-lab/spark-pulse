/** The MCP tab of Settings: where the endpoint is, and how to point a client
 *  at it.
 *
 * This was a page of its own under `/mcp`, which is a route an operator
 * reached once — to copy a snippet — and never again. It is a way *in* to this
 * control plane, like authentication and the allowed origins, so it belongs
 * with them.
 *
 * Two things here are easy to get wrong and expensive to debug. The endpoint:
 * the SPA can be served by the backend itself or by the Vite dev server on
 * another port, and an operator who copies `http://localhost:3000/mcp` out of
 * the dev server gets a 404 from a server that has no MCP on it — so the
 * endpoint is derived from the *backend's* port, and the page says which
 * situation it is in. And whether MCP is mounted at all: `app.py` mounts it
 * only when `config.mcp_enabled`, so a page that hardcoded "Active" handed the
 * operator an endpoint that answers 404 and no way to find out why.
 *
 * The tool list is **asked of the server**, over the same `tools/list` call a
 * client makes. It used to be a nine-entry array in this file; the server has
 * offered more than twice that for a while, so the page was quietly
 * documenting a subset and nothing could notice. A list that cannot go stale
 * is the only kind worth showing next to "how to connect".
 */

import { useCallback, useState } from "react";
import { Check, ChevronDown, Copy, Globe, Key, Lock, Plug } from "lucide-react";
import { useI18n } from "@/lib/i18n";
import { useQuery } from "@/hooks/useQuery";
import { Code, ErrorLine, Spinner } from "@/ui";
import SettingsSection from "@/components/SettingsSection";

/** The ports `vite.config.ts` and `config.cors_allowed_origins` both name.
 *  A page served from one of these, on a port the backend does not claim, is
 *  the dev server; anything else serving this SPA is the backend. */
const VITE_DEV_PORTS = new Set(["3000", "5173"]);

/** One tool, as `tools/list` reports it. */
export interface McpTool {
  name: string;
  description?: string;
}

/** Ask the MCP endpoint for its own tool list.
 *
 * Not through `lib/api.ts`: that wrapper prefixes `/api` and this is the
 * JSON-RPC endpoint mounted beside it. `origin` is the *backend's* — the same
 * one the snippets carry — so this works from the dev server too, which the
 * default CORS list already allows.
 */
export async function fetchMcpTools(origin: string, signal?: AbortSignal): Promise<McpTool[]> {
  const res = await fetch(`${origin}/mcp`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list", params: {} }),
    signal,
  });
  if (!res.ok) throw new Error(`MCP ${res.status}`);
  const body = (await res.json()) as {
    result?: { tools?: McpTool[] };
    error?: { message?: string };
  };
  if (body.error) throw new Error(body.error.message ?? "MCP error");
  return body.result?.tools ?? [];
}

function CodeBlock({ code, label }: { code: string; label: string }) {
  const { t } = useI18n();
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    await navigator.clipboard.writeText(code);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };
  return (
    <div className="overflow-hidden rounded-md border border-line bg-surface">
      <div className="flex items-center justify-between gap-3 border-b border-line bg-bg px-4 py-2">
        <span className="font-mono text-[12.5px] text-muted">{label}</span>
        <button
          type="button"
          onClick={copy}
          className="flex shrink-0 items-center gap-1.5 text-[13px] text-muted transition-colors hover:text-text"
        >
          {copied ? <Check size={13} className="text-good" /> : <Copy size={13} />}
          {copied ? t("common.copied") : t("common.copy")}
        </button>
      </div>
      <pre className="overflow-x-auto p-4 font-mono text-[12.5px] leading-relaxed">
        <code>{code}</code>
      </pre>
    </div>
  );
}

function SetupSection({ title, children }: { title: string; children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="overflow-hidden rounded-md border border-line bg-surface">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center justify-between gap-3 px-5 py-4 text-left transition-colors hover:border-line-strong"
      >
        <span className="text-[14px] font-semibold">{title}</span>
        <ChevronDown
          size={16}
          className={`shrink-0 text-muted transition-transform ${open ? "rotate-180" : ""}`}
        />
      </button>
      {open && <div className="space-y-4 border-t border-line px-5 pb-5 pt-4">{children}</div>}
    </div>
  );
}

/** One `label · value` row of the connection block. */
function Row({ icon: Icon, label, value }: { icon: typeof Globe; label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-sm bg-bg px-3 py-2.5">
      <span className="flex shrink-0 items-center gap-2 text-[13px] text-muted">
        <Icon size={13} />
        {label}
      </span>
      <span className="truncate font-mono text-[12.5px]">{value}</span>
    </div>
  );
}

export interface SettingsMcpProps {
  /** Whether `app.py` mounted the endpoint — `/api/config`'s `mcp_enabled`. */
  enabled: boolean;
  /** The port the backend serves on, from `/api/settings`. */
  port: number;
}

export default function SettingsMcp({ enabled, port }: SettingsMcpProps) {
  const { t } = useI18n();

  const browserUrl = typeof window === "undefined" ? undefined : new URL(window.location.href);
  const currentPort = browserUrl?.port;
  // The only way the SPA is served by something that is *not* the backend is
  // the Vite dev server, and it is on one of two known ports — the same two
  // `config.cors_allowed_origins` names. Any other origin serving this page is
  // the backend itself, whatever `webui_port` happens to say: a control plane
  // started on another port, or behind a proxy, used to be told its endpoint
  // was on 8100, which answers nothing.
  const isDevServer =
    currentPort !== undefined &&
    VITE_DEV_PORTS.has(currentPort) &&
    currentPort !== String(port);
  const backendOrigin = isDevServer
    ? `${browserUrl?.protocol ?? "http:"}//${browserUrl?.hostname ?? "127.0.0.1"}:${port}`
    : (browserUrl?.origin ?? `http://127.0.0.1:${port}`);
  const endpoint = `${backendOrigin}/mcp`;

  const loadTools = useCallback(
    (signal?: AbortSignal) => fetchMcpTools(backendOrigin, signal),
    [backendOrigin],
  );
  const { data: tools, loading: toolsLoading, error: toolsError } = useQuery(loadTools);

  return (
    <div>
      {/* ── Status and connection ────────────────────────────────────────── */}
      <SettingsSection
        first
        title={t("mcp.status")}
        hint={
          enabled
            ? isDevServer
              ? t("settingsPage.mcpDevServer", { devPort: currentPort ?? "", endpoint })
              : t("settingsPage.mcpSameOrigin", { endpoint })
            : t("settingsPage.mcpOffHint")
        }
        actions={
          enabled ? (
            <span className="inline-flex items-center gap-1.5 rounded-full bg-good/15 px-2.5 py-1 text-[13px] font-medium text-good">
              <span className="h-1.5 w-1.5 rounded-full bg-good" />
              {t("mcp.active")}
            </span>
          ) : (
            <span className="inline-flex items-center gap-1.5 rounded-full border border-line px-2.5 py-1 text-[13px] font-medium text-muted">
              <span className="h-1.5 w-1.5 rounded-full bg-muted" />
              {t("mcp.disabled")}
            </span>
          )
        }
      >
        {enabled ? (
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
            <Row icon={Globe} label={t("mcp.endpoint")} value={endpoint} />
            <Row icon={Plug} label={t("mcp.transport")} value={t("mcp.transportValue")} />
            <Row icon={Lock} label={t("mcp.security")} value={t("mcp.securityValue")} />
            <Row icon={Key} label={t("mcp.apiToken")} value={t("settingsPage.apiTokenValue")} />
          </div>
        ) : (
          <div className="space-y-3">
            <p className="text-[14px] font-medium">{t("mcp.notMounted")}</p>
            <p className="text-[13px] leading-snug text-muted">
              {t("settingsPage.mcpNotMountedBody")}
            </p>
            <p className="text-[13px] text-muted">{t("mcp.turnOn")}</p>
            <CodeBlock
              label={t("settingsPage.envVarLabel")}
              code={"SPARK_PULSE_MCP_ENABLED=true spark-pulse start"}
            />
            <CodeBlock
              label="~/.config/spark-pulse/settings.json"
              code={'{\n  "mcp_enabled": true\n}'}
            />
          </div>
        )}
      </SettingsSection>

      {/* ── The tools, as the server reports them ────────────────────────── */}
      <SettingsSection
        title={t("settingsPage.toolsHeading", { count: tools?.length ?? 0 })}
        hint={enabled ? t("settingsPage.toolsHint") : t("mcp.toolsNote")}
      >
        {toolsLoading ? (
          <div className="flex justify-center py-8">
            <Spinner label={t("common.loading")} />
          </div>
        ) : toolsError ? (
          <ErrorLine>{t("settingsPage.toolsFailed", { endpoint })}</ErrorLine>
        ) : (
          <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
            {(tools ?? []).map((tool) => (
              <div key={tool.name} className="rounded-md border border-line bg-surface p-4">
                <p className="mb-1 font-mono text-[13px] text-blue2">{tool.name}</p>
                <p className="text-[13px] leading-snug text-muted">{tool.description}</p>
              </div>
            ))}
          </div>
        )}
      </SettingsSection>

      {/* ── How to connect. Every snippet embeds the endpoint, so there is
              nothing honest to show while MCP is off. ────────────────────── */}
      {enabled && (
        <SettingsSection title={t("mcp.guides")} hint={t("settingsPage.guidesHint")}>
          <div className="space-y-2">
            <SetupSection title={t("mcp.claudeDesktop")}>
              <p className="text-[13px] text-muted">
                {t("mcp.claudeDesktopBody")} <Code>claude_desktop_config.json</Code>
              </p>
              <CodeBlock
                label="claude_desktop_config.json"
                code={`{
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
}`}
              />
            </SetupSection>

            <SetupSection title={t("mcp.cursor")}>
              <p className="text-[13px] text-muted">{t("mcp.cursorBody")}</p>
              <CodeBlock label={t("settingsPage.installLabel")} code={"pip install -e '.[mcp]'"} />
              <CodeBlock label={t("settingsPage.stdioLabel")} code={"spark-pulse mcp"} />
            </SetupSection>

            <SetupSection title={t("mcp.python")}>
              <CodeBlock
                label="python"
                code={`from mcp import Client

async with Client("${endpoint}") as client:
    tools = await client.list_tools()
    for t in tools:
        print(t.name)`}
              />
            </SetupSection>

            <SetupSection title={t("mcp.curl")}>
              <CodeBlock
                label={t("settingsPage.listToolsLabel")}
                code={`curl -X POST ${endpoint} \\
  -H "Content-Type: application/json" \\
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'`}
              />
              <CodeBlock
                label={t("settingsPage.callToolLabel")}
                code={`curl -X POST ${endpoint} \\
  -H "Content-Type: application/json" \\
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"get_memory","arguments":{}}}'`}
              />
            </SetupSection>
          </div>
        </SettingsSection>
      )}
    </div>
  );
}
