import { apiRequest } from "./client";

export type MarketplacePackage = {
  package_id: string;
  display_name: string;
  summary: string;
  latest_stable_version: string | null;
};

export type MarketplacePage = {
  items: MarketplacePackage[];
  next_cursor: string | null;
};

export type MarketplaceInstallRequest = {
  id: number;
  package_id: string;
  package_version: string;
  state: "created" | "downloaded" | "approved" | "installed" | "failed";
  error_code: string | null;
  expected_signer_fingerprint: string;
  expected_archive_sha256: string;
  expected_content_digest: string;
};

export type MarketplaceVersion = {
  exact_version: string;
  distribution_state: "available" | "yanked" | "revoked";
  status_revision: number;
};
export type MarketplaceRelease = MarketplaceVersion & {
  package_id: string;
  archive_sha256: string;
  content_digest: string;
  signer_fingerprint: string;
  size_bytes: number;
};

function isVersion(value: unknown): value is MarketplaceVersion {
  if (typeof value !== "object" || value === null) return false;
  const item = value as Record<string, unknown>;
  return typeof item.exact_version === "string" && typeof item.status_revision === "number" &&
    ["available", "yanked", "revoked"].includes(String(item.distribution_state));
}

function isVersions(value: unknown): value is {items: MarketplaceVersion[]; next_cursor: string | null} {
  if (typeof value !== "object" || value === null) return false;
  const item = value as Record<string, unknown>;
  return Array.isArray(item.items) && item.items.every(isVersion) &&
    (item.next_cursor === null || typeof item.next_cursor === "string");
}

function isRelease(value: unknown): value is MarketplaceRelease {
  if (!isVersion(value)) return false;
  const item = value as unknown as Record<string, unknown>;
  return ["package_id", "archive_sha256", "content_digest", "signer_fingerprint"].every(
    (key) => typeof item[key] === "string"
  ) && typeof item.size_bytes === "number";
}

export function marketplaceVersions(packageId: string) {
  return apiRequest(`/marketplace/packages/${encodeURIComponent(packageId)}/versions`, isVersions);
}

export function marketplaceRelease(packageId: string, version: string) {
  return apiRequest(`/marketplace/packages/${encodeURIComponent(packageId)}/versions/${encodeURIComponent(version)}`, isRelease);
}

function isPage(value: unknown): value is MarketplacePage {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as { items?: unknown; next_cursor?: unknown };
  return Array.isArray(candidate.items) && candidate.items.every((item: unknown) => {
    if (typeof item !== "object" || item === null) return false;
    const row = item as Record<string, unknown>;
    return ["package_id", "display_name", "summary"].every((key) => typeof row[key] === "string") &&
      (row.latest_stable_version === null || typeof row.latest_stable_version === "string");
  }) && (candidate.next_cursor === null || typeof candidate.next_cursor === "string");
}

export function searchMarketplace(query = "", cursor?: string) {
  const params = new URLSearchParams({ q: query, limit: "20" });
  if (cursor) params.set("cursor", cursor);
  return apiRequest<MarketplacePage>(`/marketplace/packages?${params.toString()}`, isPage);
}

function isInstallRequest(value: unknown): value is MarketplaceInstallRequest {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return typeof candidate.id === "number" && typeof candidate.package_id === "string" &&
    typeof candidate.package_version === "string" && ["created", "downloaded", "approved", "installed", "failed"].includes(String(candidate.state)) &&
    ["expected_signer_fingerprint", "expected_archive_sha256", "expected_content_digest"].every(
      (key) => typeof candidate[key] === "string" && /^[0-9a-f]{64}$/.test(candidate[key] as string)
    );
}

export function createMarketplaceInstallRequest(packageId: string, packageVersion: string, acknowledgement?: string) {
  return apiRequest<MarketplaceInstallRequest>("/marketplace/install-requests", isInstallRequest, {
    method: "POST",
    body: { package_id: packageId, package_version: packageVersion, acknowledgement },
  });
}

export function installMarketplaceRequest(requestId: number) {
  return apiRequest<MarketplaceInstallRequest>(`/marketplace/install-requests/${requestId}/install`, isInstallRequest, {
    method: "POST",
  });
}

export function prepareMarketplaceRequest(requestId: number) {
  return apiRequest<MarketplaceInstallRequest>(`/marketplace/install-requests/${requestId}/prepare`, isInstallRequest, {
    method: "POST",
  });
}

export function cancelMarketplaceRequest(requestId: number) {
  return apiRequest<MarketplaceInstallRequest>(`/marketplace/install-requests/${requestId}/cancel`, isInstallRequest, {
    method: "POST",
  });
}
