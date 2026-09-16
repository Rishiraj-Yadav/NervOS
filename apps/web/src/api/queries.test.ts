import { describe, expect, it } from "vitest";

import {
  MAX_NONTERMINAL_POLL_COUNT,
  RUN_POLL_INTERVAL_MS,
  runPollInterval,
} from "./queries";
import { apiRun } from "../test/agentFixtures";

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

  it("stops polling when the nonterminal poll budget is exhausted", () => {
    const page = {
      items: [apiRun({ id: 1, status: "running", output_text: null })],
      next_before_id: null,
    };
    expect(runPollInterval(page, MAX_NONTERMINAL_POLL_COUNT - 1)).toBe(RUN_POLL_INTERVAL_MS);
    expect(runPollInterval(page, MAX_NONTERMINAL_POLL_COUNT)).toBe(false);
    expect(runPollInterval(page, MAX_NONTERMINAL_POLL_COUNT + 10)).toBe(false);
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
