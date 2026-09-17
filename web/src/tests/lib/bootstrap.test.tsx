/** The entry point has to start the theme.
 *
 * `initTheme()` is what subscribes to the OS colour-scheme preference, so
 * "System" only keeps following the OS if something calls it. Nothing did:
 * the inline script in index.html painted the class once at load and that was
 * the end of it, which made "System" a synonym for "whatever the OS said when
 * this tab opened". This test is the ratchet on the call.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";

const render = vi.fn();
const createRoot = vi.fn(() => ({ render, unmount: vi.fn() }));
const initTheme = vi.fn();

vi.mock("react-dom/client", () => ({ default: { createRoot }, createRoot }));
vi.mock("@/lib/theme", () => ({ initTheme }));
vi.mock("@/App", () => ({ default: () => null }));

describe("main", () => {
  beforeEach(() => {
    vi.resetModules();
    initTheme.mockClear();
    createRoot.mockClear();
    document.body.innerHTML = '<div id="root"></div>';
  });

  it("initialises the theme before mounting", async () => {
    await import("@/main");

    expect(initTheme).toHaveBeenCalledTimes(1);
    expect(createRoot).toHaveBeenCalledTimes(1);
    expect(initTheme.mock.invocationCallOrder[0]).toBeLessThan(
      createRoot.mock.invocationCallOrder[0],
    );
  });
});
