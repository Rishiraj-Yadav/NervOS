import { screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { authenticatedHandler, renderRoute, setupStatusHandler } from "../test/agentFixtures";
import { server } from "../test/server";

const ticket = {
  id: 7, package_id: "com.acme.demo", package_version: "1.0.0", state: "downloaded",
  error_code: null, expected_archive_sha256: "a".repeat(64),
  expected_content_digest: "b".repeat(64), expected_signer_fingerprint: "c".repeat(64),
};

function signedIn() {
  server.use(setupStatusHandler(true), authenticatedHandler(),
    http.get("/api/v1/marketplace/packages", () => HttpResponse.json({items: [{
      package_id: ticket.package_id, display_name: "Demo agent", summary: "<script>untrusted</script>",
      latest_stable_version: "1.0.0",
    }], next_cursor: null})),
    http.post("/api/v1/marketplace/install-requests", () => HttpResponse.json({...ticket, state: "created"})),
    http.post("/api/v1/marketplace/install-requests/7/prepare", () => HttpResponse.json(ticket)),
  );
}

describe("Marketplace installation", () => {
  it("verifies first and waits for separate approval before installing", async () => {
    signedIn();
    let installations = 0;
    server.use(http.post("/api/v1/marketplace/install-requests/7/install", () => {
      installations += 1;
      return HttpResponse.json({...ticket, state: "installed"});
    }));
    const {user} = await renderRoute("/marketplace");
    await user.click(await screen.findByRole("button", {name: "Review 1.0.0"}));
    expect(await screen.findByText(/Verified com.acme.demo@1.0.0/)).toBeVisible();
    expect(installations).toBe(0);
    expect(screen.getByText("<script>untrusted</script>")).toBeVisible();
    expect(document.querySelector("script")).toBeNull();
    await user.click(screen.getByRole("button", {name: "Approve and install"}));
    expect(await screen.findByRole("status")).toHaveTextContent("Installed.");
    expect(installations).toBe(1);
  });

  it("cancels the server ticket and removes the review panel", async () => {
    signedIn();
    let cancellations = 0;
    server.use(http.post("/api/v1/marketplace/install-requests/7/cancel", () => {
      cancellations += 1;
      return HttpResponse.json({...ticket, state: "failed", error_code: "request_cancelled"});
    }));
    const {user} = await renderRoute("/marketplace");
    await user.click(await screen.findByRole("button", {name: "Review 1.0.0"}));
    await user.click(await screen.findByRole("button", {name: "Cancel"}));
    await screen.findByRole("button", {name: "Review 1.0.0"});
    expect(cancellations).toBe(1);
  });
});
