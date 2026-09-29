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
  ];
}

describe("package install page", () => {
  it("renders upload input and pre-H disclaimer", async () => {
    server.use(...signedIn());

    await renderRoute("/packages/install");

    expect(await screen.findByRole("heading", { name: /install agent package/i })).toBeVisible();
    expect(screen.getByLabelText(/select \.nervos package file/i)).toBeVisible();
  });

  it("inspects file and displays verified signer fingerprint", async () => {
    server.use(
      ...signedIn(),
      http.post("/api/v1/packages/inspect", () =>
        HttpResponse.json({
          package_id: "com.acme.invoice",
          package_version: "1.2.3",
          display_name: "Acme Invoice Agent",
          manifest_version: "1",
          signer_fingerprint: "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
          content_digest: "sha256:123",
          archive_digest: "sha256:456",
          min_nervos_version: "0.1.0",
          max_nervos_version: "0.1.0",
          is_compatible: true,
          entrypoint_module: "agent",
          entrypoint_object: "InvoiceAgent",
          tools_required: [],
          tools_optional: [],
          memory_declarations: [],
          trigger_declarations: [],
          config_schema: {},
          resource_limits: {},
        }),
      ),
    );

    const { user } = await renderRoute("/packages/install");

    const file = new File(["dummy"], "agent.nervos", { type: "application/octet-stream" });
    const input = await screen.findByLabelText(/select \.nervos package file/i);
    await user.upload(input, file);

    expect(await screen.findByText("Signature Verified (Ed25519)")).toBeVisible();
    expect(screen.getByText("a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2")).toBeVisible();
    expect(screen.getByText(/Pre-Stage-H Isolation Notice/i)).toBeVisible();
    expect(screen.getByRole("button", { name: /authorize & install/i })).toBeEnabled();
  });
});
