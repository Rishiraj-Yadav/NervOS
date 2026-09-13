import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";

import {
  createAgentInstance,
  createRun,
  getAgentInstance,
  getRun,
  isAgentInstance,
  isRun,
  listAgentInstances,
  listRuns,
  updateAgentInstance,
} from "./agentInstances";
import { apiAgentInstance, apiError, apiRun } from "../test/agentFixtures";
import { server } from "../test/server";

describe("agent instance and run API contract", () => {
  it("lists agent instances from the owner-scoped collection", async () => {
    server.use(
      http.get("/api/v1/agent-instances", ({ request }) => {
        expect(new URL(request.url).searchParams.get("before_id")).toBeNull();
        return HttpResponse.json({ items: [apiAgentInstance()], next_before_id: null });
      }),
    );

    const page = await listAgentInstances();

    expect(page.items).toHaveLength(1);
    expect(page.next_before_id).toBeNull();
  });

  it("passes a cursor through unchanged", async () => {
    server.use(
      http.get("/api/v1/agent-instances", ({ request }) => {
        expect(new URL(request.url).searchParams.get("before_id")).toBe("42");
        return HttpResponse.json({ items: [], next_before_id: null });
      }),
    );

    await listAgentInstances(42);
  });

  it("creates an agent instance with the exact trusted definition", async () => {
    let body: unknown;
    server.use(
      http.post("/api/v1/agent-instances", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(apiAgentInstance(), { status: 201 });
      }),
    );

    await createAgentInstance({
      agent_key: "nervos.chat",
      agent_definition_version: "1",
      display_name: "Chat",
      model_provider: "anthropic",
      model_name: "opaque/model",
    });

    expect(body).toEqual({
      agent_key: "nervos.chat",
      agent_definition_version: "1",
      display_name: "Chat",
      model_provider: "anthropic",
      model_name: "opaque/model",
    });
    expect(JSON.stringify(body)).not.toContain("owner_user_id");
  });

  it("fetches a single owned agent instance", async () => {
    server.use(
      http.get("/api/v1/agent-instances/7", () => HttpResponse.json(apiAgentInstance({ id: 7 }))),
    );

    await expect(getAgentInstance(7)).resolves.toMatchObject({ id: 7 });
  });

  it("sends a configuration update with all three values and no enabled flag", async () => {
    let body: unknown;
    server.use(
      http.patch("/api/v1/agent-instances/3", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(apiAgentInstance({ id: 3 }));
      }),
    );

    await updateAgentInstance(3, {
      display_name: "Renamed",
      model_provider: "anthropic",
      model_name: "opaque/other",
    });

    expect(body).toEqual({
      display_name: "Renamed",
      model_provider: "anthropic",
      model_name: "opaque/other",
    });
    expect(Object.keys(body as object)).not.toContain("enabled");
  });

  it("sends an enable-state update containing only enabled", async () => {
    let body: unknown;
    server.use(
      http.patch("/api/v1/agent-instances/3", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(apiAgentInstance({ id: 3, enabled: false }));
      }),
    );

    await updateAgentInstance(3, { enabled: false });

    expect(body).toEqual({ enabled: false });
  });

  it("executes a run and returns the persisted run", async () => {
    let body: unknown;
    server.use(
      http.post("/api/v1/agent-instances/5/runs", async ({ request }) => {
        body = await request.json();
        return HttpResponse.json(apiRun({ agent_instance_id: 5 }), { status: 201 });
      }),
    );

    const run = await createRun(5, "hello");

    expect(body).toEqual({ input: "hello" });
    expect(run.status).toBe("succeeded");
  });

  it("lists runs for one agent instance and fetches a single run", async () => {
    server.use(
      http.get("/api/v1/agent-instances/5/runs", () =>
        HttpResponse.json({ items: [apiRun()], next_before_id: null }),
      ),
      http.get("/api/v1/runs/9", () => HttpResponse.json(apiRun({ id: 9 }))),
    );

    await expect(listRuns(5)).resolves.toMatchObject({ items: [apiRun()] });
    await expect(getRun(9)).resolves.toMatchObject({ id: 9 });
  });

  it("rejects an unreadable payload rather than trusting it", async () => {
    server.use(
      http.get("/api/v1/agent-instances/7", () => HttpResponse.json({ id: "not-a-number" })),
    );

    await expect(getAgentInstance(7)).rejects.toThrow(/unexpected response/i);
  });

  it("surfaces the safe server error envelope", async () => {
    server.use(
      http.post("/api/v1/agent-instances/5/runs", () =>
        apiError(409, "model_provider_unavailable", "The model provider is not configured."),
      ),
    );

    await expect(createRun(5, "hello")).rejects.toMatchObject({
      status: 409,
      code: "model_provider_unavailable",
    });
  });
});

describe("runtime guards", () => {
  it("accepts a valid agent instance and rejects malformed shapes", () => {
    expect(isAgentInstance(apiAgentInstance())).toBe(true);
    expect(isAgentInstance({ ...apiAgentInstance(), enabled: "yes" })).toBe(false);
    expect(isAgentInstance({ ...apiAgentInstance(), id: "1" })).toBe(false);
  });

  it("accepts a valid run including a null usage object", () => {
    expect(isRun(apiRun())).toBe(true);
    expect(isRun(apiRun({ usage: null }))).toBe(true);
    expect(isRun({ ...apiRun(), status: "cancelled" })).toBe(false);
    expect(isRun({ ...apiRun(), output_text: 7 })).toBe(false);
  });

  it("rejects every status the domain does not define", () => {
    // Only the four B1 states are representable; a run must never be shown as cancelled,
    // retrying, or timed out.
    for (const invented of ["cancelled", "retrying", "timed_out", "queued", "SUCCEEDED", ""]) {
      expect(isRun({ ...apiRun(), status: invented })).toBe(false);
    }
    for (const real of ["created", "running", "succeeded", "failed"]) {
      expect(isRun({ ...apiRun(), status: real })).toBe(true);
    }
  });

  it("rejects an unreadable run rather than trusting it", async () => {
    server.use(
      http.get("/api/v1/runs/9", () => HttpResponse.json({ ...apiRun(), id: "not-a-number" })),
    );

    await expect(getRun(9)).rejects.toThrow(/unexpected response/i);
  });

  it("never derives a total token count", () => {
    const run = apiRun();

    expect(run.usage?.total_tokens).toBeNull();
    expect(isRun(run)).toBe(true);
  });
});

describe("request helper", () => {
  it("uses relative versioned URLs with credentials", async () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    fetchSpy.mockResolvedValueOnce(
      new Response(JSON.stringify({ items: [], next_before_id: null }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await listAgentInstances();

    const [url, init] = fetchSpy.mock.calls[0];
    expect(url).toBe("/api/v1/agent-instances");
    expect(init).toMatchObject({ method: "GET", credentials: "include" });
    fetchSpy.mockRestore();
  });
});
