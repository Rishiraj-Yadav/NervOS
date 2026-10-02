import { useState } from "react";
import { Link } from "react-router-dom";
import { useMarketplace, useMarketplaceApproval, useMarketplaceInstall, useMarketplaceRelease, useMarketplaceVersions } from "../api/marketplaceQueries";
import { ErrorState, LoadingState } from "../components/AsyncState";
import { Brand } from "../components/Brand";
import { cancelMarketplaceRequest } from "../api/marketplace";

export function MarketplacePage() {
  const [query, setQuery] = useState("");
  const [submitted, setSubmitted] = useState("");
  const marketplace = useMarketplace(submitted);
  const install = useMarketplaceInstall();
  const approval = useMarketplaceApproval();
  const [selectedPackage, setSelectedPackage] = useState<string | null>(null);
  const [selectedVersion, setSelectedVersion] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [cancelError, setCancelError] = useState("");
  const versions = useMarketplaceVersions(selectedPackage);
  const release = useMarketplaceRelease(selectedPackage, selectedVersion);

  return (
    <main className="dashboard-shell">
      <header className="topbar">
        <Brand />
        <Link className="button-link secondary" to="/dashboard">Dashboard</Link>
      </header>
      <section className="dashboard-content" aria-labelledby="marketplace-title">
        <p className="eyebrow">Hosted catalog</p>
        <h1 id="marketplace-title">Marketplace</h1>
        <p className="lede">Discover public agent packages from the configured Marketplace.</p>
        <form
          className="panel flex gap-2"
          onSubmit={(event) => { event.preventDefault(); setSubmitted(query.trim()); }}
        >
          <input
            aria-label="Search Marketplace"
            className="input flex-1"
            value={query}
            maxLength={128}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search packages"
          />
          <button className="button-link primary" type="submit">Search</button>
        </form>
        {marketplace.isPending ? <LoadingState message="Loading Marketplace…" /> : null}
        {marketplace.isError ? <ErrorState error={marketplace.error} onRetry={() => void marketplace.refetch()} /> : null}
        {marketplace.data ? (
          <div className="panel">
            {marketplace.data.items.length === 0 ? <p>No public packages found.</p> : (
              <ul aria-label="Marketplace packages" className="space-y-3">
                {marketplace.data.items.map((item) => (
                  <li key={item.package_id} className="border-b pb-3 last:border-0">
                    <strong>{item.display_name}</strong>
                    <div className="text-sm font-mono">{item.package_id}</div>
                    <p>{item.summary || "No summary provided."}</p>
                    <button className="button-link secondary" type="button" onClick={() => {
                      setSelectedPackage(item.package_id); setSelectedVersion(item.latest_stable_version ?? ""); setAcknowledged(false);
                    }}>Versions and status</button>
                    <div className="flex items-center gap-3">
                      <span className="text-sm">Latest: {item.latest_stable_version ?? "none"}</span>
                      {item.latest_stable_version ? (
                        <button
                          className="button-link primary"
                          type="button"
                          disabled={install.isPending}
                          onClick={() => install.mutate({ packageId: item.package_id, packageVersion: item.latest_stable_version! })}
                        >
                          {install.isPending ? "Verifying…" : `Review ${item.latest_stable_version}`}
                        </button>
                      ) : null}
                    </div>
                    {install.isError ? <p role="alert" className="text-sm">Install failed: {install.error.message}</p> : null}
                    {install.data?.package_id === item.package_id ? (
                      <div className="panel mt-3">
                        <p>Verified {install.data.package_id}@{install.data.package_version}</p>
                        <p className="break-all text-sm">Signer: {install.data.expected_signer_fingerprint}</p>
                        <p className="break-all text-sm">Archive SHA-256: {install.data.expected_archive_sha256}</p>
                        <p className="break-all text-sm">Content digest: {install.data.expected_content_digest}</p>
                        <p>Signature verification does not guarantee that agent code is safe. Approve this exact installation only if you trust its source.</p>
                        <button className="button-link primary" type="button" disabled={approval.isPending || approval.data?.id === install.data.id}
                          onClick={() => approval.mutate(install.data!.id)}>
                          {approval.isPending ? "Installing…" : "Approve and install"}
                        </button>
                        <button className="button-link secondary" type="button" disabled={approval.isPending || approval.data?.id === install.data.id} onClick={() => {
                          setCancelError("");
                          void cancelMarketplaceRequest(install.data!.id).then(() => install.reset()).catch(() => setCancelError("Could not cancel the retained download. Retry cancellation."));
                        }}>Cancel</button>
                        {cancelError ? <p role="alert">{cancelError}</p> : null}
                        {approval.isError ? <p role="alert">{approval.error.message}</p> : null}
                        {approval.data?.id === install.data.id ? <p role="status">Installed. <Link to="/packages">Open Packages</Link> to configure an agent.</p> : null}
                      </div>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </div>
        ) : null}
      </section>
      {selectedPackage ? (
        <section className="panel dashboard-content" aria-label="Exact Marketplace release">
          <h2>{selectedPackage}</h2>
          {versions.isError ? <ErrorState error={versions.error} onRetry={() => void versions.refetch()} /> : null}
          <label>Exact version <select value={selectedVersion} onChange={(event) => {
            setSelectedVersion(event.target.value); setAcknowledged(false);
          }}>
            <option value="">Select a version</option>
            {versions.data?.items.map((item) => <option key={item.exact_version} value={item.exact_version}>
              {item.exact_version} — {item.distribution_state}
            </option>)}
          </select></label>
          {release.isError ? <ErrorState error={release.error} onRetry={() => void release.refetch()} /> : null}
          {release.data ? <>
            <p>Status: {release.data.distribution_state}; revision {release.data.status_revision}</p>
            <p className="break-all">Signer: {release.data.signer_fingerprint}</p>
            <p className="break-all">Archive SHA-256: {release.data.archive_sha256}</p>
            <p>Size: {release.data.size_bytes} bytes</p>
            {release.data.distribution_state === "revoked" ? <p role="alert">This release is revoked. Marketplace download is blocked. Existing local installations and historical Runs are preserved.</p> : null}
            {release.data.distribution_state === "yanked" ? <label>
              <input type="checkbox" checked={acknowledged} onChange={(event) => setAcknowledged(event.target.checked)} />
              This release is yanked. I acknowledge retrieval of this exact archive.
            </label> : null}
            <button className="button-link primary" type="button"
              disabled={install.isPending || release.data.distribution_state === "revoked" || (release.data.distribution_state === "yanked" && !acknowledged)}
              onClick={() => { approval.reset(); install.mutate({packageId: selectedPackage, packageVersion: selectedVersion,
                acknowledgement: release.data!.distribution_state === "yanked" ? release.data!.archive_sha256 : undefined}); }}>
              Download and verify {selectedVersion}
            </button>
          </> : null}
          <button className="button-link secondary" type="button" onClick={() => setSelectedPackage(null)}>Close details</button>
        </section>
      ) : null}
    </main>
  );
}
