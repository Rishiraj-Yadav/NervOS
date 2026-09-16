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
