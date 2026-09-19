import { existsSync, readFileSync, writeFileSync } from "node:fs";

import { expect, test, type Page } from "@playwright/test";

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

// Stage D: the tool-enabled Agent Instance cannot be created through the UI (no definition-version
// selector exists), and there is no capability-management UI at all, so the seed is split. The
// browser creates the instance through the same-origin API; the supervisor then inserts the one
// grant and acknowledges it here. Both are setup, never the behavior under proof -- the Run itself
// is submitted through the real form, and only what the timeline renders is asserted.
function toolPrompt(): string {
  const prompt = process.env.NERVOS_E2E_TOOL_INPUT;
  if (prompt === undefined || prompt === "") {
    throw new Error(
      "NERVOS_E2E_TOOL_INPUT is required; run the journey through scripts/check.py e2e",
    );
  }
  return prompt;
}

interface ToolAgentSeed {
  agentInstanceId: number;
  displayName: string;
  toolModelName: string;
}

async function seedToolEnabledAgent(page: Page): Promise<ToolAgentSeed> {
  const displayName = "E2E Tool Chat";
  const created = await page.evaluate(async (name) => {
    const response = await fetch("/api/v1/agent-instances", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        agent_key: "nervos.chat",
        agent_definition_version: "2",
        display_name: name,
        model_provider: "anthropic",
        model_name: "opaque/e2e-model",
      }),
    });
    const body: unknown = await response.json();
    return { status: response.status, body };
  }, displayName);
  if (created.status !== 201 || typeof created.body !== "object" || created.body === null) {
    throw new Error(`tool-enabled agent creation failed with status ${created.status}`);
  }
  const agentInstanceId = (created.body as { id?: unknown }).id;
  if (typeof agentInstanceId !== "number") {
    throw new Error("tool-enabled agent creation returned no numeric id");
  }
  const ackPath = process.env.NERVOS_E2E_TOOL_GRANT_ACK;
  if (ackPath === undefined || ackPath === "") {
    throw new Error(
      "NERVOS_E2E_TOOL_GRANT_ACK is required; run the journey through scripts/check.py e2e",
    );
  }
  // The supervisor commits the grant and only then writes the acknowledgment, so observing it
  // proves the Run submitted next will snapshot a cutoff that admits the grant.
  await expect.poll(() => existsSync(ackPath), { timeout: ASYNC_TIMEOUT }).toBe(true);
  const toolModelName = readFileSync(ackPath, "utf-8").trim();
  if (toolModelName === "") {
    throw new Error("the supervisor seeded the tool grant without reporting a tool name");
  }
  return { agentInstanceId, displayName, toolModelName };
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
  // The Stage-D tool scenario later visits a second Agent Instance; the original page is restored
  // afterwards so the remaining assertions still describe this instance.
  const chatAgentUrl = page.url();

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
  // C7 made this card phase-aware. Before C7 a Run waiting to retry and a Run executing now were
  // both an ordinary `running` Run, so the copy had to hedge across both cases in one paragraph.
  // The derived phase lets the card say which one it is, and the retry wait is this journey's six
  // seconds -- several polls deep at the 2s cadence.
  await expect(page.getByText(/waiting to retry/i)).toBeVisible({ timeout: ASYNC_TIMEOUT });
  await expect(page.getByText(/Deterministic Anthropic reply from NervOS\./)).toHaveCount(2, {
    timeout: ASYNC_TIMEOUT,
  });
  await page.reload();
  await expect(page.getByText(/Deterministic Anthropic reply from NervOS\./)).toHaveCount(2, {
    timeout: ASYNC_TIMEOUT,
  });

  // C7 proof: the durable execution timeline reaches an actual user interface, in order, across a
  // real retry. Every row below is a persisted Event rather than anything the client inferred, and
  // the browser is not the authority for any of it: the timeline is fetched from the API, keyed on
  // the durable sequence, and rebuilt from scratch after a reload.
  const retryCard = page.locator("article.run-item").filter({ hasText: retryPrompt() });
  await retryCard.getByRole("button", { name: /show timeline/i }).click();
  const retryTimeline = retryCard.locator("ol.run-timeline");
  await expect(retryTimeline.locator("li")).toHaveCount(9, { timeout: ASYNC_TIMEOUT });
  const expectedTimeline = [
    "Run accepted",
    "Queued for a worker",
    "A worker claimed this run",
    "Execution started",
    "Attempt 1 failed",
    "Retry scheduled",
    "A worker claimed this run",
    "Execution started",
    "Run succeeded",
  ];
  await expect(retryTimeline.locator(".run-timeline-headline")).toHaveText(expectedTimeline);
  // The retry row names the instant it is waiting for, taken from the durable Job.
  await expect(retryTimeline.getByText(/^Waiting until /)).toBeVisible();
  // Nothing internal is rendered, whatever the API chose to send.
  await expect(retryTimeline).not.toContainText(/claim_token|worker_id|lease|heartbeat|job_id/);

  await page.reload();
  const reloadedRetry = page.locator("article.run-item").filter({ hasText: retryPrompt() });
  await reloadedRetry.getByRole("button", { name: /show timeline/i }).click();
  await expect(reloadedRetry.locator(".run-timeline-headline")).toHaveText(expectedTimeline);

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

  // A cancelled Run's timeline is exactly its two durable cancellation facts. There is no
  // `attempt.cancelled` Event in the vocabulary, so nothing may be fabricated to fill the gap.
  await reloadedCard.getByRole("button", { name: /show timeline/i }).click();
  const cancelledTimeline = reloadedCard.locator("ol.run-timeline");
  await expect(cancelledTimeline.locator("li")).toHaveCount(6, { timeout: ASYNC_TIMEOUT });
  await expect(cancelledTimeline.locator(".run-timeline-headline")).toHaveText([
    "Run accepted",
    "Queued for a worker",
    "A worker claimed this run",
    "Execution started",
    "Cancellation requested",
    "Run cancelled",
  ]);

  // --- Stage D: a tool-enabled Run renders its tool lifecycle and settles ---
  const toolSeed = await seedToolEnabledAgent(page);
  await page.goto(`/agents/${toolSeed.agentInstanceId}`);
  await expect(page.getByRole("heading", { name: toolSeed.displayName })).toBeVisible();
  await page.getByLabel("Message").fill(toolPrompt());
  await page.getByRole("button", { name: /run agent/i }).click();

  // The Run settles through the same durable path: the scripted model turn requests the one
  // granted tool, the tool succeeds, the concluding turn answers, and the browser observes it.
  const toolCard = page.locator("article.run-item").filter({ hasText: toolPrompt() });
  await expect(toolCard.getByText(/^Succeeded$/)).toBeVisible({ timeout: ASYNC_TIMEOUT });
  await expect(toolCard.getByText(/Deterministic Anthropic reply from NervOS\./)).toBeVisible();
  await toolCard.getByRole("button", { name: /show timeline/i }).click();
  const toolTimeline = toolCard.locator("ol.run-timeline");
  await expect(toolTimeline.locator("li")).toHaveCount(8, { timeout: ASYNC_TIMEOUT });
  await expect(toolTimeline.locator(".run-timeline-headline")).toHaveText([
    "Run accepted",
    "Queued for a worker",
    "A worker claimed this run",
    "Execution started",
    "Requested tool",
    "Ran tool",
    "Tool succeeded",
    "Run succeeded",
  ]);
  // The lifecycle rows are rendered by event type, and no denied, failed, or unknown-outcome row
  // exists: the one granted call went through and nothing was fabricated to fill a gap.
  await expect(toolTimeline.locator(".run-timeline-tool-requested")).toHaveCount(1);
  await expect(toolTimeline.locator(".run-timeline-tool-started")).toHaveCount(1);
  await expect(toolTimeline.locator(".run-timeline-tool-succeeded")).toHaveCount(1);
  await expect(
    toolTimeline.locator(
      ".run-timeline-tool-failed, .run-timeline-tool-denied, .run-timeline-tool-ambiguous",
    ),
  ).toHaveCount(0);
  // No raw or internal tool material reaches the page: no detail text at all, and neither the
  // durable model-facing tool name nor any internal column name is rendered.
  await expect(toolTimeline.locator(".run-timeline-detail")).toHaveCount(0);
  const renderedToolTimeline = (await toolTimeline.innerText()).toLowerCase();
  expect(renderedToolTimeline).not.toContain(toolSeed.toolModelName.toLowerCase());
  for (const internal of ["tool_invocation", "call_id", "fingerprint", "arguments", "traceback"]) {
    expect(renderedToolTimeline).not.toContain(internal);
  }
  // Return to the original Agent Instance so the disable and logout assertions below are unchanged.
  await page.goto(chatAgentUrl);

  // Disabling the agent blocks new Runs while leaving the existing history readable.
  await page.getByRole("button", { name: /disable agent/i }).click();
  await expect(page.getByText(/cannot start new runs/i)).toBeVisible();
  await expect(page.getByRole("button", { name: /run agent/i })).toBeDisabled();
  await expect(page.getByText(/Deterministic OpenAI reply from NervOS\./)).toBeVisible();

  await page.goto("/dashboard");
  await page.getByRole("button", { name: /log out/i }).click();
  await expect(page.getByRole("heading", { name: /welcome back/i })).toBeVisible();
});
