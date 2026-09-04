/** The nav chip: Cluster is marked experimental, and the mark explains
 * itself rather than leaving an operator to guess what "exp" means. */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import Layout from "@/components/Layout";
import { MULTI_NODE_BADGE_TITLE } from "@/lib/experimental";
import type { AppConfig } from "@/lib/config";

let config: AppConfig | null = null;

vi.mock("@/lib/config", () => ({
  useConfig: () => ({ config, configLoaded: true }),
}));

vi.mock("@/lib/auth", () => ({
  useAuth: () => ({ isAuthenticated: false, user: null, logout: vi.fn() }),
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
});
