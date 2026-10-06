import { existsSync } from "node:fs";
import { expect, test } from "@playwright/test";

test("installs a package and manages package agent instance lifecycle", async ({ page }) => {
  test.setTimeout(180_000);
  const packageFile = process.env.NERVOS_E2E_PACKAGE_FILE;
  if (!packageFile || !existsSync(packageFile)) {
    throw new Error("NERVOS_E2E_PACKAGE_FILE must point to the supervised signed package fixture");
  }
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

  await page.goto("/packages");
  await expect(page.getByRole("heading", { name: "Packages", exact: true })).toBeVisible();
  await page.getByRole("link", { name: "Install Package", exact: true }).click();

  await expect(page.getByRole("heading", { name: "Install Agent Package", exact: true })).toBeVisible();
  await expect(page.getByText(/Pre-Stage-H/i)).toHaveCount(0);
  await page.getByLabel(/select .+ package file/i).setInputFiles(packageFile);
  await expect(page.getByText("com.acme.browserdemo@1.0.0", { exact: true })).toBeVisible();
  await expect(page.getByText("Signature Verified (Ed25519)")).toBeVisible();
  await expect(page.getByText(/^[0-9a-f]{64}$/i).first()).toBeVisible();
  await page.getByRole("button", { name: "Authorize & Install com.acme.browserdemo@1.0.0" }).click();
  if (process.platform !== "linux") {
    await expect(page.getByRole("alert")).toContainText("containment is unavailable", {timeout: 90000});
    await expect(page).toHaveURL(/\/packages\/install$/);
    return;
  }
  await expect(page).toHaveURL(/\/packages\/com\.acme\.browserdemo\/1\.0\.0$/, {
    timeout: 90_000,
  });
  await expect(page.getByRole("heading", { name: "Browser Runtime Demo" })).toBeVisible();
  await expect(page.getByText("active", { exact: true })).toBeVisible({ timeout: 90_000 });

  await page.goto("/agents");
  await page.getByRole("button", { name: /create (another )?chat agent/i }).click();
  await page.getByLabel("Display name").fill("G5 Browser Runtime Agent");
  await page.getByLabel("Agent definition").selectOption("com.acme.browserdemo@1.0.0");
  await page.getByLabel("Model provider").selectOption("anthropic");
  await page.getByLabel("Model", { exact: true }).fill("offline-demo");
  await page.getByRole("button", { name: "Create agent", exact: true }).click();
  await expect(page.getByRole("heading", { name: "G5 Browser Runtime Agent" })).toBeVisible();
  await page.getByLabel("Message").fill("run the installed package");
  await page.getByRole("button", { name: "Run agent" }).click();
  await expect(page.getByText("stage-g-browser-runtime-ok", { exact: true })).toBeVisible({
    timeout: 90_000,
  });
  await expect(page.getByText("Succeeded", { exact: true })).toBeVisible();
});
