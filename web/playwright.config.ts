import { defineConfig } from "@playwright/test";

// The built app is served by `vite preview`; its API URL points at the same origin, so the tests
// can intercept both REST calls and the WebSocket (no CORS preflights, no real backend).
export default defineConfig({
  testDir: "e2e",
  timeout: 30_000,
  retries: process.env.CI ? 1 : 0,
  use: { baseURL: "http://localhost:4173", trace: "retain-on-failure" },
  webServer: {
    command: "npm run build && npx vite preview --port 4173 --strictPort",
    url: "http://localhost:4173",
    reuseExistingServer: !process.env.CI,
    env: { VITE_API_URL: "http://localhost:4173" },
    timeout: 120_000,
  },
});
