import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { WorkflowsPage } from "./WorkflowsPage";
import type { WorkflowDetail, WorkflowList, WorkflowSummary } from "../api/workflows";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, apiRequest: vi.fn() };
});

vi.mock("../api/queries", () => ({
  agentInstancesQuery: () => ({
    queryKey: ["agents"],
    queryFn: () => Promise.resolve({ items: [{ id: 1, display_name: "Researcher" }] }),
  }),
}));

const { apiRequest } = await import("../api/client");

function summary(overrides: Partial<WorkflowSummary> = {}): WorkflowSummary {
  return {
    id: 1,
    workflow_kind: "research",
    status: "running",
    paused: false,
    step_count: 2,
    checkpoint_revision: 3,
    wait_kind: null,
    signal_key: null,
    decision_key: null,
    wakeup_at: null,
    deadline_at: "2026-10-05T12:00:00+00:00",
    created_at: "2026-10-04T12:00:00+00:00",
    finished_at: null,
    review_reason: null,
    budget: {
      steps_used: 2,
      steps_allowed: 32,
      model_calls_reserved: 4,
      model_calls_remaining: 252,
      tool_calls_reserved: 1,
      tool_calls_remaining: 255,
      output_tokens_reserved: 100,
      output_tokens_remaining: 262044,
    },
    ...overrides,
  };
}

function detail(overrides: Partial<WorkflowDetail> = {}): WorkflowDetail {
  return {
    workflow: summary(),
    steps: [
      {
        step_number: 1,
        run_id: 11,
        status: "succeeded",
        run_status: "succeeded",
        job_phase: null,
        summary: "Collected two sources.",
        expected_checkpoint_revision: 2,
        finished_at: "2026-10-04T12:05:00+00:00",
      },
    ],
    checkpoints: [
      { revision: 3, step_number: 1, byte_size: 42, keys: ["sources"], created_at: null },
    ],
    decisions: [],
    signals: [],
    recovery: {
      needs_attention: false,
      summary: "This workflow is advancing normally.",
      actions: ["pause", "cancel"],
    },
    ...overrides,
  };
}

function renderPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <WorkflowsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.mocked(apiRequest).mockReset();
  // A default so a post-mutation refetch resolves to a real page instead of undefined.
  vi.mocked(apiRequest).mockResolvedValue({
    workflows: [summary()],
    next_before_id: null,
  } as WorkflowList);
});

describe("WorkflowsPage", () => {
  it("delivers a signal with the current checkpoint revision", async () => {
    const waiting = summary({status: "waiting", wait_kind: "signal", signal_key: "go"});
    vi.mocked(apiRequest).mockResolvedValueOnce({workflows: [waiting], next_before_id: null});
    vi.mocked(apiRequest).mockResolvedValueOnce(detail({workflow: waiting}));
    vi.mocked(apiRequest).mockImplementation(async (path) => {
      if (String(path).endsWith("/signals")) return {accepted: true, outcome: "accepted"};
      if (String(path).endsWith("/workflows/1")) return detail({workflow: waiting});
      return {workflows: [waiting], next_before_id: null};
    });
    renderPage();
    await userEvent.click(await screen.findByRole("button", {name: /#1 research/}));
    await userEvent.clear(await screen.findByLabelText("Signal payload (JSON)"));
    fireEvent.change(screen.getByLabelText("Signal payload (JSON)"), {target: {value: "[]"}});
    await userEvent.click(screen.getByRole("button", {name: "Deliver signal"}));
    expect(await screen.findByRole("alert")).toHaveTextContent("JSON object");
    await userEvent.clear(screen.getByLabelText("Signal payload (JSON)"));
    fireEvent.change(screen.getByLabelText("Signal payload (JSON)"), {target: {value: '{"count":3}'}});
    await userEvent.click(screen.getByRole("button", {name: "Deliver signal"}));
    await waitFor(() => expect(vi.mocked(apiRequest).mock.calls.find(c => String(c[0]).endsWith("/signals"))?.[2])
      .toMatchObject({body: {signal_key: "go", payload: {count: 3}, expected_revision: 3}}));
  });

  it("shows private state only after explicit checkpoint inspection", async () => {
    vi.mocked(apiRequest).mockResolvedValueOnce({workflows: [summary()], next_before_id: null});
    vi.mocked(apiRequest).mockResolvedValueOnce(detail());
    vi.mocked(apiRequest).mockResolvedValueOnce({revision: 3, state: {brief: "Private evidence"}});
    renderPage();
    await userEvent.click(await screen.findByRole("button", {name: /#1 research/}));
    expect(screen.queryByText(/Private evidence/)).toBeNull();
    await userEvent.click(await screen.findByRole("button", {name: /Inspect checkpoint/}));
    expect(await screen.findByLabelText("Private checkpoint 3")).toHaveTextContent("Private evidence");
  });

  it("disables lifecycle mutations on terminal workflows", async () => {
    vi.mocked(apiRequest).mockResolvedValueOnce({workflows: [summary({status: "succeeded"})], next_before_id: null});
    renderPage();
    expect(await screen.findByRole("button", {name: "Pause"})).toBeDisabled();
    expect(screen.getByRole("button", {name: "Cancel"})).toBeDisabled();
  });

  it("shows a running workflow with its budget and deadline", async () => {
    vi.mocked(apiRequest).mockResolvedValueOnce({
      workflows: [summary()],
      next_before_id: null,
    } as WorkflowList);

    renderPage();

    expect(await screen.findByText("Running")).toBeTruthy();
    expect(screen.getByText(/Steps 2 \/ 32/)).toBeTruthy();
    expect(screen.getByText(/Pause/)).toBeTruthy();
  });

  it("says what a waiting workflow is waiting for", async () => {
    // A bare "Waiting" leaves an owner unable to tell a deliberate sleep from a stall.
    vi.mocked(apiRequest).mockResolvedValueOnce({
      workflows: [
        summary({ status: "waiting", wait_kind: "owner_decision", decision_key: "send-reply-7" }),
      ],
      next_before_id: null,
    } as WorkflowList);

    renderPage();

    expect(
      await screen.findByText(/Waiting for your decision on "send-reply-7"/),
    ).toBeTruthy();
  });

  it("names the reason a workflow needs review", async () => {
    vi.mocked(apiRequest).mockResolvedValueOnce({
      workflows: [
        summary({ status: "needs_review", review_reason: "deadline_exceeded" }),
      ],
      next_before_id: null,
    } as WorkflowList);

    renderPage();

    expect(await screen.findByText("Needs review")).toBeTruthy();
    expect(screen.getByText(/Reason: deadline_exceeded/)).toBeTruthy();
  });

  it("surfaces recovery guidance for a workflow that needs attention", async () => {
    vi.mocked(apiRequest).mockResolvedValueOnce({
      workflows: [
        summary({ status: "needs_review", review_reason: "deadline_exceeded" }),
      ],
      next_before_id: null,
    } as WorkflowList);
    vi.mocked(apiRequest).mockResolvedValueOnce(
      detail({
        recovery: {
          needs_attention: true,
          summary: "The workflow passed its deadline before it finished.",
          detail: "deadline_exceeded",
          guidance: "Start a new workflow with a longer budget.",
          actions: ["cancel"],
        },
      }) as WorkflowDetail,
    );

    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: /#1 research/ }));

    expect(await screen.findByText(/passed its deadline/)).toBeTruthy();
    expect(screen.getByText(/Start a new workflow with a longer budget/)).toBeTruthy();
  });

  it("shows a checkpoint's shape without showing its contents", async () => {
    vi.mocked(apiRequest).mockResolvedValueOnce({
      workflows: [summary()],
      next_before_id: null,
    } as WorkflowList);
    vi.mocked(apiRequest).mockResolvedValueOnce(detail() as WorkflowDetail);

    renderPage();
    await userEvent.click(await screen.findByRole("button", { name: /#1 research/ }));

    expect(await screen.findByText(/Revision 3 from step 1: 42 bytes, keys sources/)).toBeTruthy();
    expect(screen.getByText(/Checkpoint contents stay on the server/)).toBeTruthy();
  });

  it("pauses and resumes with the action the workflow's state calls for", async () => {
    const user = userEvent.setup();
    vi.mocked(apiRequest).mockResolvedValueOnce({
      workflows: [summary({ paused: true })],
      next_before_id: null,
    } as WorkflowList);

    renderPage();

    const resume = await screen.findByRole("button", { name: "Resume" });
    await user.click(resume);

    await waitFor(() => {
      const pauseCalls = vi
        .mocked(apiRequest)
        .mock.calls.filter((call) => String(call[0]).endsWith("/pause"));
      expect(pauseCalls).toHaveLength(1);
      expect(pauseCalls[0][2]).toMatchObject({ body: { paused: false } });
    });
  });

  it("keeps cancel separate from pausing", async () => {
    const user = userEvent.setup();
    vi.mocked(apiRequest).mockResolvedValueOnce({
      workflows: [summary()],
      next_before_id: null,
    } as WorkflowList);

    renderPage();
    await user.click(await screen.findByRole("button", { name: "Cancel" }));

    await waitFor(() => {
      const calls = vi
        .mocked(apiRequest)
        .mock.calls.filter((call) => String(call[0]).includes("/cancel"));
      expect(calls).toHaveLength(1);
    });
  });

  it("explains an empty workflow list rather than showing a bare blank", async () => {
    vi.mocked(apiRequest).mockResolvedValueOnce({
      workflows: [],
      next_before_id: null,
    } as WorkflowList);

    renderPage();

    expect(await screen.findByText(/No workflows yet/)).toBeTruthy();
  });

  it("requires a submission key before starting a workflow", async () => {
    const user = userEvent.setup();
    vi.mocked(apiRequest).mockResolvedValueOnce({
      workflows: [],
      next_before_id: null,
    } as WorkflowList);

    renderPage();
    await user.click(await screen.findByRole("button", { name: "New workflow" }));
    await user.click(screen.getByRole("button", { name: "Start" }));

    expect(await screen.findByText(/Choose an agent and provide both/)).toBeTruthy();
    // No POST: the form refused before making a request.
    expect(
      vi.mocked(apiRequest).mock.calls.filter((call) => (call[2] as { method?: string })?.method === "POST"),
    ).toHaveLength(0);
  });
});
