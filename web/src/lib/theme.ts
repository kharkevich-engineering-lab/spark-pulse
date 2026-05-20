/** Theme state manager — persists to localStorage. */

export type ThemeMode = "dark" | "light" | "system";

const STORAGE_KEY = "spark-pulse-theme";

function getSystemTheme(): "dark" | "light" {
  if (typeof window === "undefined") return "dark";
  return window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

export function getTheme(): ThemeMode {
  if (typeof window === "undefined") return "dark";
  return (localStorage.getItem(STORAGE_KEY) as ThemeMode) || "system";
}

export function setTheme(mode: ThemeMode): void {
  localStorage.setItem(STORAGE_KEY, mode);
  applyTheme(mode);
}

function applyTheme(mode: ThemeMode): void {
  const target = mode === "system" ? getSystemTheme() : mode;
  if (target === "dark") {
    document.documentElement.classList.add("dark");
    document.documentElement.classList.remove("light");
  } else {
    document.documentElement.classList.add("light");
    document.documentElement.classList.remove("dark");
  }
}

export function initTheme(): void {
  const mode = getTheme();
  applyTheme(mode);

  // Listen for system theme changes when in "system" mode
  if (typeof window !== "undefined") {
    window
      .matchMedia("(prefers-color-scheme: dark)")
      .addEventListener("change", () => {
        if (getTheme() === "system") {
          applyTheme("system");
        }
      });
  }
}

export function resolvedClass(dark: string, light: string): string {
  /**
   * Resolve Tailwind-like conditional classes based on current theme.
   * Usage: `resolvedClass("bg-zinc-900 text-zinc-100", "bg-white text-zinc-900")`
   * Returns the appropriate class based on current theme.
   */
  const mode = getTheme();
  const isDark = mode === "system" ? getSystemTheme() === "dark" : mode === "dark";
  return isDark ? dark : light;
}
