/** The login page, which exists only to hand the browser to the OIDC provider.
 *
 * Two properties matter. The button has to actually start the provider
 * handshake — there is no local password to fall back on, so a dead button is
 * a locked-out installation. And an already-authenticated visitor must be sent
 * on rather than shown a sign-in screen, because the SPA redirects here on any
 * 401 and a stale tab would otherwise strand the operator on a page that looks
 * like their session expired when it did not.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import LoginPage from "@/pages/LoginPage";

const navigate = vi.fn();
const login = vi.fn();
let isAuthenticated = false;

vi.mock("@/lib/auth", () => ({
  useAuth: () => ({ isAuthenticated, login }),
}));

vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>("react-router-dom");
  return { ...actual, useNavigate: () => navigate };
});

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/login"]}>
      <LoginPage />
    </MemoryRouter>,
  );
}

describe("LoginPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    isAuthenticated = false;
  });

  it("offers exactly one way in, and it starts the provider handshake", async () => {
    const user = userEvent.setup();
    renderPage();

    expect(screen.getByText("Spark Pulse")).toBeInTheDocument();
    expect(screen.getByText("Sign in to continue")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Sign In" }));

    expect(login).toHaveBeenCalledTimes(1);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("sends an already-signed-in visitor on rather than asking again", async () => {
    isAuthenticated = true;
    renderPage();

    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/", { replace: true }));
  });

  it("carries the project links, which are the only other way off this page", () => {
    renderPage();

    expect(screen.getByRole("link", { name: /GitHub/ })).toHaveAttribute(
      "href",
      "https://github.com/kharkevich-engineering-lab/spark-pulse",
    );
    expect(screen.getByRole("link", { name: /PyPI/ })).toHaveAttribute(
      "href",
      "https://pypi.org/project/spark-pulse/",
    );
    expect(
      screen.getByRole("link", { name: new RegExp(String(new Date().getFullYear())) }),
    ).toBeInTheDocument();
  });
});
