import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { afterEach, describe, expect, it } from "vitest";

import {
  RUN_POLL_INTERVAL_MS,
  RUN_POLL_SLOW_INTERVAL_MS,
  runEventsQuery,
} from "../api/queries";
import { apiError, apiRunEvent, type ApiRunEvent } from "../test/agentFixtures";
import { renderWithQueryClient } from "../test/render";
import { server } from "../test/server";
import { RunTimeline } from "./RunTimeline";

afterEach(() => {
  server.resetHandlers();
});

interface Page {
  items: ApiRunEvent[];
  next: number | null;
}

/**
 * Stub the Event endpoint by cursor, recording the `after_sequence` of every request so a test can
 * prove a poll is incremental rather than a full refetch -- which is exactly the failure the MSW
 * server's `onUnhandledRequest: "error"` setting could not catch on its own.
 */
function eventServer(pages: Record<number, Page>, requests: number[] = [], runId = 1) {
  return http.get(`/api/v1/runs/${runId}/events`, ({ request }) => {
    const after = Number(new URL(request.url).searchParams.get("after_sequence") ?? "0");
    requests.push(after);
    const page = pages[after];
    if (page === undefined) {
      return HttpResponse.json({ items: [], next_after_sequence: null });
    }
    return HttpResponse.json({
      items: page.items,
      next_after_sequence: page.next,
    });
  });
}

function page(items: ApiRunEvent[], next: number | null = null): Page {
  return { items, next };
}

const FULL_HISTORY: ApiRunEvent[] = [
  apiRunEvent({ sequence: 1, event_type: "run.created" }),
  apiRunEvent({ sequence: 2, event_type: "run.queued" }),
  apiRunEvent({
    sequence: 3,
    event_type: "attempt.claimed",
    attempt_number: 1,
  }),
  apiRunEvent({ sequence: 4, event_type: "attempt.started", attempt_number: 1 }),
  apiRunEvent({ sequence: 5, event_type: "run.succeeded" }),
];

describe("RunTimeline", () => {
  it("renders every event in ascending sequence order", async () => {
    server.use(eventServer({ 0: page([...FULL_HISTORY].reverse()) }));
    renderWithQueryClient(<RunTimeline runId={1} isTerminal />);

    const steps = await screen.findAllByRole("listitem");
    expect(steps.map((step) => step.textContent)).toEqual([
      expect.stringContaining("Run accepted"),
      expect.stringContaining("Queued for a worker"),
      expect.stringContaining("A worker claimed this run"),
      expect.stringContaining("Execution started"),
      expect.stringContaining("Run succeeded"),
    ]);
  });

  it("reports loading until the first page arrives", async () => {
    server.use(eventServer({}));
    renderWithQueryClient(<RunTimeline runId={1} isTerminal={false} />);

    expect(screen.getByText(/loading execution timeline/i)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("list")).toBeNull());
  });

  it("treats an empty timeline as not-yet-read rather than as an empty history", async () => {
    // A Run always has at least two Events, so an empty page can only mean "not read yet".
    server.use(eventServer({ 0: page([]) }));
    renderWithQueryClient(<RunTimeline runId={1} isTerminal />);

    await waitFor(() => expect(screen.queryByRole("list")).toBeNull());
    expect(screen.getByText(/loading execution timeline/i)).toBeInTheDocument();
  });

  it("surfaces a safe API error without rendering the raw failure", async () => {
    server.use(
      http.get("/api/v1/runs/1/events", () =>
        apiError(404, "run_not_found", "The run was not found."),
      ),
    );
    renderWithQueryClient(<RunTimeline runId={1} isTerminal />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The run was not found.");
    expect(alert).not.toHaveTextContent("ApiHttpError");
    expect(alert).not.toHaveTextContent("stack");
  });

  it("drains every page and reports the final terminal event once", async () => {
    const requests: number[] = [];
    server.use(
      eventServer(
        {
          0: page(FULL_HISTORY.slice(0, 4), 4),
          4: page([apiRunEvent({ sequence: 5, event_type: "run.succeeded" })]),
        },
        requests,
      ),
    );
    renderWithQueryClient(<RunTimeline runId={1} isTerminal />);

    const steps = await screen.findAllByRole("listitem");
    expect(requests).toEqual([0, 4]);
    expect(steps.filter((step) => step.textContent?.includes("Run succeeded"))).toHaveLength(1);
    expect(steps).toHaveLength(5);
  });

  it("fetches only what it has not applied, never the whole history again", async () => {
    const requests: number[] = [];
    server.use(
      eventServer(
        {
          0: page([apiRunEvent({ sequence: 1 }), apiRunEvent({ sequence: 2 })]),
          2: page([
            apiRunEvent({ sequence: 3, event_type: "attempt.claimed", attempt_number: 1 }),
          ]),
        },
        requests,
      ),
    );
    const { queryClient } = renderWithQueryClient(<RunTimeline runId={1} isTerminal={false} />);
    await screen.findAllByRole("listitem");

    await queryClient.refetchQueries({ queryKey: ["run-events", 1] });

    expect(requests).toEqual([0, 2]);
    await waitFor(() => expect(screen.getAllByRole("listitem")).toHaveLength(3));
  });

  it("renders one row when the same sequence is delivered twice", async () => {
    const requests: number[] = [];
    const first = apiRunEvent({ sequence: 1 });
    server.use(
      eventServer({ 0: page([first, apiRunEvent({ sequence: 2, event_type: "run.queued" })]) }, requests),
    );
    const { queryClient } = renderWithQueryClient(<RunTimeline runId={1} isTerminal={false} />);
    await screen.findAllByRole("listitem");

    // The server replays sequence 1 and 2: an overlapping or retried response must not double up.
    server.use(
      eventServer({ 2: page([first, apiRunEvent({ sequence: 2, event_type: "run.queued" })]) }, requests),
    );
    await queryClient.refetchQueries({ queryKey: ["run-events", 1] });

    await waitFor(() => expect(screen.getAllByRole("listitem")).toHaveLength(2));
  });

  it("labels attempt-scoped rows only", async () => {
    server.use(
      eventServer({
        0: page([
          apiRunEvent({ sequence: 1, event_type: "run.created" }),
          apiRunEvent({ sequence: 2, event_type: "attempt.claimed", attempt_number: 2 }),
          apiRunEvent({ sequence: 3, event_type: "run.succeeded" }),
        ]),
      }),
    );
    renderWithQueryClient(<RunTimeline runId={1} isTerminal />);

    expect(await screen.findByText("Attempt 2")).toBeInTheDocument();
    expect(screen.getAllByText(/^Attempt /)).toHaveLength(1);
  });

  it("shows a retry failure and the instant it is waiting for", async () => {
    server.use(
      eventServer({
        0: page([
          apiRunEvent({
            sequence: 1,
            event_type: "attempt.failed",
            attempt_number: 1,
            code: "model_rate_limited",
            message: "The model provider is temporarily rate limited.",
          }),
          apiRunEvent({
            sequence: 2,
            event_type: "retry.scheduled",
            attempt_number: 1,
            available_at: "2026-01-01T00:00:04Z",
          }),
        ]),
      }),
    );
    renderWithQueryClient(<RunTimeline runId={1} isTerminal={false} />);

    expect(await screen.findByText("Attempt 1 failed")).toBeInTheDocument();
    expect(screen.getByText(/temporarily rate limited/)).toBeInTheDocument();
    expect(screen.getByText("Retry scheduled")).toBeInTheDocument();
    expect(screen.getByText(/Waiting until 2026-01-01 00:00:04 UTC/)).toBeInTheDocument();
  });

  it("distinguishes recovery before execution from an ambiguous outcome, and blames no worker", async () => {
    server.use(
      eventServer({
        0: page([
          apiRunEvent({ sequence: 1, event_type: "attempt.expired", attempt_number: 1 }),
          apiRunEvent({ sequence: 2, event_type: "recovery.pre_start" }),
          apiRunEvent({ sequence: 3, event_type: "attempt.expired", attempt_number: 2 }),
          apiRunEvent({
            sequence: 4,
            event_type: "recovery.ambiguous",
            code: "execution_outcome_ambiguous",
            message: "The attempt's outcome could not be determined, so it was closed as failed.",
          }),
        ]),
      }),
    );
    const { container } = renderWithQueryClient(<RunTimeline runId={1} isTerminal />);

    expect(await screen.findByText("Recovered before execution began")).toBeInTheDocument();
    expect(screen.getByText("Execution outcome unknown")).toBeInTheDocument();
    expect(screen.getByText(/could not be determined/)).toBeInTheDocument();
    // A stale registry row and a paused process are indistinguishable, so no copy may assert one.
    const text = container.textContent ?? "";
    expect(text).not.toMatch(/crashed/i);
    expect(text).not.toMatch(/\bdied\b/i);
    expect(text).not.toMatch(/was killed/i);
  });

  it("keeps the limit of cancellation honest and calls it a cancellation", async () => {
    server.use(
      eventServer({
        0: page([
          apiRunEvent({ sequence: 1, event_type: "cancellation.requested" }),
          apiRunEvent({ sequence: 2, event_type: "run.cancelled" }),
        ]),
      }),
    );
    const { container } = renderWithQueryClient(<RunTimeline runId={1} isTerminal />);

    expect(await screen.findByText("Cancellation requested")).toBeInTheDocument();
    expect(screen.getByText("Run cancelled")).toBeInTheDocument();
    expect(
      screen.getByText(/A request already sent may still have been processed/),
    ).toBeInTheDocument();
    // No attempt.cancelled event exists, and the timeline must not fabricate one.
    expect(container.textContent).not.toMatch(/attempt cancelled/i);
  });

  it("never presents a timeout as a cancellation", async () => {
    server.use(
      eventServer({
        0: page([
          apiRunEvent({ sequence: 1, event_type: "attempt.started", attempt_number: 1 }),
          apiRunEvent({
            sequence: 2,
            event_type: "run.failed",
            code: "model_timed_out",
            message: "The model request exceeded its time limit.",
          }),
        ]),
      }),
    );
    const { container } = renderWithQueryClient(<RunTimeline runId={1} isTerminal />);

    expect(await screen.findByText("Run failed")).toBeInTheDocument();
    expect(screen.getByText(/exceeded its time limit/)).toBeInTheDocument();
    expect(screen.getByText(/was not retried/)).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/cancel/i);
  });

  it("renders a success and a plain failure without borrowing either's wording", async () => {
    server.use(
      eventServer({
        0: page([
          apiRunEvent({ sequence: 1, event_type: "run.succeeded" }),
          apiRunEvent({
            sequence: 2,
            event_type: "run.failed",
            code: "worker_recovery_exhausted",
            message: "NervOS stopped recovering this run.",
          }),
        ]),
      }),
    );
    renderWithQueryClient(<RunTimeline runId={1} isTerminal />);

    expect(await screen.findByText("Run succeeded")).toBeInTheDocument();
    expect(screen.getByText("Run failed")).toBeInTheDocument();
    expect(screen.getByText(/stopped recovering this run/)).toBeInTheDocument();
  });

  it("rebuilds the same timeline from the server after a reload", async () => {
    server.use(eventServer({ 0: page(FULL_HISTORY) }));
    const first = renderWithQueryClient(<RunTimeline runId={1} isTerminal />);
    const before = (await screen.findAllByRole("listitem")).map((step) => step.textContent);
    first.unmount();

    server.resetHandlers();
    server.use(eventServer({ 0: page(FULL_HISTORY) }));
    renderWithQueryClient(<RunTimeline runId={1} isTerminal />);

    // Nothing is carried over in the client: the durable history is the only authority.
    expect((await screen.findAllByRole("listitem")).map((step) => step.textContent)).toEqual(before);
  });

  it("renders no internal execution identifier, whatever the server sends", async () => {
    const adversarial = {
      ...apiRunEvent({ sequence: 1, event_type: "attempt.claimed", attempt_number: 1 }),
      // A future column must not leak by default: the projection is an allow-list, not the row.
      claim_token: "sk-test-claim-token-value",
      worker_id: "worker-secret-7",
      lease_expires_at: "2026-01-01T00:05:00Z",
      last_served_attempt_id: 99,
      active_count: 3,
      pending_count: 8,
      job_id: 41,
      attempt_id: 17,
      id: 1234,
    };
    server.use(
      http.get("/api/v1/runs/1/events", () =>
        HttpResponse.json({ items: [adversarial], next_after_sequence: null }),
      ),
    );
    const { container } = renderWithQueryClient(<RunTimeline runId={1} isTerminal />);
    await screen.findByRole("listitem");

    const text = container.textContent ?? "";
    for (const leaked of [
      "sk-test-claim-token-value",
      "worker-secret-7",
      "last_served_attempt_id",
      "active_count",
      "pending_count",
      "99",
    ]) {
      expect(text).not.toContain(leaked);
    }
    expect(container.innerHTML).not.toContain("claim_token");
    expect(container.innerHTML).not.toContain("worker_id");
  });

  it("lets the reader refresh a timeline on demand", async () => {
    const requests: number[] = [];
    server.use(eventServer({ 0: page(FULL_HISTORY) }, requests));
    renderWithQueryClient(<RunTimeline runId={1} isTerminal />);
    await screen.findAllByRole("listitem");

    await userEvent.click(screen.getByRole("button", { name: /refresh timeline/i }));

    // The refresh is incremental too: only the unseen tail is requested.
    await waitFor(() => expect(requests).toEqual([0, 5]));
  });
});

describe("RunTimeline polling lifetime", () => {
  const decide = (isTerminal: boolean, dataUpdateCount: number): number | false => {
    const interval = runEventsQuery(1, isTerminal).refetchInterval;
    if (typeof interval !== "function") {
      return false;
    }
    // Only `state.dataUpdateCount` is read by the predicate; a full Query would be noise here.
    const probe = { state: { dataUpdateCount } } as unknown as Parameters<typeof interval>[0];
    return interval(probe) as number | false;
  };

  it("keeps observing a live run past the retired polling budget", () => {
    expect(decide(false, 0)).toBe(RUN_POLL_INTERVAL_MS);
    expect(decide(false, 150)).toBe(RUN_POLL_SLOW_INTERVAL_MS);
    expect(decide(false, 5000)).toBe(RUN_POLL_SLOW_INTERVAL_MS);
  });

  it("stops the timer permanently for a terminal run", () => {
    expect(decide(true, 0)).toBe(false);
    expect(decide(true, 5000)).toBe(false);
  });
});
