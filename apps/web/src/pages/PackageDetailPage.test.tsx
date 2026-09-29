import { screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import {
  authenticatedHandler,
  renderRoute,
  setupStatusHandler,
} from "../test/agentFixtures";
import { server } from "../test/server";

function signedIn() {
  return [
    setupStatusHandler(true),
    authenticatedHandler(),
    http.get("/api/v1/packages/com.acme.invoice/versions/1.2.3", () =>
      HttpResponse.json({
        package_id: "com.acme.invoice",
        package_version: "1.2.3",
        display_name: "Acme Invoice Agent",
        status: "active",
        signer_fingerprint: "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
        content_digest: "sha256:123",
        archive_digest: "sha256:456",
        manifest_version: "1",
        min_nervos_version: "0.1.0",
        max_nervos_version: "0.1.0",
        is_compatible: true,
        entrypoint_module: "agent",
        entrypoint_object: "InvoiceAgent",
        tools_required: [],
        tools_optional: [],
        memory_declarations: [],
        trigger_declarations: [],
        config_schema: { type: "object" },
        resource_limits: {},
        bound_instances_count: 1,
        environment_id: 1,
        environment_status: "ready",
        installed_at: "2026-09-28T12:00:00Z",
        activated_at: "2026-09-28T12:00:00Z",
        failed_at: null,
        removed_at: null,
        last_error_code: null,
        last_error_message: null,
      }),
    ),
    http.get("/api/v1/packages/com.acme.invoice/versions/1.2.3/removal-plan", () =>
      HttpResponse.json({
        package_id: "com.acme.invoice",
        package_version: "1.2.3",
        status: "active",
        bound_instance_ids: [1],
        bound_instances_count: 1,
        nonterminal_run_ids: [],
        nonterminal_runs_count: 0,
        is_environment_shared: false,
        can_remove_immediately: false,
        can_begin_removal: false,
        blocking_reasons: ["1 agent instance(s) are bound to this package version."],
      }),
    ),
  ];
}

describe("package detail page", () => {
  it("displays package verification metadata", async () => {
    server.use(...signedIn());

    await renderRoute("/packages/com.acme.invoice/1.2.3");

    expect(await screen.findByRole("heading", { name: "Acme Invoice Agent" })).toBeVisible();
    expect(screen.getByText("com.acme.invoice@1.2.3")).toBeVisible();
    expect(screen.getByText("a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2")).toBeVisible();
  });

  it("opens removal plan modal and displays blockers", async () => {
    server.use(...signedIn());

    const { user } = await renderRoute("/packages/com.acme.invoice/1.2.3");

    const uninstallBtn = await screen.findByRole("button", { name: /uninstall/i });
    await user.click(uninstallBtn);

    expect(await screen.findByText(/1 agent instance\(s\) are bound to this package version/i)).toBeVisible();
    expect(screen.getByRole("button", { name: /confirm uninstall/i })).toBeDisabled();
  });
});
