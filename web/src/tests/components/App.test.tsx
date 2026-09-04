/** Routing: which URL renders which page, and the two routes that are special.
 *
 * Every page is stubbed here — this file is about the route table, not about
 * what any page draws. The two behaviours worth pinning are the ones a
 * refactor breaks silently:
 *
 * * **The login route renders outside the shell.** It is the one page
 *   reachable without a session, so drawing the sidebar around it would offer
 *   an operator a nav they cannot use, and (worse) mount the pages behind it.
 * * **The benchmarking route is gated on `/api/config`.** Benchmarking ships disabled;
 *   a build that routes to it anyway shows a feature the backend will refuse.
 *   The gate redirects home rather than rendering an error, so the assertion
 *   is that Recipes appears.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import App from "@/App";
import type { AppConfig } from "@/lib/config";

let config: AppConfig | null = null;

vi.mock("@/lib/api", () => ({ initCsrfToken: vi.fn() }));

vi.mock("@/lib/config", () => ({
  ConfigProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useConfig: () => ({ config, configLoaded: config !== null }),
}));

vi.mock("@/lib/auth", () => ({
  AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useAuth: () => ({ isAuthenticated: false, user: null, logout: vi.fn() }),
}));

vi.mock("@/components/Layout", () => ({
  default: ({ children }: { children: React.ReactNode }) => (
    <div data-testid="shell">{children}</div>
  ),
}));

// Every routed page, stubbed to nothing but its own name. The factories are
// hoisted above every local binding, so each one spells itself out.
vi.mock("@/pages/RecipesPage", () => ({ default: () => <div>recipes page</div> }));
vi.mock("@/pages/InferencePage", () => ({ default: () => <div>inference page</div> }));
vi.mock("@/pages/ClusterPage", () => ({ default: () => <div>cluster page</div> }));
vi.mock("@/pages/BenchmarkingPage", () => ({ default: () => <div>benchmarking page</div> }));
vi.mock("@/pages/MemoryPage", () => ({ default: () => <div>monitoring page</div> }));
vi.mock("@/pages/ModelsPage", () => ({ default: () => <div>models page</div> }));
vi.mock("@/pages/ImagesPage", () => ({ default: () => <div>images page</div> }));
vi.mock("@/pages/CachePage", () => ({ default: () => <div>cache page</div> }));
vi.mock("@/pages/MCPPage", () => ({ default: () => <div>mcp page</div> }));
vi.mock("@/pages/OciRegistryPage", () => ({ default: () => <div>oci page</div> }));
vi.mock("@/pages/SettingsPage", () => ({ default: () => <div>settings page</div> }));
vi.mock("@/pages/LoginPage", () => ({ default: () => <div>login page</div> }));

function renderAt(path: string) {
  window.history.pushState({}, "", path);
  return render(<App />);
}

const ROUTES: [string, string][] = [
  ["/", "recipes page"],
  ["/jobs", "inference page"],
  ["/cluster", "cluster page"],
  ["/monitoring", "monitoring page"],
  ["/models", "models page"],
  ["/images", "images page"],
  ["/cache", "cache page"],
  ["/mcp", "mcp page"],
  ["/oci", "oci page"],
  ["/settings", "settings page"],
];

describe("App routing", () => {
  beforeEach(() => {
    config = {
      auth_enabled: false,
      mcp_enabled: true,
      cluster_enabled: false,
      cluster_experimental: true,
      benchmarking_enabled: false,
      simulation_mode: true,
      runtime: "native",
    };
  });

  it.each(ROUTES)("renders %s inside the shell", (path, content) => {
    renderAt(path);

    expect(screen.getByText(content)).toBeInTheDocument();
    expect(screen.getByTestId("shell")).toBeInTheDocument();
  });

  it("renders the login page with no shell around it", () => {
    renderAt("/login");

    expect(screen.getByText("login page")).toBeInTheDocument();
    expect(screen.queryByTestId("shell")).toBeNull();
    expect(screen.queryByText("recipes page")).toBeNull();
  });

  it("sends /benchmarking home while the backend has the feature off", async () => {
    renderAt("/benchmarking");

    await waitFor(() => expect(screen.getByText("recipes page")).toBeInTheDocument());
    expect(screen.queryByText("benchmarking page")).toBeNull();
  });

  it("routes to /benchmarking once the backend enables it", () => {
    config = { ...config!, benchmarking_enabled: true };
    renderAt("/benchmarking");

    expect(screen.getByText("benchmarking page")).toBeInTheDocument();
  });

  /** Config arrives a tick after the first render, so the gate has to be
   *  closed while it is still null rather than flashing the page open. */
  it("keeps the gate shut while config has not arrived", async () => {
    config = null;
    renderAt("/benchmarking");

    await waitFor(() => expect(screen.getByText("recipes page")).toBeInTheDocument());
  });
});
