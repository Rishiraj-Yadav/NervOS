import { renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { QueryClientProvider } from "@tanstack/react-query";
import { createQueryClient } from "../app/queryClient";
import { useTriggerActions } from "../api/triggerQueries";
import { authenticatedHandler, setupStatusHandler } from "../test/agentFixtures";
import { server } from "../test/server";

const trigger = {
  id: 4,
  agent_instance_id: 1,
  kind: "webhook",
  display_name: "Inbound",
  enabled: true,
  config_revision: 1,
  next_fire_at: null,
  event_type: null,
  webhook_path: "/hooks/v1/pub-4",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  input_text: "Handle delivery",
  run_at: null,
  interval_seconds: null,
  cron_expression: null,
  timezone: null,
  secret_created_at: "2026-01-01T00:00:00Z",
};

describe("automation secret handling", () => {
  it("keeps the plaintext secret out of mutation state and browser storage", async () => {
    server.use(
      setupStatusHandler(true),
      authenticatedHandler(),
      http.post("/api/v1/triggers", () => HttpResponse.json({ trigger, secret: "one-time-secret" })),
    );
    const queryClient = createQueryClient();
    const { result } = renderHook(() => useTriggerActions(), {
      wrapper: ({ children }) => <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>,
    });

    result.current.create.mutate({ kind: "webhook", agent_instance_id: 1, display_name: "Inbound", input_text: "Handle", enabled: true });
    await waitFor(() => expect(result.current.create.isSuccess).toBe(true));

    expect(result.current.create.data).toEqual(trigger);
    expect(JSON.stringify(queryClient.getMutationCache().getAll())).not.toContain("one-time-secret");
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });
});
