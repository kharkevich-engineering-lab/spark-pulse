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

function withAuth(overrides: Partial<AppConfig> = {}): AppConfig {
  return {
    auth_enabled: false,
    mcp_enabled: true,
    cluster_enabled: true,
    cluster_experimental: true,
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

describe("the header nav", () => {
  it("lists five groups, not eleven routes", () => {
    renderLayout();

    const labels = primaryNav()
      .getAllByRole("link")
      .map((a) => a.textContent?.replace(/exp$/, "").trim());
    expect(labels).toEqual(["Deploy", "Runs", "Fleet", "Library", "Settings"]);
  });

  /** The reason the nav is groups: a route that is not a group's own href is
   *  still *in* that group, and reading `/monitoring` with nothing lit is how
   *  an operator loses track of where they are. */
  it("marks a group for any route it speaks for", () => {
    for (const [path, label] of [
      ["/", "Deploy"],
      ["/benchmarking", "Runs"],
      ["/monitoring", "Fleet"],
      ["/engines", "Library"],
      ["/oci", "Library"],
      ["/mcp", "Settings"],
    ] as const) {
      const { unmount } = renderLayout(path);
      expect(primaryNav().getByRole("link", { name: new RegExp(label) })).toHaveAttribute(
        "aria-current",
        "page",
      );
      unmount();
    }
  });

  it("marks nothing for a path no group claims", () => {
    expect(activeGroup("/nope")).toBe("");
    renderLayout("/nope");
    for (const link of primaryNav().getAllByRole("link")) {
      expect(link).not.toHaveAttribute("aria-current");
    }
  });

  it("covers every route the groups were built from", () => {
    const routes = NAV_GROUPS.flatMap((g) => g.routes);
    expect(routes).toEqual(
      expect.arrayContaining([
        "/",
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

  it("keeps the experimental mark on Fleet, which is where the cluster is", () => {
    renderLayout();
    const fleet = primaryNav().getByRole("link", { name: /Fleet/ });
    expect(within(fleet).getByTitle(MULTI_NODE_BADGE_TITLE)).toBeInTheDocument();
  });

  it("drops the mark when the installation says multi-node is no longer experimental", () => {
    config = withAuth({ cluster_experimental: false });
    renderLayout();
    expect(primaryNav().getByRole("link", { name: /Fleet/ })).toBeInTheDocument();
    expect(screen.queryByTitle(MULTI_NODE_BADGE_TITLE)).toBeNull();
  });

  /** Benchmarking is a tab of Runs now, so the flag no longer removes a nav
   *  entry — Runs is there either way. */
  it("shows the same five groups whether or not benchmarking is enabled", () => {
    config = withAuth({ benchmarking_enabled: false });
    const { unmount } = renderLayout();
    expect(primaryNav().getAllByRole("link")).toHaveLength(5);
    unmount();

    config = withAuth({ benchmarking_enabled: true });
    renderLayout();
    expect(primaryNav().getAllByRole("link")).toHaveLength(5);
  });
});

describe("the brand lockup", () => {
  it("reports the backend version beside the company", async () => {
    renderLayout();
    expect(await screen.findByText("1.2.3")).toBeInTheDocument();
    expect(screen.getAllByText(/Kharkevich Engineering Lab/).length).toBeGreaterThan(0);
  });

  it("goes home", () => {
    renderLayout("/settings");
    expect(screen.getByRole("link", { name: /Spark Pulse home/ })).toHaveAttribute("href", "/");
  });
});

describe("the header actions", () => {
  it("cycles the theme system → light → dark and says which it is on", async () => {
    const user = userEvent.setup();
    renderLayout();

    const button = () => screen.getAllByRole("button", { name: /Colour theme/ })[0];
    expect(button()).toHaveAttribute("aria-label", "Colour theme: System");
    await user.click(button());
    expect(button()).toHaveAttribute("aria-label", "Colour theme: Light");
    await user.click(button());
    expect(button()).toHaveAttribute("aria-label", "Colour theme: Dark");
    await user.click(button());
    expect(button()).toHaveAttribute("aria-label", "Colour theme: System");
  });

  it("offers the other language and marks the current one", async () => {
    const user = userEvent.setup();
    renderLayout();

    const control = screen.getAllByRole("button", { name: /Language/ })[0];
    expect(within(control).getByText("EN")).toHaveAttribute("aria-current", "true");
    expect(within(control).getByText("FR")).not.toHaveAttribute("aria-current");

    await user.click(control);
    const after = screen.getAllByRole("button", { name: /Langue/ })[0];
    expect(within(after).getByText("FR")).toHaveAttribute("aria-current", "true");
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
    await user.click(within(screen.getByTestId("mobile-nav")).getByRole("link", { name: /Fleet/ }));
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
