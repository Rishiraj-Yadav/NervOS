import { screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import {
  apiAgentInstance,
  apiError,
  apiRun,
  apiRunEvent,
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
        : apiRun({ id: history.length + 1, input_text: body.input, status: "created", output_text: null, finish_reason: null, elapsed_ms: null, usage: null });
      history.unshift(created);
      return HttpResponse.json(created, { status: 202 });
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

  it("shows a second-provider instance with its stored provider selected", async () => {
    server.use(
      ...detailHandlers({
        instance: apiAgentInstance({ model_provider: "openai", model_name: "opaque/second" }),
      }),
    );

    await renderRoute("/agents/1");

    await screen.findByRole("heading", { name: "Chat" });
    expect(screen.getByLabelText("Model provider")).toHaveValue("openai");
    expect(screen.getByLabelText("Model")).toHaveValue("opaque/second");
  });

  it("switches provider without rewriting the user's model string", async () => {
    let body: unknown;
    server.use(
      ...detailHandlers({
        patch: (value) => (
          (body = value),
          HttpResponse.json(apiAgentInstance({ model_provider: "openai", model_name: "opaque/model" }))
        ),
      }),
    );
    const { user } = await renderRoute("/agents/1");
    await screen.findByRole("heading", { name: "Chat" });

    await user.selectOptions(screen.getByLabelText("Model provider"), "openai");
    expect(screen.getByLabelText("Model")).toHaveValue("opaque/model");
    await user.click(screen.getByRole("button", { name: /save configuration/i }));

    await waitFor(() =>
      expect(body).toEqual({
        display_name: "Chat",
        model_provider: "openai",
        model_name: "opaque/model",
      }),
    );
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

  it("submits the agent and renders the queued run", async () => {
    let body: unknown;
    server.use(
      ...detailHandlers({
        createdRun: (input) => {
          body = { input };
          return apiRun({
            id: 3,
            input_text: input,
            status: "created",
            output_text: null,
            finish_reason: null,
            elapsed_ms: null,
            usage: null,
          });
        },
      }),
    );
    const { user } = await renderRoute("/agents/1");
    await screen.findByRole("heading", { name: "Chat" });

    await user.type(screen.getByLabelText("Message"), "hello");
    await user.click(screen.getByRole("button", { name: /run agent/i }));

    expect(await screen.findByText("Queued")).toBeVisible();
    expect(screen.getByText(/a worker must be running to execute this run/i)).toBeVisible();
    expect(screen.getByText("Run #3")).toBeVisible();
    expect(body).toEqual({ input: "hello" });
  });

  it("disables submission while in flight and displays submitting state", async () => {
    let release: (() => void) | undefined;
    const history: ApiRun[] = [];
    server.use(
      http.post("/api/v1/agent-instances/1/runs", async () => {
        await new Promise<void>((resolve) => {
          release = resolve;
        });
        const created = apiRun({
          id: 1,
          status: "created",
          output_text: null,
          finish_reason: null,
          elapsed_ms: null,
          usage: null,
        });
        history.unshift(created);
        return HttpResponse.json(created, { status: 202 });
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

    const pending = await screen.findByRole("button", { name: /submitting/i });
    expect(pending).toBeDisabled();
    expect(screen.getByLabelText("Message")).toBeDisabled();
    expect(screen.getByRole("status")).toHaveTextContent(/submitting the run/i);
    expect(screen.queryByText(/%/)).toBeNull();

    release?.();
    expect(await screen.findByText("Queued")).toBeVisible();
  });

  it("renders a persisted failed run as a run card, not as a request error", async () => {
    server.use(
      ...detailHandlers({
        runs: [
          apiRun({
            id: 4,
            input_text: "failed prompt",
            status: "failed",
            output_text: null,
            finish_reason: null,
            error_code: "model_rate_limited",
            error_message: "The model provider is temporarily rate limited.",
          }),
        ],
      }),
    );

    await renderRoute("/agents/1");
    await screen.findByRole("heading", { name: "Chat" });

    expect(await screen.findByText(/temporarily rate limited/i)).toBeVisible();
    expect(screen.getByText(/model_rate_limited/)).toBeVisible();
    expect(screen.getByText("Failed")).toBeVisible();
    expect(screen.queryByRole("alert")).toBeNull();
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
    const configPanel = screen.getByRole("heading", { name: "Configuration" }).closest("section");
    expect(configPanel).not.toBeNull();
    expect(configPanel).not.toContainElement(alert);
  });

  it("renders a created run with the truthful worker dependency copy", async () => {
    server.use(...detailHandlers({ runs: [apiRun({ status: "created", output_text: null })] }));

    await renderRoute("/agents/1");

    expect(await screen.findByText("Queued")).toBeVisible();
    expect(screen.getByText(/a worker must be running to execute this run/i)).toBeVisible();
  });

  it("renders a running run with the truthful crash recovery and retry copy", async () => {
    server.use(...detailHandlers({ runs: [apiRun({ status: "running", output_text: null })] }));

    await renderRoute("/agents/1");

    expect(await screen.findByText("Running")).toBeVisible();
    expect(screen.getByText(/closes this run as failed without replaying it/i)).toBeVisible();
    // C4: a running Run may be waiting briefly before it executes again, and the copy must not
    // promise that no retry ever happens.
    expect(screen.getByText(/may be retried/i)).toBeVisible();
  });

  it("renders a run closed before execution started without a duration or start", async () => {
    // C3's exhausted pre-start recovery produces a failed Run with no `started_at` and no
    // `elapsed_ms`. There is no `durationMs`/`startedAt` fallback to a raw value, so the row
    // must render the safe message and code and nothing else.
    server.use(
      ...detailHandlers({
        runs: [
          apiRun({
            status: "failed",
            output_text: null,
            elapsed_ms: null,
            started_at: null,
            usage: null,
            error_code: "worker_recovery_exhausted",
            error_message:
              "NervOS could not start this run after repeated worker losses, so it was closed"
              + " without invoking the model.",
          }),
        ],
      }),
    );

    await renderRoute("/agents/1");

    expect(await screen.findByText("Failed")).toBeVisible();
    expect(screen.getByText(/closed without invoking the model/i)).toBeVisible();
    expect(screen.getByText(/worker_recovery_exhausted/)).toBeVisible();
  });

  it("says so when the run history may be truncated rather than looking complete", async () => {
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

describe("agent detail page cancellation", () => {
  it("cancels a running run through the server and shows the durable result", async () => {
    const running = apiRun({ id: 7, status: "running" });
    const cancelled = apiRun({
      id: 7,
      status: "cancelled",
      finished_at: "2026-09-14T00:00:05Z",
      elapsed_ms: 5000,
    });
    let cancelCalls = 0;
    // The mock behaves like the real server: the durable state changes, so a refetch after the
    // mutation observes the cancelled Run rather than the pre-cancellation one.
    let current: ApiRun = running;
    server.use(
      setupStatusHandler(true),
      authenticatedHandler(),
      http.get("/api/v1/agent-instances/1", () => HttpResponse.json(apiAgentInstance())),
      http.get("/api/v1/agent-instances/1/runs", () =>
        HttpResponse.json({ items: [current], next_before_id: null }),
      ),
      http.post("/api/v1/runs/7/cancel", () => {
        cancelCalls += 1;
        current = cancelled;
        return HttpResponse.json(cancelled);
      }),
    );

    const { user } = await renderRoute("/agents/1");
    const button = await screen.findByRole("button", { name: /cancel/i });

    await user.click(button);

    await waitFor(() => expect(screen.getByText("Cancelled")).toBeVisible());
    expect(cancelCalls).toBe(1);
    expect(screen.queryByRole("button", { name: /cancel/i })).toBeNull();
  });

  it("surfaces a conflict truthfully instead of pretending the run was cancelled", async () => {
    const running = apiRun({ id: 8, status: "running" });
    server.use(
      ...detailHandlers({ runs: [running] }),
      http.post("/api/v1/runs/8/cancel", () =>
        apiError(409, "run_not_cancellable", "The run already reached a terminal state."),
      ),
    );

    const { user } = await renderRoute("/agents/1");
    await user.click(await screen.findByRole("button", { name: /cancel/i }));

    // The run keeps the status the server actually reports; no optimistic rewrite.
    await waitFor(() => expect(screen.queryByText("Cancelled")).toBeNull());
    expect(screen.getByText("Running")).toBeVisible();
  });
});

/**
 * The execution timeline reaches the page through the Run card, and only when the reader asks for
 * it. These assertions are the reason the harness's `onUnhandledRequest: "error"` setting matters:
 * the events endpoint is deliberately *not* stubbed in most of this file, so any request the card
 * made on its own would fail the suite rather than quietly log.
 */
describe("agent detail page timeline", () => {
  it("costs no request until the reader expands a run", async () => {
    const requests: number[] = [];
    const run = apiRun({ id: 9, status: "succeeded" });
    server.use(
      ...detailHandlers({ runs: [run] }),
      http.get("/api/v1/runs/9/events", ({ request }) => {
        requests.push(Number(new URL(request.url).searchParams.get("after_sequence") ?? "0"));
        return HttpResponse.json({
          items: [
            apiRunEvent({ sequence: 1, event_type: "run.created" }),
            apiRunEvent({ sequence: 2, event_type: "run.queued" }),
            apiRunEvent({ sequence: 3, event_type: "run.succeeded" }),
          ],
          next_after_sequence: null,
        });
      }),
    );

    const { user } = await renderRoute("/agents/1");
    await screen.findByText("Succeeded");
    expect(requests).toEqual([]);

    await user.click(screen.getByRole("button", { name: /show timeline/i }));

    expect(await screen.findByText("Run accepted")).toBeVisible();
    expect(requests).toEqual([0]);
  });

  it("reconstructs the timeline from the server on a reload", async () => {
    const run = apiRun({ id: 9, status: "failed", output_text: null, error_code: "model_timed_out", error_message: "The model request exceeded its time limit." });
    server.use(
      ...detailHandlers({ runs: [run] }),
      http.get("/api/v1/runs/9/events", () =>
        HttpResponse.json({
          items: [
            apiRunEvent({ sequence: 1, event_type: "run.created" }),
            apiRunEvent({
              sequence: 2,
              event_type: "run.failed",
              code: "model_timed_out",
              message: "The model request exceeded its time limit.",
            }),
          ],
          next_after_sequence: null,
        }),
      ),
    );

    const first = await renderRoute("/agents/1");
    await first.user.click(await screen.findByRole("button", { name: /show timeline/i }));
    const rows = (await screen.findAllByRole("listitem")).map((row) => row.textContent);
    first.unmount();

    const second = await renderRoute("/agents/1");
    await second.user.click(await screen.findByRole("button", { name: /show timeline/i }));

    expect((await screen.findAllByRole("listitem")).map((row) => row.textContent)).toEqual(rows);
    expect(screen.getByText(/was not retried/)).toBeVisible();
  });

  it("explains a timeout without calling it a cancellation", async () => {
    const run = apiRun({ id: 9, status: "failed", output_text: null, error_code: "model_timed_out", error_message: "The model request exceeded its time limit." });
    server.use(
      ...detailHandlers({ runs: [run] }),
      http.get("/api/v1/runs/9/events", () =>
        HttpResponse.json({
          items: [apiRunEvent({ sequence: 1, event_type: "run.created" })],
          next_after_sequence: null,
        }),
      ),
    );

    const { user, container } = await renderRoute("/agents/1");
    await user.click(await screen.findByRole("button", { name: /show timeline/i }));
    await screen.findByRole("list");

    expect(container.textContent).not.toMatch(/cancel/i);
  });
});
