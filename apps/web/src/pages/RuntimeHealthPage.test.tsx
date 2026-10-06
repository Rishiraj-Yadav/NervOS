import { screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { RuntimeHealthPage } from "./RuntimeHealthPage";
import { renderWithQueryClient } from "../test/render";
import { server } from "../test/server";

describe("runtime health", () => {
  it("explains fail-closed package execution without disabling built-in agents", async () => {
    server.use(
      http.get("/api/v1/runtime-health", () =>
        HttpResponse.json({
          observed_at: "2026-10-04T10:00:00Z",
          execution_available: true,
          owner_jobs: { queued: 1 },
          package_sandbox: {
            supported: false,
            platform: "windows",
            backend: null,
            reason: "Package isolation is not qualified on this host.",
          },
        }),
      ),
    );

    renderWithQueryClient(<RuntimeHealthPage />);

    const sandboxStatus = await screen.findByText(/package agents are refused on windows/i);
    expect(sandboxStatus).toHaveTextContent(/trusted built-in agents remain available/i);
    expect(screen.getByText("1")).toBeVisible();
  });
});
