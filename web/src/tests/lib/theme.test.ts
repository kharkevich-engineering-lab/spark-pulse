/** Theme persistence and the class it puts on <html>.
 *
 * Tailwind keys every colour off `dark`/`light` on the root element, so
 * "which class is on <html>" *is* the theme. The three modes are two colour
 * schemes plus "follow the OS", and the third one is the one with behaviour:
 * it has to keep following after the OS changes its mind.
 */

import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { getTheme, initTheme, resolvedClass, setTheme } from "@/lib/theme";

const STORAGE_KEY = "spark-pulse-theme";

/** Stand in for the OS preference, and hand back the listeners registered. */
function stubSystemTheme(prefersDark: boolean) {
  const listeners: Array<() => void> = [];
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({
      matches: prefersDark,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: (_type: string, listener: () => void) => listeners.push(listener),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  );
  return listeners;
}

describe("theme", () => {
  beforeEach(() => {
    document.documentElement.className = "";
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    document.documentElement.className = "";
  });

  it("follows the system until an operator chooses", () => {
    expect(getTheme()).toBe("system");
  });

  it("remembers an explicit choice", () => {
    stubSystemTheme(false);
    setTheme("dark");

    expect(getTheme()).toBe("dark");
    expect(window.localStorage.getItem(STORAGE_KEY)).toBe("dark");
  });

  it("puts exactly one of dark/light on the root element", () => {
    stubSystemTheme(false);

    setTheme("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(document.documentElement.classList.contains("light")).toBe(false);

    setTheme("light");
    expect(document.documentElement.classList.contains("light")).toBe(true);
    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });

  it("resolves 'system' against the OS preference", () => {
    stubSystemTheme(true);
    setTheme("system");
    expect(document.documentElement.classList.contains("dark")).toBe(true);

    stubSystemTheme(false);
    setTheme("system");
    expect(document.documentElement.classList.contains("light")).toBe(true);
  });

  it("applies the stored theme on startup", () => {
    stubSystemTheme(false);
    window.localStorage.setItem(STORAGE_KEY, "dark");

    initTheme();

    expect(document.documentElement.classList.contains("dark")).toBe(true);
  });

  // "System" is a standing subscription, not a one-time read: an OS that
  // switches to dark at sunset has to take the page with it.
  it("keeps following the OS after startup while in system mode", () => {
    const listeners = stubSystemTheme(false);
    initTheme();
    expect(document.documentElement.classList.contains("light")).toBe(true);

    stubSystemTheme(true);
    for (const listener of listeners) listener();

    expect(document.documentElement.classList.contains("dark")).toBe(true);
  });

  it("stops following the OS once a theme has been chosen", () => {
    const listeners = stubSystemTheme(false);
    initTheme();
    window.localStorage.setItem(STORAGE_KEY, "light");

    stubSystemTheme(true);
    for (const listener of listeners) listener();

    expect(document.documentElement.classList.contains("light")).toBe(true);
  });

  describe("resolvedClass", () => {
    it("picks the dark classes when the theme is dark", () => {
      stubSystemTheme(false);
      setTheme("dark");
      expect(resolvedClass("bg-zinc-900", "bg-white")).toBe("bg-zinc-900");
    });

    it("picks the light classes when the theme is light", () => {
      stubSystemTheme(true);
      setTheme("light");
      expect(resolvedClass("bg-zinc-900", "bg-white")).toBe("bg-white");
    });

    it("defers to the OS in system mode", () => {
      stubSystemTheme(true);
      setTheme("system");
      expect(resolvedClass("bg-zinc-900", "bg-white")).toBe("bg-zinc-900");

      stubSystemTheme(false);
      expect(resolvedClass("bg-zinc-900", "bg-white")).toBe("bg-white");
    });
  });
});
