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
      // Floored at what the suite actually reaches after removing the unused
      // operation-subsystem code (96.64 lines / 94.27 statements / 92.04
      // functions / 86.99 branches) that used to inflate this denominator
      // with dead code kept "covered" only by its own tests. This is a
      // ratchet floor at the honest post-cleanup baseline, not an
      // aspiration: raise it when the suite earns it, never lower it to
      // make a red build green.
      thresholds: {
        lines: 96,
        statements: 94,
        functions: 92,
        branches: 86,
      },
    },
  },
});
