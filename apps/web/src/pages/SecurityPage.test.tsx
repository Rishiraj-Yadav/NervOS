import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { authenticatedHandler, renderRoute, setupStatusHandler } from "../test/agentFixtures";
import { server } from "../test/server";

const apiSecret = {
  id: 1,
  name: "github-token",
  provider_hint: "github",
  status: "active",
  key_version: 1,
  rotation_count: 0,
  created_at: "2026-10-01T00:00:00Z",
  updated_at: "2026-10-01T00:00:00Z",
};

const apiApproval = {
  id: 5,
  agent_instance_id: 1,
  run_id: 9,
  tool_sequence: 1,
  upstream_name: "search",
  fingerprint: "d".repeat(64),
  input_digest: "e".repeat(64),
  preview: { q: "<string:13 bytes>" },
  state: "pending",
  requested_at: "2026-10-01T00:00:00Z",
  expires_at: "2026-10-01T00:15:00Z",
  decided_at: null,
  consumed_at: null,
};

function signedIn(overrides: { secrets?: unknown[]; approvals?: unknown[]; trust?: unknown[] } = {}) {
  return [
    setupStatusHandler(true),
    authenticatedHandler(),
    http.get("/api/v1/account-oauth/providers", () => HttpResponse.json([])),
    http.get("/api/v1/secrets", () => HttpResponse.json(overrides.secrets ?? [apiSecret])),
    http.get("/api/v1/account-connections", () => HttpResponse.json([])),
    http.get("/api/v1/action-approvals", () =>
      HttpResponse.json(overrides.approvals ?? [apiApproval]),
    ),
    http.get("/api/v1/publisher-trust", () => HttpResponse.json(overrides.trust ?? [])),
  ];
}

describe("security page", () => {
  it("never renders a stored secret value and lists secrets with their state", async () => {
    server.use(...signedIn());

    await renderRoute("/security");

    expect(await screen.findByText("github-token")).toBeVisible();
    expect(screen.getByText(/active · key v1/)).toBeVisible();
    expect(document.body.textContent).not.toContain("ghp-");
  });

  it("stores a new secret through the write-only form", async () => {
    let received: unknown = null;
    server.use(
      ...signedIn({ secrets: [] }),
      http.post("/api/v1/secrets", async ({ request }) => {
        received = await request.json();
        return HttpResponse.json(apiSecret, { status: 201 });
      }),
    );

    await renderRoute("/security");
    await userEvent.type(await screen.findByLabelText(/^name$/i), "slack-token");
    await userEvent.type(screen.getByLabelText(/^value$/i), "xoxb-example");
    await userEvent.click(screen.getByRole("button", { name: /store encrypted secret/i }));

    await waitFor(() => expect(received).not.toBeNull());
    expect(received).toEqual({ name: "slack-token", value: "xoxb-example" });
    await waitFor(() => expect(screen.getByLabelText(/^value$/i)).toHaveValue(""));
  });

  it("shows a pending external action with its redacted preview and one-shot decision", async () => {
    const decided: unknown[] = [];
    server.use(
      ...signedIn(),
      http.post("/api/v1/action-approvals/5/decision", async ({ request }) => {
        decided.push(await request.json());
        return HttpResponse.json({ ...apiApproval, state: "approved" });
      }),
    );

    await renderRoute("/security");

    expect(await screen.findByText("search")).toBeVisible();
    expect(screen.getByText(/<string:13 bytes>/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /approve once/i })).toBeVisible();

    await userEvent.click(screen.getByRole("button", { name: /approve once/i }));

    await waitFor(() => expect(decided).toHaveLength(1));
    expect(decided).toEqual([{ approve: true }]);
  });

  it("records a local publisher revocation", async () => {
    const decisions: unknown[] = [];
    server.use(
      ...signedIn(),
      http.put("/api/v1/publisher-trust/*", async ({ request }) => {
        const fingerprint = new URL(request.url).pathname.split("/").pop() ?? "";
        decisions.push({ fingerprint, body: await request.json() });
        return HttpResponse.json({
          signer_fingerprint: fingerprint,
          state: "revoked",
          reason: null,
          source: "manual",
          created_at: "2026-10-01T00:00:00Z",
          updated_at: "2026-10-01T00:00:00Z",
        });
      }),
    );

    await renderRoute("/security");
    await userEvent.type(await screen.findByLabelText(/signer fingerprint/i), "b".repeat(64));
    await userEvent.click(screen.getByRole("button", { name: /record decision/i }));

    await waitFor(() => expect(decisions).toHaveLength(1));
    expect(decisions).toEqual([{ fingerprint: "b".repeat(64), body: { state: "revoked" } }]);
  });
});