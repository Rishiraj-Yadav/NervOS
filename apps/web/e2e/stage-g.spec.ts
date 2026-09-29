import { expect, test } from "@playwright/test";

test("installs a package and manages package agent instance lifecycle", async ({ page }) => {
  test.setTimeout(90_000);
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.hostname !== "127.0.0.1" && url.hostname !== "localhost") {
      throw new Error(`E2E blocked unexpected external request to ${url.origin}`);
    }
    await route.continue();
  });

  await page.goto("/");
  await page.getByLabel(/^username$/i).fill("Stage-A.Admin");
  await page.getByLabel(/^password$/i).fill("StageA test password 2026!");
  await page.getByRole("button", { name: /sign in/i }).click();
  await expect(page.getByRole("heading", { name: /nervos is ready/i })).toBeVisible();

  // Navigate to packages
  await page.goto("/packages");
  await expect(page.getByRole("heading", { name: "Packages", exact: true })).toBeVisible();
  await page.getByRole("link", { name: "Install Package", exact: true }).click();

  await expect(page.getByRole("heading", { name: "Install Agent Package", exact: true })).toBeVisible();
  await expect(page.getByText(/Pre-Stage-H/i)).toHaveCount(0); // Before file selection
});
