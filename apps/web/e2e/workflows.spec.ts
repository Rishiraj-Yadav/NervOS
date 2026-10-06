import { test, expect } from "@playwright/test";

test("starts a workflow, observes fenced completion and follows its Run", async ({page}) => {
  await page.goto("/");
  await page.getByLabel(/^username$/i).fill("Stage-A.Admin");
  await page.getByLabel(/^password$/i).fill("StageA test password 2026!");
  await page.getByRole("button", { name: /sign in/i }).click();
  await expect(page.getByRole("heading", { name: /nervos is ready/i })).toBeVisible();
  const agent = await page.evaluate(async () => {
    const response = await fetch("/api/v1/agent-instances", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({
        agent_key: "nervos.chat", agent_definition_version: "1", display_name: "Workflow browser agent",
        model_provider: "anthropic", model_name: "opaque/workflow-model"})});
    if (!response.ok) throw new Error("Could not create fixture agent");
    return await response.json() as {id: number};
  });
  await page.goto("/workflows");
  await page.getByRole("button", {name: "New workflow"}).click();
  await page.getByLabel("Agent", {exact: true}).selectOption(String(agent.id));
  await page.getByLabel("Workflow kind").fill("research");
  await page.getByLabel("Request", {exact: true}).fill("workflow browser request");
  await page.getByLabel("Submission key").fill("browser-workflow");
  await page.getByRole("button", {name: "Start", exact: true}).click();
  const detail = page.getByRole("region", {name: "Workflow detail"});
  await expect(detail.getByRole("status").filter({hasText: "Succeeded"})).toBeVisible({timeout: 15000});
  await detail.getByRole("button", {name: /Inspect checkpoint/}).click();
  await expect(detail.getByLabel("Private checkpoint 1")).toContainText("{}");
  await detail.getByRole("button", {name: /^Run /}).click();
  await expect(page.getByRole("heading", {name: /^Run #/})).toBeVisible();
  await expect(page.getByText("Succeeded", {exact: true})).toBeVisible();
  await page.getByRole("button", {name: /Show timeline/i}).click();
  await expect(page.getByText("Run succeeded", {exact: true})).toBeVisible();
});
