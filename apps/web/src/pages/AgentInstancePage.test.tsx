import { screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import {
  apiAgentInstance,
  apiError,
  apiRun,
  authenticatedHandler,
  renderRoute,
  setupStatusHandler,
  type ApiAgentInstance,
  type ApiRun,
} from "../test/agentFixtures";
import { server } from "../test/server";

interface Handlers {
  instance?: ApiAgentInstance;
  runs?: ApiRun[];
  patch?: (body: unknown) => Response;
  createdRun?: (input: string) => ApiRun;
  runError?: () => Response;
  nextBeforeId?: number | null;
}

function detailHandlers(options: Handlers = {}) {
  const instance = options.instance ?? apiAgentInstance();
  // The history is stateful so a refetch after execution returns the persisted run, exactly as
  // the server would.
  const history: ApiRun[] = [...(options.runs ?? [])];

  return [
    setupStatusHandler(true),
    authenticatedHandler(),
    http.get("/api/v1/agent-instances/1", () => HttpResponse.json(instance)),
    http.get("/api/v1/agent-instances/1/runs", () =>
      HttpResponse.json({ items: [...history], next_before_id: options.nextBeforeId ?? null }),
    ),
    http.patch("/api/v1/agent-instances/1", async ({ request }) =>
      options.patch
        ? options.patch(await request.json())
        : HttpResponse.json({ ...instance, updated_at: "2026-02-02T00:00:00Z" }),
    ),
    http.post("/api/v1/agent-instances/1/runs", async ({ request }) => {
      if (options.runError) {
        return options.runError();
      }
      const body = (await request.json()) as { input: string };
      const created = options.createdRun
        ? options.createdRun(body.input)
        : apiRun({ id: history.length + 1, input_text: body.input });
      history.unshift(created);
      return HttpResponse.json(created, { status: 201 });
    }),
  ];
}

describe("agent detail page", () => {
  it("shows the configuration and the independent-run disclosure", async () => {
    server.use(...detailHandlers());

    await renderRoute("/agents/1");

    expect(await screen.findByRole("heading", { name: "Chat" })).toBeVisible();
    expect(screen.getByText(/nervos\.chat v1/)).toBeVisible();
    expect(screen.getByRole("heading", { name: /run history/i })).toBeVisible();
    expect(screen.getByText(/never sent to the model/i)).toBeVisible();
    expect(screen.getByLabelText("Model provider")).toHaveValue("anthropic");
    expect(screen.getByLabelText("Model")).toHaveValue("opaque/model");
  });

  it("sends the full configuration triple when saving", async () => {
    let body: unknown;
    server.use(...detailHandlers({ patch: (value) => ((body = value), HttpResponse.json(apiAgentInstance())) }));
    const { user } = await renderRoute("/agents/1");
    await screen.findByRole("heading", { name: "Chat" });

    const name = screen.getByLabelText("Display name");
    await user.clear(name);
    await user.type(name, "Renamed");
    await user.click(screen.getByRole("button", { name: /save configuration/i }));

    await waitFor(() =>
      expect(body).toEqual({
        display_name: "Renamed",
        model_provider: "anthropic",
        model_name: "opaque/model",
      }),
    );
    expect(Object.keys(body as object)).not.toContain("enabled");
  });

  it("sends only enabled when toggling the agent", async () => {
    let body: unknown;
    server.use(
      ...detailHandlers({
        patch: (value) => ((body = value), HttpResponse.json(apiAgentInstance({ enabled: false }))),
      }),
    );
    const { user } = await renderRoute("/agents/1");
    await screen.findByRole("heading", { name: "Chat" });

    await user.click(screen.getByRole("button", { name: /disable agent/i }));

    await waitFor(() => expect(body).toEqual({ enabled: false }));
    expect(Object.keys(body as object)).toEqual(["enabled"]);
  });

  it("runs the agent and renders the persisted run", async () => {
    let body: unknown;
    server.use(
      ...detailHandlers({
        createdRun: (input) => {
          body = { input };
          return apiRun({ id: 3, input_text: input });
        },
      }),
    );
    const { user } = await renderRoute("/agents/1");
    await screen.findByRole("heading", { name: "Chat" });

    await user.type(screen.getByLabelText("Message"), "hello");
    await user.click(screen.getByRole("button", { name: /run agent/i }));

    expect(await screen.findByText("deterministic answer")).toBeVisible();
    expect(screen.getByText("Run #3")).toBeVisible();
    expect(body).toEqual({ input: "hello" });
  });

  it("disables submission while a run is in flight and never fakes progress", async () => {
    let release: (() => void) | undefined;
    const history: ApiRun[] = [];
    // Earlier handlers in the list take precedence, so the deferred POST is registered first.
    // The history it appends to is the same one the list handler serves, so the post-run
    // refetch agrees with the response — exactly as the server would.
    server.use(
      http.post("/api/v1/agent-instances/1/runs", async () => {
        await new Promise<void>((resolve) => {
          release = resolve;
        });
        const created = apiRun({ id: 1 });
        history.unshift(created);
        return HttpResponse.json(created, { status: 201 });
      }),
      setupStatusHandler(true),
      authenticatedHandler(),
      http.get("/api/v1/agent-instances/1", () => HttpResponse.json(apiAgentInstance())),
      http.get("/api/v1/agent-instances/1/runs", () =>
        HttpResponse.json({ items: [...history], next_before_id: null }),
      ),
    );
    const { user } = await renderRoute("/agents/1");
    await screen.findByRole("heading", { name: "Chat" });

    await user.type(screen.getByLabelText("Message"), "hello");
    await user.click(screen.getByRole("button", { name: /run agent/i }));

    const pending = await screen.findByRole("button", { name: /running/i });
    expect(pending).toBeDisabled();
    expect(screen.getByLabelText("Message")).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent(/can take up to a minute/i);
    expect(screen.queryByText(/%/)).toBeNull();

    release?.();
    expect(await screen.findByText("deterministic answer")).toBeVisible();
  });

  it("renders a persisted failed run as a run card, not as a request error", async () => {
    server.use(
      ...detailHandlers({
        createdRun: (input) =>
          apiRun({
            id: 4,
            input_text: input,
            status: "failed",
            output_text: null,
            finish_reason: null,
            error_code: "model_rate_limited",
            error_message: "The model provider is temporarily rate limited.",
          }),
      }),
    );
    const { user } = await renderRoute("/agents/1");
    await screen.findByRole("heading", { name: "Chat" });

    await user.type(screen.getByLabelText("Message"), "hello");
    await user.click(screen.getByRole("button", { name: /run agent/i }));

    expect(await screen.findByText(/temporarily rate limited/i)).toBeVisible();
    expect(screen.getByText(/model_rate_limited/)).toBeVisible();
    expect(screen.getByText("Failed")).toBeVisible();
    // The request succeeded and the Run was persisted, so this must not be shown as a request error.
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("renders a pre-run rejection as an inline request error", async () => {
    server.use(
      ...detailHandlers({
        runError: () =>
          apiError(
            409,
            "model_provider_unavailable",
            "The model provider is not configured for this NervOS process.",
          ),
      }),
    );
    const { user } = await renderRoute("/agents/1");
    await screen.findByRole("heading", { name: "Chat" });

    await user.type(screen.getByLabelText("Message"), "hello");
    await user.click(screen.getByRole("button", { name: /run agent/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The model provider is not configured for this NervOS process.",
    );
  });

  it("blocks new runs while the agent is disabled and explains why", async () => {
    server.use(...detailHandlers({ instance: apiAgentInstance({ enabled: false }) }));

    await renderRoute("/agents/1");

    expect(await screen.findByRole("button", { name: /run agent/i })).toBeDisabled();
    expect(screen.getByLabelText("Message")).toBeDisabled();
    expect(screen.getByText(/cannot start new runs/i)).toBeVisible();
    expect(screen.getByRole("button", { name: /enable agent/i })).toBeVisible();
  });

  it("surfaces the server's disabled-instance rejection inline", async () => {
    // The agent was disabled by another tab between load and submit, so the client-side block has
    // already been bypassed and only the server's 409 can explain what happened.
    server.use(
      ...detailHandlers({
        runError: () =>
          apiError(
            409,
            "agent_instance_unavailable",
            "The agent instance is not eligible to create new runs.",
          ),
      }),
    );
    const { user } = await renderRoute("/agents/1");
    await screen.findByRole("heading", { name: "Chat" });

    await user.type(screen.getByLabelText("Message"), "hello");
    await user.click(screen.getByRole("button", { name: /run agent/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The agent instance is not eligible to create new runs.",
    );
  });

  it("reports an enable/disable failure next to the toggle, not inside Configuration", async () => {
    server.use(
      ...detailHandlers({
        patch: () =>
          apiError(503, "service_unavailable", "NervOS storage is temporarily unavailable."),
      }),
    );
    const { user } = await renderRoute("/agents/1");
    await screen.findByRole("heading", { name: "Chat" });

    await user.click(screen.getByRole("button", { name: /disable agent/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("NervOS storage is temporarily unavailable.");
    // The failure belongs to the toggle, not to the model/name settings form.
    const configPanel = screen.getByRole("heading", { name: "Configuration" }).closest("section");
    expect(configPanel).not.toBeNull();
    expect(configPanel).not.toContainElement(alert);
  });

  it("never claims a non-terminal run is queued for work", async () => {
    // `created` means the run was persisted but is not being executed by anything. There is no
    // queue or worker in this milestone, so the label must not imply one.
    server.use(...detailHandlers({ runs: [apiRun({ status: "created", output_text: null })] }));

    await renderRoute("/agents/1");

    expect(await screen.findByText("Created")).toBeVisible();
    expect(screen.queryByText(/queued/i)).toBeNull();
    expect(screen.getByText(/does not resume or retry it/i)).toBeVisible();
  });

  it("says so when the run history may be truncated rather than looking complete", async () => {
    // A non-null cursor means the page was full, so more runs may exist beyond it. The note must
    // not claim the list is complete — and must not claim older runs exist either, since the
    // server returns a cursor for a final full page too.
    server.use(
      ...detailHandlers({
        runs: [apiRun({ id: 2, output_text: "Second answer" })],
        nextBeforeId: 2,
      }),
    );

    await renderRoute("/agents/1");

    expect(await screen.findByText("Second answer")).toBeVisible();
    expect(screen.getByText(/only the most recent runs are listed/i)).toBeVisible();
  });

  it("does not claim truncation when the whole history fits on one page", async () => {
    server.use(...detailHandlers({ runs: [apiRun({ output_text: "Only answer" })] }));

    await renderRoute("/agents/1");

    expect(await screen.findByText("Only answer")).toBeVisible();
    expect(screen.queryByText(/only the most recent runs are listed/i)).toBeNull();
  });

  it("renders stored run history from the API so a reload reconstructs it", async () => {
    server.use(
      ...detailHandlers({
        runs: [
          apiRun({ id: 2, input_text: "second", output_text: "Second answer" }),
          apiRun({ id: 1, input_text: "first", output_text: "First answer" }),
        ],
      }),
    );

    const first = await renderRoute("/agents/1");

    expect(await screen.findByText("Second answer")).toBeVisible();
    expect(screen.getByText("First answer")).toBeVisible();
    expect(screen.getByText("Run #2")).toBeVisible();
    first.unmount();

    await renderRoute("/agents/1");
    expect(await screen.findByText("Second answer")).toBeVisible();
  });

  it("renders html-looking model output as text and never creates an element", async () => {
    const hostile = '<img src=x onerror="alert(1)"><script>alert(2)</script>';
    server.use(...detailHandlers({ runs: [apiRun({ output_text: hostile })] }));

    const { container } = await renderRoute("/agents/1");

    expect(await screen.findByText(hostile)).toBeVisible();
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
  });

  it("preserves newlines in model output", async () => {
    server.use(...detailHandlers({ runs: [apiRun({ output_text: "line one\nline two" })] }));

    await renderRoute("/agents/1");

    const output = await screen.findByText(/line one/);
    expect(output.textContent).toBe("line one\nline two");
    expect(output.className).toContain("run-text");
  });

  it("shows a safe error for a missing or foreign agent", async () => {
    server.use(
      setupStatusHandler(true),
      authenticatedHandler(),
      http.get("/api/v1/agent-instances/1", () =>
        apiError(404, "agent_instance_not_found", "The agent instance was not found."),
      ),
      // A missing or foreign instance fails both reads: the server resolves the parent before it
      // lists runs, so the history is never a misleading empty page.
      http.get("/api/v1/agent-instances/1/runs", () =>
        apiError(404, "agent_instance_not_found", "The agent instance was not found."),
      ),
    );

    await renderRoute("/agents/1");

    expect(await screen.findByRole("alert")).toHaveTextContent("The agent instance was not found.");
  });

  it("shows a network failure without exposing internals", async () => {
    server.use(
      setupStatusHandler(true),
      authenticatedHandler(),
      http.get("/api/v1/agent-instances/1", () => HttpResponse.error()),
      http.get("/api/v1/agent-instances/1/runs", () => HttpResponse.error()),
    );

    await renderRoute("/agents/1");

    expect(await screen.findByRole("alert")).toHaveTextContent(/unable to reach nervos/i);
  });

  it("renders a not-found page for a non-numeric agent id without calling the API", async () => {
    server.use(...detailHandlers());

    await renderRoute("/agents/not-a-number");

    expect(await screen.findByRole("heading", { name: /page not found/i })).toBeVisible();
  });

  it("never renders a credential or sends an owner field", async () => {
    const requests: string[] = [];
    server.use(
      setupStatusHandler(true),
      authenticatedHandler(),
      http.get("/api/v1/agent-instances/1", async ({ request }) => {
        requests.push(request.url);
        return HttpResponse.json(apiAgentInstance());
      }),
      http.get("/api/v1/agent-instances/1/runs", () =>
        HttpResponse.json({ items: [], next_before_id: null }),
      ),
    );

    const { container } = await renderRoute("/agents/1");
    await screen.findByRole("heading", { name: "Chat" });

    expect(container.textContent ?? "").not.toMatch(/credential/i);
    expect(requests.every((url) => !url.includes("owner_user_id"))).toBe(true);
  });
});
