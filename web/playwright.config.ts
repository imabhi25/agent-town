import { defineConfig } from "@playwright/test";

// The built app is served by `vite preview`; its API URL points at the same origin, so the tests
// can intercept both REST calls and the WebSocket (no CORS preflights, no real backend).
export default defineConfig({
  testDir: "e2e",
  timeout: 30_000,
  retries: process.env.CI ? 1 : 0,
  // One worker on CI: the map renders with software WebGL on a 2-core runner, so parallel tests
  // starve each other (the frame-rate test and the clock-driven replay test are CPU-sensitive).
  workers: process.env.CI ? 1 : undefined,
  // reducedMotion keeps the existing tests on the 2D map (the default view is 3D when WebGL2 works
  // and motion is not reduced); 3D tests force it with ?view=3d.
  use: { baseURL: "http://localhost:4173", trace: "retain-on-failure", contextOptions: { reducedMotion: "reduce" } },
  webServer: {
    command: "npm run build && npx vite preview --port 4173 --strictPort",
    url: "http://localhost:4173",
    reuseExistingServer: !process.env.CI,
    env: { VITE_API_URL: "http://localhost:4173" },
    timeout: 120_000,
  },
});
