import { describe, expect, it } from "vitest";

import {
  RUN_POLL_FAST_WINDOW_COUNT,
  RUN_POLL_INTERVAL_MS,
  RUN_POLL_SLOW_INTERVAL_MS,
  lastAppliedSequence,
  mergeRunEvents,
  runPollCadence,
  runPollInterval,
} from "./queries";
import { apiRun, apiRunEvent } from "../test/agentFixtures";

describe("runPollInterval predicate", () => {
  it("returns false when data is undefined", () => {
    expect(runPollInterval(undefined)).toBe(false);
  });

  it("returns false when the history is empty", () => {
    expect(runPollInterval({ items: [], next_before_id: null })).toBe(false);
  });

  it("returns 2000 ms when at least one run is created", () => {
    const page = {
      items: [
        apiRun({ id: 2, status: "created", output_text: null }),
        apiRun({ id: 1, status: "succeeded" }),
      ],
      next_before_id: null,
    };
    expect(runPollInterval(page)).toBe(RUN_POLL_INTERVAL_MS);
  });

  it("returns 2000 ms when at least one run is running", () => {
    const page = {
      items: [apiRun({ id: 1, status: "running", output_text: null })],
      next_before_id: null,
    };
    expect(runPollInterval(page)).toBe(RUN_POLL_INTERVAL_MS);
  });

  it("returns false when every run is terminal", () => {
    const page = {
      items: [
        apiRun({ id: 2, status: "succeeded" }),
        apiRun({ id: 1, status: "failed", error_code: "model_error" }),
      ],
      next_before_id: null,
    };
    expect(runPollInterval(page)).toBe(false);
  });

  it("keeps observing a live run long past the retired polling budget", () => {
    // C4 bounded polling at 150 updates because an abandoned Run could otherwise be presented as
    // actively progressing. With a durable timeline and a truthful derived phase that risk is
    // gone, so the lifetime is now open-ended and only the cadence widens.
    const page = {
      items: [apiRun({ id: 1, status: "running", output_text: null })],
      next_before_id: null,
    };
    expect(runPollInterval(page, 150)).toBe(RUN_POLL_SLOW_INTERVAL_MS);
    expect(runPollInterval(page, 1500)).toBe(RUN_POLL_SLOW_INTERVAL_MS);
    expect(runPollInterval(page, 150000)).toBe(RUN_POLL_SLOW_INTERVAL_MS);
  });
});

describe("runPollCadence", () => {
  it("stays at 2 seconds through the fast window", () => {
    expect(runPollCadence(0)).toBe(RUN_POLL_INTERVAL_MS);
    expect(runPollCadence(RUN_POLL_FAST_WINDOW_COUNT - 1)).toBe(RUN_POLL_INTERVAL_MS);
  });

  it("widens to 10 seconds afterwards and never stops on its own", () => {
    expect(runPollCadence(RUN_POLL_FAST_WINDOW_COUNT)).toBe(RUN_POLL_SLOW_INTERVAL_MS);
    expect(runPollCadence(RUN_POLL_FAST_WINDOW_COUNT + 1)).toBe(RUN_POLL_SLOW_INTERVAL_MS);
  });
});

describe("runPollInterval stops on cancellation", () => {
  it("treats a cancelled run as terminal", () => {
    // Cancellation is terminal, so polling must stop for it exactly like succeeded/failed.
    expect(
      runPollInterval({
        items: [apiRun({ status: "cancelled" })],
        next_before_id: null,
      }),
    ).toBe(false);
  });

  it("keeps polling while a sibling run is still nonterminal", () => {
    expect(
      runPollInterval({
        items: [apiRun({ status: "cancelled" }), apiRun({ id: 2, status: "running" })],
        next_before_id: null,
      }),
    ).toBe(RUN_POLL_INTERVAL_MS);
  });
});

describe("run timeline merging", () => {
  it("reports the highest applied sequence, and 0 for nothing applied", () => {
    expect(lastAppliedSequence([])).toBe(0);
    expect(lastAppliedSequence([apiRunEvent({ sequence: 3 }), apiRunEvent({ sequence: 7 })])).toBe(
      7,
    );
  });

  it("renders one row when the same sequence is delivered twice", () => {
    const delivered = apiRunEvent({ sequence: 2, event_type: "attempt.started" });
    const merged = mergeRunEvents([delivered], [delivered]);

    expect(merged).toHaveLength(1);
    expect(merged[0].sequence).toBe(2);
  });

  it("cannot move the applied cursor backwards when responses arrive out of order", () => {
    const applied = [
      apiRunEvent({ sequence: 1 }),
      apiRunEvent({ sequence: 2, event_type: "run.queued" }),
      apiRunEvent({ sequence: 3, event_type: "attempt.claimed" }),
    ];
    // A slow earlier response lands after a newer one: it carries only sequences already applied.
    const late = mergeRunEvents(applied, [apiRunEvent({ sequence: 1 }), apiRunEvent({ sequence: 2 })]);

    expect(late.map((event) => event.sequence)).toEqual([1, 2, 3]);
    expect(lastAppliedSequence(late)).toBe(3);
  });

  it("keeps the timeline in ascending sequence order regardless of arrival order", () => {
    const merged = mergeRunEvents(
      [apiRunEvent({ sequence: 1 }), apiRunEvent({ sequence: 3 })],
      [apiRunEvent({ sequence: 4 }), apiRunEvent({ sequence: 2 })],
    );

    expect(merged.map((event) => event.sequence)).toEqual([1, 2, 3, 4]);
  });

  it("never drops an applied event when nothing new arrives", () => {
    const applied = [apiRunEvent({ sequence: 1 }), apiRunEvent({ sequence: 2 })];
    expect(mergeRunEvents(applied, [])).toBe(applied);
  });
});
