import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  // Unit and component tests live in src/; the Playwright specs in e2e/ run with `npm run e2e`.
  test: { environment: "node", include: ["src/**/*.test.{ts,tsx}"] },
});
