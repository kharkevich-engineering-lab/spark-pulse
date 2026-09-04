import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { resolve } from "path";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": resolve(__dirname, "./src"),
    },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: "./src/tests/setupTests.ts",
    // The Playwright suite matches vitest's default spec glob but is not a
    // vitest suite: it needs a browser and a running backend.
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    coverage: {
      provider: "v8",
      reporter: ["text", "json", "html"],
      // Every source file counts, whether or not a test happens to import it.
      // Without this, vitest measures only the files the suite loaded — so
      // deleting a page's test *raises* the percentage, and a new untested
      // module never appears at all. The thresholds below are only meaningful
      // against a denominator that cannot shrink.
      include: ["src/**/*.{ts,tsx}"],
      exclude: [
        "node_modules/",
        "src/tests/",
        "**/*.d.ts",
        "src/main.tsx",
        "src/vite-env.d.ts",
      ],
      // Set from what the suite actually reaches (97.2 lines / 94.8 statements
      // / 92.7 functions / 86.6 branches), minus a point or two of headroom.
      // They are a ratchet, not an aspiration: raise them when the suite
      // earns it, and never lower one to make a red build green.
      thresholds: {
        lines: 95,
        statements: 93,
        functions: 90,
        branches: 85,
      },
    },
  },
});
