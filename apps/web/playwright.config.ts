import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.NERVOS_E2E_WEB_ORIGIN;
if (!baseURL) {
  throw new Error("NERVOS_E2E_WEB_ORIGIN is required. Run E2E through scripts/e2e.py.");
}

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 45_000,
  expect: { timeout: 7_500 },
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    baseURL,
    actionTimeout: 7_500,
    navigationTimeout: 15_000,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"], browserName: "chromium" },
    },
  ],
});
