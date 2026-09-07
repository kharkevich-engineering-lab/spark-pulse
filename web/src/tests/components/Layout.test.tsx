/** The nav chip: Cluster is marked experimental, and the mark explains
 * itself rather than leaving an operator to guess what "exp" means. */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import Layout from "@/components/Layout";
import { MULTI_NODE_BADGE_TITLE } from "@/lib/experimental";
import type { AppConfig } from "@/lib/config";

let config: AppConfig | null = null;

vi.mock("@/lib/config", () => ({
  useConfig: () => ({ config, configLoaded: true }),
}));

const logout = vi.fn();
let auth: { isAuthenticated: boolean; user: { name?: string; email?: string } | null } = {
  isAuthenticated: false,
  user: null,
};

vi.mock("@/lib/auth", () => ({
  useAuth: () => ({ ...auth, logout }),
}));

function renderLayout() {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <Layout>
        <div>content</div>
      </Layout>
    </MemoryRouter>,
  );
}

describe("Layout nav", () => {
  beforeEach(() => {
    config = null; // config not loaded yet: the mark defaults to on
    auth = { isAuthenticated: false, user: null };
    logout.mockClear();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ json: async () => ({ version: "1.2.3" }) }),
    );
    // This jsdom environment ships no Web Storage at all, and the theme
    // toggle reads `localStorage` on mount, so give it one — the same kind of
    // missing-browser-API stub setupTests makes for ResizeObserver.
    const store = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, String(v)),
      removeItem: (k: string) => void store.delete(k),
      clear: () => store.clear(),
      key: (i: number) => [...store.keys()][i] ?? null,
      get length() {
        return store.size;
      },
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("marks Cluster as experimental", () => {
    renderLayout();

    const cluster = screen.getByRole("link", { name: /Cluster/ });
    expect(within(cluster).getByTitle(MULTI_NODE_BADGE_TITLE)).toBeInTheDocument();
  });

  it("explains the chip rather than leaving a bare label", () => {
    renderLayout();

    const chip = within(screen.getByRole("link", { name: /Cluster/ })).getByTitle(
      MULTI_NODE_BADGE_TITLE,
    );
    const explanation = chip.getAttribute("title") ?? "";
    // The chip itself is three letters; the tooltip has to carry the reason.
    expect(explanation).toMatch(/never been run on two machines/i);
    expect(explanation.length).toBeGreaterThan(chip.textContent!.length * 5);
    expect(explanation.toLowerCase()).not.toBe("experimental");
  });

  it("marks nothing else, because nothing else is unproven", () => {
    renderLayout();

    for (const label of ["Recipes & Mods", "Inference", "Monitoring", "Settings"]) {
      const link = screen.getByRole("link", { name: new RegExp(label) });
      expect(within(link).queryByTitle(MULTI_NODE_BADGE_TITLE)).toBeNull();
    }
  });

  it("drops the mark when the installation says multi-node is no longer experimental", () => {
    config = {
      auth_enabled: false,
      mcp_enabled: true,
      cluster_enabled: true,
      cluster_experimental: false,
      benchmarking_enabled: false,
      simulation_mode: false,
      runtime: "native",
    };
    renderLayout();

    expect(screen.getByRole("link", { name: /Cluster/ })).toBeInTheDocument();
    expect(screen.queryByTitle(MULTI_NODE_BADGE_TITLE)).toBeNull();
  });

  it("hides Benchmarking until the installation enables it", () => {
    config = {
      auth_enabled: false,
      mcp_enabled: true,
      cluster_enabled: true,
      cluster_experimental: true,
      benchmarking_enabled: false,
      simulation_mode: true,
      runtime: "native",
    };
    renderLayout();

    expect(screen.queryByRole("link", { name: /Benchmarking/ })).toBeNull();
    expect(screen.getByRole("link", { name: /Monitoring/ })).toBeInTheDocument();
  });

  it("shows Benchmarking once it is enabled", () => {
    config = {
      auth_enabled: false,
      mcp_enabled: true,
      cluster_enabled: true,
      cluster_experimental: true,
      benchmarking_enabled: true,
      simulation_mode: true,
      runtime: "native",
    };
    renderLayout();

    expect(screen.getByRole("link", { name: /Benchmarking/ })).toBeInTheDocument();
  });

  it("marks the route being viewed, so the sidebar says where you are", () => {
    render(
      <MemoryRouter initialEntries={["/cluster"]}>
        <Layout>
          <div>content</div>
        </Layout>
      </MemoryRouter>,
    );

    expect(screen.getByRole("link", { name: /Cluster/ })).toHaveClass("text-primary");
    expect(screen.getByRole("link", { name: /Inference/ })).not.toHaveClass("text-primary");
  });

  /** On a phone the sidebar is off-canvas until the menu button is pressed,
   *  and picking a destination puts it away again — otherwise the nav covers
   *  the page it just navigated to. */
  it("opens the sidebar on a small screen and closes it on a choice", async () => {
    const user = userEvent.setup();
    const { container } = renderLayout();
    const sidebar = container.querySelector("aside")!;
    const menu = container.querySelector("button.lg\\:hidden")!;

    expect(sidebar.className).toContain("-translate-x-full");

    await user.click(menu);
    expect(sidebar.className).toContain("translate-x-0");

    await user.click(screen.getByRole("link", { name: /Monitoring/ }));
    expect(sidebar.className).toContain("-translate-x-full");
  });
});

/** The header: who you are, and the way out.
 *
 *  When the installation has auth on, it names the signed-in user and offers
 *  the way to sign out. When it does not, it renders nothing at all. */
describe("Layout header", () => {
  beforeEach(() => {
    config = null;
    auth = { isAuthenticated: false, user: null };
    logout.mockClear();
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({ json: async () => ({ version: "1.2.3" }) }),
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("shows the version the backend reports", async () => {
    renderLayout();
    expect(await screen.findByText("1.2.3")).toBeInTheDocument();
  });

  /** The header carried a red/green SSE dot, a refresh button and a theme
   *  cycler. The dot reported a connection nothing on the page depended on and
   *  read as an error indicator; refresh is what the browser's own reload
   *  does; and the theme is a preference, which now lives with the other
   *  preferences on the Settings page instead of one unlabelled click away
   *  from everywhere. What is left is who you are and the way out. */
  it("carries no theme cycler, refresh button or connection dot", () => {
    renderLayout();

    expect(screen.queryByTitle(/^Theme: /)).toBeNull();
    expect(screen.queryByTitle("Refresh")).toBeNull();
    expect(screen.queryByTitle(/SSE (Connected|Disconnected)/)).toBeNull();
  });

  it("says nothing about a user when the installation has no auth", () => {
    auth = { isAuthenticated: true, user: { name: "Ada" } };
    config = {
      auth_enabled: false,
      mcp_enabled: true,
      cluster_enabled: true,
      cluster_experimental: true,
      benchmarking_enabled: false,
      simulation_mode: true,
      runtime: "native",
    };
    renderLayout();

    expect(screen.queryByText("Ada")).toBeNull();
    expect(screen.queryByTitle("Logout")).toBeNull();
  });

  it("names the signed-in user and signs them out again", async () => {
    const user = userEvent.setup();
    auth = { isAuthenticated: true, user: { name: "Ada", email: "ada@example.com" } };
    config = {
      auth_enabled: true,
      mcp_enabled: true,
      cluster_enabled: true,
      cluster_experimental: true,
      benchmarking_enabled: false,
      simulation_mode: false,
      runtime: "native",
    };
    renderLayout();

    expect(screen.getByText("Ada")).toBeInTheDocument();
    await user.click(screen.getByTitle("Logout"));
    expect(logout).toHaveBeenCalledTimes(1);
  });

  it("falls back to the email when the identity provider sent no name", () => {
    auth = { isAuthenticated: true, user: { email: "ada@example.com" } };
    config = {
      auth_enabled: true,
      mcp_enabled: true,
      cluster_enabled: true,
      cluster_experimental: true,
      benchmarking_enabled: false,
      simulation_mode: false,
      runtime: "native",
    };
    renderLayout();

    expect(screen.getByText("ada@example.com")).toBeInTheDocument();
  });
});
