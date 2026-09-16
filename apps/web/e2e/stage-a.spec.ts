import { writeFileSync } from "node:fs";

import { expect, test } from "@playwright/test";

const username = "Stage-A.Admin";
const canonicalUsername = "stage-a.admin";
const password = "StageA test password 2026!";
// The Worker is held at the claim gate until the journey has observed the queued Run, so the
// asynchronous cutover is proved rather than raced. Releasing the gate hands the Job to Worker A,
// which claims it and is then lost BEFORE the execution-start boundary; the supervisor starts a
// recovery Worker, whose startup reclamation reconciles that expired claim and executes it once.
// The terminal result therefore travels through a claim, a pre-start loss, a lease expiry, a
// reclamation, a fresh claim, a durable terminal write, and a browser poll, so it gets a bound
// wider than the 7.5s default; the whole-test budget is untouched.
const ASYNC_TIMEOUT = 25_000;
function releaseWorkerClaimGate(): void {
  const gate = process.env.NERVOS_E2E_CLAIM_GATE;
  if (gate === undefined || gate === "") {
    throw new Error(
      "NERVOS_E2E_CLAIM_GATE is required; run the journey through scripts/check.py e2e",
    );
  }
  writeFileSync(gate, "released\n", "utf-8");
}

function storageContainsAuthenticationState(entries: [string, string][]): boolean {
  return entries.some(([key]) => /auth|session|user|token|login/i.test(key));
}

// C4: the exact prompt whose first provider call the supervisor scripts as a normalized rate
// limit. Sending it through the real form proves the retry path end to end, and using one
// distinctive prompt keeps the script from affecting any other Run in the journey.
function retryPrompt(): string {
  const prompt = process.env.NERVOS_E2E_RETRY_INPUT;
  if (prompt === undefined || prompt === "") {
    throw new Error(
      "NERVOS_E2E_RETRY_INPUT is required; run the journey through scripts/check.py e2e",
    );
  }
  return prompt;
}

function cancelPrompt(): string {
  const prompt = process.env.NERVOS_E2E_CANCEL_INPUT;
  if (prompt === undefined || prompt === "") {
    throw new Error(
      "NERVOS_E2E_CANCEL_INPUT is required; run the journey through scripts/check.py e2e",
    );
  }
  return prompt;
}

test("completes the Stage A setup and authentication journey", async ({ page }) => {
  // This single authenticated session now drives C2 submission, C3 crash recovery, C4 durable
  // retry, and C5 owner cancellation in sequence, so it legitimately outgrew the repository's
  // default 45s budget. The extra time is real durable work -- a retry that waits for its due
  // instant and a cancellation that a Worker discovers on its next heartbeat -- not slack, and
  // shortening the production heartbeat to fit the old budget would weaken the lease
  // relationship the C3 recovery proof depends on.
  test.setTimeout(120_000);
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

  // --- Stage B: deterministic two-provider portability journey ---
  // The supervisor removes both provider credentials and installs two offline fakes.
  await page.getByLabel(/^username$/i).fill(username);
  await page.getByLabel(/^password$/i).fill(password);
  await page.getByRole("button", { name: /sign in/i }).click();
  await expect(page.getByRole("heading", { name: /nervos is ready/i })).toBeVisible();

  await page.goto("/agents");
  await expect(page.getByRole("heading", { name: /no agents yet/i })).toBeVisible();

  await page.getByRole("button", { name: /create a chat agent/i }).click();
  await page.getByLabel("Display name").fill("E2E Chat");
  await page.getByLabel("Model", { exact: true }).fill("opaque/e2e-model");
  await page.getByRole("button", { name: /^create agent$/i }).click();

  await expect(page.getByRole("heading", { name: "E2E Chat" })).toBeVisible();
  await expect(page.getByText(/nervos\.chat v1/)).toBeVisible();

  await page.getByLabel("Message").fill("first question from the browser");
  await page.getByRole("button", { name: /run agent/i }).click();

  // C2 proof: the accepted Run is observably queued before any Worker may claim it. A
  // synchronous POST would return a terminal Run, so `.run-status-created` would never appear
  // and this assertion would fail.
  await expect(page.locator(".run-status-created")).toHaveText("Queued");
  await expect(
    page.getByText(/Accepted and queued\. A worker must be running to execute this run\./),
  ).toBeVisible();

  // Only now may the Worker claim, execute, terminalize, and be observed by the browser poll.
  releaseWorkerClaimGate();

  await expect(page.getByText(/Deterministic Anthropic reply from NervOS\./)).toBeVisible({
    timeout: ASYNC_TIMEOUT,
  });

  // The result survives a reload because it was persisted, not held in browser state.
  await page.reload();
  await expect(page.getByText(/Deterministic Anthropic reply from NervOS\./)).toBeVisible({
    timeout: ASYNC_TIMEOUT,
  });
  await expect(page.getByText("first question from the browser")).toBeVisible();

  // Switch the same instance explicitly; the model stays operator-controlled.
  await page.getByLabel("Model provider").selectOption("openai");
  await page.getByLabel("Model", { exact: true }).fill("opaque/openai-e2e-model");
  await page.getByRole("button", { name: /save configuration/i }).click();
  await page.getByLabel("Message").fill("second question from the browser");
  await page.getByRole("button", { name: /run agent/i }).click();
  await expect(page.getByText(/Deterministic OpenAI reply from NervOS\./)).toBeVisible({
    timeout: ASYNC_TIMEOUT,
  });
  await page.reload();
  await expect(page.getByText(/Deterministic Anthropic reply from NervOS\./)).toBeVisible({
    timeout: ASYNC_TIMEOUT,
  });
  await expect(page.getByText(/Deterministic OpenAI reply from NervOS\./)).toBeVisible({
    timeout: ASYNC_TIMEOUT,
  });
  await expect(page.getByText("anthropic · opaque/e2e-model")).toBeVisible();
  await expect(page.getByText("openai · opaque/openai-e2e-model")).toBeVisible();
  // C4 proof: a positively safe provider failure is retried durably instead of terminalized.
  // The supervisor scripts this one prompt's first Anthropic call to be refused with a
  // normalized rate limit, so this Run can only reach a final answer if the Worker committed a
  // retry, waited for its due instant, and a later Attempt executed it. The Run stays `running`
  // across that wait, and the copy must therefore stop promising that no retry ever happens.
  await page.getByLabel("Model provider").selectOption("anthropic");
  await page.getByLabel("Model", { exact: true }).fill("opaque/e2e-model");
  await page.getByRole("button", { name: /save configuration/i }).click();
  await page.getByLabel("Message").fill(retryPrompt());
  await page.getByRole("button", { name: /run agent/i }).click();
  await expect(page.getByText(/may be retried/i)).toBeVisible({ timeout: ASYNC_TIMEOUT });
  await expect(page.getByText(/Deterministic Anthropic reply from NervOS\./)).toHaveCount(2, {
    timeout: ASYNC_TIMEOUT,
  });
  await page.reload();
  await expect(page.getByText(/Deterministic Anthropic reply from NervOS\./)).toHaveCount(2, {
    timeout: ASYNC_TIMEOUT,
  });

  // C5 proof: an owner can cancel a Run that is genuinely mid-provider-call. The supervisor
  // holds this one prompt's provider call open, so the browser cancels real in-flight work
  // rather than a Run that happened to be slow; the durable cancellation lands immediately,
  // while the Worker stops its local task on its next heartbeat. Nothing here depends on
  // timing luck, and no remote cancellation is claimed.
  await page.getByLabel("Message").fill(cancelPrompt());
  await page.getByRole("button", { name: /run agent/i }).click();
  // Scope every assertion to this Run's own card. Any other nonterminal Run on the page would
  // otherwise make the Cancel locator ambiguous, and a strict-mode violation fails the journey
  // for a reason that has nothing to do with cancellation.
  const cancelCard = page.locator("article.run-item").filter({ hasText: cancelPrompt() });
  const cancelButton = cancelCard.getByRole("button", { name: /cancel/i });
  await expect(cancelButton).toBeVisible({ timeout: ASYNC_TIMEOUT });
  await expect(cancelCard.getByText(/^Running$/)).toBeVisible({ timeout: ASYNC_TIMEOUT });
  await cancelButton.click();
  await expect(cancelCard.getByText(/^Cancelled$/)).toBeVisible({ timeout: ASYNC_TIMEOUT });
  // Cancellation is terminal, so it survives a reload and no cancellation control remains.
  await page.reload();
  const reloadedCard = page.locator("article.run-item").filter({ hasText: cancelPrompt() });
  await expect(reloadedCard.getByText(/^Cancelled$/)).toBeVisible({ timeout: ASYNC_TIMEOUT });
  await expect(reloadedCard.getByRole("button", { name: /cancel/i })).toHaveCount(0);

  // Disabling the agent blocks new Runs while leaving the existing history readable.
  await page.getByRole("button", { name: /disable agent/i }).click();
  await expect(page.getByText(/cannot start new runs/i)).toBeVisible();
  await expect(page.getByRole("button", { name: /run agent/i })).toBeDisabled();
  await expect(page.getByText(/Deterministic OpenAI reply from NervOS\./)).toBeVisible();

  await page.goto("/dashboard");
  await page.getByRole("button", { name: /log out/i }).click();
  await expect(page.getByRole("heading", { name: /welcome back/i })).toBeVisible();
});
