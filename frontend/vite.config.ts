import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

const rootDir = path.dirname(fileURLToPath(import.meta.url));
// Docker Desktop bind mounts (Windows/macOS) don't forward file-change
// events into the container, so the dev server would keep serving stale
// modules. Poll only when running inside a container.
const inDocker = fs.existsSync("/.dockerenv");

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, rootDir, "");
  const apiTarget =
    env.API_PROXY_TARGET || process.env.API_PROXY_TARGET || "http://localhost:8000";

  return {
    plugins: [react()],
    resolve: {
      alias: {
        "@": path.resolve(rootDir, "./src"),
      },
    },
    server: {
      host: "0.0.0.0",
      port: 3000,
      watch: inDocker ? { usePolling: true, interval: 300 } : undefined,
      proxy: {
        "/api": {
          target: apiTarget,
          changeOrigin: true,
          ws: true,
        },
        "/health": {
          target: apiTarget,
          changeOrigin: true,
        },
      },
    },
  };
});
