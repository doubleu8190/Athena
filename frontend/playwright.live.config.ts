import { defineConfig } from "@playwright/test";

import { readSkillsLiveContract } from "./e2e/skills.live.contract";

const contract = readSkillsLiveContract();
const loginURL = new URL("/login", contract.frontend_base_url).toString();

export default defineConfig({
  expect: {
    timeout: 20_000,
  },
  fullyParallel: false,
  outputDir:
    process.env.PLAYWRIGHT_OUTPUT_DIR ??
    "../var/skills-e2e/scripted-final",
  reporter: [["list"], ["html", { open: "never" }]],
  retries: 0,
  testDir: "./e2e",
  testMatch: /skills\.live\.spec\.ts/,
  timeout: 240_000,
  use: {
    actionTimeout: 20_000,
    baseURL: contract.frontend_base_url,
    navigationTimeout: 45_000,
    screenshot: "only-on-failure",
    trace: "on",
    video: "off",
  },
  webServer: {
    command: "npm run dev -- --config e2e/skills.live.vite.config.ts",
    reuseExistingServer: false,
    stderr: "pipe",
    stdout: "pipe",
    timeout: 60_000,
    url: loginURL,
  },
  workers: 1,
});
