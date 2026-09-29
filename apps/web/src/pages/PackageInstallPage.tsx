import React, { useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { useInspectPackage, useInstallPackage } from "../api/packageQueries";
import type { PackageInspection } from "../api/packages";
import { InlineError } from "../components/AsyncState";
import { Brand } from "../components/Brand";

export function PackageInstallPage() {
  const [file, setFile] = useState<File | null>(null);
  const [inspected, setInspected] = useState<PackageInspection | null>(null);
  const [isCopied, setIsCopied] = useState(false);

  const inspectMutation = useInspectPackage();
  const installMutation = useInstallPackage();
  const navigate = useNavigate();

  async function handleFileChange(event: React.ChangeEvent<HTMLInputElement>) {
    const selected = event.target.files?.[0];
    if (!selected) return;
    setFile(selected);
    setInspected(null);
    try {
      const data = await inspectMutation.mutateAsync(selected);
      setInspected(data);
    } catch {
      // Handled via mutation error state
    }
  }

  async function handleInstall() {
    if (!file || !inspected) return;
    try {
      await installMutation.mutateAsync({
        file,
        auth: {
          package_id: inspected.package_id,
          package_version: inspected.package_version,
          content_digest: inspected.content_digest,
          signer_fingerprint: inspected.signer_fingerprint,
          archive_digest: inspected.archive_digest,
        },
      });
      navigate(`/packages/${encodeURIComponent(inspected.package_id)}/${encodeURIComponent(inspected.package_version)}`);
    } catch {
      // Handled via mutation error state
    }
  }

  function handleCopyFingerprint() {
    if (!inspected) return;
    void navigator.clipboard.writeText(inspected.signer_fingerprint);
    setIsCopied(true);
    setTimeout(() => setIsCopied(false), 2000);
  }

  return (
    <main className="dashboard-shell">
      <header className="topbar">
        <Brand />
        <div className="account-actions">
          <Link className="button-link secondary" to="/packages">
            Packages
          </Link>
          <Link className="button-link secondary" to="/dashboard">
            Dashboard
          </Link>
        </div>
      </header>

      <section className="dashboard-content" aria-labelledby="install-title">
        <p className="eyebrow">Local installation</p>
        <h1 id="install-title">Install Agent Package</h1>
        <p className="lede">
          Select a <code>.nervos</code> archive to inspect its verified cryptographic identity and authorize installation.
        </p>

        <div className="panel" style={{ marginBottom: "1.5rem" }}>
          <label htmlFor="package-file" className="block text-sm font-medium text-gray-900 dark:text-gray-100 mb-2">
            Select <code>.nervos</code> package file
          </label>
          <input
            id="package-file"
            type="file"
            accept=".nervos"
            onChange={(e) => void handleFileChange(e)}
            disabled={inspectMutation.isPending || installMutation.isPending}
            className="block w-full text-sm text-gray-500 file:mr-4 file:py-2 file:px-4 file:rounded file:border-0 file:text-sm file:font-semibold file:bg-blue-50 file:text-blue-700 hover:file:bg-blue-100"
          />
          {inspectMutation.isPending && <p className="text-sm text-gray-500 mt-2">Verifying package archive and signature…</p>}
          {inspectMutation.error && <InlineError error={inspectMutation.error} />}
        </div>

        {inspected && (
          <div className="space-y-6">
            <div className="panel space-y-4">
              <div className="flex items-center justify-between border-b pb-3 dark:border-gray-700">
                <div>
                  <h2 className="text-lg font-bold text-gray-900 dark:text-gray-100">{inspected.display_name}</h2>
                  <p className="font-mono text-xs text-gray-500">{inspected.package_id}@{inspected.package_version}</p>
                </div>
                <span className="inline-flex items-center px-2.5 py-0.5 rounded-full text-xs font-medium bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200">
                  Signature Verified (Ed25519)
                </span>
              </div>

              <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-sm">
                <div>
                  <span className="font-semibold text-gray-700 dark:text-gray-300">Signer Fingerprint:</span>
                  <div className="flex items-center space-x-2 mt-1">
                    <span className="font-mono text-xs p-1 bg-gray-100 dark:bg-gray-800 rounded break-all select-all">
                      {inspected.signer_fingerprint}
                    </span>
                    <button
                      type="button"
                      onClick={handleCopyFingerprint}
                      className="text-xs px-2 py-1 bg-gray-200 dark:bg-gray-700 rounded hover:bg-gray-300"
                    >
                      {isCopied ? "Copied" : "Copy"}
                    </button>
                  </div>
                </div>

                <div>
                  <span className="font-semibold text-gray-700 dark:text-gray-300">Content Digest:</span>
                  <p className="font-mono text-xs p-1 bg-gray-100 dark:bg-gray-800 rounded break-all mt-1">
                    {inspected.content_digest}
                  </p>
                </div>

                <div>
                  <span className="font-semibold text-gray-700 dark:text-gray-300">NervOS Compatibility:</span>
                  <p className="text-xs mt-1">
                    {inspected.is_compatible ? (
                      <span className="text-green-600 dark:text-green-400 font-medium">Compatible with current NervOS</span>
                    ) : (
                      <span className="text-red-600 dark:text-red-400 font-medium">Incompatible</span>
                    )}
                  </p>
                </div>

                <div>
                  <span className="font-semibold text-gray-700 dark:text-gray-300">Entrypoint:</span>
                  <p className="font-mono text-xs mt-1">
                    {inspected.entrypoint_module}:{inspected.entrypoint_object}
                  </p>
                </div>
              </div>

              <div className="p-3 bg-amber-50 dark:bg-amber-950/40 border border-amber-200 dark:border-amber-800 rounded text-xs text-amber-800 dark:text-amber-200">
                <strong>Pre-Stage-H Isolation Notice:</strong> Installing this package prepares an isolated Python environment. Third-party packages run with explicit operator trust. Full hostile-code containment and secret management are part of Stage H.
              </div>

              {installMutation.error && <InlineError error={installMutation.error} />}

              <div className="pt-2 flex justify-end space-x-3">
                <Link to="/packages" className="button-link secondary">
                  Cancel
                </Link>
                <button
                  type="button"
                  onClick={() => void handleInstall()}
                  disabled={!inspected.is_compatible || installMutation.isPending}
                  className="button primary"
                >
                  {installMutation.isPending ? "Installing…" : `Authorize & Install ${inspected.package_id}@${inspected.package_version}`}
                </button>
              </div>
            </div>
          </div>
        )}
      </section>
    </main>
  );
}
