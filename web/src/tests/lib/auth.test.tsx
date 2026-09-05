/** The auth context: who is signed in, and what the shell shows because of it.
 *
 * There is no token in the browser — the session is a cookie — so
 * "authenticated" is decided by two things: whether the deployment has auth
 * turned on at all, and whether `/auth/me` answers. The case worth guarding is
 * the first one: with auth disabled, nobody signs in and *everything* must
 * still be reachable. A regression there locks an operator out of their own
 * single-user install.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { AuthProvider, useAuth } from "@/lib/auth";
import type { AppConfig } from "@/lib/config";

const navigate = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...actual, useNavigate: () => navigate };
});

let config: AppConfig | null = null;
const loadConfig = vi.fn(async () => config as AppConfig);
vi.mock("@/lib/config", () => ({
  loadConfig: () => loadConfig(),
  useConfig: () => ({ config, configLoaded: config !== null }),
}));

const AUTH_ON: AppConfig = {
  auth_enabled: true,
  mcp_enabled: true,
  cluster_enabled: false,
  cluster_experimental: true,
  benchmarking_enabled: false,
  simulation_mode: true,
  runtime: "native",
};
const AUTH_OFF: AppConfig = { ...AUTH_ON, auth_enabled: false };

const fetchMock = () => globalThis.fetch as unknown as ReturnType<typeof vi.fn>;

/** Renders every field of the context an operator's session depends on. */
function Probe() {
  const { isAuthenticated, loading, user, token, isConfigLoaded, login, logout } = useAuth();
  return (
    <div>
      <span data-testid="authenticated">{String(isAuthenticated)}</span>
      <span data-testid="loading">{String(loading)}</span>
      <span data-testid="token">{token ?? "—"}</span>
      <span data-testid="user">{user?.name ?? "—"}</span>
      <span data-testid="config-loaded">{String(isConfigLoaded)}</span>
      <button onClick={login}>Sign in</button>
      <button onClick={() => void logout()}>Sign out</button>
    </div>
  );
}

function renderAuth() {
  return render(
    <MemoryRouter>
      <AuthProvider>
        <Probe />
      </AuthProvider>
    </MemoryRouter>,
  );
}

describe("AuthProvider", () => {
  beforeEach(() => {
    navigate.mockClear();
    loadConfig.mockClear();
    vi.stubGlobal("fetch", vi.fn());
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  // The single-user install: auth off, so there is nobody to sign in as and
  // every page has to be reachable anyway.
  it("treats everyone as signed in when the deployment has auth turned off", async () => {
    config = AUTH_OFF;
    renderAuth();

    await waitFor(() => expect(screen.getByTestId("authenticated")).toHaveTextContent("true"));
    expect(screen.getByTestId("token")).toHaveTextContent("disabled");
    expect(screen.getByTestId("loading")).toHaveTextContent("false");
    // Nothing is asked about a session that does not exist.
    expect(fetchMock()).not.toHaveBeenCalledWith("/auth/me", expect.anything());
  });

  it("reports the signed-in user when the session cookie is good", async () => {
    config = AUTH_ON;
    fetchMock().mockResolvedValue({ ok: true, json: async () => ({ user: { name: "Ada" } }) });

    renderAuth();

    await waitFor(() => expect(screen.getByTestId("user")).toHaveTextContent("Ada"));
    expect(screen.getByTestId("authenticated")).toHaveTextContent("true");
    expect(fetchMock()).toHaveBeenCalledWith("/auth/me", { credentials: "include" });
  });

  it("is not signed in when the session has expired", async () => {
    config = AUTH_ON;
    fetchMock().mockResolvedValue({ ok: false, status: 401, json: async () => ({}) });

    renderAuth();

    await waitFor(() => expect(screen.getByTestId("loading")).toHaveTextContent("false"));
    expect(screen.getByTestId("authenticated")).toHaveTextContent("false");
    expect(screen.getByTestId("token")).toHaveTextContent("—");
  });

  // A backend that is down is not a signed-in session: failing open here
  // would render the whole app to someone with no session at all.
  it("is not signed in when /auth/me cannot be reached", async () => {
    config = AUTH_ON;
    fetchMock().mockRejectedValue(new Error("network down"));

    renderAuth();

    await waitFor(() => expect(screen.getByTestId("loading")).toHaveTextContent("false"));
    expect(screen.getByTestId("authenticated")).toHaveTextContent("false");
  });

  it("survives a /auth/me answer with no user object in it", async () => {
    config = AUTH_ON;
    fetchMock().mockResolvedValue({ ok: true, json: async () => ({}) });

    renderAuth();

    await waitFor(() => expect(screen.getByTestId("authenticated")).toHaveTextContent("true"));
    expect(screen.getByTestId("user")).toHaveTextContent("—");
  });

  it("sends the browser to the provider when asked to sign in", async () => {
    config = AUTH_OFF;
    const original = window.location;
    Object.defineProperty(window, "location", {
      configurable: true,
      writable: true,
      value: { href: "" } as unknown as Location,
    });
    try {
      renderAuth();
      await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
      expect(window.location.href).toBe("/auth/login");
    } finally {
      Object.defineProperty(window, "location", {
        configurable: true,
        writable: true,
        value: original,
      });
    }
  });

  it("drops the session and lands on /login when asked to sign out", async () => {
    config = AUTH_ON;
    fetchMock().mockResolvedValue({ ok: true, json: async () => ({ user: { name: "Ada" } }) });
    renderAuth();
    await waitFor(() => expect(screen.getByTestId("user")).toHaveTextContent("Ada"));

    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));

    expect(fetchMock()).toHaveBeenCalledWith("/auth/logout", {
      method: "POST",
      credentials: "include",
    });
    await waitFor(() => expect(screen.getByTestId("authenticated")).toHaveTextContent("false"));
    expect(navigate).toHaveBeenCalledWith("/login", { replace: true });
  });

  // Signing out is how an operator gets away from a broken session, so a
  // logout request that fails must not leave them signed in.
  it("signs out locally even when the logout request fails", async () => {
    config = AUTH_ON;
    fetchMock().mockResolvedValueOnce({ ok: true, json: async () => ({ user: { name: "Ada" } }) });
    renderAuth();
    await waitFor(() => expect(screen.getByTestId("user")).toHaveTextContent("Ada"));

    fetchMock().mockRejectedValueOnce(new Error("network down"));
    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));

    await waitFor(() => expect(screen.getByTestId("authenticated")).toHaveTextContent("false"));
    expect(navigate).toHaveBeenCalledWith("/login", { replace: true });
  });

  it("waits for config before deciding anything", async () => {
    config = null;
    renderAuth();

    await waitFor(() => expect(screen.getByTestId("config-loaded")).toHaveTextContent("true"));
    // Config loaded, but no config value yet: still deciding, so not signed in.
    expect(screen.getByTestId("loading")).toHaveTextContent("true");
    expect(screen.getByTestId("authenticated")).toHaveTextContent("false");
  });
});

describe("useAuth", () => {
  it("refuses to be used outside the provider rather than returning nothing", () => {
    // React logs the thrown render error; the assertion is the throw itself.
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      expect(() => render(<Probe />)).toThrow(/must be used within AuthProvider/);
    } finally {
      errors.mockRestore();
    }
  });
});
