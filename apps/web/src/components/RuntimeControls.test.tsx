import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { AgentRuntimeControls } from "./RuntimeControls";
import { renderWithQueryClient } from "../test/render";
import { server } from "../test/server";

describe("agent runtime controls", () => {
  it("offers explicitly declared native Gmail reads for package binding", async () => {
    let binding: unknown;
    const hash = "a".repeat(64);
    server.use(
      http.get("/api/v1/agent-instances/7/tools", () => HttpResponse.json({
        requirements: [{alias: "gmail.messages", upstream_name: "gmail.users.messages.list", required: true, input_schema_sha256: hash}],
        bindings: [], grants: [], history: [],
      })),
      http.get("/api/v1/tools", () => HttpResponse.json({items: [{id: 41,
        name: "gmail.users.messages.list", display_name: "Gmail list/search", source_kind: "builtin",
        status: "available", fingerprint: hash, input_schema_sha256: hash, connection_id: null}], next_before_id: null})),
      http.post("/api/v1/agent-instances/7/tool-bindings", async ({request}) => {
        binding = await request.json();
        return new HttpResponse(null, {status: 204});
      }),
    );
    const user = userEvent.setup();
    renderWithQueryClient(<AgentRuntimeControls instanceId={7} />);
    await user.click(screen.getByText("Tools & permissions"));
    const choice = await screen.findByRole("option", {name: "Gmail list/search · native connector"});
    const select = choice.closest("select");
    expect(select).not.toBeNull();
    await user.selectOptions(select!, "41");
    await user.click(screen.getByRole("button", {name: "Save binding"}));
    await waitFor(() => expect(binding).toEqual({alias: "gmail.messages", definition_id: 41}));
  });
  it("saves an explicitly selected memory policy with its revision", async () => {
    let requestBody: unknown;
    server.use(
      http.get("/api/v1/agent-instances/7/memory-policy", () =>
        HttpResponse.json({ mode: "manual", revision: 3, extraction_enabled: false }),
      ),
      http.patch("/api/v1/agent-instances/7/memory-policy", async ({ request }) => {
        requestBody = await request.json();
        return HttpResponse.json({
          mode: "review",
          revision: 4,
          extraction_enabled: false,
        });
      }),
    );
    const user = userEvent.setup();
    renderWithQueryClient(<AgentRuntimeControls instanceId={7} />);

    await user.click(screen.getByText("Memory policy"));
    const mode = await screen.findByLabelText("Memory saving");
    await user.selectOptions(mode, "review");
    await user.click(screen.getByRole("button", { name: "Save memory policy" }));

    expect(await screen.findByRole("status")).toHaveTextContent("Memory policy saved.");
    await waitFor(() =>
      expect(requestBody).toEqual({
        mode: "review",
        extraction_enabled: false,
        expected_revision: 3,
      }),
    );
  });

  it("lets the owner inspect and approve a pending memory suggestion", async () => {
    let decision: unknown;
    server.use(
      http.get("/api/v1/memory-suggestions", () =>
        HttpResponse.json({
          items: [
            {
              id: 12,
              agent_instance_id: 7,
              source_run_id: 41,
              scope: "agent",
              content: "Prefer concise summaries.",
              state: "pending",
              memory_item_id: null,
            },
          ],
          next_before_id: null,
        }),
      ),
      http.post("/api/v1/memory-suggestions/12/decision", async ({ request }) => {
        decision = await request.json();
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    renderWithQueryClient(<AgentRuntimeControls instanceId={7} />);

    await user.click(screen.getByText("Memory suggestions & saved facts"));
    expect(await screen.findByText("Prefer concise summaries.")).toBeVisible();
    expect(screen.getByText(/Run #41/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Approve fact" }));

    await waitFor(() => expect(decision).toEqual({ approve: true }));
  });
});
