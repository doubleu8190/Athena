import { defineConfig } from "@playwright/test";

export default defineConfig({
  expect: {
    timeout: 5_000,
  },
  fullyParallel: true,
  outputDir: "test-results/mock-admin",
  reporter: [["list"]],
  retries: 0,
  testDir: "./e2e",
  testIgnore: /skills\.live\.spec\.ts/,
  timeout: 30_000,
  use: {
    baseURL: "http://127.0.0.1:4173",
    trace: "retain-on-failure",
  },
  webServer: {
    command:
      "npm run dev -- --host 127.0.0.1 --port 4173 --strictPort",
    reuseExistingServer: !process.env.CI,
    stderr: "pipe",
    stdout: "pipe",
    timeout: 60_000,
    url: "http://127.0.0.1:4173/login",
  },
});
