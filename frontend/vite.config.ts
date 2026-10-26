import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// In `npm run dev`, proxy the control-plane API so the browser stays same-origin
// (mirrors the nginx reverse proxy used by the production image).
const apiTarget = process.env.VITE_API_PROXY_TARGET ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": apiTarget,
      "/auth": apiTarget,
      "/health": apiTarget,
    },
  },
  test: {
    environment: "node",
    include: ["src/**/*.test.ts"],
  },
});
