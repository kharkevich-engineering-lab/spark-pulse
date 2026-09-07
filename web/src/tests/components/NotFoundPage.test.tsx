/** The page for an address this application does not have.
 *
 * There was no such page. The backend serves `index.html` for any path it does
 * not recognise — that is how a single-page app has to work — and the router
 * then matched nothing and rendered an empty shell inside the sidebar. A typed
 * URL, a stale bookmark or a renamed route all produced a blank panel with no
 * explanation and no way back.
 */

import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import NotFoundPage from "@/pages/NotFoundPage";
import { KNOWN_PATHS } from "@/App";

vi.mock("@/lib/auth", () => ({
  AuthProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useAuth: () => ({ isAuthenticated: false, user: null, login: vi.fn(), logout: vi.fn() }),
}));

vi.mock("@/lib/config", () => ({
  ConfigProvider: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  useConfig: () => ({ config: null }),
}));

describe("NotFoundPage", () => {
  it("says the address does not exist, rather than showing an empty panel", () => {
    render(
      <MemoryRouter>
        <NotFoundPage />
      </MemoryRouter>,
    );

    expect(screen.getByText("404")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "This page does not exist" })).toBeInTheDocument();
  });

  /** A dead end is the failure this page exists to fix: the browser's own back
   *  button was the only way out. */
  it("offers a way back into the application", () => {
    render(
      <MemoryRouter>
        <NotFoundPage />
      </MemoryRouter>,
    );

    expect(screen.getByRole("link", { name: /Back to Recipes/ })).toHaveAttribute("href", "/");
  });

  /** Login and this page render outside `Layout`, so they carry no sidebar and
   *  had no attribution at all — and they are the two pages most likely to be
   *  someone's first view of the product. */
  it("names who made this, and links to them", () => {
    render(
      <MemoryRouter>
        <NotFoundPage />
      </MemoryRouter>,
    );

    const link = screen.getByRole("link", { name: /Kharkevich Engineering Lab/ });
    expect(link).toHaveAttribute("href", "https://kharkevich.com");
    expect(screen.getByAltText("Kharkevich Engineering Lab")).toBeInTheDocument();
  });
});

/** The list the not-found check consults is *derived* from the routes, so it
 *  cannot drift: adding a page without remembering a second list would
 *  otherwise make that page answer 404. This holds the derivation. */
describe("the known paths", () => {
  it("covers every route the application renders, and login", () => {
    expect(KNOWN_PATHS).toContain("/login");
    for (const path of ["/", "/jobs", "/cluster", "/monitoring", "/models", "/settings"]) {
      expect(KNOWN_PATHS).toContain(path);
    }
  });

  it("holds nothing else", () => {
    for (const path of ["/nope", "/settings/extra", "/jobs/42", ""]) {
      expect(KNOWN_PATHS).not.toContain(path);
    }
  });
});
