/** The MCP page: where the endpoint actually is, and how to point a client at it.
 *
 * The one thing here that is easy to get wrong and expensive to debug is the
 * endpoint. The SPA can be served two ways — by the backend itself in the
 * packaged app, or by the Vite dev server on another port — and an operator
 * who copies `http://localhost:3000/mcp` out of the dev server gets a 404 from
 * a server that has no MCP on it. So the page derives the backend's port from
 * settings rather than from the browser's, and says which situation it is in.
 * Both branches are asserted here, and so is the copy button, because a config
 * snippet that cannot be copied is a config snippet that gets mistyped.
 *
 * The other thing worth pinning is that MCP can be off. `app.py` mounts `/mcp`
 * only when `config.mcp_enabled`, so a page that hardcoded "Active" handed the
 * operator an endpoint that answers 404 and no way to find out why. The page
 * reads the flag `/api/config` publishes; `useConfig` is stubbed here so each
 * test can say which server it is looking at, and the unstubbed case falls
 * through to the same cached default the rest of the SPA uses.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import MCPPage from "@/pages/MCPPage";
import type { Settings } from "@/lib/types";

vi.mock("@/lib/api", () => ({
  fetchSettings: vi.fn(),
}));

/** What `/api/config` said, or `null` for "the page is outside a provider". */
let appConfig: AppConfig | null = null;

vi.mock("@/lib/config", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/config")>();
  return { ...actual, useConfig: () => ({ config: appConfig, configLoaded: appConfig !== null }) };
});

import { fetchSettings } from "@/lib/api";
import type { AppConfig } from "@/lib/config";

const config = (over: Partial<AppConfig> = {}): AppConfig => ({
  auth_enabled: false,
  mcp_enabled: true,
  cluster_enabled: false,
  cluster_experimental: true,
  benchmarking_enabled: false,
  simulation_mode: true,
  runtime: "native",
  ...over,
});

/** jsdom serves this suite from http://localhost:3000. */
const BROWSER_PORT = "3000";

function settings(webui_port: number): Settings {
  return {
    spark_vllm_path: "/opt/spark-vllm-docker",
    default_container: "vllm-node",
    default_gpu_mem_util: 0.8,
    default_port_range_start: 9000,
    default_port_range_end: 9100,
    webui_port,
    cluster_enabled: false,
    cluster_experimental: true,
    job_retention_days: 7,
    benchmarking_enabled: false,
  };
}

describe("MCPPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    appConfig = null;
    vi.mocked(fetchSettings).mockResolvedValue(settings(8100));
  });

  it("points at the backend's port, not the dev server's, and says why", async () => {
    appConfig = config({ mcp_enabled: true });
    render(<MCPPage />);

    expect(await screen.findByText("http://localhost:8100/mcp")).toBeInTheDocument();
    expect(
      screen.getByText(new RegExp(`viewing the Vite dev server on port ${BROWSER_PORT}`)),
    ).toBeInTheDocument();
  });

  it("says the origin is already the backend's when the backend is serving this page", async () => {
    appConfig = config({ mcp_enabled: true });
    vi.mocked(fetchSettings).mockResolvedValue(settings(Number(BROWSER_PORT)));
    render(<MCPPage />);

    expect(await screen.findByText("http://localhost:3000/mcp")).toBeInTheDocument();
    expect(screen.getByText(/already being served by the backend/)).toBeInTheDocument();
    expect(screen.queryByText(/Vite dev server/)).toBeNull();
  });

  it("reports the server as active with its transport", async () => {
    appConfig = config({ mcp_enabled: true });
    render(<MCPPage />);

    expect(await screen.findByText("Active")).toBeInTheDocument();
    expect(screen.getByText("HTTP (JSON-RPC 2.0)")).toBeInTheDocument();
  });

  // With no config in context the page falls back to the SPA's cached default,
  // which is the same one `/api/config` failing produces. It must still render
  // rather than blanking the status.
  it("assumes MCP is on when no config has been loaded", async () => {
    render(<MCPPage />);

    expect(await screen.findByText("Active")).toBeInTheDocument();
  });

  it("says MCP is off, and how to turn it on, instead of offering an endpoint", async () => {
    appConfig = config({ mcp_enabled: false });
    render(<MCPPage />);

    expect(await screen.findByText("Disabled")).toBeInTheDocument();
    expect(screen.getByText("The MCP endpoint is not mounted.")).toBeInTheDocument();
    expect(screen.getByText(/a client pointed there would be refused/)).toBeInTheDocument();

    // Both ways to enable it, and the restart that makes it take effect.
    expect(
      screen.getByText(/SPARK_PULSE_MCP_ENABLED=true spark-pulse start/),
    ).toBeInTheDocument();
    expect(screen.getByText(/"mcp_enabled": true/)).toBeInTheDocument();
    expect(screen.getByText(/then restart Spark Pulse/)).toBeInTheDocument();

    // And nothing that looks like something a client could connect to.
    expect(screen.queryByText("http://localhost:8100/mcp")).toBeNull();
    expect(screen.queryByText("HTTP (JSON-RPC 2.0)")).toBeNull();
    expect(screen.queryByText("Endpoint")).toBeNull();
    expect(screen.queryByRole("button", { name: /Claude Desktop/ })).toBeNull();
    expect(screen.queryByText(/same FastAPI process on port/)).toBeNull();
  });

  it("still documents the tools while saying none of them can be called", async () => {
    appConfig = config({ mcp_enabled: false });
    render(<MCPPage />);

    expect(await screen.findByText("Disabled")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /Available Tools \(9\)/ })).toBeInTheDocument();
    expect(
      screen.getByText("These are what MCP would expose. None of them can be called while it is disabled."),
    ).toBeInTheDocument();
  });

  it("lists every tool an assistant can call, with what it does", async () => {
    appConfig = config({ mcp_enabled: true });
    render(<MCPPage />);

    const heading = await screen.findByRole("heading", { name: /Available Tools \(9\)/ });
    expect(heading).toBeInTheDocument();
    expect(screen.getByText("create_deployment")).toBeInTheDocument();
    expect(screen.getByText("Launch a new deployment")).toBeInTheDocument();
    expect(screen.getByText("clean_cache")).toBeInTheDocument();
  });

  it("keeps the setup guides collapsed until one is asked for", async () => {
    appConfig = config({ mcp_enabled: true });
    const user = userEvent.setup();
    render(<MCPPage />);

    await screen.findByText("Active");
    expect(screen.queryByText("claude_desktop_config.json", { selector: "span" })).toBeNull();

    await user.click(screen.getByRole("button", { name: /Claude Desktop/ }));

    expect(
      screen.getByText("claude_desktop_config.json", { selector: "span" }),
    ).toBeInTheDocument();
    // The snippet carries the endpoint the page just derived, not a placeholder.
    expect(screen.getByText(/"http:\/\/localhost:8100\/mcp"/)).toBeInTheDocument();
  });

  it("copies a snippet to the clipboard and confirms it did", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    appConfig = config({ mcp_enabled: true });
    render(<MCPPage />);
    await screen.findByText("Active");
    await user.click(screen.getByRole("button", { name: /curl — quick test/ }));

    const block = screen.getByText("list tools").closest("div")!.parentElement!;
    await user.click(within(block).getByRole("button", { name: /copy/i }));

    expect(writeText).toHaveBeenCalledWith(expect.stringContaining("http://localhost:8100/mcp"));
    expect(await screen.findByText("Copied")).toBeInTheDocument();
  });

  it("assumes the packaged port rather than rendering nothing before settings land", () => {
    appConfig = config({ mcp_enabled: true });
    vi.mocked(fetchSettings).mockReturnValue(new Promise(() => {}));
    render(<MCPPage />);

    expect(screen.getByText("http://localhost:8100/mcp")).toBeInTheDocument();
  });
});
