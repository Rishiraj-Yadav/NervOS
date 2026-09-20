import { expect, test } from "@playwright/test";

test("manages a webhook automation and records its Run", async ({ page }) => {
  test.setTimeout(90_000);
  await page.route("**/*", async (route) => {
    const url = new URL(route.request().url());
    if (url.hostname !== "127.0.0.1" && url.hostname !== "localhost") {
      throw new Error(`E2E blocked unexpected external request to ${url.origin}`);
    }
    await route.continue();
  });

  await page.goto("/");
  await page.context().grantPermissions(["clipboard-read", "clipboard-write"], { origin: new URL(page.url()).origin });
  await page.getByLabel(/^username$/i).fill("Stage-A.Admin");
  await page.getByLabel(/^password$/i).fill("StageA test password 2026!");
  await page.getByRole("button", { name: /sign in/i }).click();
  await expect(page.getByRole("heading", { name: /nervos is ready/i })).toBeVisible();

  const agent = await page.evaluate(async () => {
    const response = await fetch("/api/v1/agent-instances", {
      method: "POST",
      headers: { "Content-Type": "application/json", Origin: window.location.origin },
      body: JSON.stringify({
        agent_key: "nervos.chat",
        agent_definition_version: "1",
        display_name: "E4 Webhook Agent",
        model_provider: "anthropic",
        model_name: "opaque/e4-model",
      }),
    });
    if (!response.ok) throw new Error(`agent creation failed: ${response.status}`);
    return (await response.json()) as { id: number };
  });

  await page.goto("/automations");
  await page.getByRole("link", { name: /create automation/i }).click();
  await page.getByLabel("Kind").selectOption("webhook");
  await page.getByLabel("Agent instance ID").fill(String(agent.id));
  await page.getByLabel("Name").fill("E4 inbound webhook");
  await page.getByLabel("Input text").fill("Process this webhook");
  await page.getByRole("button", { name: /^create$/i }).click();

  await expect(page.getByText(/copy this secret now/i)).toBeVisible();
  const secret = await page.locator(".secret-banner code").innerText();
  expect(secret).not.toBe("");
  await expect(page.getByText(/\/hooks\/v1\//)).toBeVisible();
  await page.getByRole("button", { name: /copy secret/i }).click();
  await expect(page.getByRole("button", { name: /copied/i })).toBeVisible();
  await page.getByRole("button", { name: /dismiss/i }).click();
  await expect(page.getByText(secret, { exact: true })).toHaveCount(0);

  const hookPath = await page.locator("p").filter({ hasText: "Webhook path:" }).locator("code").innerText();
  await page.getByRole("link", { name: /open automation/i }).click();
  await expect(page.getByRole("heading", { name: "E4 inbound webhook" })).toBeVisible();
  const delivery = await page.evaluate(async ({ path, token }) => {
    const response = await fetch(`${window.location.origin}${path}`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
      body: JSON.stringify({ source: "stage-e", value: 42 }),
    });
    return { status: response.status, body: await response.text() };
  }, { path: hookPath, token: secret });
  expect(delivery.status).toBe(202);

  await page.reload();
  await expect(page.getByRole("heading", { name: "E4 inbound webhook" })).toBeVisible();
  await expect(page.getByText(secret, { exact: true })).toHaveCount(0);
  await expect(page.getByText(/run \d+/i)).toBeVisible({ timeout: 30_000 });
  await expect(page.locator("a").filter({ hasText: /run \d+/i })).toHaveAttribute("href", `/agents/${agent.id}`);
});
