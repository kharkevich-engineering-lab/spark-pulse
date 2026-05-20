import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { resolve } from "path";

export default defineConfig({
  base: "./",
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 3000,
    proxy: {
      "/api": "http://localhost:8100",
      "/sse": "http://localhost:8100",
      "/health": "http://localhost:8100",
    },
  },
  build: {
    outDir: "../spark_pulse/ui",
    emptyOutDir: true,
  },
});
