/** The app shell: five groups in one header, and a menu under 900px.
 *
 * The properties that matter are the ones the sidebar got wrong. A group has
 * to light up for *any* route it speaks for, not only its own — `/monitoring`
 * used to leave nothing marked. The menu has to close on Escape and on a
 * choice, and hand focus back to the button that opened it. And sign-out has
 * to be reachable at every width: the old header was `hidden lg:flex`, so on a
 * phone there was no way out at all.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import Layout, { NAV_GROUPS, activeGroup } from "@/components/Layout";
import { I18nProvider } from "@/lib/i18n";
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

function withAuth(overrides: Partial<AppConfig> = {}): AppConfig {
  return {
    auth_enabled: false,
    mcp_enabled: true,
    cluster_enabled: true,
    benchmarking_enabled: false,
    simulation_mode: true,
    runtime: "native",
    ...overrides,
  };
}

function renderLayout(path = "/") {
  return render(
    <I18nProvider>
      <MemoryRouter initialEntries={[path]}>
        <Layout>
          <div>content</div>
        </Layout>
      </MemoryRouter>
    </I18nProvider>,
  );
}

/** The desktop nav, which is the one that carries `data-testid`. */
function primaryNav() {
  return within(screen.getByTestId("primary-nav"));
}

function stubStorage() {
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
}

beforeEach(() => {
  config = null;
  auth = { isAuthenticated: false, user: null };
  logout.mockClear();
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({ json: async () => ({ version: "1.2.3" }) }),
  );
  stubStorage();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the sidebar", () => {
  it("lists every object type, grouped as Operate, Library and System", () => {
    renderLayout();

    const labels = primaryNav()
      .getAllByRole("link")
      .map((a) => a.textContent?.trim());
    expect(labels).toEqual([
      "Overview",
      "Runs",
      "Nodes",
      "Recipes",
      "Models",
      "Engines",
      "Mods",
      "Settings",
    ]);
    for (const section of ["Operate", "Library", "System"]) {
      expect(primaryNav().getByText(section)).toBeInTheDocument();
    }
  });

  /** A route without an entry of its own still belongs to one: `/mcp` is
   *  Settings, `/cache` is Models, and reading either with nothing lit is how
   *  an operator loses track of where they are. */
  it("marks the entry for any route it speaks for", () => {
    for (const [path, label] of [
      ["/", "Overview"],
      ["/recipes", "Recipes"],
      ["/jobs", "Runs"],
      ["/monitoring", "Nodes"],
      ["/mods", "Mods"],
      ["/engines", "Engines"],
      ["/oci", "Recipes"],
      ["/cache", "Models"],
      ["/mcp", "Settings"],
    ] as const) {
      const { unmount } = renderLayout(path);
      expect(primaryNav().getByRole("link", { name: label })).toHaveAttribute("aria-current", "page");
      unmount();
    }
  });

  it("marks nothing for a path no entry claims", () => {
    expect(activeGroup("/nope")).toBe("");
    renderLayout("/nope");
    for (const link of primaryNav().getAllByRole("link")) {
      expect(link).not.toHaveAttribute("aria-current");
    }
  });

  it("covers every route the app answers", () => {
    const routes = NAV_GROUPS.flatMap((g) => g.routes);
    expect(routes).toEqual(
      expect.arrayContaining([
        "/",
        "/recipes",
        "/jobs",
        "/benchmarking",
        "/cluster",
        "/monitoring",
        "/models",
        "/engines",
        "/cache",
        "/oci",
        "/settings",
        "/mcp",
      ]),
    );
  });

  /** Benchmarks has an entry only where the feature is on; off, `/benchmarking`
   *  still answers and lights nothing rather than a dead link. */
  it("lists Benchmarks only when benchmarking is enabled", () => {
    config = withAuth({ benchmarking_enabled: false });
    const { unmount } = renderLayout();
    expect(primaryNav().queryByRole("link", { name: "Benchmarks" })).toBeNull();
    unmount();

    config = withAuth({ benchmarking_enabled: true });
    renderLayout("/benchmarking");
    expect(primaryNav().getByRole("link", { name: "Benchmarks" })).toHaveAttribute("aria-current", "page");
  });
});

describe("the brand lockup", () => {
  it("reports the backend version beside the company", async () => {
    renderLayout();
    expect((await screen.findAllByText("1.2.3")).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Kharkevich Engineering Lab/).length).toBeGreaterThan(0);
  });

  it("goes home", () => {
    renderLayout("/settings");
    for (const link of screen.getAllByRole("link", { name: /Spark Pulse home/ })) {
      expect(link).toHaveAttribute("href", "/");
    }
  });
});

describe("the header actions", () => {
  it("leaves the theme and the language to Settings", () => {
    renderLayout();
    // Both are preferences set once, not controls used while reading a page;
    // they live on Settings → Preferences and nowhere in the shell.
    expect(screen.queryByRole("button", { name: /Colour theme/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Language/ })).toBeNull();
  });

  it("says nothing about a user when the installation has no auth", () => {
    auth = { isAuthenticated: true, user: { name: "Ada" } };
    config = withAuth({ auth_enabled: false });
    renderLayout();

    expect(screen.queryByText("Ada")).toBeNull();
    expect(screen.queryByRole("button", { name: "Logout" })).toBeNull();
  });

  it("names the signed-in user and signs them out again", async () => {
    const user = userEvent.setup();
    auth = { isAuthenticated: true, user: { name: "Ada", email: "ada@example.com" } };
    config = withAuth({ auth_enabled: true });
    renderLayout();

    expect(screen.getByText("Ada")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Logout" }));
    expect(logout).toHaveBeenCalledTimes(1);
  });

  it("falls back to the email when the identity provider sent no name", () => {
    auth = { isAuthenticated: true, user: { email: "ada@example.com" } };
    config = withAuth({ auth_enabled: true });
    renderLayout();

    expect(screen.getByText("ada@example.com")).toBeInTheDocument();
  });
});

describe("the mobile menu", () => {
  it("is shut until the menu button is pressed, and says so", async () => {
    const user = userEvent.setup();
    renderLayout();

    const button = screen.getByRole("button", { name: "Menu" });
    expect(button).toHaveAttribute("aria-expanded", "false");
    expect(button).toHaveAttribute("aria-controls", "mobile-menu");
    expect(screen.queryByTestId("mobile-nav")).toBeNull();

    await user.click(button);
    expect(button).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("mobile-nav")).toBeInTheDocument();
  });

  /** Sign-out was `hidden lg:flex`: on a phone there was no way out of the
   *  application at all. */
  it("carries the user chip and the way out", async () => {
    const user = userEvent.setup();
    auth = { isAuthenticated: true, user: { name: "Ada" } };
    config = withAuth({ auth_enabled: true });
    renderLayout();

    await user.click(screen.getByRole("button", { name: "Menu" }));
    const menu = within(document.getElementById("mobile-menu")!);
    expect(menu.getByText("Ada")).toBeInTheDocument();
    await menu.getByRole("button", { name: "Logout" }).click();
    expect(logout).toHaveBeenCalled();
  });

  it("puts itself away on a choice, because it covers the page it navigated to", async () => {
    const user = userEvent.setup();
    renderLayout();

    await user.click(screen.getByRole("button", { name: "Menu" }));
    await user.click(within(screen.getByTestId("mobile-nav")).getByRole("link", { name: "Nodes" }));
    expect(screen.queryByTestId("mobile-nav")).toBeNull();
  });

  it("closes on Escape and hands focus back to the button that opened it", async () => {
    const user = userEvent.setup();
    renderLayout();

    const button = screen.getByRole("button", { name: "Menu" });
    await user.click(button);
    expect(screen.getByTestId("mobile-nav")).toBeInTheDocument();

    await user.keyboard("{Escape}");
    expect(screen.queryByTestId("mobile-nav")).toBeNull();
    expect(document.activeElement).toBe(button);
  });

  it("moves focus into the menu on open, and locks the page behind it", async () => {
    const user = userEvent.setup();
    renderLayout();

    await user.click(screen.getByRole("button", { name: "Menu" }));
    expect(document.body.style.overflow).toBe("hidden");
    expect(screen.getByTestId("mobile-nav").contains(document.activeElement)).toBe(true);

    await user.keyboard("{Escape}");
    expect(document.body.style.overflow).not.toBe("hidden");
  });
});
