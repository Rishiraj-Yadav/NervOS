import { expect, test } from "@playwright/test";

// Browser presentation acceptance uses deterministic catalog responses. Real hosted storage,
// verification, installation and Worker execution are proved by test_publication_journey.py.
test("Marketplace review waits for approval and shows revoked releases", async ({ page }) => {
  const ticket = {id: 7, package_id: "com.acme.browser", package_version: "1.0.0",
    state: "downloaded", error_code: null, expected_archive_sha256: "a".repeat(64),
    expected_content_digest: "b".repeat(64), expected_signer_fingerprint: "c".repeat(64)};
  let approvals = 0;
  await page.route("**/api/v1/marketplace/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    let body: unknown;
    if (path.endsWith("/install")) { approvals += 1; body = {...ticket, state: "installed"}; }
    else if (path.endsWith("/prepare")) body = ticket;
    else if (path.endsWith("/install-requests")) body = {...ticket, state: "created"};
    else if (path.endsWith("/versions/1.0.0")) body = {package_id: ticket.package_id,
      exact_version: "1.0.0", distribution_state: "revoked", status_revision: 2,
      archive_sha256: ticket.expected_archive_sha256, content_digest: ticket.expected_content_digest,
      signer_fingerprint: ticket.expected_signer_fingerprint, size_bytes: 1024};
    else if (path.endsWith("/versions")) body = {items: [{exact_version: "1.0.0",
      distribution_state: "revoked", status_revision: 2}], next_cursor: null};
    else body = {items: [{package_id: ticket.package_id, display_name: "Marketplace browser agent",
      summary: "<script>untrusted listing</script>", latest_stable_version: "1.0.0"}], next_cursor: null};
    await route.fulfill({status: 200, json: body});
  });
  await page.goto("/login");
  await page.getByLabel(/^username$/i).fill("Stage-A.Admin");
  await page.getByLabel(/^password$/i).fill("StageA test password 2026!");
  await page.getByRole("button", {name: /sign in/i}).click();
  await expect(page.getByRole("heading", {name: /nervos is ready/i})).toBeVisible();
  await page.goto("/marketplace");
  await expect(page.getByText("<script>untrusted listing</script>")).toBeVisible();
  await page.getByRole("button", {name: "Review 1.0.0"}).click();
  await expect(page.getByText("Verified com.acme.browser@1.0.0")).toBeVisible();
  expect(approvals).toBe(0);
  await page.getByRole("button", {name: "Approve and install"}).click();
  await expect(page.getByRole("status")).toContainText("Installed.");
  expect(approvals).toBe(1);
  await page.getByRole("button", {name: "Versions and status"}).click();
  await expect(page.getByRole("alert")).toContainText("This release is revoked");
  await expect(page.getByRole("button", {name: "Download and verify 1.0.0"})).toBeDisabled();
});
