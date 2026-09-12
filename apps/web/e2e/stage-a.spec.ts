import { expect, test } from "@playwright/test";

const username = "Stage-A.Admin";
const canonicalUsername = "stage-a.admin";
const password = "StageA test password 2026!";

function storageContainsAuthenticationState(entries: [string, string][]): boolean {
  return entries.some(([key]) => /auth|session|user|token|login/i.test(key));
}

test("completes the Stage A setup and authentication journey", async ({ page }) => {
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.hostname !== "127.0.0.1" && url.hostname !== "localhost") {
      throw new Error(`E2E blocked unexpected external request to ${url.origin}`);
    }
    await route.continue();
  });

  await page.goto("/");
  await expect(page.getByRole("heading", { name: /create your administrator/i })).toBeVisible();

  await page.getByLabel(/^username$/i).fill(username);
  await page.getByLabel(/^password$/i).fill(password);
  await page.getByLabel(/confirm password/i).fill(password);
  await page.getByRole("button", { name: /create administrator/i }).click();

  await expect(page.getByRole("heading", { name: /nervos is ready/i })).toBeVisible();
  await expect(page.getByText(canonicalUsername, { exact: true })).toBeVisible();

  await page.getByRole("button", { name: /log out/i }).click();
  await expect(page.getByRole("heading", { name: /welcome back/i })).toBeVisible();
  await expect(page.getByRole("heading", { name: /nervos is ready/i })).toHaveCount(0);

  await page.getByLabel(/^username$/i).fill(username);
  await page.getByLabel(/^password$/i).fill(password);
  await page.getByRole("button", { name: /sign in/i }).click();
  await expect(page.getByRole("heading", { name: /nervos is ready/i })).toBeVisible();
  await expect(page.getByText(canonicalUsername, { exact: true })).toBeVisible();

  await page.reload();
  await expect(page.getByRole("heading", { name: /nervos is ready/i })).toBeVisible();
  await expect(page.getByText(canonicalUsername, { exact: true })).toBeVisible();

  const storageEntries = await page.evaluate(() => ({
    local: Object.entries(localStorage),
    session: Object.entries(sessionStorage),
  }));
  expect(storageContainsAuthenticationState(storageEntries.local)).toBe(false);
  expect(storageContainsAuthenticationState(storageEntries.session)).toBe(false);

  await page.getByRole("button", { name: /log out/i }).click();
  await expect(page.getByRole("heading", { name: /welcome back/i })).toBeVisible();

  await page.goto("/dashboard");
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("heading", { name: /welcome back/i })).toBeVisible();
  await expect(page.getByRole("heading", { name: /nervos is ready/i })).toHaveCount(0);
});
