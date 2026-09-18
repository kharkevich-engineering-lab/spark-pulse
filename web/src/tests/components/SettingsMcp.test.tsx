/** The MCP tab: where the endpoint is, and how to point a client at it.
 *
 * The page's own tests cover the tool list and the two origin branches. What
 * is here is the rest of the tab: the state where MCP is *off*, where every
 * snippet would be a lie because there is nothing to connect to, and the
 * setup guides — collapsed until asked for, and each carrying the endpoint the
 * tab just derived rather than a placeholder somebody has to replace.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import SettingsMcp from "@/components/SettingsMcp";

/** jsdom serves this suite from http://localhost:3000, and the backend says
 *  8100 — so these tests are looking at the dev server, which is the case an
 *  operator gets wrong by copying the browser's own origin. */
const ENDPOINT = "http://localhost:8100/mcp";

function stubTools(tools: { name: string; description?: string }[]) {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ jsonrpc: "2.0", id: 1, result: { tools } }),
    }),
  );
}

describe("SettingsMcp when the endpoint is mounted", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    stubTools([{ name: "get_memory", description: "Get GPU / CPU / disk memory stats" }]);
  });

  it("reports the server as active with its transport", async () => {
    render(<SettingsMcp enabled port={8100} />);

    expect(await screen.findByText("Active")).toBeInTheDocument();
    expect(screen.getByText("HTTP (JSON-RPC 2.0)")).toBeInTheDocument();
    expect(screen.getByText(ENDPOINT)).toBeInTheDocument();
  });

  it("keeps the setup guides collapsed until one is asked for", async () => {
    const user = userEvent.setup();
    render(<SettingsMcp enabled port={8100} />);
    await screen.findByText("Active");

    expect(screen.queryByText("claude_desktop_config.json", { selector: "span" })).toBeNull();

    await user.click(screen.getByRole("button", { name: /Claude Desktop/ }));

    expect(screen.getByText("claude_desktop_config.json", { selector: "span" })).toBeInTheDocument();
    // The snippet carries the endpoint the tab just derived, not a placeholder.
    expect(screen.getByText(new RegExp(`"${ENDPOINT}"`))).toBeInTheDocument();
  });

  it("closes a guide that was opened", async () => {
    const user = userEvent.setup();
    render(<SettingsMcp enabled port={8100} />);
    await screen.findByText("Active");

    const guide = screen.getByRole("button", { name: /Claude Desktop/ });
    await user.click(guide);
    expect(guide).toHaveAttribute("aria-expanded", "true");

    await user.click(guide);
    expect(guide).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("claude_desktop_config.json", { selector: "span" })).toBeNull();
  });

  /** A config snippet that cannot be copied is a config snippet that gets
   *  mistyped. */
  it("copies a snippet to the clipboard and confirms it did", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });

    render(<SettingsMcp enabled port={8100} />);
    await screen.findByText("Active");
    await user.click(screen.getByRole("button", { name: /curl — quick test/ }));

    const block = screen.getByText("list tools").closest("div")!.parentElement!;
    await user.click(within(block).getByRole("button", { name: /copy/i }));

    expect(writeText).toHaveBeenCalledWith(expect.stringContaining(ENDPOINT));
    expect(await screen.findByText("Copied")).toBeInTheDocument();
  });

  it("offers the stdio route for a client that spawns a process", async () => {
    const user = userEvent.setup();
    render(<SettingsMcp enabled port={8100} />);
    await screen.findByText("Active");

    await user.click(screen.getByRole("button", { name: /Cursor/ }));

    expect(screen.getByText("spark-pulse mcp")).toBeInTheDocument();
  });

  it("offers a Python client that lists the same tools", async () => {
    const user = userEvent.setup();
    render(<SettingsMcp enabled port={8100} />);
    await screen.findByText("Active");

    await user.click(screen.getByRole("button", { name: /Python client/ }));

    expect(screen.getByText(/await client.list_tools\(\)/)).toBeInTheDocument();
  });
});

/** `app.py` mounts `/mcp` only when `config.mcp_enabled`, so a tab that
 *  hardcoded "Active" handed the operator an endpoint that answers 404 and no
 *  way to find out why. */
describe("SettingsMcp when the endpoint is not mounted", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    stubTools([]);
  });

  it("says MCP is off, and how to turn it on, instead of offering an endpoint", async () => {
    render(<SettingsMcp enabled={false} port={8100} />);

    expect(await screen.findByText("Disabled")).toBeInTheDocument();
    expect(screen.getByText("The MCP endpoint is not mounted.")).toBeInTheDocument();
    expect(screen.getByText(/a client pointed there would be refused/)).toBeInTheDocument();

    // Both ways to enable it, and the restart that makes it take effect.
    expect(screen.getByText(/SPARK_PULSE_MCP_ENABLED=true spark-pulse start/)).toBeInTheDocument();
    expect(screen.getByText(/"mcp_enabled": true/)).toBeInTheDocument();
    expect(screen.getByText(/then restart Spark Pulse/)).toBeInTheDocument();

    // And nothing that looks like something a client could connect to.
    expect(screen.queryByText(ENDPOINT)).toBeNull();
    expect(screen.queryByText("HTTP (JSON-RPC 2.0)")).toBeNull();
    expect(screen.queryByRole("button", { name: /Claude Desktop/ })).toBeNull();
  });

  /** The tools are still worth documenting — they are what turning it on would
   *  expose — but the page has to say none of them can be called. */
  it("still documents the tools while saying none of them can be called", async () => {
    stubTools([{ name: "list_recipes", description: "List all deployment recipes" }]);
    render(<SettingsMcp enabled={false} port={8100} />);

    expect(await screen.findByRole("heading", { name: "Tools (1)" })).toBeInTheDocument();
    expect(
      screen.getByText(
        "These are what MCP would expose. None of them can be called while it is disabled.",
      ),
    ).toBeInTheDocument();
  });
});

describe("SettingsMcp tool list", () => {
  beforeEach(() => vi.clearAllMocks());

  /** A JSON-RPC error is a 200 with an `error` member, so "the response was
   *  OK" is not the same as "the call worked". */
  it("treats a JSON-RPC error as a failure rather than an empty list", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ jsonrpc: "2.0", id: 1, error: { code: -32001, message: "Unauthorized" } }),
      }),
    );
    render(<SettingsMcp enabled port={8100} />);

    expect(await screen.findByText(`Could not read the tool list from ${ENDPOINT}.`)).toBeInTheDocument();
  });

  it("reports an empty list as empty rather than as a failure", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ ok: true, json: async () => ({ jsonrpc: "2.0", id: 1, result: {} }) }),
    );
    render(<SettingsMcp enabled port={8100} />);

    expect(await screen.findByRole("heading", { name: "Tools (0)" })).toBeInTheDocument();
    expect(screen.queryByText(/Could not read the tool list/)).toBeNull();
  });
});
