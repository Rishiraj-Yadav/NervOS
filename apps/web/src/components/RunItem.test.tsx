import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { apiRun } from "../test/agentFixtures";
import { RunItem } from "./RunItem";

/**
 * C5's cancellation control. The component stays presentational: it renders the control only
 * when the page supplies an action and the Run is still nonterminal, and it owns no request.
 */
describe("RunItem cancellation control", () => {
  it("offers cancellation while the run is queued or running", () => {
    for (const status of ["created", "running"] as const) {
      const { unmount } = render(<RunItem run={apiRun({ status })} onCancel={() => {}} />);
      expect(screen.getByRole("button", { name: /cancel/i })).toBeEnabled();
      unmount();
    }
  });

  it("never offers cancellation for a terminal run", () => {
    for (const status of ["succeeded", "failed", "cancelled"] as const) {
      const { unmount } = render(<RunItem run={apiRun({ status })} onCancel={() => {}} />);
      expect(screen.queryByRole("button", { name: /cancel/i })).toBeNull();
      unmount();
    }
  });

  it("renders no control when the page supplies no action", () => {
    render(<RunItem run={apiRun({ status: "running" })} />);
    expect(screen.queryByRole("button", { name: /cancel/i })).toBeNull();
  });

  it("reports the run it belongs to when clicked", async () => {
    const onCancel = vi.fn();
    const user = userEvent.setup();
    render(<RunItem run={apiRun({ id: 42, status: "running" })} onCancel={onCancel} />);

    await user.click(screen.getByRole("button", { name: /cancel/i }));

    expect(onCancel).toHaveBeenCalledExactlyOnceWith(42);
  });

  it("disables itself while a cancellation is in flight", async () => {
    const onCancel = vi.fn();
    const user = userEvent.setup();
    render(
      <RunItem run={apiRun({ status: "running" })} onCancel={onCancel} isCancelling />,
    );

    const button = screen.getByRole("button", { name: /cancelling/i });
    expect(button).toBeDisabled();
    await user.click(button);
    // A double click must not start a second request.
    expect(onCancel).not.toHaveBeenCalled();
  });

  it("renders a cancelled run without an error, because it carries none", () => {
    render(<RunItem run={apiRun({ status: "cancelled" })} />);

    expect(screen.getByText("Cancelled")).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent(/cancelled/i);
    // The provider may already have processed the request; the copy must not claim otherwise.
    expect(screen.getByRole("note")).toHaveTextContent(/provider/i);
    expect(screen.queryByText(/model_/)).toBeNull();
  });

  it("keeps the cancelled state visible after a reload of the same data", () => {
    const cancelled = apiRun({ status: "cancelled", finished_at: "2026-09-14T00:00:05Z" });
    const { unmount } = render(<RunItem run={cancelled} />);
    expect(screen.getByText("Cancelled")).toBeInTheDocument();
    unmount();

    render(<RunItem run={cancelled} />);
    expect(screen.getByText("Cancelled")).toBeInTheDocument();
  });
});

/**
 * The derived execution phase. A Run waiting to retry and a Run executing now are both `running`,
 * so before C7 the card had to hedge across both cases in one paragraph.
 */
describe("RunItem execution phase copy", () => {
  it("distinguishes waiting to retry from executing, and names the instant", () => {
    render(
      <RunItem
        run={apiRun({
          status: "running",
          output_text: null,
          execution_phase: "retry_wait",
          retry_available_at: "2026-01-01T00:00:04Z",
        })}
      />,
    );

    expect(screen.getByRole("note")).toHaveTextContent(/waiting to retry/i);
    expect(screen.getByRole("note")).toHaveTextContent(/2026-01-01 00:00:04 UTC/);
    // It must not promise the retry will succeed.
    expect(screen.getByRole("note")).toHaveTextContent(/nothing promises/i);
  });

  it("describes a claimed run as claimed but not yet started", () => {
    render(
      <RunItem run={apiRun({ status: "running", output_text: null, execution_phase: "claimed" })} />,
    );

    expect(screen.getByRole("note")).toHaveTextContent(/has claimed this run/i);
    expect(screen.getByRole("note")).toHaveTextContent(/has not yet begun/i);
  });

  it("keeps the ordinary running copy while the run is actually executing", () => {
    render(
      <RunItem run={apiRun({ status: "running", output_text: null, execution_phase: "running" })} />,
    );

    expect(screen.getByRole("note")).toHaveTextContent(/replaying it/i);
    expect(screen.queryByText(/waiting to retry/i)).toBeNull();
  });

  it("renders one note at a time, so the card never contradicts itself", () => {
    for (const phase of ["queued", "claimed", "running", "retry_wait"] as const) {
      const { unmount } = render(
        <RunItem
          run={apiRun({
            status: "running",
            output_text: null,
            execution_phase: phase,
            retry_available_at: phase === "retry_wait" ? "2026-01-01T00:00:04Z" : null,
          })}
        />,
      );
      expect(screen.getAllByRole("note")).toHaveLength(1);
      unmount();
    }
  });
});

describe("RunItem timeline disclosure", () => {
  it("offers no disclosure when the page supplies no timeline", () => {
    render(<RunItem run={apiRun()} />);
    expect(screen.queryByRole("button", { name: /timeline/i })).toBeNull();
  });

  it("reveals the timeline on demand and reports its state", () => {
    const { container } = render(<RunItem run={apiRun({ id: 9 })} timeline={<p>steps</p>} />);
    const toggle = screen.getByRole("button", { name: /show timeline/i });

    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(container.querySelector("#run-timeline-9")).toBeNull();

    return userEvent.click(toggle).then(() => {
      expect(screen.getByRole("button", { name: /hide timeline/i })).toHaveAttribute(
        "aria-expanded",
        "true",
      );
      expect(container.querySelector("#run-timeline-9")).toHaveTextContent("steps");
    });
  });

  it("hides the timeline again without discarding the card", async () => {
    const user = userEvent.setup();
    const { container } = render(<RunItem run={apiRun({ id: 9 })} timeline={<p>steps</p>} />);

    await user.click(screen.getByRole("button", { name: /show timeline/i }));
    await user.click(screen.getByRole("button", { name: /hide timeline/i }));

    expect(container.querySelector("#run-timeline-9")).toBeNull();
    expect(screen.getByRole("button", { name: /show timeline/i })).toBeInTheDocument();
  });
});
