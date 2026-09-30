import { screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import {
  authenticatedHandler,
  renderRoute,
  setupStatusHandler,
} from "../test/agentFixtures";
import { server } from "../test/server";

function signedIn(extra?: { items?: unknown[] }) {
  return [
    setupStatusHandler(true),
    authenticatedHandler(),
    http.get("/api/v1/packages", () =>
      HttpResponse.json({ items: extra?.items ?? [] }),
    ),
  ];
}

describe("packages list page", () => {
  it("shows empty state when no packages are installed", async () => {
    server.use(...signedIn());

    await renderRoute("/packages");

    expect(
      await screen.findByRole("heading", { name: /no packages installed/i }, { timeout: 10_000 }),
    ).toBeVisible();
    expect(screen.getAllByRole("link", { name: /install package/i })[0]).toBeVisible();
  });

  it("lists installed packages with version, status, and fingerprint", async () => {
    server.use(
      ...signedIn({
        items: [
          {
            package_id: "com.acme.invoice",
            package_version: "1.2.3",
            display_name: "Acme Invoice Agent",
            status: "active",
            signer_fingerprint: "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
            content_digest: "sha256:123",
            archive_digest: "sha256:456",
            bound_instances_count: 2,
            installed_at: "2026-09-28T12:00:00Z",
            activated_at: "2026-09-28T12:00:00Z",
            failed_at: null,
            removed_at: null,
            last_error_code: null,
            last_error_message: null,
          },
        ],
      }),
    );

    await renderRoute("/packages");

    expect(await screen.findByText("Acme Invoice Agent")).toBeVisible();
    expect(screen.getByText("com.acme.invoice@1.2.3")).toBeVisible();
    expect(screen.getByText("active")).toBeVisible();
    expect(screen.getByText("2")).toBeVisible();
  });
});
